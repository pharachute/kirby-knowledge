"""Memory Formation (Phase 2): raw input -> LLM judgment -> validated Memory -> Repository.

Pipeline implemented here (and nowhere else)::

    RawInput (temporary, never persisted by itself)
        -> LLM adapter (prompts.py + llm.py)
        -> strict schema validation (ExtractionResult.parse)
        -> MemoryValuePolicy (drop/keep, status, source requirement)
        -> ONE SQLite transaction via MemoryRepository.transaction()
             Source (optional) + Memory rows + memory_sources links
        -> FormationOutcome

Guarantees
----------
* Nothing reaches SQLite unless it passed :meth:`ExtractionResult.parse` **and**
  Phase 1's ``Memory.create()`` validation.
* An input judged not worth remembering causes **zero** database writes.
* ``Source`` is optional: it is persisted only when required for traceability
  (policy :class:`FormationPolicy`).
* ``source + memories + links`` are written atomically; any failure rolls the
  whole formation back, so no half-formed memory can survive.

This module never issues SQL itself -- all persistence goes through Phase 1's
``MemoryRepository``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .errors import MemorySystemError, ValidationError
from .llm import LLMClient, LLMRequest, LLMResponse, LLMResponseError
from .models import (
    MAX_TAGS,
    MAX_TAG_LENGTH,
    MAX_TITLE_LENGTH,
    InformationOrigin,
    Memory,
    MemoryStatus,
    MemoryType,
    Source,
    SourceType,
    compute_content_hash,
    utcnow_iso,
)
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_retry_suffix, build_user_prompt
from .quality import MemoryQualityGate, QualityDecision, QualityReport, fingerprint_of
from .store import MemoryRepository

__all__ = [
    "RawInput",
    "MemoryDraft",
    "ExtractionResult",
    "FormationPolicy",
    "FormationOutcome",
    "MemoryFormationService",
    "ExtractionError",
    "ExtractionValidationError",
    "ALLOWED_MEMORY_KEYS",
    "ALLOWED_RESULT_KEYS",
    "KEEP_SOURCE_MODES",
]

KEEP_SOURCE_MODES = ("when_required", "always", "never")

ALLOWED_RESULT_KEYS = frozenset({"worth_remembering", "reason", "memories"})
ALLOWED_MEMORY_KEYS = frozenset(
    {
        "type",
        "title",
        "content",
        "summary",
        "tags",
        "importance",
        "confidence",
        "information_origin",
        "requires_source",
        "evidence_quote",
    }
)
MAX_MEMORY_DRAFTS = 10


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class ExtractionError(MemorySystemError):
    """Base class for Memory Formation failures."""


class ExtractionValidationError(ExtractionError):
    """The LLM answer could not be turned into a legal ExtractionResult.

    Carries ``problems`` (``(field, message)`` pairs) and ``attempts``, so a
    caller can tell *what* was wrong and *how many* tries were made.  Nothing
    was written to SQLite when this is raised.
    """

    def __init__(
        self,
        message: str,
        *,
        problems: Sequence[tuple[str, str]] = (),
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.problems: tuple[tuple[str, str], ...] = tuple(problems)
        self.attempts = attempts

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.problems)


class _Problems:
    """Collect every problem before failing, so one retry can fix several fields."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.items: list[tuple[str, str]] = []

    def add(self, field_name: str, message: str) -> None:
        self.items.append((field_name, message))

    def require(self, condition: bool, field_name: str, message: str) -> bool:
        if not condition:
            self.add(field_name, message)
        return bool(condition)

    def as_detail(self) -> str:
        return "; ".join(f"{field}: {message}" for field, message in self.items)


