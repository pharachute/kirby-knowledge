"""Phase 2C-4: TeacherRuntime (single-turn orchestration) tests.

What is locked:

* one ``turn()`` = read context -> one model call -> ordered execution -> re-read
  context, and nothing else (no loop, no second model call, no retry)
* the model sees the **pre-turn** snapshot, the caller gets the **post-turn** state
* ``response.actions`` order is preserved; a failing action stops the rest without
  undoing what already happened (no fake atomicity)
* every failure travels up unchanged: ``TeacherModelError`` / ``ValidationError`` /
  ``ConflictError`` / ``NotFoundError``; the executor is not called when the model
  fails or when the session does not exist
* the runtime is an orchestration layer only: no repository, no database, no SQL, no
  HTTP, no provider, and it never calls a ``LearningService`` write method directly
"""

from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import unittest

from personal_memory import (
    SUPPORTED_SCHEMA_VERSION,
    LearningRepository,
    LearningService,
    TeacherRuntime,
    TeacherTurnResult,
)
from personal_memory.errors import ConflictError, NotFoundError, ValidationError
from personal_memory.learning import PUBLIC_API as LEARNING_PUBLIC_API
from personal_memory.retrieval import MemoryRetriever
from personal_memory.teacher import ALLOWED_ACTION_TYPES
from personal_memory.teacher_executor import SUPPORTED_ACTION_TYPES
from personal_memory.teacher_llm import TEACHER_PROMPT_VERSION, TeacherModelError
from personal_memory.teacher_runtime import TeacherRuntime as RuntimeClass

from .helpers import RepositoryTestCase
from .llm_fakes import FakeTeacherModel

RUNTIME_SOURCE = pathlib.Path("personal_memory/teacher_runtime.py")
WRITE_METHODS = ("record_learning", "record_assessment", "finish_session", "abandon_session")
FORBIDDEN_SESSION_PROBE = "lrn_someone_elses_session"


def parsed_prompt_block(user_prompt: str, tag: str = "teacher_context"):
    """The JSON object embedded in one ``<tag ...>`` block of the prompt."""
    opening = user_prompt.index(f"<{tag}")
    start = user_prompt.index(">\n", opening) + 2
    end = user_prompt.index(f"\n</{tag}>", start)
    return json.loads(user_prompt[start:end].replace("\\u003c", "<").replace("\\u003e", ">"))


class RecordingLearningService:
    """Delegates every call to the real service and records the call order.

    The runtime only needs ``get_context`` / ``get_learning_overview``; the executor
    needs the four write methods.  Recording both proves *who* acted (the executor, on
    the runtime's behalf) and that no write happens when the turn fails earlier.
    """

    def __init__(self, real: LearningService) -> None:
        self.real = real
        self.calls: list[tuple[str, str]] = []

    # -- reads used by the runtime
    def get_context(self, session_id):
        self.calls.append(("get_context", session_id))
        return self.real.get_context(session_id)

    def get_learning_overview(self, source_id):
        self.calls.append(("get_learning_overview", source_id))
        return self.real.get_learning_overview(source_id)

    # -- writes used by the executor
    def record_learning(self, session_id, *, memory_id=None):
        self.calls.append(("record_learning", session_id))
        return self.real.record_learning(session_id, memory_id=memory_id)

    def record_assessment(self, session_id, *, understanding_level, known_aspects=None,
                          weak_aspects=None, misconceptions=None, memory_id=None):
        self.calls.append(("record_assessment", session_id))
        return self.real.record_assessment(
            session_id, understanding_level=understanding_level, known_aspects=known_aspects,
            weak_aspects=weak_aspects, misconceptions=misconceptions, memory_id=memory_id,
        )

    def finish_session(self, session_id):
        self.calls.append(("finish_session", session_id))
        return self.real.finish_session(session_id)

    def abandon_session(self, session_id):
        self.calls.append(("abandon_session", session_id))
        return self.real.abandon_session(session_id)

    # -- views
    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] in WRITE_METHODS]


