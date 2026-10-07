"""Knowledge Base 1.0 Phase 1 acceptance: the spec's scenarios A-D through the real CLI.

    A. high-value text  -> capture -> formation -> persisted -> Memory created
    B. low-value text   -> capture -> formation -> skipped   -> 0 Memory, 0 Source
    C. LLM failure      -> capture -> (transport error)      -> no half-written record
    D. persistence      -> a NEW process finds the Memory again
    F. empty input      -> ValidationError, no model call, no writes

Scenarios A/B use the configured real LLM (the only model calls in this run);
C/D/F need no model at all.  The credential is read from the environment or from the
DSH credential file into THIS process's environment and is never printed or written
to evidence.

Run::

    python scripts/kb1_capture_acceptance.py --reset
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

from personal_memory import Database, MemoryRepository, compute_content_hash, utcnow_iso  # noqa: E402

DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"

HIGH_VALUE = (
    "Retrieval-Augmented Generation（RAG）把检索与生成结合起来：先用检索器从外部语料库取回相关片段，"
    "再让生成模型只基于这些片段作答，因此答案可以追溯到具体资料，并显著降低幻觉。"
)
LOW_VALUE = "今天喝了一杯奶茶。"
FAILURE_INPUT = "这段输入会撞上一个不可达的模型端点，不应该留下任何半成品记录。"
EMPTY_INPUT = "   "
SEARCH_KEYWORD = "RAG"


def ensure_credential_from_dsh_file() -> str | None:
    """Load ``DEEPSEEK_API_KEY`` into this process's environment (never printed)."""
    if os.environ.get("PERSONAL_MEMORY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"):
        return "environment"
    if not DSH_CREDENTIALS.exists():
        return None
    for line in DSH_CREDENTIALS.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("DEEPSEEK_API_KEY:"):
            value = line.split(":", 1)[1].strip().strip('"').strip("'")
            if value:
                os.environ["DEEPSEEK_API_KEY"] = value
                return f"{DSH_CREDENTIALS} (refs.DEEPSEEK_API_KEY)"
    return None


def child_env(**extra: str) -> dict[str, str]:
    return {**os.environ, "PYTHONIOENCODING": "utf-8", **extra}