# --------------------------------------------------------------------------
# temporary input
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RawInput:
    """One piece of user input **in memory only**.

    This is deliberately *not* a ``Source``: Phase 2 treats user input as a
    temporary processing object.  It becomes a Source only if a formed Memory
    needs it for traceability (see :class:`FormationPolicy`).
    """

    content: str
    title: str | None = None
    url: str | None = None
    source_type: SourceType | str = SourceType.TEXT
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str | None = None

    def __post_init__(self) -> None:
        problems = _Problems("RawInput")
        if not isinstance(self.content, str) or not self.content.strip():
            problems.add("content", "must be a non-empty string")
        if self.title is not None and (not isinstance(self.title, str) or not self.title.strip()):
            problems.add("title", "must be None or a non-empty string")
        if self.url is not None:
            if not isinstance(self.url, str) or not self.url.strip().startswith(("http://", "https://")):
                problems.add("url", "must be None or an http(s) URL")
        source_type = self.source_type
        if not isinstance(source_type, SourceType):
            try:
                source_type = SourceType(str(source_type).strip().lower())
            except ValueError:
                problems.add("source_type", f"must be one of {[m.value for m in SourceType]}")
        if self.metadata is None or not isinstance(self.metadata, Mapping):
            problems.add("metadata", "must be a mapping")
        if problems.items:
            raise ExtractionValidationError(
                f"RawInput is not usable -> {problems.as_detail()}", problems=tuple(problems.items)
            )
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def content_hash(self) -> str:
        """Phase 1 dedup key of the raw text (used if the Source is persisted)."""
        return compute_content_hash(self.content)

    def as_prompt_context(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source_type": str(self.source_type),
            "metadata": dict(self.metadata),
            "content": self.content,
        }


# --------------------------------------------------------------------------
# extracted (validated) result
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryDraft:
    """One candidate Memory produced by the model, after schema validation.

    Not yet a ``Memory``: the policy still decides status and whether the draft
    survives; :meth:`to_memory` hands it to Phase 1's ``Memory.create()``, which
    is the final application-level validator.
    """

    type: MemoryType | str
    title: str
    content: str
    information_origin: InformationOrigin | str
    summary: str | None = None
    tags: tuple[str, ...] = ()
    importance: float = 0.5
    confidence: float = 0.5
    requires_source: bool = False
    evidence_quote: str | None = None

    def to_memory(self, *, status: MemoryStatus | str = MemoryStatus.ACTIVE) -> Memory:
        """Run the draft through the Phase 1 Memory validator/constructor."""
        try:
            return Memory.create(
                type=self.type,
                title=self.title,
                content=self.content,
                information_origin=self.information_origin,
                summary=self.summary,
                tags=list(self.tags),
                importance=self.importance,
                confidence=self.confidence,
                status=status,
            )
        except ValidationError as exc:
            raise ExtractionValidationError(
                f"LLM-produced memory failed Phase 1 validation -> {exc}", problems=exc.problems
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "title": self.title,
            "content": self.content,
            "summary": self.summary,
            "tags": list(self.tags),
            "importance": float(self.importance),
            "confidence": float(self.confidence),
            "information_origin": str(self.information_origin),
            "requires_source": bool(self.requires_source),
            "evidence_quote": self.evidence_quote,
        }


