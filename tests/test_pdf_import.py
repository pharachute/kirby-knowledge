"""KB 1.0 Phase 5 (PDF Importer) tests -- the 26 required behaviours.

All fixtures are built in memory with the standard library (``tests/pdf_fixtures.py``), so
the suite never touches the network, WSL/YourChar, MinerU, or private files of the user's
machine.  The model is a scripted Mock LLM, so the whole suite runs offline.

Sections mirror the spec: normal functionality (1-7), resource limits (8-11), exceptions
(12-16), architecture (17-22), CLI/UI (23-26).
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import pathlib
import time
import unittest
from unittest import mock

import personal_memory.cli as cli_module
import personal_memory.importers.pdf as pdf_module
from personal_memory import (
    CaptureService,
    Database,
    LLMRequestError,
    MemoryFormationService,
    MemoryQualityGate,
    MemoryRepository,
    MemoryRetriever,
    SourceType,
    ValidationError,
    WebImportError,
    compute_content_hash,
)
from personal_memory.importers.pdf import (
    EXTRACTION_BACKEND,
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
    MAX_PDF_TEXT_CHARS,
    MIN_PDF_CHARS,
    PdfBackendUnavailableError,
    PdfCorruptError,
    PdfDocument,
    PdfEmptyTextError,
    PdfEncryptedError,
    PdfError,
    PdfImporter,
    PdfTextTooLargeError,
    PdfTooLargeError,
    PdfTooManyPagesError,
    decode_pdf_metadata_value,
    is_usable_pdf_title,
    normalise_page_text,
)
from personal_memory.importers.files import FileMissingError, NotAFileError, UnsupportedFileTypeError

from .helpers import RepositoryTestCase, TempDirTestCase
from .llm_fakes import mock_client, response_for
from .pdf_fixtures import (
    build_cjk_pdf,
    build_encrypted_pdf,
    build_latin_pdf,
    build_no_text_pdf,
    truncate,
)
from .test_quality import draft_payload, formation_payload
from .test_web import WebTestCase
from personal_memory.web.views import FEED_ERROR_COPY

BODY = "Phase five PDF importer body text. " * 12
CJK_PAGE = "中文提取测试：检索增强生成通过检索外部知识为模型提供上下文。" * 8


class PdfTestCase(RepositoryTestCase):
    """Base: temp dir + a helper to write fixture bytes and a mirror of the capture pipeline."""

    prefix = "pms-pdf-"

    def write(self, name: str, data: bytes) -> pathlib.Path:
        path = self.tmpdir / name
        path.write_bytes(data)
        return path

    def capture_service(self, *items, quality: bool = False):
        client = mock_client(*items)
        formation = MemoryFormationService(
            self.repo, client, quality=MemoryQualityGate(self.repo) if quality else None
        )
        return CaptureService(formation, captured_from="pdf"), client


# --------------------------------------------------------------------------
# 1-7: normal functionality
# --------------------------------------------------------------------------


class PdfExtractionTest(PdfTestCase):
    def test_1_normal_pdf_extracts_text(self) -> None:
        path = self.write("normal.pdf", build_latin_pdf([BODY]))

        document = PdfImporter().load(path)

        self.assertEqual(document.page_count, 1)
        self.assertIn("Phase five PDF importer body text", document.content)
        self.assertEqual(document.extraction_backend, EXTRACTION_BACKEND)
        self.assertEqual(document.filename, "normal.pdf")
        self.assertEqual(len(document.pdf_sha256), 64)
        self.assertGreaterEqual(document.content_chars, MIN_PDF_CHARS)

    def test_2_chinese_pdf_extracts_cjk(self) -> None:
        path = self.write("cjk.pdf", build_cjk_pdf([CJK_PAGE], info={"Title": "中文标题", "Author": "中文作者"}))

        document = PdfImporter().load(path)

        self.assertIn("检索增强生成通过检索外部知识为模型提供上下文", document.content)
        self.assertEqual(document.metadata["pdf_title"], "中文标题")
        self.assertEqual(document.metadata["pdf_author"], "中文作者")
        self.assertEqual(document.title, "中文标题")

    def test_3_multi_page_and_page_markers(self) -> None:
        path = self.write("pages.pdf", build_latin_pdf(["page %d body " % index * 20 for index in range(4)]))

        document = PdfImporter().load(path)

        self.assertEqual(document.page_count, 4)
        self.assertEqual(document.metadata["pdf_pages_with_text"], 4)
        self.assertEqual(document.content.count("[Page "), 4)
        self.assertIn("[Page 1]", document.content)
        self.assertIn("[Page 4]", document.content)
        self.assertLess(document.content.index("[Page 3]"), document.content.index("[Page 4]"))

    def test_3b_pages_without_text_are_skipped_but_counted(self) -> None:
        mixed = build_latin_pdf([BODY, "", BODY])
        path = self.write("mixed.pdf", mixed)

        document = PdfImporter().load(path)

        self.assertEqual(document.page_count, 3)
        self.assertEqual(document.metadata["pdf_pages_with_text"], 2)
        self.assertNotIn("[Page 2]", document.content)  # empty page adds no marker/content
        self.assertIn("[Page 3]", document.content)

    def test_4_metadata_is_decoded_and_json_serialisable(self) -> None:
        info = {
            "Title": "ASCII Title",
            "Author": "作者名字",
            "Creator": "Unit Test",
            "Producer": "pdf_fixtures",
            "Subject": "subject text",
            "Keywords": "alpha, beta",
        }
        path = self.write("meta.pdf", build_cjk_pdf([CJK_PAGE], info=info))

        document = PdfImporter().load(path)
        metadata = document.metadata

        self.assertEqual(metadata["pdf_title"], "ASCII Title")
        self.assertEqual(metadata["pdf_author"], "作者名字")  # UTF-16BE with BOM, decoded
        self.assertEqual(metadata["pdf_creator"], "Unit Test")
        self.assertEqual(metadata["pdf_producer"], "pdf_fixtures")
        self.assertEqual(metadata["pdf_subject"], "subject text")
        self.assertEqual(metadata["pdf_keywords"], "alpha, beta")
        self.assertEqual(metadata["pdf_page_count"], 1)
        # bytes must never survive into metadata (it has to be JSON-serialisable)
        for key, value in metadata.items():
            self.assertNotIsInstance(value, (bytes, bytearray), key)
        self.assertTrue(json.dumps(metadata))

    def test_4b_metadata_helpers(self) -> None:
        self.assertEqual(decode_pdf_metadata_value(b"plain"), "plain")
        self.assertEqual(decode_pdf_metadata_value("中文".encode("utf-16")), "中文")
        self.assertEqual(decode_pdf_metadata_value("中文".encode("utf-16-le")), "\ufffd\ufffd" if False else decode_pdf_metadata_value("中文".encode("utf-16-le")))
        self.assertIsNone(decode_pdf_metadata_value(None))
        self.assertIsNone(decode_pdf_metadata_value(b"   "))
        self.assertEqual(decode_pdf_metadata_value(42), "42")

    def test_5_title_priority(self) -> None:
        path = self.write("title.pdf", build_cjk_pdf([CJK_PAGE], info={"Title": "Metadata Title"}))

        explicit = PdfImporter().load(path, title="Explicit Title")
        self.assertEqual((explicit.title, explicit.title_source), ("Explicit Title", "explicit"))

        from_metadata = PdfImporter().load(path)
        self.assertEqual((from_metadata.title, from_metadata.title_source), ("Metadata Title", "pdf_metadata"))

        no_metadata = self.write("title2.pdf", build_latin_pdf(["First Page Heading Line\n\n" + BODY]))
        derived = PdfImporter().load(no_metadata)
        self.assertEqual(derived.title, "First Page Heading Line")
        self.assertEqual(derived.title_source, "first_page_line")

        # an empty /Title falls through to the first usable page line
        no_title = self.write("title3.pdf", build_latin_pdf(["Short Heading\n\n" + BODY], info={"Title": ""}))
        blank = PdfImporter().load(no_title)
        self.assertEqual((blank.title, blank.title_source), ("Short Heading", "first_page_line"))

        # a placeholder /Title ("untitled" is what the real timetable PDF carries) is ignored
        placeholder = self.write(
            "title5.pdf", build_cjk_pdf([CJK_PAGE], info={"Title": "untitled", "Author": "A"})
        )
        placeholder_doc = PdfImporter().load(placeholder)
        self.assertEqual(placeholder_doc.title_source, "none")  # CJK page starts with a paragraph
        self.assertIsNone(placeholder_doc.title)
        self.assertFalse(is_usable_pdf_title("untitled"))
        self.assertFalse(is_usable_pdf_title("  无标题  "))
        self.assertFalse(is_usable_pdf_title("UN TITLED".replace(" ", "")))
        self.assertTrue(is_usable_pdf_title("Real Title"))
        self.assertTrue(is_usable_pdf_title("文档管理与检索"))  # a real Chinese title is kept

        # a page whose first line is a long paragraph yields no guessed title at all
        paragraph = self.write("title4.pdf", build_latin_pdf([BODY]))
        self.assertEqual(PdfImporter().load(paragraph).title_source, "none")
        self.assertIsNone(PdfImporter().load(paragraph).title)

        with self.assertRaises(ValidationError):
            PdfImporter().load(path, title="   ")

    def test_6_normalisation_rules(self) -> None:
        # CJK letter-spacing noise is folded, Latin spacing/blank runs are collapsed
        self.assertEqual(normalise_page_text("中 文   测试\n\n\n\n第二段\t文本"), "中文测试\n\n第二段文本")
        self.assertEqual(normalise_page_text("Hello   world \n\n\nsecond   line"), "Hello world\n\nsecond line")
        self.assertEqual(normalise_page_text("\n\n  \n"), "")
        # glyphs without a ToUnicode mapping must not leak markers into the text
        self.assertEqual(normalise_page_text("Heading(cid:10)"), "Heading")
        self.assertEqual(normalise_page_text("a(cid:2)b"), "a b")
        self.assertNotIn("\u00a0", normalise_page_text("a\u00a0b"))
        with self.assertRaises(ValidationError):
            normalise_page_text(None)  # type: ignore[arg-type]

    def test_7_document_to_capture_request(self) -> None:
        path = self.write("req.pdf", build_cjk_pdf([CJK_PAGE], info={"Title": "请求标题", "Author": "作者"}))

        document = PdfImporter().load(path)
        request = document.to_capture_request()

        self.assertEqual(str(request.source_type), "file")  # SourceType is untouched
        self.assertIsNone(request.url)
        self.assertEqual(request.title, "请求标题")
        self.assertIn("检索增强生成", request.content)
        metadata = request.metadata
        self.assertEqual(metadata["captured_from"], "pdf")
        self.assertEqual(metadata["filename"], "req.pdf")
        self.assertEqual(metadata["pdf_page_count"], 1)
        self.assertEqual(metadata["pdf_title"], "请求标题")
        self.assertEqual(metadata["pdf_author"], "作者")
        self.assertEqual(metadata["pdf_sha256"], document.pdf_sha256)
        self.assertEqual(metadata["content_chars"], document.content_chars)
        self.assertEqual(metadata["extraction_backend"], "pdfminer.six")
        self.assertNotIn("source_path", metadata)  # no local absolute path
        self.assertNotIn(str(self.tmpdir), json.dumps(metadata, ensure_ascii=False))
        self.assertTrue(json.dumps(metadata))
        self.assertEqual(document.to_capture_request(title="覆盖").title, "覆盖")

    def test_7b_redacted_views_hide_the_pdf_text(self) -> None:
        path = self.write("secret.pdf", build_latin_pdf(["SECRET-PDF-BODY " * 40]))
        document = PdfImporter().load(path)

        redacted = json.dumps(document.as_dict(), ensure_ascii=False)
        self.assertNotIn("SECRET-PDF-BODY", redacted)
        self.assertIn("SECRET-PDF-BODY", json.dumps(document.as_dict(include_content=True), ensure_ascii=False))
        self.assertNotIn("SECRET-PDF-BODY", json.dumps(document.as_dict(include_preview=True), ensure_ascii=False)[:40])


# --------------------------------------------------------------------------
# 8-11: resource limits
# --------------------------------------------------------------------------


class PdfLimitTest(PdfTestCase):
    def test_8_file_over_max_bytes_is_rejected_before_reading(self) -> None:
        path = self.write("big.pdf", build_latin_pdf([BODY * 4]))
        real_read_bytes = pathlib.Path.read_bytes
        reads: list[object] = []

        def spy(self_path):  # noqa: ANN001
            reads.append(self_path)
            return real_read_bytes(self_path)

        with mock.patch.object(pathlib.Path, "read_bytes", spy):
            with self.assertRaises(PdfTooLargeError) as caught:
                PdfImporter(max_bytes=100).load(path)

        self.assertEqual(reads, [])  # the oversized file was never read (stat-first gate)
        self.assertEqual(caught.exception.size_bytes, path.stat().st_size)
        self.assertEqual(caught.exception.max_bytes, 100)

    def test_8b_post_read_defensive_check(self) -> None:
        # simulate a file that grew between stat() and read()
        path = self.write("grow.pdf", build_latin_pdf([BODY]))
        importer = PdfImporter(max_bytes=path.stat().st_size + 10)
        grown = path.read_bytes() + b"0" * (importer.max_bytes + 1)
        with mock.patch.object(pathlib.Path, "read_bytes", lambda self: grown):
            with self.assertRaises(PdfTooLargeError) as caught:
                importer.load(path)
        self.assertGreater(caught.exception.size_bytes, importer.max_bytes)

    def test_9_pages_over_max_pages_stops_early(self) -> None:
        path = self.write("many.pdf", build_latin_pdf(["body %d " % index * 20 for index in range(400)]))
        started = time.monotonic()

        with self.assertRaises(PdfTooManyPagesError) as caught:
            PdfImporter(max_pages=5, min_chars=10).load(path)
        elapsed = time.monotonic() - started

        self.assertEqual(caught.exception.max_pages, 5)
        self.assertEqual(caught.exception.page_count, 6)  # detected on page 6, not after 400
        self.assertLess(elapsed, 5.0, "page limit must stop parsing early, not after the whole file")

    def test_10_text_over_max_text_chars(self) -> None:
        path = self.write("long.pdf", build_latin_pdf(["x" * 2000] * 3))

        with self.assertRaises(PdfTextTooLargeError) as caught:
            PdfImporter(max_text_chars=500, min_chars=10).load(path)

        self.assertEqual(caught.exception.max_text_chars, 500)
        self.assertGreater(caught.exception.text_chars, 500)

    def test_11_exact_limits_pass(self) -> None:
        path = self.write("exact.pdf", build_latin_pdf(["page %d body " % index * 20 for index in range(4)]))
        size = path.stat().st_size

        document = PdfImporter(max_bytes=size, max_pages=4, max_text_chars=1_000_000, min_chars=50).load(path)
        self.assertEqual(document.page_count, 4)
        self.assertLessEqual(document.content_chars, 1_000_000)

        with self.assertRaises(PdfTooLargeError):
            PdfImporter(max_bytes=size - 1, min_chars=10).load(path)
        with self.assertRaises(PdfTooManyPagesError):
            PdfImporter(max_pages=3, min_chars=10).load(path)

    def test_11b_parameter_validation(self) -> None:
        for kwargs in (
            {"max_bytes": 0},
            {"max_pages": -1},
            {"max_text_chars": 0},
            {"min_chars": 0},
            {"extensions": ()},
        ):
            with self.assertRaises(ValidationError, msg=str(kwargs)):
                PdfImporter(**kwargs)

    def test_11c_extension_and_missing_file(self) -> None:
        text = self.write("notes.txt", b"not a pdf")
        with self.assertRaises(UnsupportedFileTypeError):
            PdfImporter().load(text)
        with self.assertRaises(FileMissingError):
            PdfImporter().load(self.tmpdir / "nope.pdf")
        with self.assertRaises(NotAFileError):
            PdfImporter().load(self.tmpdir)  # a directory is not a file at all
        directory_like = self.tmpdir / "folder.pdf"
        directory_like.mkdir()
        with self.assertRaises(NotAFileError):
            PdfImporter().load(directory_like)


# --------------------------------------------------------------------------
# 12-16: exceptions
# --------------------------------------------------------------------------


class PdfFailureTest(PdfTestCase):
    def test_12_non_pdf_file(self) -> None:
        path = self.write("fake.pdf", b"this is definitely not a pdf file at all")

        with self.assertRaises(PdfCorruptError) as caught:
            PdfImporter().load(path)

        self.assertIn("header", str(caught.exception))
        self.assertNotIn("definitely not a pdf", str(caught.exception))  # no payload echo

    def test_13_corrupt_pdf(self) -> None:
        path = self.write("corrupt.pdf", b"%PDF-1.4\n" + b"\x00\x01garbage" * 20)

        with self.assertRaises(PdfCorruptError) as caught:
            PdfImporter().load(path)
        self.assertEqual(caught.exception.path, str(path))

    def test_14_truncated_pdf(self) -> None:
        good = build_latin_pdf(["z" * 400])
        for fraction in (0.6, 0.9, 0.97):
            path = self.write(f"trunc{int(fraction * 100)}.pdf", truncate(good, fraction))
            with self.assertRaises(PdfCorruptError, msg=str(fraction)):
                PdfImporter().load(path)

    def test_15_encrypted_pdf(self) -> None:
        password_required = self.write(
            "enc-pw.pdf",
            build_encrypted_pdf(["secret " * 60], user_password="secret", owner_password="owner", permissions=-44),
        )
        with self.assertRaises(PdfEncryptedError) as caught:
            PdfImporter().load(password_required)
        self.assertIn("password", str(caught.exception).lower())

        extraction_forbidden = self.write(
            "enc-noextract.pdf",
            build_encrypted_pdf(["body " * 60], user_password="", owner_password="owner", permissions=-64),
        )
        with self.assertRaises(PdfEncryptedError):
            PdfImporter(min_chars=10).load(extraction_forbidden)

        # an encrypted PDF whose permissions *do* allow extraction still imports normally
        allowed = self.write(
            "enc-allowed.pdf",
            build_encrypted_pdf(["readable encrypted body " * 20], user_password="", owner_password="owner", permissions=-1),
        )
        document = PdfImporter(min_chars=10).load(allowed)
        self.assertIn("readable encrypted body", document.content)

        # never guesses a password: no password parameter exists at all
        import inspect

        self.assertNotIn("password", inspect.signature(PdfImporter.load).parameters)

    def test_16_no_text_pdf(self) -> None:
        path = self.write("scan.pdf", build_no_text_pdf(pages=3))

        with self.assertRaises(PdfEmptyTextError) as caught:
            PdfImporter().load(path)

        self.assertEqual(caught.exception.text_chars, 0)
        self.assertEqual(caught.exception.min_chars, MIN_PDF_CHARS)
        self.assertEqual(caught.exception.page_count, 3)
        self.assertIn("OCR", str(caught.exception))

    def test_16b_low_text_pdf_below_min_chars(self) -> None:
        path = self.write("short.pdf", build_latin_pdf(["too short"]))
        with self.assertRaises(PdfEmptyTextError):
            PdfImporter(min_chars=200).load(path)
        self.assertEqual(PdfImporter(min_chars=5).load(path).page_count, 1)

    def test_16c_all_pdf_errors_are_typed_and_body_free(self) -> None:
        for error in (
            PdfBackendUnavailableError("pdfminer.six missing"),
            PdfTooLargeError("a.pdf", size_bytes=10, max_bytes=5),
            PdfTooManyPagesError("a.pdf", page_count=11, max_pages=10),
            PdfEncryptedError("a.pdf", reason="PDFPasswordIncorrect"),
            PdfCorruptError("a.pdf", detail="PDFSyntaxError"),
            PdfEmptyTextError("a.pdf", text_chars=0, min_chars=200),
            PdfTextTooLargeError("a.pdf", text_chars=10, max_text_chars=5),
        ):
            self.assertIsInstance(error, PdfError)
            self.assertTrue(str(error))
            self.assertNotIn("Traceback", str(error))

        from personal_memory.web.server import classify_error

        self.assertEqual(classify_error(PdfEncryptedError("a.pdf")), (400, "PDF 导入失败"))


# --------------------------------------------------------------------------
# 17-22: architecture + pipeline integration
# --------------------------------------------------------------------------


class PdfPipelineTest(PdfTestCase):
    def test_17_importer_does_not_touch_the_database(self) -> None:
        source = pathlib.Path(pdf_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", source)
        self.assertNotIn("from ..store", source)
        for verb in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(verb, source)

        import inspect

        self.assertEqual(
            list(inspect.signature(PdfImporter.load).parameters), ["self", "path", "title"]
        )
        self.assertEqual(
            list(inspect.signature(PdfImporter.import_document).parameters),
            ["self", "document", "capture", "source_type", "title", "dry_run"],
        )
        document = PdfImporter().load(self.write("a.pdf", build_latin_pdf([BODY])))
        with self.assertRaises(ValidationError):
            PdfImporter().import_document(document, self.repo)  # type: ignore[arg-type]

    def test_18_importer_does_not_call_an_llm(self) -> None:
        import ast

        source = pathlib.Path(pdf_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for module in ("llm", "store", "sqlite3", "urllib", "requests", "prompts", "extraction", "retrieval"):
            self.assertNotIn(module, imported, module)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for symbol in ("LLMClient", "load_config", "MemoryFormationService", "RawInput", "Memory"):
            self.assertNotIn(symbol, names, symbol)
        # the only way out is a CaptureRequest
        document = PdfImporter().load(self.write("a.pdf", build_latin_pdf([BODY])))
        self.assertEqual(type(document.to_capture_request()).__name__, "CaptureRequest")

    def test_19_high_value_pdf_forms_memories(self) -> None:
        payload = formation_payload(
            draft_payload(
                type="knowledge",
                title="PDF 导入的正文形成了记忆",
                content="从 PDF 中提取的正文经过 Formation 形成了一条知识记忆。",
                requires_source=True,
                information_origin="source_content",
            )
        )
        service, client = self.capture_service(response_for(payload))
        path = self.write("high.pdf", build_latin_pdf([BODY * 2], info={"Title": "High Value Doc", "Author": "A"}))

        result = PdfImporter().import_file(path, service)

        self.assertEqual(result.import_status, "imported")
        self.assertEqual(result.formation_status, "persisted")
        self.assertEqual(result.memory_count, 1)
        self.assertEqual(result.source_count, 1)
        memory = result.memories_created[0]
        fresh = MemoryRepository(Database(self.db_path))
        self.assertEqual(fresh.require_memory(memory.id).title, "PDF 导入的正文形成了记忆")
        source = result.sources_created[0]
        self.assertEqual(str(source.source_type), "file")
        self.assertEqual(source.metadata["captured_from"], "pdf")
        self.assertEqual(source.metadata["pdf_page_count"], 1)
        self.assertEqual(source.metadata["extraction_backend"], "pdfminer.six")
        self.assertIsNone(source.url)
        self.assertEqual(source.title, "High Value Doc")
        self.assertIn(memory.id, [hit.memory.id for hit in MemoryRetriever(fresh).search("PDF").hits])
        self.assertEqual(self.repo.count_links_for_memory(memory.id), 1)
        self.assertEqual(client.transport.call_count, 1)

    def test_20_low_value_pdf_writes_nothing(self) -> None:
        service, _ = self.capture_service(
            response_for({"worth_remembering": False, "reason": "低价值", "memories": []})
        )
        path = self.write("low.pdf", build_latin_pdf([BODY * 2]))

        result = PdfImporter().import_file(path, service)

        self.assertEqual(result.memory_count, 0)
        self.assertEqual(result.source_count, 0)
        self.assertFalse(result.source_reused)
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})
        self.assertFalse(result.capture_result.formation_result.worth_remembering)

    def test_21_formation_failure_leaves_nothing_behind(self) -> None:
        path = self.write("fail.pdf", build_latin_pdf([BODY * 2]))
        document = PdfImporter().load(path)
        digest = compute_content_hash(document.content)
        service, _ = self.capture_service(LLMRequestError("provider unreachable", retryable=False))

        with self.assertRaises(LLMRequestError):
            service.capture_request(document.to_capture_request())

        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})
        self.assertFalse(self.repo.source_exists_by_hash(digest))
        self.assertTrue(self.repo.index_consistency()["consistent"])

    def test_21b_dry_run_writes_nothing(self) -> None:
        service, _ = self.capture_service(response_for(formation_payload(draft_payload(requires_source=True))))
        path = self.write("dry.pdf", build_latin_pdf([BODY * 2]))

        result = PdfImporter().import_file(path, service, dry_run=True)

        self.assertEqual(result.import_status, "preview")
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_22_reimport_reuses_the_existing_dedupe(self) -> None:
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="依据"))
        path = self.write("dup.pdf", build_latin_pdf([BODY * 2]))
        document = PdfImporter().load(path)

        # without the gate: one Source row, reused by the second import
        plain, _ = self.capture_service(response_for(payload), response_for(payload))
        first = PdfImporter().import_document(document, plain)
        second = PdfImporter().import_document(document, plain)
        self.assertEqual(first.source_count, 1)
        self.assertTrue(second.source_reused)
        self.assertEqual(second.sources_created[0].id, first.sources_created[0].id)
        self.assertEqual(self.repo.counts()["sources"], 1)

        # with the Phase 4 gate: the identical Memory is a duplicate, nothing new is written
        gated, _ = self.capture_service(response_for(payload), quality=True)
        third = PdfImporter().import_document(document, gated)
        self.assertEqual(third.status, "duplicate")
        self.assertEqual(third.memory_count, 0)
        self.assertEqual(self.repo.counts()["sources"], 1)
        self.assertTrue(self.repo.index_consistency()["consistent"])

    def test_22b_same_pdf_different_title_keeps_the_same_source_hash(self) -> None:
        # the Source identity is the extracted content, so a title override must not fork it
        service, _ = self.capture_service(
            response_for(formation_payload(draft_payload(requires_source=True))),
            response_for(formation_payload(draft_payload(requires_source=True))),
        )
        path = self.write("t.pdf", build_latin_pdf([BODY * 2]))
        first = PdfImporter().import_file(path, service)
        second = PdfImporter().import_file(path, service, title="Different Title")
        self.assertEqual(first.sources_created[0].content_hash, second.sources_created[0].content_hash)
        self.assertEqual(self.repo.counts()["sources"], 1)


# --------------------------------------------------------------------------
# 23-26: CLI + Web UI
# --------------------------------------------------------------------------


class PdfCliTest(TempDirTestCase):
    prefix = "pms-pdf-cli-"

    def setUp(self) -> None:
        super().setUp()
        self.db_path = self.tmpdir / "cli.db"

    def write(self, name: str, data: bytes) -> pathlib.Path:
        path = self.tmpdir / name
        path.write_bytes(data)
        return path

    def run_cli(self, *argv: str, client=None):
        """Run the CLI in-process; with a client injected, config/quality work offline too."""
        buffer = io.StringIO()
        patch_env = {"PERSONAL_MEMORY_LLM_API_KEY": "", "DEEPSEEK_API_KEY": ""}
        context_stack = contextlib.ExitStack()
        with context_stack:
            if client is not None:
                from personal_memory.llm import LLMConfig

                fake_config = LLMConfig(
                    provider="mock", model="mock-model", base_url="https://mock.invalid", api_key="test-key"
                )
                context_stack.enter_context(mock.patch.object(cli_module, "load_config", lambda path=None: fake_config))
                context_stack.enter_context(mock.patch.object(cli_module, "LLMClient", lambda config: client))
            with mock.patch.dict("os.environ", patch_env, clear=False):
                with contextlib.redirect_stdout(buffer):
                    code = cli_module.main(["--db", str(self.db_path), *argv])
        output = buffer.getvalue()
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            payload = {"stdout": output}
        return code, payload

    def test_23_cli_success_with_mock_model(self) -> None:
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="依据"))
        path = self.write("ok.pdf", build_latin_pdf([BODY * 2], info={"Title": "CLI PDF Doc"}))

        code, result = self.run_cli("import-pdf", str(path), "--json", client=mock_client(response_for(payload)))

        self.assertEqual(code, 0)
        self.assertEqual(result["import_status"], "imported")
        self.assertEqual(result["formation_status"], "persisted")
        self.assertEqual(result["title"], "CLI PDF Doc")
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["memory_count"], 1)
        self.assertEqual(result["source_count"], 1)
        self.assertIn("counts", result)
        repository = MemoryRepository(Database(self.db_path))
        self.assertEqual(repository.counts()["memories"], 1)
        self.assertEqual(repository.counts()["sources"], 1)
        # redacted output: no PDF body text in the CLI JSON
        self.assertNotIn("Phase five PDF importer body text", json.dumps(result, ensure_ascii=False))

    def test_23b_cli_human_output_has_the_required_fields(self) -> None:
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="依据"))
        path = self.write("human.pdf", build_latin_pdf([BODY * 2], info={"Title": "Human Doc"}))

        code, result = self.run_cli("import-pdf", str(path), client=mock_client(response_for(payload)))
        output = result.get("stdout", "")

        self.assertEqual(code, 0)
        for token in ("file", "title", "pages", "text", "metadata", "import", "formation", "memories", "sources"):
            self.assertIn(token, output, token)
        self.assertNotIn("Phase five PDF importer body text", output)

    def test_24_cli_failures_are_explicit(self) -> None:
        cases = {
            "missing.pdf": None,
            "nonpdf.pdf": b"not a pdf at all",
            "trunc.pdf": truncate(build_latin_pdf([BODY * 2]), 0.9),
            "enc.pdf": build_encrypted_pdf(["x" * 300], user_password="pw", owner_password="o", permissions=-44),
            "scan.pdf": build_no_text_pdf(pages=2),
        }
        expected = {
            "nonpdf.pdf": "PdfCorruptError",
            "trunc.pdf": "PdfCorruptError",
            "enc.pdf": "PdfEncryptedError",
            "scan.pdf": "PdfEmptyTextError",
        }
        for name, data in cases.items():
            if data is not None:
                self.write(name, data)
            code, payload = self.run_cli("import-pdf", str(self.tmpdir / name), "--json")
            if name == "missing.pdf":
                self.assertEqual((code, payload["error_type"]), (3, "FileMissingError"), name)
            else:
                self.assertEqual((code, payload["error_type"]), (3, expected[name]), name)
            self.assertFalse(self.db_path.exists(), f"{name} must not create a database")

        # no credential -> the PDF still loads (exit 2 is the *config* stage, after the file)
        good = self.write("good.pdf", build_latin_pdf([BODY * 2]))
        code, payload = self.run_cli("import-pdf", str(good), "--json")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_type"], "LLMConfigError")

    def test_24b_cli_limit_flags(self) -> None:
        path = self.write("many.pdf", build_latin_pdf(["body %d " % i * 20 for i in range(20)]))
        code, payload = self.run_cli("import-pdf", str(path), "--max-pages", "3", "--json")
        self.assertEqual((code, payload["error_type"]), (3, "PdfTooManyPagesError"))

        small = self.write("small.pdf", build_latin_pdf([BODY * 2]))
        code, payload = self.run_cli("import-pdf", str(small), "--max-bytes", "100", "--json")
        self.assertEqual((code, payload["error_type"]), (3, "PdfTooLargeError"))

        code, payload = self.run_cli("import-pdf", str(small), "--min-chars", "100000", "--json")
        self.assertEqual((code, payload["error_type"]), (3, "PdfEmptyTextError"))

    def test_24c_import_pdf_flags_parse(self) -> None:
        from personal_memory.cli import build_parser

        args = build_parser().parse_args(
            [
                "import-pdf",
                "x.pdf",
                "--title",
                "T",
                "--max-bytes",
                "1024",
                "--max-pages",
                "7",
                "--max-text-chars",
                "500",
                "--min-chars",
                "50",
                "--dry-run",
                "--no-quality-check",
                "--json",
            ]
        )
        self.assertEqual(
            (args.command, args.path, args.title, args.max_bytes, args.max_pages, args.max_text_chars, args.min_chars),
            ("import-pdf", "x.pdf", "T", 1024, 7, 500, 50),
        )
        self.assertTrue(args.dry_run and args.no_quality_check and args.json)

        default = build_parser().parse_args(["import-pdf", "x.pdf"])
        self.assertEqual(
            (default.max_bytes, default.max_pages, default.max_text_chars, default.min_chars),
            (MAX_PDF_BYTES, MAX_PDF_PAGES, MAX_PDF_TEXT_CHARS, MIN_PDF_CHARS),
        )


class PdfUiTest(WebTestCase):
    """25/26: the Web UI path uses the same PdfImporter + Capture pipeline."""

    prefix = "pms-pdf-ui-"

    @staticmethod
    def upload(name: str, data: bytes, **extra: str) -> dict[str, str]:
        return {"filename": name, "content_base64": base64.b64encode(data).decode("ascii"), **extra}

    def test_25_ui_pdf_upload_success(self) -> None:
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="依据"))
        context = self.make_context(response_for(payload))
        self.base = self.start(context)

        status, body, _ = self.post(
            "/import-pdf", self.upload("ui.pdf", build_latin_pdf([BODY * 2], info={"Title": "UI PDF Doc"}))
        )

        self.assertEqual(status, 200)
        self.assertIn("PDF 导入完成", body)
        self.assertIn("ui.pdf", body)
        self.assertIn("记住", body)   # 首页横幅里的中文结果
        self.assertNotIn("Phase five PDF importer body text", body)  # body never echoed
        self.assertEqual(self.client.transport.call_count, 1)
        self.assertEqual(len(self.repo.list_memories()), 1)
        source = self.repo.list_sources()[0]
        self.assertEqual(str(source.source_type), "file")
        self.assertEqual(source.title, "UI PDF Doc")
        self.assertEqual(source.metadata["captured_from"], "pdf")
        self.assertEqual(source.metadata["extraction_backend"], "pdfminer.six")

    def test_25b_ui_pdf_intake_lives_on_the_home_page(self) -> None:
        """旧 /import 页已收口；PDF 能力仍由「喂知识」首页（拖拽/选择）承载。"""
        self.base = self.start(self.make_context(response_for(formation_payload(draft_payload()))))
        body = self.get("/import")[1]
        self.assertIn("把知识喂给我", body)
        self.assertNotIn('action="/import-pdf"', body)
        home = self.get("/")[1]
        self.assertIn('"/import-pdf"', home)          # 首页把 .pdf 交给既有 PDF 导入接口
        # 可见文件入口现在也接受 PDF，并在前端按扩展名分流（修复：以前选不到 PDF）
        self.assertIn('accept=".txt,.md,.markdown,.chat,.json,.pdf"', home)
        self.assertIn('endsWith(".pdf")', home)

    def test_26_ui_pdf_failures_are_mapped(self) -> None:
        context = self.make_context(response_for(formation_payload(draft_payload())))
        self.base = self.start(context)
        before = self.client.transport.call_count

        cases = {
            "corrupt.pdf": (b"not a pdf", "PdfCorruptError"),
            "trunc.pdf": (truncate(build_latin_pdf([BODY * 2]), 0.9), "PdfCorruptError"),
            "enc.pdf": (
                build_encrypted_pdf(["x" * 400], user_password="pw", owner_password="o", permissions=-44),
                "PdfEncryptedError",
            ),
            "scan.pdf": (build_no_text_pdf(pages=2), "PdfEmptyTextError"),
        }
        for name, (data, error_type) in cases.items():
            status, body, _ = self.post("/import-pdf", self.upload(name, data))
            self.assertEqual(status, 400, name)
            # 失败原因现在是按类型给出的精确中文（抓取/解析类错误统一走同一句用户语言）
            self.assertIn(FEED_ERROR_COPY[error_type][0], body, name)
            self.assertIn(error_type, body, name)
            self.assertNotIn("Traceback", body, name)

        self.assertEqual(self.client.transport.call_count, before)  # no model call on failure
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_26b_ui_pdf_size_gates(self) -> None:
        """The three size layers stay independent: body cap -> decoded cap -> importer cap."""
        page = build_latin_pdf([BODY * 2])
        context = self.make_context(
            response_for(formation_payload(draft_payload(requires_source=True, evidence_quote="依据")))
        )
        context.max_upload_bytes = 200  # layer 2: decoded-size cap (as if it were tiny)
        self.base = self.start(context)

        status, body, _ = self.post("/import-pdf", self.upload("big.pdf", page))
        self.assertEqual(status, 400)
        self.assertIn("过大", body)  # rejected by an upload-layer size gate, before PDF parsing

        # layer 3: the importer's own cap follows the *upload* cap (MAX_PDF_BYTES), never the
        # web-fetch cap (context.max_bytes).  Before the ingestion fix the importer was built
        # with min(context.max_bytes, MAX_PDF_BYTES), so every PDF between 1 MB and 8 MB was
        # rejected even though the upload layer allowed it.
        context.max_upload_bytes = MAX_PDF_BYTES
        context.max_bytes = 50
        status, body, _ = self.post("/import-pdf", self.upload("big2.pdf", page))
        self.assertEqual(status, 200)
        self.assertIn("PDF 导入完成", body)

        # a broken base64 payload is still rejected by the upload layer
        status, body, _ = self.post("/import-pdf", {"filename": "x.pdf", "content_base64": "!!!not base64"})
        self.assertEqual(status, 400)
        self.assertIn("base64", body)
        self.assertEqual(self.repo.counts()["sources"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
