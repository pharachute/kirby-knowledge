"""Unified data model + validation layer for the Personal Memory System (Phase 1).

Design notes
------------
* There is exactly **one** Memory structure for all four memory types
  (``knowledge`` / ``experience`` / ``event`` / ``profile``).  The type is a
  validated column, not a subclass -- so schema evolution stays additive.
* ``Source`` and ``Memory`` are two independent entities.  They are connected
  only through the ``memory_sources`` relation table (see :mod:`personal_memory.store`).
* Validation lives in ``__post_init__``, therefore an invalid instance cannot
  be constructed at all.  Nothing invalid ever reaches SQLite through this
  layer; SQLite re-checks the same rules with CHECK constraints.
* No third-party dependency: the Phase 1 environment has no reachable PyPI
  (``pip install pydantic`` hangs), and the spec allows dataclasses.  The
  validation layer uses plain dataclasses + explicit checks.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .errors import ValidationError, describe_problems

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "SourceType",
    "MemoryType",
    "InformationOrigin",
    "MemoryStatus",
    "ALLOWED_TRANSITIONS",
    "allowed_transitions",
    "can_transition",
    "Source",
    "Memory",
    "utcnow_iso",
    "new_source_id",
    "new_memory_id",
    "normalize_content",
    "compute_content_hash",
    "is_valid_timestamp",
    "coerce_enum",
]

#: Schema version written into every Memory row (and tracked per migration).
CURRENT_SCHEMA_VERSION = 1

MAX_ID_LENGTH = 128
MAX_TITLE_LENGTH = 512
MAX_TAG_LENGTH = 64
MAX_TAGS = 64
MAX_URL_LENGTH = 2048

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_URL_RE = re.compile(r"^https?://\S+$")


class SourceType(StrEnum):
    """Kind of raw content a Source holds.

    Only ``text`` is created by Phase 1 code, the remaining values exist so
    later collectors (chat export, article, web page, file) are additive.
    """

    TEXT = "text"
    CHAT = "chat"
    ARTICLE = "article"
    WEB = "web"
    FILE = "file"


class MemoryType(StrEnum):
    """The four memory types share one structure."""

    KNOWLEDGE = "knowledge"
    EXPERIENCE = "experience"
    EVENT = "event"
    PROFILE = "profile"


class InformationOrigin(StrEnum):
    """Where a memory came from -- provenance, not confidence."""

    USER_EXPLICIT = "user_explicit"
    SOURCE_CONTENT = "source_content"
    AGENT_INFERENCE = "agent_inference"


class MemoryStatus(StrEnum):
    """Lifecycle state of a memory (Phase 1 choice; extensible later)."""

    ACTIVE = "active"
    PENDING = "pending"
    ARCHIVED = "archived"


#: The complete Memory status transition table (Phase 4).  It lives in this
#: dependency-free module because there is exactly **one** such table in the package
#: and both the lifecycle service and the repository must enforce the same object:
#:
#:     pending  -> active, archived      (activate_memory / archive_memory)
#:     active   -> archived              (archive_memory)
#:     archived -> active                (restore_memory)
#:
#: ``-> pending`` is deliberately absent.  ``pending`` Memories are only ever
#: *created* (Memory Formation's conservative policy) -- an existing Memory is never
#: rewritten into ``pending``, so ``active -> pending`` and ``archived -> pending``
#: are refused by both the lifecycle API and the repository.
ALLOWED_TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    str(MemoryStatus.ACTIVE): (str(MemoryStatus.ARCHIVED),),
    str(MemoryStatus.PENDING): (str(MemoryStatus.ACTIVE), str(MemoryStatus.ARCHIVED)),
    str(MemoryStatus.ARCHIVED): (str(MemoryStatus.ACTIVE),),
}


def allowed_transitions(status: Any) -> tuple[str, ...]:
    """Target statuses reachable from ``status`` (empty for an unknown status)."""
    return tuple(ALLOWED_TRANSITIONS.get(str(coerce_enum(status, MemoryStatus, "status")), ()))


def can_transition(from_status: Any, to_status: Any) -> bool:
    """True when ``from_status -> to_status`` is a legal, *actual* change."""
    return str(coerce_enum(to_status, MemoryStatus, "status")) in allowed_transitions(from_status)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def utcnow_iso() -> str:
    """Current UTC time as a sortable ISO-8601 string, e.g. ``2026-10-04T12:00:00.000Z``."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_source_id() -> str:
    return f"src_{uuid.uuid4().hex}"


def new_memory_id() -> str:
    return f"mem_{uuid.uuid4().hex}"


