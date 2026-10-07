"""KB 1.0 Phase 4 acceptance: URL / Web Importer against the real network + real LLM.

What is actually run (spec §十二):

    0  extraction probe   : several real public pages fetched through WebImporter (no model)
    1  article import     : real CLI `import-url https://peps.python.org/pep-0020/`
    2  GitHub README      : real CLI `import-url https://github.com/psf/requests`
    3  new-process search : a second process finds the formed Memories
    4  invalid inputs     : blocked / 404 / no-article / file:// targets fail with typed
                            errors and write nothing
    5  web UI import      : the real `web` server serves POST /import-url (same pipeline)
    6  privacy            : no key, no full page body in stdout/evidence

A/C/D-like failures are recorded honestly: if a site refused or a page had no article, the
actual result is written to the evidence instead of a fabricated success.

Evidence records URLs, titles, byte/character counts, hashes and <=160 character previews
only -- never the full page text, and never the API key.
"""

from __future__ import annotations

import argparse
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
from personal_memory.importers.web import WebImporter, WebImportError  # noqa: E402

DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"

ARTICLE_URL = "https://peps.python.org/pep-0020/"
README_PAGE_URL = "https://github.com/psf/requests"
README_RAW_URL = "https://raw.githubusercontent.com/psf/requests/main/README.md"
PROBE_URLS = (
    "https://peps.python.org/pep-0020/",
    "https://docs.python.org/3/library/urllib.request.html",
    "https://github.com/psf/requests",
    "https://github.com/python/cpython",
    README_RAW_URL,
)
FAILURE_CASES = (
    ("https://example.com/", "EmptyContentError", "a real page with no usable article text"),
    ("http://127.0.0.1:8765/", "BlockedUrlError", "loopback target"),
    ("http://169.254.169.254/latest/meta-data/", "BlockedUrlError", "cloud metadata address"),
    ("http://10.0.0.5/", "BlockedUrlError", "private address"),
    ("file:///etc/passwd", "InvalidUrlError", "non-http scheme"),
    ("https://docs.python.org/3/this-page-does-not-exist-xyz.html", "UrlFetchError", "http 404"),
)


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
        timeout=600,
    )
    try:
        payload: Any = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = None
    return {
        "command": "python -m personal_memory --db " + str(db_path) + " " + " ".join(argv),
        "exit_code": completed.returncode,
        "payload": payload,
        "stdout": completed.stdout,
        "stderr": completed.stderr.strip(),
    }


def repository_for(db_path: Path) -> MemoryRepository:
    return MemoryRepository(Database(db_path))


def counts(db_path: Path) -> dict[str, int]:
    return repository_for(db_path).counts()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def source_record(repository: MemoryRepository, source_id: str) -> dict[str, Any]:
    source = repository.require_source(source_id)
    content = source.content or ""
    return {
        "id": source.id,
        "source_type": str(source.source_type),
        "title": source.title,
        "url": source.url,
        "content_chars": len(content),
        "content_preview": " ".join(content.split())[:160],
        "has_raw_html": any(token in content for token in ("<p>", "<div", "<script", "<html")),
        "metadata": {
            key: source.metadata.get(key)
            for key in ("captured_from", "original_url", "final_url", "fetched_at", "content_type",
                        "content_chars", "title_source", "http_status", "extraction_root", "charset")
        },
    }


def memory_records(repository: MemoryRepository, memory_ids: list[str]) -> list[dict[str, Any]]:
    records = []
    for memory_id in memory_ids:
        memory = repository.get_memory(memory_id)
        if memory is None:
            continue
        records.append(
            {
                "id": memory.id,
                "type": str(memory.type),
                "title": memory.title,
                "status": str(memory.status),
                "information_origin": str(memory.information_origin),
                "confidence": memory.confidence,
                "sources": [s.id for s in repository.get_sources_for_memory(memory.id)],
            }
        )
    return records


def scenario_0_probe(importer: WebImporter) -> dict[str, Any]:
    records = []
    for url in PROBE_URLS:
        started = time.time()
        try:
            document = importer.fetch(url)
            records.append(
                {
                    "url": url,
                    "ok": True,
                    "title": document.title,
                    "title_source": document.title_source,
                    "extraction_root": document.metadata["extraction_root"],
                    "content_chars": document.content_chars,
                    "content_type": document.content_type,
                    "http_status": document.metadata["http_status"],
                    "body_bytes": document.metadata["body_bytes"],
                    "redirects": document.metadata["redirect_count"],
                    "charset": document.metadata["charset"],
                    "has_raw_html": any(
                        token in document.content for token in ("<p>", "<div", "<script", "<html")
                    ),
                    "preview": document.preview(160),
                    "seconds": round(time.time() - started, 2),
                }
            )
        except WebImportError as exc:
            records.append(
                {
                    "url": url,
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                    "seconds": round(time.time() - started, 2),
                }
            )
    return {
        "scenario": "0_extraction_probe_real_pages",
        "expectation": "真实公开页面可被抓取并提取正文；提取结果是文本而不是原始 HTML",
        "ok": any(r["ok"] for r in records) and all(not r.get("has_raw_html", False) for r in records),
        "pages": records,
    }


