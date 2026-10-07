"""Phase 2A: LearningState / LearningSession model validation.

Pure unit tests -- no database involved.  They lock the *shape* claimed by
``docs/schema.sql`` and the repository: the enums, the defaults, the bounds and
the "absence means never studied" semantics.
"""

from __future__ import annotations

import unittest

from personal_memory.errors import ValidationError
from personal_memory.learning_models import (
    LEARNING_SCHEMA_VERSION,
    MAX_ASPECTS,
    MAX_ASPECT_LENGTH,
    MAX_PLAN_ITEMS,
    SESSION_STATUS_LABELS,
    TEACHING_STAGE_LABELS,
    UNDERSTANDING_LABELS,
    LearningSession,
    LearningState,
    SessionStatus,
    TeachingStage,
    UnderstandingLevel,
    new_learning_session_id,
)


class LearningStateDefaultsTest(unittest.TestCase):
    def test_create_gives_the_documented_defaults(self) -> None:
        state = LearningState.create(memory_id="mem_abc")

        self.assertEqual(str(state.understanding_level), "unknown")
        self.assertEqual(state.known_aspects, [])
        self.assertEqual(state.weak_aspects, [])
        self.assertEqual(state.misconceptions, [])
        self.assertEqual(state.learn_count, 0)           # creating a state is NOT a learning event
        self.assertIsNone(state.last_learned_at)         # NULL = never actually learned
        self.assertEqual(state.schema_version, LEARNING_SCHEMA_VERSION)
        self.assertTrue(state.created_at)
        self.assertEqual(state.updated_at, state.created_at)

    def test_enum_members_are_exactly_the_documented_ones(self) -> None:
        self.assertEqual([m.value for m in UnderstandingLevel], ["unknown", "fuzzy", "partial", "solid"])
        self.assertEqual([m.value for m in SessionStatus], ["active", "completed", "abandoned"])
        self.assertEqual(
            [m.value for m in TeachingStage],
            ["explain", "question", "analyze", "remedy", "reinforce", "practice", "done"],
        )

    def test_default_labels_are_chinese_and_complete(self) -> None:
        for level in UnderstandingLevel:
            self.assertTrue(UNDERSTANDING_LABELS[str(level)].strip())
        for status in SessionStatus:
            self.assertTrue(SESSION_STATUS_LABELS[str(status)].strip())
        for stage in TeachingStage:
            self.assertTrue(TEACHING_STAGE_LABELS[str(stage)].strip())
        self.assertEqual(UNDERSTANDING_LABELS["unknown"], "还没学过")

    def test_session_id_uses_the_project_id_convention(self) -> None:
        first, second = new_learning_session_id(), new_learning_session_id()
        self.assertTrue(first.startswith("lrn_"))
        self.assertEqual(len(first), len("lrn_") + 32)
        self.assertNotEqual(first, second)


