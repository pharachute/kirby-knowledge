"""Learning-layer data models (KB 1.1 Phase 2A).

This module deliberately **reuses** the 1.0 conventions instead of inventing a
second set of them:

* timestamps come from :func:`personal_memory.models.utcnow_iso`
* validation uses the same ``_Problems`` / ``_check_*`` machinery and raises the
  same :class:`~personal_memory.errors.ValidationError` (with per-field problems)
* ``create() / validate() / to_record() / from_record() / as_dict()`` mirror
  :class:`~personal_memory.models.Source` and :class:`~personal_memory.models.Memory`

Two objects live here:

``LearningState``
    The relationship between the user and **one Memory** (1:1, ``memory_id`` is
    the natural primary key).  A missing row means "the user has never studied
    this Memory" -- that is the default state of the whole system.

``LearningSession``
    One concrete learning run over **one Source**.  A Source usually maps to
    several Memories, so a session walks them; ``plan`` is only the *candidate*
    order, ``plan_cursor`` only a hint -- neither carries teaching rules.

Out of scope on purpose (Phase 2A is the data layer): no LLM calls, no scoring,
no automatic progress.  In particular neither ``create()`` nor any repository
operation ever increments ``learn_count``: that may only happen after a real,
successfully analysed learning exchange, which is Phase 3's job.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .models import (
    _Problems,
    _as_enum,
    _check_encodable,
    _check_id,
    _check_timestamp,
    _load_json,
    utcnow_iso,
)

__all__ = [
    "LEARNING_SCHEMA_VERSION",
    "MAX_ASPECTS",
    "MAX_ASPECT_LENGTH",
    "MAX_PLAN_ITEMS",
    "MAX_EXCHANGE_KEYS",
    "UNDERSTANDING_LABELS",
    "SESSION_STATUS_LABELS",
    "TEACHING_STAGE_LABELS",
    "UnderstandingLevel",
    "SessionStatus",
    "TeachingStage",
    "new_learning_session_id",
    "LearningState",
    "LearningSession",
]

#: Version of the *learning row* shape (mirrors ``Memory.schema_version``).
LEARNING_SCHEMA_VERSION = 1

#: Bounds for the free-text aspect lists.  These are prose read/written as a
#: whole (never filtered in SQL), so the caps exist to keep rows small and the
#: UI honest -- not to model pedagogy.
MAX_ASPECTS = 16
MAX_ASPECT_LENGTH = 200
#: A session's candidate plan is a list of memory ids.
MAX_PLAN_ITEMS = 200
#: The resume buffer keeps the LAST exchange only; it is not a transcript.
MAX_EXCHANGE_KEYS = 16


class UnderstandingLevel(StrEnum):
    """Coarse, user-facing level.  No percentages, no float scores by design."""

    UNKNOWN = "unknown"   # 还没学过
    FUZZY = "fuzzy"       # 有点模糊
    PARTIAL = "partial"   # 部分理解
    SOLID = "solid"       # 讲得清


class SessionStatus(StrEnum):
    """Lifecycle of one learning run.

    ``active`` also covers "the user closed the browser" -- that is resumable,
    not abandoned.  ``abandoned`` is only ever an explicit user action.
    """

    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class TeachingStage(StrEnum):
    """Where the session currently stands in the teach -> ask -> analyse loop."""

    EXPLAIN = "explain"
    QUESTION = "question"
    ANALYZE = "analyze"
    REMEDY = "remedy"
    REINFORCE = "reinforce"
    PRACTICE = "practice"
    DONE = "done"


#: UI labels.  Kept next to the enums so the data layer never invents copy.
UNDERSTANDING_LABELS: Mapping[str, str] = {
    str(UnderstandingLevel.UNKNOWN): "还没学过",
    str(UnderstandingLevel.FUZZY): "有点模糊",
    str(UnderstandingLevel.PARTIAL): "部分理解",
    str(UnderstandingLevel.SOLID): "讲得清",
}

SESSION_STATUS_LABELS: Mapping[str, str] = {
    str(SessionStatus.ACTIVE): "学习中",
    str(SessionStatus.COMPLETED): "已完成",
    str(SessionStatus.ABANDONED): "已放弃",
}

TEACHING_STAGE_LABELS: Mapping[str, str] = {
    str(TeachingStage.EXPLAIN): "讲解",
    str(TeachingStage.QUESTION): "提问",
    str(TeachingStage.ANALYZE): "分析回答",
    str(TeachingStage.REMEDY): "补前置",
    str(TeachingStage.REINFORCE): "换讲法",
    str(TeachingStage.PRACTICE): "小练习",
    str(TeachingStage.DONE): "结束",
}


def new_learning_session_id() -> str:
    """A new session id, following the 1.0 ``src_``/``mem_`` id convention."""
    return f"lrn_{uuid.uuid4().hex}"


# --------------------------------------------------------------------------
# field validators (learning-specific types; reuse the 1.0 problem collector)
# --------------------------------------------------------------------------

def _check_count(value: Any, field_name: str, problems: _Problems, *, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        problems.add(field_name, f"must be an integer, got {value!r}")
        return
    if value < minimum:
        problems.add(field_name, f"must be >= {minimum}, got {value!r}")


def _normalize_aspects(value: Any, field_name: str, problems: _Problems) -> list[str]:
    """Short prose list: whitespace-collapsed, de-duplicated, bounded."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        problems.add(field_name, f"must be a sequence of strings, got {type(value).__name__}")
        return []
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            problems.add(field_name, f"every entry must be a string, got {item!r}")
            continue
        text = " ".join(item.split())
        if not text:
            problems.add(field_name, "entries must not be empty strings")
            continue
        if not _check_encodable(text, field_name, problems):
            continue
        if len(text) > MAX_ASPECT_LENGTH:
            problems.add(field_name, f"entry exceeds {MAX_ASPECT_LENGTH} characters")
            continue
        if text not in items:
            items.append(text)
    if len(items) > MAX_ASPECTS:
        problems.add(field_name, f"at most {MAX_ASPECTS} entries are allowed, got {len(items)}")
    return items


