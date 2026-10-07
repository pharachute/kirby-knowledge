"""Source model + persistence tests (covers Phase 1 Test 1 and Test 8)."""

from __future__ import annotations

import unittest

from personal_memory import (
    ConflictError,
    DuplicateContentHashError,
    Source,
    SourceType,
    ValidationError,
    compute_content_hash,
    is_valid_timestamp,
)
from personal_memory.models import MAX_TITLE_LENGTH

from .helpers import RepositoryTestCase


class SourceCreationTest(RepositoryTestCase):
    def test_1_create_source_succeeds(self) -> None:
        """Test 1: creating a Source works and the row is really persisted."""
        source = self.make_source(
            source_type=SourceType.TEXT,
            title="阶段 1 测试来源",
            content="这是一段用于阶段 1 测试的文本内容。",
            url="https://example.com/article",
            metadata={"k": "v", "nested": {"n": 1}},
        )

        self.assertTrue(source.id.startswith("src_"))
        self.assertRegex(source.content_hash, r"^[0-9a-f]{64}$")
        self.assertTrue(is_valid_timestamp(source.created_at))
        self.assertEqual(source.created_at, source.updated_at)

        loaded = self.repo.require_source(source.id)
        self.assertEqual(loaded, source)
        self.assertEqual(loaded.source_type, SourceType.TEXT)
        self.assertEqual(loaded.title, "阶段 1 测试来源")
        self.assertEqual(loaded.url, "https://example.com/article")
        self.assertEqual(loaded.metadata["nested"], {"n": 1})
        self.assertEqual(self.repo.counts()["sources"], 1)

    def test_source_without_url_and_metadata_is_allowed(self) -> None:
        source = self.make_source(title="最小来源", content="最小正文", metadata={})
        loaded = self.repo.require_source(source.id)
        self.assertIsNone(loaded.url)
        self.assertEqual(loaded.metadata, {})

    def test_get_source_returns_none_when_missing(self) -> None:
        self.assertIsNone(self.repo.get_source("src_does_not_exist"))

    def test_8_duplicate_content_hash_is_recognised(self) -> None:
        """Test 8: a second Source with the same content_hash is refused and reported."""
        first = self.make_source(title="原始标题", content="完全相同的正文内容。")

        duplicate = Source.create(
            source_type=SourceType.TEXT,
            title="另一个标题（正文相同）",
            content="完全相同的正文内容。",
        )
        self.assertEqual(duplicate.content_hash, first.content_hash)

        with self.assertRaises(DuplicateContentHashError) as ctx:
            self.repo.create_source(duplicate)
        self.assertEqual(ctx.exception.content_hash, first.content_hash)
        self.assertEqual(ctx.exception.existing_source_id, first.id)

        found = self.repo.find_source_by_content_hash(first.content_hash)
        self.assertIsNotNone(found)
        self.assertEqual(found.id, first.id)
        self.assertTrue(self.repo.source_exists_by_hash(first.content_hash))
        self.assertFalse(self.repo.source_exists_by_hash("0" * 64))
        self.assertEqual(self.repo.counts()["sources"], 1)

    def test_content_hash_normalises_line_endings_and_trailing_spaces(self) -> None:
        self.assertEqual(
            compute_content_hash("line1\nline2\n"),
            compute_content_hash("line1   \r\nline2\r\n"),
        )
        self.assertNotEqual(
            compute_content_hash("line1\nline2"),
            compute_content_hash("line1\nline3"),
        )

    def test_duplicate_source_id_is_a_conflict(self) -> None:
        first = self.make_source(source_id="src_fixed_id", content="内容一")
        second = Source.create(
            source_type=SourceType.TEXT, title="t", content="内容二", source_id="src_fixed_id"
        )
        with self.assertRaises(ConflictError) as ctx:
            self.repo.create_source(second)
        self.assertNotIsInstance(ctx.exception, DuplicateContentHashError)
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual(self.repo.require_source(first.id).content, "内容一")


