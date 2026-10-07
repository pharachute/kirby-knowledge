"""Provider adapters: the seam between a platform's file and the normalised chat.

::

    Provider Format  ->  Provider Adapter  ->  ChatConversation  ->  CaptureRequest
                                                                  -> Capture -> Formation

Adding support for another platform later means adding **one adapter class** (and
registering it) -- the normalisation layer, Capture, Memory Formation and the Memory
System are untouched.

Two provider-neutral adapters ship with this phase (the two formats the spec requires):

* :class:`RoleTextAdapter` -- ``[USER]`` / ``## Assistant`` role-marked text;
* :class:`GenericJsonAdapter` -- the documented provider-neutral JSON object.

**No platform-specific adapter is included, on purpose.**  The spec forbids guessing a
vendor's current export format, and a check of this workspace found no exported
ChatGPT / DSH / Codex chat sample to model one on (no ``*.json``/``*.jsonl``
conversation export exists under ``D:\\DSH-worlp``).  The machine-local
``~/.dsh/sessions`` and ``~/.codex/sessions`` stores are private *live* state, not
provided samples, so nothing was inferred from them.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from ..errors import ValidationError
from .chat import ParsedChat, parse_chat_json, parse_role_text

__all__ = [
    "ChatAdapter",
    "RoleTextAdapter",
    "GenericJsonAdapter",
    "ROLE_TEXT_ADAPTER",
    "GENERIC_JSON_ADAPTER",
    "ADAPTERS",
    "select_adapter",
    "register_adapter",
]


@runtime_checkable
class ChatAdapter(Protocol):
    """Recognises one input format and parses it into :class:`ParsedChat`."""

    name: str
    provider: str | None
    extensions: tuple[str, ...]

    def matches(self, text: str, *, extension: str) -> bool:  # pragma: no cover - protocol
        ...

    def parse(self, text: str, *, source: str = "") -> ParsedChat:  # pragma: no cover - protocol
        ...


class RoleTextAdapter:
    """``[USER]`` / ``## Assistant`` role-marked text (priority-1 format)."""

    name = "role-text"
    provider: str | None = None
    extensions: tuple[str, ...] = (".txt", ".md", ".markdown", ".chat")

    def matches(self, text: str, *, extension: str) -> bool:
        # the documented fallback: anything that is not JSON is read as role text, and
        # the parser reports a clear error when it finds no role header
        return True

    def parse(self, text: str, *, source: str = "") -> ParsedChat:
        return parse_role_text(text, source=source)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<RoleTextAdapter name={self.name!r}>"


class GenericJsonAdapter:
    """The documented provider-neutral JSON conversation object (priority-2 format)."""

    name = "provider-neutral-json"
    provider: str | None = None
    extensions: tuple[str, ...] = (".json",)

    def matches(self, text: str, *, extension: str) -> bool:
        return text.lstrip().startswith("{")

    def parse(self, text: str, *, source: str = "") -> ParsedChat:
        return parse_chat_json(text, source=source)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<GenericJsonAdapter name={self.name!r}>"


ROLE_TEXT_ADAPTER = RoleTextAdapter()
GENERIC_JSON_ADAPTER = GenericJsonAdapter()

#: Process-wide default registry, most specific first.  ``register_adapter`` prepends.
ADAPTERS: list[ChatAdapter] = [GENERIC_JSON_ADAPTER, ROLE_TEXT_ADAPTER]


def register_adapter(adapter: ChatAdapter, *, first: bool = True) -> None:
    """Add a provider adapter to the process-wide registry (one platform = one adapter)."""
    for attribute in ("name", "extensions", "parse"):
        if not hasattr(adapter, attribute):
            raise ValidationError(
                f"adapter must define {attribute!r} (ChatAdapter protocol)", field="adapter"
            )
    index = 0 if first else len(ADAPTERS)
    ADAPTERS.insert(index, adapter)


def _by_name(adapters: Sequence[ChatAdapter], name: str) -> ChatAdapter | None:
    for adapter in adapters:
        if getattr(adapter, "name", None) == name:
            return adapter
    return None


def select_adapter(
    text: str,
    *,
    extension: str,
    format: str = "auto",
    adapters: Sequence[ChatAdapter] | None = None,
) -> ChatAdapter:
    """Pick the adapter for this text (documented, deterministic rules).

    ``format``:
      * ``"roles"`` -- force the role-text adapter;
      * ``"json"``  -- force the JSON adapter;
      * ``"auto"``  -- ``.json`` files go to the JSON adapter (so a broken JSON file
        reports "invalid JSON" rather than "no role header"), text that starts with ``{``
        is treated as JSON, anything else is role-marked text.
    """
    pool: list[ChatAdapter] = list(adapters) if adapters is not None else list(ADAPTERS)
    if not pool:
        raise ValidationError("no chat adapters configured", field="adapters")
    json_adapter = _by_name(pool, "provider-neutral-json") or GENERIC_JSON_ADAPTER
    role_text_adapter = _by_name(pool, "role-text") or ROLE_TEXT_ADAPTER

    if format == "roles":
        return role_text_adapter
    if format == "json":
        return json_adapter
    if extension.lower() == ".json" or text.lstrip().startswith("{"):
        return json_adapter
    for adapter in pool:  # a registered platform adapter may claim other extensions
        if adapter is json_adapter or adapter is role_text_adapter:
            continue
        if extension.lower() in tuple(getattr(adapter, "extensions", ())) and adapter.matches(
            text, extension=extension
        ):
            return adapter
    return role_text_adapter