def _normalize_memory_id_list(value: Any, field_name: str, problems: _Problems) -> list[str]:
    """The candidate plan: memory ids, order preserved, de-duplicated."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        problems.add(field_name, f"must be a sequence of memory ids, got {type(value).__name__}")
        return []
    items: list[str] = []
    for item in value:
        before = len(problems.items)
        _check_id(item, field_name, problems)
        if len(problems.items) != before:
            continue
        if item not in items:
            items.append(item)
    if len(items) > MAX_PLAN_ITEMS:
        problems.add(field_name, f"at most {MAX_PLAN_ITEMS} entries are allowed, got {len(items)}")
    return items


def _normalize_exchange(value: Any, field_name: str, problems: _Problems) -> dict[str, Any]:
    """The single "last exchange" buffer: a JSON object, never a transcript."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        problems.add(field_name, f"must be a mapping, got {type(value).__name__}")
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            problems.add(field_name, f"keys must be non-empty strings, got {key!r}")
            continue
        result[key.strip()] = item
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        problems.add(field_name, f"must be JSON-serializable: {exc}")
        return {}
    if len(result) > MAX_EXCHANGE_KEYS:
        problems.add(field_name, f"at most {MAX_EXCHANGE_KEYS} keys are allowed, got {len(result)}")
    return result


# --------------------------------------------------------------------------
# LearningState
# --------------------------------------------------------------------------

