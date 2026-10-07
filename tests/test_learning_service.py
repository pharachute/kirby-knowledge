"""Phase 2B-1: LearningService (business orchestration, no LLM).

The tests split into six groups:

* session creation (single/multi Memory, defaults, ordering, duplicates)
* validation and error boundaries
* the LearningState rules locked by Phase 2A (create-if-missing, reuse otherwise,
  never a ``learn_count`` / ``last_learned_at`` / cursor side effect)
* context assembly (including a Memory that disappeared mid-session)
* transaction atomicity (a failure leaves nothing behind)
* the boundary contract (no LLM / prompt / HTTP / UI anywhere in the module)
"""

from __future__ import annotations

import pathlib
import unittest
from unittest import mock

from personal_memory import (
    Database,
    LearningRepository,
    LearningService,
    LearningState,
    MemoryRepository,
    SessionStatus,
    TeachingStage,
    UnderstandingLevel,
)
from personal_memory.errors import ConflictError, NotFoundError, ValidationError
from personal_memory.learning import PUBLIC_API, LearningContext
from personal_memory.learning_store import LearningUnitOfWork

from .helpers import RepositoryTestCase


class LearningServiceTestCase(RepositoryTestCase):
    """Shared fixture: one Source with three linked Memories."""

    prefix = "pms-learnsvc-"

    def setUp(self) -> None:
        super().setUp()
        self.learning = LearningRepository(self.database)
        self.service = LearningService(self.learning, self.repo)
        self.source = self.make_source(title="高等数学第一章")
        self.first = self.make_memory(title="极限")
        self.second = self.make_memory(title="连续")
        self.third = self.make_memory(title="切线斜率")
        for memory in (self.first, self.second, self.third):
            self.repo.link(memory.id, self.source.id)

    def other_source_with(self, *memories) -> object:
        source = self.make_source(title="另一份材料")
        for memory in memories:
            self.repo.link(memory.id, source.id)
        return source


class StartSessionBasicsTest(LearningServiceTestCase):
    def test_start_with_explicit_memory_ids(self) -> None:
        context = self.service.start_session(
            source_id=self.source.id, memory_ids=[self.first.id, self.second.id]
        )

        session = context.session
        self.assertEqual(str(session.status), "active")
        self.assertEqual(str(session.current_stage), "explain")
        self.assertEqual(session.plan, [self.first.id, self.second.id])
        self.assertEqual(session.plan_cursor, 0)
        self.assertEqual(session.current_memory_id, self.first.id)
        self.assertIsNone(session.ended_at)
        self.assertEqual(context.source.id, self.source.id)
        self.assertEqual(context.memory_ids, (self.first.id, self.second.id))
        self.assertEqual([memory.title for memory in context.memories], ["极限", "连续"])

    def test_start_defaults_to_every_linked_memory(self) -> None:
        context = self.service.start_session(source_id=self.source.id)

        self.assertEqual(
            context.memory_ids, (self.first.id, self.second.id, self.third.id)
        )
        self.assertEqual(context.session.plan, list(context.memory_ids))
        self.assertEqual(self.learning.counts()["learning_sessions"], 1)

    def test_multi_memory_session_keeps_the_requested_order(self) -> None:
        context = self.service.start_session(
            source_id=self.source.id, memory_ids=[self.third.id, self.first.id]
        )

        self.assertEqual(context.memory_ids, (self.third.id, self.first.id))
        self.assertEqual(context.session.current_memory_id, self.third.id)
        self.assertEqual([state.memory_id for state in context.states], [self.third.id, self.first.id])

    def test_duplicate_memory_ids_are_collapsed_and_keep_the_first_position(self) -> None:
        context = self.service.start_session(
            source_id=self.source.id, memory_ids=[self.second.id, self.first.id, self.second.id]
        )

        self.assertEqual(context.session.plan, [self.second.id, self.first.id])
        self.assertEqual(self.learning.counts()["learning_states"], 2)

    def test_the_session_is_persisted_and_reloadable(self) -> None:
        context = self.service.start_session(source_id=self.source.id, memory_ids=[self.first.id])

        stored = self.learning.get_session(context.session.id)
        self.assertEqual(stored.as_dict(), context.session.as_dict())
        self.assertEqual(self.service.get_active_session(self.source.id).id, context.session.id)

    def test_start_creates_exactly_one_session_row(self) -> None:
        self.service.start_session(source_id=self.source.id)
        self.assertEqual(self.learning.counts()["learning_sessions"], 1)
        self.assertEqual(len(self.learning.list_active_sessions()), 1)


