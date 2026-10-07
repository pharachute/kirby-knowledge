"""Text file -> capture-ready input (Knowledge Base 1.0, Phase 2).

Data flow this module implements -- and nothing beyond it::

    path
      -> FileImporter.load()          read + validate + decode + extract title/body
      -> ImportedDocument             (pure data: no DB, no model was touched)
      -> to_capture_request()         -> Phase 1 CaptureRequest
      -> CaptureService.capture_request(...)
      -> MemoryFormationService       (value judgment, extraction, policy, atomic write)

Guarantees
----------
* **No SQLite and no model access here.**  ``load()`` is pure file work; the only way
  out is a :class:`~personal_memory.capture.CaptureRequest`.  Persistence happens in
  Memory Formation through Phase 1's Capture, which is why a failed import can never
  leave a half-written Source or Memory behind.
* **No duplicate-detection of its own.**  Importing the same file twice re-uses the
  frozen mechanisms: the Source ``content_hash`` lookup and (when the caller wires the
  Phase 4 gate, as the CLI does) Memory exact-duplicate detection.
* **No absolute path in long-term data.**  Metadata carries the file *name*,
  extension, size, a stable content digest and the encoding -- never the local path
  (which would be machine-specific and a privacy leak).

Supported extensions: ``.txt``, ``.md``, ``.markdown``.
"""

from __future__ import annotations

import codecs
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..capture import CaptureRequest, CaptureResult, CaptureService
from ..errors import MemorySystemError, ValidationError
from ..models import SourceType, normalize_content, utcnow_iso
from .markdown import clean_markdown

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "MARKDOWN_EXTENSIONS",
    "MAX_FILE_BYTES",
    "FileImportError",
    "FileMissingError",
    "NotAFileError",
    "UnsupportedFileTypeError",
    "EmptyFileError",
    "FileTooLargeError",
    "FileEncodingError",
    "FileReadError",
    "ImportedDocument",
    "ImportResult",
    "FileImporter",
    "import_file",
    "decode_utf8",
    "read_text_file",
]

#: Extensions this phase can read.
TEXT_EXTENSIONS: tuple[str, ...] = (".txt",)
MARKDOWN_EXTENSIONS: tuple[str, ...] = (".md", ".markdown")
SUPPORTED_EXTENSIONS: tuple[str, ...] = TEXT_EXTENSIONS + MARKDOWN_EXTENSIONS

#: Default upper bound: this layer feeds a prompt, it is not a data-dump loader.
MAX_FILE_BYTES = 1024 * 1024

# --------------------------------------------------------------------------
# errors: every failure is explicit and typed (spec §九)
# --------------------------------------------------------------------------


