"""Phase 2B-3: ``LearningService.record_assessment`` -- explicit learning assessment.

Locked behaviour:

* only the four assessment fields may change (``understanding_level`` + the three
  aspect lists); ``learn_count`` / ``last_learned_at`` stay untouched
* the session is not touched at all (no cursor/stage/status movement, no
  auto-finish): assessment and progress are fully decoupled
* the target must be the session's current Memory; a stale/exhausted session can
  never be used to write into another Memory
* a missing ``LearningState`` is created first, so the row ends up
  ``learn_count = 0`` / ``last_learned_at = NULL`` + the submitted assessment
* the level is validated against the existing enum (no new levels, no silent
  conversion); aspect lists follow the Phase 2A model rules
* the frozen 1.0 tables, index and retrieval stay byte-for-byte unaffected
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from unittest import mock

from personal_memory import (
    LearningRepository,
    LearningService,
    LearningState,
    MemoryStatus,
    UnderstandingLevel,
)
from personal_memory.errors import ConflictError, NotFoundError, ValidationError
from personal_memory.learning import PUBLIC_API
from personal_memory.learning_models import MAX_ASPECTS, MAX_ASPECT_LENGTH
from personal_memory.learning_store import LearningUnitOfWork
from personal_memory.retrieval import MemoryRetriever

from .helpers import RepositoryTestCase


class AssessmentTestCase(RepositoryTestCase):
    """One Source with three Memories (A, B, C); the session starts on A."""

    prefix = "pms-learnassess-"

    def setUp(self) -> None:
        super().setUp()
        self.learning = LearningRepository(self.database)
        self.service = LearningService(self.learning, self.repo)
        self.source = self.make_source(title="高等数学第一章")
        self.a = self.make_memory(title="极限")
        self.b = self.make_memory(title="连续")
        self.c = self.make_memory(title="切线斜率")
        for memory in (self.a, self.b, self.c):
            self.repo.link(memory.id, self.source.id)
        self.session_id = self.service.start_session(source_id=self.source.id).session.id

    # -- helpers ----------------------------------------------------------
    def session(self):
        return self.learning.get_session(self.session_id)

    def state(self, memory):
        return self.learning.get_state(memory.id)

    def assess(self, memory=None, **kwargs):
        return self.service.record_assessment(
            self.session_id, memory_id=(memory.id if memory is not None else None), **kwargs
        )

    def advance_to_b(self) -> None:
        self.service.record_learning(self.session_id)          # A learned -> session on B


class BasicAssessmentTest(AssessmentTestCase):
    def test_level_is_persisted(self) -> None:
        self.service.record_assessment(self.session_id, understanding_level=UnderstandingLevel.PARTIAL)

        self.assertEqual(str(self.state(self.a).understanding_level), "partial")

    def test_all_four_fields_are_persisted_together(self) -> None:
        self.service.record_assessment(
            self.session_id,
            understanding_level="solid",
            known_aspects=["极限是趋近", "ε-δ 语言"],
            weak_aspects=["极限的严格定义"],
            misconceptions=["极限等于函数值"],
        )

        state = self.state(self.a)
        self.assertEqual(str(state.understanding_level), "solid")
        self.assertEqual(state.known_aspects, ["极限是趋近", "ε-δ 语言"])
        self.assertEqual(state.weak_aspects, ["极限的严格定义"])
        self.assertEqual(state.misconceptions, ["极限等于函数值"])

    def test_context_reflects_the_assessment(self) -> None:
        context = self.service.record_assessment(
            self.session_id, understanding_level="fuzzy", weak_aspects=["ε-δ"])

        self.assertEqual(context.session.id, self.session_id)
        self.assertEqual(str(context.state_for(self.a.id).understanding_level), "fuzzy")
        self.assertEqual(context.state_for(self.a.id).weak_aspects, ["ε-δ"])
        self.assertEqual(context.created_state_ids, ())          # the state already existed

    def test_a_repeated_assessment_overwrites_the_previous_one(self) -> None:
        self.service.record_assessment(self.session_id, understanding_level="fuzzy",
                                       weak_aspects=["ε-δ"])
        self.service.record_assessment(self.session_id, understanding_level="partial",
                                       weak_aspects=["严格定义"])

        state = self.state(self.a)
        self.assertEqual(str(state.understanding_level), "partial")
        self.assertEqual(state.weak_aspects, ["严格定义"])           # replaced, not appended
        self.assertEqual(self.learning.count_states(), 3)            # no duplicate row

    def test_updated_at_advances(self) -> None:
        before = self.state(self.a).updated_at
        self.service.record_assessment(self.session_id, understanding_level="solid")

        after = self.state(self.a)
        self.assertNotEqual(after.updated_at, before)
        self.assertEqual(after.created_at, self.learning.get_state(self.a.id).created_at)

    def test_empty_list_clears_a_list_and_none_leaves_it_alone(self) -> None:
        self.service.record_assessment(self.session_id, understanding_level="partial",
                                       known_aspects=["极限是趋近"], weak_aspects=["ε-δ"])
        # omit both lists -> untouched
        self.service.record_assessment(self.session_id, understanding_level="partial")
        state = self.state(self.a)
        self.assertEqual(state.known_aspects, ["极限是趋近"])
        self.assertEqual(state.weak_aspects, ["ε-δ"])
        # explicit [] -> cleared (the model's own rule), only the given one
        self.service.record_assessment(self.session_id, understanding_level="partial",
                                       known_aspects=[])
        state = self.state(self.a)
        self.assertEqual(state.known_aspects, [])
        self.assertEqual(state.weak_aspects, ["ε-δ"])

    def test_assessment_does_not_create_extra_state_rows(self) -> None:
        for level in ("fuzzy", "partial", "solid", "unknown"):
            self.service.record_assessment(self.session_id, understanding_level=level)

        self.assertEqual(self.learning.count_states(), 3)
        self.assertEqual(self.learning.counts()["learning_sessions"], 1)


class FieldIsolationTest(AssessmentTestCase):
    def test_learn_count_and_last_learned_at_are_untouched(self) -> None:
        self.learning.upsert_state(LearningState.create(
            memory_id=self.a.id, learn_count=3, last_learned_at="2026-10-06T08:00:00.000Z"))

        self.service.record_assessment(self.session_id, understanding_level="solid")

        state = self.state(self.a)
        self.assertEqual(state.learn_count, 3)
        self.assertEqual(state.last_learned_at, "2026-10-06T08:00:00.000Z")

    def test_untouched_when_the_state_was_fresh(self) -> None:
        self.service.record_assessment(self.session_id, understanding_level="partial")

        state = self.state(self.a)
        self.assertEqual(state.learn_count, 0)
        self.assertIsNone(state.last_learned_at)

    def test_session_does_not_change_at_all(self) -> None:
        self.advance_to_b()
        before = self.session().as_dict()

        self.service.record_assessment(self.session_id, understanding_level="solid")

        self.assertEqual(self.session().as_dict(), before)

    def test_cursor_and_current_memory_do_not_advance(self) -> None:
        self.advance_to_b()
        self.assertEqual((self.session().plan_cursor, self.session().current_memory_id), (1, self.b.id))

        self.service.record_assessment(self.session_id, understanding_level="solid")

        session = self.session()
        self.assertEqual(session.plan_cursor, 1)
        self.assertEqual(session.current_memory_id, self.b.id)

    def test_stage_and_status_are_untouched(self) -> None:
        before = (str(self.session().current_stage), str(self.session().status))

        self.service.record_assessment(self.session_id, understanding_level="fuzzy")

        self.assertEqual((str(self.session().current_stage), str(self.session().status)), before)
        self.assertEqual(before, ("explain", "active"))

    def test_assessing_a_non_current_memory_is_impossible(self) -> None:
        """There is no way to write an assessment into another Memory of the plan."""
        self.advance_to_b()

        with self.assertRaises(ConflictError):
            self.service.record_assessment(self.session_id, memory_id=self.a.id,
                                           understanding_level="solid")

        self.assertEqual(str(self.state(self.a).understanding_level), "unknown")
        self.assertEqual(str(self.state(self.b).understanding_level), "unknown")

    def test_plan_exchange_and_ended_at_are_untouched(self) -> None:
        self.learning.update_session(self.session_id, exchange={"question": "什么是极限？"})
        before = self.session().as_dict()

        self.service.record_assessment(self.session_id, understanding_level="solid")

        after = self.session().as_dict()
        self.assertEqual(after["plan"], before["plan"])
        self.assertEqual(after["exchange"], before["exchange"])
        self.assertIsNone(after["ended_at"])


class SessionBoundaryTest(AssessmentTestCase):
    def test_unknown_session(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.service.record_assessment("lrn_missing", understanding_level="solid")
        self.assertEqual(ctx.exception.entity, "learning session")
        self.assertEqual(self.learning.count_states(), 3)

    def test_completed_session(self) -> None:
        self.service.finish_session(self.session_id)

        with self.assertRaises(ConflictError) as ctx:
            self.service.record_assessment(self.session_id, understanding_level="solid")
        self.assertIn("only an active session", str(ctx.exception))
        self.assertEqual(str(self.state(self.a).understanding_level), "unknown")

    def test_abandoned_session(self) -> None:
        self.service.abandon_session(self.session_id)

        with self.assertRaises(ConflictError):
            self.service.record_assessment(self.session_id, understanding_level="solid")
        self.assertEqual(str(self.state(self.a).understanding_level), "unknown")

    def test_session_without_a_current_memory(self) -> None:
        self.learning.update_session(self.session_id, current_memory_id=None)

        with self.assertRaises(ConflictError) as ctx:
            self.service.record_assessment(self.session_id, understanding_level="solid")
        self.assertIn("no current Memory", str(ctx.exception))

    def test_exhausted_plan(self) -> None:
        for _ in range(3):
            self.service.record_learning(self.session_id)

        with self.assertRaises(ConflictError) as ctx:
            self.service.record_assessment(self.session_id, understanding_level="solid")
        self.assertIn("finished its plan", str(ctx.exception))

    def test_memory_id_must_name_the_current_memory(self) -> None:
        with self.assertRaises(ConflictError) as ctx:
            self.service.record_assessment(self.session_id, memory_id=self.b.id,
                                           understanding_level="solid")
        self.assertIn("not the Memory this session is on", str(ctx.exception))

    def test_unknown_memory_id(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.service.record_assessment(self.session_id, memory_id="mem_missing",
                                           understanding_level="solid")
        self.assertEqual(ctx.exception.entity, "memory")

    def test_memory_id_must_be_a_non_empty_string(self) -> None:
        for bad in (42, "", "   "):
            with self.assertRaises(ValidationError) as ctx:
                self.service.record_assessment(self.session_id, memory_id=bad,
                                               understanding_level="solid")
            self.assertEqual(ctx.exception.fields, ("memory_id",))

    def test_current_memory_diverging_from_the_plan(self) -> None:
        self.learning.update_session(self.session_id, current_memory_id=self.b.id)

        with self.assertRaises(ConflictError) as ctx:
            self.service.record_assessment(self.session_id, understanding_level="solid")
        self.assertIn("inconsistent", str(ctx.exception))

    def test_a_deleted_current_memory_is_reported_clearly(self) -> None:
        self.repo.delete_memory(self.a.id)

        with self.assertRaises(ConflictError) as ctx:
            self.service.record_assessment(self.session_id, understanding_level="solid")
        self.assertIn("no current Memory", str(ctx.exception))


class LearningStateInteractionTest(AssessmentTestCase):
    def test_missing_state_is_created_with_the_model_defaults(self) -> None:
        with self.database.transaction() as conn:
            conn.execute("DELETE FROM learning_states WHERE memory_id = ?", (self.a.id,))
        self.assertIsNone(self.state(self.a))

        context = self.service.record_assessment(self.session_id, understanding_level="partial",
                                                 known_aspects=["极限是趋近"])

        state = self.state(self.a)
        self.assertEqual(str(state.understanding_level), "partial")
        self.assertEqual(state.known_aspects, ["极限是趋近"])
        self.assertEqual(state.learn_count, 0)                  # still not a learning event
        self.assertIsNone(state.last_learned_at)
        self.assertEqual(context.created_state_ids, ())          # record_assessment does not report creations

    def test_existing_state_is_reused_not_replaced(self) -> None:
        self.learning.upsert_state(LearningState.create(
            memory_id=self.a.id, understanding_level="fuzzy", known_aspects=["旧的方面"],
            learn_count=4, last_learned_at="2026-10-06T08:00:00.000Z"))
        stored_created_at = self.state(self.a).created_at      # the row's own timestamp

        self.service.record_assessment(self.session_id, understanding_level="solid")

        state = self.state(self.a)
        self.assertEqual(state.created_at, stored_created_at)
        self.assertEqual(state.learn_count, 4)
        self.assertEqual(state.known_aspects, ["旧的方面"])       # not passed -> untouched
        self.assertEqual(str(state.understanding_level), "solid")

    def test_no_state_is_created_for_a_memory_outside_the_plan(self) -> None:
        outside = self.make_memory(title="计划外的记忆")
        self.repo.link(outside.id, self.source.id)

        self.service.record_assessment(self.session_id, understanding_level="solid")

        self.assertIsNone(self.state(outside))

    def test_assessment_first_then_a_learning_event(self) -> None:
        """The two operations are independent: assess, then still record the event."""
        self.service.record_assessment(self.session_id, understanding_level="partial",
                                       known_aspects=["极限是趋近"])
        self.assertEqual(self.state(self.a).learn_count, 0)

        context = self.service.record_learning(self.session_id)

        state = self.state(self.a)
        self.assertEqual(state.learn_count, 1)                  # the event counted once
        self.assertIsNotNone(state.last_learned_at)
        self.assertEqual(str(state.understanding_level), "partial")   # the judgement survived
        self.assertEqual(state.known_aspects, ["极限是趋近"])
        self.assertEqual((context.session.plan_cursor, context.session.current_memory_id),
                         (1, self.b.id))

    def test_learning_event_then_assessment_of_the_next_memory(self) -> None:
        self.service.record_learning(self.session_id)            # A learned
        self.service.record_assessment(self.session_id, understanding_level="fuzzy")

        self.assertEqual(str(self.state(self.a).understanding_level), "unknown")   # A untouched
        self.assertEqual(str(self.state(self.b).understanding_level), "fuzzy")
        self.assertEqual(self.state(self.a).learn_count, 1)
        self.assertEqual(self.state(self.b).learn_count, 0)

    def test_repeated_assessment_keeps_one_row_and_latest_values(self) -> None:
        self.service.record_assessment(self.session_id, understanding_level="fuzzy",
                                       misconceptions=["极限等于函数值"])
        self.service.record_assessment(self.session_id, understanding_level="solid",
                                       misconceptions=[])

        state = self.state(self.a)
        self.assertEqual(str(state.understanding_level), "solid")
        self.assertEqual(state.misconceptions, [])
        self.assertEqual(self.learning.count_states(), 3)


class ValidationTest(AssessmentTestCase):
    def assert_level_rejected(self, level) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.service.record_assessment(self.session_id, understanding_level=level)
        self.assertEqual(ctx.exception.fields, ("understanding_level",))
        self.assertEqual(str(self.state(self.a).understanding_level), "unknown")

    def test_illegal_levels_are_rejected(self) -> None:
        for illegal in ("mastered", "learning", "familiar", "", "UNKNOWN_LEVEL", 42, None, 0.5, True):
            self.assert_level_rejected(illegal)

    def test_all_existing_levels_are_accepted(self) -> None:
        for level in UnderstandingLevel:
            context = self.service.record_assessment(self.session_id, understanding_level=level)
            self.assertEqual(str(context.state_for(self.a.id).understanding_level), str(level))

    def test_level_strings_are_normalised_like_the_rest_of_the_project(self) -> None:
        self.service.record_assessment(self.session_id, understanding_level="  SOLID ")
        self.assertEqual(str(self.state(self.a).understanding_level), "solid")

    def test_enum_members_are_accepted(self) -> None:
        self.service.record_assessment(self.session_id,
                                       understanding_level=UnderstandingLevel.PARTIAL)
        self.assertEqual(str(self.state(self.a).understanding_level), "partial")

    def test_illegal_aspects_are_rejected(self) -> None:
        cases = (
            {"known_aspects": "极限"},
            {"known_aspects": [42]},
            {"known_aspects": ["   "]},
            {"weak_aspects": [""]},
            {"misconceptions": ["x" * (MAX_ASPECT_LENGTH + 1)]},
            {"known_aspects": [f"要点{i}" for i in range(MAX_ASPECTS + 1)]},
        )
        for payload in cases:
            with self.assertRaises(ValidationError) as ctx:
                self.service.record_assessment(self.session_id, understanding_level="solid", **payload)
            self.assertTrue(set(payload) <= set(ctx.exception.fields), payload)
        self.assertEqual(self.state(self.a).known_aspects, [])
        self.assertEqual(self.state(self.a).weak_aspects, [])
        self.assertEqual(self.state(self.a).misconceptions, [])

    def test_aspects_are_normalised_like_the_model_does(self) -> None:
        self.service.record_assessment(
            self.session_id, understanding_level="solid",
            known_aspects=["  极限   是趋近  ", "极限 是趋近", "ε-δ"],
        )

        self.assertEqual(self.state(self.a).known_aspects, ["极限 是趋近", "ε-δ"])

    def test_boundary_values_are_accepted(self) -> None:
        self.service.record_assessment(
            self.session_id, understanding_level="solid",
            known_aspects=["x" * MAX_ASPECT_LENGTH],
            weak_aspects=[f"w{i}" for i in range(MAX_ASPECTS)],
        )

        state = self.state(self.a)
        self.assertEqual(len(state.known_aspects), 1)
        self.assertEqual(len(state.weak_aspects), MAX_ASPECTS)

    def test_contradictory_lists_are_stored_as_submitted(self) -> None:
        """No business cross-checking here: the same aspect may be known *and* weak."""
        self.service.record_assessment(
            self.session_id, understanding_level="partial",
            known_aspects=["极限定义"], weak_aspects=["极限定义"],
        )

        state = self.state(self.a)
        self.assertEqual(state.known_aspects, ["极限定义"])
        self.assertEqual(state.weak_aspects, ["极限定义"])


class AssessmentTransactionTest(AssessmentTestCase):
    def snapshot(self):
        state = self.state(self.a)
        return (str(state.understanding_level), state.known_aspects, state.learn_count,
                state.last_learned_at, state.updated_at)

    def test_failure_after_the_state_write_rolls_it_back(self) -> None:
        self.learning.upsert_state(LearningState.create(
            memory_id=self.a.id, understanding_level="fuzzy", known_aspects=["旧的"]))
        before = self.snapshot()
        original = LearningUnitOfWork.update_state

        def write_then_fail(self, memory_id, **changes):
            original(self, memory_id, **changes)                 # the row IS written...
            raise RuntimeError("assessment persistence failed")   # ...then the unit fails

        with mock.patch.object(LearningUnitOfWork, "update_state", write_then_fail):
            with self.assertRaises(RuntimeError):
                self.service.record_assessment(self.session_id, understanding_level="solid",
                                               known_aspects=["新的"])

        self.assertEqual(self.snapshot(), before)                # nothing left in the database

    def test_failure_before_the_write_rolls_back_a_created_state(self) -> None:
        with self.database.transaction() as conn:
            conn.execute("DELETE FROM learning_states WHERE memory_id = ?", (self.a.id,))
        self.assertIsNone(self.state(self.a))

        with mock.patch.object(LearningUnitOfWork, "update_state",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.service.record_assessment(self.session_id, understanding_level="solid")

        self.assertIsNone(self.state(self.a))                    # the created row was rolled back
        self.assertEqual(self.learning.count_states(), 2)

    def test_a_successful_assessment_commits(self) -> None:
        self.service.record_assessment(self.session_id, understanding_level="solid",
                                       weak_aspects=["ε-δ"])

        state = self.state(self.a)
        self.assertEqual(str(state.understanding_level), "solid")
        self.assertEqual(state.weak_aspects, ["ε-δ"])

    def test_a_rejected_assessment_writes_nothing(self) -> None:
        before = self.snapshot()

        with self.assertRaises(ValidationError):
            self.service.record_assessment(self.session_id, understanding_level="mastered")

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.learning.count_states(), 3)


class CompatibilityWithOneOhTest(AssessmentTestCase):
    def test_1_0_tables_index_and_retrieval_are_untouched(self) -> None:
        counts_before = self.repo.counts()
        memory_before = self.repo.get_memory(self.a.id).as_dict()
        source_before = self.repo.get_source(self.source.id).as_dict()
        search_before = [(hit.memory.id, hit.score) for hit in MemoryRetriever(self.repo).search("极限").hits]

        self.service.record_assessment(self.session_id, understanding_level="solid",
                                       known_aspects=["极限是趋近"])

        self.assertEqual(self.repo.counts(), counts_before)
        self.assertEqual(self.repo.get_memory(self.a.id).as_dict(), memory_before)
        self.assertEqual(self.repo.get_source(self.source.id).as_dict(), source_before)
        self.assertEqual(
            [(hit.memory.id, hit.score) for hit in MemoryRetriever(self.repo).search("极限").hits],
            search_before,
        )
        self.assertEqual(self.repo.index_row_count("word"), 3)
        self.assertEqual(self.repo.index_row_count("trigram"), 3)

    def test_memory_lifecycle_and_links_are_untouched(self) -> None:
        self.service.record_assessment(self.session_id, understanding_level="solid")

        self.assertEqual(self.repo.require_memory(self.a.id).status, MemoryStatus.ACTIVE)
        self.assertEqual(self.repo.link_count(), 3)

    def test_no_new_tables(self) -> None:
        with self.database.connection() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual({name for name in tables if name.startswith("learning")},
                         {"learning_states", "learning_sessions"})
        for legacy in ("memories", "sources", "memory_sources", "schema_migrations"):
            self.assertIn(legacy, tables)

    def test_assessment_never_creates_memories_or_sources(self) -> None:
        counts_before = self.repo.counts()
        self.service.record_assessment(self.session_id, understanding_level="solid")

        self.assertEqual(self.repo.counts(), counts_before)
        self.assertEqual(len(self.repo.list_memories(status=None)), 3)
        self.assertEqual(len(self.repo.list_sources()), 1)


class AssessmentBoundaryContractTest(unittest.TestCase):
    def test_record_assessment_is_part_of_the_public_api(self) -> None:
        self.assertIn("record_assessment", PUBLIC_API)
        self.assertTrue(callable(LearningService.record_assessment))
        self.assertEqual(
            PUBLIC_API,
            ("start_session", "record_learning", "record_assessment", "get_context",
             "get_learning_overview", "get_active_session", "finish_session",
             "abandon_session"),
        )

    def test_record_assessment_body_never_touches_event_or_progress_fields(self) -> None:
        """AST guard: the assessment code may only write the judgement fields."""
        source = pathlib.Path("personal_memory/learning.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "record_assessment")
        body = list(function.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]                                       # drop the docstring
        code = "\n".join(ast.unparse(node) for node in body)

        for forbidden in ("learn_count", "last_learned_at", "plan_cursor", "current_stage",
                          "update_session", "finish_session", "abandon_session", "status"):
            self.assertNotIn(forbidden, code, f"record_assessment must not touch {forbidden}")
        self.assertIn("understanding_level", code)
        self.assertIn("update_state", code)

    def test_shared_session_guard_is_used_by_both_operations(self) -> None:
        source = pathlib.Path("personal_memory/learning.py").read_text(encoding="utf-8")
        # one definition, called by exactly the two write operations
        self.assertEqual(source.count("def _resolve_current_memory("), 1)
        self.assertEqual(source.count("self._resolve_current_memory("), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
