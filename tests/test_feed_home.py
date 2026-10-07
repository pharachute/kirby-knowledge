"""卡比 Capture Home tests: the 喂知识 page, its state machine and the JSON result binding.

Everything here exercises the **real** pipeline through real HTTP against a real server
process started in-process (`WebTestCase` from ``test_web``), with a scripted Mock LLM, so
the "吃饱了~" contract can be asserted: success feedback only appears when Capture /
Formation actually finished, and worthless content is *not* reported as a failure.
"""

from __future__ import annotations

import base64
import json
import pathlib
import re
import struct
import unittest
import urllib.error
import urllib.request
from unittest import mock

import personal_memory.web.server as web_server
from personal_memory.importers.web import WebImporter
from personal_memory.web import views as views_module

from .llm_fakes import mock_client, response_for
from .pdf_fixtures import build_latin_pdf
from .test_quality import draft_payload, formation_payload
from .test_web import WebTestCase, VALID_CAPTURE_PAYLOAD
from .test_web_import import FakeTransport, LONG_BODY, html_page, public_resolver, response

HIGH_VALUE = response_for(VALID_CAPTURE_PAYLOAD)
LOW_VALUE = response_for({"worth_remembering": False, "reason": "一次性内容", "memories": []})

CHAT_TEXT = "[USER]\n你好，我在系统学习。\n\n[ASSISTANT]\n很好，我们继续。\n"
MARKDOWN_TEXT = "# 标题\n\n" + "正文内容，用于测试喂给卡比。" * 20


class FeedPageTest(WebTestCase):
    """The home page itself: quiet, Kirby-first, Chinese, no dashboard."""

    prefix = "pms-feed-"

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start(self.make_context(HIGH_VALUE))

    def test_home_is_the_feed_page_without_a_dashboard(self) -> None:
        status, body = self.get("/")

        self.assertEqual(status, 200)
        self.assertIn("把知识喂给我", body)
        self.assertIn("卡比", body)
        # spec §二: no statistics, no record list, no technical state on the home page
        self.assertNotIn('class="stat"', body)
        self.assertNotIn("memory.db", body)
        self.assertNotIn("API Key", body)
        self.assertNotIn("index_consistency", body)
        self.assertNotIn("Model", body)
        # ... and the health endpoint still exists
        self.assertEqual(self.get("/healthz"), (200, "ok\n"))

    def test_home_navigation_is_chinese(self) -> None:
        _, body = self.get("/")
        # 主导航（第四阶段定稿）：喂知识 / 我的记忆 / 搜索；来源从记忆的「来自」进入
        for label in ("喂知识", "我的记忆", "搜索"):
            self.assertIn(label, body, label)
        nav = body[body.index("<header>"):body.index("</header>")]
        self.assertNotIn("我的来源", nav)
        self.assertNotIn("/sources", nav)
        for english in (">Capture<", ">Import<", ">Memories<", ">Sources<", ">Search<", "Personal Knowledge Base"):
            self.assertNotIn(english, body, english)

    def test_kirby_component_states_and_size(self) -> None:
        _, body = self.get("/")

        self.assertIn('data-kirby-state="idle"', body)
        self.assertIn('id="kirby-mouth"', body)
        self.assertIn('id="kirby-feedback"', body)
        self.assertIn('id="kirby-note"', body)
        # the four visual states required by spec §三 (plus the flow states)
        for state in ("idle", "open", "inhale", "closed", "detected", "processing", "feedback"):
            self.assertIn(state, body, state)
        # animation logic must not be hard-wired to one image
        self.assertIn("KIRBY_TRANSITIONS", body)
        self.assertIn("nextKirbyState", body)
        self.assertEqual(views_module.KIRBY_SIZE_PX, 320)
        self.assertTrue(280 <= views_module.KIRBY_SIZE_PX <= 360)
        self.assertEqual(
            set(views_module.KIRBY_FRAMES),
            {"idle", "open", "inhale1", "inhale2", "inhale3", "closed"},
        )

    def test_unified_input_replaces_the_type_specific_entries(self) -> None:
        """Phase 2：文字 / 网址 / 文件共用一个输入区，不再有类型入口（spec §三/§四）。"""
        _, body = self.get("/")
        self.assertIn('id="feed-text"', body)
        self.assertIn('id="feed-chat-file"', body)
        self.assertIn('id="feed-add-file"', body)
        self.assertIn('id="feed-submit"', body)
        self.assertIn('accept=".txt,.md,.markdown,.chat,.json,.pdf"', body)
        for removed in ('id="feed-url"', 'id="feed-url-form"', 'id="feed-chat-form"',
                        'feed-url-toggle', 'feed-chat-toggle', "喂一个网址", "喂一个文件"):
            self.assertNotIn(removed, body, removed)
        for endpoint in ('"/capture"', '"/import-url"', '"/import-chat"', '"/import-file"', '"/import-pdf"'):
            self.assertIn(endpoint, body, endpoint)

    def test_state_machine_transitions_and_recovery(self) -> None:
        """Simulate the *served* transition table: happy path plus the failure reset."""
        _, body = self.get("/")
        raw = re.search(r"var KIRBY_TRANSITIONS = \{(.*?)\n\};", body, re.S)
        self.assertIsNotNone(raw, "transition table must be present in the page")
        table: dict[str, dict[str, str]] = {}
        for line in raw.group(1).strip().splitlines():
            match = re.match(r"\s*(\w+):\s*\{(.*?)\},?\s*$", line)
            if match:
                table[match.group(1)] = dict(re.findall(r"(\w+):\s*\"(\w+)\"", match.group(2)))
        self.assertEqual(set(table), set(views_module.KIRBY_STATES))

        def walk(events: list[str]) -> list[str]:
            state = "idle"
            seen = [state]
            for event in events:
                state = table.get(state, {}).get(event) or table["idle"].get(event) or "idle"
                seen.append(state)
            return seen

        happy = walk(["detect", "open", "absorb", "close", "process", "feedback", "done"])
        self.assertEqual(happy, ["idle", "detected", "open", "inhale", "closed", "processing", "feedback", "idle"])
        # a failure during processing must return to idle instead of sticking
        self.assertEqual(walk(["detect", "open", "absorb", "close", "process", "fail"])[-1], "idle")
        for state in table:
            self.assertIn("reset", table[state], state)  # every state can be reset


