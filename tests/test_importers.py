"""Knowledge Base 1.0 -- Phase 2 (TXT + Markdown Importer) tests.

The 20 required behaviours (8 TXT, 5 Markdown, 7 integration) plus the boundary cases
the spec calls out (size limit, unsupported extension, metadata privacy) and the
architectural guard rails (the importer touches no SQLite and owns no model client).

Everything runs offline with a Mock LLM; the only file that needs a real credential is
the acceptance script, not this suite.
"""

from __future__ import annotations

import codecs
import inspect
import json
import pathlib
import unittest

import personal_memory.importers.files
import personal_memory.importers.markdown
from personal_memory import (
    CaptureService,
    Database,
    EmptyFileError,
    FileEncodingError,
    FileImporter,
    FileMissingError,
    FileTooLargeError,
    ImportedDocument,
    LLMRequestError,
    MemoryFormationService,
    MemoryQualityGate,
    MemoryRepository,
    MemoryRetriever,
    NotAFileError,
    UnsupportedFileTypeError,
    ValidationError,
    clean_markdown,
    compute_content_hash,
    import_file,
)
from personal_memory.errors import MemorySystemError

from .helpers import RepositoryTestCase
from .llm_fakes import mock_client, response_for
from .test_quality import draft_payload, formation_payload

LOW_VALUE_PAYLOAD = {"worth_remembering": False, "reason": "一次性的临时状态", "memories": []}


class ImporterTestCase(RepositoryTestCase):
    prefix = "pms-import-"

    def write(self, name: str, text: str, *, encoding: str = "utf-8") -> pathlib.Path:
        path = self.tmpdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        return path

    def write_bytes(self, name: str, data: bytes) -> pathlib.Path:
        path = self.tmpdir / name
        path.write_bytes(data)
        return path

    def capture_service(self, *items, quality: bool = False):
        """A CaptureService wired to a scripted (offline) formation."""
        client = mock_client(*items)
        formation = MemoryFormationService(
            self.repo, client, quality=MemoryQualityGate(self.repo) if quality else None
        )
        return CaptureService(formation, captured_from="file"), client

    def counts(self) -> dict[str, int]:
        return self.repo.counts()


# --------------------------------------------------------------------------
# TXT (1-8)
# --------------------------------------------------------------------------