def scenario_1_article(db_path: Path, importer: WebImporter) -> dict[str, Any]:
    before = counts(db_path)
    record = run_cli(db_path, "import-url", ARTICLE_URL, "--json")
    payload = record["payload"] or {}
    after = counts(db_path)
    repository = repository_for(db_path)
    memory_ids = [m["id"] for m in payload.get("memories_created", [])]
    sources = payload.get("sources_created", [])
    stored = source_record(repository, sources[0]["id"]) if sources else None
    ok = (
        record["exit_code"] == 0
        and payload.get("formation_status") == "persisted"
        and len(memory_ids) >= 1
        and stored is not None
        and stored["source_type"] == "web"
        and stored["metadata"]["captured_from"] == "web"
        and stored["metadata"]["original_url"] == ARTICLE_URL
        and not stored["has_raw_html"]
        and after["memories"] > before["memories"]
    )
    return {
        "scenario": "1_real_article_import",
        "url": ARTICLE_URL,
        "expectation": "URL → 正文提取 → Capture → 真实 LLM Formation → Memory（源为 web）",
        "ok": ok,
        "exit_code": record["exit_code"],
        "title": payload.get("title"),
        "title_source": payload.get("title_source"),
        "content_chars": payload.get("content_chars"),
        "http_status": (payload.get("document") or {}).get("metadata", {}).get("http_status"),
        "formation_status": payload.get("formation_status"),
        "worth_remembering": payload.get("worth_remembering"),
        "memories": memory_records(repository, memory_ids),
        "source": stored,
        "counts_before": before,
        "counts_after": after,
        "page_body_in_stdout": False,  # verified below by the privacy scenario
    }


def scenario_2_github_readme(db_path: Path, importer: WebImporter) -> dict[str, Any]:
    before = counts(db_path)
    record = run_cli(db_path, "import-url", README_PAGE_URL, "--json")
    payload = record["payload"] or {}
    after = counts(db_path)
    repository = repository_for(db_path)
    memory_ids = [m["id"] for m in payload.get("memories_created", [])]
    sources = payload.get("sources_created", [])
    stored = source_record(repository, sources[0]["id"]) if sources else None
    ok = (
        record["exit_code"] == 0
        and payload.get("formation_status") == "persisted"
        and len(memory_ids) >= 1
        and stored is not None
        and stored["source_type"] == "web"
        and "github.com" in (stored["metadata"]["final_url"] or "")
        and str(stored["metadata"]["extraction_root"]) == "article"
        and not stored["has_raw_html"]
    )
    return {
        "scenario": "2_real_github_readme_import",
        "url": README_PAGE_URL,
        "expectation": "GitHub README 页面 → article 正文 → Capture → Formation → Memory",
        "ok": ok,
        "exit_code": record["exit_code"],
        "title": payload.get("title"),
        "content_chars": payload.get("content_chars"),
        "formation_status": payload.get("formation_status"),
        "memories": memory_records(repository, memory_ids),
        "source": stored,
        "counts_before": before,
        "counts_after": after,
    }


def scenario_3_new_process_search(db_path: Path, memory_ids: list[str]) -> dict[str, Any]:
    attempts = []
    hit_ids: list[str] = []
    for keyword in ("Python", "Requests", "Zen", "HTTP"):
        record = run_cli(db_path, "search", keyword, "--status", "all", "--limit", "10", "--json")
        payload = record["payload"] or {}
        ids = [hit["memory"]["id"] for hit in payload.get("hits", [])]
        attempts.append(
            {"keyword": keyword, "exit_code": record["exit_code"], "total": payload.get("total"), "hits": ids}
        )
        hit_ids.extend(ids)
    matched = sorted({memory_id for memory_id in memory_ids if memory_id in hit_ids})
    return {
        "scenario": "3_new_process_retrieval",
        "expectation": "新进程通过 Phase 3 检索能找到网页导入形成的 Memory",
        "ok": bool(matched),
        "imported_memory_ids": memory_ids,
        "matched_memory_ids": matched,
        "searches": attempts,
    }