class StartSessionValidationTest(LearningServiceTestCase):
    def test_source_must_exist(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.service.start_session(source_id="src_missing")
        self.assertEqual(ctx.exception.entity, "source")
        self.assertEqual(self.learning.counts(), {"learning_states": 0, "learning_sessions": 0})

    def test_every_memory_must_exist(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.service.start_session(
                source_id=self.source.id, memory_ids=[self.first.id, "mem_missing"]
            )
        self.assertEqual(ctx.exception.entity, "memory")
        self.assertEqual(ctx.exception.entity_id, "mem_missing")
        self.assertEqual(self.learning.counts(), {"learning_states": 0, "learning_sessions": 0})

    def test_memory_must_belong_to_the_source(self) -> None:
        stranger = self.make_memory(title="不属于这份材料")
        other = self.other_source_with(stranger)

        with self.assertRaises(ValidationError) as ctx:
            self.service.start_session(source_id=self.source.id, memory_ids=[stranger.id])
        self.assertEqual(ctx.exception.fields, ("memory_ids",))
        # the same Memory is fine through its own Source
        context = self.service.start_session(source_id=other.id, memory_ids=[stranger.id])
        self.assertEqual(context.memory_ids, (stranger.id,))
        self.assertEqual(self.learning.counts()["learning_sessions"], 1)

    def test_empty_memory_ids_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.service.start_session(source_id=self.source.id, memory_ids=[])
        self.assertEqual(ctx.exception.fields, ("memory_ids",))
        self.assertEqual(self.learning.counts()["learning_sessions"], 0)

    def test_memory_ids_must_be_a_sequence_of_ids(self) -> None:
        for bad in ("mem_abc", 42, [42], [""], ["  "]):
            with self.assertRaises(ValidationError) as ctx:
                self.service.start_session(source_id=self.source.id, memory_ids=bad)
            self.assertEqual(ctx.exception.fields, ("memory_ids",), bad)
        self.assertEqual(self.learning.counts()["learning_sessions"], 0)

    def test_source_without_memories_cannot_start(self) -> None:
        empty = self.make_source(title="没有任何记忆的材料")

        with self.assertRaises(ValidationError) as ctx:
            self.service.start_session(source_id=empty.id)
        self.assertEqual(ctx.exception.fields, ("source_id",))
        self.assertEqual(self.learning.counts()["learning_sessions"], 0)

    def test_a_source_can_have_only_one_running_session(self) -> None:
        first = self.service.start_session(source_id=self.source.id)

        with self.assertRaises(ConflictError) as ctx:
            self.service.start_session(source_id=self.source.id)
        self.assertIn(first.session.id, str(ctx.exception))
        self.assertEqual(self.learning.counts()["learning_sessions"], 1)

    def test_finish_and_abandon_free_the_source_for_a_new_session(self) -> None:
        finished = self.service.finish_session(
            self.service.start_session(source_id=self.source.id).session.id
        )
        self.assertEqual(str(finished.session.status), "completed")
        self.assertIsNotNone(finished.session.ended_at)
        self.assertIsNone(self.service.get_active_session(self.source.id))

        second = self.service.start_session(source_id=self.source.id)
        self.assertEqual(str(second.session.status), "active")
        abandoned = self.service.abandon_session(second.session.id)
        self.assertEqual(str(abandoned.session.status), "abandoned")
        third = self.service.start_session(source_id=self.source.id)
        self.assertEqual(str(third.session.status), "active")
        self.assertEqual(self.learning.counts()["learning_sessions"], 3)

    def test_closing_an_already_closed_session_is_a_conflict(self) -> None:
        session = self.service.start_session(source_id=self.source.id).session
        self.service.finish_session(session.id)

        with self.assertRaises(ConflictError):
            self.service.finish_session(session.id)
        with self.assertRaises(ConflictError):
            self.service.abandon_session(session.id)

    def test_unknown_session_is_not_found(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.service.get_context("lrn_missing")
        self.assertEqual(ctx.exception.entity, "learning session")
        with self.assertRaises(NotFoundError):
            self.service.finish_session("lrn_missing")


class LearningStateRulesTest(LearningServiceTestCase):
    def test_start_initialises_a_default_state_for_every_memory(self) -> None:
        context = self.service.start_session(source_id=self.source.id)

        self.assertEqual(len(context.states), 3)
        self.assertEqual(context.created_state_ids, context.memory_ids)
        self.assertEqual(self.learning.counts()["learning_states"], 3)
        for state in context.states:
            self.assertEqual(str(state.understanding_level), "unknown")
            self.assertEqual(state.learn_count, 0)
            self.assertIsNone(state.last_learned_at)
            self.assertEqual(state.known_aspects, [])
            self.assertEqual(state.weak_aspects, [])
            self.assertEqual(state.misconceptions, [])

    def test_an_existing_state_is_reused_untouched(self) -> None:
        existing = self.learning.create_state(LearningState.create(
            memory_id=self.first.id, understanding_level=UnderstandingLevel.SOLID,
            known_aspects=["极限是趋近"], weak_aspects=["ε-δ"], misconceptions=["极限等于函数值"],
            learn_count=7, last_learned_at="2026-10-06T08:00:00.000Z",
        ))

        context = self.service.start_session(source_id=self.source.id)

        reloaded = self.learning.get_state(self.first.id)
        self.assertEqual(reloaded.as_dict(), existing.as_dict())      # nothing rewritten
        self.assertNotIn(self.first.id, context.created_state_ids)    # not re-created either
        self.assertEqual(set(context.created_state_ids), {self.second.id, self.third.id})
        self.assertEqual(context.state_for(self.first.id).learn_count, 7)

    def test_start_never_bumps_learn_count_or_last_learned_at(self) -> None:
        self.learning.create_state(LearningState.create(
            memory_id=self.first.id, learn_count=4, last_learned_at="2026-10-06T08:00:00.000Z"))

        self.service.start_session(source_id=self.source.id)
        with self.assertRaises(ConflictError):
            self.service.start_session(source_id=self.source.id)      # refused: a session is running

        state = self.learning.get_state(self.first.id)
        self.assertEqual(state.learn_count, 4)
        self.assertEqual(state.last_learned_at, "2026-10-06T08:00:00.000Z")
        self.assertEqual(self.learning.get_state(self.second.id).learn_count, 0)

    def test_start_does_not_advance_stage_or_cursor(self) -> None:
        context = self.service.start_session(source_id=self.source.id)

        session = self.learning.get_session(context.session.id)
        self.assertEqual(str(session.current_stage), "explain")
        self.assertEqual(session.plan_cursor, 0)
        self.assertEqual(session.exchange, {})
        self.assertIsNone(session.ended_at)

    def test_start_does_not_touch_memories_sources_or_links(self) -> None:
        before_counts = self.repo.counts()
        before_memory = self.repo.get_memory(self.first.id).as_dict()
        before_source = self.repo.get_source(self.source.id).as_dict()

        self.service.start_session(source_id=self.source.id)

        self.assertEqual(self.repo.counts(), before_counts)
        self.assertEqual(self.repo.get_memory(self.first.id).as_dict(), before_memory)
        self.assertEqual(self.repo.get_source(self.source.id).as_dict(), before_source)

    def test_finishing_a_session_does_not_touch_learning_state(self) -> None:
        context = self.service.start_session(source_id=self.source.id)
        before = self.learning.get_state(self.first.id).as_dict()

        self.service.finish_session(context.session.id)

        self.assertEqual(self.learning.get_state(self.first.id).as_dict(), before)


class LearningContextTest(LearningServiceTestCase):
    def test_get_context_returns_session_source_memories_and_states(self) -> None:
        started = self.service.start_session(
            source_id=self.source.id, memory_ids=[self.first.id, self.third.id])

        context = self.service.get_context(started.session.id)

        self.assertIsInstance(context, LearningContext)
        self.assertEqual(context.session.id, started.session.id)
        self.assertEqual(context.source.id, self.source.id)
        self.assertEqual(context.memory_ids, (self.first.id, self.third.id))
        self.assertEqual([state.memory_id for state in context.states], [self.first.id, self.third.id])
        self.assertEqual(context.created_state_ids, ())          # nothing new this time
        self.assertEqual(context.memory_for(self.first.id).title, "极限")
        self.assertIsNone(context.memory_for("mem_missing"))
        self.assertIsNone(context.state_for("mem_missing"))

    def test_context_survives_a_memory_deleted_mid_session(self) -> None:
        started = self.service.start_session(
            source_id=self.source.id, memory_ids=[self.first.id, self.second.id])

        self.repo.delete_memory(self.second.id)                  # state cascades, plan keeps the id

        context = self.service.get_context(started.session.id)
        self.assertEqual(context.session.plan, [self.first.id, self.second.id])
        self.assertEqual(context.memory_ids, (self.first.id,))   # gone from the assembled context
        self.assertEqual([state.memory_id for state in context.states], [self.first.id])
        self.assertIsNone(context.memory_for(self.second.id))

    def test_context_as_dict_is_json_friendly(self) -> None:
        started = self.service.start_session(source_id=self.source.id, memory_ids=[self.first.id])

        payload = self.service.get_context(started.session.id).as_dict()

        self.assertEqual(sorted(payload), ["created_state_ids", "memories", "session", "source", "states"])
        self.assertEqual(payload["session"]["id"], started.session.id)
        self.assertEqual(payload["source"]["id"], self.source.id)
        self.assertEqual([memory["id"] for memory in payload["memories"]], [self.first.id])
        self.assertEqual([state["memory_id"] for state in payload["states"]], [self.first.id])
        self.assertEqual(payload["states"][0]["understanding_level"], "unknown")
        self.assertNotIn("known_aspects_json", payload["states"][0])
        import json
        json.dumps(payload)                                       # must be serialisable

    def test_get_active_session_is_none_before_the_first_start(self) -> None:
        self.assertIsNone(self.service.get_active_session(self.source.id))


class SessionTransactionTest(LearningServiceTestCase):
    def test_a_failure_while_initialising_states_leaves_nothing_behind(self) -> None:
        original = LearningUnitOfWork.create_state
        calls = {"count": 0}

        def flaky(self, state):
            calls["count"] += 1
            if calls["count"] == 2:                              # blow up on the second Memory
                raise RuntimeError("simulated failure while initialising learning state")
            return original(self, state)

        with mock.patch.object(LearningUnitOfWork, "create_state", flaky):
            with self.assertRaises(RuntimeError):
                self.service.start_session(source_id=self.source.id)

        self.assertEqual(self.learning.counts(), {"learning_states": 0, "learning_sessions": 0})
        self.assertIsNone(self.service.get_active_session(self.source.id))
        self.assertEqual(self.learning.list_active_sessions(), [])
        # and the Source is still startable afterwards
        context = self.service.start_session(source_id=self.source.id)
        self.assertEqual(len(context.states), 3)

    def test_a_failure_while_creating_the_session_leaves_nothing_behind(self) -> None:
        original = LearningUnitOfWork.create_session

        def flaky(self, session):
            original(self, session)                              # row written inside the transaction
            raise RuntimeError("simulated failure after the session insert")

        with mock.patch.object(LearningUnitOfWork, "create_session", flaky):
            with self.assertRaises(RuntimeError):
                self.service.start_session(source_id=self.source.id)

        self.assertEqual(self.learning.counts(), {"learning_states": 0, "learning_sessions": 0})

    def test_a_successful_start_commits_session_and_states_together(self) -> None:
        context = self.service.start_session(source_id=self.source.id)

        self.assertEqual(self.learning.counts(), {"learning_states": 3, "learning_sessions": 1})
        self.assertEqual(self.learning.get_session(context.session.id).plan, list(context.memory_ids))

    def test_a_rejected_start_writes_nothing(self) -> None:
        stranger = self.make_memory(title="别人家的记忆")
        with self.assertRaises(ValidationError):
            self.service.start_session(source_id=self.source.id, memory_ids=[stranger.id])

        self.assertEqual(self.learning.counts(), {"learning_states": 0, "learning_sessions": 0})
        self.assertIsNone(self.learning.get_state(stranger.id))


class ServiceBoundaryContractTest(LearningServiceTestCase):
    """The module must stay free of LLM / prompt / HTTP / UI concerns."""

    def test_module_does_not_import_llm_prompt_network_or_ui(self) -> None:
        """AST guard: docstrings may *say* "no LLM", the code must not import one."""
        import ast

        source = pathlib.Path("personal_memory/learning.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden_modules = {"urllib", "http", "socket", "requests", "webbrowser",
                             "subprocess", "ssl", "asyncio", "openai"}
        self.assertEqual(imported & forbidden_modules, set())
        # this layer may only build on the learning + frozen 1.0 store layers
        self.assertEqual(
            imported,
            {"__future__", "dataclasses", "typing", "errors", "learning_models",
             "learning_store", "models", "store"},
        )
        identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for token in ("llm", "prompt", "openai", "deepseek", "embedding", "http"):
            self.assertFalse(
                [name for name in identifiers if token in name.lower()],
                f"learning.py references {token!r} in code",
            )

    def test_public_api_is_the_documented_set(self) -> None:
        exported = {
            name for name in dir(LearningService)
            if not name.startswith("_") and callable(getattr(LearningService, name))
        }
        self.assertEqual(exported, set(PUBLIC_API))
        self.assertEqual(
            PUBLIC_API,
            ("start_session", "record_learning", "record_assessment", "get_context",
             "get_learning_overview", "get_active_session", "finish_session",
             "abandon_session"),
        )

    def test_service_uses_the_repositories_it_was_given(self) -> None:
        service = LearningService(self.learning, self.repo)
        self.assertIs(service.learning, self.learning)
        self.assertIs(service.memory, self.repo)

    def test_service_shares_one_database_with_both_repositories(self) -> None:
        self.assertIs(self.learning.database, self.repo.database)
        self.assertIsInstance(LearningService(self.learning, self.repo).learning, LearningRepository)
        self.assertIsInstance(self.service.memory, MemoryRepository)
        self.assertIsInstance(self.learning.database, Database)

    def test_the_service_adds_no_new_tables(self) -> None:
        with self.database.connection() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(
            {name for name in tables if name.startswith("learning")},
            {"learning_states", "learning_sessions"},
        )
        for legacy in ("memories", "sources", "memory_sources", "schema_migrations"):
            self.assertIn(legacy, tables)


class SessionStatusUsageTest(LearningServiceTestCase):
    def test_closed_statuses_keep_their_ended_at(self) -> None:
        session = self.service.start_session(source_id=self.source.id).session
        completed = self.learning.get_session(self.service.finish_session(session.id).session.id)

        self.assertEqual(completed.status, SessionStatus.COMPLETED)
        self.assertIsNotNone(completed.ended_at)
        self.assertEqual(completed.current_stage, TeachingStage.EXPLAIN)
        self.assertEqual(
            str(self.service.start_session(source_id=self.source.id).session.status), "active"
        )

    def test_understanding_level_defaults_stay_unknown_across_starts(self) -> None:
        self.service.start_session(source_id=self.source.id)
        self.service.finish_session(self.learning.list_active_sessions()[0].id)
        context = self.service.start_session(source_id=self.source.id)

        for state in context.states:
            self.assertEqual(str(state.understanding_level), str(UnderstandingLevel.UNKNOWN))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