class FileImportError(MemorySystemError):
    """The file could not be turned into capture-ready text.

    Raised before anything is written, so a failed import creates **no Memory and no
    long-term Source**.  Subclasses name the exact reason; ``path`` (as given by the
    caller) is kept for the message only -- it never becomes long-term data.
    """

    def __init__(self, message: str, *, path: str | os.PathLike[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.path = str(path) if path is not None else None


class FileMissingError(FileImportError):
    """The path does not exist."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__(f"file not found: {path}", path=path)


class NotAFileError(FileImportError):
    """The path exists but is a directory (or another non-regular file)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__(f"not a regular file (directory or special file): {path}", path=path)


class UnsupportedFileTypeError(FileImportError):
    """The extension has no importer in this phase."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        extension: str,
        supported: Sequence[str] = SUPPORTED_EXTENSIONS,
    ) -> None:
        shown = extension or "(none)"
        super().__init__(
            f"unsupported file type {shown!r} for {path}; this phase imports {list(supported)}",
            path=path,
        )
        self.extension = shown
        self.supported = tuple(supported)


class EmptyFileError(FileImportError):
    """The file has no usable text (empty, or whitespace only)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__(f"file is empty (no content to capture): {path}", path=path)


class FileTooLargeError(FileImportError):
    """The file exceeds the configured limit."""

    def __init__(self, path: str | os.PathLike[str], *, size_bytes: int, max_bytes: int) -> None:
        super().__init__(
            f"file is too large: {size_bytes} bytes > limit {max_bytes} bytes ({path})",
            path=path,
        )
        self.size_bytes = int(size_bytes)
        self.max_bytes = int(max_bytes)


class FileEncodingError(FileImportError):
    """The bytes are not UTF-8 text."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        encoding: str = "utf-8",
        detail: str = "",
    ) -> None:
        hint = "convert the file to UTF-8 and import it again"
        message = f"could not decode {path} as {encoding}: {detail or 'invalid byte sequence'}; {hint}"
        super().__init__(message, path=path)
        self.encoding = encoding
        self.detail = detail


class FileReadError(FileImportError):
    """The file exists but could not be read (permissions, I/O error)."""

    def __init__(self, path: str | os.PathLike[str], cause: BaseException) -> None:
        super().__init__(f"could not read {path}: {cause}", path=path)
        self.cause = cause


# --------------------------------------------------------------------------
# shared file pipeline (used by the file importer AND the chat importer)
# --------------------------------------------------------------------------


def decode_utf8(raw: bytes, path: str | os.PathLike[str]) -> tuple[str, str]:
    """UTF-8 first (BOM aware) -> ``(text, encoding)``; ``FileEncodingError`` otherwise.

    No encoding guessing: mojibake is worse than a clear failure.  UTF-16/UTF-32 BOMs
    are reported explicitly because "convert to UTF-8" is the actionable fix.
    """
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raise FileEncodingError(path, encoding="utf-16", detail="UTF-16 byte-order mark detected")
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise FileEncodingError(path, encoding="utf-32", detail="UTF-32 byte-order mark detected")
    encoding = "utf-8-sig" if raw.startswith(codecs.BOM_UTF8) else "utf-8"
    try:
        return raw.decode(encoding), encoding
    except UnicodeDecodeError as exc:
        raise FileEncodingError(path, encoding="utf-8", detail=str(exc)) from exc


def read_text_file(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    extensions: Sequence[str],
) -> tuple[str, str, int, bytes]:
    """Shared validation pipeline: ``(text, encoding, size_bytes, raw_bytes)``.

    Order: exists -> is a regular file -> extension -> read -> size limit -> UTF-8 decode.
    Every failure is one of the typed :class:`FileImportError` subclasses, and it happens
    before any caller can persist anything.
    """
    file_path = Path(path)
    extension = file_path.suffix.lower()
    if not file_path.exists():
        raise FileMissingError(file_path)
    if not file_path.is_file():
        raise NotAFileError(file_path)
    if extension not in tuple(extensions):
        raise UnsupportedFileTypeError(file_path, extension=extension, supported=extensions)
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise FileReadError(file_path, exc) from exc
    size_bytes = len(raw)
    if size_bytes > max_bytes:
        raise FileTooLargeError(file_path, size_bytes=size_bytes, max_bytes=max_bytes)
    text, encoding = decode_utf8(raw, file_path)
    return text, encoding, size_bytes, raw


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportedDocument:
    """One file turned into capture-ready text.

    Produced by :meth:`FileImporter.load`; holding it means the file was read, decoded
    and parsed -- **no database row and no model call exists yet**.
    """

    filename: str
    extension: str
    title: str
    content: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: ``"explicit"`` | ``"heading"`` | ``"front_matter"`` | ``"filename"``
    title_source: str = "filename"
    size_bytes: int = 0

    def to_capture_request(
        self,
        *,
        source_type: SourceType | str = SourceType.FILE,
        captured_at: str | None = None,
    ) -> CaptureRequest:
        """The one adapter to Phase 1: file metadata + text -> ``CaptureRequest``."""
        return CaptureRequest(
            content=self.content,
            title=self.title,
            source_type=source_type,
            url=None,  # a local path is not a URL, and Phase 1 never fetches anything
            metadata=dict(self.metadata),
            captured_at=captured_at or utcnow_iso(),
        )

    def as_dict(self) -> dict[str, Any]:
        """Display view.  Deliberately carries **no** local path (see module docstring)."""
        return {
            "filename": self.filename,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
            "title": self.title,
            "title_source": self.title_source,
            "content_chars": len(self.content),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ImportResult:
    """What the importer can tell the caller (it reports, it does not judge)."""

    document: ImportedDocument
    capture_result: CaptureResult
    #: ``"imported"`` | ``"preview"`` (dry run)
    import_status: str = "imported"

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

    def as_dict(self) -> dict[str, Any]:
        capture_payload = self.capture_result.as_dict()
        return {
            "file": self.document.as_dict(),
            "import_status": self.import_status,
            "status": capture_payload["status"],
            "formation_status": capture_payload["formation_status"],
            "memory_count": capture_payload["memory_count"],
            "source_count": capture_payload["source_count"],
            "source_reused": capture_payload["source_reused"],
            "memories_created": capture_payload["memories_created"],
            "sources_created": capture_payload["sources_created"],
            "capture_result": capture_payload,
        }


# --------------------------------------------------------------------------
# importer
# --------------------------------------------------------------------------


class FileImporter:
    """Reads ``.txt`` / ``.md`` files into capture-ready text.

    It never opens SQLite and never calls a model: persistence is reached only by
    handing the ``CaptureRequest`` to a :class:`~personal_memory.capture.CaptureService`.
    """

    def __init__(
        self,
        *,
        max_bytes: int = MAX_FILE_BYTES,
        extensions: Sequence[str] = SUPPORTED_EXTENSIONS,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValidationError(f"max_bytes must be an integer >= 1, got {max_bytes!r}", field="max_bytes")
        if not extensions:
            raise ValidationError("extensions must not be empty", field="extensions")
        normalized: list[str] = []
        for extension in extensions:
            if not isinstance(extension, str) or not extension.startswith("."):
                raise ValidationError(
                    f"extensions must be strings starting with '.', got {extension!r}", field="extensions"
                )
            normalized.append(extension.strip().lower())
        self.max_bytes = int(max_bytes)
        self.extensions: tuple[str, ...] = tuple(normalized)

    # -- file -> document --------------------------------------------------
    def load(self, path: str | os.PathLike[str], *, title: str | None = None) -> ImportedDocument:
        """Read one file and return its capture-ready document.

        Raises a :class:`FileImportError` subclass for every failure mode the spec
        lists (missing, directory, unsupported extension, unreadable, undecodable,
        empty, too large) -- all of them before anything can be persisted.
        """
        file_path = Path(path)
        filename = file_path.name
        extension = file_path.suffix.lower()

        # one shared pipeline for every importer: same validation order, same typed errors
        text, encoding, size_bytes, raw = read_text_file(
            file_path, max_bytes=self.max_bytes, extensions=self.extensions
        )
        content, parsed_title, parsed_title_source = self._parse(extension, text)
        if not content.strip():
            raise EmptyFileError(file_path)

        explicit_title = (title or "").strip()
        if explicit_title:
            final_title, title_source = explicit_title, "explicit"
        elif parsed_title:
            final_title, title_source = parsed_title, parsed_title_source
        else:
            final_title, title_source = file_path.stem or filename, "filename"

        metadata: dict[str, Any] = {
            "captured_from": "file",
            "filename": filename,
            "extension": extension,
            "size_bytes": size_bytes,
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "source_format": "markdown" if extension in MARKDOWN_EXTENSIONS else "text",
            "encoding": encoding,
        }
        return ImportedDocument(
            filename=filename,
            extension=extension,
            title=final_title,
            content=content,
            metadata=metadata,
            title_source=title_source,
            size_bytes=size_bytes,
        )

    @staticmethod
    def _parse(extension: str, text: str) -> tuple[str, str | None, str]:
        """``(body, title, title_source)`` -- the only per-format difference."""
        if extension in MARKDOWN_EXTENSIONS:
            document = clean_markdown(text)
            # same Unicode/whitespace normalisation Phase 1 uses for hashing, so the
            # captured text is stable no matter which editor wrote the file
            return normalize_content(document.body), document.title, document.title_source
        return normalize_content(text), None, "none"

    # -- document -> Capture ----------------------------------------------
    def import_document(
        self,
        document: ImportedDocument,
        capture: CaptureService,
        *,
        source_type: SourceType | str = SourceType.FILE,
        dry_run: bool = False,
    ) -> ImportResult:
        """Hand a loaded document to Phase 1's Capture (the only persistence path)."""
        if not isinstance(document, ImportedDocument):
            raise ValidationError(
                f"expected an ImportedDocument, got {type(document).__name__}", field="document"
            )
        if not isinstance(capture, CaptureService):
            raise ValidationError(
                f"FileImporter needs a CaptureService, got {type(capture).__name__}", field="capture"
            )
        result = capture.capture_request(
            document.to_capture_request(source_type=source_type), dry_run=dry_run
        )
        return ImportResult(
            document=document,
            capture_result=result,
            import_status="preview" if dry_run else "imported",
        )

    def import_file(
        self,
        path: str | os.PathLike[str],
        capture: CaptureService,
        *,
        title: str | None = None,
        source_type: SourceType | str = SourceType.FILE,
        dry_run: bool = False,
    ) -> ImportResult:
        """``load(path)`` + ``import_document(...)`` in one call."""
        document = self.load(path, title=title)
        return self.import_document(document, capture, source_type=source_type, dry_run=dry_run)


def import_file(
    path: str | os.PathLike[str],
    capture: CaptureService,
    *,
    title: str | None = None,
    source_type: SourceType | str = SourceType.FILE,
    dry_run: bool = False,
    max_bytes: int = MAX_FILE_BYTES,
) -> ImportResult:
    """Module-level convenience: ``FileImporter(max_bytes=...).import_file(...)``."""
    return FileImporter(max_bytes=max_bytes).import_file(
        path, capture, title=title, source_type=source_type, dry_run=dry_run
    )
