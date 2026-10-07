"""Chat importer core: chat data -> normalised conversation -> CaptureRequest.

Data flow (nothing else happens here)::

    Chat data (role text or provider-neutral JSON)
      -> ChatAdapter            (importers/chat_adapters.py: format recognition)
      -> ParsedChat             (messages + what the file said about the conversation)
      -> ChatMessage / ChatConversation   (validated, ordered, role-labelled)
      -> CaptureRequest         (source_type=chat, role-marked text, chat metadata)
      -> CaptureService -> MemoryFormationService -> Memory System   (all frozen)

Why this shape
--------------
A chat is not an article: **who said what** is part of the meaning.  So every message
keeps its ``role``, the order is fixed, timestamps survive, and the text handed to
Memory Formation carries a stable role marker (``[USER]`` / ``[ASSISTANT]`` / …) that
Formation can rely on:

* a ``[USER]`` block is the user's own words -- Formation may judge ``user_explicit``;
* an ``[ASSISTANT]`` block is model output -- it must never be silently promoted to
  "the user explicitly said this"; Formation decides ``source_content`` /
  ``agent_inference`` from what it sees (this module never rewrites or reassigns roles);
* a role marker is only emitted for the role the message actually has, and role-like
  text *inside* a message body stays inside that message.

No SQLite, no model call, no value judgment, no summarising, no chunking, no
deduplication of its own: those belong to the frozen layers.  Persistence is reached
only by handing a ``CaptureRequest`` to Phase 1's ``CaptureService``.

Privacy
-------
Chat logs are sensitive.  This module therefore:

* never puts the conversation body into metadata (only id/provider/counts/timestamps);
* offers redacted views by default (``as_dict()`` returns role/timestamp/length plus a
  short preview, and the compact Source view never contains the body);
* keeps full content out of error messages (they carry indices/labels, not the chat);
* makes no network call itself -- but note that if the caller wires a real LLM, the
  conversation text **is sent to that configured model service** during Memory
  Formation.  Local SQLite storage does not mean local inference.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..capture import CaptureRequest, CaptureResult, CaptureService
from ..errors import MemorySystemError, ValidationError
from ..models import SourceType, is_valid_timestamp, normalize_content, utcnow_iso
from .files import MAX_FILE_BYTES, read_text_file

__all__ = [
    "CHAT_FORMATS",
    "CHAT_EXTENSIONS",
    "MAX_CHAT_BYTES",
    "UNTITLED_TITLE",
    "ROLE_MARKERS",
    "ChatRole",
    "ChatMessage",
    "ChatConversation",
    "ParsedChat",
    "ChatImportError",
    "ChatParseError",
    "UnsupportedChatRoleError",
    "EmptyConversationError",
    "coerce_chat_role",
    "parse_role_text",
    "parse_chat_json",
    "render_role_text",
    "short_title_from_first_user_message",
    "resolve_title",
    "ChatImporter",
    "ChatImportResult",
    "import_chat_file",
]

#: File extensions the chat importer accepts.
CHAT_EXTENSIONS: tuple[str, ...] = (".json", ".txt", ".md", ".markdown", ".chat")

#: Same "prompt-sized" limit as the file importer (spec: reuse that mechanism).
MAX_CHAT_BYTES = MAX_FILE_BYTES

#: Explicit format selection; ``auto`` decides from the extension/content.
CHAT_FORMATS: tuple[str, ...] = ("auto", "roles", "json")

#: Used only when nothing else can name the conversation -- no model call is made for it.
UNTITLED_TITLE = "Untitled Conversation"
TITLE_MAX_CHARS = 60


class ChatRole(StrEnum):
    """Roles a chat message can have."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    DEVELOPER = "developer"


#: Canonical marker per role, used in the text handed to Capture/Formation.
ROLE_MARKERS: dict[str, str] = {str(role): f"[{str(role).upper()}]" for role in ChatRole}

