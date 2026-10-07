"""Phase 2C-6: bounded TeacherAgent loop tests.

What is locked:

* one step == one ``TeacherRuntime.turn`` (so one model call per step, ordered
  execution, context re-read after execution)
* stop conditions: ``actions == ()``, ``finish_session``, ``abandon_session``,
  ``max_steps`` -- and nothing else; the loop never judges whether an action is sensible
* a repeated action cannot extend a run: the budget is the bound, so no de-duplication
  policy and no copied learning rule is needed
* failures travel up unchanged (no retry, no skip, no rollback, no wrapping) while the
  effects of the steps that already committed stay real
* the loop is an orchestration layer only: no database, no repository, no SQL, no HTTP,
  no provider, and no service call of its own
* the final context is the last step's post-execution state, kept exactly once
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import pathlib
import unittest

from personal_memory import (
    ALLOWED_ACTION_TYPES,
    DEFAULT_MAX_STEPS,
    MAX_STEPS_LIMIT,
    STOP_ACTION_TYPES,
    SUPPORTED_SCHEMA_VERSION,
    TeacherAgent,
    TeacherAgentResult,
    TeacherPromptBuilder,
    TeacherPromptV2Builder,
    TeacherProvider,
    TeacherRuntime,
    TeacherRuntime as RuntimeClass,
    LearningRepository,
    LearningService,
)
from personal_memory.errors import ConflictError, NotFoundError, ValidationError
from personal_memory.learning import PUBLIC_API as LEARNING_PUBLIC_API
from personal_memory.retrieval import MemoryRetriever
from personal_memory.teacher_executor import SUPPORTED_ACTION_TYPES
from personal_memory.teacher_llm import TEACHER_PROMPT_VERSION, TeacherLLMAdapter, TeacherModelError

from .helpers import RepositoryTestCase
from .llm_fakes import FakeTeacherModel

AGENT_SOURCE = pathlib.Path("personal_memory/teacher_agent.py")
WITHDRAWALS = ("record_learning", "record_assessment", "finish_session", "abandon_session")
UNSET = object()


def block(text: str, tag: str) -> str:
    opening = text.index(f"<{tag}")
    start = text.index(">\n", opening) + 2
    end = text.index(f"\n</{tag}>", start)
    return text[start:end].replace("\\u003c", "<").replace("\\u003e", ">")


class RecordingLearningService:
    """Delegates to the real service and records the exact call chain."""

    def __init__(self, real: LearningService) -> None:
        self.real = real
        self.calls: list[str] = []

    def get_context(self, session_id):
        self.calls.append("get_context")
        return self.real.get_context(session_id)

    def get_learning_overview(self, source_id):
        self.calls.append("get_learning_overview")
        return self.real.get_learning_overview(source_id)

    def record_learning(self, session_id, *, memory_id=None):
        self.calls.append("record_learning")
        return self.real.record_learning(session_id, memory_id=memory_id)

    def record_assessment(self, session_id, **kwargs):
        self.calls.append("record_assessment")
        return self.real.record_assessment(session_id, **kwargs)

    def finish_session(self, session_id):
        self.calls.append("finish_session")
        return self.real.finish_session(session_id)

    def abandon_session(self, session_id):
        self.calls.append("abandon_session")
        return self.real.abandon_session(session_id)


class AgentTestCase(RepositoryTestCase):
    """Source A (3 Memories) + Source B (1 Memory), one running session each."""

    prefix = "pms-agent-"

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

        self.model = None
        self.spy = None
        self.runtime = None
        self.agent = None

    # -- helpers ----------------------------------------------------------
    def agent_for(self, *responses, prompt_builder=None):
        self.model = FakeTeacherModel(*responses)
        self.spy = RecordingLearningService(self.service)
        self.runtime = TeacherRuntime(self.spy, self.model, prompt_builder=prompt_builder)
        self.agent = TeacherAgent(self.runtime)
        return self.agent

    def perform(self, *responses, max_steps=UNSET, prompt_builder=None, session_id=None,
                user_message="我懂了极限"):
        """Never name a test helper ``run``: ``unittest.TestCase.run`` must stay intact."""
        self.agent_for(*responses, prompt_builder=prompt_builder)
        target = self.session_id if session_id is None else session_id
        if max_steps is UNSET:
            return self.agent.run(session_id=target, user_message=user_message)
        return self.agent.run(session_id=target, user_message=user_message, max_steps=max_steps)

    def refusal(self, *responses, exception=Exception, **kwargs):
        with self.assertRaises(exception) as ctx:
            self.perform(*responses, **kwargs)
        return ctx.exception

    def response(self, *actions, message="继续吧") -> dict:
        return {"assistant_message": message, "actions": list(actions)}

    def session(self, session_id=None):
        return self.learning.get_session(self.session_id if session_id is None else session_id)

    def state(self, memory):
        return self.learning.get_state(memory.id)

    def step_context(self, index: int = 0) -> dict:
        """The teacher_context the model was shown on its ``index``-th call."""
        return json.loads(block(self.model.calls[index]["user_prompt"], "teacher_context"))

    def step_user_message(self, index: int = 0) -> str:
        return json.loads(block(self.model.calls[index]["user_prompt"], "user_message"))

    def snapshot(self) -> dict:
        with self.database.connection() as conn:
            fts = {name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
                   for name in ("memory_fts_word", "memory_fts_trigram")}
        return {
            "memory_counts": self.repo.counts(),
            "learning_counts": self.learning.counts(),
            "fts": fts,
            "sessions": [self.session().as_dict(), self.session(self.other_session_id).as_dict()],
            "states": [self.state(m).as_dict() for m in (self.a, self.b, self.c, self.stranger)],
            "memory": self.repo.get_memory(self.a.id).as_dict(),
            "source": self.repo.get_source(self.source.id).as_dict(),
            "search": [(hit.memory.id, hit.score)
                       for hit in MemoryRetriever(self.repo).search("极限").hits],
        }


# ==========================================================================
# 1. the loop: steps, ordering, context refresh
# ==========================================================================

class LoopFlowTest(AgentTestCase):
    def test_empty_actions_end_the_run_after_one_step(self) -> None:
        result = self.perform(self.response(message="那我们继续看连续。"))

        self.assertEqual(result.steps, 1)
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(result.stop_reason, "no_actions")
        self.assertEqual(result.executed_actions, ())
        self.assertEqual(result.final_response.assistant_message, "那我们继续看连续。")
        self.assertEqual(result.context.session.id, self.session_id)

    def test_an_action_causes_a_second_step(self) -> None:
        result = self.perform(self.response({"type": "record_learning"}),
                              self.response(message="已经记下了。"))

        self.assertEqual(result.steps, 2)
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(result.stop_reason, "no_actions")
        self.assertEqual(result.responses[0].action_types, ("record_learning",))
        self.assertEqual(result.responses[1].action_types, ())

    def test_the_second_step_sees_the_updated_context(self) -> None:
        self.perform(self.response({"type": "record_learning"}),
                     self.response({"type": "record_assessment", "understanding_level": "partial"}),
                     max_steps=2)

        first = self.step_context(0)
        second = self.step_context(1)
        self.assertEqual(first["session"]["plan_cursor"], 0)
        self.assertEqual(first["current_memory"]["id"], self.a.id)
        self.assertEqual(second["session"]["plan_cursor"], 1)          # what step 1 did
        self.assertEqual(second["current_memory"]["id"], self.b.id)
        self.assertEqual(second["current_state"]["memory_id"], self.b.id)

    def test_the_same_user_message_is_sent_every_step(self) -> None:
        self.perform(self.response({"type": "record_learning"}), self.response())

        self.assertEqual(self.step_user_message(0), "我懂了极限")
        self.assertEqual(self.step_user_message(1), "我懂了极限")
        self.assertEqual(self.step_user_message(0), self.step_user_message(1))

    def test_no_assistant_history_leaks_into_later_steps(self) -> None:
        """Loop state travels through the context, never through a fake user message."""
        self.perform(self.response({"type": "record_learning"},
                                   message="独特标记ABC：我已经记录了一次学习事件。"),
                     self.response(message="第二步"))

        later_prompt = self.model.calls[1]["user_prompt"]
        self.assertNotIn("独特标记ABC", later_prompt)
        self.assertEqual(self.step_user_message(1), "我懂了极限")
        self.assertNotIn("独特标记ABC", self.step_context(1).__str__())

    def test_the_context_is_the_post_execution_state_of_the_last_step(self) -> None:
        result = self.perform(self.response({"type": "record_learning"}),
                              self.response({"type": "record_learning"}),
                              max_steps=2)

        self.assertEqual(result.steps, 2)
        self.assertEqual(result.stop_reason, "max_steps")
        self.assertEqual(result.context.session.plan_cursor, 2)
        self.assertEqual(result.context.session.current_memory_id, self.c.id)
        self.assertEqual(result.context.state_for(self.a.id).learn_count, 1)
        self.assertEqual(result.context.state_for(self.b.id).learn_count, 1)
        self.assertEqual(result.context.state_for(self.c.id).learn_count, 0)

    def test_responses_keep_the_step_order(self) -> None:
        result = self.perform(
            self.response({"type": "record_learning"}, message="第一步"),
            self.response({"type": "record_assessment", "understanding_level": "partial"},
                          message="第二步"),
            self.response(message="第三步"),
        )
        self.assertEqual([r.assistant_message for r in result.responses],
                         ["第一步", "第二步", "第三步"])
        self.assertEqual([r.action_types for r in result.responses],
                         [("record_learning",), ("record_assessment",), ()])

    def test_executed_actions_keep_step_and_within_step_order(self) -> None:
        result = self.perform(
            self.response({"type": "record_learning"},
                          {"type": "record_assessment", "understanding_level": "partial"}),
            self.response({"type": "record_learning"},
                          {"type": "record_assessment", "understanding_level": "fuzzy"}),
            self.response(),
        )
        self.assertEqual([a.kind for a in result.executed_actions],
                         ["record_learning", "record_assessment",
                          "record_learning", "record_assessment"])
        self.assertEqual(result.executed_actions,
                         tuple(a for r in result.responses for a in r.actions))

    def test_actions_inside_one_step_keep_the_model_order(self) -> None:
        self.perform(self.response({"type": "record_learning"},
                                   {"type": "record_assessment", "understanding_level": "solid"},
                                   {"type": "record_learning"}),
                     self.response())
        self.assertEqual(self.spy.calls[2:5], ["record_learning", "record_assessment",
                                               "record_learning"])

    def test_the_documented_call_chain_per_step(self) -> None:
        self.perform(self.response({"type": "record_learning"}), self.response())
        self.assertEqual(self.spy.calls, [
            "get_context", "get_learning_overview", "record_learning", "get_context",  # step 1
            "get_context", "get_learning_overview", "get_context",                     # step 2
        ])

    def test_a_run_without_actions_changes_no_row(self) -> None:
        before = self.snapshot()
        self.perform(self.response(message="只是聊聊。"))
        self.assertEqual(self.snapshot(), before)

    def test_two_explicit_runs_are_two_runs(self) -> None:
        first = self.perform(self.response({"type": "record_learning"}), self.response())
        cursor_after_first = first.context.session.plan_cursor

        second = self.perform(self.response({"type": "record_learning"}), self.response())
        self.assertEqual(cursor_after_first, 1)
        self.assertEqual(second.context.session.plan_cursor, 2)
        self.assertEqual(self.model.call_count, 2)          # a fresh model for the second run

    def test_a_single_step_run_carries_one_context_only(self) -> None:
        result = self.perform(self.response())
        self.assertEqual([field.name for field in dataclasses.fields(TeacherAgentResult)],
                         ["responses", "context"])
        self.assertEqual(result.context.source.id, self.source.id)


# ==========================================================================
# 2. stop conditions and the step budget
# ==========================================================================

class StopConditionTest(AgentTestCase):
    def test_finish_session_stops_the_run(self) -> None:
        result = self.perform(self.response({"type": "finish_session",
                                             "session_id": self.session_id},
                                            message="今天到这里。"))

        self.assertEqual(result.steps, 1)
        self.assertEqual(result.stop_reason, "session_ended")
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(str(result.context.session.status), "completed")

    def test_abandon_session_stops_the_run(self) -> None:
        result = self.perform(self.response({"type": "abandon_session",
                                             "session_id": self.session_id}))
        self.assertEqual(result.steps, 1)
        self.assertEqual(result.stop_reason, "session_ended")
        self.assertEqual(str(result.context.session.status), "abandoned")

    def test_a_session_ending_action_with_other_actions_still_stops(self) -> None:
        result = self.perform(self.response({"type": "record_learning"},
                                            {"type": "finish_session",
                                             "session_id": self.session_id}))

        self.assertEqual(result.steps, 1)
        self.assertEqual(result.stop_reason, "session_ended")
        self.assertEqual(self.state(self.a).learn_count, 1)          # everything ran
        self.assertEqual(str(result.context.session.status), "completed")

    def test_a_queued_response_is_never_used_after_a_stop(self) -> None:
        self.perform(self.response(message="结束"), self.response({"type": "record_learning"}))
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(len(self.model.responses), 1)               # step 2 never happened

    def test_max_steps_is_a_hard_stop(self) -> None:
        result = self.perform(
            self.response({"type": "record_assessment", "understanding_level": "fuzzy"}),
            self.response({"type": "record_assessment", "understanding_level": "partial"}),
            self.response({"type": "record_assessment", "understanding_level": "solid"}),
            max_steps=3,
        )
        self.assertEqual(result.steps, 3)
        self.assertEqual(result.stop_reason, "max_steps")
        self.assertEqual(self.model.call_count, 3)

    def test_max_steps_one_is_enough_for_a_single_step(self) -> None:
        result = self.perform(self.response({"type": "record_learning"}), max_steps=1)
        self.assertEqual(result.steps, 1)
        self.assertEqual(result.stop_reason, "max_steps")
        self.assertEqual(self.state(self.a).learn_count, 1)

    def test_the_default_budget_and_the_hard_limit(self) -> None:
        self.assertEqual(DEFAULT_MAX_STEPS, 3)
        self.assertEqual(MAX_STEPS_LIMIT, 10)
        self.assertEqual(STOP_ACTION_TYPES, ("finish_session", "abandon_session"))

    def test_the_signature_defaults_to_the_documented_budget(self) -> None:
        signature = inspect.signature(TeacherAgent.run)
        self.assertEqual(list(signature.parameters), ["self", "session_id", "user_message",
                                                      "max_steps"])
        for name in ("session_id", "user_message", "max_steps"):
            self.assertEqual(signature.parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(signature.parameters["max_steps"].default, DEFAULT_MAX_STEPS)

    def test_zero_max_steps_is_refused_before_any_model_call(self) -> None:
        error = self.refusal(self.response(), max_steps=0, exception=ValidationError)
        self.assertEqual(error.fields, ("max_steps",))
        self.assertEqual(self.model.call_count, 0)

    def test_negative_max_steps_is_refused(self) -> None:
        for value in (-1, -10):
            error = self.refusal(self.response(), max_steps=value, exception=ValidationError)
            self.assertEqual(error.fields, ("max_steps",))
        self.assertEqual(self.model.call_count, 0)

    def test_non_integer_max_steps_is_refused(self) -> None:
        for value in (3.0, "3", None, [3], True, False):
            error = self.refusal(self.response(), max_steps=value, exception=ValidationError)
            self.assertEqual(error.fields, ("max_steps",), f"{value!r} was accepted")
        self.assertEqual(self.model.call_count, 0)

    def test_max_steps_above_the_hard_limit_is_refused(self) -> None:
        error = self.refusal(self.response(), max_steps=MAX_STEPS_LIMIT + 1,
                             exception=ValidationError)
        self.assertEqual(error.fields, ("max_steps",))
        self.assertIn(str(MAX_STEPS_LIMIT), str(error))
        self.assertEqual(self.model.call_count, 0)

    def test_the_hard_limit_itself_is_accepted(self) -> None:
        responses = [self.response({"type": "record_assessment",
                                    "understanding_level": "fuzzy"})] * MAX_STEPS_LIMIT
        result = self.perform(*responses, max_steps=MAX_STEPS_LIMIT)
        self.assertEqual(result.steps, MAX_STEPS_LIMIT)
        self.assertEqual(self.model.call_count, MAX_STEPS_LIMIT)


# ==========================================================================
# 3. repetition (§六) -- bounded, never infinite
# ==========================================================================

class RepetitionTest(AgentTestCase):
    def test_the_same_action_repeated_is_bounded_by_max_steps(self) -> None:
        identical = self.response({"type": "record_assessment", "understanding_level": "partial"})
        result = self.perform(*([identical] * 5), max_steps=3)

        self.assertEqual(result.steps, 3)
        self.assertEqual(self.model.call_count, 3)                    # never a 4th call
        self.assertEqual(len(self.model.responses), 2)                # two were never used
        self.assertEqual(result.stop_reason, "max_steps")
        self.assertEqual(str(self.state(self.a).understanding_level), "partial")

    def test_repeated_record_learning_is_bounded_by_max_steps(self) -> None:
        result = self.perform(*([self.response({"type": "record_learning"})] * 5), max_steps=2)

        self.assertEqual(result.steps, 2)
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(result.context.session.plan_cursor, 2)
        self.assertEqual([self.state(m).learn_count for m in (self.a, self.b, self.c)], [1, 1, 0])

    def test_repeated_record_learning_until_the_plan_runs_out(self) -> None:
        """The 4th repetition hits the exhausted plan: the service refuses, the loop stops."""
        error = self.refusal(*([self.response({"type": "record_learning"})] * 5),
                             max_steps=5, exception=ConflictError)

        self.assertIn("finished its plan", str(error))
        self.assertEqual(self.model.call_count, 4)                    # <= max_steps
        self.assertEqual(self.context_cursor(), 3)
        self.assertEqual([self.state(m).learn_count for m in (self.a, self.b, self.c)], [1, 1, 1])

    def test_repeated_assessment_does_not_advance_but_stays_bounded(self) -> None:
        result = self.perform(*([self.response({"type": "record_assessment",
                                                "understanding_level": "solid"})] * 4),
                              max_steps=3)
        self.assertEqual(result.steps, 3)
        self.assertEqual(result.context.session.plan_cursor, 0)        # assessment never advances
        self.assertEqual(str(self.state(self.a).understanding_level), "solid")

    def test_an_exhausted_plan_with_no_actions_ends_cleanly(self) -> None:
        for _ in range(3):
            self.service.record_learning(self.session_id)

        result = self.perform(self.response(message="计划走完了。"))
        self.assertEqual(result.steps, 1)
        self.assertEqual(result.stop_reason, "no_actions")
        self.assertIsNone(result.context.session.current_memory_id)

    def test_an_exhausted_plan_with_an_action_is_refused_and_stops(self) -> None:
        for _ in range(3):
            self.service.record_learning(self.session_id)
        before = self.snapshot()

        error = self.refusal(self.response({"type": "record_learning"}), exception=ConflictError)

        self.assertIn("finished its plan", str(error))
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(self.snapshot(), before)

    def test_a_completed_session_fails_on_the_first_action(self) -> None:
        self.service.finish_session(self.session_id)

        error = self.refusal(self.response({"type": "record_learning"}), exception=ConflictError)
        self.assertIn("only an active session", str(error))
        self.assertEqual(self.model.call_count, 1)                     # no pre-gating in the loop

    def test_a_completed_session_with_no_actions_ends_cleanly(self) -> None:
        self.service.finish_session(self.session_id)
        result = self.perform(self.response(message="复习一下就好。"))

        self.assertEqual(result.steps, 1)
        self.assertEqual(str(result.context.session.status), "completed")
        self.assertEqual(self.spy.calls, ["get_context", "get_learning_overview", "get_context"])

    def test_an_abandoned_session_fails_on_the_first_action(self) -> None:
        self.service.abandon_session(self.session_id)
        error = self.refusal(self.response({"type": "record_assessment",
                                           "understanding_level": "solid"}),
                             exception=ConflictError)
        self.assertIn("only an active session", str(error))

    def test_no_loop_can_run_forever(self) -> None:
        """Even an always-proposing model is cut off by the budget."""
        result = self.perform(*([self.response({"type": "record_assessment",
                                                "understanding_level": "fuzzy"})]
                                * (MAX_STEPS_LIMIT + 5)),
                              max_steps=MAX_STEPS_LIMIT)
        self.assertEqual(result.steps, MAX_STEPS_LIMIT)
        self.assertEqual(self.model.call_count, MAX_STEPS_LIMIT)

    def context_cursor(self) -> int:
        return self.service.get_context(self.session_id).session.plan_cursor


# ==========================================================================
# 4. failure semantics
# ==========================================================================

class LoopFailureTest(AgentTestCase):
    def test_a_model_error_in_the_first_step_propagates(self) -> None:
        original = TeacherModelError("fake", "provider down")
        error = self.refusal(original, self.response(), exception=TeacherModelError)
        self.assertIs(error, original)
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(self.spy.calls, ["get_context", "get_learning_overview"])

    def test_a_model_error_in_the_second_step_keeps_the_first_step_effect(self) -> None:
        error = self.refusal(self.response({"type": "record_learning"}),
                             TimeoutError("provider timed out"),
                             exception=TeacherModelError)

        self.assertEqual(self.model.call_count, 2)
        self.assertIsInstance(error.__cause__, TimeoutError)
        self.assertEqual(self.state(self.a).learn_count, 1)            # step 1 stayed real
        self.assertEqual(self.session().plan_cursor, 1)

    def test_an_illegal_output_in_the_second_step_propagates(self) -> None:
        error = self.refusal(self.response({"type": "record_learning"}),
                             "我们继续吧",
                             exception=ValidationError)

        self.assertEqual(error.fields, ("model_output",))
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(self.state(self.a).learn_count, 1)
        self.assertEqual(self.session().plan_cursor, 1)

    def test_a_failed_action_in_the_second_step_stops_the_run(self) -> None:
        error = self.refusal(self.response({"type": "record_learning"}),
                             self.response({"type": "record_learning", "memory_id": self.a.id}),
                             exception=ConflictError)

        self.assertIn("not the Memory this session is on", str(error))
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(self.state(self.a).learn_count, 1)            # step 1 committed
        self.assertEqual(self.state(self.b).learn_count, 0)

    def test_a_mid_step_failure_keeps_the_earlier_action_of_the_same_step(self) -> None:
        error = self.refusal(self.response({"type": "record_learning"},
                                           {"type": "record_learning", "memory_id": self.a.id},
                                           {"type": "record_learning"}),
                             self.response(),
                             exception=ConflictError)

        self.assertEqual(self.model.call_count, 1)                     # the run never reached step 2
        self.assertEqual(self.state(self.a).learn_count, 1)            # A committed
        self.assertEqual(self.state(self.b).learn_count, 0)            # C never ran
        self.assertEqual(self.spy.calls.count("record_learning"), 2)   # A and the failing B only

    def test_no_retry_after_any_failure(self) -> None:
        for index, item in enumerate((TimeoutError("down"),
                                      "not json",
                                      self.response({"type": "delete_memory"}))):
            with self.subTest(case=index):
                with self.assertRaises(Exception):
                    self.perform(self.response({"type": "record_learning"}), item,
                                 self.response())
                self.assertEqual(self.model.call_count, 2)             # the 3rd was never used
                self.assertEqual(len(self.model.responses), 1)

    def test_a_model_error_is_not_wrapped_by_the_loop(self) -> None:
        original = TeacherModelError("fake", "provider down")
        self.refusal(self.response({"type": "record_learning"}), original,
                     exception=TeacherModelError)
        self.assertEqual(self.model.call_count, 2)

    def test_a_service_error_is_not_wrapped_by_the_loop(self) -> None:
        self.service.finish_session(self.session_id)
        try:
            self.service.record_learning(self.session_id)
        except ConflictError as direct:                                # pragma: no cover
            direct_message = str(direct)

        error = self.refusal(self.response({"type": "record_learning"}), exception=ConflictError)
        self.assertEqual(str(error), direct_message)

    def test_an_unknown_session_fails_before_any_model_call(self) -> None:
        error = self.refusal(self.response(), session_id="lrn_missing", exception=NotFoundError)
        self.assertEqual(error.entity, "learning session")
        self.assertEqual(self.model.call_count, 0)

    def test_the_agent_module_has_no_try_except(self) -> None:
        tree = ast.parse(AGENT_SOURCE.read_text(encoding="utf-8"))
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.Try)], [])
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)], [])


# ==========================================================================
# 5. database / engine boundaries
# ==========================================================================

class LoopBoundaryTest(AgentTestCase):
    def test_a_run_does_not_touch_1_0_data(self) -> None:
        before = self.snapshot()
        self.perform(self.response({"type": "record_learning"},
                                   {"type": "record_assessment", "understanding_level": "solid"}),
                     self.response({"type": "record_learning"}),
                     self.response())
        after = self.snapshot()

        self.assertEqual(after["memory_counts"], before["memory_counts"])
        self.assertEqual(after["memory"], before["memory"])
        self.assertEqual(after["source"], before["source"])
        self.assertEqual(after["fts"], before["fts"])
        self.assertEqual(after["search"], before["search"])

    def test_a_run_never_creates_a_second_session(self) -> None:
        before = self.snapshot()["learning_counts"]
        self.perform(self.response({"type": "record_learning"}), self.response())
        self.assertEqual(self.learning.counts()["learning_sessions"], before["learning_sessions"])
        self.assertNotIn("start_session", self.spy.calls)

    def test_a_run_never_modifies_the_session_plan(self) -> None:
        plan_before = self.session().plan
        self.perform(self.response({"type": "record_learning"}),
                     self.response({"type": "record_learning"}), self.response())
        self.assertEqual(self.session().plan, plan_before)
        self.assertEqual(self.session().plan_cursor, 2)

    def test_a_run_only_touches_the_session_it_was_given(self) -> None:
        before = self.snapshot()
        self.perform(self.response({"type": "record_learning"}),
                     self.response({"type": "finish_session", "session_id": self.session_id}))

        self.assertEqual(str(self.session(self.other_session_id).status), "active")
        self.assertEqual(self.learning.get_state(self.stranger.id).as_dict(),
                         before["states"][3])
        self.assertEqual(self.session().id, self.session_id)

    def test_the_loop_makes_no_service_call_of_its_own(self) -> None:
        self.perform(self.response(), self.response())
        self.assertEqual(self.spy.calls, ["get_context", "get_learning_overview", "get_context"])
        self.assertTrue(set(self.spy.calls) <= {"get_context", "get_learning_overview",
                                                *WITHDRAWALS})

    def test_the_loop_asks_the_runtime_not_the_engine_for_state(self) -> None:
        result = self.perform(self.response({"type": "record_learning"}), self.response())
        # 3 reads per step (pre-context, overview, post-context) + the executed writes
        self.assertEqual(len(self.spy.calls),
                         3 * result.steps + len(result.executed_actions))
        self.assertNotIn("start_session", self.spy.calls)

    def test_a_finished_session_cannot_be_resumed_by_the_loop(self) -> None:
        self.perform(self.response({"type": "finish_session", "session_id": self.session_id}))
        error = self.refusal(self.response({"type": "record_learning"}), exception=ConflictError)
        self.assertIn("only an active session", str(error))


# ==========================================================================
# 6. the result object
# ==========================================================================

class AgentResultTest(AgentTestCase):
    def result(self):
        return self.perform(self.response({"type": "record_learning"}), self.response())

    def test_the_result_has_two_stored_fields(self) -> None:
        self.assertEqual([field.name for field in dataclasses.fields(TeacherAgentResult)],
                         ["responses", "context"])

    def test_the_derived_views_are_properties(self) -> None:
        result = self.result()
        for name in ("steps", "final_response", "executed_actions", "stop_reason"):
            self.assertNotIn(name, vars(result), f"{name} must be derived, not stored")
        self.assertEqual(result.steps, len(result.responses))
        self.assertIs(result.final_response, result.responses[-1])

    def test_the_result_is_frozen(self) -> None:
        result = self.result()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.context = None                                     # type: ignore[misc]

    def test_the_result_validates_its_fields(self) -> None:
        good = self.result()
        cases = [
            ({"responses": (), "context": good.context}, ("responses",)),
            ({"responses": ("not a response",), "context": good.context}, ("responses",)),
            ({"responses": "not a sequence", "context": good.context}, ("responses",)),
            ({"responses": good.responses, "context": None}, ("context",)),
        ]
        for kwargs, expected in cases:
            with self.assertRaises(ValidationError) as ctx:
                TeacherAgentResult(**kwargs)
            self.assertEqual(ctx.exception.fields, expected)

    def test_the_result_is_json_serializable(self) -> None:
        dumped = json.dumps(self.result().as_dict(), ensure_ascii=False)
        payload = json.loads(dumped)
        self.assertEqual(len(payload["responses"]), 2)
        self.assertEqual(payload["steps"], 2)
        self.assertEqual(payload["stop_reason"], "no_actions")
        self.assertIn("context", payload)

    def test_the_result_exposes_the_service_context(self) -> None:
        result = self.result()
        self.assertEqual(result.context.session.id, self.session_id)
        self.assertEqual(result.context.memory_ids, (self.a.id, self.b.id, self.c.id))

    def test_every_response_is_the_contract_object(self) -> None:
        result = self.result()
        for response in result.responses:
            self.assertEqual(sorted(response.as_dict()), ["actions", "assistant_message"])


# ==========================================================================
# 7. architecture
# ==========================================================================

class AgentArchitectureTest(unittest.TestCase):
    @property
    def tree(self) -> ast.Module:
        return ast.parse(AGENT_SOURCE.read_text(encoding="utf-8"))

    def imports(self) -> tuple[set[str], set[str]]:
        """(runtime imports, TYPE_CHECKING imports) -- annotations are not dependencies."""
        type_checking_lines = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
                for child in ast.walk(node):
                    if hasattr(child, "lineno"):
                        type_checking_lines.add(child.lineno)
        runtime: set[str] = set()
        deferred: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            else:
                continue
            (deferred if node.lineno in type_checking_lines else runtime).update(names)
        return runtime, deferred

    def test_runtime_imports_stay_minimal(self) -> None:
        runtime, deferred = self.imports()
        self.assertEqual(runtime, {"__future__", "dataclasses", "typing", "errors", "teacher",
                                   "teacher_runtime"})
        self.assertEqual(deferred, {"learning"})

    def test_forbidden_dependencies_are_absent(self) -> None:
        runtime, _ = self.imports()
        for forbidden in ("learning", "learning_store", "db", "sqlite3", "store", "models",
                          "retrieval", "web", "cli", "launcher", "teacher_executor",
                          "teacher_llm", "teacher_provider", "llm", "prompts", "urllib",
                          "requests", "socket", "http"):
            self.assertNotIn(forbidden, runtime, f"teacher_agent.py imports {forbidden}")

    def test_no_database_sql_or_engine_reference(self) -> None:
        source = AGENT_SOURCE.read_text(encoding="utf-8")
        for marker in ("import sqlite3", "sqlite3.", "SELECT ", "INSERT ", "UPDATE ", "DELETE ",
                       "executemany", "commit("):
            self.assertNotIn(marker, source, f"teacher_agent.py contains {marker!r}")

        identifiers = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)}
        for forbidden in ("Database", "LearningRepository", "MemoryRepository", "LearningService",
                          "transaction", "Connection", "connect", "cursor", "execute_sql",
                          "TeacherActionExecutor", "TeacherLLMAdapter", "TeacherProvider",
                          "generate"):
            self.assertNotIn(forbidden, identifiers, f"teacher_agent.py uses {forbidden}")

    def test_the_loop_only_talks_to_the_runtime(self) -> None:
        calls = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
        self.assertEqual(calls & {"turn", "get_context", "get_learning_overview",
                                  "record_learning", "record_assessment", "finish_session",
                                  "abandon_session", "send", "generate", "execute"}, {"turn"})
        self.assertIn("TeacherAgentResult", calls)
        self.assertIn("ValidationError", calls)

    def test_there_is_no_loop_escape_hatch(self) -> None:
        tree = self.tree
        run_fn = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef) and node.name == "run")
        self.assertEqual(len([n for n in ast.walk(run_fn) if isinstance(n, ast.For)]), 1)
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.While)], [])
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.AsyncFor)], [])
        # no recursion: run() never calls itself
        run_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and n.func.attr == "run"]
        self.assertEqual(run_calls, [])

    def test_no_http_provider_or_ui_dependency(self) -> None:
        source = AGENT_SOURCE.read_text(encoding="utf-8")
        for marker in ("api_key", "API_KEY", "Bearer", "https://", "http://", "os.environ",
                       "fastapi", "FastAPI", "Flask", "websocket", "WebSocket", "stream"):
            self.assertNotIn(marker, source, f"teacher_agent.py mentions {marker!r}")

        identifiers = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)}
        for token in ("urllib", "requests", "socket", "httpx", "fastapi", "flask", "openai",
                      "deepseek", "anthropic", "retry"):
            self.assertFalse([name for name in identifiers if token in name.lower()],
                             f"teacher_agent.py references {token!r} in code")

    def test_the_agent_exposes_one_public_method(self) -> None:
        self.assertEqual({name for name in vars(TeacherAgent) if not name.startswith("_")},
                         {"run"})

    def test_the_agent_holds_only_the_runtime(self) -> None:
        class Stub:
            def turn(self, **kwargs):  # pragma: no cover - never called here
                raise AssertionError

        agent = TeacherAgent(Stub())
        for forbidden in ("model", "learning", "service", "repository", "database", "connection",
                          "transaction", "session", "prompt_builder", "provider"):
            self.assertFalse(hasattr(agent, forbidden), forbidden)
        self.assertTrue(hasattr(agent, "runtime"))

    def test_the_agent_refuses_a_broken_runtime(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            TeacherAgent(None)
        self.assertEqual(ctx.exception.fields, ("runtime",))
        with self.assertRaises(ValidationError) as ctx:
            TeacherAgent(object())
        self.assertEqual(ctx.exception.fields, ("runtime",))

    def test_the_dependency_direction_is_one_way(self) -> None:
        for name in ("personal_memory/teacher_runtime.py", "personal_memory/teacher_llm.py",
                     "personal_memory/teacher_executor.py", "personal_memory/teacher.py",
                     "personal_memory/teacher_provider.py", "personal_memory/learning.py",
                     "personal_memory/learning_store.py"):
            tree = ast.parse(pathlib.Path(name).read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[-1] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[-1])
            self.assertNotIn("teacher_agent", imported, f"{name} imports the agent loop")


class ModelCallBudgetTest(AgentTestCase):
    def test_one_model_call_per_step(self) -> None:
        result = self.perform(self.response({"type": "record_learning"}),
                              self.response({"type": "record_learning"}),
                              self.response())
        self.assertEqual(result.steps, 3)
        self.assertEqual(self.model.call_count, result.steps)

    def test_at_most_max_steps_model_calls(self) -> None:
        self.perform(*([self.response({"type": "record_learning"})] * 6), max_steps=2)
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(len(self.model.responses), 4)

    def test_the_model_is_never_called_twice_for_one_step(self) -> None:
        """Assessments never advance the plan, so every budget fits in one shared session."""
        for max_steps in (1, 2, 3):
            with self.subTest(max_steps=max_steps):
                self.perform(*([self.response({"type": "record_assessment",
                                               "understanding_level": "fuzzy"})] * 3),
                             max_steps=max_steps)
                self.assertEqual(self.model.call_count, max_steps)


# ==========================================================================
# 8. regression: the layers below are untouched
# ==========================================================================

class Phase2C6RegressionTest(AgentTestCase):
    def test_the_contract_and_executor_are_unchanged(self) -> None:
        self.assertEqual(len(ALLOWED_ACTION_TYPES), 4)
        self.assertEqual(SUPPORTED_ACTION_TYPES, ALLOWED_ACTION_TYPES)
        self.assertEqual(STOP_ACTION_TYPES, ("finish_session", "abandon_session"))

    def test_the_adapter_and_runtime_apis_are_unchanged(self) -> None:
        self.assertEqual({name for name in vars(TeacherLLMAdapter) if not name.startswith("_")},
                         {"generate"})
        self.assertEqual({name for name in vars(RuntimeClass) if not name.startswith("_")},
                         {"turn"})
        self.assertEqual(TEACHER_PROMPT_VERSION, "teacher_prompt_v1")

    def test_the_provider_api_is_unchanged(self) -> None:
        self.assertEqual({name for name in vars(TeacherProvider) if not name.startswith("_")},
                         {"from_credentials", "generate"})

    def test_the_learning_engine_api_is_unchanged(self) -> None:
        self.assertEqual(len(LEARNING_PUBLIC_API), 8)
        self.assertEqual(LEARNING_PUBLIC_API[-1], "abandon_session")

    def test_no_new_schema(self) -> None:
        from personal_memory.db import MIGRATIONS

        self.assertEqual(SUPPORTED_SCHEMA_VERSION, max(m.version for m in MIGRATIONS))
        with self.database.connection() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        self.assertFalse([name for name in tables if "agent" in name.lower()])

    def test_the_prompt_builders_are_still_plain_prompt_objects(self) -> None:
        for builder in (TeacherPromptBuilder(), TeacherPromptV2Builder()):
            self.assertFalse(hasattr(builder, "generate"))
            self.assertFalse(hasattr(builder, "execute"))

    def test_a_v2_run_behaves_like_a_v1_run(self) -> None:
        """The prompt version changes the model's wording, never the loop semantics."""
        v1 = self.perform(self.response({"type": "record_learning"}), self.response())
        self.agent_for(self.response({"type": "record_learning"}), self.response(),
                       prompt_builder=TeacherPromptV2Builder())
        v2 = self.agent.run(session_id=self.session_id, user_message="我懂了极限",
                            max_steps=DEFAULT_MAX_STEPS)

        self.assertEqual(v1.steps, v2.steps)
        self.assertEqual(v1.stop_reason, v2.stop_reason)
        self.assertEqual([r.action_types for r in v1.responses],
                         [r.action_types for r in v2.responses])
        self.assertIn("Never guess a Memory id", self.model.calls[0]["system_prompt"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
