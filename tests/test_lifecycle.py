"""Phase 4 tests: Memory lifecycle -- legal transitions, validated updates, safe delete.

Covers required behaviours 1-16 of the Phase 4 spec (lifecycle, update, delete);
``test_lifecycle_retrieval.py`` covers 25-28 (Retrieval integration) and
``test_quality.py`` covers 17-24 (duplicate + conflict).
"""

from __future__ import annotations

import unittest

from personal_memory import (
    Database,
    IllegalTransitionError,
    MemoryFormationService,
    MemoryLifecycle,
    MemoryRepository,
    MemoryRetriever,
    MemoryStatus,
    NotFoundError,
    RawInput,
    ValidationError,
)

from .helpers import RepositoryTestCase
from .llm_fakes import mock_client, response_for
from .test_quality import draft_payload, formation_payload


class LifecycleTransitionTest(RepositoryTestCase):
    prefix = "pms-lifecycle-"

    def setUp(self) -> None:
        super().setUp()
        self.lifecycle = MemoryLifecycle(self.repo)
        self.retriever = MemoryRetriever(self.repo)

    # -- helpers -----------------------------------------------------------
    def titles(self, query: str, **kwargs) -> list[str]:
        return [hit.memory.title for hit in self.retriever.search(query, **kwargs).hits]

    def fresh(self) -> MemoryRepository:
        """A brand-new repository/connection, so reads are not served from state."""
        return MemoryRepository(Database(self.db_path))

    # -- 1. active -> archived --------------------------------------------
    def test_1_active_to_archived_hides_the_memory_from_default_search(self) -> None:
        memory = self.make_memory(title="归档测试记忆", content="归档前的检索关键词 zebra")
        self.assertEqual(self.titles("zebra"), ["归档测试记忆"])

        report = self.lifecycle.archive_memory(memory.id)

        self.assertTrue(report.changed)
        self.assertEqual((report.from_status, report.to_status), ("active", "archived"))
        self.assertEqual(str(self.fresh().require_memory(memory.id).status), "archived")
        self.assertGreaterEqual(report.memory.updated_at, memory.updated_at)
        self.assertEqual(self.titles("zebra"), [])
        self.assertEqual(self.titles("zebra", status="all"), ["归档测试记忆"])
        self.assertTrue(self.repo.index_consistency()["consistent"])

    # -- 2. archived -> active --------------------------------------------
    def test_2_restore_brings_an_archived_memory_back(self) -> None:
        memory = self.make_memory(title="恢复测试记忆", content="恢复后的检索关键词 yak")
        self.lifecycle.archive_memory(memory.id)
        self.assertEqual(self.titles("yak"), [])

        report = self.lifecycle.restore_memory(memory.id)

        self.assertTrue(report.changed)
        self.assertEqual((report.from_status, report.to_status), ("archived", "active"))
        self.assertEqual(str(self.fresh().require_memory(memory.id).status), "active")
        self.assertEqual(self.titles("yak"), ["恢复测试记忆"])

    def test_2b_restore_on_an_archived_memory_is_the_only_way_back(self) -> None:
        """``restore_memory`` is documented as an alias of ``set_status(active)``."""
        archived = self.make_memory(title="别名语义", status=MemoryStatus.ARCHIVED)
        self.assertTrue(self.lifecycle.restore_memory(archived.id).changed)
        pending = self.make_memory(title="pending 别名", status=MemoryStatus.PENDING)
        report = self.lifecycle.restore_memory(pending.id)
        self.assertTrue(report.changed)
        self.assertEqual(report.to_status, "active")

    # -- 3. pending -> active ---------------------------------------------
    def test_3_pending_to_active_makes_the_memory_trusted(self) -> None:
        memory = self.make_memory(title="待确认记忆", content="待确认的检索关键词 quokka", status=MemoryStatus.PENDING)
        self.assertEqual(self.titles("quokka"), [])  # pending is not recalled by default

        report = self.lifecycle.activate_memory(memory.id)

        self.assertTrue(report.changed)
        self.assertEqual((report.from_status, report.to_status), ("pending", "active"))
        self.assertEqual(str(self.fresh().require_memory(memory.id).status), "active")
        self.assertEqual(self.titles("quokka"), ["待确认记忆"])
        self.assertEqual(self.titles("quokka", status="pending"), [])

    # -- 4. pending -> archived -------------------------------------------
    def test_4_pending_to_archived_is_allowed(self) -> None:
        memory = self.make_memory(title="待确认后归档", content="内容 archived-pending", status=MemoryStatus.PENDING)
        report = self.lifecycle.archive_memory(memory.id)
        self.assertTrue(report.changed)
        self.assertEqual((report.from_status, report.to_status), ("pending", "archived"))
        self.assertEqual(str(self.fresh().require_memory(memory.id).status), "archived")

    # -- 5. illegal transitions -------------------------------------------
    def test_5_transitions_to_pending_and_unknown_ids_are_refused(self) -> None:
        memory = self.make_memory(title="非法转换记忆", content="内容 stays active")
        with self.assertRaises(IllegalTransitionError) as caught:
            self.lifecycle.set_status(memory.id, MemoryStatus.PENDING)
        self.assertEqual(caught.exception.memory_id, memory.id)
        self.assertEqual(caught.exception.from_status, "active")
        self.assertEqual(caught.exception.to_status, "pending")
        self.assertEqual(caught.exception.allowed, ("archived",))
        self.assertEqual(str(self.fresh().require_memory(memory.id).status), "active")

        archived = self.make_memory(title="已归档", status=MemoryStatus.ARCHIVED)
        with self.assertRaises(IllegalTransitionError) as caught_archived:
            self.lifecycle.set_status(archived.id, MemoryStatus.PENDING)
        self.assertEqual(caught_archived.exception.allowed, ("active",))

        with self.assertRaises(ValidationError):
            self.lifecycle.set_status(memory.id, "deleted")
        with self.assertRaises(NotFoundError):
            self.lifecycle.archive_memory("mem_does_not_exist")

    def test_5b_same_status_is_a_noop_and_writes_nothing(self) -> None:
        memory = self.make_memory(title="重复归档", status=MemoryStatus.ARCHIVED)
        report = self.lifecycle.archive_memory(memory.id)
        self.assertFalse(report.changed)
        self.assertIn("already archived", report.reason)
        stored = self.fresh().require_memory(memory.id)
        self.assertEqual(stored.updated_at, memory.updated_at)
        self.assertEqual(stored.as_dict(), memory.as_dict())

        active = self.make_memory(title="重复激活")
        self.assertFalse(self.lifecycle.activate_memory(active.id).changed)

    def test_5c_transition_table_is_exposed(self) -> None:
        from personal_memory import ALLOWED_TRANSITIONS, allowed_transitions, can_transition

        self.assertEqual(ALLOWED_TRANSITIONS["active"], ("archived",))
        self.assertEqual(ALLOWED_TRANSITIONS["pending"], ("active", "archived"))
        self.assertEqual(ALLOWED_TRANSITIONS["archived"], ("active",))
        self.assertTrue(can_transition("pending", "active"))
        self.assertFalse(can_transition("active", "pending"))
        self.assertEqual(allowed_transitions(MemoryStatus.ARCHIVED), ("active",))
        memory = self.make_memory(title="读取转换表")
        self.assertEqual(self.lifecycle.allowed_transitions(memory.id), ("archived",))

    # -- 6. delete keeps Sources ------------------------------------------
    def test_6_deleting_a_memory_keeps_its_sources(self) -> None:
        source = self.make_source(title="保留来源", content="来源正文 keep-alpha")
        other = self.make_source(title="第二个来源", content="第二个来源正文 keep-beta")
        memory = self.make_memory(title="将被删除的记忆", content="删除目标 body")
        self.repo.link(memory.id, source.id)
        self.repo.link(memory.id, other.id)

        report = self.lifecycle.delete_memory(memory.id)

        self.assertTrue(report.deleted)
        self.assertEqual(report.links_removed, 2)
        self.assertEqual(report.links_remaining, 0)
        self.assertEqual(sorted(report.sources_kept), sorted([source.id, other.id]))
        self.assertEqual(report.sources_deleted, ())
        self.assertTrue(report.sources_intact)

        fresh = self.fresh()
        self.assertIsNone(fresh.get_memory(memory.id))
        self.assertEqual(fresh.require_source(source.id).content, "来源正文 keep-alpha")
        self.assertEqual(fresh.require_source(other.id).content, "第二个来源正文 keep-beta")
        self.assertEqual(fresh.counts(), {"sources": 2, "memories": 0, "memory_sources": 0})
        self.assertTrue(fresh.index_consistency()["consistent"])

        with self.assertRaises(NotFoundError):
            self.lifecycle.delete_memory(memory.id)

    def test_16_deleting_a_memory_cleans_its_relation_rows(self) -> None:
        source = self.make_source(title="关系清理来源")
        memory = self.make_memory(title="关系清理记忆")
        self.repo.link(memory.id, source.id)
        self.assertEqual(self.repo.count_links_for_memory(memory.id), 1)

        self.lifecycle.delete_memory(memory.id)

        self.assertEqual(self.repo.count_links_for_memory(memory.id), 0)
        self.assertEqual(self.repo.link_count(), 0)
        # the Source row itself is untouched and still findable
        self.assertEqual(self.repo.get_memories_for_source(source.id), [])


