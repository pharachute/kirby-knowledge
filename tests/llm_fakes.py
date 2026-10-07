"""Offline fakes for the Phase 2 tests.

Every Phase 2 test runs without network access: the LLM adapter is exercised
through :class:`ScriptedTransport` (a queue of canned responses/exceptions) and
through a fake ``urlopen`` for the wire-level assertions.
"""

from __future__ import annotations

import io
import json
from typing import Any, Iterable, Mapping, Sequence

from personal_memory.llm import LLMClient, LLMConfig, LLMResponse


class FakeTeacherModel:
    """A ``TeacherModel`` (Phase 2C-3) with no network and no provider behind it.

    Scripted like :class:`ScriptedTransport`: every queued item is returned as-is, and
    an ``Exception`` item is raised.  Every call is recorded, so a test can assert
    exactly what the adapter asked the model for (prompt texts, response schema) and
    how many times it asked (retry checks).
    """

    def __init__(self, *responses: Any) -> None:
        self.responses: list[Any] = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, system_prompt: str, user_prompt: str, response_schema: Any) -> Any:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response_schema": response_schema,
            }
        )
        if not self.responses:
            raise AssertionError("FakeTeacherModel ran out of scripted responses")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_call(self) -> dict[str, Any]:
        return self.calls[-1]


def response(
    content: str,
    *,
    model: str = "mock-model",
    finish_reason: str = "stop",
    usage: Mapping[str, Any] | None = None,
    reasoning: str = "",
) -> LLMResponse:
    return LLMResponse(
        text=content,
        model=model,
        finish_reason=finish_reason,
        usage=dict(usage or {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}),
        latency_seconds=0.01,
        reasoning_text=reasoning,
    )


def response_for(payload: Mapping[str, Any] | Sequence[Any] | str, **kwargs: Any) -> LLMResponse:
    """Wrap a Python structure as the JSON text a provider would return."""
    if isinstance(payload, str):
        return response(payload, **kwargs)
    return response(json.dumps(payload, ensure_ascii=False), **kwargs)


class ScriptedTransport:
    """A transport that returns queued items; an ``Exception`` item is raised.

    Records every request so tests can assert what the model was actually asked.
    """

    def __init__(self, items: Iterable[Any]) -> None:
        self.items: list[Any] = list(items)
        self.requests: list[Any] = []

    def send(self, request: Any, config: LLMConfig) -> LLMResponse:
        self.requests.append(request)
        if not self.items:
            raise AssertionError("ScriptedTransport ran out of scripted responses")
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def call_count(self) -> int:
        return len(self.requests)


def mock_client(*items: Any, config: LLMConfig | None = None) -> LLMClient:
    """An LLMClient wired to a scripted transport, with instant back-off."""
    cfg = config or LLMConfig(provider="deepseek", model="mock-model", base_url="https://mock.invalid", api_key="test-key")
    return LLMClient(cfg, transport=ScriptedTransport(items), sleep=lambda _seconds: None)


# --------------------------------------------------------------------------
# fake urlopen for HttpTransport tests
# --------------------------------------------------------------------------

class FakeHTTPResponse:
    def __init__(self, payload: Any, status: int = 200, raw: str | None = None) -> None:
        self.status = status
        self._raw = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._raw.encode("utf-8")

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


class RecordingOpener:
    """Stand-in for ``urllib.request.urlopen`` that records requests."""

    def __init__(self, *responses: Any) -> None:
        self.responses: list[Any] = list(responses)
        self.requests: list[Any] = []
        self.timeouts: list[float] = []

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout or 0.0)
        if not self.responses:
            raise AssertionError("RecordingOpener ran out of scripted responses")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def body_of(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].data.decode("utf-8"))


def http_error(status: int, message: str = "error", body: str | None = None):
    """Build an ``urllib.error.HTTPError`` the way a provider would return it."""
    import urllib.error

    payload = body if body is not None else json.dumps({"error": {"message": message}})
    return urllib.error.HTTPError(
        url="https://mock.invalid/chat/completions",
        code=status,
        msg=message,
        hdrs={},
        fp=io.BytesIO(payload.encode("utf-8")),
    )


def url_error(reason: str = "connection refused"):
    import urllib.error

    return urllib.error.URLError(reason)
