"""Memory Formation tests with a Mock LLM (Phase 2 Tests 1-9 + policy/robustness).

Every test is offline and deterministic: the "model" is a queue of canned JSON
answers (:class:`tests.llm_fakes.ScriptedTransport`), so the suite never costs a
token and never depends on network availability.
"""

from __future__ import annotations

import json
import unittest

from personal_memory import (
    Database,
    ExtractionValidationError,
    FormationPolicy,
    InformationOrigin,
    LLMRequestError,
    Memory,
    MemoryFormationService,
    MemoryRepository,
    MemoryStatus,
    MemoryType,
    RawInput,
)
from personal_memory.prompts import PROMPT_VERSION

from .helpers import RepositoryTestCase
from .llm_fakes import mock_client, response_for

HIGH_VALUE_INPUT = "我正在系统学习 Agent，希望深入理解底层原理，而不是只会调用现成工具。"
LOW_VALUE_INPUT = "今天下午喝了一杯奶茶。"


def memory_item(**overrides):
    item = {
        "type": "knowledge",
        "title": "SQLite 适合本地优先存储",
        "content": "SQLite 单文件、零服务、易备份，个人规模下无需独立数据库进程。",
        "summary": "本地优先场景优先选 SQLite。",
        "tags": ["sqlite", "local-first"],
        "importance": 0.8,
        "confidence": 0.9,
        "information_origin": "source_content",
        "requires_source": True,
        "evidence_quote": "本地优先的笔记系统用 SQLite 就够了",
    }
    item.update(overrides)
    return item


def payload(worth_remembering=True, reason="对未来长期使用有价值", memories=None):
    return {
        "worth_remembering": worth_remembering,
        "reason": reason,
        "memories": [] if memories is None else memories,
    }


class FormationTestCase(RepositoryTestCase):
    def build_service(self, *items, policy=None, max_attempts=2):
        self.client = mock_client(*items)
        self.service = MemoryFormationService(
            self.repo, self.client, policy=policy or FormationPolicy(), max_attempts=max_attempts
        )
        return self.service


