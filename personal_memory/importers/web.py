"""URL / Web importer: a public web page -> :class:`WebDocument` -> CaptureRequest.

    URL -> WebImporter -> WebDocument -> CaptureRequest -> CaptureService
        -> MemoryFormationService -> Phase 4 quality gate -> Memory System

The importer only **fetches, parses, cleans and normalises**.  It never opens SQLite,
never calls a model and never decides that something is worth remembering; persistence is
reached exclusively by handing a ``CaptureRequest`` to Phase 1's ``CaptureService``.

Security boundaries (spec §五) -- what is actually enforced
----------------------------------------------------------
* scheme whitelist: ``http`` / ``https`` only (``file:``, ``ftp:``, ``data:`` … rejected);
* credentials in the URL (``http://user:pass@host/``) are rejected;
* the host is checked **twice**: syntactically (``localhost``, ``*.local``, ``*.internal`` …)
  and by resolving it; every resolved address must be a *global* unicast address
  (loopback, private, link-local, CGNAT, multicast, reserved, unspecified and therefore
  cloud metadata endpoints such as ``169.254.169.254`` are refused);
* literal IP hosts are validated directly, without DNS;
* redirects are **never followed automatically**: the transport returns the 3xx, the
  importer validates the next target with the same rules and only then continues,
  with a hard hop limit;
* connect/read timeout, a byte cap (checked against ``Content-Length`` and enforced while
  reading), a ``Content-Type`` whitelist and a minimum extracted-body length;
* the body is never logged and never echoed in error messages.

Known boundary (stated honestly, per spec §五): validation happens *before* the request,
so a DNS answer that changes between validation and connection (DNS rebinding / TOCTOU) is
**not** fully prevented, and the response of an allowed host could still proxy to an
internal service.  This is a local single-user tool: it reduces SSRF risk substantially
but must not be described as absolute SSRF protection.  No cookies, no credentials, no
proxy support, no headless browser are used, and nothing here follows links found in a page.

Dependencies: standard library only (``urllib.request`` + ``html.parser`` + ``ipaddress``).
``lxml`` / ``beautifulsoup4`` / ``html5lib`` / ``readability`` / ``trafilatura`` are **not**
installed in this environment and are deliberately not required; ``requests`` happens to be
present but is not used, so the project keeps its zero-runtime-dependency guarantee.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from ..capture import CaptureRequest, CaptureResult, CaptureService
from ..errors import MemorySystemError, ValidationError
from ..models import SourceType, utcnow_iso
from .files import MAX_FILE_BYTES

__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_REDIRECTS",
    "MAX_WEB_BYTES",
    "MIN_CONTENT_CHARS",
    "USER_AGENT",
    "WebImportError",
    "InvalidUrlError",
    "BlockedUrlError",
    "UrlFetchError",
    "RedirectError",
    "ResponseTooLargeError",
    "ContentEncodingError",
    "UnsupportedContentTypeError",
    "ContentExtractionError",
    "EmptyContentError",
    "FetchResponse",
    "HttpTransport",
    "UrllibTransport",
    "WebDocument",
    "WebImportResult",
    "WebImporter",
    "import_web_url",
    "normalise_url",
    "assert_public_url",
    "extract_content",
    "decode_body",
    "decompress_bounded",
    "ExtractionResult",
]

#: Reuse the project's existing size limit (spec §二: 复用现有错误体系与大小限制).
MAX_WEB_BYTES = MAX_FILE_BYTES
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT_SECONDS = 15.0
#: Below this many characters of extracted text a page is considered empty/nav-only.
MIN_CONTENT_CHARS = 200
USER_AGENT = "PersonalKnowledgeBase/1.0 (+local URL importer; stdlib urllib)"

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan", ".localdomain")
BLOCKED_HOSTNAMES = ("localhost", "localhost.localdomain", "metadata", "metadata.google.internal")

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE)


# --------------------------------------------------------------------------
# errors (typed, body-free, no secrets)
# --------------------------------------------------------------------------


class WebImportError(MemorySystemError):
    """Base class: a URL could not be turned into capture-ready text.

    Raised before anything is persisted, so a failed URL import writes no Memory and no
    Source.  Messages never contain the page body or request credentials.
    """

    def __init__(self, message: str, *, url: str | None = None, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.url = url
        self.detail = detail


class InvalidUrlError(WebImportError):
    """Not a usable absolute http(s) URL."""


class BlockedUrlError(WebImportError):
    """The target is not a public address (SSRF guard)."""

    def __init__(self, message: str, *, url: str | None = None, addresses: Sequence[str] = ()) -> None:
        super().__init__(message, url=url)
        self.addresses = tuple(addresses)


class UrlFetchError(WebImportError):
    """DNS / connection / timeout / HTTP failure."""

    def __init__(
        self, message: str, *, url: str | None = None, status: int | None = None, reason: str | None = None
    ) -> None:
        super().__init__(message, url=url, detail=reason)
        self.status = status
        self.reason = reason


class RedirectError(WebImportError):
    """Too many redirects, or a redirect target that is not allowed."""


class ResponseTooLargeError(WebImportError):
    """The response exceeds the byte cap."""

    def __init__(self, url: str, *, size_bytes: int, max_bytes: int) -> None:
        super().__init__(
            f"response too large for {url}: {size_bytes} bytes > limit {max_bytes} bytes", url=url
        )
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes


class ContentEncodingError(WebImportError):
    """``Content-Encoding`` is unsupported, or the compressed body is corrupt/truncated.

    Previously such payloads were silently passed through as raw bytes and could be
    imported as garbage text; they are now an explicit, typed failure.
    """

    def __init__(self, url: str, *, encoding: str | None, detail: str | None = None) -> None:
        super().__init__(
            f"cannot decode content-encoding {encoding!r} for {url}: {detail or 'invalid stream'}",
            url=url,
            detail=detail,
        )
        self.encoding = encoding


class UnsupportedContentTypeError(WebImportError):
    """Content-Type is not a supported text/HTML type."""

    def __init__(self, url: str, *, content_type: str | None, supported: Sequence[str] = ALLOWED_CONTENT_TYPES) -> None:
        super().__init__(
            f"unsupported content type {content_type!r} for {url}; supported: {sorted(supported)}",
            url=url,
        )
        self.content_type = content_type
        self.supported = tuple(supported)


class PdfUrlContentError(UnsupportedContentTypeError):
    """The URL served a PDF file rather than a web page.

    The bytes ride along on the exception so the caller can hand them to
    :class:`~personal_memory.importers.pdf.PdfImporter` without a second download. The message
    never contains the body (only the URL and the media type), and the class subclasses
    :class:`UnsupportedContentTypeError`, so any caller that only knows the old error type keeps
    working unchanged.
    """

    def __init__(self, url: str, *, body: bytes = b"", content_type: str | None = None) -> None:
        super().__init__(url, content_type=content_type)
        self.body = body


def _looks_like_pdf(media_type: str | None, body: bytes) -> bool:
    """True when the response is a PDF: declared type, or PDF magic bytes on an untyped body."""
    if media_type == "application/pdf":
        return True
    return media_type in (None, "application/octet-stream") and body[:5].lstrip().startswith(b"%PDF-")


class ContentExtractionError(WebImportError):
    """The HTML could not be parsed into article text."""


class EmptyContentError(WebImportError):
    """The page has no usable body (empty, too short, or navigation only)."""

    def __init__(self, url: str, *, content_chars: int, min_chars: int) -> None:
        super().__init__(
            f"no usable article text at {url}: extracted {content_chars} characters, minimum is {min_chars}",
            url=url,
        )
        self.content_chars = content_chars
        self.min_chars = min_chars


# --------------------------------------------------------------------------
# URL validation / SSRF guard
# --------------------------------------------------------------------------


def normalise_url(raw: str) -> str:
    """Validate an absolute http(s) URL and return its normalised form (fragment dropped)."""
    if not isinstance(raw, str):
        raise InvalidUrlError(f"url must be a string, got {type(raw).__name__}")
    candidate = raw.strip()
    if not candidate:
        raise InvalidUrlError("url must not be empty")
    if any(ord(char) < 32 or char == " " for char in candidate):
        raise InvalidUrlError("url must not contain whitespace or control characters")
    parts = urllib.parse.urlsplit(candidate)
    if not parts.scheme:
        raise InvalidUrlError(f"url must be absolute and include a scheme: {candidate!r}")
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidUrlError(
            f"unsupported url scheme {scheme!r}; only {list(ALLOWED_SCHEMES)} are allowed"
        )
    if parts.username or parts.password:
        raise InvalidUrlError("url must not contain user credentials")
    if not parts.hostname:
        raise InvalidUrlError(f"url has no host: {candidate!r}")
    try:
        port = parts.port  # property raises ValueError for a malformed port
    except ValueError as exc:
        raise InvalidUrlError(f"invalid port in url: {exc}") from exc
    if port is not None and not 1 <= port <= 65535:
        raise InvalidUrlError(f"port out of range: {port}")
    netloc = parts.netloc
    path = parts.path or "/"
    return urllib.parse.urlunsplit((scheme, netloc, path, parts.query, ""))


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlFetchError(f"dns lookup failed for {host}", reason=str(exc)) from exc
    return sorted({info[4][0] for info in infos})


def assert_public_url(
    url: str,
    *,
    resolver: Callable[[str, int], Sequence[str]] | None = None,
) -> tuple[str, list[str]]:
    """Reject anything that is not a public http(s) resource; return ``(host, addresses)``."""
    normalised = normalise_url(url)
    parts = urllib.parse.urlsplit(normalised)
    host = parts.hostname or ""
    lowered = host.strip().lower().rstrip(".")
    if lowered in BLOCKED_HOSTNAMES or lowered.endswith(BLOCKED_HOST_SUFFIXES):
        raise BlockedUrlError(f"host {host!r} is a local/internal name and is not allowed", url=normalised)

    literal = None
    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise BlockedUrlError(
                f"address {literal} is not a public address (loopback/private/reserved)", url=normalised,
                addresses=[str(literal)],
            )
        return host, [str(literal)]

    port = parts.port or (443 if parts.scheme == "https" else 80)
    addresses = list((resolver or _resolve)(host, port))
    if not addresses:
        raise UrlFetchError(f"host {host!r} did not resolve to any address", url=normalised)
    blocked: list[str] = []
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            blocked.append(address)
            continue
        if not parsed.is_global:
            blocked.append(str(parsed))
    if blocked:
        raise BlockedUrlError(
            f"host {host!r} resolves to non-public addresses {sorted(blocked)}", url=normalised, addresses=blocked
        )
    return host, addresses


# --------------------------------------------------------------------------
# HTTP transport (no automatic redirects)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResponse:
    """One HTTP response, exactly as it came off the wire."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


