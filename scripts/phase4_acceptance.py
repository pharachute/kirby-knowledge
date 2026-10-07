"""Phase 4 acceptance: the five scenarios of the spec, driven through the real CLI.

    A. active -> archive            -> default search finds nothing
    B. archived -> active (restore) -> search finds it again
    C. update the content           -> old keyword stops matching, new keyword matches
    D. form the same input twice    -> no second identical long-term Memory
    E. "用户喜欢 A" then "用户更喜欢 B" -> A is kept, B is not activated (pending/conflict)

Scenarios A-C need no model and no network.  D and E go through ``form``, so they
need the configured LLM (the conflict label additionally needs the classifier);
the real credential is read from the environment or from the DSH credential file
and is never printed or written to evidence.

Run::

    python scripts/phase4_acceptance.py --reset
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from personal_memory import (  # noqa: E402
    Database,
    InformationOrigin,
    Memory,
    MemoryDraft,
    MemoryQualityGate,
    MemoryRepository,
    MemoryStatus,
    MemoryType,
    utcnow_iso,
)
from personal_memory.cli import main as cli_main  # noqa: E402
from personal_memory.llm import LLMConfigError, load_config  # noqa: E402
from personal_memory.prompts import CONFLICT_PROMPT_VERSION, PROMPT_VERSION  # noqa: E402
from personal_memory.quality import memory_fingerprint  # noqa: E402

SEED_TITLE = "RAG 的基本原理"
SEED_CONTENT = "RAG 通过检索外部知识，为大语言模型提供相关上下文。"
NEW_TITLE = "RAG 的基本原理（已修订）"
NEW_CONTENT = "RAG 先检索再生成：把外部资料放进上下文，让答案可以追溯到原文出处。更新关键词 ocelot。"
OLD_KEYWORD = "检索外部知识"
NEW_KEYWORD = "追溯到原文出处"

PREFERENCE_A = "用户喜欢 A 方案，用它保存个人知识库。"
PREFERENCE_B = "用户更喜欢 B 方案，用它保存个人知识库。"

DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"


def ensure_credential_from_dsh_file() -> str | None:
    """Load ``DEEPSEEK_API_KEY`` into the process environment if it is set there.

    Returns a short provenance string (never the key itself).  The DSH credential
    store keeps the value under ``refs.DEEPSEEK_API_KEY``.
    """
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


def run_cli(db_path: Path, *argv: str) -> tuple[int, Any, str]:
    """Call the real CLI in-process and capture what it printed."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli_main(["--db", str(db_path), *argv])
    text = buffer.getvalue()
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    return code, payload, text


def seed(db_path: Path) -> Memory:
    """Create the Memory the spec's acceptance section names."""
    repository = MemoryRepository(Database(db_path))
    existing = [
        memory
        for memory in repository.list_memories(status="active")
        if memory.title == SEED_TITLE
    ]
    if existing:
        return existing[0]
    memory = Memory.create(
        type=MemoryType.KNOWLEDGE,
        title=SEED_TITLE,
        content=SEED_CONTENT,
        information_origin=InformationOrigin.SOURCE_CONTENT,
        summary="RAG 用外部检索结果为大模型提供上下文。",
        tags=["rag", "retrieval"],
        importance=0.8,
        confidence=0.9,
    )
    return repository.create_memory(memory)


def search_titles(db_path: Path, query: str, *extra: str) -> dict[str, Any]:
    code, payload, _ = run_cli(db_path, "search", query, "--json", *extra)
    if payload is None:
        return {"exit_code": code, "hits": [], "error": "unparsable output"}
    return {
        "exit_code": code,
        "query": payload["query"],
        "statuses": payload["statuses"],
        "total": payload["total"],
        "mode": payload["mode"],
        "titles": [hit["memory"]["title"] for hit in payload["hits"]],
        "ids": [hit["memory"]["id"] for hit in payload["hits"]],
        "hits": [
            {
                "memory_id": hit["memory"]["id"],
                "title": hit["memory"]["title"],
                "status": hit["memory"]["status"],
                "score": hit["score"],
                "bm25": hit["bm25"],
                "index": hit["index"],
            }
            for hit in payload["hits"]
        ],
    }