#: Accepted role labels (case-insensitive).  Anything else is refused instead of being
#: silently treated as "user" -- mis-attributing a speaker is the one mistake this
#: importer must never make.
ROLE_ALIASES: dict[str, str] = {
    "user": "user",
    "human": "user",
    "me": "user",
    "user:": "user",
    "用户": "user",
    "我": "user",
    "assistant": "assistant",
    "ai": "assistant",
    "bot": "assistant",
    "model": "assistant",
    "gpt": "assistant",
    "助手": "assistant",
    "系统助手": "assistant",
    "system": "system",
    "sys": "system",
    "系统": "system",
    "tool": "tool",
    "function": "tool",
    "工具": "tool",
    "developer": "developer",
    "dev": "developer",
    "开发者": "developer",
}


def coerce_chat_role(
    value: Any, *, field_name: str = "role", source: str | None = None
) -> ChatRole:
    """Coerce a role label to :class:`ChatRole`; raise on anything unknown.

    Unknown labels are refused instead of being defaulted to ``user``: silently
    attributing someone else's words to the user is the one mistake this importer must
    never make.
    """
    if isinstance(value, ChatRole):
        return value
    if isinstance(value, str):
        key = value.strip().strip("[]").strip().lower()
        if key in ROLE_ALIASES:
            return ChatRole(ROLE_ALIASES[key])
        try:
            return ChatRole(key)
        except ValueError:
            pass
    allowed = sorted({str(role) for role in ChatRole} | set(ROLE_ALIASES))
    raise UnsupportedChatRoleError(value, allowed=allowed, field_name=field_name, source=source)


class ChatImportError(MemorySystemError):
    """Base class: chat data could not be turned into capture-ready text.

    Raised before anything is persisted, so a failed chat import creates no Memory and
    no long-term Source.  Messages never embed the conversation body.
    """

    def __init__(self, message: str, *, source: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.source = source


class ChatParseError(ChatImportError):
    """The input is not a chat this importer can read (or is malformed)."""


class UnsupportedChatRoleError(ChatParseError, ValidationError):
    """A role label that is not one of the supported roles.

    Both a :class:`ChatParseError` (a file said something unreadable) and a
    :class:`ValidationError` (a directly constructed ``ChatMessage`` has a bad field),
    so callers can catch whichever fits.
    """

    def __init__(
        self,
        role: Any,
        *,
        allowed: Sequence[str],
        field_name: str = "role",
        source: str | None = None,
    ) -> None:
        super().__init__(
            f"unsupported chat role {role!r} for {field_name}; supported roles/labels: {sorted(allowed)}",
            source=source,
        )
        self.role = role
        self.allowed = tuple(allowed)
        self.field_name = field_name


class EmptyConversationError(ChatImportError):
    """The conversation has no usable message (empty, or blank messages only)."""


# --------------------------------------------------------------------------
# normalised structures
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatMessage:
    """One chat message: who said it, what they said, and when (if known).

    ``content`` is normalised (LF line endings, NFC, trimmed noise) but otherwise kept
    verbatim -- no rewriting, no summarising, no role reassignment.
    """

    role: ChatRole | str
    content: str
    timestamp: str | None = None

    def __post_init__(self) -> None:
        role = coerce_chat_role(self.role)
        if not isinstance(self.content, str):
            raise ValidationError(
                f"message content must be a string, got {type(self.content).__name__}", field="content"
            )
        if not self.content.strip():
            raise ValidationError("message content must not be empty or whitespace only", field="content")
        timestamp = self.timestamp
        if timestamp is not None:
            if not is_valid_timestamp(timestamp):
                raise ValidationError(
                    f"message timestamp must be an ISO-8601 string or None, got {timestamp!r}",
                    field="timestamp",
                )
            timestamp = str(timestamp).strip()
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "content", normalize_content(self.content))
        object.__setattr__(self, "timestamp", timestamp)

    @property
    def marker(self) -> str:
        """The stable role marker this message contributes to the captured text."""
        return ROLE_MARKERS[str(self.role)]

    def preview(self, limit: int = 60) -> str:
        """A single-line, length-limited view (used by redacted output)."""
        line = " ".join(self.content.split())
        return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"

    def as_dict(
        self, *, include_content: bool = False, include_preview: bool = False
    ) -> dict[str, Any]:
        """Role/timestamp/length by default -- **no characters of the message**.

        ``include_preview=True`` adds a <=60 character single-line preview and
        ``include_content=True`` the full text; both are explicit opt-ins because chat
        logs are sensitive (spec §十九).
        """
        payload: dict[str, Any] = {
            "role": str(self.role),
            "timestamp": self.timestamp,
            "content_chars": len(self.content),
        }
        if include_content:
            payload["content"] = self.content
        elif include_preview:
            payload["content_preview"] = self.preview()
        return payload


