"""Phase 2C-2: TeacherActionExecutor tests.

What is locked:

* each of the four actions maps onto exactly one ``LearningService`` call, and every
  domain rule stays in the service (proved both by behaviour and by a recording spy)
* an action can never act on a session other than the one being executed
* unknown/foreign actions are refused before the engine is touched
* service exceptions (``ConflictError`` / ``NotFoundError`` / ``ValidationError``)
  travel up unchanged
* the module contains no database access, no business rules, no LLM/HTTP/UI
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import unittest

from personal_memory import LearningRepository, LearningService, UnderstandingLevel
from personal_memory.errors import ConflictError, NotFoundError, ValidationError
from personal_memory.learning import PUBLIC_API as LEARNING_PUBLIC_API
from personal_memory.retrieval import MemoryRetriever
from personal_memory.teacher import (
    ALLOWED_ACTION_TYPES,
    AbandonSessionAction,
    FinishSessionAction,
    RecordAssessmentAction,
    RecordLearningAction,
    TeacherAction,
)
from personal_memory.teacher_executor import SUPPORTED_ACTION_TYPES, TeacherActionExecutor

from .helpers import RepositoryTestCase

EXECUTOR_SOURCE = pathlib.Path("personal_memory/teacher_executor.py")


class RecordingLearningService:
    """Delegates everything to the real service and records what it was asked to do.

    The executor only needs the four write methods (duck typing), so this proves the
    executor *maps* rather than *does*: every effect still comes from the real service.
    """

    def __init__(self, real: LearningService) -> None:
        self.real = real
        self.calls: list[tuple[str, str, dict]] = []

    def record_learning(self, session_id, *, memory_id=None):
        self.calls.append(("record_learning", session_id, {"memory_id": memory_id}))
        return self.real.record_learning(session_id, memory_id=memory_id)

    def record_assessment(self, session_id, *, understanding_level, known_aspects=None,
                          weak_aspects=None, misconceptions=None, memory_id=None):
        self.calls.append(("record_assessment", session_id, {
            "understanding_level": understanding_level, "known_aspects": known_aspects,
            "weak_aspects": weak_aspects, "misconceptions": misconceptions, "memory_id": memory_id,
        }))
        return self.real.record_assessment(
            session_id, understanding_level=understanding_level, known_aspects=known_aspects,
            weak_aspects=weak_aspects, misconceptions=misconceptions, memory_id=memory_id,
        )

    def finish_session(self, session_id):
        self.calls.append(("finish_session", session_id, {}))
        return self.real.finish_session(session_id)

    def abandon_session(self, session_id):
        self.calls.append(("abandon_session", session_id, {}))
        return self.real.abandon_session(session_id)


class RecordingOnlyService:
    """Fails loudly if anything is executed (used for "refused before the engine")."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def __getattr__(self, name):  # pragma: no cover - any call is a bug in these tests
        def boom(*args, **kwargs):
            self.calls.append((name, args[0] if args else "", kwargs))
            raise AssertionError(f"the executor called {name} when it should not have")

        return boom


class ExecutorTestCase(RepositoryTestCase):
    """A Source with three Memories and a started session."""

    prefix = "pms-exec-"

    def setUp(self) -> None:
        super().setUp()
        self.learning = LearningRepository(self.database)
        self.service = LearningService(self.learning, self.repo)
        self.executor = TeacherActionExecutor(self.service)
        self.source = self.make_source(title="高等数学第一章")
        self.a = self.make_memory(title="极限")
        self.b = self.make_memory(title="连续")
        self.c = self.make_memory(title="切线斜率")
        for memory in (self.a, self.b, self.c):
            self.repo.link(memory.id, self.source.id)
        self.session_id = self.service.start_session(source_id=self.source.id).session.id

    # -- helpers ----------------------------------------------------------
    def session(self, session_id=None):
        return self.learning.get_session(session_id or self.session_id)

    def state(self, memory):
        return self.learning.get_state(memory.id)

    def apply_action(self, action, *, session_id=None):
        """Never name this ``run``: unittest.TestCase.run would be shadowed."""
        return self.executor.execute(session_id=session_id or self.session_id, action=action)


