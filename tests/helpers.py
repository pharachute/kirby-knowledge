"""Shared helpers for the Phase 1 test suite."""

from __future__ import annotations

import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from personal_memory import (
    Database,
    InformationOrigin,
    Memory,
    MemoryRepository,
    MemoryType,
    Source,
    SourceType,
)

#: Project root, used as cwd for the child process in the cross-process test.
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_temp_dir(prefix: str = "pms-test-") -> Path:
    """Create a throwaway directory that this environment can actually use.

    ``tempfile.mkdtemp`` creates a 0o700 directory; under the DSH Windows
    sandbox such a directory can neither be written by sqlite3
    ("unable to open database file") nor removed again (WinError 5).  A plain
    ``mkdir`` inherits normal ACLs and works, so the tests use this helper.
    """
    path = Path(tempfile.gettempdir()) / f"{prefix}{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def remove_temp_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


class TempDirTestCase(unittest.TestCase):
    """Base class giving each test a private, sandbox-safe temporary directory."""

    prefix = "pms-test-"

    def setUp(self) -> None:
        self.tmpdir = make_temp_dir(self.prefix)

    def tearDown(self) -> None:
        remove_temp_dir(self.tmpdir)


def example_source(**overrides: Any) -> Source:
    """A valid text Source; content is unique per call unless overridden."""
    kwargs: dict[str, Any] = {
        "source_type": SourceType.TEXT,
        "title": "示例来源",
        "content": f"示例正文 {uuid.uuid4().hex}",
        "metadata": {"origin": "test"},
    }
    kwargs.update(overrides)
    return Source.create(**kwargs)


def example_memory(**overrides: Any) -> Memory:
    """A valid knowledge Memory; content is unique per call unless overridden."""
    kwargs: dict[str, Any] = {
        "type": MemoryType.KNOWLEDGE,
        "title": "示例记忆",
        "content": f"示例记忆正文 {uuid.uuid4().hex}",
        "information_origin": InformationOrigin.USER_EXPLICIT,
    }
    kwargs.update(overrides)
    return Memory.create(**kwargs)


class RepositoryTestCase(TempDirTestCase):
    """Fresh temporary SQLite file per test -- no state leaks between tests."""

    prefix = "pms-repo-"

    def setUp(self) -> None:
        super().setUp()
        self.db_path = self.tmpdir / "memory.db"
        self.database = Database(self.db_path)
        self.repo = MemoryRepository(self.database)

    # convenience wrappers that also persist
    def make_source(self, **overrides: Any) -> Source:
        return self.repo.create_source(example_source(**overrides))

    def make_memory(self, **overrides: Any) -> Memory:
        return self.repo.create_memory(example_memory(**overrides))
