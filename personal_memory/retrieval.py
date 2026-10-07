"""Phase 3: Memory Retrieval -- keyword search over saved Memories, plus their Sources.

What this module does::

    query -> normalise -> route tokens to the right index -> rank -> load Memory
          -> load related Sources -> RetrievalResult

What it deliberately does **not** do: no LLM, no network, no embeddings, no vector
store, no reranking, no answer generation.  It only *finds* memories and their
provenance; answering questions is a later layer's job.

Retrieval design (driven by measurements in ``scripts/phase3_fts5_lab.py``):

* **Latin/digit tokens** -> ``memory_fts_word`` (FTS5 ``unicode61``) with a prefix
  query (``"rag"*``).  Word/prefix matching keeps precision -- ``RAG`` does *not*
  match ``storage`` -- while still finding ``retriev`` -> ``retrieval``, and bm25
  ranks this index usefully.
* **Chinese/CJK tokens (>=3 chars)** -> ``memory_fts_trigram`` (FTS5 ``trigram``).
  ``unicode61`` finds nothing at all for ``长期记忆``; trigram finds and ranks it.
* **Short CJK tokens (1-2 chars)** -> parameterised ``LIKE`` fallback: no FTS5
  tokenizer can match a 2-character Chinese query (measured).  ``LIKE`` is used
  *only* here, so Latin queries never get noisy substring matches.

Scoring (documented, not normalised to 0..1):

* ``score_kind="bm25"``: ``score = -bm25(index)`` -- **higher is better**.  SQLite
  returns bm25 as a negative number where more negative = better, so the sign is
  flipped for a consistent "higher is better" direction.  ``bm25`` (raw) and
  ``index`` are kept on the hit for transparency.
* ``score_kind="like"``: weighted field-substring score (title 3, tags 2,
  summary 2, content 1) -- same direction.
* Ordering: all index matches rank **before** LIKE-only matches; then score
  descending; then a documented field priority (title > tags/summary > content);
  then ``created_at`` descending; then ``id`` ascending.  The field priority only
  breaks ties -- bm25 gives no signal when every candidate contains the term -- and
  the last two keys make ordering fully deterministic (never random).
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .errors import MemorySystemError, NotFoundError, ValidationError
from .models import Memory, MemoryStatus, MemoryType, Source
from .store import MemoryRepository

__all__ = [
    "MemoryRetriever",
    "RetrievalError",
    "RetrievalResult",
    "MemoryHit",
    "SourceRef",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "STATUS_ALL",
    "STATUS_CHOICES",
    "tokenize",
    "build_match_expression",
    "classify_token",
    "prefix_fts_token",
    "is_searchable_token",
    "contains_cjk",
]

DEFAULT_LIMIT = 5
MAX_LIMIT = 100
STATUS_ALL = "all"
STATUS_CHOICES: tuple[str, ...] = ("active", "pending", "archived", STATUS_ALL)
DEFAULT_STATUSES: tuple[str, ...] = (str(MemoryStatus.ACTIVE),)

#: Relevance ordering: index matches first, LIKE-only matches after.
_TIER_INDEX = 0
_TIER_LIKE = 1

#: Field weights for the LIKE fallback score (deterministic, documented).
_FIELD_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("title", 3),
    ("tags", 2),
    ("summary", 2),
    ("content", 1),
)

#: Cap on candidates collected before ranking, so a pathological query on a huge
#: database cannot exhaust memory.  Personal scale is far below this.
MAX_CANDIDATES = 1000

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_WHITESPACE_RE = re.compile(r"\s+")


class RetrievalError(MemorySystemError):
    """The search index could not be queried (corrupt/missing index, SQLite error)."""


# --------------------------------------------------------------------------
# query normalisation helpers (pure functions, easy to test)
# --------------------------------------------------------------------------

def contains_cjk(text: str) -> bool:
    """True when the text contains CJK/Kana/Hangul characters."""
    return bool(_CJK_RE.search(text))


def tokenize(query: str) -> list[str]:
    """Split a query on whitespace, dropping empties; order preserved."""
    return [token for token in _WHITESPACE_RE.split(query.strip()) if token]


def quote_fts_token(token: str) -> str:
    """Quote one token so FTS5 treats it as a literal phrase.

    Measured in the lab: unquoted input makes MATCH fail with a syntax error
    (unbalanced double quote, apostrophe, ``AND``, ``local-first`` ...), while a
    quoted token turns every hostile string into a harmless literal that simply
    matches nothing.
    """
    return '"' + token.replace('"', '""') + '"'


def build_match_expression(tokens: Sequence[str]) -> str:
    """Build a safe ``"tok" OR "tok2"`` expression (empty string when no tokens)."""
    return " OR ".join(quote_fts_token(token) for token in tokens if token.strip())


def is_searchable_token(token: str) -> bool:
    """True when a token contains something worth searching for.

    Punctuation-only input (``"``, ``'``, ``*``, ``:``, ``(`` ...) is not a
    search term: treating it as one would either raise an FTS syntax error or
    match JSON punctuation inside ``tags_json``.
    """
    return contains_cjk(token) or any(character.isalnum() for character in token)


def classify_token(token: str) -> str:
    """Route one token: ``"trigram"`` | ``"word"`` | ``"short"`` (LIKE fallback).

    ``short`` is reserved for CJK tokens of 1-2 characters -- the one case no FTS5
    tokenizer can serve.  Latin tokens always go to the word index (with a prefix
    match), which is what keeps English precision.
    """
    if contains_cjk(token):
        return "trigram" if len(token) >= 3 else "short"
    return "word"


def prefix_fts_token(token: str) -> str:
    """Word-index expression: quoted token followed by FTS5's prefix operator."""
    return quote_fts_token(token) + "*"


# --------------------------------------------------------------------------
# result types
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceRef:
    """Lightweight Source reference returned with a hit (no full content)."""

    id: str
    source_type: str
    title: str
    url: str | None
    created_at: str

    @classmethod
    def from_source(cls, source: Source) -> "SourceRef":
        return cls(
            id=source.id,
            source_type=str(source.source_type),
            title=source.title,
            url=source.url,
            created_at=source.created_at,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "title": self.title,
            "url": self.url,
            "created_at": self.created_at,
            "content_hint": "use MemoryRetriever.resolve_source(source_id) for the full text",
        }


@dataclass(frozen=True)
class MemoryHit:
    """One retrieved Memory with its score, matched fields and Sources."""

    memory: Memory
    score: float
    score_kind: str  # "bm25" | "like"
    matched_fields: tuple[str, ...]
    sources: tuple[SourceRef, ...] = ()
    index: str | None = None
    bm25: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory": self.memory.as_dict(),
            "score": self.score,
            "score_kind": self.score_kind,
            "index": self.index,
            "bm25": self.bm25,
            "matched_fields": list(self.matched_fields),
            "sources": [source.as_dict() for source in self.sources],
        }