class RecordLearningMappingTest(ExecutorTestCase):
    def test_records_one_event_and_advances_the_session(self) -> None:
        context = self.apply_action(RecordLearningAction())

        self.assertEqual(self.state(self.a).learn_count, 1)
        self.assertIsNotNone(self.state(self.a).last_learned_at)
        self.assertEqual(context.session.plan_cursor, 1)
        self.assertEqual(context.session.current_memory_id, self.b.id)
        self.assertEqual(self.session().plan_cursor, 1)

    def test_explicit_memory_id_of_the_current_memory_is_accepted(self) -> None:
        self.apply_action(RecordLearningAction(memory_id=self.a.id))

        self.assertEqual(self.state(self.a).learn_count, 1)
        self.assertEqual(self.session().current_memory_id, self.b.id)

    def test_a_different_memory_id_is_refused_by_the_service(self) -> None:
        with self.assertRaises(ConflictError):
            self.apply_action(RecordLearningAction(memory_id=self.b.id))

        self.assertEqual(self.state(self.b).learn_count, 0)
        self.assertEqual(self.session().plan_cursor, 0)

    def test_a_missing_state_is_created_by_the_service(self) -> None:
        with self.database.transaction() as conn:
            conn.execute("DELETE FROM learning_states WHERE memory_id = ?", (self.a.id,))

        context = self.apply_action(RecordLearningAction())

        self.assertEqual(self.state(self.a).learn_count, 1)
        self.assertEqual(str(self.state(self.a).understanding_level), "unknown")
        self.assertEqual(context.state_for(self.a.id).learn_count, 1)

    def test_returns_the_service_context(self) -> None:
        context = self.apply_action(RecordLearningAction())

        self.assertEqual(context.session.id, self.session_id)
        self.assertEqual(context.source.id, self.source.id)
        self.assertEqual([memory.id for memory in context.memories], [self.a.id, self.b.id, self.c.id])

    def test_three_memories_are_walked_one_action_at_a_time(self) -> None:
        for _ in range(3):
            self.apply_action(RecordLearningAction())

        self.assertEqual([self.state(m).learn_count for m in (self.a, self.b, self.c)], [1, 1, 1])
        session = self.session()
        self.assertEqual(session.plan_cursor, 3)
        self.assertIsNone(session.current_memory_id)
        self.assertEqual(str(session.current_stage), "done")


class RecordAssessmentMappingTest(ExecutorTestCase):
    def test_the_four_assessment_fields_are_persisted(self) -> None:
        self.apply_action(RecordAssessmentAction(
            understanding_level="partial",
            known_aspects=["极限是趋近"], weak_aspects=["ε-δ 定义"], misconceptions=["极限等于函数值"],
        ))

        state = self.state(self.a)
        self.assertEqual(str(state.understanding_level), "partial")
        self.assertEqual(state.known_aspects, ["极限是趋近"])
        self.assertEqual(state.weak_aspects, ["ε-δ 定义"])
        self.assertEqual(state.misconceptions, ["极限等于函数值"])

    def test_none_keeps_and_empty_list_clears(self) -> None:
        self.apply_action(RecordAssessmentAction(understanding_level="fuzzy", known_aspects=["旧的"],
                                        weak_aspects=["弱的"]))
        self.apply_action(RecordAssessmentAction(understanding_level="partial"))                 # None
        state = self.state(self.a)
        self.assertEqual(state.known_aspects, ["旧的"])
        self.assertEqual(state.weak_aspects, ["弱的"])
        self.apply_action(RecordAssessmentAction(understanding_level="partial", known_aspects=[]))  # []
        state = self.state(self.a)
        self.assertEqual(state.known_aspects, [])
        self.assertEqual(state.weak_aspects, ["弱的"])

    def test_event_fields_and_progress_are_untouched(self) -> None:
        self.apply_action(RecordAssessmentAction(understanding_level="solid", known_aspects=["x"]))

        state = self.state(self.a)
        self.assertEqual(state.learn_count, 0)
        self.assertIsNone(state.last_learned_at)
        session = self.session()
        self.assertEqual(session.plan_cursor, 0)
        self.assertEqual(session.current_memory_id, self.a.id)
        self.assertEqual(str(session.current_stage), "explain")

    def test_a_learning_event_then_an_assessment_of_the_next_memory(self) -> None:
        self.apply_action(RecordLearningAction())                                    # A
        self.apply_action(RecordAssessmentAction(understanding_level="fuzzy"))       # B

        self.assertEqual(self.state(self.a).learn_count, 1)
        self.assertEqual(str(self.state(self.b).understanding_level), "fuzzy")
        self.assertEqual(self.state(self.b).learn_count, 0)
        self.assertEqual(self.session().plan_cursor, 1)

    def test_explicit_memory_id_of_the_current_memory(self) -> None:
        self.apply_action(RecordAssessmentAction(understanding_level="solid", memory_id=self.a.id))
        self.assertEqual(str(self.state(self.a).understanding_level), "solid")

    def test_a_different_memory_id_is_refused_by_the_service(self) -> None:
        with self.assertRaises(ConflictError):
            self.apply_action(RecordAssessmentAction(understanding_level="solid", memory_id=self.b.id))
        self.assertEqual(str(self.state(self.b).understanding_level), "unknown")


