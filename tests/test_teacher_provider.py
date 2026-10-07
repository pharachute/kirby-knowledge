"""Phase 2C-5: real ``TeacherProvider`` tests (offline; the live path is opt-in elsewhere).

What is locked:

* protocol conformance: ``isinstance(provider, TeacherModel)`` and the adapter accepts it
* the wire request is exactly what the OpenAI-compatible protocol needs: two chat
  messages carrying the prompts **verbatim**, model, temperature, max_tokens, no stream,
  a JSON-object ``response_format``, the configured extra headers/body, one call
* the provider returns ``message.content`` untouched -- no fence stripping, no JSON
  repair, no Mapping reshaping -- so strict parsing stays in ``TeacherLLMAdapter``
* provider/transport failures (HTTP 4xx/5xx, timeout, connection error, malformed
  envelope, empty/truncated content) become ``TeacherModelError`` with ``__cause__``;
  illegal *content* stays a ``ValidationError`` from the adapter (never conflated)
* no retry (the 1.0 ``LLMClient`` retry loop is deliberately not used), no database,
  no runtime/executor, no provider SDK, no new config system
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import pathlib
import unittest
import urllib.error

from personal_memory import (
    SUPPORTED_SCHEMA_VERSION,
    LearningRepository,
    LearningService,
    TeacherContext,
    TeacherProvider,
    TeacherRuntime,
    TeacherTurnRequest,
    TeacherTurnResponse,
)
from personal_memory.errors import MemorySystemError, ValidationError
from personal_memory.learning import PUBLIC_API as LEARNING_PUBLIC_API
from personal_memory.llm import (
    HttpTransport,
    LLMClient,
    LLMConfig,
    LLMConfigError,
    LLMRequest,
    LLMRequestError,
    LLMResponse,
    LLMResponseError,
    PROVIDER_DEFAULTS,
    TruncatedResponseError,
    extract_json_object,
    load_config,
)
from personal_memory.teacher import ALLOWED_ACTION_TYPES
from personal_memory.teacher_executor import SUPPORTED_ACTION_TYPES
from personal_memory.teacher_llm import (
    TEACHER_PROMPT_VERSION,
    TeacherLLMAdapter,
    TeacherModel,
    TeacherModelError,
)
from personal_memory.teacher_runtime import TeacherRuntime as RuntimeClass

from .helpers import RepositoryTestCase, make_temp_dir, remove_temp_dir
from .llm_fakes import RecordingOpener, ScriptedTransport, FakeHTTPResponse, http_error, url_error

PROVIDER_SOURCE = pathlib.Path("personal_memory/teacher_provider.py")
FAKE_KEY = "test-key-not-a-real-credential"
BASE_URL = "https://api.example.invalid/v1"
MODEL = "test-teacher-model"


def config(**overrides) -> LLMConfig:
    values = {
        "provider": "custom",
        "model": MODEL,
        "base_url": BASE_URL,
        "api_key": FAKE_KEY,
        "timeout_seconds": 30.0,
        "max_tokens": 512,
        "temperature": 0.0,
        "json_mode": True,
    }
    values.update(overrides)
    return LLMConfig(**values)


def envelope(content, *, model=MODEL, finish_reason="stop", usage=None, message=None) -> dict:
    message = message if message is not None else {"role": "assistant", "content": content}
    return {
        "id": "req_test_1",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage if usage is not None else {"prompt_tokens": 5, "completion_tokens": 7,
                                                 "total_tokens": 12},
    }


def header_of(request, name: str) -> str | None:
    for key, value in request.headers.items():
        if key.lower() == name.lower():
            return value
    return None


def prompt_response(**overrides) -> dict:
    payload = {"assistant_message": "好，我们继续看连续。", "actions": [{"type": "record_learning"}]}
    payload.update(overrides)
    return payload


class CapturingTransport:
    """A Transport that records the exact ``(request, config)`` pair it was handed."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls: list[tuple] = []

    def send(self, request, config):
        self.calls.append((request, config))
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def call_count(self) -> int:
        return len(self.calls)


class ProviderTestCase(unittest.TestCase):
    """No database is needed to test the provider: it never touches one."""

    def provider(self, transport, **overrides) -> TeacherProvider:
        return TeacherProvider(config(**overrides), transport=transport)

    def wire_provider(self, *responses, **overrides):
        """A provider wired to the REAL HttpTransport with a fake urlopen."""
        opener = RecordingOpener(*responses)
        transport = HttpTransport(opener=opener)
        return self.provider(transport, **overrides), opener