class TxtImporterTest(ImporterTestCase):
    def test_1_normal_txt_import(self) -> None:
        path = self.write("notes.txt", "RAG 通过检索外部知识提供上下文。\n第二段。\n")

        document = FileImporter().load(path)

        self.assertIsInstance(document, ImportedDocument)
        self.assertEqual(document.filename, "notes.txt")
        self.assertEqual(document.extension, ".txt")
        self.assertEqual(document.content, "RAG 通过检索外部知识提供上下文。\n第二段。")
        self.assertEqual(document.metadata["source_format"], "text")
        self.assertEqual(document.metadata["captured_from"], "file")
        self.assertEqual(document.size_bytes, path.stat().st_size)

    def test_2_utf8_is_read_first_and_chinese_survives(self) -> None:
        plain = FileImporter().load(self.write("plain.txt", "中文内容。"))
        self.assertEqual(plain.content, "中文内容。")
        self.assertEqual(plain.metadata["encoding"], "utf-8")

        bom = FileImporter().load(self.write_bytes("bom.txt", codecs.BOM_UTF8 + "带 BOM 的中文。".encode("utf-8")))
        self.assertEqual(bom.content, "带 BOM 的中文。")
        self.assertEqual(bom.metadata["encoding"], "utf-8-sig")

        crlf = FileImporter().load(self.write_bytes("crlf.txt", "第一行\r\n第二行\r\n".encode("utf-8")))
        self.assertEqual(crlf.content, "第一行\n第二行")  # line endings normalised

    def test_3_empty_txt_is_rejected(self) -> None:
        for index, body in enumerate(("", "   \n\n\t ", "\ufeff")):
            with self.subTest(body=repr(body)):
                path = self.write(f"empty-{index}.txt", body)
                with self.assertRaises(EmptyFileError) as caught:
                    FileImporter().load(path)
                self.assertIn("empty", str(caught.exception))
        self.assertEqual(self.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_4_missing_file_is_rejected(self) -> None:
        with self.assertRaises(FileMissingError) as caught:
            FileImporter().load(self.tmpdir / "does-not-exist.txt")
        self.assertIn("file not found", str(caught.exception))
        self.assertTrue(str(caught.exception).endswith("does-not-exist.txt"))

    def test_5_directory_is_rejected(self) -> None:
        directory = self.tmpdir / "looks-like-a-file.txt"
        directory.mkdir()
        with self.assertRaises(NotAFileError):
            FileImporter().load(directory)
        with self.assertRaises(NotAFileError):
            FileImporter().load(self.tmpdir)  # a directory without a supported extension

    def test_6_non_utf8_encoding_fails_with_a_clear_error(self) -> None:
        gbk = self.write_bytes("gbk.txt", "中文 GBK 内容".encode("gbk"))
        with self.assertRaises(FileEncodingError) as caught:
            FileImporter().load(gbk)
        self.assertEqual(caught.exception.encoding, "utf-8")
        self.assertIn("could not decode", str(caught.exception))
        self.assertIn("convert the file to UTF-8", str(caught.exception))

        utf16 = self.write_bytes("utf16.txt", "中文内容".encode("utf-16"))
        with self.assertRaises(FileEncodingError) as caught16:
            FileImporter().load(utf16)
        self.assertEqual(caught16.exception.encoding, "utf-16")
        self.assertIn("byte-order mark", str(caught16.exception))

        self.assertEqual(self.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_7_title_defaults_to_the_file_name(self) -> None:
        path = self.write("rag-notes.txt", "正文没有标题行。")

        document = FileImporter().load(path)
        self.assertEqual(document.title, "rag-notes")
        self.assertEqual(document.title_source, "filename")

        explicit = FileImporter().load(path, title="  显式标题  ")
        self.assertEqual(explicit.title, "显式标题")
        self.assertEqual(explicit.title_source, "explicit")

    def test_8_source_type_is_file(self) -> None:
        path = self.write("notes.txt", "正文内容。")
        request = FileImporter().load(path).to_capture_request()
        self.assertEqual(str(request.source_type), "file")
        self.assertIsNone(request.url)

        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="正文内容"))
        service, _ = self.capture_service(response_for(payload))
        result = FileImporter().import_file(path, service)
        self.assertEqual(result.source_count, 1)
        self.assertEqual(str(result.sources_created[0].source_type), "file")
        self.assertEqual(result.sources_created[0].metadata["filename"], "notes.txt")
        self.assertEqual(result.sources_created[0].metadata["extension"], ".txt")


# --------------------------------------------------------------------------
# Markdown (9-13)
# --------------------------------------------------------------------------


