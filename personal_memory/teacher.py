"""Teacher Agent contract (KB 1.1 Phase 2C-1): structured intent, **no execution**.

Architecture
------------
::

    Teacher Agent (any model: DeepSeek / OpenAI / local / another runtime)
            ↓  produces
    TeacherTurnResponse              ← this module: strict, structured, frozen
            ↓  carries
    TeacherAction(s)                 ← record_learning / record_assessment /
                                        finish_session / abandon_session
            ↓  (a future ActionExecutor, NOT part of 2C-1)
    LearningService                  ← the only place that enforces session rules
            ↓
    LearningRepository

This module is a **pure domain contract**:

* no LLM, no prompt, no HTTP client, no sqlite, no SQL
* no import of :mod:`personal_memory.learning` at runtime (annotations only, via
  ``TYPE_CHECKING``), so the contract can never become a second learning engine
* no execution: constructing an action does not touch the database, and nothing
  here calls ``record_learning()`` / ``record_assessment()`` / ``finish_session()``
  / ``abandon_session()``

It answers exactly one question: *"what did the model say it wants to do?"* --
never *"should it happen?"* (that stays with :class:`~personal_memory.learning.LearningService`)
and never *"do it"* (that will be Phase 2C-2+).

Vocabulary reuse: ``UnderstandingLevel``, the aspect-list rules and the id rules
come from the existing learning/models layers -- the contract does not invent a
second enum and does not change the meaning of ``None`` (leave alone) or ``[]``
(clear).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Mapping, Sequence

from .errors import ValidationError
from .learning_models import UnderstandingLevel, _Problems, _normalize_aspects
from .models import _check_id, coerce_enum

if TYPE_CHECKING:  # annotation only -- no runtime dependency on the learning engine
    from .learning import LearningContext, LearningOverview, LearningSession
    from .learning_models import LearningState
    from .models import Memory, Source

__all__ = [
    "ALLOWED_ACTION_TYPES",
    "FORBIDDEN_ACTION_TYPES",
    "TeacherAction",
    "RecordLearningAction",
    "RecordAssessmentAction",
    "FinishSessionAction",
    "AbandonSessionAction",
    "TeacherContext",
    "TeacherTurnRequest",
    "TeacherTurnResponse",
]

#: The complete set of things a Teacher Agent may ask for (Phase 2C-1).
ALLOWED_ACTION_TYPES: tuple[str, ...] = (
    "record_learning",
    "record_assessment",
    "finish_session",
    "abandon_session",
)

#: Explicitly *not* part of the contract.  ``say`` is listed on purpose: what the
#: assistant tells the user is ``TeacherTurnResponse.assistant_message``, not a
#: database action.  Everything else here would let an agent touch state the
#: Learning Engine never delegated to it.
FORBIDDEN_ACTION_TYPES: tuple[str, ...] = (
    "say",
    "delete_memory",
    "edit_memory",
    "create_memory",
    "edit_source",
    "delete_source",
    "modify_learning_schema",
    "modify_session_plan",
    "modify_database",
    "execute_sql",
)


# --------------------------------------------------------------------------
# field helpers (reuse the project's problem collector)
# --------------------------------------------------------------------------

def _check_text(value: Any, field_name: str, problems: _Problems) -> None:
    if not isinstance(value, str):
        problems.add(field_name, f"must be a string, got {type(value).__name__}")
        return
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        problems.add(field_name, f"must be valid UTF-8 text (unpaired surrogate at {exc.start})")


def _check_required_id(value: Any, field_name: str, problems: _Problems) -> None:
    """Exactly one problem per bad value (no duplicate field entries)."""
    if not isinstance(value, str) or not value.strip():
        problems.add(field_name, f"must be a non-empty id string, got {value!r}")
        return
    _check_id(value.strip(), field_name, problems)


def _check_optional_id(value: Any, field_name: str, problems: _Problems) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        problems.add(field_name, f"must be a string or None, got {type(value).__name__}")
        return
    _check_id(value.strip(), field_name, problems)


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TeacherAction:
    """Base class of every structured intent a Teacher Agent may express.

    The base itself is abstract: building a bare ``TeacherAction`` (i.e. an action
    without a known kind) is refused, and :meth:`parse` refuses any ``type`` that is
    not in :data:`ALLOWED_ACTION_TYPES` -- an illegal action is rejected *before*
    anything could act on it.
    """

    kind: ClassVar[str] = ""

    def __post_init__(self) -> None:
        if type(self) is TeacherAction:
            raise ValidationError(
                "TeacherAction is abstract; use one of "
                f"{list(ALLOWED_ACTION_TYPES)} (or TeacherAction.parse)",
                field="type",
            )

    def as_dict(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- strict parsing ----------------------------------------------------
    @staticmethod
    def parse(payload: Mapping[str, Any]) -> "TeacherAction":
        """Build an action from a plain mapping (the model's structured output).

        Strict by design: an unknown ``type``, a forbidden type (``delete_memory``,
        ``execute_sql``, ``say`` …) or an unknown field is a
        :class:`~personal_memory.errors.ValidationError` -- never a silent no-op and
        never a guess.  This does not execute anything.
        """
        if not isinstance(payload, Mapping):
            raise ValidationError(
                f"an action must be a mapping, got {type(payload).__name__}", field="type"
            )
        raw_kind = payload.get("type")
        if not isinstance(raw_kind, str) or not raw_kind.strip():
            raise ValidationError("an action needs a non-empty 'type'", field="type")
        kind = raw_kind.strip()
        cls = _ACTION_REGISTRY.get(kind)
        if cls is None:
            hint = " (this action is not part of the Teacher contract)" if kind in FORBIDDEN_ACTION_TYPES else ""
            raise ValidationError(
                f"unknown teacher action {kind!r}{hint}; allowed: {list(ALLOWED_ACTION_TYPES)}",
                field="type",
            )
        allowed = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(payload) - allowed - {"type"})
        if unknown:
            raise ValidationError(
                f"unknown field(s) for action {kind!r}: {unknown}; allowed: {sorted(allowed)}",
                problems=tuple((name, "not part of this action") for name in unknown),
            )
        arguments = {key: value for key, value in payload.items() if key != "type"}
        try:
            return cls(**arguments)  # type: ignore[arg-type]
        except TypeError as exc:  # pragma: no cover - defensive (unknown keys caught above)
            raise ValidationError(f"could not build action {kind!r}: {exc}", field="type") from exc


@dataclass(frozen=True)
class RecordLearningAction(TeacherAction):
    """ "I consider that one learning event happened in this turn."

    ``memory_id`` is optional and may only *confirm* the session's current Memory;
    the actual rule (it must be the current one, the session must be running) stays
    in :meth:`LearningService.record_learning`.
    """

    kind: ClassVar[str] = "record_learning"
    memory_id: str | None = None

    def __post_init__(self) -> None:
        problems = _Problems("RecordLearningAction")
        _check_optional_id(self.memory_id, "memory_id", problems)
        problems.raise_if_any()

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.kind, "memory_id": self.memory_id}


@dataclass(frozen=True)
class RecordAssessmentAction(TeacherAction):
    """ "I have made an explicit judgement about the current Memory's understanding."

    ``understanding_level`` is required and must be an existing
    :class:`~personal_memory.learning_models.UnderstandingLevel` (no new levels).
    The three lists keep the data model's meaning: ``None`` leaves the stored list
    untouched, a list (including ``[]``) replaces it.
    """

    kind: ClassVar[str] = "record_assessment"
    understanding_level: UnderstandingLevel | str
    known_aspects: Sequence[str] | None = None
    weak_aspects: Sequence[str] | None = None
    misconceptions: Sequence[str] | None = None
    memory_id: str | None = None

    def __post_init__(self) -> None:
        problems = _Problems("RecordAssessmentAction")
        level = _coerce_level(self.understanding_level, problems)
        known = _normalize_aspects(self.known_aspects, "known_aspects", problems)
        weak = _normalize_aspects(self.weak_aspects, "weak_aspects", problems)
        misconceptions = _normalize_aspects(self.misconceptions, "misconceptions", problems)
        _check_optional_id(self.memory_id, "memory_id", problems)
        problems.raise_if_any()
        object.__setattr__(self, "understanding_level", level)
        object.__setattr__(self, "known_aspects", None if self.known_aspects is None else known)
        object.__setattr__(self, "weak_aspects", None if self.weak_aspects is None else weak)
        object.__setattr__(
            self, "misconceptions", None if self.misconceptions is None else misconceptions
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.kind,
            "memory_id": self.memory_id,
            "understanding_level": str(self.understanding_level),
            "known_aspects": None if self.known_aspects is None else list(self.known_aspects),
            "weak_aspects": None if self.weak_aspects is None else list(self.weak_aspects),
            "misconceptions": None if self.misconceptions is None else list(self.misconceptions),
        }


@dataclass(frozen=True)
class FinishSessionAction(TeacherAction):
    """ "This learning run is over."  Legality is decided by the LearningService."""

    kind: ClassVar[str] = "finish_session"
    session_id: str

    def __post_init__(self) -> None:
        problems = _Problems("FinishSessionAction")
        _check_required_id(self.session_id, "session_id", problems)
        problems.raise_if_any()
        object.__setattr__(self, "session_id", self.session_id.strip())

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.kind, "session_id": self.session_id}


@dataclass(frozen=True)
class AbandonSessionAction(TeacherAction):
    """ "This learning run is being given up (explicit user action)." """

    kind: ClassVar[str] = "abandon_session"
    session_id: str

    def __post_init__(self) -> None:
        problems = _Problems("AbandonSessionAction")
        _check_required_id(self.session_id, "session_id", problems)
        problems.raise_if_any()
        object.__setattr__(self, "session_id", self.session_id.strip())

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.kind, "session_id": self.session_id}


def _coerce_level(value: Any, problems: _Problems) -> Any:
    """Reuse the project's enum coercion, but collect the problem instead of raising."""
    try:
        return coerce_enum(value, UnderstandingLevel, "understanding_level")
    except ValidationError as exc:
        problems.add("understanding_level", exc.problems[0][1] if exc.problems else str(exc))
        return None


_ACTION_REGISTRY: dict[str, type[TeacherAction]] = {
    cls.kind: cls
    for cls in (
        RecordLearningAction,
        RecordAssessmentAction,
        FinishSessionAction,
        AbandonSessionAction,
    )
}


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TeacherContext:
    """What a model is allowed to *see* in one Teacher turn (read-only projection).

    Built from the learning engine, never by the model itself: see
    :meth:`from_learning`, which only reads the objects
    :meth:`LearningService.get_context` and :meth:`LearningService.get_learning_overview`
    already produced (no second query layer, no ``transaction``, no writes).

    When the session has no current Memory (its plan is exhausted, or the Memory was
    deleted), ``current_memory`` and ``current_state`` are ``None`` -- the projection
    does not invent a business state.
    """

    session: "LearningSession"
    source: "Source"
    overview: "LearningOverview"
    current_memory: "Memory | None" = None
    current_state: "LearningState | None" = None

    def __post_init__(self) -> None:
        problems = _Problems("TeacherContext")
        requirements = (
            ("session", self.session, ("id", "source_id", "current_memory_id")),
            ("source", self.source, ("id", "title")),
            ("overview", self.overview, ("source", "memories", "states", "total_memories")),
        )
        for name, value, attributes in requirements:
            if value is None:
                problems.add(name, "must not be None")
                continue
            missing = [attribute for attribute in attributes if not hasattr(value, attribute)]
            if missing:
                problems.add(name, f"is not a learning object (missing {missing})")

        if not problems.items:
            if str(self.session.source_id) != str(self.source.id):
                problems.add("source", "must be the Source the session belongs to")
            overview_source = getattr(self.overview, "source", None)
            if getattr(overview_source, "id", None) != self.source.id:
                problems.add("overview", "must describe the same Source as the session")
            if self.current_state is not None:
                if self.current_memory is None:
                    problems.add("current_state", "requires a current_memory")
                elif str(self.current_state.memory_id) != str(self.current_memory.id):
                    problems.add("current_state", "must belong to current_memory")
            session_current = self.session.current_memory_id
            if session_current is None:
                if self.current_memory is not None:
                    problems.add(
                        "current_memory",
                        "must be None when the session has no current Memory",
                    )
            elif self.current_memory is None or str(self.current_memory.id) != str(session_current):
                problems.add("current_memory", "must be the session's current Memory")
        problems.raise_if_any()

    # -- projection --------------------------------------------------------
    @classmethod
    def from_learning(
        cls,
        learning_context: "LearningContext",
        learning_overview: "LearningOverview",
    ) -> "TeacherContext":
        """Pure projection of an existing ``LearningContext`` + ``LearningOverview``.

        Uses the context's own lookups (``memory_for`` / ``state_for``); it performs
        no query of its own and writes nothing.
        """
        if learning_context is None or learning_overview is None:
            raise ValidationError(
                "TeacherContext.from_learning needs a LearningContext and a LearningOverview",
                field="context",
            )
        session = getattr(learning_context, "session", None)
        source = getattr(learning_context, "source", None)
        current_memory_id = getattr(session, "current_memory_id", None)
        current_memory = None
        current_state = None
        if current_memory_id is not None:
            current_memory = learning_context.memory_for(current_memory_id)
            current_state = learning_context.state_for(current_memory_id)
        return cls(
            session=session,
            source=source,
            overview=learning_overview,
            current_memory=current_memory,
            current_state=current_state,
        )

    # -- read-only accessors ----------------------------------------------
    @property
    def session_id(self) -> str:
        return str(self.session.id)

    @property
    def current_memory_id(self) -> str | None:
        return None if self.current_memory is None else str(self.current_memory.id)

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view (plain data only, engine objects flattened)."""
        return {
            "session": self.session.as_dict(),
            "source": self.source.as_dict(),
            "current_memory": self.current_memory.as_dict() if self.current_memory else None,
            "current_state": self.current_state.as_dict() if self.current_state else None,
            "overview": self.overview.as_dict(),
        }


# --------------------------------------------------------------------------
# one turn
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TeacherTurnRequest:
    """One model turn as the system hands it over (context built by the system).

    The context is *not* trusted from the model: :meth:`__post_init__` requires
    ``context.session_id == session_id``, so a model (or a buggy caller) cannot make
    the contract act on one session while claiming another.
    """

    session_id: str
    user_message: str
    context: "TeacherContext"

    def __post_init__(self) -> None:
        problems = _Problems("TeacherTurnRequest")
        _check_id(self.session_id.strip() if isinstance(self.session_id, str) else self.session_id,
                  "session_id", problems)
        _check_text(self.user_message, "user_message", problems)
        if self.context is None:
            problems.add("context", "must not be None")
        elif not hasattr(self.context, "session_id"):
            problems.add("context", "must be a TeacherContext")
        else:
            session_id = self.session_id.strip() if isinstance(self.session_id, str) else self.session_id
            if str(self.context.session_id) != str(session_id):
                problems.add(
                    "context",
                    f"describes session {self.context.session_id!r} but the request is for "
                    f"{self.session_id!r}",
                )
        problems.raise_if_any()
        object.__setattr__(self, "session_id", self.session_id.strip())

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_message": self.user_message,
            "context": self.context.as_dict(),
        }


@dataclass(frozen=True)
class TeacherTurnResponse:
    """What the model produced for one turn: a message plus structured intents.

    ``assistant_message`` is what the user is told -- deliberately **not** an action.
    ``actions`` keep their order (a future executor must honour it) and are only
    *requests*: nothing here is executed.
    """

    assistant_message: str
    actions: tuple[TeacherAction, ...] = ()

    def __post_init__(self) -> None:
        problems = _Problems("TeacherTurnResponse")
        _check_text(self.assistant_message, "assistant_message", problems)
        if isinstance(self.actions, (str, bytes)) or not isinstance(self.actions, Sequence):
            problems.add("actions", f"must be a sequence of TeacherAction, got {type(self.actions).__name__}")
        else:
            for index, action in enumerate(self.actions):
                if not isinstance(action, TeacherAction):
                    problems.add(
                        "actions",
                        f"entry {index} must be a TeacherAction, got {type(action).__name__}",
                    )
        problems.raise_if_any()
        object.__setattr__(self, "actions", tuple(self.actions))

    @property
    def action_types(self) -> tuple[str, ...]:
        return tuple(action.kind for action in self.actions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "assistant_message": self.assistant_message,
            "actions": [action.as_dict() for action in self.actions],
        }

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> "TeacherTurnResponse":
        """Build a response from a model's structured output (strict).

        Every action goes through :meth:`TeacherAction.parse`, so an unknown or
        forbidden action type fails here -- before the learning engine is involved.
        """
        if not isinstance(payload, Mapping):
            raise ValidationError(
                f"a teacher response must be a mapping, got {type(payload).__name__}", field="response"
            )
        unknown = sorted(set(payload) - {"assistant_message", "actions"})
        if unknown:
            raise ValidationError(
                f"unknown field(s) for a teacher response: {unknown}; allowed: "
                "['assistant_message', 'actions']",
                problems=tuple((name, "not part of the response") for name in unknown),
            )
        raw_actions = payload.get("actions", ())
        if isinstance(raw_actions, (str, bytes)) or not isinstance(raw_actions, Sequence):
            raise ValidationError(
                f"actions must be a sequence, got {type(raw_actions).__name__}", field="actions"
            )
        actions = tuple(TeacherAction.parse(item) for item in raw_actions)
        return cls(assistant_message=payload.get("assistant_message", ""), actions=actions)