class SourceValidationTest(unittest.TestCase):
    def test_invalid_source_payloads_are_rejected(self) -> None:
        cases = [
            ("unsupported source_type", {"source_type": "video"}),
            ("empty title", {"title": "   "}),
            ("empty content", {"content": "\n\n"}),
            ("non-http url", {"url": "ftp://example.com/x"}),
            ("bad content_hash", {"content_hash": "not-a-sha256"}),
            ("uppercase content_hash", {"content_hash": "A" * 64}),
            ("metadata not a mapping", {"metadata": ["a"]}),
            ("metadata not serialisable", {"metadata": {"bad": object()}}),
            ("bad id", {"source_id": "has space"}),
            ("bad created_at", {"created_at": "yesterday"}),
            ("title too long", {"title": "x" * (MAX_TITLE_LENGTH + 1)}),
        ]
        for label, overrides in cases:
            kwargs = {"source_type": SourceType.TEXT, "title": "ok", "content": "ok"}
            kwargs.update(overrides)
            with self.subTest(case=label), self.assertRaises(ValidationError):
                Source.create(**kwargs)

    def test_validation_error_reports_the_offending_field(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            Source.create(source_type="video", title="ok", content="ok")
        self.assertIn("source_type", ctx.exception.fields)

    def test_all_declared_source_types_are_accepted(self) -> None:
        for source_type in SourceType:
            with self.subTest(source_type=source_type):
                source = Source.create(
                    source_type=source_type, title=f"{source_type} title", content=f"{source_type} content"
                )
                self.assertEqual(source.source_type, source_type)


class SourceUpdateTest(RepositoryTestCase):
    def test_update_title_url_and_metadata(self) -> None:
        source = self.make_source(title="旧标题", content="正文保持不变", url=None)
        updated = self.repo.update_source(
            source.id, title="新标题", url="https://example.com/new", metadata={"a": 1}
        )
        self.assertEqual(updated.title, "新标题")
        self.assertEqual(updated.url, "https://example.com/new")
        self.assertEqual(updated.metadata, {"a": 1})
        self.assertEqual(updated.content_hash, source.content_hash)
        self.assertEqual(updated.created_at, source.created_at)
        self.assertGreaterEqual(updated.updated_at, source.updated_at)

        reloaded = self.repo.require_source(source.id)
        self.assertEqual(reloaded.title, "新标题")
        self.assertEqual(reloaded.metadata, {"a": 1})

    def test_update_content_recomputes_content_hash(self) -> None:
        source = self.make_source(content="第一版内容")
        updated = self.repo.update_source(source.id, content="第二版内容")
        self.assertNotEqual(updated.content_hash, source.content_hash)
        self.assertEqual(updated.content_hash, compute_content_hash("第二版内容"))
        self.assertEqual(self.repo.find_source_by_content_hash(updated.content_hash).id, source.id)
        self.assertIsNone(self.repo.find_source_by_content_hash(source.content_hash))

    def test_update_to_existing_content_is_a_duplicate_conflict(self) -> None:
        first = self.make_source(content="内容 A")
        second = self.make_source(content="内容 B")
        with self.assertRaises(DuplicateContentHashError) as ctx:
            self.repo.update_source(second.id, content="内容 A")
        self.assertEqual(ctx.exception.existing_source_id, first.id)
        self.assertEqual(self.repo.require_source(second.id).content, "内容 B")

    def test_update_rejects_unknown_or_immutable_fields(self) -> None:
        source = self.make_source()
        for field in ("id", "created_at", "content_hash", "nonsense"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.repo.update_source(source.id, **{field: "x"})

    def test_update_unknown_source_raises_not_found(self) -> None:
        from personal_memory import NotFoundError

        with self.assertRaises(NotFoundError):
            self.repo.update_source("src_missing", title="x")

    def test_update_metadata_wrong_type_is_rejected(self) -> None:
        """A non-mapping metadata value must raise ValidationError, not a raw ValueError."""
        source = self.make_source(metadata={"keep": True})
        with self.assertRaises(ValidationError) as ctx:
            self.repo.update_source(source.id, metadata="ab")
        self.assertEqual(ctx.exception.fields, ("metadata",))
        self.assertEqual(self.repo.require_source(source.id).metadata, {"keep": True})

    def test_update_metadata_none_clears_it(self) -> None:
        source = self.make_source(metadata={"a": 1})
        self.assertEqual(self.repo.update_source(source.id, metadata=None).metadata, {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
