"""Real Teacher provider (KB 1.1 Phase 2C-5): ``TeacherModel`` over the existing HTTP layer.

Where it sits
-------------
::

    TeacherRuntime (P2C-4)  →  TeacherLLMAdapter (P2C-3)
                                      │  TeacherModel protocol
                                      ▼
                              TeacherProvider          ← this module
                                      │  Transport protocol (P2C/1.0 ``llm.py``)
                                      ▼
                              HttpTransport            ← the 1.0 OpenAI-compatible client
                                      │  urllib, no redirects, redacted errors
                                      ▼
                              provider endpoint

Why this is *reuse*, not a second HTTP client
---------------------------------------------
The 1.0 LLM layer already owns everything wire-level, and this module reuses it instead
of copying it:

* ``LLMConfig`` / ``load_config`` / ``resolve_api_key`` -- the project's only
  configuration and credential mechanism (overrides > environment > JSON file >
  provider defaults; the key is never hardcoded and never echoed by
  ``LLMConfig.safe_summary``).  No new config system is introduced here.
* ``HttpTransport`` -- ``POST {base_url}/chat/completions`` over ``urllib``, with
  redirects refused (so the bearer token cannot be replayed elsewhere) and secrets
  scrubbed from provider echoes by ``redact_secrets``.
* ``LLMRequest`` / ``LLMResponse`` / ``Transport`` -- the request/response value objects
  and the injection point tests use to stay offline.

Two 1.0 pieces are deliberately **not** reused, because reusing them would change the
teacher semantics this phase must preserve:

* ``LLMClient.complete()`` retries retryable transport failures.  The teacher path must
  not retry, so this provider calls ``transport.send()`` **once** and lets the failure
  travel up (``LLMConfig.max_transport_retries`` is therefore inert here, on purpose).
* ``LLMClient.complete_json()`` / ``extract_json_object()`` unpack and (for fenced
  answers) unwrap the model's text.  The teacher contract requires the model output to
  reach ``TeacherLLMAdapter`` **verbatim**, so the provider returns the raw text and
  never repairs, unwraps or re-parses it.

Nothing above this module sees "chat completions" vocabulary: the dependency is
expressed only through the ``Transport`` protocol, so a different provider means a
different transport (or a different ``TeacherModel``), never a change to the contract,
the adapter, the executor or the runtime.

Boundaries
----------
* it knows only: "here is a system prompt, a user prompt and a response schema -- give me
  the model's answer".  It has no idea what a Memory, Source, LearningState,
  LearningSession or TeacherAction is.
* no database, no repository, no ``LearningService``, no ``TeacherRuntime``, no
  ``TeacherActionExecutor``, no web/UI, no agent loop, no retry, no streaming, no tool
  calling, no JSON repair, no action validation.
* a provider/transport failure (HTTP 4xx/5xx, timeout, connection error, malformed
  provider envelope, empty or truncated content) becomes ``TeacherModelError`` with the
  original exception kept as ``__cause__``.  A *successful* call whose text is not a
  legal teacher response is **not** this module's business: the text is returned as-is
  and ``TeacherLLMAdapter`` raises ``ValidationError`` -- the two failure classes stay
  distinct.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .errors import ValidationError
from .llm import (
    HttpTransport,
    LLMConfig,
    LLMConfigError,
    LLMRequest,
    LLMRequestError,
    LLMResponse,
    LLMResponseError,
    Transport,
    redact_secrets,
)
from .teacher_llm import TeacherModel, TeacherModelError

__all__ = ["TeacherProvider"]

#: Tracing metadata carried on every teacher request (the existing ``LLMRequest``
#: field; 1.0 uses the same mechanism to tag its own calls).
REQUEST_METADATA: Mapping[str, str] = {"caller": "teacher_provider"}


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"{field_name} must be a non-empty string, got {type(value).__name__}", field=field_name
        )
    return value


def _scrub(error: BaseException, api_key: str) -> str:
    """Text of a failure, with the credential removed (``llm.redact_secrets``).

    ``HttpTransport`` already redacts its own messages, but this provider is the
    boundary that publishes provider text to the layers above: an injected or future
    transport that forgets to redact must not be able to put the key into an exception
    message that ends up in a log.
    """
    return redact_secrets(str(error), api_key)


def _validate_config(config: Any) -> LLMConfig:
    """Refuse a config that cannot produce a real call (never echoes the key)."""
    if not isinstance(config, LLMConfig):
        raise LLMConfigError(
            f"the teacher provider needs an LLMConfig, got {type(config).__name__}"
        )
    model = config.model.strip() if isinstance(config.model, str) else ""
    if not model:
        raise LLMConfigError("model must not be empty")
    api_key = config.api_key.strip() if isinstance(config.api_key, str) else ""
    if not api_key:
        raise LLMConfigError(
            f"no API key configured for provider {config.provider!r}; set {config.api_key_env} "
            "(or pass api_key) -- the provider never falls back to an unauthenticated call"
        )
    if not isinstance(config.base_url, str) or not config.base_url.startswith(("http://", "https://")):
        raise LLMConfigError(
            f"base_url must start with http:// or https://, got {config.base_url!r}"
        )
    if config.timeout_seconds <= 0:
        raise LLMConfigError(f"timeout_seconds must be positive, got {config.timeout_seconds!r}")
    if config.max_tokens <= 0:
        raise LLMConfigError(f"max_tokens must be positive, got {config.max_tokens!r}")
    return replace(config, model=model, api_key=api_key)


class TeacherProvider:
    """A real :class:`~personal_memory.teacher_llm.TeacherModel` (one call per turn).

    Construct it from the project's own config object -- typically
    ``load_config(path)``, which resolves the key from the environment or the local
    JSON file -- or from explicit credentials via :meth:`from_credentials`.  Inject a
    ``transport`` to test or to talk to a different wire protocol.
    """

    def __init__(self, config: LLMConfig, *, transport: Transport | None = None) -> None:
        self.config = _validate_config(config)
        self.transport: Transport = transport or HttpTransport()
        #: The last successful provider envelope (usage / latency / finish_reason).
        #: Observability only: never used to decide anything.
        self.last_response: LLMResponse | None = None

    # ==================================================================
    # construction helpers (same fields as the existing LLMConfig)
    # ==================================================================
    @classmethod
    def from_credentials(
        cls,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float | None = None,
        provider: str = "custom",
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool | None = None,
        extra_headers: Mapping[str, str] | None = None,
        extra_body: Mapping[str, Any] | None = None,
        transport: Transport | None = None,
    ) -> "TeacherProvider":
        """Build a provider from explicit credentials (everything else keeps the
        ``LLMConfig`` defaults; no field is invented outside that dataclass)."""
        base = LLMConfig(provider=provider, model=model, base_url=base_url, api_key=api_key)
        overrides: dict[str, Any] = {
            "timeout_seconds": timeout,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "json_mode": json_mode,
            "extra_headers": extra_headers,
            "extra_body": extra_body,
        }
        return cls(
            replace(base, **{name: value for name, value in overrides.items() if value is not None}),
            transport=transport,
        )

    # ==================================================================
    # TeacherModel
    # ==================================================================
    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        """Send one turn to the provider and return the model's raw answer text.

        ``response_schema`` is part of the ``TeacherModel`` contract and is checked to
        be a mapping, but it is deliberately **not** sent as a wire parameter: the
        OpenAI-compatible protocol only offers "answer with a JSON object"
        (``response_format``, already applied by ``HttpTransport`` when
        ``config.json_mode`` is set), and the schema itself reaches the model inside the
        user prompt -- ``TeacherPromptBuilder`` embeds it as ``<response_contract>``.
        That is this provider's entire compatibility layer: a request-level JSON-object
        mode plus the prompt the layer above built.  A provider that supports native
        JSON Schema can override ``response_format`` through ``LLMConfig.extra_body``
        (which ``HttpTransport`` applies after its own defaults) without any teacher
        layer changing.

        The returned text is exactly ``message.content``: this module never strips a
        markdown fence, repairs a broken object, guesses a field, rewrites or reorders
        an action.  Strict parsing is ``TeacherLLMAdapter``'s job.
        """
        _require_text(system_prompt, "system_prompt")
        _require_text(user_prompt, "user_prompt")
        if not isinstance(response_schema, Mapping):
            raise ValidationError(
                f"response_schema must be a mapping, got {type(response_schema).__name__}",
                field="response_schema",
            )

        request = LLMRequest.of(system_prompt, user_prompt, **REQUEST_METADATA)
        return self._send_once(request).text

    # ==================================================================
    # internals
    # ==================================================================
    def _send_once(self, request: LLMRequest) -> LLMResponse:
        """Exactly one ``transport.send`` -- no retry, no back-off, no second attempt.

        Provider-side failures are translated into :class:`TeacherModelError` with
        ``__cause__`` preserved.  Anything the transport raises that is *not* one of the
        project's typed LLM failures (a bug, not a provider condition) is allowed to
        travel unchanged: the adapter is the last-resort net one layer up, and masking a
        programming error as "the model failed" would be misleading.
        """
        try:
            response = self.transport.send(request, self.config)
        except LLMRequestError as exc:
            raise TeacherModelError(
                self.config.model, f"provider call failed: {_scrub(exc, self.config.api_key)}"
            ) from exc
        except LLMResponseError as exc:
            raise TeacherModelError(
                self.config.model,
                f"provider response rejected: {_scrub(exc, self.config.api_key)}",
            ) from exc
        except OSError as exc:  # bare socket/timeout errors from a custom transport
            raise TeacherModelError(
                self.config.model,
                f"{type(exc).__name__} while calling the provider: "
                f"{_scrub(exc, self.config.api_key)}",
            ) from exc
        self.last_response = response
        return response

    # ==================================================================
    # diagnostics (never contain the key)
    # ==================================================================
    def __repr__(self) -> str:
        return f"TeacherProvider({self.config.safe_summary()})"

    __str__ = __repr__
