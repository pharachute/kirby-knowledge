"""Ingestion 入口测试：PDF 上传入口、PDF 当网址、URL 失败文案（KB 1.0 修复）。

注意：形成记忆时是否保留 Source 由既有的 evidence 策略决定，所以这些用例统一使用
``requires_source=True`` 的 formation 结果，才能断言 Source 真的落盘。
"""

from __future__ import annotations

import base64
import unittest
from unittest import mock

from personal_memory import importers as importers_pkg
from personal_memory.importers.web import WebImporter
from personal_memory.web import server as web_server
from personal_memory.web import views

from .llm_fakes import response_for
from .test_pdf_import import build_latin_pdf, draft_payload, formation_payload
from .test_web import WebTestCase
from .test_web_import import FakeTransport, html_page, public_resolver, response

PAGE_TEXT = ("Ingestion acceptance PDF: SQLite wal checkpoint notes for the knowledge base. "
             "The wal file merges back into the main database during a checkpoint. ")
SOURCE_PAYLOAD = formation_payload(draft_payload(requires_source=True, evidence_quote="wal checkpoint"))


def pdf_bytes() -> bytes:
    return build_latin_pdf([PAGE_TEXT * 3], info={"Title": "Ingestion PDF Doc"})


def bound_importer(transport):
    """Build a real WebImporter bound to a scripted transport (never the patched class)."""

    def factory(**kwargs):  # noqa: ANN001 - mirrors how the server constructs it
        kwargs.pop("transport", None)
        return WebImporter(transport=transport, resolver=public_resolver, **kwargs)

    return factory


class HomeFileEntryTest(WebTestCase):
    """首页可见文件入口：接受 PDF，并在前端按扩展名分流。"""

    prefix = "pms-ingest-home-"

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start(self.make_context(response_for(SOURCE_PAYLOAD)))

    def test_file_entry_accepts_pdf_and_routes_by_extension(self) -> None:
        _, body = self.get("/")
        self.assertIn('id="feed-chat-file"', body)
        self.assertIn('accept=".txt,.md,.markdown,.chat,.json,.pdf"', body)
        self.assertIn('feed("/import-pdf"', body)          # PDF → 既有 PDF 解析入口
        self.assertIn('endsWith(".pdf")', body)
        self.assertIn('feed("/import-chat"', body)         # .chat / .json → 聊天解析
        self.assertIn('feed("/import-file"', body)         # .txt / .md → 文件导入（修复：以前一律走聊天解析）
        self.assertIn('id="feed-add-file"', body)          # Phase 2：文件从统一输入区添加

    def test_pdf_upload_from_the_visible_entry_end_to_end(self) -> None:
        status, body, _ = self.post(
            "/import-pdf",
            {"filename": "paper.pdf", "content_base64": base64.b64encode(pdf_bytes()).decode("ascii")},
        )
        self.assertEqual(status, 200)
        self.assertIn("PDF 导入完成", body)
        source = self.repo.list_sources()[0]
        self.assertIn("wal checkpoint notes", source.content)
        self.assertEqual(source.metadata["captured_from"], "pdf")
        self.assertEqual(len(self.repo.get_memories_for_source(source.id)), 1)


