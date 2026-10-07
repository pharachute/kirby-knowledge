"""Data access layer: CRUD for Source / Memory and the Source<->Memory relation.

All writes go through here.  Two guarantees are enforced:

1. **Application validation** -- every payload is a validated dataclass
   (:mod:`personal_memory.models`), so a bad value raises ``ValidationError``
   before SQL is built.
2. **Database enforcement** -- SQLite CHECK / UNIQUE / FOREIGN KEY constraints
   are translated back into the :mod:`personal_memory.errors` hierarchy, so a
   write that slips past the app layer is still reported as a typed error
   instead of a raw ``sqlite3.IntegrityError``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Iterator, Sequence

from .db import SEARCH_INDEX_TABLES, Database
from .errors import (
    ConflictError,
    DuplicateContentHashError,
    IllegalTransitionError,
    MemorySystemError,
    NotFoundError,
    ReferentialIntegrityError,
    ValidationError,
)
from .models import (
    ALLOWED_TRANSITIONS,
    Memory,
    MemoryStatus,
    MemoryType,
    Source,
    coerce_enum,
    compute_content_hash,
    utcnow_iso,
)

__all__ = ["MemoryRepository", "MemoryUnitOfWork", "SearchIndexHit"]

SOURCE_COLUMNS: tuple[str, ...] = (
    "id",
    "source_type",
    "title",
    "content",
    "url",
    "content_hash",
    "metadata_json",
    "created_at",
    "updated_at",
)

MEMORY_COLUMNS: tuple[str, ...] = (
    "id",
    "type",
    "title",
    "content",
    "summary",
    "tags_json",
    "importance",
    "confidence",
    "information_origin",
    "status",
    "created_at",
    "updated_at",
    "schema_version",
)

SOURCE_UPDATE_FIELDS = frozenset({"source_type", "title", "content", "url", "metadata"})
MEMORY_UPDATE_FIELDS = frozenset(
    {
        "type",
        "title",
        "content",
        "summary",
        "tags",
        "importance",
        "confidence",
        "information_origin",
        "status",
    }
)


def _column_list(columns: Sequence[str]) -> str:
    return ", ".join(columns)


def _placeholders(columns: Sequence[str]) -> str:
    return ", ".join("?" for _ in columns)


def _update_assignment(columns: Sequence[str]) -> str:
    return ", ".join(f"{column} = ?" for column in columns)


def _enum_filter(value: Any, enum_cls: type, field_name: str) -> str:
    """Validate a list filter against its enum instead of silently matching nothing."""
    if isinstance(value, enum_cls):
        return str(value)
    if isinstance(value, str):
        try:
            return str(enum_cls(value.strip().lower()))
        except ValueError:
            pass
    allowed = ", ".join(member.value for member in enum_cls)
    raise ValidationError(f"{field_name} must be one of [{allowed}], got {value!r}", field=field_name)


@dataclass(frozen=True)
class SearchIndexHit:
    """One raw hit from a Phase 3 FTS5 index.

    ``bm25`` is SQLite's raw value (**more negative = better match**);
    ``score`` is its negation so that *higher is better* everywhere in this
    package.  No normalisation to 0..1 is attempted, by design.
    """

    memory_id: str
    score: float
    bm25: float
    index: str
    created_at: str = ""


def _escape_like(text: str) -> str:
    """Make a user token safe inside ``LIKE ... ESCAPE '\\'``."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filter_clause(
    statuses: Sequence[str] | None, memory_type: str | None, params: list[Any]
) -> str:
    """Build the shared ``status``/``type`` predicate; values are always bound."""
    clauses: list[str] = []
    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"m.status IN ({placeholders})")
        params.extend(statuses)
    if memory_type is not None:
        clauses.append("m.type = ?")
        params.append(memory_type)
    return f" AND {' AND '.join(clauses)}" if clauses else ""