class TeacherRuntimeTestCase(RepositoryTestCase):
    """Source A (3 Memories) + Source B (1 Memory), one running session each."""

    prefix = "pms-truntime-"

    def setUp(self) -> None:
        super().setUp()
        self.learning = LearningRepository(self.database)
        self.service = LearningService(self.learning, self.repo)
        self.source = self.make_source(title="高等数学第一章")
        self.a = self.make_memory(title="极限", content="极限描述的是趋近")
        self.b = self.make_memory(title="连续", content="连续要求左右极限相等")
        self.c = self.make_memory(title="切线斜率", content="切线斜率是导数的几何意义")
        for memory in (self.a, self.b, self.c):
            self.repo.link(memory.id, self.source.id)

        self.other_source = self.make_source(title="另一份材料")
        self.stranger = self.make_memory(title="别人家的记忆")
        self.repo.link(self.stranger.id, self.other_source.id)

        self.session_id = self.service.start_session(source_id=self.source.id).session.id
        self.other_session_id = self.service.start_session(
            source_id=self.other_source.id).session.id
        self.spy = None
        self.model = None
        self.runtime = None

    # -- helpers ----------------------------------------------------------
    def runtime_for(self, *responses, learning=None):
        """(model, runtime) with a fresh recording service -- what the model saw stays observable."""
        self.model = FakeTeacherModel(*responses)
        self.spy = RecordingLearningService(learning or self.service)
        self.runtime = TeacherRuntime(self.spy, self.model)
        return self.model, self.runtime

    def turn(self, *responses, session_id=None, user_message="我懂了极限", learning=None):
        model, runtime = self.runtime_for(*responses, learning=learning)
        result = runtime.turn(
            session_id=self.session_id if session_id is None else session_id,
            user_message=user_message,
        )
        return model, result

    def rejection(self, *responses, session_id=None, user_message="我懂了极限"):
        with self.assertRaises(Exception) as ctx:      # noqa: B017 - the type is asserted by the caller
            self.turn(*responses, session_id=session_id, user_message=user_message)
        return ctx.exception

    def response(self, *actions, message="继续吧") -> dict:
        return {"assistant_message": message, "actions": list(actions)}

    def session(self, session_id=None):
        return self.learning.get_session(self.session_id if session_id is None else session_id)

    def state(self, memory):
        return self.learning.get_state(memory.id)

    def prompt_context(self, index=0):
        """The teacher_context the model was shown on its ``index``-th call."""
        return parsed_prompt_block(self.model.calls[index]["user_prompt"])

    def snapshot(self) -> dict:
        with self.database.connection() as conn:
            fts = {
                name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
                for name in ("memory_fts_word", "memory_fts_trigram")
            }
        return {
            "memory_counts": self.repo.counts(),
            "learning_counts": self.learning.counts(),
            "fts": fts,
            "sessions": [self.session().as_dict(), self.session(self.other_session_id).as_dict()],
            "states": [self.state(memory).as_dict() for memory in (self.a, self.b, self.c, self.stranger)],
            "memory": self.repo.get_memory(self.a.id).as_dict(),
            "source": self.repo.get_source(self.source.id).as_dict(),
            "search": [(hit.memory.id, hit.score)
                       for hit in MemoryRetriever(self.repo).search("极限").hits],
        }


# ==========================================================================
# 1. the flow: empty / single / multi action
# ==========================================================================