@dataclass(frozen=True)
class ChatConversation:
    """One conversation: an ordered list of role-labelled messages plus its identity.

    ``provider`` is ``None`` when the provider is genuinely unknown -- it is never
    guessed.  ``title_source`` records which rule produced ``title``
    (``explicit`` / ``export`` / ``first_user_message`` / ``untitled``).
    """

    conversation_id: str
    messages: tuple[ChatMessage, ...]
    title: str | None = None
    provider: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    title_source: str = "untitled"

    def __post_init__(self) -> None:
        if not isinstance(self.conversation_id, str) or not self.conversation_id.strip():
            raise ValidationError("conversation_id must be a non-empty string", field="conversation_id")
        messages = self.messages
        if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
            raise ValidationError(
                f"messages must be a sequence of ChatMessage, got {type(messages).__name__}", field="messages"
            )
        ordered: list[ChatMessage] = []
        for index, message in enumerate(messages):
            if not isinstance(message, ChatMessage):
                raise ValidationError(
                    f"messages[{index}] must be a ChatMessage, got {type(message).__name__}", field="messages"
                )
            ordered.append(message)
        if not ordered:
            raise ValidationError("messages must contain at least one message", field="messages")
        if self.title is not None and (not isinstance(self.title, str) or not self.title.strip()):
            raise ValidationError("title must be None or a non-empty string", field="title")
        if self.provider is not None and (not isinstance(self.provider, str) or not self.provider.strip()):
            raise ValidationError("provider must be None or a non-empty string", field="provider")
        if self.metadata is None or not isinstance(self.metadata, Mapping):
            raise ValidationError("metadata must be a mapping", field="metadata")
        for name, value in (("started_at", self.started_at), ("ended_at", self.ended_at)):
            if value is not None and not is_valid_timestamp(value):
                raise ValidationError(
                    f"{name} must be an ISO-8601 timestamp string or None, got {value!r}", field=name
                )
        object.__setattr__(self, "messages", tuple(ordered))
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.title is not None:
            object.__setattr__(self, "title", self.title.strip())
        if self.provider is not None:
            object.__setattr__(self, "provider", self.provider.strip())

    # -- views -------------------------------------------------------------
    @property
    def message_count(self) -> int:
        return len(self.messages)

    def roles(self) -> tuple[ChatRole, ...]:
        """Roles in first-appearance order (stable, duplicates removed)."""
        seen: list[ChatRole] = []
        for message in self.messages:
            if message.role not in seen:
                seen.append(message.role)
        return tuple(seen)

    @property
    def has_user_message(self) -> bool:
        return any(str(message.role) == str(ChatRole.USER) for message in self.messages)

    def render_role_text(self) -> str:
        """The stable role-marked text handed to Capture (and then to Formation)."""
        return "\n\n".join(f"{message.marker}\n{message.content}" for message in self.messages)

    def as_dict(
        self, *, include_content: bool = False, include_preview: bool = False
    ) -> dict[str, Any]:
        """Redacted by default: identity, counts and timestamps -- no message text."""
        return {
            "conversation_id": self.conversation_id,
            "title": self.title,
            "title_source": self.title_source,
            "provider": self.provider,
            "message_count": self.message_count,
            "roles": [str(role) for role in self.roles()],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "metadata": dict(self.metadata),
            "messages": [
                message.as_dict(include_content=include_content, include_preview=include_preview)
                for message in self.messages
            ],
        }

    # -- the one adapter to the Capture layer ------------------------------
    def to_capture_request(
        self,
        *,
        source_type: SourceType | str = SourceType.CHAT,
        title: str | None = None,
        provider: str | None = None,
        captured_at: str | None = None,
    ) -> CaptureRequest:
        """Build the Phase 1 request: role-marked text + chat metadata, no body in metadata."""
        metadata: dict[str, Any] = {
            "captured_from": "chat",
            # ``None`` means "unknown provider" -- explicitly not guessed
            "provider": provider if provider is not None else self.provider,
            "conversation_id": self.conversation_id,
            "message_count": self.message_count,
            "roles": [str(role) for role in self.roles()],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }
        return CaptureRequest(
            content=self.render_role_text(),
            title=title or self.title,
            source_type=source_type,
            url=None,
            metadata=metadata,
            captured_at=captured_at or utcnow_iso(),
        )