class PdfByUrlTest(WebTestCase):
    """一个返回 PDF 的网址：走同一条 PDF 解析 + Capture 链，不再报"不是网页文字"。"""

    prefix = "pms-ingest-pdfurl-"

    def test_pdf_url_becomes_source_and_memory(self) -> None:
        transport = FakeTransport({
            "https://example.com/paper.pdf": response(pdf_bytes(), content_type="application/pdf")
        })
        self.base = self.start(self.make_context(response_for(SOURCE_PAYLOAD)))
        with mock.patch.object(web_server, "WebImporter", bound_importer(transport)):
            status, body, _ = self.post("/import-url", {"url": "https://example.com/paper.pdf"})

        self.assertEqual(status, 200)
        self.assertIn("PDF 导入完成", body)
        self.assertIn("example.com", body)
        self.assertEqual(len(transport.calls), 1)                    # 只下载一次
        source = self.repo.list_sources()[0]
        self.assertIn("wal checkpoint notes", source.content)        # 真实提取出的文本
        self.assertEqual(source.metadata["captured_from"], "pdf")
        self.assertEqual(len(self.repo.get_memories_for_source(source.id)), 1)   # Memory 能引用 PDF Source

        source_status, source_body = self.get(f"/sources/{source.id}")
        self.assertEqual(source_status, 200)
        self.assertIn("原稿", source_body)
        self.assertIn("wal checkpoint notes", source_body)

    def test_pdf_url_without_a_pdf_content_type_is_still_recognised(self) -> None:
        transport = FakeTransport({
            "https://example.com/download?id=1": response(pdf_bytes(), content_type="application/octet-stream")
        })
        self.base = self.start(self.make_context(response_for(SOURCE_PAYLOAD)))
        with mock.patch.object(web_server, "WebImporter", bound_importer(transport)):
            status, body, _ = self.post("/import-url", {"url": "https://example.com/download?id=1"})

        self.assertEqual(status, 200)
        self.assertIn("PDF 导入完成", body)
        self.assertIn("wal checkpoint notes", self.repo.list_sources()[0].content)

    def test_corrupt_pdf_url_fails_in_chinese_and_writes_nothing(self) -> None:
        broken = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n" + bytes(range(256)) * 4
        transport = FakeTransport({
            "https://example.com/broken.pdf": response(broken, content_type="application/pdf")
        })
        self.base = self.start(self.make_context(response_for(SOURCE_PAYLOAD)))
        with mock.patch.object(web_server, "WebImporter", bound_importer(transport)):
            status, body, _ = self.post("/import-url", {"url": "https://example.com/broken.pdf"})

        self.assertEqual(status, 400)
        self.assertIn("这份 PDF 好像坏掉了", body)   # HTML 错误页也用同一句精确原因
        self.assertNotIn("Traceback", body)
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_html_urls_still_behave_exactly_as_before(self) -> None:
        transport = FakeTransport({
            "https://example.com/a": response(html_page(PAGE_TEXT * 4, title="静态文章"))
        })
        self.base = self.start(self.make_context(response_for(SOURCE_PAYLOAD)))
        with mock.patch.object(web_server, "WebImporter", bound_importer(transport)):
            status, body, _ = self.post("/import-url", {"url": "https://example.com/a"})

        self.assertEqual(status, 200)
        self.assertIn("网页导入完成", body)
        source = self.repo.list_sources()[0]
        self.assertEqual(str(source.source_type), "web")
        self.assertIn("wal checkpoint notes", source.content)


class UrlFailureCopyTest(WebTestCase):
    """动态渲染页面必须给出准确原因，而不是笼统的"稍后再试"。"""

    prefix = "pms-ingest-copy-"

    def test_dynamic_page_copy_explains_the_reason(self) -> None:
        title, hint = views.FEED_ERROR_COPY["EmptyContentError"]
        self.assertEqual(title, "这个页面读不出正文")
        self.assertIn("动态渲染", hint)
        self.assertNotIn("稍后再试", hint)

    def test_binary_non_pdf_url_keeps_the_binary_hint(self) -> None:
        title, hint = views.FEED_ERROR_COPY["UnsupportedContentTypeError"]
        self.assertEqual(title, "这一页不是文字内容")
        self.assertIn("二进制", hint)

    def test_scanned_pdf_copy_no_longer_misleads(self) -> None:
        title, hint = views.FEED_ERROR_COPY["PdfEmptyTextError"]
        self.assertEqual(title, "这份 PDF 里没有可提取的文字")
        self.assertIn("图片识别", hint)

    def test_pdf_url_error_is_compatible_with_the_old_exception_type(self) -> None:
        error = importers_pkg.PdfUrlContentError("https://example.com/a.pdf", body=b"%PDF-1.4",
                                                 content_type="application/pdf")
        self.assertIsInstance(error, importers_pkg.UnsupportedContentTypeError)
        self.assertIsInstance(error, importers_pkg.WebImportError)
        self.assertEqual(error.body, b"%PDF-1.4")
        self.assertNotIn("%PDF", str(error))          # 正文不进错误文本

    def test_dynamic_page_through_the_real_web_layer(self) -> None:
        empty_shell = "<!doctype html><html><head><title>App</title></head><body><div id=root></div></body></html>"
        transport = FakeTransport({"https://example.com/app": response(empty_shell)})
        self.base = self.start(self.make_context(response_for(SOURCE_PAYLOAD)))
        with mock.patch.object(web_server, "WebImporter", bound_importer(transport)):
            status, body, _ = self.post("/import-url", {"url": "https://example.com/app"})

        self.assertEqual(status, 400)
        self.assertIn("这个页面读不出正文", body)
        self.assertIn("动态渲染", body)
        self.assertNotIn("稍后再试", body)
        self.assertEqual(self.repo.counts()["sources"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
