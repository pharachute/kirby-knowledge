"""Learning business layer (KB 1.1 Phase 2B-1) -- orchestration only, no LLM.

Position in the architecture
----------------------------
::

    caller (CLI / future web route / future LLM pipeline)
        ↓
    LearningService          ← this module: business rules, transactions, errors
        ↓
    LearningRepository       ← learning tables (Phase 2A)
    MemoryRepository         ← read-only use of the frozen 1.0 store
        ↓
    Database (SQLite)

What this layer does
--------------------
* ``start_session``: validate the request, open **one** transaction, create the
  ``LearningSession`` and make sure every involved Memory has a
  ``LearningState`` row (creating the default one when it is missing, reusing it
  otherwise).
* ``record_learning``: book ONE explicit learning event for the session's current
  Memory -- ``learn_count += 1`` and ``last_learned_at = now`` for that Memory,
  then advance ``plan_cursor`` / ``current_memory_id``.  Both writes happen in one
  transaction.  "Starting to learn" and "having learned" stay two different
  things: only this call counts an event.
* ``record_assessment``: persist an explicit judgement about the user's
  understanding of the current Memory (``understanding_level`` and the three
  aspect lists).  It is neither a learning event nor progress: ``learn_count`` /
  ``last_learned_at`` and the whole session stay untouched.  Callers (a human
  review or, later, a teacher agent) submit the judgement; this layer only
  validates and stores it -- it never infers one.
* ``get_context``: assemble the session + its Source + the involved Memories +
  their learning states (session scoped).
* ``get_learning_overview``: a **read-only, Source scoped** projection -- every
  Memory of one Source, its optional ``LearningState``, the running session (if
  any) and five counters.  It never creates a state, never starts a session and
  never writes anything; see :class:`LearningOverview`.
* ``get_active_session`` / ``finish_session`` / ``abandon_session``: the session
  lifecycle a caller needs so that a Source can be studied more than once.

What this layer deliberately does **not** do (Phase 2B-1 scope)
--------------------------------------------------------------
* no LLM, no prompt, no HTTP client, no embedding/RAG
* no judgement of the user's understanding, no level updates, no automatic
  ``learn_count`` bump (``record_learning`` counts an event only because the
  caller explicitly reports it), no automatic ``last_learned_at`` write outside
  that call, no cursor advance outside that call
* no teaching plan generation: ``plan`` is only the candidate order the caller
  asked for (or, when the caller passes nothing, the Source's own Memory order)
* no writes to the frozen 1.0 tables: Memories and Sources are only **read**

Consequences of that split, stated honestly:

* Starting a session is *not* a learning event.  Only a later, analysed exchange
  (Phase 3) may raise ``learn_count`` / ``last_learned_at`` / the level.
* A Memory whose ``LearningState`` already exists keeps every value it had; the
  service never rewrites an existing state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .errors import ConflictError, NotFoundError, ValidationError
from .learning_models import (
    LearningSession,
    LearningState,
    SessionStatus,
    TeachingStage,
    UnderstandingLevel,
)
from .learning_store import LearningRepository, LearningUnitOfWork
from .models import Memory, Source, coerce_enum, utcnow_iso
from .store import MemoryRepository

__all__ = ["LearningContext", "LearningOverview", "LearningService"]

#: Public methods of :class:`LearningService` (used by its contract test).
PUBLIC_API: tuple[str, ...] = (
    "start_session",
    "record_learning",
    "record_assessment",
    "get_context",
    "get_learning_overview",
    "get_active_session",
    "finish_session",
    "abandon_session",
)


@dataclass(frozen=True)
class LearningContext:
    """Everything an upper layer needs to render/continue one learning run.

    ``memories`` and ``states`` follow the session's ``plan`` order, so the
    caller never has to re-sort anything.  ``created_state_ids`` tells the caller
    which ``LearningState`` rows this very call initialised (purely informational
    -- it carries no teaching meaning).
    """

    session: LearningSession
    source: Source
    memories: tuple[Memory, ...]
    states: tuple[LearningState, ...]
    created_state_ids: tuple[str, ...] = ()

    @property
    def memory_ids(self) -> tuple[str, ...]:
        return tuple(memory.id for memory in self.memories)

    def memory_for(self, memory_id: str) -> Memory | None:
        for memory in self.memories:
            if memory.id == memory_id:
                return memory
        return None

    def state_for(self, memory_id: str) -> LearningState | None:
        for state in self.states:
            if state.memory_id == memory_id:
                return state
        return None

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view (no ORM objects, only plain mappings)."""
        return {
            "session": self.session.as_dict(),
            "source": self.source.as_dict(),
            "memories": [memory.as_dict() for memory in self.memories],
            "states": [state.as_dict() for state in self.states],
            "created_state_ids": list(self.created_state_ids),
        }


