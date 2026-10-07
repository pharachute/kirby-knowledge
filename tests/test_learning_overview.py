"""Phase 2B-4: ``LearningService.get_learning_overview`` + ``LearningOverview``.

Locked behaviour of the Source-scoped read model:

* Source -> Memory -> State alignment is positional (``states[i]`` may be ``None``)
* the five counters are derived from the states and never disagree with them
* the Memory order is the existing Source order (never re-sorted by progress)
* it is genuinely read-only: no state/session is created, no row is updated
* an existing but empty Source is a valid all-zero overview; an unknown Source is
  a ``NotFoundError``
* ``as_dict()`` is plain JSON data (no dataclass, no repository, no sqlite Row)
"""

from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import unittest

from personal_memory import (
    LearningOverview,
    LearningRepository,
    LearningService,
    LearningState,
    MemoryStatus,
    UnderstandingLevel,
)
from personal_memory.errors import NotFoundError, ValidationError
from personal_memory.learning import PUBLIC_API
from personal_memory.retrieval import MemoryRetriever

from .helpers import RepositoryTestCase


class OverviewTestCase(RepositoryTestCase):
    """One Source with three Memories (A, B, C) in creation order."""

    prefix = "pms-learnover-"

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

    # -- helpers ----------------------------------------------------------
    def overview(self, source=None):
        return self.service.get_learning_overview((source or self.source).id)

    def ids(self, overview):
        return [memory.id for memory in overview.memories]

    def full_snapshot(self):
        """Everything that must not move while reading."""
        memory = self.repo.get_memory(self.a.id)
        source = self.repo.get_source(self.source.id)
        return {
            "counts": self.repo.counts(),
            "learning": self.learning.counts(),
            "index_word": self.repo.index_row_count("word"),
            "index_trigram": self.repo.index_row_count("trigram"),
            "memory_updated_at": memory.updated_at,
            "source_updated_at": source.updated_at,
            "search": [(hit.memory.id, hit.score) for hit in MemoryRetriever(self.repo).search("极限").hits],
        }

    def seed_state(self, memory, **kwargs):
        kwargs.setdefault("memory_id", memory.id)
        return self.learning.upsert_state(LearningState.create(**kwargs))


