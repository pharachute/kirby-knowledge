"""P2D-3: Knowledge Base 1.1 final acceptance (freeze criteria, offline, real HTTP).

Every test here is the *product* path, not an internal API test:

* the real startup chain's server objects (``WebContext`` + ``create_server``, exactly what
  ``launcher -> cli -> web`` builds) on a temporary database
* the user journey: /memories -> /memories/<id> -> 「学习这一条」 -> start -> 对话 ->
  learning action -> refresh -> continue
* **persistence**: a learning action survives a *server restart* (new context, new server,
  new model object) because it lives in SQLite, not in process memory
* recovery: an active session is resumed (never a second session); failures (wrong
  ``memory_id``, model error, invalid JSON) never corrupt the session and the next message
  still works
* 1.1 freeze facts: schema v3, prompt v2 by default, no new table/column/chat history
"""

from __future__ import annotations

import ast
import json
import pathlib
import unittest

from personal_memory import LearningRepository
from personal_memory.db import MIGRATIONS, SUPPORTED_SCHEMA_VERSION
from personal_memory.learning import PUBLIC_API as LEARNING_PUBLIC_API
from personal_memory.teacher_llm import (
    TEACHER_PROMPT_VERSION,
    TEACHER_PROMPT_VERSION_V2,
    TeacherPromptBuilder,
    TeacherPromptV2Builder,
)
from personal_memory.teacher_agent import DEFAULT_MAX_STEPS
from personal_memory.web import WebContext

from .helpers import example_memory, example_source
from .llm_fakes import FakeTeacherModel
from .test_web import WebTestCase

WEB_DIR = pathlib.Path("personal_memory/web")


def answer(*actions, message="好，我们继续。") -> dict:
    return {"assistant_message": message, "actions": list(actions)}