@dataclass(frozen=True)
class ParsedChat:
    """What a parser produced (not yet a validated conversation)."""

    messages: tuple[ChatMessage, ...]
    conversation_id: str | None = None
    title: str | None = None
    provider: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

#: A role header is a WHOLE line: ``[USER]`` / ``[Assistant]:`` / ``## User``.
#: A bracket anywhere else in a line is ordinary content (citations, code, links).
_BRACKET_HEADER_RE = re.compile(r"^\s{0,3}\[(?P<label>[^\[\]]{1,32})\]\s*:?\s*$")
_MD_HEADER_RE = re.compile(r"^\s{0,3}(?P<hashes>#{1,6})\s+(?P<label>[^\n]{1,32}?)\s*:?\s*$")
_JSON_OBJECT_START = "{"


def _match_role_header(line: str) -> str | None:
    """Return the raw role label when ``line`` is a role header, else ``None``."""
    bracket = _BRACKET_HEADER_RE.match(line)
    if bracket:
        return bracket.group("label")
    heading = _MD_HEADER_RE.match(line)
    if heading:
        label = heading.group("label").strip()
        try:
            coerce_chat_role(label)
        except UnsupportedChatRoleError:
            return None  # an ordinary Markdown heading inside the chat body
        return label
    return None


def parse_role_text(text: str, *, source: str = "") -> ParsedChat:
    """Parse ``[USER]`` / ``## Assistant`` role-marked text (priority-1 format).

    Rules
    -----
    * a role header is a whole line: ``[Label]`` (optional trailing ``:``) or a Markdown
      heading whose text is a known role label;
    * a bracket/label that is not a known role and stands alone on its line is an
      **error** -- assigning it to the previous speaker would mis-attribute content;
    * before the first header only a single-line title (or nothing) is allowed;
    * messages keep their order; blank messages are dropped;
    * the first message's role header is required (no implicit ``user``).
    """
    if not isinstance(text, str):
        raise ValidationError(f"chat text must be a string, got {type(text).__name__}", field="text")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    headers: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        label = _match_role_header(line)
        if label is not None:
            role = coerce_chat_role(label, source=source or None)  # raises for unknown labels
            headers.append((index, str(role)))

    if not headers:
        raise ChatParseError(
            "no role header found; start each message with [USER] / [ASSISTANT] "
            "(or '## User' / '## Assistant')",
            source=source or None,
        )

    first_index = headers[0][0]
    preamble = [line for line in lines[:first_index] if line.strip()]
    title = None
    if preamble:
        if len(preamble) > 1:
            raise ChatParseError(
                "text before the first role header is ambiguous; keep the file's leading block to "
                "one title line or start directly with [USER]",
                source=source or None,
            )
        title = preamble[0].lstrip("#").strip() or None

    messages: list[ChatMessage] = []
    for position, (line_index, role) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        body = "\n".join(lines[line_index + 1 : end]).strip()
        if not body:
            continue  # blank message: dropped, exactly as the spec allows
        messages.append(ChatMessage(role=role, content=body))

    return ParsedChat(messages=tuple(messages), title=title)


