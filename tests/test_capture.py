"""Knowledge Base 1.0 -- Phase 1 (Capture Layer) tests.

Ten required behaviours, plus the architectural guard rails of spec §十二 (Capture is
its own layer: it holds a Memory Formation service and nothing else -- no SQLite, no
LLM adapter, no second Memory store).

Everything runs offline with a Mock LLM; the one real end-to-end test
(:class:`RealLLMCaptureTest`) is skipped unless a credential is configured.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import pathlib
import unittest
from unittest import mock

import personal_memory.capture
from personal_memory import (
    CAPTURED_FROM_DEFAULT,
    CURRENT_SCHEMA_VERSION,
    CaptureRequest,
    CaptureResult,
    CaptureService,
    Database,
    ExtractionValidationError,
    LLMClient,
    LLMRequestError,
    Memory,
    MemoryFormationService,
    MemoryRepository,
    MemoryQualityGate,
    MemoryRetriever,
    RawInput,
    SourceType,
    ValidationError,
    compute_content_hash,
    load_config,
)
from personal_memory.store import MemoryUnitOfWork

from .helpers import RepositoryTestCase
from .llm_fakes import mock_client, response, response_for
from .test_quality import draft_payload, formation_payload

MEMORY_FIELDS = [
    "id",
    "type",
    "title",
    "content",
    "information_origin",
    "summary",
    "tags",
    "importance",
    "confidence",
    "status",
    "created_at",
    "updated_at",
    "schema_version",
]

KNOWLEDGE_INPUT = (
    "Retrieval-Augmented Generation（RAG）把检索与生成结合：先用检索器从外部语料库取回相关片段，"
    "再让生成模型只基于这些片段作答。这样做可以降低幻觉，并让答案能够追溯到具体资料。"
)

REAL_CREDENTIAL_PRESENT = bool(
    os.environ.get("PERSONAL_MEMORY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
)


def build_capture(repo, *items, captured_from: str = "unit-test"):
    """A CaptureService wired to a scripted (offline) Memory Formation."""
    client = mock_client(*items)
    service = CaptureService(MemoryFormationService(repo, client), captured_from=captured_from)
    return service, client, client.transport


class CaptureInputTest(RepositoryTestCase):
    """Tests 1, 2, 5, 6, 7, 8 and the input structure itself."""

    prefix = "pms-capture-"

    # -- Test 1: valid text reaches Formation ------------------------------
    def test_1_valid_text_reaches_memory_formation(self) -> None:
        payload = formation_payload(draft_payload())
        service, _, transport = build_capture(self.repo, response_for(payload))

        result = service.capture("RAG 通过检索外部知识，为大语言模型提供相关上下文。")

        self.assertIsInstance(result, CaptureResult)
        self.assertTrue(result.captured)
        self.assertEqual(result.status, "persisted")
        self.assertEqual(result.formation_status, "persisted")
        self.assertEqual(transport.call_count, 1)  # Formation asked the model exactly once
        user_message = transport.requests[0].messages[-1].content
        self.assertIn("RAG 通过检索外部知识", user_message)
        self.assertEqual(result.formation_result.attempts, 1)
        json.dumps(result.as_dict(), ensure_ascii=False)  # the CLI prints this

    # -- Test 2: empty text is rejected before any model call ---------------
    def test_2_empty_or_invalid_content_is_rejected_without_calling_the_model(self) -> None:
        service, _, transport = build_capture(self.repo)  # no scripted answers at all

        for bad in ("", "   ", "\n\t ", None, 42, ["text"]):
            with self.subTest(content=bad), self.assertRaises(ValidationError) as caught:
                service.capture(bad)
            self.assertEqual(caught.exception.fields, ("content",))

        self.assertEqual(transport.call_count, 0)  # the model was never asked
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_2b_invalid_title_url_source_type_metadata_and_timestamp_are_rejected(self) -> None:
        service, _, transport = build_capture(self.repo)

        cases = (
            ({"title": "   "}, "title"),
            ({"url": "ftp://example.com/x"}, "url"),
            ({"url": "not-a-url"}, "url"),
            ({"source_type": "pdf"}, "source_type"),
            ({"metadata": "not-a-mapping"}, "metadata"),
            ({"captured_at": "yesterday"}, "captured_at"),
        )
        for kwargs, field_name in cases:
            with self.subTest(given=kwargs), self.assertRaises(ValidationError) as caught:
                service.capture("有效正文", **kwargs)
            self.assertEqual(caught.exception.fields, (field_name,))

        self.assertEqual(transport.call_count, 0)
        self.assertEqual(self.repo.counts()["memories"], 0)

    def test_capture_request_is_the_only_adapter_to_the_phase_2_input(self) -> None:
        request = CaptureRequest(
            content="正文",
            title="T",
            source_type="web",
            url="https://example.com/x",
            metadata={"language": "zh"},
            captured_at="2026-10-04T00:00:00.000Z",
        )

        raw = request.to_raw_input(captured_from="unit-test")

        self.assertIsInstance(raw, RawInput)
        self.assertEqual(raw.content, "正文")
        self.assertEqual(raw.title, "T")
        self.assertEqual(str(raw.source_type), "web")
        self.assertEqual(raw.url, "https://example.com/x")
        self.assertEqual(raw.created_at, "2026-10-04T00:00:00.000Z")  # captured_at -> created_at
        self.assertEqual(raw.metadata, {"language": "zh", "captured_from": "unit-test"})
        self.assertEqual(str(request.source_type), "web")
        # an explicit captured_from in metadata wins over the service default
        explicit = CaptureRequest(content="x", metadata={"captured_from": "importer"}).to_raw_input(
            captured_from="cli"
        )
        self.assertEqual(explicit.metadata["captured_from"], "importer")
        self.assertIn("captured_at", request.as_dict())

    # -- Test 5: title -----------------------------------------------------
    def test_5_capture_title_reaches_formation_and_the_source(self) -> None:
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="逐字依据"))
        service, _, transport = build_capture(self.repo, response_for(payload))

        result = service.capture("正文第一行\n第二行", title="捕获标题")

        self.assertIn("标题：捕获标题", transport.requests[0].messages[-1].content)
        self.assertEqual(result.request.title, "捕获标题")
        self.assertEqual(result.sources_created[0].title, "捕获标题")

    def test_5b_without_a_title_the_source_falls_back_to_the_first_line(self) -> None:
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="逐字依据"))
        service, _, _ = build_capture(self.repo, response_for(payload))

        result = service.capture("第一行标题\n第二行正文")

        self.assertEqual(result.sources_created[0].title, "第一行标题")

    # -- Test 6: metadata --------------------------------------------------
    def test_6_metadata_reaches_formation_and_the_source_without_touching_the_memory_schema(self) -> None:
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="逐字依据"))
        service, _, transport = build_capture(self.repo, response_for(payload), captured_from="manual")

        result = service.capture("正文内容", metadata={"language": "zh", "captured_from": "unit-test"})

        # (a) Formation saw the metadata in its prompt context
        user_message = transport.requests[0].messages[-1].content
        self.assertIn("language", user_message)
        self.assertIn("unit-test", user_message)
        # (b) the Source kept it -- the only place capture metadata is stored
        source = result.sources_created[0]
        self.assertEqual(source.metadata["language"], "zh")
        self.assertEqual(source.metadata["captured_from"], "unit-test")  # explicit wins
        self.assertEqual(source.metadata["formation"]["prompt_version"], "memory-formation-v1")
        # (c) no Memory schema change was needed to carry capture metadata
        self.assertEqual([field.name for field in dataclasses.fields(Memory)], MEMORY_FIELDS)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 1)
        self.assertEqual(result.memories_created[0].schema_version, CURRENT_SCHEMA_VERSION)
        self.assertNotIn("captured_from", result.memories_created[0].as_dict())

    def test_6b_captured_at_becomes_the_source_timestamp_and_defaults_to_now(self) -> None:
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="逐字依据"))
        service, _, _ = build_capture(self.repo, response_for(payload))
        stamp = "2026-10-04T12:00:00.000Z"

        result = service.capture("正文", captured_at=stamp)

        self.assertEqual(result.request.captured_at, stamp)
        self.assertEqual(result.sources_created[0].created_at, stamp)

        defaulted, _, _ = build_capture(self.repo, response_for(payload))
        other = defaulted.capture("正文二")
        self.assertTrue(other.request.captured_at.endswith("Z"))
        self.assertGreater(len(other.request.captured_at), 10)

    # -- Test 7: source_type ----------------------------------------------
    def test_7_text_works_and_the_future_kinds_are_structurally_accepted(self) -> None:
        for source_kind in (SourceType.TEXT, "chat", "article", "web", "file"):
            with self.subTest(source_type=source_kind):
                payload = formation_payload(draft_payload(requires_source=True, evidence_quote="逐字依据"))
                service, _, _ = build_capture(self.repo, response_for(payload))

                # distinct content per kind: Phase 2 dedupes Sources by content hash, so
                # reusing one body would legitimately reuse the first Source (source_type
                # "text") instead of creating one for this kind
                result = service.capture(f"带来源的正文内容 [{source_kind}]", source_type=source_kind)

                self.assertEqual(result.source_count, 1)
                self.assertEqual(str(result.sources_created[0].source_type), str(source_kind))
                self.assertEqual(result.request.source_type, SourceType(str(source_kind)))

        # unknown kinds are refused by the structure (no importer exists for them)
        service, _, transport = build_capture(self.repo)
        with self.assertRaises(ValidationError) as caught:
            service.capture("正文", source_type="youtube")
        self.assertEqual(caught.exception.fields, ("source_type",))
        self.assertEqual(transport.call_count, 0)

    # -- Test 8: url is data, never fetched -------------------------------
    def test_8_url_is_carried_as_data_and_never_fetched(self) -> None:
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="逐字依据"))
        service, _, transport = build_capture(self.repo, response_for(payload))
        url = "https://example.com/articles/rag"

        with mock.patch("socket.socket", side_effect=AssertionError("Capture must not open sockets")), mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("Capture must not fetch URLs")
        ):
            result = service.capture("网页正文摘要", url=url)

        self.assertEqual(result.request.url, url)
        self.assertEqual(result.sources_created[0].url, url)
        self.assertEqual(transport.call_count, 1)  # only the mocked Formation call happened

    def test_8b_url_flows_into_the_formation_prompt_context(self) -> None:
        payload = formation_payload(draft_payload())
        service, _, transport = build_capture(self.repo, response_for(payload))

        service.capture("网页正文", url="https://example.com/rag")

        self.assertIn("来源链接：https://example.com/rag", transport.requests[0].messages[-1].content)


class CaptureFormationFlowTest(RepositoryTestCase):
    """Tests 3 and 4: value judgment stays where it belongs (Phase 2)."""

    prefix = "pms-capture-flow-"

    def test_3_high_value_text_ends_up_as_a_memory(self) -> None:
        payload = formation_payload(
            draft_payload(type="knowledge", title="RAG 基本原理", content="RAG 通过检索外部知识为模型提供上下文。")
        )
        service, _, _ = build_capture(self.repo, response_for(payload))

        result = service.capture("RAG 通过检索外部知识为模型提供上下文。", title="RAG")

        self.assertEqual(result.status, "persisted")
        self.assertEqual(result.memory_count, 1)
        memory = result.memories_created[0]

        # it lives in the FROZEN memory system: readable through a new connection ...
        fresh = MemoryRepository(Database(self.db_path))
        stored = fresh.require_memory(memory.id)
        self.assertIsInstance(stored, Memory)
        self.assertEqual(stored.content, memory.content)
        self.assertEqual(fresh.counts()["memories"], 1)
        # ... and found by Phase 3 retrieval (there is no second KB store)
        hits = MemoryRetriever(fresh).search("RAG")
        self.assertEqual([hit.memory.id for hit in hits.hits], [memory.id])

    def test_4_low_value_text_creates_nothing_and_persists_no_source(self) -> None:
        payload = {"worth_remembering": False, "reason": "一次性的日常状态", "memories": []}
        service, _, transport = build_capture(self.repo, response_for(payload))

        result = service.capture("今天喝了一杯奶茶。")

        self.assertTrue(result.captured)  # Capture accepted the input ...
        self.assertEqual(result.status, "skipped")
        self.assertFalse(result.formation_result.worth_remembering)
        self.assertEqual(result.memories_created, ())
        self.assertEqual(result.sources_created, ())  # ... but did NOT persist a Source for it
        self.assertFalse(result.source_reused)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})
        self.assertEqual(
            MemoryRepository(Database(self.db_path)).counts(),
            {"sources": 0, "memories": 0, "memory_sources": 0},
        )

    def test_4b_dry_run_previews_without_writing(self) -> None:
        payload = formation_payload(draft_payload())
        service, _, _ = build_capture(self.repo, response_for(payload))

        result = service.capture("RAG 相关内容", dry_run=True)

        self.assertFalse(result.captured)  # preview: nothing entered the system
        self.assertEqual(result.status, "preview")
        self.assertEqual(result.memory_count, 1)  # a previewed Memory, not a stored one
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_capture_of_the_same_text_twice_uses_the_frozen_dedupe(self) -> None:
        """Idempotence is Phase 4's job; Capture must not invent its own."""
        payload = formation_payload(draft_payload())
        client = mock_client(response_for(payload), response_for(payload))
        service = CaptureService(
            MemoryFormationService(self.repo, client, quality=MemoryQualityGate(self.repo)),
            captured_from="unit-test",
        )

        first = service.capture("RAG 通过检索外部知识为模型提供上下文。")
        second = service.capture("RAG 通过检索外部知识为模型提供上下文。")

        self.assertEqual(first.status, "persisted")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(second.memory_count, 0)
        self.assertEqual([memory.id for memory in second.formation_result.reused], [first.memories_created[0].id])
        self.assertEqual(self.repo.counts()["memories"], 1)