class SessionActionMappingTest(ExecutorTestCase):
    def test_finish_completes_the_session(self) -> None:
        context = self.apply_action(FinishSessionAction(session_id=self.session_id))

        self.assertEqual(str(context.session.status), "completed")
        self.assertEqual(str(self.session().status), "completed")
        self.assertIsNotNone(self.session().ended_at)

    def test_finish_frees_the_source_for_a_new_session(self) -> None:
        self.apply_action(FinishSessionAction(session_id=self.session_id))

        second = self.service.start_session(source_id=self.source.id)
        self.assertEqual(str(second.session.status), "active")
        self.assertEqual(self.learning.counts()["learning_sessions"], 2)

    def test_abandon_marks_the_session_abandoned(self) -> None:
        context = self.apply_action(AbandonSessionAction(session_id=self.session_id))

        self.assertEqual(str(context.session.status), "abandoned")
        self.assertEqual(str(self.session().status), "abandoned")
        self.assertIsNone(self.service.get_active_session(self.source.id))

    def test_session_actions_do_not_touch_learning_state(self) -> None:
        before = [self.state(m).as_dict() for m in (self.a, self.b, self.c)]

        self.apply_action(FinishSessionAction(session_id=self.session_id))

        self.assertEqual([self.state(m).as_dict() for m in (self.a, self.b, self.c)], before)

    def test_session_actions_only_close_the_named_session(self) -> None:
        other_source = self.make_source(title="另一份材料")
        stranger = self.make_memory(title="别人家的记忆")
        self.repo.link(stranger.id, other_source.id)
        other_session = self.service.start_session(source_id=other_source.id).session.id

        self.apply_action(FinishSessionAction(session_id=self.session_id))

        self.assertEqual(str(self.session().status), "completed")
        self.assertEqual(str(self.session(other_session).status), "active")     # untouched

    def test_a_padded_action_session_id_matches_after_normalisation(self) -> None:
        self.apply_action(FinishSessionAction(session_id=f"  {self.session_id}  "))
        self.assertEqual(str(self.session().status), "completed")