@dataclass
class LearningState:
    """How well the user knows one Memory (1:1, keyed by ``memory_id``).

    A row exists only once the user has actually interacted with the Memory; a
    missing row is the normal "未学过" state and must not be materialised just
    because a session started.
    """

    memory_id: str
    understanding_level: UnderstandingLevel | str = UnderstandingLevel.UNKNOWN
    known_aspects: list[str] = field(default_factory=list)
    weak_aspects: list[str] = field(default_factory=list)
    misconceptions: list[str] = field(default_factory=list)
    learn_count: int = 0
    last_learned_at: str | None = None
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str | None = None
    schema_version: int = LEARNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.validate()

    # -- construction ------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        memory_id: str,
        understanding_level: UnderstandingLevel | str = UnderstandingLevel.UNKNOWN,
        known_aspects: Sequence[str] | None = None,
        weak_aspects: Sequence[str] | None = None,
        misconceptions: Sequence[str] | None = None,
        learn_count: int = 0,
        last_learned_at: str | None = None,
        created_at: str | None = None,
        schema_version: int = LEARNING_SCHEMA_VERSION,
    ) -> "LearningState":
        """Build a state row.

        ``learn_count`` defaults to **0** and ``last_learned_at`` to **None**:
        creating a state (or a session) is not a learning event.  Only a
        analysed exchange may raise them, and that is Phase 3's decision.
        """
        timestamp = created_at or utcnow_iso()
        return cls(
            memory_id=memory_id,
            understanding_level=understanding_level,
            known_aspects=known_aspects if known_aspects is not None else [],
            weak_aspects=weak_aspects if weak_aspects is not None else [],
            misconceptions=misconceptions if misconceptions is not None else [],
            learn_count=learn_count,
            last_learned_at=last_learned_at,
            created_at=timestamp,
            updated_at=timestamp,
            schema_version=schema_version,
        )

    # -- validation --------------------------------------------------------
    def validate(self) -> "LearningState":
        problems = _Problems("LearningState")
        _check_id(self.memory_id, "memory_id", problems)
        level = _as_enum(self.understanding_level, UnderstandingLevel, "understanding_level", problems)
        known = _normalize_aspects(self.known_aspects, "known_aspects", problems)
        weak = _normalize_aspects(self.weak_aspects, "weak_aspects", problems)
        misconceptions = _normalize_aspects(self.misconceptions, "misconceptions", problems)
        _check_count(self.learn_count, "learn_count", problems, minimum=0)
        if self.last_learned_at is not None:
            _check_timestamp(self.last_learned_at, "last_learned_at", problems)
        _check_timestamp(self.created_at, "created_at", problems)
        updated_at = self.created_at if self.updated_at is None else self.updated_at
        _check_timestamp(updated_at, "updated_at", problems)
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            problems.add("schema_version", f"must be an integer, got {self.schema_version!r}")
        elif not 1 <= self.schema_version <= LEARNING_SCHEMA_VERSION:
            problems.add(
                "schema_version",
                f"must be within [1, {LEARNING_SCHEMA_VERSION}], got {self.schema_version!r}",
            )
        problems.raise_if_any()

        self.understanding_level = level  # type: ignore[assignment]
        self.known_aspects = known
        self.weak_aspects = weak
        self.misconceptions = misconceptions
        self.learn_count = int(self.learn_count)
        self.updated_at = updated_at
        return self

    # -- (de)serialization -------------------------------------------------
    def to_record(self) -> dict[str, Any]:
        """Flat mapping matching the ``learning_states`` table columns."""
        return {
            "memory_id": self.memory_id,
            "understanding_level": str(self.understanding_level),
            "known_aspects_json": json.dumps(list(self.known_aspects), ensure_ascii=False),
            "weak_aspects_json": json.dumps(list(self.weak_aspects), ensure_ascii=False),
            "misconceptions_json": json.dumps(list(self.misconceptions), ensure_ascii=False),
            "learn_count": int(self.learn_count),
            "last_learned_at": self.last_learned_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at or self.created_at,
            "schema_version": int(self.schema_version),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "LearningState":
        """Rebuild a state from a sqlite3.Row / dict."""
        data = dict(record)
        return cls(
            memory_id=data["memory_id"],
            understanding_level=data["understanding_level"],
            known_aspects=_load_json(data.get("known_aspects_json"), [], "known_aspects_json"),
            weak_aspects=_load_json(data.get("weak_aspects_json"), [], "weak_aspects_json"),
            misconceptions=_load_json(data.get("misconceptions_json"), [], "misconceptions_json"),
            learn_count=data["learn_count"],
            last_learned_at=data.get("last_learned_at"),
            created_at=data["created_at"],
            updated_at=data.get("updated_at"),
            schema_version=data["schema_version"],
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view (nested lists instead of the ``*_json`` columns)."""
        data = self.to_record()
        data["known_aspects"] = list(self.known_aspects)
        data["weak_aspects"] = list(self.weak_aspects)
        data["misconceptions"] = list(self.misconceptions)
        for key in ("known_aspects_json", "weak_aspects_json", "misconceptions_json"):
            data.pop(key, None)
        return data


# --------------------------------------------------------------------------
# LearningSession
# --------------------------------------------------------------------------

@dataclass
class LearningSession:
    """One learning run over one Source.

    ``plan`` / ``plan_cursor`` are **bookkeeping only**: the service is free to
    skip, repeat or reorder anything (see the module docstring).  ``exchange``
    holds the LAST exchange so a reopened page can resume in place; it is
    overwritten each turn and is not a conversation archive.
    """

    id: str
    source_id: str
    status: SessionStatus | str = SessionStatus.ACTIVE
    current_memory_id: str | None = None
    current_stage: TeachingStage | str = TeachingStage.EXPLAIN
    plan: list[str] = field(default_factory=list)
    plan_cursor: int = 0
    exchange: dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=utcnow_iso)
    updated_at: str | None = None
    ended_at: str | None = None
    schema_version: int = LEARNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.validate()

    # -- construction ------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        source_id: str,
        status: SessionStatus | str = SessionStatus.ACTIVE,
        current_memory_id: str | None = None,
        current_stage: TeachingStage | str = TeachingStage.EXPLAIN,
        plan: Sequence[str] | None = None,
        plan_cursor: int = 0,
        exchange: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        schema_version: int = LEARNING_SCHEMA_VERSION,
    ) -> "LearningSession":
        timestamp = started_at or utcnow_iso()
        return cls(
            id=session_id or new_learning_session_id(),
            source_id=source_id,
            status=status,
            current_memory_id=current_memory_id,
            current_stage=current_stage,
            plan=plan if plan is not None else [],
            plan_cursor=plan_cursor,
            exchange=exchange if exchange is not None else {},
            started_at=timestamp,
            updated_at=timestamp,
            ended_at=ended_at,
            schema_version=schema_version,
        )

    # -- validation --------------------------------------------------------
    def validate(self) -> "LearningSession":
        problems = _Problems("LearningSession")
        _check_id(self.id, "id", problems)
        _check_id(self.source_id, "source_id", problems)
        status = _as_enum(self.status, SessionStatus, "status", problems)
        stage = _as_enum(self.current_stage, TeachingStage, "current_stage", problems)
        if self.current_memory_id is not None:
            _check_id(self.current_memory_id, "current_memory_id", problems)
        plan = _normalize_memory_id_list(self.plan, "plan", problems)
        _check_count(self.plan_cursor, "plan_cursor", problems, minimum=0)
        exchange = _normalize_exchange(self.exchange, "exchange", problems)
        _check_timestamp(self.started_at, "started_at", problems)
        updated_at = self.started_at if self.updated_at is None else self.updated_at
        _check_timestamp(updated_at, "updated_at", problems)
        if self.ended_at is not None:
            _check_timestamp(self.ended_at, "ended_at", problems)
        # status and ended_at must agree: a running session has no end, a closed one must have it
        if status is not None:
            finished = str(status) != str(SessionStatus.ACTIVE)
            if finished and self.ended_at is None:
                problems.add(
                    "ended_at", f"must be set when status is {str(status)!r}"
                )
            if not finished and self.ended_at is not None:
                problems.add("ended_at", "must be None while the session is active")
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            problems.add("schema_version", f"must be an integer, got {self.schema_version!r}")
        elif not 1 <= self.schema_version <= LEARNING_SCHEMA_VERSION:
            problems.add(
                "schema_version",
                f"must be within [1, {LEARNING_SCHEMA_VERSION}], got {self.schema_version!r}",
            )
        problems.raise_if_any()

        self.status = status  # type: ignore[assignment]
        self.current_stage = stage  # type: ignore[assignment]
        self.plan = plan
        self.plan_cursor = int(self.plan_cursor)
        self.exchange = exchange
        self.updated_at = updated_at
        return self

    # -- (de)serialization -------------------------------------------------
    def to_record(self) -> dict[str, Any]:
        """Flat mapping matching the ``learning_sessions`` table columns."""
        return {
            "id": self.id,
            "source_id": self.source_id,
            "status": str(self.status),
            "current_memory_id": self.current_memory_id,
            "current_stage": str(self.current_stage),
            "plan_json": json.dumps(list(self.plan), ensure_ascii=False),
            "plan_cursor": int(self.plan_cursor),
            "exchange_json": json.dumps(self.exchange, ensure_ascii=False),
            "started_at": self.started_at,
            "updated_at": self.updated_at or self.started_at,
            "ended_at": self.ended_at,
            "schema_version": int(self.schema_version),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "LearningSession":
        """Rebuild a session from a sqlite3.Row / dict."""
        data = dict(record)
        return cls(
            id=data["id"],
            source_id=data["source_id"],
            status=data["status"],
            current_memory_id=data.get("current_memory_id"),
            current_stage=data["current_stage"],
            plan=_load_json(data.get("plan_json"), [], "plan_json"),
            plan_cursor=data["plan_cursor"],
            exchange=_load_json(data.get("exchange_json"), {}, "exchange_json"),
            started_at=data["started_at"],
            updated_at=data.get("updated_at"),
            ended_at=data.get("ended_at"),
            schema_version=data["schema_version"],
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view (nested plan/exchange instead of the ``*_json`` columns)."""
        data = self.to_record()
        data["plan"] = list(self.plan)
        data["exchange"] = dict(self.exchange)
        data.pop("plan_json", None)
        data.pop("exchange_json", None)
        return data
