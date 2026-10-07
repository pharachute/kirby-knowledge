"""Phase 2A: migration 3 (``learning_layer``) and the constraints it installs.

Covers the guarantees Phase 2A must ship:

* v2 -> v3 adds exactly two tables + their indexes, in one append-only migration
* re-initialising is idempotent
* the promised columns/CHECK/FK/UNIQUE exist **in the database**, not only in the model
* CASCADE / SET NULL behave exactly as documented (state dies with the Memory;
  a session survives a deleted Memory but not a deleted Source)
"""

from __future__ import annotations

import sqlite3
import unittest

from personal_memory import (
    Database,
    LearningRepository,
    LearningSession,
    LearningState,
    MemoryRepository,
)
from personal_memory import db as db_module
from personal_memory.db import (
    LEARNING_REQUIRED_COLUMNS,
    LEARNING_TABLES,
    MIGRATIONS,
    SUPPORTED_SCHEMA_VERSION,
)
from personal_memory.errors import ConflictError, SchemaError
from personal_memory.retrieval import MemoryRetriever

from .helpers import RepositoryTestCase, TempDirTestCase, example_memory, example_source


class MigrationShapeTest(TempDirTestCase):
    prefix = "pms-learnmig-"

    def setUp(self) -> None:
        super().setUp()
        self.db_path = self.tmpdir / "memory.db"
        self.database = Database(self.db_path)

    def test_migration_is_registered_as_version_three_append_only(self) -> None:
        versions = [(m.version, m.name) for m in MIGRATIONS]

        self.assertEqual(versions[:2], [(1, "initial_schema"), (2, "memory_search_index")])
        self.assertEqual(versions[-1], (3, "learning_layer"))
        self.assertEqual(SUPPORTED_SCHEMA_VERSION, max(m.version for m in MIGRATIONS))

    def test_fresh_database_reaches_version_three_with_both_tables(self) -> None:
        report = self.database.initialize()

        self.assertEqual(report.version, SUPPORTED_SCHEMA_VERSION)
        with self.database.connection() as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            versions = [r["version"] for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version")]
            indexes = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'")}
        for table in LEARNING_TABLES:
            self.assertIn(table, tables)
        self.assertEqual(versions, [1, 2, 3])
        self.assertIn("idx_learning_states_level", indexes)
        self.assertIn("idx_learning_sessions_source", indexes)
        self.assertIn("idx_learning_sessions_status", indexes)
        self.assertIn("idx_learning_sessions_one_active", indexes)
        # 1.0 objects are still there
        for legacy in ("memories", "sources", "memory_sources", "memory_fts_word", "memory_fts_trigram"):
            self.assertIn(legacy, tables)

    def test_required_columns_match_the_real_tables(self) -> None:
        self.database.initialize()
        with self.database.connection() as conn:
            for table, expected in LEARNING_REQUIRED_COLUMNS.items():
                present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                self.assertEqual(present, set(expected), table)

    def test_initialize_is_idempotent(self) -> None:
        first = self.database.initialize()
        second = self.database.initialize()

        self.assertEqual(first.version, second.version)
        self.assertEqual(second.applied, ())          # nothing left to apply
        with self.database.connection() as conn:
            rows = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
        self.assertEqual(rows, len(MIGRATIONS))       # no duplicated migration rows

    def test_describe_reports_the_learning_tables(self) -> None:
        self.database.initialize()
        describe = self.database.describe()

        self.assertEqual(describe["schema_version"], SUPPORTED_SCHEMA_VERSION)
        for table in LEARNING_TABLES:
            self.assertIn(table, describe["tables"])
            self.assertEqual(describe["counts"][table], 0)

    def test_v2_database_upgrades_by_applying_only_migration_three(self) -> None:
        """The real 1.0 path: an existing v2 file gets migration 3 and nothing else."""
        original_migrations = db_module.MIGRATIONS
        original_supported = db_module.SUPPORTED_SCHEMA_VERSION
        try:
            db_module.MIGRATIONS = tuple(m for m in original_migrations if m.version <= 2)
            db_module.SUPPORTED_SCHEMA_VERSION = 2
            v2_report = self.database.initialize()
            self.assertEqual(v2_report.version, 2)

            repo = MemoryRepository(self.database)
            source = repo.create_source(example_source(title="v2 材料"))
            memory = repo.create_memory(example_memory(title="v2 记忆"))
            repo.link(memory.id, source.id)
        finally:
            db_module.MIGRATIONS = original_migrations
            db_module.SUPPORTED_SCHEMA_VERSION = original_supported

        upgrade = self.database.initialize()

        self.assertEqual(upgrade.applied, ((3, "learning_layer"),))
        self.assertEqual(upgrade.version, 3)
        reloaded = MemoryRepository(self.database)
        self.assertEqual(reloaded.counts(), {"sources": 1, "memories": 1, "memory_sources": 1})
        self.assertEqual(reloaded.get_memory(memory.id).title, "v2 记忆")
        self.assertEqual(reloaded.index_row_count("word"), 1)   # FTS untouched by migration 3
        self.assertEqual([hit.memory.id for hit in MemoryRetriever(reloaded).search("v2").hits],
                         [memory.id])
        self.assertEqual(LearningRepository(self.database).counts(),
                         {"learning_states": 0, "learning_sessions": 0})

    def test_database_newer_than_this_build_is_refused(self) -> None:
        self.database.initialize()
        with self.database.connection() as conn:
            conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                         (SUPPORTED_SCHEMA_VERSION + 1, "from_the_future", "2026-10-07T00:00:00.000Z"))

        with self.assertRaises(SchemaError) as ctx:
            Database(self.db_path).initialize()
        self.assertIn(str(SUPPORTED_SCHEMA_VERSION + 1), str(ctx.exception))

    def test_documented_rollback_recipe_restores_v2(self) -> None:
        self.database.initialize()
        with self.database.connection() as conn:
            conn.execute("DROP TABLE learning_sessions")
            conn.execute("DROP TABLE learning_states")
            conn.execute("DELETE FROM schema_migrations WHERE version = 3")

        self.assertEqual(self.database.schema_version(), 2)
        self.assertEqual(self.database.initialize().applied, ((3, "learning_layer"),))


