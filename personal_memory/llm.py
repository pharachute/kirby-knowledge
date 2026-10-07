"""LLM adapter -- the only module in this package that talks to a model provider.

Design
------
* :func:`load_config` resolves ``provider`` / ``model`` / ``base_url`` / timeouts
  and the **API key** from, in order: explicit overrides, environment variables,
  a JSON config file, then provider defaults.  No key is ever hardcoded, and the
  adapter never logs or reprs it (:meth:`LLMConfig.safe_summary`).
* Credentials never travel further than the configured endpoint: redirects are
  refused (:class:`NoRedirectHandler`) and provider error bodies are scrubbed by
  :func:`redact_secrets` before they reach an exception message.
* :class:`Transport` isolates the wire protocol from the client.  The default
  :class:`HttpTransport` uses only the standard library (``urllib``) against an
  OpenAI-compatible ``POST {base_url}/chat/completions``; tests inject a fake
  transport so the whole suite runs offline.
* :class:`LLMClient` owns retry/back-off for *transport* failures (timeouts,
  429, 5xx) and maps provider errors onto :class:`LLMError` subclasses.

Nothing above this module may call a provider directly: ``models.py`` and
``store.py`` remain model-free (Phase 2 requirement).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from .errors import MemorySystemError

__all__ = [
    "ChatMessage",
    "LLMConfig",
    "LLMClient",
    "LLMError",
    "LLMConfigError",
    "LLMRequestError",
    "LLMResponseError",
    "TruncatedResponseError",
    "LLMRequest",
    "LLMResponse",
    "HttpTransport",
    "PROVIDER_DEFAULTS",
    "DEFAULT_API_KEY_ENV",
    "ENV_CONFIG_PATH",
    "extract_json_object",
    "load_config",
    "redact_secrets",
    "resolve_api_key",
]

DEFAULT_API_KEY_ENV = "PERSONAL_MEMORY_LLM_API_KEY"
ENV_CONFIG_PATH = "PERSONAL_MEMORY_LLM_CONFIG"
DEFAULT_CONFIG_PATH = Path("config") / "llm.json"

#: Provider presets.  ``custom`` has no default and must be given a ``base_url``.
PROVIDER_DEFAULTS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
    "custom": "",
}

#: Conventional key variables tried when the primary one is unset.
PROVIDER_KEY_ENVS: dict[str, tuple[str, ...]] = {
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "custom": (),
}

_ENV_FIELDS: dict[str, str] = {
    "PERSONAL_MEMORY_LLM_PROVIDER": "provider",
    "PERSONAL_MEMORY_LLM_MODEL": "model",
    "PERSONAL_MEMORY_LLM_BASE_URL": "base_url",
    "PERSONAL_MEMORY_LLM_TIMEOUT": "timeout_seconds",
    "PERSONAL_MEMORY_LLM_MAX_TOKENS": "max_tokens",
    "PERSONAL_MEMORY_LLM_TEMPERATURE": "temperature",
    "PERSONAL_MEMORY_LLM_JSON_MODE": "json_mode",
    "PERSONAL_MEMORY_LLM_RETRIES": "max_transport_retries",
}

#: Config keys accepted from the JSON config file.
CONFIG_FILE_FIELDS = frozenset(
    {
        "provider",
        "model",
        "base_url",
        "api_key",
        "api_key_env",
        "timeout_seconds",
        "max_tokens",
        "temperature",
        "json_mode",
        "max_transport_retries",
        "backoff_seconds",
        "extra_headers",
        "extra_body",
    }
)


class LLMError(MemorySystemError):
    """Base class for every error raised by the LLM adapter."""


class LLMConfigError(LLMError):
    """Configuration is missing or invalid (no key, unknown provider, bad file)."""


class LLMRequestError(LLMError):
    """The provider could not be reached or refused the request."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class LLMResponseError(LLMError):
    """The provider answered, but the answer is not the structured object we require."""


class TruncatedResponseError(LLMResponseError):
    """The provider stopped early (``finish_reason=length``).

    Raised instead of returning half an answer: a truncated JSON object must
    never be guessed at, and a half-written Memory must never reach SQLite.
    """


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LLMConfig:
    provider: str = "deepseek"
    model: str = "deepseek-flash"
    base_url: str = PROVIDER_DEFAULTS["deepseek"]
    api_key: str = ""
    api_key_env: str = DEFAULT_API_KEY_ENV
    timeout_seconds: float = 120.0
    max_tokens: int = 2048
    temperature: float = 0.0
    json_mode: bool = True
    max_transport_retries: int = 2
    backoff_seconds: float = 1.0
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    extra_body: Mapping[str, Any] = field(default_factory=dict)

    # -- redaction ---------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - trivial, asserted in tests
        return f"LLMConfig({self.safe_summary()})"

    __str__ = __repr__

    def safe_summary(self) -> str:
        """Human-readable summary that deliberately never contains the key."""
        state = "set" if self.api_key else "missing"
        return (
            f"provider={self.provider!r} model={self.model!r} base_url={self.base_url!r} "
            f"api_key=<{state}, via {self.api_key_env}> timeout={self.timeout_seconds}s "
            f"max_tokens={self.max_tokens} json_mode={self.json_mode}"
        )

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_present": bool(self.api_key),
            "api_key_env": self.api_key_env,
            "timeout_seconds": self.timeout_seconds,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "json_mode": self.json_mode,
            "max_transport_retries": self.max_transport_retries,
        }

    def chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def _coerce_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise LLMConfigError(f"{field_name} must be a boolean, got {value!r}")