def payload(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class FreezeTestCase(WebTestCase):
    """Shared fixture: one Source + two Memories in a temporary database."""

    prefix = "pms-freeze-"

    def setUp(self) -> None:
        super().setUp()
        self.source = self.repo.create_source(example_source(title="高等数学第一章"))
        self.first = self.repo.create_memory(example_memory(title="极限", content="极限描述的是趋近"))
        self.second = self.repo.create_memory(example_memory(title="连续", content="连续要求左右极限相等"))
        for memory in (self.first, self.second):
            self.repo.link(memory.id, self.source.id)

    # -- product server (the same objects launcher -> cli -> web builds) -----
    def product_server(self, *responses):
        context = WebContext.create(self.db_path, config_path=None, quality_check=False)
        context.teacher_model = FakeTeacherModel(*responses)
        self.base = self.start(context)
        return context

    def restart_product_server(self, *responses):
        """Stop the current server, then start a brand-new one on the same database."""
        for server, thread in list(self.servers):
            try:
                server.shutdown()
                server.server_close()
            except Exception:  # pragma: no cover - already closed
                pass
            thread.join(timeout=5)
        self.servers.clear()
        return self.product_server(*responses)

    # -- helpers -----------------------------------------------------------
    def learning(self) -> LearningRepository:
        return LearningRepository(self.repo.database)

    def session(self):
        return self.learning().get_active_session_for_source(self.source.id)

    def state(self, memory):
        return self.learning().get_state(memory.id)

    def learn(self, memory_id=None):
        return f"/learn/{memory_id or self.first.id}"

    def start_learning(self):
        return self.post(self.learn() + "/start", {})

    def send(self, message: str, **kwargs):
        return self.post(self.learn() + "/turn", {"message": message}, **kwargs)

    def send_json(self, message: str):
        status, body, _ = self.send(message, headers={"X-Requested-With": "fetch"})
        return status, json.loads(body)


# ==========================================================================
# 1. audit facts that the freeze depends on
# ==========================================================================

class FreezeAuditTest(FreezeTestCase):
    def test_schema_is_v3_with_exactly_three_migrations(self) -> None:
        self.assertEqual(SUPPORTED_SCHEMA_VERSION, 3)
        self.assertEqual([(migration.version, migration.name) for migration in MIGRATIONS],
                         [(1, "initial_schema"), (2, "memory_search_index"), (3, "learning_layer")])

    def test_the_default_prompt_is_v2_and_v1_is_still_available(self) -> None:
        from personal_memory.teacher_llm import DEFAULT_PROMPT_BUILDER

        self.assertIs(DEFAULT_PROMPT_BUILDER, TeacherPromptV2Builder)
        self.assertEqual(DEFAULT_PROMPT_BUILDER().version, TEACHER_PROMPT_VERSION_V2)
        self.assertEqual(TeacherPromptBuilder().version, TEACHER_PROMPT_VERSION)
        self.assertEqual((TEACHER_PROMPT_VERSION, TEACHER_PROMPT_VERSION_V2),
                         ("teacher_prompt_v1", "teacher_prompt_v2"))

    def test_the_product_routes_exist(self) -> None:
        self.product_server(payload(answer()))

        status, health = self.get("/healthz")
        self.assertEqual((status, health.strip()), (200, "ok"))
        status, listing = self.get("/memories")
        self.assertEqual(status, 200)
        self.assertIn("极限", listing)
        status, detail = self.get(f"/memories/{self.first.id}")
        self.assertEqual(status, 200)
        self.assertIn("学习这一条", detail)
        self.assertIn(f'href="/learn/{self.first.id}"', detail)
        status, page = self.get(self.learn())
        self.assertEqual(status, 200)
        self.assertIn("还没有开始学习", page)

    def test_the_learning_engine_api_is_unchanged(self) -> None:
        self.assertEqual(len(LEARNING_PUBLIC_API), 8)
        self.assertEqual(LEARNING_PUBLIC_API[-1], "abandon_session")
        self.assertEqual(DEFAULT_MAX_STEPS, 3)

    def test_no_learning_table_or_column_was_added(self) -> None:
        with self.database.connection() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
            columns: set[str] = set()
            for table in ("sources", "memories", "learning_states", "learning_sessions"):
                columns |= {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        self.assertEqual({name for name in tables if name.startswith("learning")},
                         {"learning_states", "learning_sessions"})
        for forbidden in ("chat", "conversation", "message", "ui_state", "prompt"):
            self.assertFalse([name for name in tables if forbidden in name.lower()],
                             f"a {forbidden} table exists")
        self.assertNotIn("prompt_version", columns)

    def test_the_web_layer_never_reaches_past_the_product_entry(self) -> None:
        """No web module imports a teacher *capability* at runtime.

        Allowed exceptions, both of which carry no behaviour:
        ``TeacherModelError`` (the shared error type, used for the 502 mapping and the copy)
        and ``TYPE_CHECKING``-only annotations (``LearningContext`` / ``TeacherAgentResult``).
        """
        for name in ("server.py", "teacher.py", "views.py"):
            tree = ast.parse((WEB_DIR / name).read_text(encoding="utf-8"))
            deferred_lines = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
                    for child in ast.walk(node):
                        if hasattr(child, "lineno"):
                            deferred_lines.add(child.lineno)

            runtime: set[str] = set()
            deferred: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names, module = {alias.name.split(".")[0] for alias in node.names}, None
                elif isinstance(node, ast.ImportFrom):
                    names = {alias.name for alias in node.names}
                    module = (node.module or "").split(".")[-1]
                else:
                    continue
                bucket = deferred if node.lineno in deferred_lines else runtime
                if module == "teacher_llm":
                    self.assertEqual(names, {"TeacherModelError"}, f"{name} imports {names}")
                    continue
                if module is None:
                    bucket.update(names)
                else:
                    bucket.add(module)

            for forbidden in ("sqlite3", "teacher_agent", "teacher_runtime", "teacher_provider",
                              "teacher_executor", "teacher_llm"):
                self.assertNotIn(forbidden, runtime, f"{name} imports {forbidden} at runtime")
            self.assertTrue(deferred <= {"learning", "teacher_agent"}, f"{name} defers {deferred}")


# ==========================================================================
# 2. the complete user journey (§二 / §三 / §十四)
# ==========================================================================

class FinalUserJourneyTest(FreezeTestCase):
    def test_the_whole_journey_from_browsing_to_a_recorded_learning_event(self) -> None:
        self.product_server(
            payload(answer(message="先讲讲这一条。")),             # E: a plain question
            payload(answer({"type": "record_learning"})),          # F: step 1
            payload(answer(message="记下了。")),                    # F: step 2
        )

        # C. browse
        status, listing = self.get("/memories")
        self.assertEqual(status, 200)
        self.assertIn("极限", listing)
        status, detail = self.get(f"/memories/{self.first.id}")
        self.assertEqual(status, 200)
        self.assertIn("学习这一条", detail)

        # D. start learning
        status, page, _ = self.start_learning()
        self.assertEqual(status, 200)
        self.assertIn("进行中", page)
        self.assertIn("计划 2 条", page)
        session = self.session()
        self.assertIsNotNone(session)
        self.assertEqual(session.current_memory_id, self.first.id)
        self.assertEqual(list(session.plan), [self.first.id, self.second.id])
        self.assertEqual(len(session.plan), 2)                       # learning states exist
        self.assertEqual(self.state(self.first).learn_count, 0)

        # E. a plain question: no state change
        status, data = self.send_json("给我讲讲这一条")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["actions"], [])
        self.assertEqual(data["steps"], 1)
        self.assertEqual(data["stop_reason"], "no_actions")
        self.assertTrue(data["assistant_message"])
        self.assertEqual(self.session().plan_cursor, 0)
        self.assertEqual(self.state(self.first).learn_count, 0)

        # F. a real learning event
        status, data = self.send_json("我懂了，记一次学习")
        self.assertEqual(status, 200)
        self.assertEqual(data["actions"], ["record_learning"])

        # ...and the engine really moved
        self.assertEqual(self.state(self.first).learn_count, 1)
        self.assertEqual(self.session().plan_cursor, 1)
        self.assertEqual(self.session().current_memory_id, self.second.id)
        self.assertEqual(data["session"]["plan_cursor"], 1)
        self.assertEqual(data["current_memory"]["title"], "连续")

        # ...and a fresh GET shows the persisted state, not a page-level recomputation
        status, page = self.get(self.learn())
        self.assertEqual(status, 200)
        self.assertIn("进行到第 2 条", page)
        self.assertIn("当前正在学：<strong>连续</strong>", page)

    def test_a_second_turn_continues_from_the_persisted_state(self) -> None:
        self.product_server(
            payload(answer({"type": "record_learning"})), payload(answer()),
            payload(answer({"type": "record_learning"})), payload(answer()),
        )
        self.start_learning()
        self.send_json("我懂了第一条")

        status, data = self.send_json("第二条也懂了")

        self.assertEqual(status, 200)
        self.assertEqual(data["actions"], ["record_learning"])
        self.assertEqual(self.session().plan_cursor, 2)
        self.assertEqual(self.state(self.first).learn_count, 1)
        self.assertEqual(self.state(self.second).learn_count, 1)
        self.assertIsNone(self.session().current_memory_id)          # the plan is exhausted

    def test_the_user_never_needs_to_know_the_internals(self) -> None:
        """The visible page mentions products, never architecture (P2D-3 §十四)."""
        self.product_server(payload(answer()))
        self.start_learning()
        status, page = self.get(self.learn())
        for internal in ("TeacherAgent", "TeacherRuntime", "TeacherProvider", "LearningService",
                         "prompt", "PromptBuilder", "TeacherApplication", "session_id"):
            self.assertNotIn(internal, page, f"the UI leaks {internal}")


# ==========================================================================
# 3. refresh and restart persistence (§三 / §四) -- the freeze gate
# ==========================================================================

class PersistenceTest(FreezeTestCase):
    def test_state_survives_a_server_restart(self) -> None:
        self.product_server(payload(answer({"type": "record_learning"})), payload(answer()))
        self.start_learning()
        self.send_json("我懂了第一条")

        session = self.session()
        before = (session.id, session.plan_cursor, session.current_memory_id,
                  self.state(self.first).learn_count)
        self.assertEqual(before[1:], (1, self.second.id, 1))

        # a brand-new server, context and model object on the same database
        self.restart_product_server(payload(answer()))

        status, page = self.get(self.learn())
        self.assertEqual(status, 200)
        self.assertIn("进行中", page)
        self.assertIn("进行到第 2 条", page)
        self.assertIn("当前正在学：<strong>连续</strong>", page)

        session_after = self.session()
        self.assertEqual((session_after.id, session_after.plan_cursor,
                          session_after.current_memory_id,
                          self.state(self.first).learn_count), before)

        # ...and the session keeps working after the restart
        status, data = self.send_json("重启之后继续聊")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["session"]["id"], before[0])

    def test_restart_after_the_plan_is_exhausted_is_still_consistent(self) -> None:
        self.product_server(
            payload(answer({"type": "record_learning"})), payload(answer()),
            payload(answer({"type": "record_learning"})), payload(answer()),
        )
        self.start_learning()
        self.send_json("第一条")
        self.send_json("第二条")
        cursor = self.session().plan_cursor

        self.restart_product_server(payload(answer(message="走完了。")))

        status, page = self.get(self.learn())
        self.assertEqual(status, 200)
        self.assertEqual(self.session().plan_cursor, cursor)
        self.assertIsNone(self.session().current_memory_id)
        self.assertIn("走完了全部内容", page)

    def test_no_restart_leaves_the_one_zero_tables_untouched(self) -> None:
        self.product_server(payload(answer({"type": "record_learning"})), payload(answer()))
        before = self.repo.counts()
        memory_before = self.repo.get_memory(self.first.id).as_dict()

        self.start_learning()
        self.send_json("我懂了第一条")
        self.restart_product_server(payload(answer()))
        self.get(self.learn())

        self.assertEqual(self.repo.counts(), before)
        self.assertEqual(self.repo.get_memory(self.first.id).as_dict(), memory_before)


