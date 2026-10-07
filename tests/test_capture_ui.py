"""Phase 2：Capture /「喂知识」页面测试。

覆盖：统一输入模型（文字/网址/文件共用一个入口）、页面内容收敛（无类型入口、无实现说明、
无页面内导航）、状态反馈文案、以及 Design System 复用约束。
"""

from __future__ import annotations

import re
import unittest

from personal_memory.web import views

from .llm_fakes import response_for
from .test_web import VALID_CAPTURE_PAYLOAD, WebTestCase

REMOVED_FROM_CAPTURE = (
    "喂一个网址", "喂一个文件", "真实的记忆形成", "feed-url", "feed-url-form",
    "feed-url-toggle", "feed-chat-box", "feed-chat-toggle", "privacy-note", "feed-actions",
)


def capture_area(body: str) -> str:
    """The Capture content region (everything outside the global header/footer chrome)."""
    region = body[body.index("</header>"):]
    return region[: region.index("<footer>")] if "<footer>" in region else region


class CaptureStructureTest(WebTestCase):
    """页面结构：卡比 + 统一输入区，别无他物。"""

    prefix = "pms-capture-"

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    def test_capture_page_only_has_kirby_and_the_unified_input(self) -> None:
        status, body = self.get("/")
        area = capture_area(body)

        self.assertEqual(status, 200)
        # 卡比是主体
        self.assertIn('id="kirby-zone"', area)
        self.assertIn('id="kirby-mouth"', area)
        self.assertIn('id="kirby-feedback"', area)
        self.assertIn('id="kirby-note"', area)
        self.assertIn("把知识喂给我", area)
        # 统一输入区
        self.assertIn('id="feed-text-box"', area)
        self.assertIn('id="feed-text"', area)
        self.assertIn('id="feed-chat-file"', area)
        self.assertIn('id="feed-add-file"', area)
        self.assertIn('id="feed-submit"', area)
        self.assertIn('id="feed-file-name"', area)
        self.assertIn('id="feed-file-clear"', area)
        # 单一入口：没有类型选择器 / 类型按钮
        self.assertEqual(area.count("<select"), 0)
        self.assertEqual(area.count("<textarea"), 1)
        self.assertEqual(area.count('type="file"'), 1)
        self.assertEqual(area.count('type="submit"'), 1)

    def test_removed_entries_and_notes_are_gone(self) -> None:
        _, body = self.get("/")
        area = capture_area(body)
        for removed in REMOVED_FROM_CAPTURE:
            self.assertNotIn(removed, area, removed)
        # 实现说明不再出现在页面上
        self.assertNotIn("模型服务", area)
        self.assertNotIn("数据库", area)
        self.assertNotIn("本地推断", area)

    def test_page_does_not_repeat_global_navigation(self) -> None:
        _, body = self.get("/")
        area = capture_area(body)
        self.assertNotIn("我的记忆", area)          # 只应出现在 header
        self.assertNotIn("搜索", area)
        self.assertNotIn('href="/memories"', area)
        self.assertNotIn('href="/search"', area)
        header = body[body.index("<header>"):body.index("</header>")]
        self.assertIn("我的记忆", header)
        self.assertIn("搜索", header)

    def test_single_h1_and_design_system_reuse(self) -> None:
        _, body = self.get("/")
        self.assertEqual(body.count("<h1>"), 1)          # 页面标题只有一个：卡比
        self.assertIn("<h1>卡比</h1>", body)
        self.assertIn("--color-bg", body)                # Phase 1 令牌层
        for selector in (".capture-row", ".capture-file"):
            self.assertIn(selector, body, selector)
        capture_css = re.search(r"\.capture-row[^}]*\}", body)
        self.assertIsNotNone(capture_css)
        self.assertIn("var(--space-", capture_css.group(0))
        self.assertNotIn('style="', body)                # 无内联样式


