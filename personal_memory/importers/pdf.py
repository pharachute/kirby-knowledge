"""PDF importer: a local text-based PDF -> :class:`PdfDocument` -> CaptureRequest.

    PDF -> PdfImporter -> PdfDocument -> CaptureRequest -> CaptureService
        -> MemoryFormationService -> Phase 4 quality gate -> Memory System

Scope of this importer (Phase 5): **ordinary text PDFs**.  Nothing here OCRs images,
detects tables, recovers reading order, or understands formulas.  The only promise about
layout is that the presence of a table or of multiple columns must not make the text
import *fail*; a perfect reconstruction is explicitly not attempted (see the phase report
for the measured limits).

Bounded processing (never "parse everything, then check")
--------------------------------------------------------
The page loop is streamed and every limit is enforced **while** iterating:

* ``Path.stat()`` is checked against ``max_bytes`` **before** the file is read, and the
  buffer length is re-checked after reading (defensive double gate);
* pages are pulled lazily from ``PDFPage.create_pages`` -- the counter is incremented per
  page and ``PdfTooManyPagesError`` is raised the moment ``max_pages`` is exceeded, so a
  thousands-of-pages file is never parsed to the end;
* the accumulated character count is checked after every page and
  ``PdfTextTooLargeError`` is raised as soon as ``max_text_chars`` is exceeded.

Security / privacy
------------------
* No SQLite, no LLM, no persistence of its own: the only way out is a ``CaptureRequest``
  handed to Phase 1's ``CaptureService`` (the frozen pipeline decides value and dedupe).
* Encrypted PDFs are never cracked or guessed: password-protected files, files whose
  permissions forbid text extraction, and other encryption errors all become
  :class:`PdfEncryptedError`.
* Corrupt/truncated files (bad header, missing root object, syntax errors, EOF in the
  middle of a stream) become :class:`PdfCorruptError`; error messages carry the file name,
  the failing stage and the parser exception *type*, never a binary payload.
* ``source_path`` lives on the in-memory document only; the Source metadata records the
  **filename**, never a local absolute path, and every value is JSON-serialisable
  (``pdfminer`` returns ``bytes`` for metadata, which is decoded here).

Dependency: ``pdfminer.six`` (installed into the Knowledge Base interpreter for this
phase).  It is imported lazily so the rest of the package keeps working on an interpreter
without it, and a missing backend raises :class:`PdfBackendUnavailableError`.
"""

from __future__ import annotations

import codecs
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..capture import CaptureRequest, CaptureResult, CaptureService
from ..errors import MemorySystemError, ValidationError
from ..models import SourceType, normalize_content, utcnow_iso
from .files import FileMissingError, FileReadError, NotAFileError, UnsupportedFileTypeError

__all__ = [
    "EXTRACTION_BACKEND",
    "MAX_PDF_BYTES",
    "MAX_PDF_PAGES",
    "MAX_PDF_TEXT_CHARS",
    "MIN_PDF_CHARS",
    "PDF_EXTENSIONS",
    "PDF_METADATA_FIELDS",
    "PdfError",
    "PdfBackendUnavailableError",
    "PdfTooLargeError",
    "PdfTooManyPagesError",
    "PdfEncryptedError",
    "PdfCorruptError",
    "PdfEmptyTextError",
    "PdfTextTooLargeError",
    "PdfDocument",
    "PdfImportResult",
    "PdfImporter",
    "import_pdf_file",
    "is_usable_pdf_title",
    "normalise_page_text",
    "decode_pdf_metadata_value",
]

#: Defaults (spec §四).  All of them are configurable on the importer and on the CLI.
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_PDF_TEXT_CHARS = 2 * 1024 * 1024
MIN_PDF_CHARS = 200

PDF_EXTENSIONS: tuple[str, ...] = (".pdf",)
EXTRACTION_BACKEND = "pdfminer.six"

#: The Info-dictionary fields this importer records (spec §十一).
PDF_METADATA_FIELDS: tuple[str, ...] = ("Title", "Author", "Creator", "Producer", "Subject", "Keywords")