class RepositoryTransitionGuardTest(RepositoryTestCase):
    """The transition table is enforced at the *repository* layer too.

    Phase 4 closeout: ``MemoryRepository.update_memory(status=...)`` could write
    ``active -> pending`` even though the lifecycle API refused it.  Both layers now
    read the ONE table in ``personal_memory.models``, while ``create_memory`` stays
    ungated so Memory Formation can still create ``pending`` Memories.
    """

    prefix = "pms-repo-transition-"

    def setUp(self) -> None:
        super().setUp()
        # a raw SQLite trigger makes "no write happened" provable rather than inferred
        # from a timestamp that could coincide within the same millisecond
        with self.database.transaction() as conn:
            conn.execute("CREATE TABLE audit_updates (id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL)")
            conn.execute(
                "CREATE TRIGGER audit_memories_update AFTER UPDATE ON memories "
                "BEGIN INSERT INTO audit_updates (memory_id) VALUES (old.id); END"
            )

    def update_count(self, memory_id: str) -> int:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM audit_updates WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        return int(row["n"])

    def raw_row(self, memory_id: str) -> tuple:
        with self.database.connection() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return tuple(row)

    # -- 1/2: illegal targets -------------------------------------------------
    def test_1_active_to_pending_is_refused_by_the_repository(self) -> None:
        memory = self.make_memory(title="仓库层非法转换 active", content="内容 repo-active-pending")
        before_row = self.raw_row(memory.id)
        before = self.repo.require_memory(memory.id).as_dict()

        with self.assertRaises(IllegalTransitionError) as caught:
            self.repo.update_memory(memory.id, status="pending")

        error = caught.exception
        self.assertEqual(error.memory_id, memory.id)
        self.assertEqual((error.from_status, error.to_status), ("active", "pending"))
        self.assertEqual(error.allowed, ("archived",))
        self.assertIn("illegal Memory status transition", str(error))
        self.assertEqual(self.raw_row(memory.id), before_row)
        self.assertEqual(self.repo.require_memory(memory.id).as_dict(), before)
        self.assertEqual(self.update_count(memory.id), 0)
        self.assertTrue(self.repo.index_consistency()["consistent"])

    def test_2_archived_to_pending_is_refused_by_the_repository(self) -> None:
        memory = self.make_memory(title="仓库层非法转换 archived", status=MemoryStatus.ARCHIVED)
        before_row = self.raw_row(memory.id)

        for target in ("pending", MemoryStatus.PENDING):
            with self.subTest(target=target):
                with self.assertRaises(IllegalTransitionError) as caught:
                    self.repo.update_memory(memory.id, status=target)
                self.assertEqual(caught.exception.allowed, ("active",))
                self.assertEqual(caught.exception.from_status, "archived")
                self.assertEqual(self.raw_row(memory.id), before_row)

        self.assertEqual(self.update_count(memory.id), 0)

    # -- 3-6: legal transitions keep working through the repository -----------
    def test_3_pending_to_active_works(self) -> None:
        memory = self.make_memory(title="仓库层合法转换 pending-active", status=MemoryStatus.PENDING)
        updated = self.repo.update_memory(memory.id, status="active")
        self.assertEqual(str(updated.status), "active")
        self.assertEqual(str(self.repo.require_memory(memory.id).status), "active")
        self.assertEqual(self.update_count(memory.id), 1)

    def test_4_pending_to_archived_works(self) -> None:
        memory = self.make_memory(title="仓库层合法转换 pending-archived", status=MemoryStatus.PENDING)
        updated = self.repo.update_memory(memory.id, status=MemoryStatus.ARCHIVED)
        self.assertEqual(str(updated.status), "archived")
        self.assertEqual(str(self.repo.require_memory(memory.id).status), "archived")

    def test_5_active_to_archived_works(self) -> None:
        memory = self.make_memory(title="仓库层合法转换 active-archived")
        updated = self.repo.update_memory(memory.id, status="archived")
        self.assertEqual(str(updated.status), "archived")
        self.assertEqual(self.update_count(memory.id), 1)

    def test_6_archived_to_active_works(self) -> None:
        memory = self.make_memory(title="仓库层合法转换 archived-active", status=MemoryStatus.ARCHIVED)
        updated = self.repo.update_memory(memory.id, status="active")
        self.assertEqual(str(updated.status), "active")
        self.assertEqual(str(self.repo.require_memory(memory.id).status), "active")

    # -- 7: same status is a no-op --------------------------------------------
    def test_7_same_status_update_is_a_noop_and_keeps_updated_at(self) -> None:
        memory = self.make_memory(title="仓库层同状态 no-op")

        unchanged = self.repo.update_memory(memory.id, status="active")

        self.assertEqual(unchanged.updated_at, memory.updated_at)
        self.assertEqual(unchanged.as_dict(), memory.as_dict())
        self.assertEqual(self.raw_row(memory.id), self.raw_row(memory.id))
        self.assertEqual(self.update_count(memory.id), 0)

        # mixing the current status with a real field change still applies the change
        changed = self.repo.update_memory(memory.id, status="active", title="改名成功")
        self.assertEqual(changed.title, "改名成功")
        self.assertEqual(str(changed.status), "active")
        self.assertGreaterEqual(changed.updated_at, memory.updated_at)
        self.assertEqual(self.update_count(memory.id), 1)

    # -- 8: the whole row survives a refused transition -----------------------
    def test_8_illegal_transition_leaves_the_row_relations_and_index_untouched(self) -> None:
        source = self.make_source(title="非法转换来源")
        memory = self.make_memory(title="非法转换整行不变", content="内容 illegal-row")
        self.repo.link(memory.id, source.id)
        before_row = self.raw_row(memory.id)
        before_memory = self.repo.require_memory(memory.id).as_dict()

        with self.assertRaises(IllegalTransitionError):
            self.repo.update_memory(memory.id, status="pending")

        self.assertEqual(self.raw_row(memory.id), before_row)
        self.assertEqual(self.repo.require_memory(memory.id).as_dict(), before_memory)
        self.assertEqual([s.id for s in self.repo.get_sources_for_memory(memory.id)], [source.id])
        self.assertTrue(self.repo.index_consistency()["consistent"])

        # a normal field update on the same row still works and keeps the status
        renamed = self.repo.update_memory(memory.id, title="改名后仍 active")
        self.assertEqual(renamed.title, "改名后仍 active")
        self.assertEqual(str(renamed.status), "active")

    # -- 9: Formation may still create pending --------------------------------
    def test_9_formation_can_still_create_a_pending_memory(self) -> None:
        payload = formation_payload(
            draft_payload(
                type="profile",
                title="用户可能更偏好结构化学习",
                content="根据用户多次强调原理，推断其偏好结构化学习。",
                information_origin="agent_inference",
                confidence=0.9,
            )
        )
        service = MemoryFormationService(self.repo, mock_client(response_for(payload)))

        outcome = service.process(RawInput(content="用户多次强调原理。"))

        self.assertEqual(outcome.status, "persisted")
        self.assertEqual(len(outcome.memories), 1)
        self.assertEqual(str(outcome.memories[0].status), "pending")
        self.assertEqual(str(self.repo.require_memory(outcome.memories[0].id).status), "pending")
        # an explicit insert is not gated either: creating a pending Memory is legitimate
        direct = self.make_memory(title="直接创建 pending", status=MemoryStatus.PENDING)
        self.assertEqual(str(direct.status), "pending")

    # -- 10: one table only ---------------------------------------------------
    def test_10_there_is_exactly_one_transition_table(self) -> None:
        from personal_memory import ALLOWED_TRANSITIONS as exported
        from personal_memory.lifecycle import ALLOWED_TRANSITIONS as lifecycle_table
        from personal_memory.models import ALLOWED_TRANSITIONS as models_table

        self.assertIs(exported, models_table)
        self.assertIs(lifecycle_table, models_table)
        self.assertEqual(
            dict(models_table),
            {"active": ("archived",), "pending": ("active", "archived"), "archived": ("active",)},
        )
        memory = self.make_memory(title="表检查")
        self.assertEqual(self.lifecycle_.allowed_transitions(memory.id), ("archived",))
        self.assertEqual(self.lifecycle_.set_status(memory.id, "archived").to_status, "archived")

    @property
    def lifecycle_(self) -> MemoryLifecycle:
        return MemoryLifecycle(self.repo)


