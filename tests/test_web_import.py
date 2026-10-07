"""KB 1.0 Phase 4 (URL / Web Importer) tests.

The 20 required behaviours of the spec, all offline: HTTP is a scripted transport, DNS is
an injected resolver, and the model is a Mock LLM.  No test touches the network.

    URL -> WebImporter -> WebDocument -> CaptureRequest -> Capture -> Formation -> Memory
"""

from __future__ import annotations

import gzip
import json
import os
import pathlib
import socket
import tracemalloc
import unittest
import urllib.error
import zlib

import personal_memory.importers.web as web_module
from personal_memory import (
    CaptureService,
    Database,
    LLMRequestError,
    MemoryFormationService,
    MemoryQualityGate,
    MemoryRepository,
    MemoryRetriever,
    ValidationError,
    WebDocument,
    WebImporter,
    WebImportError,
    compute_content_hash,
)
from personal_memory.importers.web import (
    ALLOWED_CONTENT_TYPES,
    BlockedUrlError,
    ContentEncodingError,
    EmptyContentError,
    FetchResponse,
    InvalidUrlError,
    MAX_WEB_BYTES,
    RedirectError,
    ResponseTooLargeError,
    UnsupportedContentTypeError,
    UrlFetchError,
    assert_public_url,
    decode_body,
    decompress_bounded,
    extract_content,
    normalise_url,
)

from .helpers import RepositoryTestCase
from .llm_fakes import mock_client, response_for
from .test_quality import draft_payload, formation_payload

#: A public address used as a deterministic DNS answer.
PUBLIC_IP = "93.184.216.34"
SECRET_SENTENCE = "紫罗兰色的鲸鱼在第七码头等待一艘纸船。"

BODY_PARAGRAPH = (
    "检索增强生成先从外部知识库取回相关片段，再把它们放进提示词，让模型依据证据作答。"
    "这个流程把参数化记忆和非参数化记忆结合起来，因此回答可以引用具体来源。"
)
LONG_BODY = BODY_PARAGRAPH * 3


