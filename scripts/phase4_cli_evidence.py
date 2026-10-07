"""Phase 4 CLI evidence: run the real CLI as separate processes and record the output.

Covers the lifecycle commands end to end on a throwaway database:

    init -> search -> archive -> search (miss) -> search --status archived (hit)
    -> pending -> restore -> search (hit) -> update -> search (old miss / new hit)
    -> illegal transition (exit 3) -> delete -> search (miss) + source still present
    -> info -> version

No model and no network are involved.  Output is captured byte-exactly (UTF-8) and
written to ``docs/phase4-cli-lifecycle.txt`` plus a machine-readable JSON twin.

Run::

    python scripts/phase4_cli_evidence.py --reset
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from personal_memory import (  # noqa: E402
    Database,
    InformationOrigin,
    Memory,
    MemoryRepository,
    MemoryStatus,
    MemoryType,
    Source,
    SourceType,
    utcnow_iso,
)

MEMORY_TITLE = "RAG 的基本原理"
MEMORY_CONTENT = "RAG 通过检索外部知识，为大语言模型提供相关上下文。检索关键词 zebra。"
UPDATED_TITLE = "RAG 的基本原理（修订）"
UPDATED_CONTENT = "RAG 先检索再生成，答案可以追溯出处。更新关键词 ocelot。"
DOOMED_TITLE = "待删除的临时记忆"
DOOMED_CONTENT = "这条记忆会被删除，关键词 narwhal 只出现在这里。"


def child_env() -> dict[str, str]:
    # the CLI must emit the same encoding this script decodes (this machine's
    # locale is cp936, so Python would otherwise print GBK into the pipe)
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run(db_path: Path, argv: Sequence[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "personal_memory", "--db", str(db_path), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=PROJECT_ROOT,
        env=child_env(),
    )
    return {
        "command": "python -m personal_memory --db "
        + str(db_path)
        + " "
        + " ".join(f'"{argument}"' if " " in argument else argument for argument in argv),
        "argv": list(argv),
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr.strip(),
    }


#: Phase 4 closeout: the transition table must also hold when the *repository* API is
#: called directly (the CLI goes through the lifecycle service, this goes around it).
REPO_GUARD_PROBE = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from personal_memory import Database, IllegalTransitionError, MemoryRepository

repository = MemoryRepository(Database(Path(sys.argv[2])))
memory_id = sys.argv[3]
before = repository.require_memory(memory_id).as_dict()
try:
    repository.update_memory(memory_id, status="pending")
    print(json.dumps({"error": None, "refused": False}))
except IllegalTransitionError as exc:
    after = repository.require_memory(memory_id).as_dict()
    print(json.dumps({
        "error": type(exc).__name__,
        "from_status": exc.from_status,
        "to_status": exc.to_status,
        "allowed": list(exc.allowed),
        "row_unchanged": after == before,
        "status_after": after["status"],
    }))
"""


def run_probe(db_path: Path, code: str, *args: str) -> dict[str, Any]:
    """Run an inline python probe against the same database (repository-level API)."""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(PROJECT_ROOT), str(db_path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=PROJECT_ROOT,
        env=child_env(),
    )
    return {
        "command": "python -c <repository guard probe> " + str(db_path),
        "argv": ["<repository guard probe>", str(db_path), *args],
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr.strip(),
    }