def parse_chat_json(text: str | Mapping[str, Any], *, source: str = "") -> ParsedChat:
    """Parse the documented provider-neutral JSON object (priority-2 format).

    Schema::

        {"conversation_id": "...", "title": "...", "provider": "...",
         "started_at": "...", "ended_at": "...", "metadata": {...},
         "messages": [{"role": "user", "content": "...", "timestamp": "..."}]}

    Unknown top-level keys are ignored (the format is explicitly extensible); a message
    object must carry ``role`` and ``content``.  Provider-specific shapes need an
    adapter -- this parser does not guess them.
    """
    if isinstance(text, (str, bytes)):
        raw = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text
        try:
            payload: Any = json.loads(raw)
        except ValueError as exc:
            raise ChatParseError(f"invalid JSON chat file: {exc}", source=source or None) from exc
    else:
        payload = text

    if not isinstance(payload, Mapping):
        raise ChatParseError(
            f"chat JSON must be an object, got {type(payload).__name__}", source=source or None
        )

    raw_messages = payload.get("messages")
    if raw_messages is None:
        raise ChatParseError("chat JSON is missing the 'messages' array", source=source or None)
    if isinstance(raw_messages, (str, bytes)) or not isinstance(raw_messages, list):
        raise ChatParseError(
            f"'messages' must be an array, got {type(raw_messages).__name__}", source=source or None
        )

    messages: list[ChatMessage] = []
    for index, item in enumerate(raw_messages):
        label = f"messages[{index}]"
        if not isinstance(item, Mapping):
            raise ChatParseError(f"{label} must be an object, got {type(item).__name__}", source=source or None)
        if item.get("role") is None or (
            isinstance(item.get("role"), str) and not item["role"].strip()
        ):
            raise ChatParseError(f"{label}.role is required", source=source or None)
        role = coerce_chat_role(item["role"], field_name=f"{label}.role", source=source or None)
        if "content" not in item or item.get("content") is None:
            raise ChatParseError(f"{label}.content is required", source=source or None)
        content = item.get("content")
        if not isinstance(content, str):
            raise ChatParseError(
                f"{label}.content must be a string, got {type(content).__name__} "
                "(provider-specific block content needs an adapter)",
                source=source or None,
            )
        timestamp = item.get("timestamp")
        if timestamp is not None and not is_valid_timestamp(timestamp):
            raise ChatParseError(
                f"{label}.timestamp must be an ISO-8601 string, got {timestamp!r}", source=source or None
            )
        if not content.strip():
            continue  # blank message: dropped
        messages.append(ChatMessage(role=role, content=content, timestamp=timestamp))

    def _optional_text(key: str) -> str | None:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    metadata = payload.get("metadata")
    parsed = ParsedChat(
        messages=tuple(messages),
        conversation_id=_optional_text("conversation_id"),
        title=_optional_text("title"),
        provider=_optional_text("provider"),
        started_at=_optional_text("started_at"),
        ended_at=_optional_text("ended_at"),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )
    return parsed


def render_role_text(conversation: ChatConversation) -> str:
    """Convenience wrapper around :meth:`ChatConversation.render_role_text`."""
    return conversation.render_role_text()


def short_title_from_first_user_message(
    messages: Sequence[ChatMessage], *, limit: int = TITLE_MAX_CHARS
) -> str | None:
    """First line of the first user message, shortened -- no model call."""
    for message in messages:
        if str(message.role) != str(ChatRole.USER):
            continue
        for line in message.content.splitlines():
            collapsed = " ".join(line.split())
            if collapsed:
                return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"
    return None


def resolve_title(
    *,
    explicit: str | None,
    export_title: str | None,
    messages: Sequence[ChatMessage],
) -> tuple[str, str]:
    """Title priority (spec §九): explicit > export > first user message > Untitled."""
    if explicit is not None:
        candidate = explicit.strip()
        if not candidate:
            raise ValidationError("title must not be blank", field="title")
        return candidate, "explicit"
    if export_title:
        return export_title.strip(), "export"
    derived = short_title_from_first_user_message(messages)
    if derived:
        return derived, "first_user_message"
    return UNTITLED_TITLE, "untitled"