def _coerce_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError(f"{field_name} must be a number, got {value!r}") from exc


def _coerce_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError(f"{field_name} must be an integer, got {value!r}") from exc


def resolve_api_key(config: LLMConfig | Mapping[str, Any], env: Mapping[str, str] | None = None) -> str | None:
    """Return the API key from the environment, without ever exposing it elsewhere."""
    env = os.environ if env is None else env
    if isinstance(config, LLMConfig):
        candidates = (config.api_key_env, *PROVIDER_KEY_ENVS.get(config.provider, ()))
    else:
        candidates = (
            str(config.get("api_key_env") or DEFAULT_API_KEY_ENV),
            *PROVIDER_KEY_ENVS.get(str(config.get("provider") or "deepseek"), ()),
        )
    for name in candidates:
        if not name:
            continue
        value = env.get(name)
        if value and value.strip():
            return value.strip()
    return None


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    require_key: bool = True,
    **overrides: Any,
) -> LLMConfig:
    """Build an :class:`LLMConfig` from overrides > environment > file > defaults."""
    env = os.environ if env is None else env

    file_values: dict[str, Any] = {}
    raw_path = config_path or env.get(ENV_CONFIG_PATH) or DEFAULT_CONFIG_PATH
    path = Path(raw_path).expanduser()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LLMConfigError(f"could not read LLM config file {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise LLMConfigError(f"LLM config file {path} must contain a JSON object")
        unknown = sorted(set(loaded) - CONFIG_FILE_FIELDS)
        if unknown:
            raise LLMConfigError(f"LLM config file {path} has unknown field(s): {unknown}")
        file_values = loaded
    elif config_path is not None or env.get(ENV_CONFIG_PATH):
        # an explicitly requested config file must exist
        raise LLMConfigError(f"LLM config file not found: {path}")

    values: dict[str, Any] = dict(file_values)
    for env_name, field_name in _ENV_FIELDS.items():
        if env_name in env and env[env_name].strip():
            values[field_name] = env[env_name].strip()
    values.update({k: v for k, v in overrides.items() if v is not None})

    provider = str(values.get("provider", "deepseek")).strip().lower()
    if provider not in PROVIDER_DEFAULTS:
        raise LLMConfigError(
            f"unknown provider {provider!r}; known providers: {sorted(PROVIDER_DEFAULTS)} "
            "(use provider='custom' with an explicit base_url for anything else)"
        )

    base_url = str(values.get("base_url") or PROVIDER_DEFAULTS[provider]).strip()
    if not base_url:
        raise LLMConfigError(f"provider {provider!r} has no default base_url; pass base_url explicitly")
    if not re.match(r"^https?://", base_url):
        raise LLMConfigError(f"base_url must start with http:// or https://, got {base_url!r}")

    api_key_env = str(values.get("api_key_env") or DEFAULT_API_KEY_ENV).strip()

    config = LLMConfig(
        provider=provider,
        model=str(values.get("model") or "deepseek-flash").strip(),
        base_url=base_url,
        api_key="",  # resolved below: explicit > environment > config file
        api_key_env=api_key_env,
        timeout_seconds=_coerce_float(values.get("timeout_seconds", 120.0), "timeout_seconds"),
        max_tokens=_coerce_int(values.get("max_tokens", 2048), "max_tokens"),
        temperature=_coerce_float(values.get("temperature", 0.0), "temperature"),
        json_mode=_coerce_bool(values.get("json_mode", True), "json_mode"),
        max_transport_retries=_coerce_int(values.get("max_transport_retries", 2), "max_transport_retries"),
        backoff_seconds=_coerce_float(values.get("backoff_seconds", 1.0), "backoff_seconds"),
        extra_headers=dict(values.get("extra_headers") or {}),
        extra_body=dict(values.get("extra_body") or {}),
    )

    if not config.model:
        raise LLMConfigError("model must not be empty")
    if config.timeout_seconds <= 0:
        raise LLMConfigError("timeout_seconds must be positive")
    if config.max_tokens <= 0:
        raise LLMConfigError("max_tokens must be positive")

    # API key precedence: explicit argument > environment > config file.  A key
    # typed into config/llm.json must never silently win over an env rotation.
    explicit_key = str(overrides.get("api_key") or "").strip()
    file_key = str(file_values.get("api_key") or "").strip()
    from_env = resolve_api_key(config, env)
    config = replace(config, api_key=explicit_key or from_env or file_key)
    if require_key and not config.api_key:
        tried = ", ".join((config.api_key_env, *PROVIDER_KEY_ENVS.get(config.provider, ())))
        raise LLMConfigError(
            f"no API key configured for provider {config.provider!r}; set one of: {tried}, "
            f"or put 'api_key_env' in {path}"
        )
    return config


# --------------------------------------------------------------------------
# request / response
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class LLMRequest:
    """A structured request; ``messages`` is the whole prompt (system + user)."""

    messages: tuple[ChatMessage, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, system: str, user: str, **metadata: str) -> "LLMRequest":
        return cls(messages=(ChatMessage("system", system), ChatMessage("user", user)), metadata=metadata)

    def with_user_suffix(self, suffix: str) -> "LLMRequest":
        """Append to the last user message (used for correction retries)."""
        messages = list(self.messages)
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].role == "user":
                messages[index] = ChatMessage("user", messages[index].content + suffix)
                break
        else:  # pragma: no cover - requests always carry a user message
            messages.append(ChatMessage("user", suffix))
        return LLMRequest(messages=tuple(messages), metadata=self.metadata)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    finish_reason: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    latency_seconds: float = 0.0
    reasoning_text: str = ""
    request_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int | None:
        value = self.usage.get("total_tokens")
        return int(value) if isinstance(value, int) else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "finish_reason": self.finish_reason,
            "usage": dict(self.usage),
            "latency_seconds": round(self.latency_seconds, 3),
            "request_id": self.request_id,
            "reasoning_chars": len(self.reasoning_text),
            "text_chars": len(self.text),
        }


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

