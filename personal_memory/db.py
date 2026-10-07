"""SQLite storage: schema, connection handling, initialisation and migrations.

Initialisation contract
-----------------------
* :class:`Database.initialize` is **idempotent** -- calling it on an existing
  file applies only the migrations that are not recorded yet, so re-running a
  program never destroys data (Phase 1 test 7).
* Applied migrations are recorded in ``schema_migrations`` so the schema state
  is queryable instead of guessed.
* Every connection enables ``PRAGMA foreign_keys = ON``; without it SQLite
  silently ignores foreign keys, which would break the relation guarantees.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .errors import SchemaError
from .models import utcnow_iso

__all__ = [
    "ENV_DB_PATH",
    "DEFAULT_DB_PATH",
    "Migration",
    "MIGRATIONS",
    "SUPPORTED_SCHEMA_VERSION",
    "REQUIRED_COLUMNS",
    "SEARCH_INDEX_TABLES",
    "SEARCH_INDEX_TRIGGER_NAMES",
    "MigrationReport",
    "Database",
    "resolve_db_path",
]

ENV_DB_PATH = "PERSONAL_MEMORY_DB"
DEFAULT_DB_PATH = Path("data") / "memory.db"

# --------------------------------------------------------------------------
# Schema (migration 1)
# --------------------------------------------------------------------------
#
# Application-level validation and these CHECK constraints intentionally
# overlap: the app layer produces good error messages, SQLite is the last line
# of defence against writes that bypass the app layer (raw SQL, other tools).
#
_SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT    PRIMARY KEY,
    source_type   TEXT    NOT NULL
                          CHECK (source_type IN ('text', 'chat', 'article', 'web', 'file')),
    title         TEXT    NOT NULL CHECK (length(trim(title)) > 0),
    content       TEXT    NOT NULL CHECK (length(trim(content)) > 0),
    url           TEXT    CHECK (url IS NULL OR url LIKE 'http%'),
    content_hash  TEXT    NOT NULL UNIQUE
                          CHECK (length(content_hash) = 64 AND content_hash = lower(content_hash)),
    metadata_json TEXT    NOT NULL DEFAULT '{}'
                          CHECK (json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
    created_at    TEXT    NOT NULL CHECK (length(created_at) > 0),
    updated_at    TEXT    NOT NULL CHECK (length(updated_at) > 0)
)
"""

_MEMORIES_DDL = """
CREATE TABLE IF NOT EXISTS memories (
    id                 TEXT    PRIMARY KEY,
    type               TEXT    NOT NULL
                               CHECK (type IN ('knowledge', 'experience', 'event', 'profile')),
    title              TEXT    NOT NULL CHECK (length(trim(title)) > 0),
    content            TEXT    NOT NULL CHECK (length(trim(content)) > 0),
    summary            TEXT,
    tags_json          TEXT    NOT NULL DEFAULT '[]'
                               CHECK (json_valid(tags_json) AND json_type(tags_json) = 'array'),
    importance         REAL    NOT NULL DEFAULT 0.5
                               CHECK (typeof(importance) IN ('integer', 'real')
                                      AND importance >= 0.0 AND importance <= 1.0),
    confidence         REAL    NOT NULL DEFAULT 0.5
                               CHECK (typeof(confidence) IN ('integer', 'real')
                                      AND confidence >= 0.0 AND confidence <= 1.0),
    information_origin TEXT    NOT NULL
                               CHECK (information_origin IN ('user_explicit', 'source_content', 'agent_inference')),
    status             TEXT    NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active', 'pending', 'archived')),
    created_at         TEXT    NOT NULL CHECK (length(created_at) > 0),
    updated_at         TEXT    NOT NULL CHECK (length(updated_at) > 0),
    schema_version     INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""

_MEMORY_SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS memory_sources (
    memory_id  TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    source_id  TEXT NOT NULL REFERENCES sources(id)  ON DELETE CASCADE,
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    PRIMARY KEY (memory_id, source_id)
)
"""