class LearningStateValidationTest(unittest.TestCase):
    def assert_rejects(self, field: str, **kwargs) -> None:
        with self.assertRaises(ValidationError) as ctx:
            LearningState.create(**kwargs)
        self.assertIn(field, ctx.exception.fields)

    def test_invalid_memory_id(self) -> None:
        for bad in ("", "  ", "mem with space", "a" * 200, 42, None):
            self.assert_rejects("memory_id", memory_id=bad)

    def test_invalid_understanding_level(self) -> None:
        self.assert_rejects("understanding_level", memory_id="mem_a", understanding_level="mastered")
        self.assert_rejects("understanding_level", memory_id="mem_a", understanding_level=0.8)
        self.assert_rejects("understanding_level", memory_id="mem_a", understanding_level=None)

    def test_level_is_coerced_from_a_string(self) -> None:
        state = LearningState.create(memory_id="mem_a", understanding_level=" SOLID ")
        self.assertIsInstance(state.understanding_level, UnderstandingLevel)
        self.assertEqual(str(state.understanding_level), "solid")

    def test_aspects_must_be_a_sequence_of_short_strings(self) -> None:
        self.assert_rejects("known_aspects", memory_id="mem_a", known_aspects="极限")
        self.assert_rejects("known_aspects", memory_id="mem_a", known_aspects=[42])
        self.assert_rejects("known_aspects", memory_id="mem_a", known_aspects=["   "])
        self.assert_rejects(
            "known_aspects", memory_id="mem_a", known_aspects=["x" * (MAX_ASPECT_LENGTH + 1)]
        )

    def test_aspects_are_trimmed_deduped_and_capped(self) -> None:
        state = LearningState.create(
            memory_id="mem_a", known_aspects=["  极限  是趋近  ", "极限 是趋近", "ε-δ"]
        )
        self.assertEqual(state.known_aspects, ["极限 是趋近", "ε-δ"])
        with self.assertRaises(ValidationError) as ctx:
            LearningState.create(memory_id="mem_a", known_aspects=[f"要点{i}" for i in range(MAX_ASPECTS + 1)])
        self.assertIn("known_aspects", ctx.exception.fields)

    def test_learn_count_must_be_a_non_negative_integer(self) -> None:
        self.assert_rejects("learn_count", memory_id="mem_a", learn_count=-1)
        self.assert_rejects("learn_count", memory_id="mem_a", learn_count=1.5)
        self.assert_rejects("learn_count", memory_id="mem_a", learn_count=True)
        self.assert_rejects("learn_count", memory_id="mem_a", learn_count="3")

    def test_last_learned_at_may_be_null_but_not_garbage(self) -> None:
        self.assertIsNone(LearningState.create(memory_id="mem_a", last_learned_at=None).last_learned_at)
        self.assert_rejects("last_learned_at", memory_id="mem_a", last_learned_at="not-a-time")
        state = LearningState.create(memory_id="mem_a", last_learned_at="2026-10-07T08:00:00.000Z")
        self.assertEqual(state.last_learned_at, "2026-10-07T08:00:00.000Z")

    def test_timestamps_and_schema_version_are_checked(self) -> None:
        self.assert_rejects("created_at", memory_id="mem_a", created_at="yesterday")
        self.assert_rejects("schema_version", memory_id="mem_a", schema_version=0)
        self.assert_rejects("schema_version", memory_id="mem_a", schema_version=LEARNING_SCHEMA_VERSION + 1)
        self.assert_rejects("schema_version", memory_id="mem_a", schema_version="1")

    def test_record_round_trip_and_json_view(self) -> None:
        state = LearningState.create(
            memory_id="mem_a", understanding_level=UnderstandingLevel.PARTIAL,
            known_aspects=["极限是趋近"], weak_aspects=["ε-δ"], misconceptions=["极限等于函数值"],
            learn_count=2, last_learned_at="2026-10-07T08:00:00.000Z",
        )
        record = state.to_record()
        self.assertEqual(
            sorted(record),
            ["created_at", "known_aspects_json", "last_learned_at", "learn_count",
             "memory_id", "misconceptions_json", "schema_version", "understanding_level",
             "updated_at", "weak_aspects_json"],
        )
        self.assertEqual(LearningState.from_record(record).as_dict(), state.as_dict())
        view = state.as_dict()
        self.assertEqual(view["known_aspects"], ["极限是趋近"])
        self.assertNotIn("known_aspects_json", view)
        self.assertEqual(view["misconceptions"], ["极限等于函数值"])


