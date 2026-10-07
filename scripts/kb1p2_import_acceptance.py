"""Knowledge Base 1.0 Phase 2 acceptance: TXT + Markdown import through the real CLI.

    A. rag.txt  -> import-file -> Capture -> real LLM -> Memory (+ 1 file Source)
    B. rag.md   -> import-file -> Capture -> real LLM -> Memory (H1 title used, code kept)
    C. rag.md again (human-readable output) -> the frozen dedupe applies, no second
       identical long-term Memory
    D. a NEW process finds both Memories through Phase 3 retrieval
    E. missing / unsupported / empty / directory inputs -> typed errors, no writes,
       and no database file is even created

A/B/C use the configured real LLM (3 model calls at most, plus a conflict-classification
call if the second file looks related to the first).  D/E need no model at all.
The credential is read into THIS process's environment and is never printed or written
to evidence.

Run::

    python scripts/kb1p2_import_acceptance.py --reset
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from personal_memory import (  # noqa: E402
    Database,
    MemoryRepository,
    memory_fingerprint,
    utcnow_iso,
)

DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"

TXT_CONTENT = """Retrieval-Augmented Generation（RAG）的核心机制：先用检索器从外部语料库取回与问题相关的片段，
再让生成模型只依据这些片段作答。这样做可以显著降低幻觉，并让答案能够追溯到具体资料。

实践要点：检索质量决定效果上限，切分粒度与召回数量都需要按语料规模调整。
"""

MD_CONTENT = """---
source: 手写笔记
tags: [rag, 检索]
---

# 向量检索与重排序

向量检索先把文本编码为向量，再按相似度取回候选片段；重排序（rerank）再用更强的模型对候选重新打分，
通常能在不改变索引的前提下提升前几条结果的质量。

## 实践顺序

1. 先做关键词或向量召回
2. 再加 rerank 精排
3. 最后按离线评测结果调参

