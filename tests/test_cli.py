"""CLI + demo tests (``python -m personal_memory <command>``)."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from personal_memory import MIGRATIONS, SUPPORTED_SCHEMA_VERSION
from personal_memory.cli import main
from personal_memory.db import ENV_DB_PATH, Database, resolve_db_path
from personal_memory.demo import run_demo
from personal_memory.store import MemoryRepository

from .helpers import TempDirTestCase


class CliTest(TempDirTestCase):
    prefix = "pms-cli-"

    def setUp(self) -> None:
        super().setUp()
        self.db_path = self.tmpdir / "cli.db"

    def run_cli(self, *argv: str) -> dict:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = main(["--db", str(self.db_path), *argv])
        self.assertEqual(exit_code, 0)
        return json.loads(buffer.getvalue())

    def test_init_then_info(self) -> None:
        init_payload = self.run_cli("init")
        self.assertEqual(init_payload["schema_version"], SUPPORTED_SCHEMA_VERSION)
        self.assertEqual(init_payload["applied_count"], len(MIGRATIONS))
        self.assertEqual(
            init_payload["applied"],
            [{"version": m.version, "name": m.name} for m in MIGRATIONS],
        )

        info = self.run_cli("info")
        self.assertTrue(info["exists"])
        self.assertEqual(info["schema_version"], SUPPORTED_SCHEMA_VERSION)
        self.assertEqual(info["counts"]["schema_migrations"], len(MIGRATIONS))
        self.assertEqual(
            {k: v for k, v in info["counts"].items() if k in {"sources", "memories", "memory_sources"}},
            {"sources": 0, "memories": 0, "memory_sources": 0},
        )
        for table in ("sources", "memories", "memory_sources"):
            self.assertIn(table, info["tables"])
            self.assertIn("CREATE TABLE", info["tables"][table])

    def test_init_is_idempotent(self) -> None:
        self.run_cli("init")
        second = self.run_cli("init")
        self.assertEqual(second["applied_count"], 0)
        self.assertEqual(second["schema_version"], SUPPORTED_SCHEMA_VERSION)

    def test_migrate_is_an_alias_of_init(self) -> None:
        payload = self.run_cli("migrate")
        self.assertEqual(payload["applied_count"], len(MIGRATIONS))
        self.assertTrue(self.db_path.exists())

    def test_version_command(self) -> None:
        payload = self.run_cli("version")
        self.assertEqual(payload["schema_version"], SUPPORTED_SCHEMA_VERSION)
        # version bumped by KB 1.0 Phase 4 (URL / Web importer)
        self.assertEqual(payload["package_version"], "0.9.0")
        self.assertEqual(payload["prompt_version"], "memory-formation-v1")
        self.assertEqual(payload["conflict_prompt_version"], "memory-conflict-v1")
        self.assertIn("memory_system", payload)
        self.assertIn("phase-4", payload["memory_system"])
        self.assertIn("url importer", payload["phase"])

    def test_db_option_also_works_after_the_subcommand(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = main(["init", "--db", str(self.db_path)])
        self.assertEqual(exit_code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["applied_count"], len(MIGRATIONS))
        self.assertTrue(self.db_path.exists())

    def test_demo_creates_and_reads_a_full_dataset(self) -> None:
        payload = self.run_cli("demo")
        self.assertEqual(payload["counts"], {"sources": 2, "memories": 4, "memory_sources": 5})
        self.assertEqual(payload["schema_version"], SUPPORTED_SCHEMA_VERSION)
        self.assertTrue(payload["duplicate_hash_check"]["already_known"])
        self.assertEqual(payload["duplicate_hash_check"]["existing_source_id"], "src_demo_text_01")
        self.assertTrue(payload["persistence_check"]["identical"])
        # one source -> many memories, and one memory -> many sources, both observed
        self.assertEqual(len(payload["source_to_memories"]["src_demo_text_01"]), 3)
        self.assertEqual(len(payload["memory_to_sources"]["mem_demo_event_01"]), 2)

    def test_demo_is_idempotent(self) -> None:
        first = self.run_cli("demo")
        second = self.run_cli("demo")
        self.assertEqual(first["counts"], second["counts"])
        self.assertEqual(len(second["sources"]), 2)
        self.assertEqual(len(second["memories"]), 4)

    def test_demo_reset_starts_from_scratch(self) -> None:
        self.run_cli("demo")
        payload = self.run_cli("demo", "--reset")
        self.assertEqual(payload["counts"], {"sources": 2, "memories": 4, "memory_sources": 5})
        self.assertEqual(
            payload["migrations_applied"],
            [{"version": m.version, "name": m.name} for m in MIGRATIONS],
        )


class Phase4CommandSurfaceTest(unittest.TestCase):
    """Phase 4 CLI surface: the lifecycle commands and the quality-gate switch parse."""

    def test_lifecycle_commands_are_registered(self) -> None:
        from personal_memory.cli import build_parser

        parser = build_parser()
        choices = sorted(parser._subparsers._group_actions[0].choices)
        for command in ("archive", "activate", "restore", "update", "delete", "pending"):
            self.assertIn(command, choices)

    def test_quality_check_can_be_disabled(self) -> None:
        from personal_memory.cli import build_parser

        parser = build_parser()
        default = parser.parse_args(["form", "--text", "x"])
        disabled = parser.parse_args(["form", "--text", "x", "--no-quality-check"])
        self.assertFalse(default.no_quality_check)
        self.assertTrue(disabled.no_quality_check)

    def test_update_flags_map_to_fields(self) -> None:
        from personal_memory.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(
            ["update", "mem_x", "--title", "t", "--tags", "a,b", "--importance", "0.9", "--status", "archived"]
        )
        self.assertEqual(args.memory_id, "mem_x")
        self.assertEqual(args.title, "t")
        self.assertEqual(args.tags, "a,b")
        self.assertAlmostEqual(args.importance, 0.9)
        self.assertEqual(args.status, "archived")


class CaptureCommandTest(TempDirTestCase):
    """KB 1.0 Phase 1 CLI surface (offline paths; the real call is the acceptance script)."""

    prefix = "pms-capture-cli-"

    def setUp(self) -> None:
        super().setUp()
        self.db_path = self.tmpdir / "capture.db"

    def test_capture_without_credentials_fails_cleanly_and_writes_nothing(self) -> None:
        import io
        import os
        from contextlib import redirect_stdout

        from personal_memory.cli import main

        buffer = io.StringIO()
        with mock.patch.dict(
            os.environ, {"PERSONAL_MEMORY_LLM_API_KEY": "", "DEEPSEEK_API_KEY": ""}, clear=False
        ):
            with redirect_stdout(buffer):
                code = main(["--db", str(self.db_path), "capture", "RAG 是检索增强生成"])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_type"], "LLMConfigError")
        self.assertEqual(
            MemoryRepository(Database(self.db_path)).counts(),
            {"sources": 0, "memories": 0, "memory_sources": 0},
        )

    def test_capture_flags_parse(self) -> None:
        from personal_memory.cli import build_parser

        args = build_parser().parse_args(
            [
                "capture",
                "正文",
                "--title",
                "T",
                "--source-type",
                "article",
                "--url",
                "https://example.com/x",
                "--captured-from",
                "cli",
                "--json",
            ]
        )
        self.assertEqual(args.command, "capture")
        self.assertEqual(args.content, "正文")
        self.assertEqual(args.title, "T")
        self.assertEqual(args.source_type, "article")
        self.assertEqual(args.url, "https://example.com/x")
        self.assertEqual(args.captured_from, "cli")
        self.assertTrue(args.json)
        self.assertFalse(args.no_quality_check)
        self.assertFalse(args.dry_run)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["capture", "正文", "--source-type", "pdf"])

    def test_capture_stdin_and_dry_run_flags(self) -> None:
        from personal_memory.cli import build_parser

        args = build_parser().parse_args(["capture", "--stdin", "--dry-run", "--no-quality-check"])
        self.assertTrue(args.stdin)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.no_quality_check)
        self.assertIsNone(args.content)


class FormCommandTest(TempDirTestCase):
    """Phase 2 CLI: offline paths only (a real formation call is the E2E script)."""

    prefix = "pms-form-"

    def setUp(self) -> None:
        super().setUp()
        self.db_path = self.tmpdir / "form.db"

    def run_form(self, *extra: str, env: dict | None = None):
        import io
        import os
        from contextlib import redirect_stdout

        from personal_memory.cli import main

        patch_env = {
            "PERSONAL_MEMORY_LLM_API_KEY": "",
            "DEEPSEEK_API_KEY": "",
            **(env or {}),
        }
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, patch_env, clear=False):
            with redirect_stdout(buffer):
                code = main(["--db", str(self.db_path), "form", "--text", "一段测试输入", *extra])
        import json

        return code, json.loads(buffer.getvalue())

    def test_form_without_credentials_fails_cleanly_and_writes_nothing(self) -> None:
        code, payload = self.run_form()
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_type"], "LLMConfigError")
        self.assertIn("no API key configured", payload["error"])
        repository = MemoryRepository(Database(self.db_path))
        self.assertEqual(repository.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_form_with_empty_input_reports_a_validation_error(self) -> None:
        import io
        import os
        from contextlib import redirect_stdout

        from personal_memory.cli import main

        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {"PERSONAL_MEMORY_LLM_API_KEY": "test-key"}, clear=False):
            with redirect_stdout(buffer):
                code = main(["--db", str(self.db_path), "form", "--text", "   "])
        import json

        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(payload["error_type"], "ExtractionValidationError")
        self.assertEqual(MemoryRepository(Database(self.db_path)).counts()["memories"], 0)


class ImportFileCommandTest(TempDirTestCase):
    """KB 1.0 Phase 2 CLI surface: `import-file` offline paths."""

    prefix = "pms-import-cli-"

    def setUp(self) -> None:
        super().setUp()
        self.db_path = self.tmpdir / "import.db"

    def run_import(self, *argv: str):
        import io
        import os
        from contextlib import redirect_stdout

        from personal_memory.cli import main

        patch_env = {"PERSONAL_MEMORY_LLM_API_KEY": "", "DEEPSEEK_API_KEY": ""}
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, patch_env, clear=False):
            with redirect_stdout(buffer):
                code = main(["--db", str(self.db_path), "import-file", *argv])
        import json

        return code, json.loads(buffer.getvalue())

    def test_missing_file_fails_cleanly_without_creating_a_database(self) -> None:
        code, payload = self.run_import(str(self.tmpdir / "nope.txt"))

        self.assertEqual(code, 3)
        self.assertEqual(payload["error_type"], "FileMissingError")
        self.assertIn("file not found", payload["error"])
        # the file is validated before any database is opened
        self.assertFalse(self.db_path.exists())

    def test_directory_unsupported_and_empty_files_fail_with_typed_errors(self) -> None:
        directory = self.tmpdir / "folder"
        directory.mkdir()
        code, payload = self.run_import(str(directory))
        self.assertEqual((code, payload["error_type"]), (3, "NotAFileError"))

        unsupported = self.tmpdir / "notes.pdf"
        unsupported.write_text("x", encoding="utf-8")
        code, payload = self.run_import(str(unsupported))
        self.assertEqual((code, payload["error_type"]), (3, "UnsupportedFileTypeError"))

        empty = self.tmpdir / "empty.txt"
        empty.write_text("   ", encoding="utf-8")
        code, payload = self.run_import(str(empty))
        self.assertEqual((code, payload["error_type"]), (3, "EmptyFileError"))

        self.assertFalse(self.db_path.exists())

    def test_valid_file_without_credentials_fails_cleanly_and_writes_nothing(self) -> None:
        path = self.tmpdir / "notes.txt"
        path.write_text("RAG 是检索增强生成。", encoding="utf-8")

        code, payload = self.run_import(str(path))

        self.assertEqual(code, 2)
        self.assertEqual(payload["error_type"], "LLMConfigError")
        self.assertFalse(self.db_path.exists())

    def test_import_file_flags_parse(self) -> None:
        from personal_memory.cli import build_parser
        from personal_memory.importers import MAX_FILE_BYTES

        args = build_parser().parse_args(
            [
                "import-file",
                "notes/rag.md",
                "--title",
                "T",
                "--source-type",
                "text",
                "--max-bytes",
                "2048",
                "--dry-run",
                "--no-quality-check",
                "--json",
            ]
        )
        self.assertEqual(args.command, "import-file")
        self.assertEqual(args.path, "notes/rag.md")
        self.assertEqual(args.title, "T")
        self.assertEqual(args.source_type, "text")
        self.assertEqual(args.max_bytes, 2048)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.no_quality_check)
        self.assertTrue(args.json)

        default = build_parser().parse_args(["import-file", "notes/rag.md"])
        self.assertEqual(default.source_type, "file")
        self.assertEqual(default.max_bytes, MAX_FILE_BYTES)
        self.assertFalse(default.dry_run)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["import-file", "notes/rag.md", "--source-type", "pdf"])


class ImportChatCommandTest(TempDirTestCase):
    """KB 1.0 Phase 3 CLI surface: `import-chat` offline paths."""

    prefix = "pms-chat-cli-"

    def setUp(self) -> None:
        super().setUp()
        self.db_path = self.tmpdir / "chat.db"

    def run_chat(self, *argv: str):
        import io
        import os
        from contextlib import redirect_stdout

        from personal_memory.cli import main

        patch_env = {"PERSONAL_MEMORY_LLM_API_KEY": "", "DEEPSEEK_API_KEY": ""}
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, patch_env, clear=False):
            with redirect_stdout(buffer):
                code = main(["--db", str(self.db_path), "import-chat", *argv])
        import json

        return code, json.loads(buffer.getvalue())

    def test_file_errors_are_typed_and_create_no_database(self) -> None:
        missing = self.tmpdir / "nope.txt"
        code, payload = self.run_chat(str(missing))
        self.assertEqual((code, payload["error_type"]), (3, "FileMissingError"))

        unsupported = self.tmpdir / "chat.pdf"
        unsupported.write_text("not a chat", encoding="utf-8")
        code, payload = self.run_chat(str(unsupported))
        self.assertEqual((code, payload["error_type"]), (3, "UnsupportedFileTypeError"))

        broken = self.tmpdir / "broken.json"
        broken.write_text("{ not json ", encoding="utf-8")
        code, payload = self.run_chat(str(broken))
        self.assertEqual((code, payload["error_type"]), (3, "ChatParseError"))

        roleless = self.tmpdir / "norole.txt"
        roleless.write_text("这里没有任何角色标记。", encoding="utf-8")
        code, payload = self.run_chat(str(roleless))
        self.assertEqual((code, payload["error_type"]), (3, "ChatParseError"))

        no_roles = self.tmpdir / "unknown-role.txt"
        no_roles.write_text("[Moderator]\n欢迎。\n", encoding="utf-8")
        code, payload = self.run_chat(str(no_roles))
        self.assertEqual((code, payload["error_type"]), (3, "UnsupportedChatRoleError"))

        # the file is validated before any database is opened
        self.assertFalse(self.db_path.exists())

    def test_valid_chat_without_credentials_fails_cleanly_and_writes_nothing(self) -> None:
        path = self.tmpdir / "conv.txt"
        path.write_text("[User]\n你好。\n\n[Assistant]\n你好，我能帮你。\n", encoding="utf-8")

        code, payload = self.run_chat(str(path))

        self.assertEqual(code, 2)
        self.assertEqual(payload["error_type"], "LLMConfigError")
        self.assertFalse(self.db_path.exists())

    def test_import_chat_flags_parse(self) -> None:
        from personal_memory.cli import build_parser
        from personal_memory.importers import MAX_CHAT_BYTES

        args = build_parser().parse_args(
            [
                "import-chat",
                "conv.json",
                "--title",
                "T",
                "--provider",
                "some-provider",
                "--conversation-id",
                "conv-9",
                "--format",
                "json",
                "--max-bytes",
                "4096",
                "--dry-run",
                "--no-quality-check",
                "--keep-source",
                "always",
                "--json",
            ]
        )
        self.assertEqual(args.command, "import-chat")
        self.assertEqual(args.path, "conv.json")
        self.assertEqual(args.title, "T")
        self.assertEqual(args.provider, "some-provider")
        self.assertEqual(args.conversation_id, "conv-9")
        self.assertEqual(args.chat_format, "json")
        self.assertEqual(args.max_bytes, 4096)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.no_quality_check)
        self.assertTrue(args.json)
        self.assertEqual(args.keep_source, "always")
        self.assertFalse(hasattr(args, "source_type"))

        default = build_parser().parse_args(["import-chat", "conv.txt"])
        self.assertEqual(default.chat_format, "auto")
        self.assertEqual(default.max_bytes, MAX_CHAT_BYTES)
        self.assertIsNone(default.provider)
        self.assertEqual(default.keep_source, "when_required")
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["import-chat", "conv.txt", "--format", "xml"])


class WebCommandTest(unittest.TestCase):
    """KB 1.0 MVP: the `web` command must exist and default to loopback only."""

    def test_web_flags_parse_with_localhost_defaults(self) -> None:
        from personal_memory.cli import build_parser
        from personal_memory.importers import MAX_FILE_BYTES

        args = build_parser().parse_args(["web", "--db", "data/memory.db", "--port", "8765"])
        self.assertEqual(args.command, "web")
        self.assertEqual(args.db, "data/memory.db")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.max_bytes, MAX_FILE_BYTES)
        self.assertFalse(args.no_quality_check)

        ephemeral = build_parser().parse_args(["web", "--port", "0"])
        self.assertEqual(ephemeral.port, 0)
        self.assertEqual(ephemeral.host, "127.0.0.1")

    def test_web_help_mentions_the_local_only_default(self) -> None:
        import io
        from contextlib import redirect_stdout

        from personal_memory.cli import main

        buffer = io.StringIO()
        with self.assertRaises(SystemExit):
            with redirect_stdout(buffer):
                main(["web", "--help"])
        self.assertIn("127.0.0.1", buffer.getvalue())


class ImportUrlCommandTest(TempDirTestCase):
    """KB 1.0 Phase 4 CLI surface: `import-url` validation before any network/DB work."""

    prefix = "pms-url-cli-"

    def setUp(self) -> None:
        super().setUp()
        self.db_path = self.tmpdir / "url.db"

    def run_url(self, *argv: str):
        import io
        import os
        from contextlib import redirect_stdout

        from personal_memory.cli import main

        patch_env = {"PERSONAL_MEMORY_LLM_API_KEY": "", "DEEPSEEK_API_KEY": ""}
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, patch_env, clear=False):
            with redirect_stdout(buffer):
                code = main(["--db", str(self.db_path), "import-url", *argv])
        import json

        return code, json.loads(buffer.getvalue())

    def test_blocked_and_invalid_urls_fail_before_anything_else(self) -> None:
        cases = {
            "http://127.0.0.1:8765/admin": "BlockedUrlError",
            "http://169.254.169.254/latest/meta-data/": "BlockedUrlError",
            "http://10.0.0.5/": "BlockedUrlError",
            "https://localhost/": "BlockedUrlError",
            "https://service.internal/x": "BlockedUrlError",
            "file:///etc/passwd": "InvalidUrlError",
            "ftp://example.com/x": "InvalidUrlError",
            "http://user:pw@example.com/": "InvalidUrlError",
            "not-a-url": "InvalidUrlError",
        }
        for url, error_type in cases.items():
            code, payload = self.run_url(url)
            self.assertEqual(code, 3, url)
            self.assertEqual(payload["error_type"], error_type, url)
            self.assertEqual(payload["url"], url)

        # nothing was created and no model was contacted
        self.assertFalse(self.db_path.exists())

    def test_import_url_flags_parse(self) -> None:
        from personal_memory.cli import build_parser
        from personal_memory.importers.web import DEFAULT_TIMEOUT_SECONDS, MAX_WEB_BYTES, MIN_CONTENT_CHARS

        args = build_parser().parse_args(
            [
                "import-url",
                "https://example.com/a",
                "--title",
                "T",
                "--max-bytes",
                "4096",
                "--timeout",
                "5",
                "--min-chars",
                "100",
                "--dry-run",
                "--no-quality-check",
                "--json",
            ]
        )
        self.assertEqual(args.command, "import-url")
        self.assertEqual(args.url, "https://example.com/a")
        self.assertEqual((args.title, args.max_bytes, args.timeout, args.min_chars), ("T", 4096, 5.0, 100))
        self.assertTrue(args.dry_run)
        self.assertTrue(args.no_quality_check)
        self.assertTrue(args.json)

        default = build_parser().parse_args(["import-url", "https://example.com/a"])
        self.assertEqual(default.max_bytes, MAX_WEB_BYTES)
        self.assertEqual(default.timeout, DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(default.min_chars, MIN_CONTENT_CHARS)


class DbPathResolutionTest(unittest.TestCase):
    def test_explicit_path_wins(self) -> None:
        self.assertEqual(resolve_db_path("custom.db"), Path("custom.db"))

    def test_env_variable_is_used_when_no_argument(self) -> None:
        with mock.patch.dict(os.environ, {ENV_DB_PATH: "env-memory.db"}):
            self.assertEqual(resolve_db_path(None), Path("env-memory.db"))

    def test_default_path_is_data_memory_db(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_DB_PATH, None)
            self.assertEqual(resolve_db_path(None), Path("data") / "memory.db")


class DemoFunctionTest(TempDirTestCase):
    prefix = "pms-demo-"

    def test_run_demo_reports_persistence_and_relation_evidence(self) -> None:
        db_path = self.tmpdir / "demo.db"
        payload = run_demo(db_path)
        self.assertTrue(db_path.exists())
        self.assertEqual(payload["counts"]["memory_sources"], 5)
        describe = Database(db_path).describe()
        self.assertEqual(describe["counts"]["sources"], 2)
        self.assertEqual(describe["counts"]["memories"], 4)
        self.assertEqual(describe["counts"]["memory_sources"], 5)

    def test_run_demo_with_reset_on_missing_file_is_fine(self) -> None:
        payload = run_demo(self.tmpdir / "fresh.db", reset=True)
        self.assertEqual(payload["counts"]["sources"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