class MarkdownImporterTest(ImporterTestCase):
    def test_9_normal_markdown_import(self) -> None:
        path = self.write(
            "rag.md",
            "# RAG 基础\n\nRAG 是检索增强生成。\n\n## 原理\n\n先检索，再生成。\n\n- 要点一\n- 要点二\n",
        )

        document = FileImporter().load(path)

        self.assertEqual(document.extension, ".md")
        self.assertEqual(document.metadata["source_format"], "markdown")
        self.assertIn("RAG 是检索增强生成。", document.content)
        self.assertIn("## 原理", document.content)  # headings kept, markers included
        self.assertIn("- 要点二", document.content)  # list text kept

    def test_10_h1_heading_becomes_the_title(self) -> None:
        path = self.write("notes.md", "前言一句。\n\n# RAG 基础\n\n正文。\n")

        document = FileImporter().load(path)

        self.assertEqual(document.title, "RAG 基础")
        self.assertEqual(document.title_source, "heading")
        self.assertIn("# RAG 基础", document.content)  # preserved, not deleted

    def test_11_without_h1_the_file_name_is_used_and_front_matter_is_a_fallback(self) -> None:
        plain = self.write("my-notes.md", "没有一级标题，只有正文。\n\n### 三级标题\n")
        document = FileImporter().load(plain)
        self.assertEqual(document.title, "my-notes")
        self.assertEqual(document.title_source, "filename")

        front = self.write("front.md", "---\ntitle: 来自 front matter\ntags: [rag]\n---\n\n只有正文。\n")
        document_front = FileImporter().load(front)
        self.assertEqual(document_front.title, "来自 front matter")
        self.assertEqual(document_front.title_source, "front_matter")
        self.assertIn("tags: [rag]", document_front.content)  # front matter is kept, not deleted

    def test_12_markdown_body_reaches_capture(self) -> None:
        path = self.write(
            "rag.md",
            "# RAG 基础\n\nRAG 通过检索外部知识为模型提供上下文。\n\n- 要点一\n- 要点二\n",
        )
        service, client = self.capture_service(response_for(formation_payload(draft_payload())))

        result = FileImporter().import_file(path, service)

        prompt_text = client.transport.requests[0].messages[-1].content
        self.assertIn("RAG 通过检索外部知识为模型提供上下文。", prompt_text)
        self.assertIn("- 要点一", prompt_text)
        self.assertIn("标题：RAG 基础", prompt_text)  # the H1 became the Capture title
        self.assertEqual(result.document.title, "RAG 基础")
        self.assertEqual(result.capture_result.request.content, result.document.content)
        self.assertEqual(result.capture_result.request.metadata["source_format"], "markdown")

    def test_13_code_block_text_is_not_removed(self) -> None:
        path = self.write(
            "code.md",
            "# 代码示例\n\n```python\ndef rag(query):\n    return search(query)\n```\n\n普通段落。\n",
        )

        document = FileImporter().load(path)

        self.assertIn("def rag(query):", document.content)
        self.assertIn("return search(query)", document.content)
        self.assertIn("普通段落。", document.content)
        self.assertNotIn("```", document.content)  # fence lines are syntax, not content
        self.assertNotIn("python\n", document.content.split("\n")[0])  # no stray info string

    def test_clean_markdown_rules_are_documented_behaviour(self) -> None:
        document = clean_markdown("# T\n\npara\n\n\n\n\n- a\n\n```\ncode()\n```\n\n## sub\n\ntail\n")
        self.assertEqual(document.title, "T")
        self.assertEqual(document.title_source, "heading")
        self.assertNotIn("\n\n\n", document.body)  # blank runs collapse
        self.assertIn("code()", document.body)  # fence content kept
        self.assertIn("## sub", document.body)
        self.assertEqual(clean_markdown("no heading at all").title_source, "none")
        self.assertIsNone(clean_markdown("no heading at all").title)


# --------------------------------------------------------------------------
# Integration (14-20)
# --------------------------------------------------------------------------