class FeedJsonFlowTest(WebTestCase):
    """The JSON binding: 吃饱了~ is tied to the real Capture/Formation result (spec §六)."""

    prefix = "pms-feed-flow-"

    def post_json(self, path: str, data: dict[str, object]):
        return self.post(path, data, headers={"X-Requested-With": "fetch", "Accept": "application/json"})

    def upload(self, name: str, data: bytes, **extra: str) -> dict[str, str]:
        return {"filename": name, "content_base64": base64.b64encode(data).decode("ascii"), **extra}

    def test_text_capture_success_binds_to_real_result(self) -> None:
        self.base = self.start(self.make_context(HIGH_VALUE))

        status, body, _ = self.post_json("/capture", {"content": "RAG 是检索增强生成，通过检索外部知识提供上下文。"})
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["feedback"], "吃饱了~")    # 角色反馈：绑定真实完成
        self.assertEqual(payload["title"], "处理完成")         # 业务结果标题（传统页面沿用）
        self.assertEqual(payload["memory_count"], 1)
        self.assertEqual(payload["source_count"], 1)
        self.assertEqual(payload["note"], "记住了 1 件事")
        self.assertTrue(payload["worth_remembering"])
        self.assertEqual(self.repo.counts(), {"sources": 1, "memories": 1, "memory_sources": 1})
        # the response only exists after formation ran
        self.assertEqual(self.client.transport.call_count, 1)

    def test_low_value_content_is_not_reported_as_failure(self) -> None:
        self.base = self.start(self.make_context(LOW_VALUE))

        status, body, _ = self.post_json("/capture", {"content": "今天喝了一杯奶茶。"})
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"], "低价值内容是正常业务结果，不是失败")
        self.assertEqual(payload["feedback"], "吃饱了~")
        self.assertEqual(payload["memory_count"], 0)
        self.assertEqual(payload["source_count"], 0)
        self.assertEqual(payload["note"], "这次没有留下长期记忆")
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_file_chat_and_pdf_imports_through_the_feed(self) -> None:
        context = self.make_context(HIGH_VALUE, HIGH_VALUE, HIGH_VALUE)
        self.base = self.start(context)

        markdown = self.post_json(
            "/import-file", {**self.upload("note.md", MARKDOWN_TEXT.encode()), "title": "", "source_type": "file"}
        )
        chat = self.post_json("/import-chat", {**self.upload("chat.txt", CHAT_TEXT.encode()), "format": "auto"})
        pdf = self.post_json("/import-pdf", self.upload("doc.pdf", build_latin_pdf(["Phase five body text. " * 20])))

        for label, (status, body, _) in {"markdown": markdown, "chat": chat, "pdf": pdf}.items():
            payload = json.loads(body)
            self.assertEqual(status, 200, label)
            self.assertTrue(payload["ok"], label)
            self.assertEqual(payload["feedback"], "吃饱了~", label)
            self.assertEqual(payload["memory_count"], 1, label)
        self.assertEqual(self.repo.counts()["memories"], 3)
        self.assertEqual(self.repo.counts()["sources"], 3)

    def test_url_import_through_the_feed(self) -> None:
        transport = FakeTransport({"https://example.com/article": response(html_page(LONG_BODY))})
        self.base = self.start(self.make_context(HIGH_VALUE))

        with mock.patch.object(web_server, "WebImporter", lambda **kwargs: WebImporter(transport=transport, resolver=public_resolver)):
            status, body, _ = self.post_json("/import-url", {"url": "https://example.com/article", "title": ""})

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["note"], "记住了 1 件事")
        self.assertEqual(transport.calls[0]["url"], "https://example.com/article")
        self.assertEqual(self.repo.counts()["memories"], 1)

    def test_failures_are_chinese_and_never_leak_internals(self) -> None:
        context = self.make_context(LOW_VALUE)
        self.base = self.start(context)

        cases = [
            ("/import-pdf", self.upload("broken.pdf", b"not a pdf at all")),
            ("/import-chat", {**self.upload("chat.txt", b""), "format": "auto"}),
            ("/import-file", {**self.upload("bad.md", b""), "source_type": "file"}),
            ("/import-url", {"url": "http://127.0.0.1:8765/", "title": ""}),
            ("/capture", {"content": "   "}),
        ]
        for path, data in cases:
            status, body, _ = self.post_json(path, data)
            payload = json.loads(body)
            self.assertFalse(payload["ok"], path)
            self.assertIn(status, (400, 503), path)
            self.assertTrue(payload["title"], path)
            self.assertTrue(re.search(r"[\u4e00-\u9fff]", payload["title"]), path)
            # user language only: no exception names, no stack, no model/DB/URL-policy detail
            for leak in ("Traceback", "Error", "Exception", "sqlite", "PdfCorrupt", "WebImport",
                         "LLMConfig", "deepseek", "127.0.0.1", "http://"):
                self.assertNotIn(leak, body, f"{path} leaked {leak}")
        # nothing was half-written and the page is still usable
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})
        self.assertEqual(self.get("/")[0], 200)

    def test_missing_model_is_a_friendly_refusal(self) -> None:
        self.base = self.start(self.make_context(with_llm=False))

        status, body, _ = self.post_json("/capture", {"content": "想喂一点内容。"})
        payload = json.loads(body)

        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["title"], "卡比还没准备好")
        self.assertNotIn("LLMConfigError", body)
        self.assertNotIn("api_key", body.lower())

    def test_browser_form_path_still_uses_redirect_and_flash(self) -> None:
        """Existing form pages must keep working (no fetch header -> 303 + flash)."""
        self.base = self.start(self.make_context(HIGH_VALUE))

        status, body, url = self.post("/capture", {"content": "浏览器表单路径。"})

        self.assertEqual(status, 200)  # urllib follows the 303
        self.assertTrue((url or "").startswith(self.base + "/"), url)   # 落在「喂知识」首页
        self.assertIn("处理完成", body)   # 一次性结果横幅仍然显示
        self.assertIn("把知识喂给我", body)
        self.assertNotIn("Traceback", body)
        self.assertEqual(self.repo.counts()["memories"], 1)


