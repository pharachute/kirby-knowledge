"""Error hierarchy for the Personal Memory System (Phase 1).

The hierarchy is deliberately small and explicit so that callers can tell
*why* a write was refused:

* :class:`ValidationError`      -- the payload is not a legal Source/Memory
* :class:`NotFoundError`        -- the referenced entity does not exist
* :class:`ConflictError`        -- the write collides with existing data
* :class:`ReferentialIntegrityError` -- a foreign key could not be satisfied
* :class:`IllegalTransitionError` -- a Memory status transition is not allowed (Phase 4)

Both the application layer (dataclass validation) and the database layer
(SQLite CHECK/UNIQUE/FOREIGN KEY constraints) are mapped onto this hierarchy,
so invalid data is rejected before/while it is written.
"""

from __future__ import annotations

from typing import Iterable, Sequence

__all__ = [
    "MemorySystemError",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
    "DuplicateContentHashError",
    "ReferentialIntegrityError",
    "IllegalTransitionError",
    "SchemaError",
]


class MemorySystemError(Exception):
    """Base class for every error raised by this package."""


class ValidationError(MemorySystemError, ValueError):
    """A Source/Memory field (or several fields) failed validation.

    ``problems`` holds ``(field_name, message)`` pairs so tests and callers can
    assert on the exact field that was rejected.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        problems: Sequence[tuple[str, str]] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        if problems:
            self.problems: tuple[tuple[str, str], ...] = tuple(problems)
        elif field is not None:
            self.problems = ((field, message),)
        else:
            self.problems = ()

    @property
    def fields(self) -> tuple[str, ...]:
        """Names of the fields that failed validation."""
        return tuple(name for name, _ in self.problems)


class NotFoundError(MemorySystemError, LookupError):
    """A requested entity does not exist in the database."""

    def __init__(self, entity: str, entity_id: str) -> None:
        super().__init__(f"{entity} not found: {entity_id!r}")
        self.entity = entity
        self.entity_id = entity_id


class ConflictError(MemorySystemError):
    """The write collides with an existing row (unique constraint)."""


class DuplicateContentHashError(ConflictError):
    """A Source with the same ``content_hash`` already exists.

    Raised on purpose instead of silently inserting a second copy: the hash is
    the deduplication key of the raw layer, and the caller decides whether the
    existing Source is reused (``existing_source_id``) or the new content is
    genuinely different.
    """

    def __init__(self, content_hash: str, existing_source_id: str | None = None) -> None:
        detail = f" (existing_source_id={existing_source_id!r})" if existing_source_id else ""
        super().__init__(f"a source with content_hash {content_hash!r} already exists{detail}")
        self.content_hash = content_hash
        self.existing_source_id = existing_source_id


class ReferentialIntegrityError(MemorySystemError):
    """A foreign key reference could not be satisfied."""


class IllegalTransitionError(MemorySystemError):
    """A Memory status transition is not part of the Phase 4 lifecycle.

    Deleting a Memory is an operation, not a status, so ``deleted`` is not a
    valid target here.  ``allowed`` lists the target statuses that *would* have
    been accepted from ``from_status``.
    """

    def __init__(
        self,
        memory_id: str,
        from_status: str,
        to_status: str,
        *,
        allowed: Sequence[str] = (),
    ) -> None:
        detail = f"; allowed from {from_status}: {list(allowed)}" if allowed else ""
        super().__init__(
            f"illegal Memory status transition for {memory_id!r}: {from_status} -> {to_status}{detail}"
        )
        self.memory_id = memory_id
        self.from_status = from_status
        self.to_status = to_status
        self.allowed = tuple(allowed)


class SchemaError(MemorySystemError):
    """The SQLite file does not match the schema this code expects.

    Raised when the file claims a schema version newer than this code supports,
    or when a version recorded in ``schema_migrations`` has no matching tables
    (a truncated / hand-made / foreign file).  Failing here is deliberate: the
    alternative is a raw ``sqlite3.OperationalError: no such table`` on the
    first write, or silently writing into an unknown schema.
    """


def describe_problems(problems: Iterable[tuple[str, str]]) -> str:
    """Render ``(field, message)`` pairs as a single readable line."""
    return "; ".join(f"{field}: {message}" for field, message in problems)