class CaptureFailureTest(RepositoryTestCase):
    """Test 9: a Formation failure must leave nothing half-written."""

    prefix = "pms-capture-fail-"

    def test_9_llm_transport_failure_leaves_no_partial_data(self) -> None:
        text = "高价值内容：RAG 通过检索外部知识提供上下文。"
        service, _, _ = build_capture(self.repo, LLMRequestError("provider unreachable", retryable=False))
        before = self.repo.counts()

        with self.assertRaises(LLMRequestError):
            service.capture(text, metadata={"captured_from": "unit-test"})

        self.assertEqual(self.repo.counts(), before)
        self.assertFalse(self.repo.source_exists_by_hash(compute_content_hash(text)))
        self.assertTrue(self.repo.index_consistency()["consistent"])

    def test_9b_unusable_model_answers_leave_no_partial_data(self) -> None:
        service, _, transport = build_capture(self.repo, response("not json"), response("still not json"))

        with self.assertRaises(ExtractionValidationError):
            service.capture("高价值内容")

        self.assertEqual(transport.call_count, 2)
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_9c_write_failure_rolls_back_source_memories_and_links(self) -> None:
        payload = formation_payload(
            draft_payload(requires_source=True, evidence_quote="逐字依据"),
            draft_payload(title="第二条结论", content="第二条结论正文", requires_source=True, evidence_quote="依据二"),
        )
        service, _, _ = build_capture(self.repo, response_for(payload))

        with mock.patch.object(MemoryUnitOfWork, "link", side_effect=RuntimeError("injected link failure")):
            with self.assertRaises(RuntimeError):
                service.capture("需要来源的正文")

        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_9d_capture_cannot_write_sql_even_when_formation_is_broken(self) -> None:
        """Capture holds no repository: the only writer is Phase 2."""
        service, _, _ = build_capture(self.repo, LLMRequestError("boom", retryable=False))
        self.assertFalse(hasattr(service, "repository"))
        self.assertFalse(hasattr(service, "database"))
        self.assertIsInstance(service.formation, MemoryFormationService)


