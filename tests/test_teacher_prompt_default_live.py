"""Phase 2C-7: live smoke for the **default** prompt version (opt-in, default OFF).

Unlike the P2C-6 live smoke (which pinned ``TeacherPromptV2Builder`` explicitly), this
module never passes ``prompt_builder=``: it exercises what a caller gets *by default*,
which since P2C-7 is ``teacher_prompt_v2``.

::

    $env:PERSONAL_MEMORY_TEACHER_LIVE = "1"
    D:\\python\\python.exe -m unittest tests.test_teacher_prompt_default_live -v

Live A -- ordinary explanation question: default runtime -> v2 on the wire -> model
          answers -> ``actions: []`` -> 1 step -> ``stop_reason=no_actions``.
Live B -- explicit request to record the **current** Memory: if the model proposes
          ``record_learning``, the LearningService accepts it and the returned context
          shows the update.
Live C -- the wrong-``memory_id`` case with two Memories in context: whatever the real
          model does, no non-current Memory may ever advance -- and the deterministic
          half proves the same with a wrong id through the very same default (v2)
          runtime, so no prompt logic can bypass the engine gate.

A temporary database only; ``data/memory.db`` is never opened; the API key is never
printed (only ``LLMConfig.safe_summary()``).
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
    TeacherAgent,
    TeacherPromptV2Builder,
    TeacherProvider,
    TeacherRuntime,
)
from personal_memory.errors import ConflictError
from personal_memory.llm import LLMConfigError, load_config

from .helpers import RepositoryTestCase
from .llm_fakes import FakeTeacherModel

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER_DIR = ROOT / "launcher"

LIVE_ENABLED = os.environ.get("PERSONAL_MEMORY_TEACHER_LIVE") == "1"


class RecordingTeacherModel:
    """The real provider plus the observation this smoke needs.

    Counts calls and keeps every system prompt it was asked to send, so the test can
    assert *from the wire* which prompt version the default path really used.
    """

    def __init__(self, provider: TeacherProvider) -> None:
        self.provider = provider
        self.calls = 0
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    def generate(self, *, system_prompt, user_prompt, response_schema):
        self.calls += 1
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        return self.provider.generate(system_prompt=system_prompt, user_prompt=user_prompt,
                                      response_schema=response_schema)

    @property
    def prompt_versions(self) -> list[str]:
        versions = []
        for user_prompt in self.user_prompts:
            marker = 'version="'
            start = user_prompt.index(marker) + len(marker)
            versions.append(user_prompt[start:user_prompt.index('"', start)])
        return versions


def live_config():
    if str(LAUNCHER_DIR) not in sys.path:
        sys.path.insert(0, str(LAUNCHER_DIR))
    import launch_pkb  # noqa: PLC0415 - the launcher owns the local config location

    try:
        return load_config(launch_pkb.config_path())
    except LLMConfigError:
        return None


@unittest.skipUnless(LIVE_ENABLED,
                     "real prompt-default smoke test is opt-in (set PERSONAL_MEMORY_TEACHER_LIVE=1)")
class RealPromptDefaultSmokeTest(RepositoryTestCase):
    prefix = "pms-promptdefault-live-"

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
        self.session_id = self.service.start_session(source_id=self.source.id).session.id

        self.provider = TeacherProvider(self.config)
        self.model = RecordingTeacherModel(self.provider)
        self.runtime = TeacherRuntime(self.service, self.model)          # ← no prompt_builder
        self.agent = TeacherAgent(self.runtime)

    # -- helpers ----------------------------------------------------------
    def learn_counts(self) -> dict[str, int]:
        return {memory.id: self.learning.get_state(memory.id).learn_count
                for memory in (self.a, self.b)}

    def cursor(self) -> int:
        return self.learning.get_session(self.session_id).plan_cursor

    def report(self, label: str, **values) -> None:  # pragma: no cover - evidence output
        print(f"\n[live-prompt-default] {label}: " + json.dumps(values, ensure_ascii=False,
                                                               default=str))

    def assert_default_is_v2(self) -> None:
        self.assertEqual(self.runtime.adapter.prompt_builder.version, "teacher_prompt_v2")
        self.assertIsInstance(self.runtime.adapter.prompt_builder, TeacherPromptV2Builder)
        for version in self.model.prompt_versions:
            self.assertEqual(version, "teacher_prompt_v2")
        for system_prompt in self.model.system_prompts:
            self.assertIn("Never guess a Memory id", system_prompt)
            self.assertIn("MUST omit", system_prompt)

    # -- Live A -----------------------------------------------------------
    def test_live_a_default_runtime_uses_v2_and_answers_without_actions(self) -> None:
        before = self.learn_counts()
        result = self.agent.run(
            session_id=self.session_id,
            user_message="请只用文字解释「极限」和「连续」的关系，不要修改我的学习状态。",
            max_steps=3,
        )
        envelope = self.provider.last_response
        self.report(
            "Live A", model=envelope.model, latency_s=round(envelope.latency_seconds, 2),
            finish_reason=envelope.finish_reason, total_tokens=envelope.total_tokens,
            prompt_versions=self.model.prompt_versions, steps=result.steps,
            stop_reason=result.stop_reason,
            action_types=[list(r.action_types) for r in result.responses],
            message_head=result.final_response.assistant_message[:60],
        )

        self.assert_default_is_v2()
        self.assertEqual(self.model.calls, result.steps)
        self.assertLessEqual(result.steps, 3)
        if result.steps == 1:
            self.assertEqual(result.stop_reason, "no_actions")
        self.assertEqual(self.learn_counts(), before)
        self.assertEqual(self.cursor(), 0)

    # -- Live B -----------------------------------------------------------
    def test_live_b_a_recorded_learning_event_lands_on_the_current_memory(self) -> None:
        before = self.learn_counts()
        try:
            result = self.agent.run(
                session_id=self.session_id,
                user_message=("我已经完全理解了「极限」这一条，请把这次学习记录下来"
                              "（输出 record_learning 动作），然后结束这一轮。"),
                max_steps=2,
            )
        except ConflictError as exc:                                  # pragma: no cover - model dependent
            self.report("Live B refused by the engine", message=str(exc)[:160])
            self.skipTest(f"the real model proposed something the engine refuses: {exc}")

        executed = [action.kind for action in result.executed_actions]
        self.report(
            "Live B", prompt_versions=self.model.prompt_versions, steps=result.steps,
            stop_reason=result.stop_reason, executed=executed,
            learn_counts=self.learn_counts(), cursor=self.cursor(),
            message_head=result.final_response.assistant_message[:60],
        )

        self.assert_default_is_v2()
        self.assertEqual(self.model.calls, result.steps)
        self.assertLessEqual(result.steps, 2)

        if "record_learning" in executed:
            # accepted by the LearningService and visible in the returned context
            self.assertEqual(self.cursor(), result.context.session.plan_cursor)
            self.assertGreaterEqual(self.learn_counts()[self.a.id], 1)
            self.assertEqual(result.context.state_for(self.a.id).learn_count,
                             self.learn_counts()[self.a.id])
        else:
            # the model chose to answer instead of recording: no learning row may move
            self.assertEqual(self.learn_counts(), before)
            self.assertEqual(self.cursor(), 0)

    # -- Live C -----------------------------------------------------------
    def test_live_c_a_wrong_memory_id_cannot_bypass_the_engine_gate(self) -> None:
        """Two Memories in context; only the *current* one may ever advance."""
        before = self.learn_counts()
        before_cursor = self.cursor()
        observed = "no_action"
        try:
            result = self.agent.run(
                session_id=self.session_id,
                user_message=("请记录我对「连续」这条记忆的学习"
                              "（如果你认为应当记录，就输出 record_learning）。"),
                max_steps=2,
            )
        except ConflictError as exc:
            observed = f"refused by the engine: {str(exc)[:90]}"
            result = None

        self.report("Live C (real model)", prompt_versions=self.model.prompt_versions,
                    observed=observed,
                    learn_counts=self.learn_counts(), cursor=self.cursor())

        # the gate invariant, whatever the real model decided:
        #  * the current Memory (a) may have advanced
        #  * the non-current Memory (b) must never have advanced
        self.assertGreaterEqual(self.learn_counts()[self.a.id], before[self.a.id])
        self.assertEqual(self.learn_counts()[self.b.id], before[self.b.id])
        if result is not None:
            executed = [action.kind for action in result.executed_actions]
            if "record_learning" in executed:
                self.assertGreaterEqual(self.learn_counts()[self.a.id], 1)
                self.assertGreater(self.cursor(), before_cursor)
            else:
                self.assertEqual(self.cursor(), before_cursor)

        # deterministic half: a wrong memory_id through the *same* default (v2) runtime
        counts_before_scripted = self.learn_counts()
        cursor_before_scripted = self.cursor()
        scripted = FakeTeacherModel(json.dumps({
            "assistant_message": "记录一下连续。",
            "actions": [{"type": "record_learning", "memory_id": self.b.id}],
        }, ensure_ascii=False))
        default_runtime = TeacherRuntime(self.service, scripted)      # default = v2
        with self.assertRaises(ConflictError) as ctx:
            default_runtime.turn(session_id=self.session_id, user_message="请记录连续")

        self.assertIn("not the Memory this session is on", str(ctx.exception))
        self.report("Live C (scripted wrong id)", error=str(ctx.exception)[:110],
                    learn_counts=self.learn_counts(), cursor=self.cursor())
        # the refused action wrote nothing at all
        self.assertEqual(self.learn_counts(), counts_before_scripted)
        self.assertEqual(self.cursor(), cursor_before_scripted)


class LivePromptDefaultGuardTest(unittest.TestCase):
    """The live module must use the *default* path and stay temp-DB-only."""

    @property
    def tree(self) -> ast.Module:
        return ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))

    def test_the_smoke_class_is_skipped_unless_explicitly_enabled(self) -> None:
        if LIVE_ENABLED:
            self.assertFalse(getattr(RealPromptDefaultSmokeTest, "__unittest_skip__", False))
        else:
            self.assertTrue(RealPromptDefaultSmokeTest.__unittest_skip__)
            self.assertIn("PERSONAL_MEMORY_TEACHER_LIVE=1",
                          RealPromptDefaultSmokeTest.__unittest_skip_why__)

    def test_the_live_module_never_pins_a_prompt_version(self) -> None:
        """The whole point: every runtime here must use the default resolution."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    self.assertNotEqual(keyword.arg, "prompt_builder",
                                        "the prompt-default smoke must not pin a builder")

    def test_the_live_module_never_uses_the_production_database(self) -> None:
        identifiers = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)}
        for marker in ("resolve_db" + "_path", "DEFAULT_DB" + "_PATH"):
            self.assertNotIn(marker, identifiers)

    def test_no_credential_is_written_into_the_live_module(self) -> None:
        tree = self.tree
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    docstrings.add(id(body[0].value))
        literals = [node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings]
        for marker in ("sk" + "-", "Bear" + "er ", "api_key" + "="):
            self.assertFalse([literal for literal in literals if marker in literal])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
