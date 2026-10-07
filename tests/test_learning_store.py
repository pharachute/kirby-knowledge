"""Phase 2A: LearningRepository behaviour (states, sessions, allowlists, transactions).

Everything here runs on a throwaway SQLite file (``RepositoryTestCase``); the
real 1.0 database is never touched.
"""

from __future__ import annotations

import unittest

from personal_memory import (
    Database,
    LearningRepository,
    LearningSession,
    LearningState,
    MemoryRepository,
    SessionStatus,
    TeachingStage,
    UnderstandingLevel,
)
from personal_memory.errors import ConflictError, NotFoundError, SchemaError, ValidationError
from personal_memory.learning_store import (
    LEARNING_SESSION_UPDATE_FIELDS,
    LEARNING_STATE_UPDATE_FIELDS,
    MAX_LIST_LIMIT,
)

from .helpers import RepositoryTestCase


class LearningStateRepositoryTest(RepositoryTestCase):
    prefix = "pms-learnstate-"

    def setUp(self) -> None:
        super().setUp()
        self.learning = LearningRepository(self.database)
        self.memory = self.make_memory(title="极限")
        self.other = self.make_memory(title="连续")

    def test_get_returns_none_when_the_user_never_studied_the_memory(self) -> None:
        self.assertIsNone(self.learning.get_state(self.memory.id))
        self.assertEqual(self.learning.count_states(), 0)

    def test_create_then_get_round_trips_every_field(self) -> None:
        state = self.learning.create_state(LearningState.create(
            memory_id=self.memory.id, understanding_level=UnderstandingLevel.FUZZY,
            known_aspects=["极限是趋近"], weak_aspects=["ε-δ"], misconceptions=["极限等于函数值"],
            learn_count=0, last_learned_at=None,
        ))
        loaded = self.learning.get_state(self.memory.id)

        self.assertEqual(loaded.as_dict(), state.as_dict())
        self.assertEqual(str(loaded.understanding_level), "fuzzy")
        self.assertEqual(loaded.known_aspects, ["极限是趋近"])
        self.assertEqual(loaded.misconceptions, ["极限等于函数值"])
        self.assertIsNone(loaded.last_learned_at)
        self.assertEqual(self.learning.count_states(), 1)

    def test_a_memory_can_never_have_two_states(self) -> None:
        self.learning.create_state(LearningState.create(memory_id=self.memory.id))

        with self.assertRaises(ConflictError) as ctx:
            self.learning.create_state(LearningState.create(memory_id=self.memory.id))
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual(self.learning.count_states(), 1)

    def test_update_patches_only_whitelisted_fields_and_refreshes_updated_at(self) -> None:
        original = self.learning.create_state(LearningState.create(memory_id=self.memory.id))
        updated = self.learning.update_state(
            self.memory.id, understanding_level="partial", known_aspects=["极限是趋近"],
            weak_aspects=[" ε-δ "], learn_count=1, last_learned_at="2026-10-07T08:00:00.000Z",
        )

        self.assertEqual(str(updated.understanding_level), "partial")
        self.assertEqual(updated.known_aspects, ["极限是趋近"])
        self.assertEqual(updated.weak_aspects, ["ε-δ"])
        self.assertEqual(updated.learn_count, 1)
        self.assertEqual(updated.last_learned_at, "2026-10-07T08:00:00.000Z")
        self.assertEqual(updated.created_at, original.created_at)          # immutable
        self.assertNotEqual(updated.updated_at, original.updated_at)        # refreshed
        self.assertEqual(self.learning.get_state(self.memory.id).as_dict(), updated.as_dict())

    def test_update_refuses_unknown_and_immutable_fields(self) -> None:
        self.learning.create_state(LearningState.create(memory_id=self.memory.id))
        for payload in (
            {"nope": 1},
            {"memory_id": self.other.id},
            {"created_at": "2026-01-01T00:00:00.000Z"},
            {"updated_at": "2026-01-01T00:00:00.000Z"},
            {"schema_version": 2},
        ):
            with self.assertRaises(ValidationError) as ctx:
                self.learning.update_state(self.memory.id, **payload)
            self.assertTrue(set(payload) <= set(ctx.exception.fields))
        # nothing was written by the refused calls
        self.assertEqual(str(self.learning.get_state(self.memory.id).understanding_level), "unknown")

    def test_update_on_a_missing_state_is_not_found(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.learning.update_state(self.other.id, understanding_level="solid")
        self.assertEqual(ctx.exception.entity, "learning state")

    def test_update_with_no_changes_is_a_no_op(self) -> None:
        original = self.learning.create_state(LearningState.create(memory_id=self.memory.id))
        self.assertEqual(self.learning.update_state(self.memory.id).as_dict(), original.as_dict())

    def test_upsert_inserts_then_replaces_but_keeps_created_at(self) -> None:
        first = self.learning.upsert_state(LearningState.create(memory_id=self.memory.id))
        self.assertEqual(self.learning.count_states(), 1)

        second = self.learning.upsert_state(LearningState.create(
            memory_id=self.memory.id, understanding_level=UnderstandingLevel.SOLID,
            known_aspects=["讲得清"], learn_count=2, last_learned_at="2026-10-07T09:00:00.000Z",
        ))
        loaded = self.learning.get_state(self.memory.id)

        self.assertEqual(self.learning.count_states(), 1)
        self.assertEqual(str(loaded.understanding_level), "solid")
        self.assertEqual(loaded.learn_count, 2)
        self.assertEqual(loaded.created_at, first.created_at)     # preserved by the upsert
        self.assertEqual(str(second.understanding_level), "solid")

    def test_upsert_refuses_a_non_state_argument(self) -> None:
        with self.assertRaises(ValidationError):
            self.learning.upsert_state({"memory_id": self.memory.id})

    def test_repository_never_bumps_learn_count_on_its_own(self) -> None:
        """§四: creating a state or starting/advancing a session is not a learning event."""
        self.learning.create_state(LearningState.create(memory_id=self.memory.id))
        source = self.make_source()
        self.repo.link(self.memory.id, source.id)
        session = self.learning.create_session(LearningSession.create(source_id=source.id))
        self.learning.update_session(session.id, current_stage="question", plan_cursor=1)

        self.assertEqual(self.learning.get_state(self.memory.id).learn_count, 0)
        self.assertIsNone(self.learning.get_state(self.memory.id).last_learned_at)

    def test_update_fields_allowlist_is_explicit(self) -> None:
        self.assertEqual(
            LEARNING_STATE_UPDATE_FIELDS,
            frozenset({"understanding_level", "known_aspects", "weak_aspects",
                       "misconceptions", "learn_count", "last_learned_at"}),
        )
        self.assertEqual(
            LEARNING_SESSION_UPDATE_FIELDS,
            frozenset({"current_memory_id", "current_stage", "plan", "plan_cursor", "exchange"}),
        )


class LearningSessionRepositoryTest(RepositoryTestCase):
    prefix = "pms-learnsess-"

    def setUp(self) -> None:
        super().setUp()
        self.learning = LearningRepository(self.database)
        self.source = self.make_source(title="高等数学第一章")
        self.other_source = self.make_source(title="另一份材料")
        self.first = self.make_memory(title="极限")
        self.second = self.make_memory(title="连续")
        self.repo.link(self.first.id, self.source.id)
        self.repo.link(self.second.id, self.source.id)

    def make_session(self, **overrides) -> LearningSession:
        kwargs = {"source_id": self.source.id, "plan": [self.first.id, self.second.id],
                  "current_memory_id": self.first.id}
        kwargs.update(overrides)
        return self.learning.create_session(LearningSession.create(**kwargs))

    def test_create_get_and_require(self) -> None:
        session = self.make_session()

        loaded = self.learning.get_session(session.id)
        self.assertEqual(loaded.as_dict(), session.as_dict())
        self.assertEqual(loaded.plan, [self.first.id, self.second.id])
        self.assertEqual(str(loaded.status), "active")
        self.assertEqual(str(loaded.current_stage), "explain")
        self.assertEqual(self.learning.get_session("lrn_missing"), None)
        with self.assertRaises(NotFoundError) as ctx:
            self.learning.require_session("lrn_missing")
        self.assertEqual(ctx.exception.entity, "learning session")

    def test_a_source_can_have_only_one_active_session(self) -> None:
        self.make_session()

        with self.assertRaises(ConflictError) as ctx:
            self.make_session()
        self.assertIn(self.source.id, str(ctx.exception))
        self.assertEqual(len(self.learning.list_active_sessions()), 1)
        # another source is unaffected
        self.learning.create_session(LearningSession.create(source_id=self.other_source.id))
        self.assertEqual(len(self.learning.list_active_sessions()), 2)
        self.assertEqual(len(self.learning.list_active_sessions(source_id=self.source.id)), 1)

    def test_finish_sets_completed_and_ended_at_and_frees_the_slot(self) -> None:
        session = self.make_session()

        finished = self.learning.finish_session(session.id)

        self.assertEqual(str(finished.status), "completed")
        self.assertIsNotNone(finished.ended_at)
        self.assertEqual(self.learning.get_active_session_for_source(self.source.id), None)
        self.assertEqual(self.learning.list_active_sessions(), [])
        restarted = self.make_session()
        self.assertEqual(str(restarted.status), "active")
        self.assertNotEqual(restarted.id, session.id)

    def test_abandon_sets_abandoned_and_frees_the_slot(self) -> None:
        session = self.make_session()

        abandoned = self.learning.abandon_session(session.id)

        self.assertEqual(str(abandoned.status), "abandoned")
        self.assertIsNotNone(abandoned.ended_at)
        self.assertEqual(str(self.make_session().status), "active")

    def test_a_closed_session_cannot_be_closed_again(self) -> None:
        finished = self.learning.finish_session(self.make_session().id)

        with self.assertRaises(ConflictError) as ctx:
            self.learning.finish_session(finished.id)
        self.assertIn("only an active session can be closed", str(ctx.exception))
        with self.assertRaises(ConflictError):
            self.learning.abandon_session(finished.id)

    def test_update_session_patches_progress_fields(self) -> None:
        session = self.make_session()
        updated = self.learning.update_session(
            session.id, current_stage="question", plan_cursor=1,
            exchange={"question": "什么是极限？", "answer": "趋近"},
        )

        self.assertEqual(str(updated.current_stage), "question")
        self.assertEqual(updated.plan_cursor, 1)
        self.assertEqual(updated.exchange, {"question": "什么是极限？", "answer": "趋近"})
        self.assertNotEqual(updated.updated_at, session.updated_at)
        self.assertEqual(updated.started_at, session.started_at)

    def test_update_session_refuses_identity_and_status_fields(self) -> None:
        session = self.make_session()
        for payload in (
            {"status": "completed"},
            {"id": "lrn_other"},
            {"source_id": self.other_source.id},
            {"started_at": "2026-01-01T00:00:00.000Z"},
            {"ended_at": "2026-01-01T00:00:00.000Z"},
            {"schema_version": 2},
            {"nope": 1},
        ):
            with self.assertRaises(ValidationError):
                self.learning.update_session(session.id, **payload)
        self.assertEqual(str(self.learning.get_session(session.id).status), "active")

    def test_update_session_on_a_missing_session_is_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.learning.update_session("lrn_missing", current_stage="question")

    def test_current_memory_can_be_cleared_and_point_at_another_memory(self) -> None:
        session = self.make_session()

        moved = self.learning.update_session(session.id, current_memory_id=self.second.id)
        self.assertEqual(moved.current_memory_id, self.second.id)
        cleared = self.learning.update_session(session.id, current_memory_id=None)
        self.assertIsNone(cleared.current_memory_id)

    def test_update_session_refuses_an_unknown_memory(self) -> None:
        session = self.make_session()
        with self.assertRaises(Exception) as ctx:      # FK violation -> typed integrity error
            self.learning.update_session(session.id, current_memory_id="mem_does_not_exist")
        self.assertIn(type(ctx.exception).__name__, {"ReferentialIntegrityError", "ValidationError"})

    def test_list_active_sessions_orders_by_recency_and_validates_limit(self) -> None:
        first = self.learning.create_session(LearningSession.create(source_id=self.source.id))
        second = self.learning.create_session(LearningSession.create(source_id=self.other_source.id))
        self.learning.update_session(first.id, current_stage="question")   # newer updated_at

        listed = self.learning.list_active_sessions()
        self.assertEqual([s.id for s in listed], [first.id, second.id])
        for bad in (0, -1, "10", 1.5, True, MAX_LIST_LIMIT + 1):
            with self.assertRaises(ValidationError) as ctx:
                self.learning.list_active_sessions(limit=bad)
            self.assertEqual(ctx.exception.fields, ("limit",))
        self.assertEqual(len(self.learning.list_active_sessions(limit=1)), 1)

    def test_get_active_session_for_source(self) -> None:
        self.assertIsNone(self.learning.get_active_session_for_source(self.source.id))
        session = self.make_session()
        self.assertEqual(self.learning.get_active_session_for_source(self.source.id).id, session.id)
        self.learning.finish_session(session.id)
        self.assertIsNone(self.learning.get_active_session_for_source(self.source.id))

    def test_counts(self) -> None:
        self.assertEqual(self.learning.counts(), {"learning_states": 0, "learning_sessions": 0})
        self.learning.create_state(LearningState.create(memory_id=self.first.id))
        self.make_session()
        self.assertEqual(self.learning.counts(), {"learning_states": 1, "learning_sessions": 1})

    def test_repository_without_initialize_requires_an_existing_schema(self) -> None:
        missing = Database(self.tmpdir / "missing.db")
        with self.assertRaises(SchemaError):
            LearningRepository(missing, initialize=False)


class LearningUnitOfWorkTest(RepositoryTestCase):
    prefix = "pms-learnuow-"

    def setUp(self) -> None:
        super().setUp()
        self.learning = LearningRepository(self.database)
        self.source = self.make_source()
        self.memory = self.make_memory()
        self.repo.link(self.memory.id, self.source.id)
        self.session = self.learning.create_session(
            LearningSession.create(source_id=self.source.id, current_memory_id=self.memory.id))
        self.learning.create_state(LearningState.create(memory_id=self.memory.id))

    def test_session_and_state_are_updated_in_one_transaction(self) -> None:
        with self.learning.transaction() as tx:
            tx.update_session(self.session.id, current_stage=TeachingStage.ANALYZE, plan_cursor=1)
            tx.update_state(self.memory.id, understanding_level="partial", learn_count=1,
                            last_learned_at="2026-10-07T08:00:00.000Z")

        session = self.learning.get_session(self.session.id)
        state = self.learning.get_state(self.memory.id)
        self.assertEqual(str(session.current_stage), "analyze")
        self.assertEqual(session.plan_cursor, 1)
        self.assertEqual(str(state.understanding_level), "partial")
        self.assertEqual(state.learn_count, 1)

    def test_an_exception_rolls_the_whole_unit_back(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.learning.transaction() as tx:
                tx.update_session(self.session.id, current_stage=TeachingStage.PRACTICE, plan_cursor=5)
                tx.update_state(self.memory.id, understanding_level="solid", learn_count=9)
                raise RuntimeError("model output was unusable")

        session = self.learning.get_session(self.session.id)
        state = self.learning.get_state(self.memory.id)
        self.assertEqual(str(session.current_stage), "explain")     # nothing was committed
        self.assertEqual(session.plan_cursor, 0)
        self.assertEqual(str(state.understanding_level), "unknown")
        self.assertEqual(state.learn_count, 0)

    def test_the_unit_of_work_uses_the_same_allowlists(self) -> None:
        with self.learning.transaction() as tx:
            with self.assertRaises(ValidationError):
                tx.update_state(self.memory.id, memory_id="mem_other")
            with self.assertRaises(ValidationError):
                tx.update_session(self.session.id, status="completed")

    def test_unit_of_work_can_create_and_read_inside_the_transaction(self) -> None:
        second = self.make_memory(title="另一条记忆")
        with self.learning.transaction() as tx:
            self.assertIsNone(tx.get_state(second.id))
            self.assertIsNone(tx.get_session("lrn_missing"))
            self.assertEqual(tx.get_active_session_for_source(self.source.id).id, self.session.id)
            created = tx.create_state(LearningState.create(memory_id=second.id))
            self.assertEqual(tx.get_state(second.id).as_dict(), created.as_dict())
        self.assertIsNotNone(self.learning.get_state(second.id))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
