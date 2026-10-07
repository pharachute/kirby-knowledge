"""Phase 2 UI tests: 我的记忆 list + Memory Detail (Chinese, content-first)."""

from __future__ import annotations

import re
import unittest

from personal_memory import MemoryStatus

from .llm_fakes import response_for
from .test_web import WebTestCase, VALID_CAPTURE_PAYLOAD

#: 用户可见文案里不允许出现的英文（§十八）
FORBIDDEN = (
    "Memories",
    "Memory Manager",
    "Pending",
    "Active",
    "Archived",
    "Confidence",
    "Importance",
    "Metadata",
    "Schema",
    "Database",
    "Memory",
)


def visible_text(body: str) -> str:
    """Roughly the text a user sees: drop tags/attributes and inline styles/scripts."""
    body = re.sub(r"<style>.*?</style>", " ", body, flags=re.S)
    body = re.sub(r"<script>.*?</script>", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return body


class MemoriesListTest(WebTestCase):
    """我的记忆：第一眼只看到「卡比记住了什么」。"""

    prefix = "pms-memui-"

    def setUp(self) -> None:
        super().setUp()
        self.active = self.make_memory(title="你正在持续学习人工智能 Agent", content="更喜欢通过实际项目理解和掌握新东西。")
        self.pending = self.make_memory(title="你最近开始关注模型量化", content="正在逐渐成为持续学习的方向。",
                                        status=MemoryStatus.PENDING)
        self.archived = self.make_memory(title="上个月整理过的旧笔记", content="已经不再需要经常查看。",
                                         status=MemoryStatus.ARCHIVED)
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    def test_list_shows_real_content_as_cards(self) -> None:
        status, body = self.get("/memories")

        self.assertEqual(status, 200)
        self.assertIn("我的记忆", body)
        self.assertEqual(body.count('class="memory-card"'), 1)          # 默认只显示已记住
        self.assertIn("你正在持续学习人工智能 Agent", body)
        self.assertIn("更喜欢通过实际项目理解和掌握新东西。", body)
        self.assertIn("已记住", body)
        # 不是后台表格
        self.assertNotIn("<table", body)
        self.assertNotIn("<th", body)

    def test_list_does_not_render_internal_fields(self) -> None:
        _, body = self.get("/memories?status=all")
        text = visible_text(body)

        for internal in ("importance", "confidence", "created_at", "updated_at", "source_content",
                         "information_origin", "knowledge", "schema_version"):
            self.assertNotIn(internal, body.lower(), internal)
        # the id may appear only inside a link target, never as visible text
        self.assertNotIn(self.active.id, text)
        self.assertIn(f"/memories/{self.active.id}", body)

    def test_status_filter_chips_are_chinese_with_counts(self) -> None:
        for status_key, label, title in (
            ("active", "已记住", "你正在持续学习人工智能 Agent"),
            ("pending", "待确认", "你最近开始关注模型量化"),
            ("archived", "已归档", "上个月整理过的旧笔记"),
            ("all", "全部", "你正在持续学习人工智能 Agent"),
        ):
            code, body = self.get(f"/memories?status={status_key}")
            self.assertEqual(code, 200, status_key)
            self.assertIn(label, body, status_key)
            self.assertIn(title, body, status_key)
        _, all_body = self.get("/memories?status=all")
        for title in ("你正在持续学习人工智能 Agent", "你最近开始关注模型量化", "上个月整理过的旧笔记"):
            self.assertIn(title, all_body, title)
        self.assertNotIn("模型量化", self.get("/memories?status=active")[1])
        self.assertNotIn("Agent", self.get("/memories?status=archived")[1])

    def test_status_is_expressed_at_the_page_level(self) -> None:
        """Phase 3：状态由「状态栏目 + 筛选胶囊」表达，单条卡片不再重复状态。"""
        _, body = self.get("/memories?status=all")
        for label in ("待确认", "已记住", "已归档"):
            self.assertIn(label, body, label)              # 栏目标题 / 胶囊
        self.assertIn("已记住（", body)                     # 栏目层
        text = visible_text(body)
        for english in ("pending", "active", "archived"):
            self.assertNotIn(english, text, english)
        # 卡片里没有任何状态胶囊（meta 行只在多来源时出现）
        self.assertNotIn('class="memory-meta"', body)

    def test_empty_state_points_back_to_capture_home(self) -> None:
        """空状态：用视图层直接校验（真实空库由 /memories 首屏覆盖）。"""
        from personal_memory.web import views

        empty = views.memories_page(
            memories=[], status_filter="active", type_filter="all", status_counts={"_total": 0}, total_shown=0
        )
        self.assertIn("这里还没有记忆", empty)
        self.assertIn("去喂一点知识给卡比吧", empty)
        self.assertIn('href="/"', empty)
        self.assertNotIn("暂无", empty)
        self.assertNotIn("数据库", empty)
        self.assertNotIn("暂无数据库记录", empty)

    def test_no_english_ui_text_on_the_list(self) -> None:
        _, body = self.get("/memories?status=all")
        for word in FORBIDDEN:
            self.assertNotIn(word, body, word)
        # 标题就是「我的记忆」
        self.assertIn("<title>我的记忆", body)


class MemoryDetailTest(WebTestCase):
    """单条记忆：先看到内容，再看到依据与操作。"""

    prefix = "pms-memdetail-"

    def setUp(self) -> None:
        super().setUp()
        self.memory = self.make_memory(title="你正在持续学习人工智能 Agent",
                                       content="你正在持续学习人工智能 Agent。\n\n更喜欢通过实际项目理解和掌握新东西。")
        self.source = self.make_source(title="学习记录", content="记录：这周在学 Agent 的记忆系统设计。")
        self.repo.link(self.memory.id, self.source.id)
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    def test_content_comes_first_and_fully(self) -> None:
        status, body = self.get(f"/memories/{self.memory.id}")

        self.assertEqual(status, 200)
        self.assertIn(self.memory.title, body)
        self.assertIn("更喜欢通过实际项目理解和掌握新东西。", body)
        self.assertLess(body.index(self.memory.title), body.index("来自"))
        self.assertLess(body.index("来自"), body.index("归档"))
        self.assertNotIn("<table", body)

    def test_source_links_are_real_and_openable(self) -> None:
        _, body = self.get(f"/memories/{self.memory.id}")

        self.assertIn("来自", body)
        self.assertIn(f'href="/sources/{self.source.id}"', body)
        self.assertIn("学习记录", body)
        self.assertIn("📝", body)                          # 「来自」显示来源类型图标
        self.assertIn("文字", body)                        # ...以及中文类型名
        source_status, source_body = self.get(f"/sources/{self.source.id}")
        self.assertEqual(source_status, 200)
        self.assertIn("学习记录", source_body)

    def test_related_memories_only_when_a_real_link_exists(self) -> None:
        _, lonely = self.get(f"/memories/{self.memory.id}")
        self.assertNotIn("相关记忆", lonely)               # 只有一条记忆 → 不显示该区块

        sibling = self.make_memory(title="模型量化这样入门", content="先理解量化的基本取舍。")
        self.repo.link(sibling.id, self.source.id)
        _, body = self.get(f"/memories/{self.memory.id}")
        self.assertIn("相关记忆", body)
        self.assertIn("模型量化这样入门", body)
        self.assertIn(f'href="/memories/{sibling.id}"', body)

    def test_lifecycle_actions_follow_the_real_state_machine(self) -> None:
        active_id = self.memory.id
        _, active_body = self.get(f"/memories/{active_id}")
        self.assertIn("归档", active_body)
        self.assertNotIn("/restore", active_body)
        self.assertNotIn("/delete", active_body)          # 不提供删除作为主要操作

        archived = self.make_memory(title="已归档的记忆", content="旧内容。", status=MemoryStatus.ARCHIVED)
        _, archived_body = self.get(f"/memories/{archived.id}")
        self.assertIn("恢复", archived_body)
        self.assertIn(f"/memories/{archived.id}/restore", archived_body)

        pending = self.make_memory(title="待确认的记忆", content="也许是偏好。", status=MemoryStatus.PENDING)
        _, pending_body = self.get(f"/memories/{pending.id}")
        self.assertIn("记住", pending_body)
        self.assertIn("先不记", pending_body)
        self.assertIn(f"/memories/{pending.id}/activate", pending_body)
        self.assertIn(f"/memories/{pending.id}/archive", pending_body)

    def test_archive_then_restore_from_the_new_ui(self) -> None:
        status, body, _ = self.post(f"/memories/{self.memory.id}/archive", {})
        self.assertEqual(status, 200)
        self.assertIn("已归档", body)
        self.assertEqual(str(self.repo.require_memory(self.memory.id).status), "archived")

        status, body, _ = self.post(f"/memories/{self.memory.id}/restore", {})
        self.assertEqual(status, 200)
        self.assertIn("已恢复", body)
        self.assertEqual(str(self.repo.require_memory(self.memory.id).status), "active")

    def test_pending_can_be_remembered_or_set_aside(self) -> None:
        keep = self.make_memory(title="值得记住的偏好", content="喜欢简短回答。", status=MemoryStatus.PENDING)
        status, body, _ = self.post(f"/memories/{keep.id}/activate", {})
        self.assertEqual(status, 200)
        self.assertIn("已记住", body)
        self.assertEqual(str(self.repo.require_memory(keep.id).status), "active")

        drop = self.make_memory(title="先不记的内容", content="一次性的。", status=MemoryStatus.PENDING)
        status, body, _ = self.post(f"/memories/{drop.id}/archive", {})
        self.assertEqual(status, 200)
        self.assertIn("已归档", body)
        self.assertEqual(str(self.repo.require_memory(drop.id).status), "archived")

    def test_edit_is_available_but_secondary(self) -> None:
        _, body = self.get(f"/memories/{self.memory.id}")
        self.assertIn("修改这条记忆", body)
        self.assertIn(f'action="/memories/{self.memory.id}/update"', body)
        # 折叠在 details 里，不占第一眼
        self.assertLess(body.index("来自"), body.index("修改这条记忆"))

    def test_no_english_ui_text_on_the_detail(self) -> None:
        _, body = self.get(f"/memories/{self.memory.id}")
        for word in FORBIDDEN:
            self.assertNotIn(word, body, word)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