class ValueJudgmentTest(FormationTestCase):
    def test_1_high_value_input_forms_a_memory(self) -> None:
        service = self.build_service(response_for(payload(memories=[memory_item()])))
        outcome = service.process(RawInput(content=HIGH_VALUE_INPUT, title="学习目标"))

        self.assertTrue(outcome.worth_remembering)
        self.assertEqual(outcome.status, "persisted")
        self.assertEqual(len(outcome.memories), 1)
        memory = self.repo.require_memory(outcome.memories[0].id)
        self.assertEqual(memory.title, "SQLite 适合本地优先存储")
        self.assertEqual(memory.tags, ["sqlite", "local-first"])
        self.assertAlmostEqual(memory.importance, 0.8)
        self.assertAlmostEqual(memory.confidence, 0.9)
        self.assertEqual(memory.schema_version, 1)

    def test_2_low_value_input_forms_nothing_and_persists_no_source(self) -> None:
        service = self.build_service(
            response_for(payload(False, "一次性的日常闲聊，对未来没有持续价值", []))
        )
        outcome = service.process(RawInput(content=LOW_VALUE_INPUT))

        self.assertFalse(outcome.worth_remembering)
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(outcome.memories, ())
        self.assertIsNone(outcome.source)
        self.assertEqual(outcome.links, 0)
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_3_professional_knowledge_is_typed_and_traceable(self) -> None:
        service = self.build_service(
            response_for(
                payload(
                    memories=[
                        memory_item(
                            type="knowledge",
                            information_origin="source_content",
                            requires_source=True,
                            evidence_quote="RAG 把检索与生成结合",
                        )
                    ]
                )
            )
        )
        raw = RawInput(
            title="RAG 基础",
            content="Retrieval-Augmented Generation（RAG）把检索与生成结合：先检索相关片段，再基于片段生成答案。",
        )
        outcome = service.process(raw)

        memory = outcome.memories[0]
        self.assertEqual(memory.type, MemoryType.KNOWLEDGE)
        self.assertEqual(memory.information_origin, InformationOrigin.SOURCE_CONTENT)
        self.assertIsNotNone(outcome.source)
        self.assertEqual(outcome.source.content, raw.content)  # traceability: the raw text is kept
        self.assertTrue(self.repo.is_linked(memory.id, outcome.source.id))
        self.assertEqual(outcome.evidence[0]["quote"], "RAG 把检索与生成结合")

    def test_4_user_explicit_statement_keeps_its_origin(self) -> None:
        service = self.build_service(
            response_for(
                payload(
                    memories=[
                        memory_item(
                            type="profile",
                            title="正在系统学习 Agent",
                            content="用户正在系统学习 Agent，重点是底层原理，而不是只会调用现成工具。",
                            information_origin="user_explicit",
                            requires_source=False,
                            evidence_quote=None,
                            importance=0.9,
                            confidence=0.95,
                        )
                    ]
                )
            )
        )
        outcome = service.process(RawInput(content=HIGH_VALUE_INPUT))

        memory = outcome.memories[0]
        self.assertEqual(memory.information_origin, InformationOrigin.USER_EXPLICIT)
        self.assertEqual(memory.type, MemoryType.PROFILE)
        self.assertEqual(memory.status, MemoryStatus.ACTIVE)
        # a self-contained user statement does not need the raw text
        self.assertIsNone(outcome.source)
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 1, "memory_sources": 0})

    def test_5_low_confidence_inference_is_not_stored(self) -> None:
        service = self.build_service(
            response_for(
                payload(
                    memories=[
                        memory_item(
                            type="profile",
                            title="用户可能更喜欢结构化学习",
                            information_origin="agent_inference",
                            confidence=0.3,
                            requires_source=False,
                        )
                    ]
                )
            )
        )
        outcome = service.process(RawInput(content=HIGH_VALUE_INPUT))

        self.assertEqual(outcome.status, "skipped_by_policy")
        self.assertEqual(outcome.memories, ())
        self.assertTrue(any("confidence" in note for note in outcome.dropped))
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_5b_supported_inference_is_stored_as_a_pending_proposal(self) -> None:
        service = self.build_service(
            response_for(
                payload(
                    memories=[
                        memory_item(
                            type="profile",
                            title="用户可能更喜欢结构化学习",
                            content="用户反复强调原理，可能更喜欢结构化、成体系的学习材料。",
                            information_origin="agent_inference",
                            confidence=0.8,
                            requires_source=False,
                            evidence_quote=None,
                        )
                    ]
                )
            )
        )
        outcome = service.process(RawInput(content=HIGH_VALUE_INPUT))

        memory = outcome.memories[0]
        self.assertEqual(memory.information_origin, InformationOrigin.AGENT_INFERENCE)
        self.assertEqual(memory.status, MemoryStatus.PENDING)  # an inference is a proposal, not a fact
        self.assertAlmostEqual(memory.confidence, 0.8)
        self.assertEqual(self.repo.require_memory(memory.id).status, MemoryStatus.PENDING)

    def test_9_no_value_input_leaves_existing_data_untouched(self) -> None:
        self.make_source(content="已有的原始文本")
        existing_memory = self.make_memory(content="已有的记忆")
        before = self.repo.counts()

        service = self.build_service(response_for(payload(False, "无长期价值", [])))
        outcome = service.process(RawInput(content=LOW_VALUE_INPUT))

        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(self.repo.counts(), before)
        self.assertIsNotNone(self.repo.get_memory(existing_memory.id))

    def test_8_one_input_forms_several_memories_linked_to_one_source(self) -> None:
        memories = [
            memory_item(title=f"知识点 {index}", content=f"第 {index} 条独立结论。", requires_source=True)
            for index in range(3)
        ]
        service = self.build_service(response_for(payload(memories=memories)))
        raw = RawInput(title="多知识点文章", content="一篇文章里包含三个独立的知识点。")
        outcome = service.process(raw)

        self.assertEqual(outcome.status, "persisted")
        self.assertEqual(len(outcome.memories), 3)
        self.assertEqual(outcome.links, 3)
        self.assertEqual(self.repo.counts(), {"sources": 1, "memories": 3, "memory_sources": 3})
        # 1 Source -> 3 Memories, verified through the Phase 1 relation API
        linked = self.repo.get_memories_for_source(outcome.source.id)
        self.assertEqual({m.id for m in linked}, {m.id for m in outcome.memories})
        for memory in outcome.memories:
            self.assertEqual([s.id for s in self.repo.get_sources_for_memory(memory.id)], [outcome.source.id])