def is_valid_timestamp(value: Any) -> bool:
    """True when ``value`` is a non-empty ISO-8601 timestamp string."""
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def normalize_content(content: str) -> str:
    """Canonical form used for hashing: NFC, LF line endings, no trailing blanks."""
    if not isinstance(content, str):
        raise ValidationError("content must be a string when hashing", field="content")
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def compute_content_hash(content: str) -> str:
    """SHA-256 over :func:`normalize_content`.

    Hashing the normalized text means CRLF/whitespace-only differences are
    recognised as the same Source, which is what deduplication wants.
    """
    return hashlib.sha256(normalize_content(content).encode("utf-8")).hexdigest()


class _Problems:
    """Collects every validation problem before raising, so callers see all of them."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.items: list[tuple[str, str]] = []

    def add(self, field_name: str, message: str) -> None:
        self.items.append((field_name, message))

    def check(self, condition: bool, field_name: str, message: str) -> bool:
        if not condition:
            self.add(field_name, message)
        return bool(condition)

    def raise_if_any(self) -> None:
        if self.items:
            raise ValidationError(
                f"{self.model} validation failed -> {describe_problems(self.items)}",
                problems=tuple(self.items),
            )


def _check_id(value: Any, field_name: str, problems: _Problems) -> None:
    if not isinstance(value, str) or not _ID_RE.match(value):
        problems.add(
            field_name,
            f"must be 1-{MAX_ID_LENGTH} chars of [A-Za-z0-9_.:-] starting alphanumeric, got {value!r}",
        )


def _check_encodable(value: Any, field_name: str, problems: _Problems) -> bool:
    """Reject text SQLite cannot bind: unpaired surrogates are not valid UTF-8.

    Found by the Phase 4 adversarial review: a lone surrogate passed every regex,
    length and emptiness check and then died inside the sqlite3 driver as a raw
    ``UnicodeEncodeError`` -- outside the typed error hierarchy, and (through the
    CLI) as a traceback instead of exit code 3.
    """
    if not isinstance(value, str):
        return True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        problems.add(field_name, f"must be valid UTF-8 text (unpaired surrogate at index {exc.start})")
        return False
    return True


def _check_title(value: Any, field_name: str, problems: _Problems) -> None:
    if not isinstance(value, str):
        problems.add(field_name, f"must be a string, got {type(value).__name__}")
    elif not value.strip():
        problems.add(field_name, "must not be empty")
    elif len(value.strip()) > MAX_TITLE_LENGTH:
        problems.add(field_name, f"must be at most {MAX_TITLE_LENGTH} characters")
    _check_encodable(value, field_name, problems)


def _check_content(value: Any, field_name: str, problems: _Problems) -> None:
    if not isinstance(value, str):
        problems.add(field_name, f"must be a string, got {type(value).__name__}")
    elif not value.strip():
        problems.add(field_name, "must not be empty")
    _check_encodable(value, field_name, problems)


def _check_timestamp(value: Any, field_name: str, problems: _Problems) -> None:
    if not is_valid_timestamp(value):
        problems.add(field_name, f"must be an ISO-8601 timestamp string, got {value!r}")


def _as_enum(value: Any, enum_cls: type[StrEnum], field_name: str, problems: _Problems):
    """Coerce a string to the enum; record a problem and return None on failure."""
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value.strip().lower())
        except ValueError:
            pass
    allowed = ", ".join(member.value for member in enum_cls)
    problems.add(field_name, f"must be one of [{allowed}], got {value!r}")
    return None


def coerce_enum(value: Any, enum_cls: type[StrEnum], field_name: str) -> Any:
    """Coerce a string/enum member to ``enum_cls``; raise ``ValidationError`` otherwise.

    Used by the Phase 4 lifecycle/quality services, which accept either an enum
    member or its string form and must fail loudly on anything else instead of
    silently writing a status the schema does not know.
    """
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value.strip().lower())
        except ValueError:
            pass
    allowed = ", ".join(member.value for member in enum_cls)
    raise ValidationError(f"{field_name} must be one of [{allowed}], got {value!r}", field=field_name)


def _check_unit_number(value: Any, field_name: str, problems: _Problems) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.add(field_name, f"must be a number within [0.0, 1.0], got {value!r}")
        return
    number = float(value)
    if math.isnan(number) or math.isinf(number) or not (0.0 <= number <= 1.0):
        problems.add(field_name, f"must be within [0.0, 1.0], got {value!r}")


def _normalize_tags(value: Any, problems: _Problems) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        problems.add("tags", f"must be a sequence of strings, got {type(value).__name__}")
        return []
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            problems.add("tags", f"every tag must be a string, got {item!r}")
            continue
        tag = item.strip()
        if not tag:
            problems.add("tags", "tags must not be empty strings")
            continue
        if not _check_encodable(tag, "tags", problems):
            continue
        if len(tag) > MAX_TAG_LENGTH:
            problems.add("tags", f"tag {tag!r} exceeds {MAX_TAG_LENGTH} characters")
            continue
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        problems.add("tags", f"at most {MAX_TAGS} tags are allowed, got {len(tags)}")
    return tags


def _normalize_metadata(value: Any, problems: _Problems) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        problems.add("metadata", f"must be a mapping, got {type(value).__name__}")
        return {}
    metadata: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            problems.add("metadata", f"metadata keys must be non-empty strings, got {key!r}")
            continue
        metadata[key] = item
    try:
        json.dumps(metadata, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        problems.add("metadata", f"must be JSON-serializable: {exc}")
        return {}
    return metadata


def _normalize_url(value: Any, problems: _Problems) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        problems.add("url", f"must be a string or None, got {type(value).__name__}")
        return None
    url = value.strip()
    if not url:
        return None
    if not _check_encodable(url, "url", problems):
        return None
    if not _URL_RE.match(url):
        problems.add("url", f"must start with http:// or https://, got {value!r}")
        return None
    if len(url) > MAX_URL_LENGTH:
        problems.add("url", f"must be at most {MAX_URL_LENGTH} characters")
        return None
    return url


def _load_json(raw: Any, default: Any, field_name: str) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"stored {field_name} is not valid JSON: {exc}", field=field_name
        ) from exc


# --------------------------------------------------------------------------
# Source
# --------------------------------------------------------------------------

@dataclass
class Source:
    """One piece of raw input, addressed by its content hash.

    Phase 1 creates ``text`` sources only; ``source_type`` already accepts the
    future values (``chat`` / ``article`` / ``web`` / ``file``).
    """

    id: str
    source_type: SourceType | str
    title: str
    content: str
    content_hash: str
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    # -- construction ------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        source_type: SourceType | str,
        title: str,
        content: str,
        url: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        source_id: str | None = None,
        content_hash: str | None = None,
        created_at: str | None = None,
    ) -> "Source":
        """Build a Source, generating id / timestamp / content hash when omitted."""
        timestamp = created_at or utcnow_iso()
        return cls(
            id=source_id or new_source_id(),
            source_type=source_type,
            title=title,
            content=content,
            content_hash=content_hash if content_hash is not None else compute_content_hash(content),
            url=url,
            metadata=metadata if metadata is not None else {},
            created_at=timestamp,
            updated_at=timestamp,
        )

    # -- validation --------------------------------------------------------
    def validate(self) -> "Source":
        problems = _Problems("Source")
        source_type = _as_enum(self.source_type, SourceType, "source_type", problems)
        _check_id(self.id, "id", problems)
        _check_title(self.title, "title", problems)
        _check_content(self.content, "content", problems)
        if not isinstance(self.content_hash, str) or not _HASH_RE.match(self.content_hash):
            problems.add("content_hash", f"must be 64 lowercase hex characters, got {self.content_hash!r}")
        url = _normalize_url(self.url, problems)
        metadata = _normalize_metadata(self.metadata, problems)
        _check_timestamp(self.created_at, "created_at", problems)
        updated_at = self.created_at if self.updated_at is None else self.updated_at
        _check_timestamp(updated_at, "updated_at", problems)
        problems.raise_if_any()

        self.source_type = source_type  # type: ignore[assignment]
        self.title = self.title.strip()
        self.url = url
        self.metadata = metadata
        self.updated_at = updated_at
        return self

    # -- (de)serialization -------------------------------------------------
    def to_record(self) -> dict[str, Any]:
        """Flat mapping matching the ``sources`` table columns."""
        return {
            "id": self.id,
            "source_type": str(self.source_type),
            "title": self.title,
            "content": self.content,
            "url": self.url,
            "content_hash": self.content_hash,
            "metadata_json": json.dumps(self.metadata, ensure_ascii=False, sort_keys=True),
            "created_at": self.created_at,
            "updated_at": self.updated_at or self.created_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Source":
        """Rebuild a Source from a sqlite3.Row / dict."""
        data = dict(record)
        return cls(
            id=data["id"],
            source_type=data["source_type"],
            title=data["title"],
            content=data["content"],
            content_hash=data["content_hash"],
            url=data.get("url"),
            metadata=_load_json(data.get("metadata_json"), {}, "metadata_json"),
            created_at=data["created_at"],
            updated_at=data.get("updated_at"),
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view (nested metadata instead of ``metadata_json``)."""
        data = self.to_record()
        data["metadata"] = dict(self.metadata)
        data.pop("metadata_json", None)
        return data


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------

