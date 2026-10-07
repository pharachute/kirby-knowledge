"""Phase 4 tests: Memory quality control -- exact duplicates and conservative conflicts.

Covers required behaviours 17-24 of the Phase 4 spec, plus the Phase 4 section 16
requirement that a model answer can only ever travel

    LLM output -> schema validation -> policy -> MemoryRepository

and never ``LLM -> database`` directly.  All model interaction is a Mock LLM
(:mod:`tests.llm_fakes`); no network and no credential is used.
"""

from __future__ import annotations

import json
import unittest

from personal_memory import (
    Database,
    ExtractionValidationError,
    LLMConflictClassifier,
    Memory,
    MemoryDraft,
    MemoryFormationService,
    MemoryQualityGate,
    MemoryRepository,
    MemoryRetriever,
    MemoryStatus,
    QualityError,
    QualityPolicy,
    RawInput,
    ValidationError,
    canonicalize_text,
    fingerprint_of,
    memory_fingerprint,
)
from personal_memory.quality import (
    RELATION_COMPATIBLE,
    RELATION_CONFLICT,
    RELATION_SAME,
    RELATION_UNCERTAIN,
    ConflictClassificationError,
    DuplicateScan,
)

from .helpers import RepositoryTestCase
from .llm_fakes import mock_client, response, response_for


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def draft_payload(**overrides):
    payload = {
        "type": "knowledge",
        "title": "RAG 基本原理",
        "content": "RAG 通过检索外部知识，为大语言模型提供相关上下文。",
        "summary": "检索增强生成。",
        "tags": ["rag"],
        "importance": 0.8,
        "confidence": 0.9,
        "information_origin": "user_explicit",
        "requires_source": False,
        "evidence_quote": None,
    }
    payload.update(overrides)
    return payload


def formation_payload(*drafts):
    return {"worth_remembering": True, "reason": "对用户长期有用", "memories": list(drafts)}


def draft(**overrides) -> MemoryDraft:
    payload = draft_payload(**overrides)
    return MemoryDraft(
        type=payload["type"],
        title=payload["title"],
        content=payload["content"],
        information_origin=payload["information_origin"],
        summary=payload["summary"],
        tags=tuple(payload["tags"]),
        importance=payload["importance"],
        confidence=payload["confidence"],
        requires_source=payload["requires_source"],
        evidence_quote=payload["evidence_quote"],
    )


def classifier_for(relation: str, reason: str = "mock reason") -> LLMConflictClassifier:
    return LLMConflictClassifier(mock_client(response_for({"relation": relation, "reason": reason})))


# --------------------------------------------------------------------------
# canonical fingerprint
# --------------------------------------------------------------------------

class FingerprintTest(unittest.TestCase):
    def test_normalisation_is_case_whitespace_and_nfkc_insensitive(self) -> None:
        self.assertEqual(
            memory_fingerprint("knowledge", "  RAG   基本原理 ", "RAG\t通过检索外部知识，为大语言模型提供相关上下文。"),
            memory_fingerprint("knowledge", "rag 基本原理", "RAG 通过检索外部知识，为大语言模型提供相关上下文。"),
        )
        # full-width characters are folded by NFKC
        self.assertEqual(memory_fingerprint("knowledge", "ＲＡＧ", "ｃ"), memory_fingerprint("knowledge", "RAG", "c"))

    def test_type_participates_and_punctuation_is_kept(self) -> None:
        self.assertNotEqual(memory_fingerprint("knowledge", "t", "c"), memory_fingerprint("profile", "t", "c"))
        # documented conservative choice: "likes A." != "likes A"
        self.assertNotEqual(memory_fingerprint("knowledge", "t", "likes A."), memory_fingerprint("knowledge", "t", "likes A"))

    def test_nfkc_folding_class_is_documented(self) -> None:
        """NFKC folds compatibility characters; this class of false positives is documented."""
        self.assertEqual(memory_fingerprint("knowledge", "①", "c"), memory_fingerprint("knowledge", "1", "c"))
        self.assertEqual(memory_fingerprint("knowledge", "㎡", "c"), memory_fingerprint("knowledge", "m2", "c"))
        self.assertEqual(memory_fingerprint("knowledge", "Ⅻ", "c"), memory_fingerprint("knowledge", "XII", "c"))
        self.assertEqual(memory_fingerprint("knowledge", "a，b", "c"), memory_fingerprint("knowledge", "a,b", "c"))
        self.assertEqual(memory_fingerprint("knowledge", "a\u00a0b", "c"), memory_fingerprint("knowledge", "a b", "c"))
        self.assertEqual(memory_fingerprint("knowledge", "Straße", "c"), memory_fingerprint("knowledge", "STRASSE", "c"))

    def test_fingerprint_rejects_bad_input(self) -> None:
        with self.assertRaises(ValidationError):
            memory_fingerprint("invalid-type", "t", "c")
        with self.assertRaises(ValidationError):
            canonicalize_text(42)
        with self.assertRaises(ValidationError):
            fingerprint_of(object())

    def test_canonicalize_text_returns_a_plain_normal_form(self) -> None:
        self.assertEqual(canonicalize_text("  A\u3000b\r\n C  "), "a b c")


