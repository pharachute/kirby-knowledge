"""Phase 4: Memory Quality Control -- exact-duplicate detection and conservative
conflict handling for newly formed Memories.

Where this sits in the pipeline::

    MemoryDraft (Memory Formation)
        |
        +-- canonical fingerprint -- equal to a stored Memory? --+--> reuse it;
        |                                                        |    never a second
        |                                                        |    identical row
        +-- Phase 3 keyword retrieval finds related Memories (active + pending)
                |
                +-- conflict classifier (optional; the EXISTING llm.py adapter)
                        same | compatible | conflict | uncertain
                            |
                            +-- policy (code, deterministic) decides the status:
                                  same / conflict / uncertain -> pending (conservative)
                                  compatible                  -> keep the proposed status

Hard rules (Phase 4 spec, section 12):

* **New information never overwrites old information automatically.**  Nothing in
  this module updates or deletes an existing Memory; it only decides the status
  of the *new* candidate.
* The classifier only classifies.  It cannot write: code maps the relation to a
  status and :class:`~personal_memory.store.MemoryRepository` performs the write.
* Exact duplicates are decided by normalisation rules, not by a model.
* No embeddings, vectors, semantic search or reranking: "almost the same" is
  explicitly out of scope for v0.1 (see docs/PHASE4.md, known limitations).

This module contains **no SQL**: it reads and writes only through the repository
-- or through a :class:`~personal_memory.store.MemoryUnitOfWork`, so the final
duplicate check can run inside the same transaction as the insert it guards.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from .errors import MemorySystemError, ValidationError
from .llm import LLMClient, LLMRequest, LLMResponseError
from .models import Memory, MemoryStatus, MemoryType, coerce_enum
from .prompts import (
    CONFLICT_PROMPT_VERSION,
    CONFLICT_SYSTEM_PROMPT,
    build_conflict_user_prompt,
    build_retry_suffix,
)
from .retrieval import MemoryRetriever

__all__ = [
    "FINGERPRINT_VERSION",
    "RELATION_SAME",
    "RELATION_COMPATIBLE",
    "RELATION_CONFLICT",
    "RELATION_UNCERTAIN",
    "RELATION_NONE",
    "RELATION_UNCHECKED",
    "RELATION_DUPLICATE",
    "RELATIONS",
    "PENDING_RELATIONS",
    "QualityError",
    "ConflictClassificationError",
    "canonicalize_text",
    "memory_fingerprint",
    "fingerprint_of",
    "DuplicateScan",
    "DuplicateCheck",
    "ConflictVerdict",
    "ConflictCheck",
    "QualityDecision",
    "QualityReport",
    "QualityPolicy",
    "ConflictClassifier",
    "LLMConflictClassifier",
    "MemoryQualityGate",
]

#: Bump when the normalisation rules change (fingerprints are not portable across versions).
FINGERPRINT_VERSION = "memory-fingerprint-v1"

RELATION_SAME = "same"
RELATION_COMPATIBLE = "compatible"
RELATION_CONFLICT = "conflict"
RELATION_UNCERTAIN = "uncertain"

#: The only labels a conflict classifier may return.
RELATIONS: tuple[str, ...] = (
    RELATION_SAME,
    RELATION_COMPATIBLE,
    RELATION_CONFLICT,
    RELATION_UNCERTAIN,
)

#: Internal states of a conflict check that are *not* model answers.
RELATION_NONE = "none"            # keyword retrieval found nothing related
RELATION_UNCHECKED = "unchecked"  # related memories exist, but checking was disabled
RELATION_DUPLICATE = "duplicate"  # exact duplicate (deterministic, never model-driven)

#: Relations that force the conservative ``pending`` status (never ``active``).
PENDING_RELATIONS: tuple[str, ...] = (RELATION_SAME, RELATION_CONFLICT, RELATION_UNCERTAIN)

#: How much of the candidate content is turned into the retrieval query (chars).
MAX_QUERY_CHARS = 300

_WHITESPACE_RE = re.compile(r"\s+")


class QualityError(MemorySystemError):
    """Memory quality control could not produce a decision."""


class ConflictClassificationError(QualityError):
    """The conflict classifier did not return a usable ``same|compatible|conflict|uncertain``.

    Carries ``problems`` (``(field, message)`` pairs) and ``attempts`` like the
    Phase 2 validation error.  Nothing was written when this is raised.
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