class ImporterIntegrationTest(ImporterTestCase):
    def test_14_txt_file_reaches_capture(self) -> None:
        path = self.write("rag.txt", "RAG 是检索增强生成：先检索，再生成。")
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="先检索"))
        service, _ = self.capture_service(response_for(payload))

        result = import_file(path, service)

        self.assertEqual(result.import_status, "imported")
        self.assertEqual(result.status, "persisted")
        self.assertEqual(result.capture_result.request.content, "RAG 是检索增强生成：先检索，再生成。")
        self.assertEqual(result.capture_result.request.title, "rag")
        self.assertEqual(str(result.capture_result.request.source_type), "file")
        self.assertEqual(result.memory_count, 1)
        self.assertEqual(result.source_count, 1)

    def test_15_markdown_file_reaches_capture(self) -> None:
        path = self.write("rag.md", "# RAG 基础\n\nRAG 是检索增强生成：先检索，再生成。\n")
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="先检索"))
        service, _ = self.capture_service(response_for(payload))

        result = FileImporter().import_file(path, service)

        self.assertEqual(result.status, "persisted")
        self.assertEqual(result.document.title, "RAG 基础")
        self.assertEqual(result.capture_result.request.title, "RAG 基础")
        self.assertIn("RAG 是检索增强生成", result.capture_result.request.content)

    def test_16_high_value_file_forms_a_memory(self) -> None:
        payload = formation_payload(
            draft_payload(type="knowledge", title="RAG 基本原理", content="RAG 通过检索外部知识为模型提供上下文。")
        )
        service, _ = self.capture_service(response_for(payload))
        path = self.write("rag.md", "# RAG 基本原理\n\nRAG 通过检索外部知识为模型提供上下文。\n")

        result = FileImporter().import_file(path, service)

        self.assertEqual(result.formation_status, "persisted")
        self.assertEqual(result.memory_count, 1)
        fresh = MemoryRepository(Database(self.db_path))
        stored = fresh.require_memory(result.memories_created[0].id)
        self.assertEqual(stored.title, "RAG 基本原理")
        self.assertEqual(fresh.counts()["memories"], 1)
        hits = MemoryRetriever(fresh).search("RAG")
        self.assertEqual([hit.memory.id for hit in hits.hits], [stored.id])

    def test_17_low_value_file_forms_nothing(self) -> None:
        service, client = self.capture_service(response_for(LOW_VALUE_PAYLOAD))
        path = self.write("shopping.txt", "今天买了一瓶牛奶。\n")

        result = FileImporter().import_file(path, service)

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.memory_count, 0)
        self.assertEqual(result.source_count, 0)
        self.assertFalse(result.source_reused)
        self.assertEqual(client.transport.call_count, 1)
        self.assertEqual(self.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_18_importer_never_touches_sqlite(self) -> None:
        files_source = pathlib.Path(personal_memory.importers.files.__file__).read_text(encoding="utf-8")
        markdown_source = pathlib.Path(personal_memory.importers.markdown.__file__).read_text(encoding="utf-8")
        for source in (files_source, markdown_source):
            self.assertNotIn("sqlite3", source)
            self.assertNotIn("from ..store", source)
            for verb in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
                self.assertNotIn(verb, source)

        # the API itself has no database handle: a repository is not accepted anywhere
        self.assertEqual(list(inspect.signature(FileImporter.load).parameters), ["self", "path", "title"])
        self.assertEqual(
            list(inspect.signature(FileImporter.import_file).parameters),
            ["self", "path", "capture", "title", "source_type", "dry_run"],
        )
        path = self.write("notes.txt", "内容")
        document = FileImporter().load(path)
        self.assertIsInstance(document, ImportedDocument)
        self.assertEqual(self.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})
        with self.assertRaises(ValidationError):
            FileImporter().import_file(path, self.repo)  # must go through Capture, not the repo

    def test_19_formation_failure_leaves_no_partial_data(self) -> None:
        path = self.write("rag.md", "# RAG\n\n高价值内容：RAG 通过检索外部知识提供上下文。\n")
        document = FileImporter().load(path)
        digest = compute_content_hash(document.content)
        service, _ = self.capture_service(LLMRequestError("provider unreachable", retryable=False))

        with self.assertRaises(LLMRequestError):
            FileImporter().import_file(path, service)

        self.assertEqual(self.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})
        self.assertFalse(self.repo.source_exists_by_hash(digest))
        self.assertTrue(self.repo.index_consistency()["consistent"])

    def test_19b_dry_run_imports_nothing(self) -> None:
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="正文"))
        service, _ = self.capture_service(response_for(payload))
        path = self.write("rag.md", "# RAG\n\n正文内容。\n")

        result = FileImporter().import_file(path, service, dry_run=True)

        self.assertEqual(result.import_status, "preview")
        self.assertEqual(result.status, "preview")
        self.assertEqual(result.memory_count, 1)  # previewed only
        self.assertEqual(self.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_20_reimporting_the_same_file_reuses_the_frozen_dedupe(self) -> None:
        path = self.write("rag.md", "# RAG 基础\n\nRAG 通过检索外部知识为模型提供上下文。\n")
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="逐字依据"))

        # (a) Phase 2 path: the Source content_hash is reused, no second Source row
        service, _ = self.capture_service(response_for(payload), response_for(payload))
        first = FileImporter().import_file(path, service)
        second = FileImporter().import_file(path, service)
        self.assertEqual(first.status, "persisted")
        self.assertEqual(first.source_count, 1)
        self.assertTrue(second.source_reused)
        self.assertEqual(second.sources_created[0].id, first.sources_created[0].id)
        self.assertEqual(self.counts(), {"sources": 1, "memories": 2, "memory_sources": 2})

        # (b) Phase 4 gate on: the Memory itself is recognised as an exact duplicate
        gated, _ = self.capture_service(response_for(payload), quality=True)
        third = FileImporter().import_file(path, gated)
        self.assertEqual(third.status, "duplicate")
        self.assertEqual(third.memory_count, 0)
        self.assertEqual(self.counts(), {"sources": 1, "memories": 2, "memory_sources": 2})
        self.assertTrue(self.repo.index_consistency()["consistent"])


