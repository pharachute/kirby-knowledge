"""Phase P2D-1: TeacherApplication (application boundary) tests.

What is locked:

* the facade's API is exactly four names -- ``compose``, ``start``, ``active_session``,
  ``turn`` -- and an instance holds only ``learning`` + ``agent`` (no provider, no
  adapter, no prompt builder, no database, no state)
* every method is pure delegation: session creation is ``LearningService``'s, the turn
  sequence is ``TeacherAgent``'s, and the caller gets those layers' own objects back
  (no second result model, no copied fields)
* no business rule is re-implemented: active/completed/abandoned, current Memory, plan
  cursor, assessment and ``memory_id`` rules stay in the engine
* no error is caught, wrapped, retried or rolled back; the module has no ``try``
* architecture: no DB/SQL/repository, no HTTP/provider SDK, no UI, and the prompt
  builder is not even imported
"""

from __future__ import annotations

import ast
import json
import pathlib
import unittest

from personal_memory import (
    ALLOWED_ACTION_TYPES,
    DEFAULT_PROMPT_BUILDER,
    DEFAULT_MAX_STEPS,
    LearningRepository,
    LearningService,
    TeacherAgent,
    TeacherAgentResult,
    TeacherApplication,
    TeacherPromptV2Builder,
    TeacherProvider,
    TeacherRuntime,
)
from personal_memory.errors import ConflictError, NotFoundError, ValidationError
from personal_memory.learning import PUBLIC_API as LEARNING_PUBLIC_API
from personal_memory.llm import LLMConfig, LLMConfigError, LLMResponse
from personal_memory.retrieval import MemoryRetriever
from personal_memory.teacher_executor import SUPPORTED_ACTION_TYPES
from personal_memory.teacher_llm import TeacherLLMAdapter, TeacherModelError

from .helpers import RepositoryTestCase, make_temp_dir, remove_temp_dir
from .llm_fakes import FakeTeacherModel, ScriptedTransport

APPLICATION_SOURCE = pathlib.Path("personal_memory/teacher_application.py")

FAKE_KEY = "test-key-not-a-real-credential"
BASE_URL = "https://api.example.invalid/v1"


def test_config(**overrides) -> LLMConfig:
    values = {"provider": "custom", "model": "test-model", "base_url": BASE_URL,
              "api_key": FAKE_KEY, "timeout_seconds": 30.0, "max_tokens": 256}
    values.update(overrides)
    return LLMConfig(**values)