# --------------------------------------------------------------------------
# 17-20: duplicate detection
# --------------------------------------------------------------------------

class DuplicateDetectionTest(RepositoryTestCase):
    prefix = "pms-quality-dup-"

    def setUp(self) -> None:
        super().setUp()
        self.gate = MemoryQualityGate(self.repo)

    def test_17_exact_duplicate_is_detected(self) -> None:
        existing = self.make_memory(
            title="RAG 基本原理", content="RAG 通过检索外部知识，为大语言模型提供相关上下文。"
        )
        candidate = draft(title="  rag   基本原理 ", content="RAG 通过检索外部知识，为大语言模型提供相关上下文。")

        check = self.gate.check_duplicate(candidate)

        self.assertTrue(check.is_duplicate)
        self.assertEqual(check.duplicate_of, existing.id)
        self.assertEqual(check.existing.id, existing.id)
        self.assertEqual(check.scanned, 1)

        decision = self.gate.evaluate_candidate(candidate)
        self.assertEqual(decision.action, "reuse")
        self.assertEqual(decision.relation, "duplicate")
        self.assertIsNone(decision.status)
        self.assertIn(existing.id, decision.related_ids)

    def test_17b_fingerprint_of_a_stored_memory_matches_its_draft(self) -> None:
        memory = self.make_memory(title="同一内容", content="同一内容正文 body")
        candidate = draft(title="同一内容", content="同一内容正文 body")
        self.assertEqual(fingerprint_of(memory), fingerprint_of(candidate))

    def test_18_different_memories_are_not_flagged(self) -> None:
        self.make_memory(title="RAG 基本原理", content="RAG 通过检索外部知识，为大语言模型提供相关上下文。")

        # different type, identical text
        self.assertFalse(self.gate.check_duplicate(draft(type="profile")).is_duplicate)
        # same type, different content
        self.assertFalse(
            self.gate.check_duplicate(draft(content="完全不同的正文，讲的是别的事情。")).is_duplicate
        )
        # same type, near-duplicate wording: NOT an exact duplicate (documented scope)
        self.assertFalse(
            self.gate.check_duplicate(
                draft(content="RAG 通过 Retrieval-Augmented Generation 为模型提供外部上下文。")
            ).is_duplicate
        )
        self.assertFalse(
            self.gate.check_duplicate(draft(title="RAG 的原理与用途")).is_duplicate
        )

    def test_18b_dedupe_can_be_disabled_by_policy(self) -> None:
        self.make_memory(title="RAG 基本原理", content="RAG 通过检索外部知识，为大语言模型提供相关上下文。")
        gate = MemoryQualityGate(self.repo, policy=QualityPolicy(dedupe=False))
        check = gate.check_duplicate(draft())
        self.assertFalse(check.is_duplicate)
        self.assertEqual(check.scanned, 0)
        self.assertIn("disabled", check.reason)

    def test_19_formation_does_not_create_a_second_identical_row(self) -> None:
        payload = formation_payload(draft_payload())
        service = MemoryFormationService(
            self.repo,
            mock_client(response_for(payload), response_for(payload)),
            quality=MemoryQualityGate(self.repo),
        )
        user_input = RawInput(content="RAG 通过检索外部知识，为大语言模型提供相关上下文。")

        first = service.process(user_input)
        second = service.process(user_input)

        self.assertEqual(first.status, "persisted")
        self.assertEqual(len(first.memories), 1)

        self.assertEqual(second.status, "duplicate")
        self.assertEqual(len(second.memories), 0)
        self.assertEqual([memory.id for memory in second.reused], [first.memories[0].id])
        self.assertIn("no second row", second.reason)

        self.assertEqual(self.repo.counts()["memories"], 1)
        self.assertEqual(second.quality.reused, 1)
        self.assertEqual(second.quality.persisted, 0)
        self.assertEqual(second.quality.duplicate_ids, (first.memories[0].id,))
        # the whole outcome is JSON-serializable (the CLI prints it)
        json.dumps(second.as_dict(), ensure_ascii=False)

    def test_19b_two_identical_drafts_in_one_answer_write_one_row(self) -> None:
        payload = formation_payload(draft_payload(), draft_payload())
        service = MemoryFormationService(
            self.repo,
            mock_client(response_for(payload)),
            quality=MemoryQualityGate(self.repo),
        )

        outcome = service.process(RawInput(content="RAG 通过检索外部知识，为大语言模型提供相关上下文。"))

        self.assertEqual(outcome.status, "persisted")
        self.assertEqual(len(outcome.memories), 1)
        self.assertEqual(len(outcome.reused), 1)
        self.assertEqual(outcome.reused[0].id, outcome.memories[0].id)
        self.assertEqual(self.repo.counts()["memories"], 1)

    def test_19c_phase_2_without_a_gate_still_writes_every_draft(self) -> None:
        """Regression guard: the Phase 2 path is unchanged when no gate is configured."""
        payload = formation_payload(draft_payload(), draft_payload())
        service = MemoryFormationService(self.repo, mock_client(response_for(payload)))

        outcome = service.process(RawInput(content="RAG 通过检索外部知识，为大语言模型提供相关上下文。"))

        self.assertEqual(outcome.status, "persisted")
        self.assertEqual(len(outcome.memories), 2)  # Phase 2 behaviour, unchanged
        self.assertEqual(outcome.reused, ())
        self.assertIsNone(outcome.quality)

    def test_20_sources_survive_memory_deduplication(self) -> None:
        payload = formation_payload(
            draft_payload(requires_source=True, evidence_quote="RAG 通过检索外部知识")
        )
        service = MemoryFormationService(
            self.repo,
            mock_client(response_for(payload), response_for(payload)),
            quality=MemoryQualityGate(self.repo),
        )
        user_input = RawInput(content="RAG 通过检索外部知识，为大语言模型提供相关上下文。")

        first = service.process(user_input)
        self.assertIsNotNone(first.source)
        source_id = first.source.id
        memory_id = first.memories[0].id
        self.assertEqual(self.repo.get_sources_for_memory(memory_id)[0].id, source_id)

        second = service.process(user_input)

        self.assertEqual(second.status, "duplicate")
        self.assertIsNone(second.source)  # nothing new to trace
        fresh = MemoryRepository(Database(self.db_path))
        self.assertEqual(fresh.counts(), {"sources": 1, "memories": 1, "memory_sources": 1})
        stored_source = fresh.require_source(source_id)
        self.assertEqual(stored_source.content, user_input.content)
        self.assertEqual([source.id for source in fresh.get_sources_for_memory(memory_id)], [source_id])

    def test_scan_duplicates_reports_what_it_read(self) -> None:
        self.make_memory(title="扫描 A")
        self.make_memory(title="扫描 B", status=MemoryStatus.ARCHIVED)
        scan = self.gate.scan_duplicates()
        self.assertEqual(scan.scanned, 2)  # archived rows count: no second row either
        self.assertEqual(len(scan.fingerprints), 2)
        self.assertIsInstance(scan, DuplicateScan)
        self.assertEqual(scan.as_dict()["scanned"], 2)