class InvalidOutputTest(FormationTestCase):
    def test_6_importance_out_of_range_is_rejected_and_nothing_is_written(self) -> None:
        bad = payload(memories=[memory_item(importance=2.5)])
        service = self.build_service(response_for(bad), response_for(bad), max_attempts=2)

        with self.assertRaises(ExtractionValidationError) as ctx:
            service.process(RawInput(content=HIGH_VALUE_INPUT))

        self.assertEqual(ctx.exception.attempts, 2)
        self.assertTrue(any("importance" in field for field in ctx.exception.fields))
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_6b_illegal_type_is_rejected(self) -> None:
        bad = payload(memories=[memory_item(type="idea")])
        service = self.build_service(response_for(bad), response_for(bad), max_attempts=2)
        with self.assertRaises(ExtractionValidationError) as ctx:
            service.process(RawInput(content=HIGH_VALUE_INPUT))
        self.assertTrue(any("type" in field for field in ctx.exception.fields))
        self.assertEqual(self.repo.counts()["memories"], 0)

    def test_6c_illegal_origin_and_non_boolean_flags_are_rejected(self) -> None:
        cases = [
            payload(memories=[memory_item(information_origin="model_guess")]),
            payload(memories=[memory_item(requires_source="yes")]),
            {"worth_remembering": "yes", "reason": "x", "memories": []},
            {"worth_remembering": True, "reason": "", "memories": []},
            {"worth_remembering": True, "reason": "x", "memories": []},  # inconsistent
            {"worth_remembering": False, "reason": "x", "memories": [memory_item()]},  # inconsistent
            {"worth_remembering": True, "reason": "x", "memories": [], "extra": 1},  # unknown field
            payload(memories=[{**memory_item(), "unknown_field": 1}]),
        ]
        for index, bad in enumerate(cases):
            with self.subTest(case=index):
                service = self.build_service(response_for(bad), max_attempts=1)
                with self.assertRaises(ExtractionValidationError):
                    service.process(RawInput(content=HIGH_VALUE_INPUT))
                self.assertEqual(self.repo.counts()["memories"], 0)

    def test_6d_phase1_limits_are_caught_at_parse_time(self) -> None:
        """Limit violations are reported to the model for a retry, not discovered mid-transaction."""
        cases = [
            memory_item(title="x" * 600),
            memory_item(tags=[f"tag{i}" for i in range(65)]),
            memory_item(tags=["y" * 80]),
        ]
        for index, item in enumerate(cases):
            with self.subTest(case=index):
                bad = response_for(payload(memories=[item]))
                service = self.build_service(bad, bad, max_attempts=2)
                with self.assertRaises(ExtractionValidationError):
                    service.process(RawInput(content=HIGH_VALUE_INPUT))
                self.assertEqual(self.client.transport.call_count, 2)  # invalid -> retried once
                self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_non_object_answer_is_retried_then_fails_explicitly(self) -> None:
        service = self.build_service(
            response_for("Here you go: {\"worth_remembering\": true}"),
            response_for("[1, 2, 3]"),
            max_attempts=2,
        )
        with self.assertRaises(ExtractionValidationError):
            service.process(RawInput(content=HIGH_VALUE_INPUT))
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_retry_after_invalid_answer_succeeds_and_reports_the_feedback(self) -> None:
        service = self.build_service(
            response_for(payload(memories=[memory_item(importance=7)])),
            response_for(payload(memories=[memory_item()])),
            max_attempts=2,
        )
        outcome = service.process(RawInput(content=HIGH_VALUE_INPUT))

        self.assertEqual(outcome.status, "persisted")
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(len(outcome.memories), 1)
        retry_request = self.client.transport.requests[1]
        self.assertIn("importance", retry_request.messages[-1].content)
        self.assertIn("上一次输出无效", retry_request.messages[-1].content)

    def test_7_api_failure_propagates_and_writes_nothing(self) -> None:
        service = self.build_service(LLMRequestError("provider is down", status_code=503, retryable=False))
        with self.assertRaises(LLMRequestError):
            service.process(RawInput(content=HIGH_VALUE_INPUT))
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_raw_input_validation(self) -> None:
        for kwargs in ({"content": "   "}, {"content": "ok", "url": "ftp://x"}, {"content": "ok", "source_type": "video"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ExtractionValidationError):
                RawInput(**kwargs)


class SourcePolicyTest(FormationTestCase):
    def test_source_is_persisted_when_a_memory_needs_the_raw_text(self) -> None:
        service = self.build_service(
            response_for(
                payload(
                    memories=[
                        memory_item(
                            information_origin="user_explicit",
                            requires_source=True,
                            evidence_quote="我 2019 年在慕尼黑工作时用的是这一套流程",
                        )
                    ]
                )
            )
        )
        outcome = service.process(RawInput(content="我 2019 年在慕尼黑工作时用的是这一套流程，细节记不清了。"))
        self.assertIsNotNone(outcome.source)
        self.assertIn("cannot be explained without the raw text", outcome.reason)

    def test_source_is_skipped_when_every_memory_is_self_contained(self) -> None:
        service = self.build_service(
            response_for(
                payload(
                    memories=[
                        memory_item(information_origin="user_explicit", requires_source=False, evidence_quote=None)
                    ]
                )
            )
        )
        outcome = service.process(RawInput(content="我不喜欢在晚上处理复杂任务。"))
        self.assertIsNone(outcome.source)
        self.assertIn("self-contained", outcome.reason)

    def test_keep_source_never_skips_a_source_that_is_not_strictly_required(self) -> None:
        # source_content is normally kept, but keep_source="never" opts out of that convenience
        service = self.build_service(
            response_for(
                payload(
                    memories=[
                        memory_item(
                            information_origin="source_content", requires_source=False, evidence_quote=None
                        )
                    ]
                )
            ),
            policy=FormationPolicy(keep_source="never"),
        )
        outcome = service.process(RawInput(content="原文内容 A"))
        self.assertIsNone(outcome.source)
        self.assertEqual(outcome.links, 0)
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 1, "memory_sources": 0})

    def test_keep_source_never_cannot_delete_a_required_source(self) -> None:
        """Traceability is a hard rule: no flag may drop the only evidence of a memory."""
        service = self.build_service(
            response_for(payload(memories=[memory_item(requires_source=True)])),
            policy=FormationPolicy(keep_source="never"),
        )
        outcome = service.process(RawInput(content="原文内容 A"))
        self.assertIsNotNone(outcome.source)
        self.assertEqual(outcome.links, 1)
        self.assertIn("cannot be explained without the raw text", outcome.reason)

    def test_keep_source_always(self) -> None:
        service = self.build_service(
            response_for(
                payload(memories=[memory_item(information_origin="user_explicit", requires_source=False)])
            ),
            policy=FormationPolicy(keep_source="always"),
        )
        outcome = service.process(RawInput(content="原文内容 B"))
        self.assertIsNotNone(outcome.source)
        self.assertEqual(outcome.links, 1)

    def test_source_is_reused_when_the_same_input_is_formed_twice(self) -> None:
        item = memory_item()
        service = self.build_service(
            response_for(payload(memories=[item])),
            response_for(payload(memories=[item])),
        )
        raw = RawInput(content="同一段原始输入。")
        first = service.process(raw)
        second = service.process(raw)

        self.assertFalse(first.source_reused)
        self.assertTrue(second.source_reused)
        self.assertEqual(first.source.id, second.source.id)
        counts = self.repo.counts()
        self.assertEqual(counts["sources"], 1)
        self.assertEqual(counts["memories"], 2)
        self.assertEqual(counts["memory_sources"], 2)

    def test_invalid_policy_is_rejected(self) -> None:
        with self.assertRaises(ExtractionValidationError):
            FormationPolicy(keep_source="sometimes")
        with self.assertRaises(ExtractionValidationError):
            FormationPolicy(max_memories=0)
        with self.assertRaises(ExtractionValidationError):
            FormationPolicy(agent_inference_min_confidence=2.0)