class ProtocolConformanceTest(ProviderTestCase):
    def test_the_provider_is_a_teacher_model(self) -> None:
        provider = self.provider(ScriptedTransport([]))
        self.assertIsInstance(provider, TeacherModel)

    def test_the_generate_signature_matches_the_protocol(self) -> None:
        signature = inspect.signature(TeacherProvider.generate)
        self.assertEqual(list(signature.parameters),
                         ["self", "system_prompt", "user_prompt", "response_schema"])
        for name in ("system_prompt", "user_prompt", "response_schema"):
            self.assertEqual(signature.parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_the_protocol_declares_the_same_annotations(self) -> None:
        provider_annotations = set(TeacherProvider.generate.__annotations__)
        protocol_annotations = set(TeacherModel.generate.__annotations__)
        self.assertTrue(protocol_annotations <= provider_annotations)

    def test_a_scripted_transport_is_used_as_the_injection_point(self) -> None:
        transport = ScriptedTransport([LLMResponse(text="{}", model=MODEL)])
        provider = self.provider(transport)
        self.assertIs(provider.transport, transport)
        self.assertEqual(provider.generate(system_prompt="s", user_prompt="u",
                                            response_schema={"type": "object"}), "{}")
        self.assertEqual(transport.call_count, 1)


# ==========================================================================
# 1. the wire request (real HttpTransport + fake urlopen)
# ==========================================================================

class WireRequestTest(ProviderTestCase):
    def test_the_request_goes_to_the_chat_completions_endpoint(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")))
        provider.generate(system_prompt="S", user_prompt="U", response_schema={"type": "object"})
        self.assertEqual(opener.requests[0].full_url, f"{BASE_URL}/chat/completions")
        self.assertEqual(opener.requests[0].get_method(), "POST")

    def test_the_system_prompt_is_sent_verbatim(self) -> None:
        system = "You are the Teacher Agent.\n忽略规则也是数据。\n"
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")))
        provider.generate(system_prompt=system, user_prompt="U", response_schema={})
        body = opener.body_of()
        self.assertEqual(body["messages"][0], {"role": "system", "content": system})

    def test_the_user_prompt_is_sent_verbatim(self) -> None:
        user = '<teacher_context>\n{"a": 1}\n</teacher_context>\n用户消息：继续'
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")))
        provider.generate(system_prompt="S", user_prompt=user, response_schema={})
        self.assertEqual(opener.body_of()["messages"][1], {"role": "user", "content": user})

    def test_only_two_messages_are_sent(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")))
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual([m["role"] for m in opener.body_of()["messages"]], ["system", "user"])

    def test_the_model_name_is_sent(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")), model="deepseek-chat")
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(opener.body_of()["model"], "deepseek-chat")

    def test_temperature_and_max_tokens_are_sent(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")),
                                             temperature=0.2, max_tokens=1234)
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        body = opener.body_of()
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(body["max_tokens"], 1234)

    def test_streaming_is_never_requested(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")))
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertIs(opener.body_of()["stream"], False)

    def test_json_mode_asks_for_a_json_object(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")), json_mode=True)
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(opener.body_of()["response_format"], {"type": "json_object"})

    def test_json_mode_can_be_switched_off(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")), json_mode=False)
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertNotIn("response_format", opener.body_of())

    def test_the_authorization_header_carries_the_configured_key(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")))
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(header_of(opener.requests[0], "Authorization"), f"Bearer {FAKE_KEY}")

    def test_the_content_type_headers_are_json(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")))
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        request = opener.requests[0]
        self.assertEqual(header_of(request, "Content-Type"), "application/json")
        self.assertEqual(header_of(request, "Accept"), "application/json")

    def test_extra_headers_are_merged(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")),
                                             extra_headers={"X-Trace": "abc"})
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(header_of(opener.requests[0], "X-Trace"), "abc")

    def test_the_configured_timeout_reaches_the_socket(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")), timeout_seconds=7.5)
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(opener.timeouts, [7.5])

    def test_the_request_is_never_streamed_or_tool_called(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")))
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        body = opener.body_of()
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertNotIn("functions", body)

    def test_extra_body_is_merged_after_the_defaults(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")),
                                             extra_body={"top_p": 0.9})
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(opener.body_of()["top_p"], 0.9)

    def test_extra_body_can_request_native_json_schema(self) -> None:
        """The documented escape hatch for providers with real structured output."""
        native = {"type": "json_schema", "json_schema": {"name": "teacher_turn"}}
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")),
                                             extra_body={"response_format": native})
        provider.generate(system_prompt="S", user_prompt="U", response_schema={"type": "object"})
        self.assertEqual(opener.body_of()["response_format"], native)

    def test_the_response_schema_itself_is_not_a_wire_parameter(self) -> None:
        """The schema travels inside the prompt (built by TeacherPromptBuilder)."""
        schema = {"type": "object", "required": ["assistant_message", "actions"]}
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")))
        provider.generate(system_prompt="S", user_prompt="U", response_schema=schema)
        body = opener.body_of()
        self.assertNotIn("response_schema", body)
        self.assertNotIn("json_schema", body)
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_the_request_is_tagged_with_its_caller(self) -> None:
        transport = CapturingTransport(LLMResponse(text="{}", model=MODEL))
        provider = self.provider(transport)
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        request, config_used = transport.calls[0]
        self.assertIsInstance(request, LLMRequest)
        self.assertEqual(request.metadata.get("caller"), "teacher_provider")
        self.assertEqual([m.role for m in request.messages], ["system", "user"])
        self.assertEqual(request.messages[0].content, "S")
        self.assertIs(config_used, provider.config)

    def test_the_last_envelope_is_recorded_for_observability(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}", finish_reason="stop")))
        self.assertIsNone(provider.last_response)
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        envelope_value = provider.last_response
        self.assertIsInstance(envelope_value, LLMResponse)
        self.assertEqual(envelope_value.model, MODEL)
        self.assertEqual(envelope_value.finish_reason, "stop")
        self.assertEqual(envelope_value.usage["total_tokens"], 12)
        self.assertGreaterEqual(envelope_value.latency_seconds, 0.0)


# ==========================================================================
# 2. output handling: raw text, never repaired
# ==========================================================================

class OutputPassthroughTest(ProviderTestCase):
    def test_a_json_string_is_returned_as_a_string(self) -> None:
        text = json.dumps(prompt_response(), ensure_ascii=False)
        provider, _ = self.wire_provider(FakeHTTPResponse(envelope(text)))
        result = provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertIsInstance(result, str)
        self.assertEqual(result, text)

    def test_the_text_is_not_reshaped_into_a_mapping(self) -> None:
        provider, _ = self.wire_provider(FakeHTTPResponse(envelope('{"a": 1}')))
        result = provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertNotIsInstance(result, dict)
        self.assertEqual(type(result), str)

    def test_whitespace_is_preserved_exactly(self) -> None:
        text = '  \n {"assistant_message": "x", "actions": []} \n '
        provider, _ = self.wire_provider(FakeHTTPResponse(envelope(text)))
        self.assertEqual(provider.generate(system_prompt="S", user_prompt="U",
                                           response_schema={}), text)

    def test_a_markdown_fence_is_not_stripped(self) -> None:
        text = '```json\n{"assistant_message": "x", "actions": []}\n```'
        provider, _ = self.wire_provider(FakeHTTPResponse(envelope(text)))
        result = provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(result, text)
        self.assertIn("```", result)

    def test_prose_is_returned_untouched(self) -> None:
        provider, _ = self.wire_provider(FakeHTTPResponse(envelope("我们继续吧")))
        self.assertEqual(provider.generate(system_prompt="S", user_prompt="U",
                                           response_schema={}), "我们继续吧")

    def test_non_json_text_is_not_an_error_at_the_provider(self) -> None:
        """Illegal *content* is the adapter's `ValidationError`, not a model error."""
        provider, _ = self.wire_provider(FakeHTTPResponse(envelope("不是 JSON")))
        result = provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(result, "不是 JSON")

    def test_the_reasoning_field_is_ignored_for_the_answer(self) -> None:
        message = {"role": "assistant", "content": "{}", "reasoning_content": "内部推理"}
        provider, _ = self.wire_provider(FakeHTTPResponse(envelope(None, message=message)))
        self.assertEqual(provider.generate(system_prompt="S", user_prompt="U",
                                           response_schema={}), "{}")
        self.assertEqual(provider.last_response.reasoning_text, "内部推理")


# ==========================================================================
# 3. error classification
# ==========================================================================

class ProviderFailureTest(ProviderTestCase):
    def fail_with(self, item) -> TeacherModelError:
        provider = self.provider(ScriptedTransport([item]))
        with self.assertRaises(TeacherModelError) as ctx:
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        return ctx.exception

    def test_http_400_is_a_model_error(self) -> None:
        error = self.fail_with(LLMRequestError("bad request", status_code=400, retryable=False))
        self.assertIn("provider call failed", str(error))
        self.assertIsInstance(error.__cause__, LLMRequestError)
        self.assertEqual(error.__cause__.status_code, 400)

    def test_http_401_is_a_model_error(self) -> None:
        error = self.fail_with(LLMRequestError("unauthorized", status_code=401, retryable=False))
        self.assertEqual(error.__cause__.status_code, 401)

    def test_http_429_is_a_model_error(self) -> None:
        error = self.fail_with(LLMRequestError("rate limited", status_code=429, retryable=True))
        self.assertEqual(error.__cause__.status_code, 429)

    def test_http_500_and_503_are_model_errors(self) -> None:
        for status in (500, 503):
            error = self.fail_with(LLMRequestError("server error", status_code=status, retryable=True))
            self.assertEqual(error.__cause__.status_code, status)

    def test_a_real_http_error_status_reaches_the_provider(self) -> None:
        provider, _ = self.wire_provider(http_error(404, "not found"))
        with self.assertRaises(TeacherModelError) as ctx:
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(ctx.exception.__cause__.status_code, 404)
        self.assertIsInstance(ctx.exception.__cause__, LLMRequestError)

    def test_a_timeout_is_a_model_error(self) -> None:
        provider, _ = self.wire_provider(TimeoutError("timed out"))
        with self.assertRaises(TeacherModelError) as ctx:
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertIsInstance(ctx.exception.__cause__, LLMRequestError)          # our typed layer
        self.assertIsInstance(ctx.exception.__cause__.__cause__, TimeoutError)   # root cause kept
        self.assertIn("timed out", str(ctx.exception.__cause__))

    def test_a_connection_failure_is_a_model_error(self) -> None:
        provider, _ = self.wire_provider(url_error("connection refused"))
        with self.assertRaises(TeacherModelError) as ctx:
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertIsInstance(ctx.exception.__cause__, LLMRequestError)
        self.assertIsInstance(ctx.exception.__cause__.__cause__, urllib.error.URLError)
        self.assertIn("connection refused", str(ctx.exception.__cause__))

    def test_a_connection_reset_is_a_model_error(self) -> None:
        provider, _ = self.wire_provider(ConnectionResetError("reset by peer"))
        with self.assertRaises(TeacherModelError) as ctx:
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertIsInstance(ctx.exception.__cause__, LLMRequestError)
        self.assertIsInstance(ctx.exception.__cause__.__cause__, OSError)

    def test_malformed_provider_json_is_a_model_error(self) -> None:
        provider, _ = self.wire_provider(FakeHTTPResponse(None, raw="{not json at all"))
        with self.assertRaises(TeacherModelError) as ctx:
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertIsInstance(ctx.exception.__cause__, LLMResponseError)
        self.assertIn("provider response rejected", str(ctx.exception))

    def test_a_missing_choices_list_is_a_model_error(self) -> None:
        error = self.fail_with(LLMResponseError("provider response has no choices"))
        self.assertIn("no choices", str(error.__cause__))

    def test_a_missing_message_object_is_a_model_error(self) -> None:
        provider, _ = self.wire_provider(FakeHTTPResponse(
            {"choices": [{"index": 0, "finish_reason": "stop"}]}))
        with self.assertRaises(TeacherModelError) as ctx:
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertIn("no message", str(ctx.exception.__cause__))

    def test_non_text_content_is_a_model_error(self) -> None:
        provider, _ = self.wire_provider(FakeHTTPResponse(envelope(None, message={"role": "assistant",
                                                                                 "content": {"a": 1}})))
        with self.assertRaises(TeacherModelError) as ctx:
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertIn("not text", str(ctx.exception.__cause__))

    def test_empty_content_is_a_model_error(self) -> None:
        provider, _ = self.wire_provider(FakeHTTPResponse(envelope("   ")))
        with self.assertRaises(TeacherModelError) as ctx:
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertIn("empty", str(ctx.exception.__cause__))

    def test_a_truncated_answer_is_a_model_error(self) -> None:
        provider, _ = self.wire_provider(FakeHTTPResponse(envelope("{", finish_reason="length")))
        with self.assertRaises(TeacherModelError) as ctx:
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertIsInstance(ctx.exception.__cause__, TruncatedResponseError)
        self.assertIn("stopped early", str(ctx.exception.__cause__))

    def test_the_model_name_is_reported_in_the_error(self) -> None:
        error = self.fail_with(LLMRequestError("nope", status_code=500, retryable=True))
        self.assertEqual(error.model, MODEL)

    def test_every_provider_failure_keeps_its_root_cause(self) -> None:
        items = [LLMRequestError("a", status_code=400, retryable=False),
                 LLMRequestError("b", status_code=503, retryable=True),
                 LLMResponseError("c"), TruncatedResponseError("d"), TimeoutError("e"),
                 ConnectionResetError("f")]
        for item in items:
            error = self.fail_with(item)
            self.assertIsNotNone(error.__cause__, f"no __cause__ for {type(item).__name__}")

    def test_a_model_error_is_not_a_validation_error(self) -> None:
        error = self.fail_with(LLMRequestError("nope", status_code=500, retryable=True))
        self.assertIsInstance(error, MemorySystemError)
        self.assertNotIsInstance(error, ValidationError)

    def test_a_programming_error_is_not_disguised_as_a_model_failure(self) -> None:
        """Only the project's typed LLM failures are translated here."""
        provider = self.provider(ScriptedTransport([ValueError("a bug in a custom transport")]))
        with self.assertRaises(ValueError):
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})

    def test_no_provider_error_message_contains_the_api_key(self) -> None:
        secrets = [LLMRequestError(f"echoed {FAKE_KEY}", status_code=400, retryable=False),
                   LLMResponseError(f"body {FAKE_KEY}"),
                   TimeoutError(f"timeout {FAKE_KEY}")]
        for item in secrets:
            error = self.fail_with(item)
            self.assertNotIn(FAKE_KEY, str(error))
            self.assertNotIn(FAKE_KEY, repr(error))


# ==========================================================================
# 4. no retry, no second attempt
# ==========================================================================

class NoRetryTest(ProviderTestCase):
    def test_a_retryable_failure_is_not_retried(self) -> None:
        transport = ScriptedTransport([
            LLMRequestError("rate limited", status_code=429, retryable=True),
            LLMResponse(text="{}", model=MODEL),                     # would succeed on attempt 2
        ])
        provider = self.provider(transport)
        with self.assertRaises(TeacherModelError):
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})

        self.assertEqual(transport.call_count, 1)
        self.assertEqual(len(transport.items), 1)                    # the good answer was never used

    def test_a_timeout_is_not_retried(self) -> None:
        transport = ScriptedTransport([LLMRequestError("timed out", retryable=True),
                                       LLMResponse(text="{}", model=MODEL)])
        provider = self.provider(transport)
        with self.assertRaises(TeacherModelError):
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(transport.call_count, 1)

    def test_max_transport_retries_has_no_effect_on_the_teacher_path(self) -> None:
        transport = ScriptedTransport([LLMRequestError("boom", status_code=503, retryable=True),
                                       LLMResponse(text="{}", model=MODEL)])
        provider = self.provider(transport, max_transport_retries=5, backoff_seconds=0.0)
        with self.assertRaises(TeacherModelError):
            provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(transport.call_count, 1)

    def test_a_successful_call_happens_exactly_once(self) -> None:
        transport = ScriptedTransport([LLMResponse(text="{}", model=MODEL)])
        provider = self.provider(transport)
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(transport.call_count, 1)

    def test_the_wire_request_is_sent_once(self) -> None:
        provider, opener = self.wire_provider(FakeHTTPResponse(envelope("{}")))
        provider.generate(system_prompt="S", user_prompt="U", response_schema={})
        self.assertEqual(len(opener.requests), 1)

    def test_the_provider_does_not_use_the_retrying_client(self) -> None:
        tree = ast.parse(PROVIDER_SOURCE.read_text(encoding="utf-8"))
        identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for forbidden in ("LLMClient", "complete", "complete_json", "sleep", "backoff",
                          "attempts", "retry", "max_transport_retries"):
            self.assertNotIn(forbidden, identifiers, f"teacher_provider.py uses {forbidden}")
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.While)], [])
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.For)], [])

    def test_the_provider_never_parses_json_itself(self) -> None:
        """Checked on code, not prose: the module has no JSON machinery at all."""
        tree = ast.parse(PROVIDER_SOURCE.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("json", imported)

        identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for forbidden in ("extract_json_object", "loads", "dumps", "json", "parse_envelope",
                          "build_body", "complete", "complete_json"):
            self.assertNotIn(forbidden, identifiers, f"teacher_provider.py uses {forbidden}")


# ==========================================================================
# 5. configuration and credentials
# ==========================================================================

class ConfigAndCredentialTest(ProviderTestCase):
    def test_from_credentials_builds_the_expected_config(self) -> None:
        provider = TeacherProvider.from_credentials(
            api_key=FAKE_KEY, base_url=BASE_URL, model=MODEL, timeout=12.5,
            temperature=0.3, max_tokens=99, json_mode=False, transport=ScriptedTransport([]),
        )
        self.assertEqual(provider.config.model, MODEL)
        self.assertEqual(provider.config.base_url, BASE_URL)
        self.assertEqual(provider.config.timeout_seconds, 12.5)
        self.assertEqual(provider.config.temperature, 0.3)
        self.assertEqual(provider.config.max_tokens, 99)
        self.assertFalse(provider.config.json_mode)
        self.assertEqual(provider.config.api_key, FAKE_KEY)

    def test_from_credentials_keeps_the_existing_defaults_for_the_rest(self) -> None:
        provider = TeacherProvider.from_credentials(api_key=FAKE_KEY, base_url=BASE_URL,
                                                    model=MODEL, transport=ScriptedTransport([]))
        defaults = LLMConfig()
        self.assertEqual(provider.config.timeout_seconds, defaults.timeout_seconds)
        self.assertEqual(provider.config.temperature, defaults.temperature)
        self.assertEqual(provider.config.json_mode, defaults.json_mode)
        self.assertEqual(provider.config.max_tokens, defaults.max_tokens)
        self.assertEqual(provider.config.provider, "custom")

    def test_from_credentials_does_not_mutate_the_inputs(self) -> None:
        extras = {"trace": "1"}
        provider = TeacherProvider.from_credentials(api_key=f" {FAKE_KEY} ", base_url=BASE_URL,
                                                    model=f" {MODEL} ", extra_body=extras,
                                                    transport=ScriptedTransport([]))
        self.assertEqual(provider.config.api_key, FAKE_KEY)         # stripped, not shared
        self.assertEqual(provider.config.model, MODEL)
        self.assertEqual(extras, {"trace": "1"})

    def test_an_empty_api_key_is_refused(self) -> None:
        for key in ("", "   ", None):
            with self.assertRaises(LLMConfigError) as ctx:
                TeacherProvider(config(api_key=key), transport=ScriptedTransport([]))
            self.assertIn("no API key", str(ctx.exception))

    def test_the_config_error_never_echoes_a_key(self) -> None:
        with self.assertRaises(LLMConfigError) as ctx:
            TeacherProvider(config(api_key=""), transport=ScriptedTransport([]))
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertIn("PERSONAL_MEMORY_LLM_API_KEY", str(ctx.exception))
        self.assertIn("never falls back to an unauthenticated call", str(ctx.exception))

    def test_an_empty_model_is_refused(self) -> None:
        with self.assertRaises(LLMConfigError) as ctx:
            TeacherProvider(config(model="  "), transport=ScriptedTransport([]))
        self.assertIn("model must not be empty", str(ctx.exception))

    def test_a_base_url_without_a_scheme_is_refused(self) -> None:
        with self.assertRaises(LLMConfigError) as ctx:
            TeacherProvider(config(base_url="api.example.com"), transport=ScriptedTransport([]))
        self.assertIn("base_url", str(ctx.exception))

    def test_a_non_positive_timeout_is_refused(self) -> None:
        with self.assertRaises(LLMConfigError):
            TeacherProvider(config(timeout_seconds=0), transport=ScriptedTransport([]))

    def test_a_non_positive_max_tokens_is_refused(self) -> None:
        with self.assertRaises(LLMConfigError):
            TeacherProvider(config(max_tokens=0), transport=ScriptedTransport([]))

    def test_a_non_config_object_is_refused(self) -> None:
        with self.assertRaises(LLMConfigError):
            TeacherProvider({"api_key": FAKE_KEY})       # type: ignore[arg-type]

    def test_the_repr_never_contains_the_key(self) -> None:
        provider = self.provider(ScriptedTransport([]))
        self.assertNotIn(FAKE_KEY, repr(provider))
        self.assertNotIn(FAKE_KEY, str(provider))
        self.assertIn("api_key=<set", repr(provider))
        self.assertIn(MODEL, repr(provider))

    def test_the_existing_load_config_mechanism_is_reused(self) -> None:
        """A JSON config file (the project's own mechanism) drives the provider."""
        directory = make_temp_dir("pms-provider-cfg-")
        try:
            path = directory / "llm.json"
            path.write_text(json.dumps({
                "provider": "custom", "model": "from-file", "base_url": BASE_URL,
                "api_key": FAKE_KEY, "timeout_seconds": 3, "max_tokens": 64, "json_mode": True,
            }), encoding="utf-8")
            loaded = load_config(path, env={})
            provider = TeacherProvider(loaded, transport=ScriptedTransport([]))
            self.assertEqual(provider.config.model, "from-file")
            self.assertEqual(provider.config.timeout_seconds, 3.0)
            self.assertNotIn(FAKE_KEY, repr(provider))
        finally:
            remove_temp_dir(directory)

    def test_the_environment_key_is_used_when_the_file_has_none(self) -> None:
        directory = make_temp_dir("pms-provider-env-")
        try:
            path = directory / "llm.json"
            path.write_text(json.dumps({"provider": "custom", "model": MODEL,
                                        "base_url": BASE_URL, "api_key_env": "MY_TEST_KEY"}),
                            encoding="utf-8")
            loaded = load_config(path, env={"MY_TEST_KEY": FAKE_KEY})
            provider = TeacherProvider(loaded, transport=ScriptedTransport([]))
            self.assertEqual(provider.config.api_key, FAKE_KEY)
            self.assertNotIn(FAKE_KEY, repr(provider))
        finally:
            remove_temp_dir(directory)

    def test_no_credential_is_hardcoded_in_the_module(self) -> None:
        source = PROVIDER_SOURCE.read_text(encoding="utf-8")
        for marker in ("sk-", "Bearer ", "api.deepseek.com", "api.openai.com", "os.environ"):
            self.assertNotIn(marker, source, f"teacher_provider.py contains {marker!r}")


# ==========================================================================
# 6. argument validation happens before any call
# ==========================================================================

class ArgumentValidationTest(ProviderTestCase):
    def test_a_bad_system_prompt_is_refused_before_the_call(self) -> None:
        transport = ScriptedTransport([LLMResponse(text="{}", model=MODEL)])
        provider = self.provider(transport)
        for value in ("", "   ", None, 42):
            with self.assertRaises(ValidationError) as ctx:
                provider.generate(system_prompt=value, user_prompt="U", response_schema={})
            self.assertEqual(ctx.exception.fields, ("system_prompt",))
        self.assertEqual(transport.call_count, 0)

    def test_a_bad_user_prompt_is_refused_before_the_call(self) -> None:
        transport = ScriptedTransport([LLMResponse(text="{}", model=MODEL)])
        provider = self.provider(transport)
        with self.assertRaises(ValidationError) as ctx:
            provider.generate(system_prompt="S", user_prompt="", response_schema={})
        self.assertEqual(ctx.exception.fields, ("user_prompt",))
        self.assertEqual(transport.call_count, 0)

    def test_a_non_mapping_schema_is_refused_before_the_call(self) -> None:
        transport = ScriptedTransport([LLMResponse(text="{}", model=MODEL)])
        provider = self.provider(transport)
        for value in ([], "schema", 42, None):
            with self.assertRaises(ValidationError) as ctx:
                provider.generate(system_prompt="S", user_prompt="U", response_schema=value)
            self.assertEqual(ctx.exception.fields, ("response_schema",))
        self.assertEqual(transport.call_count, 0)


# ==========================================================================
# 7. architecture guards
# ==========================================================================

class ProviderArchitectureTest(unittest.TestCase):
    @property
    def tree(self) -> ast.Module:
        return ast.parse(PROVIDER_SOURCE.read_text(encoding="utf-8"))

    def test_runtime_imports_stay_minimal(self) -> None:
        tree = self.tree
        type_checking_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
                for child in ast.walk(node):
                    if hasattr(child, "lineno"):
                        type_checking_lines.add(child.lineno)
        runtime: set[str] = set()
        deferred: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            else:
                continue
            (deferred if node.lineno in type_checking_lines else runtime).update(names)
        self.assertEqual(runtime, {"__future__", "dataclasses", "typing", "errors", "llm",
                                   "teacher_llm"})
        self.assertEqual(deferred, set())

    def test_forbidden_dependencies_are_absent(self) -> None:
        imported: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("learning", "learning_store", "db", "sqlite3", "store", "models",
                          "retrieval", "web", "cli", "launcher", "teacher_runtime",
                          "teacher_executor", "urllib", "requests", "socket", "http"):
            self.assertNotIn(forbidden, imported, f"teacher_provider.py imports {forbidden}")

    def test_no_database_sql_or_engine_identifier(self) -> None:
        source = PROVIDER_SOURCE.read_text(encoding="utf-8")
        for marker in ("import sqlite3", "sqlite3.", "SELECT ", "INSERT ", "UPDATE ", "DELETE ",
                       "executemany", "commit("):
            self.assertNotIn(marker, source, f"teacher_provider.py contains {marker!r}")

        identifiers = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)}
        for forbidden in ("Database", "LearningRepository", "MemoryRepository", "Memory", "Source",
                          "LearningState", "LearningSession", "LearningService", "transaction",
                          "TeacherRuntime", "TeacherActionExecutor", "TeacherAction", "execute",
                          "record_learning", "finish_session", "abandon_session"):
            self.assertNotIn(forbidden, identifiers, f"teacher_provider.py uses {forbidden}")

    def test_the_only_transport_call_is_send(self) -> None:
        calls = [node.func.attr for node in ast.walk(self.tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertEqual(calls.count("send"), 1)
        for forbidden in ("execute", "record_learning", "record_assessment", "finish_session",
                          "abandon_session", "start_session", "update_state", "transaction",
                          "commit", "extract_json_object", "complete", "complete_json"):
            self.assertNotIn(forbidden, calls, f"teacher_provider.py calls {forbidden}")

    def test_no_llm_semantics_leak_into_the_class_api(self) -> None:
        public = {name for name in vars(TeacherProvider) if not name.startswith("_")}
        self.assertEqual(public, {"from_credentials", "generate"})

    def test_no_http_provider_sdk_or_ui_dependency(self) -> None:
        source = PROVIDER_SOURCE.read_text(encoding="utf-8")
        for marker in ("import openai", "import deepseek", "import anthropic", "fastapi",
                       "FastAPI", "Flask", "websocket", "tool_choice", "stream=True"):
            self.assertNotIn(marker, source, f"teacher_provider.py contains {marker!r}")

    def test_the_provider_holds_no_database_or_engine_object(self) -> None:
        provider = TeacherProvider(config(), transport=ScriptedTransport([]))
        for forbidden in ("repository", "database", "connection", "transaction", "learning",
                          "service", "runtime", "executor", "session", "memory"):
            self.assertFalse(hasattr(provider, forbidden), forbidden)
        self.assertIsInstance(provider.transport, ScriptedTransport)
        self.assertIsInstance(provider.config, LLMConfig)

    def test_the_dependency_direction_is_one_way(self) -> None:
        for name in ("personal_memory/teacher_llm.py", "personal_memory/teacher_runtime.py",
                     "personal_memory/teacher_executor.py", "personal_memory/teacher.py",
                     "personal_memory/learning.py", "personal_memory/llm.py"):
            tree = ast.parse(pathlib.Path(name).read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[-1] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[-1])
            self.assertNotIn("teacher_provider", imported, f"{name} imports the provider")

    def test_the_provider_is_not_a_prompt_or_contract_module(self) -> None:
        """It must not re-implement the prompt, the contract or the executor."""
        identifiers = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)}
        for forbidden in ("TeacherPromptBuilder", "PromptPayload", "TeacherTurnResponse",
                          "TeacherTurnRequest", "TeacherContext", "validate_action_context",
                          "ALLOWED_ACTION_TYPES", "FORBIDDEN_ACTION_TYPES"):
            self.assertNotIn(forbidden, identifiers, f"teacher_provider.py uses {forbidden}")