_PDF_HEADER = b"%PDF-"
#: Placeholder titles some producers write into /Title.  Taking them as the document title
#: would be worse than falling through to the first page line or the file name.
_PLACEHOLDER_TITLES = frozenset(
    {
        "untitled",
        "untitled document",
        "untitled1",
        "document",
        "document1",
        "no title",
        "unknown",
        "none",
        "无标题",
        "未命名",
        "未命名文档",
    }
)


def is_usable_pdf_title(value: str | None) -> bool:
    """True when a /Title value is worth using as the document title."""
    if not value:
        return False
    candidate = " ".join(str(value).split()).strip()
    return bool(candidate) and candidate.lower() not in _PLACEHOLDER_TITLES
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff"
_SPACE_BETWEEN_CJK = re.compile(f"(?<=[{_CJK}])[ \\t]+(?=[{_CJK}])")
#: ``pdfminer`` writes "(cid:10)" (etc.) for glyphs without a ToUnicode mapping.  Control
#: CIDs carry no text (typically line/paragraph separators); dropping them keeps the title
#: and body clean instead of leaking markers like "Heading(cid:10)" into the import.
_CONTROL_CID = re.compile(r"\(cid:(?:[0-9]|[12][0-9]|3[01])\)")
_MULTI_SPACE = re.compile(r"[ \t\f\v]{2,}")
_MULTI_BLANK = re.compile(r"\n{3,}")


# --------------------------------------------------------------------------
# errors (typed, mapped by CLI and Web UI, never a raw traceback)
# --------------------------------------------------------------------------


class PdfError(MemorySystemError):
    """Base class: a local PDF could not be turned into capture-ready text.

    Raised before anything is persisted, so a failed PDF import writes no Memory and no
    Source.
    """

    def __init__(self, message: str, *, path: str | Path | None = None, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.path = str(path) if path is not None else None
        self.detail = detail


class PdfBackendUnavailableError(PdfError):
    """``pdfminer.six`` is not installed in this interpreter."""


class PdfTooLargeError(PdfError):
    """The file exceeds ``max_bytes`` (checked before reading, and again after)."""

    def __init__(self, path: str | Path, *, size_bytes: int, max_bytes: int) -> None:
        super().__init__(
            f"pdf too large: {size_bytes} bytes > limit {max_bytes} bytes", path=path
        )
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes


class PdfTooManyPagesError(PdfError):
    """More than ``max_pages`` pages (detected while iterating, not after parsing)."""

    def __init__(self, path: str | Path, *, page_count: int, max_pages: int) -> None:
        super().__init__(f"pdf has more than {max_pages} pages (stopped at page {page_count})", path=path)
        self.page_count = page_count
        self.max_pages = max_pages


class PdfEncryptedError(PdfError):
    """Password-protected, or text extraction is not permitted.  Never bypassed."""

    def __init__(self, path: str | Path, *, reason: str = "encrypted or password protected") -> None:
        super().__init__(
            f"pdf is protected (password or encryption) and cannot be imported: {reason}", path=path, detail=reason
        )
        self.reason = reason


class PdfCorruptError(PdfError):
    """Not a PDF, bad header, missing root object, truncated file, syntax error."""

    def __init__(self, path: str | Path, *, detail: str | None = None) -> None:
        super().__init__(f"pdf could not be parsed: {detail or 'invalid or truncated pdf'}", path=path, detail=detail)


class PdfEmptyTextError(PdfError):
    """Opened fine, but there is not enough extractable text (likely a scan)."""

    def __init__(self, path: str | Path, *, text_chars: int, min_chars: int, page_count: int = 0) -> None:
        super().__init__(
            "no usable text extracted from the pdf: "
            f"{text_chars} characters < minimum {min_chars} (pages={page_count}); "
            "it may be a scanned or image-only pdf and need OCR",
            path=path,
        )
        self.text_chars = text_chars
        self.min_chars = min_chars
        self.page_count = page_count


class PdfTextTooLargeError(PdfError):
    """The extracted text exceeds ``max_text_chars`` (detected per page)."""

    def __init__(self, path: str | Path, *, text_chars: int, max_text_chars: int) -> None:
        super().__init__(
            f"pdf text too large: {text_chars} characters > limit {max_text_chars}", path=path
        )
        self.text_chars = text_chars
        self.max_text_chars = max_text_chars


# --------------------------------------------------------------------------
# text normalisation helpers (unit-testable)
# --------------------------------------------------------------------------


def normalise_page_text(text: str) -> str:
    """Light cleanup of one page's extracted text (spec §八).

    Keeps paragraphs, Chinese and English, folds runs of spaces and blank lines, and
    removes the single spaces ``pdfminer`` inserts between CJK glyphs when the page uses
    letter/position-based spacing (a common source of "fragmented" Chinese output).
    No summarising, no re-ordering, no dropping of content.
    """
    if not isinstance(text, str):
        raise ValidationError(f"page text must be a string, got {type(text).__name__}", field="text")
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    cleaned = _CONTROL_CID.sub(" ", cleaned)
    cleaned = _SPACE_BETWEEN_CJK.sub("", cleaned)
    lines: list[str] = []
    for line in cleaned.split("\n"):
        collapsed = _MULTI_SPACE.sub(" ", line).strip()
        if collapsed:
            lines.append(collapsed)
        elif lines and lines[-1] != "":
            lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines).strip()


