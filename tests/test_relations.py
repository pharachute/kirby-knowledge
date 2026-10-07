"""Source <-> Memory relation tests (covers Phase 1 Test 4 and Test 5)."""

from __future__ import annotations

import unittest

from personal_memory import NotFoundError

from .helpers import RepositoryTestCase


class RelationDirectionTest(RepositoryTestCase):
    def test_4_one_source_can_be_linked_to_many_memories(self) -> None:
        """Test 4: one Source -> N Memories, queryable in both directions."""
        source = self.make_source(title="共享来源", content="一条被多条记忆共享的原始文本。")
        memories = [
            self.make_memory(title=f"记忆 {index}", content=f"记忆正文 {index}") for index in range(3)
        ]

        for memory in memories:
            self.assertTrue(self.repo.link(memory.id, source.id))

        linked_memories = self.repo.get_memories_for_source(source.id)
        self.assertEqual({memory.id for memory in linked_memories}, {m.id for m in memories})
        for memory in memories:
            sources = self.repo.get_sources_for_memory(memory.id)
            self.assertEqual([s.id for s in sources], [source.id])

        self.assertEqual(self.repo.counts()["memory_sources"], 3)
        self.assertEqual(self.repo.counts()["sources"], 1)
        self.assertEqual(self.repo.counts()["memories"], 3)

    def test_5_one_memory_can_be_linked_to_many_sources(self) -> None:
        """Test 5: one Memory -> N Sources, queryable in both directions."""
        sources = [
            self.make_source(title=f"来源 {index}", content=f"来源正文 {index}") for index in range(3)
        ]
        memory = self.make_memory(title="综合记忆", content="由多份来源综合得出的记忆。")

        for source in sources:
            self.assertTrue(self.repo.link(memory.id, source.id))

        linked_sources = self.repo.get_sources_for_memory(memory.id)
        self.assertEqual({source.id for source in linked_sources}, {s.id for s in sources})
        for source in sources:
            memories = self.repo.get_memories_for_source(source.id)
            self.assertEqual([m.id for m in memories], [memory.id])

        self.assertEqual(self.repo.counts()["memory_sources"], 3)
        self.assertEqual(self.repo.counts()["sources"], 3)
        self.assertEqual(self.repo.counts()["memories"], 1)

    def test_many_to_many_matrix(self) -> None:
        sources = [self.make_source(content=f"matrix source {i}") for i in range(3)]
        memories = [self.make_memory(content=f"matrix memory {i}") for i in range(3)]
        expected = set()
        for source in sources:
            for memory in memories:
                if self.repo.link(memory.id, source.id):
                    expected.add((memory.id, source.id))
        self.assertEqual(len(expected), 9)
        self.assertEqual(self.repo.counts()["memory_sources"], 9)
        for source in sources:
            self.assertEqual(len(self.repo.get_memories_for_source(source.id)), 3)
        for memory in memories:
            self.assertEqual(len(self.repo.get_sources_for_memory(memory.id)), 3)

    def test_relation_rows_live_in_a_relation_table(self) -> None:
        """The relation is a real table with a composite PK, not a joined string."""
        source = self.make_source()
        memory = self.make_memory()
        self.repo.link(memory.id, source.id)

        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT memory_id, source_id, created_at FROM memory_sources"
            ).fetchone()
            pk_columns = [
                info["name"]
                for info in conn.execute("PRAGMA table_info(memory_sources)").fetchall()
                if info["pk"]
            ]
        self.assertEqual(row["memory_id"], memory.id)
        self.assertEqual(row["source_id"], source.id)
        self.assertTrue(row["created_at"])
        self.assertEqual(sorted(pk_columns), ["memory_id", "source_id"])
        # the source row itself only stores its own content, never relation ids
        self.assertNotIn(memory.id, source.content)


class LinkSemanticsTest(RepositoryTestCase):
    def test_link_is_idempotent(self) -> None:
        source = self.make_source()
        memory = self.make_memory()
        self.assertTrue(self.repo.link(memory.id, source.id))
        self.assertFalse(self.repo.link(memory.id, source.id))
        self.assertEqual(self.repo.counts()["memory_sources"], 1)
        self.assertTrue(self.repo.is_linked(memory.id, source.id))

    def test_link_many_returns_new_link_count(self) -> None:
        source_a = self.make_source(content="a")
        source_b = self.make_source(content="b")
        memory = self.make_memory()
        self.assertEqual(self.repo.link_many(memory.id, [source_a.id, source_b.id]), 2)
        self.assertEqual(self.repo.link_many(memory.id, [source_a.id, source_b.id]), 0)

    def test_link_many_is_all_or_nothing(self) -> None:
        """A bad id in the batch must not leave earlier links behind."""
        source = self.make_source()
        memory = self.make_memory()
        with self.assertRaises(NotFoundError):
            self.repo.link_many(memory.id, [source.id, "src_missing"])
        self.assertEqual(self.repo.counts()["memory_sources"], 0)
        self.assertFalse(self.repo.is_linked(memory.id, source.id))

    def test_unlink_removes_only_the_relation(self) -> None:
        source = self.make_source()
        memory = self.make_memory()
        self.repo.link(memory.id, source.id)
        self.assertTrue(self.repo.unlink(memory.id, source.id))
        self.assertFalse(self.repo.unlink(memory.id, source.id))
        self.assertIsNotNone(self.repo.get_memory(memory.id))
        self.assertIsNotNone(self.repo.get_source(source.id))
        self.assertEqual(self.repo.counts()["memory_sources"], 0)

    def test_linking_unknown_entities_is_refused(self) -> None:
        source = self.make_source()
        memory = self.make_memory()
        with self.assertRaises(NotFoundError):
            self.repo.link("mem_missing", source.id)
        with self.assertRaises(NotFoundError):
            self.repo.link(memory.id, "src_missing")
        self.assertEqual(self.repo.counts()["memory_sources"], 0)

    def test_querying_unknown_id_raises_instead_of_returning_empty(self) -> None:
        with self.assertRaises(NotFoundError):
            self.repo.get_sources_for_memory("mem_missing")
        with self.assertRaises(NotFoundError):
            self.repo.get_memories_for_source("src_missing")

    def test_entities_without_links_return_empty_lists(self) -> None:
        source = self.make_source()
        memory = self.make_memory()
        self.assertEqual(self.repo.get_sources_for_memory(memory.id), [])
        self.assertEqual(self.repo.get_memories_for_source(source.id), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
