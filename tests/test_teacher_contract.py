"""Phase 2C-1: Teacher Agent contract tests.

The contract is *structure only*: these tests lock what a model may say, what it may
never say, and prove that saying it touches nothing.

Groups: context projection / actions / assessment / request / response /
permission boundary / architecture (AST) / 1.0 compatibility.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import unittest

from personal_memory import LearningRepository, LearningService, LearningState, UnderstandingLevel
from personal_memory.errors import ValidationError
from personal_memory.learning_models import MAX_ASPECTS, MAX_ASPECT_LENGTH
from personal_memory.retrieval import MemoryRetriever
from personal_memory.teacher import (
    ALLOWED_ACTION_TYPES,
    FORBIDDEN_ACTION_TYPES,
    AbandonSessionAction,
    FinishSessionAction,
    RecordAssessmentAction,
    RecordLearningAction,
    TeacherAction,
    TeacherContext,
    TeacherTurnRequest,
    TeacherTurnResponse,
)

from .helpers import RepositoryTestCase

TEACHER_SOURCE = pathlib.Path("personal_memory/teacher.py")


class TeacherTestCase(RepositoryTestCase):
    """A Source with three Memories and a started session."""

    prefix = "pms-teacher-"

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
    def build_context(self) -> TeacherContext:
        return TeacherContext.from_learning(
            self.service.get_context(self.session_id),
            self.service.get_learning_overview(self.source.id),
        )

    def db_snapshot(self):
        return {
            "counts": self.repo.counts(),
            "learning": self.learning.counts(),
            "index_word": self.repo.index_row_count("word"),
            "index_trigram": self.repo.index_row_count("trigram"),
        }


class TeacherContextTest(TeacherTestCase):
    def test_projection_of_a_running_session(self) -> None:
        context = self.build_context()

        self.assertEqual(context.session_id, self.session_id)
        self.assertEqual(context.source.id, self.source.id)
        self.assertEqual(context.session.id, self.session_id)
        self.assertEqual(context.current_memory.id, self.a.id)          # plan[0]
        self.assertEqual(context.current_memory_id, self.a.id)
        self.assertEqual(str(context.current_state.understanding_level), "unknown")
        self.assertEqual(context.current_state.memory_id, self.a.id)
        self.assertEqual(context.overview.total_memories, 3)
        self.assertEqual(context.overview.active_session.id, self.session_id)

    def test_current_memory_follows_the_session(self) -> None:
        self.service.record_learning(self.session_id)                   # A done -> B current

        context = self.build_context()

        self.assertEqual(context.current_memory.id, self.b.id)
        self.assertEqual(context.current_state.memory_id, self.b.id)

    def test_current_state_is_none_when_the_memory_has_no_state(self) -> None:
        with self.database.transaction() as conn:
            conn.execute("DELETE FROM learning_states WHERE memory_id = ?", (self.a.id,))

        context = self.build_context()

        self.assertEqual(context.current_memory.id, self.a.id)
        self.assertIsNone(context.current_state)
        self.assertEqual(context.overview.states[0], None)

    def test_an_exhausted_plan_yields_none_instead_of_inventing_a_state(self) -> None:
        for _ in range(3):
            self.service.record_learning(self.session_id)

        context = self.build_context()

        self.assertIsNone(context.current_memory)
        self.assertIsNone(context.current_state)
        self.assertIsNone(context.current_memory_id)
        self.assertEqual(str(context.session.status), "active")         # unchanged by the projection
        self.assertEqual(context.overview.total_learning_count, 3)

    def test_overview_reflects_the_latest_state(self) -> None:
        self.service.record_assessment(self.session_id, understanding_level="solid")

        context = self.build_context()

        self.assertEqual(context.overview.solid_memories, 1)
        self.assertEqual(context.overview.assessed_memories, 1)
        self.assertEqual(str(context.current_state.understanding_level), "solid")

    def test_context_is_frozen(self) -> None:
        context = self.build_context()
        self.assertTrue(dataclasses.is_dataclass(context))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            context.current_memory = None                              # type: ignore[misc]

    def test_context_is_json_serialisable_and_leaks_nothing(self) -> None:
        payload = self.build_context().as_dict()

        self.assertEqual(sorted(payload),
                         ["current_memory", "current_state", "overview", "session", "source"])
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("极限", text)

        def check(value) -> None:
            self.assertFalse(dataclasses.is_dataclass(value), type(value).__name__)
            for forbidden in ("database", "transaction", "connection", "repository"):
                self.assertFalse(hasattr(value, forbidden), type(value).__name__)
            if isinstance(value, dict):
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
            else:
                self.assertIn(type(value).__name__,
                              {"str", "int", "float", "bool", "NoneType", "dict", "list"})

        check(payload)

    def test_exhausted_context_payload_keeps_the_none_slots(self) -> None:
        for _ in range(3):
            self.service.record_learning(self.session_id)

        payload = self.build_context().as_dict()

        self.assertIsNone(payload["current_memory"])
        self.assertIsNone(payload["current_state"])
        self.assertEqual(payload["overview"]["stats"]["total_memories"], 3)

    def test_source_mismatch_is_refused(self) -> None:
        other = self.make_source(title="另一份材料")
        context = self.build_context()
        with self.assertRaises(ValidationError) as ctx:
            TeacherContext(session=context.session, source=other, overview=context.overview)
        self.assertIn("source", ctx.exception.fields)

    def test_overview_of_another_source_is_refused(self) -> None:
        other = self.make_source(title="另一份材料")
        stranger = self.make_memory(title="别人家的记忆")
        self.repo.link(stranger.id, other.id)
        context = self.build_context()

        with self.assertRaises(ValidationError) as ctx:
            TeacherContext(session=context.session, source=context.source,
                           overview=self.service.get_learning_overview(other.id))
        self.assertIn("overview", ctx.exception.fields)

    def test_state_of_another_memory_is_refused(self) -> None:
        context = self.build_context()
        other_state = self.learning.get_state(self.b.id)

        with self.assertRaises(ValidationError) as ctx:
            TeacherContext(session=context.session, source=context.source,
                           overview=context.overview, current_memory=context.current_memory,
                           current_state=other_state)
        self.assertIn("current_state", ctx.exception.fields)

    def test_current_memory_without_a_session_pointer_is_refused(self) -> None:
        context = self.build_context()
        with self.assertRaises(ValidationError) as ctx:
            TeacherContext(session=context.session, source=context.source,
                           overview=context.overview, current_memory=self.b)
        self.assertIn("current_memory", ctx.exception.fields)

    def test_from_learning_requires_both_projections(self) -> None:
        context = self.build_context()
        with self.assertRaises(ValidationError):
            TeacherContext.from_learning(None, context.overview)
        with self.assertRaises(ValidationError):
            TeacherContext.from_learning(context, None)


class TeacherActionTest(TeacherTestCase):
    def test_the_four_allowed_action_types_exist(self) -> None:
        self.assertEqual(ALLOWED_ACTION_TYPES,
                         ("record_learning", "record_assessment", "finish_session", "abandon_session"))
        for cls in (RecordLearningAction, RecordAssessmentAction, FinishSessionAction,
                    AbandonSessionAction):
            self.assertIn(cls.kind, ALLOWED_ACTION_TYPES)

    def test_every_action_is_a_frozen_teacher_action(self) -> None:
        actions = [
            RecordLearningAction(),
            RecordAssessmentAction(understanding_level="partial"),
            FinishSessionAction(session_id=self.session_id),
            AbandonSessionAction(session_id=self.session_id),
        ]
        for action in actions:
            self.assertIsInstance(action, TeacherAction)
            self.assertTrue(dataclasses.is_dataclass(action))
            with self.assertRaises(dataclasses.FrozenInstanceError):
                action.kind = "nope"                                    # type: ignore[misc]

    def test_the_base_class_is_abstract(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            TeacherAction()
        self.assertIn("abstract", str(ctx.exception))

    def test_actions_are_json_serialisable(self) -> None:
        for action in (
            RecordLearningAction(memory_id=self.a.id),
            RecordAssessmentAction(understanding_level="partial", known_aspects=["极限是趋近"]),
            FinishSessionAction(session_id=self.session_id),
            AbandonSessionAction(session_id=self.session_id),
        ):
            text = json.dumps(action.as_dict(), ensure_ascii=False)
            self.assertIn(action.kind, text)

    def test_parse_round_trips_every_action(self) -> None:
        for action in (
            RecordLearningAction(),
            RecordLearningAction(memory_id=self.a.id),
            RecordAssessmentAction(understanding_level="solid", known_aspects=["a"], weak_aspects=[],
                                   misconceptions=None, memory_id=self.a.id),
            FinishSessionAction(session_id=self.session_id),
            AbandonSessionAction(session_id=self.session_id),
        ):
            self.assertEqual(TeacherAction.parse(action.as_dict()), action)

    def test_unknown_action_type_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            TeacherAction.parse({"type": "teach_me", "memory_id": "mem_x"})
        self.assertIn("unknown teacher action", str(ctx.exception))
        self.assertIn("type", ctx.exception.fields)

    def test_every_forbidden_action_type_is_refused(self) -> None:
        self.assertEqual(len(FORBIDDEN_ACTION_TYPES), 10)
        for kind in FORBIDDEN_ACTION_TYPES:
            with self.assertRaises(ValidationError) as ctx:
                TeacherAction.parse({"type": kind, "anything": 1})
            self.assertIn("not part of the Teacher contract", str(ctx.exception))
            self.assertIn(kind, str(ctx.exception))

    def test_say_is_not_a_database_action(self) -> None:
        with self.assertRaises(ValidationError):
            TeacherAction.parse({"type": "say", "text": "你好"})
        response = TeacherTurnResponse(assistant_message="你好")
        self.assertEqual(response.actions, ())                          # the message is the message

    def test_unknown_fields_are_refused(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            TeacherAction.parse({"type": "record_learning", "nope": 1, "sql": "DROP TABLE"})
        self.assertEqual(set(ctx.exception.fields), {"nope", "sql"})

    def test_illegal_payload_shapes_are_refused(self) -> None:
        for payload in ("record_learning", 42, None, [], {"type": ""}, {"type": "   "},
                        {"type": 42}, {"memory_id": self.a.id}):
            with self.assertRaises(ValidationError):
                TeacherAction.parse(payload)                            # type: ignore[arg-type]

    def test_missing_required_fields_are_refused(self) -> None:
        with self.assertRaises(TypeError):
            FinishSessionAction()                                       # Python signature
        with self.assertRaises(ValidationError):
            TeacherAction.parse({"type": "finish_session"})              # parsed payload
        with self.assertRaises(ValidationError):
            TeacherAction.parse({"type": "record_assessment"})

    def test_record_learning_memory_id_is_optional_and_must_be_an_id(self) -> None:
        self.assertIsNone(RecordLearningAction().memory_id)
        self.assertEqual(RecordLearningAction(memory_id=self.a.id).memory_id, self.a.id)
        for bad in (42, "", "  ", "mem with space", "x" * 200):
            with self.assertRaises(ValidationError) as ctx:
                RecordLearningAction(memory_id=bad)
            self.assertEqual(ctx.exception.fields, ("memory_id",))

    def test_session_actions_need_a_session_id(self) -> None:
        for cls in (FinishSessionAction, AbandonSessionAction):
            self.assertEqual(cls(session_id=self.session_id).session_id, self.session_id)
            for bad in ("", "   ", 42, None):
                with self.assertRaises(ValidationError) as ctx:
                    cls(session_id=bad)
                self.assertEqual(ctx.exception.fields, ("session_id",))


class AssessmentActionTest(TeacherTestCase):
    def test_legal_levels_are_accepted_and_reuse_the_existing_enum(self) -> None:
        for level in UnderstandingLevel:
            action = RecordAssessmentAction(understanding_level=level)
            self.assertIsInstance(action.understanding_level, UnderstandingLevel)
            self.assertEqual(str(action.understanding_level), str(level))
        self.assertEqual([m.value for m in UnderstandingLevel],
                         ["unknown", "fuzzy", "partial", "solid"])       # no new level was added

    def test_strings_are_normalised_like_the_rest_of_the_project(self) -> None:
        action = RecordAssessmentAction(understanding_level="  SOLID ")
        self.assertEqual(str(action.understanding_level), "solid")
        self.assertEqual(action.as_dict()["understanding_level"], "solid")

    def test_illegal_levels_are_refused(self) -> None:
        for illegal in ("mastered", "learning", "familiar", "expert", "", 42, None, True, 0.8):
            with self.assertRaises(ValidationError) as ctx:
                RecordAssessmentAction(understanding_level=illegal)
            self.assertIn("understanding_level", ctx.exception.fields)

    def test_none_means_leave_alone_and_empty_list_means_clear(self) -> None:
        untouched = RecordAssessmentAction(understanding_level="solid")
        cleared = RecordAssessmentAction(understanding_level="solid", known_aspects=[],
                                         weak_aspects=[], misconceptions=[])

        self.assertIsNone(untouched.known_aspects)
        self.assertIsNone(untouched.weak_aspects)
        self.assertIsNone(untouched.misconceptions)
        self.assertIsNone(untouched.as_dict()["known_aspects"])
        self.assertEqual(cleared.known_aspects, [])
        self.assertEqual(cleared.as_dict()["known_aspects"], [])
        self.assertEqual(cleared.as_dict()["weak_aspects"], [])

    def test_aspects_follow_the_current_model_rules(self) -> None:
        legal = RecordAssessmentAction(
            understanding_level="partial",
            known_aspects=["  极限   是趋近  ", "极限 是趋近", "ε-δ"],
            weak_aspects=[f"w{i}" for i in range(MAX_ASPECTS)],
            misconceptions=["x" * MAX_ASPECT_LENGTH],
        )
        self.assertEqual(legal.known_aspects, ["极限 是趋近", "ε-δ"])   # trimmed + de-duplicated
        self.assertEqual(len(legal.weak_aspects), MAX_ASPECTS)

        for payload in ({"known_aspects": "极限"}, {"known_aspects": [42]}, {"known_aspects": ["  "]},
                        {"weak_aspects": [""]},
                        {"misconceptions": ["x" * (MAX_ASPECT_LENGTH + 1)]},
                        {"known_aspects": [f"a{i}" for i in range(MAX_ASPECTS + 1)]}):
            with self.assertRaises(ValidationError) as ctx:
                RecordAssessmentAction(understanding_level="solid", **payload)
            self.assertTrue(set(payload) <= set(ctx.exception.fields), payload)

    def test_memory_id_is_optional_and_validated(self) -> None:
        self.assertIsNone(RecordAssessmentAction(understanding_level="solid").memory_id)
        self.assertEqual(
            RecordAssessmentAction(understanding_level="solid", memory_id=self.a.id).memory_id,
            self.a.id,
        )
        with self.assertRaises(ValidationError) as ctx:
            RecordAssessmentAction(understanding_level="solid", memory_id=7)
        self.assertEqual(ctx.exception.fields, ("memory_id",))

    def test_parsed_assessment_keeps_the_field_semantics(self) -> None:
        action = TeacherAction.parse({
            "type": "record_assessment", "understanding_level": "partial",
            "known_aspects": ["极限是趋近"], "weak_aspects": [], "misconceptions": None,
            "memory_id": self.a.id,
        })
        payload = action.as_dict()

        self.assertEqual(payload["understanding_level"], "partial")
        self.assertEqual(payload["known_aspects"], ["极限是趋近"])
        self.assertEqual(payload["weak_aspects"], [])
        self.assertIsNone(payload["misconceptions"])


class TeacherTurnRequestTest(TeacherTestCase):
    def request(self, **overrides) -> TeacherTurnRequest:
        kwargs = {"session_id": self.session_id, "user_message": "什么是极限？",
                  "context": self.build_context()}
        kwargs.update(overrides)
        return TeacherTurnRequest(**kwargs)

    def test_a_legal_request(self) -> None:
        request = self.request()
        self.assertEqual(request.session_id, self.session_id)
        self.assertEqual(request.user_message, "什么是极限？")
        self.assertEqual(request.context.session_id, self.session_id)

    def test_context_must_describe_the_same_session(self) -> None:
        context = self.build_context()
        with self.assertRaises(ValidationError) as ctx:
            TeacherTurnRequest(session_id="lrn_other", user_message="x", context=context)
        self.assertIn("context", ctx.exception.fields)
        self.assertIn(self.session_id, str(ctx.exception))

    def test_session_id_must_be_a_valid_id(self) -> None:
        for bad in ("", "   ", "lrn with space", 42, None):
            with self.assertRaises(ValidationError) as ctx:
                self.request(session_id=bad)
            self.assertIn("session_id", ctx.exception.fields)

    def test_user_message_must_be_a_string(self) -> None:
        for bad in (42, None, ["x"], {"text": "x"}):
            with self.assertRaises(ValidationError) as ctx:
                self.request(user_message=bad)
            self.assertEqual(ctx.exception.fields, ("user_message",))
        self.assertEqual(self.request(user_message="").user_message, "")   # an empty turn is legal

    def test_context_must_be_a_teacher_context(self) -> None:
        for bad in (None, {"session_id": self.session_id}, "context"):
            with self.assertRaises(ValidationError) as ctx:
                self.request(context=bad)
            self.assertIn("context", ctx.exception.fields)

    def test_request_is_frozen_and_json_serialisable(self) -> None:
        request = self.request()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.user_message = "x"                                 # type: ignore[misc]
        payload = request.as_dict()
        self.assertEqual(sorted(payload), ["context", "session_id", "user_message"])
        self.assertTrue(json.dumps(payload, ensure_ascii=False))


class TeacherTurnResponseTest(TeacherTestCase):
    def test_message_and_actions(self) -> None:
        response = TeacherTurnResponse(
            assistant_message="我们来看极限。",
            actions=(RecordLearningAction(), RecordAssessmentAction(understanding_level="partial")),
        )
        self.assertEqual(response.assistant_message, "我们来看极限。")
        self.assertEqual(response.action_types, ("record_learning", "record_assessment"))

    def test_action_order_is_preserved(self) -> None:
        actions = (
            RecordAssessmentAction(understanding_level="fuzzy"),
            RecordLearningAction(),
            FinishSessionAction(session_id=self.session_id),
        )
        response = TeacherTurnResponse(assistant_message="好的", actions=actions)

        self.assertEqual([a.kind for a in response.actions],
                         ["record_assessment", "record_learning", "finish_session"])
        self.assertEqual([a["type"] for a in response.as_dict()["actions"]],
                         ["record_assessment", "record_learning", "finish_session"])

    def test_default_response_has_no_actions(self) -> None:
        response = TeacherTurnResponse(assistant_message="只是聊聊天")
        self.assertEqual(response.actions, ())
        self.assertEqual(response.as_dict()["actions"], [])

    def test_response_is_frozen_and_json_serialisable(self) -> None:
        response = TeacherTurnResponse(assistant_message="x", actions=(RecordLearningAction(),))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            response.assistant_message = "y"                           # type: ignore[misc]
        text = json.dumps(response.as_dict(), ensure_ascii=False)
        self.assertIn("record_learning", text)

    def test_actions_must_be_teacher_actions(self) -> None:
        for bad in ({"type": "record_learning"}, "record_learning", 42, [{"type": "x"}]):
            with self.assertRaises(ValidationError) as ctx:
                TeacherTurnResponse(assistant_message="x", actions=bad)   # type: ignore[arg-type]
            self.assertIn("actions", ctx.exception.fields)
        with self.assertRaises(ValidationError):
            TeacherTurnResponse(assistant_message=42)                  # type: ignore[arg-type]

    def test_parse_accepts_a_model_payload(self) -> None:
        response = TeacherTurnResponse.parse({
            "assistant_message": "我们来看极限。",
            "actions": [{"type": "record_learning"},
                        {"type": "record_assessment", "understanding_level": "partial",
                         "known_aspects": ["极限是趋近"]}],
        })
        self.assertEqual(response.action_types, ("record_learning", "record_assessment"))
        self.assertEqual(response.actions[1].known_aspects, ["极限是趋近"])

    def test_parse_is_strict_about_the_response_and_its_actions(self) -> None:
        with self.assertRaises(ValidationError):
            TeacherTurnResponse.parse({"assistant_message": "x", "extra": 1})
        with self.assertRaises(ValidationError):
            TeacherTurnResponse.parse({"assistant_message": "x", "actions": "record_learning"})
        with self.assertRaises(ValidationError):
            TeacherTurnResponse.parse({"assistant_message": "x",
                                       "actions": [{"type": "delete_memory"}]})
        with self.assertRaises(ValidationError):
            TeacherTurnResponse.parse("not a mapping")                 # type: ignore[arg-type]
        self.assertEqual(TeacherTurnResponse.parse({"assistant_message": "x"}).actions, ())

    def test_response_leaks_no_internals(self) -> None:
        response = TeacherTurnResponse(assistant_message="x",
                                       actions=(RecordLearningAction(),))
        payload = response.as_dict()
        self.assertEqual(sorted(payload), ["actions", "assistant_message"])
        for forbidden in ("database", "repository", "transaction", "connection", "rows"):
            self.assertNotIn(forbidden, payload)


class PermissionBoundaryTest(TeacherTestCase):
    def test_building_and_parsing_actions_writes_nothing(self) -> None:
        self.service.record_learning(self.session_id)                   # some real data first
        before = self.db_snapshot()

        actions = [
            RecordLearningAction(),
            RecordLearningAction(memory_id=self.b.id),
            RecordAssessmentAction(understanding_level="solid", known_aspects=["x"],
                                   weak_aspects=[], misconceptions=["y"]),
            FinishSessionAction(session_id=self.session_id),
            AbandonSessionAction(session_id=self.session_id),
        ]
        for action in actions:
            action.as_dict()
            TeacherAction.parse(action.as_dict())
        TeacherTurnResponse(assistant_message="好的", actions=tuple(actions)).as_dict()
        response_payload = {"assistant_message": "好的", "actions": [a.as_dict() for a in actions]}
        TeacherTurnResponse.parse(response_payload)

        self.assertEqual(self.db_snapshot(), before)
        self.assertEqual(str(self.learning.get_session(self.session_id).status), "active")
        self.assertEqual(self.learning.get_state(self.b.id).learn_count, 0)

    def test_building_a_teacher_context_writes_nothing(self) -> None:
        before = self.db_snapshot()

        self.build_context()

        self.assertEqual(self.db_snapshot(), before)

    def test_the_contract_never_executes_the_engine(self) -> None:
        """AST guard: no call may invoke a LearningService write operation."""
        tree = ast.parse(TEACHER_SOURCE.read_text(encoding="utf-8"))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    called.add(node.func.id)
        for forbidden in ("record_learning", "record_assessment", "finish_session",
                          "abandon_session", "transaction", "create_state", "update_state",
                          "create_session", "update_session", "start_session", "execute", "execute_sql"):
            self.assertNotIn(forbidden, called, f"teacher.py calls {forbidden}")

    def test_the_contract_contains_no_sql_or_database_access(self) -> None:
        source = TEACHER_SOURCE.read_text(encoding="utf-8")
        for marker in ("sqlite3", "SELECT ", "INSERT ", "UPDATE ", "DELETE ", "executemany"):
            self.assertNotIn(marker, source, f"teacher.py mentions {marker!r}")


class TeacherArchitectureTest(unittest.TestCase):
    def test_runtime_imports_are_the_expected_minimal_set(self) -> None:
        tree = ast.parse(TEACHER_SOURCE.read_text(encoding="utf-8"))
        type_checking_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
                for child in ast.walk(node):
                    if hasattr(child, "lineno"):
                        type_checking_lines.add(child.lineno)

        runtime: set[str] = set()
        deferred: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            else:
                continue
            (deferred if node.lineno in type_checking_lines else runtime).update(names)

        self.assertEqual(runtime, {"__future__", "dataclasses", "typing", "errors",
                                   "learning_models", "models"})
        self.assertIn("learning", deferred)                              # annotations only
        for forbidden in ("urllib", "http", "requests", "socket", "sqlite3", "subprocess",
                          "llm", "prompt", "openai", "deepseek"):
            self.assertNotIn(forbidden, runtime | deferred, f"teacher.py imports {forbidden}")

    def test_the_contract_has_no_network_or_llm_identifiers(self) -> None:
        tree = ast.parse(TEACHER_SOURCE.read_text(encoding="utf-8"))
        identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for token in ("llm", "prompt", "openai", "deepseek", "embedding", "http", "urllib"):
            self.assertFalse([name for name in identifiers if token in name.lower()],
                             f"teacher.py references {token!r} in code")


class TeacherCompatibilityTest(TeacherTestCase):
    def test_using_the_contract_does_not_touch_1_0_data(self) -> None:
        self.service.record_learning(self.session_id)
        counts_before = self.repo.counts()
        memory_before = self.repo.get_memory(self.a.id).as_dict()
        source_before = self.repo.get_source(self.source.id).as_dict()
        search_before = [(hit.memory.id, hit.score)
                         for hit in MemoryRetriever(self.repo).search("极限").hits]

        context = self.build_context()
        TeacherTurnResponse.parse({
            "assistant_message": "继续",
            "actions": [{"type": "record_learning"}, {"type": "record_assessment",
                                                      "understanding_level": "solid"}],
        }).as_dict()
        TeacherTurnRequest(session_id=self.session_id, user_message="继续", context=context).as_dict()

        self.assertEqual(self.repo.counts(), counts_before)
        self.assertEqual(self.repo.get_memory(self.a.id).as_dict(), memory_before)
        self.assertEqual(self.repo.get_source(self.source.id).as_dict(), source_before)
        self.assertEqual([(hit.memory.id, hit.score)
                          for hit in MemoryRetriever(self.repo).search("极限").hits], search_before)

    def test_learning_semantics_are_untouched_by_the_contract(self) -> None:
        """The contract adds nothing to the engine's behaviour."""
        before = self.learning.counts()
        self.build_context()
        self.assertEqual(self.learning.counts(), before)

        self.service.record_learning(self.session_id)
        self.assertEqual(self.learning.get_state(self.a.id).learn_count, 1)
        self.assertEqual(str(self.learning.get_state(self.a.id).understanding_level), "unknown")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