# ==========================================================================
# 4. active session recovery (§五)
# ==========================================================================

class ActiveSessionRecoveryTest(FreezeTestCase):
    def test_an_existing_session_is_resumed_not_restarted(self) -> None:
        self.product_server(payload(answer()))
        self.start_learning()
        session_id = self.session().id

        status, page = self.get(self.learn())

        self.assertEqual(status, 200)
        self.assertIn("进行中", page)
        self.assertIn('name="message"', page)                        # continue form
        self.assertNotIn(f'action="{self.learn()}/start"', page)     # no start button
        self.assertEqual(self.session().id, session_id)

    def test_starting_again_is_refused_by_the_engine_and_creates_nothing(self) -> None:
        self.product_server(payload(answer()))
        self.start_learning()

        status, page, _ = self.start_learning()

        self.assertEqual(status, 409)
        self.assertIn("没有被接受", page)
        self.assertEqual(len(self.learning().list_active_sessions()), 1)

    def test_resuming_works_after_a_failed_turn(self) -> None:
        self.product_server(TimeoutError("down"), payload(answer(message="好了。")), payload(answer()))
        self.start_learning()
        self.send_json("第一次失败")                                   # 502

        status, page = self.get(self.learn())
        self.assertEqual(status, 200)
        self.assertIn('name="message"', page)

        status, data = self.send_json("再试一次")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])