# --------------------------------------------------------------------------
# 21-24: conflict handling
# --------------------------------------------------------------------------

class ConflictHandlingTest(RepositoryTestCase):
    prefix = "pms-quality-conflict-"

    #: The old and the new Memory describe the same subject, so Phase 3 keyword
    #: retrieval links them -- which is what triggers classification.
    OLD_TITLE = "用户偏好的数据库方案"
    OLD_CONTENT = "用户喜欢 A 方案，把它用于个人知识库。"
    NEW_CONTENT = "用户更喜欢 B 方案，把它用于个人知识库。"

    def setUp(self) -> None:
        super().setUp()
        self.old = self.make_memory(title=self.OLD_TITLE, content=self.OLD_CONTENT, type="profile")

    def run_formation(self, relation: str, content: str | None = None) -> tuple[object, Memory]:
        """One formation run whose candidate is classified with ``relation``."""
        payload = formation_payload(
            draft_payload(type="profile", title=self.OLD_TITLE, content=content or self.NEW_CONTENT)
        )
        gate = MemoryQualityGate(self.repo, classifier=classifier_for(relation))
        service = MemoryFormationService(self.repo, mock_client(response_for(payload)), quality=gate)
        outcome = service.process(RawInput(content=content or self.NEW_CONTENT))
        return outcome, self.old

    def assert_old_untouched(self, old: Memory) -> None:
        stored = self.repo.require_memory(old.id)
        self.assertEqual(stored.as_dict(), old.as_dict())
        self.assertEqual(str(stored.status), "active")

    def test_21_a_conflict_becomes_pending_and_the_old_memory_stays(self) -> None:
        outcome, old = self.run_formation(RELATION_CONFLICT)

        self.assertEqual(outcome.status, "persisted")
        self.assertEqual(len(outcome.memories), 1)
        new_memory = self.repo.require_memory(outcome.memories[0].id)
        self.assertEqual(str(new_memory.status), "pending")
        self.assertEqual(outcome.quality.pended, 1)
        self.assertEqual(outcome.quality.decisions[0].relation, RELATION_CONFLICT)
        self.assertEqual(outcome.quality.decisions[0].related_ids, (old.id,))
        self.assert_old_untouched(old)

        # the pending candidate is not recalled by default, the old one still is
        retriever = MemoryRetriever(self.repo)
        titles = [hit.memory.title for hit in retriever.search(self.OLD_TITLE).hits]
        self.assertEqual(titles, [self.OLD_TITLE])
        self.assertEqual(self.repo.counts()["memories"], 2)

    def test_22_compatible_information_is_not_flagged_as_a_conflict(self) -> None:
        outcome, old = self.run_formation(RELATION_COMPATIBLE)

        new_memory = self.repo.require_memory(outcome.memories[0].id)
        self.assertEqual(str(new_memory.status), "active")
        self.assertEqual(outcome.quality.decisions[0].relation, RELATION_COMPATIBLE)
        self.assertEqual(outcome.quality.pended, 0)
        self.assert_old_untouched(old)
        retriever = MemoryRetriever(self.repo)
        self.assertEqual(len(retriever.search(self.OLD_TITLE).hits), 2)

    def test_23_uncertain_never_overwrites_the_old_memory(self) -> None:
        outcome, old = self.run_formation(RELATION_UNCERTAIN)

        new_memory = self.repo.require_memory(outcome.memories[0].id)
        self.assertEqual(str(new_memory.status), "pending")
        self.assertEqual(outcome.quality.decisions[0].relation, RELATION_UNCERTAIN)
        self.assert_old_untouched(old)

    def test_23b_same_relation_is_stored_as_pending_and_never_merged(self) -> None:
        outcome, old = self.run_formation(RELATION_SAME)

        new_memory = self.repo.require_memory(outcome.memories[0].id)
        self.assertEqual(str(new_memory.status), "pending")
        self.assertEqual(outcome.quality.decisions[0].relation, RELATION_SAME)
        self.assert_old_untouched(old)
        self.assertEqual(self.repo.counts()["memories"], 2)  # nothing merged or deleted

    def test_24_the_old_memory_is_never_modified_by_any_relation(self) -> None:
        for relation in (RELATION_SAME, RELATION_COMPATIBLE, RELATION_CONFLICT, RELATION_UNCERTAIN):
            with self.subTest(relation=relation):
                repo = MemoryRepository(Database(self.db_path))
                old = repo.list_memories(status=MemoryStatus.ACTIVE)[0]
                gate = MemoryQualityGate(repo, classifier=classifier_for(relation))
                decision = gate.evaluate_candidate(
                    draft(type="profile", title=self.OLD_TITLE, content=self.NEW_CONTENT)
                )
                self.assertIn(decision.status, {"active", "pending"})
                self.assertEqual(repo.require_memory(old.id).as_dict(), old.as_dict())
                # a classification is a read-only operation: no row is written by it
                self.assertEqual(repo.counts(), {"sources": 0, "memories": 1, "memory_sources": 0})

    def test_24b_a_classifier_alone_cannot_write(self) -> None:
        classifier = classifier_for(RELATION_CONFLICT)
        related = self.repo.list_memories()

        verdict = classifier.classify(draft(), related)

        self.assertEqual(verdict.relation, RELATION_CONFLICT)
        self.assertEqual(verdict.classifier, "llm")
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 1, "memory_sources": 0})
        self.assertEqual(str(self.repo.require_memory(self.old.id).status), "active")

    def test_unrelated_candidate_keeps_the_proposed_status_without_calling_the_model(self) -> None:
        gate = MemoryQualityGate(self.repo, classifier=classifier_for(RELATION_CONFLICT))
        decision = gate.evaluate_candidate(draft(title="完全无关的新话题", content="关于独立主题的全新内容 nonsense"))
        self.assertEqual(decision.relation, "none")
        self.assertEqual(decision.status, "active")
        self.assertEqual(decision.related_ids, ())

    def test_pending_memories_participate_as_conflict_partners(self) -> None:
        self.repo.update_memory(self.old.id, status="archived")
        pending_old = self.make_memory(
            title=self.OLD_TITLE, content="用户更喜欢 B 方案，已经记录在案。", type="profile", status=MemoryStatus.PENDING
        )
        gate = MemoryQualityGate(self.repo, classifier=classifier_for(RELATION_CONFLICT))
        check = gate.check_conflict(draft(type="profile", title=self.OLD_TITLE, content=self.NEW_CONTENT))
        self.assertEqual([memory.id for memory in check.related], [pending_old.id])
        self.assertEqual(check.relation, RELATION_CONFLICT)

    def test_exclude_ids_keeps_a_candidate_from_conflicting_with_itself(self) -> None:
        gate = MemoryQualityGate(self.repo, classifier=classifier_for(RELATION_CONFLICT))
        check = gate.check_conflict(
            draft(type="profile", title=self.OLD_TITLE, content=self.OLD_CONTENT),
            exclude_ids=(self.old.id,),
        )
        self.assertEqual(check.related, ())
        self.assertEqual(check.relation, "none")


