"""Phase 2B-2: ``LearningService.record_learning`` -- the explicit learning event.

What is locked here:

* one call == one reported learning event (``learn_count += 1``, ``last_learned_at``)
* the session advances exactly one plan item, without skipping or double counting
* the plan's last item leaves the session ``active`` in the Phase 2A representation
  (``plan_cursor == len(plan)``, ``current_memory_id = NULL``, ``current_stage = 'done'``)
* understanding (level / aspects / misconceptions) is never touched here
* a failure rolls back **both** the state and the session (verified in the database)
* the frozen 1.0 tables, index and retrieval results are untouched
"""

from __future__ import annotations

import pathlib
import unittest
from unittest import mock

from personal_memory import (
    LearningRepository,
    LearningService,
    LearningState,
    MemoryStatus,
    TeachingStage,
    UnderstandingLevel,
)
from personal_memory.errors import ConflictError, NotFoundError, ValidationError
from personal_memory.learning import PUBLIC_API
from personal_memory.learning_store import LearningUnitOfWork
from personal_memory.models import utcnow_iso
from personal_memory.retrieval import MemoryRetriever

from .helpers import RepositoryTestCase


class LearningEventTestCase(RepositoryTestCase):
    """One Source with three linked Memories (A, B, C) and a started session."""

    prefix = "pms-learnevent-"

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
        self.context = self.service.start_session(source_id=self.source.id)
        self.session_id = self.context.session.id

    # -- helpers ----------------------------------------------------------
    def session(self):
        return self.learning.get_session(self.session_id)

    def counts(self):
        return [self.learning.get_state(memory.id).learn_count for memory in (self.a, self.b, self.c)]

    def levels(self):
        return [str(self.learning.get_state(memory.id).understanding_level)
                for memory in (self.a, self.b, self.c)]


class NormalFlowTest(LearningEventTestCase):
    def test_one_event_counts_once_and_timestamps_the_memory(self) -> None:
        before = utcnow_iso()
        context = self.service.record_learning(self.session_id)
        after = utcnow_iso()

        state = self.learning.get_state(self.a.id)
        self.assertEqual(state.learn_count, 1)
        self.assertIsNotNone(state.last_learned_at)
        self.assertTrue(before <= state.last_learned_at <= after, state.last_learned_at)
        self.assertEqual(context.session.id, self.session_id)

    def test_the_returned_context_reflects_the_event(self) -> None:
        context = self.service.record_learning(self.session_id)

        self.assertEqual(context.session.plan_cursor, 1)
        self.assertEqual(context.session.current_memory_id, self.b.id)
        self.assertEqual(context.state_for(self.a.id).learn_count, 1)
        self.assertEqual(context.state_for(self.b.id).learn_count, 0)
        self.assertEqual(context.created_state_ids, ())       # states were initialised by start

    def test_the_session_advances_exactly_one_item(self) -> None:
        session = self.session()
        self.assertEqual((session.plan_cursor, session.current_memory_id), (0, self.a.id))

        self.service.record_learning(self.session_id)

        session = self.session()
        self.assertEqual(session.plan_cursor, 1)
        self.assertEqual(session.current_memory_id, self.b.id)
        self.assertEqual(str(session.current_stage), "explain")   # mid-plan stage is left alone
        self.assertEqual(str(session.status), "active")
        self.assertEqual(session.exchange, {})                    # no content is invented here

    def test_starting_alone_still_does_not_count_an_event(self) -> None:
        self.assertEqual(self.counts(), [0, 0, 0])
        self.assertEqual([self.learning.get_state(m.id).last_learned_at for m in (self.a, self.b, self.c)],
                         [None, None, None])

    def test_explicit_memory_id_must_name_the_current_memory(self) -> None:
        self.service.record_learning(self.session_id, memory_id=self.a.id)

        self.assertEqual(self.counts(), [1, 0, 0])
        self.assertEqual(self.session().current_memory_id, self.b.id)

    def test_learning_state_row_is_not_duplicated_by_recording(self) -> None:
        self.service.record_learning(self.session_id)
        self.service.record_learning(self.session_id)

        self.assertEqual(self.learning.count_states(), 3)