class StubLearning:
    """Records what the facade asked a LearningService to do (no database)."""

    def __init__(self, context=None, active=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.context = context
        self.active = active

    def start_session(self, *, source_id, memory_ids=None):
        self.calls.append(("start_session", {"source_id": source_id, "memory_ids": memory_ids}))
        return self.context

    def get_active_session(self, source_id):
        self.calls.append(("get_active_session", {"source_id": source_id}))
        return self.active


class StubAgent:
    """Records what the facade asked an agent to run; returns a sentinel or raises."""

    def __init__(self, result=None, error=None) -> None:
        self.calls: list[dict] = []
        self.result = result if result is not None else object()
        self.error = error

    def run(self, *, session_id, user_message, max_steps):
        self.calls.append({"session_id": session_id, "user_message": user_message,
                           "max_steps": max_steps})
        if self.error is not None:
            raise self.error
        return self.result


class RecordingLearningService:
    """Delegates to the real service and records the exact call chain."""

    def __init__(self, real: LearningService) -> None:
        self.real = real
        self.calls: list[str] = []

    def start_session(self, *, source_id, memory_ids=None):
        self.calls.append("start_session")
        return self.real.start_session(source_id=source_id, memory_ids=memory_ids)

    def get_active_session(self, source_id):
        self.calls.append("get_active_session")
        return self.real.get_active_session(source_id)

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


class ApplicationTestCase(RepositoryTestCase):
    """Source A (2 Memories) + Source B (1 Memory), no session started yet."""

    prefix = "pms-teacherapp-"

    def setUp(self) -> None:
        super().setUp()
        self.learning = LearningRepository(self.database)
        self.service = LearningService(self.learning, self.repo)
        self.source = self.make_source(title="高等数学第一章")
        self.a = self.make_memory(title="极限", content="极限描述的是趋近")
        self.b = self.make_memory(title="连续", content="连续要求左右极限相等")
        for memory in (self.a, self.b):
            self.repo.link(memory.id, self.source.id)

        self.other_source = self.make_source(title="另一份材料")
        self.stranger = self.make_memory(title="别人家的记忆")
        self.repo.link(self.stranger.id, self.other_source.id)

        self.model = None
        self.app = None

    # -- helpers ----------------------------------------------------------
    def response(self, *actions, message="继续吧") -> dict:
        return {"assistant_message": message, "actions": list(actions)}

    def app_for(self, *responses, learning=None):
        """A full facade over the real engine, with a scripted model (offline)."""
        self.model = FakeTeacherModel(*responses)
        self.app = TeacherApplication.compose(learning or self.service, model=self.model)
        return self.model, self.app

    def start(self, app=None, **kwargs):
        app = self.app if app is None else app
        return app.start(source_id=kwargs.pop("source_id", self.source.id), **kwargs)

    def session(self, session_id):
        return self.learning.get_session(session_id)

    def state(self, memory):
        return self.learning.get_state(memory.id)

    def snapshot(self) -> dict:
        with self.database.connection() as conn:
            fts = {name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
                   for name in ("memory_fts_word", "memory_fts_trigram")}
        return {
            "memory_counts": self.repo.counts(),
            "learning_counts": self.learning.counts(),
            "fts": fts,
            "memory": self.repo.get_memory(self.a.id).as_dict(),
            "source": self.repo.get_source(self.source.id).as_dict(),
            "search": [(hit.memory.id, hit.score)
                       for hit in MemoryRetriever(self.repo).search("极限").hits],
        }

    def assert_no_write(self, before: dict) -> None:
        after = self.snapshot()
        self.assertEqual(after["memory_counts"], before["memory_counts"])
        self.assertEqual(after["memory"], before["memory"])
        self.assertEqual(after["source"], before["source"])
        self.assertEqual(after["fts"], before["fts"])
        self.assertEqual(after["search"], before["search"])


# ==========================================================================
# 1. the API surface
# ==========================================================================

class BoundaryApiTest(ApplicationTestCase):
    def test_the_minimal_construction_works(self) -> None:
        _, app = self.app_for(self.response())
        self.assertIsInstance(app, TeacherApplication)
        self.assertIs(app.learning, self.service)
        self.assertIsInstance(app.agent, TeacherAgent)

    def test_the_facade_accepts_any_object_that_can_run_and_serve_sessions(self) -> None:
        app = TeacherApplication(StubLearning(context="ctx"), StubAgent(result="result"))
        self.assertEqual(app.start(source_id="src_x"), "ctx")
        self.assertEqual(app.turn(session_id="lrn_x", user_message="hi"), "result")

    def test_a_missing_learning_service_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            TeacherApplication(None, StubAgent())
        self.assertEqual(ctx.exception.fields, ("learning",))

    def test_a_missing_agent_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            TeacherApplication(StubLearning(), None)
        self.assertEqual(ctx.exception.fields, ("agent",))

    def test_an_agent_without_run_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            TeacherApplication(StubLearning(), object())
        self.assertEqual(ctx.exception.fields, ("agent",))

    def test_a_learning_service_without_the_needed_methods_is_refused(self) -> None:
        class Half:
            def start_session(self, **kwargs):  # pragma: no cover - never called
                raise AssertionError

        with self.assertRaises(ValidationError) as ctx:
            TeacherApplication(Half(), StubAgent())
        self.assertEqual(ctx.exception.fields, ("learning",))

        class OtherHalf:
            def get_active_session(self, source_id):  # pragma: no cover - never called
                raise AssertionError

        with self.assertRaises(ValidationError) as ctx:
            TeacherApplication(OtherHalf(), StubAgent())
        self.assertEqual(ctx.exception.fields, ("learning",))

    def test_the_public_api_is_exactly_four_names(self) -> None:
        public = {name for name in vars(TeacherApplication) if not name.startswith("_")}
        self.assertEqual(public, {"compose", "start", "active_session", "turn"})

    def test_the_instance_holds_only_the_service_and_the_agent(self) -> None:
        _, app = self.app_for(self.response())
        self.assertEqual(set(vars(app)), {"learning", "agent"})
        for forbidden in ("provider", "adapter", "model", "prompt_builder", "runtime",
                          "repository", "database", "connection", "session", "result",
                          "history", "state", "transport", "config"):
            self.assertFalse(hasattr(app, forbidden), forbidden)

    def test_the_facade_hides_the_wiring_it_built(self) -> None:
        app = TeacherApplication.compose(self.service, config=test_config(),
                                        transport=ScriptedTransport([]))
        self.assertIsInstance(app.agent, TeacherAgent)
        self.assertFalse(hasattr(app, "provider"))
        self.assertFalse(hasattr(app, "model"))

    def test_the_facade_is_stateless_between_calls(self) -> None:
        model, app = self.app_for(self.response({"type": "record_learning"}), self.response(),
                                  self.response({"type": "record_learning"}), self.response())
        context = self.start()

        app.turn(session_id=context.session.id, user_message="第一轮")
        attributes_after_first = dict(vars(app))
        app.turn(session_id=context.session.id, user_message="第二轮")

        self.assertEqual(set(vars(app)), set(attributes_after_first))
        self.assertEqual(model.call_count, 4)              # 2 steps per turn, nothing cached


class ComposeTest(ApplicationTestCase):
    def test_compose_with_a_model_builds_the_default_stack(self) -> None:
        model, app = self.app_for(self.response())
        self.assertIsInstance(app.agent.runtime, TeacherRuntime)
        self.assertIsInstance(app.agent.runtime.adapter, TeacherLLMAdapter)
        self.assertIs(app.agent.runtime.adapter.model, model)

    def test_compose_uses_the_default_prompt_version_without_naming_it(self) -> None:
        _, app = self.app_for(self.response())
        self.assertEqual(app.agent.runtime.adapter.prompt_builder.version, "teacher_prompt_v2")
        self.assertIsInstance(app.agent.runtime.adapter.prompt_builder, TeacherPromptV2Builder)
        self.assertIs(DEFAULT_PROMPT_BUILDER, TeacherPromptV2Builder)

    def test_compose_with_a_config_and_a_fake_transport_is_offline(self) -> None:
        transport = ScriptedTransport([LLMResponse(text=json.dumps(self.response()), model="m")])
        app = TeacherApplication.compose(self.service, config=test_config(), transport=transport)
        context = self.start(app)

        result = app.turn(session_id=context.session.id, user_message="你好")

        self.assertIsInstance(result, TeacherAgentResult)
        self.assertEqual(transport.call_count, 1)          # one provider call for one step
        self.assertEqual(result.steps, 1)

    def test_compose_with_a_config_path_uses_the_project_config_mechanism(self) -> None:
        directory = make_temp_dir("pms-teacher-app-")
        try:
            path = directory / "llm.json"
            path.write_text(json.dumps({"provider": "custom", "model": "from-file",
                                        "base_url": BASE_URL, "api_key": FAKE_KEY,
                                        "timeout_seconds": 5, "max_tokens": 64}),
                            encoding="utf-8")
            transport = ScriptedTransport([LLMResponse(text=json.dumps(self.response()),
                                                       model="from-file")])
            app = TeacherApplication.compose(self.service, config_path=path, transport=transport)
            self.assertEqual(app.agent.runtime.adapter.model.config.model, "from-file")
            self.assertEqual(app.agent.runtime.adapter.model.config.timeout_seconds, 5.0)
            self.assertNotIn(FAKE_KEY, repr(app.agent.runtime.adapter.model))
        finally:
            remove_temp_dir(directory)

    def test_compose_prefers_an_explicit_model_over_a_config(self) -> None:
        model = FakeTeacherModel(self.response())
        app = TeacherApplication.compose(self.service, model=model,
                                        config_path="does-not-exist-llm.json")
        self.assertIs(app.agent.runtime.adapter.model, model)

    def test_compose_without_a_credential_is_a_config_error(self) -> None:
        with self.assertRaises(LLMConfigError):
            TeacherApplication.compose(self.service, config=test_config(api_key=""))

    def test_compose_with_a_missing_config_file_is_a_config_error(self) -> None:
        with self.assertRaises(LLMConfigError):
            TeacherApplication.compose(self.service, config_path="does-not-exist-llm.json")

    def test_compose_does_not_leak_the_key(self) -> None:
        with self.assertRaises(LLMConfigError) as ctx:
            TeacherApplication.compose(self.service, config=test_config(api_key=""))
        self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_compose_builds_the_real_provider_when_no_model_is_given(self) -> None:
        app = TeacherApplication.compose(self.service, config=test_config(),
                                        transport=ScriptedTransport([]))
        self.assertIsInstance(app.agent.runtime.adapter.model, TeacherProvider)
        self.assertIsInstance(app.agent.runtime.adapter.model.transport, ScriptedTransport)
        self.assertIs(app.agent.runtime.adapter.model.config.api_key, FAKE_KEY)

    def test_compose_does_not_touch_the_database(self) -> None:
        before = self.snapshot()
        app = TeacherApplication.compose(self.service, config=test_config(),
                                        transport=ScriptedTransport([]))
        self.assertIsInstance(app, TeacherApplication)
        self.assertEqual(self.learning.counts()["learning_sessions"], 0)
        self.assert_no_write(before)


# ==========================================================================
# 2. orchestration: sessions and turns
# ==========================================================================

class SessionDelegationTest(ApplicationTestCase):
    def test_start_returns_the_services_own_context(self) -> None:
        _, app = self.app_for(self.response())

        context = app.start(source_id=self.source.id)

        direct = self.service.get_context(context.session.id)
        self.assertEqual(context.session.id, direct.session.id)
        self.assertEqual(context.session.source_id, self.source.id)
        self.assertEqual(list(context.session.plan), [self.a.id, self.b.id])
        self.assertEqual(context.memory_ids, (self.a.id, self.b.id))
        self.assertEqual(str(context.session.status), "active")
        self.assertEqual(len(context.states), 2)                  # the engine created them

    def test_start_creates_exactly_one_session(self) -> None:
        _, app = self.app_for(self.response())
        app.start(source_id=self.source.id)
        self.assertEqual(self.learning.counts()["learning_sessions"], 1)

    def test_start_delegates_the_arguments_verbatim(self) -> None:
        stub = StubLearning(context="ctx")
        app = TeacherApplication(stub, StubAgent())

        app.start(source_id="src_1")
        app.start(source_id="src_2", memory_ids=["mem_b", "mem_a"])

        self.assertEqual(stub.calls, [
            ("start_session", {"source_id": "src_1", "memory_ids": None}),
            ("start_session", {"source_id": "src_2", "memory_ids": ["mem_b", "mem_a"]}),
        ])

    def test_start_preserves_an_explicit_memory_order(self) -> None:
        _, app = self.app_for(self.response())
        context = app.start(source_id=self.source.id, memory_ids=[self.b.id, self.a.id])
        self.assertEqual(list(context.session.plan), [self.b.id, self.a.id])

    def test_a_second_start_on_the_same_source_is_the_engines_conflict(self) -> None:
        _, app = self.app_for(self.response())
        app.start(source_id=self.source.id)

        with self.assertRaises(ConflictError) as ctx:
            app.start(source_id=self.source.id)
        self.assertIn("already has an active learning session", str(ctx.exception))
        self.assertEqual(self.learning.counts()["learning_sessions"], 1)

    def test_start_on_an_unknown_source_is_the_engines_not_found(self) -> None:
        _, app = self.app_for(self.response())
        with self.assertRaises(NotFoundError):
            app.start(source_id="src_missing")

    def test_start_on_an_empty_source_is_the_engines_validation_error(self) -> None:
        empty = self.make_source(title="空材料")
        _, app = self.app_for(self.response())
        with self.assertRaises(ValidationError):
            app.start(source_id=empty.id)

    def test_start_with_a_memory_of_another_source_is_the_engines_refusal(self) -> None:
        _, app = self.app_for(self.response())
        with self.assertRaises(ValidationError):
            app.start(source_id=self.source.id, memory_ids=[self.stranger.id])

    def test_active_session_delegates(self) -> None:
        stub = StubLearning(active="session")
        app = TeacherApplication(stub, StubAgent())
        self.assertEqual(app.active_session("src_1"), "session")
        self.assertEqual(stub.calls, [("get_active_session", {"source_id": "src_1"})])

    def test_active_session_is_none_before_start_and_the_session_after(self) -> None:
        _, app = self.app_for(self.response())
        self.assertIsNone(app.active_session(self.source.id))

        context = app.start(source_id=self.source.id)

        active = app.active_session(self.source.id)
        self.assertIsNotNone(active)
        self.assertEqual(active.id, context.session.id)
        self.assertEqual(active.id, self.service.get_active_session(self.source.id).id)

    def test_active_session_follows_the_engines_lifecycle(self) -> None:
        _, app = self.app_for(self.response())
        context = app.start(source_id=self.source.id)

        self.service.finish_session(context.session.id)

        self.assertIsNone(app.active_session(self.source.id))
        self.assertIsNone(self.service.get_active_session(self.source.id))

    def test_start_resumes_nothing_by_itself(self) -> None:
        """No hidden 'start or resume' policy: the caller asks, the engine decides."""
        _, app = self.app_for(self.response())
        first = app.start(source_id=self.source.id)
        with self.assertRaises(ConflictError):
            app.start(source_id=self.source.id)
        self.assertEqual(app.active_session(self.source.id).id, first.session.id)


class TurnDelegationTest(ApplicationTestCase):
    def test_turn_calls_the_agent_with_the_documented_arguments(self) -> None:
        agent = StubAgent(result="sentinel")
        app = TeacherApplication(StubLearning(), agent)

        result = app.turn(session_id="lrn_x", user_message="你好")

        self.assertIs(result, "sentinel")
        self.assertEqual(agent.calls, [{"session_id": "lrn_x", "user_message": "你好",
                                        "max_steps": DEFAULT_MAX_STEPS}])

    def test_turn_returns_the_agents_result_object_unchanged(self) -> None:
        sentinel = object()
        app = TeacherApplication(StubLearning(), StubAgent(result=sentinel))
        self.assertIs(app.turn(session_id="lrn_x", user_message="你好"), sentinel)
    def test_turn_forwards_max_steps_verbatim(self) -> None:
        agent = StubAgent()
        app = TeacherApplication(StubLearning(), agent)
        app.turn(session_id="lrn_x", user_message="你好", max_steps=1)
        self.assertEqual(agent.calls[0]["max_steps"], 1)

    def test_turn_uses_the_agents_own_default_budget(self) -> None:
        agent = StubAgent()
        app = TeacherApplication(StubLearning(), agent)
        app.turn(session_id="lrn_x", user_message="你好")
        self.assertEqual(agent.calls[0]["max_steps"], DEFAULT_MAX_STEPS)
        self.assertEqual(DEFAULT_MAX_STEPS, 3)

    def test_a_full_turn_runs_through_the_agent_and_the_runtime(self) -> None:
        spy = RecordingLearningService(self.service)
        model, app = self.app_for(self.response({"type": "record_learning"}),
                                  self.response(), learning=spy)
        context = self.start(app)

        result = app.turn(session_id=context.session.id, user_message="我懂了极限")

        self.assertIsInstance(result, TeacherAgentResult)
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.stop_reason, "no_actions")
        self.assertEqual(result.final_response.action_types, ())
        self.assertEqual([action.kind for action in result.executed_actions], ["record_learning"])
        self.assertEqual(spy.calls, ["start_session", "get_context", "get_learning_overview",
                                     "record_learning", "get_context",
                                     "get_context", "get_learning_overview", "get_context"])
        self.assertEqual(model.call_count, 2)

    def test_an_empty_action_turn_ends_after_one_step(self) -> None:
        model, app = self.app_for(self.response(message="那我们继续看连续。"))
        context = self.start(app)

        result = app.turn(session_id=context.session.id, user_message="你好")

        self.assertEqual(result.steps, 1)
        self.assertEqual(result.stop_reason, "no_actions")
        self.assertEqual(result.final_response.assistant_message, "那我们继续看连续。")
        self.assertEqual(model.call_count, 1)

    def test_the_default_prompt_version_is_used_on_the_wire(self) -> None:
        model, app = self.app_for(self.response(), self.response({"type": "record_learning"}))
        context = self.start(app)
        app.turn(session_id=context.session.id, user_message="我懂了极限")

        for call in model.calls:
            self.assertIn('version="teacher_prompt_v2"', call["user_prompt"])
            self.assertIn("Never guess a Memory id", call["system_prompt"])

    def test_a_later_step_sees_the_updated_context(self) -> None:
        model, app = self.app_for(self.response({"type": "record_learning"}), self.response())
        context = self.start(app)

        result = app.turn(session_id=context.session.id, user_message="我懂了极限")

        def block(text: str, tag: str) -> str:
            start = text.index(f"<{tag}") + len(f"<{tag}") + 2
            end = text.index(f"\n</{tag}>", start)
            return text[start:end]

        first = json.loads(block(model.calls[0]["user_prompt"], "teacher_context"))
        second = json.loads(block(model.calls[1]["user_prompt"], "teacher_context"))
        self.assertEqual(first["session"]["plan_cursor"], 0)
        self.assertEqual(second["session"]["plan_cursor"], 1)
        self.assertEqual(second["current_memory"]["id"], self.b.id)
        self.assertEqual(result.context.session.plan_cursor, 1)
        self.assertEqual(self.state(self.a).learn_count, 1)

    def test_the_facade_does_not_apply_the_session_lifecycle_itself(self) -> None:
        """Gating stays in the engine: the facade neither pre-checks nor post-fixes."""
        _, bootstrap = self.app_for(self.response())
        context = bootstrap.start(source_id=self.source.id)

        model = FakeTeacherModel(self.response({"type": "finish_session",
                                                "session_id": context.session.id}))
        app = TeacherApplication.compose(self.service, model=model)

        result = app.turn(session_id=context.session.id, user_message="结束")

        self.assertEqual(result.stop_reason, "session_ended")
        self.assertEqual(str(result.context.session.status), "completed")
        self.assertEqual(str(self.learning.get_session(context.session.id).status), "completed")

    def test_two_turns_are_independent_calls(self) -> None:
        model, app = self.app_for(self.response({"type": "record_learning"}), self.response(),
                                  self.response({"type": "record_learning"}), self.response())
        context = self.start(app)

        first = app.turn(session_id=context.session.id, user_message="第一轮")
        second = app.turn(session_id=context.session.id, user_message="第二轮")

        self.assertEqual(first.context.session.plan_cursor, 1)
        self.assertEqual(second.context.session.plan_cursor, 2)
        self.assertEqual(model.call_count, 4)

    def test_an_invalid_max_steps_is_refused_by_the_agent(self) -> None:
        model, app = self.app_for(self.response())
        context = self.start(app)

        with self.assertRaises(ValidationError) as ctx:
            app.turn(session_id=context.session.id, user_message="你好", max_steps=0)

        self.assertEqual(ctx.exception.fields, ("max_steps",))
        self.assertEqual(model.call_count, 0)