_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_sources_type_created ON sources (source_type, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_sources_created_at ON sources (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_memories_type_status ON memories (type, status)",
    "CREATE INDEX IF NOT EXISTS idx_memories_status_created ON memories (status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories (importance)",
    "CREATE INDEX IF NOT EXISTS idx_memory_sources_source ON memory_sources (source_id, memory_id)",
)

_MIGRATIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    applied_at TEXT    NOT NULL
)
"""

INITIAL_SCHEMA_STATEMENTS: tuple[str, ...] = (
    _SOURCES_DDL,
    _MEMORIES_DDL,
    _MEMORY_SOURCES_DDL,
    *_INDEX_DDL,
)


# --------------------------------------------------------------------------
# Search index (migration 2) -- Phase 3
# --------------------------------------------------------------------------
#
# Two FTS5 tables, because one tokenizer cannot serve both languages (measured
# in scripts/phase3_fts5_lab.py, SQLite 3.45.3):
#   * unicode61 -> word matching for Latin/digits ("RAG" hits RAG, not storage;
#     ranked: bm25 -0.37/-0.32/-0.29 across three rows)
#   * trigram   -> substring matching for Chinese (unicode61 finds NOTHING for
#     "长期记忆", trigram finds it and ranks it)
# Neither index can match a 2-character Chinese query (trigram needs >=3 chars),
# so Retrieval adds a parameterised LIKE fallback for short/unmatched tokens.
#
_FTS_COLUMNS: tuple[str, ...] = ("memory_id", "title", "content", "summary", "tags")

#: ``memories.tags_json`` holds a JSON array; index the joined values, not the JSON syntax.
_TAGS_TEXT = "COALESCE((SELECT group_concat(value, ' ') FROM json_each(%s.tags_json)), '')"

SEARCH_INDEX_TABLES: dict[str, str] = {
    "word": "memory_fts_word",
    "trigram": "memory_fts_trigram",
}

SEARCH_INDEX_DDL: tuple[str, ...] = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_word USING fts5("
    "memory_id UNINDEXED, title, content, summary, tags, tokenize='unicode61')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_trigram USING fts5("
    "memory_id UNINDEXED, title, content, summary, tags, tokenize='trigram')",
)


def _fts_backfill(table: str) -> str:
    """Index every existing Memory (runs once, inside migration 2)."""
    return (
        f"INSERT INTO {table} (memory_id, title, content, summary, tags) "
        f"SELECT id, title, content, COALESCE(summary, ''), {_TAGS_TEXT % 'memories'} FROM memories"
    )


def _fts_insert_row(table: str, source: str) -> str:
    return (
        f"INSERT INTO {table} (memory_id, title, content, summary, tags) VALUES ("
        f"{source}.id, {source}.title, {source}.content, COALESCE({source}.summary, ''), "
        f"{_TAGS_TEXT % source})"
    )


def _fts_triggers(table: str, prefix: str) -> tuple[str, ...]:
    """Keep the index in sync on INSERT / UPDATE / DELETE -- no rescan needed."""
    return (
        f"CREATE TRIGGER IF NOT EXISTS {prefix}_ai AFTER INSERT ON memories "
        f"BEGIN {_fts_insert_row(table, 'new')}; END",
        f"CREATE TRIGGER IF NOT EXISTS {prefix}_au AFTER UPDATE ON memories "
        f"BEGIN DELETE FROM {table} WHERE memory_id = old.id; {_fts_insert_row(table, 'new')}; END",
        f"CREATE TRIGGER IF NOT EXISTS {prefix}_ad AFTER DELETE ON memories "
        f"BEGIN DELETE FROM {table} WHERE memory_id = old.id; END",
    )


SEARCH_INDEX_TRIGGER_NAMES: tuple[str, ...] = tuple(
    f"{prefix}_{suffix}"
    for prefix in ("memory_fts_word", "memory_fts_trigram")
    for suffix in ("ai", "au", "ad")
)

SEARCH_INDEX_STATEMENTS: tuple[str, ...] = (
    *SEARCH_INDEX_DDL,
    _fts_backfill("memory_fts_word"),
    _fts_backfill("memory_fts_trigram"),
    *_fts_triggers("memory_fts_word", "memory_fts_word"),
    *_fts_triggers("memory_fts_trigram", "memory_fts_trigram"),
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    #: Tables this migration is responsible for; verified after initialisation
    #: so a ``schema_migrations`` record can never vouch for a missing schema.
    tables: tuple[str, ...] = ()
    #: Triggers this migration is responsible for (index synchronisation).
    triggers: tuple[str, ...] = ()


#: Ordered migration list.  Never edit an applied migration; append a new one.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_schema",
        statements=INITIAL_SCHEMA_STATEMENTS,
        tables=("sources", "memories", "memory_sources", "schema_migrations"),
    ),
    Migration(
        version=2,
        name="memory_search_index",
        statements=SEARCH_INDEX_STATEMENTS,
        tables=("memory_fts_word", "memory_fts_trigram"),
        triggers=SEARCH_INDEX_TRIGGER_NAMES,
    ),
)

#: Highest migration version this build knows about.
SUPPORTED_SCHEMA_VERSION = max(migration.version for migration in MIGRATIONS)

#: Columns this code reads/writes.  Verified after initialisation so that a
#: same-named but wrongly-shaped file fails loudly during ``initialize()``
#: instead of later with a raw ``sqlite3.OperationalError: no such column``.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "sources": (
        "id", "source_type", "title", "content", "url", "content_hash",
        "metadata_json", "created_at", "updated_at",
    ),
    "memories": (
        "id", "type", "title", "content", "summary", "tags_json", "importance",
        "confidence", "information_origin", "status", "created_at", "updated_at",
        "schema_version",
    ),
    "memory_sources": ("memory_id", "source_id", "created_at"),
    "schema_migrations": ("version", "name", "applied_at"),
    "memory_fts_word": _FTS_COLUMNS,
    "memory_fts_trigram": _FTS_COLUMNS,
}


@dataclass(frozen=True)
class MigrationReport:
    """Result of :meth:`Database.initialize`."""

    db_path: str
    applied: tuple[tuple[int, str], ...]
    version: int

    @property
    def applied_count(self) -> int:
        return len(self.applied)

    def as_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "applied": [{"version": v, "name": n} for v, n in self.applied],
            "schema_version": self.version,
        }


def resolve_db_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the database path: CLI argument > ``PERSONAL_MEMORY_DB`` > ``data/memory.db``."""
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get(ENV_DB_PATH)
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_DB_PATH


