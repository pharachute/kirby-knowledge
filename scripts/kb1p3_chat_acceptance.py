"""KB 1.0 Phase 3 acceptance: the spec's §十八 checks through the real CLI.

    A. agent-chat.txt  -> import-chat -> Capture -> real LLM -> Memory
       (>=1 valuable Memory; Source type "chat"; Source still shows the role boundaries;
        the Memory is found again by a NEW process through Phase 3 retrieval)
    B. small-talk.txt  -> import-chat -> Formation says worthless -> Memory = 0, Source = 0
    C. provider-neutral JSON -> the same normalised conversation and role-marked text
       (no model call at all)
    D. a conversation over the limit fails explicitly -- never a silent truncation
    E. privacy: the CLI's default JSON output contains no message body

Only A and B use the real LLM (the chats are fictional, so nothing sensitive is sent).
The credential is read into THIS process's environment and is never printed or written
to evidence; the evidence file is checked to contain no conversation body.

Run::

    python scripts/kb1p3_chat_acceptance.py --reset
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
    ChatImporter,
    Database,
    MemoryRepository,
    SourceType,
    utcnow_iso,
)

DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"

ROLE_TEXT_CHAT = """[USER]
我最近开始系统学习 Agent，希望理解底层原理，而不是只会调用现成框架。

[ASSISTANT]
可以按三条线推进：先读一份最小实现，再自己写一个只支持工具调用的循环，最后补上记忆与规划。

[USER]
那就按这个顺序来，我每周至少投入十小时。
"""

KNOWLEDGE_CHAT = """[USER]
帮我梳理一下向量检索和重排序的分工。

[ASSISTANT]
向量检索先用嵌入模型把文本编码为向量，再按相似度取回候选片段；重排序（rerank）随后用更强的模型对候选重新打分，
通常能在不改变索引的前提下提升前几条结果的质量。两者是流水线关系，而不是互相替代。
"""

LOW_VALUE_CHAT = """[USER]
哈哈。

