"""P2D-2: Knowledge Base ↔ Teacher integration tests (offline, real HTTP).

What is locked:

* **product entry** -- a Memory detail page offers "学习这一条"; ``/learn/<memory_id>``
  shows the real state; the POST handlers start/turn through the product entry only
* **state** -- starting really creates a session (in the engine), the header shows the
  current Memory, and after a ``record_learning`` the user's page shows the *updated*
  context (goes out and reads the engine state through the application boundary)
* **errors** -- ConflictError / ValidationError / NotFoundError / TeacherModelError keep
  their meaning: the correct HTTP status, the original message visible, never rendered as
  success, no write, and the user can immediately send the next message
* **safety** -- the web layer never imports/calls ``LearningService`` writes, the agent,
  the runtime, the adapter, the provider, a prompt builder or the executor; no sqlite, no
  SQL, no repository writes; no new table or persisted chat state
"""

from __future__ import annotations

import ast
import json
import pathlib
import unittest

from personal_memory import LearningRepository
from personal_memory.web import WebContext, create_server

from .helpers import example_memory, example_source
from .llm_fakes import FakeTeacherModel
from .test_web import WebTestCase

WEB_DIR = pathlib.Path("personal_memory/web")


def answer(*actions, message="好，我们继续。") -> dict:
    return {"assistant_message": message, "actions": list(actions)}


def payload(text: str) -> str:
    return json.dumps(text if isinstance(text, str) else text, ensure_ascii=False)


class TeacherWebTestCase(WebTestCase):
    """Base: an offline Teacher stack injected into the real HTTP server."""

    prefix = "pms-webteacher-"

    def teacher_context(self, *responses):
        """A web context whose Teacher entry uses a scripted fake model (no network)."""
        context = WebContext.create(self.db_path, config_path=None, quality_check=False)
        context.teacher_model = FakeTeacherModel(*responses)
        self.base = self.start(context)
        return context

    def make_learning_material(self, *, memories=2, linked=True):
        """One Source + N Memories (optionally unlinked), ready for studying."""
        source = self.repo.create_source(example_source(title="高等数学第一章"))
        created = []
        for index in range(memories):
            memory = self.repo.create_memory(
                example_memory(title=f"知识点{index + 1}", content=f"第 {index + 1} 条正文")
            )
            if linked:
                self.repo.link(memory.id, source.id)
            created.append(memory)
        return source, created

    def learning(self) -> LearningRepository:
        return LearningRepository(self.repo.database)

    def active_session(self, source_id):
        return self.learning().get_active_session_for_source(source_id)

    def learn_url(self, memory_id) -> str:
        return f"/learn/{memory_id}"

    def start_learning(self, memory_id):
        return self.post(self.learn_url(memory_id) + "/start", {})

    def send(self, memory_id, message: str, **kwargs):
        return self.post(self.learn_url(memory_id) + "/turn", {"message": message}, **kwargs)


# ==========================================================================
# 1. product entry
# ==========================================================================