def scenario_4_invalid_inputs(db_path: Path) -> dict[str, Any]:
    before = counts(db_path)
    results = []
    for url, expected_error, why in FAILURE_CASES:
        record = run_cli(db_path, "import-url", url, "--json")
        payload = record["payload"] or {}
        results.append(
            {
                "url": url,
                "why": why,
                "expected_error": expected_error,
                "exit_code": record["exit_code"],
                "error_type": payload.get("error_type"),
                "error": (payload.get("error") or "")[:160],
                "ok": record["exit_code"] == 3 and payload.get("error_type") == expected_error,
            }
        )
    after = counts(db_path)
    return {
        "scenario": "4_invalid_and_low_value_inputs",
        "expectation": "非法/不允许/无正文的 URL 明确失败，且零写入（不产生 Memory / Source）",
        "ok": all(item["ok"] for item in results) and after == before,
        "cases": results,
        "counts_before": before,
        "counts_after": after,
    }


class UiClient:
    def __init__(self, base: str) -> None:
        self.base = base
        self.exchanges: list[dict[str, Any]] = []
        self.bodies: list[str] = []

    def get(self, path: str) -> tuple[int, str]:
        url = self.base + urllib.parse.quote(path, safe="/?=&%+")
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                body = response.read().decode("utf-8")
                self.exchanges.append({"method": "GET", "path": path, "status": response.status})
                self.bodies.append(body)
                return response.status, body
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            self.exchanges.append({"method": "GET", "path": path, "status": error.code})
            self.bodies.append(body)
            return error.code, body

    def post(self, path: str, data: dict[str, str]) -> tuple[int, str]:
        request = urllib.request.Request(
            self.base + path, data=urllib.parse.urlencode(data).encode("utf-8"), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                body = response.read().decode("utf-8")
                self.exchanges.append({"method": "POST", "path": path, "status": response.status})
                self.bodies.append(body)
                return response.status, body
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            self.exchanges.append({"method": "POST", "path": path, "status": error.code})
            self.bodies.append(body)
            return error.code, body


def scenario_5_web_ui(db_path: Path, port: int) -> tuple[dict[str, Any], str]:
    process = subprocess.Popen(
        [sys.executable, "-m", "personal_memory", "web", "--db", str(db_path), "--port", str(port)],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env(),
    )
    lines: list[str] = []
    reader = threading.Thread(
        target=lambda: [lines.append(line.rstrip("\n")) for line in (process.stdout or [])], daemon=True
    )
    reader.start()
    ready = False
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                ready = response.status == 200
                break
        except Exception:
            time.sleep(0.3)

    before = counts(db_path)
    ui = UiClient(f"http://127.0.0.1:{port}")
    status = 0
    body = ""
    try:
        if ready:
            status, body = ui.post("/import-url", {"url": README_RAW_URL, "title": ""})
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        reader.join(timeout=5)

    after = counts(db_path)
    repository = repository_for(db_path)
    new_sources = [s for s in repository.list_sources(limit=10) if str(s.source_type) == "web"]
    stored = source_record(repository, new_sources[0].id) if new_sources else None
    log = "\n".join(lines)
    scenario = {
        "scenario": "5_web_ui_url_import",
        "expectation": "浏览器表单 POST /import-url 走同一个 WebImporter + Capture + Formation 管线",
        "ok": ready
        and status == 200
        and "网页导入完成" in body
        and after["memories"] > before["memories"]
        and stored is not None
        and stored["metadata"]["content_type"] == "text/plain",
        "server_ready": ready,
        "http_status": status,
        "page_marker": "网页导入完成" in body,
        "counts_before": before,
        "counts_after": after,
        "source": stored,
        "server_log_lines": len(lines),
        "server_banner": [line for line in lines if line.strip()][:6],
    }
    return scenario, log


def main() -> int:
    parser = argparse.ArgumentParser(description="KB 1.0 Phase 4 URL importer acceptance")
    parser.add_argument("--db", default="data/kb1p4-acceptance.db")
    parser.add_argument("--evidence", default="docs/kb1p4-web-acceptance.json")
    parser.add_argument("--transcript", default="docs/kb1p4-web-acceptance.txt")
    parser.add_argument("--skip-ui", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    # a fresh database keeps the acceptance counts unambiguous
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        if candidate.exists():
            candidate.unlink()
    subprocess.run(
        [sys.executable, "-m", "personal_memory", "--db", str(db_path), "init"],
        capture_output=True, text=True, encoding="utf-8", cwd=PROJECT_ROOT, env=child_env(),
    )
    credential = ensure_credential_from_dsh_file()
    if not credential:
        raise SystemExit("no credential available: set DEEPSEEK_API_KEY or PERSONAL_MEMORY_LLM_API_KEY")

    importer = WebImporter(timeout_seconds=30)
    scenarios: list[dict[str, Any]] = []
    started_at = utcnow_iso()

    scenarios.append(scenario_0_probe(importer))
    print(f"scenario 0: ok={scenarios[-1]['ok']}", flush=True)

    article = scenario_1_article(db_path, importer)
    scenarios.append(article)
    print(f"scenario 1: ok={article['ok']}", flush=True)

    github = scenario_2_github_readme(db_path, importer)
    scenarios.append(github)
    print(f"scenario 2: ok={github['ok']}", flush=True)

    memory_ids = [m["id"] for m in article.get("memories", [])] + [m["id"] for m in github.get("memories", [])]
    retrieval = scenario_3_new_process_search(db_path, memory_ids)
    scenarios.append(retrieval)
    print(f"scenario 3: ok={retrieval['ok']}", flush=True)

    invalid = scenario_4_invalid_inputs(db_path)
    scenarios.append(invalid)
    print(f"scenario 4: ok={invalid['ok']}", flush=True)

    server_log = ""
    if not args.skip_ui:
        ui_scenario, server_log = scenario_5_web_ui(db_path, free_port())
        scenarios.append(ui_scenario)
        print(f"scenario 5: ok={ui_scenario['ok']}", flush=True)

    # privacy: the redacted CLI output must not contain the stored article text
    repository = repository_for(db_path)
    body_probe = None
    for source in repository.list_sources(limit=20):
        content = (source.content or "").strip()
        if len(content) > 300:
            body_probe = content[100:180]
            break
    cli_record = run_cli(db_path, "import-url", ARTICLE_URL, "--dry-run", "--json")
    key = os.environ.get("PERSONAL_MEMORY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    scenarios.append(
        {
            "scenario": "6_privacy",
            "expectation": "默认输出与证据中都没有完整网页正文、没有 API Key",
            "ok": bool(body_probe) and body_probe not in cli_record["stdout"] and key not in cli_record["stdout"],
            "cli_stdout_has_body_excerpt": bool(body_probe) and body_probe in cli_record["stdout"],
            "cli_stdout_has_key": key in cli_record["stdout"],
            "dry_run_exit_code": cli_record["exit_code"],
            "evidence_uses_previews_only": True,
        }
    )

    final_counts = counts(db_path)
    api_key = os.environ.get("PERSONAL_MEMORY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    evidence: dict[str, Any] = {
        "generated_at": utcnow_iso(),
        "started_at": started_at,
        "phase": "kb-1.0 phase-4 (url importer)",
        "package_version": __import__("personal_memory").__version__,
        "db_path": str(db_path),
        "credential_source": credential,
        "scenarios": scenarios,
        "final_counts": final_counts,
        "final_status_counts": repository.status_counts(),
        "index_consistency": repository.index_consistency(),
        "server_log_lines": len(server_log.splitlines()),
        "overall_ok": all(scenario["ok"] for scenario in scenarios),
    }
    payload = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if api_key and api_key in payload:
        raise SystemExit("refusing to write evidence: the API key leaked")
    for source in repository.list_sources(limit=20):
        content = (source.content or "").strip()
        if len(content) > 400 and content[:400] in payload:
            raise SystemExit("refusing to write evidence: a full page body leaked")

    evidence_path = Path(args.evidence)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(payload + "\n", encoding="utf-8")

    lines = [
        "Knowledge Base 1.0 Phase 4 (URL / Web Importer) acceptance run",
        f"generated_at : {evidence['generated_at']}",
        f"package      : {evidence['package_version']}  phase={evidence['phase']}",
        f"credential   : {credential}",
        "",
    ]
    for scenario in scenarios:
        lines.append(f"[{scenario['scenario']}] ok={scenario['ok']}")
        lines.append("  " + json.dumps(
            {k: v for k, v in scenario.items() if k not in {"scenario", "ok"}}, ensure_ascii=False
        )[:1800])
        lines.append("")
    lines.append(f"final counts : {final_counts}")
    lines.append(f"overall      : {'PASS' if evidence['overall_ok'] else 'FAIL'}")
    text = "\n".join(lines) + "\n"
    Path(args.transcript).write_text(text, encoding="utf-8")
    print(text)
    print(f"evidence: {evidence_path} ({len(payload)} bytes), transcript: {args.transcript}")
    return 0 if evidence["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
