"""KB 1.0 MVP (local Web UI) acceptance: the spec's §二十一 scenarios over real HTTP.

The **real** server process is started exactly as a user starts it::

    python -m personal_memory web --db data/ui-acceptance.db --port <free port>

and every scenario is then driven over HTTP against that process:

    0  startup/binding : serves 127.0.0.1, refuses connections on the LAN IP, prints banner
    A  text capture    : Memory created (+ Source when Formation requires it)
    B  low-value text  : Memory = 0, Source = 0, database does not grow
    C  .md upload      : FileImporter -> Capture -> Formation -> Memory
    D  chat upload     : ChatImporter -> Capture -> Formation -> Memory + information_origin
    E  search "RAG"    : the Memory from A is found through the UI
    F  lifecycle       : Edit (new content searchable) -> Archive (gone from default search)
                         -> Restore (back) -> Delete (Memory gone, Source kept)
    G  privacy         : no API key / chat body in any response; privacy notice shown

A/B/C/D use the real LLM; E/F/G call no model.  The credential is read into this
process's environment only and is never printed or written to evidence; the evidence
records counts, ids and boolean marker checks instead of page bodies.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from personal_memory import Database, MemoryRepository, utcnow_iso  # noqa: E402

DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"
ROLE_MARKERS = ("[USER]", "[ASSISTANT]", "[SYSTEM]", "[TOOL]", "[DEVELOPER]")

MARKDOWN_FIXTURE = """# 向量检索与重排序

向量检索先用嵌入模型把文本编码为向量，再按相似度取回候选片段。

- 重排序（rerank）随后用更强的模型对候选重新打分
- 两者是流水线关系，而不是互相替代
"""

CHAT_FIXTURE = """[USER]
我在系统学习检索系统，希望理解底层原理。

[ASSISTANT]
向量检索负责召回，重排序负责精排；先保证召回率，再优化排序质量。