class MultiMemoryProgressTest(LearningEventTestCase):
    def test_a_b_c_are_walked_in_order(self) -> None:
        expected = [(1, self.b.id), (2, self.c.id), (3, None)]
        for cursor, current in expected:
            self.service.record_learning(self.session_id)
            session = self.session()
            self.assertEqual(session.plan_cursor, cursor)
            self.assertEqual(session.current_memory_id, current)

        self.assertEqual(self.counts(), [1, 1, 1])

    def test_progress_does_not_skip_or_double_count(self) -> None:
        self.service.record_learning(self.session_id)
        self.assertEqual(self.counts(), [1, 0, 0])
        self.service.record_learning(self.session_id)
        self.assertEqual(self.counts(), [1, 1, 0])
        self.service.record_learning(self.session_id)
        self.assertEqual(self.counts(), [1, 1, 1])
        self.assertEqual(self.session().plan_cursor, 3)

    def test_each_memory_gets_its_own_timestamp(self) -> None:
        stamps = []
        for _ in range(3):
            self.service.record_learning(self.session_id)
        for memory in (self.a, self.b, self.c):
            stamps.append(self.learning.get_state(memory.id).last_learned_at)
        self.assertEqual(len(set(stamps)), 3)
        self.assertEqual(stamps, sorted(stamps))              # monotonic, never re-used

    def test_a_two_memory_plan(self) -> None:
        self.service.finish_session(self.session_id)
        session = self.service.start_session(source_id=self.source.id,
                                             memory_ids=[self.b.id, self.c.id]).session

        self.service.record_learning(session.id)
        reloaded = self.learning.get_session(session.id)
        self.assertEqual((reloaded.plan_cursor, reloaded.current_memory_id), (1, self.c.id))
        self.service.record_learning(session.id)
        reloaded = self.learning.get_session(session.id)
        self.assertEqual((reloaded.plan_cursor, reloaded.current_memory_id), (2, None))

    def test_a_single_memory_plan_is_finished_by_one_event(self) -> None:
        self.service.finish_session(self.session_id)
        session = self.service.start_session(source_id=self.source.id, memory_ids=[self.c.id]).session

        self.service.record_learning(session.id)

        reloaded = self.learning.get_session(session.id)
        self.assertEqual(reloaded.plan_cursor, 1)
        self.assertIsNone(reloaded.current_memory_id)
        self.assertEqual(str(reloaded.current_stage), "done")
        self.assertEqual(self.learning.get_state(self.c.id).learn_count, 1)


class PlanFinishedTest(LearningEventTestCase):
    def exhaust(self) -> None:
        for _ in range(3):
            self.service.record_learning(self.session_id)

    def test_finishing_the_plan_uses_only_existing_representations(self) -> None:
        self.exhaust()

        session = self.session()
        self.assertEqual(session.plan_cursor, len(session.plan))
        self.assertIsNone(session.current_memory_id)
        self.assertEqual(str(session.current_stage), "done")
        self.assertEqual(str(session.status), "active")       # finishing the plan is not closing it

    def test_recording_again_after_the_plan_is_an_explicit_error(self) -> None:
        self.exhaust()

        with self.assertRaises(ConflictError) as ctx:
            self.service.record_learning(self.session_id)
        self.assertIn("finished its plan", str(ctx.exception))
        self.assertEqual(self.counts(), [1, 1, 1])            # nothing changed

    def test_the_session_is_not_auto_closed_and_can_still_be_finished(self) -> None:
        self.exhaust()

        self.assertIsNotNone(self.service.get_active_session(self.source.id))
        closed = self.service.finish_session(self.session_id)
        self.assertEqual(str(closed.session.status), "completed")
        self.assertIsNone(self.service.get_active_session(self.source.id))

    def test_context_after_exhaustion_is_still_readable(self) -> None:
        self.exhaust()

        context = self.service.get_context(self.session_id)
        self.assertEqual(context.memory_ids, (self.a.id, self.b.id, self.c.id))
        self.assertEqual([state.learn_count for state in context.states], [1, 1, 1])
        self.assertEqual(context.session.plan_cursor, 3)

    def test_get_active_session_still_reports_the_running_session(self) -> None:
        self.exhaust()
        self.assertEqual(self.service.get_active_session(self.source.id).id, self.session_id)