# --------------------------------------------------------------------------
# canonicalisation + fingerprint (pure functions)
# --------------------------------------------------------------------------

def canonicalize_text(text: Any) -> str:
    """Stable normal form used by the duplicate fingerprint.

    ``NFKC`` -> LF line endings -> ``casefold`` -> every whitespace run becomes a
    single space -> strip.  Punctuation is deliberately **kept**: "likes A." and
    "likes A" stay different strings (conservative; near-duplicates are out of
    scope for v0.1).
    """
    if not isinstance(text, str):
        raise ValidationError(f"text to canonicalise must be a string, got {type(text).__name__}", field="text")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        # unpaired surrogates survive every regex/length check and only fail when
        # SQLite binds them; refuse them here as a typed error (Phase 4 review fix)
        raise ValidationError(
            f"text to canonicalise must be valid UTF-8 (unpaired surrogate at index {exc.start})", field="text"
        ) from exc
    normalized = unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return _WHITESPACE_RE.sub(" ", normalized).strip().casefold()


def memory_fingerprint(memory_type: Any, title: Any, content: Any) -> str:
    """Canonical fingerprint: ``type`` + normalised ``title`` + normalised ``content``.

    The type participates, so a ``knowledge`` and a ``profile`` Memory with the
    same wording stay different Memories.  Tags/summary/importance do **not**
    participate (documented in docs/PHASE4.md).
    """
    type_text = str(coerce_enum(memory_type, MemoryType, "type"))
    payload = "\x1f".join(
        (FINGERPRINT_VERSION, type_text, canonicalize_text(title), canonicalize_text(content))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint_of(candidate: Any) -> str:
    """Fingerprint of a :class:`~personal_memory.models.Memory` or ``MemoryDraft``."""
    for attribute in ("type", "title", "content"):
        if not hasattr(candidate, attribute):
            raise ValidationError(
                f"cannot fingerprint {type(candidate).__name__}: missing {attribute!r}", field=attribute
            )
    return memory_fingerprint(candidate.type, candidate.title, candidate.content)


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

@dataclass
class DuplicateScan:
    """Working set ``fingerprint -> memory_id`` for one dedupe pass.

    Deliberately mutable: :class:`MemoryQualityGate` builds it once and the
    formation transaction registers each newly inserted Memory in it, so two
    identical drafts inside one LLM answer cannot both be written.
    """

    fingerprints: dict[str, str] = field(default_factory=dict)
    scanned: int = 0

    def find(self, fingerprint: str) -> str | None:
        return self.fingerprints.get(fingerprint)

    def remember(self, fingerprint: str, memory_id: str) -> None:
        self.fingerprints.setdefault(fingerprint, memory_id)

    def as_dict(self) -> dict[str, Any]:
        return {"scanned": self.scanned, "unique_fingerprints": len(self.fingerprints)}


@dataclass(frozen=True)
class DuplicateCheck:
    """Result of an exact-duplicate lookup."""

    fingerprint: str
    duplicate_of: str | None
    existing: Memory | None
    scanned: int
    reason: str

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "duplicate_of": self.duplicate_of,
            "scanned": self.scanned,
            "reason": self.reason,
            "existing_status": str(self.existing.status) if self.existing else None,
        }


@dataclass(frozen=True)
class ConflictVerdict:
    """A classifier answer: one of the four relations plus its reason."""

    relation: str
    reason: str
    classifier: str = "llm"
    llm: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "reason": self.reason,
            "classifier": self.classifier,
            "llm": dict(self.llm),
        }


@dataclass(frozen=True)
class ConflictCheck:
    """Everything the conflict step observed for one candidate."""

    relation: str
    query: str
    related: tuple[Memory, ...]
    verdict: ConflictVerdict | None
    reason: str

    @property
    def related_ids(self) -> tuple[str, ...]:
        return tuple(memory.id for memory in self.related)

    def as_dict(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "query": self.query,
            "reason": self.reason,
            "related": [
                {"id": memory.id, "title": memory.title, "status": str(memory.status)} for memory in self.related
            ],
            "verdict": self.verdict.as_dict() if self.verdict else None,
        }