# ==========================================================================
# 3. error propagation
# ==========================================================================

class ErrorPropagationTest(ApplicationTestCase):
    def test_a_conflict_from_start_is_not_wrapped(self) -> None:
        _, app = self.app_for(self.response())
        app.start(source_id=self.source.id)
        try:
            self.service.start_session(source_id=self.source.id)
        except ConflictError as direct:
            direct_message = str(direct)

        with self.assertRaises(ConflictError) as ctx:
            app.start(source_id=self.source.id)
        self.assertEqual(str(ctx.exception), direct_message)

    def test_a_not_found_from_start_is_not_wrapped(self) -> None:
        _, app = self.app_for(self.response())
        with self.assertRaises(NotFoundError) as ctx:
            app.start(source_id="src_missing")
        self.assertIn("source not found", str(ctx.exception))

    def test_a_not_found_from_turn_is_not_wrapped_and_calls_no_model(self) -> None:
        model, app = self.app_for(self.response())
        with self.assertRaises(NotFoundError) as ctx:
            app.turn(session_id="lrn_missing", user_message="你好")
        self.assertEqual(ctx.exception.entity, "learning session")
        self.assertEqual(model.call_count, 0)

    def test_a_conflict_from_turn_is_the_engines_conflict(self) -> None:
        _, app = self.app_for(self.response())
        context = self.start(app)
        self.service.finish_session(context.session.id)

        model = FakeTeacherModel(self.response({"type": "record_learning"}))
        app = TeacherApplication.compose(self.service, model=model)
        with self.assertRaises(ConflictError) as ctx:
            app.turn(session_id=context.session.id, user_message="继续")

        self.assertIn("only an active session", str(ctx.exception))
        self.assertEqual(self.state(self.a).learn_count, 0)

    def test_a_model_error_travels_unchanged(self) -> None:
        original = TeacherModelError("fake-provider", "provider unavailable")
        _, app = self.app_for(original)
        context = self.start(app)

        with self.assertRaises(TeacherModelError) as ctx:
            app.turn(session_id=context.session.id, user_message="你好")
        self.assertIs(ctx.exception, original)

    def test_an_illegal_model_output_is_a_validation_error(self) -> None:
        _, app = self.app_for("我们继续吧")
        context = self.start(app)

        with self.assertRaises(ValidationError) as ctx:
            app.turn(session_id=context.session.id, user_message="你好")
        self.assertEqual(ctx.exception.fields, ("model_output",))

    def test_an_agent_error_travels_unchanged(self) -> None:
        error = RuntimeError("a bug in a custom agent")
        app = TeacherApplication(StubLearning(), StubAgent(error=error))
        with self.assertRaises(RuntimeError) as ctx:
            app.turn(session_id="lrn_x", user_message="你好")
        self.assertIs(ctx.exception, error)

    def test_no_retry_after_a_model_error(self) -> None:
        model, app = self.app_for(TimeoutError("down"), self.response())
        context = self.start(app)

        with self.assertRaises(TeacherModelError):
            app.turn(session_id=context.session.id, user_message="你好")

        self.assertEqual(model.call_count, 1)
        self.assertEqual(len(model.responses), 1)          # the good answer was never used

    def test_no_rollback_when_a_later_step_fails(self) -> None:
        model, app = self.app_for(self.response({"type": "record_learning"}), "not json")
        context = self.start(app)

        with self.assertRaises(ValidationError):
            app.turn(session_id=context.session.id, user_message="你好")

        self.assertEqual(model.call_count, 2)
        self.assertEqual(self.learning.get_session(context.session.id).plan_cursor, 1)
        self.assertEqual(self.state(self.a).learn_count, 1)      # step 1 stayed committed

    def test_a_read_only_run_writes_nothing(self) -> None:
        before = self.snapshot()
        _, app = self.app_for(self.response(message="只是聊聊。"))
        context = self.start(app)

        app.turn(session_id=context.session.id, user_message="你好")

        self.assert_no_write(before)

    def test_a_failing_run_writes_nothing(self) -> None:
        before = self.snapshot()
        _, app = self.app_for("not json")
        context = self.start(app)

        with self.assertRaises(ValidationError):
            app.turn(session_id=context.session.id, user_message="你好")

        self.assert_no_write(before)

    def test_the_module_never_catches_anything(self) -> None:
        tree = ast.parse(APPLICATION_SOURCE.read_text(encoding="utf-8"))
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.Try)], [])
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)], [])
        # the only raises are the facade's own argument checks -- never a re-raise
        raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
        self.assertTrue(raises)
        for node in raises:
            self.assertIsInstance(node.exc, ast.Call)
            self.assertEqual(ast.unparse(node.exc.func), "ValidationError")


