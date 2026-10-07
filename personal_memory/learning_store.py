"""Learning-layer data access (KB 1.1 Phase 2A).

The repository is intentionally **dumb**: it saves, reads, updates and lets the
database enforce the constraints.  It contains no teaching logic whatsoever --
no judgement of the user's understanding, no automatic ``learn_count`` bump, no
plan generation/sorting, no skipping, no LLM.

It reuses the 1.0 mechanisms instead of duplicating them:

* ``Database.connection()`` / ``Database.transaction()`` (same WAL, FK,
  ``BEGIN IMMEDIATE`` semantics as ``MemoryRepository``)
* the same ``_column_list`` / ``_placeholders`` / ``_update_assignment`` helpers
  and the same ``_translate_integrity_error`` mapping
* explicit update **allowlists** (``LEARNING_STATE_UPDATE_FIELDS`` /
  ``LEARNING_SESSION_UPDATE_FIELDS``) -- a caller can never smuggle an arbitrary
  column name into the UPDATE statement
* a ``LearningUnitOfWork`` so several learning writes (and, in Phase 3, "advance
  the session **and** update the state") commit or roll back as one unit:
  ``with repository.transaction() as tx: ...``
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Iterator, Sequence

from .db import Database
from .errors import (
    ConflictError,
    MemorySystemError,
    NotFoundError,
    ValidationError,
)
from .learning_models import (
    LearningSession,
    LearningState,
    SessionStatus,
)
from .models import coerce_enum, utcnow_iso
from .store import (
    _column_list,
    _placeholders,
    _translate_integrity_error,
    _update_assignment,
)

__all__ = [
    "LEARNING_STATE_COLUMNS",
    "LEARNING_SESSION_COLUMNS",
    "LEARNING_STATE_UPDATE_FIELDS",
    "LEARNING_SESSION_UPDATE_FIELDS",
    "LearningRepository",
    "LearningUnitOfWork",
]

LEARNING_STATE_COLUMNS: tuple[str, ...] = (
    "memory_id", "understanding_level", "known_aspects_json", "weak_aspects_json",
    "misconceptions_json", "learn_count", "last_learned_at", "created_at", "updated_at",
    "schema_version",
)

LEARNING_SESSION_COLUMNS: tuple[str, ...] = (
    "id", "source_id", "status", "current_memory_id", "current_stage", "plan_json",
    "plan_cursor", "exchange_json", "started_at", "updated_at", "ended_at", "schema_version",
)

#: ``memory_id`` is the identity of a state and is therefore NOT updatable.
LEARNING_STATE_UPDATE_FIELDS = frozenset(
    {
        "understanding_level",
        "known_aspects",
        "weak_aspects",
        "misconceptions",
        "learn_count",
        "last_learned_at",
    }
)

#: ``id``/``source_id``/``status`` are NOT updatable here: a session changes
#: status only through :meth:`LearningRepository.finish_session` /
#: :meth:`LearningRepository.abandon_session`.
LEARNING_SESSION_UPDATE_FIELDS = frozenset(
    {"current_memory_id", "current_stage", "plan", "plan_cursor", "exchange"}
)

MAX_LIST_LIMIT = 1000


class LearningRepository:
    """Data access for ``learning_states`` + ``learning_sessions``."""

    def __init__(self, database: Database, *, initialize: bool = True) -> None:
        self.database = database
        if initialize:
            self.database.initialize()
        else:
            self.database.verify_ready()

    @contextmanager
    def transaction(self) -> Iterator["LearningUnitOfWork"]:
        """One SQLite transaction spanning several learning writes.

        Phase 3 needs "advance the session **and** update the learning state" to
        be atomic; both calls go through the same unit of work.
        """
        with self.database.transaction() as conn:
            yield LearningUnitOfWork(conn, self)

    # ==================================================================
    # LearningState
    # ==================================================================
    def get_state(self, memory_id: str) -> LearningState | None:
        """The state of one Memory, or ``None`` when the user never studied it."""
        with self.database.connection() as conn:
            return self._get_state(conn, memory_id)

    def create_state(self, state: LearningState) -> LearningState:
        """Insert a state row. Raises ``ConflictError`` if one already exists."""
        with self.database.transaction() as conn:
            self._insert_state(conn, state)
        return state

    def upsert_state(self, state: LearningState) -> LearningState:
        """Insert or replace the state of ``state.memory_id`` (whole-row write).

        ``created_at`` is preserved for an existing row; everything else comes from
        the argument -- this is not a merge and it never touches ``learn_count``
        on its own (the caller passes the value it wants).
        """
        with self.database.transaction() as conn:
            self._upsert_state(conn, state)
        return state

    def update_state(self, memory_id: str, /, **changes: Any) -> LearningState:
        """Patch the mutable state fields; ``updated_at`` is refreshed.

        ``memory_id``, ``created_at`` and ``schema_version`` are immutable, and an
        unknown field is refused instead of being silently dropped.
        """
        unknown = set(changes) - LEARNING_STATE_UPDATE_FIELDS
        if unknown:
            raise ValidationError(
                f"unsupported LearningState field(s) for update: {sorted(unknown)}; "
                f"allowed: {sorted(LEARNING_STATE_UPDATE_FIELDS)}",
                problems=tuple((name, "not an updatable LearningState field") for name in sorted(unknown)),
            )
        with self.database.transaction() as conn:
            return self._update_state(conn, memory_id, changes)

    def count_states(self) -> int:
        with self.database.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM learning_states").fetchone()["n"])

    # ==================================================================
    # LearningSession
    # ==================================================================
    def get_session(self, session_id: str) -> LearningSession | None:
        with self.database.connection() as conn:
            return self._get_session(conn, session_id)

    def require_session(self, session_id: str) -> LearningSession:
        session = self.get_session(session_id)
        if session is None:
            raise NotFoundError("learning session", session_id)
        return session

    def create_session(self, session: LearningSession) -> LearningSession:
        """Insert a session.

        The partial unique index (``source_id`` where ``status = 'active'``) is the
        guard: a second active session for the same Source is refused by the
        database and translated into :class:`~personal_memory.errors.ConflictError`.
        """
        with self.database.transaction() as conn:
            self._insert_session(conn, session)
        return session

    def update_session(self, session_id: str, /, **changes: Any) -> LearningSession:
        """Patch the mutable session fields; ``updated_at`` is refreshed.

        ``status`` is deliberately **not** patchable here (use ``finish_session`` /
        ``abandon_session``), and neither are ``id`` / ``source_id`` / timestamps.
        """
        unknown = set(changes) - LEARNING_SESSION_UPDATE_FIELDS
        if unknown:
            raise ValidationError(
                f"unsupported LearningSession field(s) for update: {sorted(unknown)}; "
                f"allowed: {sorted(LEARNING_SESSION_UPDATE_FIELDS)}",
                problems=tuple((name, "not an updatable LearningSession field") for name in sorted(unknown)),
            )
        with self.database.transaction() as conn:
            return self._update_session(conn, session_id, changes)

    def finish_session(self, session_id: str) -> LearningSession:
        """Mark a running session ``completed`` (writes ``ended_at``)."""
        with self.database.transaction() as conn:
            return self._close_session(conn, session_id, SessionStatus.COMPLETED)

    def abandon_session(self, session_id: str) -> LearningSession:
        """Mark a running session ``abandoned`` (explicit user action)."""
        with self.database.transaction() as conn:
            return self._close_session(conn, session_id, SessionStatus.ABANDONED)

    def get_active_session_for_source(self, source_id: str) -> LearningSession | None:
        """The (at most one) running session of a Source, or ``None``."""
        with self.database.connection() as conn:
            return self._get_active_session_for_source(conn, source_id)

    def list_active_sessions(
        self, *, source_id: str | None = None, limit: int = 100
    ) -> list[LearningSession]:
        """Running sessions, most recently touched first."""
        limit = _check_limit(limit)
        sql = "SELECT * FROM learning_sessions WHERE status = ?"
        params: list[Any] = [str(SessionStatus.ACTIVE)]
        if source_id is not None:
            sql += " AND source_id = ?"
            params.append(source_id)
        sql += " ORDER BY updated_at DESC, id ASC LIMIT ?"
        params.append(limit)
        with self.database.connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [LearningSession.from_record(row) for row in rows]

    def counts(self) -> dict[str, int]:
        """Row counts of the two learning tables (used by tests and the CLI/info path)."""
        with self.database.connection() as conn:
            return {
                "learning_states": int(
                    conn.execute("SELECT COUNT(*) AS n FROM learning_states").fetchone()["n"]
                ),
                "learning_sessions": int(
                    conn.execute("SELECT COUNT(*) AS n FROM learning_sessions").fetchone()["n"]
                ),
            }

    # ==================================================================
    # internals (also used by LearningUnitOfWork -- one implementation)
    # ==================================================================
    @staticmethod
    def _get_state(conn: sqlite3.Connection, memory_id: str) -> LearningState | None:
        row = conn.execute("SELECT * FROM learning_states WHERE memory_id = ?", (memory_id,)).fetchone()
        return LearningState.from_record(row) if row is not None else None

    def _insert_state(self, conn: sqlite3.Connection, state: LearningState) -> None:
        if not isinstance(state, LearningState):
            raise ValidationError(f"expected a LearningState instance, got {type(state).__name__}")
        record = state.to_record()
        try:
            conn.execute(
                f"INSERT INTO learning_states ({_column_list(LEARNING_STATE_COLUMNS)}) "
                f"VALUES ({_placeholders(LEARNING_STATE_COLUMNS)})",
                tuple(record[column] for column in LEARNING_STATE_COLUMNS),
            )
        except sqlite3.IntegrityError as exc:
            raise _state_integrity_error(exc, conn, state.memory_id) from exc

    def _upsert_state(self, conn: sqlite3.Connection, state: LearningState) -> None:
        if not isinstance(state, LearningState):
            raise ValidationError(f"expected a LearningState instance, got {type(state).__name__}")
        record = state.to_record()
        mutable = tuple(c for c in LEARNING_STATE_COLUMNS if c != "memory_id")
        assignments = ", ".join(f"{column} = excluded.{column}" for column in mutable if column != "created_at")
        try:
            conn.execute(
                f"INSERT INTO learning_states ({_column_list(LEARNING_STATE_COLUMNS)}) "
                f"VALUES ({_placeholders(LEARNING_STATE_COLUMNS)}) "
                f"ON CONFLICT(memory_id) DO UPDATE SET {assignments}",
                tuple(record[column] for column in LEARNING_STATE_COLUMNS),
            )
        except sqlite3.IntegrityError as exc:
            raise _state_integrity_error(exc, conn, state.memory_id) from exc

    def _update_state(
        self, conn: sqlite3.Connection, memory_id: str, changes: dict[str, Any]
    ) -> LearningState:
        row = conn.execute("SELECT * FROM learning_states WHERE memory_id = ?", (memory_id,)).fetchone()
        if row is None:
            raise NotFoundError("learning state", memory_id)
        current = LearningState.from_record(row)
        if not changes:
            return current
        updated = replace(current, **changes, updated_at=utcnow_iso())
        record = updated.to_record()
        update_columns = tuple(c for c in LEARNING_STATE_COLUMNS if c != "memory_id")
        conn.execute(
            f"UPDATE learning_states SET {_update_assignment(update_columns)} WHERE memory_id = ?",
            tuple(record[column] for column in update_columns) + (memory_id,),
        )
        return updated

    @staticmethod
    def _get_session(conn: sqlite3.Connection, session_id: str) -> LearningSession | None:
        row = conn.execute("SELECT * FROM learning_sessions WHERE id = ?", (session_id,)).fetchone()
        return LearningSession.from_record(row) if row is not None else None

    def _insert_session(self, conn: sqlite3.Connection, session: LearningSession) -> None:
        if not isinstance(session, LearningSession):
            raise ValidationError(f"expected a LearningSession instance, got {type(session).__name__}")
        record = session.to_record()
        try:
            conn.execute(
                f"INSERT INTO learning_sessions ({_column_list(LEARNING_SESSION_COLUMNS)}) "
                f"VALUES ({_placeholders(LEARNING_SESSION_COLUMNS)})",
                tuple(record[column] for column in LEARNING_SESSION_COLUMNS),
            )
        except sqlite3.IntegrityError as exc:
            raise _session_integrity_error(exc, session) from exc

    def _update_session(
        self, conn: sqlite3.Connection, session_id: str, changes: dict[str, Any]
    ) -> LearningSession:
        row = conn.execute("SELECT * FROM learning_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise NotFoundError("learning session", session_id)
        current = LearningSession.from_record(row)
        if not changes:
            return current
        updated = replace(current, **changes, updated_at=utcnow_iso())
        record = updated.to_record()
        update_columns = tuple(c for c in LEARNING_SESSION_COLUMNS if c != "id")
        try:
            conn.execute(
                f"UPDATE learning_sessions SET {_update_assignment(update_columns)} WHERE id = ?",
                tuple(record[column] for column in update_columns) + (session_id,),
            )
        except sqlite3.IntegrityError as exc:
            raise _session_integrity_error(exc, updated) from exc
        return updated

    def _close_session(
        self, conn: sqlite3.Connection, session_id: str, target: SessionStatus
    ) -> LearningSession:
        row = conn.execute("SELECT * FROM learning_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise NotFoundError("learning session", session_id)
        current = LearningSession.from_record(row)
        if str(current.status) != str(SessionStatus.ACTIVE):
            raise ConflictError(
                f"learning session {session_id!r} is already {str(current.status)!r}; "
                f"only an active session can be closed"
            )
        now = utcnow_iso()
        updated = replace(current, status=coerce_enum(target, SessionStatus, "status"), ended_at=now, updated_at=now)
        record = updated.to_record()
        update_columns = tuple(c for c in LEARNING_SESSION_COLUMNS if c != "id")
        conn.execute(
            f"UPDATE learning_sessions SET {_update_assignment(update_columns)} WHERE id = ?",
            tuple(record[column] for column in update_columns) + (session_id,),
        )
        return updated

    @staticmethod
    def _get_active_session_for_source(
        conn: sqlite3.Connection, source_id: str
    ) -> LearningSession | None:
        row = conn.execute(
            "SELECT * FROM learning_sessions WHERE source_id = ? AND status = ? LIMIT 1",
            (source_id, str(SessionStatus.ACTIVE)),
        ).fetchone()
        return LearningSession.from_record(row) if row is not None else None


class LearningUnitOfWork:
    """Several learning writes sharing ONE transaction.

    Obtained from :meth:`LearningRepository.transaction`.  It never opens its own
    connection, so "advance the session + update the state" (Phase 3) commits or
    rolls back together.
    """

    def __init__(self, connection: sqlite3.Connection, repository: LearningRepository) -> None:
        self._conn = connection
        self._repository = repository

    def get_state(self, memory_id: str) -> LearningState | None:
        return self._repository._get_state(self._conn, memory_id)

    def create_state(self, state: LearningState) -> LearningState:
        self._repository._insert_state(self._conn, state)
        return state

    def upsert_state(self, state: LearningState) -> LearningState:
        self._repository._upsert_state(self._conn, state)
        return state

    def update_state(self, memory_id: str, /, **changes: Any) -> LearningState:
        unknown = set(changes) - LEARNING_STATE_UPDATE_FIELDS
        if unknown:
            raise ValidationError(
                f"unsupported LearningState field(s) for update: {sorted(unknown)}; "
                f"allowed: {sorted(LEARNING_STATE_UPDATE_FIELDS)}",
                problems=tuple((name, "not an updatable LearningState field") for name in sorted(unknown)),
            )
        return self._repository._update_state(self._conn, memory_id, changes)

    def get_session(self, session_id: str) -> LearningSession | None:
        return self._repository._get_session(self._conn, session_id)

    def create_session(self, session: LearningSession) -> LearningSession:
        self._repository._insert_session(self._conn, session)
        return session

    def update_session(self, session_id: str, /, **changes: Any) -> LearningSession:
        unknown = set(changes) - LEARNING_SESSION_UPDATE_FIELDS
        if unknown:
            raise ValidationError(
                f"unsupported LearningSession field(s) for update: {sorted(unknown)}; "
                f"allowed: {sorted(LEARNING_SESSION_UPDATE_FIELDS)}",
                problems=tuple((name, "not an updatable LearningSession field") for name in sorted(unknown)),
            )
        return self._repository._update_session(self._conn, session_id, changes)

    def get_active_session_for_source(self, source_id: str) -> LearningSession | None:
        return self._repository._get_active_session_for_source(self._conn, source_id)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _check_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValidationError(f"limit must be an integer, got {limit!r}", field="limit")
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValidationError(f"limit must be within [1, {MAX_LIST_LIMIT}], got {limit!r}", field="limit")
    return limit


def _state_integrity_error(
    exc: sqlite3.IntegrityError, conn: sqlite3.Connection, memory_id: str
) -> MemorySystemError:
    message = str(exc)
    if "UNIQUE constraint failed" in message and "learning_states.memory_id" in message:
        return ConflictError(
            f"a learning state for memory {memory_id!r} already exists: {message}"
        )
    return _translate_integrity_error(exc, entity="learning state")


def _session_integrity_error(
    exc: sqlite3.IntegrityError, session: LearningSession
) -> MemorySystemError:
    message = str(exc)
    if "UNIQUE constraint failed" in message and "learning_sessions.source_id" in message:
        return ConflictError(
            f"source {session.source_id!r} already has an active learning session; "
            "finish or abandon it before starting another one"
        )
    if "UNIQUE constraint failed" in message and "learning_sessions.id" in message:
        return ConflictError(f"a learning session with this id already exists: {message}")
    return _translate_integrity_error(exc, entity="learning session")
