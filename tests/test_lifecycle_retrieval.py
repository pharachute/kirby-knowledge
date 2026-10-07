"""Phase 4 tests: lifecycle <-> retrieval integration (required behaviours 25-28).

Everything here runs without a model and without network: Phase 3 retrieval is
the observable that proves a lifecycle change took effect immediately.
"""

from __future__ import annotations

import unittest

from personal_memory import MemoryLifecycle, MemoryRetriever, MemoryStatus

from .helpers import RepositoryTestCase


class RetrievalLifecycleIntegrationTest(RepositoryTestCase):
    prefix = "pms-lifecycle-retrieval-"

    def setUp(self) -> None:
        super().setUp()
        self.lifecycle = MemoryLifecycle(self.repo)
        self.retriever = MemoryRetriever(self.repo)

    def titles(self, query: str, **kwargs) -> list[str]:
        return [hit.memory.title for hit in self.retriever.search(query, **kwargs).hits]

    def test_25_archived_is_not_returned_by_default(self) -> None:
        memory = self.make_memory(title="归档召回测试", content="关键词 flutter 出现在正文")

        self.lifecycle.archive_memory(memory.id)

        default = self.retriever.search("flutter")
        self.assertEqual(default.hits, ())
        self.assertEqual(default.total, 0)
        self.assertEqual(default.statuses, ("active",))
        self.assertEqual(self.titles("flutter", status="archived"), ["归档召回测试"])
        self.assertEqual(self.titles("flutter", status="all"), ["归档召回测试"])

    def test_26_pending_is_not_returned_by_default(self) -> None:
        memory = self.make_memory(
            title="待确认召回", content="关键词 mongoose 出现在正文", status=MemoryStatus.PENDING
        )

        self.assertEqual(self.titles("mongoose"), [])
        self.assertEqual(self.titles("mongoose", status="pending"), ["待确认召回"])
        self.assertEqual(self.titles("mongoose", status="all"), ["待确认召回"])


    def test_27_restore_makes_it_searchable_again(self) -> None:
        memory = self.make_memory(title="恢复召回", content="关键词 pangolin 正文")

        self.lifecycle.archive_memory(memory.id)
        self.assertEqual(self.titles("pangolin"), [])

        report = self.lifecycle.restore_memory(memory.id)

        self.assertTrue(report.changed)
        result = self.retriever.search("pangolin")
        self.assertEqual([hit.memory.id for hit in result.hits], [memory.id])
        self.assertEqual(result.mode, "index")
        self.assertEqual(result.statuses, ("active",))

    def test_28_update_is_visible_to_retrieval_immediately(self) -> None:
        memory = self.make_memory(title="即时更新检索", content="更新前关键词 quetzal")
        self.assertEqual(self.titles("quetzal"), ["即时更新检索"])

        self.lifecycle.update_memory(memory.id, content="更新后关键词 ocelot", title="即时更新检索 v2")

        self.assertEqual(self.titles("quetzal"), [])
        self.assertEqual(self.titles("quetzal", status="all"), [])
        self.assertEqual(self.titles("ocelot"), ["即时更新检索 v2"])
        self.assertTrue(self.repo.index_consistency()["consistent"])

    def test_28b_delete_removes_it_from_every_search_scope(self) -> None:
        memory = self.make_memory(title="删除后检索", content="关键词 tapir 正文")
        self.assertEqual(self.titles("tapir"), ["删除后检索"])

        self.lifecycle.delete_memory(memory.id)

        for status in ("active", "pending", "archived", "all"):
            with self.subTest(status=status):
                self.assertEqual(self.titles("tapir", status=status), [])
        self.assertEqual(self.repo.index_row_count("word"), 0)
        self.assertEqual(self.repo.index_row_count("trigram"), 0)
        self.assertTrue(self.repo.index_consistency()["consistent"])

    def test_28c_lifecycle_changes_leave_no_index_drift(self) -> None:
        memories = [
            self.make_memory(title=f"漂移检查 {index}", content=f"关键词 drift{index}") for index in range(3)
        ]

        self.lifecycle.archive_memory(memories[0].id)
        self.lifecycle.update_memory(memories[1].id, content="关键词 switched1")
        self.lifecycle.delete_memory(memories[2].id)

        consistency = self.repo.index_consistency()
        self.assertTrue(consistency["consistent"])
        self.assertEqual(consistency["memories"], 2)
        self.assertEqual(consistency["word_index"], 2)
        self.assertEqual(consistency["trigram_index"], 2)
        self.assertEqual(self.titles("switched1", status="all"), ["漂移检查 1"])
        # the old keyword is gone: Latin matching is word/prefix based, and
        # "drift1" is neither a word nor a prefix of "switched1"
        self.assertEqual(self.titles("drift1", status="all"), [])
        self.assertTrue(self.repo.rebuild_search_index()["consistent"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
