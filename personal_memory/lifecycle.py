"""Phase 4: Memory lifecycle -- legal status transitions and safe deletion.

The lifecycle is deliberately tiny.  Three statuses already existed in Phase 1
(``active`` / ``pending`` / ``archived``); Phase 4 only decides **which moves
between them are legal** and refuses everything else::

    pending  -> active      activate_memory()  the fact is now trusted
    pending  -> archived    archive_memory()
    active   -> archived    archive_memory()   no longer recalled by default
    archived -> active      restore_memory()   recalled again

The table itself is defined once in :mod:`personal_memory.models`
(``ALLOWED_TRANSITIONS``) and is enforced both here and in
:meth:`MemoryRepository.update_memory`, so no caller can bypass it.

Refused on purpose:

* anything -> ``pending``.  ``pending`` is entered by Memory Formation's
  conservative quality policy (a suspected conflict / an unverified inference),
  never by a lifecycle call: a trusted ``active`` Memory is not silently
  downgraded, and ``archived -> pending`` is meaningless in this model.
* ``deleted`` is **not** a status.  Deleting is an operation
  (:meth:`MemoryLifecycle.delete_memory`) that removes the Memory row and its
  ``memory_sources`` links (ON DELETE CASCADE) and leaves every Source intact.

Guarantees
----------
* No SQL here: all writes go through Phase 1's
  :class:`~personal_memory.store.MemoryRepository`, so Phase 1 validation and
  the typed error hierarchy still apply.
* A status change is a single ``UPDATE``, so the Phase 3 search-index triggers
  keep the index consistent by themselves -- this module never re-implements or
  rebuilds index synchronisation.
* ``activate_memory``/``archive_memory``/``restore_memory`` on a Memory that is
  already in the target status are **no-ops** (no write, ``updated_at``
  untouched) and report ``changed=False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .errors import IllegalTransitionError
from .models import (
    ALLOWED_TRANSITIONS,
    Memory,
    MemoryStatus,
    allowed_transitions,
    can_transition,
    coerce_enum,
)
from .store import MemoryRepository

__all__ = [
    "ACTIVE",
    "PENDING",
    "ARCHIVED",
    "LIFECYCLE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "allowed_transitions",
    "can_transition",
    "TransitionReport",
    "DeleteReport",
    "MemoryLifecycle",
]

ACTIVE = str(MemoryStatus.ACTIVE)
PENDING = str(MemoryStatus.PENDING)
ARCHIVED = str(MemoryStatus.ARCHIVED)

#: Every status the lifecycle knows about (``pending`` is reachable only from Formation).
LIFECYCLE_STATUSES: tuple[str, ...] = (ACTIVE, PENDING, ARCHIVED)

#: The transition table itself is defined exactly once, in
#: :mod:`personal_memory.models` (``ALLOWED_TRANSITIONS``), and re-exported here for
#: the Phase 4 public API.  The repository reads the same object, so the lifecycle
#: service and the data-access layer can never disagree about what is legal.
def _as_status(value: Any) -> str:
    return str(coerce_enum(value, MemoryStatus, "status"))


@dataclass(frozen=True)
class TransitionReport:
    """What one lifecycle transition did (or deliberately did not do)."""

    memory_id: str
    from_status: str
    to_status: str
    changed: bool
    reason: str
    memory: Memory

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "changed": self.changed,
            "reason": self.reason,
            "memory": self.memory.as_dict(),
        }


@dataclass(frozen=True)
class DeleteReport:
    """Evidence for ``delete_memory``: the Memory and its links go, Sources stay."""

    memory_id: str
    deleted: bool
    links_removed: int
    links_remaining: int
    sources_kept: tuple[str, ...]
    sources_deleted: tuple[str, ...]

    @property
    def sources_intact(self) -> bool:
        return not self.sources_deleted

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "deleted": self.deleted,
            "links_removed": self.links_removed,
            "links_remaining": self.links_remaining,
            "sources_kept": list(self.sources_kept),
            "sources_deleted": list(self.sources_deleted),
            "sources_intact": self.sources_intact,
        }


class MemoryLifecycle:
    """Legal status transitions, validated updates and safe deletion.

    Reads go through :class:`MemoryRepository`; ``NotFoundError`` is raised for
    an unknown Memory id instead of silently doing nothing.
    """

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def allowed_transitions(self, memory_id: str) -> tuple[str, ...]:
        """Statuses this specific Memory may move to."""
        return allowed_transitions(self.repository.require_memory(memory_id).status)

    def pending(self, *, limit: int = 100, offset: int = 0) -> list[Memory]:
        """The review queue: Memories waiting for a human decision."""
        return self.repository.list_memories(status=MemoryStatus.PENDING, limit=limit, offset=offset)

    def status_counts(self) -> dict[str, int]:
        """One count per lifecycle status (``active`` / ``pending`` / ``archived``)."""
        return self.repository.status_counts()

    # ------------------------------------------------------------------
    # transitions
    # ------------------------------------------------------------------
    def set_status(self, memory_id: str, status: MemoryStatus | str) -> TransitionReport:
        """Move one Memory to ``status`` if the transition table allows it.

        Raises :class:`~personal_memory.errors.IllegalTransitionError` otherwise.
        A move to the current status is a no-op.
        """
        target = _as_status(status)
        current = self.repository.require_memory(memory_id)
        current_status = str(current.status)
        if current_status == target:
            return TransitionReport(
                memory_id=memory_id,
                from_status=current_status,
                to_status=target,
                changed=False,
                reason=f"already {target}: no write, updated_at unchanged",
                memory=current,
            )
        if target not in ALLOWED_TRANSITIONS[current_status]:
            raise IllegalTransitionError(
                memory_id, current_status, target, allowed=ALLOWED_TRANSITIONS[current_status]
            )
        updated = self.repository.update_memory(memory_id, status=target)
        return TransitionReport(
            memory_id=memory_id,
            from_status=current_status,
            to_status=target,
            changed=True,
            reason=f"{current_status} -> {target}",
            memory=updated,
        )

    def activate_memory(self, memory_id: str) -> TransitionReport:
        """``pending -> active`` (also ``archived -> active``; see :meth:`restore_memory`)."""
        return self.set_status(memory_id, ACTIVE)

    def archive_memory(self, memory_id: str) -> TransitionReport:
        """``active|pending -> archived``: keep the data, stop recalling it by default."""
        return self.set_status(memory_id, ARCHIVED)

    def restore_memory(self, memory_id: str) -> TransitionReport:
        """``archived -> active``: recalled again (alias of ``set_status(ACTIVE)``)."""
        return self.set_status(memory_id, ACTIVE)

    # ------------------------------------------------------------------
    # update / delete
    # ------------------------------------------------------------------
    def update_memory(self, memory_id: str, **changes: Any) -> Memory:
        """Validated field update; a ``status`` change still obeys the transition table.

        Phase 1 validation is unchanged: an unknown field, ``importance = 2`` or a
        bad ``type`` is rejected by :meth:`MemoryRepository.update_memory` before
        SQL runs, so nothing invalid can reach SQLite.
        """
        payload = dict(changes)
        if "status" in payload:
            target = _as_status(payload["status"])
            current_status = str(self.repository.require_memory(memory_id).status)
            if current_status == target:
                payload.pop("status")  # no-op status change; keep any other field updates
            elif target not in ALLOWED_TRANSITIONS[current_status]:
                raise IllegalTransitionError(
                    memory_id, current_status, target, allowed=ALLOWED_TRANSITIONS[current_status]
                )
            else:
                payload["status"] = target
        if not payload:
            return self.repository.require_memory(memory_id)
        return self.repository.update_memory(memory_id, **payload)

    def delete_memory(self, memory_id: str) -> DeleteReport:
        """Hard-delete one Memory: row + ``memory_sources`` links go, Sources stay.

        The delete itself is a single transaction (``ON DELETE CASCADE``), so a
        Memory can never survive with dangling relation rows.  The report records
        which Source ids were verified to still exist afterwards.
        """
        self.repository.require_memory(memory_id)
        source_ids = [source.id for source in self.repository.get_sources_for_memory(memory_id)]
        links_before = self.repository.count_links_for_memory(memory_id)
        deleted = self.repository.delete_memory(memory_id)
        links_after = self.repository.count_links_for_memory(memory_id)
        kept = tuple(sid for sid in source_ids if self.repository.get_source(sid) is not None)
        lost = tuple(sid for sid in source_ids if self.repository.get_source(sid) is None)
        return DeleteReport(
            memory_id=memory_id,
            deleted=deleted,
            links_removed=links_before,
            links_remaining=links_after,
            sources_kept=kept,
            sources_deleted=lost,
        )

    # ------------------------------------------------------------------
    # convenience
    # ------------------------------------------------------------------
    def describe(self, memory_id: str) -> dict[str, Any]:
        """Current status plus the legal next moves (used by the CLI and tests)."""
        memory = self.repository.require_memory(memory_id)
        status = str(memory.status)
        return {
            "memory_id": memory.id,
            "title": memory.title,
            "status": status,
            "updated_at": memory.updated_at,
            "allowed_transitions": list(ALLOWED_TRANSITIONS.get(status, ())),
            "source_ids": [source.id for source in self.repository.get_sources_for_memory(memory.id)],
        }


def lifecycle_statuses() -> Sequence[str]:
    """All lifecycle statuses (kept as a function so ``__all__`` stays a data list)."""
    return LIFECYCLE_STATUSES