class UnderstandingUntouchedTest(LearningEventTestCase):
    def seed_understanding(self) -> None:
        self.learning.upsert_state(LearningState.create(
            memory_id=self.a.id, understanding_level=UnderstandingLevel.SOLID,
            known_aspects=["极限是趋近"], weak_aspects=["ε-δ定义"],
            misconceptions=["极限等于函数值"], learn_count=5,
            last_learned_at="2026-10-06T08:00:00.000Z",
        ))

    def test_level_and_aspects_are_never_touched_by_an_event(self) -> None:
        self.seed_understanding()
        before = self.learning.get_state(self.a.id)

        self.service.record_learning(self.session_id)

        after = self.learning.get_state(self.a.id)
        self.assertEqual(str(after.understanding_level), "solid")
        self.assertEqual(after.known_aspects, before.known_aspects)
        self.assertEqual(after.weak_aspects, before.weak_aspects)
        self.assertEqual(after.misconceptions, before.misconceptions)
        self.assertEqual(str(after.understanding_level), str(before.understanding_level))
        # only the event facts moved
        self.assertEqual(after.learn_count, before.learn_count + 1)
        self.assertGreater(after.last_learned_at, before.last_learned_at)

    def test_other_memories_keep_their_understanding(self) -> None:
        self.learning.upsert_state(LearningState.create(
            memory_id=self.b.id, understanding_level=UnderstandingLevel.FUZZY, learn_count=2))

        self.service.record_learning(self.session_id)          # records A

        untouched = self.learning.get_state(self.b.id)
        self.assertEqual(str(untouched.understanding_level), "fuzzy")
        self.assertEqual(untouched.learn_count, 2)
        self.assertIsNone(untouched.last_learned_at)

    def test_default_level_stays_unknown_after_an_event(self) -> None:
        self.service.record_learning(self.session_id)

        self.assertEqual(str(self.learning.get_state(self.a.id).understanding_level), "unknown")
        self.assertEqual(self.levels(), ["unknown", "unknown", "unknown"])


class StateInitialisationOnEventTest(LearningEventTestCase):
    def test_a_missing_state_is_created_and_counted_once(self) -> None:
        # simulate a Memory that lost its state (e.g. state deleted between start and the event)
        with self.database.transaction() as conn:
            conn.execute("DELETE FROM learning_states WHERE memory_id = ?", (self.a.id,))
        self.assertIsNone(self.learning.get_state(self.a.id))

        context = self.service.record_learning(self.session_id)

        state = self.learning.get_state(self.a.id)
        self.assertEqual(state.learn_count, 1)
        self.assertIsNotNone(state.last_learned_at)
        self.assertEqual(str(state.understanding_level), "unknown")
        self.assertEqual(context.state_for(self.a.id).learn_count, 1)

    def test_created_state_keeps_the_model_defaults(self) -> None:
        with self.database.transaction() as conn:
            conn.execute("DELETE FROM learning_states WHERE memory_id = ?", (self.b.id,))
        self.service.record_learning(self.session_id)          # A
        self.service.record_learning(self.session_id)          # B (state recreated here)

        state = self.learning.get_state(self.b.id)
        self.assertEqual(state.learn_count, 1)
        self.assertEqual(state.known_aspects, [])
        self.assertEqual(state.weak_aspects, [])
        self.assertEqual(state.misconceptions, [])
        self.assertEqual(str(state.understanding_level), "unknown")

    def test_state_is_not_created_for_memories_outside_the_plan(self) -> None:
        memory = self.make_memory(title="不属于本次计划的记忆")
        self.repo.link(memory.id, self.source.id)

        self.service.record_learning(self.session_id)

        self.assertIsNone(self.learning.get_state(memory.id))