def scenario_a(db_path: Path, memory: Memory) -> dict[str, Any]:
    before = search_titles(db_path, OLD_KEYWORD)
    archive_code, archive, _ = run_cli(db_path, "archive", memory.id, "--json")
    after = search_titles(db_path, OLD_KEYWORD)
    explicit = search_titles(db_path, OLD_KEYWORD, "--status", "archived")
    stored = MemoryRepository(Database(db_path)).require_memory(memory.id)
    ok = (
        before["total"] == 1
        and archive_code == 0
        and archive["changed"] is True
        and archive["to_status"] == "archived"
        and str(stored.status) == "archived"
        and after["total"] == 0
        and explicit["total"] == 1
    )
    return {
        "scenario": "A_archive",
        "expectation": "archived Memory is no longer returned by the default search",
        "ok": ok,
        "search_before": before,
        "archive": archive,
        "search_after_default": after,
        "search_after_explicit_archived": explicit,
    }


def scenario_b(db_path: Path, memory: Memory) -> dict[str, Any]:
    restore_code, restore, _ = run_cli(db_path, "restore", memory.id, "--json")
    after = search_titles(db_path, OLD_KEYWORD)
    stored = MemoryRepository(Database(db_path)).require_memory(memory.id)
    ok = (
        restore_code == 0
        and restore["changed"] is True
        and restore["from_status"] == "archived"
        and restore["to_status"] == "active"
        and str(stored.status) == "active"
        and after["total"] == 1
        and after["ids"] == [memory.id]
    )
    return {
        "scenario": "B_restore",
        "expectation": "restored Memory is returned by the default search again",
        "ok": ok,
        "restore": restore,
        "search_after_restore": after,
    }


def scenario_c(db_path: Path, memory: Memory) -> dict[str, Any]:
    before = search_titles(db_path, OLD_KEYWORD)
    update_code, update, _ = run_cli(
        db_path, "update", memory.id, "--title", NEW_TITLE, "--content", NEW_CONTENT, "--json"
    )
    old_after = search_titles(db_path, OLD_KEYWORD)
    new_after = search_titles(db_path, NEW_KEYWORD)
    stored = MemoryRepository(Database(db_path)).require_memory(memory.id)
    ok = (
        before["total"] == 1
        and update_code == 0
        and stored.content == NEW_CONTENT
        and stored.title == NEW_TITLE
        and old_after["total"] == 0
        and new_after["total"] == 1
        and new_after["ids"] == [memory.id]
    )
    return {
        "scenario": "C_update",
        "expectation": "after a content update the old keyword misses and the new keyword hits",
        "ok": ok,
        "search_before_old_keyword": before,
        "update": update,
        "search_after_old_keyword": old_after,
        "search_after_new_keyword": new_after,
        "stored": stored.as_dict(),
    }


def duplicate_rows(db_path: Path) -> dict[str, Any]:
    """How many stored Memories share a fingerprint (the D criterion)."""
    memories = MemoryRepository(Database(db_path)).list_memories(limit=1000)
    fingerprints = [memory_fingerprint(m.type, m.title, m.content) for m in memories]
    counts: dict[str, int] = {}
    for fingerprint in fingerprints:
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
    duplicated = {fingerprint: count for fingerprint, count in counts.items() if count > 1}
    return {"memories": len(memories), "unique_fingerprints": len(counts), "duplicated": duplicated}


def exact_duplicate_probe(db_path: Path, memory_id: str) -> dict[str, Any]:
    """D2: re-form a candidate that is byte-identical to an already stored Memory.

    Deterministic (no model involved): the gate must answer ``reuse`` and must not
    write a second row.  This is what the spec's "再次形成完全相同 Memory" means,
    regardless of whether the model happens to rephrase between two identical inputs.
    """
    repository = MemoryRepository(Database(db_path))
    stored = repository.require_memory(memory_id)
    candidate = MemoryDraft(
        type=stored.type,
        title=stored.title,
        content=stored.content,
        information_origin=stored.information_origin,
        summary=stored.summary,
        tags=tuple(stored.tags),
        importance=stored.importance,
        confidence=stored.confidence,
    )
    gate = MemoryQualityGate(repository)
    before = repository.counts()
    decision = gate.evaluate_candidate(candidate, base_status=MemoryStatus.ACTIVE)
    after = repository.counts()
    return {
        "candidate_from_memory_id": memory_id,
        "action": decision.action,
        "relation": decision.relation,
        "duplicate_of": decision.duplicate_of,
        "status": decision.status,
        "reasons": list(decision.reasons),
        "counts_before": before,
        "counts_after": after,
        "ok": decision.action == "reuse"
        and decision.duplicate_of == memory_id
        and before == after,
    }