class LearnEntryPointTest(TeacherWebTestCase):
    def test_memory_detail_offers_the_learn_entry(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context()

        status, body = self.get(f"/memories/{first.id}")

        self.assertEqual(status, 200)
        self.assertIn("学习这一条", body)
        self.assertIn(f'href="/learn/{first.id}"', body)
        self.assertIn("/memories/", body)                       # existing actions still there

    def test_learn_page_shows_memory_and_source_before_starting(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer()))

        status, body = self.get(self.learn_url(first.id))

        self.assertEqual(status, 200)
        self.assertIn(first.title, body)
        self.assertIn(source.title, body)
        self.assertIn("还没有开始学习", body)
        self.assertIn(f'action="/learn/{first.id}/start"', body)

    def test_learn_page_for_an_unknown_memory_is_a_404(self) -> None:
        self.teacher_context(payload(answer()))
        status, body = self.get("/learn/mem_missing")
        self.assertEqual(status, 404)

    def test_a_memory_without_a_source_explains_itself(self) -> None:
        source, (lonely,) = self.make_learning_material(memories=1, linked=False)
        self.teacher_context(payload(answer()))

        status, body = self.get(self.learn_url(lonely.id))

        self.assertEqual(status, 200)
        self.assertIn("没有关联来源", body)
        self.assertNotIn(f'action="/learn/{lonely.id}/start"', body)

    def test_without_a_model_the_entry_stays_usable_and_says_so(self) -> None:
        source, (first, second) = self.make_learning_material()
        context = WebContext.create(self.db_path, config_path=None, quality_check=False)
        self.base = self.start(context)

        status, body = self.get(self.learn_url(first.id))

        self.assertEqual(status, 200)
        self.assertIn("还不能开始学习", body)
        self.assertNotIn("发送", body)
        self.assertNotIn(f'action="/learn/{first.id}/start"', body)

    def test_the_model_error_is_reported_without_a_stack_trace(self) -> None:
        source, (first, second) = self.make_learning_material()
        context = WebContext.create(self.db_path, config_path=None, quality_check=False)
        context.llm_error = "没有配置密钥"
        self.base = self.start(context)

        status, body = self.get(self.learn_url(first.id))

        self.assertEqual(status, 200)
        self.assertIn("模型未配置", body)
        self.assertNotIn("Traceback", body)


# ==========================================================================
# 2. Flow A -- start learning
# ==========================================================================

class StartLearningTest(TeacherWebTestCase):
    def test_starting_creates_a_real_session_and_shows_it(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer()))

        status, body, url = self.start_learning(first.id)

        self.assertEqual(status, 200)                     # followed the 303 redirect
        self.assertIn("进行中", body)
        self.assertIn("计划 2 条", body)
        self.assertIn(f"当前正在学：<strong>{first.title}</strong>", body)
        session = self.active_session(source.id)
        self.assertIsNotNone(session)
        self.assertEqual(session.current_memory_id, first.id)
        self.assertEqual(list(session.plan), [first.id, second.id])   # clicked memory first

    def test_the_session_is_visible_after_a_page_refresh(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer()))
        self.start_learning(first.id)

        status, body = self.get(self.learn_url(first.id))

        self.assertEqual(status, 200)
        self.assertIn("进行中", body)
        self.assertIn('name="message"', body)             # the chat form is offered

    def test_starting_twice_is_refused_by_the_engine(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer()))
        self.start_learning(first.id)

        status, body, _ = self.start_learning(first.id)

        self.assertEqual(status, 409)
        self.assertIn("already has an active learning session", body)
        self.assertIn("没有被接受", body)   # ConflictError 的真实文案
        self.assertEqual(len(self.learning().list_active_sessions()), 1)

    def test_starting_from_another_memory_of_the_same_source_is_also_refused(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer()))
        self.start_learning(first.id)

        status, body, _ = self.start_learning(second.id)

        self.assertEqual(status, 409)
        self.assertIn("没有被接受", body)   # ConflictError 的真实文案

    def test_starting_a_memory_without_a_source_is_a_400(self) -> None:
        source, (lonely,) = self.make_learning_material(memories=1, linked=False)
        self.teacher_context(payload(answer()))

        status, body, _ = self.start_learning(lonely.id)

        self.assertEqual(status, 400)
        self.assertIn("没有关联来源", body)
        self.assertIsNone(self.active_session(source.id))

    def test_starting_an_unknown_memory_is_a_404(self) -> None:
        self.teacher_context(payload(answer()))
        status, body, _ = self.start_learning("mem_missing")
        self.assertEqual(status, 404)

    def test_starting_does_not_touch_the_memory_itself(self) -> None:
        source, (first, second) = self.make_learning_material()
        before = self.repo.get_memory(first.id).as_dict()
        self.teacher_context(payload(answer()))

        self.start_learning(first.id)

        self.assertEqual(self.repo.get_memory(first.id).as_dict(), before)
        self.assertEqual(self.repo.counts()["memories"], 2)


# ==========================================================================
# 3. Flow B/C -- the Teacher turn and the real learning state
# ==========================================================================