class MemoryUpdateTest(RepositoryTestCase):
    """Required behaviours 7-13: validated field updates keep the index correct."""

    prefix = "pms-update-"

    def setUp(self) -> None:
        super().setUp()
        self.lifecycle = MemoryLifecycle(self.repo)
        self.retriever = MemoryRetriever(self.repo)

    def titles(self, query: str, **kwargs) -> list[str]:
        return [hit.memory.title for hit in self.retriever.search(query, **kwargs).hits]

    def test_7_update_content(self) -> None:
        memory = self.make_memory(title="更新内容", content="原始正文 original-body")
        updated = self.lifecycle.update_memory(memory.id, content="替换后的正文 replacement-body")
        self.assertEqual(updated.content, "替换后的正文 replacement-body")
        self.assertEqual(updated.id, memory.id)
        self.assertEqual(updated.created_at, memory.created_at)
        self.assertEqual(self.repo.require_memory(memory.id).content, "替换后的正文 replacement-body")
        self.assertGreaterEqual(updated.updated_at, memory.updated_at)

    def test_8_update_title(self) -> None:
        memory = self.make_memory(title="旧标题 old-title", content="内容 for title update")
        updated = self.lifecycle.update_memory(memory.id, title="新标题 new-title")
        self.assertEqual(updated.title, "新标题 new-title")
        self.assertEqual(self.repo.require_memory(memory.id).title, "新标题 new-title")

    def test_9_update_tags(self) -> None:
        memory = self.make_memory(title="更新标签", content="内容 for tags", tags=["oldtag"])
        updated = self.lifecycle.update_memory(memory.id, tags=["newtag", "second"])
        self.assertEqual(list(updated.tags), ["newtag", "second"])
        self.assertEqual(list(self.repo.require_memory(memory.id).tags), ["newtag", "second"])
        # an empty string clears the list (Phase 1 semantics)
        cleared = self.lifecycle.update_memory(memory.id, tags=[])
        self.assertEqual(list(cleared.tags), [])

    def test_10_update_importance(self) -> None:
        memory = self.make_memory(title="更新重要度", content="内容 for importance")
        updated = self.lifecycle.update_memory(memory.id, importance=0.95)
        self.assertAlmostEqual(updated.importance, 0.95)
        self.assertAlmostEqual(self.repo.require_memory(memory.id).importance, 0.95)

    def test_11_update_confidence(self) -> None:
        memory = self.make_memory(title="更新置信度", content="内容 for confidence")
        updated = self.lifecycle.update_memory(memory.id, confidence=0.2)
        self.assertAlmostEqual(updated.confidence, 0.2)
        self.assertAlmostEqual(self.repo.require_memory(memory.id).confidence, 0.2)

    def test_11b_update_summary_origin_and_type(self) -> None:
        memory = self.make_memory(title="更新其他字段", content="内容 for other fields")
        updated = self.lifecycle.update_memory(
            memory.id,
            summary="新的摘要",
            information_origin="source_content",
            type="knowledge",
        )
        self.assertEqual(updated.summary, "新的摘要")
        self.assertEqual(str(updated.information_origin), "source_content")
        stored = self.repo.require_memory(memory.id)
        self.assertEqual(stored.summary, "新的摘要")
        self.assertEqual(str(stored.information_origin), "source_content")
        # status through update obeys the same transition table as the named methods
        archived = self.lifecycle.update_memory(memory.id, status="archived")
        self.assertEqual(str(archived.status), "archived")
        with self.assertRaises(IllegalTransitionError):
            self.lifecycle.update_memory(memory.id, status="pending")

    def test_12_invalid_updates_are_rejected_and_change_nothing(self) -> None:
        memory = self.make_memory(title="非法更新", content="原始内容 original-content")
        before = self.repo.require_memory(memory.id).as_dict()

        bad_changes = (
            {"importance": 2.0},
            {"confidence": -1.0},
            {"importance": "high"},
            {"type": "invalid"},
            {"status": "deleted"},
            {"title": "   "},
            {"content": ""},
            {"tags": "not-a-list"},
            {"unknown_field": 1},
        )
        for changes in bad_changes:
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError):
                    self.lifecycle.update_memory(memory.id, **changes)
                self.assertEqual(self.repo.require_memory(memory.id).as_dict(), before)

        with self.assertRaises(NotFoundError):
            self.lifecycle.update_memory("mem_missing", importance=0.9)
        with self.assertRaises(NotFoundError):
            self.lifecycle.update_memory("mem_missing")

    def test_12b_lone_surrogates_are_rejected_instead_of_crashing(self) -> None:
        """A value SQLite cannot bind must be a typed rejection, not a raw UnicodeEncodeError."""
        memory = self.make_memory(title="代理项测试", content="内容 surrogate-guard")
        before = self.repo.require_memory(memory.id).as_dict()

        for changes in ({"tags": ["bad\ud800tag"]}, {"title": "bad\ud800title"}, {"content": "x\ud800"}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ValidationError):
                    self.lifecycle.update_memory(memory.id, **changes)
                self.assertEqual(self.repo.require_memory(memory.id).as_dict(), before)

        with self.assertRaises(ValidationError):
            self.make_memory(title="bad\ud800", content="content")

    def test_13_update_keeps_the_search_index_consistent(self) -> None:
        memory = self.make_memory(title="索引同步记忆", content="旧关键词 oldword")
        self.assertEqual(self.titles("oldword"), ["索引同步记忆"])

        self.lifecycle.update_memory(memory.id, content="新关键词 newword")

        self.assertEqual(self.titles("oldword"), [])
        self.assertEqual(self.titles("newword"), ["索引同步记忆"])
        self.assertTrue(self.repo.index_consistency()["consistent"])

    def test_13b_title_and_tag_updates_are_immediately_searchable(self) -> None:
        memory = self.make_memory(title="旧标题名称", content="正文 body-text", tags=["oldtag"])
        self.assertEqual(self.titles("oldtag"), ["旧标题名称"])

        self.lifecycle.update_memory(memory.id, title="新标题名称", tags=["newtag"])

        self.assertEqual(self.titles("oldtag"), [])
        self.assertEqual(self.titles("newtag"), ["新标题名称"])
        self.assertEqual(self.titles("旧标题名称"), [])
        self.assertEqual(self.titles("新标题名称"), ["新标题名称"])
        self.assertEqual(self.titles("body-text"), ["新标题名称"])
        self.assertTrue(self.repo.index_consistency()["consistent"])

    def test_13c_update_of_an_archived_memory_keeps_it_out_of_default_search(self) -> None:
        memory = self.make_memory(title="归档中更新", content="内容 archived-update")
        self.lifecycle.archive_memory(memory.id)
        self.lifecycle.update_memory(memory.id, content="内容 archived-update-two")
        self.assertEqual(self.titles("archived-update-two"), [])
        self.assertEqual(self.titles("archived-update-two", status="archived"), ["归档中更新"])