@dataclass(frozen=True)
class QualityDecision:
    """The policy's decision for one candidate Memory.

    ``action="persist"`` -> write the candidate with ``status``;
    ``action="reuse"``   -> do **not** write it, the exact duplicate
    ``duplicate_of`` already holds this content.
    """

    action: str
    fingerprint: str
    title: str
    status: str | None
    base_status: str
    relation: str
    duplicate_of: str | None = None
    related_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    duplicate: DuplicateCheck | None = None
    conflict: ConflictCheck | None = None

    @property
    def reused(self) -> bool:
        return self.action == "reuse"

    @property
    def pended(self) -> bool:
        return self.action == "persist" and self.status == str(MemoryStatus.PENDING)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "title": self.title,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "base_status": self.base_status,
            "relation": self.relation,
            "duplicate_of": self.duplicate_of,
            "related_ids": list(self.related_ids),
            "reasons": list(self.reasons),
            "duplicate": self.duplicate.as_dict() if self.duplicate else None,
            "conflict": self.conflict.as_dict() if self.conflict else None,
        }


@dataclass(frozen=True)
class QualityReport:
    """All decisions of one formation run (audit trail, no writes by itself)."""

    decisions: tuple[QualityDecision, ...] = ()

    @property
    def total(self) -> int:
        return len(self.decisions)

    @property
    def persisted(self) -> int:
        return sum(1 for decision in self.decisions if decision.action == "persist")

    @property
    def reused(self) -> int:
        return sum(1 for decision in self.decisions if decision.action == "reuse")

    @property
    def pended(self) -> int:
        return sum(1 for decision in self.decisions if decision.pended)

    @property
    def duplicate_ids(self) -> tuple[str, ...]:
        return tuple(
            decision.duplicate_of for decision in self.decisions if decision.duplicate_of is not None
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "persisted": self.persisted,
            "reused": self.reused,
            "pended": self.pended,
            "duplicate_ids": list(self.duplicate_ids),
            "decisions": [decision.as_dict() for decision in self.decisions],
        }


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityPolicy:
    """Deterministic rules of the quality gate (no model involvement)."""

    dedupe: bool = True
    conflict_check: bool = True
    related_limit: int = 5
    related_statuses: tuple[str, ...] = (str(MemoryStatus.ACTIVE), str(MemoryStatus.PENDING))
    conservative_status: MemoryStatus | str = MemoryStatus.PENDING
    unclassified_relation: str = RELATION_UNCERTAIN
    scan_batch_size: int = 200

    def __post_init__(self) -> None:
        if isinstance(self.related_limit, bool) or not isinstance(self.related_limit, int):
            raise ValidationError("related_limit must be an integer", field="related_limit")
        if not 1 <= self.related_limit <= 100:
            raise ValidationError(f"related_limit must be within [1, 100], got {self.related_limit}", field="related_limit")
        statuses = tuple(str(coerce_enum(status, MemoryStatus, "related_statuses")) for status in self.related_statuses)
        if not statuses:
            raise ValidationError("related_statuses must not be empty", field="related_statuses")
        conservative = str(coerce_enum(self.conservative_status, MemoryStatus, "conservative_status"))
        if conservative == str(MemoryStatus.ACTIVE):
            raise ValidationError(
                "conservative_status must not be 'active': a suspected conflict is never stored as a trusted fact",
                field="conservative_status",
            )
        # "unclassified" means "we could not get an answer", which must never be
        # treated as compatibility: only the conservative labels are allowed here
        if self.unclassified_relation not in PENDING_RELATIONS:
            raise ValidationError(
                f"unclassified_relation must be one of {list(PENDING_RELATIONS)} "
                f"(an unclassified relation is never 'compatible'), got {self.unclassified_relation!r}",
                field="unclassified_relation",
            )
        if isinstance(self.scan_batch_size, bool) or not isinstance(self.scan_batch_size, int) or self.scan_batch_size < 1:
            raise ValidationError(
                f"scan_batch_size must be an integer >= 1, got {self.scan_batch_size!r}", field="scan_batch_size"
            )
        object.__setattr__(self, "related_statuses", statuses)
        object.__setattr__(self, "conservative_status", conservative)

    @property
    def pending_status(self) -> str:
        return str(self.conservative_status)