class TurnFlowTest(TeacherWebTestCase):
    def test_a_plain_answer_is_rendered(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer(message="极限讲的是趋近。")), payload(answer()))
        self.start_learning(first.id)

        status, body, _ = self.send(first.id, "给我讲讲这个")

        self.assertEqual(status, 200)
        self.assertIn("卡比的回答", body)
        self.assertIn("极限讲的是趋近。", body)
        self.assertIn("这一轮没有修改学习状态", body)

    def test_empty_messages_never_reach_the_model(self) -> None:
        source, (first, second) = self.make_learning_material()
        context = self.teacher_context(payload(answer(message="不应该被调用")))
        self.start_learning(first.id)

        status, body, _ = self.send(first.id, "   ")

        self.assertEqual(status, 400)
        self.assertIn("请先说点什么", body)
        self.assertNotIn("不应该被调用", body)
        self.assertEqual(context.teacher_model.call_count, 0)

    def test_the_assistant_message_and_actions_are_shown(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(
            payload(answer({"type": "record_learning"}, message="先记一次学习。")),
            payload(answer(message="那我们看下一条。")),
        )
        self.start_learning(first.id)

        status, body, _ = self.send(first.id, "我懂了第一条")

        self.assertEqual(status, 200)
        self.assertIn("先记一次学习。", body)
        self.assertIn("那我们看下一条。", body)
        self.assertIn("这一轮的动作：<strong>记录了一次学习</strong>", body)
        self.assertIn("第 1 步", body)
        self.assertIn("第 2 步", body)                     # a 2-step loop is visible

    def test_record_learning_really_updates_the_engine_state(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer({"type": "record_learning"})), payload(answer()))
        self.start_learning(first.id)

        self.send(first.id, "我懂了第一条")

        state = self.learning().get_state(first.id)
        self.assertEqual(state.learn_count, 1)
        session = self.active_session(source.id)
        self.assertEqual(session.plan_cursor, 1)
        self.assertEqual(session.current_memory_id, second.id)

    def test_the_page_shows_the_updated_context_after_the_turn(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer({"type": "record_learning"})), payload(answer()))
        self.start_learning(first.id)

        status, body, _ = self.send(first.id, "我懂了第一条")

        self.assertEqual(status, 200)
        self.assertIn("进行到第 2 条", body)
        self.assertIn(f"当前正在学：<strong>{second.title}</strong>", body)

    def test_an_assessment_turn_updates_the_level_without_advancing(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(
            payload(answer({"type": "record_assessment", "understanding_level": "partial",
                            "known_aspects": ["趋近"]})),
            payload(answer()),
        )
        self.start_learning(first.id)

        self.send(first.id, "我大概懂一半")

        state = self.learning().get_state(first.id)
        self.assertEqual(str(state.understanding_level), "partial")
        self.assertEqual(list(state.known_aspects), ["趋近"])
        self.assertEqual(self.active_session(source.id).plan_cursor, 0)

    def test_finish_session_action_is_shown_and_ends_the_session(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer({"type": "record_learning"})), payload(answer()))
        self.start_learning(first.id)
        session = self.active_session(source.id)

        # a second context whose model finishes the session
        context = WebContext.create(self.db_path, config_path=None, quality_check=False)
        context.teacher_model = FakeTeacherModel(
            payload(answer({"type": "finish_session", "session_id": session.id},
                           message="今天就到这里。"))
        )
        self.base = self.start(context)

        status, body, _ = self.send(first.id, "今天先结束")

        self.assertEqual(status, 200)
        self.assertIn("结束", body)
        self.assertEqual(str(self.learning().get_session(session.id).status), "completed")

    def test_the_json_view_is_available_for_fetch_clients(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer({"type": "record_learning"})), payload(answer()))
        self.start_learning(first.id)

        status, body, _ = self.send(first.id, "我懂了第一条",
                                    headers={"X-Requested-With": "fetch"})

        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["actions"], ["record_learning"])
        self.assertEqual(data["session"]["status"], "active")
        self.assertEqual(data["session"]["plan_cursor"], 1)
        self.assertEqual(data["current_memory"]["title"], second.title)
        self.assertEqual([entry["learn_count"] for entry in data["learning"]], [1, 0])
        self.assertEqual(data["steps"], 2)

    def test_two_turns_in_a_row_work(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(
            payload(answer({"type": "record_learning"})), payload(answer()),
            payload(answer({"type": "record_learning"})), payload(answer()),
        )
        self.start_learning(first.id)

        self.send(first.id, "第一条我懂了")
        status, body, _ = self.send(first.id, "第二条我也懂了")

        self.assertEqual(status, 200)
        session = self.active_session(source.id)
        self.assertEqual(session.plan_cursor, 2)
        self.assertEqual(self.learning().get_state(second.id).learn_count, 1)


# ==========================================================================
# 4. error handling (P2D-1's real-world failure, rendered honestly)
# ==========================================================================

class TeacherErrorHandlingTest(TeacherWebTestCase):
    def test_a_wrong_memory_id_is_refused_and_never_rendered_as_success(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer({"type": "record_learning"})), payload(answer()),
                             payload(answer({"type": "record_learning",
                                             "memory_id": first.id})),
                             payload(answer()))
        self.start_learning(first.id)
        self.send(first.id, "我懂了第一条")            # now the current Memory is the second one

        status, body, _ = self.send(first.id, "再记录一次第一条")

        self.assertEqual(status, 409)
        self.assertIn("没有被接受", body)
        self.assertIn("not the Memory this session is on", body)     # engine message preserved
        self.assertIn("<!-- teacher error kind: ConflictError -->", body)
        self.assertNotIn("这一轮的动作", body)                       # no success claim
        self.assertEqual(self.learning().get_state(second.id).learn_count, 0)

    def test_the_user_can_send_the_next_message_after_a_conflict(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer({"type": "record_learning"})), payload(answer()),
                             payload(answer({"type": "record_learning", "memory_id": first.id})),
                             payload(answer(message="那我们继续第二条。")))
        self.start_learning(first.id)
        self.send(first.id, "我懂了第一条")
        self.send(first.id, "再记录一次第一条")          # conflict

        status, body, _ = self.send(first.id, "那我们继续")

        self.assertEqual(status, 200)
        self.assertIn("那我们继续第二条。", body)
        self.assertIn('name="message"', body)

    def test_a_conflict_json_view_reports_failure(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer({"type": "record_learning"})), payload(answer()),
                             payload(answer({"type": "record_learning", "memory_id": first.id})))
        self.start_learning(first.id)
        self.send(first.id, "我懂了第一条")

        status, body, _ = self.send(first.id, "再记录一次第一条",
                                    headers={"X-Requested-With": "fetch"})

        self.assertEqual(status, 409)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "ConflictError")
        self.assertIn("没有被接受", data["message"])

    def test_turning_without_an_active_session_is_a_400(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer()))

        status, body, _ = self.send(first.id, "你好")

        self.assertEqual(status, 400)
        self.assertIn("还没有进行中的学习", body)
        self.assertNotIn("卡比的回答", body)

    def test_a_model_error_is_a_502_and_changes_nothing(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(TimeoutError("provider down"))
        self.start_learning(first.id)

        status, body, _ = self.send(first.id, "你好")

        self.assertEqual(status, 502)
        self.assertIn("模型这次没有回答成功", body)
        self.assertIn("<!-- teacher error kind: TeacherModelError -->", body)
        self.assertEqual(self.learning().get_state(first.id).learn_count, 0)
        self.assertEqual(self.active_session(source.id).plan_cursor, 0)

    def test_an_illegal_model_answer_is_a_400_and_changes_nothing(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context("我们继续吧")                 # prose, not the contract's JSON
        self.start_learning(first.id)

        status, body, _ = self.send(first.id, "你好")

        self.assertEqual(status, 400)
        self.assertIn("不满足当前状态的要求", body)
        self.assertIn("did not return valid JSON", body)               # the real reason, verbatim
        self.assertIn("<!-- teacher error kind: ValidationError -->", body)
        self.assertEqual(self.learning().get_state(first.id).learn_count, 0)

    def test_a_failed_turn_keeps_the_earlier_step_effect_visible(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer({"type": "record_learning"})), "not json")
        self.start_learning(first.id)

        status, body, _ = self.send(first.id, "我懂了第一条")

        self.assertEqual(status, 400)
        self.assertEqual(self.learning().get_state(first.id).learn_count, 1)   # step 1 really ran
        self.assertEqual(self.active_session(source.id).plan_cursor, 1)
        self.assertIn("进行到第 2 条", body)                                    # real state shown

    def test_cross_origin_posts_are_still_refused(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer()))
        self.start_learning(first.id)

        status, body, _ = self.post(self.learn_url(first.id) + "/turn", {"message": "你好"},
                                    headers={"Origin": "http://evil.example"})

        self.assertEqual(status, 403)
        self.assertIn("拒绝跨站提交", body)

    def test_an_unknown_route_is_still_a_404(self) -> None:
        self.teacher_context(payload(answer()))
        status, body = self.get("/learn")
        self.assertEqual(status, 404)