class LearningSessionValidationTest(unittest.TestCase):
    def assert_rejects(self, field: str, **kwargs) -> None:
        with self.assertRaises(ValidationError) as ctx:
            LearningSession.create(**kwargs)
        self.assertIn(field, ctx.exception.fields)

    def test_create_gives_the_documented_defaults(self) -> None:
        session = LearningSession.create(source_id="src_a")

        self.assertTrue(session.id.startswith("lrn_"))
        self.assertEqual(str(session.status), "active")
        self.assertEqual(str(session.current_stage), "explain")
        self.assertIsNone(session.current_memory_id)
        self.assertEqual(session.plan, [])
        self.assertEqual(session.plan_cursor, 0)
        self.assertEqual(session.exchange, {})
        self.assertIsNone(session.ended_at)
        self.assertEqual(session.updated_at, session.started_at)
        self.assertEqual(session.schema_version, LEARNING_SCHEMA_VERSION)

    def test_invalid_status_and_stage(self) -> None:
        self.assert_rejects("status", source_id="src_a", status="paused")
        self.assert_rejects("status", source_id="src_a", status=3)
        self.assert_rejects("current_stage", source_id="src_a", current_stage="teaching")
        self.assert_rejects("current_stage", source_id="src_a", current_stage=None)

    def test_invalid_ids(self) -> None:
        self.assert_rejects("source_id", source_id="")
        self.assert_rejects("source_id", source_id="src a")
        self.assert_rejects("current_memory_id", source_id="src_a", current_memory_id="not an id")
        self.assert_rejects("id", source_id="src_a", session_id="lrn bad")

    def test_plan_must_be_a_list_of_memory_ids(self) -> None:
        self.assert_rejects("plan", source_id="src_a", plan="mem_a")
        self.assert_rejects("plan", source_id="src_a", plan=[42])
        self.assert_rejects("plan", source_id="src_a", plan=["mem_ok", "bad id"])
        session = LearningSession.create(source_id="src_a", plan=["mem_b", "mem_a", "mem_b"])
        self.assertEqual(session.plan, ["mem_b", "mem_a"])          # order kept, duplicates dropped
        with self.assertRaises(ValidationError) as ctx:
            LearningSession.create(source_id="src_a", plan=[f"mem_{i}" for i in range(MAX_PLAN_ITEMS + 1)])
        self.assertIn("plan", ctx.exception.fields)

    def test_plan_cursor_must_be_a_non_negative_integer(self) -> None:
        self.assert_rejects("plan_cursor", source_id="src_a", plan_cursor=-1)
        self.assert_rejects("plan_cursor", source_id="src_a", plan_cursor=0.5)
        self.assert_rejects("plan_cursor", source_id="src_a", plan_cursor=True)

    def test_exchange_must_be_a_json_object(self) -> None:
        self.assert_rejects("exchange", source_id="src_a", exchange=["not", "an", "object"])
        self.assert_rejects("exchange", source_id="src_a", exchange={1: "bad key"})
        self.assert_rejects("exchange", source_id="src_a", exchange={"bad": {1, 2}})
        session = LearningSession.create(source_id="src_a", exchange={"question": "什么是极限？"})
        self.assertEqual(session.exchange, {"question": "什么是极限？"})

    def test_status_and_ended_at_must_agree(self) -> None:
        self.assert_rejects(
            "ended_at", source_id="src_a", status=SessionStatus.COMPLETED
        )
        self.assert_rejects(
            "ended_at", source_id="src_a", status=SessionStatus.ABANDONED
        )
        self.assert_rejects(
            "ended_at", source_id="src_a",
            ended_at="2026-10-07T08:00:00.000Z",
        )
        finished = LearningSession.create(
            source_id="src_a", status=SessionStatus.COMPLETED, ended_at="2026-10-07T08:00:00.000Z"
        )
        self.assertEqual(str(finished.status), "completed")

    def test_timestamps_and_schema_version_are_checked(self) -> None:
        self.assert_rejects("started_at", source_id="src_a", started_at="nope")
        self.assert_rejects("schema_version", source_id="src_a", schema_version=0)

    def test_record_round_trip_and_json_view(self) -> None:
        session = LearningSession.create(
            source_id="src_a", current_memory_id="mem_a", current_stage=TeachingStage.QUESTION,
            plan=["mem_a", "mem_b"], plan_cursor=1, exchange={"answer": "趋近"},
        )
        record = session.to_record()
        self.assertEqual(
            sorted(record),
            ["current_memory_id", "current_stage", "ended_at", "exchange_json", "id", "plan_cursor",
             "plan_json", "schema_version", "source_id", "started_at", "status", "updated_at"],
        )
        self.assertEqual(LearningSession.from_record(record).as_dict(), session.as_dict())
        view = session.as_dict()
        self.assertEqual(view["plan"], ["mem_a", "mem_b"])
        self.assertEqual(view["exchange"], {"answer": "趋近"})
        self.assertNotIn("plan_json", view)
        self.assertNotIn("exchange_json", view)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
