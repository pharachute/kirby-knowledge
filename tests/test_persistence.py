"""Delete semantics + persistence across re-initialisation (Phase 1 Test 6 and Test 7)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest

from personal_memory import CURRENT_SCHEMA_VERSION, MIGRATIONS, SUPPORTED_SCHEMA_VERSION, Database, MemoryRepository

from .helpers import PROJECT_ROOT, RepositoryTestCase


class DeleteSemanticsTest(RepositoryTestCase):
    def test_6_deleting_a_memory_does_not_delete_its_sources(self) -> None:
        """Test 6: delete a Memory -> Sources survive, only the relation rows cascade."""
        source = self.make_source(title="不应被删除的来源", content="来源正文")
        other_source = self.make_source(title="另一个来源", content="另一个来源正文")
        memory = self.make_memory(title="将被删除的记忆")
        self.repo.link(memory.id, source.id)
        self.repo.link(memory.id, other_source.id)

        before = self.repo.counts()
        self.assertEqual(before, {"sources": 2, "memories": 1, "memory_sources": 2})

        self.assertTrue(self.repo.delete_memory(memory.id))

        self.assertIsNone(self.repo.get_memory(memory.id))
        self.assertEqual(self.repo.counts(), {"sources": 2, "memories": 0, "memory_sources": 0})
        for surviving in (source, other_source):
            reloaded = self.repo.require_source(surviving.id)
            self.assertEqual(reloaded.content, surviving.content)
            self.assertEqual(reloaded.content_hash, surviving.content_hash)
        # raw SQL check: the source rows are physically still there
        with self.database.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"], 2)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM memory_sources").fetchone()["n"], 0
            )

    def test_deleting_a_memory_twice_reports_false(self) -> None:
        memory = self.make_memory()
        self.assertTrue(self.repo.delete_memory(memory.id))
        self.assertFalse(self.repo.delete_memory(memory.id))
        self.assertFalse(self.repo.delete_memory("mem_never_existed"))

    def test_deleting_a_source_keeps_its_memories(self) -> None:
        source = self.make_source()
        memory = self.make_memory()
        self.repo.link(memory.id, source.id)
        self.assertTrue(self.repo.delete_source(source.id))
        self.assertIsNone(self.repo.get_source(source.id))
        self.assertIsNotNone(self.repo.get_memory(memory.id))
        self.assertEqual(self.repo.counts()["memory_sources"], 0)
        self.assertFalse(self.repo.is_linked(memory.id, source.id))


class PersistenceTest(RepositoryTestCase):
    def test_7_data_survives_reinitialisation(self) -> None:
        """Test 7: after re-initialising the program, the SQLite data is still there."""
        source = self.make_source(
            title="持久化来源", content="持久化正文", metadata={"kept": True}
        )
        second_source = self.make_source(title="第二个来源", content="第二个正文")
        memory = self.make_memory(
            title="持久化记忆",
            content="持久化记忆正文",
            tags=["persist", "sqlite"],
            importance=0.75,
            confidence=0.6,
        )
        self.repo.link(memory.id, source.id)
        self.repo.link(memory.id, second_source.id)
        expected_counts = self.repo.counts()

        # a full process restart: brand-new Database + Repository over the same file
        restarted_database = Database(self.db_path)
        report = restarted_database.initialize()  # idempotent: nothing new to apply
        self.assertEqual(report.applied_count, 0)
        self.assertEqual(report.version, SUPPORTED_SCHEMA_VERSION)
        restarted_repo = MemoryRepository(restarted_database)

        self.assertEqual(restarted_repo.counts(), expected_counts)

        reloaded_memory = restarted_repo.require_memory(memory.id)
        self.assertEqual(reloaded_memory.title, memory.title)
        self.assertEqual(reloaded_memory.content, memory.content)
        self.assertEqual(reloaded_memory.tags, ["persist", "sqlite"])
        self.assertEqual(reloaded_memory.importance, 0.75)
        self.assertEqual(reloaded_memory.confidence, 0.6)
        self.assertEqual(reloaded_memory.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(reloaded_memory.created_at, memory.created_at)

        reloaded_source = restarted_repo.require_source(source.id)
        self.assertEqual(reloaded_source.content, source.content)
        self.assertEqual(reloaded_source.content_hash, source.content_hash)
        self.assertEqual(reloaded_source.metadata, {"kept": True})

        self.assertEqual(
            {s.id for s in restarted_repo.get_sources_for_memory(memory.id)},
            {source.id, second_source.id},
        )
        self.assertEqual(
            [m.id for m in restarted_repo.get_memories_for_source(source.id)], [memory.id]
        )

    def test_migrations_are_recorded_once(self) -> None:
        applied = self.database.applied_migrations()
        self.assertEqual(len(applied), len(MIGRATIONS))
        self.assertEqual(
            [(version, name) for version, name, _ in applied],
            [(m.version, m.name) for m in MIGRATIONS],
        )
        self.assertTrue(all(applied_at for _, _, applied_at in applied))
        self.assertEqual(self.database.schema_version(), SUPPORTED_SCHEMA_VERSION)
        # a third initialisation still applies nothing
        self.assertEqual(self.database.initialize().applied_count, 0)

    def test_database_file_and_tables_exist_on_disk(self) -> None:
        self.assertTrue(self.db_path.exists())
        describe = self.database.describe()
        self.assertTrue(describe["exists"])
        for table in ("sources", "memories", "memory_sources", "schema_migrations"):
            self.assertIn(table, describe["tables"])
        self.assertEqual(describe["schema_version"], SUPPORTED_SCHEMA_VERSION)
        self.assertIn("idx_memories_type_status", describe["indexes"])
        self.assertIn("idx_memory_sources_source", describe["indexes"])

    def test_initialize_creates_missing_parent_directory(self) -> None:
        nested = self.db_path.parent / "nested" / "deeper" / "memory.db"
        report = Database(nested).initialize()
        self.assertTrue(nested.exists())
        self.assertEqual(report.applied_count, len(MIGRATIONS))

    def test_7b_data_survives_a_real_second_process(self) -> None:
        """Test 7, hardened: write in process A, read in a separate interpreter process."""
        source = self.make_source(title="跨进程来源", content="进程 A 写入的正文")
        memory = self.make_memory(title="跨进程记忆", content="进程 A 写入的记忆", tags=["xproc"])
        self.repo.link(memory.id, source.id)

        reader = textwrap.dedent(
            """
            import json, sys
            from personal_memory import Database, MemoryRepository

            repo = MemoryRepository(Database(sys.argv[1]))
            payload = {
                "counts": repo.counts(),
                "sources": [s.as_dict() for s in repo.list_sources()],
                "memories": [m.as_dict() for m in repo.list_memories()],
                "reapplied": Database(sys.argv[1]).initialize().applied_count,
            }
            payload["memory_to_sources"] = {
                m.id: [s.id for s in repo.get_sources_for_memory(m.id)] for m in repo.list_memories()
            }
            print(json.dumps(payload, ensure_ascii=False))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", reader, str(self.db_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            cwd=PROJECT_ROOT,
            # the child must emit the same encoding this process decodes: this
            # machine's locale is cp936, so without this the Chinese payload came
            # back as GBK and ``json.loads(completed.stdout)`` failed to decode it
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        payload = json.loads(completed.stdout)

        self.assertEqual(payload["counts"], {"sources": 1, "memories": 1, "memory_sources": 1})
        self.assertEqual(payload["reapplied"], 0)
        self.assertEqual(payload["sources"][0]["id"], source.id)
        self.assertEqual(payload["sources"][0]["content_hash"], source.content_hash)
        self.assertEqual(payload["memories"][0]["id"], memory.id)
        self.assertEqual(payload["memories"][0]["tags"], ["xproc"])
        self.assertEqual(payload["memories"][0]["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(payload["memory_to_sources"], {memory.id: [source.id]})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