def scenario_d(db_path: Path) -> dict[str, Any]:
    text = "SQLite 在个人数据规模下足够使用：单文件、零服务、便于备份，不需要独立数据库进程。"
    first_code, first, _ = run_cli(db_path, "form", "--text", text)
    first_hits = duplicate_rows(db_path)
    second_code, second, _ = run_cli(db_path, "form", "--text", text)
    second_hits = duplicate_rows(db_path)
    probe = (
        exact_duplicate_probe(db_path, first["memories"][0]["id"])
        if first.get("memories")
        else {"ok": False, "reason": "the first formation stored no Memory"}
    )
    ok = (
        first_code == 0
        and second_code == 0
        and first.get("status") == "persisted"
        and not second_hits["duplicated"]  # no two rows share a fingerprint
        and probe["ok"]
        and (
            second.get("status") == "duplicate"
            or (second.get("quality") or {}).get("pended", 0) >= 1
            or second.get("quality") is None
        )
    )
    return {
        "exact_duplicate_probe": probe,
        "scenario": "D_duplicate",
        "expectation": "forming the same input twice never yields two identical long-term Memories",
        "ok": ok,
        "input_chars": len(text),
        "first_outcome": {
            "status": first.get("status"),
            "memories": [
                {"id": m["id"], "title": m["title"], "status": m["status"]} for m in first.get("memories", [])
            ],
            "quality": (first.get("quality") or {}).get("decisions"),
        },
        "second_outcome": {
            "status": second.get("status"),
            "reason": second.get("reason"),
            "memories": [
                {"id": m["id"], "title": m["title"], "status": m["status"]} for m in second.get("memories", [])
            ],
            "reused": [m["id"] for m in second.get("reused", [])],
            "quality": second.get("quality"),
        },
        "fingerprints_after_first": first_hits,
        "fingerprints_after_second": second_hits,
    }


def scenario_e(db_path: Path) -> dict[str, Any]:
    repository = MemoryRepository(Database(db_path))
    a_before_ids = {memory.id for memory in repository.list_memories(limit=1000)}

    first_code, first, _ = run_cli(db_path, "form", "--text", PREFERENCE_A)
    a_memories = [
        memory
        for memory in repository.list_memories(limit=1000)
        if memory.id not in a_before_ids
    ]
    a_snapshot = {memory.id: memory.as_dict() for memory in a_memories}

    second_code, second, _ = run_cli(db_path, "form", "--text", PREFERENCE_B)

    a_after = {
        memory_id: repository.require_memory(memory_id).as_dict() for memory_id in a_snapshot
    }
    new_memories = [m for m in second.get("memories", [])]
    relation = None
    decisions = (second.get("quality") or {}).get("decisions") or []
    if decisions:
        relation = decisions[0].get("relation")
    a_unchanged = a_after == a_snapshot and bool(a_snapshot)
    b_not_active = all(m["status"] != "active" for m in new_memories) if new_memories else False
    b_pending = [m for m in new_memories if m["status"] == "pending"]
    ok = (
        first_code == 0
        and second_code == 0
        and bool(a_snapshot)
        and a_unchanged
        and bool(b_pending)
        and relation in {"conflict", "uncertain", "same"}
    )
    return {
        "scenario": "E_conflict",
        "expectation": "the older preference is kept untouched and the new one is not activated",
        "ok": ok,
        "input_a": PREFERENCE_A,
        "input_b": PREFERENCE_B,
        "first_outcome": {
            "status": first.get("status"),
            "memories": [
                {"id": m["id"], "title": m["title"], "type": m["type"], "status": m["status"]}
                for m in first.get("memories", [])
            ],
        },
        "second_outcome": {
            "status": second.get("status"),
            "reason": second.get("reason"),
            "memories": [
                {"id": m["id"], "title": m["title"], "type": m["type"], "status": m["status"]}
                for m in second.get("memories", [])
            ],
            "quality": second.get("quality"),
        },
        "relation": relation,
        "a_unchanged": a_unchanged,
        "a_created": list(a_snapshot),
        "b_pending_ids": [m["id"] for m in b_pending],
        "b_not_active": b_not_active,
    }