class CaptureBoundaryTest(unittest.TestCase):
    """Spec §十二: Capture is its own layer -- no SQLite, no LLM adapter of its own."""

    def test_capture_module_has_no_sql_and_no_repository_access(self) -> None:
        source = pathlib.Path(personal_memory.capture.__file__).read_text(encoding="utf-8")

        self.assertNotIn("sqlite3", source)
        self.assertNotIn("from .store", source)
        self.assertNotIn("import store", source)
        for verb in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(verb, source)
        # the one pipeline it can reach is the frozen Memory Formation
        self.assertIn("MemoryFormationService", source)
        self.assertIn("RawInput", source)

    def test_capture_service_signature_is_formation_only(self) -> None:
        parameters = list(inspect.signature(CaptureService.__init__).parameters)

        self.assertEqual(parameters, ["self", "formation", "captured_from"])
        with self.assertRaises(ValidationError):
            CaptureService(object())  # no repository/database handle can be injected
        with self.assertRaises(ValidationError):
            # a spec'd Mock still passes the isinstance check, so only captured_from fails
            CaptureService(mock.Mock(spec=MemoryFormationService), captured_from="  ")

    def test_capture_request_rejects_a_bad_captured_from(self) -> None:
        request = CaptureRequest(content="x")
        self.assertEqual(CAPTURED_FROM_DEFAULT, "manual")
        with self.assertRaises(ValidationError):
            request.to_raw_input(captured_from="")
        self.assertEqual(request.to_raw_input().metadata, {})  # no provenance key when not given

    def test_capture_request_is_immutable(self) -> None:
        request = CaptureRequest(content="x", metadata={"a": 1})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.content = "y"  # type: ignore[misc]
        self.assertEqual(request.metadata, {"a": 1})
        # the stored metadata is a copy, not the caller's mutable mapping
        given = {"a": 1}
        copy = CaptureRequest(content="x", metadata=given)
        given["a"] = 2
        self.assertEqual(copy.metadata, {"a": 1})