class SessionIdSafetyTest(ExecutorTestCase):
    def make_other_session(self) -> str:
        other_source = self.make_source(title="另一份材料")
        stranger = self.make_memory(title="别人家的记忆")
        self.repo.link(stranger.id, other_source.id)
        return self.service.start_session(source_id=other_source.id).session.id

    def test_finish_of_another_session_is_refused(self) -> None:
        other = self.make_other_session()

        with self.assertRaises(ValidationError) as ctx:
            self.apply_action(FinishSessionAction(session_id=other))
        self.assertEqual(ctx.exception.fields, ("session_id",))
        self.assertEqual(str(self.session().status), "active")                 # A untouched
        self.assertEqual(str(self.session(other).status), "active")            # B untouched

    def test_abandon_of_another_session_is_refused(self) -> None:
        other = self.make_other_session()

        with self.assertRaises(ValidationError) as ctx:
            self.apply_action(AbandonSessionAction(session_id=other))
        self.assertEqual(ctx.exception.fields, ("session_id",))
        self.assertEqual(str(self.session().status), "active")
        self.assertEqual(str(self.session(other).status), "active")

    def test_the_mismatch_is_refused_before_touching_the_service(self) -> None:
        other = self.make_other_session()
        spy = RecordingOnlyService()

        with self.assertRaises(ValidationError):
            TeacherActionExecutor(spy).execute(session_id=self.session_id,
                                               action=FinishSessionAction(session_id=other))
        self.assertEqual(spy.calls, [])

    def test_an_invalid_executor_session_id_is_refused(self) -> None:
        spy = RecordingOnlyService()
        for bad in ("", "   ", None, 42, ["lrn_x"]):
            with self.assertRaises(ValidationError) as ctx:
                TeacherActionExecutor(spy).execute(session_id=bad, action=RecordLearningAction())
            self.assertEqual(ctx.exception.fields, ("session_id",))
        self.assertEqual(spy.calls, [])

    def test_learning_actions_carry_no_session_id_to_forge(self) -> None:
        fields = {f.name for f in dataclasses.fields(RecordLearningAction)}
        self.assertNotIn("session_id", fields)
        self.assertNotIn("session_id", {f.name for f in dataclasses.fields(RecordAssessmentAction)})


class ActionTypeSafetyTest(ExecutorTestCase):
    def test_a_foreign_action_subclass_is_refused(self) -> None:
        class FakeAction(TeacherAction):                                    # not in the contract
            kind = "delete_memory"

        spy = RecordingOnlyService()
        with self.assertRaises(ValidationError) as ctx:
            TeacherActionExecutor(spy).execute(session_id=self.session_id, action=FakeAction())
        self.assertEqual(ctx.exception.fields, ("action",))
        self.assertIn("unsupported teacher action", str(ctx.exception))
        self.assertEqual(spy.calls, [])

    def test_an_action_with_an_empty_kind_is_refused(self) -> None:
        class EmptyKindAction(TeacherAction):
            kind = ""

        with self.assertRaises(ValidationError) as ctx:
            self.apply_action(EmptyKindAction())
        self.assertEqual(ctx.exception.fields, ("action",))

    def test_non_action_payloads_are_refused(self) -> None:
        spy = RecordingOnlyService()
        executor = TeacherActionExecutor(spy)
        for bad in ({"type": "record_learning"}, "record_learning", 42, None, ["record_learning"]):
            with self.assertRaises(ValidationError) as ctx:
                executor.execute(session_id=self.session_id, action=bad)   # type: ignore[arg-type]
            self.assertEqual(ctx.exception.fields, ("action",))
        self.assertEqual(spy.calls, [])

    def test_the_contract_still_refuses_forbidden_types_end_to_end(self) -> None:
        for kind in ("say", "delete_memory", "execute_sql", "modify_session_plan"):
            with self.assertRaises(ValidationError):
                TeacherAction.parse({"type": kind})

    def test_the_supported_set_is_the_contract_allowlist(self) -> None:
        self.assertEqual(SUPPORTED_ACTION_TYPES, ALLOWED_ACTION_TYPES)
        self.assertEqual(SUPPORTED_ACTION_TYPES,
                         ("record_learning", "record_assessment", "finish_session", "abandon_session"))

    def test_only_the_single_action_api_exists(self) -> None:
        for absent in ("execute_response", "execute_many", "batch_execute", "preview", "dry_run",
                       "approve", "validate"):
            self.assertFalse(hasattr(TeacherActionExecutor, absent), absent)
        public = {name for name in dir(TeacherActionExecutor)
                  if not name.startswith("_") and callable(getattr(TeacherActionExecutor, name))}
        self.assertEqual(public, {"execute"})

    def test_the_executor_needs_a_service(self) -> None:
        with self.assertRaises(ValidationError):
            TeacherActionExecutor(None)                                      # type: ignore[arg-type]