def seed(db_path: Path) -> tuple[Memory, Memory, Source]:
    repository = MemoryRepository(Database(db_path))
    source = repository.create_source(
        Source.create(
            source_type=SourceType.TEXT,
            title="《RAG 入门》",
            content="RAG 是 Retrieval-Augmented Generation 的缩写：先检索，再生成。",
        )
    )
    memory = repository.create_memory(
        Memory.create(
            type=MemoryType.KNOWLEDGE,
            title=MEMORY_TITLE,
            content=MEMORY_CONTENT,
            information_origin=InformationOrigin.SOURCE_CONTENT,
            tags=["rag", "retrieval"],
            importance=0.8,
            confidence=0.9,
        )
    )
    doomed = repository.create_memory(
        Memory.create(
            type=MemoryType.EVENT,
            title=DOOMED_TITLE,
            content=DOOMED_CONTENT,
            information_origin=InformationOrigin.USER_EXPLICIT,
            status=MemoryStatus.PENDING,
        )
    )
    repository.link(memory.id, source.id)
    repository.link(doomed.id, source.id)
    return memory, doomed, source


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 CLI evidence run")
    parser.add_argument("--db", default="data/phase4-cli.db")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--transcript", default="docs/phase4-cli-lifecycle.txt")
    parser.add_argument("--json-out", default="docs/phase4-cli-lifecycle.json")
    args = parser.parse_args()

    db_path = Path(args.db)
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    init = run(db_path, ["init"])
    memory, doomed, source = seed(db_path)

    commands: list[dict[str, Any]] = []
    named: dict[str, dict[str, Any]] = {}

    def step(name: str, record: dict[str, Any]) -> None:
        named[name] = record
        commands.append(record)

    step("init", init)
    step("search_before", run(db_path, ["search", "zebra"]))
    step("archive", run(db_path, ["archive", memory.id]))
    step("search_after_archive", run(db_path, ["search", "zebra"]))
    step("search_archived_explicit", run(db_path, ["search", "zebra", "--status", "archived", "--json"]))
    step("pending", run(db_path, ["pending"]))
    step("restore", run(db_path, ["restore", memory.id]))
    step("search_after_restore", run(db_path, ["search", "zebra"]))
    step(
        "update",
        run(db_path, ["update", memory.id, "--title", UPDATED_TITLE, "--content", UPDATED_CONTENT]),
    )
    step("search_old_keyword", run(db_path, ["search", "zebra"]))
    step("search_new_keyword", run(db_path, ["search", "ocelot"]))
    step("illegal_status_update", run(db_path, ["update", memory.id, "--status", "pending"]))  # exit 3
    step("invalid_value_update", run(db_path, ["update", memory.id, "--importance", "2"]))  # exit 3
    step("repository_guard", run_probe(db_path, REPO_GUARD_PROBE, memory.id))
    step("delete", run(db_path, ["delete", doomed.id, "--json"]))
    step("search_after_delete", run(db_path, ["search", "narwhal", "--status", "all"]))
    step("source_after_delete", run(db_path, ["source", source.id]))
    step("info", run(db_path, ["info"]))
    step("version", run(db_path, ["version"]))

    repository = MemoryRepository(Database(db_path))
    guard = json.loads(named["repository_guard"]["stdout"])
    checks = {
        "search_missed_after_archive": "(no memory matched)" in named["search_after_archive"]["stdout"],
        "archived_visible_when_asked": '"archived"' in named["search_archived_explicit"]["stdout"],
        "restored_searchable": "RAG" in named["search_after_restore"]["stdout"],
        "old_keyword_gone": "(no memory matched)" in named["search_old_keyword"]["stdout"],
        "new_keyword_hits": "ocelot" in named["search_new_keyword"]["stdout"],
        "illegal_transition_exit_3": named["illegal_status_update"]["exit_code"] == 3
        and "IllegalTransitionError" in named["illegal_status_update"]["stdout"],
        "invalid_update_exit_3": named["invalid_value_update"]["exit_code"] == 3
        and "ValidationError" in named["invalid_value_update"]["stdout"],
        # Phase 4 closeout: the SAME table must hold for a direct repository call
        "repository_refuses_active_to_pending": guard["error"] == "IllegalTransitionError"
        and guard["row_unchanged"] is True
        and guard["status_after"] == "active"
        and guard["allowed"] == ["archived"],
        "source_survives_memory_delete": "《RAG 入门》" in named["source_after_delete"]["stdout"],
        "deleted_memory_unsearchable": "(no memory matched)" in named["search_after_delete"]["stdout"],
        "index_consistent": repository.index_consistency()["consistent"],
        "counts": repository.counts(),
        "status_counts": repository.status_counts(),
        "schema_version": Database(db_path).schema_version(),
    }

    lines = [
        "Phase 4 CLI evidence -- real processes, real SQLite file",
        f"generated_at : {utcnow_iso()}",
        f"db           : {db_path}",
        f"python       : {sys.executable}",
        f"commands     : {len(commands)}",
        "",
    ]
    for record in commands:
        lines.append("=" * 78)
        lines.append(f"$ {record['command']}")
        lines.append(f"[exit {record['exit_code']}]")
        lines.append(record["stdout"].rstrip())
        if record["stderr"]:
            lines.append("stderr: " + record["stderr"])
        lines.append("")
    lines.append("=" * 78)
    lines.append("cross-checks")
    lines.append(json.dumps(checks, ensure_ascii=False, indent=2, sort_keys=True))
    transcript = "\n".join(lines) + "\n"

    transcript_path = Path(args.transcript)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(transcript, encoding="utf-8")

    payload = {
        "generated_at": utcnow_iso(),
        "db_path": str(db_path),
        "seed": {"memory_id": memory.id, "doomed_id": doomed.id, "source_id": source.id},
        "commands": commands,
        "checks": checks,
    }
    json_path = Path(args.json_out)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(checks, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"transcript: {transcript_path} ({len(transcript)} chars)")
    print(f"json      : {json_path}")
    required = [
        checks["search_missed_after_archive"],
        checks["archived_visible_when_asked"],
        checks["restored_searchable"],
        checks["old_keyword_gone"],
        checks["new_keyword_hits"],
        checks["illegal_transition_exit_3"],
        checks["invalid_update_exit_3"],
        checks["repository_refuses_active_to_pending"],
        checks["source_survives_memory_delete"],
        checks["deleted_memory_unsearchable"],
        checks["index_consistent"],
    ]
    return 0 if all(required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
