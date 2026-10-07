"""P2D-2: end-to-end smoke through the real product entry (opt-in, default OFF).

::

    $env:PERSONAL_MEMORY_TEACHER_LIVE = "1"
    D:\\python\\python.exe -m unittest tests.test_web_teacher_live -v

E2E-A -- Memory 详情 → 开始学习 → 普通问题：the real provider answers with
          ``actions: []``; one model call, the default (v2) prompt, no learning change.
E2E-B -- the same product path, explicitly asked to record the current Memory: if the real
          model proposes ``record_learning``, the engine updates the state and the page
          shows the updated context.
E2E-C -- a scripted (fake) model returns ``record_learning(memory_id=<wrong>)``: the engine
          refuses it, the page reports the failure with the correct status, nothing is
          written, and the user can send the next message.

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

from personal_memory import LearningRepository
from personal_memory.llm import LLMConfigError, load_config
from personal_memory.web import WebContext

from .helpers import example_memory, example_source
from .llm_fakes import FakeTeacherModel
from .test_web import WebTestCase

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER_DIR = ROOT / "launcher"

LIVE_ENABLED = os.environ.get("PERSONAL_MEMORY_TEACHER_LIVE") == "1"


def live_config_path():
    """The project's own config location (the same helper the launcher uses)."""
    if str(LAUNCHER_DIR) not in sys.path:
        sys.path.insert(0, str(LAUNCHER_DIR))
    import launch_pkb  # noqa: PLC0415 - the launcher owns the local config location

    return launch_pkb.config_path()


def live_config():
    try:
        return load_config(live_config_path())
    except LLMConfigError:
        return None


@unittest.skipUnless(LIVE_ENABLED,
                     "real product E2E is opt-in (set PERSONAL_MEMORY_TEACHER_LIVE=1)")