class DelegationProofTest(ExecutorTestCase):
    """The executor maps; the service decides. A recording spy shows exactly that."""

    def setUp(self) -> None:
        super().setUp()
        self.spy = RecordingLearningService(self.service)
        self.executor = TeacherActionExecutor(self.spy)

    def test_record_learning_is_forwarded_verbatim(self) -> None:
        self.executor.execute(session_id=self.session_id,
                              action=RecordLearningAction(memory_id=self.a.id))

        self.assertEqual(self.spy.calls, [("record_learning", self.session_id,
                                           {"memory_id": self.a.id})])
        self.assertEqual(self.state(self.a).learn_count, 1)

    def test_record_assessment_is_forwarded_verbatim(self) -> None:
        self.executor.execute(session_id=self.session_id, action=RecordAssessmentAction(
            understanding_level=UnderstandingLevel.PARTIAL, known_aspects=["a"], weak_aspects=[],
            misconceptions=None, memory_id=self.a.id))

        self.assertEqual(len(self.spy.calls), 1)
        name, session_id, kwargs = self.spy.calls[0]
        self.assertEqual((name, session_id), ("record_assessment", self.session_id))
        self.assertEqual(kwargs, {"understanding_level": UnderstandingLevel.PARTIAL,
                                  "known_aspects": ["a"], "weak_aspects": [], "misconceptions": None,
                                  "memory_id": self.a.id})

    def test_finish_is_forwarded_with_the_execution_session(self) -> None:
        self.executor.execute(session_id=self.session_id,
                              action=FinishSessionAction(session_id=self.session_id))
        self.assertEqual(self.spy.calls, [("finish_session", self.session_id, {})])

    def test_abandon_is_forwarded_with_the_execution_session(self) -> None:
        self.executor.execute(session_id=self.session_id,
                              action=AbandonSessionAction(session_id=self.session_id))
        self.assertEqual(self.spy.calls, [("abandon_session", self.session_id, {})])

    def test_the_result_is_the_service_result(self) -> None:
        """No new result model: the executor returns exactly what the service returned."""
        captured = {}

        class IdentityService(RecordingLearningService):
            def record_learning(self, session_id, *, memory_id=None):
                result = super().record_learning(session_id, memory_id=memory_id)
                captured["result"] = result
                return result

        executor = TeacherActionExecutor(IdentityService(self.service))
        returned = executor.execute(session_id=self.session_id, action=RecordLearningAction())

        self.assertIs(returned, captured["result"])

    def test_one_action_is_one_service_call(self) -> None:
        self.executor.execute(session_id=self.session_id, action=RecordLearningAction())
        self.assertEqual(len(self.spy.calls), 1)                            # no implicit extras
        self.assertEqual(self.state(self.b).learn_count, 0)                 # no implicit second event
        self.assertEqual(str(self.state(self.a).understanding_level), "unknown")   # no inference
        self.assertIsNotNone(self.state(self.a).last_learned_at)            # the event did land


