"""UI 收口测试：旧入口 /capture、/import 不再有独立界面，POST 能力保持不变。"""

from __future__ import annotations

import json
import re
import unittest
import urllib.error
import urllib.request

from .llm_fakes import response_for
from .test_web import WebTestCase, VALID_CAPTURE_PAYLOAD

FORBIDDEN_VISIBLE = (
    "API", "JSON", "FTS", "BM25", "database", "Database", "pipeline", "embedding",
    "debug", "loading", "success", "successful", "error", "failed", "import", "Import",
    "capture", "Capture", "source", "Source", "memory", "Memory", "Formation",
)


def visible_text(body: str) -> str:
    body = re.sub(r"<style>.*?</style>", " ", body, flags=re.S)
    body = re.sub(r"<script>.*?</script>", " ", body, flags=re.S)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return " ".join(body.split())


class LegacyEntryCleanupTest(WebTestCase):
    """旧入口一律回到「喂知识」，且旧产品界面不再存在。"""

    prefix = "pms-legacy-"

    def setUp(self) -> None:
        super().setUp()
        self.make_memory(title="读书笔记", content="今天读了一本书，记下了三件事。")
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    # -- helpers ----------------------------------------------------------
    def raw_get(self, path: str):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(self.base + path, timeout=15) as response:
                return response.status, dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers)

    # -- 1. 旧入口跳转 ----------------------------------------------------
    def test_legacy_get_entries_redirect_to_the_home_page(self) -> None:
        for path in ("/capture", "/import"):
            status, headers = self.raw_get(path)
            self.assertEqual(status, 303, path)
            self.assertEqual(headers.get("Location"), "/", path)

    def test_redirect_keeps_a_one_shot_result_banner(self) -> None:
        before = self.repo.counts()["memories"]
        status, body, url = self.post("/capture", {"title": "", "content": "浏览器表单路径。"})
        self.assertEqual(status, 200)                     # urllib 跟着 303 走到首页
        self.assertEqual(url.rstrip("/").split("?")[0].rsplit("/", 1)[-1], "", url)
        self.assertIn("把知识喂给我", body)                # 落在「喂知识」首页
        self.assertIn("处理完成", body)                    # 横幅仍然显示真实结果
        self.assertEqual(self.repo.counts()["memories"], before + 1)

    # -- 2. 旧界面不再出现 -------------------------------------------------
    def test_old_forms_and_english_are_gone(self) -> None:
        for path in ("/", "/memories", "/search", "/capture", "/import"):
            _, body = self.get(path)
            for marker in ('action="/capture"', 'action="/import-file"', 'action="/import-chat"',
                           'action="/import-url"', 'action="/import-pdf"', 'id="file_b64"',
                           "<h2>环境</h2>", ">Capture<", ">Import<", "Sources"):
                self.assertNotIn(marker, body, f"{path}: {marker}")
            text = visible_text(body)
            for word in FORBIDDEN_VISIBLE:
                self.assertNotIn(word, text, f"{path}: {word}")

    def test_home_page_still_carries_every_intake(self) -> None:
        _, body = self.get("/")
        self.assertIn("把知识喂给我", body)
        self.assertEqual(body.count('class="kirby kirby-frame'), 6)
        for endpoint in ('"/capture"', '"/import-file"', '"/import-chat"', '"/import-url"', '"/import-pdf"'):
            self.assertIn(endpoint, body, endpoint)       # 首页仍然接住全部真实能力

    def test_navigation_is_the_final_three(self) -> None:
        for path in ("/", "/memories", "/search", "/capture"):
            _, body = self.get(path)
            nav = body[body.index("<header>"):body.index("</header>")]
            labels = re.findall(r"<a [^>]*>([^<]+)</a>", nav)
            self.assertEqual(labels, ["喂知识", "我的记忆", "搜索"], path)
            self.assertNotIn("/sources", nav, path)

    # -- 3. 后端能力不受影响 -----------------------------------------------
    def test_post_endpoints_still_work_after_the_cleanup(self) -> None:
        status, body, _ = self.post(
            "/capture",
            {"content": "RAG 是检索增强生成。"},
            headers={"X-Requested-With": "fetch", "Accept": "application/json"},
        )
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["memory_count"], 1)
        self.assertEqual(self.repo.counts()["memories"], 2)

    def test_empty_capture_on_the_browser_path_is_chinese(self) -> None:
        status, body, _ = self.post("/capture", {"content": "   "})
        self.assertEqual(status, 400)
        self.assertIn("先写点东西再喂", body)
        self.assertIn("内容不能为空", body)
        self.assertEqual(self.client.transport.call_count, 0)   # 空输入不调用模型

    def test_error_pages_show_chinese_only(self) -> None:
        status, body = self.get("/memories/missing-id")
        self.assertEqual(status, 404)
        self.assertIn("页面不存在", body) if "页面不存在" in body else self.assertIn("操作失败", body)
        text = visible_text(body)
        self.assertNotIn("NotFoundError", text)            # 技术类型只在不可见的注释里
        self.assertIn("<!-- kind: NotFoundError -->", body)
        self.assertNotIn("Sources", text)
        self.assertNotIn("Memories", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