class ErrorSemanticsTest(LearningEventTestCase):
    def test_unknown_session(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.service.record_learning("lrn_missing")
        self.assertEqual(ctx.exception.entity, "learning session")

    def test_completed_session(self) -> None:
        self.service.finish_session(self.session_id)

        with self.assertRaises(ConflictError) as ctx:
            self.service.record_learning(self.session_id)
        self.assertIn("only an active session", str(ctx.exception))
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_abandoned_session(self) -> None:
        self.service.abandon_session(self.session_id)

        with self.assertRaises(ConflictError):
            self.service.record_learning(self.session_id)
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_session_without_a_current_memory(self) -> None:
        self.learning.update_session(self.session_id, current_memory_id=None)

        with self.assertRaises(ConflictError) as ctx:
            self.service.record_learning(self.session_id)
        self.assertIn("no current Memory", str(ctx.exception))
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_unknown_memory_id(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.service.record_learning(self.session_id, memory_id="mem_missing")
        self.assertEqual(ctx.exception.entity, "memory")
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_memory_id_that_is_not_the_current_one(self) -> None:
        with self.assertRaises(ConflictError) as ctx:
            self.service.record_learning(self.session_id, memory_id=self.b.id)
        self.assertIn("not the Memory this session is on", str(ctx.exception))
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_memory_id_must_be_a_non_empty_string(self) -> None:
        for bad in (42, "", "   "):
            with self.assertRaises(ValidationError) as ctx:
                self.service.record_learning(self.session_id, memory_id=bad)
            self.assertEqual(ctx.exception.fields, ("memory_id",))
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_current_memory_diverging_from_the_plan(self) -> None:
        # external tampering: current points at B while the cursor still says A
        self.learning.update_session(self.session_id, current_memory_id=self.b.id)

        with self.assertRaises(ConflictError) as ctx:
            self.service.record_learning(self.session_id)
        self.assertIn("inconsistent", str(ctx.exception))
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_cursor_beyond_the_plan_is_reported_as_finished(self) -> None:
        self.learning.update_session(self.session_id, plan_cursor=99)

        with self.assertRaises(ConflictError) as ctx:
            self.service.record_learning(self.session_id)
        self.assertIn("finished its plan", str(ctx.exception))

    def test_a_deleted_current_memory_is_reported_clearly(self) -> None:
        self.repo.delete_memory(self.a.id)                     # current_memory_id becomes NULL

        with self.assertRaises(ConflictError) as ctx:
            self.service.record_learning(self.session_id)
        self.assertIn("no current Memory", str(ctx.exception))


class NoDedupeAcrossSessionsTest(LearningEventTestCase):
    def test_the_same_memory_learned_twice_accumulates_two_events(self) -> None:
        before = utcnow_iso()
        self.service.record_learning(self.session_id)          # A: 1st event
        first_stamp = self.learning.get_state(self.a.id).last_learned_at
        self.service.finish_session(self.session_id)

        second = self.service.start_session(source_id=self.source.id, memory_ids=[self.a.id])
        self.service.record_learning(second.session.id)        # A: 2nd event, a NEW session
        after = utcnow_iso()

        state = self.learning.get_state(self.a.id)
        self.assertEqual(state.learn_count, 2)                 # not de-duplicated
        self.assertGreaterEqual(state.last_learned_at, first_stamp)
        self.assertTrue(before <= state.last_learned_at <= after)
        self.assertEqual(self.learning.counts()["learning_sessions"], 2)
        self.assertEqual(self.learning.counts()["learning_states"], 3)

    def test_two_events_inside_one_session_are_two_rows_of_history(self) -> None:
        self.service.record_learning(self.session_id)          # A
        self.service.record_learning(self.session_id)          # B

        reloaded = self.session()
        self.assertEqual(reloaded.plan_cursor, 2)
        self.assertEqual([self.learning.get_state(m.id).learn_count for m in (self.a, self.b)],
                         [1, 1])

    def test_repeating_the_same_memory_inside_one_session_is_refused(self) -> None:
        """One event per current Memory: once the session moves on, A is no longer recordable."""
        self.service.record_learning(self.session_id)          # A, session now on B

        with self.assertRaises(ConflictError):
            self.service.record_learning(self.session_id, memory_id=self.a.id)
        self.assertEqual(self.learning.get_state(self.a.id).learn_count, 1)


class TransactionRollbackTest(LearningEventTestCase):
    def state_snapshot(self):
        state = self.learning.get_state(self.a.id)
        return (state.learn_count, state.last_learned_at, state.updated_at)

    def session_snapshot(self):
        session = self.session()
        return (session.plan_cursor, session.current_memory_id, str(session.current_stage),
                session.updated_at)

    def test_failure_before_the_session_write_rolls_the_state_back(self) -> None:
        state_before, session_before = self.state_snapshot(), self.session_snapshot()

        with mock.patch.object(LearningUnitOfWork, "update_session",
                               side_effect=RuntimeError("session write failed")):
            with self.assertRaises(RuntimeError):
                self.service.record_learning(self.session_id)

        # read again through fresh connections: the database really has no leftovers
        self.assertEqual(self.state_snapshot(), state_before)
        self.assertEqual(self.session_snapshot(), session_before)
        self.assertIsNone(self.learning.get_state(self.a.id).last_learned_at)

    def test_failure_after_both_writes_rolls_everything_back(self) -> None:
        state_before, session_before = self.state_snapshot(), self.session_snapshot()
        original = LearningUnitOfWork.update_session

        def write_then_fail(self, session_id, **changes):
            original(self, session_id, **changes)              # the session row IS written...
            raise RuntimeError("failure after both writes")     # ...then the unit fails

        with mock.patch.object(LearningUnitOfWork, "update_session", write_then_fail):
            with self.assertRaises(RuntimeError):
                self.service.record_learning(self.session_id)

        self.assertEqual(self.state_snapshot(), state_before)   # state write rolled back too
        self.assertEqual(self.session_snapshot(), session_before)
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_a_successful_event_commits_state_and_session_together(self) -> None:
        state_before, session_before = self.state_snapshot(), self.session_snapshot()

        self.service.record_learning(self.session_id)

        state_after, session_after = self.state_snapshot(), self.session_snapshot()
        self.assertEqual(state_after[0], 1)                        # learn_count committed
        self.assertNotEqual(state_after[1], state_before[1])       # last_learned_at changed
        self.assertNotEqual(state_after[2], state_before[2])       # state updated_at changed
        self.assertEqual((session_after[0], session_after[1]), (1, self.b.id))
        self.assertNotEqual(session_after[3], session_before[3])   # session updated_at changed

    def test_a_failed_event_leaves_the_source_usable(self) -> None:
        with mock.patch.object(LearningUnitOfWork, "update_session",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.service.record_learning(self.session_id)

        context = self.service.record_learning(self.session_id)   # works afterwards
        self.assertEqual(context.session.plan_cursor, 1)
        self.assertEqual(self.learning.get_state(self.a.id).learn_count, 1)

    def test_a_refused_event_writes_nothing(self) -> None:
        with self.assertRaises(ConflictError):
            self.service.record_learning(self.session_id, memory_id=self.c.id)

        self.assertEqual(self.counts(), [0, 0, 0])
        self.assertEqual(self.session_snapshot()[:2], (0, self.a.id))


class CompatibilityWithOneOhTest(LearningEventTestCase):
    def test_1_0_tables_index_and_retrieval_are_untouched(self) -> None:
        counts_before = self.repo.counts()
        memory_before = self.repo.get_memory(self.a.id).as_dict()
        source_before = self.repo.get_source(self.source.id).as_dict()
        search_before = [(hit.memory.id, hit.score) for hit in MemoryRetriever(self.repo).search("极限").hits]

        for _ in range(3):
            self.service.record_learning(self.session_id)

        self.assertEqual(self.repo.counts(), counts_before)
        self.assertEqual(self.repo.get_memory(self.a.id).as_dict(), memory_before)
        self.assertEqual(self.repo.get_source(self.source.id).as_dict(), source_before)
        self.assertEqual(
            [(hit.memory.id, hit.score) for hit in MemoryRetriever(self.repo).search("极限").hits],
            search_before,
        )
        self.assertEqual(self.repo.index_row_count("word"), 3)
        self.assertEqual(self.repo.index_row_count("trigram"), 3)

    def test_memory_status_and_lifecycle_are_untouched(self) -> None:
        self.service.record_learning(self.session_id)

        self.assertEqual(self.repo.require_memory(self.a.id).status, MemoryStatus.ACTIVE)
        self.assertEqual(self.repo.link_count(), 3)

    def test_no_new_tables_appear(self) -> None:
        with self.database.connection() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual({name for name in tables if name.startswith("learning")},
                         {"learning_states", "learning_sessions"})

    def test_learning_never_creates_memories_or_sources(self) -> None:
        counts_before = self.repo.counts()
        for _ in range(3):
            self.service.record_learning(self.session_id)

        self.assertEqual(self.repo.counts(), counts_before)
        self.assertEqual(len(self.repo.list_memories(status=None)), 3)
        self.assertEqual(len(self.repo.list_sources()), 1)


class EventBoundaryContractTest(unittest.TestCase):
    def test_record_learning_is_part_of_the_public_api(self) -> None:
        self.assertIn("record_learning", PUBLIC_API)
        self.assertTrue(callable(LearningService.record_learning))
        self.assertEqual(
            PUBLIC_API,
            ("start_session", "record_learning", "record_assessment", "get_context",
             "get_learning_overview", "get_active_session", "finish_session",
             "abandon_session"),
        )

    def test_record_learning_never_touches_understanding_levels(self) -> None:
        """Guard against a future edit that starts inferring 'understanding' in the code.

        Only the *code* is scanned: the docstring is allowed to explain that these
        fields are deliberately left alone.
        """
        import ast

        source = pathlib.Path("personal_memory/learning.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "record_learning"
        )
        body = list(function.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]                                    # drop the docstring
        code = "\n".join(ast.unparse(node) for node in body)
        for forbidden in ("understanding_level", "known_aspects", "weak_aspects", "misconceptions"):
            self.assertNotIn(forbidden, code, f"record_learning must not write {forbidden}")
        self.assertIn("learn_count", code)
        self.assertIn("last_learned_at", code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