def decode_pdf_metadata_value(value: Any) -> str | None:
    """Decode one Info-dictionary value into a JSON-serialisable string.

    ``pdfminer.six`` returns ``bytes`` for metadata; PDF text strings may be UTF-16BE with
    a BOM or PDFDocEncoding/latin-1.  ``None``/empty values become ``None`` so the caller
    can drop them.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if raw.startswith((codecs.BOM_UTF16_BE, codecs.BOM_UTF16_LE)):
            try:
                # a double BOM (or a BOM kept as a character) is stripped as well
                return raw.decode("utf-16").strip().lstrip("\ufeff").strip() or None
            except UnicodeDecodeError:
                pass
        for encoding in ("utf-8", "latin-1"):
            try:
                decoded = raw.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
            if decoded:
                return decoded
        return raw.decode("utf-8", errors="replace").strip() or None
    text = str(value).strip()
    return text or None


# --------------------------------------------------------------------------
# document + importer
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfDocument:
    """One imported PDF: identity, extracted text, page count and light provenance."""

    source_path: str
    title: str | None
    content: str
    page_count: int
    metadata: Mapping[str, Any] = field(default_factory=dict)
    title_source: str = "none"
    pdf_sha256: str = ""
    extraction_backend: str = EXTRACTION_BACKEND

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ValidationError("content must be a string", field="content")
        if isinstance(self.page_count, bool) or not isinstance(self.page_count, int) or self.page_count < 0:
            raise ValidationError(f"page_count must be an integer >= 0, got {self.page_count!r}", field="page_count")
        if self.title is not None and (not isinstance(self.title, str) or not self.title.strip()):
            raise ValidationError("title must be None or a non-empty string", field="title")

    @property
    def filename(self) -> str:
        """Basename only -- local absolute paths never reach the Source metadata."""
        return Path(self.source_path).name

    @property
    def content_chars(self) -> int:
        return len(self.content)

    @property
    def pages_with_text(self) -> int:
        return int(self.metadata.get("pdf_pages_with_text", 0) or 0)

    def preview(self, limit: int = 160) -> str:
        line = " ".join(self.content.split())
        return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"

    def as_dict(self, *, include_content: bool = False, include_preview: bool = False) -> dict[str, Any]:
        """Redacted by default: identity/counts/metadata, never the PDF text."""
        payload: dict[str, Any] = {
            "filename": self.filename,
            "title": self.title,
            "title_source": self.title_source,
            "page_count": self.page_count,
            "content_chars": self.content_chars,
            "pdf_sha256": self.pdf_sha256,
            "extraction_backend": self.extraction_backend,
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
        source_type: SourceType | str = SourceType.FILE,
        title: str | None = None,
        captured_at: str | None = None,
    ) -> CaptureRequest:
        """Build the Phase 1 request: extracted text + PDF provenance (no local path)."""
        metadata: dict[str, Any] = {
            "captured_from": "pdf",
            "filename": self.filename,
            "pdf_page_count": self.page_count,
            "pdf_pages_with_text": self.pages_with_text,
            "pdf_sha256": self.pdf_sha256,
            "content_chars": self.content_chars,
            "extraction_backend": self.extraction_backend,
            "title_source": self.title_source,
        }
        for field_name in PDF_METADATA_FIELDS:
            value = self.metadata.get(f"pdf_{field_name.lower()}")
            if value is not None:
                metadata[f"pdf_{field_name.lower()}"] = value
        for key, value in self.metadata.items():
            if key.startswith("pdf_") and key not in metadata:
                metadata.setdefault(key, value)
        return CaptureRequest(
            content=self.content,
            title=title or self.title,
            source_type=source_type,
            url=None,
            metadata=metadata,
            captured_at=captured_at or utcnow_iso(),
        )


class PdfImporter:
    """Reads a local text PDF page by page with hard limits; never persists anything."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_PDF_BYTES,
        max_pages: int = MAX_PDF_PAGES,
        max_text_chars: int = MAX_PDF_TEXT_CHARS,
        min_chars: int = MIN_PDF_CHARS,
        extensions: Sequence[str] = PDF_EXTENSIONS,
        page_markers: bool = True,
    ) -> None:
        for name, value in (
            ("max_bytes", max_bytes),
            ("max_pages", max_pages),
            ("max_text_chars", max_text_chars),
            ("min_chars", min_chars),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValidationError(f"{name} must be an integer >= 1, got {value!r}", field=name)
        if not extensions:
            raise ValidationError("extensions must not be empty", field="extensions")
        self.max_bytes = int(max_bytes)
        self.max_pages = int(max_pages)
        self.max_text_chars = int(max_text_chars)
        self.min_chars = int(min_chars)
        self.extensions = tuple(extensions)
        self.page_markers = bool(page_markers)

    # -- backend ----------------------------------------------------------
    @staticmethod
    def _backend() -> tuple[Any, ...]:
        """Import pdfminer.six lazily so a missing backend is a typed error."""
        try:
            from pdfminer.converter import TextConverter
            from pdfminer.layout import LAParams
            from pdfminer.pdfdocument import PDFDocument
            from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
            from pdfminer.pdfpage import PDFPage
            from pdfminer.pdfparser import PDFParser
        except ImportError as exc:  # pragma: no cover - exercised by a monkeypatch test
            raise PdfBackendUnavailableError(
                "pdfminer.six is required for PDF import; install it with "
                "`python -m pip install pdfminer.six`",
                detail=str(exc),
            ) from exc
        return TextConverter, LAParams, PDFDocument, PDFPageInterpreter, PDFResourceManager, PDFPage, PDFParser

    # -- file -> document -------------------------------------------------
    def load(self, path: str | Path, *, title: str | None = None) -> PdfDocument:
        """Read one PDF with all limits enforced before/while parsing."""
        TextConverter, LAParams, PDFDocument, PDFPageInterpreter, PDFResourceManager, PDFPage, PDFParser = self._backend()
        from pdfminer.pdfdocument import PDFEncryptionError
        from pdfminer.psexceptions import PSException

        file_path = Path(path)
        if not file_path.exists():
            raise FileMissingError(file_path)
        if not file_path.is_file():
            raise NotAFileError(file_path)
        extension = file_path.suffix.lower()
        if extension not in self.extensions:
            raise UnsupportedFileTypeError(file_path, extension=extension, supported=self.extensions)

        # (1) size gate before reading anything
        try:
            size_bytes = file_path.stat().st_size
        except OSError as exc:
            raise FileReadError(file_path, exc) from exc
        if size_bytes > self.max_bytes:
            raise PdfTooLargeError(file_path, size_bytes=size_bytes, max_bytes=self.max_bytes)

        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            raise FileReadError(file_path, exc) from exc
        # (2) defensive re-check: the path could have grown between stat and read
        if len(raw) > self.max_bytes:
            raise PdfTooLargeError(file_path, size_bytes=len(raw), max_bytes=self.max_bytes)
        if not raw.lstrip()[: len(_PDF_HEADER)] == _PDF_HEADER:
            raise PdfCorruptError(file_path, detail="missing %PDF- header (not a PDF file)")

        pdf_sha256 = hashlib.sha256(raw).hexdigest()
        resource_manager = PDFResourceManager()
        laparams = LAParams()
        pages: list[tuple[int, str]] = []
        pages_with_text = 0
        total_chars = 0
        page_count = 0
        info: Mapping[str, Any] = {}

        try:
            with io.BytesIO(raw) as buffer:
                try:
                    document = PDFDocument(PDFParser(buffer), password="")
                except PDFEncryptionError as exc:
                    raise PdfEncryptedError(file_path, reason=type(exc).__name__) from exc
                info = document.info[0] if document.info else {}
                # (3) lazily pull pages: the page/char limits are enforced inside the loop.
                #     `check_extractable=True` is what turns "permissions forbid extraction"
                #     into PDFTextExtractionNotAllowed (verified against a fixture) -- in this
                #     pdfminer version `document.encryption` is only a tuple of objects and
                #     exposes no `is_extractable`, so the paginator is the authoritative check.
                #     Asking for one page beyond the limit is how "too many pages" is detected.
                for page in PDFPage.get_pages(
                    io.BytesIO(raw),
                    maxpages=self.max_pages + 1,
                    password="",
                    check_extractable=True,
                ):
                    page_count += 1
                    if page_count > self.max_pages:
                        raise PdfTooManyPagesError(file_path, page_count=page_count, max_pages=self.max_pages)
                    text = self._page_text(
                        TextConverter, PDFPageInterpreter, resource_manager, laparams, page, page_count
                    )
                    if text.strip():
                        pages_with_text += 1
                    total_chars += len(text)
                    if total_chars > self.max_text_chars:
                        raise PdfTextTooLargeError(
                            file_path, text_chars=total_chars, max_text_chars=self.max_text_chars
                        )
                    pages.append((page_count, normalise_page_text(text)))
        except PdfError:
            raise
        except PDFEncryptionError as exc:
            raise PdfEncryptedError(file_path, reason=type(exc).__name__) from exc
        except PSException as exc:
            # pdfminer's parser/stream errors (PSEOF, PDFSyntaxError, ...) mean corruption
            raise PdfCorruptError(file_path, detail=f"{type(exc).__name__}: {str(exc)[:120]}") from exc
        except Exception as exc:  # defensive: malformed structures raise all sorts of errors
            raise PdfCorruptError(file_path, detail=f"{type(exc).__name__}: {str(exc)[:120]}") from exc

        content_parts = [f"[Page {number}]\n{text}" for number, text in pages if text] if self.page_markers else [
            text for _, text in pages if text
        ]
        content = normalize_content("\n\n".join(content_parts))
        if len(content) < self.min_chars:
            raise PdfEmptyTextError(
                file_path, text_chars=len(content), min_chars=self.min_chars, page_count=page_count
            )

        decoded_metadata = self._decode_info(info)
        decoded_metadata["pdf_page_count"] = page_count
        decoded_metadata["pdf_pages_with_text"] = pages_with_text
        decoded_metadata["pdf_sha256"] = pdf_sha256
        resolved_title, title_source = self._resolve_title(title, decoded_metadata, pages)
        return PdfDocument(
            source_path=str(file_path),
            title=resolved_title,
            content=content,
            page_count=page_count,
            metadata=decoded_metadata,
            title_source=title_source,
            pdf_sha256=pdf_sha256,
        )

    @staticmethod
    def _page_text(
        TextConverter: Any,
        PDFPageInterpreter: Any,
        resource_manager: Any,
        laparams: Any,
        page: Any,
        page_number: int,
    ) -> str:
        """Extract exactly one page (fresh device per page keeps memory bounded)."""
        buffer = io.BytesIO()
        device = TextConverter(resource_manager, buffer, codec="utf-8", laparams=laparams, pageno=page_number)
        try:
            PDFPageInterpreter(resource_manager, device).process_page(page)
        finally:
            device.close()
        return buffer.getvalue().decode("utf-8", errors="replace")

    @staticmethod
    def _decode_info(info: Mapping[str, Any]) -> dict[str, Any]:
        decoded: dict[str, Any] = {}
        for field_name in PDF_METADATA_FIELDS:
            value = decode_pdf_metadata_value(info.get(field_name))
            if value:
                decoded[f"pdf_{field_name.lower()}"] = value
        return decoded

    @staticmethod
    def _resolve_title(
        explicit: str | None, metadata: Mapping[str, Any], pages: Sequence[tuple[int, str]]
    ) -> tuple[str | None, str]:
        """Title priority: explicit --title > PDF /Title > first usable page line > none."""
        if explicit is not None:
            candidate = explicit.strip()
            if not candidate:
                raise ValidationError("title must not be blank", field="title")
            return candidate, "explicit"
        pdf_title = metadata.get("pdf_title")
        if is_usable_pdf_title(pdf_title):
            return str(pdf_title), "pdf_metadata"
        for _, text in pages:
            for line in text.splitlines():
                candidate = line.strip()
                if 4 <= len(candidate) <= 120:
                    return candidate, "first_page_line"
                if candidate:
                    break
            break
        return None, "none"

    # -- Capture handoff --------------------------------------------------
    def import_document(
        self,
        document: PdfDocument,
        capture: CaptureService,
        *,
        source_type: SourceType | str = SourceType.FILE,
        title: str | None = None,
        dry_run: bool = False,
    ) -> "PdfImportResult":
        """Hand a document to Phase 1's Capture (the only persistence path)."""
        if not isinstance(document, PdfDocument):
            raise ValidationError(f"expected a PdfDocument, got {type(document).__name__}", field="document")
        if not isinstance(capture, CaptureService):
            raise ValidationError(
                f"PdfImporter needs a CaptureService, got {type(capture).__name__}", field="capture"
            )
        result = capture.capture_request(
            document.to_capture_request(source_type=source_type, title=title), dry_run=dry_run
        )
        return PdfImportResult(
            document=document, capture_result=result, import_status="preview" if dry_run else "imported"
        )

    def import_file(
        self,
        path: str | Path,
        capture: CaptureService,
        *,
        title: str | None = None,
        source_type: SourceType | str = SourceType.FILE,
        dry_run: bool = False,
    ) -> "PdfImportResult":
        """``load(path)`` + ``import_document(...)`` in one call."""
        document = self.load(path, title=title)
        return self.import_document(document, capture, source_type=source_type, dry_run=dry_run)


@dataclass(frozen=True)
class PdfImportResult:
    """What the PDF importer can report (counts/identity -- not the PDF text)."""

    document: PdfDocument
    capture_result: CaptureResult
    import_status: str = "imported"

    @property
    def title(self) -> str | None:
        return self.document.title

    @property
    def page_count(self) -> int:
        return self.document.page_count

    @property
    def content_chars(self) -> int:
        return self.document.content_chars

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
        """Redacted view: counts/metadata; the PDF text needs an explicit opt-in."""
        capture_payload = self.capture_result.as_dict()
        formation = self.capture_result.formation_result
        payload: dict[str, Any] = {
            "document": self.document.as_dict(
                include_content=include_content, include_preview=include_preview
            ),
            "filename": self.document.filename,
            "title": self.title,
            "title_source": self.document.title_source,
            "page_count": self.page_count,
            "content_chars": self.content_chars,
            "extraction_backend": self.document.extraction_backend,
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


def import_pdf_file(
    path: str | Path,
    capture: CaptureService,
    *,
    title: str | None = None,
    source_type: SourceType | str = SourceType.FILE,
    dry_run: bool = False,
    max_bytes: int = MAX_PDF_BYTES,
    max_pages: int = MAX_PDF_PAGES,
    min_chars: int = MIN_PDF_CHARS,
) -> PdfImportResult:
    """Module-level convenience: ``PdfImporter(...).import_file(...)``."""
    return PdfImporter(max_bytes=max_bytes, max_pages=max_pages, min_chars=min_chars).import_file(
        path, capture, title=title, source_type=source_type, dry_run=dry_run
    )
