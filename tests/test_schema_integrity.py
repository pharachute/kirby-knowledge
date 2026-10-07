"""Schema-integrity guards: initialize() must never trust ``schema_migrations`` blindly.

Regression tests for two gaps an independent review found in the first cut of
Phase 1:

* C1 -- a file whose ``schema_migrations`` claims version 1 while the tables are
  missing used to initialise "successfully" and then fail on the first write
  with a raw ``sqlite3.OperationalError: no such table``.
* C2 -- a database written by a *newer* build used to be accepted silently.
"""

from __future__ import annotations

import sqlite3
import unittest

from personal_memory import (
    MIGRATIONS,
    SUPPORTED_SCHEMA_VERSION,
    Database,
    MemoryRepository,
    SchemaError,
)
from personal_memory.models import utcnow_iso

from .helpers import TempDirTestCase


def _make_raw_db(path, *, version: int, create_migrations_table: bool = True) -> None:
    conn = sqlite3.connect(str(path))
    try:
        if create_migrations_table:
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, "fake", utcnow_iso()),
            )
        conn.commit()
    finally:
        conn.close()


class SchemaIntegrityTest(TempDirTestCase):
    prefix = "pms-schema-"

    def test_fresh_zero_byte_file_is_migrated(self) -> None:
        """A 0-byte file (e.g. left behind by an interrupted start) must migrate cleanly."""
        db_path = self.tmpdir / "zero.db"
        db_path.touch()
        report = Database(db_path).initialize()
        self.assertEqual(report.applied_count, len(MIGRATIONS))
        self.assertEqual(report.version, SUPPORTED_SCHEMA_VERSION)
        self.assertEqual(MemoryRepository(Database(db_path)).counts()["sources"], 0)

    def test_every_declared_migration_table_is_present_after_init(self) -> None:
        database = Database(self.tmpdir / "ok.db")
        database.initialize()
        describe = database.describe()
        declared = {table for migration in MIGRATIONS for table in migration.tables}
        self.assertTrue(declared)
        for table in sorted(declared):
            self.assertIn(table, describe["tables"])

    def test_c1_lying_schema_migrations_is_detected(self) -> None:
        db_path = self.tmpdir / "lying.db"
        _make_raw_db(db_path, version=1)  # records v1 but creates no tables

        with self.assertRaises(SchemaError) as ctx:
            MemoryRepository(Database(db_path))
        message = str(ctx.exception)
        self.assertIn("missing table", message)
        self.assertIn("memories", message)
        # and the failure is a typed package error, not a raw sqlite3 error
        self.assertNotIsInstance(ctx.exception, sqlite3.Error)

    def test_c1_partially_truncated_database_is_detected(self) -> None:
        db_path = self.tmpdir / "partial.db"
        database = Database(db_path)
        database.initialize()
        with database.transaction() as conn:
            conn.execute("DROP TABLE memory_sources")

        with self.assertRaises(SchemaError) as ctx:
            MemoryRepository(Database(db_path))
        self.assertIn("memory_sources", str(ctx.exception))

    def test_c2_database_from_a_newer_build_is_refused(self) -> None:
        db_path = self.tmpdir / "future.db"
        _make_raw_db(db_path, version=SUPPORTED_SCHEMA_VERSION + 1)

        with self.assertRaises(SchemaError) as ctx:
            Database(db_path).initialize()
        message = str(ctx.exception)
        self.assertIn("schema version", message)
        self.assertIn(str(SUPPORTED_SCHEMA_VERSION + 1), message)

    def test_c1_wrongly_shaped_tables_are_detected(self) -> None:
        """Same-named tables with wrong columns must fail at open time, not on the first read."""
        db_path = self.tmpdir / "wrongshape.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO schema_migrations VALUES (1, 'initial_schema', '2026-10-04T00:00:00.000Z')"
            )
            conn.execute("CREATE TABLE sources (id TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE memory_sources (memory_id TEXT, source_id TEXT)")
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(SchemaError) as ctx:
            MemoryRepository(Database(db_path))
        message = str(ctx.exception)
        self.assertIn("missing column", message)
        self.assertIn("created_at", message)

    def test_c1_non_sqlite_file_is_reported_as_schema_error(self) -> None:
        db_path = self.tmpdir / "junk.db"
        db_path.write_bytes(b"this is definitely not a sqlite database file")
        with self.assertRaises(SchemaError) as ctx:
            Database(db_path).initialize()
        self.assertIn("not a usable SQLite database", str(ctx.exception))

    def test_initialize_false_still_verifies_the_schema(self) -> None:
        """The initialize=False escape hatch must not bypass the schema guards."""
        missing = self.tmpdir / "never-created.db"
        with self.assertRaises(SchemaError):
            MemoryRepository(Database(missing), initialize=False)

        really_empty = self.tmpdir / "empty.db"
        really_empty.touch()
        with self.assertRaises(SchemaError):
            MemoryRepository(Database(really_empty), initialize=False)

        good = self.tmpdir / "good.db"
        Database(good).initialize()
        prepared = MemoryRepository(Database(good), initialize=False)
        self.assertEqual(prepared.counts()["sources"], 0)

    def test_initialize_still_idempotent_after_the_guards(self) -> None:
        database = Database(self.tmpdir / "idem.db")
        first = database.initialize()
        second = database.initialize()
        self.assertEqual(first.applied_count, len(MIGRATIONS))
        self.assertEqual(second.applied_count, 0)
        self.assertEqual(second.version, SUPPORTED_SCHEMA_VERSION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