# --------------------------------------------------------------------------
# Boundaries the spec calls out
# --------------------------------------------------------------------------


class ImporterBoundaryTest(ImporterTestCase):
    def test_unsupported_extensions_are_rejected_and_markdown_alias_works(self) -> None:
        for name in ("notes.pdf", "notes.rst", "notes.docx", "notes"):
            with self.subTest(name=name):
                path = self.write(name, "内容")
                with self.assertRaises(UnsupportedFileTypeError) as caught:
                    FileImporter().load(path)
                self.assertIn("unsupported file type", str(caught.exception))
        self.assertEqual(FileImporter().load(self.write("n.markdown", "内容")).extension, ".markdown")
        self.assertEqual(self.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_file_size_limit_is_enforced_and_configurable(self) -> None:
        path = self.write("big.txt", "x" * 200)

        with self.assertRaises(FileTooLargeError) as caught:
            FileImporter(max_bytes=10).load(path)
        self.assertEqual((caught.exception.size_bytes, caught.exception.max_bytes), (200, 10))
        self.assertTrue(FileImporter(max_bytes=1000).load(path).content.startswith("x"))
        with self.assertRaises(ValidationError):
            FileImporter(max_bytes=0)

    def test_metadata_records_no_local_path(self) -> None:
        path = self.write("nested/rag.md", "# T\n\n正文。\n")

        document = FileImporter().load(path)
        metadata = document.metadata

        self.assertEqual(metadata["filename"], "rag.md")
        self.assertEqual(metadata["extension"], ".md")
        self.assertEqual(metadata["size_bytes"], path.stat().st_size)
        self.assertEqual(len(metadata["file_sha256"]), 64)
        self.assertEqual(metadata["source_format"], "markdown")
        self.assertNotIn(str(self.tmpdir), json.dumps(metadata, ensure_ascii=False))
        self.assertNotIn(str(path), json.dumps(document.as_dict(), ensure_ascii=False))
        self.assertEqual(FileImporter().load(path).metadata["file_sha256"], metadata["file_sha256"])

    def test_persisted_source_metadata_has_no_local_path(self) -> None:
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="正文"))
        service, _ = self.capture_service(response_for(payload))
        path = self.write("nested/rag.md", "# T\n\n正文内容。\n")

        result = FileImporter().import_file(path, service)

        source_metadata = result.sources_created[0].metadata
        self.assertEqual(source_metadata["filename"], "rag.md")
        self.assertEqual(source_metadata["captured_from"], "file")
        self.assertNotIn(str(self.tmpdir), json.dumps(source_metadata, ensure_ascii=False))
        self.assertEqual(result.sources_created[0].title, "T")

    def test_import_failures_are_typed_under_one_base_class(self) -> None:
        for error_cls in (
            FileMissingError,
            NotAFileError,
            UnsupportedFileTypeError,
            EmptyFileError,
            FileTooLargeError,
            FileEncodingError,
        ):
            with self.subTest(error=error_cls.__name__):
                self.assertTrue(issubclass(error_cls, MemorySystemError))

    def test_title_precedence_explicit_over_heading(self) -> None:
        path = self.write("rag.md", "# 文件里的标题\n\n正文。\n")
        document = FileImporter().load(path, title="调用方标题")
        self.assertEqual(document.title, "调用方标题")
        self.assertEqual(document.title_source, "explicit")
        self.assertEqual(FileImporter().load(path).title, "文件里的标题")
        self.assertEqual(FileImporter().load(path).title_source, "heading")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