def run_cli(db_path: Path, *argv: str, env_extra: dict[str, str] | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "personal_memory", "--db", str(db_path), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=PROJECT_ROOT,
        env=child_env(**(env_extra or {})),
    )
    try:
        payload: Any = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = None
    return {
        "command": "python -m personal_memory --db "
        + str(db_path)
        + " "
        + " ".join(f'"{a}"' if " " in a or a == "" else a for a in argv),
        "argv": list(argv),
        "exit_code": completed.returncode,
        "payload": payload,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def counts(db_path: Path) -> dict[str, int]:
    return MemoryRepository(Database(db_path)).counts()


def scenario_a(db_path: Path) -> dict[str, Any]:
    before = counts(db_path)
    record = run_cli(db_path, "capture", HIGH_VALUE, "--title", "RAG 基础", "--json")
    payload = record["payload"] or {}
    after = counts(db_path)
    memories = payload.get("memories_created", [])
    ok = (
        record["exit_code"] == 0
        and payload.get("captured") is True
        and payload.get("status") == "persisted"
        and len(memories) >= 1
        and after["memories"] == before["memories"] + len(memories)
    )
    return {
        "scenario": "A_high_value",
        "expectation": "capture -> formation -> persisted -> Memory created",
        "ok": ok,
        "exit_code": record["exit_code"],
        "capture_status": payload.get("status"),
        "formation_status": payload.get("formation_status"),
        "captured_from": payload.get("captured_from"),
        "memory_count": payload.get("memory_count"),
        "source_count": payload.get("source_count"),
        "memories_created": [
            {"id": m["id"], "type": m["type"], "title": m["title"], "status": m["status"]}
            for m in memories
        ],
        "sources_created": [
            {"id": s["id"], "source_type": s["source_type"], "title": s["title"]}
            for s in payload.get("sources_created", [])
        ],
        "reason": (payload.get("formation_result") or {}).get("reason"),
        "counts_before": before,
        "counts_after": after,
        "command_output": record["stdout"][:1200],
    }


def scenario_b(db_path: Path) -> dict[str, Any]:
    before = counts(db_path)
    record = run_cli(db_path, "capture", LOW_VALUE, "--json")
    payload = record["payload"] or {}
    after = counts(db_path)
    ok = (
        record["exit_code"] == 0
        and payload.get("status") == "skipped"
        and payload.get("memory_count") == 0
        and payload.get("source_count") == 0
        and after == before  # nothing written at all
    )
    return {
        "scenario": "B_low_value",
        "expectation": "capture -> formation -> skipped -> 0 Memory, 0 Source, no write",
        "ok": ok,
        "exit_code": record["exit_code"],
        "capture_status": payload.get("status"),
        "worth_remembering": (payload.get("formation_result") or {}).get("worth_remembering"),
        "memory_count": payload.get("memory_count"),
        "source_count": payload.get("source_count"),
        "counts_before": before,
        "counts_after": after,
        "command_output": record["stdout"][:800],
    }


def scenario_c(db_path: Path, broken_config: Path) -> dict[str, Any]:
    broken_config.write_text(
        json.dumps(
            {
                "provider": "custom",
                "model": "unreachable-model",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key_env": "KB1_BROKEN_KEY",
                "timeout_seconds": 2,
                "max_transport_retries": 0,
            }
        ),
        encoding="utf-8",
    )
    before = counts(db_path)
    record = run_cli(
        db_path,
        "capture",
        FAILURE_INPUT,
        "--config",
        str(broken_config),
        "--json",
        env_extra={"KB1_BROKEN_KEY": "not-a-real-key"},
    )
    payload = record["payload"] or {}
    after = counts(db_path)
    repository = MemoryRepository(Database(db_path))
    digest = compute_content_hash(FAILURE_INPUT)
    leftover_source = repository.source_exists_by_hash(digest)
    leftover_memory = any(FAILURE_INPUT in memory.content for memory in repository.list_memories(limit=500))
    ok = (
        record["exit_code"] == 3
        and payload.get("error_type") in {"LLMRequestError", "LLMError"}
        and after == before
        and not leftover_source
        and not leftover_memory
        and repository.index_consistency()["consistent"]
    )
    return {
        "scenario": "C_failure",
        "expectation": "LLM/transport failure -> typed error, no half-written long-term record",
        "ok": ok,
        "exit_code": record["exit_code"],
        "error_type": payload.get("error_type"),
        "error": payload.get("error"),
        "broken_config": str(broken_config),
        "counts_before": before,
        "counts_after": after,
        "leftover_source_for_this_input": leftover_source,
        "leftover_memory_for_this_input": leftover_memory,
        "index_consistent": repository.index_consistency()["consistent"],
    }


def scenario_d(db_path: Path) -> dict[str, Any]:
    record = run_cli(db_path, "search", SEARCH_KEYWORD, "--json")
    payload = record["payload"] or {}
    hits = payload.get("hits", [])
    # a brand-new connection in THIS process too (belt and braces for "process exited")
    fresh = MemoryRepository(Database(db_path))
    ok = record["exit_code"] == 0 and payload.get("total", 0) >= 1 and fresh.counts()["memories"] >= 1
    return {
        "scenario": "D_persistence",
        "expectation": "after the capturing process exited, the Memory is still there (new process)",
        "ok": ok,
        "exit_code": record["exit_code"],
        "search_total": payload.get("total"),
        "search_titles": [hit["memory"]["title"] for hit in hits],
        "search_ids": [hit["memory"]["id"] for hit in hits],
        "source_refs": [len(hit.get("sources", [])) for hit in hits],
        "fresh_connection_counts": fresh.counts(),
        "command_output": record["stdout"][:800],
    }


def scenario_f(db_path: Path, config_error: str | None) -> dict[str, Any]:
    if config_error:
        return {
            "scenario": "F_empty_input",
            "expectation": "empty content -> ValidationError, no model call, no write",
            "ok": False,
            "skipped": f"credentials unavailable: {config_error}",
        }
    before = counts(db_path)
    record = run_cli(db_path, "capture", EMPTY_INPUT, "--json")
    payload = record["payload"] or {}
    after = counts(db_path)
    ok = record["exit_code"] == 3 and payload.get("error_type") == "ValidationError" and after == before
    return {
        "scenario": "F_empty_input",
        "expectation": "empty/whitespace content -> ValidationError, no model call, no write",
        "ok": ok,
        "exit_code": record["exit_code"],
        "error_type": payload.get("error_type"),
        "error": payload.get("error"),
        "counts_before": before,
        "counts_after": after,
    }


def transcript(evidence: dict[str, Any]) -> str:
    lines = [
        "Knowledge Base 1.0 -- Phase 1 (Capture Layer) acceptance run",
        f"generated_at : {evidence['generated_at']}",
        f"db           : {evidence['db_path']}",
        f"package      : {evidence['package_version']}  phase={evidence['phase']}",
        f"credential   : {evidence['credential_source']}",
        f"llm          : {json.dumps(evidence['llm'], ensure_ascii=False)}",
        f"model calls  : A = 1 formation call, B = 1 formation call (C/D/F call no model)",
        "",
    ]
    for scenario in evidence["scenarios"]:
        lines.append("=" * 78)
        lines.append(f"[{scenario['scenario']}] ok={scenario['ok']}")
        lines.append(f"expectation: {scenario['expectation']}")
        lines.append(
            json.dumps(
                {k: v for k, v in scenario.items() if k not in {"scenario", "expectation", "ok"}},
                ensure_ascii=False,
                indent=2,
            )
        )
        lines.append("")
    lines.append("=" * 78)
    lines.append(f"final counts: {evidence['final_counts']}  statuses: {evidence['final_status_counts']}")
    lines.append(f"index consistent: {evidence['index_consistency']['consistent']}")
    lines.append(f"overall: {'PASS' if evidence['overall_ok'] else 'FAIL'}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="KB 1.0 Phase 1 capture acceptance")
    parser.add_argument("--db", default="data/kb1-capture.db")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--evidence", default="docs/kb1-capture-acceptance.json")
    parser.add_argument("--transcript", default="docs/kb1-capture-acceptance.txt")
    parser.add_argument("--skip-llm", action="store_true", help="run only the model-free scenarios")
    args = parser.parse_args()

    db_path = Path(args.db)
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    credential_source = ensure_credential_from_dsh_file()
    config_error = None
    if not os.environ.get("PERSONAL_MEMORY_LLM_API_KEY") and not os.environ.get("DEEPSEEK_API_KEY"):
        config_error = "no API key in the environment"

    # initialise the file so the model-free scenarios always have a database to inspect
    subprocess.run(
        [sys.executable, "-m", "personal_memory", "--db", str(db_path), "init"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=PROJECT_ROOT,
        env=child_env(),
    )

    evidence: dict[str, Any] = {
        "generated_at": utcnow_iso(),
        "phase": "kb-1.0 phase-1 (capture layer)",
        "package_version": __import__("personal_memory").__version__,
        "db_path": str(db_path),
        "credential_source": credential_source if not config_error else None,
        "llm": {"provider": "deepseek", "model": "deepseek-flash", "key_present": not config_error},
        "scenarios": [],
    }

    broken_config = db_path.parent / f"{db_path.stem}-broken-llm.json"
    if not args.skip_llm and not config_error:
        evidence["scenarios"].append(scenario_a(db_path))
        print(f"scenario A: ok={evidence['scenarios'][-1]['ok']}")
        evidence["scenarios"].append(scenario_b(db_path))
        print(f"scenario B: ok={evidence['scenarios'][-1]['ok']}")
    else:
        evidence["note"] = "scenarios A/B skipped (--skip-llm or no credential)"

    evidence["scenarios"].append(scenario_c(db_path, broken_config))
    print(f"scenario C: ok={evidence['scenarios'][-1]['ok']}")
    evidence["scenarios"].append(scenario_d(db_path))
    print(f"scenario D: ok={evidence['scenarios'][-1]['ok']}")
    evidence["scenarios"].append(scenario_f(db_path, config_error if args.skip_llm else config_error))
    print(f"scenario F: ok={evidence['scenarios'][-1]['ok']}")

    repository = MemoryRepository(Database(db_path))
    evidence["final_counts"] = repository.counts()
    evidence["final_status_counts"] = repository.status_counts()
    evidence["index_consistency"] = repository.index_consistency()
    evidence["overall_ok"] = all(scenario["ok"] for scenario in evidence["scenarios"])

    payload = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    api_key = os.environ.get("PERSONAL_MEMORY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    if api_key and api_key in payload:
        raise SystemExit("refusing to write evidence: the API key leaked into the payload")
    evidence_path = Path(args.evidence)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(payload + "\n", encoding="utf-8")

    text_out = transcript(evidence)
    transcript_path = Path(args.transcript)
    transcript_path.write_text(text_out, encoding="utf-8")

    print(text_out)
    print(f"evidence: {evidence_path} ({len(payload)} bytes), transcript: {transcript_path}")
    return 0 if evidence["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