class HttpTransport(Protocol):  # pragma: no cover - protocol
    def fetch(
        self, url: str, *, timeout: float, max_bytes: int, headers: Mapping[str, str]
    ) -> FetchResponse:
        ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Make 3xx responses visible instead of following them (each hop is re-validated)."""

    def redirect_request(self, req, fp, code, msg, hdrs, newurl):  # noqa: ANN001
        return None


class UrllibTransport:
    """Real HTTP(S) transport: single GET, no redirects, byte-capped read, no cookies."""

    def __init__(self, *, opener: Any | None = None) -> None:
        self._opener = opener or urllib.request.build_opener(_NoRedirect)

    def fetch(
        self, url: str, *, timeout: float, max_bytes: int, headers: Mapping[str, str]
    ) -> FetchResponse:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            # 3xx (our handler returns None), 4xx and 5xx all arrive here
            try:
                body = error.read(max_bytes + 1)
            except Exception:  # pragma: no cover - defensive
                body = b""
            finally:
                error.close()
            return FetchResponse(
                status=int(error.code),
                headers={k: v for k, v in (error.headers or {}).items()},
                body=body,
                url=url,
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise UrlFetchError(f"request failed for {url}", url=url, reason=str(reason)) from exc
        with response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ResponseTooLargeError(url, size_bytes=int(declared), max_bytes=max_bytes)
            body = response.read(max_bytes + 1)
            return FetchResponse(
                status=int(response.status),
                headers={k: v for k, v in response.headers.items()},
                body=body,
                url=url,
            )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


#: How much compressed input is handed to the decompressor per step.  Together with
#: ``max_length`` this bounds both the input slice and the output of a single call.
_INFLATE_CHUNK = 64 * 1024


def _inflate_bounded(
    data: bytes,
    *,
    wbits: int,
    limit: int,
    url: str,
    encoding: str,
) -> bytes:
    """Inflate ``data`` while never producing more than ``limit + 1`` bytes.

    The cap is enforced **during** decompression, not after it: each ``decompress`` call is
    given ``max_length = remaining + 1``, so a decompression bomb is stopped as soon as it
    crosses the limit instead of materialising the whole expansion first.
    """
    decompressor = zlib.decompressobj(wbits)
    out = bytearray()

    def step(chunk: bytes) -> bytes:
        """One bounded inflate call: at most ``limit - len(out)`` bytes (minimum 1).

        Asking for one byte when the cap is reached is what makes "exactly at the limit"
        acceptable while "one byte over" is still detected -- without ever letting a single
        call produce unbounded output.
        """
        allowance = max(1, limit - len(out))
        try:
            piece = decompressor.decompress(chunk, allowance)
        except zlib.error as exc:
            raise ContentEncodingError(url, encoding=encoding, detail=str(exc)) from exc
        out.extend(piece)
        if len(out) > limit:
            raise ResponseTooLargeError(url, size_bytes=len(out), max_bytes=limit)
        return piece

    pending = data
    while pending:
        step(pending)
        # unconsumed_tail carries the input that max_length prevented us from processing
        pending = decompressor.unconsumed_tail

    # drain anything the inflate state still buffers internally, still bounded
    while step(b""):
        pass

    if not decompressor.eof:
        raise ContentEncodingError(url, encoding=encoding, detail="truncated or incomplete compressed stream")
    return bytes(out)


def decompress_bounded(
    body: bytes,
    headers: Mapping[str, str],
    *,
    max_bytes: int,
    url: str | None = None,
) -> tuple[bytes, str]:
    """Decode a response body: ``(data, content_encoding_used)``.

    * absent/``identity`` -> the body is returned unchanged;
    * ``gzip``/``x-gzip``  -> streamed inflate with a hard output cap;
    * ``deflate``          -> zlib-wrapped stream, falling back to raw deflate;
    * anything else (``br``, ``zstd``, stacked ``gzip, br``, unknown tokens) -> explicit
      :class:`ContentEncodingError` instead of treating compressed bytes as text.

    ``max_bytes`` caps the **decompressed** output; the already-enforced cap on the raw
    response bytes stays in place in :meth:`WebImporter.fetch`.
    """
    source = url or "<page>"
    declared = (_header(headers, "content-encoding") or "").strip().lower()
    if not declared or declared == "identity":
        return body, "identity"

    encodings = [part.strip() for part in declared.split(",") if part.strip()]
    if len(encodings) != 1:
        raise ContentEncodingError(
            source, encoding=declared, detail="stacked/multiple content encodings are not supported"
        )
    name = encodings[0]
    if name in ("gzip", "x-gzip"):
        return _inflate_bounded(body, wbits=16 + zlib.MAX_WBITS, limit=max_bytes, url=source, encoding=name), name
    if name == "deflate":
        try:
            return _inflate_bounded(body, wbits=zlib.MAX_WBITS, limit=max_bytes, url=source, encoding=name), name
        except ContentEncodingError as wrapped_error:
            try:
                return _inflate_bounded(body, wbits=-zlib.MAX_WBITS, limit=max_bytes, url=source, encoding=name), name
            except ContentEncodingError:
                raise wrapped_error
    raise ContentEncodingError(source, encoding=declared, detail="unsupported content encoding")


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------


def decode_body(body: bytes, headers: Mapping[str, str] | None = None) -> tuple[str, str, bool]:
    """Decode a page: declared charset first, then common fallbacks.

    Returns ``(text, encoding_used, fallback_used)``.  Never raises: a page that cannot be
    decoded cleanly is decoded with replacement characters and flagged, because a wrong
    guess is better reported than silently mangled.
    """
    headers = headers or {}
    declared = None
    content_type = _header(headers, "content-type") or ""
    match = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_\-]+)", content_type, re.IGNORECASE)
    if match:
        declared = match.group(1)
    if not declared:
        meta = _META_CHARSET_RE.search(body[:4096])
        if meta:
            declared = meta.group(1).decode("ascii", errors="ignore")
    candidates: list[str] = []
    if declared:
        candidates.append(declared)
    candidates.extend(["utf-8", "gb18030", "cp1252", "latin-1"])
    for candidate in candidates:
        try:
            text = body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
        declared_used = bool(declared) and candidate.lower().replace("_", "-") == declared.lower().replace("_", "-")
        return text, candidate, bool(declared) and not declared_used
    return body.decode("utf-8", errors="replace"), "utf-8/replace", True


# --------------------------------------------------------------------------
# HTML -> text
# --------------------------------------------------------------------------

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
#: never part of article text
_SKIP_TAGS = {
    "script", "style", "noscript", "template", "svg", "math", "iframe", "object", "embed",
    "canvas", "form", "button", "select", "option", "textarea", "label", "input", "dialog",
    "audio", "video", "map",
}
#: page furniture (dropped from the body; the title is still found tree-wide)
_NOISE_TAGS = {"nav", "footer", "header", "aside"}
#: ARIA landmark roles that are never article text (semantic, not a class-name heuristic)
_NOISE_ROLES = {
    "navigation", "banner", "contentinfo", "complementary", "search", "form", "dialog",
    "alertdialog", "menu", "menubar", "toolbar", "tablist", "tabpanel", "log", "status",
}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre", "table", "thead", "tbody", "tr", "td", "th",
    "figure", "figcaption", "dl", "dt", "dd", "details", "summary", "address", "hr", "br",
}
_CODE_TAGS = {"pre", "code", "kbd", "samp", "var"}
_CONTENT_ROOTS = ("article", "main")


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Mapping[str, str] | None = None, parent: "_Node | None" = None) -> None:
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children: list[Any] = []
        self.parent = parent

    def find_all(self, tag: str) -> list["_Node"]:
        found: list[_Node] = []
        for child in self.children:
            if isinstance(child, _Node):
                if child.tag == tag:
                    found.append(child)
                found.extend(child.find_all(tag))
        return found

    def attribute(self, name: str) -> str | None:
        for key, value in self.attrs.items():
            if key.lower() == name:
                return value
        return None


class _TreeBuilder(HTMLParser):
    """Minimal, forgiving DOM builder (enough for article extraction)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document")
        self._stack: list[_Node] = [self.root]

    # -- HTMLParser hooks ------------------------------------------------
    def handle_starttag(self, tag: str, attrs: Iterable[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k: (v or "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag in _VOID_TAGS:
            return
        self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: Iterable[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k: (v or "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        # skipped at render time (script/style/... are never rendered), so data can always
        # be attached to the current node
        if data:
            self._stack[-1].children.append(data)

    def handle_comment(self, data: str) -> None:  # comments carry no article text
        return

    def error(self, message: str) -> None:  # pragma: no cover - HTMLParser legacy hook
        return


def _text_of(node: _Node) -> str:
    """Plain concatenated text of a subtree (used for titles)."""
    parts: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            parts.append(item)
            return
        if item.tag in _SKIP_TAGS:
            return
        for child in item.children:
            walk(child)

    walk(node)
    return " ".join("".join(parts).split())


def _render(node: Any, *, preserve: bool = False, out: list[str] | None = None) -> str:
    """Render a subtree to text: blocks separated by blank lines, code preserved."""
    if out is None:
        out = []
    if isinstance(node, str):
        text = node.replace("\xa0", " ").replace("\u200b", "")
        if preserve:
            out.append(text)
        else:
            out.append(re.sub(r"[ \t\r\f\v]+", " ", text))
        return "".join(out)

    tag = node.tag
    if tag in _SKIP_TAGS or tag in _NOISE_TAGS:
        return "".join(out)
    role = (node.attribute("role") or "").strip().lower()
    if role in _NOISE_ROLES:
        return "".join(out)
    if tag in _CODE_TAGS:
        preserve = True

    if tag == "br":
        out.append("\n")
        return "".join(out)
    if tag == "hr":
        out.append("\n\n")
        return "".join(out)
    if tag == "li":
        out.append("\n- ")
    elif tag == "img":
        alt = (node.attribute("alt") or "").strip()
        if alt:
            out.append(f"[{alt}]")
        return "".join(out)
    elif tag == "a":
        out.append(_render_children(node, preserve=preserve, out=[]))
        href = (node.attribute("href") or "").strip()
        if href and not href.startswith("#") and href not in _text_of(node):
            out.append(f" ({href})")
        return "".join(out)
    elif tag in _BLOCK_TAGS:
        out.append("\n\n")

    _render_children(node, preserve=preserve, out=out)

    if tag in _BLOCK_TAGS and tag != "li":
        out.append("\n\n")
    return "".join(out)


def _render_children(node: _Node, *, preserve: bool, out: list[str]) -> str:
    for child in node.children:
        _render(child, preserve=preserve, out=out)
    return "".join(out)


def _tidy(text: str) -> str:
    """Normalise whitespace, but keep the indentation of preserved (code) blocks.

    Non-code text is already collapsed to single spaces by :func:`_render`, so a line that
    still starts with whitespace can only come from a ``pre``/``code`` block.
    """
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line.rstrip() if line[:1].isspace() else line.strip() for line in raw_lines]
    cleaned: list[str] = []
    blank = 0
    for line in lines:
        if line:
            blank = 0
            cleaned.append(line)
            continue
        blank += 1
        if blank <= 1:
            cleaned.append("")
    return "\n".join(cleaned).strip()


@dataclass(frozen=True)
class ExtractionResult:
    """What the HTML parser could recover from a page."""

    title: str | None
    title_source: str
    content: str
    links: int
    headings: int
    root_tag: str

    @property
    def content_chars(self) -> int:
        return len(self.content)


def extract_content(html_text: str, *, min_chars: int = MIN_CONTENT_CHARS, url: str | None = None) -> ExtractionResult:
    """Extract the article title + body text from HTML (spec §六).

    Order: ``<article>`` -> ``<main>`` -> ``body`` -> whole document; the first candidate
    whose text reaches ``min_chars`` wins, otherwise the largest one is reported as too
    short (never silently imported).  ``script``/``style``/``nav``/``footer``/``header``/
    ``aside``/forms and other non-article tags are dropped, entities are decoded and
    whitespace is normalised; paragraphs, lists and code text are preserved.
    """
    if not isinstance(html_text, str):
        raise ValidationError(f"html must be a string, got {type(html_text).__name__}", field="html")
    if not html_text.strip():
        raise EmptyContentError(url or "<page>", content_chars=0, min_chars=min_chars)

    builder = _TreeBuilder()
    try:
        builder.feed(html_text)
        builder.close()
    except Exception as exc:  # pragma: no cover - HTMLParser is very forgiving
        raise ContentExtractionError(f"could not parse html: {exc}", url=url) from exc

    document = builder.root

    # title: <title> first, then the first <h1>, then og:title (tree-wide, noise included)
    title: str | None = None
    title_source = "none"
    for node in document.find_all("title"):
        candidate = _text_of(node)
        if candidate:
            title, title_source = candidate, "title-tag"
            break
    if title is None:
        for node in document.find_all("h1"):
            candidate = _text_of(node)
            if candidate:
                title, title_source = candidate, "h1"
                break
    if title is None:
        for node in document.find_all("meta"):
            if (node.attribute("property") or "").lower() == "og:title":
                candidate = (node.attribute("content") or "").strip()
                if candidate:
                    title, title_source = candidate, "og:title"
                    break

    candidates: list[tuple[str, _Node]] = []
    for tag in _CONTENT_ROOTS:
        for node in document.find_all(tag):
            candidates.append((tag, node))
    body_nodes = document.find_all("body")
    candidates.append(("body", body_nodes[0] if body_nodes else document))
    if not body_nodes:
        candidates.append(("document", document))

    rendered: list[tuple[str, str]] = []
    for tag, node in candidates:
        text = _tidy(_render(node))
        rendered.append((tag, text))
        if len(text) >= min_chars:
            links = len(node.find_all("a"))
            headings = sum(len(node.find_all(f"h{level}")) for level in range(1, 7))
            return ExtractionResult(title, title_source, text, links, headings, tag)

    best_tag, best_text = max(rendered, key=lambda item: len(item[1])) if rendered else ("document", "")
    raise EmptyContentError(url or "<page>", content_chars=len(best_text), min_chars=min_chars)


# --------------------------------------------------------------------------
# document + importer
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WebDocument:
    """A fetched page: title, article text, identity and light provenance."""

    original_url: str
    final_url: str
    title: str | None
    content: str
    content_type: str | None
    fetched_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    title_source: str = "none"

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValidationError("web document content must not be empty", field="content")
        if self.title is not None and (not isinstance(self.title, str) or not self.title.strip()):
            raise ValidationError("title must be None or a non-empty string", field="title")

    @property
    def content_chars(self) -> int:
        return len(self.content)

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.final_url).hostname or ""

    @property
    def content_hash(self) -> str:
        """sha256 of the *extracted text* (raw bytes digest is in the metadata)."""
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def preview(self, limit: int = 160) -> str:
        line = " ".join(self.content.split())
        return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"

    def as_dict(self, *, include_content: bool = False, include_preview: bool = False) -> dict[str, Any]:
        """Redacted by default: identity/counts, never the page body."""
        payload: dict[str, Any] = {
            "original_url": self.original_url,
            "final_url": self.final_url,
            "host": self.host,
            "title": self.title,
            "title_source": self.title_source,
            "content_type": self.content_type,
            "fetched_at": self.fetched_at,
            "content_chars": self.content_chars,
            "content_sha256": self.content_hash[:16].upper(),
            "metadata": dict(self.metadata),
        }
        if include_content:
            payload["content"] = self.content
        elif include_preview:
            payload["content_preview"] = self.preview()
        return payload

    def to_capture_request(
        self,
        *,
        source_type: SourceType | str = SourceType.WEB,
        title: str | None = None,
        captured_at: str | None = None,
    ) -> CaptureRequest:
        """Build the Phase 1 request: extracted text + web provenance, no raw HTML."""
        metadata: dict[str, Any] = {
            "captured_from": "web",
            "original_url": self.original_url,
            "final_url": self.final_url,
            "fetched_at": self.fetched_at,
            "content_type": self.content_type,
            "content_sha256": self.content_hash,
            "content_chars": self.content_chars,
            "title_source": self.title_source,
        }
        for key, value in self.metadata.items():
            metadata.setdefault(key, value)
        for key in list(metadata):
            if metadata[key] is None:
                metadata.pop(key)
        return CaptureRequest(
            content=self.content,
            title=title or self.title,
            source_type=source_type,
            url=self.final_url,
            metadata=metadata,
            captured_at=captured_at or utcnow_iso(),
        )


class WebImporter:
    """Fetches a public page and returns a :class:`WebDocument`; never persists anything."""

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        resolver: Callable[[str, int], Sequence[str]] | None = None,
        max_bytes: int = MAX_WEB_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_redirects: int = MAX_REDIRECTS,
        min_content_chars: int = MIN_CONTENT_CHARS,
        user_agent: str = USER_AGENT,
        allowed_content_types: Sequence[str] = ALLOWED_CONTENT_TYPES,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValidationError(f"max_bytes must be an integer >= 1, got {max_bytes!r}", field="max_bytes")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValidationError(f"timeout_seconds must be > 0, got {timeout_seconds!r}", field="timeout_seconds")
        if isinstance(max_redirects, bool) or not isinstance(max_redirects, int) or max_redirects < 0:
            raise ValidationError(f"max_redirects must be an integer >= 0, got {max_redirects!r}", field="max_redirects")
        if isinstance(min_content_chars, bool) or not isinstance(min_content_chars, int) or min_content_chars < 1:
            raise ValidationError(
                f"min_content_chars must be an integer >= 1, got {min_content_chars!r}", field="min_content_chars"
            )
        if not allowed_content_types:
            raise ValidationError("allowed_content_types must not be empty", field="allowed_content_types")
        self.transport = transport or UrllibTransport()
        self.resolver = resolver
        self.max_bytes = int(max_bytes)
        self.timeout_seconds = float(timeout_seconds)
        self.max_redirects = int(max_redirects)
        self.min_content_chars = int(min_content_chars)
        self.user_agent = user_agent
        self.allowed_content_types = tuple(allowed_content_types)

    # -- fetch ------------------------------------------------------------
    def fetch(self, url: str, *, fetched_at: str | None = None) -> WebDocument:
        """Validate -> GET -> (validated redirects) -> decode -> extract -> WebDocument."""
        current = normalise_url(url)
        original = current
        chain: list[str] = []
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
            "Accept-Language": "en,zh-CN;q=0.8",
            "Accept-Encoding": "identity",
        }
        response: FetchResponse | None = None
        for _ in range(self.max_redirects + 1):
            # every hop is validated with the same rules before the request is sent
            assert_public_url(current, resolver=self.resolver)
            try:
                response = self.transport.fetch(
                    current, timeout=self.timeout_seconds, max_bytes=self.max_bytes, headers=headers
                )
            except WebImportError:
                raise
            except (TimeoutError, socket.timeout, urllib.error.URLError, OSError) as exc:
                # a transport (real or injected) must never leak a raw socket error
                reason = getattr(exc, "reason", exc)
                raise UrlFetchError(f"request failed for {current}", url=current, reason=str(reason)) from exc
            if response.status in _REDIRECT_STATUSES:
                location = _header(response.headers, "location")
                if not location:
                    raise RedirectError(f"redirect without a Location header from {current}", url=current)
                target = urllib.parse.urljoin(current, location)
                target = normalise_url(target)  # scheme/credentials validated here
                if len(chain) >= self.max_redirects:
                    raise RedirectError(
                        f"more than {self.max_redirects} redirects while fetching {original}", url=current
                    )
                chain.append(target)
                current = target
                continue
            break
        else:  # pragma: no cover - the loop always breaks or raises
            raise RedirectError(f"too many redirects while fetching {original}", url=current)

        assert response is not None
        if response.status >= 400:
            raise UrlFetchError(
                f"http error {response.status} for {current}", url=current, status=response.status
            )
        if response.status >= 300:
            raise UrlFetchError(f"unhandled http status {response.status} for {current}", url=current, status=response.status)

        declared_length = (_header(response.headers, "content-length") or "").strip()
        if declared_length.isdigit() and int(declared_length) > self.max_bytes:
            raise ResponseTooLargeError(current, size_bytes=int(declared_length), max_bytes=self.max_bytes)
        if len(response.body) > self.max_bytes:
            # transport-independent guard: an oversized *compressed* body never reaches
            # the decompressor at all
            raise ResponseTooLargeError(current, size_bytes=len(response.body), max_bytes=self.max_bytes)
        body, content_encoding_used = decompress_bounded(
            response.body, response.headers, max_bytes=self.max_bytes, url=current
        )
        if len(body) > self.max_bytes:  # defence in depth; decompress_bounded already caps
            raise ResponseTooLargeError(current, size_bytes=len(body), max_bytes=self.max_bytes)

        content_type_header = _header(response.headers, "content-type") or ""
        media_type = content_type_header.split(";", 1)[0].strip().lower() or None
        sniff = body[:512].lstrip().lower()
        looks_like_html = sniff.startswith(b"<!doctype") or b"<html" in sniff or b"<body" in sniff
        if media_type not in self.allowed_content_types:
            if media_type is None and looks_like_html:
                media_type = "text/html"  # no Content-Type: accept only when it clearly is HTML
            elif _looks_like_pdf(media_type, body):
                # a PDF served over HTTP goes to the PDF importer (same URL policy, same bytes)
                raise PdfUrlContentError(current, body=body, content_type=media_type)
            else:
                raise UnsupportedContentTypeError(current, content_type=media_type)

        text, encoding_used, encoding_fallback = decode_body(body, response.headers)
        if media_type == "text/plain":
            content = _tidy(text)
            extraction = ExtractionResult(None, "none", content, 0, 0, "text/plain")
            if len(content) < self.min_content_chars:
                raise EmptyContentError(current, content_chars=len(content), min_chars=self.min_content_chars)
            title, title_source = None, "none"
        else:
            extraction = extract_content(text, min_chars=self.min_content_chars, url=current)
            content = extraction.content
            title, title_source = extraction.title, extraction.title_source

        metadata = {
            "http_status": response.status,
            "body_bytes": len(response.body),
            "content_encoding": content_encoding_used,
            "decompressed_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "charset": encoding_used,
            "encoding_fallback": encoding_fallback,
            "redirects": list(chain),
            "redirect_count": len(chain),
            "links": extraction.links,
            "headings": extraction.headings,
            "extraction_root": extraction.root_tag,
            "user_agent": self.user_agent,
        }
        return WebDocument(
            original_url=original,
            final_url=current,
            title=title,
            content=content,
            content_type=media_type,
            fetched_at=fetched_at or utcnow_iso(),
            metadata=metadata,
            title_source=title_source,
        )

    # -- Capture handoff --------------------------------------------------
    def import_document(
        self,
        document: WebDocument,
        capture: CaptureService,
        *,
        source_type: SourceType | str = SourceType.WEB,
        title: str | None = None,
        dry_run: bool = False,
    ) -> "WebImportResult":
        """Hand a document to Phase 1's Capture (the only persistence path)."""
        if not isinstance(document, WebDocument):
            raise ValidationError(
                f"expected a WebDocument, got {type(document).__name__}", field="document"
            )
        if not isinstance(capture, CaptureService):
            raise ValidationError(
                f"WebImporter needs a CaptureService, got {type(capture).__name__}", field="capture"
            )
        result = capture.capture_request(
            document.to_capture_request(source_type=source_type, title=title), dry_run=dry_run
        )
        return WebImportResult(
            document=document, capture_result=result, import_status="preview" if dry_run else "imported"
        )

    def import_url(
        self,
        url: str,
        capture: CaptureService,
        *,
        title: str | None = None,
        source_type: SourceType | str = SourceType.WEB,
        dry_run: bool = False,
    ) -> "WebImportResult":
        """``fetch(url)`` + ``import_document(...)`` in one call."""
        document = self.fetch(url)
        return self.import_document(document, capture, source_type=source_type, title=title, dry_run=dry_run)


@dataclass(frozen=True)
class WebImportResult:
    """What the URL importer can report (counts/identity -- not the page body)."""

    document: WebDocument
    capture_result: CaptureResult
    import_status: str = "imported"

    @property
    def title(self) -> str | None:
        return self.document.title

    @property
    def url(self) -> str:
        return self.document.final_url

    @property
    def status(self) -> str:
        return self.capture_result.status

    @property
    def formation_status(self) -> str:
        return self.capture_result.formation_status

    @property
    def memories_created(self):
        return self.capture_result.memories_created

    @property
    def sources_created(self):
        return self.capture_result.sources_created

    @property
    def memory_count(self) -> int:
        return self.capture_result.memory_count

    @property
    def source_count(self) -> int:
        return self.capture_result.source_count

    @property
    def source_reused(self) -> bool:
        return self.capture_result.source_reused

    def as_dict(self, *, include_content: bool = False, include_preview: bool = False) -> dict[str, Any]:
        """Redacted view: URL/title/counts; the extracted text needs an explicit opt-in."""
        capture_payload = self.capture_result.as_dict()
        formation = self.capture_result.formation_result
        payload: dict[str, Any] = {
            "document": self.document.as_dict(
                include_content=include_content, include_preview=include_preview
            ),
            "title": self.title,
            "title_source": self.document.title_source,
            "url": self.url,
            "original_url": self.document.original_url,
            "host": self.document.host,
            "content_chars": self.document.content_chars,
            "import_status": self.import_status,
            "status": capture_payload["status"],
            "formation_status": capture_payload["formation_status"],
            "worth_remembering": formation.worth_remembering,
            "memory_count": capture_payload["memory_count"],
            "source_count": capture_payload["source_count"],
            "source_reused": capture_payload["source_reused"],
            "memories_created": capture_payload["memories_created"],
            "sources_created": capture_payload["sources_created"],
            "reason": formation.reason,
            "attempts": formation.attempts,
            "model": formation.model,
        }
        if include_content:
            payload["capture_result"] = capture_payload
        return payload


def import_web_url(
    url: str,
    capture: CaptureService,
    *,
    title: str | None = None,
    source_type: SourceType | str = SourceType.WEB,
    dry_run: bool = False,
    max_bytes: int = MAX_WEB_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> WebImportResult:
    """Module-level convenience: ``WebImporter(...).import_url(...)``."""
    return WebImporter(max_bytes=max_bytes, timeout_seconds=timeout_seconds).import_url(
        url, capture, title=title, source_type=source_type, dry_run=dry_run
    )
