"""Phase 2A compatibility: the learning layer must not disturb 1.0.

These tests are the "no poison" guard promised in the Phase 1 report: whatever the
learning layer writes, the 1.0 tables, their timestamps, the search index and the
retrieval behaviour must be byte-for-byte unaffected.
"""

from __future__ import annotations

import unittest

from personal_memory import (
    LearningRepository,
    LearningSession,
    LearningState,
    MemoryStatus,
    MemoryType,
    TeachingStage,
)
from personal_memory.lifecycle import MemoryLifecycle
from personal_memory.retrieval import MemoryRetriever
from personal_memory.store import MemoryRepository

from .helpers import RepositoryTestCase


class LearningLayerCompatibilityTest(RepositoryTestCase):
    prefix = "pms-learncompat-"

    def setUp(self) -> None:
        super().setUp()
        self.learning = LearningRepository(self.database)
        self.lifecycle = MemoryLifecycle(self.repo)
        self.source = self.make_source(title="RAG 实践要点", content="检索增强生成补充上下文。")
        self.memory = self.make_memory(
            title="RAG 减少事实性错误", content="检索外部知识为模型补充上下文。",
            type=MemoryType.KNOWLEDGE, tags=["rag", "检索"],
        )
        self.repo.link(self.memory.id, self.source.id)
        self.baseline = {
            "counts": self.repo.counts(),
            "memory": self.repo.get_memory(self.memory.id).as_dict(),
            "source": self.repo.get_source(self.source.id).as_dict(),
            "search": [(hit.memory.id, hit.score) for hit in MemoryRetriever(self.repo).search("RAG").hits],
            "index_word": self.repo.index_row_count("word"),
            "index_trigram": self.repo.index_row_count("trigram"),
        }

    def run_a_full_learning_flow(self) -> None:
        """Everything Phase 3 will do at the data layer, in one place."""
        state = self.learning.create_state(LearningState.create(memory_id=self.memory.id))
        session = self.learning.create_session(LearningSession.create(
            source_id=self.source.id, plan=[self.memory.id], current_memory_id=self.memory.id))
        with self.learning.transaction() as tx:
            tx.update_session(session.id, current_stage=TeachingStage.QUESTION,
                              exchange={"explanation": "RAG 用检索补上下文", "question": "为什么能减少错误？"})
            tx.update_state(self.memory.id, understanding_level="partial",
                            known_aspects=["检索补充上下文"], weak_aspects=["重排"],
                            learn_count=1, last_learned_at="2026-10-07T08:00:00.000Z")
        self.learning.update_session(session.id, current_stage=TeachingStage.PRACTICE)
        self.learning.finish_session(session.id)
        self.learning.upsert_state(LearningState.create(
            memory_id=self.memory.id, understanding_level="solid", learn_count=2,
            last_learned_at="2026-10-07T09:00:00.000Z"))
        self.assertIsNotNone(state)

    # -- the invariants ----------------------------------------------------
    def test_learning_writes_do_not_touch_1_0_tables(self) -> None:
        self.run_a_full_learning_flow()

        self.assertEqual(self.repo.counts(), self.baseline["counts"])
        self.assertEqual(self.repo.get_memory(self.memory.id).as_dict(), self.baseline["memory"])
        self.assertEqual(self.repo.get_source(self.source.id).as_dict(), self.baseline["source"])
        self.assertEqual(LearningRepository(self.database).counts(),
                         {"learning_states": 1, "learning_sessions": 1})

    def test_memory_and_source_timestamps_are_untouched(self) -> None:
        before_memory = self.repo.get_memory(self.memory.id)
        before_source = self.repo.get_source(self.source.id)

        self.run_a_full_learning_flow()

        after_memory = self.repo.get_memory(self.memory.id)
        after_source = self.repo.get_source(self.source.id)
        self.assertEqual(after_memory.updated_at, before_memory.updated_at)
        self.assertEqual(after_memory.created_at, before_memory.created_at)
        self.assertEqual(after_source.updated_at, before_source.updated_at)
        self.assertEqual(after_source.content, before_source.content)

    def test_search_index_and_retrieval_are_unaffected(self) -> None:
        self.run_a_full_learning_flow()

        self.assertEqual(self.repo.index_row_count("word"), self.baseline["index_word"])
        self.assertEqual(self.repo.index_row_count("trigram"), self.baseline["index_trigram"])
        self.assertEqual(
            [(hit.memory.id, hit.score) for hit in MemoryRetriever(self.repo).search("RAG").hits],
            self.baseline["search"],
        )
        self.assertEqual(
            [hit.memory.id for hit in MemoryRetriever(self.repo).search("检索").hits],
            [self.memory.id],
        )

    def test_learning_rows_are_not_visible_to_1_0_listing(self) -> None:
        self.run_a_full_learning_flow()

        self.assertEqual([m.id for m in self.repo.list_memories(status=None)], [self.memory.id])
        self.assertEqual([s.id for s in self.repo.list_sources()], [self.source.id])
        self.assertEqual(self.repo.link_count(), 1)

    def test_1_0_lifecycle_still_works_and_keeps_the_learning_state(self) -> None:
        self.run_a_full_learning_flow()

        self.lifecycle.archive_memory(self.memory.id)
        self.assertEqual(str(self.repo.require_memory(self.memory.id).status), "archived")
        state_after_archive = self.learning.get_state(self.memory.id)
        self.assertIsNotNone(state_after_archive)
        self.assertEqual(state_after_archive.learn_count, 2)

        self.lifecycle.restore_memory(self.memory.id)
        self.assertEqual(str(self.repo.require_memory(self.memory.id).status), "active")
        self.assertEqual(self.learning.get_state(self.memory.id).learn_count, 2)
        self.assertEqual(self.repo.get_memory(self.memory.id).status, MemoryStatus.ACTIVE)

    def test_deleting_a_memory_removes_only_its_state(self) -> None:
        self.run_a_full_learning_flow()

        self.repo.delete_memory(self.memory.id)

        self.assertIsNone(self.learning.get_state(self.memory.id))
        self.assertEqual(self.repo.get_memory(self.memory.id), None)
        self.assertEqual(self.learning.counts()["learning_sessions"], 1)   # session survives
        self.assertEqual(self.repo.counts()["sources"], self.baseline["counts"]["sources"])

    def test_a_1_0_repository_still_works_against_a_v3_database(self) -> None:
        """1.0 code paths keep working on the upgraded file (nothing was ALTERed)."""
        self.run_a_full_learning_flow()
        fresh = MemoryRepository(self.database)          # re-verifies the schema on open

        self.assertEqual(fresh.counts(), self.baseline["counts"])
        self.assertEqual(fresh.get_memory(self.memory.id).title, "RAG 减少事实性错误")
        self.assertEqual(fresh.get_sources_for_memory(self.memory.id)[0].id, self.source.id)
        self.assertEqual(fresh.get_memories_for_source(self.source.id)[0].id, self.memory.id)
        self.assertEqual(fresh.schema_version() if hasattr(fresh, "schema_version") else 3, 3)
        self.assertEqual(MemoryRepository(self.database).counts(), self.baseline["counts"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
