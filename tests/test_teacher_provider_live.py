"""Phase 2C-5: real-provider smoke test (opt-in, default OFF, one real call).

The default suite stays fully offline: this module is skipped unless it is explicitly
enabled **and** a credential is configured.

::

    $env:PERSONAL_MEMORY_TEACHER_LIVE = "1"          # opt-in
    D:\\python\\python.exe -m unittest tests.test_teacher_provider_live -v

    $env:PERSONAL_MEMORY_TEACHER_LIVE_RUNTIME = "1"  # second opt-in: also drive TeacherRuntime
    D:\\python\\python.exe -m unittest tests.test_teacher_provider_live -v

What it proves:

* a real model answer can be produced and accepted by ``TeacherLLMAdapter`` (strict
  parse, contract, session-id checks)
* the call happens exactly once (no retry) and never touches a database when the
  executor is not used
* the credentials come from the project's own config mechanism (``launch_pkb.config_path``
  + ``load_config``) and are never printed
* the runtime variant writes only to a **temporary** database, and only through the
  P2C-2 executor -> ``LearningService`` path
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest

from personal_memory import (
    LearningRepository,
    LearningService,
    TeacherContext,
    TeacherProvider,
    TeacherRuntime,
    TeacherTurnRequest,
    TeacherTurnResponse,
)
from personal_memory.errors import ValidationError
from personal_memory.llm import LLMConfigError, load_config
from personal_memory.teacher_llm import TeacherLLMAdapter

from .helpers import RepositoryTestCase

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER_DIR = ROOT / "launcher"

#: One real provider call, on purpose: opt-in only, never part of the default suite.
LIVE_ENABLED = os.environ.get("PERSONAL_MEMORY_TEACHER_LIVE") == "1"
#: Driving TeacherRuntime executes the model's own actions -- in the temporary DB only.
RUNTIME_ENABLED = os.environ.get("PERSONAL_MEMORY_TEACHER_LIVE_RUNTIME") == "1"


def live_config() -> "object | None":
    """The project's own config resolution; ``None`` when no credential is available."""
    if str(LAUNCHER_DIR) not in sys.path:
        sys.path.insert(0, str(LAUNCHER_DIR))
    import launch_pkb  # noqa: PLC0415 - the launcher owns the local config location

    try:
        return load_config(launch_pkb.config_path())
    except LLMConfigError:
        return None