# ==========================================================================
# 5. safety: the UI is not an engine
# ==========================================================================

class TeacherUiSafetyTest(unittest.TestCase):
    """The web layer must stay a client of the application boundary."""

    def module(self, name: str) -> ast.Module:
        return ast.parse((WEB_DIR / name).read_text(encoding="utf-8"))

    def identifiers(self, tree: ast.Module) -> set[str]:
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        return names

    def test_the_product_entry_never_uses_the_teacher_core_directly(self) -> None:
        identifiers = self.identifiers(self.module("teacher.py"))
        for forbidden in ("TeacherAgent", "TeacherRuntime", "TeacherLLMAdapter", "TeacherProvider",
                          "TeacherActionExecutor", "TeacherPromptBuilder", "TeacherPromptV2Builder",
                          "execute", "generate", "record_learning", "record_assessment",
                          "finish_session", "abandon_session", "start_session", "get_context",
                          "update_state", "transaction", "sqlite3", "Database"):
            self.assertNotIn(forbidden, identifiers,
                             f"web/teacher.py uses {forbidden}")

    def test_the_product_entry_talks_to_the_application_boundary(self) -> None:
        identifiers = self.identifiers(self.module("teacher.py"))
        self.assertIn("TeacherApplication", identifiers)
        # composition root only: a LearningService is built and handed to the boundary,
        # never called for a learning operation from the product code.
        self.assertIn("LearningService", identifiers)
        self.assertIn("LearningRepository", identifiers)
        self.assertIn("active_session", identifiers)
        self.assertIn("turn", identifiers)
        self.assertIn("start", identifiers)

    def test_the_entry_only_calls_the_application_it_built(self) -> None:
        tree = self.module("teacher.py")
        entry = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.ClassDef) and node.name == "TeacherEntry")
        called = set()
        for node in ast.walk(entry):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called.add(ast.unparse(node.func))
        for forbidden in ("self.context.repository.create_memory", "self.context.repository.link",
                          "self.context.lifecycle.update_memory", "self.learning.start_session",
                          "self.learning.record_learning", "self.learning.get_active_session"):
            self.assertNotIn(forbidden, called, f"TeacherEntry calls {forbidden}")
        self.assertIn("self.application.start", called)
        self.assertIn("self.application.turn", called)
        self.assertIn("self.application.active_session", called)

    def test_the_routes_call_only_the_product_entry(self) -> None:
        tree = self.module("server.py")
        methods = [node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and "learn" in node.name]
        self.assertTrue(methods)
        for method in methods:
            identifiers = self.identifiers(method)
            for forbidden in ("LearningService", "TeacherAgent", "TeacherRuntime",
                              "TeacherLLMAdapter", "TeacherProvider", "execute",
                              "record_learning", "record_assessment", "start_session",
                              "sqlite3", "transaction"):
                self.assertNotIn(forbidden, identifiers,
                                 f"{method.name} uses {forbidden}")
            # every teacher route goes through the product entry (directly or via a helper)
            self.assertTrue({"_teacher_entry", "_learn_snapshot", "_render_learn"} & identifiers,
                            f"{method.name} does not use the product entry")

    def test_no_web_module_executes_a_learning_action(self) -> None:
        """No web module *calls* an engine operation (labels/strings are fine)."""
        for name in ("server.py", "views.py", "teacher.py", "components.py"):
            tree = self.module(name)
            touched = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        touched.add(node.func.attr)
                    elif isinstance(node.func, ast.Name):
                        touched.add(node.func.id)
            for forbidden in ("record_learning", "record_assessment", "finish_session",
                              "abandon_session", "start_session", "update_state",
                              "create_session", "upsert_state"):
                self.assertNotIn(forbidden, touched, f"{name} calls {forbidden}")

    def test_no_web_module_opens_sqlite_directly(self) -> None:
        for name in ("server.py", "teacher.py"):
            tree = self.module(name)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertNotIn("sqlite3", imported)
            source = (WEB_DIR / name).read_text(encoding="utf-8")
            for marker in ("import sqlite3", "sqlite3.connect", "SELECT ", "INSERT ",
                           "UPDATE ", "DELETE "):
                self.assertNotIn(marker, source, f"{name} contains {marker!r}")

    def test_the_entry_uses_only_the_existing_read_apis(self) -> None:
        source = (WEB_DIR / "teacher.py").read_text(encoding="utf-8")
        # reads the product already performs on the memory detail page
        self.assertIn("require_memory", source)
        self.assertIn("get_sources_for_memory", source)
        self.assertIn("get_memories_for_source", source)
        # and no write-side repository method at all
        for forbidden in ("create_memory", "create_source", "update_memory", "delete_memory",
                          "repository.link(", "archive_memory", "restore_memory"):
            self.assertNotIn(forbidden, source, f"web/teacher.py calls {forbidden}")

    def test_the_routes_do_not_ask_the_client_for_a_session_id(self) -> None:
        """The page derives the session from engine state; nothing session-ish is posted."""
        source = (WEB_DIR / "teacher.py").read_text(encoding="utf-8")
        self.assertNotIn('name="session_id"', source)
        self.assertIn('name="message"', source)