# --------------------------------------------------------------------------
# classifier
# --------------------------------------------------------------------------

class ConflictClassifier(Protocol):
    """Anything that can label a candidate against related Memories."""

    def classify(self, candidate: Any, related: Sequence[Memory]) -> ConflictVerdict:  # pragma: no cover
        ...


def _candidate_payload(candidate: Any) -> dict[str, Any]:
    """The candidate as prompt data (works for ``Memory`` and ``MemoryDraft``)."""
    return {
        "type": str(getattr(candidate, "type", "")),
        "title": str(getattr(candidate, "title", "")),
        "content": str(getattr(candidate, "content", "")),
        "summary": getattr(candidate, "summary", None),
        "tags": list(getattr(candidate, "tags", ()) or ()),
    }


def _memory_payload(memory: Memory) -> dict[str, Any]:
    return {
        "type": str(memory.type),
        "title": memory.title,
        "content": memory.content,
        "summary": memory.summary,
        "tags": list(memory.tags),
    }


class LLMConflictClassifier:
    """Labels a candidate with the **existing** :mod:`personal_memory.llm` adapter.

    Only ``same | compatible | conflict | uncertain`` can come out of this class;
    it never touches the database.  Output is schema-validated
    (:meth:`parse_verdict`) and retried on unusable answers, exactly like the
    Phase 2 formation path.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        max_attempts: int = 2,
        prompt_version: str = CONFLICT_PROMPT_VERSION,
        system_prompt: str = CONFLICT_SYSTEM_PROMPT,
    ) -> None:
        if max_attempts < 1:
            raise ValidationError("max_attempts must be >= 1", field="max_attempts")
        self.llm = llm
        self.max_attempts = max_attempts
        self.prompt_version = prompt_version
        self.system_prompt = system_prompt
        self.last_verdict: ConflictVerdict | None = None

    def classify(self, candidate: Any, related: Sequence[Memory]) -> ConflictVerdict:
        if not related:
            raise QualityError("conflict classification needs at least one related Memory")
        request = LLMRequest.of(
            self.system_prompt,
            build_conflict_user_prompt(_candidate_payload(candidate), [_memory_payload(m) for m in related]),
            purpose="memory_quality",
            prompt_version=self.prompt_version,
        )
        errors: list[str] = []
        problems: list[tuple[str, str]] = []
        for attempt in range(1, self.max_attempts + 1):
            try:
                payload, response = self.llm.complete_json(request)
            except LLMResponseError as exc:
                # unusable answer (not JSON / empty / truncated): retry with feedback
                errors.append(f"attempt {attempt}: {exc}")
                problems.append((f"attempt {attempt}", str(exc)))
                request = request.with_user_suffix(
                    build_retry_suffix(str(exc), attempt=attempt, max_attempts=self.max_attempts)
                )
                continue
            try:
                verdict = self.parse_verdict(payload)
            except ConflictClassificationError as exc:
                errors.append(f"attempt {attempt}: {exc}")
                problems.extend((f"attempt {attempt} · {field}", msg) for field, msg in exc.problems)
                request = request.with_user_suffix(
                    build_retry_suffix("; ".join(f"{f}: {m}" for f, m in exc.problems) or str(exc),
                                       attempt=attempt, max_attempts=self.max_attempts)
                )
                continue
            verdict = ConflictVerdict(
                relation=verdict.relation,
                reason=verdict.reason,
                classifier="llm",
                llm={**response.as_dict(), "attempts": attempt, "prompt_version": self.prompt_version},
            )
            self.last_verdict = verdict
            return verdict
        detail = " | ".join(errors) or "no attempt was made"
        raise ConflictClassificationError(
            f"the model did not classify the candidate after {self.max_attempts} attempt(s): {detail}",
            problems=tuple(problems),
            attempts=self.max_attempts,
        )

    @staticmethod
    def parse_verdict(payload: Mapping[str, Any]) -> ConflictVerdict:
        """Strict validation of the model answer; raises without writing anything."""
        problems: list[tuple[str, str]] = []
        if not isinstance(payload, Mapping):
            raise ConflictClassificationError(
                f"classifier answer must be a JSON object, got {type(payload).__name__}",
                problems=(("<root>", "not a JSON object"),),
            )
        unknown = sorted(set(payload) - {"relation", "reason"})
        if unknown:
            problems.append(("<root>", f"unexpected field(s) {unknown}; allowed: ['reason', 'relation']"))
        relation = payload.get("relation")
        if isinstance(relation, str):
            relation = relation.strip().lower()
        if relation not in RELATIONS:
            problems.append(("relation", f"must be one of {list(RELATIONS)}, got {payload.get('relation')!r}"))
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.append(("reason", "must be a non-empty string"))
        if problems:
            detail = "; ".join(f"{field}: {message}" for field, message in problems)
            raise ConflictClassificationError(
                f"invalid conflict-classification answer -> {detail}", problems=tuple(problems)
            )
        return ConflictVerdict(relation=str(relation), reason=reason.strip(), classifier="llm")


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------

class MemoryQualityGate:
    """Duplicate detection + conservative conflict handling for new candidates.

    Reads through :class:`MemoryRepository` (or a unit of work) and Phase 3's
    :class:`~personal_memory.retrieval.MemoryRetriever`; writes nothing.
    """

    def __init__(
        self,
        repository: Any,
        *,
        policy: QualityPolicy | None = None,
        retriever: MemoryRetriever | None = None,
        classifier: ConflictClassifier | None = None,
    ) -> None:
        self.repository = repository
        self.policy = policy or QualityPolicy()
        self.retriever = retriever or MemoryRetriever(repository)
        self.classifier = classifier

    # -- duplicates ------------------------------------------------------
    def scan_duplicates(
        self,
        source: Any = None,
        *,
        memory_type: MemoryType | str | None = None,
    ) -> DuplicateScan:
        """Build the fingerprint index by streaming Memories (never a full-table load).

        ``source`` may be a repository **or** a unit of work; the latter makes the
        scan and the guarded INSERT share one transaction (and one write lock).
        """
        target = self.repository if source is None else source
        iterator = getattr(target, "iter_memories", None)
        if iterator is None:
            raise ValidationError(
                f"{type(target).__name__} cannot be scanned: expected iter_memories() "
                "(MemoryRepository or MemoryUnitOfWork)"
            )
        scan = DuplicateScan()
        for memory in iterator(memory_type=memory_type, batch_size=self.policy.scan_batch_size):
            scan.scanned += 1
            scan.remember(memory_fingerprint(memory.type, memory.title, memory.content), memory.id)
        return scan

    def check_duplicate(
        self,
        candidate: Any,
        *,
        scan: DuplicateScan | None = None,
        memory_type: MemoryType | str | None = None,
    ) -> DuplicateCheck:
        """Is this candidate an *exact* duplicate of a stored Memory?"""
        fingerprint = fingerprint_of(candidate)
        if not self.policy.dedupe:
            return DuplicateCheck(fingerprint, None, None, 0, "dedupe disabled by policy")
        scope = memory_type if memory_type is not None else getattr(candidate, "type", None)
        if scan is None:
            scan = self.scan_duplicates(memory_type=scope)
        duplicate_of = scan.find(fingerprint)
        if duplicate_of is None:
            return DuplicateCheck(fingerprint, None, None, scan.scanned, "no exact duplicate in the store")
        existing = self.repository.get_memory(duplicate_of)
        if existing is None:
            # conservative: the fingerprint says this content is already stored, so a
            # second row is still refused even if the row is currently unreadable
            return DuplicateCheck(
                fingerprint,
                duplicate_of,
                None,
                scan.scanned,
                f"fingerprint matches {duplicate_of!r}, which is not readable right now",
            )
        return DuplicateCheck(
            fingerprint,
            duplicate_of,
            existing,
            scan.scanned,
            f"exact duplicate of {duplicate_of!r} (status={existing.status})",
        )

    # -- conflicts -------------------------------------------------------
    def check_conflict(
        self,
        candidate: Any,
        *,
        exclude_ids: Sequence[str] = (),
    ) -> ConflictCheck:
        """Retrieve related Memories (Phase 3) and classify the relation.

        Returns ``relation="none"`` when nothing related is found.  When related
        Memories exist but no classifier is configured, the policy's
        ``unclassified_relation`` (``uncertain`` by default) is used -- never
        ``compatible``.
        """
        if not self.policy.conflict_check:
            return ConflictCheck(
                relation=RELATION_UNCHECKED,
                query="",
                related=(),
                verdict=None,
                reason="conflict check disabled by policy: the proposed status is kept",
            )
        query = self._query_for(candidate)
        if not query:
            return ConflictCheck(RELATION_NONE, query, (), None, "candidate has no searchable text")
        try:
            result = self.retriever.search(
                query, limit=self.policy.related_limit, status=self.policy.related_statuses
            )
        except ValidationError as exc:
            return ConflictCheck(RELATION_NONE, query, (), None, f"query is not searchable: {exc}")
        excluded = set(exclude_ids)
        own_id = getattr(candidate, "id", None)
        if isinstance(own_id, str) and own_id:
            excluded.add(own_id)  # a stored Memory is never its own conflict partner
        related = tuple(hit.memory for hit in result.hits if hit.memory.id not in excluded)
        if not related:
            return ConflictCheck(
                RELATION_NONE, query, (), None, "no related Memory found by keyword retrieval"
            )
        if self.classifier is None:
            relation = self.policy.unclassified_relation
            verdict = ConflictVerdict(
                relation=relation,
                reason="no conflict classifier configured; conservative default",
                classifier="none",
            )
            return ConflictCheck(
                relation, query, related, verdict,
                "related Memories found and no classifier is configured "
                f"-> conservative relation {relation!r}",
            )
        verdict = self.classifier.classify(candidate, related)
        if not isinstance(verdict, ConflictVerdict):
            raise QualityError(
                f"classifier must return a ConflictVerdict, got {type(verdict).__name__}"
            )
        if verdict.relation not in RELATIONS:
            raise QualityError(
                f"classifier returned an unknown relation {verdict.relation!r}; expected one of {list(RELATIONS)}"
            )
        return ConflictCheck(verdict.relation, query, related, verdict, verdict.reason)

    # -- decision --------------------------------------------------------
    def evaluate_candidate(
        self,
        candidate: Any,
        *,
        base_status: MemoryStatus | str = MemoryStatus.ACTIVE,
        scan: DuplicateScan | None = None,
        exclude_ids: Sequence[str] = (),
    ) -> QualityDecision:
        """Full policy decision for one candidate (no write is performed here)."""
        base = str(coerce_enum(base_status, MemoryStatus, "base_status"))
        duplicate = self.check_duplicate(candidate, scan=scan)
        title = str(getattr(candidate, "title", ""))
        if duplicate.duplicate_of is not None:
            return QualityDecision(
                action="reuse",
                fingerprint=duplicate.fingerprint,
                title=title,
                status=None,
                base_status=base,
                relation=RELATION_DUPLICATE,
                duplicate_of=duplicate.duplicate_of,
                related_ids=(duplicate.duplicate_of,),
                reasons=(duplicate.reason, "refusing to store a second identical Memory"),
                duplicate=duplicate,
            )
        # check_conflict() itself excludes the candidate's own id when it has one
        conflict = self.check_conflict(candidate, exclude_ids=exclude_ids)
        related_ids = conflict.related_ids
        if conflict.relation in PENDING_RELATIONS:
            status = self.policy.pending_status
            return QualityDecision(
                action="persist",
                fingerprint=duplicate.fingerprint,
                title=title,
                status=status,
                base_status=base,
                relation=conflict.relation,
                related_ids=related_ids,
                reasons=(
                    f"relation={conflict.relation} with {len(related_ids)} related memory/memories "
                    f"-> status={status} (conservative; the old Memory is kept unchanged)",
                    conflict.reason,
                ),
                duplicate=duplicate,
                conflict=conflict,
            )
        return QualityDecision(
            action="persist",
            fingerprint=duplicate.fingerprint,
            title=title,
            status=base,
            base_status=base,
            relation=conflict.relation,
            related_ids=related_ids,
            reasons=(conflict.reason,),
            duplicate=duplicate,
            conflict=conflict,
        )

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _query_for(candidate: Any) -> str:
        title = str(getattr(candidate, "title", "") or "").strip()
        content = str(getattr(candidate, "content", "") or "").strip()
        return _WHITESPACE_RE.sub(" ", f"{title} {content[:MAX_QUERY_CHARS]}").strip()