# ==========================================================================
# 4. architecture
# ==========================================================================

class ApplicationArchitectureTest(unittest.TestCase):
    @property
    def tree(self) -> ast.Module:
        return ast.parse(APPLICATION_SOURCE.read_text(encoding="utf-8"))

    def imports(self) -> tuple[set[str], set[str]]:
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

    def code_identifiers(self) -> set[str]:
        """Names and attributes used in code (docstrings are prose, not dependencies)."""
        names = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)}
        return names

    def code_literals(self) -> list[str]:
        """String literals in code, with docstrings excluded."""
        tree = self.tree
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    docstrings.add(id(body[0].value))
        return [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings]

    def test_runtime_imports_are_only_what_the_boundary_needs(self) -> None:
        runtime, deferred = self.imports()
        self.assertEqual(runtime, {"__future__", "typing", "errors", "llm", "teacher_agent",
                                   "teacher_runtime", "teacher_provider"})
        self.assertEqual(deferred, {"learning", "teacher_llm"})

    def test_no_database_sql_or_store_dependency(self) -> None:
        runtime, deferred = self.imports()
        for forbidden in ("db", "sqlite3", "store", "models", "retrieval", "learning_store"):
            self.assertNotIn(forbidden, runtime | deferred,
                             f"teacher_application.py imports {forbidden}")

        for marker in ("import sqlite3", "sqlite3.", "SELECT ", "INSERT ", "UPDATE ", "DELETE ",
                       "executemany", "commit("):
            self.assertFalse([literal for literal in self.code_literals() if marker in literal],
                             f"teacher_application.py contains {marker!r} in code")

        for forbidden in ("Database", "LearningRepository", "MemoryRepository", "transaction",
                          "Connection", "connect", "cursor", "execute_sql", "sqlite3"):
            self.assertNotIn(forbidden, self.code_identifiers(),
                             f"teacher_application.py uses {forbidden}")

    def test_no_prompt_builder_dependency(self) -> None:
        """The prompt version stays a decision of the runtime (P2C-7)."""
        identifiers = self.code_identifiers()
        for name in ("TeacherPromptBuilder", "TeacherPromptV2Builder", "PromptPayload",
                     "TEACHER_PROMPT_VERSION", "TEACHER_PROMPT_VERSION_V2", "_SYSTEM_PROMPT",
                     "prompt_builder", "system_prompt_template", "build"):
            self.assertNotIn(name, identifiers, f"teacher_application.py uses {name}")
        for marker in ("TeacherPromptBuilder", "TeacherPromptV2Builder", "PromptPayload",
                       "TEACHER_PROMPT_VERSION", "_SYSTEM_PROMPT", "prompt_builder"):
            self.assertFalse([literal for literal in self.code_literals() if marker in literal],
                             f"teacher_application.py mentions {marker!r} in code")

    def test_no_adapter_or_executor_call(self) -> None:
        calls = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
        for forbidden in ("generate", "execute", "execute_many", "preview", "dry_run",
                          "record_learning", "record_assessment", "finish_session",
                          "abandon_session", "get_context", "get_learning_overview"):
            self.assertNotIn(forbidden, calls, f"teacher_application.py calls {forbidden}")

    def test_the_only_calls_are_the_three_delegations_and_the_composition(self) -> None:
        calls = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
        self.assertTrue({"start_session", "get_active_session", "run", "load_config",
                         "TeacherProvider", "TeacherRuntime", "TeacherAgent",
                         "ValidationError"} <= calls)

    def test_there_is_no_loop_in_the_delegating_methods(self) -> None:
        """The only ``for`` in the module is the two-name dependency check in ``__init__``."""
        tree = self.tree
        for method_name in ("start", "active_session", "turn", "compose"):
            method = next(node for node in ast.walk(tree)
                          if isinstance(node, ast.FunctionDef) and node.name == method_name)
            self.assertEqual([n for n in ast.walk(method) if isinstance(n, ast.For)], [],
                             f"{method_name} iterates over something")
            self.assertEqual([n for n in ast.walk(method) if isinstance(n, ast.While)], [],
                             f"{method_name} loops")
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.While)], [])
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.AsyncFor)], [])
        self.assertEqual(len([n for n in ast.walk(tree) if isinstance(n, ast.For)]), 1)

    def test_no_http_provider_sdk_or_ui_dependency(self) -> None:
        for marker in ("api_key", "API_KEY", "Bearer", "https://", "http://", "os.environ",
                       "fastapi", "FastAPI", "Flask", "websocket", "WebSocket", "stream",
                       "sqlite", "requests", "urllib", "http.client"):
            self.assertFalse([literal for literal in self.code_literals() if marker in literal],
                             f"teacher_application.py mentions {marker!r} in code")

        for token in ("urllib", "requests", "socket", "httpx", "aiohttp", "fastapi", "flask",
                      "websocket", "openai", "deepseek", "anthropic", "retry", "rollback"):
            self.assertFalse([name for name in self.code_identifiers() if token in name.lower()],
                             f"teacher_application.py references {token!r} in code")

    def test_the_dependency_direction_is_one_way(self) -> None:
        for name in ("personal_memory/learning.py", "personal_memory/learning_store.py",
                     "personal_memory/teacher.py", "personal_memory/teacher_executor.py",
                     "personal_memory/teacher_llm.py", "personal_memory/teacher_runtime.py",
                     "personal_memory/teacher_agent.py", "personal_memory/teacher_provider.py"):
            tree = ast.parse(pathlib.Path(name).read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[-1] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[-1])
            self.assertNotIn("teacher_application", imported, f"{name} imports the facade")