# ==========================================================================
# 6. regression
# ==========================================================================

class Phase2D2RegressionTest(TeacherWebTestCase):
    def test_existing_pages_still_work(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer()))

        for path, needle in (("/", "把知识喂给我"), ("/memories", "我的记忆"),
                             ("/search", "搜索"), ("/healthz", "ok")):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertIn(needle, body)

    def test_the_memory_page_keeps_its_lifecycle_actions(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer()))

        status, body = self.get(f"/memories/{first.id}")

        self.assertEqual(status, 200)
        self.assertIn("学习这一条", body)
        self.assertIn(f"/memories/{first.id}/archive", body)

    def test_no_new_table_and_no_chat_persistence(self) -> None:
        from personal_memory.db import MIGRATIONS, SUPPORTED_SCHEMA_VERSION

        self.assertEqual(SUPPORTED_SCHEMA_VERSION, max(migration.version for migration in MIGRATIONS))
        with self.database.connection() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        for forbidden in ("chat", "message", "teacher", "conversation", "turn"):
            self.assertFalse([name for name in tables if forbidden in name.lower()],
                             f"a {forbidden} table was added")
        self.assertEqual({name for name in tables if name.startswith("learning")},
                         {"learning_states", "learning_sessions"})

    def test_a_turn_persists_nothing_about_the_conversation(self) -> None:
        source, (first, second) = self.make_learning_material()
        self.teacher_context(payload(answer({"type": "record_learning"})), payload(answer()))
        self.start_learning(first.id)
        before = self.repo.counts(), self.learning().counts()

        self.send(first.id, "我懂了第一条")

        after = self.repo.counts(), self.learning().counts()
        self.assertEqual(after[0], before[0])                     # 1.0 rows untouched
        self.assertEqual(after[1]["learning_sessions"], before[1]["learning_sessions"])

    def test_the_application_boundary_is_unchanged(self) -> None:
        from personal_memory.teacher_application import TeacherApplication

        public = {name for name in vars(TeacherApplication) if not name.startswith("_")}
        self.assertEqual(public, {"compose", "start", "active_session", "turn"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