@dataclass(frozen=True)
class ExtractionResult:
    """The strictly validated content of one LLM answer."""

    worth_remembering: bool
    reason: str
    memories: tuple[MemoryDraft, ...]

    @property
    def source_required(self) -> bool:
        """True when at least one draft says it cannot be understood without the raw text."""
        return any(draft.requires_source for draft in self.memories)

    def as_dict(self) -> dict[str, Any]:
        return {
            "worth_remembering": self.worth_remembering,
            "reason": self.reason,
            "source_required": self.source_required,
            "memories": [draft.as_dict() for draft in self.memories],
        }

    # -- strict parsing ----------------------------------------------------
    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> "ExtractionResult":
        """Validate a raw JSON object coming from the model.

        Raises :class:`ExtractionValidationError` listing **every** problem, so
        the caller can either retry with that feedback or fail explicitly.
        Nothing is persisted by this method.
        """
        problems = _Problems("ExtractionResult")
        if not isinstance(payload, Mapping):
            raise ExtractionValidationError(
                f"model answer must be a JSON object, got {type(payload).__name__}",
                problems=(("<root>", "not a JSON object"),),
            )

        unknown = sorted(set(payload) - ALLOWED_RESULT_KEYS)
        if unknown:
            problems.add("<root>", f"unexpected field(s) {unknown}; allowed: {sorted(ALLOWED_RESULT_KEYS)}")

        worth = payload.get("worth_remembering")
        problems.require(
            isinstance(worth, bool),
            "worth_remembering",
            f"must be a JSON boolean, got {type(worth).__name__}",
        )
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.add("reason", "must be a non-empty string")

        raw_memories = payload.get("memories")
        drafts: list[MemoryDraft] = []
        if not isinstance(raw_memories, list):
            problems.add("memories", f"must be an array, got {type(raw_memories).__name__}")
            raw_memories = []
        elif len(raw_memories) > MAX_MEMORY_DRAFTS:
            problems.add("memories", f"at most {MAX_MEMORY_DRAFTS} memories per input, got {len(raw_memories)}")

        for index, item in enumerate(raw_memories):
            draft = cls._parse_draft(item, index, problems)
            if draft is not None:
                drafts.append(draft)

        if isinstance(worth, bool):
            if worth and not drafts:
                problems.add("memories", "worth_remembering is true but no memory was extracted")
            if not worth and drafts:
                problems.add(
                    "memories",
                    "worth_remembering is false but memories were returned; they must be empty",
                )

        if problems.items:
            raise ExtractionValidationError(
                f"invalid LLM answer -> {problems.as_detail()}", problems=tuple(problems.items)
            )
        return cls(worth_remembering=bool(worth), reason=reason.strip(), memories=tuple(drafts))

    @staticmethod
    def _parse_draft(item: Any, index: int, problems: _Problems) -> MemoryDraft | None:
        label = f"memories[{index}]"
        if not isinstance(item, Mapping):
            problems.add(label, f"must be an object, got {type(item).__name__}")
            return None
        unknown = sorted(set(item) - ALLOWED_MEMORY_KEYS)
        if unknown:
            problems.add(label, f"unexpected field(s) {unknown}; allowed: {sorted(ALLOWED_MEMORY_KEYS)}")
        before = len(problems.items)

        memory_type = _parse_enum(item.get("type"), MemoryType, f"{label}.type", problems)
        origin = _parse_enum(item.get("information_origin"), InformationOrigin, f"{label}.information_origin", problems)
        title = _parse_text(
            item.get("title"), f"{label}.title", problems, max_length=MAX_TITLE_LENGTH
        )
        content = _parse_text(item.get("content"), f"{label}.content", problems)
        summary = _parse_optional_text(item.get("summary"), f"{label}.summary", problems)
        evidence = _parse_optional_text(item.get("evidence_quote"), f"{label}.evidence_quote", problems)
        tags = _parse_tags(item.get("tags", []), f"{label}.tags", problems)
        importance = _parse_unit(item.get("importance", 0.5), f"{label}.importance", problems)
        confidence = _parse_unit(item.get("confidence", 0.5), f"{label}.confidence", problems)
        requires_source = item.get("requires_source", False)
        if not isinstance(requires_source, bool):
            problems.add(f"{label}.requires_source", f"must be a JSON boolean, got {type(requires_source).__name__}")

        if len(problems.items) > before:
            return None
        assert memory_type is not None and origin is not None  # guaranteed by the checks above
        return MemoryDraft(
            type=memory_type,
            title=title,
            content=content,
            information_origin=origin,
            summary=summary,
            tags=tuple(tags),
            importance=importance,
            confidence=confidence,
            requires_source=bool(requires_source),
            evidence_quote=evidence,
        )


def _parse_enum(value: Any, enum_cls: type, field_name: str, problems: _Problems):
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value.strip().lower())
        except ValueError:
            pass
    problems.add(field_name, f"must be one of {[m.value for m in enum_cls]}, got {value!r}")
    return None