# ==========================================================================
# 5. regression: nothing below moved
# ==========================================================================

class Phase2D1RegressionTest(ApplicationTestCase):
    def test_the_teacher_layers_are_unchanged(self) -> None:
        self.assertEqual(len(ALLOWED_ACTION_TYPES), 4)
        self.assertEqual(SUPPORTED_ACTION_TYPES, ALLOWED_ACTION_TYPES)
        self.assertEqual({name for name in vars(TeacherAgent) if not name.startswith("_")},
                         {"run"})
        self.assertEqual({name for name in vars(TeacherRuntime) if not name.startswith("_")},
                         {"turn"})
        self.assertEqual({name for name in vars(TeacherLLMAdapter) if not name.startswith("_")},
                         {"generate"})
        self.assertEqual({name for name in vars(TeacherProvider) if not name.startswith("_")},
                         {"from_credentials", "generate"})
        self.assertIs(DEFAULT_PROMPT_BUILDER, TeacherPromptV2Builder)

    def test_the_learning_service_api_is_unchanged(self) -> None:
        self.assertEqual(len(LEARNING_PUBLIC_API), 8)
        self.assertEqual(LEARNING_PUBLIC_API[-1], "abandon_session")
        self.assertNotIn("turn", LEARNING_PUBLIC_API)

    def test_no_new_schema_and_no_new_persistence(self) -> None:
        from personal_memory.db import MIGRATIONS, SUPPORTED_SCHEMA_VERSION

        self.assertEqual(SUPPORTED_SCHEMA_VERSION, max(m.version for m in MIGRATIONS))
        with self.database.connection() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
            columns: set[str] = set()
            for table in ("learning_states", "learning_sessions", "sources", "memories"):
                columns |= {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        self.assertFalse([name for name in tables if "application" in name.lower()])
        self.assertFalse([name for name in tables if "teacher" in name.lower()])
        self.assertNotIn("prompt_version", columns)

    def test_a_facade_driven_run_is_a_normal_engine_run(self) -> None:
        """The facade adds no semantics: the same action gives the same engine outcome."""
        model, app = self.app_for(self.response({"type": "record_learning"}), self.response())
        context = self.start(app)
        via_facade = app.turn(session_id=context.session.id, user_message="我懂了极限")

        direct = self.service.record_learning(context.session.id)

        self.assertEqual(via_facade.context.session.plan_cursor + 1,
                         direct.session.plan_cursor)
        self.assertEqual(direct.state_for(self.b.id).learn_count, 1)

    def test_the_facade_holds_the_service_it_was_given(self) -> None:
        _, app = self.app_for(self.response())
        self.assertIs(app.learning, self.service)
        self.assertIs(app.agent.runtime.executor.learning, self.service)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
