"""Search-index tests: migration v2, backfill, trigger synchronisation, guards.

These cover the "database" half of Phase 3: the index must exist for existing
data, stay in sync automatically, and never be silently missing.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from personal_memory import (
    MIGRATIONS,
    SUPPORTED_SCHEMA_VERSION,
    Database,
    Memory,
    MemoryRepository,
    SchemaError,
    Source,
    ValidationError,
)
from personal_memory.db import INITIAL_SCHEMA_STATEMENTS, SEARCH_INDEX_TRIGGER_NAMES, Database as DatabaseClass
from personal_memory.retrieval import MemoryRetriever, RetrievalError

from .helpers import RepositoryTestCase, TempDirTestCase

MIGRATIONS_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
)


class MigrationV2Test(TempDirTestCase):
    prefix = "pms-idx-"

    def test_migration_list_contains_v1_and_v2(self) -> None:
        self.assertEqual([(m.version, m.name) for m in MIGRATIONS], [
            (1, "initial_schema"),
            (2, "memory_search_index"),
        ])
        self.assertEqual(SUPPORTED_SCHEMA_VERSION, 2)

    def test_fresh_database_has_indexes_and_triggers(self) -> None:
        database = Database(self.tmpdir / "fresh.db")
        report = database.initialize()
        self.assertEqual(report.applied_count, len(MIGRATIONS))
        self.assertEqual(report.version, SUPPORTED_SCHEMA_VERSION)
        describe = database.describe()
        self.assertIn("memory_fts_word", describe["tables"])
        self.assertIn("memory_fts_trigram", describe["tables"])
        with database.connection() as conn:
            triggers = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            }
        self.assertEqual(triggers, set(SEARCH_INDEX_TRIGGER_NAMES))

    def test_index_definition_is_fts5_trigram_and_word(self) -> None:
        database = Database(self.tmpdir / "tokenizers.db")
        database.initialize()
        with database.connection() as conn:
            word_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'memory_fts_word'"
            ).fetchone()["sql"]
            trigram_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'memory_fts_trigram'"
            ).fetchone()["sql"]
        self.assertIn("fts5", word_sql.lower())
        self.assertIn("unicode61", word_sql)
        self.assertIn("trigram", trigram_sql)

    def test_existing_phase1_phase2_database_is_backfilled(self) -> None:
        """A v1 database with data must become searchable after initialize()."""
        db_path = self.tmpdir / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        try:
            for statement in INITIAL_SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.execute(MIGRATIONS_TABLE_DDL)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (1, 'initial_schema', ?)",
                ("2026-10-04T00:00:00.000Z",),
            )
            memory = Memory.create(
                type="knowledge",
                title="RAG 的基本原理",
                content="RAG 通过检索外部知识为模型提供上下文。",
                information_origin="source_content",
                tags=["rag", "retrieval"],
                memory_id="mem_legacy_1",
            )
            record = memory.to_record()
            conn.execute(
                "INSERT INTO memories (id, type, title, content, summary, tags_json, importance, confidence,"
                " information_origin, status, created_at, updated_at, schema_version)"
                " VALUES (:id, :type, :title, :content, :summary, :tags_json, :importance, :confidence,"
                " :information_origin, :status, :created_at, :updated_at, :schema_version)",
                record,
            )
            source = Source.create(
                source_type="text", title="RAG 入门", content="RAG 把检索与生成结合。", source_id="src_legacy_1"
            )
            source_record = source.to_record()
            conn.execute(
                "INSERT INTO sources (id, source_type, title, content, url, content_hash, metadata_json,"
                " created_at, updated_at) VALUES (:id, :source_type, :title, :content, :url, :content_hash,"
                " :metadata_json, :created_at, :updated_at)",
                source_record,
            )
            conn.execute(
                "INSERT INTO memory_sources (memory_id, source_id, created_at) VALUES (?, ?, ?)",
                ("mem_legacy_1", "src_legacy_1", "2026-10-04T00:00:00.000Z"),
            )
            conn.commit()
        finally:
            conn.close()

        # initialize() must apply ONLY migration 2 and backfill the existing memory
        database = Database(db_path)
        report = database.initialize()
        self.assertEqual(report.applied, ((2, "memory_search_index"),))
        self.assertEqual(report.version, 2)

        repository = MemoryRepository(database)
        self.assertEqual(repository.index_row_count("word"), 1)
        self.assertEqual(repository.index_row_count("trigram"), 1)
        result = MemoryRetriever(repository).search("RAG")
        self.assertEqual([hit.memory.id for hit in result.hits], ["mem_legacy_1"])
        self.assertEqual([source.id for source in result.hits[0].sources], ["src_legacy_1"])

    def test_repeated_initialize_does_not_rebuild_or_break_the_index(self) -> None:
        db_path = self.tmpdir / "idempotent.db"
        repository = MemoryRepository(Database(db_path))
        memory = repository.create_memory(
            Memory.create(type="knowledge", title="RAG", content="RAG 内容", information_origin="user_explicit")
        )

        database = Database(db_path)
        before = repository.index_row_count("word")
        second = database.initialize()
        self.assertEqual(second.applied_count, 0)
        self.assertEqual(second.version, 2)
        self.assertEqual(repository.index_row_count("word"), before)
        self.assertTrue(MemoryRetriever(repository).search("RAG").hits[0].memory.id == memory.id)

    def test_missing_index_table_is_detected(self) -> None:
        db_path = self.tmpdir / "noindex.db"
        repository = MemoryRepository(Database(db_path))
        with repository.database.transaction() as conn:
            conn.execute("DROP TABLE memory_fts_trigram")
        with self.assertRaises(SchemaError) as ctx:
            Database(db_path).initialize()
        self.assertIn("memory_fts_trigram", str(ctx.exception))

    def test_missing_trigger_is_detected(self) -> None:
        db_path = self.tmpdir / "notrigger.db"
        repository = MemoryRepository(Database(db_path))
        with repository.database.transaction() as conn:
            conn.execute("DROP TRIGGER memory_fts_word_au")
        with self.assertRaises(SchemaError) as ctx:
            Database(db_path).initialize()
        self.assertIn("memory_fts_word_au", str(ctx.exception))

    def test_index_definition_is_verified_by_required_columns(self) -> None:
        db_path = self.tmpdir / "wrongcols.db"
        conn = sqlite3.connect(str(db_path))
        try:
            for statement in INITIAL_SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.execute(MIGRATIONS_TABLE_DDL)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (1, 'initial_schema', 'x')"
            )
            conn.execute("CREATE VIRTUAL TABLE memory_fts_word USING fts5(memory_id UNINDEXED)")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(SchemaError) as ctx:
            Database(db_path).initialize()
        message = str(ctx.exception)
        self.assertIn("memory_fts_word", message)
        self.assertIn("missing column", message)


class IndexSyncTest(RepositoryTestCase):
    """Triggers must keep both indexes identical to the memories table."""

    def search_titles(self, query: str) -> list[str]:
        return [hit.memory.title for hit in MemoryRetriever(self.repo).search(query, status="all").hits]

    def test_index_matches_table_after_full_lifecycle(self) -> None:
        memory = self.make_memory(title="可检索的记忆", content="初始内容里有检索词 alpha。")
        self.assertEqual(self.repo.index_row_count("word"), 1)
        self.assertEqual(self.repo.index_row_count("trigram"), 1)

        self.assertEqual(self.search_titles("alpha"), ["可检索的记忆"])

        self.repo.update_memory(memory.id, content="更新后的内容里有检索词 beta。")
        self.assertEqual(self.repo.index_row_count("word"), 1)
        self.assertEqual(self.search_titles("beta"), ["可检索的记忆"])
        self.assertEqual(self.search_titles("alpha"), [])

        self.repo.delete_memory(memory.id)
        self.assertEqual(self.repo.index_row_count("word"), 0)
        self.assertEqual(self.repo.index_row_count("trigram"), 0)
        self.assertEqual(self.search_titles("beta"), [])

    def test_tags_are_indexed_without_json_syntax(self) -> None:
        memory = self.make_memory(title="标签测试", content="正文无关。", tags=["python", "sqlite"])
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT tags FROM memory_fts_word WHERE memory_id = ?", (memory.id,)
            ).fetchone()
        self.assertEqual(row["tags"], "python sqlite")  # joined values, not `["python", "sqlite"]`

    def test_archiving_and_restoring_keeps_the_index(self) -> None:
        memory = self.make_memory(title="归档测试", content="归档相关的记忆内容。")
        self.repo.update_memory(memory.id, status="archived")
        self.assertEqual(self.repo.index_row_count("word"), 1)
        self.assertEqual(MemoryRetriever(self.repo).search("归档相关").hits, ())
        self.assertEqual(
            [hit.memory.id for hit in MemoryRetriever(self.repo).search("归档相关", status="archived").hits],
            [memory.id],
        )
        self.repo.update_memory(memory.id, status="active")
        self.assertEqual(
            [hit.memory.id for hit in MemoryRetriever(self.repo).search("归档相关").hits], [memory.id]
        )

    def test_index_never_drifts_across_many_writes(self) -> None:
        memories = [self.make_memory(title=f"记忆 {i}", content=f"内容 {i}") for i in range(10)]
        for memory in memories[:5]:
            self.repo.update_memory(memory.id, content=f"更新内容 {memory.title}")
        for memory in memories[:3]:
            self.repo.delete_memory(memory.id)
        self.assertEqual(self.repo.counts()["memories"], 7)
        self.assertEqual(self.repo.index_row_count("word"), 7)
        self.assertEqual(self.repo.index_row_count("trigram"), 7)
        # every remaining memory is findable through the index
        for memory in memories[3:]:
            self.assertTrue(MemoryRetriever(self.repo).search(memory.title, status="all").hits)

    def test_legacy_rows_inserted_by_raw_sql_are_indexed_by_triggers(self) -> None:
        """Even a row inserted outside the repository goes through the triggers."""
        memory = self.make_memory(title="触发器探针", content="原始 SQL 插入的记忆。")
        record = memory.to_record()
        record["id"] = "mem_raw_trigger"
        record["title"] = "触发器探针二"
        with self.database.transaction() as conn:
            conn.execute(
                "INSERT INTO memories (id, type, title, content, summary, tags_json, importance, confidence,"
                " information_origin, status, created_at, updated_at, schema_version)"
                " VALUES (:id, :type, :title, :content, :summary, :tags_json, :importance, :confidence,"
                " :information_origin, :status, :created_at, :updated_at, :schema_version)",
                record,
            )
        self.assertEqual(self.repo.index_row_count("word"), 2)
        self.assertIn("触发器探针二", self.search_titles("触发器探针二"))


class IndexIntegrityTest(RepositoryTestCase):
    """The index must survive raw-SQL writers, and drift must be detectable/repairable."""

    def test_raw_sql_insert_or_replace_keeps_the_index_in_sync(self) -> None:
        memory = self.make_memory(title="同步探针", content="Sync gamma 内容。")
        replacement = memory.to_record()
        replacement["content"] = "Sync delta 内容。"
        with self.database.transaction() as conn:
            # REPLACE deletes the conflicting row: only recursive triggers fire the
            # AFTER DELETE trigger, and Database.connect() enables them
            conn.execute(
                "INSERT OR REPLACE INTO memories (id, type, title, content, summary, tags_json, importance,"
                " confidence, information_origin, status, created_at, updated_at, schema_version)"
                " VALUES (:id, :type, :title, :content, :summary, :tags_json, :importance, :confidence,"
                " :information_origin, :status, :created_at, :updated_at, :schema_version)",
                replacement,
            )
        self.assertEqual(self.repo.index_row_count("word"), 1)
        self.assertEqual(self.repo.index_row_count("trigram"), 1)
        self.assertTrue(self.repo.index_consistency()["consistent"])
        # the indexed text is the NEW one: the old value is gone, not merely outranked
        with self.database.connection() as conn:
            indexed = conn.execute(
                "SELECT content FROM memory_fts_word WHERE memory_id = ?", (memory.id,)
            ).fetchone()["content"]
        self.assertIn("delta", indexed)
        self.assertNotIn("gamma", indexed)
        self.assertEqual(
            [hit.memory.title for hit in MemoryRetriever(self.repo).search("delta").hits], ["同步探针"]
        )
        self.assertEqual(MemoryRetriever(self.repo).search("gamma").total, 0)

    def test_index_drift_is_detectable_and_repairable(self) -> None:
        memory = self.make_memory(title="漂移探针", content="Drift probe 内容。")
        self.assertTrue(self.repo.index_consistency()["consistent"])

        with self.database.transaction() as conn:  # simulate an external index edit
            conn.execute("DELETE FROM memory_fts_word")
        status = self.repo.index_consistency()
        self.assertFalse(status["consistent"])
        self.assertEqual(status["word_index"], 0)
        self.assertEqual(MemoryRetriever(self.repo).search("Drift probe").total, 0)

        repaired = self.repo.rebuild_search_index()
        self.assertTrue(repaired["consistent"])
        self.assertEqual(repaired["word_index"], 1)
        self.assertEqual(
            [hit.memory.id for hit in MemoryRetriever(self.repo).search("Drift probe").hits], [memory.id]
        )

    def test_recursive_triggers_are_enabled_on_every_connection(self) -> None:
        with self.database.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA recursive_triggers").fetchone()[0], 1)
        with self.database.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA recursive_triggers").fetchone()[0], 1)


class SearchErrorTranslationTest(RepositoryTestCase):
    def test_missing_index_raises_retrieval_error_not_sqlite_error(self) -> None:
        self.make_memory(title="indexprobe", content="used to verify error translation.")
        retriever = MemoryRetriever(self.repo)
        with self.database.transaction() as conn:
            conn.execute("DROP TABLE memory_fts_word")
        with self.assertRaises(RetrievalError) as ctx:
            retriever.search("indexprobe")
        self.assertNotIsInstance(ctx.exception, sqlite3.Error)

    def test_unknown_index_name_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.repo.search_index("nope", '"x"')

    def test_like_metacharacters_are_escaped_not_wildcards(self) -> None:
        self.make_memory(title="百分号", content="折扣 50% 的说明。")
        self.make_memory(title="普通", content="没有任何特殊符号的说明。")
        hits = MemoryRetriever(self.repo).search("50%").hits
        self.assertEqual([hit.memory.title for hit in hits], ["百分号"])
        underscore = self.make_memory(title="下划线", content="file_name 字段说明。")
        self.assertEqual(
            [hit.memory.id for hit in MemoryRetriever(self.repo).search("file_name").hits], [underscore.id]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
