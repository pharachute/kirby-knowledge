"""Memory model + validation tests (covers Phase 1 Test 2 and Test 3)."""

from __future__ import annotations

import unittest

from personal_memory import (
    CURRENT_SCHEMA_VERSION,
    InformationOrigin,
    Memory,
    MemoryStatus,
    MemoryType,
    ValidationError,
    is_valid_timestamp,
)

from .helpers import RepositoryTestCase


class MemoryCreationTest(RepositoryTestCase):
    def test_2_create_valid_memory_succeeds(self) -> None:
        """Test 2: a legal Memory is created and persisted with defaults applied."""
        memory = self.make_memory(
            type=MemoryType.KNOWLEDGE,
            title="SQLite 适合本地优先存储",
            content="单文件、零服务，个人规模足够。",
            summary="本地优先选 SQLite。",
            information_origin=InformationOrigin.SOURCE_CONTENT,
            tags=["sqlite", "storage"],
            importance=0.8,
            confidence=0.9,
        )

        self.assertTrue(memory.id.startswith("mem_"))
        self.assertEqual(memory.status, MemoryStatus.ACTIVE)
        self.assertEqual(memory.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertTrue(is_valid_timestamp(memory.created_at))
        self.assertEqual(memory.created_at, memory.updated_at)

        loaded = self.repo.require_memory(memory.id)
        self.assertEqual(loaded, memory)
        self.assertEqual(loaded.tags, ["sqlite", "storage"])
        self.assertEqual(loaded.importance, 0.8)
        self.assertEqual(loaded.confidence, 0.9)
        self.assertEqual(loaded.information_origin, InformationOrigin.SOURCE_CONTENT)
        self.assertEqual(loaded.summary, "本地优先选 SQLite。")

    def test_all_four_types_share_one_structure(self) -> None:
        """The four memory types are rows of the same table, not separate models."""
        created = []
        for memory_type in MemoryType:
            memory = self.make_memory(
                type=memory_type,
                title=f"{memory_type.value} 记忆",
                content=f"{memory_type.value} 正文",
            )
            created.append(memory)
        self.assertEqual({m.type for m in created}, set(MemoryType))
        self.assertEqual(self.repo.counts()["memories"], len(MemoryType))
        for memory in created:
            loaded = self.repo.require_memory(memory.id)
            self.assertIsInstance(loaded, Memory)
            self.assertEqual(loaded.schema_version, CURRENT_SCHEMA_VERSION)

    def test_all_information_origins_and_statuses_are_accepted(self) -> None:
        for origin in InformationOrigin:
            for status in MemoryStatus:
                memory = self.make_memory(
                    information_origin=origin, status=status, content=f"{origin}-{status}"
                )
                self.assertEqual(self.repo.require_memory(memory.id).information_origin, origin)
                self.assertEqual(self.repo.require_memory(memory.id).status, status)

    def test_boundary_values_are_accepted(self) -> None:
        low = self.make_memory(importance=0.0, confidence=0.0, content="low")
        high = self.make_memory(importance=1.0, confidence=1.0, content="high")
        self.assertEqual(self.repo.require_memory(low.id).importance, 0.0)
        self.assertEqual(self.repo.require_memory(high.id).confidence, 1.0)

    def test_tags_are_deduplicated_and_trimmed(self) -> None:
        memory = self.make_memory(tags=["  sqlite ", "sqlite", "中文"])
        self.assertEqual(memory.tags, ["sqlite", "中文"])

    def test_summary_defaults_to_none(self) -> None:
        memory = self.make_memory()
        self.assertIsNone(memory.summary)
        self.assertIsNone(self.repo.require_memory(memory.id).summary)

    def test_string_inputs_are_coerced_to_enums(self) -> None:
        memory = self.make_memory(type="profile", information_origin="agent_inference", status="pending")
        self.assertEqual(memory.type, MemoryType.PROFILE)
        self.assertEqual(memory.information_origin, InformationOrigin.AGENT_INFERENCE)
        self.assertEqual(memory.status, MemoryStatus.PENDING)


class MemoryValidationTest(unittest.TestCase):
    def test_3_invalid_memory_is_rejected(self) -> None:
        """Test 3: every illegal payload is refused by the application layer."""
        cases = [
            ("unknown type", {"type": "idea"}),
            ("unknown information_origin", {"information_origin": "llm_guess"}),
            ("unknown status", {"status": "deleted"}),
            ("importance above 1", {"importance": 1.5}),
            ("importance below 0", {"importance": -0.01}),
            ("confidence above 1", {"confidence": 1.0001}),
            ("confidence below 0", {"confidence": -1}),
            ("importance is bool", {"importance": True}),
            ("confidence is NaN", {"confidence": float("nan")}),
            ("importance is text", {"importance": "high"}),
            ("empty title", {"title": "   "}),
            ("empty content", {"content": "\n"}),
            ("tags not a sequence", {"tags": "sqlite"}),
            ("tags with empty entry", {"tags": ["sqlite", "  "]}),
            ("tags with non-string", {"tags": ["sqlite", 7]}),
            ("schema_version in the future", {"schema_version": CURRENT_SCHEMA_VERSION + 1}),
            ("schema_version zero", {"schema_version": 0}),
            ("bad id", {"memory_id": "bad id"}),
            ("bad created_at", {"created_at": "not-a-date"}),
        ]
        for label, overrides in cases:
            kwargs = {
                "type": MemoryType.KNOWLEDGE,
                "title": "ok",
                "content": "ok",
                "information_origin": InformationOrigin.USER_EXPLICIT,
            }
            kwargs.update(overrides)
            with self.subTest(case=label), self.assertRaises(ValidationError):
                Memory.create(**kwargs)

    def test_validation_error_reports_every_offending_field(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            Memory.create(
                type="idea",
                title="",
                content="ok",
                information_origin="guess",
                importance=5,
            )
        self.assertEqual(set(ctx.exception.fields), {"type", "title", "information_origin", "importance"})


class MemoryRejectionAgainstStoreTest(RepositoryTestCase):
    def test_invalid_payload_never_reaches_the_database(self) -> None:
        with self.assertRaises(ValidationError):
            self.repo.create_memory(
                Memory.create(type="knowledge", title="ok", content="ok", information_origin="nope")
            )
        self.assertEqual(self.repo.counts()["memories"], 0)
        with self.database.connection() as conn:
            rows = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
        self.assertEqual(rows, 0)


class MemoryUpdateTest(RepositoryTestCase):
    def test_update_mutable_fields(self) -> None:
        memory = self.make_memory(title="旧标题", importance=0.2, tags=["a"])
        updated = self.repo.update_memory(
            memory.id,
            title="新标题",
            content="新正文",
            summary="新摘要",
            tags=["a", "b"],
            importance=0.9,
            confidence=0.4,
            status=MemoryStatus.ARCHIVED,
        )
        self.assertEqual(updated.title, "新标题")
        self.assertEqual(updated.tags, ["a", "b"])
        self.assertEqual(updated.importance, 0.9)
        self.assertEqual(updated.status, MemoryStatus.ARCHIVED)
        self.assertEqual(updated.created_at, memory.created_at)
        self.assertEqual(updated.schema_version, memory.schema_version)

        reloaded = self.repo.require_memory(memory.id)
        self.assertEqual(reloaded.title, "新标题")
        self.assertEqual(reloaded.content, "新正文")
        self.assertEqual(reloaded.summary, "新摘要")
        self.assertEqual(reloaded.tags, ["a", "b"])

    def test_invalid_update_is_rejected_and_nothing_is_written(self) -> None:
        memory = self.make_memory(title="保持原样", tags=["keep"])
        with self.assertRaises(ValidationError):
            self.repo.update_memory(memory.id, importance=2.0)
        with self.assertRaises(ValidationError):
            self.repo.update_memory(memory.id, type="idea")
        reloaded = self.repo.require_memory(memory.id)
        self.assertEqual(reloaded.title, "保持原样")
        self.assertEqual(reloaded.tags, ["keep"])
        self.assertEqual(reloaded.importance, memory.importance)

    def test_update_rejects_unknown_or_immutable_fields(self) -> None:
        memory = self.make_memory()
        for field in ("id", "created_at", "schema_version", "importance_level"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.repo.update_memory(memory.id, **{field: 1})

    def test_update_unknown_memory_raises_not_found(self) -> None:
        from personal_memory import NotFoundError

        with self.assertRaises(NotFoundError):
            self.repo.update_memory("mem_missing", title="x")

    def test_update_tags_none_clears_tags(self) -> None:
        """Symmetric with update_source(metadata=None): an explicit None clears the list."""
        memory = self.make_memory(tags=["a", "b"])
        updated = self.repo.update_memory(memory.id, tags=None)
        self.assertEqual(updated.tags, [])
        self.assertEqual(self.repo.require_memory(memory.id).tags, [])

    def test_update_tags_wrong_type_is_rejected_not_split(self) -> None:
        """A string must not be silently exploded into characters."""
        memory = self.make_memory(tags=["keep"])
        with self.assertRaises(ValidationError) as ctx:
            self.repo.update_memory(memory.id, tags="abc")
        self.assertEqual(ctx.exception.fields, ("tags",))
        self.assertEqual(self.repo.require_memory(memory.id).tags, ["keep"])

    def test_list_memories_filters_are_validated(self) -> None:
        """An unknown filter value must raise instead of silently returning nothing."""
        self.make_memory(type="knowledge", status="active")
        self.make_memory(type="event", status="pending")

        self.assertEqual(len(self.repo.list_memories(memory_type="knowledge")), 1)
        self.assertEqual(len(self.repo.list_memories(status=MemoryStatus.PENDING)), 1)
        self.assertEqual(len(self.repo.list_memories(memory_type=MemoryType.KNOWLEDGE, status="active")), 1)
        # case and surrounding whitespace are normalised, not rejected
        self.assertEqual(len(self.repo.list_memories(memory_type="KNOWLEDGE ")), 1)

        for bad_filter in ("idea", "", "   "):
            with self.subTest(filter=bad_filter):
                with self.assertRaises(ValidationError) as ctx:
                    self.repo.list_memories(memory_type=bad_filter)
                self.assertEqual(ctx.exception.fields, ("memory_type",))
        with self.assertRaises(ValidationError) as ctx:
            self.repo.list_memories(status="deleted")
        self.assertEqual(ctx.exception.fields, ("status",))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
