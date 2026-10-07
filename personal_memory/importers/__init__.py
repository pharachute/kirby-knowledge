"""Knowledge Base 1.0 -- Phase 2: file importers (``.txt`` / ``.md``).

One layer, one job: **turn a file into the unified input structure** that the frozen
Knowledge Base / Memory System pipeline already understands::

    path
      -> FileImporter / ChatImporter   (.txt / .md / chat -> text, roles, metadata)
      -> ImportedDocument / ChatConversation
      -> CaptureRequest          (Phase 1's unified input structure)
      -> CaptureService          (Phase 1)
      -> MemoryFormationService  (Memory System Phase 2, frozen)
      -> Memory System           (SQLite / retrieval / lifecycle, frozen)

Design rules this package follows:

* different formats converge on **one** input structure -- there is no per-format
  Memory logic, no per-format Formation, no second Source model;
* the importer writes **no SQLite** and calls **no model**: it only produces text and
  hands it to Capture (``load()`` is pure file work);
* light, documented cleaning only (see :mod:`personal_memory.importers.markdown`);
  summarising, chunking, classification and importance judgment stay in Formation;
* deduplication is not re-implemented here -- Source ``content_hash`` reuse and Memory
  exact-duplicate detection keep doing that job;
* file metadata records the file *name*, extension, size, a stable content digest and
  the encoding -- never the local absolute path.

Usage::

    from personal_memory import CaptureService, FileImporter, MemoryFormationService, MemoryRepository

    capture = CaptureService(MemoryFormationService(repository, llm_client))
    result = FileImporter().import_file("notes/rag.md", capture)
    print(result.import_status, result.formation_status, result.memory_count)
"""

from __future__ import annotations

from .chat import (
    CHAT_EXTENSIONS,
    CHAT_FORMATS,
    MAX_CHAT_BYTES,
    ROLE_MARKERS,
    UNTITLED_TITLE,
    ChatConversation,
    ChatImportError,
    ChatImporter,
    ChatImportResult,
    ChatMessage,
    ChatParseError,
    ChatRole,
    EmptyConversationError,
    UnsupportedChatRoleError,
    coerce_chat_role,
    import_chat_file,
    parse_chat_json,
    parse_role_text,
    render_role_text,
    resolve_title,
    short_title_from_first_user_message,
)
from .chat_adapters import (
    ADAPTERS,
    GENERIC_JSON_ADAPTER,
    ROLE_TEXT_ADAPTER,
    ChatAdapter,
    GenericJsonAdapter,
    RoleTextAdapter,
    register_adapter,
    select_adapter,
)
from .pdf import (
    EXTRACTION_BACKEND,
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
    MAX_PDF_TEXT_CHARS,
    MIN_PDF_CHARS,
    PDF_EXTENSIONS,
    PdfBackendUnavailableError,
    PdfCorruptError,
    PdfDocument,
    PdfEmptyTextError,
    PdfEncryptedError,
    PdfError,
    PdfImportResult,
    PdfImporter,
    PdfTextTooLargeError,
    PdfTooLargeError,
    PdfTooManyPagesError,
    decode_pdf_metadata_value,
    import_pdf_file,
    is_usable_pdf_title,
    normalise_page_text,
)
from .web import (
    ALLOWED_CONTENT_TYPES,
    MAX_WEB_BYTES,
    PdfUrlContentError,
    BlockedUrlError,
    ContentEncodingError,
    ContentExtractionError,
    EmptyContentError,
    FetchResponse,
    InvalidUrlError,
    RedirectError,
    ResponseTooLargeError,
    UnsupportedContentTypeError,
    UrlFetchError,
    UrllibTransport,
    WebDocument,
    WebImportError,
    WebImporter,
    WebImportResult,
    assert_public_url,
    extract_content,
    import_web_url,
    normalise_url,
)
from .files import (
    MAX_FILE_BYTES,
    MARKDOWN_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TEXT_EXTENSIONS,
    EmptyFileError,
    FileEncodingError,
    FileImportError,
    FileImporter,
    FileMissingError,
    FileReadError,
    FileTooLargeError,
    ImportedDocument,
    ImportResult,
    NotAFileError,
    UnsupportedFileTypeError,
    import_file,
)
from .markdown import MarkdownDocument, clean_markdown, split_front_matter

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "TEXT_EXTENSIONS",
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
    "MarkdownDocument",
    "clean_markdown",
    "split_front_matter",
    # -- chat importer (KB 1.0 Phase 3) --
    "ChatRole",
    "ChatMessage",
    "ChatConversation",
    "ChatImporter",
    "ChatImportResult",
    "ChatImportError",
    "ChatParseError",
    "UnsupportedChatRoleError",
    "EmptyConversationError",
    "coerce_chat_role",
    "parse_role_text",
    "parse_chat_json",
    "render_role_text",
    "resolve_title",
    "short_title_from_first_user_message",
    "import_chat_file",
    "ChatAdapter",
    "RoleTextAdapter",
    "GenericJsonAdapter",
    "ROLE_TEXT_ADAPTER",
    "GENERIC_JSON_ADAPTER",
    "ADAPTERS",
    "select_adapter",
    "register_adapter",
    "CHAT_EXTENSIONS",
    "CHAT_FORMATS",
    "MAX_CHAT_BYTES",
    "ROLE_MARKERS",
    "UNTITLED_TITLE",
    # -- url / web importer (KB 1.0 Phase 4) --
    "WebImporter",
    "WebDocument",
    "WebImportResult",
    "WebImportError",
    "PdfUrlContentError",
    "InvalidUrlError",
    "BlockedUrlError",
    "UrlFetchError",
    "RedirectError",
    "ResponseTooLargeError",
    "UnsupportedContentTypeError",
    "ContentExtractionError",
    "ContentEncodingError",
    "EmptyContentError",
    "FetchResponse",
    "UrllibTransport",
    "import_web_url",
    "normalise_url",
    "assert_public_url",
    "extract_content",
    "MAX_WEB_BYTES",
    "ALLOWED_CONTENT_TYPES",
    # -- pdf importer (KB 1.0 Phase 5) --
    "PdfImporter",
    "PdfDocument",
    "PdfImportResult",
    "PdfError",
    "PdfBackendUnavailableError",
    "PdfTooLargeError",
    "PdfTooManyPagesError",
    "PdfEncryptedError",
    "PdfCorruptError",
    "PdfEmptyTextError",
    "PdfTextTooLargeError",
    "import_pdf_file",
    "is_usable_pdf_title",
    "normalise_page_text",
    "decode_pdf_metadata_value",
    "PDF_EXTENSIONS",
    "MAX_PDF_BYTES",
    "MAX_PDF_PAGES",
    "MAX_PDF_TEXT_CHARS",
    "MIN_PDF_CHARS",
    "EXTRACTION_BACKEND",
]