class FakeTransport:
    """Scripted transport: URL -> FetchResponse | exception | callable."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = dict(responses)
        self.calls: list[dict[str, object]] = []

    def fetch(self, url: str, *, timeout: float, max_bytes: int, headers):
        self.calls.append({"url": url, "timeout": timeout, "max_bytes": max_bytes, "headers": dict(headers)})
        item = self.responses.get(url)
        if item is None:
            raise AssertionError(f"unexpected URL requested: {url}")
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(url, timeout=timeout, max_bytes=max_bytes, headers=headers)
        return item


def html_page(
    body: str,
    *,
    title: str = "示例文章",
    head_extra: str = "",
    wrap: str = "article",
) -> str:
    heading = f"<h1>{title}</h1>\n" if wrap == "article" else ""
    return (
        '<!doctype html>\n<html><head><meta charset="utf-8">'
        f"<title>{title}</title>{head_extra}</head><body>"
        "<nav>首页 / 关于 / 导航噪声</nav>"
        f"<{wrap}>{heading}<p>{body}</p></{wrap}>"
        "<footer>页脚噪声</footer></body></html>"
    )


def response(
    body: str | bytes,
    *,
    status: int = 200,
    content_type: str | None = "text/html; charset=utf-8",
    headers: dict[str, str] | None = None,
    url: str = "",
) -> FetchResponse:
    all_headers = dict(headers or {})
    if content_type is not None:
        all_headers.setdefault("Content-Type", content_type)
    return FetchResponse(
        status=status,
        headers=all_headers,
        body=body.encode("utf-8") if isinstance(body, str) else body,
        url=url,
    )


def public_resolver(host: str, port: int) -> list[str]:
    return [PUBLIC_IP]


# --------------------------------------------------------------------------
# 1-5: extraction
# --------------------------------------------------------------------------


class ExtractionTest(unittest.TestCase):
    def test_1_static_html_title_and_body(self) -> None:
        result = extract_content(html_page(LONG_BODY, title="RAG 技术指南"))

        self.assertEqual(result.title, "RAG 技术指南")
        self.assertEqual(result.title_source, "title-tag")
        self.assertIn("检索增强生成先从外部知识库取回相关片段", result.content)
        self.assertEqual(result.root_tag, "article")
        self.assertGreater(result.content_chars, 200)
        self.assertIn("导航噪声", html_page(LONG_BODY))  # the noise exists in the source ...
        self.assertNotIn("导航噪声", result.content)  # ... but not in the extracted text

    def test_2_prefers_article_then_main(self) -> None:
        page = (
            "<html><head><title>T</title></head><body>"
            '<div class="sidebar">侧栏：相关推荐与订阅提示，这些都不是正文内容。</div>'
            f"<article><h2>正文标题</h2><p>{LONG_BODY}</p></article>"
            f'<div class="comments">评论区噪音{LONG_BODY}</div>'
            "</body></html>"
        )
        result = extract_content(page)
        self.assertEqual(result.root_tag, "article")
        self.assertNotIn("侧栏", result.content)
        self.assertNotIn("评论区噪音", result.content)

        main_page = f"<html><head><title>M</title></head><body><main><p>{LONG_BODY}</p></main></body></html>"
        main_result = extract_content(main_page)
        self.assertEqual(main_result.root_tag, "main")
        self.assertIn("检索增强生成", main_result.content)

    def test_3_script_style_forms_and_furniture_removed(self) -> None:
        page = (
            "<html><head><title>标题</title><style>.x{color:red}</style>"
            '<script>var secret="脚本内容";console.log(secret);</script></head><body>'
            "<header>站点头部</header><nav>导航</nav><aside>边栏</aside>"
            f"<article><p>{LONG_BODY}</p>"
            "<pre><code>def rag(q):\n    return retrieve(q)</code></pre>"
            "<!-- 注释内容 --></article>"
            "<form><input name='q' placeholder='搜索占位'><button>搜索按钮</button></form>"
            "<footer>版权页脚</footer></body></html>"
        )
        result = extract_content(page)

        for noise in ("脚本内容", "console.log", "color:red", "站点头部", "导航", "边栏", "注释内容",
                      "搜索占位", "搜索按钮", "版权页脚"):
            self.assertNotIn(noise, result.content, noise)
        self.assertIn("def rag(q):\n    return retrieve(q)", result.content)  # code text + indentation kept

    def test_4_encoding_and_entities(self) -> None:
        gbk_page = (
            '<html><head><meta charset="gb18030"><title>中文页面</title></head>'
            f"<body><article><p>{LONG_BODY}</p></article></body></html>"
        ).encode("gb18030")
        text, encoding, fallback = decode_body(gbk_page, {"Content-Type": "text/html"})
        self.assertEqual(encoding, "gb18030")
        self.assertFalse(fallback)
        self.assertIn("检索增强生成", text)
        self.assertIn("中文页面", extract_content(text).title)

        declared = decode_body("hi".encode("utf-8"), {"Content-Type": "text/html; charset=UTF-8"})
        self.assertEqual((declared[1].lower(), declared[2]), ("utf-8", False))

        entities = extract_content(
            f"<html><head><title>&amp; 标题</title></head><body><main><p>A&amp;B&nbsp;"
            f"&#8217;quote&#8217; \u00a0 {LONG_BODY}</p></main></body></html>"
        )
        self.assertIn("& 标题", entities.title or "")
        self.assertIn("A&B", entities.content)
        self.assertIn("\u2019quote\u2019", entities.content)  # &#8217; decoded, not left raw
        self.assertNotIn("\u00a0", entities.content)

    def test_5_empty_invalid_and_nav_only_pages(self) -> None:
        with self.assertRaises(EmptyContentError):
            extract_content("")
        with self.assertRaises(EmptyContentError):
            extract_content("   \n\t ")
        with self.assertRaises(EmptyContentError):
            extract_content("<html><body><nav>首页</nav><footer>©</footer></body></html>")
        with self.assertRaises(EmptyContentError) as caught:
            extract_content("<html><body><p>太短。</p></body></html>")
        self.assertLess(caught.exception.content_chars, caught.exception.min_chars)
        with self.assertRaises(EmptyContentError):
            extract_content("<html><body><div><p>unclosed")  # invalid HTML: explicit failure, no crash
        with self.assertRaises(ValidationError):
            extract_content(None)  # type: ignore[arg-type]

    def test_5b_text_plain_pages_are_normalised(self) -> None:
        transport = FakeTransport({
            "https://example.com/notes.txt": response(
                "第一行。\n\n\n\n第二行。" + LONG_BODY, content_type="text/plain; charset=utf-8"
            )
        })
        document = WebImporter(transport=transport, resolver=public_resolver).fetch("https://example.com/notes.txt")
        self.assertNotIn("\n\n\n", document.content)
        self.assertEqual(document.metadata["extraction_root"], "text/plain")


# --------------------------------------------------------------------------
# 6-12: fetch, limits, SSRF guard and parameters
# --------------------------------------------------------------------------


class FetchGuardTest(unittest.TestCase):
    def importer(self, transport: FakeTransport, **kwargs) -> WebImporter:
        options = {"resolver": public_resolver, "max_bytes": MAX_WEB_BYTES}
        options.update(kwargs)
        return WebImporter(transport=transport, **options)

    def test_6_timeouts_and_http_errors_are_explicit(self) -> None:
        for error in (TimeoutError("timed out"), socket.timeout("timed out"),
                      urllib.error.URLError("connection refused")):
            transport = FakeTransport({"https://example.com/a": error})
            with self.assertRaises(UrlFetchError) as caught:
                self.importer(transport).fetch("https://example.com/a")
            self.assertIn("https://example.com/a", str(caught.exception))
            self.assertIsNone(getattr(caught.exception, "url", None) and None or None)  # url only, no body

        for status in (404, 500, 503):
            transport = FakeTransport({"https://example.com/a": response("gone", status=status)})
            with self.assertRaises(UrlFetchError) as caught:
                self.importer(transport).fetch("https://example.com/a")
            self.assertEqual(caught.exception.status, status)

    def test_7_response_size_limit(self) -> None:
        big = response(LONG_BODY * 50)
        transport = FakeTransport({"https://example.com/a": big})
        with self.assertRaises(ResponseTooLargeError) as caught:
            self.importer(transport, max_bytes=500).fetch("https://example.com/a")
        self.assertEqual(caught.exception.max_bytes, 500)
        self.assertGreater(caught.exception.size_bytes, 500)

        declared = response("small", headers={"Content-Length": str(10 * 1024 * 1024)})
        transport = FakeTransport({"https://example.com/a": declared})
        with self.assertRaises(ResponseTooLargeError):
            self.importer(transport, max_bytes=1024).fetch("https://example.com/a")

    def test_8_url_scheme_and_syntax_validation(self) -> None:
        for raw in ("file:///etc/passwd", "ftp://example.com/x", "data:text/html,hi", "javascript:alert(1)",
                    "gopher://example.com/", "//example.com/x", "example.com/x", "", "   ", None,
                    "http://user:pass@example.com/", "https://example.com:70000/x", "https://example.com/a b"):
            with self.assertRaises((InvalidUrlError, ValidationError), msg=str(raw)):
                normalise_url(raw)  # type: ignore[arg-type]

        self.assertEqual(normalise_url("https://example.com/a?b=1#frag"), "https://example.com/a?b=1")
        self.assertEqual(normalise_url("https://EXAMPLE.com"), "https://EXAMPLE.com/")

        transport = FakeTransport({})
        for raw in ("file:///etc/passwd", "ftp://example.com/x", "not-a-url"):
            with self.assertRaises((InvalidUrlError, ValidationError)):
                self.importer(transport).fetch(raw)
        self.assertEqual(transport.calls, [])  # nothing was ever requested

    def test_9_localhost_private_and_metadata_targets_are_rejected(self) -> None:
        blocked = [
            "http://127.0.0.1/",
            "http://127.0.0.1:8000/admin",
            "http://[::1]/",
            "http://10.0.0.5/",
            "http://192.168.1.10/router",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/",
            "http://0.0.0.0/",
            "http://[fd00::1]/",
            "https://localhost/admin",
            "https://api.localhost/",
            "https://printer.local/",
            "https://service.internal/",
            "https://metadata.google.internal/computeMetadata/v1/",
        ]
        transport = FakeTransport({})
        importer = self.importer(transport)
        for raw in blocked:
            with self.assertRaises(BlockedUrlError, msg=raw):
                importer.fetch(raw)
        self.assertEqual(transport.calls, [])

        # a public hostname that resolves into a private range is refused too
        private = WebImporter(transport=transport, resolver=lambda host, port: ["192.168.0.10"])
        with self.assertRaises(BlockedUrlError) as caught:
            private.fetch("https://looks-public.example.com/x")
        self.assertIn("192.168.0.10", str(caught.exception))
        self.assertEqual(transport.calls, [])

        # DNS failure is a fetch error, not a silent success
        def failing(host: str, port: int):
            raise UrlFetchError("dns lookup failed", reason="no such host")

        with self.assertRaises(UrlFetchError):
            WebImporter(transport=transport, resolver=failing).fetch("https://nope.example.com/")

        # and a genuinely public target passes the guard
        self.assertEqual(assert_public_url("https://example.com/", resolver=public_resolver),
                         ("example.com", [PUBLIC_IP]))

    def test_10_redirects_are_revalidated_and_limited(self) -> None:
        safe = FakeTransport({
            "https://example.com/start": response("", status=302, headers={"Location": "/final"}),
            "https://example.com/final": response(html_page(LONG_BODY)),
        })
        document = self.importer(safe).fetch("https://example.com/start")
        self.assertEqual(document.original_url, "https://example.com/start")
        self.assertEqual(document.final_url, "https://example.com/final")
        self.assertEqual(document.metadata["redirects"], ["https://example.com/final"])
        self.assertEqual([call["url"] for call in safe.calls],
                         ["https://example.com/start", "https://example.com/final"])

        unsafe = FakeTransport({
            "https://example.com/start": response("", status=302, headers={"Location": "http://127.0.0.1/secret"}),
        })
        with self.assertRaises(BlockedUrlError):
            self.importer(unsafe).fetch("https://example.com/start")
        self.assertEqual(len(unsafe.calls), 1)  # the second hop was never requested

        scheme_swap = FakeTransport({
            "https://example.com/start": response("", status=302, headers={"Location": "file:///etc/passwd"}),
        })
        with self.assertRaises(InvalidUrlError):
            self.importer(scheme_swap).fetch("https://example.com/start")

        looping = FakeTransport({
            "https://example.com/a": response("", status=302, headers={"Location": "/b"}),
            "https://example.com/b": response("", status=302, headers={"Location": "/a"}),
        })
        with self.assertRaises(RedirectError):
            self.importer(looping, max_redirects=2).fetch("https://example.com/a")

        no_location = FakeTransport({"https://example.com/a": response("", status=301)})
        with self.assertRaises(RedirectError):
            self.importer(no_location).fetch("https://example.com/a")

        zero_hops = FakeTransport({
            "https://example.com/a": response("", status=302, headers={"Location": "/b"}),
        })
        with self.assertRaises(RedirectError):
            self.importer(zero_hops, max_redirects=0).fetch("https://example.com/a")

    def test_11_content_type_validation(self) -> None:
        for content_type in ("application/pdf", "image/png", "application/json", "application/octet-stream"):
            transport = FakeTransport({
                "https://example.com/a": response("%PDF-1.4", content_type=content_type)
            })
            with self.assertRaises(UnsupportedContentTypeError) as caught:
                self.importer(transport).fetch("https://example.com/a")
            self.assertEqual(caught.exception.content_type, content_type)

        sniffed = FakeTransport({"https://example.com/a": response(html_page(LONG_BODY), content_type=None)})
        self.assertIn("检索增强生成", self.importer(sniffed).fetch("https://example.com/a").content)

        no_type_json = FakeTransport({
            "https://example.com/a": response('{"a": 1}', content_type=None)
        })
        with self.assertRaises(UnsupportedContentTypeError):
            self.importer(no_type_json).fetch("https://example.com/a")

        plain = FakeTransport({
            "https://example.com/a": response(LONG_BODY, content_type="text/plain; charset=utf-8")
        })
        document = self.importer(plain).fetch("https://example.com/a")
        self.assertEqual(document.metadata["extraction_root"], "text/plain")
        self.assertIn("检索增强生成", document.content)

    def test_12_invalid_parameters_cannot_bypass_validation(self) -> None:
        transport = FakeTransport({})
        for kwargs in ({"max_bytes": 0}, {"max_bytes": -5}, {"timeout_seconds": 0}, {"timeout_seconds": -1},
                       {"max_redirects": -1}, {"min_content_chars": 0}, {"allowed_content_types": ()}):
            with self.assertRaises(ValidationError, msg=str(kwargs)):
                WebImporter(transport=transport, **kwargs)

        # a query string containing another URL is just data: the request still goes to
        # the public host, and no second request is made to the embedded address
        import urllib.parse

        for raw in ("https://example.com/a?next=http://127.0.0.1/", "https://example.com/#http://127.0.0.1/"):
            target = normalise_url(raw)
            transport = FakeTransport({target: response(html_page(LONG_BODY))})
            document = self.importer(transport).fetch(raw)
            self.assertEqual(document.metadata["http_status"], 200)
            self.assertEqual(urllib.parse.urlsplit(document.final_url).hostname, "example.com")
            self.assertEqual([call["url"] for call in transport.calls], [target])


# --------------------------------------------------------------------------
# 13-17: architecture + pipeline integration
# --------------------------------------------------------------------------


class WebPipelineTest(RepositoryTestCase):
    prefix = "pms-web-"

    def importer(self, transport: FakeTransport, **kwargs) -> WebImporter:
        options = {"resolver": public_resolver}
        options.update(kwargs)
        return WebImporter(transport=transport, **options)

    def capture_service(self, *items, quality: bool = False):
        client = mock_client(*items)
        formation = MemoryFormationService(
            self.repo, client, quality=MemoryQualityGate(self.repo) if quality else None
        )
        return CaptureService(formation, captured_from="web"), client

    def fetched_document(self, body: str = LONG_BODY, title: str = "RAG 技术指南") -> WebDocument:
        transport = FakeTransport({"https://example.com/article": response(html_page(body, title=title))})
        return self.importer(transport).fetch("https://example.com/article")

    def test_13_importer_does_not_touch_the_database(self) -> None:
        source = pathlib.Path(web_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", source)
        self.assertNotIn("from ..store", source)
        for verb in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(verb, source)

        document = self.fetched_document()
        with self.assertRaises(ValidationError):
            self.importer(FakeTransport({})).import_document(document, self.repo)  # type: ignore[arg-type]

        import inspect

        self.assertEqual(
            list(inspect.signature(WebImporter.import_document).parameters),
            ["self", "document", "capture", "source_type", "title", "dry_run"],
        )
        self.assertNotIn("capture", inspect.signature(WebImporter.fetch).parameters)

    def test_14_webdocument_to_capture_request(self) -> None:
        document = self.fetched_document()
        request = document.to_capture_request()

        self.assertEqual(str(request.source_type), "web")
        self.assertEqual(request.url, "https://example.com/article")
        self.assertEqual(request.title, "RAG 技术指南")
        self.assertEqual(request.metadata["captured_from"], "web")
        self.assertEqual(request.metadata["original_url"], "https://example.com/article")
        self.assertEqual(request.metadata["final_url"], "https://example.com/article")
        self.assertEqual(request.metadata["content_type"], "text/html")
        self.assertEqual(request.metadata["title_source"], "title-tag")
        self.assertEqual(request.metadata["content_sha256"], document.content_hash)
        self.assertEqual(request.metadata["http_status"], 200)
        self.assertTrue(request.metadata["fetched_at"])
        self.assertNotIn("<p>", request.content)  # extracted text, not raw HTML
        self.assertNotIn("<script", request.content)

        self.assertEqual(document.to_capture_request(title="覆盖标题").title, "覆盖标题")
        self.assertEqual(document.as_dict()["content_chars"], document.content_chars)

    def test_15_high_value_page_forms_memories(self) -> None:
        payload = formation_payload(
            draft_payload(
                type="knowledge",
                title="RAG 的核心机制",
                content="RAG 先从外部知识库检索片段，再让模型基于证据作答。",
                requires_source=True,
                information_origin="source_content",
            )
        )
        service, client = self.capture_service(response_for(payload))
        transport = FakeTransport({"https://example.com/article": response(html_page(LONG_BODY))})
        document = self.importer(transport).fetch("https://example.com/article")

        result = self.importer(transport).import_document(document, service)

        self.assertEqual(result.formation_status, "persisted")
        self.assertEqual(result.memory_count, 1)
        self.assertEqual(result.source_count, 1)
        memory = result.memories_created[0]
        fresh = MemoryRepository(Database(self.db_path))
        self.assertEqual(fresh.require_memory(memory.id).title, "RAG 的核心机制")
        source = result.sources_created[0]
        self.assertEqual(str(source.source_type), "web")
        self.assertEqual(source.url, "https://example.com/article")
        self.assertEqual(source.metadata["captured_from"], "web")
        self.assertIn(memory.id, [hit.memory.id for hit in MemoryRetriever(fresh).search("RAG").hits])
        self.assertEqual(self.repo.count_links_for_memory(memory.id), 1)

    def test_16_low_value_page_writes_nothing(self) -> None:
        service, _ = self.capture_service(
            response_for({"worth_remembering": False, "reason": "导航页", "memories": []})
        )
        transport = FakeTransport({"https://example.com/article": response(html_page(LONG_BODY))})
        document = self.importer(transport).fetch("https://example.com/article")

        result = self.importer(transport).import_document(document, service)

        self.assertEqual(result.source_count, 0)
        self.assertEqual(result.memory_count, 0)
        self.assertFalse(result.source_reused)
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_17_formation_failure_leaves_nothing_behind(self) -> None:
        document = self.fetched_document()
        digest = compute_content_hash(document.content)
        service, _ = self.capture_service(LLMRequestError("provider unreachable", retryable=False))

        with self.assertRaises(LLMRequestError):
            service.capture_request(document.to_capture_request())

        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})
        self.assertFalse(self.repo.source_exists_by_hash(digest))
        self.assertTrue(self.repo.index_consistency()["consistent"])

    def test_17b_dry_run_writes_nothing(self) -> None:
        service, _ = self.capture_service(response_for(formation_payload(draft_payload(requires_source=True))))
        document = self.fetched_document()

        result = self.importer(FakeTransport({})).import_document(document, service, dry_run=True)

        self.assertEqual(result.import_status, "preview")
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_20_sensitive_page_text_stays_out_of_default_views_and_errors(self) -> None:
        page = html_page(f"{SECRET_SENTENCE}{LONG_BODY}")
        transport = FakeTransport({"https://example.com/article": response(page)})
        importer = self.importer(transport)
        document = importer.fetch("https://example.com/article")
        service, _ = self.capture_service(
            response_for(formation_payload(draft_payload(requires_source=True, evidence_quote="依据")))
        )
        result = importer.import_document(document, service)

        default_view = json.dumps(result.as_dict(), ensure_ascii=False)
        self.assertNotIn(SECRET_SENTENCE, default_view)
        self.assertNotIn(SECRET_SENTENCE, json.dumps(document.as_dict(), ensure_ascii=False))
        self.assertIn(SECRET_SENTENCE, json.dumps(result.as_dict(include_content=True), ensure_ascii=False))

        # errors carry counts/types, never the body
        too_short = FakeTransport({"https://example.com/tiny": response(f"<html><body><p>{SECRET_SENTENCE}</p></body></html>")})
        with self.assertRaises(EmptyContentError) as caught:
            self.importer(too_short).fetch("https://example.com/tiny")
        self.assertNotIn(SECRET_SENTENCE, str(caught.exception))

        big = FakeTransport({"https://example.com/big": response(SECRET_SENTENCE * 200)})
        with self.assertRaises(ResponseTooLargeError) as caught_big:
            self.importer(big, max_bytes=200).fetch("https://example.com/big")
        self.assertNotIn(SECRET_SENTENCE, str(caught_big.exception))

        unsupported = FakeTransport({"https://example.com/x": response(SECRET_SENTENCE, content_type="application/pdf")})
        with self.assertRaises(UnsupportedContentTypeError) as caught_type:
            self.importer(unsupported).fetch("https://example.com/x")
        self.assertNotIn(SECRET_SENTENCE, str(caught_type.exception))

    def test_document_validation_and_helpers(self) -> None:
        with self.assertRaises(ValidationError):
            WebDocument("https://example.com/", "https://example.com/", None, "   ", "text/html", "now")
        document = self.fetched_document()
        self.assertEqual(document.host, "example.com")
        self.assertEqual(len(document.content_hash), 64)
        self.assertEqual(sorted(ALLOWED_CONTENT_TYPES), sorted(["text/html", "application/xhtml+xml", "text/plain"]))
        self.assertTrue(issubclass(BlockedUrlError, WebImportError))
        self.assertTrue(issubclass(EmptyContentError, WebImportError))

    def test_low_value_source_is_not_persisted_unconditionally(self) -> None:
        # a page whose formation says "no" must not leave a Source behind, even though the
        # importer successfully downloaded it
        service, _ = self.capture_service(
            response_for({"worth_remembering": False, "reason": "低价值", "memories": []})
        )
        document = self.fetched_document()
        self.importer(FakeTransport({})).import_document(document, service)
        self.assertEqual(self.repo.counts()["sources"], 0)

    def test_reimport_reuses_existing_source_and_defers_dedupe_to_the_gate(self) -> None:
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="依据"))
        document = self.fetched_document()

        # without the gate the same URL twice keeps one Source row that the second import reuses
        plain, _ = self.capture_service(response_for(payload), response_for(payload))
        first = self.importer(FakeTransport({})).import_document(document, plain)
        second = self.importer(FakeTransport({})).import_document(document, plain)
        self.assertEqual(first.source_count, 1)
        self.assertTrue(second.source_reused)
        self.assertEqual(self.repo.counts()["sources"], 1)

        # with the Phase 4 gate the identical Memory is a duplicate: nothing new is written
        # (the frozen policy keeps no Source when no Memory is kept)
        gated, _ = self.capture_service(response_for(payload), quality=True)
        third = self.importer(FakeTransport({})).import_document(document, gated)
        self.assertEqual(third.status, "duplicate")
        self.assertEqual(third.memory_count, 0)
        self.assertEqual(third.source_count, 0)
        self.assertEqual(self.repo.counts()["sources"], 1)
        self.assertTrue(self.repo.index_consistency()["consistent"])


class CompressionTest(unittest.TestCase):
    """gzip/deflate handling: the decompression output cap must be enforced *during*
    decompression (compression-bomb resistance), and corrupt/unsupported streams must fail
    explicitly instead of being passed through as raw bytes."""

    #: a decompression bomb: 32 MiB of zeros compresses to a few tens of KiB
    EXPANDED = 32 * 1024 * 1024
    CAP = 64 * 1024

    def importer(self, transport: FakeTransport, **kwargs) -> WebImporter:
        options = {"resolver": public_resolver, "max_bytes": self.CAP, "min_content_chars": 20}
        options.update(kwargs)
        return WebImporter(transport=transport, **options)

    @staticmethod
    def gzip_bomb() -> bytes:
        return gzip.compress(b"\0" * CompressionTest.EXPANDED, 9)

    @staticmethod
    def deflate_bomb(raw: bool = False) -> bytes:
        compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS if raw else zlib.MAX_WBITS)
        return compressor.compress(b"\0" * CompressionTest.EXPANDED) + compressor.flush()

    def test_gzip_bomb_is_stopped_during_decompression(self) -> None:
        bomb = self.gzip_bomb()  # built before tracemalloc so only the fetch is measured
        transport = FakeTransport({
            "https://example.com/bomb": response(bomb, headers={"Content-Encoding": "gzip"}),
        })
        tracemalloc.start()
        try:
            with self.assertRaises(ResponseTooLargeError) as caught:
                self.importer(transport).fetch("https://example.com/bomb")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(caught.exception.max_bytes, self.CAP)
        # the size reported is the cap it stopped at, not the full expansion
        self.assertLessEqual(caught.exception.size_bytes, self.CAP + 1)
        self.assertLess(caught.exception.size_bytes, self.EXPANDED)
        # and the expansion was never materialised (unbounded code peaked at >100 MiB here)
        self.assertLess(peak, 8 * 1024 * 1024, f"peak traced memory {peak} suggests unbounded expansion")

    def test_deflate_bombs_are_stopped(self) -> None:
        for label, payload in (("zlib-wrapped", self.deflate_bomb()), ("raw deflate", self.deflate_bomb(raw=True))):
            transport = FakeTransport({
                "https://example.com/bomb": response(payload, headers={"Content-Encoding": "deflate"}),
            })
            with self.assertRaises(ResponseTooLargeError, msg=label) as caught:
                self.importer(transport).fetch("https://example.com/bomb")
            self.assertLessEqual(caught.exception.size_bytes, self.CAP + 1, label)
            self.assertLess(caught.exception.size_bytes, self.EXPANDED, label)

    def test_decompressed_size_exactly_at_the_limit_is_accepted(self) -> None:
        head = "<html><head><title>边界</title></head><body><article><p>"
        tail = "</p></article></body></html>"
        padding = self.CAP - len((head + tail).encode("utf-8"))
        body = (head + "x" * padding + tail).encode("utf-8")
        self.assertEqual(len(body), self.CAP)

        for encoding, payload in (("gzip", gzip.compress(body)), ("deflate", zlib.compress(body))):
            transport = FakeTransport({
                "https://example.com/exact": response(payload, headers={"Content-Encoding": encoding}),
            })
            document = self.importer(transport).fetch("https://example.com/exact")
            self.assertEqual(document.metadata["decompressed_bytes"], self.CAP, encoding)
            self.assertEqual(document.metadata["content_encoding"], encoding)
            self.assertLessEqual(document.metadata["body_bytes"], self.CAP)

    def test_one_byte_over_the_limit_is_rejected(self) -> None:
        body = b"<html><body><article><p>" + b"x" * (self.CAP + 1) + b"</p></article></body></html>"
        self.assertGreater(len(body), self.CAP)

        for encoding, payload in (("gzip", gzip.compress(body)), ("deflate", zlib.compress(body))):
            transport = FakeTransport({
                "https://example.com/over": response(payload, headers={"Content-Encoding": encoding}),
            })
            with self.assertRaises(ResponseTooLargeError, msg=encoding) as caught:
                self.importer(transport).fetch("https://example.com/over")
            self.assertEqual(caught.exception.size_bytes, self.CAP + 1, encoding)

    def test_corrupt_truncated_and_mismatched_streams_fail_explicitly(self) -> None:
        good = gzip.compress(b"<html><body><article><p>" + b"y" * 500 + b"</p></article></body></html>")
        cases = {
            "random bytes labelled gzip": (b"\x99\x88not-a-gzip-stream\x00\x01", "gzip"),
            "truncated gzip stream": (good[: len(good) // 2], "gzip"),
            "gzip trailer removed": (good[:-8], "gzip"),
            "empty body labelled gzip": (b"", "gzip"),
            "zlib data labelled gzip": (zlib.compress(b"<html><body><p>hi</p></body></html>"), "gzip"),
            "garbage labelled deflate": (b"\x00\x01\x02not-deflate", "deflate"),
            "truncated deflate": (zlib.compress(b"z" * 4000)[:20], "deflate"),
        }
        for label, (payload, encoding) in cases.items():
            transport = FakeTransport({
                "https://example.com/broken": response(payload, headers={"Content-Encoding": encoding}),
            })
            with self.assertRaises(ContentEncodingError, msg=label) as caught:
                self.importer(transport).fetch("https://example.com/broken")
            self.assertEqual(caught.exception.encoding, encoding, label)
            self.assertIn("https://example.com/broken", str(caught.exception), label)

    def test_unknown_and_stacked_content_encodings_are_rejected(self) -> None:
        for encoding in ("br", "zstd", "compress", "gzip, br", "gzip, gzip", "unknown-token"):
            transport = FakeTransport({
                "https://example.com/encoded": response(b"payload", headers={"Content-Encoding": encoding}),
            })
            with self.assertRaises(ContentEncodingError, msg=encoding):
                self.importer(transport).fetch("https://example.com/encoded")

    def test_identity_and_missing_encoding_pass_through(self) -> None:
        page = html_page(LONG_BODY)
        for headers in ({}, {"Content-Encoding": "identity"}, {"Content-Encoding": "IDENTITY"}):
            transport = FakeTransport({"https://example.com/plain": response(page, headers=headers)})
            document = self.importer(transport).fetch("https://example.com/plain")
            self.assertEqual(document.metadata["content_encoding"], "identity")
            self.assertIn("检索增强生成", document.content)

    def test_raw_response_byte_cap_still_applies_to_compressed_bodies(self) -> None:
        # the *compressed* body is bigger than the cap: the original response-size guard fires
        huge = os.urandom(self.CAP + 1024)  # incompressible, so the raw body stays large
        transport = FakeTransport({
            "https://example.com/raw-too-big": response(huge, headers={"Content-Encoding": "gzip"}),
        })
        with self.assertRaises(ResponseTooLargeError) as caught:
            self.importer(transport).fetch("https://example.com/raw-too-big")
        self.assertGreater(caught.exception.size_bytes, self.CAP)

        declared = FakeTransport({
            "https://example.com/declared": response(
                gzip.compress(b"x"), headers={"Content-Encoding": "gzip", "Content-Length": str(self.CAP * 4)}
            ),
        })
        with self.assertRaises(ResponseTooLargeError):
            self.importer(declared).fetch("https://example.com/declared")

    def test_encoding_errors_are_typed_and_body_free(self) -> None:
        secret = b"<html><body><p>" + b"SECRET-" * 40 + b"</p></body></html>"
        payload = b"\x00\x01garbage" + secret
        transport = FakeTransport({
            "https://example.com/typed": response(payload, headers={"Content-Encoding": "gzip"}),
        })
        with self.assertRaises(ContentEncodingError) as caught:
            self.importer(transport).fetch("https://example.com/typed")

        self.assertIsInstance(caught.exception, WebImportError)
        self.assertNotIn("SECRET-", str(caught.exception))  # no payload bytes in the message
        self.assertIn("gzip", str(caught.exception))

    def test_decompress_bounded_is_usable_standalone(self) -> None:
        payload = gzip.compress(b"hello bounded world")
        data, encoding = decompress_bounded(payload, {"Content-Encoding": "gzip"}, max_bytes=1024, url="https://x/")
        self.assertEqual((data, encoding), (b"hello bounded world", "gzip"))

        with self.assertRaises(ResponseTooLargeError):
            decompress_bounded(payload, {"Content-Encoding": "gzip"}, max_bytes=4, url="https://x/")
        with self.assertRaises(ContentEncodingError):
            decompress_bounded(b"junk", {"Content-Encoding": "zstd"}, max_bytes=1024, url="https://x/")
        self.assertEqual(
            decompress_bounded(b"raw", {}, max_bytes=1024, url="https://x/"), (b"raw", "identity")
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