@dataclass
class Memory:
    """The single structure shared by all four memory types."""

    id: str
    type: MemoryType | str
    title: str
    content: str
    information_origin: InformationOrigin | str
    summary: str | None = None
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.5
    status: MemoryStatus | str = MemoryStatus.ACTIVE
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.validate()

    # -- construction ------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        type: MemoryType | str,
        title: str,
        content: str,
        information_origin: InformationOrigin | str,
        summary: str | None = None,
        tags: Sequence[str] | None = None,
        importance: float = 0.5,
        confidence: float = 0.5,
        status: MemoryStatus | str = MemoryStatus.ACTIVE,
        memory_id: str | None = None,
        created_at: str | None = None,
        schema_version: int = CURRENT_SCHEMA_VERSION,
    ) -> "Memory":
        timestamp = created_at or utcnow_iso()
        return cls(
            id=memory_id or new_memory_id(),
            type=type,
            title=title,
            content=content,
            information_origin=information_origin,
            summary=summary,
            tags=tags if tags is not None else [],
            importance=importance,
            confidence=confidence,
            status=status,
            created_at=timestamp,
            updated_at=timestamp,
            schema_version=schema_version,
        )

    # -- validation --------------------------------------------------------
    def validate(self) -> "Memory":
        problems = _Problems("Memory")
        memory_type = _as_enum(self.type, MemoryType, "type", problems)
        origin = _as_enum(self.information_origin, InformationOrigin, "information_origin", problems)
        status = _as_enum(self.status, MemoryStatus, "status", problems)
        _check_id(self.id, "id", problems)
        _check_title(self.title, "title", problems)
        _check_content(self.content, "content", problems)
        if self.summary is not None and not isinstance(self.summary, str):
            problems.add("summary", f"must be a string or None, got {type(self.summary).__name__}")
        _check_encodable(self.summary, "summary", problems)
        tags = _normalize_tags(self.tags, problems)
        _check_unit_number(self.importance, "importance", problems)
        _check_unit_number(self.confidence, "confidence", problems)
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            problems.add("schema_version", f"must be an integer, got {self.schema_version!r}")
        elif not 1 <= self.schema_version <= CURRENT_SCHEMA_VERSION:
            problems.add(
                "schema_version",
                f"must be within [1, {CURRENT_SCHEMA_VERSION}], got {self.schema_version!r}",
            )
        _check_timestamp(self.created_at, "created_at", problems)
        updated_at = self.created_at if self.updated_at is None else self.updated_at
        _check_timestamp(updated_at, "updated_at", problems)
        problems.raise_if_any()

        self.type = memory_type  # type: ignore[assignment]
        self.information_origin = origin  # type: ignore[assignment]
        self.status = status  # type: ignore[assignment]
        self.title = self.title.strip()
        if isinstance(self.summary, str):
            stripped = self.summary.strip()
            self.summary = stripped or None
        self.tags = tags
        self.importance = float(self.importance)
        self.confidence = float(self.confidence)
        self.updated_at = updated_at
        return self

    # -- (de)serialization -------------------------------------------------
    def to_record(self) -> dict[str, Any]:
        """Flat mapping matching the ``memories`` table columns."""
        return {
            "id": self.id,
            "type": str(self.type),
            "title": self.title,
            "content": self.content,
            "summary": self.summary,
            "tags_json": json.dumps(list(self.tags), ensure_ascii=False),
            "importance": float(self.importance),
            "confidence": float(self.confidence),
            "information_origin": str(self.information_origin),
            "status": str(self.status),
            "created_at": self.created_at,
            "updated_at": self.updated_at or self.created_at,
            "schema_version": int(self.schema_version),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Memory":
        """Rebuild a Memory from a sqlite3.Row / dict."""
        data = dict(record)
        return cls(
            id=data["id"],
            type=data["type"],
            title=data["title"],
            content=data["content"],
            information_origin=data["information_origin"],
            summary=data.get("summary"),
            tags=_load_json(data.get("tags_json"), [], "tags_json"),
            importance=data["importance"],
            confidence=data["confidence"],
            status=data["status"],
            created_at=data["created_at"],
            updated_at=data.get("updated_at"),
            schema_version=data["schema_version"],
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view (nested tags instead of ``tags_json``)."""
        data = self.to_record()
        data["tags"] = list(self.tags)
        data.pop("tags_json", None)
        return data