# ==========================================================================
# 5. lifecycle: finish / abandon (§六)
# ==========================================================================

class SessionLifecycleTest(FreezeTestCase):
    def test_finish_session_completes_the_run_and_the_ui_shows_it(self) -> None:
        self.product_server(payload(answer()), payload(answer()))
        self.start_learning()
        session = self.session()

        # a second context whose model finishes the session
        context = WebContext.create(self.db_path, config_path=None, quality_check=False)
        context.teacher_model = FakeTeacherModel(
            payload(answer({"type": "finish_session", "session_id": session.id},
                           message="今天就到这里。"))
        )
        self.base = self.start(context)

        status, data = self.send_json("今天先结束")

        self.assertEqual(status, 200)
        self.assertEqual(data["actions"], ["finish_session"])
        self.assertEqual(data["session"]["status"], "completed")
        self.assertEqual(str(self.learning().get_session(session.id).status), "completed")

        # the run is over: the page no longer offers the chat form, and a *new* run may start.
        # (The page reports the Source's *active* session; the engine has no "sessions of a
        # Source" read, and P2D-3 does not add one -- §六 only requires the state to be right.)
        status, page = self.get(self.learn())
        self.assertEqual(status, 200)
        self.assertNotIn('name="message"', page)
        self.assertIn(f'action="{self.learn()}/start"', page)
        self.assertIsNone(self.session())                            # no active session left

        # ...and starting again produces a genuinely new session
        status, page, _ = self.start_learning()
        self.assertEqual(status, 200)
        self.assertIn("进行中", page)
        self.assertNotEqual(self.session().id, session.id)

    def test_abandon_session_is_available_at_the_engine_level(self) -> None:
        """The engine still owns the rule; the UI only reflects it."""
        self.product_server(payload(answer()))
        self.start_learning()
        session = self.session()

        self.learning().abandon_session(session.id)

        self.assertEqual(str(self.learning().get_session(session.id).status), "abandoned")
        self.assertIsNone(self.session())
        status, page = self.get(self.learn())
        self.assertEqual(status, 200)
        self.assertIn("还没有开始学习", page)                        # no active session
        self.assertIn(f'action="{self.learn()}/start"', page)        # a new run may start

    def test_a_new_session_may_start_after_a_completed_one(self) -> None:
        self.product_server(payload(answer()), payload(answer()))
        self.start_learning()
        session = self.session()
        self.learning().finish_session(session.id)

        status, page, _ = self.start_learning()

        self.assertEqual(status, 200)
        self.assertIn("进行中", page)
        new_session = self.session()
        self.assertIsNotNone(new_session)
        self.assertNotEqual(new_session.id, session.id)