class Database:
    """Owns one SQLite file: connections, migrations and a read-only description."""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> None:
        self.path = Path(path).expanduser()

    # -- connections -------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """Open a connection with foreign keys enforced and dict-like rows."""
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        # REPLACE resolution deletes the conflicting row; SQLite only fires the
        # implicit DELETE trigger when recursive triggers are enabled, which is what
        # keeps the Phase 3 search index in sync for raw-SQL writers too.
        conn.execute("PRAGMA recursive_triggers = ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Read-only usage: open, yield, close."""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Write usage: BEGIN IMMEDIATE, COMMIT on success, ROLLBACK on any error."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - connection already dead
                    pass
                raise
        finally:
            conn.close()

    # -- schema state ------------------------------------------------------
    def is_initialized(self) -> bool:
        if not self.path.exists():
            return False
        with self.connection() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
        return row is not None

    def applied_migrations(self) -> list[tuple[int, str, str]]:
        """Applied ``(version, name, applied_at)``, oldest first."""
        with self.connection() as conn:
            if not self._has_table(conn, "schema_migrations"):
                return []
            rows = conn.execute(
                "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        return [(int(r["version"]), str(r["name"]), str(r["applied_at"])) for r in rows]

    def schema_version(self) -> int:
        """Highest applied migration version (0 when the file is empty/new)."""
        if not self.path.exists():
            return 0
        with self.connection() as conn:
            if not self._has_table(conn, "schema_migrations"):
                return 0
            row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
        return int(row["v"])

    def verify_ready(self) -> int:
        """Check the file is an initialised, correctly shaped database; return its version."""
        if not self.path.exists():
            raise SchemaError(f"{self.path} does not exist; call initialize() first")
        try:
            with self.connection() as conn:
                if not self._has_table(conn, "schema_migrations"):
                    raise SchemaError(
                        f"{self.path} is not initialised (no schema_migrations table); call initialize() first"
                    )
                version = self._current_version(conn)
                if version > SUPPORTED_SCHEMA_VERSION:
                    raise SchemaError(
                        f"{self.path} is at schema version {version}, but this build only supports "
                        f"up to {SUPPORTED_SCHEMA_VERSION}; upgrade the code instead of downgrading the data"
                    )
                self._verify_schema(conn, version)
        except sqlite3.DatabaseError as exc:
            # SchemaError is not a sqlite3 error, so a SchemaError raised above propagates untouched
            raise SchemaError(f"{self.path} is not a usable SQLite database: {exc}") from exc
        return version

    # -- initialisation / migration ---------------------------------------
    def initialize(self) -> MigrationReport:
        """Create the file if needed, apply pending migrations, then verify the result.

        Raises :class:`~personal_memory.errors.SchemaError` when the file is
        newer than this build, is not a SQLite database at all, or when a
        recorded migration's tables/columns are missing (the file is truncated,
        wrongly shaped, hand-made or otherwise foreign).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError as exc:
            raise SchemaError(f"{self.path} is not a usable SQLite database: {exc}") from exc
        finally:
            conn.close()

        applied: list[tuple[int, str]] = []
        with self.transaction() as conn:
            conn.execute(_MIGRATIONS_TABLE_DDL)
            current = self._current_version(conn)
            if current > SUPPORTED_SCHEMA_VERSION:
                raise SchemaError(
                    f"{self.path} is at schema version {current}, but this build only supports "
                    f"up to {SUPPORTED_SCHEMA_VERSION}; upgrade the code instead of downgrading the data"
                )
            if current:
                # verify what the file CLAIMS before running anything new: a migration
                # (e.g. the v2 index backfill reading `memories`) must not be the first
                # thing to discover that the file is lying about its schema
                self._verify_schema(conn, current)
            for migration in MIGRATIONS:
                if migration.version <= current:
                    continue
                for table in migration.tables:
                    # a same-named table with the wrong shape would make the migration
                    # fail halfway with a raw sqlite3 error; refuse it up front instead
                    if self._has_object(conn, "table", table):
                        self._verify_columns(conn, table)
                for statement in migration.statements:
                    try:
                        conn.execute(statement)
                    except sqlite3.Error as exc:
                        raise SchemaError(
                            f"migration {migration.version} ({migration.name}) failed on "
                            f"{self.path}: {exc}"
                        ) from exc
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, utcnow_iso()),
                )
                applied.append((migration.version, migration.name))

            effective_version = max(current, *(version for version, _ in applied), 0)
            self._verify_schema(conn, effective_version)

        return MigrationReport(
            db_path=str(self.path),
            applied=tuple(applied),
            version=self.schema_version(),
        )

    def _verify_schema(self, conn: sqlite3.Connection, version: int) -> None:
        """Tables promised by the applied migrations must exist AND have the expected columns."""
        expected = {
            table
            for migration in MIGRATIONS
            if migration.version <= version
            for table in migration.tables
        }
        missing = sorted(table for table in expected if not self._has_table(conn, table))
        if missing:
            raise SchemaError(
                f"{self.path} claims schema version {version} but is missing table(s) "
                f"{missing}; the file is corrupt, truncated or was not created by this tool"
            )
        for table in sorted(expected & set(REQUIRED_COLUMNS)):
            self._verify_columns(conn, table, version=version)
        expected_triggers = {
            trigger
            for migration in MIGRATIONS
            if migration.version <= version
            for trigger in migration.triggers
        }
        missing_triggers = sorted(
            trigger for trigger in expected_triggers if not self._has_object(conn, "trigger", trigger)
        )
        if missing_triggers:
            raise SchemaError(
                f"{self.path} is missing index-synchronisation trigger(s) {missing_triggers}; "
                "the search index would silently drift, so this is refused"
            )

    def _verify_columns(self, conn: sqlite3.Connection, table: str, *, version: int | None = None) -> None:
        """A table this code relies on must expose every column it reads/writes."""
        required = REQUIRED_COLUMNS.get(table)
        if not required:
            return
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing_columns = [column for column in required if column not in present]
        if missing_columns:
            where = f"predates schema version {version}" if version is not None else "has the wrong shape"
            raise SchemaError(
                f"{self.path} has table {table!r} but is missing column(s) {missing_columns}; "
                f"the file was not created by this tool (or {where})"
            )

    # -- introspection (used by the CLI and tests) -------------------------
    def describe(self) -> dict[str, Any]:
        """Schema version, object DDL and row counts, straight from the file."""
        if not self.path.exists():
            return {"db_path": str(self.path), "exists": False, "schema_version": 0, "tables": {}, "indexes": {}, "counts": {}}
        with self.connection() as conn:
            tables = {
                r["name"]: r["sql"]
                for r in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            }
            indexes = {
                r["name"]: r["sql"]
                for r in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL ORDER BY name"
                ).fetchall()
            }
            counts: dict[str, int] = {}
            for table in (
                "sources",
                "memories",
                "memory_sources",
                "schema_migrations",
                "memory_fts_word",
                "memory_fts_trigram",
            ):
                if table in tables:
                    counts[table] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            version = self._current_version(conn)
        return {
            "db_path": str(self.path),
            "exists": True,
            "schema_version": version,
            "tables": tables,
            "indexes": indexes,
            "counts": counts,
        }

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _has_object(conn: sqlite3.Connection, kind: str, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?", (kind, name)
        ).fetchone()
        return row is not None

    @classmethod
    def _has_table(cls, conn: sqlite3.Connection, name: str) -> bool:
        return cls._has_object(conn, "table", name)

    @classmethod
    def _current_version(cls, conn: sqlite3.Connection) -> int:
        if not cls._has_table(conn, "schema_migrations"):
            return 0
        row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
        return int(row["v"])
