"""Database-layer enforcement: CHECK / UNIQUE / FOREIGN KEY constraints.

These tests deliberately bypass the application validation layer and talk raw
SQL to SQLite, proving that illegal data cannot be stored even when the
dataclass layer is skipped (e.g. another tool writing into the same file).
"""

from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from personal_memory import ConflictError, Memory, ValidationError

from .helpers import RepositoryTestCase
from personal_memory.store import MEMORY_COLUMNS, SOURCE_COLUMNS


def _insert(conn: sqlite3.Connection, table: str, columns: tuple[str, ...], row: dict) -> None:
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        tuple(row[column] for column in columns),
    )


class ForeignKeyPragmaTest(RepositoryTestCase):
    def test_foreign_keys_are_enabled_on_every_connection(self) -> None:
        with self.database.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.database.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)


class SourceConstraintTest(RepositoryTestCase):
    def test_check_constraints_reject_illegal_source_rows(self) -> None:
        source = self.make_source(title="合法来源", content="合法正文")
        base = source.to_record()
        cases = [
            ("unsupported source_type", {**base, "id": "src_bad_type", "source_type": "video"}),
            ("empty title", {**base, "id": "src_bad_title", "title": "   "}),
            ("empty content", {**base, "id": "src_bad_content", "content": ""}),
            ("short content_hash", {**base, "id": "src_bad_hash", "content_hash": "a" * 63}),
            ("uppercase content_hash", {**base, "id": "src_bad_hash2", "content_hash": "A" * 64}),
            ("metadata not JSON", {**base, "id": "src_bad_meta", "metadata_json": "not-json"}),
            ("metadata not an object", {**base, "id": "src_bad_meta2", "metadata_json": "[]"}),
            ("non-http url", {**base, "id": "src_bad_url", "url": "ftp://example.com"}),
        ]
        for label, row in cases:
            with self.subTest(case=label), self.assertRaises(sqlite3.IntegrityError):
                with self.database.transaction() as conn:
                    _insert(conn, "sources", SOURCE_COLUMNS, row)
        self.assertEqual(self.repo.counts()["sources"], 1)

    def test_unique_content_hash_is_enforced_by_sqlite(self) -> None:
        source = self.make_source(content="唯一正文")
        duplicate_row = {**source.to_record(), "id": "src_other_id"}
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            with self.database.transaction() as conn:
                _insert(conn, "sources", SOURCE_COLUMNS, duplicate_row)
        self.assertIn("content_hash", str(ctx.exception))
        self.assertEqual(self.repo.counts()["sources"], 1)


class MemoryConstraintTest(RepositoryTestCase):
    def test_check_constraints_reject_illegal_memory_rows(self) -> None:
        memory = self.make_memory(title="合法记忆", content="合法正文")
        base = memory.to_record()
        cases = [
            ("unsupported type", {**base, "id": "mem_bad_type", "type": "idea"}),
            ("unsupported origin", {**base, "id": "mem_bad_origin", "information_origin": "llm_guess"}),
            ("unsupported status", {**base, "id": "mem_bad_status", "status": "deleted"}),
            ("empty title", {**base, "id": "mem_bad_title", "title": " "}),
            ("empty content", {**base, "id": "mem_bad_content", "content": " "}),
            ("importance above 1", {**base, "id": "mem_bad_imp", "importance": 1.5}),
            ("importance below 0", {**base, "id": "mem_bad_imp2", "importance": -0.5}),
            ("confidence above 1", {**base, "id": "mem_bad_conf", "confidence": 1.01}),
            ("tags not JSON", {**base, "id": "mem_bad_tags", "tags_json": "sqlite,storage"}),
            ("tags not an array", {**base, "id": "mem_bad_tags2", "tags_json": '{"a": 1}'}),
            ("schema_version below 1", {**base, "id": "mem_bad_ver", "schema_version": 0}),
        ]
        for label, row in cases:
            with self.subTest(case=label), self.assertRaises(sqlite3.IntegrityError):
                with self.database.transaction() as conn:
                    _insert(conn, "memories", MEMORY_COLUMNS, row)
        self.assertEqual(self.repo.counts()["memories"], 1)

    def test_duplicate_memory_id_is_enforced_by_sqlite(self) -> None:
        memory = self.make_memory()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as conn:
                _insert(conn, "memories", MEMORY_COLUMNS, memory.to_record())
        self.assertEqual(self.repo.counts()["memories"], 1)


class RelationConstraintTest(RepositoryTestCase):
    def test_foreign_keys_block_links_to_missing_entities(self) -> None:
        source = self.make_source()
        memory = self.make_memory()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as conn:
                conn.execute(
                    "INSERT INTO memory_sources (memory_id, source_id, created_at) VALUES (?, ?, ?)",
                    ("mem_ghost", source.id, "2026-10-04T00:00:00.000Z"),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as conn:
                conn.execute(
                    "INSERT INTO memory_sources (memory_id, source_id, created_at) VALUES (?, ?, ?)",
                    (memory.id, "src_ghost", "2026-10-04T00:00:00.000Z"),
                )
        self.assertEqual(self.repo.counts()["memory_sources"], 0)

    def test_composite_primary_key_blocks_duplicate_links(self) -> None:
        source = self.make_source()
        memory = self.make_memory()
        self.repo.link(memory.id, source.id)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as conn:
                conn.execute(
                    "INSERT INTO memory_sources (memory_id, source_id, created_at) VALUES (?, ?, ?)",
                    (memory.id, source.id, "2026-10-04T00:00:00.000Z"),
                )
        self.assertEqual(self.repo.counts()["memory_sources"], 1)

    def test_relation_rows_cascade_when_the_memory_is_deleted(self) -> None:
        source = self.make_source()
        memory = self.make_memory()
        self.repo.link(memory.id, source.id)
        with self.database.transaction() as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory.id,))
            remaining = conn.execute("SELECT COUNT(*) AS n FROM memory_sources").fetchone()["n"]
            sources = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
        self.assertEqual(remaining, 0)
        self.assertEqual(sources, 1)


class RepositoryIntegrityTranslationTest(RepositoryTestCase):
    def test_check_failure_is_translated_to_validation_error(self) -> None:
        """If app validation is bypassed, SQLite rejects and the repo maps the error."""
        memory = Memory.create(
            type="knowledge", title="合法", content="合法正文", information_origin="user_explicit"
        )
        with mock.patch.object(Memory, "validate", lambda self: self):
            memory.type = "bogus"  # only possible because validation is patched away
            with self.assertRaises(ValidationError) as ctx:
                self.repo.create_memory(memory)
        self.assertIn("CHECK constraint failed", str(ctx.exception))
        self.assertEqual(self.repo.counts()["memories"], 0)

    def test_duplicate_memory_id_is_translated_to_conflict_error(self) -> None:
        memory = self.make_memory()
        clone = Memory.create(
            type="knowledge",
            title="另一个",
            content="另一段正文",
            information_origin="user_explicit",
            memory_id=memory.id,
        )
        with self.assertRaises(ConflictError):
            self.repo.create_memory(clone)
        self.assertEqual(self.repo.counts()["memories"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
