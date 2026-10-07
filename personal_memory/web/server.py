"""Local HTTP layer for the Knowledge Base MVP (standard library only).

    browser  ->  KnowledgeBaseHandler  ->  existing modules
                 (GET/POST, HTML)          CaptureService / FileImporter / ChatImporter
                                           MemoryRetriever / MemoryLifecycle / MemoryRepository

Deliberately thin: this module owns request parsing, HTML responses, error mapping and
the temp-file handoff for browser uploads.  It contains **no** business logic -- no database
driver import, no SQL, no prompt, no state machine, no ranking.  Every write goes through the
repository / lifecycle / capture objects it is given, exactly like the CLI does.

Design notes
------------
* ``http.server.ThreadingHTTPServer`` + ``BaseHTTPRequestHandler``: zero new runtime
  dependencies, no build step, no framework.  A local single-user tool does not need
  more, and the project must keep working in an offline environment.
* Server-rendered HTML with one small inline stylesheet (see ``views.py``).  Plain
  ``<form>`` POSTs with the POST/redirect/flash pattern; the only JavaScript is a
  ~20 line file reader on the Import page.
* Uploads: the browser reads the chosen file and posts it as base64.  The server decodes
  the **original bytes** into a temporary file and calls the existing path-based
  importer, so filename / extension / size / encoding / hash validation keeps applying
  unchanged (multipart parsing would add code without changing any of that).
* Privacy: binds 127.0.0.1 by default, never renders or logs the API key, keeps full
  request bodies out of logs, and reports chat imports without echoing the chat text.
"""

from __future__ import annotations

import base64
import binascii
import inspect
import re
import json
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from ..capture import CaptureService
from ..db import Database
from ..errors import (
    ConflictError,
    DuplicateContentHashError,
    IllegalTransitionError,
    MemorySystemError,
    NotFoundError,
    ReferentialIntegrityError,
    SchemaError,
    ValidationError,
)
from ..extraction import ExtractionValidationError, FormationPolicy, MemoryFormationService
from ..importers import MAX_CHAT_BYTES, MAX_FILE_BYTES, ChatImporter, FileImporter
from ..importers.chat import ChatImportError
from ..importers.files import FileImportError
from ..importers import (
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
    MIN_PDF_CHARS,
    PdfError,
    PdfImporter,
    PdfUrlContentError,
)
from ..importers.web import DEFAULT_TIMEOUT_SECONDS, WebImporter, WebImportError
from ..lifecycle import MemoryLifecycle
from ..llm import LLMClient, LLMConfigError, LLMError, LLMRequestError, LLMResponseError, load_config
from ..models import MemoryStatus, SourceType
from ..quality import LLMConflictClassifier, MemoryQualityGate
from ..retrieval import RetrievalError, MemoryRetriever
from ..store import MemoryRepository
from ..teacher_llm import TeacherModelError
from . import teacher as teacher_ui
from .views import (
    STATUS_CHOICES,
    Flash,
    FEED_ERROR_COPY,
    feed_error_message,
    feed_page,
    memory_status_label,
    error_page,
    memories_page,
    memory_detail_page,
    search_page,
    source_detail_page,
    sources_page,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MAX_UPLOAD_BYTES",
    "WebContext",
    "KnowledgeBaseHandler",
    "KnowledgeBaseServer",
    "create_server",
    "make_handler",
    "serve",
    "run_web",
]