# ==========================================================================
# 8. integration: provider behind the adapter and the runtime (still offline)
# ==========================================================================

class ProviderIntegrationTest(RepositoryTestCase):
    """Source + 3 Memories + a running session, driven by a wire-faked provider."""

    prefix = "pms-provider-"

    def setUp(self) -> None:
        super().setUp()
        self.learning = LearningRepository(self.database)
        self.service = LearningService(self.learning, self.repo)
        self.source = self.make_source(title="高等数学第一章")
        self.a = self.make_memory(title="极限")
        self.b = self.make_memory(title="连续")
        self.c = self.make_memory(title="切线斜率")
        for memory in (self.a, self.b, self.c):
            self.repo.link(memory.id, self.source.id)
        self.session_id = self.service.start_session(source_id=self.source.id).session.id
        self.context = TeacherContext.from_learning(
            self.service.get_context(self.session_id),
            self.service.get_learning_overview(self.source.id),
        )
        self.request = TeacherTurnRequest(session_id=self.session_id, user_message="我懂了极限",
                                          context=self.context)

    def provider_for(self, *bodies):
        """(provider, opener) with the real HttpTransport and a fake urlopen."""
        opener = RecordingOpener(*bodies)
        return TeacherProvider(config(), transport=HttpTransport(opener=opener)), opener

    def snapshot(self) -> dict:
        with self.database.connection() as conn:
            fts = {name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
                   for name in ("memory_fts_word", "memory_fts_trigram")}
        return {
            "counts": self.repo.counts(),
            "learning": self.learning.counts(),
            "fts": fts,
            "session": self.learning.get_session(self.session_id).as_dict(),
            "states": [self.learning.get_state(m.id).as_dict() for m in (self.a, self.b, self.c)],
        }

    def test_the_adapter_accepts_the_real_provider(self) -> None:
        text = json.dumps({"assistant_message": "好，我们继续。", "actions": []}, ensure_ascii=False)
        provider, opener = self.provider_for(FakeHTTPResponse(envelope(text)))
        adapter = TeacherLLMAdapter(provider)

        response = adapter.generate(self.request)

        self.assertIsInstance(response, TeacherTurnResponse)
        self.assertEqual(response.assistant_message, "好，我们继续。")
        self.assertEqual(response.actions, ())
        body = opener.body_of()
        self.assertIn("<teacher_context>", body["messages"][1]["content"])
        self.assertIn(json.loads(text)["assistant_message"], response.assistant_message)

    def test_the_adapter_and_provider_write_nothing(self) -> None:
        text = json.dumps({"assistant_message": "x", "actions": [{"type": "record_learning"}]})
        provider, _ = self.provider_for(*[FakeHTTPResponse(envelope(text)) for _ in range(3)])
        before = self.snapshot()

        for _ in range(3):                                        # the response *proposes* an action
            TeacherLLMAdapter(provider).generate(self.request)

        self.assertEqual(self.snapshot(), before)

    def test_a_transport_bug_is_reported_as_a_model_error_by_the_adapter(self) -> None:
        """The provider stays honest; the adapter is the documented last-resort net."""
        provider = TeacherProvider(config(),
                                   transport=ScriptedTransport([ValueError("a bug, not a provider")]))
        with self.assertRaises(TeacherModelError) as ctx:
            TeacherLLMAdapter(provider).generate(self.request)
        self.assertIsInstance(ctx.exception.__cause__, ValueError)

    def test_a_full_runtime_turn_with_a_wire_faked_provider(self) -> None:
        text = json.dumps({
            "assistant_message": "先记一次学习，再判断你对连续的理解。",
            "actions": [
                {"type": "record_learning"},
                {"type": "record_assessment", "understanding_level": "partial",
                 "known_aspects": ["连续要求左右极限相等"]},
            ],
        }, ensure_ascii=False)
        provider, opener = self.provider_for(FakeHTTPResponse(envelope(text)))
        runtime = TeacherRuntime(self.service, provider)
        before = self.snapshot()

        result = runtime.turn(session_id=self.session_id, user_message="我懂了极限")

        self.assertEqual(result.response.action_types, ("record_learning", "record_assessment"))
        self.assertEqual(result.context.session.plan_cursor, 1)
        self.assertEqual(result.context.state_for(self.a.id).learn_count, 1)
        self.assertEqual(str(result.context.state_for(self.b.id).understanding_level), "partial")
        self.assertEqual(len(opener.requests), 1)                 # one model call for one turn
        self.assertEqual(self.repo.counts(), before["counts"])     # 1.0 rows untouched
        self.assertEqual(self.snapshot()["fts"], before["fts"])
        self.assertEqual(self.snapshot()["learning"]["learning_sessions"], 1)

    def test_the_adapter_parses_a_fenced_answer_as_illegal_not_as_json(self) -> None:
        """The fence is *not* stripped by the provider: the adapter refuses it."""
        fenced = '```json\n{"assistant_message": "x", "actions": []}\n```'
        provider, _ = self.provider_for(FakeHTTPResponse(envelope(fenced)))
        with self.assertRaises(ValidationError) as ctx:
            TeacherLLMAdapter(provider).generate(self.request)
        self.assertEqual(ctx.exception.fields, ("model_output",))

    def test_a_provider_failure_stops_the_turn_before_any_execution(self) -> None:
        provider, _ = self.provider_for(http_error(500, "server error"))
        runtime = TeacherRuntime(self.service, provider)
        before = self.snapshot()

        with self.assertRaises(TeacherModelError):
            runtime.turn(session_id=self.session_id, user_message="我懂了极限")

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.learning.get_state(self.a.id).learn_count, 0)

    def test_an_illegal_model_answer_stops_the_turn_before_any_execution(self) -> None:
        provider, _ = self.provider_for(FakeHTTPResponse(envelope("我们继续吧")))
        runtime = TeacherRuntime(self.service, provider)
        before = self.snapshot()

        with self.assertRaises(ValidationError):
            runtime.turn(session_id=self.session_id, user_message="我懂了极限")

        self.assertEqual(self.snapshot(), before)

    def test_a_foreign_session_action_is_refused_after_a_real_provider_call(self) -> None:
        text = json.dumps({"assistant_message": "x",
                           "actions": [{"type": "finish_session", "session_id": "lrn_other"}]})
        provider, _ = self.provider_for(FakeHTTPResponse(envelope(text)))
        before = self.snapshot()

        with self.assertRaises(ValidationError) as ctx:
            TeacherRuntime(self.service, provider).turn(session_id=self.session_id,
                                                        user_message="结束吧")

        self.assertEqual(ctx.exception.fields, ("session_id",))
        self.assertEqual(self.snapshot(), before)


