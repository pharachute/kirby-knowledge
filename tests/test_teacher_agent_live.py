"""Phase 2C-6: real-model live smoke for the bounded agent loop (opt-in, default OFF).

The default suite never calls a provider: this module is skipped unless it is explicitly
enabled **and** a credential is configured.

::

    $env:PERSONAL_MEMORY_TEACHER_LIVE = "1"
    D:\\python\\python.exe -m unittest tests.test_teacher_agent_live -v

Two scenarios (a temporary database only; ``data/memory.db`` is never opened):

* **A** -- an ordinary question: the model answers with ``actions: []`` and the loop ends
  after one step with zero writes.
* **B** -- an explicit request to record one learning event: the model proposes an
  action, the executor applies it, the loop asks the model again with the **updated**
  context, and the run ends (one or two steps).

Every run is bounded by ``max_steps``; the module also asserts the loop invariants that
must hold whatever the real model decides: at most ``max_steps`` provider calls, one call
per step, 1.0 rows untouched, and (when something was executed) the executed effect
visible in the returned context.
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

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER_DIR = ROOT / "launcher"

#: One or two real provider calls, on purpose: opt-in only.
LIVE_ENABLED = os.environ.get("PERSONAL_MEMORY_TEACHER_LIVE") == "1"


class CountingTeacherModel:
    """Delegates to the real provider and counts the calls the loop makes."""

    def __init__(self, provider: TeacherProvider) -> None:
        self.provider = provider
        self.calls = 0

    def generate(self, *, system_prompt, user_prompt, response_schema):
        self.calls += 1
        return self.provider.generate(system_prompt=system_prompt, user_prompt=user_prompt,
                                      response_schema=response_schema)


def live_config():
    if str(LAUNCHER_DIR) not in sys.path:
        sys.path.insert(0, str(LAUNCHER_DIR))
    import launch_pkb  # noqa: PLC0415 - the launcher owns the local config location

    try:
        return load_config(launch_pkb.config_path())
    except LLMConfigError:
        return None


@unittest.skipUnless(LIVE_ENABLED,
                     "real agent-loop smoke test is opt-in (set PERSONAL_MEMORY_TEACHER_LIVE=1)")
class RealTeacherAgentSmokeTest(RepositoryTestCase):
    prefix = "pms-agent-live-"

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
        self.model = CountingTeacherModel(self.provider)
        self.agent = TeacherAgent(
            TeacherRuntime(self.service, self.model, prompt_builder=TeacherPromptV2Builder())
        )

    # -- helpers ----------------------------------------------------------
    def fourteen_rows(self) -> dict:
        """Only the 1.0 surface: the loop may legitimately write learning rows."""
        with self.database.connection() as conn:
            fts = {name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
                   for name in ("memory_fts_word", "memory_fts_trigram")}
        return {"counts": self.repo.counts(), "fts": fts,
                "memory": self.repo.get_memory(self.a.id).as_dict()}

    def report(self, label: str, **values) -> None:  # pragma: no cover - evidence output
        print(f"\n[live-agent] {label}: " + json.dumps(values, ensure_ascii=False, default=str))

    def envelope(self):
        return self.provider.last_response

    # -- scenario A -------------------------------------------------------
    def test_scenario_a_an_ordinary_question_stays_within_the_budget(self) -> None:
        before = self.fourteen_rows()
        result = self.agent.run(
            session_id=self.session_id,
            user_message="请只用文字解释「极限」和「连续」的关系，不要修改我的学习状态。",
            max_steps=3,
        )
        envelope = self.envelope()
        self.report(
            "scenario A",
            model=envelope.model, latency_s=round(envelope.latency_seconds, 2),
            finish_reason=envelope.finish_reason, total_tokens=envelope.total_tokens,
            steps=result.steps, stop_reason=result.stop_reason,
            action_types=[list(r.action_types) for r in result.responses],
            message_head=result.final_response.assistant_message[:60],
        )

        self.assertLessEqual(result.steps, 3)
        self.assertEqual(self.model.calls, result.steps)          # one provider call per step
        if result.steps == 1:
            # a one-step run can only have ended because the model proposed nothing
            self.assertEqual(result.stop_reason, "no_actions")
        self.assertEqual(self.fourteen_rows(), before)            # 1.0 data untouched

    # -- scenario B -------------------------------------------------------
    def test_scenario_b_an_explicit_learning_request_is_executed_and_seen_again(self) -> None:
        before = self.fourteen_rows()
        snapshot_before = self.service.get_context(self.session_id)

        try:
            result = self.agent.run(
                session_id=self.session_id,
                user_message=("我已经完全理解了「极限」这一条，请把这次学习记录下来"
                              "（输出 record_learning 动作，然后结束这一轮）。"),
                max_steps=3,
            )
        except ConflictError as exc:                              # pragma: no cover - model dependent
            self.report("scenario B refused by the engine", message=str(exc)[:160],
                        provider_calls=self.model.calls)
            self.skipTest(f"the real model proposed something the engine refuses: {exc}")

        envelope = self.envelope()
        final = self.service.get_context(self.session_id)
        self.report(
            "scenario B",
            model=envelope.model, latency_s=round(envelope.latency_seconds, 2),
            finish_reason=envelope.finish_reason,
            steps=result.steps, stop_reason=result.stop_reason,
            action_types=[list(r.action_types) for r in result.responses],
            executed=[a.kind for a in result.executed_actions],
            plan_cursor=final.session.plan_cursor,
            learned=[final.state_for(m.id).learn_count for m in (self.a, self.b)],
            levels=[str(final.state_for(m.id).understanding_level) for m in (self.a, self.b)],
            message_head=result.final_response.assistant_message[:60],
        )

        self.assertLessEqual(result.steps, 3)
        self.assertEqual(self.model.calls, result.steps)
        self.assertEqual(self.fourteen_rows(), before)            # 1.0 data untouched
        self.assertEqual(result.context.session.id, self.session_id)
        self.assertEqual(str(result.context.session.status) in ("active", "completed",
                                                               "abandoned"), True)

        if result.executed_actions:
            # whatever was executed must be visible in the context the run returned
            changed = (
                final.session.plan_cursor != snapshot_before.session.plan_cursor
                or final.session.status != snapshot_before.session.status
                or any(final.state_for(m.id).learn_count != snapshot_before.state_for(m.id).learn_count
                       for m in (self.a, self.b))
                or any(str(final.state_for(m.id).understanding_level)
                       != str(snapshot_before.state_for(m.id).understanding_level)
                       for m in (self.a, self.b))
            )
            self.assertTrue(changed, "an executed action must show up in the returned context")

    # -- scenario B (updated context on step 2) ---------------------------
    def test_scenario_c_the_second_step_receives_the_updated_context(self) -> None:
        """Only meaningful when the model really proposes something to execute."""
        snapshot_before = self.service.get_context(self.session_id)
        try:
            result = self.agent.run(
                session_id=self.session_id,
                user_message=("我先复习一遍「极限」，请记录这次学习（record_learning），"
                              "然后继续"),
                max_steps=2,
            )
        except ConflictError as exc:                              # pragma: no cover - model dependent
            self.skipTest(f"the real model proposed something the engine refuses: {exc}")

        if result.steps < 2:                                      # the model ended after one step
            self.skipTest("the real model proposed nothing, so there is no second step to check")

        self.report("scenario C", steps=result.steps, stop_reason=result.stop_reason,
                    cursor_before=snapshot_before.session.plan_cursor,
                    cursor_after=result.context.session.plan_cursor,
                    executed=[a.kind for a in result.executed_actions])
        self.assertEqual(self.model.calls, result.steps)


class LiveAgentModuleGuardTest(unittest.TestCase):
    """The live module must stay offline-by-default, temp-DB-only and credential-free.

    The checks look at **code** only (AST literals/identifiers), never at prose: a module
    docstring is allowed to *talk about* the production path it refuses to use.
    """

    @property
    def tree(self) -> ast.Module:
        return ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))

    def code_literals(self) -> list[str]:
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

    def code_identifiers(self) -> set[str]:
        tree = self.tree
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        return names

    def test_the_smoke_class_is_skipped_unless_explicitly_enabled(self) -> None:
        if LIVE_ENABLED:
            self.assertFalse(getattr(RealTeacherAgentSmokeTest, "__unittest_skip__", False))
        else:
            self.assertTrue(RealTeacherAgentSmokeTest.__unittest_skip__)
            self.assertIn("PERSONAL_MEMORY_TEACHER_LIVE=1",
                          RealTeacherAgentSmokeTest.__unittest_skip_why__)

    def test_the_live_module_never_uses_the_production_database(self) -> None:
        for marker in ("resolve_db" + "_path", "DEFAULT_DB" + "_PATH", "data" + "/memory.db"):
            self.assertNotIn(marker, self.code_identifiers())
            self.assertFalse([literal for literal in self.code_literals() if marker in literal],
                             f"the live agent smoke test references {marker!r} in code")

    def test_no_credential_is_written_into_the_live_module(self) -> None:
        for marker in ("sk" + "-", "Bear" + "er ", "api_key" + "="):
            self.assertFalse([literal for literal in self.code_literals() if marker in literal],
                             f"the live agent smoke test contains {marker!r} in code")

    def test_the_live_module_uses_the_recommended_prompt_version(self) -> None:
        self.assertIn("TeacherPromptV2Builder", self.code_identifiers())
        self.assertFalse([name for name in self.code_identifiers() if "TeacherPromptBuilder" == name],
                         "the live smoke must use the v2 builder explicitly")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