class ExceptionPassThroughTest(ExecutorTestCase):
    def test_unknown_session_is_a_not_found_error(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.apply_action(RecordLearningAction(), session_id="lrn_missing")
        self.assertEqual(ctx.exception.entity, "learning session")

    def test_completed_session_is_a_conflict_error(self) -> None:
        self.service.finish_session(self.session_id)

        with self.assertRaises(ConflictError) as ctx:
            self.apply_action(RecordLearningAction())
        self.assertIn("only an active session", str(ctx.exception))

    def test_abandoned_session_is_a_conflict_error(self) -> None:
        self.service.abandon_session(self.session_id)

        with self.assertRaises(ConflictError):
            self.apply_action(RecordAssessmentAction(understanding_level="solid"))

    def test_exhausted_plan_is_a_conflict_error(self) -> None:
        for _ in range(3):
            self.apply_action(RecordLearningAction())

        with self.assertRaises(ConflictError) as ctx:
            self.apply_action(RecordLearningAction())
        self.assertIn("finished its plan", str(ctx.exception))

    def test_wrong_memory_is_a_conflict_error(self) -> None:
        with self.assertRaises(ConflictError) as ctx:
            self.apply_action(RecordLearningAction(memory_id=self.c.id))
        self.assertIn("not the Memory this session is on", str(ctx.exception))

    def test_the_error_is_not_wrapped(self) -> None:
        """Same type and same message as a direct service call -- nothing is swallowed."""
        self.service.finish_session(self.session_id)
        try:
            self.service.record_learning(self.session_id)
        except ConflictError as direct:
            direct_message = str(direct)
        try:
            self.apply_action(RecordLearningAction())
        except ConflictError as through_executor:
            self.assertEqual(str(through_executor), direct_message)
        else:  # pragma: no cover - the call above must raise
            self.fail("the executor did not propagate the ConflictError")

    def test_nothing_changes_after_a_refused_action(self) -> None:
        other = self.make_source(title="另一份材料")
        stranger = self.make_memory(title="别人家的记忆")
        self.repo.link(stranger.id, other.id)
        other_session = self.service.start_session(source_id=other.id).session.id
        before = (self.learning.counts(), self.state(self.a).as_dict(), self.state(self.b).as_dict())

        for action in (FinishSessionAction(session_id=other_session),
                       AbandonSessionAction(session_id=other_session),
                       RecordLearningAction(memory_id=self.c.id)):
            with self.assertRaises((ValidationError, ConflictError)):
                self.apply_action(action)

        self.assertEqual((self.learning.counts(), self.state(self.a).as_dict(),
                          self.state(self.b).as_dict()), before)


class ExecutorArchitectureTest(ExecutorTestCase):
    def test_the_executor_makes_no_database_or_domain_calls(self) -> None:
        tree = ast.parse(EXECUTOR_SOURCE.read_text(encoding="utf-8"))
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
        for forbidden in ("update_state", "update_session", "create_state", "create_session",
                          "upsert_state", "transaction", "execute", "executemany", "commit",
                          "start_session", "get_state", "get_session", "get_context"):
            self.assertNotIn(forbidden, calls, f"teacher_executor.py calls {forbidden}")
        # the only things it calls on the service are the four mapped operations
        self.assertTrue({"record_learning", "record_assessment", "finish_session",
                         "abandon_session"} <= calls)
        self.assertTrue({"isinstance", "ValidationError"} <= calls)

    def test_the_executor_has_no_sql_database_or_store_reference(self) -> None:
        source = EXECUTOR_SOURCE.read_text(encoding="utf-8")
        for marker in ("import sqlite3", "sqlite3.", "SELECT ", "INSERT ", "UPDATE ", "DELETE ",
                       "executemany", "commit("):
            self.assertNotIn(marker, source, f"teacher_executor.py contains {marker!r}")

        # identifiers (not prose): no repository / database / transaction object is used
        tree = ast.parse(source)
        identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for forbidden in ("LearningRepository", "MemoryRepository", "Database", "transaction",
                          "Connection", "connect", "cursor", "execute_sql"):
            self.assertNotIn(forbidden, identifiers, f"teacher_executor.py uses {forbidden}")

    def test_runtime_imports_stay_minimal(self) -> None:
        tree = ast.parse(EXECUTOR_SOURCE.read_text(encoding="utf-8"))
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

        self.assertEqual(runtime, {"__future__", "typing", "errors", "teacher"})
        self.assertEqual(deferred, {"learning"})
        for forbidden in ("learning_store", "models", "db", "sqlite3", "urllib", "requests",
                          "llm", "prompt", "openai", "deepseek", "http"):
            self.assertNotIn(forbidden, runtime | deferred, f"teacher_executor.py imports {forbidden}")

    def test_dependency_direction_is_one_way(self) -> None:
        """The engine must not import (or use) the teacher layer -- checked on code, not prose."""
        for name in ("personal_memory/learning.py", "personal_memory/learning_store.py",
                     "personal_memory/teacher.py"):
            source = pathlib.Path(name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[-1] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[-1])
            self.assertNotIn("teacher_executor", imported, f"{name} imports the executor")
            if name.endswith("learning.py") or name.endswith("learning_store.py"):
                self.assertNotIn("teacher", imported, f"{name} imports the teacher contract")

        # the engine itself references no teacher type (teacher.py naturally does)
        for name in ("personal_memory/learning.py", "personal_memory/learning_store.py"):
            tree = ast.parse(pathlib.Path(name).read_text(encoding="utf-8"))
            identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            identifiers |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            self.assertFalse([n for n in identifiers if "Teacher" in n],
                             f"{name} references teacher types in code")

    def test_the_executor_holds_no_repository_or_database(self) -> None:
        executor = TeacherActionExecutor(self.service)
        for forbidden in ("repository", "database", "connection", "transaction", "session"):
            self.assertFalse(hasattr(executor, forbidden), forbidden)
        self.assertIs(executor.learning, self.service)

    def test_no_llm_or_http_identifiers(self) -> None:
        tree = ast.parse(EXECUTOR_SOURCE.read_text(encoding="utf-8"))
        identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for token in ("llm", "prompt", "openai", "deepseek", "embedding", "http", "urllib", "socket"):
            self.assertFalse([name for name in identifiers if token in name.lower()],
                             f"teacher_executor.py references {token!r} in code")


class ExecutorBehaviourEquivalenceTest(ExecutorTestCase):
    def test_executor_driven_learning_equals_a_direct_service_call(self) -> None:
        """Same action, same outcome: the executor adds no rule of its own."""
        self.apply_action(RecordLearningAction())
        via_executor = (self.state(self.a).learn_count, self.session().plan_cursor,
                        self.session().current_memory_id)

        # a second, identical learning run on the other two memories, direct service calls
        direct = self.service.record_learning(self.session_id)
        self.assertEqual(direct.state_for(self.b.id).learn_count, 1)
        self.assertEqual(self.session().plan_cursor, 2)
        self.assertEqual(via_executor, (1, 1, self.b.id))

    def test_the_executor_does_not_touch_1_0_data(self) -> None:
        counts_before = self.repo.counts()
        memory_before = self.repo.get_memory(self.a.id).as_dict()
        source_before = self.repo.get_source(self.source.id).as_dict()
        search_before = [(hit.memory.id, hit.score)
                         for hit in MemoryRetriever(self.repo).search("极限").hits]

        self.apply_action(RecordLearningAction())
        self.apply_action(RecordAssessmentAction(understanding_level="solid", known_aspects=["极限是趋近"]))

        self.assertEqual(self.repo.counts(), counts_before)
        self.assertEqual(self.repo.get_memory(self.a.id).as_dict(), memory_before)
        self.assertEqual(self.repo.get_source(self.source.id).as_dict(), source_before)
        self.assertEqual([(hit.memory.id, hit.score)
                          for hit in MemoryRetriever(self.repo).search("极限").hits], search_before)

    def test_no_new_tables_and_no_engine_api_change(self) -> None:
        with self.database.connection() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual({name for name in tables if name.startswith("learning")},
                         {"learning_states", "learning_sessions"})
        self.assertNotIn("execute_action", LEARNING_PUBLIC_API)              # engine is untouched
        self.assertEqual(LEARNING_PUBLIC_API[-1], "abandon_session")

    def test_a_full_turn_can_be_executed_action_by_action(self) -> None:
        """The intended (manual, non-LLM) flow: one action at a time, in order."""
        for action in (RecordLearningAction(),                                   # A
                       RecordAssessmentAction(understanding_level="partial"),     # B
                       RecordLearningAction(),                                   # B
                       FinishSessionAction(session_id=self.session_id)):
            self.apply_action(action)

        self.assertEqual([self.state(m).learn_count for m in (self.a, self.b, self.c)], [1, 1, 0])
        self.assertEqual(str(self.state(self.b).understanding_level), "partial")
        self.assertEqual(str(self.session().status), "completed")
        self.assertEqual(self.learning.counts()["learning_sessions"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