[USER]
那就按这个顺序实践。
"""

#: must never appear in default output or evidence.  It sits in the assistant message, so
#: the §九 title (first user message) cannot legitimately contain it.
PROBE_SENTENCE = "先保证召回率，再优化排序质量"

#: a unique keyword injected by the Edit step, so the lifecycle checks do not depend on
#: whatever wording the model chose for the Memory
EDIT_KEYWORD = "星河检索标记"


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


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Ui:
    """Minimal HTTP client for the running server (stdlib urllib)."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.exchanges: list[dict[str, Any]] = []
        self.bodies: list[str] = []
        self.responses: list[tuple[str, str]] = []

    def _record(self, method: str, path: str, status: int, body: str) -> str:
        self.exchanges.append({"method": method, "path": path, "status": status, "chars": len(body)})
        self.bodies.append(body)
        self.responses.append((path, body))
        return body

    def get(self, path: str) -> tuple[int, str]:
        url = self.base + urllib.parse.quote(path, safe="/?=&%+")
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                return response.status, self._record("GET", path, response.status, response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, self._record("GET", path, error.code, error.read().decode("utf-8"))

    def post(self, path: str, data: dict[str, str]) -> tuple[int, str]:
        body = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.status, self._record("POST", path, response.status, response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, self._record("POST", path, error.code, error.read().decode("utf-8"))


def repository_for(db_path: Path) -> MemoryRepository:
    return MemoryRepository(Database(db_path))


def counts(db_path: Path) -> dict[str, int]:
    return repository_for(db_path).counts()


class ServerProcess:
    """The real `python -m personal_memory web` process, with its stdout captured."""

    def __init__(self, db_path: Path, port: int) -> None:
        self.port = port
        self.lines: list[str] = []
        self.process = subprocess.Popen(
            [sys.executable, "-m", "personal_memory", "web", "--db", str(db_path), "--port", str(port)],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            self.lines.append(line.rstrip("\n"))

    def wait_ready(self, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=2) as response:
                    if response.status == 200:
                        time.sleep(0.2)  # let the banner flush
                        return True
            except Exception:
                time.sleep(0.3)
        return False

    @property
    def banner(self) -> list[str]:
        return [line for line in self.lines if line.strip()][:6]

    def stop(self) -> str:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.reader.join(timeout=5)
        return "\n".join(self.lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="KB 1.0 MVP web UI acceptance")
    parser.add_argument("--db", default="data/ui-acceptance.db")
    parser.add_argument("--fixtures", default="data/mvp-fixtures")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--evidence", default="docs/kb1-mvp-web-acceptance.json")
    parser.add_argument("--transcript", default="docs/kb1-mvp-web-acceptance.txt")
    parser.add_argument("--keep-server", action="store_true", help="leave the server running afterwards")
    args = parser.parse_args()

    db_path = Path(args.db)
    fixture_dir = Path(args.fixtures)
    fixture_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = fixture_dir / "vector-search.md"
    chat_path = fixture_dir / "retrieval-chat.txt"
    markdown_path.write_text(MARKDOWN_FIXTURE, encoding="utf-8")
    chat_path.write_text(CHAT_FIXTURE, encoding="utf-8")
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    credential = ensure_credential_from_dsh_file()
    if not credential:
        raise SystemExit("no credential available: set DEEPSEEK_API_KEY or PERSONAL_MEMORY_LLM_API_KEY")

    port = free_port()
    server = ServerProcess(db_path, port)
    ready = server.wait_ready()
    if not ready:
        server.stop()
        raise SystemExit("server did not become ready")
    ui = Ui(f"http://127.0.0.1:{port}")
    scenarios: list[dict[str, Any]] = []
    started_at = utcnow_iso()
    keep_running = args.keep_server

    try:
        # ---------------- 0: startup + binding + banner ----------------
        loopback_ok = ui.get("/")[0] == 200
        lan_ip: str | None = None
        lan_blocked: bool | None = None
        try:
            candidate = socket.gethostbyname(socket.gethostname())
            if not candidate.startswith("127."):
                lan_ip = candidate
                try:
                    with socket.create_connection((candidate, port), timeout=2):
                        lan_blocked = False
                except OSError:
                    lan_blocked = True
        except OSError:
            pass
        banner = server.banner
        scenarios.append(
            {
                "scenario": "0_startup_binding_banner",
                "expectation": "以 `python -m personal_memory web` 启动，打印 URL/数据库/隐私提示，只在 127.0.0.1 监听",
                "ok": ready and loopback_ok and lan_blocked is not False and any("Personal Knowledge Base" in line for line in banner),
                "command": f"python -m personal_memory web --db {db_path} --port {port}",
                "banner": banner,
                "loopback_http_ok": loopback_ok,
                "lan_ip": lan_ip,
                "lan_connection_blocked": lan_blocked,
            }
        )

        # ---------------- A: text capture ----------------
        before = counts(db_path)
        status, body = ui.post(
            "/capture", {"title": "", "content": "RAG 是检索增强生成，通过检索外部知识为模型提供上下文。"}
        )
        after = counts(db_path)
        repository = repository_for(db_path)
        delta = after["memories"] - before["memories"]
        created = repository.list_memories(limit=max(delta, 1))[:delta] if delta > 0 else []
        scenarios.append(
            {
                "scenario": "A_text_capture",
                "expectation": "Capture → Formation → 至少一条 Memory（Formation 判断需要时才有 Source）",
                "ok": status == 200
                and "处理完成" in body
                and delta >= 1
                and bool(created)
                and all(f"/memories/{m.id}" in body for m in created),
                "http_status": status,
                "page_marker_处理完成": "处理完成" in body,
                "counts_before": before,
                "counts_after": after,
                "memories_created": [
                    {
                        "id": m.id,
                        "type": str(m.type),
                        "title": m.title,
                        "status": str(m.status),
                        "information_origin": str(m.information_origin),
                    }
                    for m in created
                ],
                "sources_created": after["sources"] - before["sources"],
            }
        )

        # ---------------- B: low-value text ----------------
        before = counts(db_path)
        status, body = ui.post("/capture", {"title": "", "content": "今天喝了一杯奶茶。"})
        after = counts(db_path)
        scenarios.append(
            {
                "scenario": "B_low_value_text",
                "expectation": "Memory = 0, Source = 0，数据库数量不增长",
                "ok": status == 200 and after == before and "0 Memory / 0 Source" in body,
                "http_status": status,
                "counts_before": before,
                "counts_after": after,
                "status_line_present": "0 Memory / 0 Source" in body,
            }
        )

        # ---------------- C: markdown upload ----------------
        before = counts(db_path)
        status, body = ui.post(
            "/import-file",
            {
                "filename": markdown_path.name,
                "content_base64": base64.b64encode(markdown_path.read_bytes()).decode("ascii"),
                "title": "",
                "source_type": "file",
            },
        )
        after = counts(db_path)
        repository = repository_for(db_path)
        file_source = next(
            (s for s in repository.list_sources(limit=20) if s.metadata.get("filename") == markdown_path.name),
            None,
        )
        scenarios.append(
            {
                "scenario": "C_markdown_upload",
                "expectation": "浏览器上传 .md → FileImporter → Capture → Formation → Memory",
                "ok": status == 200
                and "文件导入完成" in body
                and after["memories"] > before["memories"]
                and file_source is not None
                and str(file_source.source_type) == "file"
                and file_source.metadata.get("encoding") == "utf-8"
                and bool(file_source.metadata.get("file_sha256")),
                "http_status": status,
                "counts_before": before,
                "counts_after": after,
                "memory_delta": after["memories"] - before["memories"],
                "source": (
                    {
                        "id": file_source.id,
                        "source_type": str(file_source.source_type),
                        "filename": file_source.metadata.get("filename"),
                        "extension": file_source.metadata.get("extension"),
                        "encoding": file_source.metadata.get("encoding"),
                        "file_sha256": file_source.metadata.get("file_sha256"),
                        "content_chars": len(file_source.content),
                    }
                    if file_source
                    else None
                ),
            }
        )

        # ---------------- D: chat upload ----------------
        before = counts(db_path)
        status, body = ui.post(
            "/import-chat",
            {
                "filename": chat_path.name,
                "content_base64": base64.b64encode(chat_path.read_bytes()).decode("ascii"),
                "format": "auto",
                "provider": "",
                "title": "",
                "keep_source": "always",
            },
        )
        after = counts(db_path)
        repository = repository_for(db_path)
        chat_source = next((s for s in repository.list_sources(limit=20) if str(s.source_type) == "chat"), None)
        markers = (
            [line.strip() for line in chat_source.content.splitlines() if line.strip() in ROLE_MARKERS]
            if chat_source
            else []
        )
        chat_delta = after["memories"] - before["memories"]
        chat_memories = repository.list_memories(limit=max(chat_delta, 1))[:chat_delta] if chat_delta > 0 else []
        scenarios.append(
            {
                "scenario": "D_chat_upload",
                "expectation": "聊天文件 → ChatImporter → Capture → Formation → Memory，并可见 information_origin",
                "ok": status == 200
                and "聊天导入完成" in body
                and chat_delta >= 1
                and chat_source is not None
                and markers == ["[USER]", "[ASSISTANT]", "[USER]"]
                and PROBE_SENTENCE not in body,
                "http_status": status,
                "counts_before": before,
                "counts_after": after,
                "chat_source": (
                    {
                        "id": chat_source.id,
                        "source_type": str(chat_source.source_type),
                        "role_marker_sequence": markers,
                        "content_chars": len(chat_source.content),
                        "metadata": {
                            k: chat_source.metadata.get(k)
                            for k in ("captured_from", "provider", "conversation_id", "message_count", "roles")
                        },
                    }
                    if chat_source
                    else None
                ),
                "information_origins": sorted({str(m.information_origin) for m in chat_memories}),
                "memory_titles": [m.title for m in chat_memories],
                "chat_body_in_page": PROBE_SENTENCE in body,
            }
        )

        # ---------------- E: search ----------------
        target = created[0] if created else None
        status, body = ui.get("/search?q=RAG")
        scenarios.append(
            {
                "scenario": "E_search",
                "expectation": "UI 搜索 RAG 找到场景 A 生成的 Memory",
                "ok": status == 200 and target is not None and f"/memories/{target.id}" in body,
                "http_status": status,
                "searched_for": "RAG",
                "memory_id_found": target.id if target else None,
                "memory_title": target.title if target else None,
                "score_rendered": "score=" in body,
            }
        )

        # ---------------- F: lifecycle (on a Memory that owns a Source) ----------------
        lifecycle_target = None
        if chat_memories and chat_source is not None:
            lifecycle_target = chat_memories[0]
        elif file_source is not None:
            derived = repository.list_memories(limit=50)
            lifecycle_target = derived[0] if derived else None
        steps: list[dict[str, Any]] = []
        if lifecycle_target is None:
            scenarios.append({"scenario": "F_lifecycle", "ok": False, "expectation": "找不到带 Source 的 Memory"})
        else:
            memory_id = lifecycle_target.id
            current = repository.require_memory(memory_id)
            source_ids = [s.id for s in repository.get_sources_for_memory(memory_id)]
            edit_status, _ = ui.post(
                f"/memories/{memory_id}/update",
                {
                    "title": current.title,
                    "content": f"{current.content}\n{EDIT_KEYWORD}：这一行用于验证编辑后的内容可以立即被检索到。",
                    "summary": current.summary or "",
                    "tags": "检索, 重排序",
                    "importance": str(current.importance),
                    "confidence": str(current.confidence),
                    "status": str(current.status),
                },
            )
            edited = repository.require_memory(memory_id)
            search_after_edit = ui.get(f"/search?q={EDIT_KEYWORD}")[1]
            steps.append(
                {
                    "step": "edit",
                    "http_status": edit_status,
                    "content_contains_keyword": EDIT_KEYWORD in edited.content,
                    "tags_in_db": edited.tags,
                    "new_content_searchable_immediately": f"/memories/{memory_id}" in search_after_edit,
                }
            )

            archive_status, _ = ui.post(f"/memories/{memory_id}/archive", {})
            archived = repository.require_memory(memory_id)
            steps.append(
                {
                    "step": "archive",
                    "http_status": archive_status,
                    "status_in_db": str(archived.status),
                    "hidden_from_default_list": f"/memories/{memory_id}" not in ui.get("/memories")[1],
                    "hidden_from_default_search": f"/memories/{memory_id}" not in ui.get(f"/search?q={EDIT_KEYWORD}")[1],
                    "visible_with_status_all": f"/memories/{memory_id}" in ui.get("/memories?status=all")[1],
                }
            )

            restore_status, _ = ui.post(f"/memories/{memory_id}/restore", {})
            restored = repository.require_memory(memory_id)
            steps.append(
                {
                    "step": "restore",
                    "http_status": restore_status,
                    "status_in_db": str(restored.status),
                    "visible_in_default_list": f"/memories/{memory_id}" in ui.get("/memories")[1],
                    "searchable_again": f"/memories/{memory_id}" in ui.get(f"/search?q={EDIT_KEYWORD}")[1],
                }
            )

            delete_status, _ = ui.post(f"/memories/{memory_id}/delete", {})
            after_delete = repository_for(db_path)
            steps.append(
                {
                    "step": "delete",
                    "http_status": delete_status,
                    "memory_gone_from_db": after_delete.get_memory(memory_id) is None,
                    "memory_gone_from_ui": f"/memories/{memory_id}" not in ui.get("/memories?status=all")[1],
                    "sources_kept": [sid for sid in source_ids if after_delete.get_source(sid) is not None],
                    "source_pages_still_ok": all(ui.get(f"/sources/{sid}")[0] == 200 for sid in source_ids),
                    "source_count_after_delete": after_delete.counts()["sources"],
                }
            )
            checks = {
                "edit": lambda s: s["http_status"] == 200 and s["new_content_searchable_immediately"]
                and s["content_contains_keyword"] and "检索" in s["tags_in_db"],
                "archive": lambda s: s["http_status"] == 200
                and s["status_in_db"] == "archived"
                and s["hidden_from_default_list"]
                and s["hidden_from_default_search"]
                and s["visible_with_status_all"],
                "restore": lambda s: s["http_status"] == 200
                and s["status_in_db"] == "active"
                and s["visible_in_default_list"]
                and s["searchable_again"],
                "delete": lambda s: s["http_status"] == 200
                and s["memory_gone_from_db"]
                and s["memory_gone_from_ui"]
                and len(s["sources_kept"]) == len(source_ids)
                and s["source_pages_still_ok"],
            }
            scenarios.append(
                {
                    "scenario": "F_lifecycle",
                    "expectation": "Edit 后新内容可立即搜索 → Archive 后从默认列表/搜索消失 → Restore 回来 → Delete 后 Memory 消失、Source 保留",
                    "ok": all(checks[step["step"]](step) for step in steps) and len(steps) == 4,
                    "target_memory": {
                        "id": memory_id,
                        "title": lifecycle_target.title,
                        "sources": source_ids,
                    },
                    "steps": steps,
                }
            )

        # ---------------- G: privacy ----------------
        # The invariant: the RAW conversation rendering must never come back from a generic
        # page.  A user who explicitly opens the persisted Source detail page does see the
        # saved raw material (spec §十一), and a Memory created by Formation may of course
        # quote phrases from the conversation -- both are recorded, not treated as leaks.
        api_key = os.environ.get("PERSONAL_MEMORY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
        raw_probe = chat_source.content[:40] if chat_source else None
        source_path = f"/sources/{chat_source.id}" if chat_source else None
        raw_block_paths = [path for path, body in ui.responses if raw_probe and raw_probe in body]
        probe_paths = [path for path, body in ui.responses if PROBE_SENTENCE in body]
        key_paths = [path for path, body in ui.responses if api_key and api_key in body]
        scenarios.append(
            {
                "scenario": "G_privacy",
                "expectation": "API Key 不出现在任何响应；原始对话渲染只出现在用户主动打开的 Source 详情页；页面明确提示内容会发送给模型服务",
                "ok": bool(api_key)
                and not key_paths
                and bool(raw_probe)
                and bool(raw_block_paths)
                and all(path == source_path for path in raw_block_paths)
                and "真实 Memory Formation" in ui.get("/")[1],
                "responses_checked": len(ui.bodies),
                "api_key_absent_from_all_responses": not key_paths,
                "raw_conversation_block_probe": raw_probe,
                "raw_conversation_block_paths": raw_block_paths,
                "raw_block_only_on_the_source_detail_page": all(path == source_path for path in raw_block_paths),
                "source_detail_page": source_path,
                "probe_phrase_paths_informational": probe_paths,
                "probe_phrase_note": "派生 Memory / Source 标题可能引用原话，属正常；被检查的是原始对话渲染本身",
                "privacy_notice_visible": "真实 Memory Formation" in ui.get("/")[1],
                "server_log_contains_key": bool(api_key) and any(api_key in line for line in server.lines),
                "server_log_contains_chat_body": any(PROBE_SENTENCE in line for line in server.lines),
                "server_log_lines_seen": len(server.lines),
            }
        )
    finally:
        server_log = "" if keep_running else server.stop()

    api_key = os.environ.get("PERSONAL_MEMORY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    evidence: dict[str, Any] = {
        "generated_at": utcnow_iso(),
        "started_at": started_at,
        "phase": "kb-1.0 mvp (local web ui)",
        "package_version": __import__("personal_memory").__version__,
        "db_path": str(db_path),
        "port": port,
        "server_command": f"python -m personal_memory web --db {db_path} --port {port}",
        "server_kept_running": keep_running,
        "server_log_lines": len(server_log.splitlines()),
        "server_log_contains_key": bool(api_key) and api_key in server_log,
        "server_log_contains_chat_body": PROBE_SENTENCE in server_log,
        "credential_source": credential,
        "scenarios": scenarios,
        "http_exchanges": ui.exchanges,
        "final_counts": counts(db_path),
        "overall_ok": all(scenario["ok"] for scenario in scenarios),
    }
    payload = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if api_key and api_key in payload:
        raise SystemExit("refusing to write evidence: the API key leaked")
    if PROBE_SENTENCE in payload:
        raise SystemExit("refusing to write evidence: chat body leaked")

    evidence_path = Path(args.evidence)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(payload + "\n", encoding="utf-8")

    lines = [
        "Knowledge Base 1.0 MVP (local Web UI) acceptance run",
        f"generated_at : {evidence['generated_at']}",
        f"command      : {evidence['server_command']}",
        f"package      : {evidence['package_version']}  phase={evidence['phase']}",
        f"credential   : {credential}",
        f"http calls   : {len(ui.exchanges)}   server log lines: {evidence['server_log_lines']}",
        "",
    ]
    for scenario in scenarios:
        lines.append(f"[{scenario['scenario']}] ok={scenario['ok']}")
        detail = {k: v for k, v in scenario.items() if k not in {"scenario", "ok"}}
        lines.append("  " + json.dumps(detail, ensure_ascii=False)[:1800])
        lines.append("")
    lines.append(f"final counts : {evidence['final_counts']}")
    lines.append(f"overall      : {'PASS' if evidence['overall_ok'] else 'FAIL'}")
    text = "\n".join(lines) + "\n"
    Path(args.transcript).write_text(text, encoding="utf-8")
    print(text)
    print(f"evidence: {evidence_path} ({len(payload)} bytes), transcript: {args.transcript}")
    return 0 if evidence["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
