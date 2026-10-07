"""Knowledge Base 1.0 -- Phase 1: the **Capture Layer**.

It answers one question and nothing else: *how does a piece of raw information get
into the frozen Personal Memory System through one standardised input structure?*::

    User Input
        -> Capture             (this module: receive, validate, standardise)
        -> CaptureRequest      (capture-layer record: content / title / source_type /
                                url / metadata / captured_at)
        -> RawInput            (Phase 2's structure -- ``to_raw_input()`` is the ONE adapter)
        -> Memory Formation    (value judgment, extraction, policy, atomic write)
        -> Memory System       (SQLite, retrieval, lifecycle -- all already frozen)

What this module deliberately does **not** do, because Phases 2-4 already own it:

* no value judgment, no extraction, no prompt, no model call;
* no SQLite, no repository, no search, no lifecycle;
* it cannot even reach the database -- :class:`CaptureService` is constructed with a
  :class:`~personal_memory.extraction.MemoryFormationService` and nothing else.

Consequences that matter:

* A low-value input stays low value.  Capture never persists a Source just because it
  received something: the Phase 2 policy decides, and ``worth_remembering=false`` means
  **zero writes**.
* A failure inside Formation (transport, unusable answer, write) propagates as the
  existing typed error and leaves nothing half-written -- Capture adds no state of its
  own and swallow nothing.

Only ``source_type="text"`` is produced in this phase.  ``chat`` / ``article`` /
``web`` / ``file`` are accepted by the structure (they exist in Phase 1's
:class:`~personal_memory.models.SourceType`) but no importer, fetcher or parser for
them exists -- ``url`` is carried as data and never fetched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import ValidationError
from .extraction import FormationOutcome, MemoryFormationService, RawInput
from .models import Memory, Source, SourceType, coerce_enum, is_valid_timestamp, utcnow_iso

__all__ = [
    "CaptureRequest",
    "CaptureResult",
    "CaptureService",
    "CAPTURED_FROM_DEFAULT",
]

#: Provenance recorded in the metadata of everything this layer captures.
CAPTURED_FROM_DEFAULT = "manual"


@dataclass(frozen=True)
class CaptureRequest:
    """One piece of raw information, standardised by the Capture layer.

    The six fields are exactly what a capture source must be able to express.  This is
    a **boundary record**, not a second Memory/Source model: the only transformation is
    :meth:`to_raw_input`, which renames ``captured_at`` to Phase 2's ``created_at`` and
    forwards everything else untouched.
    """

    content: str
    title: str | None = None
    source_type: SourceType | str = SourceType.TEXT
    url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    captured_at: str = field(default_factory=utcnow_iso)

    def __post_init__(self) -> None:
        # Boundary validation only: "is this a well-formed piece of input at all?".
        # Whether it is *worth remembering* is Phase 2's judgment, never Capture's.
        # Each problem is reported on its own field; the content itself is validated a
        # second time by Phase 2's RawInput, which stays the authority.
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValidationError("captured content must be a non-empty string", field="content")
        if self.title is not None and (not isinstance(self.title, str) or not self.title.strip()):
            raise ValidationError("title must be None or a non-empty string", field="title")
        source_type = coerce_enum(self.source_type, SourceType, "source_type")
        if self.url is not None:
            if not isinstance(self.url, str) or not self.url.strip().startswith(("http://", "https://")):
                raise ValidationError("url must be None or an http(s) URL", field="url")
        if self.metadata is None or not isinstance(self.metadata, Mapping):
            raise ValidationError("metadata must be a mapping", field="metadata")
        if not is_valid_timestamp(self.captured_at):
            raise ValidationError("captured_at must be an ISO-8601 timestamp string", field="captured_at")

        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "metadata", dict(self.metadata))

    # -- the one adapter to the frozen formation layer ----------------------
    def to_raw_input(self, *, captured_from: str | None = None) -> RawInput:
        """Turn this request into the :class:`~personal_memory.extraction.RawInput`.

        A field rename plus one metadata key -- no logic of its own:

        * ``captured_at`` -> ``created_at`` (that is what the timestamp means when the
          Knowledge Base captures an input, and Phase 2 forwards it to the Source), and
        * ``metadata["captured_from"]`` records where the input came from (default
          ``"manual"``); an explicit value in ``metadata`` wins.
        """
        metadata = dict(self.metadata)
        if captured_from is not None:
            if not isinstance(captured_from, str) or not captured_from.strip():
                raise ValidationError(
                    "captured_from must be None or a non-empty string", field="captured_from"
                )
            metadata.setdefault("captured_from", captured_from.strip())
        return RawInput(
            content=self.content,
            title=self.title,
            url=self.url,
            source_type=self.source_type,
            metadata=metadata,
            created_at=self.captured_at,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "content_chars": len(self.content),
            "title": self.title,
            "source_type": str(self.source_type),
            "url": self.url,
            "metadata": dict(self.metadata),
            "captured_at": self.captured_at,
        }


@dataclass(frozen=True)
class CaptureResult:
    """What Capture can tell the caller.

    Capture reports *acceptance and outcome*, never a judgment of its own: the value
    decision, the created Memories and the Source policy all come from the Phase 2
    :class:`~personal_memory.extraction.FormationOutcome` carried here unchanged.
    """

    request: CaptureRequest
    formation_result: FormationOutcome
    captured_from: str = CAPTURED_FROM_DEFAULT

    @property
    def captured(self) -> bool:
        """True when the input was accepted and handed to Memory Formation.

        This is about *acceptance*, not persistence: a low-value input is ``captured``
        with ``status="skipped"`` and zero writes.  ``captured=False`` only happens for
        a dry run (``status="preview"``); a Formation failure raises instead of
        returning a result.
        """
        return self.formation_result.status != "preview"

    @property
    def status(self) -> str:
        """Formation status: ``persisted`` / ``skipped`` / ``skipped_by_policy`` / ``duplicate`` / ``preview``."""
        return self.formation_result.status

    @property
    def formation_status(self) -> str:
        return self.formation_result.status

    @property
    def memories_created(self) -> tuple[Memory, ...]:
        return tuple(self.formation_result.memories)

    @property
    def sources_created(self) -> tuple[Source, ...]:
        source = self.formation_result.source
        return (source,) if source is not None else ()

    @property
    def memory_count(self) -> int:
        return len(self.formation_result.memories)

    @property
    def source_count(self) -> int:
        return len(self.sources_created)

    @property
    def source_reused(self) -> bool:
        return bool(self.formation_result.source_reused)

    @staticmethod
    def _source_summary(source: Source) -> dict[str, Any]:
        """Source metadata only -- the body is the input the caller already has."""
        return {
            "id": source.id,
            "source_type": str(source.source_type),
            "title": source.title,
            "url": source.url,
            "content_hash": source.content_hash,
            "created_at": source.created_at,
            "metadata": dict(source.metadata),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "captured": self.captured,
            "status": self.status,
            "formation_status": self.formation_status,
            "captured_from": self.captured_from,
            "input": self.request.as_dict(),
            "memories_created": [memory.as_dict() for memory in self.memories_created],
            "sources_created": [self._source_summary(source) for source in self.sources_created],
            "memory_count": self.memory_count,
            "source_count": self.source_count,
            "source_reused": self.source_reused,
            "formation_result": self.formation_result.as_dict(),
        }


class CaptureService:
    """Receive -> validate -> standardise -> Memory Formation, and nothing else.

    It holds a :class:`~personal_memory.extraction.MemoryFormationService` (which owns
    the repository and the LLM adapter) so Capture cannot write SQLite or call a model
    even by accident.
    """

    def __init__(
        self,
        formation: MemoryFormationService,
        *,
        captured_from: str = CAPTURED_FROM_DEFAULT,
    ) -> None:
        if not isinstance(formation, MemoryFormationService):
            raise ValidationError(
                f"CaptureService needs a MemoryFormationService, got {type(formation).__name__}",
                field="formation",
            )
        if not isinstance(captured_from, str) or not captured_from.strip():
            raise ValidationError("captured_from must be a non-empty string", field="captured_from")
        self.formation = formation
        self.captured_from = captured_from.strip()

    # -- public API --------------------------------------------------------
    def capture(
        self,
        content: Any,
        *,
        title: str | None = None,
        source_type: SourceType | str = SourceType.TEXT,
        url: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        captured_at: str | None = None,
        dry_run: bool = False,
    ) -> CaptureResult:
        """Capture one piece of text (the only source kind produced in this phase).

        ``content`` is the raw text the user submitted.  Nothing is decided here beyond
        "is this well-formed": :class:`CaptureRequest` validates, then Memory Formation
        judges value, extracts Memories and applies the Source policy.
        """
        request = CaptureRequest(
            content=content,
            title=title,
            source_type=source_type,
            url=url,
            metadata={} if metadata is None else metadata,
            captured_at=captured_at or utcnow_iso(),
        )
        return self.capture_request(request, dry_run=dry_run)

    def capture_request(self, request: CaptureRequest, *, dry_run: bool = False) -> CaptureResult:
        """Capture an already-standardised :class:`CaptureRequest`."""
        if not isinstance(request, CaptureRequest):
            raise ValidationError(
                f"expected a CaptureRequest, got {type(request).__name__}", field="request"
            )
        outcome = self.formation.process(
            request.to_raw_input(captured_from=self.captured_from), dry_run=dry_run
        )
        return CaptureResult(
            request=request,
            formation_result=outcome,
            captured_from=self.captured_from,
        )