class TurnFlowTest(TeacherRuntimeTestCase):
    def test_an_empty_action_turn_calls_the_model_once_and_executes_nothing(self) -> None:
        model, result = self.turn(self.response(message="那我们继续看连续。"))

        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.spy.writes, [])
        self.assertEqual(result.response.actions, ())
        self.assertEqual(result.response.assistant_message, "那我们继续看连续。")
        self.assertEqual(str(result.context.session.status), "active")

    def test_an_empty_action_turn_still_reads_the_context_before_and_after(self) -> None:
        """Uniform flow: the returned context is always read *after* the turn."""
        self.turn(self.response())
        self.assertEqual(self.spy.names, ["get_context", "get_learning_overview", "get_context"])
        self.assertEqual(self.spy.calls[0][1], self.session_id)
        self.assertEqual(self.spy.calls[1][1], self.source.id)

    def test_a_single_action_is_executed_through_the_executor(self) -> None:
        model, result = self.turn(self.response({"type": "record_learning"}))

        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.spy.names,
                         ["get_context", "get_learning_overview", "record_learning", "get_context"])
        self.assertEqual(self.state(self.a).learn_count, 1)
        self.assertIsNotNone(self.state(self.a).last_learned_at)

    def test_the_returned_context_is_the_post_turn_state_not_the_pre_turn_snapshot(self) -> None:
        before = self.service.get_context(self.session_id)
        _, result = self.turn(self.response({"type": "record_learning"}))

        self.assertEqual(before.session.plan_cursor, 0)                 # what the model saw
        self.assertEqual(before.session.current_memory_id, self.a.id)
        self.assertEqual(result.context.session.plan_cursor, 1)          # what the caller gets
        self.assertEqual(result.context.session.current_memory_id, self.b.id)
        self.assertEqual(result.context.state_for(self.a.id).learn_count, 1)
        self.assertNotEqual(result.context.session.as_dict(), before.session.as_dict())

    def test_a_multi_action_turn_keeps_the_model_order(self) -> None:
        _, result = self.turn(self.response(
            {"type": "record_learning"},                                   # A -> B
            {"type": "record_assessment", "understanding_level": "partial"},  # judge B
            {"type": "record_learning"},                                   # B -> C
        ))

        self.assertEqual(result.executed_actions,
                         result.response.actions)
        self.assertEqual(result.response.action_types,
                         ("record_learning", "record_assessment", "record_learning"))
        self.assertEqual(self.spy.names, ["get_context", "get_learning_overview", "record_learning",
                                          "record_assessment", "record_learning", "get_context"])
        self.assertEqual([self.state(m).learn_count for m in (self.a, self.b, self.c)], [1, 1, 0])
        self.assertEqual(str(self.state(self.b).understanding_level), "partial")
        self.assertEqual(result.context.session.plan_cursor, 2)
        self.assertEqual(result.context.session.current_memory_id, self.c.id)

    def test_a_finish_action_leaves_the_returned_session_completed(self) -> None:
        _, result = self.turn(self.response(
            {"type": "finish_session", "session_id": self.session_id}, message="今天到这里。"))

        self.assertEqual(str(result.context.session.status), "completed")
        self.assertIsNotNone(result.context.session.ended_at)
        self.assertEqual(self.spy.writes, [("finish_session", self.session_id)])

    def test_an_abandon_action_leaves_the_returned_session_abandoned(self) -> None:
        _, result = self.turn(self.response({"type": "abandon_session", "session_id": self.session_id}))
        self.assertEqual(str(result.context.session.status), "abandoned")

    def test_an_assessment_only_turn_updates_the_current_memory(self) -> None:
        _, result = self.turn(self.response(
            {"type": "record_assessment", "understanding_level": "solid",
             "known_aspects": ["极限是趋近"], "weak_aspects": [], "misconceptions": None}))

        self.assertEqual(str(result.context.state_for(self.a.id).understanding_level), "solid")
        self.assertEqual(list(result.context.state_for(self.a.id).known_aspects), ["极限是趋近"])
        self.assertEqual(list(result.context.state_for(self.a.id).weak_aspects), [])
        self.assertEqual(result.context.session.plan_cursor, 0)          # an assessment does not advance

    def test_an_explicit_memory_id_is_forwarded_to_the_service(self) -> None:
        _, result = self.turn(self.response({"type": "record_learning", "memory_id": self.a.id}))
        self.assertEqual(result.context.state_for(self.a.id).learn_count, 1)

    def test_a_wrong_explicit_memory_id_is_refused_by_the_service(self) -> None:
        error = self.rejection(self.response({"type": "record_learning", "memory_id": self.c.id}))
        self.assertIsInstance(error, ConflictError)
        self.assertIn("not the Memory this session is on", str(error))
        self.assertEqual(self.state(self.a).learn_count, 0)

    def test_two_explicit_turns_are_two_turns_not_a_loop(self) -> None:
        model, runtime = self.runtime_for(
            self.response({"type": "record_learning"}),
            self.response({"type": "record_learning"}),
        )
        first = runtime.turn(session_id=self.session_id, user_message="第一轮")
        second = runtime.turn(session_id=self.session_id, user_message="第二轮")

        self.assertEqual(model.call_count, 2)                     # one call per turn
        self.assertEqual(first.context.session.plan_cursor, 1)
        self.assertEqual(second.context.session.plan_cursor, 2)
        self.assertEqual(second.context.session.current_memory_id, self.c.id)
        self.assertNotIn("第一轮", model.calls[1]["user_prompt"])   # no history carried over

    def test_the_turn_does_not_touch_the_other_session(self) -> None:
        _, result = self.turn(self.response({"type": "record_learning"}))
        self.assertEqual(str(self.session(self.other_session_id).status), "active")
        self.assertEqual(self.learning.get_state(self.stranger.id).learn_count, 0)
        self.assertEqual(result.context.session.id, self.session_id)
        self.assertNotEqual(result.context.session.id, self.other_session_id)


# ==========================================================================
# 2. context construction
# ==========================================================================

