"""Command line interface: ``python -m personal_memory <command>``.

* ``init``    -- create the SQLite file and apply pending migrations
* ``info``    -- show schema version, DDL and row counts
* ``demo``    -- run the minimal Phase 1 demo and print the observed result
* ``form``    -- Phase 2: form Memories from raw input via the configured LLM
* ``capture`` -- KB 1.0 Phase 1: capture raw text (RawInput -> Memory Formation)
* ``import-file`` -- KB 1.0 Phase 2: .txt/.md file -> Importer -> Capture -> Memory Formation
* ``import-chat`` -- KB 1.0 Phase 3: chat file (role text / JSON) -> ChatImporter -> Capture -> Formation
* ``web`` -- KB 1.0 MVP: local Web UI (server-rendered HTML, 127.0.0.1 only)
* ``import-url`` -- KB 1.0 Phase 4: public web page -> WebImporter -> Capture -> Formation
* ``import-pdf`` -- KB 1.0 Phase 5: local text PDF -> PdfImporter (page/byte/text bounded) -> Capture
* ``search``  -- Phase 3: keyword retrieval over Memories (no LLM, no network)
* ``source``  -- print one Source with its full content (explicit further reading)
* ``archive`` -- Phase 4: active|pending -> archived (no longer recalled by default)
* ``activate``-- Phase 4: pending|archived -> active
* ``restore`` -- Phase 4: archived -> active
* ``update``  -- Phase 4: validated field update of one Memory
* ``delete``  -- Phase 4: delete one Memory (its Sources are kept)
* ``pending`` -- Phase 4: list the pending review queue
* ``version`` -- print the package, schema and prompt versions
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .db import SUPPORTED_SCHEMA_VERSION, Database, resolve_db_path
from .demo import run_demo
from .errors import MemorySystemError
from .extraction import FormationPolicy, MemoryFormationService, RawInput
from .models import InformationOrigin, MemoryStatus, MemoryType, SourceType
from .llm import LLMClient, LLMConfigError, load_config
from .models import CURRENT_SCHEMA_VERSION
from .prompts import CONFLICT_PROMPT_VERSION, PROMPT_VERSION
from .capture import CaptureService
from .importers import (
    CHAT_FORMATS,
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
    MAX_PDF_TEXT_CHARS,
    MIN_PDF_CHARS,
    PdfImporter,
    MAX_CHAT_BYTES,
    MAX_FILE_BYTES,
    MAX_WEB_BYTES,
    ChatImporter,
    FileImporter,
    WebImporter,
)
from .importers.web import DEFAULT_TIMEOUT_SECONDS, MIN_CONTENT_CHARS
from .lifecycle import MemoryLifecycle
from .quality import LLMConflictClassifier, MemoryQualityGate
from .retrieval import STATUS_CHOICES, MemoryRetriever
from .store import MemoryRepository
from .web import DEFAULT_HOST, DEFAULT_PORT, run_web

__all__ = ["main", "build_parser"]


def _dump(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m personal_memory",
        description="Personal Memory System v0.1 -- Phase 1 (models + SQLite foundation)",
    )
    parser.add_argument("--db", default=None, help="SQLite file path (default: data/memory.db or $PERSONAL_MEMORY_DB)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_db_option(subparser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        """Allow ``--db`` after the sub-command too (``SUPPRESS`` avoids clobbering the global value)."""
        subparser.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        return subparser

    add_db_option(subparsers.add_parser("init", help="create the database file and apply pending migrations"))
    migrate = add_db_option(subparsers.add_parser("migrate", help="alias of init (apply pending migrations)"))
    migrate.set_defaults(command="init")

    add_db_option(subparsers.add_parser("info", help="show schema version, table DDL and row counts"))

    demo = add_db_option(subparsers.add_parser("demo", help="run the minimal demo against the database"))
    demo.add_argument("--reset", action="store_true", help="delete the demo database file before running")

    form = add_db_option(
        subparsers.add_parser("form", help="form Memories from raw input using the configured LLM")
    )
    text_group = form.add_mutually_exclusive_group(required=True)
    text_group.add_argument("--text", help="raw input text")
    text_group.add_argument("--stdin", action="store_true", help="read raw input text from stdin")
    form.add_argument("--title", default=None, help="optional title of the raw input")
    form.add_argument("--url", default=None, help="optional source URL of the raw input")
    form.add_argument("--dry-run", action="store_true", help="analyse only; write nothing")
    form.add_argument("--config", default=None, help="path to the LLM config JSON file")
    form.add_argument("--attempts", type=int, default=2, help="max LLM output-validation attempts")
    form.add_argument(
        "--agent-inference-min-confidence",
        type=float,
        default=0.6,
        help="below this confidence an agent_inference draft is not stored",
    )
    form.add_argument(
        "--keep-source",
        choices=("when_required", "always", "never"),
        default="when_required",
        help="when the raw input must be persisted as a Source",
    )
    form.add_argument(
        "--no-quality-check",
        action="store_true",
        help="Phase 4: disable exact-duplicate detection and conflict classification",
    )

    capture = add_db_option(
        subparsers.add_parser("capture", help="KB 1.0 Phase 1: capture raw text into Memory Formation")
    )
    capture.add_argument("content", nargs="?", default=None, help="raw text to capture (or use --stdin)")
    capture.add_argument("--stdin", action="store_true", help="read the raw text from stdin")
    capture.add_argument("--title", default=None, help="optional title of the captured input")
    capture.add_argument(
        "--source-type",
        dest="source_type",
        default="text",
        choices=[member.value for member in SourceType],
        help="source kind (only 'text' is produced in this phase; the rest are reserved)",
    )
    capture.add_argument("--url", default=None, help="optional http(s) URL carried as data (never fetched)")
    capture.add_argument(
        "--captured-from",
        dest="captured_from",
        default="cli",
        help="capture provenance recorded in metadata (default: cli)",
    )
    capture.add_argument("--dry-run", action="store_true", help="analyse only; write nothing")
    capture.add_argument(
        "--no-quality-check",
        action="store_true",
        help="Phase 4: disable exact-duplicate detection and conflict classification",
    )
    capture.add_argument("--config", default=None, help="path to the LLM config JSON file")
    capture.add_argument("--json", action="store_true", help="print the full CaptureResult JSON")

    import_file_cmd = add_db_option(
        subparsers.add_parser(
            "import-file", help="KB 1.0 Phase 2: import a .txt/.md file through Capture"
        )
    )
    import_file_cmd.add_argument("path", help="path to a .txt / .md file")
    import_file_cmd.add_argument(
        "--title", default=None, help="override the title (default: Markdown H1 heading or file name)"
    )
    import_file_cmd.add_argument(
        "--source-type",
        dest="source_type",
        default=SourceType.FILE.value,
        choices=[member.value for member in SourceType],
        help="Source kind (default: file)",
    )
    import_file_cmd.add_argument(
        "--max-bytes",
        dest="max_bytes",
        type=int,
        default=MAX_FILE_BYTES,
        help=f"refuse files larger than this many bytes (default: {MAX_FILE_BYTES})",
    )
    import_file_cmd.add_argument("--dry-run", action="store_true", help="analyse only; write nothing")
    import_file_cmd.add_argument(
        "--no-quality-check",
        action="store_true",
        help="Phase 4: disable exact-duplicate detection and conflict classification",
    )
    import_file_cmd.add_argument("--config", default=None, help="path to the LLM config JSON file")
    import_file_cmd.add_argument("--json", action="store_true", help="print the full ImportResult JSON")

    import_chat = add_db_option(
        subparsers.add_parser(
            "import-chat", help="KB 1.0 Phase 3: import a chat file (role text or JSON) through Capture"
        )
    )
    import_chat.add_argument("path", help="path to a chat file (.json / .txt / .md / .chat)")
    import_chat.add_argument(
        "--title", default=None, help="override the conversation title (default: file title / first user message)"
    )
    import_chat.add_argument(
        "--provider", default=None, help="record the provider explicitly (default: as written in the file, else null)"
    )
    import_chat.add_argument("--conversation-id", dest="conversation_id", default=None, help="override the conversation id")
    import_chat.add_argument(
        "--format",
        dest="chat_format",
        default="auto",
        choices=list(CHAT_FORMATS),
        help="input format: auto (by extension/content) | roles | json",
    )
    import_chat.add_argument(
        "--max-bytes",
        dest="max_bytes",
        type=int,
        default=MAX_CHAT_BYTES,
        help=f"refuse conversations larger than this many bytes (default: {MAX_CHAT_BYTES}); never truncates",
    )
    import_chat.add_argument("--dry-run", action="store_true", help="analyse only; write nothing")
    import_chat.add_argument(
        "--keep-source",
        choices=("when_required", "always", "never"),
        default="when_required",
        help="when the raw chat must be kept as a Source (same policy knob as `form`)",
    )
    import_chat.add_argument(
        "--no-quality-check",
        action="store_true",
        help="Phase 4: disable exact-duplicate detection and conflict classification",
    )
    import_chat.add_argument("--config", default=None, help="path to the LLM config JSON file")
    import_chat.add_argument(
        "--json",
        action="store_true",
        help="print the ImportResult JSON (redacted: no conversation body)",
    )

    web = add_db_option(
        subparsers.add_parser(
            "web", help="KB 1.0 MVP: 本地 Web UI（默认只监听 127.0.0.1，不暴露到局域网）"
        )
    )
    web.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"bind address (default: {DEFAULT_HOST}); 不要改成 0.0.0.0，否则知识库会暴露到局域网",
    )
    web.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default: {DEFAULT_PORT}; 0 = 随机)")
    web.add_argument("--config", default=None, help="path to the LLM config JSON file")
    web.add_argument(
        "--no-quality-check",
        action="store_true",
        help="Phase 4: disable exact-duplicate detection and conflict classification",
    )
    web.add_argument(
        "--max-bytes",
        dest="max_bytes",
        type=int,
        default=MAX_FILE_BYTES,
        help=f"file/import size limit passed to the importers (default: {MAX_FILE_BYTES})",
    )
    import_url = add_db_option(
        subparsers.add_parser("import-url", help="KB 1.0 Phase 4: import a public web page through Capture")
    )
    import_url.add_argument("url", help="absolute http(s) URL of a public static page")
    import_url.add_argument("--title", default=None, help="override the extracted page title")
    import_url.add_argument(
        "--max-bytes",
        dest="max_bytes",
        type=int,
        default=MAX_WEB_BYTES,
        help=f"refuse responses larger than this many bytes (default: {MAX_WEB_BYTES})",
    )
    import_url.add_argument(
        "--timeout", dest="timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="connect/read timeout in seconds"
    )
    import_url.add_argument(
        "--min-chars",
        dest="min_chars",
        type=int,
        default=MIN_CONTENT_CHARS,
        help=f"minimum extracted article length (default: {MIN_CONTENT_CHARS})",
    )
    import_url.add_argument("--dry-run", action="store_true", help="analyse only; write nothing")
    import_url.add_argument(
        "--no-quality-check",
        action="store_true",
        help="Phase 4: disable exact-duplicate detection and conflict classification",
    )
    import_url.add_argument("--config", default=None, help="path to the LLM config JSON file")
    import_url.add_argument(
        "--json", action="store_true", help="print the ImportResult JSON (redacted: no page body)"
    )

    import_pdf = add_db_option(
        subparsers.add_parser("import-pdf", help="KB 1.0 Phase 5: import a local text PDF through Capture")
    )
    import_pdf.add_argument("path", help="path to a local .pdf file (text-based PDFs; no OCR)")
    import_pdf.add_argument("--title", default=None, help="override the PDF title (default: PDF metadata / first page line)")
    import_pdf.add_argument(
        "--max-bytes",
        dest="max_bytes",
        type=int,
        default=MAX_PDF_BYTES,
        help=f"refuse files larger than this many bytes (default: {MAX_PDF_BYTES}); checked before reading",
    )
    import_pdf.add_argument(
        "--max-pages",
        dest="max_pages",
        type=int,
        default=MAX_PDF_PAGES,
        help=f"refuse documents with more pages (default: {MAX_PDF_PAGES}); checked while iterating, never after",
    )
    import_pdf.add_argument(
        "--max-text-chars",
        dest="max_text_chars",
        type=int,
        default=MAX_PDF_TEXT_CHARS,
        help=f"refuse extracted text larger than this (default: {MAX_PDF_TEXT_CHARS} chars), checked per page",
    )
    import_pdf.add_argument(
        "--min-chars",
        dest="min_chars",
        type=int,
        default=MIN_PDF_CHARS,
        help=f"minimum extractable text (default: {MIN_PDF_CHARS}); below it the PDF is reported as a scan candidate",
    )
    import_pdf.add_argument("--dry-run", action="store_true", help="analyse only; write nothing")
    import_pdf.add_argument(
        "--no-quality-check",
        action="store_true",
        help="Phase 4: disable exact-duplicate detection and conflict classification",
    )
    import_pdf.add_argument("--config", default=None, help="path to the LLM config JSON file")
    import_pdf.add_argument(
        "--json", action="store_true", help="print the ImportResult JSON (redacted: no PDF text)"
    )

    search = add_db_option(
        subparsers.add_parser("search", help="Phase 3: keyword search over Memories (no LLM, no network)")
    )
    search.add_argument("query", help="keyword or Chinese query text")
    search.add_argument("--limit", type=int, default=5, help="max results (1-100)")
    search.add_argument("--offset", type=int, default=0, help="pagination offset")
    search.add_argument(
        "--type",
        dest="memory_type",
        default=None,
        choices=[member.value for member in MemoryType],
        help="filter by Memory type",
    )
    search.add_argument(
        "--status",
        default="active",
        choices=list(STATUS_CHOICES),
        help="active (default) | pending | archived | all",
    )
    search.add_argument("--json", action="store_true", help="print the full RetrievalResult JSON")

    source = add_db_option(subparsers.add_parser("source", help="print one Source (full content) by id"))
    source.add_argument("source_id", help="Source id returned in search results")

    archive = add_db_option(subparsers.add_parser("archive", help="Phase 4: active|pending -> archived"))
    archive.add_argument("memory_id", help="Memory id")
    archive.add_argument("--json", action="store_true", help="print the full report as JSON")

    activate = add_db_option(subparsers.add_parser("activate", help="Phase 4: pending|archived -> active"))
    activate.add_argument("memory_id", help="Memory id")
    activate.add_argument("--json", action="store_true", help="print the full report as JSON")

    restore = add_db_option(subparsers.add_parser("restore", help="Phase 4: archived -> active"))
    restore.add_argument("memory_id", help="Memory id")
    restore.add_argument("--json", action="store_true", help="print the full report as JSON")

    delete = add_db_option(
        subparsers.add_parser("delete", help="Phase 4: delete one Memory; its Sources are kept")
    )
    delete.add_argument("memory_id", help="Memory id")
    delete.add_argument("--json", action="store_true", help="print the full report as JSON")

    update = add_db_option(subparsers.add_parser("update", help="Phase 4: validated field update"))
    update.add_argument("memory_id", help="Memory id")
    update.add_argument("--title", default=None, help="new title")
    update.add_argument("--content", default=None, help="new content")
    update.add_argument("--summary", default=None, help="new summary (empty string clears it)")
    update.add_argument("--tags", default=None, help="comma-separated tags (empty string clears them)")
    update.add_argument("--importance", type=float, default=None, help="new importance in [0, 1]")
    update.add_argument("--confidence", type=float, default=None, help="new confidence in [0, 1]")
    update.add_argument(
        "--type", dest="memory_type", default=None, choices=[member.value for member in MemoryType]
    )
    update.add_argument(
        "--information-origin",
        dest="information_origin",
        default=None,
        choices=[member.value for member in InformationOrigin],
    )
    update.add_argument(
        "--status", default=None, choices=[member.value for member in MemoryStatus], help="status (transition rules apply)"
    )
    update.add_argument("--json", action="store_true", help="print the full result as JSON")

    pending = add_db_option(subparsers.add_parser("pending", help="Phase 4: list the pending review queue"))
    pending.add_argument("--limit", type=int, default=20, help="max rows (1-100)")
    pending.add_argument("--json", action="store_true", help="print the full list as JSON")

    add_db_option(subparsers.add_parser("version", help="print the package, schema and prompt version"))
    return parser


def cmd_init(db_path: Path) -> int:
    database = Database(db_path)
    report = database.initialize()
    payload = report.as_dict()
    payload["applied_count"] = report.applied_count
    _dump(payload)
    return 0


def cmd_info(db_path: Path) -> int:
    database = Database(db_path)
    describe = database.describe()
    describe["package_version"] = __version__
    describe["applied_migrations"] = [
        {"version": v, "name": n, "applied_at": t} for v, n, t in database.applied_migrations()
    ]
    _dump(describe)
    return 0


def cmd_demo(db_path: Path, reset: bool) -> int:
    _dump(run_demo(db_path, reset=reset))
    return 0


def cmd_search(db_path: Path, args: argparse.Namespace) -> int:
    """Phase 3: query -> SQLite index -> Memories + Sources.  Never calls a model."""
    try:
        repository = MemoryRepository(Database(db_path))
        result = MemoryRetriever(repository).search(
            args.query,
            limit=args.limit,
            offset=args.offset,
            type=args.memory_type,
            status=args.status,
        )
    except MemorySystemError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__, "db_path": str(db_path)})
        return 3
    if args.json:
        payload = result.as_dict()
        payload["db_path"] = str(db_path)
        _dump(payload)
        return 0
    _print_search(result, db_path)
    return 0


def _print_search(result, db_path: Path) -> None:
    """Human-readable retrieval output (Memory, Title, Type, Score, Summary, Sources)."""
    print(f"db      : {db_path}")
    print(f"query   : {result.query!r}  status={list(result.statuses) or 'all'}  type={result.memory_type}")
    print(f"matches : total={result.total}  mode={result.mode}  "
          f"index={result.index_matches} like_only={result.like_only_matches}  took={result.took_ms}ms")
    print(f"score   : {result.score_direction}")
    if not result.hits:
        print("(no memory matched)")
        return
    for position, hit in enumerate(result.hits, start=result.offset + 1):
        memory = hit.memory
        print("")
        print(f"{position}. {memory.title}   [{memory.type}]   score={hit.score:.6g} ({hit.score_kind})")
        print(f"   memory_id : {memory.id}")
        print(f"   status    : {memory.status}   importance={memory.importance} confidence={memory.confidence}")
        print(f"   origin    : {memory.information_origin}   schema_version={memory.schema_version}")
        if memory.summary:
            print(f"   summary   : {memory.summary}")
        print(f"   content   : {memory.content[:160]}")
        print(f"   tags      : {list(memory.tags)}")
        print(f"   matched   : {list(hit.matched_fields)}   created={memory.created_at}")
        if hit.sources:
            print(f"   sources ({len(hit.sources)}):")
            for source in hit.sources:
                print(f"     - {source.id}  [{source.source_type}]  {source.title}  url={source.url}")
        else:
            print("   sources   : (none)")
    print("")
    print("full source text: python -m personal_memory source <source_id>")


def cmd_source(db_path: Path, source_id: str) -> int:
    """Explicit further reading: one Source in full (content included)."""
    try:
        source = MemoryRepository(Database(db_path)).require_source(source_id)
    except MemorySystemError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__, "db_path": str(db_path)})
        return 3
    _dump(source.as_dict())
    return 0


def _print_transition(report, db_path: Path) -> None:
    """Human-readable archive/activate/restore output."""
    print(f"db      : {db_path}")
    print(f"memory  : {report.memory_id}")
    print(f"change  : {report.from_status} -> {report.to_status}   changed={report.changed}")
    print(f"reason  : {report.reason}")
    print(f"title   : {report.memory.title}")
    print(f"status  : {report.memory.status}   updated_at={report.memory.updated_at}")


def cmd_transition(db_path: Path, action: str, memory_id: str, as_json: bool) -> int:
    """Phase 4: archive / activate / restore one Memory (transition table enforced)."""
    try:
        lifecycle = MemoryLifecycle(MemoryRepository(Database(db_path)))
        report = getattr(lifecycle, f"{action}_memory")(memory_id)
    except MemorySystemError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__, "db_path": str(db_path)})
        return 3
    if as_json:
        payload = report.as_dict()
        payload["db_path"] = str(db_path)
        _dump(payload)
        return 0
    _print_transition(report, db_path)
    return 0


def cmd_delete(db_path: Path, memory_id: str, as_json: bool) -> int:
    """Phase 4: delete one Memory; report what happened to its links and Sources."""
    try:
        report = MemoryLifecycle(MemoryRepository(Database(db_path))).delete_memory(memory_id)
    except MemorySystemError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__, "db_path": str(db_path)})
        return 3
    payload = report.as_dict()
    payload["db_path"] = str(db_path)
    if as_json:
        _dump(payload)
        return 0
    print(f"db      : {db_path}")
    print(f"memory  : {report.memory_id}   deleted={report.deleted}")
    print(f"links   : removed={report.links_removed}  remaining={report.links_remaining}")
    print(f"sources : kept={list(report.sources_kept)}  deleted={list(report.sources_deleted)}")
    return 0


def cmd_update(db_path: Path, args: argparse.Namespace) -> int:
    """Phase 4: validated update; a --status change still obeys the transition table."""
    changes: dict[str, Any] = {}
    if args.title is not None:
        changes["title"] = args.title
    if args.content is not None:
        changes["content"] = args.content
    if args.summary is not None:
        changes["summary"] = args.summary or None
    if args.tags is not None:
        changes["tags"] = [tag.strip() for tag in args.tags.split(",") if tag.strip()]
    if args.importance is not None:
        changes["importance"] = args.importance
    if args.confidence is not None:
        changes["confidence"] = args.confidence
    if args.memory_type is not None:
        changes["type"] = args.memory_type
    if args.information_origin is not None:
        changes["information_origin"] = args.information_origin
    if args.status is not None:
        changes["status"] = args.status
    if not changes:
        _dump(
            {
                "error": "update needs at least one field: --title/--content/--summary/--tags/"
                         "--importance/--confidence/--type/--information-origin/--status",
                "error_type": "ValidationError",
                "db_path": str(db_path),
            }
        )
        return 3
    try:
        memory = MemoryLifecycle(MemoryRepository(Database(db_path))).update_memory(
            args.memory_id, **changes
        )
    except MemorySystemError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__, "db_path": str(db_path)})
        return 3
    payload = {
        "db_path": str(db_path),
        "memory_id": memory.id,
        "changed_fields": sorted(changes),
        "memory": memory.as_dict(),
    }
    if args.json:
        _dump(payload)
        return 0
    print(f"db      : {db_path}")
    print(f"memory  : {memory.id}")
    print(f"changed : {sorted(changes)}")
    print(f"title   : {memory.title}   [{memory.type}]   status={memory.status}")
    print(f"content : {memory.content}")
    print(f"tags    : {list(memory.tags)}   importance={memory.importance} confidence={memory.confidence}")
    return 0


def cmd_pending(db_path: Path, limit: int, as_json: bool) -> int:
    """Phase 4: the review queue -- Memories that were never trusted as facts."""
    try:
        lifecycle = MemoryLifecycle(MemoryRepository(Database(db_path)))
        memories = lifecycle.pending(limit=limit)
        counts = lifecycle.status_counts()
    except MemorySystemError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__, "db_path": str(db_path)})
        return 3
    payload = {
        "db_path": str(db_path),
        "status_counts": counts,
        "pending_count": len(memories),
        "memories": [memory.as_dict() for memory in memories],
    }
    if as_json:
        _dump(payload)
        return 0
    print(f"db      : {db_path}")
    print(f"counts  : {counts}")
    print(f"pending : {len(memories)} (limit={limit})")
    if not memories:
        print("(no pending Memory)")
        return 0
    for position, memory in enumerate(memories, start=1):
        print("")
        print(f"{position}. {memory.title}   [{memory.type}]   confidence={memory.confidence}")
        print(f"   memory_id : {memory.id}")
        print(f"   content   : {memory.content[:160]}")
    return 0


def cmd_version() -> int:
    _dump(
        {
            "package_version": __version__,
            # database schema (migrations) vs. the schema_version stored on each Memory row
            "schema_version": SUPPORTED_SCHEMA_VERSION,
            "memory_schema_version": CURRENT_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "conflict_prompt_version": CONFLICT_PROMPT_VERSION,
            "memory_system": "phase-4 (memory lifecycle & quality)",
            "phase": "kb-1.0 phase-4 (url importer)",
        }
    )
    return 0


def cmd_form(db_path: Path, args: argparse.Namespace) -> int:
    """Phase 2: raw input -> configured LLM -> validated Memory -> SQLite."""
    try:
        config = load_config(args.config)
    except LLMConfigError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__})
        return 2
    text = sys.stdin.read() if args.stdin else (args.text or "")
    try:
        raw_input = RawInput(content=text, title=args.title, url=args.url)
        policy = FormationPolicy(
            agent_inference_min_confidence=args.agent_inference_min_confidence,
            keep_source=args.keep_source,
        )
        repository = MemoryRepository(Database(db_path))
        client = LLMClient(config)
        quality = None
        if not args.no_quality_check:
            # Phase 4: duplicate detection is deterministic (no model call); the
            # conflict label comes from the SAME llm.py adapter, and only when
            # Phase 3 retrieval actually found related Memories.
            quality = MemoryQualityGate(repository, classifier=LLMConflictClassifier(client))
        service = MemoryFormationService(
            repository, client, policy=policy, max_attempts=args.attempts, quality=quality
        )
        outcome = service.process(raw_input, dry_run=args.dry_run)
    except MemorySystemError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__, "db_path": str(db_path)})
        return 3
    payload = outcome.as_dict()
    payload["db_path"] = str(db_path)
    payload["llm_config"] = config.as_public_dict()
    payload["counts"] = repository.counts()
    _dump(payload)
    return 0


def _print_capture(result, db_path: Path) -> None:
    """KB 1.0 Phase 1 output: capture status -> formation status -> what was written."""
    formation = result.formation_result
    print(f"db        : {db_path}")
    print(f"captured  : {result.captured}   captured_from={result.captured_from}")
    print(f"capture   : status={result.status}  chars={len(result.request.content)}  "
          f"source_type={result.request.source_type}  captured_at={result.request.captured_at}")
    print(f"formation : status={formation.status}  worth_remembering={formation.worth_remembering}  "
          f"attempts={formation.attempts}  model={formation.model}")
    _print_formation_tail(result)


def cmd_capture(db_path: Path, args: argparse.Namespace) -> int:
    """KB 1.0 Phase 1: Capture -> RawInput -> Memory Formation.

    Capture itself never writes SQLite: the only writer is the already-frozen Phase 2
    formation pipeline reached through :class:`~personal_memory.capture.CaptureService`.
    """
    try:
        config = load_config(args.config)
    except LLMConfigError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__})
        return 2
    text = sys.stdin.read() if args.stdin else (args.content or "")
    try:
        repository = MemoryRepository(Database(db_path))
        client = LLMClient(config)
        quality = None
        if not args.no_quality_check:
            # exactly the pipeline `form` uses: the frozen Phase 4 gate, not a copy of it
            quality = MemoryQualityGate(repository, classifier=LLMConflictClassifier(client))
        service = CaptureService(
            MemoryFormationService(repository, client, quality=quality),
            captured_from=args.captured_from,
        )
        result = service.capture(
            text,
            title=args.title,
            source_type=args.source_type,
            url=args.url,
            dry_run=args.dry_run,
        )
    except MemorySystemError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__, "db_path": str(db_path)})
        return 3
    if args.json:
        payload = result.as_dict()
        payload["db_path"] = str(db_path)
        payload["llm_config"] = config.as_public_dict()
        payload["counts"] = repository.counts()
        _dump(payload)
        return 0
    _print_capture(result, db_path)
    return 0


def _print_formation_tail(result) -> None:
    """Shared tail for capture/import output: reason, created Memories, created Sources."""
    formation = result.formation_result
    print(f"reason    : {formation.reason}")
    print(f"memories  : {result.memory_count}")
    for memory in result.memories_created:
        print(f"   - {memory.id}  [{memory.type}]  {memory.title}  status={memory.status}  "
              f"importance={memory.importance} confidence={memory.confidence}")
    print(f"sources   : {result.source_count}   reused={result.source_reused}")
    for source in result.sources_created:
        print(f"   - {source.id}  [{source.source_type}]  {source.title}  url={source.url}")
    for reason in formation.dropped:
        print(f"dropped   : {reason}")


def _print_import(result, db_path: Path, path: Path) -> None:
    """KB 1.0 Phase 2 output: file -> import status -> formation status -> what was written."""
    document = result.document
    formation = result.capture_result.formation_result
    print(f"db        : {db_path}")
    print(f"file      : {path}")
    print(f"format    : {document.filename}  ({document.extension}, {document.size_bytes} bytes, "
          f"{document.metadata.get('encoding')}, "
          f"sha256={str(document.metadata.get('file_sha256'))[:12]}…)")
    print(f"title     : {document.title}   (from {document.title_source})")
    print(f"import    : status={result.import_status}   content_chars={len(document.content)}")
    print(f"formation : status={formation.status}  worth_remembering={formation.worth_remembering}  "
          f"attempts={formation.attempts}  model={formation.model}")
    _print_formation_tail(result.capture_result)


def cmd_import_file(db_path: Path, args: argparse.Namespace) -> int:
    """KB 1.0 Phase 2: .txt/.md -> FileImporter -> Capture -> Memory Formation.

    The file is read **before** the database or the LLM config is touched, so a bad
    path fails with a typed error and without creating a database file or calling a
    model.  The importer itself never writes SQLite: persistence happens through the
    frozen Capture -> Formation pipeline, exactly like the `capture` command.
    """
    path = Path(args.path)
    importer = FileImporter(max_bytes=args.max_bytes)
    try:
        document = importer.load(path, title=args.title)
    except MemorySystemError as exc:
        _dump(
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "file": str(path),
                "db_path": str(db_path),
            }
        )
        return 3
    try:
        config = load_config(args.config)
    except LLMConfigError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__})
        return 2
    try:
        repository = MemoryRepository(Database(db_path))
        client = LLMClient(config)
        quality = None
        if not args.no_quality_check:
            quality = MemoryQualityGate(repository, classifier=LLMConflictClassifier(client))
        capture = CaptureService(
            MemoryFormationService(repository, client, quality=quality), captured_from="file"
        )
        result = importer.import_document(
            document, capture, source_type=args.source_type, dry_run=args.dry_run
        )
    except MemorySystemError as exc:
        _dump(
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "file": str(path),
                "db_path": str(db_path),
            }
        )
        return 3
    if args.json:
        payload = result.as_dict()
        payload["db_path"] = str(db_path)
        payload["file_path"] = str(path)
        payload["llm_config"] = config.as_public_dict()
        payload["counts"] = repository.counts()
        _dump(payload)
        return 0
    _print_import(result, db_path, path)
    return 0


def _print_chat_import(result, db_path: Path, path: Path) -> None:
    """KB 1.0 Phase 3 output: conversation -> import status -> formation status -> writes.

    Never echoes the conversation body: only the identity, counts and previews.
    """
    conversation = result.conversation
    formation = result.capture_result.formation_result
    provider = result.provider if result.provider is not None else "None (unknown provider)"
    print(f"db          : {db_path}")
    print(f"file        : {path}")
    print(f"conversation: {result.conversation_id}   title={result.title!r} "
          f"(from {conversation.title_source})   provider={provider}   messages={result.message_count}")
    print(f"time        : started_at={conversation.started_at}  ended_at={conversation.ended_at}  "
          f"roles={[str(role) for role in conversation.roles()]}")
    print(f"import      : status={result.import_status}   content_chars={len(conversation.render_role_text())}")
    print(f"formation   : status={formation.status}  worth_remembering={formation.worth_remembering}  "
          f"attempts={formation.attempts}  model={formation.model}")
    _print_formation_tail(result.capture_result)
    print("privacy     : 聊天正文会随 Memory Formation 发送给当前配置的模型服务；"
          "本地 SQLite 存储不代表本地推断。")


def cmd_import_chat(db_path: Path, args: argparse.Namespace) -> int:
    """KB 1.0 Phase 3: chat file -> ChatImporter -> Capture -> Memory Formation.

    Like `import-file`, the file is read **before** the database or the LLM config is
    touched, so a bad path fails with a typed error and without creating a database
    file or calling a model.  The conversation body is not echoed by default.
    """
    path = Path(args.path)
    importer = ChatImporter(max_bytes=args.max_bytes, format=args.chat_format)
    try:
        conversation = importer.load(
            path,
            title=args.title,
            provider=args.provider,
            conversation_id=args.conversation_id,
        )
    except MemorySystemError as exc:
        _dump(
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "file": str(path),
                "db_path": str(db_path),
            }
        )
        return 3
    try:
        config = load_config(args.config)
    except LLMConfigError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__})
        return 2
    try:
        repository = MemoryRepository(Database(db_path))
        client = LLMClient(config)
        quality = None
        if not args.no_quality_check:
            quality = MemoryQualityGate(repository, classifier=LLMConflictClassifier(client))
        # same Formation policy knob `form` exposes: chat is the case where keeping the
        # original conversation as a Source matters most
        policy = FormationPolicy(keep_source=args.keep_source)
        capture = CaptureService(
            MemoryFormationService(repository, client, quality=quality, policy=policy),
            captured_from="chat",
        )
        result = importer.import_conversation(conversation, capture, dry_run=args.dry_run)
    except MemorySystemError as exc:
        _dump(
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "file": str(path),
                "db_path": str(db_path),
            }
        )
        return 3
    if args.json:
        payload = result.as_dict()  # redacted: no conversation body
        payload["db_path"] = str(db_path)
        payload["file_path"] = str(path)
        payload["llm_config"] = config.as_public_dict()
        payload["counts"] = repository.counts()
        _dump(payload)
        return 0
    _print_chat_import(result, db_path, path)
    return 0


def _print_web_import(result, db_path: Path) -> None:
    """KB 1.0 Phase 4 output: URL -> extraction -> import status -> formation status -> writes.

    Never echoes the page body: URL, title, counts and the resulting rows only.
    """
    document = result.document
    formation = result.capture_result.formation_result
    print(f"db        : {db_path}")
    print(f"url       : {document.original_url}")
    if document.final_url != document.original_url:
        print(f"final url : {document.final_url}")
    print(f"page      : title={result.title!r} (from {document.title_source})  "
          f"content_type={document.content_type}  chars={document.content_chars}")
    print(f"fetch     : status={document.metadata.get('http_status')}  "
          f"bytes={document.metadata.get('body_bytes')}  charset={document.metadata.get('charset')}  "
          f"redirects={document.metadata.get('redirect_count')}  root={document.metadata.get('extraction_root')}")
    print(f"import    : status={result.import_status}")
    print(f"formation : status={formation.status}  worth_remembering={formation.worth_remembering}  "
          f"attempts={formation.attempts}  model={formation.model}")
    _print_formation_tail(result.capture_result)
    print("privacy   : 网页正文会随 Memory Formation 发送给当前配置的模型服务；默认不打印正文。")


def cmd_import_url(db_path: Path, args: argparse.Namespace) -> int:
    """KB 1.0 Phase 4: URL -> WebImporter -> Capture -> Memory Formation.

    The URL is validated and fetched before the database or the LLM config is touched, so
    a blocked or unreachable URL fails with a typed error, without creating a database and
    without calling a model.  The page body is not printed.
    """
    importer = WebImporter(max_bytes=args.max_bytes, timeout_seconds=args.timeout, min_content_chars=args.min_chars)
    try:
        document = importer.fetch(args.url)
    except MemorySystemError as exc:
        _dump(
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "url": args.url,
                "db_path": str(db_path),
            }
        )
        return 3
    try:
        config = load_config(args.config)
    except LLMConfigError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__})
        return 2
    try:
        repository = MemoryRepository(Database(db_path))
        client = LLMClient(config)
        quality = None
        if not args.no_quality_check:
            quality = MemoryQualityGate(repository, classifier=LLMConflictClassifier(client))
        capture = CaptureService(
            MemoryFormationService(repository, client, quality=quality), captured_from="web"
        )
        result = importer.import_document(document, capture, title=args.title, dry_run=args.dry_run)
    except MemorySystemError as exc:
        _dump(
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "url": args.url,
                "db_path": str(db_path),
            }
        )
        return 3
    if args.json:
        payload = result.as_dict()  # redacted: no page body
        payload["db_path"] = str(db_path)
        payload["llm_config"] = config.as_public_dict()
        payload["counts"] = repository.counts()
        _dump(payload)
        return 0
    _print_web_import(result, db_path)
    return 0


def _print_pdf_import(result, db_path: Path) -> None:
    """KB 1.0 Phase 5 output: PDF -> page-bounded extraction -> import/formation status.

    Never prints the PDF text: file name, title, page/char counts, metadata summary and
    the resulting rows only.
    """
    document = result.document
    formation = result.capture_result.formation_result
    metadata = document.metadata
    print(f"db        : {db_path}")
    print(f"file      : {document.source_path}")
    print(f"title     : {result.title!r} (from {document.title_source})")
    print(f"pages     : {result.page_count} (pages with text: {document.pages_with_text})")
    print(f"text      : {result.content_chars} chars  | extraction_backend={document.extraction_backend}")
    print(f"metadata  : title={metadata.get('pdf_title')!r} author={metadata.get('pdf_author')!r} "
          f"creator={metadata.get('pdf_creator')!r} producer={metadata.get('pdf_producer')!r} "
          f"subject={metadata.get('pdf_subject')!r} keywords={metadata.get('pdf_keywords')!r}")
    print(f"sha256    : {document.pdf_sha256[:16]}…")
    print(f"import    : status={result.import_status}")
    print(f"formation : status={formation.status}  worth_remembering={formation.worth_remembering}  "
          f"attempts={formation.attempts}  model={formation.model}")
    _print_formation_tail(result.capture_result)
    print("privacy   : PDF 正文会随 Memory Formation 发送给当前配置的模型服务；默认不打印正文。")


def cmd_import_pdf(db_path: Path, args: argparse.Namespace) -> int:
    """KB 1.0 Phase 5: PDF -> PdfImporter -> Capture -> Memory Formation.

    The PDF is opened and page-bounded **before** the database or the LLM config is
    touched, so a corrupt/encrypted/oversized PDF fails with a typed error, without
    creating a database and without calling a model.  The PDF text is never printed.
    """
    importer = PdfImporter(
        max_bytes=args.max_bytes,
        max_pages=args.max_pages,
        max_text_chars=args.max_text_chars,
        min_chars=args.min_chars,
    )
    try:
        document = importer.load(args.path, title=args.title)
    except MemorySystemError as exc:
        _dump(
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "file": str(args.path),
                "db_path": str(db_path),
            }
        )
        return 3
    try:
        config = load_config(args.config)
    except LLMConfigError as exc:
        _dump({"error": str(exc), "error_type": type(exc).__name__})
        return 2
    try:
        repository = MemoryRepository(Database(db_path))
        client = LLMClient(config)
        quality = None
        if not args.no_quality_check:
            quality = MemoryQualityGate(repository, classifier=LLMConflictClassifier(client))
        capture = CaptureService(
            MemoryFormationService(repository, client, quality=quality), captured_from="pdf"
        )
        result = importer.import_document(document, capture, dry_run=args.dry_run)
    except MemorySystemError as exc:
        _dump(
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "file": str(args.path),
                "db_path": str(db_path),
            }
        )
        return 3
    if args.json:
        payload = result.as_dict()  # redacted: no PDF text
        payload["db_path"] = str(db_path)
        payload["llm_config"] = config.as_public_dict()
        payload["counts"] = repository.counts()
        _dump(payload)
        return 0
    _print_pdf_import(result, db_path)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = resolve_db_path(getattr(args, "db", None))
    if args.command == "init":
        return cmd_init(db_path)
    if args.command == "info":
        return cmd_info(db_path)
    if args.command == "demo":
        return cmd_demo(db_path, args.reset)
    if args.command == "form":
        return cmd_form(db_path, args)
    if args.command == "capture":
        return cmd_capture(db_path, args)
    if args.command == "import-file":
        return cmd_import_file(db_path, args)
    if args.command == "import-chat":
        return cmd_import_chat(db_path, args)
    if args.command == "web":
        return run_web(db_path, args)
    if args.command == "import-url":
        return cmd_import_url(db_path, args)
    if args.command == "import-pdf":
        return cmd_import_pdf(db_path, args)
    if args.command == "search":
        return cmd_search(db_path, args)
    if args.command == "source":
        return cmd_source(db_path, args.source_id)
    if args.command in {"archive", "activate", "restore"}:
        return cmd_transition(db_path, args.command, args.memory_id, args.json)
    if args.command == "delete":
        return cmd_delete(db_path, args.memory_id, args.json)
    if args.command == "update":
        return cmd_update(db_path, args)
    if args.command == "pending":
        return cmd_pending(db_path, args.limit, args.json)
    if args.command == "version":
        return cmd_version()
    raise SystemExit(f"unknown command: {args.command}")  # pragma: no cover - argparse guards this


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
