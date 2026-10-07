"""Local Web UI MVP for the Personal Knowledge Base (standard library only).

``python -m personal_memory web --db data/memory.db`` starts a loopback-only HTTP
server that exposes the already-frozen capabilities:

* **Capture**   -> :class:`~personal_memory.capture.CaptureService` (text)
* **Import**    -> :class:`~personal_memory.importers.FileImporter` / ``ChatImporter``
* **Memories**  -> :class:`~personal_memory.store.MemoryRepository` +
  :class:`~personal_memory.lifecycle.MemoryLifecycle`
* **Sources**   -> :class:`~personal_memory.store.MemoryRepository`
* **Search**    -> :class:`~personal_memory.retrieval.MemoryRetriever`

This package is a presentation layer: it renders HTML, parses requests and maps existing
exceptions to readable messages.  It defines no memory model, no retrieval, no lifecycle
and no quality logic of its own, and it never opens SQLite directly.

Typical use::

    from personal_memory.web import WebContext, create_server

    context = WebContext.create("data/memory.db")
    server = create_server(context, host="127.0.0.1", port=0)
    ...
"""

from __future__ import annotations

from .server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_UPLOAD_BYTES,
    KnowledgeBaseHandler,
    KnowledgeBaseServer,
    WebContext,
    classify_error,
    create_server,
    make_handler,
    run_web,
    serve,
)
from .views import Flash

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MAX_UPLOAD_BYTES",
    "WebContext",
    "KnowledgeBaseHandler",
    "KnowledgeBaseServer",
    "Flash",
    "classify_error",
    "create_server",
    "make_handler",
    "serve",
    "run_web",
]