class ContextConstructionTest(TeacherRuntimeTestCase):
    def test_the_prompt_context_describes_the_session_and_its_source(self) -> None:
        self.turn(self.response())
        data = self.prompt_context()

        self.assertEqual(data["session"]["id"], self.session_id)
        self.assertEqual(data["source"]["id"], self.source.id)
        self.assertEqual(data["source"]["title"], "高等数学第一章")
        self.assertEqual(data["current_memory"]["id"], self.a.id)
        self.assertEqual(data["current_state"]["memory_id"], self.a.id)

    def test_the_overview_is_source_scoped_to_the_sessions_source(self) -> None:
        self.turn(self.response())
        data = self.prompt_context()
        overview = data["overview"]

        self.assertEqual(overview["source"]["id"], self.source.id)
        self.assertEqual({memory["id"] for memory in overview["memories"]},
                         {self.a.id, self.b.id, self.c.id})
        self.assertNotIn(self.stranger.id, {memory["id"] for memory in overview["memories"]})
        self.assertEqual(overview["active_session"]["id"], self.session_id)

    def test_the_overview_is_read_for_the_sessions_own_source(self) -> None:
        self.turn(self.response())
        self.assertEqual(self.spy.calls[1], ("get_learning_overview", self.source.id))

    def test_the_runtime_reads_the_context_for_the_requested_session(self) -> None:
        model, runtime = self.runtime_for(self.response(message="另一份材料"))
        result = runtime.turn(session_id=self.other_session_id, user_message="我们看这份材料")
        data = parsed_prompt_block(model.calls[0]["user_prompt"])

        self.assertEqual(data["session"]["id"], self.other_session_id)
        self.assertEqual(data["source"]["id"], self.other_source.id)
        self.assertEqual(data["current_memory"]["id"], self.stranger.id)
        self.assertEqual(result.context.session.id, self.other_session_id)

    def test_the_model_sees_the_pre_turn_state_while_the_caller_gets_the_post_turn_state(self) -> None:
        """The clearest statement of the refresh rule (§五)."""
        self.turn(self.response({"type": "record_learning"}))
        shown = self.prompt_context()
        after = self.service.get_context(self.session_id)

        self.assertEqual(shown["session"]["plan_cursor"], 0)
        self.assertEqual(shown["current_memory"]["id"], self.a.id)
        self.assertEqual(shown["overview"]["stats"]["learned_memories"], 0)
        self.assertEqual(after.session.plan_cursor, 1)
        self.assertEqual(after.session.current_memory_id, self.b.id)
        self.assertEqual(after.state_for(self.a.id).learn_count, 1)

    def test_an_exhausted_plan_session_still_has_a_context_to_show(self) -> None:
        for _ in range(3):
            self.service.record_learning(self.session_id)
        model, result = self.turn(self.response(message="计划走完了。"))
        data = self.prompt_context()

        self.assertIsNone(data["current_memory"])
        self.assertIsNone(data["current_state"])
        self.assertEqual(data["session"]["plan_cursor"], 3)
        self.assertIsNone(result.context.session.current_memory_id)

    def test_an_action_on_an_exhausted_plan_is_refused_by_the_service(self) -> None:
        for _ in range(3):
            self.service.record_learning(self.session_id)
        error = self.rejection(self.response({"type": "record_learning"}))
        self.assertIsInstance(error, ConflictError)
        self.assertIn("finished its plan", str(error))


# ==========================================================================
# 3. failure semantics (strict, unwrapped, no fake atomicity)
# ==========================================================================