@unittest.skipUnless(
    REAL_CREDENTIAL_PRESENT,
    "real LLM credentials not configured (set PERSONAL_MEMORY_LLM_API_KEY or DEEPSEEK_API_KEY)",
)
class RealLLMCaptureTest(RepositoryTestCase):
    """Test 10: real end-to-end Capture -> Formation -> Memory (opt-in, one call)."""

    prefix = "pms-capture-real-"

    def test_real_capture_forms_a_memory_end_to_end(self) -> None:
        config = load_config()
        service = CaptureService(
            MemoryFormationService(self.repo, LLMClient(config)), captured_from="real-llm-test"
        )

        result = service.capture(KNOWLEDGE_INPUT, title="RAG 基础", source_type="text")

        self.assertTrue(result.captured)
        self.assertEqual(result.status, "persisted", result.formation_result.reason)
        self.assertGreaterEqual(result.memory_count, 1)

        memory = result.memories_created[0]
        fresh = MemoryRepository(Database(self.db_path))
        self.assertIsNotNone(fresh.get_memory(memory.id))
        self.assertGreaterEqual(fresh.counts()["memories"], 1)
        if result.sources_created:  # depends on the model's requires_source judgment
            self.assertIn("captured_from", result.sources_created[0].metadata)
        self.assertNotIn(config.api_key, str(result.as_dict()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