# ==========================================================================
# 9. regression: nothing else moved
# ==========================================================================

class Phase2C5RegressionTest(ProviderTestCase):
    def test_the_teacher_contract_and_adapter_are_unchanged(self) -> None:
        self.assertEqual(TEACHER_PROMPT_VERSION, "teacher_prompt_v1")
        self.assertEqual(len(ALLOWED_ACTION_TYPES), 4)
        self.assertEqual(SUPPORTED_ACTION_TYPES, ALLOWED_ACTION_TYPES)
        self.assertEqual({name for name in vars(TeacherLLMAdapter) if not name.startswith("_")},
                         {"generate"})

    def test_the_runtime_api_is_unchanged(self) -> None:
        self.assertEqual({name for name in vars(RuntimeClass) if not name.startswith("_")}, {"turn"})

    def test_the_1_0_llm_layer_still_behaves_as_before(self) -> None:
        """The provider deliberately does *not* use these two 1.0 conveniences."""
        self.assertEqual(extract_json_object('```json\n{"a": 1}\n```'), {"a": 1})   # fence stripping
        self.assertEqual(PROVIDER_DEFAULTS["deepseek"], "https://api.deepseek.com")
        self.assertEqual(LLMConfig().model, "deepseek-flash")
        self.assertEqual(sorted(field.name for field in dataclasses.fields(LLMConfig)),
                         ["api_key", "api_key_env", "backoff_seconds", "base_url", "extra_body",
                          "extra_headers", "json_mode", "max_tokens", "max_transport_retries",
                          "model", "provider", "temperature", "timeout_seconds"])

    def test_the_retrying_client_is_still_available_and_unchanged(self) -> None:
        transport = ScriptedTransport([LLMRequestError("rate limited", status_code=429,
                                                       retryable=True),
                                       LLMResponse(text="{}", model=MODEL)])
        client = LLMClient(LLMConfig(api_key=FAKE_KEY), transport=transport, sleep=lambda _s: None)
        response = client.complete(LLMRequest.of("s", "u"))
        self.assertEqual(response.text, "{}")
        self.assertEqual(transport.call_count, 2)                  # 1.0 still retries; we do not

    def test_the_learning_engine_api_and_schema_are_unchanged(self) -> None:
        self.assertEqual(len(LEARNING_PUBLIC_API), 8)
        self.assertEqual(LEARNING_PUBLIC_API[-1], "abandon_session")

    def test_no_new_table_no_new_module_state(self) -> None:
        from personal_memory.db import MIGRATIONS

        self.assertEqual(SUPPORTED_SCHEMA_VERSION, max(m.version for m in MIGRATIONS))
        self.assertNotIn("provider", [m.name for m in MIGRATIONS])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