```python
def search(query, top_k=5):
    return index.query(query, top_k=top_k)
```
"""


def ensure_credential_from_dsh_file() -> str | None:
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


def child_env() -> dict[str, str]:
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run_cli(db_path: Path, *argv: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "personal_memory", "--db", str(db_path), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=PROJECT_ROOT,
        env=child_env(),
    )
    try:
        payload: Any = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = None
    return {
        "command": "python -m personal_memory --db " + str(db_path) + " " + " ".join(argv),
        "argv": list(argv),
        "exit_code": completed.returncode,
        "payload": payload,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def counts(db_path: Path) -> dict[str, int]:
    return MemoryRepository(Database(db_path)).counts()


def fingerprints(db_path: Path) -> dict[str, Any]:
    memories = MemoryRepository(Database(db_path)).list_memories(limit=1000)
    seen: dict[str, int] = {}
    for memory in memories:
        key = memory_fingerprint(memory.type, memory.title, memory.content)
        seen[key] = seen.get(key, 0) + 1
    duplicates = {key: n for key, n in seen.items() if n > 1}
    return {"memories": len(memories), "unique": len(seen), "duplicated": duplicates}


def scenario_a_txt(db_path: Path, fixture: Path) -> dict[str, Any]:
    before = counts(db_path)
    record = run_cli(db_path, "import-file", str(fixture), "--json")
    payload = record["payload"] or {}
    after = counts(db_path)
    memories = payload.get("memories_created", [])
    ok = (
        record["exit_code"] == 0
        and payload.get("import_status") == "imported"
        and payload.get("formation_status") == "persisted"
        and len(memories) >= 1
        and payload.get("source_count") == 1
        and (payload.get("file") or {}).get("filename") == fixture.name
        and (payload.get("file") or {}).get("title_source") == "filename"
        and after["memories"] > before["memories"]
    )
    return {
        "scenario": "A_txt_import",
        "expectation": "rag.txt -> import-file -> Capture -> real LLM -> Memory + file Source",
        "ok": ok,
        "exit_code": record["exit_code"],
        "import_status": payload.get("import_status"),
        "formation_status": payload.get("formation_status"),
        "file": payload.get("file"),
        "memories_created": [
            {"id": m["id"], "type": m["type"], "title": m["title"], "status": m["status"]} for m in memories
        ],
        "sources_created": payload.get("sources_created"),
        "reason": (payload.get("capture_result") or {}).get("formation_result", {}).get("reason"),
        "counts_before": before,
        "counts_after": after,
    }


def scenario_b_markdown(db_path: Path, fixture: Path) -> dict[str, Any]:
    before = counts(db_path)
    record = run_cli(db_path, "import-file", str(fixture), "--json")
    payload = record["payload"] or {}
    after = counts(db_path)
    memories = payload.get("memories_created", [])
    file_info = payload.get("file") or {}
    source = (payload.get("sources_created") or [{}])[0]
    ok = (
        record["exit_code"] == 0
        and payload.get("formation_status") == "persisted"
        and len(memories) >= 1
        and file_info.get("title") == "向量检索与重排序"
        and file_info.get("title_source") == "heading"
        and source.get("source_type") == "file"
        and source.get("title") == "向量检索与重排序"
        and after["memories"] > before["memories"]
    )
    return {
        "scenario": "B_markdown_import",
        "expectation": "rag.md -> import-file -> Capture -> real LLM -> Memory (H1 title, code kept)",
        "ok": ok,
        "exit_code": record["exit_code"],
        "import_status": payload.get("import_status"),
        "formation_status": payload.get("formation_status"),
        "file": file_info,
        "memories_created": [
            {"id": m["id"], "type": m["type"], "title": m["title"], "status": m["status"]} for m in memories
        ],
        "sources_created": payload.get("sources_created"),
        "reason": (payload.get("capture_result") or {}).get("formation_result", {}).get("reason"),
        "counts_before": before,
        "counts_after": after,
    }


def scenario_c_reimport(db_path: Path, fixture: Path) -> dict[str, Any]:
    """Re-import the Markdown file and capture the human-readable output.

    The assertions avoid depending on how the model phrases its second extraction:

    * the file Source is **reused** (Phase 2's ``content_hash`` dedupe) -- no second
      Source row for the same bytes;
    * no two Memories share a fingerprint (Phase 4's exact-duplicate rule);
    * nothing new becomes an **active** Memory: an identical extraction is reported as
      ``duplicate`` with zero new rows, and a re-phrased one is stored as ``pending``
      by the frozen quality gate;
    * the output shows the fields section 十 of the spec asks for.
    """
    repository = MemoryRepository(Database(db_path))
    before_ids = {memory.id for memory in repository.list_memories(limit=1000)}
    before = repository.counts()
    before_fingerprints = fingerprints(db_path)

    record = run_cli(db_path, "import-file", str(fixture))  # no --json: the human output

    fresh = MemoryRepository(Database(db_path))
    after = fresh.counts()
    after_ids = {memory.id for memory in fresh.list_memories(limit=1000)}
    new_memories = [fresh.require_memory(memory_id) for memory_id in sorted(after_ids - before_ids)]
    after_fingerprints = fingerprints(db_path)
    text = record["stdout"]
    required_lines = ("import    : status=", "formation : status=", "memories  :", "sources   :")

    ok = (
        record["exit_code"] == 0
        and all(line in text for line in required_lines)
        and after["sources"] == before["sources"]
        and not after_fingerprints["duplicated"]
        and all(str(memory.status) != "active" for memory in new_memories)
    )
    return {
        "scenario": "C_reimport_dedupe",
        "expectation": "re-importing the same file reuses the frozen dedupe (Source reused, "
        "no second identical or active Memory)",
        "ok": ok,
        "exit_code": record["exit_code"],
        "counts_before": before,
        "counts_after": after,
        "source_rows_before_after": [before["sources"], after["sources"]],
        "new_memories": [
            {"id": memory.id, "title": memory.title, "status": str(memory.status)} for memory in new_memories
        ],
        "fingerprints_before": before_fingerprints,
        "fingerprints_after": after_fingerprints,
        "human_readable_output": text,
    }


def scenario_d_retrieval(db_path: Path) -> dict[str, Any]:
    record = run_cli(db_path, "search", "检索", "--status", "all", "--limit", "10", "--json")
    payload = record["payload"] or {}
    titles = [hit["memory"]["title"] for hit in payload.get("hits", [])]
    fresh = MemoryRepository(Database(db_path))
    ok = record["exit_code"] == 0 and payload.get("total", 0) >= 2 and fresh.counts()["memories"] >= 2
    return {
        "scenario": "D_new_process_retrieval",
        "expectation": "a new process finds the imported Memories through Phase 3 retrieval",
        "ok": ok,
        "exit_code": record["exit_code"],
        "search_total": payload.get("total"),
        "search_titles": titles,
        "search_modes": payload.get("mode"),
        "fresh_connection_counts": fresh.counts(),
        "command_output": record["stdout"][:600],
    }


def scenario_e_failures(fixture_dir: Path, fresh_db: Path) -> dict[str, Any]:
    missing = fixture_dir / "does-not-exist.txt"
    unsupported = fixture_dir / "notes.pdf"
    unsupported.write_text("not really a pdf", encoding="utf-8")
    empty = fixture_dir / "empty.txt"
    empty.write_text("   \n", encoding="utf-8")

    cases = []
    for label, target in (
        ("missing_file", missing),
        ("unsupported_extension", unsupported),
        ("empty_file", empty),
        ("directory", fixture_dir),
    ):
        record = run_cli(fresh_db, "import-file", str(target), "--json")
        payload = record["payload"] or {}
        cases.append(
            {
                "case": label,
                "target": str(target),
                "exit_code": record["exit_code"],
                "error_type": payload.get("error_type"),
                "error": payload.get("error"),
            }
        )
    expected = {
        "missing_file": "FileMissingError",
        "unsupported_extension": "UnsupportedFileTypeError",
        "empty_file": "EmptyFileError",
        "directory": "NotAFileError",
    }
    ok = all(
        case["exit_code"] == 3 and case["error_type"] == expected[case["case"]] for case in cases
    ) and not fresh_db.exists()
    return {
        "scenario": "E_failures",
        "expectation": "every failure is typed, writes nothing, and does not even create a database",
        "ok": ok,
        "cases": cases,
        "database_created": fresh_db.exists(),
    }


def transcript(evidence: dict[str, Any]) -> str:
    lines = [
        "Knowledge Base 1.0 -- Phase 2 (TXT + Markdown Importer) acceptance run",
        f"generated_at : {evidence['generated_at']}",
        f"db           : {evidence['db_path']}",
        f"package      : {evidence['package_version']}  phase={evidence['phase']}",
        f"credential   : {evidence['credential_source']}",
        f"fixtures     : {evidence['fixture_dir']}",
        "model calls  : A + B + C (C should hit the duplicate path; D/E call no model)",
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
    lines.append(f"fingerprints: {json.dumps(evidence['fingerprints'], ensure_ascii=False)}")
    lines.append(f"index consistent: {evidence['index_consistency']['consistent']}")
    lines.append(f"overall: {'PASS' if evidence['overall_ok'] else 'FAIL'}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="KB 1.0 Phase 2 import acceptance")
    parser.add_argument("--db", default="data/kb1p2-import.db")
    parser.add_argument("--fixtures", default="data/kb1p2-fixtures")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--evidence", default="docs/kb1p2-import-acceptance.json")
    parser.add_argument("--transcript", default="docs/kb1p2-import-acceptance.txt")
    parser.add_argument("--skip-llm", action="store_true", help="run only the model-free scenarios")
    args = parser.parse_args()

    db_path = Path(args.db)
    fixture_dir = Path(args.fixtures)
    fixture_dir.mkdir(parents=True, exist_ok=True)
    txt_fixture = fixture_dir / "rag.txt"
    md_fixture = fixture_dir / "rag.md"
    txt_fixture.write_text(TXT_CONTENT, encoding="utf-8")
    md_fixture.write_text(MD_CONTENT, encoding="utf-8")

    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    credential_source = ensure_credential_from_dsh_file()
    has_credential = bool(
        os.environ.get("PERSONAL_MEMORY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    )

    subprocess.run(
        [sys.executable, "-m", "personal_memory", "--db", str(db_path), "init"],
        capture_output=True, text=True, encoding="utf-8", cwd=PROJECT_ROOT, env=child_env(),
    )

    evidence: dict[str, Any] = {
        "generated_at": utcnow_iso(),
        "phase": "kb-1.0 phase-2 (txt + markdown importer)",
        "package_version": __import__("personal_memory").__version__,
        "db_path": str(db_path),
        "fixture_dir": str(fixture_dir),
        "credential_source": credential_source if has_credential else None,
        "scenarios": [],
    }

    if args.skip_llm or not has_credential:
        evidence["note"] = "scenarios A/B/C skipped (--skip-llm or no credential available)"
    else:
        for name, function in (
            ("A", lambda: scenario_a_txt(db_path, txt_fixture)),
            ("B", lambda: scenario_b_markdown(db_path, md_fixture)),
        ):
            evidence["scenarios"].append(function())
            print(f"scenario {name}: ok={evidence['scenarios'][-1]['ok']}", flush=True)
        evidence["scenarios"].append(scenario_c_reimport(db_path, md_fixture))
        print(f"scenario C: ok={evidence['scenarios'][-1]['ok']}", flush=True)

    evidence["scenarios"].append(scenario_d_retrieval(db_path))
    print(f"scenario D: ok={evidence['scenarios'][-1]['ok']}", flush=True)
    evidence["scenarios"].append(scenario_e_failures(fixture_dir, fixture_dir.parent / "kb1p2-fresh.db"))
    print(f"scenario E: ok={evidence['scenarios'][-1]['ok']}", flush=True)

    repository = MemoryRepository(Database(db_path))
    evidence["final_counts"] = repository.counts()
    evidence["final_status_counts"] = repository.status_counts()
    evidence["fingerprints"] = fingerprints(db_path)
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
    Path(args.transcript).write_text(text_out, encoding="utf-8")
    print(text_out)
    print(f"evidence: {evidence_path} ({len(payload)} bytes), transcript: {args.transcript}")
    return 0 if evidence["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