@unittest.skipUnless(
    LIVE_ENABLED,
    "real provider smoke test is opt-in (set PERSONAL_MEMORY_TEACHER_LIVE=1)",
)
class RealTeacherProviderSmokeTest(RepositoryTestCase):
    """One real turn against a temporary database; the production DB is never opened."""

    prefix = "pms-teacher-live-"

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
        self.context = TeacherContext.from_learning(
            self.service.get_context(self.session_id),
            self.service.get_learning_overview(self.source.id),
        )
        self.request = TeacherTurnRequest(
            session_id=self.session_id,
            user_message="我刚读完极限，想确认一下：连续和极限到底什么关系？",
            context=self.context,
        )

    # -- helpers ----------------------------------------------------------
    def snapshot(self) -> dict:
        with self.database.connection() as conn:
            fts = {name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
                   for name in ("memory_fts_word", "memory_fts_trigram")}
        return {
            "counts": self.repo.counts(),
            "learning": self.learning.counts(),
            "fts": fts,
            "session": self.learning.get_session(self.session_id).as_dict(),
            "states": [self.learning.get_state(m.id).as_dict() for m in (self.a, self.b)],
        }

    def report(self, label: str, **values) -> None:  # pragma: no cover - evidence output
        print(f"\n[live-teacher] {label}: " + json.dumps(values, ensure_ascii=False, default=str))

    # -- the smoke test ---------------------------------------------------
    def test_one_real_turn_is_accepted_by_the_adapter_and_writes_nothing(self) -> None:
        provider = TeacherProvider(self.config)
        before = self.snapshot()

        response = TeacherLLMAdapter(provider).generate(self.request)     # ONE real call

        envelope = provider.last_response
        self.report(
            "adapter turn",
            config=self.config.safe_summary(),
            model=envelope.model if envelope else None,
            latency_s=round(envelope.latency_seconds, 2) if envelope else None,
            finish_reason=envelope.finish_reason if envelope else None,
            total_tokens=envelope.total_tokens if envelope else None,
            action_types=response.action_types,
            message_chars=len(response.assistant_message),
        )
        self.assertIsInstance(response, TeacherTurnResponse)
        self.assertEqual([action.kind for action in response.actions],
                         list(response.action_types))
        self.assertTrue(set(response.action_types) <= set(
            kind for kind in ("record_learning", "record_assessment", "finish_session",
                              "abandon_session")))

        # the adapter decodes and validates, it never executes: no row may change
        self.assertEqual(self.snapshot(), before)

    @unittest.skipUnless(
        RUNTIME_ENABLED,
        "driving TeacherRuntime writes to the temporary DB (set PERSONAL_MEMORY_TEACHER_LIVE_RUNTIME=1)",
    )
    def test_one_real_turn_through_the_runtime_in_a_temp_db_only(self) -> None:
        provider = TeacherProvider(self.config)
        runtime = TeacherRuntime(self.service, provider)
        before = self.snapshot()

        result = runtime.turn(session_id=self.session_id,
                              user_message="我懂了极限，我们继续看连续。")

        self.report(
            "runtime turn",
            model=provider.last_response.model if provider.last_response else None,
            latency_s=round(provider.last_response.latency_seconds, 2) if provider.last_response else None,
            action_types=result.response.action_types,
            plan_cursor=result.context.session.plan_cursor,
            current_memory=result.context.session.current_memory_id,
            learned=[result.context.state_for(m.id).learn_count for m in (self.a, self.b)],
            levels=[str(result.context.state_for(m.id).understanding_level)
                    for m in (self.a, self.b)],
        )
        # the runtime returned the post-turn state, and 1.0 data is untouched
        self.assertEqual(result.context.session.id, self.session_id)
        self.assertEqual(self.repo.counts(), before["counts"])
        self.assertEqual(self.snapshot()["fts"], before["fts"])
        self.assertLessEqual(self.snapshot()["learning"]["learning_sessions"], 1)
        self.assertTrue({"active", "completed", "abandoned"} >=
                        {str(result.context.session.status)})


class LiveModuleGuardTest(unittest.TestCase):
    """The live module must stay offline-by-default and temp-DB-only."""

    def test_the_smoke_class_is_skipped_unless_explicitly_enabled(self) -> None:
        if LIVE_ENABLED:
            self.assertFalse(getattr(RealTeacherProviderSmokeTest, "__unittest_skip__", False))
        else:
            self.assertTrue(RealTeacherProviderSmokeTest.__unittest_skip__)
            self.assertIn("PERSONAL_MEMORY_TEACHER_LIVE=1",
                          RealTeacherProviderSmokeTest.__unittest_skip_why__)

    def test_the_runtime_variant_needs_a_second_opt_in(self) -> None:
        method = RealTeacherProviderSmokeTest.test_one_real_turn_through_the_runtime_in_a_temp_db_only
        if RUNTIME_ENABLED:
            self.assertFalse(getattr(method, "__unittest_skip__", False))
        else:
            self.assertTrue(getattr(method, "__unittest_skip__", False))
            self.assertIn("PERSONAL_MEMORY_TEACHER_LIVE_RUNTIME=1", method.__unittest_skip_why__)

    def test_the_live_module_never_uses_the_production_database(self) -> None:
        source = pathlib.Path(__file__).read_text(encoding="utf-8")
        forbidden = ("resolve_db" + "_path", "DEFAULT_DB" + "_PATH", "data" + "/memory.db")
        for marker in forbidden:
            self.assertNotIn(marker, source, f"the live smoke test references {marker!r}")

    def test_no_credential_is_written_into_the_live_module(self) -> None:
        source = pathlib.Path(__file__).read_text(encoding="utf-8")
        forbidden = ("sk" + "-", "Bear" + "er ", "api_key" + "=")
        for marker in forbidden:
            self.assertNotIn(marker, source, f"the live smoke test contains {marker!r}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