# --------------------------------------------------------------------------
# policy + classifier wiring
# --------------------------------------------------------------------------

class QualityPolicyTest(RepositoryTestCase):
    prefix = "pms-quality-policy-"

    def test_conflict_check_can_be_disabled_and_keeps_the_proposed_status(self) -> None:
        self.make_memory(title="策略测试记忆", content="内容 policy-check")
        gate = MemoryQualityGate(self.repo, policy=QualityPolicy(conflict_check=False))
        decision = gate.evaluate_candidate(draft(title="策略测试记忆", content="内容 policy-check two"))
        self.assertEqual(decision.relation, "unchecked")
        self.assertEqual(decision.status, "active")
        check = gate.check_conflict(draft(title="策略测试记忆", content="内容 policy-check two"))
        self.assertEqual(check.related, ())
        self.assertIn("disabled", check.reason)

    def test_related_memories_without_a_classifier_are_conservatively_pending(self) -> None:
        self.make_memory(title="没有分类器的记忆", content="内容 no-classifier")
        gate = MemoryQualityGate(self.repo)  # no classifier configured
        decision = gate.evaluate_candidate(draft(title="没有分类器的记忆", content="内容 no-classifier two"))
        self.assertEqual(decision.relation, RELATION_UNCERTAIN)
        self.assertEqual(decision.status, "pending")
        self.assertEqual(decision.conflict.verdict.classifier, "none")

    def test_policy_rejects_unsafe_configuration(self) -> None:
        with self.assertRaises(ValidationError):
            QualityPolicy(conservative_status=MemoryStatus.ACTIVE)
        with self.assertRaises(ValidationError):
            QualityPolicy(related_limit=0)
        with self.assertRaises(ValidationError):
            QualityPolicy(related_statuses=())
        with self.assertRaises(ValidationError):
            QualityPolicy(unclassified_relation="maybe")
        with self.assertRaises(ValidationError):
            QualityPolicy(scan_batch_size=0)
        with self.assertRaises(ValidationError):
            QualityPolicy(related_statuses=("deleted",))

    def test_evaluating_a_stored_memory_does_not_conflict_with_itself(self) -> None:
        stored = self.make_memory(title="自我比较", content="内容 self-compare")
        gate = MemoryQualityGate(self.repo, classifier=classifier_for(RELATION_CONFLICT))

        # the candidate's own id is excluded, so it never becomes its own "related" Memory
        check = gate.check_conflict(stored)
        self.assertEqual(check.related_ids, ())
        self.assertEqual(check.relation, "none")

        # and an exact copy of itself is reported as reuse (no write, no new row)
        decision = gate.evaluate_candidate(stored)
        self.assertEqual(decision.action, "reuse")
        self.assertEqual(decision.duplicate_of, stored.id)
        self.assertEqual(self.repo.counts()["memories"], 1)

    def test_policy_rejects_a_non_conservative_unclassified_relation(self) -> None:
        with self.assertRaises(ValidationError):
            QualityPolicy(unclassified_relation="compatible")

    def test_a_classifier_returning_a_non_verdict_is_rejected(self) -> None:
        class DictClassifier:
            def classify(self, candidate, related):  # noqa: ANN001 - test double
                return {"relation": "conflict", "reason": "dict"}

        self.make_memory(title="字典分类器", content="内容 dict-classifier")
        gate = MemoryQualityGate(self.repo, classifier=DictClassifier())
        with self.assertRaises(QualityError):
            gate.check_conflict(draft(title="字典分类器", content="内容 dict-classifier two"))

    def test_a_surrogate_candidate_is_rejected_before_any_write(self) -> None:
        service = MemoryFormationService(
            self.repo,
            mock_client(response_for(formation_payload(draft_payload(title="bad\ud800title")))),
            quality=MemoryQualityGate(self.repo),
        )
        with self.assertRaises(ExtractionValidationError):
            service.process(RawInput(content="正文"))
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_scan_requires_a_repository_like_source(self) -> None:
        gate = MemoryQualityGate(self.repo)
        with self.assertRaises(ValidationError):
            gate.scan_duplicates(source=object())