@dataclass(frozen=True)
class LearningOverview:
    """Read-only overview of everything one Source currently knows (Phase 2B-4).

    Unlike :class:`LearningContext` (one learning run), this is **Source scoped**:
    the whole material at a glance.  It is a projection assembled from
    ``Source`` + ``Memory`` + ``LearningState`` + ``LearningSession`` -- no new
    table, no stored summary, no recommendation.

    ``memories`` and ``states`` stay **aligned position by position**: a Memory
    that has no ``LearningState`` keeps its slot with ``None`` (the service never
    creates a state just to fill the gap).

    The five counters are derived from ``states`` in :meth:`__post_init__`, so they
    can never disagree with the data:

    ``total_memories``
        how many Memories the Source currently has (== ``len(memories)``).
    ``learned_memories``
        Memories with at least one recorded learning event (``learn_count > 0``).
    ``assessed_memories``
        Memories whose level is not :data:`~personal_memory.learning_models.UnderstandingLevel.UNKNOWN`.
    ``solid_memories``
        Memories whose level is :data:`~personal_memory.learning_models.UnderstandingLevel.SOLID`.
    ``total_learning_count``
        ``sum(learn_count)`` over the Source's states -- a count of learning
        events, **not** a mastery score.
    """

    source: Source
    memories: tuple[Memory, ...]
    states: tuple[LearningState | None, ...]
    active_session: LearningSession | None = None

    total_memories: int = field(init=False)
    learned_memories: int = field(init=False)
    assessed_memories: int = field(init=False)
    solid_memories: int = field(init=False)
    total_learning_count: int = field(init=False)

    def __post_init__(self) -> None:
        if len(self.memories) != len(self.states):
            raise ValidationError(
                f"memories ({len(self.memories)}) and states ({len(self.states)}) must stay "
                "aligned position by position",
                field="states",
            )
        present = [state for state in self.states if state is not None]
        unknown = str(UnderstandingLevel.UNKNOWN)
        solid = str(UnderstandingLevel.SOLID)
        object.__setattr__(self, "total_memories", len(self.memories))
        object.__setattr__(
            self, "learned_memories", sum(1 for state in present if state.learn_count > 0)
        )
        object.__setattr__(
            self,
            "assessed_memories",
            sum(1 for state in present if str(state.understanding_level) != unknown),
        )
        object.__setattr__(
            self,
            "solid_memories",
            sum(1 for state in present if str(state.understanding_level) == solid),
        )
        object.__setattr__(
            self, "total_learning_count", sum(state.learn_count for state in present)
        )

    # -- lookups -----------------------------------------------------------
    def memory_for(self, memory_id: str) -> Memory | None:
        for memory in self.memories:
            if memory.id == memory_id:
                return memory
        return None

    def state_for(self, memory_id: str) -> LearningState | None:
        """The state of one Memory of this Source (``None`` = never touched)."""
        for memory, state in zip(self.memories, self.states):
            if memory.id == memory_id:
                return state
        return None

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view: plain data only, same shape as ``LearningContext``."""
        return {
            "source": self.source.as_dict(),
            "memories": [memory.as_dict() for memory in self.memories],
            "states": [state.as_dict() if state is not None else None for state in self.states],
            "active_session": (
                self.active_session.as_dict() if self.active_session is not None else None
            ),
            "stats": {
                "total_memories": self.total_memories,
                "learned_memories": self.learned_memories,
                "assessed_memories": self.assessed_memories,
                "solid_memories": self.solid_memories,
                "total_learning_count": self.total_learning_count,
            },
        }


class LearningService:
    """Business orchestration for learning sessions (no LLM in this phase).

    Both repositories share the same :class:`~personal_memory.db.Database`, so the
    learning writes land in one transaction while the Memory/Source reads use the
    1.0 access paths unchanged.
    """

    def __init__(self, learning: LearningRepository, memory: MemoryRepository) -> None:
        self.learning = learning
        self.memory = memory

    # ==================================================================
    # reading
    # ==================================================================
    def get_active_session(self, source_id: str) -> LearningSession | None:
        """The running session of a Source, if any (a Source has at most one)."""
        return self.learning.get_active_session_for_source(source_id)

    def get_context(self, session_id: str) -> LearningContext:
        """Session + Source + involved Memories + their LearningStates.

        A Memory that disappeared after the session was created (deleted, hence
        its ``current_memory_id`` was set to NULL and its state cascaded) is
        skipped instead of failing the whole lookup: the session stays usable.
        """
        session = self.learning.require_session(session_id)
        return self._build_context(session)

    def get_learning_overview(self, source_id: str) -> LearningOverview:
        """Everything one Source currently knows, read-only (Phase 2B-4).

        * Source scoped: every Memory linked to the Source, in the Source's own
          stable order (``created_at, id``) -- never re-sorted by progress
        * each Memory keeps its position with its ``LearningState`` or ``None``;
          **no state is created** to fill a gap
        * ``active_session`` comes from
          :meth:`LearningRepository.get_active_session_for_source` (no second
          definition of "active" here)
        * nothing is written: counts and ``updated_at`` values are untouched

        Raises :class:`NotFoundError` when the Source does not exist (an existing
        but empty Source is a valid, all-zero overview -- unlike
        :meth:`start_session`, which refuses an empty Source).
        """
        source = self.memory.require_source(source_id)
        memories = tuple(self.memory.get_memories_for_source(source_id))
        states = tuple(self.learning.get_state(memory.id) for memory in memories)
        return LearningOverview(
            source=source,
            memories=memories,
            states=states,
            active_session=self.learning.get_active_session_for_source(source_id),
        )

    # ==================================================================
    # writing
    # ==================================================================
    def start_session(
        self,
        *,
        source_id: str,
        memory_ids: Sequence[str] | None = None,
    ) -> LearningContext:
        """Start (or refuse to duplicate) a learning session over one Source.

        * ``memory_ids`` omitted -> every Memory linked to the Source, in the
          Source's own order; explicitly passed -> that order is preserved.
        * every involved Memory must exist **and** be linked to the Source.
        * one transaction: the session and the initial states commit together, so
          a failure leaves no half-written learning data behind.

        Raises :class:`NotFoundError` (missing Source/Memory),
        :class:`ValidationError` (empty list, Source without Memories, Memory not
        linked to the Source) or :class:`ConflictError` (the Source already has a
        running session -- finish/abandon it, or resume it).
        """
        source = self.memory.require_source(source_id)

        running = self.learning.get_active_session_for_source(source_id)
        if running is not None:
            raise ConflictError(
                f"source {source_id!r} already has an active learning session "
                f"({running.id!r}); finish, abandon or resume it before starting another one"
            )

        requested = self._resolve_memory_ids(source_id, memory_ids)
        memories = self._load_linked_memories(source_id, requested)

        created_state_ids: list[str] = []
        with self.learning.transaction() as tx:
            session = tx.create_session(
                LearningSession.create(
                    source_id=source.id,
                    plan=requested,
                    current_memory_id=requested[0],
                    current_stage=TeachingStage.EXPLAIN,
                )
            )
            states: list[LearningState] = []
            for memory_id in requested:
                state = tx.get_state(memory_id)
                if state is None:
                    # default state: unknown / 0 / NULL -- starting is not learning
                    state = tx.create_state(LearningState.create(memory_id=memory_id))
                    created_state_ids.append(memory_id)
                states.append(state)

        return LearningContext(
            session=session,
            source=source,
            memories=memories,
            states=tuple(states),
            created_state_ids=tuple(created_state_ids),
        )

    def record_learning(self, session_id: str, *, memory_id: str | None = None) -> LearningContext:
        """Book ONE explicit learning event for the session's current Memory.

        Semantics (Phase 2B-2)
        ---------------------
        * the caller reports "this Memory was learned once"; nothing is inferred
          about *how well* (``understanding_level`` and the aspect lists are never
          touched here -- that belongs to the answering/analysis layer)
        * ``learn_count += 1`` and ``last_learned_at = now`` for the Memory the
          session is currently on; a missing ``LearningState`` is created first
          (so a freshly created one ends up with ``learn_count = 1``)
        * then the session advances: ``plan_cursor += 1``; while another Memory is
          left, ``current_memory_id`` becomes that Memory; when the plan is
          exhausted the session is marked with the **existing** representation of
          "plan finished but not closed" -- ``plan_cursor == len(plan)``,
          ``current_memory_id = NULL``, ``current_stage = 'done'`` -- and the
          session stays ``active`` (closing it is an explicit ``finish_session``)
        * both writes are one transaction: a failure leaves neither the state nor
          the session changed

        ``memory_id`` is optional and may only name the Memory the session is
        currently on; passing anything else is a conflict, not a re-targeting.

        Raises :class:`NotFoundError` (unknown session / unknown Memory) or
        :class:`ConflictError` (session closed, plan finished, no current Memory,
        a different Memory, or a current Memory that no longer matches the plan).
        """
        session = self.learning.require_session(session_id)
        if memory_id is not None:
            if not isinstance(memory_id, str) or not memory_id.strip():
                raise ValidationError(
                    f"memory_id must be a non-empty string, got {memory_id!r}", field="memory_id"
                )
            if self.memory.get_memory(memory_id) is None:
                raise NotFoundError("memory", memory_id)

        now = utcnow_iso()
        with self.learning.transaction() as tx:
            # re-read inside the transaction: validation and the writes see one snapshot
            current_session, current_memory_id = self._resolve_current_memory(
                tx, session_id, memory_id
            )

            state = tx.get_state(current_memory_id)
            if state is None:
                # Phase 2A/2B-1 rule: a missing state is created first, then the event counts
                state = tx.create_state(LearningState.create(memory_id=current_memory_id))
            state = tx.update_state(
                current_memory_id,
                learn_count=state.learn_count + 1,
                last_learned_at=now,
            )

            next_cursor = current_session.plan_cursor + 1
            if next_cursor < len(current_session.plan):
                updated = tx.update_session(
                    session_id,
                    plan_cursor=next_cursor,
                    current_memory_id=current_session.plan[next_cursor],
                )
            else:
                # the plan is exhausted: reuse the existing "done" stage, stay active
                updated = tx.update_session(
                    session_id,
                    plan_cursor=next_cursor,
                    current_memory_id=None,
                    current_stage=TeachingStage.DONE,
                )

        return self._build_context(updated)

    def record_assessment(
        self,
        session_id: str,
        *,
        understanding_level: UnderstandingLevel | str,
        known_aspects: Sequence[str] | None = None,
        weak_aspects: Sequence[str] | None = None,
        misconceptions: Sequence[str] | None = None,
        memory_id: str | None = None,
    ) -> LearningContext:
        """Persist an explicit judgement about the user's understanding (Phase 2B-3).

        What it changes
        ---------------
        * ``understanding_level`` (required, validated against the existing
          :class:`~personal_memory.learning_models.UnderstandingLevel` enum)
        * any of ``known_aspects`` / ``weak_aspects`` / ``misconceptions`` that the
          caller actually passed; an explicit list (including ``[]``) replaces the
          stored one, ``None``/omitted leaves it untouched -- exactly the Phase 2A
          model rules, no new constraint is invented
        * a missing ``LearningState`` is created first, so the stored row ends up as
          ``learn_count = 0`` / ``last_learned_at = NULL`` plus the assessment

        What it never changes
        ---------------------
        * ``learn_count`` / ``last_learned_at`` (those belong to
          :meth:`record_learning`; an assessment is not a learning event)
        * the session in any way -- ``plan_cursor`` / ``current_memory_id`` /
          ``current_stage`` / ``status`` / ``updated_at`` stay as they were
        * the aspect lists the caller did not pass

        The target Memory must be the session's **current** one and the session must
        still be running: a stale or exhausted session can never be used to write an
        assessment into an unrelated Memory.  No business-rule cross-checking between
        the three lists happens here (an aspect may legitimately appear in both
        ``known_aspects`` and ``weak_aspects``) -- this layer stores the judgement, it
        does not second-guess it.

        Raises :class:`ValidationError` (illegal level/aspects) or
        :class:`NotFoundError` (unknown session / unknown Memory) or
        :class:`ConflictError` (session closed or exhausted, no current Memory, a
        different Memory, an inconsistent session).
        """
        level = coerce_enum(understanding_level, UnderstandingLevel, "understanding_level")
        changes: dict[str, Any] = {"understanding_level": level}
        for field_name, value in (
            ("known_aspects", known_aspects),
            ("weak_aspects", weak_aspects),
            ("misconceptions", misconceptions),
        ):
            if value is not None:
                changes[field_name] = value          # [] clears the list: the model's own rule

        session = self.learning.require_session(session_id)
        if memory_id is not None:
            if not isinstance(memory_id, str) or not memory_id.strip():
                raise ValidationError(
                    f"memory_id must be a non-empty string, got {memory_id!r}", field="memory_id"
                )
            if self.memory.get_memory(memory_id) is None:
                raise NotFoundError("memory", memory_id)

        with self.learning.transaction() as tx:
            current_session, current_memory_id = self._resolve_current_memory(
                tx, session_id, memory_id
            )
            if tx.get_state(current_memory_id) is None:
                # same 2B-1/2B-2 rule: create the default row, then write the judgement
                tx.create_state(LearningState.create(memory_id=current_memory_id))
            tx.update_state(current_memory_id, **changes)

        return self._build_context(current_session)

    def finish_session(self, session_id: str) -> LearningContext:
        """Close a running session as ``completed`` (``ended_at`` is written).

        Deliberately does **not** touch any ``LearningState``: what the user
        learned is decided per answer in Phase 3, not by closing the session.
        """
        return self._build_context(self.learning.finish_session(session_id))

    def abandon_session(self, session_id: str) -> LearningContext:
        """Close a running session as ``abandoned`` (explicit user action)."""
        return self._build_context(self.learning.abandon_session(session_id))

    # ==================================================================
    # internals
    # ==================================================================
    def _resolve_current_memory(
        self,
        tx: LearningUnitOfWork,
        session_id: str,
        memory_id: str | None,
    ) -> tuple[LearningSession, str]:
        """Shared guard for "act on the session's current Memory" (2B-2 / 2B-3).

        Both :meth:`record_learning` and :meth:`record_assessment` must obey the same
        rules, so the checks live here once instead of drifting apart:

        * the session must exist and still be ``active``
        * its plan must not be exhausted (that needs an explicit finish/abandon)
        * it must have a current Memory, which must be ``plan[plan_cursor]``
        * an explicitly passed ``memory_id`` may only name that current Memory
        """
        current_session = tx.get_session(session_id)
        if current_session is None:
            raise NotFoundError("learning session", session_id)
        if str(current_session.status) != str(SessionStatus.ACTIVE):
            raise ConflictError(
                f"learning session {session_id!r} is {str(current_session.status)!r}; "
                "only an active session can be used"
            )
        cursor = current_session.plan_cursor
        if cursor >= len(current_session.plan):
            raise ConflictError(
                f"learning session {session_id!r} has finished its plan "
                f"({cursor}/{len(current_session.plan)}); finish or abandon the session "
                "before doing anything else in it"
            )
        current_memory_id = current_session.current_memory_id
        if current_memory_id is None:
            raise ConflictError(f"learning session {session_id!r} has no current Memory")
        if memory_id is not None and memory_id != current_memory_id:
            raise ConflictError(
                f"memory {memory_id!r} is not the Memory this session is on "
                f"({current_memory_id!r}); only the current Memory can be acted on"
            )
        expected = current_session.plan[cursor]
        if current_memory_id != expected:
            raise ConflictError(
                f"learning session {session_id!r} is on memory {current_memory_id!r} but its "
                f"plan points at {expected!r} (cursor {cursor}); the session is inconsistent"
            )
        return current_session, current_memory_id

    def _build_context(self, session: LearningSession) -> LearningContext:
        source = self.memory.require_source(session.source_id)
        stored = self.memory.get_memories_by_ids(list(session.plan))
        available = [memory_id for memory_id in session.plan if memory_id in stored]
        states = tuple(
            state for state in (self.learning.get_state(memory_id) for memory_id in available)
            if state is not None
        )
        return LearningContext(
            session=session,
            source=source,
            memories=tuple(stored[memory_id] for memory_id in available),
            states=states,
        )

    def _resolve_memory_ids(
        self, source_id: str, memory_ids: Sequence[str] | None
    ) -> list[str]:
        """Normalise the requested Memory ids (order kept, duplicates collapsed)."""
        if memory_ids is None:
            candidates = [memory.id for memory in self.memory.get_memories_for_source(source_id)]
            if not candidates:
                raise ValidationError(
                    f"source {source_id!r} has no Memory to learn",
                    field="source_id",
                )
        else:
            if isinstance(memory_ids, (str, bytes)) or not isinstance(memory_ids, Sequence):
                raise ValidationError(
                    f"memory_ids must be a sequence of memory ids, got {type(memory_ids).__name__}",
                    field="memory_ids",
                )
            candidates = list(memory_ids)
            if not candidates:
                raise ValidationError(
                    "memory_ids must not be empty; pass at least one Memory of this Source",
                    field="memory_ids",
                )
        resolved: list[str] = []
        for memory_id in candidates:
            if not isinstance(memory_id, str) or not memory_id.strip():
                raise ValidationError(
                    f"every memory id must be a non-empty string, got {memory_id!r}",
                    field="memory_ids",
                )
            if memory_id not in resolved:
                resolved.append(memory_id)
        return resolved

    def _load_linked_memories(self, source_id: str, memory_ids: Sequence[str]) -> tuple[Memory, ...]:
        """Every requested Memory must exist and be linked to the Source."""
        stored: Mapping[str, Memory] = self.memory.get_memories_by_ids(list(memory_ids))
        missing = [memory_id for memory_id in memory_ids if memory_id not in stored]
        if missing:
            raise NotFoundError("memory", missing[0])
        for memory_id in memory_ids:
            if not self.memory.is_linked(memory_id, source_id):
                raise ValidationError(
                    f"memory {memory_id!r} is not linked to source {source_id!r}; "
                    "a learning session can only cover Memories of its own Source",
                    field="memory_ids",
                )
        return tuple(stored[memory_id] for memory_id in memory_ids)
