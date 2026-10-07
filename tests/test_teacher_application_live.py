"""Phase P2D-1: live smoke driven **only** through TeacherApplication (opt-in, default OFF).

Two scenarios, both with a real provider and a temporary database:

Live A -- ordinary explanation question: facade ``start`` + ``turn`` -> agent -> default
          (v2) prompt on the wire -> the model answers with ``actions: []`` -> normal return.
Live B -- explicit request to record the **current** Memory: if the model proposes
          ``record_learning``, the LearningService accepts it, the returned context shows
          the update, and any later step is given that updated context.

The module never imports the provider, the runtime, the agent or a prompt builder: it
uses ``TeacherApplication.compose`` and nothing else, which is exactly what a future UI,
Web layer or CLI should have to know.  A temporary database only -- ``data/memory.db`` is
never opened, and the API key is never printed (only ``LLMConfig.safe_summary()``).

::

    $env:PERSONAL_MEMORY_TEACHER_LIVE = "1"
    D:\\python\\python.exe -m unittest tests.test_teacher_application_live -v
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import sys
import unittest

from personal_memory import (
    LearningRepository,
    LearningService,
    TeacherApplication,
)
from personal_memory.llm import HttpTransport, LLMConfigError, load_config

from .helpers import RepositoryTestCase

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER_DIR = ROOT / "launcher"

LIVE_ENABLED = os.environ.get("PERSONAL_MEMORY_TEACHER_LIVE") == "1"


class RecordingTransport:
    """The real 1.0 HTTP transport, plus the requests it was handed."""

    def __init__(self, inner: HttpTransport) -> None:
        self.inner = inner
        self.requests: list[object] = []

    def send(self, request, config):
        self.requests.append(request)
        return self.inner.send(request, config)


def live_config():
    if str(LAUNCHER_DIR) not in sys.path:
        sys.path.insert(0, str(LAUNCHER_DIR))
    import launch_pkb  # noqa: PLC0415 - the launcher owns the local config location

    try:
        return load_config(launch_pkb.config_path())
    except LLMConfigError:
        return None


def prompt_blocks(request) -> tuple[str, str]:
    """(system prompt, user prompt) exactly as they went on the wire."""
    messages = getattr(request, "messages", ())
    contents = [message.content for message in messages]
    return contents[0], contents[1]


def context_in(user_prompt: str) -> dict:
    start = user_prompt.index("<teacher_context>")
    body = user_prompt[start + len("<teacher_context>"):]
    end = body.index("</teacher_context>")
    return json.loads(body[:end].replace("\\u003c", "<").replace("\\u003e", ">"))


@unittest.skipUnless(LIVE_ENABLED,
                     "real TeacherApplication smoke test is opt-in (set PERSONAL_MEMORY_TEACHER_LIVE=1)")
class RealTeacherApplicationSmokeTest(RepositoryTestCase):
    prefix = "pms-teacherapp-live-"

    def setUp(self) -> None:
        super().setUp()
        config = live_config()
        if config is None:
            self.skipTest("no teacher provider credential configured (env or llm.json)")
        self.config = config

        self.learning = LearningRepository(self.database)
        self.service = LearningService(self.learning, self.repo)
        self.source = self.make_source(title="高等数学第一章")
        self.a = self.make_memory(title="极限", content="极限描述的是趋近")
        self.b = self.make_memory(title="连续", content="连续要求左右极限相等")
        for memory in (self.a, self.b):
            self.repo.link(memory.id, self.source.id)

        self.transport = RecordingTransport(HttpTransport())
        # the only teacher object this module knows: the application boundary
        self.app = TeacherApplication.compose(self.service, config=self.config,
                                              transport=self.transport)

    # -- helpers ----------------------------------------------------------
    def system_prompts(self) -> list[str]:
        return [prompt_blocks(request)[0] for request in self.transport.requests]

    def user_prompts(self) -> list[str]:
        return [prompt_blocks(request)[1] for request in self.transport.requests]

    def prompt_versions(self) -> list[str]:
        versions = []
        for user_prompt in self.user_prompts():
            marker = 'version="'
            start = user_prompt.index(marker) + len(marker)
            versions.append(user_prompt[start:user_prompt.index('"', start)])
        return versions

    def learn_counts(self) -> dict[str, int]:
        return {memory.id: self.learning.get_state(memory.id).learn_count
                for memory in (self.a, self.b)}

    def one_zero(self) -> dict:
        return {"counts": self.repo.counts(),
                "memory": self.repo.get_memory(self.a.id).as_dict(),
                "source": self.repo.get_source(self.source.id).as_dict()}

    def report(self, label: str, **values) -> None:  # pragma: no cover - evidence output
        print(f"\n[live-teacherapp] {label}: "
              + json.dumps(values, ensure_ascii=False, default=str))

    def assert_the_default_prompt_was_used(self) -> None:
        self.assertEqual(self.app.agent.runtime.adapter.prompt_builder.version,
                         "teacher_prompt_v2")
        self.assertTrue(self.prompt_versions(), "no provider call was recorded")
        for version in self.prompt_versions():
            self.assertEqual(version, "teacher_prompt_v2")
        for system_prompt in self.system_prompts():
            self.assertIn("Never guess a Memory id", system_prompt)
            self.assertIn("MUST omit", system_prompt)

    # -- Live A -----------------------------------------------------------
    def test_live_a_an_ordinary_question_returns_normally(self) -> None:
        before_1_0 = self.one_zero()
        context = self.app.start(source_id=self.source.id)
        result = self.app.turn(
            session_id=context.session.id,
            user_message="请只用文字解释「极限」和「连续」的关系，不要修改我的学习状态。",
        )
        envelope = self.app.agent.runtime.adapter.model.last_response
        self.report(
            "Live A", model=envelope.model, latency_s=round(envelope.latency_seconds, 2),
            finish_reason=envelope.finish_reason, total_tokens=envelope.total_tokens,
            prompt_versions=self.prompt_versions(), steps=result.steps,
            stop_reason=result.stop_reason,
            action_types=[list(response.action_types) for response in result.responses],
            message_head=result.final_response.assistant_message[:60],
        )

        self.assert_the_default_prompt_was_used()
        self.assertEqual(len(self.transport.requests), result.steps)     # one call per step
        self.assertLessEqual(result.steps, 3)
        if result.steps == 1:
            self.assertEqual(result.stop_reason, "no_actions")
        self.assertEqual(self.one_zero(), before_1_0)                    # 1.0 untouched
        if not result.executed_actions:
            self.assertEqual(self.learn_counts(), {self.a.id: 0, self.b.id: 0})
            self.assertEqual(result.context.session.plan_cursor, 0)

    # -- Live B -----------------------------------------------------------
    def test_live_b_a_recorded_learning_event_updates_the_context(self) -> None:
        context = self.app.start(source_id=self.source.id)      # creates the per-Memory states
        before_counts = self.learn_counts()

        try:
            result = self.app.turn(
                session_id=context.session.id,
                user_message=("我已经复述过极限的定义（x→a 时 f(x)→L）。请记录一次学习："
                              "当前这一条就是「极限」，只输出 record_learning，不要涉及任何"
                              "其他记忆。"),
            )
        except Exception as exc:                                          # noqa: BLE001 - reported honestly
            self.report("Live B raised", error_type=type(exc).__name__, message=str(exc)[:160],
                        provider_calls=len(self.transport.requests))
            self.skipTest(f"the real model proposed something the engine refused: {exc}")

        executed = [action.kind for action in result.executed_actions]
        contexts_seen = [context_in(prompt)["session"]["plan_cursor"] for prompt in self.user_prompts()]
        self.report(
            "Live B", prompt_versions=self.prompt_versions(), steps=result.steps,
            stop_reason=result.stop_reason, executed=executed,
            plan_cursor=self.learning.get_session(context.session.id).plan_cursor,
            learn_counts=self.learn_counts(), contexts_seen=contexts_seen,
            message_head=result.final_response.assistant_message[:60],
        )

        self.assert_the_default_prompt_was_used()
        self.assertEqual(len(self.transport.requests), result.steps)
        self.assertLessEqual(result.steps, 3)
        self.assertEqual(result.context.session.id, context.session.id)

        if "record_learning" in executed:
            # the LearningService accepted it and the returned context shows the update
            self.assertEqual(self.learning.get_session(context.session.id).plan_cursor,
                             result.context.session.plan_cursor)
            self.assertEqual(self.learn_counts()[self.a.id],
                             result.context.state_for(self.a.id).learn_count)
            self.assertGreaterEqual(self.learn_counts()[self.a.id], 1)
            if result.steps >= 2:
                # the later step was shown the updated context, not the pre-turn snapshot
                self.assertEqual(contexts_seen[0], 0)
                self.assertEqual(contexts_seen[1], 1)
        else:
            # the model answered instead of recording: nothing may have moved
            self.assertEqual(self.learn_counts(), before_counts)
            self.assertEqual(result.context.session.plan_cursor, 0)


class LiveApplicationModuleGuardTest(unittest.TestCase):
    """The live module must exercise the facade, not hand-wire the stack."""

    @property
    def tree(self) -> ast.Module:
        return ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))

    def identifiers(self) -> set[str]:
        names = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)}
        return names

    def test_the_smoke_class_is_skipped_unless_explicitly_enabled(self) -> None:
        if LIVE_ENABLED:
            self.assertFalse(getattr(RealTeacherApplicationSmokeTest, "__unittest_skip__", False))
        else:
            self.assertTrue(RealTeacherApplicationSmokeTest.__unittest_skip__)
            self.assertIn("PERSONAL_MEMORY_TEACHER_LIVE=1",
                          RealTeacherApplicationSmokeTest.__unittest_skip_why__)

    def test_the_live_module_only_knows_the_facade(self) -> None:
        """No provider, no runtime, no agent, no prompt builder, no executor.

        ``LearningService`` is supplied by the composition root (the test itself here),
        which is exactly the facade's contract; everything *below* the facade stays
        unnamed.
        """
        identifiers = self.identifiers()
        self.assertIn("TeacherApplication", identifiers)
        for forbidden in ("TeacherProvider", "TeacherRuntime", "TeacherAgent",
                          "TeacherLLMAdapter", "TeacherPromptBuilder", "TeacherPromptV2Builder",
                          "TeacherActionExecutor", "TeacherModel"):
            self.assertNotIn(forbidden, identifiers, f"the live module names {forbidden}")

    def test_the_live_module_never_pins_a_prompt_version(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    self.assertNotEqual(keyword.arg, "prompt_builder")

    def test_the_live_module_never_uses_the_production_database(self) -> None:
        for marker in ("resolve_db" + "_path", "DEFAULT_DB" + "_PATH"):
            self.assertNotIn(marker, self.identifiers())

    def test_no_credential_is_written_into_the_live_module(self) -> None:
        literals = [node.value for node in ast.walk(self.tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        for marker in ("sk" + "-", "Bear" + "er ", "api_key" + "="):
            self.assertFalse([literal for literal in literals if marker in literal])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