class LearningConstraintTest(RepositoryTestCase):
    """The constraints must bite at the SQLite level, not only in the model."""

    prefix = "pms-learncon-"

    def setUp(self) -> None:
        super().setUp()
        self.learning = LearningRepository(self.database)
        self.source = self.make_source()
        self.memory = self.make_memory()

    def raw_state(self, **overrides) -> None:
        record = {
            "memory_id": self.memory.id, "understanding_level": "unknown",
            "known_aspects_json": "[]", "weak_aspects_json": "[]", "misconceptions_json": "[]",
            "learn_count": 0, "last_learned_at": None,
            "created_at": "2026-10-07T00:00:00.000Z", "updated_at": "2026-10-07T00:00:00.000Z",
            "schema_version": 1,
        }
        record.update(overrides)
        with self.database.transaction() as conn:
            conn.execute(
                "INSERT INTO learning_states (memory_id, understanding_level, known_aspects_json,"
                " weak_aspects_json, misconceptions_json, learn_count, last_learned_at, created_at,"
                " updated_at, schema_version) VALUES (:memory_id, :understanding_level,"
                " :known_aspects_json, :weak_aspects_json, :misconceptions_json, :learn_count,"
                " :last_learned_at, :created_at, :updated_at, :schema_version)",
                record,
            )

    def raw_session(self, **overrides) -> None:
        record = {
            "id": "lrn_raw", "source_id": self.source.id, "status": "active",
            "current_memory_id": self.memory.id, "current_stage": "explain",
            "plan_json": "[]", "plan_cursor": 0, "exchange_json": "{}",
            "started_at": "2026-10-07T00:00:00.000Z", "updated_at": "2026-10-07T00:00:00.000Z",
            "ended_at": None, "schema_version": 1,
        }
        record.update(overrides)
        with self.database.transaction() as conn:
            conn.execute(
                "INSERT INTO learning_sessions (id, source_id, status, current_memory_id, current_stage,"
                " plan_json, plan_cursor, exchange_json, started_at, updated_at, ended_at, schema_version)"
                " VALUES (:id, :source_id, :status, :current_memory_id, :current_stage, :plan_json,"
                " :plan_cursor, :exchange_json, :started_at, :updated_at, :ended_at, :schema_version)",
                record,
            )

    def assert_sqlite_rejects(self, callable_) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            callable_()

    def test_check_constraints_on_learning_states(self) -> None:
        self.assert_sqlite_rejects(lambda: self.raw_state(understanding_level="mastered"))
        self.assert_sqlite_rejects(lambda: self.raw_state(known_aspects_json='{"not": "an array"}'))
        self.assert_sqlite_rejects(lambda: self.raw_state(weak_aspects_json="not json"))
        self.assert_sqlite_rejects(lambda: self.raw_state(misconceptions_json='"a string"'))
        self.assert_sqlite_rejects(lambda: self.raw_state(learn_count=-1))
        self.assert_sqlite_rejects(lambda: self.raw_state(learn_count=1.5))
        self.assert_sqlite_rejects(lambda: self.raw_state(created_at=""))

    def test_check_constraints_on_learning_sessions(self) -> None:
        self.assert_sqlite_rejects(lambda: self.raw_session(status="paused"))
        self.assert_sqlite_rejects(lambda: self.raw_session(current_stage="teaching"))
        self.assert_sqlite_rejects(lambda: self.raw_session(plan_json="{}"))
        self.assert_sqlite_rejects(lambda: self.raw_session(exchange_json="[]"))
        self.assert_sqlite_rejects(lambda: self.raw_session(plan_cursor=-1))
        self.assert_sqlite_rejects(lambda: self.raw_session(started_at=""))

    def test_foreign_keys_are_enforced(self) -> None:
        self.assert_sqlite_rejects(lambda: self.raw_state(memory_id="mem_does_not_exist"))
        self.assert_sqlite_rejects(lambda: self.raw_session(source_id="src_does_not_exist"))
        self.assert_sqlite_rejects(lambda: self.raw_session(current_memory_id="mem_does_not_exist"))

    def test_one_active_session_per_source_is_enforced_by_the_database(self) -> None:
        self.learning.create_session(LearningSession.create(source_id=self.source.id))

        with self.assertRaises(ConflictError) as ctx:
            self.learning.create_session(LearningSession.create(source_id=self.source.id))
        self.assertIn("already has an active learning session", str(ctx.exception))
        # a second active row is refused by SQLite itself (partial unique index)
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_session(id="lrn_raw2", status="active")

    def test_learning_state_is_deleted_with_its_memory_but_session_survives(self) -> None:
        self.learning.create_state(LearningState.create(memory_id=self.memory.id, learn_count=3))
        session = self.learning.create_session(
            LearningSession.create(source_id=self.source.id, current_memory_id=self.memory.id))

        self.repo.delete_memory(self.memory.id)

        self.assertIsNone(self.learning.get_state(self.memory.id))
        reloaded = self.learning.get_session(session.id)
        self.assertIsNotNone(reloaded)                       # session survives
        self.assertIsNone(reloaded.current_memory_id)        # ... with SET NULL
        self.assertEqual(reloaded.source_id, self.source.id)

    def test_learning_state_survives_source_deletion_but_session_does_not(self) -> None:
        self.learning.create_state(LearningState.create(memory_id=self.memory.id, learn_count=2))
        session = self.learning.create_session(LearningSession.create(source_id=self.source.id))

        self.repo.delete_source(self.source.id)

        self.assertIsNone(self.learning.get_session(session.id))
        self.assertIsNotNone(self.learning.get_state(self.memory.id))
        self.assertEqual(self.learning.get_state(self.memory.id).learn_count, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