class FailureSemanticsTest(TeacherRuntimeTestCase):
    def test_a_model_error_travels_up_unchanged(self) -> None:
        original = TeacherModelError("fake-model", "provider unavailable")
        error = self.rejection(original)
        self.assertIs(error, original)

    def test_a_model_error_prevents_any_execution(self) -> None:
        self.rejection(TimeoutError("provider timed out"))
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(self.spy.writes, [])
        self.assertEqual(self.spy.names, ["get_context", "get_learning_overview"])   # no re-read

    def test_a_model_exception_becomes_a_model_error_and_nothing_is_executed(self) -> None:
        error = self.rejection(ValueError("boom"))
        self.assertIsInstance(error, TeacherModelError)
        self.assertEqual(self.spy.writes, [])

    def test_an_illegal_model_output_travels_up_as_a_validation_error(self) -> None:
        error = self.rejection("我们继续吧")            # prose, not JSON
        self.assertIsInstance(error, ValidationError)
        self.assertEqual(error.fields, ("model_output",))
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(self.spy.writes, [])

    def test_an_illegal_action_in_the_output_executes_nothing_at_all(self) -> None:
        error = self.rejection(self.response(
            {"type": "record_learning"}, {"type": "delete_memory"}))
        self.assertEqual(error.fields, ("type",))
        self.assertEqual(self.spy.writes, [])
        self.assertEqual(self.state(self.a).learn_count, 0)

    def test_a_foreign_session_action_is_refused_before_anything_runs(self) -> None:
        error = self.rejection(self.response(
            {"type": "record_learning"},
            {"type": "finish_session", "session_id": FORBIDDEN_SESSION_PROBE}))
        self.assertEqual(error.fields, ("session_id",))
        self.assertEqual(self.spy.writes, [])
        self.assertEqual(str(self.session().status), "active")

    def test_a_missing_session_fails_before_the_model_is_called(self) -> None:
        error = self.rejection(self.response({"type": "record_learning"}),
                               session_id="lrn_missing")
        self.assertIsInstance(error, NotFoundError)
        self.assertEqual(error.entity, "learning session")
        self.assertEqual(self.model.call_count, 0)
        self.assertEqual(self.spy.names, ["get_context"])
        self.assertEqual(self.spy.writes, [])

    def test_an_invalid_user_message_fails_before_the_model_is_called(self) -> None:
        error = self.rejection(self.response(), user_message=42)
        self.assertIsInstance(error, ValidationError)
        self.assertIn("user_message", error.fields)
        self.assertEqual(self.model.call_count, 0)
        self.assertEqual(self.spy.writes, [])

    def test_a_mid_sequence_failure_executes_the_rest_of_nothing(self) -> None:
        """actions = [A, B, C]: A lands, B fails, C never runs, A is not rolled back."""
        model, runtime = self.runtime_for(self.response(
            {"type": "record_learning"},                              # A: A -> B
            {"type": "record_learning", "memory_id": self.a.id},      # B: refuses (A is no longer current)
            {"type": "record_learning"},                              # C: would succeed
        ))
        error = None
        try:
            runtime.turn(session_id=self.session_id, user_message="我懂了极限")
        except ConflictError as exc:
            error = exc

        self.assertIsNotNone(error, "B must fail")
        self.assertIn("not the Memory this session is on", str(error))
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.spy.names, ["get_context", "get_learning_overview",
                                          "record_learning", "record_learning"])   # no C, no re-read
        self.assertEqual(self.state(self.a).learn_count, 1)      # A really happened (no fake atomicity)
        self.assertEqual(self.state(self.b).learn_count, 0)      # C never ran
        self.assertEqual(self.session().plan_cursor, 1)

    def test_a_message_from_the_service_is_identical_through_the_runtime(self) -> None:
        self.service.finish_session(self.session_id)                   # session now completed
        try:
            self.service.record_learning(self.session_id)
        except ConflictError as direct:
            direct_message = str(direct)

        error = self.rejection(self.response({"type": "record_learning"}))
        self.assertIsInstance(error, ConflictError)
        self.assertEqual(str(error), direct_message)

    def test_the_runtime_does_not_pre_gate_the_session_status(self) -> None:
        """Gating is the service's job: the runtime calls the model, the service refuses."""
        self.service.finish_session(self.session_id)
        error = self.rejection(self.response({"type": "record_learning"}))

        self.assertIsInstance(error, ConflictError)
        self.assertIn("only an active session", str(error))
        self.assertEqual(self.model.call_count, 1)          # no business rule was copied here
        self.assertEqual(self.spy.writes, [("record_learning", self.session_id)])

    def test_a_read_only_turn_on_a_completed_session_still_works(self) -> None:
        self.service.finish_session(self.session_id)
        _, result = self.turn(self.response(message="复习一下就好。"))

        self.assertEqual(result.response.actions, ())
        self.assertEqual(str(result.context.session.status), "completed")
        self.assertEqual(self.spy.writes, [])                # nothing was executed

    def test_there_is_no_implicit_finish_or_implicit_new_session(self) -> None:
        self.turn(self.response({"type": "record_learning"}))
        self.assertEqual(self.learning.counts()["learning_sessions"], 2)
        self.assertEqual(str(self.session().status), "active")

    def test_no_database_mutation_survives_a_failed_action_that_did_not_write(self) -> None:
        before = self.snapshot()
        for payload in ("prose", self.response({"type": "execute_sql", "sql": "DROP TABLE memories"}),
                        self.response({"type": "record_assessment", "understanding_level": "mastered"})):
            with self.assertRaises(Exception):      # noqa: B017 - all three are refusals
                self.turn(payload)
        self.assertEqual(self.snapshot(), before)


# ==========================================================================
# 4. no retry, no second model call, no loop
# ==========================================================================