# ==========================================================================
# 6. error recovery (§七)
# ==========================================================================

class ErrorRecoveryTest(FreezeTestCase):
    def test_a_wrong_memory_id_is_refused_and_the_next_message_works(self) -> None:
        self.product_server(
            payload(answer({"type": "record_learning"})), payload(answer()),
            payload(answer({"type": "record_learning", "memory_id": self.first.id})),
            payload(answer(message="那我们继续。")),
        )
        self.start_learning()
        self.send_json("我懂了第一条")                               # current becomes 连续

        status, data = self.send_json("再记录一次第一条")

        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "ConflictError")
        self.assertIn("not the Memory this session is on", data["detail"])
        self.assertEqual(self.state(self.second).learn_count, 0)     # zero wrong writes
        self.assertEqual(self.session().plan_cursor, 1)              # session unharmed

        status, data = self.send_json("那我们继续")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["assistant_message"], "那我们继续。")

    def test_a_model_error_is_a_502_and_the_next_message_works(self) -> None:
        self.product_server(TimeoutError("provider down"), payload(answer(message="恢复了。")))
        self.start_learning()

        status, data = self.send_json("你好")

        self.assertEqual(status, 502)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "TeacherModelError")
        self.assertEqual(self.session().plan_cursor, 0)
        self.assertEqual(self.state(self.first).learn_count, 0)

        status, data = self.send_json("再试一次")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_invalid_json_is_a_400_and_the_next_message_works(self) -> None:
        self.product_server("我们继续吧", payload(answer(message="这次正常了。")))
        self.start_learning()

        status, data = self.send_json("你好")

        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "ValidationError")
        self.assertIn("not return valid JSON", data["detail"])
        self.assertEqual(self.state(self.first).learn_count, 0)

        status, data = self.send_json("再试一次")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_a_successful_step_before_a_failure_is_kept_and_shown(self) -> None:
        self.product_server(payload(answer({"type": "record_learning"})), "not json")
        self.start_learning()

        status, page, _ = self.send("我懂了第一条")

        self.assertEqual(status, 400)
        self.assertEqual(self.state(self.first).learn_count, 1)      # the real effect stays
        self.assertIn("进行到第 2 条", page)                          # and the page says so
        self.assertIn('name="message"', page)                        # still usable


# ==========================================================================
# 7. freeze bookkeeping
# ==========================================================================

class FreezeBookkeepingTest(FreezeTestCase):
    def test_the_whole_suite_never_touches_the_production_database(self) -> None:
        """The freeze test's database is the harness temp file, never ``data/memory.db``."""
        import tempfile

        temp_root = pathlib.Path(tempfile.gettempdir()).resolve()
        self.assertTrue(pathlib.Path(self.db_path).resolve().is_relative_to(temp_root))
        self.assertNotEqual(pathlib.Path(self.db_path).resolve(),
                            (pathlib.Path.cwd() / "data" / "memory.db").resolve())
        self.product_server(payload(answer()))
        self.start_learning()
        self.assertIn(str(self.db_path), str(self.database.path))

    def test_a_run_leaves_no_session_state_in_memory_only(self) -> None:
        """A second, independent LearningRepository sees the same state."""
        self.product_server(payload(answer({"type": "record_learning"})), payload(answer()))
        self.start_learning()
        self.send_json("我懂了第一条")

        fresh = LearningRepository(self.repo.database)
        session = fresh.get_active_session_for_source(self.source.id)
        self.assertEqual(session.plan_cursor, 1)
        self.assertEqual(fresh.get_state(self.first.id).learn_count, 1)

    def test_no_chat_history_is_stored(self) -> None:
        self.product_server(payload(answer({"type": "record_learning"})), payload(answer()))
        self.start_learning()
        self.send_json("这是我发送的一条独特消息XYZ")

        with self.database.connection() as conn:
            tables = [row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")]
            text_columns: list[tuple[str, str]] = []
            for table in tables:
                if table.startswith("sqlite_") or "fts" in table:
                    continue
                for row in conn.execute(f"PRAGMA table_info({table})"):
                    if "TEXT" in str(row["type"]).upper():
                        text_columns.append((table, row["name"]))
            hits = 0
            for table, column in text_columns:
                sql = f'SELECT COUNT(*) AS n FROM "{table}" WHERE "{column}" LIKE ?'
                hits += int(conn.execute(sql, ("%独特消息XYZ%",)).fetchone()["n"])
        self.assertEqual(hits, 0, "the message text must not be persisted anywhere")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