class MemoryRepository:
    """Minimal, explicit data access API for Phase 1.

    Constructing the repository initialises the database (idempotent), so
    ``MemoryRepository(Database(path))`` is always usable.  With
    ``initialize=False`` the schema is still verified, so a missing or
    wrongly-shaped file raises ``SchemaError`` instead of a raw sqlite error.
    """

    def __init__(self, database: Database, *, initialize: bool = True) -> None:
        self.database = database
        if initialize:
            self.database.initialize()
        else:
            self.database.verify_ready()

    @contextmanager
    def transaction(self) -> Iterator["MemoryUnitOfWork"]:
        """One SQLite transaction spanning several writes.

        Phase 1 exposed only single-operation transactions.  Memory Formation
        must write ``Source + Memory rows + memory_sources links`` as ONE unit,
        so this yields a :class:`MemoryUnitOfWork` bound to a single connection:
        any exception inside the ``with`` block rolls the whole formation back.
        """
        with self.database.transaction() as conn:
            yield MemoryUnitOfWork(conn, self)

    # ==================================================================
    # Source
    # ==================================================================
    def create_source(self, source: Source) -> Source:
        """Persist a new Source. Raises ``DuplicateContentHashError`` on a hash collision."""
        with self.database.transaction() as conn:
            self._insert_source(conn, source)
        return source

    def get_source(self, source_id: str) -> Source | None:
        with self.database.connection() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return Source.from_record(row) if row is not None else None

    def require_source(self, source_id: str) -> Source:
        source = self.get_source(source_id)
        if source is None:
            raise NotFoundError("source", source_id)
        return source

    def find_source_by_content_hash(self, content_hash: str) -> Source | None:
        """Deduplication lookup: the Source already holding this content hash, if any."""
        with self.database.connection() as conn:
            return self._find_source_by_hash(conn, content_hash)

    @staticmethod
    def _find_source_by_hash(conn: sqlite3.Connection, content_hash: str) -> Source | None:
        row = conn.execute("SELECT * FROM sources WHERE content_hash = ?", (content_hash,)).fetchone()
        return Source.from_record(row) if row is not None else None

    def source_exists_by_hash(self, content_hash: str) -> bool:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM sources WHERE content_hash = ?", (content_hash,)
            ).fetchone()
        return row is not None

    def list_sources(self, *, limit: int = 100, offset: int = 0) -> list[Source]:
        with self.database.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM sources ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            ).fetchall()
        return [Source.from_record(row) for row in rows]

    def update_source(self, source_id: str, **changes: Any) -> Source:
        """Patch title/content/url/metadata/source_type; ``updated_at`` is refreshed.

        Changing ``content`` recomputes ``content_hash`` automatically.
        """
        unknown = set(changes) - SOURCE_UPDATE_FIELDS
        if unknown:
            raise ValidationError(
                f"unsupported Source field(s) for update: {sorted(unknown)}; "
                f"allowed: {sorted(SOURCE_UPDATE_FIELDS)}"
            )
        if not changes:
            return self.require_source(source_id)
        payload = dict(changes)
        if isinstance(payload.get("metadata"), Mapping):
            payload["metadata"] = dict(payload["metadata"])
        # a non-mapping (e.g. a string) is passed through so Source.validate()
        # rejects it with ValidationError instead of a raw ValueError
        with self.database.transaction() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row is None:
                raise NotFoundError("source", source_id)
            updated = replace(Source.from_record(row), **payload, updated_at=utcnow_iso())
            if "content" in changes:
                updated.content_hash = compute_content_hash(updated.content)
            record = updated.to_record()
            update_columns = tuple(c for c in SOURCE_COLUMNS if c != "id")
            try:
                conn.execute(
                    f"UPDATE sources SET {_update_assignment(update_columns)} WHERE id = ?",
                    tuple(record[column] for column in update_columns) + (source_id,),
                )
            except sqlite3.IntegrityError as exc:
                raise self._source_integrity_error(exc, conn, updated.content_hash) from exc
        return updated

    def delete_source(self, source_id: str) -> bool:
        """Delete a Source. Relation rows cascade; the Memories themselves stay."""
        with self.database.transaction() as conn:
            cursor = conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            return cursor.rowcount > 0

    # ==================================================================
    # Memory
    # ==================================================================
    def create_memory(self, memory: Memory) -> Memory:
        with self.database.transaction() as conn:
            self._insert_memory(conn, memory)
        return memory

    def get_memory(self, memory_id: str) -> Memory | None:
        with self.database.connection() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return Memory.from_record(row) if row is not None else None

    def require_memory(self, memory_id: str) -> Memory:
        memory = self.get_memory(memory_id)
        if memory is None:
            raise NotFoundError("memory", memory_id)
        return memory

    def list_memories(
        self,
        *,
        memory_type: MemoryType | str | None = None,
        status: MemoryStatus | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Memory]:
        """List memories, newest first, optionally filtered by type/status.

        An unknown ``memory_type``/``status`` raises ``ValidationError`` instead
        of silently returning an empty list.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if memory_type is not None:
            clauses.append("type = ?")
            params.append(_enum_filter(memory_type, MemoryType, "memory_type"))
        if status is not None:
            clauses.append("status = ?")
            params.append(_enum_filter(status, MemoryStatus, "status"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([int(limit), int(offset)])
        with self.database.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM memories {where} ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [Memory.from_record(row) for row in rows]

    def iter_memories(
        self,
        *,
        memory_type: MemoryType | str | None = None,
        statuses: Sequence[MemoryStatus | str] | None = None,
        batch_size: int = 200,
    ) -> Iterator[Memory]:
        """Stream Memories instead of materialising the whole table.

        ``statuses=None`` means *every* status.  Phase 4's exact-duplicate scan
        wants exactly that: an exact duplicate of an ``archived`` or ``pending``
        Memory must not be recreated as a second row either.
        """
        with self.database.connection() as conn:
            yield from self._iter_memories(
                conn, memory_type=memory_type, statuses=statuses, batch_size=batch_size
            )

    def status_counts(self) -> dict[str, int]:
        """How many Memories sit in each lifecycle status (the pending review queue size)."""
        counts = {str(status): 0 for status in MemoryStatus}
        with self.database.connection() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM memories GROUP BY status").fetchall()
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        return counts

    def count_links_for_memory(self, memory_id: str) -> int:
        """Number of ``memory_sources`` rows pointing at one Memory."""
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_sources WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        return int(row["n"])

    def update_memory(self, memory_id: str, **changes: Any) -> Memory:
        """Patch the mutable Memory fields; ``updated_at`` is refreshed.

        ``id``, ``created_at`` and ``schema_version`` are immutable.

        Phase 4: changing ``status`` on an **existing** Memory is a lifecycle
        transition and must obey
        :data:`personal_memory.models.ALLOWED_TRANSITIONS` -- the very same table the
        lifecycle service uses, so ``active -> pending`` / ``archived -> pending``
        raise :class:`~personal_memory.errors.IllegalTransitionError` here too.
        A ``status`` patch that repeats the current status is a no-op: the status is
        not written, and a status-only patch does not even touch ``updated_at``.

        :meth:`create_memory` (and ``MemoryUnitOfWork.create_memory``) stay **ungated
        on purpose**: Memory Formation must be able to *create* a ``pending`` Memory
        (low-confidence inference, suspected conflict).  The rule is about rewriting
        an existing Memory, not about inserting a new one.
        """
        unknown = set(changes) - MEMORY_UPDATE_FIELDS
        if unknown:
            raise ValidationError(
                f"unsupported Memory field(s) for update: {sorted(unknown)}; "
                f"allowed: {sorted(MEMORY_UPDATE_FIELDS)}"
            )
        if not changes:
            return self.require_memory(memory_id)
        payload = dict(changes)
        if "tags" in payload:
            tags = payload["tags"]
            if tags is None:
                # symmetric with update_source(metadata=None): None clears the list
                payload["tags"] = []
            elif isinstance(tags, str) or not isinstance(tags, Sequence):
                pass  # let Memory.validate() reject it ("abc" must not become [a, b, c])
            else:
                payload["tags"] = list(tags)
        with self.database.transaction() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                raise NotFoundError("memory", memory_id)
            current = Memory.from_record(row)
            if "status" in payload:
                target = str(coerce_enum(payload["status"], MemoryStatus, "status"))
                current_status = str(current.status)
                if target == current_status:
                    # repeating the current status is not a transition
                    payload.pop("status")
                    if not payload:
                        return current
                else:
                    allowed = ALLOWED_TRANSITIONS[current_status]
                    if target not in allowed:
                        raise IllegalTransitionError(
                            memory_id, current_status, target, allowed=allowed
                        )
                    payload["status"] = target
            updated = replace(current, **payload, updated_at=utcnow_iso())
            record = updated.to_record()
            update_columns = tuple(c for c in MEMORY_COLUMNS if c != "id")
            try:
                conn.execute(
                    f"UPDATE memories SET {_update_assignment(update_columns)} WHERE id = ?",
                    tuple(record[column] for column in update_columns) + (memory_id,),
                )
            except sqlite3.IntegrityError as exc:
                raise self._memory_integrity_error(exc) from exc
        return updated

    def delete_memory(self, memory_id: str) -> bool:
        """Hard-delete a Memory. Its relation rows cascade; Sources are untouched."""
        with self.database.transaction() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cursor.rowcount > 0

    # ==================================================================
    # Relation: memory_sources (many-to-many)
    # ==================================================================
    def link(self, memory_id: str, source_id: str) -> bool:
        """Create a Memory<->Source link. Idempotent: True when newly created."""
        with self.database.transaction() as conn:
            return self._insert_link(conn, memory_id, source_id)

    def link_many(self, memory_id: str, source_ids: Sequence[str]) -> int:
        """Link one Memory to several Sources atomically.

        Returns the number of newly created links.  Every id is validated
        first, so a bad id cannot leave a half-applied batch behind.
        """
        ordered_ids = list(dict.fromkeys(source_ids))
        created = 0
        with self.database.transaction() as conn:
            for source_id in ordered_ids:
                if self._insert_link(conn, memory_id, source_id):
                    created += 1
            return created

    def unlink(self, memory_id: str, source_id: str) -> bool:
        """Remove a link. True when a row was actually deleted."""
        with self.database.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM memory_sources WHERE memory_id = ? AND source_id = ?",
                (memory_id, source_id),
            )
            return cursor.rowcount > 0

    def is_linked(self, memory_id: str, source_id: str) -> bool:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM memory_sources WHERE memory_id = ? AND source_id = ?",
                (memory_id, source_id),
            ).fetchone()
        return row is not None

    def get_sources_for_memory(self, memory_id: str) -> list[Source]:
        """All Sources a Memory was derived from (raises if the Memory is unknown)."""
        with self.database.connection() as conn:
            if conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone() is None:
                raise NotFoundError("memory", memory_id)
            rows = conn.execute(
                "SELECT s.* FROM sources AS s "
                "JOIN memory_sources AS ms ON ms.source_id = s.id "
                "WHERE ms.memory_id = ? ORDER BY s.created_at, s.id",
                (memory_id,),
            ).fetchall()
        return [Source.from_record(row) for row in rows]

    def get_memories_for_source(self, source_id: str) -> list[Memory]:
        """All Memories derived from a Source (raises if the Source is unknown)."""
        with self.database.connection() as conn:
            if conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:
                raise NotFoundError("source", source_id)
            rows = conn.execute(
                "SELECT m.* FROM memories AS m "
                "JOIN memory_sources AS ms ON ms.memory_id = m.id "
                "WHERE ms.source_id = ? ORDER BY m.created_at, m.id",
                (source_id,),
            ).fetchall()
        return [Memory.from_record(row) for row in rows]

    def link_count(self) -> int:
        with self.database.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM memory_sources").fetchone()
        return int(row["n"])

    def counts(self) -> dict[str, int]:
        """Row counts of the three Phase 1 tables (plus relation rows)."""
        with self.database.connection() as conn:
            return {
                "sources": int(conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]),
                "memories": int(conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]),
                "memory_sources": int(conn.execute("SELECT COUNT(*) AS n FROM memory_sources").fetchone()["n"]),
            }

    # ==================================================================
    # Phase 3: search index access (all queries parameterised)
    # ==================================================================
    def search_index(
        self,
        index: str,
        match_expression: str,
        *,
        statuses: Sequence[str] | None = None,
        memory_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SearchIndexHit]:
        """Ranked FTS5 lookup against ``word`` or ``trigram``.

        ``match_expression`` must already be a safe FTS expression (see
        ``retrieval.build_match_expression``); it is still passed as a bound
        parameter, never concatenated into the SQL text.
        """
        if index not in SEARCH_INDEX_TABLES:
            raise ValidationError(
                f"unknown search index {index!r}; expected one of {sorted(SEARCH_INDEX_TABLES)}"
            )
        table = SEARCH_INDEX_TABLES[index]
        params: list[Any] = [match_expression]
        where = _filter_clause(statuses, memory_type, params)
        # bm25() takes the FTS table name, so the table is not aliased here
        sql = (
            f"SELECT {table}.memory_id AS memory_id, bm25({table}) AS bm25, "
            f"       m.created_at AS created_at "
            f"FROM {table} JOIN memories AS m ON m.id = {table}.memory_id "
            f"WHERE {table} MATCH ?{where} "
            f"ORDER BY bm25 ASC, m.created_at DESC, m.id ASC"
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])
        with self.database.connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            SearchIndexHit(
                memory_id=row["memory_id"],
                score=-float(row["bm25"]),
                bm25=float(row["bm25"]),
                index=index,
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def search_like(
        self,
        token: str,
        *,
        statuses: Sequence[str] | None = None,
        memory_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[str]:
        """Substring fallback over title/content/summary/tags (still parameterised).

        Used for tokens FTS5 cannot match: fewer than 3 characters in Chinese
        (trigram needs >=3) and single-character Latin tokens.
        """
        pattern = f"%{_escape_like(token)}%"
        # tags are matched through json_each so JSON punctuation ('[', '"', ',') can
        # never produce a hit that no human field actually contains
        params: list[Any] = [pattern, pattern, pattern, pattern]
        where = _filter_clause(statuses, memory_type, params)
        sql = (
            "SELECT m.id AS memory_id FROM memories AS m WHERE ("
            "m.title LIKE ? ESCAPE '\\' OR m.content LIKE ? ESCAPE '\\' "
            "OR COALESCE(m.summary, '') LIKE ? ESCAPE '\\' "
            "OR EXISTS (SELECT 1 FROM json_each(m.tags_json) WHERE json_each.value LIKE ? ESCAPE '\\')"
            f"){where} ORDER BY m.created_at DESC, m.id ASC"
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])
        with self.database.connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [row["memory_id"] for row in rows]

    def get_memory_created_at(self, memory_ids: Sequence[str]) -> dict[str, str]:
        """``created_at`` for the given ids -- used for deterministic tie-breaking."""
        unique_ids = list(dict.fromkeys(memory_ids))
        if not unique_ids:
            return {}
        found: dict[str, str] = {}
        with self.database.connection() as conn:
            for start in range(0, len(unique_ids), 400):
                chunk = unique_ids[start : start + 400]
                placeholders = ", ".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT id, created_at FROM memories WHERE id IN ({placeholders})", tuple(chunk)
                ).fetchall()
                for row in rows:
                    found[row["id"]] = str(row["created_at"])
        return found

    def index_row_count(self, index: str) -> int:
        """Rows currently held by one search index (used for consistency checks)."""
        if index not in SEARCH_INDEX_TABLES:
            raise ValidationError(f"unknown search index {index!r}")
        with self.database.connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {SEARCH_INDEX_TABLES[index]}"
            ).fetchone()
        return int(row["n"])

    def index_consistency(self) -> dict[str, Any]:
        """Compare the memories table with both indexes (drift detector)."""
        with self.database.connection() as conn:
            memories = int(conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
            word = int(conn.execute("SELECT COUNT(*) AS n FROM memory_fts_word").fetchone()["n"])
            trigram = int(conn.execute("SELECT COUNT(*) AS n FROM memory_fts_trigram").fetchone()["n"])
            orphan_word = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM memory_fts_word AS f "
                    "LEFT JOIN memories AS m ON m.id = f.memory_id WHERE m.id IS NULL"
                ).fetchone()["n"]
            )
            orphan_trigram = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM memory_fts_trigram AS f "
                    "LEFT JOIN memories AS m ON m.id = f.memory_id WHERE m.id IS NULL"
                ).fetchone()["n"]
            )
        return {
            "memories": memories,
            "word_index": word,
            "trigram_index": trigram,
            "orphan_word_rows": orphan_word,
            "orphan_trigram_rows": orphan_trigram,
            "consistent": memories == word == trigram and not orphan_word and not orphan_trigram,
        }

    def rebuild_search_index(self) -> dict[str, int]:
        """Repair path: repopulate both indexes from ``memories`` (one transaction).

        Triggers keep the index in sync for normal writes; this exists for the
        cases they cannot cover (raw ``INSERT OR REPLACE`` on a connection with
        recursive triggers off, manual index edits, a crash mid-write).
        """
        with self.database.transaction() as conn:
            for table in SEARCH_INDEX_TABLES.values():
                conn.execute(f"DELETE FROM {table}")
                conn.execute(
                    f"INSERT INTO {table} (memory_id, title, content, summary, tags) "
                    "SELECT id, title, content, COALESCE(summary, ''), "
                    "COALESCE((SELECT group_concat(value, ' ') FROM json_each(memories.tags_json)), '') "
                    "FROM memories"
                )
        return self.index_consistency()

    def get_memories_by_ids(self, memory_ids: Sequence[str]) -> dict[str, Memory]:
        """Batch-load Memories by id (one query per chunk, no N+1 per row)."""
        unique_ids = list(dict.fromkeys(memory_ids))
        found: dict[str, Memory] = {}
        with self.database.connection() as conn:
            for start in range(0, len(unique_ids), 400):
                chunk = unique_ids[start : start + 400]
                placeholders = ", ".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM memories WHERE id IN ({placeholders})", tuple(chunk)
                ).fetchall()
                for row in rows:
                    memory = Memory.from_record(row)
                    found[memory.id] = memory
        return found

    def get_sources_for_memories(self, memory_ids: Sequence[str]) -> dict[str, list[Source]]:
        """Batch-load the Source relations of several Memories at once."""
        unique_ids = list(dict.fromkeys(memory_ids))
        grouped: dict[str, list[Source]] = {memory_id: [] for memory_id in unique_ids}
        if not unique_ids:
            return grouped
        with self.database.connection() as conn:
            for start in range(0, len(unique_ids), 400):
                chunk = unique_ids[start : start + 400]
                placeholders = ", ".join("?" for _ in chunk)
                rows = conn.execute(
                    "SELECT ms.memory_id AS memory_id, s.* FROM memory_sources AS ms "
                    "JOIN sources AS s ON s.id = ms.source_id "
                    f"WHERE ms.memory_id IN ({placeholders}) "
                    "ORDER BY s.created_at, s.id",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    grouped.setdefault(row["memory_id"], []).append(Source.from_record(row))
        return grouped

    # ==================================================================
    # single-row writers (shared by the public API and MemoryUnitOfWork)
    # ==================================================================
    @staticmethod
    def _iter_memories(
        conn: sqlite3.Connection,
        *,
        memory_type: MemoryType | str | None = None,
        statuses: Sequence[MemoryStatus | str] | None = None,
        batch_size: int = 200,
    ) -> Iterator[Memory]:
        """Streaming read shared by the repository and by :class:`MemoryUnitOfWork`."""
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValidationError(f"batch_size must be an integer >= 1, got {batch_size!r}", field="batch_size")
        clauses: list[str] = []
        params: list[Any] = []
        if memory_type is not None:
            clauses.append("type = ?")
            params.append(_enum_filter(memory_type, MemoryType, "memory_type"))
        if statuses:
            ordered = [_enum_filter(status, MemoryStatus, "statuses") for status in statuses]
            placeholders = ", ".join("?" for _ in ordered)
            clauses.append(f"status IN ({placeholders})")
            params.extend(ordered)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = conn.execute(f"SELECT * FROM memories {where} ORDER BY created_at DESC, id", tuple(params))
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                yield Memory.from_record(row)

    def _insert_source(self, conn: sqlite3.Connection, source: Source) -> None:
        if not isinstance(source, Source):
            raise ValidationError(f"expected a Source instance, got {type(source).__name__}")
        record = source.to_record()
        try:
            conn.execute(
                f"INSERT INTO sources ({_column_list(SOURCE_COLUMNS)}) "
                f"VALUES ({_placeholders(SOURCE_COLUMNS)})",
                tuple(record[column] for column in SOURCE_COLUMNS),
            )
        except sqlite3.IntegrityError as exc:
            raise self._source_integrity_error(exc, conn, source.content_hash) from exc

    def _insert_memory(self, conn: sqlite3.Connection, memory: Memory) -> None:
        if not isinstance(memory, Memory):
            raise ValidationError(f"expected a Memory instance, got {type(memory).__name__}")
        record = memory.to_record()
        try:
            conn.execute(
                f"INSERT INTO memories ({_column_list(MEMORY_COLUMNS)}) "
                f"VALUES ({_placeholders(MEMORY_COLUMNS)})",
                tuple(record[column] for column in MEMORY_COLUMNS),
            )
        except sqlite3.IntegrityError as exc:
            raise self._memory_integrity_error(exc) from exc

    def _insert_link(self, conn: sqlite3.Connection, memory_id: str, source_id: str) -> bool:
        if conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone() is None:
            raise NotFoundError("memory", memory_id)
        if conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:
            raise NotFoundError("source", source_id)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO memory_sources (memory_id, source_id, created_at) VALUES (?, ?, ?)",
            (memory_id, source_id, utcnow_iso()),
        )
        return cursor.rowcount > 0

    # ==================================================================
    # integrity-error translation
    # ==================================================================
    def _source_integrity_error(
        self,
        exc: sqlite3.IntegrityError,
        conn: sqlite3.Connection,
        content_hash: str | None,
    ) -> MemorySystemError:
        message = str(exc)
        if "UNIQUE constraint failed" in message and "content_hash" in message and content_hash:
            row = conn.execute(
                "SELECT id FROM sources WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            return DuplicateContentHashError(content_hash, row["id"] if row is not None else None)
        if "UNIQUE constraint failed" in message and "sources.id" in message:
            return ConflictError(f"a source with this id already exists: {message}")
        return _translate_integrity_error(exc, entity="source")

    @staticmethod
    def _memory_integrity_error(exc: sqlite3.IntegrityError) -> MemorySystemError:
        message = str(exc)
        if "UNIQUE constraint failed" in message and "memories.id" in message:
            return ConflictError(f"a memory with this id already exists: {message}")
        return _translate_integrity_error(exc, entity="memory")


class MemoryUnitOfWork:
    """Several writes sharing ONE transaction (used by Memory Formation).

    Obtained from :meth:`MemoryRepository.transaction`; it never opens its own
    connection, so ``Source + Memories + links`` commit or roll back together.
    """

    def __init__(self, connection: sqlite3.Connection, repository: "MemoryRepository") -> None:
        self._conn = connection
        self._repository = repository

    def create_source(self, source: Source) -> Source:
        self._repository._insert_source(self._conn, source)
        return source

    def create_memory(self, memory: Memory) -> Memory:
        self._repository._insert_memory(self._conn, memory)
        return memory

    def link(self, memory_id: str, source_id: str) -> bool:
        return self._repository._insert_link(self._conn, memory_id, source_id)

    def find_source_by_content_hash(self, content_hash: str) -> Source | None:
        """Look the hash up inside this transaction (no check-then-insert window)."""
        return self._repository._find_source_by_hash(self._conn, content_hash)

    def iter_memories(
        self,
        *,
        memory_type: MemoryType | str | None = None,
        statuses: Sequence[MemoryStatus | str] | None = None,
        batch_size: int = 200,
    ) -> Iterator[Memory]:
        """Stream Memories **inside this transaction** (Phase 4 duplicate re-check).

        The caller materialises the scan *before* the first INSERT: the
        transaction already holds the write lock, so no other writer can slip an
        exact duplicate in between the scan and the insert.
        """
        yield from self._repository._iter_memories(
            self._conn, memory_type=memory_type, statuses=statuses, batch_size=batch_size
        )


def _translate_integrity_error(exc: sqlite3.IntegrityError, *, entity: str) -> MemorySystemError:
    """Map the remaining SQLite integrity failures onto the typed hierarchy."""
    message = str(exc)
    if "CHECK constraint failed" in message or "NOT NULL constraint failed" in message:
        return ValidationError(f"database rejected the {entity} row: {message}")
    if "FOREIGN KEY constraint failed" in message:
        return ReferentialIntegrityError(f"database rejected the {entity} row: {message}")
    if "UNIQUE constraint failed" in message:
        return ConflictError(f"database rejected the {entity} row: {message}")
    return MemorySystemError(f"database rejected the {entity} row: {message}")