class SingleCallTest(TeacherRuntimeTestCase):
    def test_the_model_is_called_exactly_once_per_successful_turn(self) -> None:
        model, _ = self.turn(self.response({"type": "record_learning"}))
        self.assertEqual(model.call_count, 1)

    def test_no_retry_after_a_model_error(self) -> None:
        model, runtime = self.runtime_for(TimeoutError("down"), self.response({"type": "record_learning"}))
        with self.assertRaises(TeacherModelError):
            runtime.turn(session_id=self.session_id, user_message="x")

        self.assertEqual(model.call_count, 1)               # the valid second answer was never used
        self.assertEqual(self.state(self.a).learn_count, 0)
        self.assertEqual(model.responses, [self.response({"type": "record_learning"})])

    def test_no_retry_after_an_illegal_output(self) -> None:
        model, runtime = self.runtime_for("not json", self.response({"type": "record_learning"}))
        with self.assertRaises(ValidationError):
            runtime.turn(session_id=self.session_id, user_message="x")

        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.state(self.a).learn_count, 0)

    def test_no_retry_after_a_service_failure(self) -> None:
        model, runtime = self.runtime_for(self.response(
            {"type": "record_learning", "memory_id": self.c.id}))     # refused by the service
        with self.assertRaises(ConflictError):
            runtime.turn(session_id=self.session_id, user_message="x")

        self.assertEqual(model.call_count, 1)

    def test_no_second_model_call_in_the_source(self) -> None:
        tree = ast.parse(RUNTIME_SOURCE.read_text(encoding="utf-8"))
        generate_sites = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "generate"
        ]
        self.assertEqual(len(generate_sites), 1)
        self.assertEqual(len([n for n in ast.walk(tree) if isinstance(n, ast.While)]), 0)

    def test_one_execution_pass_per_turn(self) -> None:
        tree = ast.parse(RUNTIME_SOURCE.read_text(encoding="utf-8"))
        loops = [node for node in ast.walk(tree) if isinstance(node, ast.For)]
        self.assertEqual(len(loops), 1)                       # the ordered actions pass
        self.assertEqual(len([n for n in ast.walk(tree) if isinstance(n, ast.AsyncFor)]), 0)


# ==========================================================================
# 5. the result object
# ==========================================================================

class TurnResultTest(TeacherRuntimeTestCase):
    def test_the_result_has_exactly_two_stored_fields(self) -> None:
        self.assertEqual([field.name for field in dataclasses.fields(TeacherTurnResult)],
                         ["response", "context"])

    def test_executed_actions_is_a_property_not_duplicated_state(self) -> None:
        _, result = self.turn(self.response({"type": "record_learning"}))
        self.assertEqual(result.executed_actions, result.response.actions)
        self.assertNotIn("executed_actions", vars(result))

    def test_the_result_is_frozen(self) -> None:
        _, result = self.turn(self.response())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.context = None                            # type: ignore[misc]

    def test_the_result_carries_the_model_message_and_the_actions(self) -> None:
        _, result = self.turn(self.response({"type": "record_learning"}, message="记下了。"))
        self.assertEqual(result.response.assistant_message, "记下了。")
        self.assertEqual(result.response.action_types, ("record_learning",))

    def test_the_result_validates_its_fields(self) -> None:
        _, result = self.turn(self.response())
        with self.assertRaises(ValidationError) as ctx:
            TeacherTurnResult(response="not a response", context=result.context)
        self.assertEqual(ctx.exception.fields, ("response",))
        with self.assertRaises(ValidationError) as ctx:
            TeacherTurnResult(response=result.response, context=None)
        self.assertEqual(ctx.exception.fields, ("context",))

    def test_the_result_is_json_friendly(self) -> None:
        _, result = self.turn(self.response({"type": "record_learning"}, message="记下了。"))
        dumped = result.as_dict()
        self.assertEqual(dumped["response"]["assistant_message"], "记下了。")
        self.assertEqual(dumped["context"]["session"]["plan_cursor"], 1)
        self.assertIn("context", dumped)

    def test_the_result_exposes_the_service_context_not_a_wrapper(self) -> None:
        _, result = self.turn(self.response())
        self.assertEqual(result.context.session.id, self.session_id)
        self.assertEqual(result.context.source.id, self.source.id)
        self.assertEqual(result.context.memory_ids, (self.a.id, self.b.id, self.c.id))


# ==========================================================================
# 6. architecture guards
# ==========================================================================