class CaptureScriptContractTest(WebTestCase):
    """前端脚本契约：类型识别在前端完成，后端端点不变。"""

    prefix = "pms-capture-js-"

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    def script(self) -> str:
        _, body = self.get("/")
        return max(re.findall(r"<script>(.*?)</script>", body, re.S), key=len)

    def test_auto_detects_url_vs_text_and_routes_files(self) -> None:
        script = self.script()
        self.assertIn("/^https?:\\/\\/\\S+$/.test(value)", script)   # 网址自动识别
        self.assertIn('feed("/import-url"', script)
        self.assertIn('feed("/capture"', script)
        self.assertIn("function sendFile", script)                  # 文件分流只有一处实现
        self.assertIn('feed("/import-pdf"', script)
        self.assertIn('feed("/import-chat"', script)
        self.assertIn('feed("/import-file"', script)

    def test_file_chip_uses_the_class_toggle_not_the_hidden_attribute(self) -> None:
        """回归：``[hidden]`` 属性会被 .capture-file 的 display 覆盖，导致空胶囊与 ✕ 常显。"""
        _, body = self.get("/")
        self.assertIn('class="capture-file hidden"', body)
        self.assertNotIn('id="feed-file-name" hidden', body)
        self.assertIn('fileName.classList.remove("hidden")', body)
        self.assertIn('fileName.classList.add("hidden")', body)

    def test_file_selection_state_is_wired(self) -> None:
        script = self.script()
        self.assertIn("renderSelectedFile", script)
        self.assertIn('addEventListener("change", renderSelectedFile)', script)
        self.assertIn("clearSelectedFile", script)
        self.assertIn("正在处理……", script)                          # 处理中的克制提示

    def test_drag_and_paste_stay_unified(self) -> None:
        script = self.script()
        self.assertIn('zone.addEventListener("drop"', script)
        self.assertIn('document.addEventListener("paste"', script)
        # 拖拽与文件选择共用同一个分流函数（不再各写一遍）
        self.assertEqual(script.count("sendFile("), 3)

    def test_no_obsolete_handlers_remain(self) -> None:
        script = self.script()
        for gone in ("feed-url-form", "feed-chat-form", "feed-url-toggle", "feed-chat-toggle", "function route("):
            self.assertNotIn(gone, script, gone)


class CaptureFeedbackTest(WebTestCase):
    """状态反馈仍由真实业务结果驱动（后端语义未改）。"""

    prefix = "pms-capture-fb-"

    def post_json(self, path: str, data: dict[str, object]):
        return self.post(path, data, headers={"X-Requested-With": "fetch", "Accept": "application/json"})

    def test_text_success_binds_to_the_real_result(self) -> None:
        from .test_feed_home import HIGH_VALUE

        self.base = self.start(self.make_context(HIGH_VALUE))
        status, body, _ = self.post_json("/capture", {"content": "Agent 的核心能力之一是根据环境反馈调整下一步行动。"})

        self.assertEqual(status, 200)
        payload = body if isinstance(body, dict) else __import__("json").loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["feedback"], "吃饱了~")
        self.assertIn("记住了", payload["note"])
        self.assertGreaterEqual(payload["memory_count"], 1)

    def test_low_value_is_not_reported_as_failure(self) -> None:
        from .test_feed_home import LOW_VALUE

        self.base = self.start(self.make_context(LOW_VALUE))
        status, body, _ = self.post_json("/capture", {"content": "我刚才去楼下买了一瓶水。"})
        payload = body if isinstance(body, dict) else __import__("json").loads(body)

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])                      # 不是失败
        self.assertEqual(payload["memory_count"], 0)
        self.assertEqual(payload["note"], "这次没有留下长期记忆")

    def test_capture_copy_stays_chinese(self) -> None:
        from .test_feed_home import HIGH_VALUE

        self.base = self.start(self.make_context(HIGH_VALUE))
        _, body = self.get("/")
        area = capture_area(body)
        # 只看用户可见文字：脚本注释/class 名不算产品文案
        for pattern, replacement in ((r"<script>.*?</script>", " "), (r"<style>.*?</style>", " "),
                                     (r"<!--.*?-->", " "), (r"<[^>]+>", " ")):
            area = re.sub(pattern, replacement, area, flags=re.S)
        visible = " ".join(area.split())
        for english in ("Capture", "Import", "Memory", "Source", "upload", "Submit", "Ctrl"):
            self.assertNotIn(english, visible, english)
        # 页面上必须只剩必要文案：标题 + 一行提示 + 两个动作
        self.assertIn("把知识喂给我", visible)
        self.assertIn("添加文件", visible)
        self.assertIn("喂给卡比", visible)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