class LifecycleReadTest(RepositoryTestCase):
    """Read-side helpers used by the CLI and the completion criteria."""

    prefix = "pms-lifecycle-read-"

    def setUp(self) -> None:
        super().setUp()
        self.lifecycle = MemoryLifecycle(self.repo)

    def test_status_counts_and_pending_queue(self) -> None:
        self.make_memory(title="活动的")
        self.make_memory(title="等待的 A", status=MemoryStatus.PENDING)
        self.make_memory(title="等待的 B", status=MemoryStatus.PENDING)
        self.make_memory(title="归档的", status=MemoryStatus.ARCHIVED)

        self.assertEqual(self.lifecycle.status_counts(), {"active": 1, "pending": 2, "archived": 1})
        pending = self.lifecycle.pending()
        self.assertEqual({memory.title for memory in pending}, {"等待的 A", "等待的 B"})
        self.assertEqual(self.lifecycle.pending(limit=1).__len__(), 1)

    def test_describe_reports_the_next_legal_moves(self) -> None:
        source = self.make_source(title="描述来源")
        memory = self.make_memory(title="描述记忆")
        self.repo.link(memory.id, source.id)

        described = self.lifecycle.describe(memory.id)

        self.assertEqual(described["status"], "active")
        self.assertEqual(described["allowed_transitions"], ["archived"])
        self.assertEqual(described["source_ids"], [source.id])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