class KirbySpriteTest(WebTestCase):
    """The home page must show real Kirby game sprites (files, route, markup)."""

    prefix = "pms-kirby-"

    FRAMES = (
        "kirby-idle.png",
        "kirby-open.png",
        "kirby-inhale-1.png",
        "kirby-inhale-2.png",
        "kirby-inhale-3.png",
    )

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start(self.make_context(HIGH_VALUE))

    def raw_get(self, path: str):
        request = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_asset_files_are_valid_transparent_pngs(self) -> None:
        self.assertTrue(views_module.kirby_assets_available())
        for name in self.FRAMES:
            data = (views_module.KIRBY_ASSET_DIR / name).read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n", name)
            width, height = struct.unpack(">II", data[16:24])
            colour_type = data[25]
            self.assertEqual(colour_type, 6, f"{name} must be RGBA (transparent background)")
            self.assertGreaterEqual(width, 280, f"{name} must be large enough to render at ~320px")
            self.assertGreaterEqual(height, 280, name)

    def test_frames_map_to_the_real_assets(self) -> None:
        frames = views_module.KIRBY_FRAMES
        self.assertEqual(set(frames), {"idle", "open", "inhale1", "inhale2", "inhale3", "closed"})
        for state, url in frames.items():
            self.assertTrue(str(url).startswith("/assets/"), state)
            self.assertTrue((views_module.KIRBY_ASSET_DIR / str(url).rsplit("/", 1)[-1]).is_file(), state)
        # 吸入是三帧不同的真实帧，不是同一张图重复
        inhale = [frames[name] for name in views_module.KIRBY_INHALE_ORDER]
        self.assertEqual(len(set(inhale)), 3, inhale)
        self.assertNotEqual(frames["idle"], frames["open"])
        self.assertNotEqual(frames["open"], frames["inhale1"])
        # 合嘴 uses the normal standing frame (allowed by the spec)
        self.assertEqual(frames["closed"], frames["idle"])
        # 旧的"鼓起/悬浮"近似帧不再被引用，文件也已移除
        self.assertNotIn("/assets/kirby-inhale.png", list(frames.values()))
        self.assertFalse((views_module.KIRBY_ASSET_DIR / "kirby-inhale.png").exists())

    def test_asset_route_serves_the_exact_bytes(self) -> None:
        for name in self.FRAMES:
            status, headers, body = self.raw_get(f"/assets/{name}")
            self.assertEqual(status, 200, name)
            self.assertEqual(headers.get("Content-Type"), "image/png", name)
            self.assertEqual(body, (views_module.KIRBY_ASSET_DIR / name).read_bytes(), name)

    def test_asset_route_rejects_unknown_and_traversal_paths(self) -> None:
        for path in (
            "/assets/nope.png",
            "/assets/kirby-idle.png.bak",
            "/assets/../web/views.py",
            "/assets/%2e%2e/web/views.py",
            "/assets/",
        ):
            status, _, body = self.raw_get(path)
            self.assertEqual(status, 404, path)
            self.assertTrue(body.startswith(b"<!doctype html>"), path)   # an ordinary error page
            self.assertNotIn(b"KIRBY_FRAMES", body, path)                # never python source
            self.assertNotIn(b"def _send_asset", body, path)

    def test_home_renders_the_real_frames_not_the_placeholder(self) -> None:
        status, body = self.get("/")

        self.assertEqual(status, 200)
        self.assertEqual(body.count('class="kirby kirby-frame'), 6)
        self.assertNotIn('svg class="kirby"', body)          # placeholder drawing is gone
        self.assertIn('src="/assets/kirby-idle.png"', body)
        self.assertIn('src="/assets/kirby-open.png"', body)
        for index in (1, 2, 3):
            self.assertIn(f'src="/assets/kirby-inhale-{index}.png"', body)
        self.assertIn("image-rendering: pixelated", body)     # pixel art stays crisp
        # visibility is CSS-driven (an inline display:none would win over the state rules)
        self.assertNotIn('style="display:none"', body)
        for state in ("idle", "open", "inhale", "closed"):
            self.assertIn(f'[data-kirby-state="{state}"] .kirby-frame-', body, state)
        # the three inhale frames are all shown in the inhale state and animated as a loop
        for index in (1, 2, 3):
            self.assertIn(f'[data-kirby-state="inhale"] .kirby-frame-inhale{index}', body)
        self.assertIn("@keyframes kirby-inhale-a", body)
        self.assertIn("@keyframes kirby-inhale-c", body)
        # only one frame occupies layout; the others overlay (so the page is not stretched)
        self.assertIn(".kirby-frame-inhale1, .kirby-frame-inhale3 { position: absolute", body)
        # content visibly travels into the mouth before the request finishes
        self.assertIn("kirby-intake", body)
        self.assertIn("@keyframes kirby-intake", body)
        self.assertIn("showIntake", body)

    def test_fallback_still_works_when_assets_are_missing(self) -> None:
        """The placeholder must come back if the files are unavailable (no broken image)."""
        original = views_module.KIRBY_FRAMES
        views_module.KIRBY_FRAMES = {name: None for name in original}
        try:
            html = views_module.kirby_component()
        finally:
            views_module.KIRBY_FRAMES = original
        self.assertIn('svg class="kirby"', html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