[ASSISTANT]
哈哈。
"""

JSON_CHAT = {
    "conversation_id": "conv-accept-001",
    "title": "RAG 讨论",
    "provider": "fictional-exporter",
    "messages": [
        {"role": "user", "content": "RAG 是什么？", "timestamp": "2026-10-04T09:00:00Z"},
        {"role": "assistant", "content": "RAG 是检索增强生成。", "timestamp": "2026-10-04T09:00:03Z"},
    ],
}

#: A sentence that must never appear in default output or in the evidence file.  It sits
#: in the assistant message, so the §九 title (first user message) cannot legitimately
#: contain it.
PROBE_SENTENCE = "先读一份最小实现"
ROLE_MARKERS = ("[USER]", "[ASSISTANT]", "[SYSTEM]", "[TOOL]", "[DEVELOPER]")


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
        "exit_code": completed.returncode,
        "payload": payload,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def counts(db_path: Path) -> dict[str, int]:
    return MemoryRepository(Database(db_path)).counts()


def _source_probe(db_path: Path, source_id: str) -> dict[str, Any]:
    """Read a stored Source through the repository and report markers only (never the body)."""
    repository = MemoryRepository(Database(db_path))
    stored = repository.require_source(source_id)
    markers = [line.strip() for line in stored.content.splitlines() if line.strip() in ROLE_MARKERS]
    return {
        "id": stored.id,
        "source_type": str(stored.source_type),
        "content_chars": len(stored.content),
        "role_marker_sequence": markers,
        "metadata": dict(stored.metadata),
    }


def scenario_a_high_value(db_path: Path, fixture: Path) -> dict[str, Any]:
    """Default Formation policy: the Source is kept only when Formation needs it."""
    before = counts(db_path)
    record = run_cli(db_path, "import-chat", str(fixture), "--json")
    payload = record["payload"] or {}
    after = counts(db_path)
    memories = payload.get("memories_created", [])
    sources = payload.get("sources_created", [])
    conversation_view = json.dumps(payload.get("conversation", {}), ensure_ascii=False)
    body_in_conversation_view = PROBE_SENTENCE in conversation_view

    ok = (
        record["exit_code"] == 0
        and payload.get("formation_status") == "persisted"
        and len(memories) >= 1
        and after["memories"] > before["memories"]
        and not body_in_conversation_view
        and all("content" not in message for message in payload.get("conversation", {}).get("messages", []))
        and (
            not sources  # legitimate: every memory was self-contained (Phase 2 policy)
            or _source_probe(db_path, sources[0]["id"])["source_type"] == "chat"
        )
    )
    return {
        "scenario": "A_high_value_chat_default_policy",
        "expectation": "chat -> import-chat -> Capture -> real LLM -> Memory (>=1 valuable Memory)",
        "ok": ok,
        "exit_code": record["exit_code"],
        "title_chars": len(payload.get("title") or ""),
        "title_source": payload.get("title_source"),
        "title_is_first_user_message_excerpt": (payload.get("title") or "").startswith("我最近开始系统学习 Agent"),
        "provider": payload.get("provider"),
        "provider_known": payload.get("provider_known"),
        "message_count": payload.get("message_count"),
        "import_status": payload.get("import_status"),
        "formation_status": payload.get("formation_status"),
        "memory_count": payload.get("memory_count"),
        "memories_created": [
            {"id": m["id"], "type": m["type"], "title": m["title"], "status": m["status"],
             "information_origin": m["information_origin"]}
            for m in memories
        ],
        "source_count": payload.get("source_count"),
        "source_probe": (_source_probe(db_path, sources[0]["id"]) if sources else None),
        "body_sentence_in_conversation_view": body_in_conversation_view,
        "counts_before": before,
        "counts_after": after,
    }


def scenario_a2_source_kept(db_path: Path, fixture: Path) -> dict[str, Any]:
    """`--keep-source always`: the raw chat is kept as a Source (spec §十八 items 3 and 4).

    Formation still makes the value judgment with the real model; only the Source policy
    is forced, because with the default policy a chat whose memories are self-contained
    legitimately produces no Source row at all (observed in scenario A).
    """
    before = counts(db_path)
    record = run_cli(
        db_path, "import-chat", str(fixture), "--keep-source", "always", "--json"
    )
    payload = record["payload"] or {}
    after = counts(db_path)
    sources = payload.get("sources_created", [])
    probe = _source_probe(db_path, sources[0]["id"]) if sources else None
    ok = (
        record["exit_code"] == 0
        and probe is not None
        and probe["source_type"] == "chat"
        and probe["role_marker_sequence"] == ["[USER]", "[ASSISTANT]"]
        and after["sources"] == before["sources"] + 1
    )
    return {
        "scenario": "A2_source_kept_with_keep_source_always",
        "expectation": "with keep_source=always the chat Source exists, typed chat, with role boundaries",
        "ok": ok,
        "exit_code": record["exit_code"],
        "formation_status": payload.get("formation_status"),
        "memory_count": payload.get("memory_count"),
        "source_probe": probe,
        "counts_before": before,
        "counts_after": after,
    }


def scenario_b_low_value(db_path: Path, fixture: Path) -> dict[str, Any]:
    before = counts(db_path)
    record = run_cli(db_path, "import-chat", str(fixture), "--json")
    payload = record["payload"] or {}
    after = counts(db_path)
    ok = (
        record["exit_code"] == 0
        and payload.get("status") == "skipped"
        and payload.get("memory_count") == 0
        and payload.get("source_count") == 0
        and after == before
    )
    return {
        "scenario": "B_low_value_chat",
        "expectation": "worthless chat -> Memory = 0, Source = 0, no write",
        "ok": ok,
        "exit_code": record["exit_code"],
        "status": payload.get("status"),
        "memory_count": payload.get("memory_count"),
        "source_count": payload.get("source_count"),
        "source_reused": payload.get("source_reused"),
        "counts_before": before,
        "counts_after": after,
    }


def scenario_c_json_format(fixture: Path) -> dict[str, Any]:
    """Model-free: the provider-neutral JSON becomes the same normalised conversation."""
    conversation = ChatImporter().load(fixture)
    request = conversation.to_capture_request()
    rendered = conversation.render_role_text()
    markers = [line for line in rendered.splitlines() if line in ROLE_MARKERS]
    ok = (
        conversation.conversation_id == "conv-accept-001"
        and conversation.title == "RAG 讨论"
        and conversation.provider == "fictional-exporter"
        and conversation.message_count == 2
        and conversation.started_at == "2026-10-04T09:00:00Z"
        and conversation.ended_at == "2026-10-04T09:00:03Z"
        and str(request.source_type) == str(SourceType.CHAT)
        and request.metadata["conversation_id"] == "conv-accept-001"
        and request.metadata["provider"] == "fictional-exporter"
        and markers == ["[USER]", "[ASSISTANT]"]
    )
    return {
        "scenario": "C_json_format",
        "expectation": "provider-neutral JSON -> same ChatConversation / CaptureRequest (no model call)",
        "ok": ok,
        "conversation_id": conversation.conversation_id,
        "title": conversation.title,
        "provider": conversation.provider,
        "message_count": conversation.message_count,
        "roles": [str(role) for role in conversation.roles()],
        "started_at": conversation.started_at,
        "ended_at": conversation.ended_at,
        "role_marker_sequence": markers,
        "source_type": str(request.source_type),
        "capture_metadata": {k: v for k, v in request.metadata.items() if v is not None},
        "rendered_chars": len(rendered),
    }


def scenario_d_long_conversation(db_path: Path, fixture: Path) -> dict[str, Any]:
    before = counts(db_path)
    record = run_cli(db_path, "import-chat", str(fixture), "--max-bytes", "400", "--json")
    payload = record["payload"] or {}
    after = counts(db_path)
    ok = record["exit_code"] == 3 and payload.get("error_type") == "FileTooLargeError" and after == before
    return {
        "scenario": "D_long_conversation_fails_explicitly",
        "expectation": "over the limit -> typed failure, never a silent truncation, no write",
        "ok": ok,
        "exit_code": record["exit_code"],
        "error_type": payload.get("error_type"),
        "error": payload.get("error"),
        "fixture_bytes": fixture.stat().st_size,
        "counts_before": before,
        "counts_after": after,
    }


def scenario_e_privacy_chat_probe(fixture: Path) -> dict[str, Any]:
    """Model-free: default views of a loaded conversation contain no message characters."""
    conversation = ChatImporter().load(fixture)
    default_view = json.dumps(conversation.as_dict(), ensure_ascii=False)
    preview_view = json.dumps(conversation.as_dict(include_preview=True), ensure_ascii=False)
    content_view = json.dumps(conversation.as_dict(include_content=True), ensure_ascii=False)
    default_messages = conversation.as_dict()["messages"]
    ok = bool(
        PROBE_SENTENCE not in default_view
        and PROBE_SENTENCE in preview_view
        and PROBE_SENTENCE in content_view
        and all(
            "content" not in message and "content_preview" not in message
            for message in default_messages
        )
    )
    return {
        "scenario": "E_privacy_default_view",
        "expectation": "default views carry no message text; preview/content are explicit opt-ins",
        "ok": ok,
        "probe_sentence_in_default_view": PROBE_SENTENCE in default_view,
        "probe_sentence_in_preview_view": PROBE_SENTENCE in preview_view,
        "probe_sentence_in_content_view": PROBE_SENTENCE in content_view,
        "default_message_keys": sorted(default_messages[0]),
        "privacy_note": "chat text is sent to the configured model service during real Memory Formation; "
        "local SQLite storage does not mean local inference",
    }


def scenario_f_new_process_retrieval(db_path: Path, memory_ids: list[str]) -> dict[str, Any]:
    record = run_cli(db_path, "search", "Agent", "--status", "all", "--limit", "10", "--json")
    payload = record["payload"] or {}
    hit_ids = [hit["memory"]["id"] for hit in payload.get("hits", [])]
    ok = record["exit_code"] == 0 and any(memory_id in hit_ids for memory_id in memory_ids)
    return {
        "scenario": "F_new_process_retrieval",
        "expectation": "a NEW process finds the chat-derived Memory through Phase 3 retrieval",
        "ok": ok,
        "exit_code": record["exit_code"],
        "search_total": payload.get("total"),
        "hit_ids": hit_ids,
        "chat_memory_ids": memory_ids,
        "command_output_chars": len(record["stdout"]),
    }


def transcript(evidence: dict[str, Any]) -> str:
    lines = [
        "Knowledge Base 1.0 -- Phase 3 (Chat Importer) acceptance run",
        f"generated_at : {evidence['generated_at']}",
        f"db           : {evidence['db_path']}",
        f"package      : {evidence['package_version']}  phase={evidence['phase']}",
        f"credential   : {evidence['credential_source']}",
        f"fixtures     : {evidence['fixture_dir']} (fictional chats)",
        "model calls  : A, A2 and B only; C/D/E/F call no model",
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
    lines.append(f"conversation body in evidence: {evidence['body_in_evidence']}")
    lines.append(f"overall: {'PASS' if evidence['overall_ok'] else 'FAIL'}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="KB 1.0 Phase 3 chat import acceptance")
    parser.add_argument("--db", default="data/kb1p3-chat.db")
    parser.add_argument("--fixtures", default="data/kb1p3-fixtures")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--evidence", default="docs/kb1p3-chat-acceptance.json")
    parser.add_argument("--transcript", default="docs/kb1p3-chat-acceptance.txt")
    parser.add_argument("--skip-llm", action="store_true", help="run only the model-free scenarios")
    args = parser.parse_args()

    db_path = Path(args.db)
    fixture_dir = Path(args.fixtures)
    fixture_dir.mkdir(parents=True, exist_ok=True)
    chat_fixture = fixture_dir / "agent-chat.txt"
    small_fixture = fixture_dir / "small-talk.txt"
    json_fixture = fixture_dir / "conv.json"
    knowledge_fixture = fixture_dir / "knowledge-chat.txt"
    long_fixture = fixture_dir / "long-chat.txt"
    chat_fixture.write_text(ROLE_TEXT_CHAT, encoding="utf-8")
    knowledge_fixture.write_text(KNOWLEDGE_CHAT, encoding="utf-8")
    small_fixture.write_text(LOW_VALUE_CHAT, encoding="utf-8")
    json_fixture.write_text(json.dumps(JSON_CHAT, ensure_ascii=False, indent=2), encoding="utf-8")
    long_fixture.write_text(
        "[USER]\n" + ("这是一段很长的对话内容。" * 200) + "\n\n[ASSISTANT]\n最后一句。\n", encoding="utf-8"
    )

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
        "phase": "kb-1.0 phase-3 (chat importer)",
        "package_version": __import__("personal_memory").__version__,
        "db_path": str(db_path),
        "fixture_dir": str(fixture_dir),
        "credential_source": credential_source if has_credential else None,
        "scenarios": [],
    }

    memory_ids: list[str] = []
    if args.skip_llm or not has_credential:
        evidence["note"] = "scenarios A/A2/B/F skipped (--skip-llm or no credential available)"
    else:
        evidence["scenarios"].append(scenario_a_high_value(db_path, chat_fixture))
        print(f"scenario A: ok={evidence['scenarios'][-1]['ok']}", flush=True)
        memory_ids = [m["id"] for m in evidence["scenarios"][-1]["memories_created"]]
        evidence["scenarios"].append(scenario_a2_source_kept(db_path, knowledge_fixture))
        print(f"scenario A2: ok={evidence['scenarios'][-1]['ok']}", flush=True)
        evidence["scenarios"].append(scenario_b_low_value(db_path, small_fixture))
        print(f"scenario B: ok={evidence['scenarios'][-1]['ok']}", flush=True)

    evidence["scenarios"].append(scenario_c_json_format(json_fixture))
    print(f"scenario C: ok={evidence['scenarios'][-1]['ok']}", flush=True)
    evidence["scenarios"].append(scenario_d_long_conversation(db_path, long_fixture))
    print(f"scenario D: ok={evidence['scenarios'][-1]['ok']}", flush=True)
    evidence["scenarios"].append(scenario_e_privacy_chat_probe(chat_fixture))
    print(f"scenario E: ok={evidence['scenarios'][-1]['ok']}", flush=True)
    if memory_ids:
        evidence["scenarios"].append(scenario_f_new_process_retrieval(db_path, memory_ids))
        print(f"scenario F: ok={evidence['scenarios'][-1]['ok']}", flush=True)

    repository = MemoryRepository(Database(db_path))
    evidence["final_counts"] = repository.counts()
    evidence["final_status_counts"] = repository.status_counts()
    evidence["index_consistency"] = repository.index_consistency()
    evidence["overall_ok"] = all(scenario["ok"] for scenario in evidence["scenarios"])

    payload = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    api_key = os.environ.get("PERSONAL_MEMORY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    if api_key and api_key in payload:
        raise SystemExit("refusing to write evidence: the API key leaked into the payload")
    # privacy: the evidence must not quote the conversation either
    leaked = [sentence for sentence in (PROBE_SENTENCE, "我每周至少投入十小时", "最小实现") if sentence in payload]
    evidence["body_in_evidence"] = leaked
    if leaked:
        raise SystemExit(f"refusing to write evidence: conversation text leaked ({leaked})")
    payload = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str)

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