@dataclass(frozen=True)
class _Candidate:
    """Internal ranked candidate (keeps the raw bm25 value for transparency)."""

    memory: Memory
    tier: int
    score: float
    score_kind: str
    matched_fields: tuple[str, ...]
    bm25: float | None = None
    index: str | None = None


@dataclass(frozen=True)
class RetrievalResult:
    query: str
    hits: tuple[MemoryHit, ...]
    total: int
    limit: int
    offset: int
    statuses: tuple[str, ...]
    memory_type: str | None
    mode: str  # "index" | "index+like" | "like"
    took_ms: float
    index_matches: int
    like_only_matches: int
    truncated: bool = False

    @property
    def score_direction(self) -> str:
        return "higher is more relevant (bm25 is negated; raw bm25 kept on each hit)"

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "statuses": list(self.statuses),
            "memory_type": self.memory_type,
            "mode": self.mode,
            "took_ms": self.took_ms,
            "index_matches": self.index_matches,
            "like_only_matches": self.like_only_matches,
            "truncated": self.truncated,
            "score_direction": self.score_direction,
            "hits": [hit.as_dict() for hit in self.hits],
        }


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------

class MemoryRetriever:
    """Keyword retrieval over Memories; reads through :class:`MemoryRepository`."""

    def __init__(self, repository: MemoryRepository, *, max_limit: int = MAX_LIMIT) -> None:
        self.repository = repository
        self.max_limit = max_limit

    # -- public API --------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        type: MemoryType | str | None = None,
        status: MemoryStatus | str | Sequence[MemoryStatus | str] = MemoryStatus.ACTIVE,
    ) -> RetrievalResult:
        """Find Memories relevant to ``query``.

        Defaults to ``status='active'``: ``pending`` (low-confidence inference)
        and ``archived`` memories are not returned unless asked for explicitly.
        Pass ``status='all'`` to search every status.
        """
        started = time.perf_counter()
        cleaned_query, tokens = self._validate_query(query)
        limit_value, offset_value = self._validate_paging(limit, offset)
        memory_type, statuses = self._validate_filters(type, status)

        index_hits, like_only = self._collect(tokens, statuses, memory_type)
        candidates = self._rank_candidates(index_hits, like_only, tokens)
        truncated = len(candidates) > MAX_CANDIDATES
        if truncated:
            candidates = candidates[:MAX_CANDIDATES]

        total = len(candidates)
        page = candidates[offset_value : offset_value + limit_value]
        hits = self._load_hits(page)
        took_ms = (time.perf_counter() - started) * 1000.0

        if index_hits and like_only:
            mode = "index+like"
        elif index_hits:
            mode = "index"
        elif like_only:
            mode = "like"
        else:
            mode = "none"

        return RetrievalResult(
            query=cleaned_query,
            hits=tuple(hits),
            total=total,
            limit=limit_value,
            offset=offset_value,
            statuses=statuses,
            memory_type=str(memory_type) if memory_type is not None else None,
            mode=mode,
            took_ms=round(took_ms, 3),
            index_matches=len(index_hits),
            like_only_matches=len(like_only),
            truncated=truncated,
        )

    def resolve_source(self, source_id: str) -> Source:
        """Explicit "further reading": fetch one Source **with** its full content.

        Search results intentionally carry only Source metadata, so a caller that
        really needs the original text asks for it one Source at a time.
        """
        return self.repository.require_source(source_id)

    def index_status(self) -> dict[str, int]:
        """Index vs. table row counts (used by tests and the CLI ``info`` command)."""
        return {
            "memories": self.repository.counts()["memories"],
            "word_index": self.repository.index_row_count("word"),
            "trigram_index": self.repository.index_row_count("trigram"),
        }

    # -- validation --------------------------------------------------------
    @staticmethod
    def _validate_query(query: str) -> tuple[str, list[str]]:
        if not isinstance(query, str):
            raise ValidationError(f"query must be a string, got {type(query).__name__}", field="query")
        cleaned = query.strip()
        if not cleaned:
            raise ValidationError(
                "query must not be empty or whitespace; refusing to scan the whole table", field="query"
            )
        tokens = tokenize(cleaned)
        if not tokens:
            raise ValidationError("query must contain at least one searchable token", field="query")
        if any(ord(character) < 32 and character not in "\t\n\r" for character in cleaned):
            raise ValidationError(
                "query must not contain control characters", field="query"
            )
        searchable = [token for token in tokens if is_searchable_token(token)]
        if not searchable:
            raise ValidationError(
                f"query {cleaned!r} contains no searchable characters (letters, digits or CJK)",
                field="query",
            )
        return cleaned, searchable

    def _validate_paging(self, limit: int, offset: int) -> tuple[int, int]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValidationError(f"limit must be an integer, got {limit!r}", field="limit")
        if not 1 <= limit <= self.max_limit:
            raise ValidationError(f"limit must be within [1, {self.max_limit}], got {limit}", field="limit")
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValidationError(f"offset must be an integer, got {offset!r}", field="offset")
        if offset < 0:
            raise ValidationError(f"offset must be >= 0, got {offset}", field="offset")
        return limit, offset

    @staticmethod
    def _validate_filters(
        memory_type: MemoryType | str | None,
        status: MemoryStatus | str | Sequence[MemoryStatus | str],
    ) -> tuple[MemoryType | None, tuple[str, ...]]:
        if memory_type is None:
            memory_type_value = None
        elif isinstance(memory_type, MemoryType):
            memory_type_value = memory_type
        elif isinstance(memory_type, str):
            try:
                memory_type_value = MemoryType(memory_type.strip().lower())
            except ValueError:
                raise ValidationError(
                    f"type must be one of {[m.value for m in MemoryType]}, got {memory_type!r}", field="type"
                ) from None
        else:
            raise ValidationError(f"type must be a string or None, got {type(memory_type).__name__}", field="type")

        raw_statuses: Sequence[MemoryStatus | str]
        if isinstance(status, (MemoryStatus, str)):
            raw_statuses = [status]
        elif isinstance(status, Sequence):
            raw_statuses = list(status)
        else:
            raise ValidationError(
                f"status must be a string or a sequence of strings, got {type(status).__name__}", field="status"
            )
        if not raw_statuses:
            raise ValidationError("status must not be an empty sequence", field="status")

        values: list[str] = []
        for item in raw_statuses:
            if isinstance(item, MemoryStatus):
                values.append(str(item))
                continue
            if isinstance(item, str):
                candidate = item.strip().lower()
                if candidate == STATUS_ALL:
                    return memory_type_value, ()
                if candidate in {member.value for member in MemoryStatus}:
                    values.append(candidate)
                    continue
            raise ValidationError(
                f"status must be one of {list(STATUS_CHOICES)}, got {item!r}", field="status"
            )
        return memory_type_value, tuple(dict.fromkeys(values))

    # -- collection / ranking ---------------------------------------------
    def _collect(
        self,
        tokens: Sequence[str],
        statuses: tuple[str, ...],
        memory_type: MemoryType | None,
    ) -> tuple[dict[str, Any], dict[str, float]]:
        """Run the routed searches per token; returns ``(index_hits, like_only_scores)``.

        Each token is routed independently, so a Chinese short token does not
        disable the word index for the English token next to it.
        """
        index_hits: dict[str, Any] = {}
        fallback_tokens: list[str] = []
        memory_type_value = str(memory_type) if memory_type is not None else None

        for token in tokens:
            route = classify_token(token)
            if route == "short":
                fallback_tokens.append(token)  # 1-2 character CJK: LIKE is the only option
                continue
            expression = prefix_fts_token(token) if route == "word" else quote_fts_token(token)
            try:
                rows = self.repository.search_index(
                    route,
                    expression,
                    statuses=statuses or None,
                    memory_type=memory_type_value,
                )
            except sqlite3.Error as exc:
                raise RetrievalError(f"search index {route!r} could not be queried: {exc}") from exc
            for hit in rows:
                existing = index_hits.get(hit.memory_id)
                if existing is None or hit.score > existing.score:
                    index_hits[hit.memory_id] = hit

        like_only: dict[str, float] = {}
        like_tokens_by_id: dict[str, set[str]] = {}
        for token in dict.fromkeys(fallback_tokens):
            try:
                ids = self.repository.search_like(
                    token, statuses=statuses or None, memory_type=memory_type_value
                )
            except sqlite3.Error as exc:
                raise RetrievalError(f"substring fallback failed for token {token!r}: {exc}") from exc
            for memory_id in ids:
                if memory_id in index_hits:
                    continue  # already ranked by an index; keep the stronger signal
                like_tokens_by_id.setdefault(memory_id, set()).add(token)

        if like_tokens_by_id:
            memories = self.repository.get_memories_by_ids(list(like_tokens_by_id))
            for memory_id, tokens_for_id in like_tokens_by_id.items():
                memory = memories.get(memory_id)
                if memory is None:  # pragma: no cover - row deleted mid-search
                    continue
                fields = self._matched_fields(memory, tokens_for_id)
                like_only[memory_id] = float(
                    sum(weight for name, weight in _FIELD_WEIGHTS if name in fields)
                )
        return index_hits, like_only

    def _rank_candidates(
        self,
        index_hits: Mapping[str, Any],
        like_only: Mapping[str, float],
        tokens: Sequence[str],
    ) -> list[_Candidate]:
        """Rank all candidates deterministically.

        Four stable passes, lowest priority first: ``id ASC`` -> ``created_at DESC``
        -> ``field priority DESC`` -> ``(tier ASC, score DESC)``.  Nothing is random;
        repeating a query returns the same order (covered by tests).
        """
        if not index_hits and not like_only:
            return []
        raw: list[tuple[str, int, float, str, float | None, str | None]] = [
            (memory_id, _TIER_INDEX, float(hit.score), "bm25", float(hit.bm25), hit.index)
            for memory_id, hit in index_hits.items()
        ]
        raw.extend(
            (memory_id, _TIER_LIKE, float(like_only[memory_id]), "like", None, None)
            for memory_id in like_only
            if memory_id not in index_hits
        )
        memories = self.repository.get_memories_by_ids([memory_id for memory_id, *_ in raw])

        enriched: list[_Candidate] = []
        for memory_id, tier, score, kind, bm25, index in raw:
            memory = memories.get(memory_id)
            if memory is None:  # pragma: no cover - row deleted mid-search
                continue
            enriched.append(
                _Candidate(
                    memory=memory,
                    tier=tier,
                    score=score,
                    score_kind=kind,
                    matched_fields=self._matched_fields(memory, tokens),
                    bm25=bm25,
                    index=index,
                )
            )

        def field_priority(fields: Sequence[str]) -> int:
            return max((weight for name, weight in _FIELD_WEIGHTS if name in fields), default=0)

        enriched.sort(key=lambda item: item.memory.id)  # id ASC
        enriched.sort(key=lambda item: item.memory.created_at, reverse=True)  # created_at DESC
        enriched.sort(key=lambda item: field_priority(item.matched_fields), reverse=True)
        enriched.sort(key=lambda item: (item.tier, -item.score))  # tier ASC, score DESC
        return enriched

    @staticmethod
    def _matched_fields(memory: Memory, tokens: Iterable[str]) -> tuple[str, ...]:
        """Which fields contain any of the given tokens (case-insensitive substring)."""
        haystacks = {
            "title": memory.title.casefold(),
            "content": memory.content.casefold(),
            "summary": (memory.summary or "").casefold(),
            "tags": " ".join(memory.tags).casefold(),
        }
        needles = [token.casefold() for token in tokens if token.strip()]
        return tuple(
            name for name, haystack in haystacks.items() if any(needle in haystack for needle in needles)
        )

    # -- loading -----------------------------------------------------------
    def _load_hits(self, page: Sequence[_Candidate]) -> list[MemoryHit]:
        """Attach the Source relations to the already-ranked page."""
        if not page:
            return []
        sources_by_memory = self.repository.get_sources_for_memories(
            [candidate.memory.id for candidate in page]
        )
        return [
            MemoryHit(
                memory=candidate.memory,
                score=candidate.score,
                score_kind=candidate.score_kind,
                matched_fields=candidate.matched_fields,
                index=candidate.index,
                bm25=candidate.bm25,
                sources=tuple(
                    SourceRef.from_source(source)
                    for source in sources_by_memory.get(candidate.memory.id, [])
                ),
            )
            for candidate in page
        ]