def _parse_text(value: Any, field_name: str, problems: _Problems, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        problems.add(field_name, f"must be a non-empty string, got {value!r}")
        return ""
    text = value.strip()
    if max_length is not None and len(text) > max_length:
        problems.add(field_name, f"must be at most {max_length} characters, got {len(text)}")
        return ""
    return text


def _parse_optional_text(value: Any, field_name: str, problems: _Problems) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        problems.add(field_name, f"must be a string or null, got {type(value).__name__}")
        return None
    stripped = value.strip()
    return stripped or None


def _parse_tags(value: Any, field_name: str, problems: _Problems) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        problems.add(field_name, f"must be an array of strings, got {type(value).__name__}")
        return []
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            problems.add(field_name, f"every tag must be a non-empty string, got {item!r}")
            continue
        tag = item.strip()
        if len(tag) > MAX_TAG_LENGTH:
            problems.add(field_name, f"tag {tag[:20]!r}... exceeds {MAX_TAG_LENGTH} characters")
            continue
        tags.append(tag)
    if len(tags) > MAX_TAGS:
        problems.add(field_name, f"at most {MAX_TAGS} tags are allowed, got {len(tags)}")
    return tags


def _parse_unit(value: Any, field_name: str, problems: _Problems) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.add(field_name, f"must be a number in [0, 1], got {value!r}")
        return 0.0
    number = float(value)
    if not 0.0 <= number <= 1.0:
        problems.add(field_name, f"must be within [0, 1], got {value!r}")
    return number


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FormationPolicy:
    """Deterministic rules applied *around* the model's judgment.

    The model decides "is this worth remembering"; the policy decides the parts
    that must not be left to a model: how many memories to keep, what status an
    inference gets, and whether the raw input must be persisted for traceability.
    """

    max_memories: int = 5
    agent_inference_min_confidence: float = 0.6
    agent_inference_status: MemoryStatus | str = MemoryStatus.PENDING
    keep_source: str = "when_required"

    def __post_init__(self) -> None:
        if self.keep_source not in KEEP_SOURCE_MODES:
            raise ExtractionValidationError(
                f"keep_source must be one of {list(KEEP_SOURCE_MODES)}, got {self.keep_source!r}"
            )
        if self.max_memories < 1:
            raise ExtractionValidationError("max_memories must be >= 1")
        if not 0.0 <= self.agent_inference_min_confidence <= 1.0:
            raise ExtractionValidationError("agent_inference_min_confidence must be within [0, 1]")

    def select_drafts(self, result: ExtractionResult) -> tuple[list[MemoryDraft], list[str]]:
        """Return ``(kept, dropped_reasons)`` after the confidence/count policy."""
        kept: list[MemoryDraft] = []
        dropped: list[str] = []
        for draft in result.memories:
            if str(draft.information_origin) == InformationOrigin.AGENT_INFERENCE:
                if draft.confidence < self.agent_inference_min_confidence:
                    dropped.append(
                        f"dropped inference {draft.title!r}: confidence {draft.confidence:.2f} < "
                        f"{self.agent_inference_min_confidence:.2f} (unsupported inference is not stored)"
                    )
                    continue
            if len(kept) >= self.max_memories:
                dropped.append(f"dropped {draft.title!r}: over policy limit of {self.max_memories} memories")
                continue
            kept.append(draft)
        return kept, dropped

    def decide_sources(self, kept: Sequence[MemoryDraft]) -> tuple[bool, str]:
        """Whether the raw input must be persisted as a Source, and why."""
        if not kept:
            return False, "no memory was kept, so the raw input is not persisted"
        # HARD RULE (Phase 2 requirement): if a retained memory cannot be explained
        # without the raw text, its only evidence must be kept -- no policy, flag or
        # model output may delete it.
        requiring = [draft.title for draft in kept if draft.requires_source]
        if requiring:
            return True, f"memory cannot be explained without the raw text: {requiring}"
        if self.keep_source == "never":
            return False, "policy keep_source=never (no retained memory strictly requires the raw text)"
        if self.keep_source == "always":
            return True, "policy keep_source=always"
        from_source = [
            draft.title for draft in kept if str(draft.information_origin) == InformationOrigin.SOURCE_CONTENT
        ]
        if from_source:
            return True, f"memory states facts taken from the source material: {from_source}"
        return False, "every memory is self-contained; the raw input is not persisted"

    def status_for(self, draft: MemoryDraft) -> MemoryStatus:
        if str(draft.information_origin) == InformationOrigin.AGENT_INFERENCE:
            return MemoryStatus(self.agent_inference_status)
        return MemoryStatus.ACTIVE


# --------------------------------------------------------------------------
# outcome
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FormationOutcome:
    """What actually happened, with enough evidence to audit the run."""

    status: str  # "skipped" | "skipped_by_policy" | "persisted" | "preview"
    reason: str
    worth_remembering: bool
    input_digest: str
    memories: tuple[Memory, ...] = ()
    source: Source | None = None
    source_reused: bool = False
    links: int = 0
    attempts: int = 0
    dropped: tuple[str, ...] = ()
    prompt_version: str = PROMPT_VERSION
    model: str = ""
    llm: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Mapping[str, Any], ...] = ()
    #: Phase 4: stored Memories that already held this exact content (no new row).
    reused: tuple[Memory, ...] = ()
    #: Phase 4: the quality gate's decisions, or None when no gate was configured.
    quality: QualityReport | None = None

    @property
    def persisted(self) -> bool:
        return self.status == "persisted"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "worth_remembering": self.worth_remembering,
            "input_digest": self.input_digest,
            "memories": [memory.as_dict() for memory in self.memories],
            "reused": [memory.as_dict() for memory in self.reused],
            "quality": self.quality.as_dict() if self.quality else None,
            "source": self.source.as_dict() if self.source else None,
            "source_reused": self.source_reused,
            "links": self.links,
            "attempts": self.attempts,
            "dropped": list(self.dropped),
            "prompt_version": self.prompt_version,
            "model": self.model,
            "llm": dict(self.llm),
            "evidence": [dict(item) for item in self.evidence],
        }


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------