class PolicyAndProvenanceTest(FormationTestCase):
    def test_max_memories_cap_is_enforced(self) -> None:
        items = [memory_item(title=f"M{index}", content=f"结论 {index}") for index in range(7)]
        service = self.build_service(
            response_for(payload(memories=items)), policy=FormationPolicy(max_memories=2)
        )
        outcome = service.process(RawInput(content="包含七个知识点的长文"))
        self.assertEqual(len(outcome.memories), 2)
        self.assertEqual(len(outcome.dropped), 5)
        self.assertEqual(self.repo.counts()["memories"], 2)

    def test_dry_run_previews_without_writing(self) -> None:
        service = self.build_service(response_for(payload(memories=[memory_item()])))
        outcome = service.process(RawInput(content=HIGH_VALUE_INPUT), dry_run=True)
        self.assertEqual(outcome.status, "preview")
        self.assertEqual(len(outcome.memories), 1)
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_prompt_carries_the_value_criteria_and_version(self) -> None:
        service = self.build_service(response_for(payload(False, "无价值", [])))
        service.process(RawInput(content=LOW_VALUE_INPUT))

        request = self.client.transport.requests[0]
        system = request.messages[0].content
        for phrase in ("高价值", "低价值", "information_origin", "requires_source", "worth_remembering"):
            self.assertIn(phrase, system)
        self.assertIn("长", system)  # the "long text is not value" warning
        self.assertEqual(dict(request.metadata)["prompt_version"], PROMPT_VERSION)

    def test_source_metadata_records_the_formation_provenance(self) -> None:
        service = self.build_service(response_for(payload(memories=[memory_item()])))
        outcome = service.process(RawInput(content="带来源追溯的输入", metadata={"channel": "test"}))

        self.assertEqual(outcome.source.metadata["channel"], "test")
        formation = outcome.source.metadata["formation"]
        self.assertEqual(formation["prompt_version"], PROMPT_VERSION)
        self.assertEqual(formation["model"], self.client.config.model)

    def test_outcome_records_usage_and_model_for_audit(self) -> None:
        service = self.build_service(
            response_for(payload(memories=[memory_item()]), model="deepseek-flash", usage={"total_tokens": 123})
        )
        outcome = service.process(RawInput(content=HIGH_VALUE_INPUT))
        self.assertEqual(outcome.model, "deepseek-flash")
        self.assertEqual(outcome.llm["usage"]["total_tokens"], 123)
        self.assertEqual(outcome.prompt_version, PROMPT_VERSION)
        self.assertEqual(json.loads(json.dumps(outcome.as_dict()))["status"], "persisted")

    def test_formed_memory_is_readable_from_a_fresh_repository(self) -> None:
        service = self.build_service(response_for(payload(memories=[memory_item()])))
        outcome = service.process(RawInput(content=HIGH_VALUE_INPUT))

        fresh = MemoryRepository(Database(self.db_path))
        reloaded = fresh.require_memory(outcome.memories[0].id)
        self.assertIsInstance(reloaded, Memory)
        self.assertEqual(reloaded.content, outcome.memories[0].content)
        self.assertEqual(reloaded.tags, ["sqlite", "local-first"])
        self.assertEqual(
            [s.id for s in fresh.get_sources_for_memory(reloaded.id)], [outcome.source.id]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