class RuntimeArchitectureTest(unittest.TestCase):
    @property
    def tree(self) -> ast.Module:
        return ast.parse(RUNTIME_SOURCE.read_text(encoding="utf-8"))

    def test_runtime_imports_stay_minimal(self) -> None:
        tree = self.tree
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

        self.assertEqual(runtime,
                         {"__future__", "dataclasses", "typing", "errors", "teacher",
                          "teacher_executor", "teacher_llm"})
        self.assertEqual(deferred, {"learning"})

    def test_forbidden_dependencies_are_absent(self) -> None:
        imported: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("learning_store", "db", "sqlite3", "store", "models", "retrieval",
                          "web", "cli", "llm", "prompts", "urllib", "requests", "socket",
                          "http", "openai", "deepseek", "anthropic"):
            self.assertNotIn(forbidden, imported, f"teacher_runtime.py imports {forbidden}")

    def test_no_database_sql_or_repository_reference(self) -> None:
        source = RUNTIME_SOURCE.read_text(encoding="utf-8")
        for marker in ("import sqlite3", "sqlite3.", "SELECT ", "INSERT ", "UPDATE ", "DELETE ",
                       "executemany", "commit("):
            self.assertNotIn(marker, source, f"teacher_runtime.py contains {marker!r}")

        identifiers = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)}
        for forbidden in ("LearningRepository", "MemoryRepository", "Database", "transaction",
                          "Connection", "connect", "cursor", "execute_sql", "sqlite3"):
            self.assertNotIn(forbidden, identifiers, f"teacher_runtime.py uses {forbidden}")

    def test_the_runtime_never_bypasses_the_executor(self) -> None:
        calls = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    calls.add(node.func.id)

        # no LearningService write method is called directly: only the executor may write
        for forbidden in ("record_learning", "record_assessment", "finish_session",
                          "abandon_session", "start_session", "update_state", "update_session",
                          "create_state", "create_session", "upsert_state", "transaction",
                          "get_session", "require_session", "get_state"):
            self.assertNotIn(forbidden, calls, f"teacher_runtime.py calls {forbidden} directly")

        # exactly one execution call site and one model call site
        self.assertEqual(len([n for n in ast.walk(self.tree) if isinstance(n, ast.Call)
                              and isinstance(n.func, ast.Attribute) and n.func.attr == "execute"]), 1)
        self.assertTrue({"get_context", "get_learning_overview", "generate", "execute",
                         "from_learning", "TeacherTurnRequest", "TeacherTurnResult",
                         "TeacherLLMAdapter", "TeacherActionExecutor"} <= calls)

    def test_the_runtime_exposes_one_public_method(self) -> None:
        public = {name for name in vars(RuntimeClass) if not name.startswith("_")}
        self.assertEqual(public, {"turn"})

    def test_the_runtime_holds_no_repository_database_or_provider(self) -> None:
        class Unused:                                            # never called in this test
            def generate(self, **kwargs):  # pragma: no cover
                raise AssertionError("should not be called")

        runtime = TeacherRuntime(object(), Unused())
        for forbidden in ("repository", "database", "connection", "transaction", "client",
                          "api_key", "session", "source"):
            self.assertFalse(hasattr(runtime, forbidden), forbidden)
        self.assertIs(runtime.executor.learning, runtime.learning)

    def test_the_dependency_direction_is_one_way(self) -> None:
        for name in ("personal_memory/teacher.py", "personal_memory/teacher_executor.py",
                     "personal_memory/teacher_llm.py", "personal_memory/learning.py",
                     "personal_memory/learning_store.py"):
            tree = ast.parse(pathlib.Path(name).read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[-1] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[-1])
            self.assertNotIn("teacher_runtime", imported, f"{name} imports the runtime")

        for name in ("personal_memory/learning.py", "personal_memory/learning_store.py"):
            tree = ast.parse(pathlib.Path(name).read_text(encoding="utf-8"))
            identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            identifiers |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            self.assertFalse([n for n in identifiers if "TeacherRuntime" in n],
                             f"{name} references the runtime in code")

    def test_no_http_provider_or_ui_dependency(self) -> None:
        source = RUNTIME_SOURCE.read_text(encoding="utf-8")
        for marker in ("api_key", "API_KEY", "Bearer", "https://", "http://", "os.environ",
                       "fastapi", "FastAPI", "Flask", "websocket", "WebSocket", "stream"):
            self.assertNotIn(marker, source, f"teacher_runtime.py mentions {marker!r}")

        identifiers = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)}
        for token in ("urllib", "requests", "socket", "httpx", "aiohttp", "fastapi", "flask",
                      "websocket", "openai", "deepseek", "anthropic", "retry"):
            self.assertFalse([name for name in identifiers if token in name.lower()],
                             f"teacher_runtime.py references {token!r} in code")

    def test_the_runtime_never_wraps_or_swallows_an_exception(self) -> None:
        tree = self.tree
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.Try)], [])
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)], [])


class CallChainTest(TeacherRuntimeTestCase):
    def test_the_documented_call_chain_happens_in_order(self) -> None:
        """get_context -> overview -> (model) -> executor -> service -> re-read."""
        self.turn(self.response(
            {"type": "record_learning"},
            {"type": "record_assessment", "understanding_level": "partial"},
        ))
        self.assertEqual(self.spy.names, [
            "get_context",             # 1-2  session-scoped context (gives the source id)
            "get_learning_overview",   # 3    source-scoped overview
            "record_learning",         # 8    executor -> LearningService (action 1)
            "record_assessment",       # 8    executor -> LearningService (action 2)
            "get_context",             # 9-10 re-read: the latest state
        ])

    def test_the_runtime_is_not_a_second_learning_service(self) -> None:
        model, runtime = self.runtime_for(self.response({"type": "record_learning"}))
        self.assertEqual(self.spy.calls, [])
        runtime.turn(session_id=self.session_id, user_message="我懂了极限")
        # the engine still owns every rule: it was asked, in the documented order
        self.assertTrue(set(self.spy.names) <= {"get_context", "get_learning_overview",
                                                "record_learning", "record_assessment",
                                                "finish_session", "abandon_session"})