class MemoryFormationService:
    """Turns raw input into validated, persisted Memories (or explicitly nothing)."""

    def __init__(
        self,
        repository: MemoryRepository,
        llm: LLMClient,
        *,
        policy: FormationPolicy | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        prompt_version: str = PROMPT_VERSION,
        max_attempts: int = 2,
        quality: MemoryQualityGate | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ExtractionValidationError("max_attempts must be >= 1")
        self.repository = repository
        self.llm = llm
        self.policy = policy or FormationPolicy()
        # Phase 4: optional quality gate.  ``None`` keeps the exact Phase 2 path
        # (no duplicate scan, no conflict classification, no extra model calls).
        self.quality = quality
        self.system_prompt = system_prompt
        self.prompt_version = prompt_version
        self.max_attempts = max_attempts

    # -- public API --------------------------------------------------------
    def process(self, raw_input: RawInput, *, dry_run: bool = False) -> FormationOutcome:
        """Run the whole chain for one raw input. Performs no write when not valuable."""
        if not isinstance(raw_input, RawInput):
            raise ExtractionValidationError(f"expected a RawInput, got {type(raw_input).__name__}")

        result, responses = self._analyse(raw_input)
        last_response = responses[-1]
        base = dict(
            reason=result.reason,
            worth_remembering=result.worth_remembering,
            input_digest=raw_input.content_hash,
            attempts=len(responses),
            prompt_version=self.prompt_version,
            model=last_response.model or self.llm.config.model,
            llm=last_response.as_dict(),
        )

        if not result.worth_remembering:
            # Case A: no long-term value -> no Memory, no Source, no write at all.
            return FormationOutcome(status="skipped", dropped=(), **base)

        kept, dropped = self.policy.select_drafts(result)
        if not kept:
            return FormationOutcome(
                status="skipped_by_policy",
                reason=f"{result.reason} (all drafts dropped by policy)",
                dropped=tuple(dropped),
                **{k: v for k, v in base.items() if k != "reason"},
            )

        # Phase 4: the quality gate decides the status of every kept draft
        # (exact duplicate -> reuse, suspected conflict -> pending, compatible -> keep).
        plan, quality_report = self._plan(kept)
        dropped = list(dropped)
        for decision in quality_report.decisions if quality_report else ():
            if decision.reused:
                dropped.append(
                    f"{decision.title!r} is an exact duplicate of {decision.duplicate_of} "
                    "(no second Memory written)"
                )

        keep_source, source_reason = self.policy.decide_sources([draft for draft, _ in plan])
        evidence = tuple(
            {"title": draft.title, "requires_source": draft.requires_source, "quote": draft.evidence_quote}
            for draft, _ in plan
            if draft.requires_source or draft.evidence_quote
        )
        if dry_run:
            return FormationOutcome(
                status="preview",
                reason=f"{result.reason} | source: {source_reason}",
                memories=tuple(draft.to_memory(status=status) for draft, status in plan),
                dropped=tuple(dropped),
                evidence=evidence,
                quality=quality_report,
                **{k: v for k, v in base.items() if k != "reason"},
            )

        persisted = self._persist(raw_input, plan, keep_source=keep_source)
        reuse_ids = list(quality_report.duplicate_ids) if quality_report else []
        reuse_ids.extend(persisted.reused_ids)
        reused = tuple(
            memory
            for memory_id in dict.fromkeys(reuse_ids)
            if (memory := self.repository.get_memory(memory_id)) is not None
        )
        status = "persisted" if persisted.memories else "duplicate"
        reason = f"{result.reason} | source: {source_reason}"
        if reuse_ids:
            reason += f" | reused {len(reuse_ids)} exact-duplicate candidate(s), no second row"
        return FormationOutcome(
            status=status,
            reason=reason,
            memories=persisted.memories,
            reused=reused,
            source=persisted.source,
            source_reused=persisted.source_reused,
            links=persisted.links,
            dropped=tuple(dropped),
            evidence=evidence,
            quality=quality_report,
            **{k: v for k, v in base.items() if k != "reason"},
        )

    def _plan(
        self, kept: Sequence[MemoryDraft]
    ) -> tuple[list[tuple[MemoryDraft, str]], QualityReport | None]:
        """Decide the final status of every kept draft.

        With no quality gate this reproduces Phase 2 exactly: every kept draft is
        stored with :meth:`FormationPolicy.status_for`.  With a gate, an exact
        duplicate is dropped from the plan (``action="reuse"``) and a suspected
        conflict becomes ``pending`` -- the old Memory is never touched.
        """
        if self.quality is None:
            return [(draft, str(self.policy.status_for(draft))) for draft in kept], None
        plan: list[tuple[MemoryDraft, str]] = []
        decisions: list[QualityDecision] = []
        for draft in kept:
            try:
                decision = self.quality.evaluate_candidate(draft, base_status=self.policy.status_for(draft))
            except ValidationError as exc:
                # same contract as MemoryDraft.to_memory(): an unusable candidate is a
                # Phase 2 validation failure, never a raw error and never a write
                raise ExtractionValidationError(
                    f"candidate Memory failed quality validation -> {exc}", problems=exc.problems
                ) from exc
            decisions.append(decision)
            if decision.action == "persist":
                plan.append((draft, str(decision.status)))
        return plan, QualityReport(tuple(decisions))

    # -- internal steps ----------------------------------------------------
    def _analyse(self, raw_input: RawInput) -> tuple[ExtractionResult, list[LLMResponse]]:
        """Ask the model, validate the answer, retry on *content* failures only.

        Transport failures are retried inside the LLM client and then propagate
        unchanged: an unreachable provider must never be mistaken for "nothing
        worth remembering".
        """
        request = LLMRequest.of(
            self.system_prompt,
            build_user_prompt(raw_input.as_prompt_context()),
            purpose="memory_formation",
            prompt_version=self.prompt_version,
        )
        responses: list[LLMResponse] = []
        attempt_errors: list[str] = []
        problems: list[tuple[str, str]] = []
        for attempt in range(1, self.max_attempts + 1):
            try:
                payload, response = self.llm.complete_json(request)
            except LLMResponseError as exc:
                # unusable answer (not JSON, empty, or truncated): retry with feedback
                attempt_errors.append(f"attempt {attempt}: {exc}")
                problems.append((f"attempt {attempt}", str(exc)))
                request = request.with_user_suffix(
                    build_retry_suffix(str(exc), attempt=attempt, max_attempts=self.max_attempts)
                )
                continue
            responses.append(response)
            try:
                return ExtractionResult.parse(payload), responses
            except ExtractionValidationError as exc:
                attempt_errors.append(f"attempt {attempt}: {exc}")
                problems.extend(
                    (f"attempt {attempt} · {field}", message) for field, message in exc.problems
                )
                request = request.with_user_suffix(
                    build_retry_suffix(_render_problems(exc), attempt=attempt, max_attempts=self.max_attempts)
                )
        detail = " | ".join(attempt_errors) or "no attempt was made"
        raise ExtractionValidationError(
            f"the model did not produce a valid structured result after {self.max_attempts} attempt(s): {detail}",
            problems=tuple(problems),
            attempts=self.max_attempts,
        )

    def _persist(
        self,
        raw_input: RawInput,
        plan: Sequence[tuple[MemoryDraft, str]],
        *,
        keep_source: bool,
    ) -> "_Persisted":
        """Write Source (optional) + Memories + links in ONE transaction.

        Phase 4: when a quality gate is configured, the exact-duplicate scan runs
        **inside** this transaction.  The transaction already holds the write lock
        (``BEGIN IMMEDIATE``), so no other writer can insert an exact duplicate
        between the scan and the guarded INSERT; and because every newly inserted
        Memory is registered in the same scan, two identical drafts in one model
        answer cannot both be written either.
        """
        memories: list[Memory] = []
        reused_ids: list[str] = []
        links = 0
        source_reused = False
        with self.repository.transaction() as unit:
            scan = self._duplicate_scan(unit)
            # the lookup and the insert share one transaction (and one write lock),
            # so two writers cannot both miss and then collide on content_hash
            source = unit.find_source_by_content_hash(raw_input.content_hash) if keep_source else None
            source_reused = source is not None
            if keep_source and source is None:
                source = unit.create_source(self._build_source(raw_input))
            for draft, status in plan:
                fingerprint = fingerprint_of(draft) if scan is not None else None
                if scan is not None and fingerprint is not None:
                    existing_id = scan.find(fingerprint)
                    if existing_id is not None:
                        reused_ids.append(existing_id)
                        continue
                memory = unit.create_memory(draft.to_memory(status=status))
                if scan is not None and fingerprint is not None:
                    scan.remember(fingerprint, memory.id)
                memories.append(memory)
                if source is not None:
                    if unit.link(memory.id, source.id):
                        links += 1
        return _Persisted(
            memories=tuple(memories),
            reused_ids=tuple(reused_ids),
            source=source,
            source_reused=source_reused,
            links=links,
        )

    def _duplicate_scan(self, unit: Any):
        """The in-transaction dedupe scan, or None when dedupe is off/unconfigured."""
        if self.quality is None or not self.quality.policy.dedupe:
            return None
        return self.quality.scan_duplicates(unit)

    def _build_source(self, raw_input: RawInput) -> Source:
        metadata = dict(raw_input.metadata)
        metadata.setdefault(
            "formation",
            {
                "prompt_version": self.prompt_version,
                "model": self.llm.config.model,
                "formed_at": utcnow_iso(),
            },
        )
        return Source.create(
            source_type=raw_input.source_type,
            title=raw_input.title or _first_line(raw_input.content),
            content=raw_input.content,
            url=raw_input.url,
            metadata=metadata,
            content_hash=raw_input.content_hash,
            created_at=raw_input.created_at,
        )


@dataclass(frozen=True)
class _Persisted:
    """What one formation transaction actually wrote (internal)."""

    memories: tuple[Memory, ...]
    reused_ids: tuple[str, ...]
    source: Source | None
    source_reused: bool
    links: int


def _render_problems(exc: ExtractionValidationError) -> str:
    """Flatten a validation error into the feedback text sent back to the model."""
    return "; ".join(f"{field}: {message}" for field, message in exc.problems) or str(exc)


def _first_line(text: str, limit: int = 80) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "untitled input")
    return line if len(line) <= limit else line[: limit - 1] + "…"