#: Only the loopback interface is served by default (spec §四 / §十六).
#: Read-only sprite assets (卡比 frames).  Exact-name allowlist: nothing else is served.
ASSET_DIR = Path(__file__).resolve().parent / "assets"
ASSET_FILES = frozenset(
    {
        "kirby-idle.png",
        "kirby-open.png",
        "kirby-inhale-1.png",
        "kirby-inhale-2.png",
        "kirby-inhale-3.png",
    }
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Guard rail for POST bodies (a base64 file is ~1.37x its size).
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

#: error class -> (http status, friendly explanation)   (spec §十九)
_ERROR_MAP: tuple[tuple[type[BaseException], int, str], ...] = (
    (IllegalTransitionError, 409, "当前状态不能执行该操作（生命周期规则不允许这个转换）"),
    (DuplicateContentHashError, 409, "已存在相同来源（内容重复，已自动复用）"),
    (ConflictError, 409, "已存在相同或冲突的记忆"),
    (NotFoundError, 404, "这条记忆或来源不存在"),
    (TeacherModelError, 502, "模型这次没有回答成功"),
    (ExtractionValidationError, 502, "模型输出无法形成有效记忆"),
    (LLMConfigError, 503, "模型未配置或配置无效"),
    (LLMRequestError, 502, "模型请求失败"),
    (LLMResponseError, 502, "模型响应无法解析"),
    (LLMError, 502, "模型调用失败"),
    (WebImportError, 400, "URL 导入失败"),
    (PdfError, 400, "PDF 导入失败"),
    (FileImportError, 400, "文件导入失败"),
    (ChatImportError, 400, "聊天解析失败"),
    (RetrievalError, 400, "检索失败"),
    (ReferentialIntegrityError, 409, "引用一致性冲突"),
    (SchemaError, 500, "数据库需要先初始化一次才能使用"),
    (ValidationError, 400, "输入不合法"),
    (MemorySystemError, 400, "操作失败"),
)


def classify_error(exc: BaseException) -> tuple[int, str]:
    """Map an existing exception to ``(http_status, friendly_message)`` -- no traceback."""
    for error_type, status, message in _ERROR_MAP:
        if isinstance(exc, error_type):
            return status, message
    return 500, "服务器内部错误"


def _safe_filename(filename: str) -> str:
    """Reduce a browser-supplied filename to a safe basename (no paths, no control chars)."""
    name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not name or name in {".", ".."}:
        raise ValidationError("文件名无效：请通过“选择文件”按钮上传", field="filename")
    if any(ord(char) < 32 for char in name):
        raise ValidationError("文件名包含控制字符，已拒绝", field="filename")
    if len(name) > 200:
        raise ValidationError("文件名过长（>200 字符）", field="filename")
    return name


@dataclass
class WebContext:
    """Everything the HTTP layer needs: the reused business objects plus UI settings.

    The context never opens a connection itself -- it holds the same repository /
    retriever / lifecycle objects the CLI would build.
    """

    db_path: Path
    repository: MemoryRepository
    retriever: MemoryRetriever
    lifecycle: MemoryLifecycle
    llm_config: Any | None = None
    llm_error: str | None = None
    quality_check: bool = True
    max_bytes: int = MAX_FILE_BYTES
    max_upload_bytes: int = MAX_UPLOAD_BYTES
    flashes: dict[str, Flash] = field(default_factory=dict)
    #: Injected ``TeacherModel`` for the Teacher pages (a fake in tests, or a local
    #: model).  ``None`` means "build the configured provider", which is what the real
    #: application does; browsing never needs either.
    teacher_model: Any | None = field(default=None, repr=False, compare=False)
    #: The lazily built product-level Teacher entry (P2D-2); never part of equality.
    teacher: Any | None = field(default=None, repr=False, compare=False)

    # -- construction -----------------------------------------------------
    @classmethod
    def create(
        cls,
        db_path: str | Path,
        *,
        config_path: str | None = None,
        quality_check: bool = True,
        max_bytes: int = MAX_FILE_BYTES,
    ) -> "WebContext":
        """Open/create the database (same migration path as ``init``) and load the LLM config.

        A missing or broken LLM config is **not** fatal: browsing keeps working and only
        Capture/Import report the problem, so the UI stays usable offline.
        """
        path = Path(db_path)
        database = Database(path)
        database.initialize()
        repository = MemoryRepository(database)
        try:
            config = load_config(config_path)
            error = None
        except LLMConfigError as exc:  # configuration problem, reported in the UI only
            config = None
            error = str(exc)
        return cls(
            db_path=path,
            repository=repository,
            retriever=MemoryRetriever(repository),
            lifecycle=MemoryLifecycle(repository),
            llm_config=config,
            llm_error=error,
            quality_check=quality_check,
            max_bytes=max_bytes,
        )

    # -- helpers ----------------------------------------------------------
    @property
    def llm_ready(self) -> bool:
        return self.llm_config is not None

    def llm_summary(self) -> dict[str, Any] | None:
        return self.llm_config.as_public_dict() if self.llm_config is not None else None

    def put_flash(self, flash: Flash) -> str:
        """Store a one-shot message; only its token travels through the URL."""
        if len(self.flashes) > 100:
            for key in list(self.flashes)[:50]:
                self.flashes.pop(key, None)
        token = uuid.uuid4().hex[:12]
        self.flashes[token] = flash
        return token

    def take_flash(self, token: str | None) -> Flash | None:
        if not token:
            return None
        return self.flashes.pop(token, None)

    def capture_service(
        self, *, captured_from: str = "web-ui", keep_source: str = "when_required"
    ) -> CaptureService:
        """Build the same pipeline the CLI uses (quality gate included)."""
        if self.llm_config is None:
            raise LLMConfigError(
                f"模型未配置，无法执行 Capture / Import：{self.llm_error or '未知原因'}"
            )
        client = LLMClient(self.llm_config)
        quality = (
            MemoryQualityGate(self.repository, classifier=LLMConflictClassifier(client))
            if self.quality_check
            else None
        )
        formation = MemoryFormationService(
            self.repository,
            client,
            quality=quality,
            policy=FormationPolicy(keep_source=keep_source),
        )
        return CaptureService(formation, captured_from=captured_from)

    # -- upload handoff ---------------------------------------------------
    def materialise_upload(self, filename: str, content_base64: str) -> tuple[Path, Path]:
        """Decode a browser-uploaded file to a temporary path: ``(directory, path)``.

        The original bytes are preserved, so the existing importers validate exactly what
        the user chose (extension, size, UTF-8) and compute the real content hash.
        """
        safe_name = _safe_filename(filename)
        if not content_base64 or not content_base64.strip():
            raise ValidationError("没有收到文件内容：请用“选择文件”按钮选择文件后再提交", field="content_base64")
        try:
            raw = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("文件内容不是合法的 base64 编码", field="content_base64") from exc
        if len(raw) > self.max_upload_bytes:
            raise ValidationError(
                f"上传内容过大：{len(raw)} 字节 > 上限 {self.max_upload_bytes} 字节", field="content_base64"
            )
        base = self.db_path.parent / ".pkb-uploads"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:  # read-only data directory: fall back to the system temp dir
            base = Path(tempfile.gettempdir())
        directory = base / f"upload-{uuid.uuid4().hex[:10]}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / safe_name
        path.write_bytes(raw)
        return directory, path


def _pdf_filename_from_url(url: str) -> str:
    """Name the temporary file after the URL so the importer's extension check passes."""
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1] if path else ""
    name = unquote(name)
    if not name.lower().endswith(".pdf"):
        name = (name or "document") + ".pdf"
    return name