class ConflictClassifierTest(unittest.TestCase):
    """Phase 4 section 16: schema validation before any policy decision."""

    def test_all_four_relations_are_accepted(self) -> None:
        for relation in (RELATION_SAME, RELATION_COMPATIBLE, RELATION_CONFLICT, RELATION_UNCERTAIN):
            with self.subTest(relation=relation):
                verdict = LLMConflictClassifier.parse_verdict({"relation": relation, "reason": "r"})
                self.assertEqual(verdict.relation, relation)
                self.assertEqual(verdict.classifier, "llm")

    def test_invalid_answers_are_rejected_with_field_level_problems(self) -> None:
        cases = (
            {"relation": "duplicate", "reason": "r"},
            {"relation": "CONFLICT", "reason": "r"},  # normalised, accepted -> checked below
            {"relation": "conflict", "reason": ""},
            {"relation": "conflict", "reason": "r", "action": "delete_old"},
            {"reason": "r"},
            ["not", "an", "object"],
        )
        for payload in cases:
            with self.subTest(payload=payload):
                if payload == {"relation": "CONFLICT", "reason": "r"}:
                    self.assertEqual(LLMConflictClassifier.parse_verdict(payload).relation, "conflict")
                    continue
                with self.assertRaises(ConflictClassificationError) as caught:
                    LLMConflictClassifier.parse_verdict(payload)
                self.assertTrue(caught.exception.problems)

    def test_retry_then_explicit_failure(self) -> None:
        classifier = LLMConflictClassifier(
            mock_client(response("这不是 JSON"), response_for({"relation": "nonsense", "reason": "x"})),
            max_attempts=2,
        )
        related = [Memory.create(type="profile", title="t", content="c", information_origin="user_explicit")]
        with self.assertRaises(ConflictClassificationError) as caught:
            classifier.classify(draft(), related)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertGreaterEqual(len(caught.exception.problems), 2)

    def test_a_usable_answer_on_the_second_attempt_is_used(self) -> None:
        classifier = LLMConflictClassifier(
            mock_client(response("not json"), response_for({"relation": "compatible", "reason": "second try"})),
            max_attempts=2,
        )
        related = [Memory.create(type="profile", title="t", content="c", information_origin="user_explicit")]
        verdict = classifier.classify(draft(), related)
        self.assertEqual(verdict.relation, "compatible")
        self.assertEqual(verdict.reason, "second try")
        self.assertEqual(verdict.llm["attempts"], 2)
        self.assertEqual(classifier.last_verdict.relation, "compatible")

    def test_classification_requires_related_memories(self) -> None:
        classifier = classifier_for(RELATION_CONFLICT)
        with self.assertRaises(QualityError):
            classifier.classify(draft(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