class RealTeacherProductE2ETest(WebTestCase):
    prefix = "pms-weblive-"

    def setUp(self) -> None:
        super().setUp()
        config = live_config()
        if config is None:
            self.skipTest("no teacher provider credential configured (env or llm.json)")
        self.config = config
        self.source = self.repo.create_source(example_source(title="高等数学第一章"))
        self.first = self.repo.create_memory(
            example_memory(title="极限", content="极限描述的是趋近")
        )
        self.second = self.repo.create_memory(
            example_memory(title="连续", content="连续要求左右极限相等")
        )
        for memory in (self.first, self.second):
            self.repo.link(memory.id, self.source.id)

    # -- helpers ----------------------------------------------------------
    def product_context(self, *, model=None):
        """A real web context: the real provider from the project's config (or a fake)."""
        context = WebContext.create(self.db_path, config_path=live_config_path(),
                                    quality_check=False)
        if model is not None:
            context.teacher_model = model
        self.base = self.start(context)
        return context

    def learning(self) -> LearningRepository:
        return LearningRepository(self.repo.database)

    def session_state(self):
        return self.learning().get_active_session_for_source(self.source.id)

    def report(self, label: str, **values) -> None:  # pragma: no cover - evidence output
        print(f"\n[live-p2d2] {label}: " + json.dumps(values, ensure_ascii=False, default=str))

    def send(self, memory_id, message: str, **kwargs):
        return self.post(f"/learn/{memory_id}/turn", {"message": message}, **kwargs)

    # -- E2E-A ------------------------------------------------------------
    def test_e2e_a_memory_to_start_to_a_plain_answer(self) -> None:
        context = self.product_context()

        status, detail = self.get(f"/memories/{self.first.id}")
        self.assertEqual(status, 200)
        self.assertIn(f'href="/learn/{self.first.id}"', detail)        # product entry exists

        status, page, _ = self.post(f"/learn/{self.first.id}/start", {})
        self.assertEqual(status, 200)
        self.assertIn("进行中", page)

        status, body, _ = self.send(
            self.first.id,
            "请只用文字解释「极限」和「连续」的关系，不要修改我的学习状态。",
            headers={"X-Requested-With": "fetch"},
        )
        data = json.loads(body)
        self.report("E2E-A", model=context.llm_summary().get("model"),
                    prompt_version=context.teacher.application.agent.runtime.adapter
                    .prompt_builder.version,
                    steps=data.get("steps"), actions=data.get("actions"),
                    stop_reason=data.get("stop_reason"),
                    message_head=str(data.get("assistant_message"))[:70])

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["actions"], [])                          # no learning change
        self.assertEqual(data["steps"], 1)                             # one model call
        self.assertTrue(data["assistant_message"].strip())             # a real answer
        self.assertEqual(
            context.teacher.application.agent.runtime.adapter.prompt_builder.version,
            "teacher_prompt_v2",
        )
        self.assertEqual(data["session"]["plan_cursor"], 0)
        self.assertEqual(self.learning().get_state(self.first.id).learn_count, 0)

    # -- E2E-B ------------------------------------------------------------
    def test_e2e_b_a_recorded_learning_event_updates_the_page_state(self) -> None:
        context = self.product_context()
        self.post(f"/learn/{self.first.id}/start", {})

        status, body, _ = self.send(
            self.first.id,
            "我已经复述过极限的定义（x→a 时 f(x)→L）。请记录一次学习：当前这一条就是"
            "「极限」，只输出 record_learning，不要涉及任何其他记忆。",
            headers={"X-Requested-With": "fetch"},
        )
        data = json.loads(body)
        self.report("E2E-B", prompt_version=context.teacher.application.agent.runtime.adapter
                    .prompt_builder.version, steps=data.get("steps"),
                    actions=data.get("actions"), status_code=status,
                    plan_cursor=data.get("session", {}).get("plan_cursor"),
                    current=(data.get("current_memory") or {}).get("title"),
                    learning=data.get("learning"),
                    message_head=str(data.get("assistant_message"))[:70])

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(
            context.teacher.application.agent.runtime.adapter.prompt_builder.version,
            "teacher_prompt_v2",
        )
        if "record_learning" in (data.get("actions") or []):
            self.assertEqual(self.learning().get_state(self.first.id).learn_count, 1)
            self.assertEqual(self.session_state().plan_cursor, 1)
            self.assertEqual((data.get("current_memory") or {}).get("title"), "连续")
            # ...and the *page* shows the updated context too
            status, page = self.get(f"/learn/{self.first.id}")
            self.assertEqual(status, 200)
            self.assertIn("进行到第 2 条", page)
            self.assertIn("当前正在学：<strong>连续</strong>", page)
        else:
            self.assertEqual((data.get("actions") or []), [])
            self.assertEqual(self.learning().get_state(self.first.id).learn_count, 0)
            self.assertEqual(self.session_state().plan_cursor, 0)

    # -- E2E-C ------------------------------------------------------------
    def test_e2e_c_a_wrong_memory_id_is_refused_and_the_user_can_continue(self) -> None:
        """The refusal path, guaranteed by a scripted model (no real call needed)."""
        fake = FakeTeacherModel(
            json.dumps({"assistant_message": "先记录第一条。", "actions": [
                {"type": "record_learning"}]}, ensure_ascii=False),
            json.dumps({"assistant_message": "好的。", "actions": []}, ensure_ascii=False),
            json.dumps({"assistant_message": "再记第一条。", "actions": [
                {"type": "record_learning", "memory_id": self.first.id}]}, ensure_ascii=False),
            json.dumps({"assistant_message": "那我们继续。", "actions": []}, ensure_ascii=False),
        )
        context = self.product_context(model=fake)
        self.post(f"/learn/{self.first.id}/start", {})
        self.send(self.first.id, "我懂了第一条")                       # now current == 连续

        status, body, _ = self.send(self.first.id, "再记录一次第一条",
                                    headers={"X-Requested-With": "fetch"})

        refused = json.loads(body)
        self.report("E2E-C", status_code=status, error=refused.get("error"),
                    message=refused.get("message"),
                    detail=str(refused.get("detail"))[:90],
                    engine_writes=self.learning().get_state(self.second.id).learn_count)

        self.assertEqual(status, 409)
        self.assertFalse(refused["ok"])                                # never a success
        self.assertEqual(refused["error"], "ConflictError")
        self.assertIn("not the Memory this session is on", refused["detail"])
        self.assertEqual(self.learning().get_state(self.second.id).learn_count, 0)

        status, page = self.get(f"/learn/{self.first.id}")
        self.assertEqual(status, 200)
        self.assertIn('name="message"', page)                          # can keep talking
        self.assertIn("进行中", page)                                   # session still active
        self.assertIn("进行到第 2 条", page)                            # real state, not the error

        status, body, _ = self.send(self.first.id, "那我们继续",
                                    headers={"X-Requested-With": "fetch"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])


class LiveProductModuleGuardTest(unittest.TestCase):
    """The live E2E must use the product entry and stay temp-DB-only."""

    def identifiers(self) -> set[str]:
        tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        return names

    def test_the_smoke_class_is_skipped_unless_explicitly_enabled(self) -> None:
        if LIVE_ENABLED:
            self.assertFalse(getattr(RealTeacherProductE2ETest, "__unittest_skip__", False))
        else:
            self.assertTrue(RealTeacherProductE2ETest.__unittest_skip__)
            self.assertIn("PERSONAL_MEMORY_TEACHER_LIVE=1",
                          RealTeacherProductE2ETest.__unittest_skip_why__)

    def test_the_e2e_never_hand_wires_the_teacher_stack(self) -> None:
        identifiers = self.identifiers()
        for forbidden in ("TeacherProvider", "TeacherRuntime", "TeacherAgent",
                          "TeacherLLMAdapter", "TeacherActionExecutor", "TeacherPromptBuilder",
                          "LearningService"):
            self.assertNotIn(forbidden, identifiers, f"the E2E module names {forbidden}")

    def test_the_e2e_never_uses_the_production_database(self) -> None:
        for marker in ("resolve_db" + "_path", "DEFAULT_DB" + "_PATH"):
            self.assertNotIn(marker, self.identifiers())

    def test_no_credential_is_written_into_the_e2e_module(self) -> None:
        tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
        literals = [node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        for marker in ("sk" + "-", "Bear" + "er ", "api_key" + "="):
            self.assertFalse([literal for literal in literals if marker in literal])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