def _compact_capture(capture_result: Any) -> dict[str, Any]:
    """Extract only the non-content view of a CaptureResult (never the raw input)."""
    payload = capture_result.as_dict()
    keys = (
        "status",
        "formation_status",
        "memory_count",
        "source_count",
        "source_reused",
        "memories_created",
        "sources_created",
    )
    compact = {key: payload[key] for key in keys}
    formation = capture_result.formation_result
    compact["worth_remembering"] = formation.worth_remembering
    compact["reason"] = formation.reason
    return compact


class KnowledgeBaseHandler(BaseHTTPRequestHandler):
    """Request/response + HTML only.  Business logic lives in the reused modules."""

    server_version = "PersonalKnowledgeBase/0.8"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    context: WebContext  # bound by make_handler()

    # -- logging (never the body, never the query string) ------------------
    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        path = urlparse(self.path).path
        sys.stderr.write(f"[pkb] {self.command} {path} -> {code}\n")

    def log_error(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[pkb] {self.address_string()} {fmt % args}\n")

    # -- entry points -----------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if method == "POST":
                self._handle_post(path, query)
            else:
                self._handle_get(path, query)
        except MemorySystemError as exc:
            self._render_error(exc)
        except Exception as exc:  # pragma: no cover - defensive: never leak a traceback
            sys.stderr.write(f"[pkb] unhandled {type(exc).__name__}: {str(exc)[:200]}\n")
            self._send_page(
                500,
                error_page(
                    status=500,
                    title="内部错误",
                    message="服务器发生未预期的错误，操作未完成。",
                    kind=type(exc).__name__,
                    hint="技术细节只写在服务器日志里（不包含请求正文）。",
                ),
            )

    # -- responses --------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _wants_json(self) -> bool:
        """True for the feed page's ``fetch`` calls; False for ordinary browser forms."""
        if (self.headers.get("X-Requested-With") or "").lower() == "fetch":
            return True
        accept = (self.headers.get("Accept") or "").lower()
        return "application/json" in accept and "text/html" not in accept

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _finish_post(self, flash: Flash, *, redirect_to: str, extra: Mapping[str, Any] | None = None) -> None:
        """Deliver a **finished** result: JSON for fetch, 303 + flash for plain forms.

        ``ok=False`` flashes are real business failures (the pipeline raised); they are never
        produced for "worthless content" (Memory 0 / Source 0), which is a normal outcome.
        """
        if not self._wants_json():
            token = self.context.put_flash(flash)
            return self._redirect(f"{redirect_to}?flash={token}")
        payload: dict[str, Any] = {
            "ok": flash.kind != "err",
            "kind": flash.kind,
            # 角色反馈（§七）绑定在这次真实完成的结果上：管线没返回之前，前端拿不到它。
            "feedback": "吃饱了~" if flash.kind != "err" else "",
            "title": flash.title,
            "detail": flash.detail,
            "hint": flash.hint,
        }
        if extra:
            payload.update(dict(extra))
        self._send_json(200 if payload["ok"] else 400, payload)

    @staticmethod
    def _outcome_note(memory_count: Any, worth_remembering: Any) -> str:
        """Second-layer, non-blocking feedback line (spec §八).  Never a failure."""
        try:
            count = int(memory_count or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            return "这次没有留下长期记忆"
        return f"记住了 {count} 件事"

    def _send_page(self, status: int, page: str) -> None:
        self._send(status, page.encode("utf-8"), "text/html; charset=utf-8")

    def _send_text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_asset(self, path: str) -> None:
        """Serve one of the whitelisted Kirby sprite files (read-only, no traversal).

        Only exact names from :data:`ASSET_FILES` are served: no directory listing, no
        arbitrary paths, and a missing/unknown name is an ordinary 404.
        """
        name = path[len("/assets/"):]
        if name not in ASSET_FILES:
            return self._send_page(
                404,
                error_page(
                    status=404,
                    title="素材不存在",
                    message=f"没有这个素材：{name}",
                    kind="NotFound",
                ),
            )
        try:
            data = (ASSET_DIR / name).read_bytes()
        except OSError:
            return self._send_page(
                404,
                error_page(status=404, title="素材不存在", message="素材文件缺失。", kind="NotFound"),
            )
        self._send(200, data, "image/png")

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _render_error(self, exc: MemorySystemError) -> None:
        status, friendly = classify_error(exc)
        where = inspect.stack()[1].function  # which handler raised it (log only)
        sys.stderr.write(f"[pkb] {type(exc).__name__} in {where}: {str(exc)[:200]}\n")
        detail = str(exc)[:500]
        # 只保留本身已经是中文的细节（例如「请求体过大」「不是合法的 base64 编码」）；
        # 常见的格式名/术语先去掉再判断，含英文的库异常文本不进界面（日志里仍有全文）
        probe = re.sub(r"(?i)\b(base64|pdf|url|http|https|txt|md|markdown|chat|json|utf-8)\b", "", detail)
        message = f"{friendly}：{detail}" if detail.strip() and not re.search(r"[A-Za-z]", probe) else friendly
        if self._wants_json():
            # The feed page gets user language only: no exception name, no message text,
            # no model/API/database/URL-policy detail (spec §九).
            title, hint = feed_error_message(exc)
            return self._send_json(status, {"ok": False, "kind": "err", "title": title, "hint": hint})
        # 无脚本的表单路径（HTML）也给出同一句精确原因——但只针对「抓取/解析」这一类错误
        # （网页与 PDF 导入）。其余错误保持原措辞：校验类错误本身已带中文细节，生命周期类
        # 由 _ERROR_MAP 给出更贴切的说法。
        if isinstance(exc, (WebImportError, PdfError)):
            title, hint = feed_error_message(exc)
            message = f"{title}：{hint}"
        self._send_page(
            status,
            error_page(
                status=status,
                title="操作失败",
                message=message,
                kind=type(exc).__name__,
                hint="失败的操作没有写入任何长期数据（记忆与来源保持原样）。",
            ),
        )

    # -- request parsing --------------------------------------------------
    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > int(self.context.max_upload_bytes * 1.5):
            raise ValidationError(
                f"请求体过大：{length} 字节 > 上限 {int(self.context.max_upload_bytes * 1.5)} 字节",
                field="body",
            )
        raw = self.rfile.read(length) if length else b""
        pairs = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {key: values[-1] for key, values in pairs.items()}

    def _same_origin(self) -> bool:
        """Reject cross-site form posts (cheap CSRF guard for a localhost tool)."""
        origin = self.headers.get("Origin")
        if not origin:
            return True  # same-origin form posts and CLI clients may omit it
        parsed = urlparse(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers.get("Host", "")

    @staticmethod
    def _one(query: Mapping[str, Sequence[str]], key: str, default: str) -> str:
        values = query.get(key)
        return values[-1] if values else default

    @staticmethod
    def _limit(query: Mapping[str, Sequence[str]], default: int = 10) -> int:
        raw = KnowledgeBaseHandler._one(query, "limit", str(default))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(1, min(value, 100))

    # -- GET --------------------------------------------------------------
    def _handle_get(self, path: str, query: Mapping[str, Sequence[str]]) -> None:
        context = self.context
        flash = context.take_flash(self._one(query, "flash", "") or None)

        if path == "/":
            # 卡比 Capture Home (Phase: 卡比喂知识).  Statistics and technical state moved
            # to /memories and /capture; the home page is just the creature and the input.
            return self._send_page(200, feed_page(flash=flash))
        if path == "/healthz":
            return self._send_text(200, "ok\n")
        if path.startswith("/assets/"):
            return self._send_asset(path)
        if path in ("/capture", "/import"):
            # 1.0 收口：旧入口不再有独立界面，一律回到「喂知识」首页。
            # 带上一次性结果横幅（flash 已在上面被取出，这里重新签一个再重定向）。
            if flash is not None:
                token = context.put_flash(flash)
                return self._redirect(f"/?flash={token}")
            return self._redirect("/")
        if path == "/memories":
            status = self._one(query, "status", "active")
            memory_type = self._one(query, "type", "all")
            if status not in STATUS_CHOICES:
                raise ValidationError(f"status 必须是 {list(STATUS_CHOICES)} 之一", field="status")
            memories = context.repository.list_memories(
                memory_type=None if memory_type == "all" else memory_type,
                status=None if status == "all" else status,
                limit=200,
            )
            counts = context.repository.status_counts()
            counts["_total"] = sum(counts.values())
            # 只读地把既有的 Source↔Memory 关系交给视图：Memories 页面需要按材料分组，
            # 而视图层拿不到仓储（同一模式见 /search 的 sources_by_memory）。没有新查询语义。
            sources_by_memory = {
                memory.id: context.repository.get_sources_for_memory(memory.id) for memory in memories
            }
            return self._send_page(
                200,
                memories_page(
                    memories=memories,
                    status_filter=status,
                    type_filter=memory_type,
                    status_counts=counts,
                    total_shown=len(memories),
                    sources_by_memory=sources_by_memory,
                    flash=flash,
                ),
            )
        if path.startswith("/memories/"):
            memory_id = path.split("/", 2)[2]
            memory = context.repository.require_memory(memory_id)
            sources = context.repository.get_sources_for_memory(memory_id)
            # 相关记忆 = 与这条记忆共享真实 Source 的其它记忆（既有查询，不含新算法）
            related: list[Any] = []
            seen_ids = {memory_id}
            for source in sources:
                for other in context.repository.get_memories_for_source(source.id):
                    if other.id not in seen_ids:
                        seen_ids.add(other.id)
                        related.append(other)
            related.sort(key=lambda item: (str(item.status) != "active", item.title))
            return self._send_page(
                200,
                memory_detail_page(
                    memory=memory,
                    sources=sources,
                    transitions=context.lifecycle.allowed_transitions(memory_id),
                    related=related[:6],
                    flash=flash,
                    learn_href=teacher_ui.learn_href(memory_id),
                ),
            )
        if path == "/sources":
            return self._send_page(
                200,
                sources_page(sources=context.repository.list_sources(limit=200), flash=flash),
            )
        if path.startswith("/sources/"):
            source_id = path.split("/", 2)[2]
            source = context.repository.require_source(source_id)
            return self._send_page(
                200,
                source_detail_page(
                    source=source,
                    memories=context.repository.get_memories_for_source(source_id),
                    flash=flash,
                ),
            )
        if path == "/search":
            return self._handle_search(query, flash)
        if path.startswith(f"{teacher_ui.LEARN_ROUTE}/"):
            return self._get_learn(path[len(teacher_ui.LEARN_ROUTE) + 1:], flash)
        self._send_page(
            404,
            error_page(
                status=404,
                title="页面不存在",
                message=f"没有这个地址：{path}",
                kind="NotFound",
                hint="可用页面：喂知识 · 我的记忆 · 搜索。来源可以从记忆的「来自」进入。",
            ),
        )

    def _handle_search(self, query: Mapping[str, Sequence[str]], flash: Flash | None) -> None:
        context = self.context
        text = self._one(query, "q", "")
        memory_type = self._one(query, "type", "all")
        status = self._one(query, "status", "active")
        limit = self._limit(query)
        result = None
        error = ""
        if text.strip():
            if status == "all":
                statuses: Any = tuple(MemoryStatus)
            else:
                statuses = status
            try:
                result = context.retriever.search(
                    text,
                    limit=limit,
                    type=None if memory_type == "all" else memory_type,
                    status=statuses,
                )
            except MemorySystemError as exc:
                # 搜索页只给用户中文提示，不暴露检索实现/异常名（日志里仍留痕）
                sys.stderr.write(f"[pkb] {type(exc).__name__} in search: {str(exc)[:200]}\n")
                error = "换个关键词，或者稍后再试。"
        sources_by_memory = (
            {hit.memory.id: context.repository.get_sources_for_memory(hit.memory.id) for hit in result.hits}
            if result is not None
            else {}
        )
        self._send_page(
            200,
            search_page(
                query=text,
                result=result,
                type_filter=memory_type,
                status_filter=status,
                limit=limit,
                sources_by_memory=sources_by_memory,
                error=error,
                flash=flash,
            ),
        )

    # -- POST -------------------------------------------------------------
    def _handle_post(self, path: str, query: Mapping[str, Sequence[str]]) -> None:
        if not self._same_origin():
            return self._send_page(
                403,
                error_page(
                    status=403,
                    title="拒绝跨站提交",
                    message="这个提交不是从本机页面发起的，已拒绝。",
                    kind="CrossOriginRejected",
                    hint="本工具只服务 127.0.0.1，不接受其它网页发起的提交。",
                ),
            )
        if path == "/capture":
            return self._post_capture()
        if path == "/import-file":
            return self._post_import_file()
        if path == "/import-chat":
            return self._post_import_chat()
        if path == "/import-url":
            return self._post_import_url()
        if path == "/import-pdf":
            return self._post_import_pdf()
        if path.startswith(f"{teacher_ui.LEARN_ROUTE}/"):
            segments = path.split("/")
            if len(segments) == 4 and segments[1] == teacher_ui.LEARN_ROUTE[1:]:
                if segments[3] == "start":
                    return self._post_learn_start(segments[2])
                if segments[3] == "turn":
                    return self._post_learn_turn(segments[2])
        segments = path.split("/")
        if len(segments) == 4 and segments[1] == "memories" and segments[3] in {
            "archive",
            "restore",
            "activate",
            "delete",
            "update",
        }:
            return self._post_memory_action(segments[2], segments[3])
        self._send_page(
            404,
            error_page(
                status=404,
                title="页面不存在",
                message=f"没有这个提交地址：{path}",
                kind="NotFound",
            ),
        )

    def _post_capture(self) -> None:
        form = self._read_form()
        content = form.get("content", "")
        title = (form.get("title") or "").strip() or None
        context = self.context
        if not content.strip():
            # empty input must never reach the model
            if self._wants_json():
                return self._send_json(
                    400, {"ok": False, "kind": "err", "title": "先写点东西再喂", "hint": "内容不能为空。"}
                )
            return self._send_page(
                400,
                error_page(
                    status=400,
                    title="先写点东西再喂",
                    message="内容不能为空，所以没有调用模型。",
                    hint="回到「喂知识」首页写点内容，或者拖入一个文件。",
                ),
            )
        service = context.capture_service(captured_from="web-ui")
        result = service.capture(content, title=title, source_type=SourceType.TEXT)
        payload = _compact_capture(result)
        self._finish_post(
            Flash(
                kind="ok",
                title="处理完成",
                detail=f"记住 {result.memory_count} 条 · 来源 {result.source_count} 份",
                hint="已经交给记忆形成处理。",
                extra=payload,
            ),
            redirect_to="/capture",
            extra={
                "memory_count": result.memory_count,
                "source_count": result.source_count,
                "worth_remembering": result.formation_result.worth_remembering,
                "note": self._outcome_note(result.memory_count, result.formation_result.worth_remembering),
            },
        )

    def _post_import_file(self) -> None:
        form = self._read_form()
        context = self.context
        service = context.capture_service(captured_from="web-ui")
        directory, path = context.materialise_upload(form.get("filename", ""), form.get("content_base64", ""))
        try:
            importer = FileImporter(max_bytes=context.max_bytes)
            result = importer.import_file(
                path,
                service,
                title=(form.get("title") or "").strip() or None,
                source_type=form.get("source_type") or SourceType.FILE,
            )
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        payload = _compact_capture(result.capture_result)
        payload.update(
            {
                "filename": result.document.filename,
                "file_chars": len(result.document.content),
                "title_source": result.document.title_source,
                "encoding": result.document.metadata.get("encoding"),
                "file_sha256": result.document.metadata.get("file_sha256"),
                "extension": result.document.extension,
                "size_bytes": result.document.size_bytes,
            }
        )
        self._finish_post(
            Flash(
                kind="ok",
                title="文件导入完成",
                detail=f"{result.document.filename} → 记住 {result.memory_count} 条 · 来源 {result.source_count} 份",
                extra=payload,
            ),
            redirect_to="/import",
            extra={
                "memory_count": result.memory_count,
                "source_count": result.source_count,
                "worth_remembering": result.capture_result.formation_result.worth_remembering,
                "note": self._outcome_note(result.memory_count, result.capture_result.formation_result.worth_remembering),
            },
        )

    def _post_import_chat(self) -> None:
        form = self._read_form()
        context = self.context
        service = context.capture_service(
            captured_from="web-ui",
            keep_source="always" if form.get("keep_source") == "always" else "when_required",
        )
        directory, path = context.materialise_upload(form.get("filename", ""), form.get("content_base64", ""))
        try:
            importer = ChatImporter(max_bytes=min(context.max_bytes, MAX_CHAT_BYTES), format=form.get("format") or "auto")
            result = importer.import_file(
                path,
                service,
                title=(form.get("title") or "").strip() or None,
                provider=(form.get("provider") or "").strip() or None,
            )
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        # ChatImportResult.as_dict() is already redacted (no message bodies)
        payload = result.as_dict()
        payload["worth_remembering"] = result.capture_result.formation_result.worth_remembering
        payload["filename"] = path.name
        payload["format"] = form.get("format") or "auto"
        self._finish_post(
            Flash(
                kind="ok",
                title="聊天导入完成",
                detail=f"{result.message_count} 条消息 → 记住 {result.memory_count} 条 · 来源 {result.source_count} 份",
                hint="聊天正文未回显；可以在来源页里查看已保存的原始依据。",
                extra=payload,
            ),
            redirect_to="/import",
            extra={
                "memory_count": result.memory_count,
                "source_count": result.source_count,
                "worth_remembering": result.capture_result.formation_result.worth_remembering,
                "note": self._outcome_note(result.memory_count, result.capture_result.formation_result.worth_remembering),
            },
        )

    def _post_import_url(self) -> None:
        """URL import: the same WebImporter + Capture pipeline the CLI uses."""
        form = self._read_form()
        context = self.context
        url = (form.get("url") or "").strip()
        if not url:
            raise ValidationError("请输入要导入的 URL", field="url")
        service = context.capture_service(captured_from="web")
        importer = WebImporter(
            max_bytes=context.max_bytes,
            timeout_seconds=float(form.get("timeout") or DEFAULT_TIMEOUT_SECONDS),
        )
        try:
            document = importer.fetch(url)
        except PdfUrlContentError as pdf:
            # 这个网址给的是一个 PDF 文件：交给同一条 PDF 解析 + Capture 链（只下载一次）
            self._import_pdf_bytes(
                pdf.body,
                filename=_pdf_filename_from_url(url),
                title=(form.get("title") or "").strip() or None,
                captured_from="web",
                label=urlparse(url).hostname or url,
            )
            return
        result = importer.import_document(
            document, service, title=(form.get("title") or "").strip() or None
        )
        payload = result.as_dict()  # redacted: no page body
        payload["url"] = result.url
        self._finish_post(
            Flash(
                kind="ok",
                title="网页导入完成",
                detail=f"{document.host} → 记住 {result.memory_count} 条 · 来源 {result.source_count} 份",
                hint="网页正文未回显；提取出的文本已随记忆形成发送给配置的模型服务。",
                extra=payload,
            ),
            redirect_to="/import",
            extra={
                "memory_count": result.memory_count,
                "source_count": result.source_count,
                "worth_remembering": result.capture_result.formation_result.worth_remembering,
                "note": self._outcome_note(result.memory_count, result.capture_result.formation_result.worth_remembering),
            },
        )

    def _post_import_pdf(self) -> None:
        """PDF import through the same PdfImporter + Capture pipeline the CLI uses.

        The size gates stay in force: the base64 request-body cap in ``_read_form``, the
        decoded-size cap in :meth:`WebContext.materialise_upload`, and the importer's own
        ``max_bytes`` (a ``Path.stat()`` check *before* reading, re-checked after).
        """
        form = self._read_form()
        directory, path = self.context.materialise_upload(
            form.get("filename", ""), form.get("content_base64", "")
        )
        try:
            self._import_pdf_upload(
                path,
                title=(form.get("title") or "").strip() or None,
                captured_from="web-ui",
                label=None,
            )
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def _import_pdf_bytes(
        self, raw: bytes, *, filename: str, title: str | None, captured_from: str, label: str | None
    ) -> None:
        """A PDF that arrived as bytes (a URL response): same gates, same pipeline as an upload."""
        directory, path = self.context.materialise_upload(
            filename, base64.b64encode(raw).decode("ascii")
        )
        try:
            self._import_pdf_upload(path, title=title, captured_from=captured_from, label=label)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def _import_pdf_upload(
        self, path: Path, *, title: str | None, captured_from: str, label: str | None
    ) -> None:
        """Parse one PDF file and hand the normalised text to Capture (the only write path)."""
        context = self.context
        service = context.capture_service(captured_from=captured_from)
        importer = PdfImporter(
            max_bytes=min(context.max_upload_bytes, MAX_PDF_BYTES),
            max_pages=MAX_PDF_PAGES,
            min_chars=MIN_PDF_CHARS,
        )
        result = importer.import_file(path, service, title=title)
        payload = result.as_dict()  # redacted: no PDF text
        payload["filename"] = result.document.filename
        detail = f"{label or result.document.filename} → 记住 {result.memory_count} 条 · 来源 {result.source_count} 份"
        hint = (
            "这个网址给的是一个 PDF 文件；提取出的文本已随记忆形成发送给配置的模型服务。"
            if label
            else "PDF 正文未回显；提取出的文本已随记忆形成发送给配置的模型服务。"
        )
        self._finish_post(
            Flash(kind="ok", title="PDF 导入完成", detail=detail, hint=hint, extra=payload),
            redirect_to="/import",
            extra={
                "memory_count": result.memory_count,
                "source_count": result.source_count,
                "worth_remembering": result.capture_result.formation_result.worth_remembering,
                "note": self._outcome_note(
                    result.memory_count, result.capture_result.formation_result.worth_remembering
                ),
            },
        )

    def _post_memory_action(self, memory_id: str, action: str) -> None:
        form = self._read_form() if action == "update" else {}
        context = self.context
        if action == "archive":
            report = context.lifecycle.archive_memory(memory_id)
            summary = (
                f"状态：{memory_status_label(report.from_status)} → {memory_status_label(report.to_status)}"
            )
        elif action == "restore":
            report = context.lifecycle.restore_memory(memory_id)
            summary = (
                f"状态：{memory_status_label(report.from_status)} → {memory_status_label(report.to_status)}"
            )
        elif action == "activate":
            report = context.lifecycle.activate_memory(memory_id)
            summary = (
                f"状态：{memory_status_label(report.from_status)} → {memory_status_label(report.to_status)}"
            )
        elif action == "delete":
            report = context.lifecycle.delete_memory(memory_id)
            token = context.put_flash(
                Flash(
                    kind="ok",
                    title="已删除这条记忆",
                    detail=f"关联关系移除了 {report.links_removed} 条，来源本身保留 {report.sources_kept} 个。",
                )
            )
            return self._redirect(f"/memories?flash={token}")
        else:
            current = context.repository.require_memory(memory_id)
            changes: dict[str, Any] = {
                "title": form.get("title", ""),
                "content": form.get("content", ""),
            }
            if form.get("summary", "") is not None:
                changes["summary"] = form.get("summary", "") or None
            if "tags" in form:
                changes["tags"] = [tag.strip() for tag in form.get("tags", "").split(",") if tag.strip()]
            for field_name in ("importance", "confidence"):
                raw = form.get(field_name)
                if raw not in (None, ""):
                    try:
                        changes[field_name] = float(raw)
                    except ValueError as exc:
                        raise ValidationError(
                            f"{field_name} 必须是 0–1 之间的小数，收到 {raw!r}", field=field_name
                        ) from exc
            if form.get("status") and form["status"] != str(current.status):
                # only a real change goes through the lifecycle state machine
                changes["status"] = form["status"]
            memory = context.lifecycle.update_memory(memory_id, **changes)
            token = context.put_flash(
                Flash(
                    kind="ok",
                    title="已保存修改",
                    detail="标题、正文等修改已保存。",
                )
            )
            return self._redirect(f"/memories/{memory_id}?flash={token}")

        titles = {"archive": "已归档", "restore": "已恢复", "activate": "已记住"}
        token = context.put_flash(
            Flash(kind="ok", title=titles.get(action, "操作完成"), detail=summary)
        )
        self._redirect(f"/memories/{memory_id}?flash={token}")

    # ------------------------------------------------------------------
    # Teacher (P2D-2): Memory -> 开始学习 -> 对话 -> 真实学习状态
    # ------------------------------------------------------------------
    def _teacher_entry(self) -> Any:
        """The product-level Teacher entry, built once per context (lazy)."""
        if self.context.teacher is None:
            self.context.teacher = teacher_ui.TeacherEntry(self.context)
        return self.context.teacher

    def _learn_snapshot(self, memory_id: str) -> dict[str, Any]:
        """Read-only snapshot for the Teacher page (existing APIs + application boundary).

        No learning rule is evaluated here: the session, its cursor and its current Memory
        come from ``LearningSession`` objects the application boundary returned.
        """
        memory = self.context.repository.require_memory(memory_id)
        entry = self._teacher_entry()
        ready, reason = entry.available()
        sources = entry.sources_for_memory(memory_id)
        session = None
        current_memory = None
        if ready and sources:
            session = entry.active_session(sources[0].id)
            if session is not None and session.current_memory_id:
                current_memory = self.context.repository.get_memory(session.current_memory_id)
        return {
            "memory": memory,
            "sources": sources,
            "session": session,
            "current_memory": current_memory,
            "ready": ready,
            "reason": reason,
        }

    def _render_learn(
        self,
        memory_id: str,
        *,
        status: int = 200,
        turn: Any | None = None,
        error: BaseException | None = None,
        message: str = "",
        flash: Flash | None = None,
    ) -> None:
        snapshot = self._learn_snapshot(memory_id)
        self._send_page(
            status,
            teacher_ui.learn_page(turn=turn, error=error, message=message, flash=flash, **snapshot),
        )

    def _get_learn(self, memory_id: str, flash: Flash | None) -> None:
        self._render_learn(memory_id, flash=flash)

    def _post_learn_start(self, memory_id: str) -> None:
        """Start (or refuse to duplicate) this Memory's learning session."""
        try:
            context = self._teacher_entry().start(memory_id)
        except MemorySystemError as exc:
            # a refusal is rendered as a refusal: correct status, no success claim
            status, _ = classify_error(exc)
            return self._render_learn(memory_id, status=status, error=exc)
        token = self.context.put_flash(
            Flash(
                kind="ok",
                title="开始了一次学习",
                detail=f"这次学习只包含 1 条记忆（计划共 {len(context.session.plan)} 条），"
                       "现在可以和卡比对话了。",
            )
        )
        self._redirect(f"{teacher_ui.LEARN_ROUTE}/{memory_id}?flash={token}")

    def _post_learn_turn(self, memory_id: str) -> None:
        """One real Teacher turn for the session running on this Memory's Source."""
        form = self._read_form()
        message = (form.get("message") or "").strip()
        if not message:
            error = ValidationError("请先说点什么再发送（内容不能为空）", field="message")
            if self._wants_json():
                return self._send_json(400, teacher_ui.error_payload(error))
            return self._render_learn(memory_id, status=400, error=error)
        try:
            result = self._teacher_entry().turn(memory_id, message)
        except MemorySystemError as exc:
            status, _ = classify_error(exc)
            if self._wants_json():
                return self._send_json(status, teacher_ui.error_payload(exc))
            return self._render_learn(memory_id, status=status, error=exc, message=message)
        if self._wants_json():
            return self._send_json(200, teacher_ui.turn_payload(result))
        return self._render_learn(memory_id, turn=result)


class KnowledgeBaseServer(ThreadingHTTPServer):
    """Threaded HTTP server that carries the shared context."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], context: WebContext) -> None:
        self.context = context
        super().__init__(address, make_handler(context))


def make_handler(context: WebContext) -> type[KnowledgeBaseHandler]:
    """Bind a context to a handler subclass (no global state)."""
    return type("BoundKnowledgeBaseHandler", (KnowledgeBaseHandler,), {"context": context})


def create_server(
    context: WebContext, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> KnowledgeBaseServer:
    """Create (but do not start) the server.  ``port=0`` picks a free port (used by tests)."""
    return KnowledgeBaseServer((host, port), context)


def _banner(context: WebContext, host: str, port: int) -> str:
    summary = context.llm_summary()
    model = (
        f"{summary.get('provider')} / {summary.get('model')} (API key: "
        f"{'configured' if summary.get('api_key_present') else 'missing'})"
        if summary
        else f"not configured ({context.llm_error})"
    )
    lines = [
        "Personal Knowledge Base",
        f"http://{host}:{port}",
        f"database: {context.db_path}",
        f"model: {model}",
        "privacy: 真实的记忆形成会把输入内容发送给配置的模型服务；界面不显示密钥，也不写入日志。",
        "Press Ctrl+C to stop.",
    ]
    if host not in {"127.0.0.1", "localhost", "::1"}:
        lines.insert(3, f"warning: 正在监听 {host}，局域网内其它机器可能访问到你的知识库！")
    return "\n".join(lines)


def run_web(db_path: str | Path, args: Any) -> int:
    """CLI entry point for ``python -m personal_memory web``."""
    context = WebContext.create(
        Path(db_path),
        config_path=getattr(args, "config", None),
        quality_check=not getattr(args, "no_quality_check", False),
        max_bytes=getattr(args, "max_bytes", MAX_FILE_BYTES),
    )
    return serve(context, host=args.host, port=args.port)


def serve(context: WebContext, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    """Start serving until Ctrl+C; prints the URL banner after binding."""
    server = create_server(context, host=host, port=port)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    print(_banner(context, bound_host, bound_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)
    finally:
        # shutdown() must run on another thread, so only close the socket here
        server.server_close()
    return 0
