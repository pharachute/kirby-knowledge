"""Knowledge Base MVP (local Web UI) tests.

Covers the 18 required behaviours of the MVP spec plus the boundaries this project cares
about: HTML escaping, friendly error mapping, upload sanitising, temp-file cleanup, the
POST/redirect/GET flash pattern, and proof that the HTTP layer reuses the frozen modules
instead of reimplementing them.

Every test runs offline: a scripted Mock LLM is injected into the web layer, and the
HTTP server is a real ``ThreadingHTTPServer`` on ``127.0.0.1:0`` driven over real HTTP.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import pathlib
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

import personal_memory.web.server as web_server
from personal_memory import (
    Database,
    InformationOrigin,
    MemoryLifecycle,
    MemoryRepository,
    MemoryRetriever,
    MemoryStatus,
    MemoryType,
    SourceType,
    ValidationError,
)
from personal_memory.llm import LLMConfig
from personal_memory.models import Memory, Source
from personal_memory.importers.web import WebImporter
from personal_memory.web import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    WebContext,
    classify_error,
    create_server,
)

from .helpers import RepositoryTestCase, example_memory, example_source
from .llm_fakes import mock_client, response_for
from .test_quality import draft_payload, formation_payload

#: A recognizable fake credential: it must never appear in HTML, responses or logs.
FAKE_API_KEY = "sk-test-SECRET-ui-000000000000"

URLENCODED = "application/x-www-form-urlencoded"

VALID_CAPTURE_PAYLOAD = formation_payload(
    draft_payload(
        type="knowledge",
        title="RAG 的核心机制",
        content="RAG 通过检索外部知识为模型提供上下文。",
        evidence_quote="RAG 是检索增强生成",
        requires_source=True,  # so the pipeline also creates a Source to inspect
    )
)


class WebTestCase(RepositoryTestCase):
    """Base: a real HTTP server on a free loopback port + an injected Mock LLM."""

    prefix = "pms-web-"

    def setUp(self) -> None:
        super().setUp()
        self.servers: list[tuple[object, threading.Thread]] = []

    def tearDown(self) -> None:
        for server, thread in self.servers:
            with contextlib.suppress(Exception):
                server.shutdown()
            with contextlib.suppress(Exception):
                server.server_close()
            thread.join(timeout=5)
        super().tearDown()

    # -- context / server -------------------------------------------------
    def make_context(self, *items, quality_check: bool = False, with_llm: bool = True) -> WebContext:
        context = WebContext.create(self.db_path, config_path=None, quality_check=quality_check)
        if with_llm:
            context.llm_config = LLMConfig(
                provider="mock",
                model="mock-model",
                base_url="https://mock.invalid",
                api_key=FAKE_API_KEY,
            )
            context.llm_error = None
            self.client = mock_client(*items)
            patcher = mock.patch.object(web_server, "LLMClient", lambda config: self.client)
            patcher.start()
            self.addCleanup(patcher.stop)
        return context

    def start(self, context: WebContext) -> str:
        server = create_server(context, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        self.server = server
        return f"http://127.0.0.1:{server.server_address[1]}"

    # -- HTTP helpers -----------------------------------------------------
    def get(self, path: str) -> tuple[int, str]:
        path = urllib.parse.quote(path, safe="/?=&%+")
        try:
            with urllib.request.urlopen(self.base + path, timeout=15) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def post(self, path: str, data: dict[str, object], *, headers: dict[str, str] | None = None):
        body = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=body, method="POST")
        request.add_header("Content-Type", URLENCODED)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.read().decode("utf-8"), response.url
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8"), error.url

    def no_redirect_post(self, path: str, data: dict[str, object]):
        """POST without following the 303, so the redirect itself can be asserted."""

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
                return None

        opener = urllib.request.build_opener(NoRedirect)
        body = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=body, method="POST")
        request.add_header("Content-Type", URLENCODED)
        try:
            with opener.open(request, timeout=15) as response:
                return response.status, dict(response.headers), response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read().decode("utf-8")

    @staticmethod
    def upload(name: str, text: str) -> dict[str, str]:
        return {"filename": name, "content_base64": base64.b64encode(text.encode("utf-8")).decode("ascii")}


# --------------------------------------------------------------------------
# 1-2: server + home
# --------------------------------------------------------------------------


class ServerLifecycleTest(WebTestCase):
    def test_1_server_starts_and_binds_loopback(self) -> None:
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        status, body = self.get("/")

        self.assertEqual(status, 200)
        # the home page is now 卡比 Capture Home (喂知识), see tests/test_feed_home.py
        self.assertIn("把知识喂给我", body)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertNotEqual(self.server.server_address[1], 0)

    def test_16_default_bind_is_localhost_only(self) -> None:
        import inspect

        from personal_memory.cli import build_parser

        self.assertEqual(DEFAULT_HOST, "127.0.0.1")
        self.assertEqual(DEFAULT_PORT, 8765)
        self.assertEqual(inspect.signature(create_server).parameters["host"].default, "127.0.0.1")
        args = build_parser().parse_args(["web", "--db", "x.db"])
        self.assertEqual((args.host, args.port), ("127.0.0.1", 8765))

        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        server = create_server(context, port=0)  # port 0: do not fight for the default port
        self.assertEqual(server.server_address[0], "127.0.0.1")  # host still defaults to loopback
        server.server_close()

    def test_2_home_is_minimal_and_health_responds(self) -> None:
        """The dashboard was deliberately removed from the home page (spec §二).

        Counts and technical state now live on /memories and /capture; the home page is just
        the creature and the input.
        """
        self.make_memory(title="A")
        self.make_memory(title="B", status=MemoryStatus.PENDING)
        self.make_memory(title="C", status=MemoryStatus.ARCHIVED)
        self.make_source(title="来源")
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        status, body = self.get("/")
        health, health_body = self.get("/healthz")

        self.assertEqual((status, health), (200, 200))
        self.assertEqual(health_body.strip(), "ok")
        self.assertNotIn('class="stat"', body)   # no statistics cards on the home page
        self.assertNotIn("active=1", body)       # no dashboard counters either
        self.assertNotIn("库内总计", body)
        self.assertNotIn("API Key", body)
        # the counts are still available where they belong
        # 计数现在以中文筛选标签呈现（见 tests/test_memories_ui.py）
        memories_body = self.get("/memories?status=all")[1]
        for label in ("全部", "待确认", "已记住", "已归档"):
            self.assertIn(label, memories_body, label)
        self.assertEqual(memories_body.count("<em>1</em>"), 3, "三个状态标签各 1 条")
        self.assertEqual(memories_body.count("<em>3</em>"), 1, "全部显示总数 3")


# --------------------------------------------------------------------------
# 3-5: reading pages
# --------------------------------------------------------------------------


class ReadPagesTest(WebTestCase):
    def test_3_memories_page_reads_and_filters(self) -> None:
        active = self.make_memory(title="活跃记忆")
        pending = self.make_memory(title="待审记忆", status=MemoryStatus.PENDING)
        archived = self.make_memory(title="归档记忆", status=MemoryStatus.ARCHIVED)
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

        default_status, default_body = self.get("/memories")
        all_status, all_body = self.get("/memories?status=all")
        archived_status, archived_body = self.get("/memories?status=archived")

        self.assertEqual((default_status, all_status, archived_status), (200, 200, 200))
        self.assertIn("活跃记忆", default_body)
        self.assertNotIn("待审记忆", default_body)
        self.assertNotIn("归档记忆", default_body)
        for title in ("活跃记忆", "待审记忆", "归档记忆"):
            self.assertIn(title, all_body)
        self.assertIn("归档记忆", archived_body)
        self.assertNotIn(active.id, archived_body)
        self.assertIn(pending.id, all_body)

    def test_3b_memories_page_rejects_unknown_status(self) -> None:
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))
        status, body = self.get("/memories?status=weird")
        self.assertEqual(status, 400)
        self.assertIn("ValidationError", body)

    def test_4_sources_page_and_detail(self) -> None:
        source = self.make_source(title="RAG 基础", content="RAG 是检索增强生成。",
                                  metadata={"origin": "test", "api_key": "should-not-render"})
        memory = self.make_memory(title="RAG 记忆")
        self.repo.link(memory.id, source.id)
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

        list_status, list_body = self.get("/sources")
        detail_status, detail_body = self.get(f"/sources/{source.id}")

        self.assertEqual((list_status, detail_status), (200, 200))
        self.assertIn("RAG 基础", list_body)
        self.assertIn("RAG 是检索增强生成。", detail_body)
        self.assertIn(memory.id, detail_body)
        # 内部元数据不再以键值表展示，只用一句中文提示（敏感值永不渲染）
        self.assertIn("项内部信息未展示", detail_body)
        self.assertNotIn("should-not-render", detail_body)
        self.assertNotIn("api_key", detail_body)

    def test_5_search_uses_the_retriever(self) -> None:
        found = self.make_memory(title="RAG 的核心机制", content="RAG 通过检索外部知识为模型提供上下文。")
        hidden = self.make_memory(title="归档的 RAG 笔记", content="RAG 归档内容。",
                                  status=MemoryStatus.ARCHIVED)
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

        status, body = self.get("/search?q=RAG")
        all_status, all_body = self.get("/search?q=RAG&status=all")

        self.assertEqual((status, all_status), (200, 200))
        self.assertIn(found.id, body)
        self.assertNotIn(hidden.id, body)  # default status=active
        self.assertIn(hidden.id, all_body)
        # 技术字段（score / mode / 耗时）已从搜索页移除，改为卡片 + 中文状态（见 tests/test_search_ui.py）
        self.assertNotIn("score=", all_body)
        self.assertNotIn("mode=", all_body)
        self.assertIn("已记住", all_body)
        self.assertIn("没有找到相关内容", self.get("/search?q=zzz-nothing")[1])

    def test_19_html_is_escaped(self) -> None:
        nasty = self.make_memory(title="<script>alert(1)</script>", content='<img src=x onerror="alert(2)">')
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

        _, memories_body = self.get("/memories?status=all")
        _, detail_body = self.get(f"/memories/{nasty.id}")

        for body in (memories_body, detail_body):
            self.assertNotIn("<script>alert(1)</script>", body)
            self.assertNotIn('onerror="alert(2)"', body)
            self.assertIn("&lt;script&gt;", body)

    def test_20_missing_pages_are_friendly_404(self) -> None:
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

        for path in ("/nope", "/memories/missing-id", "/sources/missing-id"):
            status, body = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertIn("NotFound", body)
            self.assertNotIn("Traceback", body)
        self.assertIn("NotFoundError", self.get("/memories/missing-id")[1])
        self.assertIn("NotFoundError", self.get("/sources/missing-id")[1])


# --------------------------------------------------------------------------
# 6-8: capture and imports through the existing pipelines
# --------------------------------------------------------------------------


class WritePathsTest(WebTestCase):
    def test_6_capture_uses_capture_service_and_formation(self) -> None:
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        status, body, url = self.post("/capture", {"title": "", "content": "RAG 是检索增强生成。"})

        self.assertEqual(status, 200)
        self.assertIn("处理完成", body)          # 横幅跟着 303 落到「喂知识」首页
        self.assertIn("把知识喂给我", body)
        memories = self.repo.list_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].title, "RAG 的核心机制")
        self.assertEqual(self.client.transport.call_count, 1)

    def test_6b_capture_uses_post_redirect_get_flash(self) -> None:
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        status, headers, _ = self.no_redirect_post("/capture", {"content": "RAG 是检索增强生成。"})
        location = headers.get("Location", "")

        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/capture?flash="), location)
        follow_status, follow_body = self.get(location)
        self.assertEqual(follow_status, 200)
        self.assertIn("处理完成", follow_body)
        self.assertIn("把知识喂给我", follow_body)   # 横幅落在首页

    def test_7_file_import_goes_through_file_importer(self) -> None:
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)
        markdown = "# 向量检索\n\n向量检索按相似度取回候选片段。\n"

        status, body, _ = self.post(
            "/import-file", {**self.upload("notes.md", markdown), "title": "", "source_type": "file"}
        )

        self.assertEqual(status, 200)
        self.assertIn("文件导入完成", body)
        self.assertIn("notes.md", body)
        self.assertIn("记住", body)   # 首页横幅里的中文结果
        source = self.repo.list_sources()[0]
        self.assertEqual(str(source.source_type), "file")
        self.assertEqual(source.metadata["filename"], "notes.md")
        self.assertEqual(source.metadata["extension"], ".md")
        self.assertEqual(source.metadata["encoding"], "utf-8")
        self.assertTrue(source.metadata["file_sha256"])
        self.assertEqual(len(self.repo.list_memories()), 1)

    def test_7b_unsupported_extension_is_rejected_without_writes(self) -> None:
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        status, body, _ = self.post("/import-file", self.upload("paper.pdf", "%PDF-1.4 not a chat"))

        self.assertEqual(status, 400)
        self.assertIn("文件导入失败", body)
        self.assertIn("UnsupportedFileTypeError", body)
        self.assertEqual(self.repo.counts()["sources"], 0)
        self.assertEqual(self.repo.counts()["memories"], 0)
        self.assertEqual(self.client.transport.call_count, 0)

    def test_8_chat_import_goes_through_chat_importer_and_hides_the_body(self) -> None:
        # the probe sits in the ASSISTANT message: the conversation title legitimately
        # echoes the first user message (spec §九), so it is not a body-leak probe
        secret = "紫罗兰色的鲸鱼在第七码头等待一艘纸船。"
        chat = f"[USER]\n你好，我有一些偏好想让你记住。\n\n[ASSISTANT]\n{secret}\n\n[USER]\n好的。\n"
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        status, body, _ = self.post("/import-chat", {**self.upload("chat.txt", chat), "format": "auto"})

        self.assertEqual(status, 200)
        self.assertIn("聊天导入完成", body)
        self.assertIn("条消息", body)
        self.assertIn("记住", body)
        self.assertNotIn(secret, body)  # the chat body is never echoed back
        self.assertIn("聊天正文未回显", body)
        # 派生的标题保存在 Source 上（不再渲染到首页横幅里）
        self.assertEqual(self.repo.list_sources()[0].title, "你好，我有一些偏好想让你记住。")
        request = self.client.transport.requests[0]
        self.assertIn("[USER]", request.messages[-1].content)
        self.assertIn(secret, request.messages[-1].content)  # ... but Formation did receive it

    def test_8b_chat_import_low_value_writes_nothing(self) -> None:
        context = self.make_context(response_for({"worth_remembering": False, "reason": "寒暄", "memories": []}))
        self.base = self.start(context)

        status, body, _ = self.post(
            "/import-chat", self.upload("small.txt", "[USER]\n哈哈。\n\n[ASSISTANT]\n哈哈。\n")
        )

        self.assertEqual(status, 200)
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})
        self.assertIn("记住 0 条", body)          # 低价值内容也是正常结果，不是失败

    def test_14_empty_input_never_calls_the_model(self) -> None:
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        status, body, _ = self.post("/capture", {"title": "x", "content": "   "})
        import_status, import_body, _ = self.post("/import-file", {"filename": "a.txt", "content_base64": ""})

        self.assertEqual((status, import_status), (400, 400))
        self.assertIn("先写点东西再喂", body)
        self.assertIn("没有收到文件内容", import_body)
        self.assertEqual(self.client.transport.call_count, 0)
        self.assertEqual(self.repo.counts()["memories"], 0)


# --------------------------------------------------------------------------
# 9-13: lifecycle, editing, errors
# --------------------------------------------------------------------------


class LifecycleTest(WebTestCase):
    def test_9_archive_then_10_restore(self) -> None:
        memory = self.make_memory(title="生命周期记忆")
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        archive_status, archive_body, _ = self.post(f"/memories/{memory.id}/archive", {})
        after_archive = self.repo.require_memory(memory.id)
        default_list = self.get("/memories")[1]
        archived_list = self.get("/memories?status=archived")[1]

        self.assertEqual(archive_status, 200)
        self.assertEqual(str(after_archive.status), "archived")
        self.assertIn("已归档", archive_body)
        self.assertNotIn(memory.id, default_list)
        self.assertIn(memory.id, archived_list)

        restore_status, restore_body, _ = self.post(f"/memories/{memory.id}/restore", {})
        after_restore = self.repo.require_memory(memory.id)

        self.assertEqual(restore_status, 200)
        self.assertEqual(str(after_restore.status), "active")
        self.assertIn("已恢复", restore_body)
        self.assertIn(memory.id, self.get("/memories")[1])

    def test_11_delete_removes_the_memory_but_keeps_the_source(self) -> None:
        source = self.make_source(title="保留的来源")
        memory = self.make_memory(title="将被删除")
        self.repo.link(memory.id, source.id)
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        status, body, _ = self.post(f"/memories/{memory.id}/delete", {})

        self.assertEqual(status, 200)
        self.assertIn("已删除这条记忆", body)
        self.assertIsNone(self.repo.get_memory(memory.id))
        self.assertIsNotNone(self.repo.get_source(source.id))
        self.assertEqual(self.repo.counts()["sources"], 1)
        self.assertNotIn(memory.id, self.get("/memories?status=all")[1])
        self.assertEqual(self.get(f"/sources/{source.id}")[0], 200)

    def test_12_edit_is_immediately_visible_to_retrieval(self) -> None:
        memory = self.make_memory(title="旧标题", content="旧内容：只讲向量数据库。")
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)
        self.assertIn(memory.id, self.get("/search?q=向量数据库")[1])

        status, body, _ = self.post(
            f"/memories/{memory.id}/update",
            {
                "title": "新标题",
                "content": "新内容：RAG 通过检索外部知识为模型提供上下文。",
                "summary": "改写摘要",
                "tags": "rag, 检索",
                "importance": "0.9",
                "confidence": "0.8",
                "status": "active",
            },
        )

        self.assertEqual(status, 200)
        self.assertIn("已保存修改", body)
        updated = self.repo.require_memory(memory.id)
        self.assertEqual((updated.title, updated.tags), ("新标题", ["rag", "检索"]))
        self.assertEqual((updated.importance, updated.confidence), (0.9, 0.8))
        self.assertIn(memory.id, self.get("/search?q=检索外部知识")[1])
        self.assertIn("新标题", self.get(f"/memories/{memory.id}")[1])

    def test_13_illegal_lifecycle_operations_show_friendly_errors(self) -> None:
        memory = self.make_memory(title="状态机记忆")
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        status, body, _ = self.post(
            f"/memories/{memory.id}/update",
            {"title": "状态机记忆", "content": "内容", "status": "pending", "importance": "0.5", "confidence": "0.5"},
        )

        self.assertEqual(status, 409)
        self.assertIn("IllegalTransitionError", body)
        self.assertIn("当前状态不能执行该操作", body)
        self.assertNotIn("Traceback", body)
        self.assertEqual(str(self.repo.require_memory(memory.id).status), "active")

        # restoring an already-active Memory is a no-op in the frozen lifecycle, not an error
        noop_status, _, _ = self.post(f"/memories/{memory.id}/restore", {})
        self.assertEqual(noop_status, 200)
        self.assertEqual(str(self.repo.require_memory(memory.id).status), "active")

        # archived -> pending is illegal: the current state machine refuses it
        archived = self.make_memory(title="归档后不能变回 pending", status=MemoryStatus.ARCHIVED)
        illegal_status, illegal_body, _ = self.post(
            f"/memories/{archived.id}/update",
            {"title": archived.title, "content": archived.content, "status": "pending",
             "importance": "0.5", "confidence": "0.5"},
        )
        self.assertEqual(illegal_status, 409)
        self.assertIn("IllegalTransitionError", illegal_body)
        self.assertEqual(str(self.repo.require_memory(archived.id).status), "archived")

    def test_22_lifecycle_actions_call_the_lifecycle_api(self) -> None:
        memory = self.make_memory(title="探针记忆")
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        with mock.patch.object(
            MemoryLifecycle, "archive_memory", autospec=True, return_value=mock.Mock(
                from_status="active", to_status="archived"
            )
        ) as spy:
            status, _, _ = self.post(f"/memories/{memory.id}/archive", {})

        self.assertEqual(status, 200)
        spy.assert_called_once()
        self.assertEqual(spy.call_args[0][1], memory.id)
        # the repository really was consulted (no SQL in the web layer)
        self.assertEqual(str(self.repo.require_memory(memory.id).status), "active")

    def test_13b_error_mapping_is_friendly(self) -> None:
        from personal_memory import DuplicateContentHashError, IllegalTransitionError, NotFoundError
        from personal_memory.importers.files import UnsupportedFileTypeError
        from personal_memory.llm import LLMRequestError

        cases = {
            IllegalTransitionError("mem_1", "active", "pending"): (409, "生命周期"),
            NotFoundError("memory", "x"): (404, "不存在"),
            UnsupportedFileTypeError("a.pdf", extension=".pdf", supported=(".txt",)): (400, "文件导入失败"),
            LLMRequestError("boom", retryable=True): (502, "模型请求失败"),
            ValidationError("bad", field="x"): (400, "输入不合法"),
            DuplicateContentHashError("dup"): (409, "相同来源"),
        }
        for error, (expected_status, fragment) in cases.items():
            status, message = classify_error(error)
            self.assertEqual(status, expected_status, type(error).__name__)
            self.assertIn(fragment, message)


# --------------------------------------------------------------------------
# 15-17: architecture, privacy, upload hygiene
# --------------------------------------------------------------------------


    def test_25_url_import_uses_the_same_chain(self) -> None:
        from .test_web_import import FakeTransport, LONG_BODY, PUBLIC_IP, html_page, public_resolver, response

        page_marker = "网页正文标记句：检索增强生成先从外部知识库取回相关片段。"
        transport = FakeTransport({
            "https://example.com/article": response(html_page(f"{page_marker}{LONG_BODY}", title="网页标题")),
        })
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)
        captured = io.StringIO()

        def factory(**kwargs):
            return WebImporter(transport=transport, resolver=public_resolver)

        with mock.patch.object(web_server, "WebImporter", factory):
            with contextlib.redirect_stderr(captured):
                status, body, _ = self.post("/import-url", {"url": "https://example.com/article"})

        self.assertEqual(status, 200)
        self.assertIn("网页导入完成", body)
        self.assertIn("example.com", body)  # 首页横幅显示真实域名
        self.assertNotIn(page_marker, body)  # the page body is never echoed
        self.assertEqual(len(self.repo.list_memories()), 1)
        self.assertEqual(self.client.transport.call_count, 1)
        source = self.repo.list_sources()[0]
        self.assertEqual(str(source.source_type), "web")
        self.assertEqual(source.url, "https://example.com/article")
        self.assertEqual(source.metadata["captured_from"], "web")
        self.assertNotIn(page_marker, captured.getvalue())  # nor logged

        # a blocked target fails with the friendly URL error and no model call
        before = self.client.transport.call_count
        with mock.patch.object(web_server, "WebImporter", factory):
            blocked_status, blocked_body, _ = self.post("/import-url", {"url": "http://127.0.0.1:8765/"})
        self.assertEqual(blocked_status, 400)
        self.assertIn("这个网址卡比不能去", blocked_body)   # 精确原因（不再是笼统的类别名）
        self.assertIn("只能喂公开的网页地址", blocked_body)
        self.assertIn("BlockedUrlError", blocked_body)
        self.assertEqual(self.client.transport.call_count, before)
        self.assertEqual(transport.calls[0]["url"], "https://example.com/article")

    def test_26_old_import_page_is_retired(self) -> None:
        """旧导入页已收口回首页；URL 能力由统一输入区（粘贴即识别）承担。"""
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))
        body = self.get("/import")[1]
        self.assertIn("把知识喂给我", body)
        self.assertIn('id="feed-text"', body)
        self.assertIn('"/import-url"', body)
        self.assertNotIn('action="/import-url"', body)


class ArchitectureAndPrivacyTest(WebTestCase):
    def test_15_web_layer_has_no_sql_and_reuses_the_modules(self) -> None:
        web_dir = pathlib.Path(web_server.__file__).parent
        for path in sorted(web_dir.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("sqlite3", text, path.name)
            for verb in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
                self.assertNotIn(verb, text, f"{path.name} contains {verb}")
            self.assertNotIn("ALLOWED_TRANSITIONS", text, path.name)

        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.assertIsInstance(context.repository, MemoryRepository)
        self.assertIsInstance(context.retriever, MemoryRetriever)
        self.assertIsInstance(context.lifecycle, MemoryLifecycle)
        self.assertEqual(web_server.MemoryLifecycle, MemoryLifecycle)
        self.assertEqual(web_server.MemoryRetriever, MemoryRetriever)
        self.assertEqual(web_server.MemoryRepository, MemoryRepository)
        self.assertEqual(web_server.CaptureService.__module__, "personal_memory.capture")

    def test_17_api_key_never_appears_in_responses_or_logs(self) -> None:
        captured = io.StringIO()
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        with contextlib.redirect_stderr(captured):
            bodies = [self.get("/")[1], self.get("/capture")[1], self.get("/import")[1],
                      self.get("/memories")[1], self.get("/sources")[1], self.get("/search")[1]]
            bodies.append(self.post("/capture", {"content": "RAG 是检索增强生成。"})[1])
            bodies.append(self.post("/import-file", self.upload("a.pdf", "x"))[1])
            bodies.append(self.get(f"/memories/missing")[1])
            self.post("/capture", {"content": "第二条，触发第二次形成。"})

        for body in bodies:
            self.assertNotIn(FAKE_API_KEY, body)
        self.assertNotIn(FAKE_API_KEY, captured.getvalue())
        summary = context.llm_summary()
        self.assertTrue(summary["api_key_present"])
        self.assertNotIn(FAKE_API_KEY, json.dumps(summary))

    def test_23_uploads_are_sanitised_and_cleaned_up(self) -> None:
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        traversal_status, traversal_body, _ = self.post(
            "/import-file", {**self.upload("../../evil.md", "# x\n"), "source_type": "file"}
        )
        bad_b64_status, bad_b64_body, _ = self.post(
            "/import-file", {"filename": "a.txt", "content_base64": "!!!not base64!!!"}
        )
        no_name_status, no_name_body, _ = self.post(
            "/import-file", {"filename": "", "content_base64": base64.b64encode(b"x").decode()}
        )

        self.assertEqual((traversal_status, bad_b64_status, no_name_status), (200, 400, 400))
        self.assertIn("文件导入完成", traversal_body)
        # "../../evil.md" became the basename "evil.md" inside a throwaway directory
        stored = self.repo.list_sources()[0]
        self.assertEqual(stored.metadata["filename"], "evil.md")
        self.assertNotIn("..", json.dumps(stored.metadata, ensure_ascii=False))
        self.assertIn("base64", bad_b64_body)
        self.assertIn("文件名无效", no_name_body)
        uploads = context.db_path.parent / ".pkb-uploads"
        leftovers = list(uploads.glob("upload-*")) if uploads.exists() else []
        self.assertEqual(leftovers, [])

    def test_21_cross_origin_post_is_rejected(self) -> None:
        context = self.make_context(response_for(VALID_CAPTURE_PAYLOAD))
        self.base = self.start(context)

        status, body, _ = self.post(
            "/capture", {"content": "RAG"}, headers={"Origin": "http://evil.example"}
        )

        self.assertEqual(status, 403)
        self.assertIn("拒绝跨站提交", body)
        self.assertEqual(self.client.transport.call_count, 0)

    def test_24_missing_llm_config_keeps_read_pages_usable(self) -> None:
        self.make_memory(title="离线也能看")
        context = self.make_context(with_llm=False)
        self.base = self.start(context)

        self.assertIn("离线也能看", self.get("/memories")[1])
        # 没有模型时，仍然可用的只读页面：首页 / 我的记忆 / 搜索
        for path in ("/", "/memories", "/search"):
            self.assertEqual(self.get(path)[0], 200, path)
        self.assertIn("把知识喂给我", self.get("/")[1])
        # 旧入口已收口：GET /capture 现在回到首页
        self.assertIn("把知识喂给我", self.get("/capture")[1])
        capture_status, capture_body, _ = self.post("/capture", {"content": "RAG"})
        self.assertEqual(capture_status, 503)
        self.assertIn("模型未配置", capture_body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