class Transport(Protocol):
    """Where a request physically goes. Tests inject a fake implementation."""

    def send(self, request: LLMRequest, config: LLMConfig) -> LLMResponse:  # pragma: no cover - protocol
        ...


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of replaying the request elsewhere.

    urllib would otherwise follow a 301/302/303 and forward the
    ``Authorization: Bearer <key>`` header to whatever host the (possibly
    compromised or misconfigured) provider points at.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise LLMRequestError(
            f"provider answered HTTP {code} with a redirect to another location; "
            "redirects are refused so the API key is never forwarded elsewhere",
            status_code=code,
            retryable=False,
        )


_NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirectHandler)


def redact_secrets(text: str, *secrets: str) -> str:
    """Remove credentials and bearer tokens from text before surfacing it.

    Byte-exact replacement plus a bearer-token pattern; this covers realistic
    provider/proxy echoes.  Deliberately not a general DLP: encoded or split
    forms of a key are out of scope (documented limitation).
    """
    cleaned = str(text)
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "<redacted>")
    return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{8,}", r"\1<redacted>", cleaned)


class HttpTransport:
    """OpenAI-compatible ``POST {base_url}/chat/completions`` over ``urllib``.

    The default opener never follows redirects (see :class:`NoRedirectHandler`).
    """

    def __init__(self, opener: Callable[..., Any] | None = None) -> None:
        self._opener = opener or _NO_REDIRECT_OPENER.open

    def build_body(self, request: LLMRequest, config: LLMConfig) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": config.model,
            "messages": [message.to_dict() for message in request.messages],
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "stream": False,
        }
        if config.json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(dict(config.extra_body))
        return body

    def send(self, request: LLMRequest, config: LLMConfig) -> LLMResponse:
        body = self.build_body(request, config)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {config.api_key}",
            **dict(config.extra_headers),
        }
        http_request = urllib.request.Request(
            config.chat_completions_url(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        started = time.monotonic()
        try:
            with self._opener(http_request, timeout=config.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                raw_text = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # provider answered with an error status
            detail = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
            # a provider (or a proxy in front of it) may echo our Authorization header
            detail = redact_secrets(detail, config.api_key)
            retryable = exc.code == 429 or exc.code >= 500
            raise LLMRequestError(
                f"provider returned HTTP {exc.code} for {config.model}: {_shorten(detail)}",
                status_code=exc.code,
                retryable=retryable,
            ) from exc
        except urllib.error.URLError as exc:
            reason = redact_secrets(str(exc.reason), config.api_key)
            raise LLMRequestError(
                f"could not reach {redact_secrets(config.base_url, config.api_key)}: {reason}",
                retryable=True,
            ) from exc
        except TimeoutError as exc:
            raise LLMRequestError(
                f"request to {redact_secrets(config.base_url, config.api_key)} timed out "
                f"after {config.timeout_seconds}s",
                retryable=True,
            ) from exc
        except OSError as exc:
            # e.g. ConnectionAbortedError/ConnectionResetError raised by http.client while
            # reading the status line: a transport failure, never a typed-but-silent crash
            raise LLMRequestError(
                f"connection error while calling "
                f"{redact_secrets(config.base_url, config.api_key)}: "
                f"{type(exc).__name__}: {redact_secrets(str(exc), config.api_key)}",
                retryable=True,
            ) from exc

        latency = time.monotonic() - started
        if status >= 400:  # pragma: no cover - defensive; HTTPError covers most cases
            raise LLMRequestError(
                f"provider returned HTTP {status}: {_shorten(redact_secrets(raw_text, config.api_key))}",
                status_code=status,
                retryable=status == 429 or status >= 500,
            )
        try:
            payload = json.loads(raw_text)
        except ValueError as exc:
            raise LLMResponseError(
                f"provider response was not JSON: {_shorten(redact_secrets(raw_text, config.api_key))}"
            ) from exc
        return self.parse_envelope(payload, latency)

    @staticmethod
    def parse_envelope(payload: Mapping[str, Any], latency: float = 0.0) -> LLMResponse:
        """Turn an OpenAI-compatible envelope into an :class:`LLMResponse`."""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError("provider response has no choices")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise LLMResponseError("provider response choice is not an object")
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise LLMResponseError("provider response choice has no message object")
        finish_reason = choice.get("finish_reason")
        text = message.get("content") or ""
        if not isinstance(text, str):
            raise LLMResponseError("provider response message.content is not text")
        if finish_reason == "length":
            raise TruncatedResponseError(
                "provider stopped early (finish_reason=length); the structured answer was truncated, "
                "raise max_tokens or shorten the input"
            )
        if not text.strip():
            raise LLMResponseError("provider returned an empty message.content")
        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        return LLMResponse(
            text=text,
            model=str(payload.get("model") or ""),
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            usage=dict(usage or {}),
            latency_seconds=latency,
            reasoning_text=str(message.get("reasoning_content") or ""),
            request_id=str(payload.get("id")) if payload.get("id") else None,
            raw={"id": payload.get("id"), "object": payload.get("object")},
        )


def _shorten(text: str, limit: int = 300) -> str:
    cleaned = " ".join(str(text).split())
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "..."


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

class LLMClient:
    """Transport retries + strict JSON extraction.  All provider access goes through here."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.transport = transport or HttpTransport()
        self._sleep = sleep
        self.last_response: LLMResponse | None = None
        self.attempts: int = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Send one request, retrying only *retryable* transport failures."""
        attempts = max(1, self.config.max_transport_retries + 1)
        last_error: LLMRequestError | None = None
        self.attempts = 0
        for attempt in range(1, attempts + 1):
            self.attempts = attempt
            try:
                response = self.transport.send(request, self.config)
                self.last_response = response
                return response
            except LLMRequestError as exc:
                last_error = exc
                if not exc.retryable or attempt == attempts:
                    raise
                self._sleep(self.config.backoff_seconds * (2 ** (attempt - 1)))
        raise last_error  # pragma: no cover - loop either returns or raises

    def complete_json(self, request: LLMRequest) -> tuple[dict[str, Any], LLMResponse]:
        """Send a request and require a JSON **object** back (no prose parsing)."""
        response = self.complete(request)
        payload = extract_json_object(response.text, secret=self.config.api_key)
        return payload, response


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)


def extract_json_object(text: str, *, secret: str | None = None) -> dict[str, Any]:
    """Strictly turn ``text`` into a JSON object.

    Accepted: the whole text is a JSON object, or the whole text is a single
    fenced block containing one.  Anything else (prose around the JSON, an
    array, a partial object) is rejected -- the adapter never guesses.

    ``secret`` is scrubbed from the reported snippet: a model (or a gateway that
    echoes our headers) must never be able to surface the credential in an error.
    """
    if not isinstance(text, str) or not text.strip():
        raise LLMResponseError("provider returned an empty answer where a JSON object was required")
    candidate = text.strip()
    fence = _FENCE_RE.match(candidate)
    if fence:
        candidate = fence.group("body").strip()
    try:
        payload = json.loads(candidate)
    except ValueError as exc:
        snippet = _shorten(redact_secrets(candidate, secret or ""), 200)
        raise LLMResponseError(f"provider answer is not a JSON object: {exc}: {snippet}") from exc
    if not isinstance(payload, dict):
        raise LLMResponseError(f"provider answer must be a JSON object, got {type(payload).__name__}")
    return payload


def iter_message_texts(request: LLMRequest) -> Iterator[str]:  # pragma: no cover - helper for debugging
    for message in request.messages:
        yield message.content