# ==========================================================================
# 7. zero direct database access
# ==========================================================================

class ZeroDirectWriteTest(TeacherRuntimeTestCase):
    def test_an_empty_action_turn_changes_no_row_at_all(self) -> None:
        before = self.snapshot()
        self.turn(self.response(message="只是聊聊。"))
        self.assertEqual(self.snapshot(), before)

    def test_the_runtime_itself_only_ever_reads_through_the_service(self) -> None:
        self.turn(self.response())
        reads = [call for call in self.spy.calls if call[0] in ("get_context", "get_learning_overview")]
        self.assertEqual(len(reads), 3)
        self.assertEqual(self.spy.writes, [])

    def test_a_learning_turn_touches_learning_rows_only(self) -> None:
        before = self.snapshot()
        _, result = self.turn(self.response({"type": "record_learning"}))

        self.assertEqual(self.repo.counts(), before["memory_counts"])
        self.assertEqual(self.repo.get_memory(self.a.id).as_dict(), before["memory"])
        self.assertEqual(self.repo.get_source(self.source.id).as_dict(), before["source"])
        self.assertEqual(result.context.state_for(self.a.id).learn_count, 1)
        self.assertEqual(self.learning.counts()["learning_sessions"], 2)

    def test_1_0_search_index_is_untouched(self) -> None:
        before = [(hit.memory.id, hit.score)
                  for hit in MemoryRetriever(self.repo).search("极限").hits]
        self.turn(self.response(
            {"type": "record_learning"},
            {"type": "record_assessment", "understanding_level": "solid"},
        ))
        after = [(hit.memory.id, hit.score)
                 for hit in MemoryRetriever(self.repo).search("极限").hits]
        self.assertEqual(after, before)

    def test_the_other_memory_of_the_other_source_is_never_touched(self) -> None:
        before = self.learning.get_state(self.stranger.id).as_dict()
        self.turn(self.response({"type": "record_learning"}))
        self.assertEqual(self.learning.get_state(self.stranger.id).as_dict(), before)


# ==========================================================================
# 8. regression: nothing else moved
# ==========================================================================

class Phase2C4RegressionTest(TeacherRuntimeTestCase):
    def test_no_new_table_and_no_schema_change(self) -> None:
        with self.database.connection() as conn:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        self.assertEqual({name for name in tables if name.startswith("learning")},
                         {"learning_states", "learning_sessions"})
        for forbidden in ("runtime", "teacher_runtime", "turn"):
            self.assertFalse([name for name in tables if forbidden in name.lower()],
                             f"P2C-4 added a table for {forbidden!r}")
        self.assertEqual(SUPPORTED_SCHEMA_VERSION, max(m.version for m in migrations()))

    def test_the_learning_service_api_is_unchanged(self) -> None:
        self.assertEqual(len(LEARNING_PUBLIC_API), 8)
        self.assertEqual(LEARNING_PUBLIC_API[-1], "abandon_session")
        self.assertNotIn("turn", LEARNING_PUBLIC_API)
        self.assertNotIn("teacher_context", LEARNING_PUBLIC_API)

    def test_the_teacher_layers_below_are_unchanged(self) -> None:
        self.assertEqual(SUPPORTED_ACTION_TYPES, ALLOWED_ACTION_TYPES)
        self.assertEqual(TEACHER_PROMPT_VERSION, "teacher_prompt_v1")
        self.assertEqual(len(ALLOWED_ACTION_TYPES), 4)

    def test_the_runtime_uses_the_existing_pieces_instead_of_reimplementing_them(self) -> None:
        model, runtime = self.runtime_for(self.response({"type": "record_learning"}))
        self.assertIs(runtime.adapter.model, model)
        self.assertIs(runtime.executor.learning, self.spy)

    def test_a_full_turn_is_decided_by_the_service_not_the_runtime(self) -> None:
        """Same action, same outcome as calling the service directly."""
        self.turn(self.response({"type": "record_learning"}))
        via_runtime = self.service.get_context(self.session_id)
        direct = self.service.record_learning(self.session_id)

        self.assertEqual(via_runtime.session.plan_cursor + 1, direct.session.plan_cursor)
        self.assertEqual(direct.state_for(self.b.id).learn_count, 1)
        self.assertEqual(via_runtime.state_for(self.a.id).learn_count, 1)


def migrations():
    from personal_memory.db import MIGRATIONS

    return MIGRATIONS


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