class BasicsTest(OverviewTestCase):
    def test_a_source_with_memories_but_no_learning_data(self) -> None:
        overview = self.overview()

        self.assertEqual(self.ids(overview), [self.a.id, self.b.id, self.c.id])
        self.assertEqual(overview.states, (None, None, None))
        self.assertEqual(overview.total_memories, 3)
        self.assertEqual((overview.learned_memories, overview.assessed_memories,
                          overview.solid_memories, overview.total_learning_count), (0, 0, 0, 0))
        self.assertIsNone(overview.active_session)
        self.assertEqual(overview.source.id, self.source.id)

    def test_order_is_the_source_order_and_is_never_re_sorted(self) -> None:
        # make C the most-learned and A the best understood: order must not move
        self.seed_state(self.c, learn_count=9)
        self.seed_state(self.a, understanding_level=UnderstandingLevel.SOLID)

        overview = self.overview()

        self.assertEqual(self.ids(overview), [self.a.id, self.b.id, self.c.id])
        self.assertEqual([state.learn_count if state else 0 for state in overview.states], [0, 0, 9])

    def test_two_sources_keep_their_own_memories(self) -> None:
        other = self.make_source(title="另一份材料")
        stranger = self.make_memory(title="别人家的记忆")
        self.repo.link(stranger.id, other.id)

        self.assertEqual(self.ids(self.overview()), [self.a.id, self.b.id, self.c.id])
        self.assertEqual(self.ids(self.overview(other)), [stranger.id])
        self.assertEqual(self.overview(other).total_memories, 1)

    def test_unknown_source_is_not_found(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.service.get_learning_overview("src_missing")
        self.assertEqual(ctx.exception.entity, "source")
        self.assertEqual(ctx.exception.entity_id, "src_missing")

    def test_empty_source_returns_an_all_zero_overview(self) -> None:
        empty = self.make_source(title="还没有任何记忆的材料")

        overview = self.overview(empty)

        self.assertEqual(overview.memories, ())
        self.assertEqual(overview.states, ())
        self.assertEqual(overview.total_memories, 0)
        self.assertEqual((overview.learned_memories, overview.assessed_memories,
                          overview.solid_memories, overview.total_learning_count), (0, 0, 0, 0))
        self.assertIsNone(overview.active_session)

    def test_empty_source_differs_from_start_session_semantics(self) -> None:
        empty = self.make_source(title="空材料")
        with self.assertRaises(ValidationError):
            self.service.start_session(source_id=empty.id)          # cannot learn nothing
        self.assertEqual(self.overview(empty).total_memories, 0)    # but overview is fine

    def test_overview_does_not_start_a_session(self) -> None:
        self.overview()
        self.assertEqual(self.learning.counts()["learning_sessions"], 0)


class StateAlignmentTest(OverviewTestCase):
    def test_all_memories_have_states(self) -> None:
        for memory in (self.a, self.b, self.c):
            self.seed_state(memory)

        overview = self.overview()

        self.assertEqual(len(overview.memories), len(overview.states))
        self.assertTrue(all(state is not None for state in overview.states))

    def test_a_missing_state_keeps_its_position_as_none(self) -> None:
        self.seed_state(self.a, learn_count=2)
        self.seed_state(self.c, understanding_level=UnderstandingLevel.PARTIAL)

        overview = self.overview()

        self.assertEqual(self.ids(overview), [self.a.id, self.b.id, self.c.id])
        self.assertIsNotNone(overview.states[0])
        self.assertIsNone(overview.states[1])                        # B keeps its slot
        self.assertIsNotNone(overview.states[2])
        self.assertEqual(overview.states[0].learn_count, 2)
        self.assertEqual(str(overview.states[2].understanding_level), "partial")

    def test_no_memory_has_a_state(self) -> None:
        overview = self.overview()
        self.assertEqual(overview.states, (None, None, None))

    def test_state_for_and_memory_for(self) -> None:
        self.seed_state(self.b, understanding_level=UnderstandingLevel.FUZZY)
        overview = self.overview()

        self.assertEqual(overview.memory_for(self.b.id).title, "连续")
        self.assertEqual(str(overview.state_for(self.b.id).understanding_level), "fuzzy")
        self.assertIsNone(overview.state_for(self.a.id))             # exists, no state
        self.assertIsNone(overview.state_for("mem_missing"))
        self.assertIsNone(overview.memory_for("mem_missing"))

    def test_alignment_is_enforced_by_the_dataclass(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            LearningOverview(source=self.source, memories=(self.a, self.b), states=(None,))
        self.assertEqual(ctx.exception.fields, ("states",))

    def test_states_are_immutable_tuples(self) -> None:
        overview = self.overview()
        self.assertIsInstance(overview.memories, tuple)
        self.assertIsInstance(overview.states, tuple)


class StatsTest(OverviewTestCase):
    def test_learned_counts_only_learn_count_above_zero(self) -> None:
        self.seed_state(self.a, learn_count=1)
        self.seed_state(self.b, learn_count=0)
        self.seed_state(self.c, learn_count=4)

        overview = self.overview()

        self.assertEqual(overview.learned_memories, 2)               # A and C
        self.assertEqual(overview.total_learning_count, 5)

    def test_assessed_counts_every_level_but_unknown(self) -> None:
        self.seed_state(self.a, understanding_level=UnderstandingLevel.FUZZY)
        self.seed_state(self.b, understanding_level=UnderstandingLevel.UNKNOWN)
        self.seed_state(self.c, understanding_level=UnderstandingLevel.PARTIAL)

        overview = self.overview()

        self.assertEqual(overview.assessed_memories, 2)              # fuzzy + partial
        self.assertEqual(overview.solid_memories, 0)

    def test_solid_counts_only_the_solid_level(self) -> None:
        self.seed_state(self.a, understanding_level=UnderstandingLevel.SOLID)
        self.seed_state(self.b, understanding_level=UnderstandingLevel.SOLID)
        self.seed_state(self.c, understanding_level=UnderstandingLevel.PARTIAL)

        overview = self.overview()

        self.assertEqual(overview.solid_memories, 2)
        self.assertEqual(overview.assessed_memories, 3)

    def test_memories_without_a_state_contribute_nothing(self) -> None:
        self.seed_state(self.a, learn_count=3, understanding_level=UnderstandingLevel.SOLID)

        overview = self.overview()

        self.assertEqual((overview.total_memories, overview.learned_memories,
                          overview.assessed_memories, overview.solid_memories,
                          overview.total_learning_count), (3, 1, 1, 1, 3))

    def test_states_of_other_sources_are_not_counted(self) -> None:
        other = self.make_source(title="另一份材料")
        stranger = self.make_memory(title="别人家的记忆")
        self.repo.link(stranger.id, other.id)
        self.seed_state(stranger, learn_count=7, understanding_level=UnderstandingLevel.SOLID)

        overview = self.overview()

        self.assertEqual(overview.total_memories, 3)
        self.assertEqual(overview.total_learning_count, 0)
        self.assertEqual(overview.solid_memories, 0)

    def test_total_learning_count_is_the_sum_of_learn_counts(self) -> None:
        self.seed_state(self.a, learn_count=2)
        self.seed_state(self.b, learn_count=5)
        self.seed_state(self.c, learn_count=0)

        overview = self.overview()

        expected = sum(state.learn_count for state in overview.states if state is not None)
        self.assertEqual(overview.total_learning_count, expected)
        self.assertEqual(overview.total_learning_count, 7)

    def test_counters_are_derived_from_states_not_passed_in(self) -> None:
        self.seed_state(self.a, learn_count=2, understanding_level=UnderstandingLevel.SOLID)

        overview = self.overview()

        self.assertEqual(overview.total_memories, len(overview.memories))
        self.assertEqual(overview.total_memories, len(overview.states))
        self.assertEqual(overview.learned_memories,
                         sum(1 for s in overview.states if s and s.learn_count > 0))
        self.assertEqual(overview.solid_memories,
                         sum(1 for s in overview.states
                             if s and str(s.understanding_level) == str(UnderstandingLevel.SOLID)))

    def test_all_unknown_states_are_not_assessed(self) -> None:
        for memory in (self.a, self.b, self.c):
            self.seed_state(memory, understanding_level=UnderstandingLevel.UNKNOWN)

        overview = self.overview()

        self.assertEqual(overview.assessed_memories, 0)
        self.assertEqual(overview.solid_memories, 0)


class ActiveSessionTest(OverviewTestCase):
    def test_no_active_session(self) -> None:
        self.assertIsNone(self.overview().active_session)

    def test_active_session_is_reported(self) -> None:
        session = self.service.start_session(source_id=self.source.id).session

        overview = self.overview()

        self.assertIsNotNone(overview.active_session)
        self.assertEqual(overview.active_session.id, session.id)
        self.assertEqual(overview.active_session.plan, list(self.ids(overview)))

    def test_a_finished_session_is_not_active(self) -> None:
        session = self.service.start_session(source_id=self.source.id).session
        self.service.finish_session(session.id)

        overview = self.overview()

        self.assertIsNone(overview.active_session)
        self.assertEqual(str(self.learning.get_session(session.id).status), "completed")

    def test_an_abandoned_session_is_not_active(self) -> None:
        session = self.service.start_session(source_id=self.source.id).session
        self.service.abandon_session(session.id)

        self.assertIsNone(self.overview().active_session)

    def test_the_next_active_session_is_reported(self) -> None:
        first = self.service.start_session(source_id=self.source.id).session
        self.service.finish_session(first.id)
        second = self.service.start_session(source_id=self.source.id, memory_ids=[self.b.id]).session

        overview = self.overview()

        self.assertEqual(overview.active_session.id, second.id)
        self.assertNotEqual(overview.active_session.id, first.id)

    def test_session_of_another_source_is_ignored(self) -> None:
        other = self.make_source(title="另一份材料")
        stranger = self.make_memory(title="别人家的记忆")
        self.repo.link(stranger.id, other.id)
        self.service.start_session(source_id=other.id)

        self.assertIsNone(self.overview().active_session)


class DynamicReflectionTest(OverviewTestCase):
    def test_learning_events_are_reflected_immediately(self) -> None:
        before = self.overview()
        session_id = self.service.start_session(source_id=self.source.id).session.id

        self.service.record_learning(session_id)
        after_one = self.overview()
        self.service.record_learning(session_id)
        after_two = self.overview()

        self.assertEqual(before.total_learning_count, 0)
        self.assertEqual(after_one.total_learning_count, 1)
        self.assertEqual(after_one.learned_memories, 1)
        self.assertEqual(after_two.total_learning_count, 2)
        self.assertEqual(after_two.learned_memories, 2)
        self.assertEqual(after_two.assessed_memories, 0)             # events do not assess
        self.assertEqual(after_two.solid_memories, 0)
        self.assertEqual(after_two.state_for(self.a.id).learn_count, 1)
        self.assertIsNotNone(after_two.state_for(self.a.id).last_learned_at)

    def test_a_learning_event_never_changes_the_assessment_counters(self) -> None:
        session_id = self.service.start_session(source_id=self.source.id).session.id
        self.service.record_assessment(session_id, understanding_level="partial")
        before = self.overview()

        self.service.record_learning(session_id)
        after = self.overview()

        self.assertEqual(after.assessed_memories, before.assessed_memories)
        self.assertEqual(after.solid_memories, before.solid_memories)
        self.assertEqual(after.total_learning_count, before.total_learning_count + 1)

    def test_assessments_are_reflected_immediately(self) -> None:
        session_id = self.service.start_session(source_id=self.source.id).session.id
        self.assertEqual(self.overview().assessed_memories, 0)

        self.service.record_assessment(session_id, understanding_level="partial")
        partial = self.overview()
        self.service.record_assessment(session_id, understanding_level="solid")
        solid = self.overview()

        self.assertEqual(partial.assessed_memories, 1)
        self.assertEqual(partial.solid_memories, 0)
        self.assertEqual(str(partial.state_for(self.a.id).understanding_level), "partial")
        self.assertEqual(solid.assessed_memories, 1)
        self.assertEqual(solid.solid_memories, 1)
        self.assertEqual(str(solid.state_for(self.a.id).understanding_level), "solid")

    def test_assessment_does_not_create_a_learning_event(self) -> None:
        session_id = self.service.start_session(source_id=self.source.id).session.id

        self.service.record_assessment(session_id, understanding_level="solid")
        overview = self.overview()

        self.assertEqual(overview.total_learning_count, 0)
        self.assertEqual(overview.learned_memories, 0)
        self.assertEqual(overview.state_for(self.a.id).learn_count, 0)
        self.assertIsNone(overview.state_for(self.a.id).last_learned_at)

    def test_counts_grow_across_two_memories(self) -> None:
        session_id = self.service.start_session(source_id=self.source.id).session.id
        self.service.record_learning(session_id)                     # A
        self.service.record_assessment(session_id, understanding_level="solid")   # B
        self.service.record_learning(session_id)                     # B (also a learning event)

        overview = self.overview()

        self.assertEqual((overview.learned_memories, overview.assessed_memories,
                          overview.solid_memories, overview.total_learning_count), (2, 1, 1, 2))

    def test_overview_of_an_exhausted_plan_still_reports_the_session(self) -> None:
        session_id = self.service.start_session(source_id=self.source.id).session.id
        for _ in range(3):
            self.service.record_learning(session_id)

        overview = self.overview()

        self.assertIsNotNone(overview.active_session)                # not finished, only exhausted
        self.assertEqual(overview.active_session.plan_cursor, 3)
        self.assertEqual(overview.total_learning_count, 3)


class ReadOnlyGuaranteeTest(OverviewTestCase):
    def test_reading_changes_nothing(self) -> None:
        session_id = self.service.start_session(source_id=self.source.id).session.id
        self.service.record_learning(session_id)
        self.service.record_assessment(session_id, understanding_level="partial")
        before = self.full_snapshot()
        state_before = self.learning.get_state(self.b.id)
        session_before = self.learning.get_session(session_id)
        self.assertIsNotNone(state_before)                           # start_session created it
        self.assertEqual(self.learning.counts(),
                         {"learning_states": 3, "learning_sessions": 1})

        for _ in range(3):
            self.overview()

        self.assertEqual(self.full_snapshot(), before)
        self.assertEqual(self.learning.get_state(self.b.id).as_dict(), state_before.as_dict())
        self.assertEqual(self.learning.counts(), {"learning_states": 3, "learning_sessions": 1})
        self.assertEqual(self.learning.get_session(session_id).updated_at, session_before.updated_at)

    def test_reading_never_creates_missing_states(self) -> None:
        self.assertEqual(self.learning.counts()["learning_states"], 0)

        for _ in range(3):
            overview = self.overview()
            self.assertEqual(overview.states, (None, None, None))

        self.assertEqual(self.learning.counts()["learning_states"], 0)

    def test_reading_never_creates_a_session(self) -> None:
        self.overview()
        self.assertEqual(self.learning.counts()["learning_sessions"], 0)
        self.assertIsNone(self.service.get_active_session(self.source.id))

    def test_state_and_session_rows_are_untouched(self) -> None:
        session_id = self.service.start_session(source_id=self.source.id).session.id
        self.service.record_learning(session_id)
        state_updated = self.learning.get_state(self.a.id).updated_at
        session_updated = self.learning.get_session(session_id).updated_at

        self.overview()

        self.assertEqual(self.learning.get_state(self.a.id).updated_at, state_updated)
        self.assertEqual(self.learning.get_session(session_id).updated_at, session_updated)

    def test_1_0_tables_and_index_are_untouched(self) -> None:
        self.service.record_learning(
            self.service.start_session(source_id=self.source.id).session.id)
        before = self.full_snapshot()

        self.overview()

        after = self.full_snapshot()
        self.assertEqual(after["counts"], before["counts"])
        self.assertEqual(after["index_word"], before["index_word"])
        self.assertEqual(after["index_trigram"], before["index_trigram"])
        self.assertEqual(after["search"], before["search"])
        self.assertEqual(after["memory_updated_at"], before["memory_updated_at"])
        self.assertEqual(after["source_updated_at"], before["source_updated_at"])


class AsDictTest(OverviewTestCase):
    def test_as_dict_shape_and_alignment(self) -> None:
        self.seed_state(self.a, learn_count=2, understanding_level=UnderstandingLevel.SOLID)
        without_session = self.overview().as_dict()

        self.assertEqual(sorted(without_session), ["active_session", "memories", "source", "states", "stats"])
        self.assertEqual(len(without_session["memories"]), 3)
        self.assertEqual(len(without_session["states"]), 3)
        self.assertIsNone(without_session["states"][1])              # B keeps its None slot
        self.assertEqual(without_session["states"][0]["learn_count"], 2)
        self.assertIsNone(without_session["active_session"])

        # starting a session initialises a default state for every Memory (2B-1 rule)
        session = self.service.start_session(source_id=self.source.id).session
        payload = self.overview().as_dict()
        self.assertTrue(all(state is not None for state in payload["states"]))
        self.assertEqual(payload["states"][1]["understanding_level"], "unknown")
        self.assertEqual(payload["active_session"]["id"], session.id)
        self.assertEqual(sorted(payload["stats"]),
                         ["assessed_memories", "learned_memories", "solid_memories",
                          "total_learning_count", "total_memories"])
        self.assertEqual(payload["stats"]["total_memories"], 3)
        self.assertEqual(payload["stats"]["solid_memories"], 1)

    def test_as_dict_is_json_serialisable(self) -> None:
        self.seed_state(self.a, learn_count=1, known_aspects=["极限是趋近"])
        self.service.start_session(source_id=self.source.id)

        text = json.dumps(self.overview().as_dict(), ensure_ascii=False)

        self.assertIn("极限", text)
        self.assertIn("total_learning_count", text)

    def test_as_dict_leaks_no_internal_objects(self) -> None:
        payload = self.overview().as_dict()

        def check(value) -> None:
            self.assertFalse(dataclasses.is_dataclass(value), type(value).__name__)
            self.assertFalse(hasattr(value, "database"), type(value).__name__)
            self.assertFalse(hasattr(value, "transaction"), type(value).__name__)
            if isinstance(value, dict):
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
            else:
                self.assertIn(type(value).__name__, {"str", "int", "float", "bool", "NoneType", "dict", "list"})

        check(payload)
        self.assertNotIn("rows", payload)

    def test_empty_source_payload(self) -> None:
        payload = self.overview(self.make_source(title="空材料")).as_dict()

        self.assertEqual(payload["memories"], [])
        self.assertEqual(payload["states"], [])
        self.assertIsNone(payload["active_session"])
        self.assertEqual(payload["stats"]["total_memories"], 0)


class OverviewContractTest(OverviewTestCase):
    def test_get_learning_overview_is_public(self) -> None:
        self.assertIn("get_learning_overview", PUBLIC_API)
        self.assertTrue(callable(LearningService.get_learning_overview))
        self.assertEqual(
            PUBLIC_API,
            ("start_session", "record_learning", "record_assessment", "get_context",
             "get_learning_overview", "get_active_session", "finish_session", "abandon_session"),
        )

    def test_learning_overview_is_a_frozen_dataclass(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(LearningOverview))
        overview = self.overview()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            overview.total_memories = 99                             # type: ignore[misc]
        self.assertEqual(overview.total_memories, 3)
        self.assertEqual({f.name for f in dataclasses.fields(LearningOverview)},
                         {"source", "memories", "states", "active_session", "total_memories",
                          "learned_memories", "assessed_memories", "solid_memories",
                          "total_learning_count"})

    def test_the_read_model_never_calls_a_write_method(self) -> None:
        source = pathlib.Path("personal_memory/learning.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "get_learning_overview")
        body = list(function.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        code = "\n".join(ast.unparse(node) for node in body)

        for forbidden in ("create_state", "update_state", "upsert_state", "create_session",
                          "update_session", "finish_session", "abandon_session",
                          "start_session", "transaction"):
            self.assertNotIn(forbidden, code, f"get_learning_overview must not call {forbidden}")

    def test_no_new_tables(self) -> None:
        with self.database.connection() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual({name for name in tables if name.startswith("learning")},
                         {"learning_states", "learning_sessions"})

    def test_overview_does_not_mutate_1_0_data(self) -> None:
        before = self.repo.counts()
        memory_before = self.repo.get_memory(self.a.id).as_dict()

        self.overview()

        self.assertEqual(self.repo.counts(), before)
        self.assertEqual(self.repo.get_memory(self.a.id).as_dict(), memory_before)
        self.assertEqual(self.repo.require_memory(self.a.id).status, MemoryStatus.ACTIVE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
