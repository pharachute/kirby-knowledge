"""LLM adapter tests: configuration, redaction, wire format, retries, JSON strictness.

All offline: no provider is contacted (``ScriptedTransport`` / recorded ``urlopen``).
"""

from __future__ import annotations

import json
import unittest

from personal_memory.llm import (
    DEFAULT_API_KEY_ENV,
    HttpTransport,
    LLMClient,
    LLMConfig,
    LLMConfigError,
    LLMRequest,
    LLMRequestError,
    LLMResponseError,
    TruncatedResponseError,
    extract_json_object,
    load_config,
    resolve_api_key,
)

from .llm_fakes import FakeHTTPResponse, RecordingOpener, http_error, mock_client, response, response_for, url_error

FAKE_KEY = "sk-test-not-a-real-key-000000000000"


class ConfigTest(unittest.TestCase):
    def test_defaults_target_the_configured_provider(self) -> None:
        config = load_config(env={}, require_key=False)
        self.assertEqual(config.provider, "deepseek")
        self.assertEqual(config.model, "deepseek-flash")
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertEqual(config.chat_completions_url(), "https://api.deepseek.com/chat/completions")
        self.assertFalse(config.api_key)

    def test_environment_configures_everything_including_the_key(self) -> None:
        config = load_config(
            env={
                "PERSONAL_MEMORY_LLM_PROVIDER": "openai",
                "PERSONAL_MEMORY_LLM_MODEL": "gpt-x",
                "PERSONAL_MEMORY_LLM_BASE_URL": "https://api.example.test/v1",
                "PERSONAL_MEMORY_LLM_API_KEY": FAKE_KEY,
                "PERSONAL_MEMORY_LLM_MAX_TOKENS": "512",
                "PERSONAL_MEMORY_LLM_TIMEOUT": "30",
                "PERSONAL_MEMORY_LLM_JSON_MODE": "false",
            }
        )
        self.assertEqual(config.provider, "openai")
        self.assertEqual(config.model, "gpt-x")
        self.assertEqual(config.base_url, "https://api.example.test/v1")
        self.assertEqual(config.max_tokens, 512)
        self.assertEqual(config.timeout_seconds, 30.0)
        self.assertFalse(config.json_mode)
        self.assertEqual(config.api_key, FAKE_KEY)

    def test_provider_specific_key_variable_is_a_fallback(self) -> None:
        config = load_config(env={"DEEPSEEK_API_KEY": FAKE_KEY})
        self.assertEqual(config.api_key, FAKE_KEY)
        self.assertIsNone(resolve_api_key(config, {}))

    def test_explicit_overrides_beat_the_environment(self) -> None:
        config = load_config(
            env={"PERSONAL_MEMORY_LLM_MODEL": "from-env", "PERSONAL_MEMORY_LLM_API_KEY": FAKE_KEY},
            model="from-argument",
        )
        self.assertEqual(config.model, "from-argument")

    def test_config_file_is_loaded_and_env_fills_the_key(self) -> None:
        from .helpers import make_temp_dir, remove_temp_dir

        tmp = make_temp_dir("pms-cfg-")
        try:
            path = tmp / "llm.json"
            path.write_text(
                json.dumps(
                    {
                        "provider": "custom",
                        "model": "local-model",
                        "base_url": "http://127.0.0.1:9999/v1",
                        "api_key_env": "MY_LOCAL_KEY",
                        "max_tokens": 256,
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(path, env={"MY_LOCAL_KEY": FAKE_KEY})
            self.assertEqual(config.provider, "custom")
            self.assertEqual(config.model, "local-model")
            self.assertEqual(config.base_url, "http://127.0.0.1:9999/v1")
            self.assertEqual(config.max_tokens, 256)
            self.assertEqual(config.api_key, FAKE_KEY)
        finally:
            remove_temp_dir(tmp)

    def test_config_file_rejects_unknown_fields(self) -> None:
        from .helpers import make_temp_dir, remove_temp_dir

        tmp = make_temp_dir("pms-cfg-")
        try:
            path = tmp / "llm.json"
            path.write_text(json.dumps({"model": "x", "api_kay": "typo"}), encoding="utf-8")
            with self.assertRaises(LLMConfigError) as ctx:
                load_config(path, env={}, require_key=False)
            self.assertIn("unknown field", str(ctx.exception))
        finally:
            remove_temp_dir(tmp)

    def test_explicitly_requested_missing_config_file_is_an_error(self) -> None:
        with self.assertRaises(LLMConfigError) as ctx:
            load_config("does-not-exist-llm.json", env={}, require_key=False)
        self.assertIn("not found", str(ctx.exception))

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaises(LLMConfigError):
            load_config(env={}, provider="mystery", require_key=False)

    def test_custom_provider_requires_a_base_url(self) -> None:
        with self.assertRaises(LLMConfigError) as ctx:
            load_config(env={}, provider="custom", require_key=False)
        self.assertIn("base_url", str(ctx.exception))

    def test_missing_key_is_an_explicit_config_error(self) -> None:
        with self.assertRaises(LLMConfigError) as ctx:
            load_config(env={})
        message = str(ctx.exception)
        self.assertIn(DEFAULT_API_KEY_ENV, message)
        self.assertIn("DEEPSEEK_API_KEY", message)

    def test_invalid_numeric_config_is_rejected(self) -> None:
        with self.assertRaises(LLMConfigError):
            load_config(env={}, require_key=False, max_tokens="many")


class RedactionTest(unittest.TestCase):
    """The key must never appear in logs, reprs or error messages."""

    def test_repr_summary_and_public_dict_hide_the_key(self) -> None:
        config = LLMConfig(api_key=FAKE_KEY, model="m", base_url="https://x.test")
        for rendered in (repr(config), str(config), config.safe_summary(), json.dumps(config.as_public_dict())):
            self.assertNotIn(FAKE_KEY, rendered)
        self.assertIn("api_key=<set", config.safe_summary())
        self.assertTrue(config.as_public_dict()["api_key_present"])

    def test_http_error_message_does_not_leak_the_key(self) -> None:
        opener = RecordingOpener(http_error(401, "Incorrect API key provided"))
        client = LLMClient(
            LLMConfig(api_key=FAKE_KEY, base_url="https://x.test"),
            transport=HttpTransport(opener=opener),
            sleep=lambda _s: None,
        )
        with self.assertRaises(LLMRequestError) as ctx:
            client.complete(LLMRequest.of("s", "u"))
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertFalse(ctx.exception.retryable)


class JsonExtractionTest(unittest.TestCase):
    def test_plain_object_is_accepted(self) -> None:
        self.assertEqual(extract_json_object('{"a": 1}'), {"a": 1})

    def test_fenced_block_is_accepted(self) -> None:
        self.assertEqual(extract_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json_object('```\n{"a": 1}\n```'), {"a": 1})

    def test_prose_around_json_is_rejected(self) -> None:
        with self.assertRaises(LLMResponseError):
            extract_json_object('Here is the result: {"a": 1} — hope it helps!')

    def test_array_and_scalars_are_rejected(self) -> None:
        for text in ("[1, 2]", '"text"', "42", "null"):
            with self.subTest(text=text), self.assertRaises(LLMResponseError):
                extract_json_object(text)

    def test_empty_and_truncated_answers_are_rejected(self) -> None:
        for text in ("", "   ", '{"a": '):
            with self.subTest(text=text), self.assertRaises(LLMResponseError):
                extract_json_object(text)


class HttpTransportWireTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "id": "req-1",
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": '{"ok": true}', "reasoning_content": "thinking..."},
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }

    def test_request_is_openai_compatible(self) -> None:
        opener = RecordingOpener(FakeHTTPResponse(self.payload))
        config = LLMConfig(
            api_key=FAKE_KEY, model="deepseek-flash", base_url="https://api.deepseek.com",
            max_tokens=321, temperature=0.25, json_mode=True,
        )
        transport = HttpTransport(opener=opener)
        result = transport.send(LLMRequest.of("system text", "user text"), config)

        self.assertEqual(opener.requests[0].full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(opener.requests[0].get_header("Authorization"), f"Bearer {FAKE_KEY}")
        self.assertIn("application/json", opener.requests[0].get_header("Content-type"))
        body = opener.body_of()
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["max_tokens"], 321)
        self.assertEqual(body["temperature"], 0.25)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertFalse(body["stream"])
        self.assertEqual(opener.timeouts[0], config.timeout_seconds)
        self.assertEqual(result.text, '{"ok": true}')
        self.assertEqual(result.reasoning_text, "thinking...")
        self.assertEqual(result.total_tokens, 12)
        self.assertEqual(result.finish_reason, "stop")

    def test_json_mode_can_be_disabled(self) -> None:
        opener = RecordingOpener(FakeHTTPResponse(self.payload))
        HttpTransport(opener=opener).send(
            LLMRequest.of("s", "u"), LLMConfig(api_key="k", base_url="https://x.test", json_mode=False)
        )
        self.assertNotIn("response_format", opener.body_of())

    def test_truncated_answer_is_never_returned(self) -> None:
        payload = json.loads(json.dumps(self.payload))
        payload["choices"][0]["finish_reason"] = "length"
        with self.assertRaises(TruncatedResponseError):
            HttpTransport(opener=RecordingOpener(FakeHTTPResponse(payload))).send(
                LLMRequest.of("s", "u"), LLMConfig(api_key="k", base_url="https://x.test")
            )

    def test_non_json_body_is_a_response_error(self) -> None:
        with self.assertRaises(LLMResponseError):
            HttpTransport(opener=RecordingOpener(FakeHTTPResponse(None, raw="<html>oops</html>"))).send(
                LLMRequest.of("s", "u"), LLMConfig(api_key="k", base_url="https://x.test")
            )

    def test_malformed_envelope_is_rejected(self) -> None:
        for payload in ({}, {"choices": []}, {"choices": [{"message": {}}]}):
            with self.subTest(payload=payload), self.assertRaises(LLMResponseError):
                HttpTransport.parse_envelope(payload)


class ClientRetryTest(unittest.TestCase):
    def test_retryable_transport_failure_is_retried_then_succeeds(self) -> None:
        sleeps: list[float] = []
        client = mock_client(
            LLMRequestError("boom", retryable=True),
            response_for({"ok": True}),
        )
        client._sleep = sleeps.append  # type: ignore[attr-defined]
        result = client.complete_json(LLMRequest.of("s", "u"))
        self.assertEqual(result[0], {"ok": True})
        self.assertEqual(client.attempts, 2)
        self.assertEqual(len(sleeps), 1)

    def test_non_retryable_failure_is_not_retried(self) -> None:
        client = mock_client(LLMRequestError("bad key", status_code=401, retryable=False))
        with self.assertRaises(LLMRequestError):
            client.complete(LLMRequest.of("s", "u"))
        self.assertEqual(client.attempts, 1)

    def test_retryable_failure_exhausts_retries_and_raises(self) -> None:
        config = LLMConfig(api_key="k", base_url="https://x.test", max_transport_retries=2)
        sleeps: list[float] = []
        client = mock_client(
            LLMRequestError("timeout", retryable=True),
            LLMRequestError("timeout", retryable=True),
            LLMRequestError("timeout", retryable=True),
            config=config,
        )
        client._sleep = sleeps.append  # type: ignore[attr-defined]
        with self.assertRaises(LLMRequestError):
            client.complete(LLMRequest.of("s", "u"))
        self.assertEqual(client.attempts, 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_url_error_and_http_5xx_are_retryable(self) -> None:
        opener = RecordingOpener(url_error("no route"), http_error(503, "unavailable"), FakeHTTPResponse(
            {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "model": "m"}
        ))
        config = LLMConfig(api_key="k", base_url="https://x.test", max_transport_retries=2)
        client = LLMClient(config, transport=HttpTransport(opener=opener), sleep=lambda _s: None)
        payload, _ = client.complete_json(LLMRequest.of("s", "u"))
        self.assertEqual(payload, {})
        self.assertEqual(client.attempts, 3)

    def test_http_429_is_retryable_and_400_is_not(self) -> None:
        for status, expected_retryable in ((429, True), (400, False)):
            with self.subTest(status=status):
                # a retryable status is attempted 1 + max_transport_retries times
                attempts = 3 if expected_retryable else 1
                opener = RecordingOpener(*[http_error(status, "nope")] * attempts)
                client = LLMClient(
                    LLMConfig(api_key="k", base_url="https://x.test", max_transport_retries=2),
                    transport=HttpTransport(opener=opener),
                    sleep=lambda _s: None,
                )
                with self.assertRaises(LLMRequestError) as ctx:
                    client.complete(LLMRequest.of("s", "u"))
                self.assertEqual(ctx.exception.retryable, expected_retryable)

    def test_complete_json_rejects_non_object_answers(self) -> None:
        client = mock_client(response_for('[1, 2, 3]'))
        with self.assertRaises(LLMResponseError):
            client.complete_json(LLMRequest.of("s", "u"))

    def test_truncated_answer_reaches_the_caller_as_a_response_error(self) -> None:
        client = mock_client(response_for('{"partial": ', finish_reason="length"))
        with self.assertRaises(LLMResponseError):
            client.complete_json(LLMRequest.of("s", "u"))


class RequestTest(unittest.TestCase):
    def test_retry_suffix_is_appended_to_the_user_message(self) -> None:
        request = LLMRequest.of("system", "user")
        updated = request.with_user_suffix("\nMORE")
        self.assertEqual(updated.messages[0].content, "system")
        self.assertEqual(updated.messages[1].content, "user\nMORE")

    def test_metadata_is_carried(self) -> None:
        request = LLMRequest.of("s", "u", purpose="x", prompt_version="v9")
        self.assertEqual(dict(request.metadata), {"purpose": "x", "prompt_version": "v9"})



class KeyPrecedenceAndLeakTest(unittest.TestCase):
    """Regression tests for two defects found by independent review:

    * a key in the config file used to beat the environment (breaking rotation);
    * a provider echoing our Authorization header put the key into the error text.
    """

    def _write_config(self, tmp, payload):
        path = tmp / "llm.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_environment_beats_the_config_file(self) -> None:
        from .helpers import make_temp_dir, remove_temp_dir

        tmp = make_temp_dir("pms-key-")
        try:
            path = self._write_config(tmp, {"api_key": "FILE-KEY", "model": "m"})
            config = load_config(path, env={"PERSONAL_MEMORY_LLM_API_KEY": "ENV-KEY"})
            self.assertEqual(config.api_key, "ENV-KEY")

            config = load_config(path, env={"DEEPSEEK_API_KEY": "ENV-DEEPSEEK-KEY"})
            self.assertEqual(config.api_key, "ENV-DEEPSEEK-KEY")

            config = load_config(path, env={})
            self.assertEqual(config.api_key, "FILE-KEY")

            config = load_config(path, env={"PERSONAL_MEMORY_LLM_API_KEY": "ENV-KEY"}, api_key="EXPLICIT-KEY")
            self.assertEqual(config.api_key, "EXPLICIT-KEY")
        finally:
            remove_temp_dir(tmp)

    def test_provider_error_body_cannot_echo_the_key(self) -> None:
        echoed = json.dumps({"error": {"message": f"bad credentials header was: Bearer {FAKE_KEY}"}})
        opener = RecordingOpener(http_error(401, "unauthorized", body=echoed))
        client = LLMClient(
            LLMConfig(api_key=FAKE_KEY, base_url="https://x.test"),
            transport=HttpTransport(opener=opener),
            sleep=lambda _s: None,
        )
        with self.assertRaises(LLMRequestError) as ctx:
            client.complete(LLMRequest.of("s", "u"))
        message = str(ctx.exception)
        self.assertNotIn(FAKE_KEY, message)
        self.assertNotIn("Bearer " + FAKE_KEY, message)
        self.assertIn("<redacted>", message)

    def test_non_json_error_body_is_scrubbed_too(self) -> None:
        body = f"<html>proxy log: Authorization: Bearer {FAKE_KEY}</html>"
        opener = RecordingOpener(FakeHTTPResponse(None, raw=body))
        client = LLMClient(
            LLMConfig(api_key=FAKE_KEY, base_url="https://x.test"), transport=HttpTransport(opener=opener)
        )
        with self.assertRaises(LLMResponseError) as ctx:
            client.complete(LLMRequest.of("s", "u"))
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertIn("<redacted>", str(ctx.exception))

    def test_redact_secrets_helper(self) -> None:
        from personal_memory.llm import redact_secrets

        self.assertEqual(redact_secrets(f"key={FAKE_KEY}", FAKE_KEY), "key=<redacted>")
        self.assertEqual(
            redact_secrets("Authorization: Bearer abcdefghijklmnop", "other"),
            "Authorization: Bearer <redacted>",
        )
        self.assertEqual(redact_secrets("nothing to hide", "secret"), "nothing to hide")


class TransportErrorHygieneTest(unittest.TestCase):
    """Regression tests for gaps found by a second independent review."""

    def test_socket_errors_are_wrapped_as_retryable_transport_errors(self) -> None:
        import errno

        opener = RecordingOpener(ConnectionAbortedError(errno.WSAECONNABORTED, "connection aborted"))
        client = LLMClient(
            LLMConfig(api_key=FAKE_KEY, base_url="https://x.test", max_transport_retries=0),
            transport=HttpTransport(opener=opener),
            sleep=lambda _s: None,
        )
        with self.assertRaises(LLMRequestError) as ctx:
            client.complete(LLMRequest.of("s", "u"))
        self.assertIsInstance(ctx.exception, LLMRequestError)  # never a bare OSError
        self.assertTrue(ctx.exception.retryable)
        self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_urlerror_reason_is_scrubbed(self) -> None:
        opener = RecordingOpener(url_error(f"Tunnel connection failed: 403 Bearer {FAKE_KEY}"))
        client = LLMClient(
            LLMConfig(api_key=FAKE_KEY, base_url="https://x.test", max_transport_retries=0),
            transport=HttpTransport(opener=opener),
        )
        with self.assertRaises(LLMRequestError) as ctx:
            client.complete(LLMRequest.of("s", "u"))
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertIn("<redacted>", str(ctx.exception))

    def test_model_answer_cannot_echo_the_key(self) -> None:
        """The provider's message.content must be scrubbed on the JSON-validation path."""
        client = mock_client(response(f"Bearer {FAKE_KEY} nope"))
        with self.assertRaises(LLMResponseError) as ctx:
            client.complete_json(LLMRequest.of("s", "u"))
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertIn("<redacted>", str(ctx.exception))

    def test_base_url_is_scrubbed_from_connection_errors(self) -> None:
        opener = RecordingOpener(url_error("no route"))
        client = LLMClient(
            LLMConfig(api_key="k", base_url=f"https://host.test/{FAKE_KEY}", max_transport_retries=0),
            transport=HttpTransport(opener=opener),
        )
        with self.assertRaises(LLMRequestError) as ctx:
            client.complete(LLMRequest.of("s", "u"))
        self.assertNotIn(FAKE_KEY, str(ctx.exception))


class RedirectSafetyTest(unittest.TestCase):
    """A redirect must never forward the Authorization header to another host."""

    def test_redirect_is_refused_and_the_key_never_reaches_the_sink(self) -> None:
        import http.server
        import threading

        received: list[dict] = []

        class SinkHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # pragma: no cover - must never be called
                received.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):  # silence the test output
                return

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                # drain the request body first: answering and closing early makes
                # http.client raise ConnectionAbortedError on Windows
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink_port}/stolen")
                self.end_headers()

            def log_message(self, *args):
                return

        sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
        sink_port = sink.server_address[1]
        redirector = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        threads = []
        try:
            for server in (sink, redirector):
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                threads.append(thread)
        except OSError as exc:  # pragma: no cover - environment cannot bind sockets
            sink.server_close()
            redirector.server_close()
            self.skipTest(f"cannot bind a local socket: {exc}")

        try:
            client = LLMClient(
                LLMConfig(api_key=FAKE_KEY, base_url=f"http://127.0.0.1:{redirector.server_address[1]}"),
                sleep=lambda _s: None,
            )
            with self.assertRaises(LLMRequestError) as ctx:
                client.complete(LLMRequest.of("s", "u"))
            self.assertIn("redirect", str(ctx.exception).lower())
            self.assertNotIn(FAKE_KEY, str(ctx.exception))
            self.assertEqual(received, [], "the credential must never be replayed to the redirect target")
        finally:
            for server in (redirector, sink):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