def _derived_conversation_id(messages: Sequence[ChatMessage]) -> str:
    """Stable id from the rendered text: the same conversation always gets the same id."""
    rendered = "\n\n".join(f"{message.marker}\n{message.content}" for message in messages)
    return f"conv_{hashlib.sha256(rendered.encode('utf-8')).hexdigest()[:16]}"


# --------------------------------------------------------------------------
# importer
# --------------------------------------------------------------------------


class ChatImporter:
    """Reads chat files into a normalised conversation and hands it to Capture.

    It never opens SQLite and never calls a model: persistence is reached only through
    a :class:`~personal_memory.capture.CaptureService`.
    """

    def __init__(
        self,
        *,
        max_bytes: int = MAX_CHAT_BYTES,
        format: str = "auto",
        adapters: Sequence[Any] | None = None,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValidationError(f"max_bytes must be an integer >= 1, got {max_bytes!r}", field="max_bytes")
        if format not in CHAT_FORMATS:
            raise ValidationError(f"format must be one of {list(CHAT_FORMATS)}, got {format!r}", field="format")
        if adapters is not None and not adapters:
            raise ValidationError("adapters must not be empty when given", field="adapters")
        self.max_bytes = int(max_bytes)
        self.format = format
        self.adapters = tuple(adapters) if adapters is not None else None
        # a registered provider adapter brings its own extension, so the file layer must
        # accept exactly what the configured adapters claim (plus the defaults)
        if self.adapters is None:
            self.extensions: tuple[str, ...] = CHAT_EXTENSIONS
        else:
            claimed = [".json", ".txt", ".md", ".markdown", ".chat"]
            for adapter in self.adapters:
                claimed.extend(str(extension) for extension in getattr(adapter, "extensions", ()))
            self.extensions = tuple(dict.fromkeys(claimed))

    # -- file -> conversation ---------------------------------------------
    def load(
        self,
        path: str | os.PathLike[str],
        *,
        title: str | None = None,
        provider: str | None = None,
        conversation_id: str | None = None,
    ) -> ChatConversation:
        """Read one chat file and return the normalised conversation.

        File-level failures use the same typed errors as the file importer (missing,
        directory, unsupported extension, unreadable, undecodable, too large), all before
        anything can be persisted.  A too-long conversation fails explicitly -- it is
        never silently truncated.
        """
        file_path = Path(path)
        extension = file_path.suffix.lower()
        text, _encoding, _size_bytes, _raw = read_text_file(
            file_path, max_bytes=self.max_bytes, extensions=self.extensions
        )

        # imported here (not at module scope) so the core module stays free of the
        # adapter layer, which itself imports this module's structures
        from .chat_adapters import select_adapter

        adapter = select_adapter(text, extension=extension, format=self.format, adapters=self.adapters)
        parsed = adapter.parse(text, source=file_path.name)

        if not parsed.messages:
            raise EmptyConversationError(
                "conversation has no usable message (all messages were blank)", source=file_path.name
            )

        resolved_title, title_source = resolve_title(
            explicit=title, export_title=parsed.title, messages=parsed.messages
        )
        derived_provider = provider if provider is not None else (parsed.provider or adapter.provider)
        started_at = parsed.started_at or _first_timestamp(parsed.messages)
        ended_at = parsed.ended_at or _last_timestamp(parsed.messages)
        return ChatConversation(
            conversation_id=conversation_id or parsed.conversation_id or _derived_conversation_id(parsed.messages),
            messages=parsed.messages,
            title=resolved_title,
            provider=derived_provider,
            started_at=started_at,
            ended_at=ended_at,
            metadata=dict(parsed.metadata),
            title_source=title_source,
        )

    # -- conversation -> Capture ------------------------------------------
    def import_conversation(
        self,
        conversation: ChatConversation,
        capture: CaptureService,
        *,
        source_type: SourceType | str = SourceType.CHAT,
        dry_run: bool = False,
    ) -> "ChatImportResult":
        """Hand a normalised conversation to Phase 1's Capture (the only persistence path)."""
        if not isinstance(conversation, ChatConversation):
            raise ValidationError(
                f"expected a ChatConversation, got {type(conversation).__name__}", field="conversation"
            )
        if not isinstance(capture, CaptureService):
            raise ValidationError(
                f"ChatImporter needs a CaptureService, got {type(capture).__name__}", field="capture"
            )
        result = capture.capture_request(
            conversation.to_capture_request(source_type=source_type), dry_run=dry_run
        )
        return ChatImportResult(
            conversation=conversation,
            capture_result=result,
            import_status="preview" if dry_run else "imported",
        )

    def import_file(
        self,
        path: str | os.PathLike[str],
        capture: CaptureService,
        *,
        title: str | None = None,
        provider: str | None = None,
        conversation_id: str | None = None,
        source_type: SourceType | str = SourceType.CHAT,
        dry_run: bool = False,
    ) -> "ChatImportResult":
        """``load(path)`` + ``import_conversation(...)`` in one call."""
        conversation = self.load(
            path, title=title, provider=provider, conversation_id=conversation_id
        )
        return self.import_conversation(conversation, capture, source_type=source_type, dry_run=dry_run)


def _first_timestamp(messages: Sequence[ChatMessage]) -> str | None:
    return next((message.timestamp for message in messages if message.timestamp), None)


def _last_timestamp(messages: Sequence[ChatMessage]) -> str | None:
    found = [message.timestamp for message in messages if message.timestamp]
    return found[-1] if found else None


@dataclass(frozen=True)
class ChatImportResult:
    """What the chat importer can tell the caller (it reports, it does not judge)."""

    conversation: ChatConversation
    capture_result: CaptureResult
    import_status: str = "imported"

    @property
    def conversation_id(self) -> str:
        return self.conversation.conversation_id

    @property
    def title(self) -> str | None:
        return self.conversation.title

    @property
    def title_source(self) -> str:
        return self.conversation.title_source

    @property
    def provider(self) -> str | None:
        return self.conversation.provider

    @property
    def message_count(self) -> int:
        return self.conversation.message_count

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

    def as_dict(
        self, *, include_content: bool = False, include_preview: bool = False
    ) -> dict[str, Any]:
        """Redacted view: no conversation text by default (spec §十九).

        A <=60 character per-message preview needs ``include_preview=True``, the full
        conversation plus the complete Capture payload needs ``include_content=True``;
        callers that print either are responsible for the privacy consequence.  The
        compact Source view never contains the body in any mode.
        """
        capture_payload = self.capture_result.as_dict()
        formation = self.capture_result.formation_result
        payload: dict[str, Any] = {
            "conversation": self.conversation.as_dict(
                include_content=include_content, include_preview=include_preview
            ),
            "conversation_id": self.conversation_id,
            "title": self.title,
            "title_source": self.conversation.title_source,
            "provider": self.provider,
            "provider_known": self.provider is not None,
            "message_count": self.message_count,
            "started_at": self.conversation.started_at,
            "ended_at": self.conversation.ended_at,
            "import_status": self.import_status,
            "status": capture_payload["status"],
            "formation_status": capture_payload["formation_status"],
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


def import_chat_file(
    path: str | os.PathLike[str],
    capture: CaptureService,
    *,
    title: str | None = None,
    provider: str | None = None,
    conversation_id: str | None = None,
    source_type: SourceType | str = SourceType.CHAT,
    dry_run: bool = False,
    max_bytes: int = MAX_CHAT_BYTES,
    format: str = "auto",
) -> ChatImportResult:
    """Module-level convenience: ``ChatImporter(...).import_file(...)``."""
    return ChatImporter(max_bytes=max_bytes, format=format).import_file(
        path,
        capture,
        title=title,
        provider=provider,
        conversation_id=conversation_id,
        source_type=source_type,
        dry_run=dry_run,
    )