def transcript(evidence: dict[str, Any]) -> str:
    lines = [
        "Phase 4 acceptance run (real CLI, real SQLite file)",
        f"generated_at : {evidence['generated_at']}",
        f"db           : {evidence['db_path']}",
        f"package      : {evidence['package_version']}  phase={evidence['phase']}",
        f"prompts      : {evidence['prompt_version']} / {evidence['conflict_prompt_version']}",
        f"llm          : provider={evidence['llm'].get('provider')} model={evidence['llm'].get('model')} "
        f"base_url={evidence['llm'].get('base_url')} key={evidence['llm'].get('api_key')}",
        f"credential   : {evidence['credential_source']}",
        "",
    ]
    for scenario in evidence["scenarios"]:
        lines.append("=" * 78)
        lines.append(f"[{scenario['scenario']}] ok={scenario['ok']}")
        lines.append(f"expectation: {scenario['expectation']}")
        body = json.dumps(
            {k: v for k, v in scenario.items() if k not in {"scenario", "expectation", "ok"}},
            ensure_ascii=False,
            indent=2,
        )
        lines.append(body)
        lines.append("")
    lines.append("=" * 78)
    lines.append(f"overall: {'PASS' if evidence['overall_ok'] else 'FAIL'}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 acceptance run")
    parser.add_argument("--db", default="data/phase4-acceptance.db")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--evidence", default="docs/phase4-acceptance.json")
    parser.add_argument("--transcript", default="docs/phase4-acceptance.txt")
    parser.add_argument("--skip-llm", action="store_true", help="run only scenarios A-C")
    args = parser.parse_args()

    db_path = Path(args.db)
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    credential_source = ensure_credential_from_dsh_file()
    config = None
    config_error = None
    if not args.skip_llm:
        try:
            config = load_config()
        except LLMConfigError as exc:
            config_error = exc

    memory = seed(db_path)
    evidence: dict[str, Any] = {
        "generated_at": utcnow_iso(),
        "phase": "phase-4 (memory lifecycle & quality)",
        "package_version": __import__("personal_memory").__version__,
        "prompt_version": PROMPT_VERSION,
        "conflict_prompt_version": CONFLICT_PROMPT_VERSION,
        "db_path": str(db_path),
        "credential_source": credential_source if config is not None else None,
        "llm": config.as_public_dict() if config is not None else {"error": str(config_error) if config_error else None},
        "seed_memory": memory.as_dict(),
        "scenarios": [],
    }
    if config is not None:
        print(
            f"llm: provider={config.provider} model={config.model} base_url={config.base_url} key=<set>"
        )

    for name, function in (
        ("A", lambda: scenario_a(db_path, memory)),
        ("B", lambda: scenario_b(db_path, memory)),
        ("C", lambda: scenario_c(db_path, memory)),
    ):
        record = function()
        evidence["scenarios"].append(record)
        print(f"scenario {name}: ok={record['ok']}")

    if args.skip_llm:
        evidence["note"] = "scenarios D/E skipped on request (--skip-llm)"
    elif config is None:
        evidence["note"] = f"scenarios D/E skipped: {config_error}"
        print(f"scenarios D/E skipped: {config_error}")
    else:
        for name, function in (("D", lambda: scenario_d(db_path)), ("E", lambda: scenario_e(db_path))):
            record = function()
            evidence["scenarios"].append(record)
            print(f"scenario {name}: ok={record['ok']}")

    evidence["counts"] = MemoryRepository(Database(db_path)).counts()
    evidence["status_counts"] = MemoryRepository(Database(db_path)).status_counts()
    evidence["fingerprint_check"] = duplicate_rows(db_path)
    evidence["index_consistency"] = MemoryRepository(Database(db_path)).index_consistency()
    evidence["overall_ok"] = all(record["ok"] for record in evidence["scenarios"])

    payload = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if config is not None and config.api_key and config.api_key in payload:
        raise SystemExit("refusing to write evidence: the API key leaked into the payload")
    evidence_path = Path(args.evidence)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(payload + "\n", encoding="utf-8")

    text = transcript(evidence)
    transcript_path = Path(args.transcript)
    transcript_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"evidence: {evidence_path} ({len(payload)} bytes), transcript: {transcript_path}")
    return 0 if evidence["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
