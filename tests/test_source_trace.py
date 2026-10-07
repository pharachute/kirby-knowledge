"""Phase 3 tests: Memory → Source → 原稿 → Memory traceability.

Everything here goes through the real HTTP layer and the real repository methods
(``get_sources_for_memory`` / ``get_memories_for_source``); nothing is fabricated in the UI.
"""

from __future__ import annotations

import re
import unittest

from personal_memory import MemoryStatus

from .helpers import example_source
from .llm_fakes import response_for
from .test_web import WebTestCase, VALID_CAPTURE_PAYLOAD

FORBIDDEN_IN_SOURCE_UI = (
    "Source",
    "Sources",
    "Memory",
    "Memories",
    "metadata",
    "Metadata",
    "content_hash",
    "source_type",
    "active",
    "pending",
    "archived",
    "Importance",
    "Confidence",
)

MANUSCRIPT = (
    "第一段：卡比把这份来源的原文保存了下来。\n\n"
    "第二段：这里是真实保存的内容，没有重新生成。\n\n"
    "第三段：用户可以从这里回到它形成的记忆。"
)


def visible_text(body: str) -> str:
    body = re.sub(r"<style>.*?</style>", " ", body, flags=re.S)
    body = re.sub(r"<script>.*?</script>", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return body


class SourceTraceTest(WebTestCase):
    """从一个真实 Memory 出发，能走到原稿，再走回 Memory。"""

    prefix = "pms-srctrace-"

    def setUp(self) -> None:
        super().setUp()
        self.source = self.make_source(
            title="Agent 学习记录",
            content=MANUSCRIPT,
            metadata={"captured_from": "text", "api_key": "should-not-render"},
        )
        self.memory = self.make_memory(title="你正在学习人工智能 Agent", content="喜欢通过实际项目理解知识。")
        self.sibling = self.make_memory(title="你更喜欢通过实际项目学习", content="项目驱动。",
                                        status=MemoryStatus.PENDING)
        self.repo.link(self.memory.id, self.source.id)
        self.repo.link(self.sibling.id, self.source.id)
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    # ---- Memory → Source -------------------------------------------------
    def test_memory_detail_links_to_the_real_source(self) -> None:
        status, body = self.get(f"/memories/{self.memory.id}")
        self.assertEqual(status, 200)
        self.assertIn("来自", body)
        href = f"/sources/{self.source.id}"
        self.assertIn(f'href="{href}"', body)
        self.assertIn("Agent 学习记录", body)

        source_status, source_body = self.get(href)
        self.assertEqual(source_status, 200)
        self.assertIn("Agent 学习记录", source_body)
        # 不再从记忆页复制原稿
        self.assertNotIn("第一段：卡比把这份来源的原文保存了下来。", body)

    # ---- 原稿 ------------------------------------------------------------
    def test_manuscript_shows_the_real_saved_content(self) -> None:
        _, body = self.get(f"/sources/{self.source.id}")

        self.assertIn("原稿", body)
        for line in ("第一段：卡比把这份来源的原文保存了下来。", "第二段：这里是真实保存的内容，没有重新生成。",
                     "第三段：用户可以从这里回到它形成的记忆。"):
            self.assertIn(line, body, line)
        # 原稿在「卡比从这里记住了」之前
        self.assertLess(body.index("原稿"), body.index("卡比从这里记住了"))
        self.assertIn("以上是这份来源里真实保存的内容。", body)

    def test_manuscript_does_not_leak_internal_fields(self) -> None:
        _, body = self.get(f"/sources/{self.source.id}")

        # 内部字段名在 HTML 里一个都不该出现
        for field in ("content_hash", "source_type", "metadata", "Metadata", "updated_at", "schema_version"):
            self.assertNotIn(field, body, field)
        self.assertNotIn(self.source.content_hash[:12], body)
        self.assertNotIn("should-not-render", body)
        # 用户可见文字里不该出现英文枚举（class 名不算可见文字）
        text = visible_text(body)
        for word in FORBIDDEN_IN_SOURCE_UI:
            self.assertNotIn(word, text, word)
        self.assertIn("项内部信息未展示", body)
        self.assertNotIn("<table", body)
        self.assertNotIn("<pre>", body)                    # 可读的排版，不是代码块

    def test_source_type_is_chinese(self) -> None:
        for source_type, label in (("text", "文字"), ("chat", "对话"), ("article", "文章"),
                                   ("web", "网页"), ("file", "文件")):
            source = self.make_source(source_type=source_type, title=f"{label}来源", content=f"{label}的原稿内容。")
            status, body = self.get(f"/sources/{source.id}")
            self.assertEqual(status, 200, source_type)
            self.assertIn(label, body, source_type)
            self.assertNotIn(source_type, visible_text(body), source_type)

    # ---- Source → Memory -------------------------------------------------
    def test_source_lists_the_memories_it_formed(self) -> None:
        _, body = self.get(f"/sources/{self.source.id}")

        self.assertIn("卡比从这里记住了", body)
        self.assertNotIn("相关记忆", body)                  # §十：共享来源不等于语义相关
        self.assertIn("你正在学习人工智能 Agent", body)
        self.assertIn("你更喜欢通过实际项目学习", body)
        self.assertIn(f'href="/memories/{self.memory.id}"', body)
        self.assertIn(f'href="/memories/{self.sibling.id}"', body)
        self.assertIn("已记住", body)
        self.assertIn("待确认", body)

    def test_round_trip_memory_source_memory(self) -> None:
        _, memory_body = self.get(f"/memories/{self.memory.id}")
        source_href = re.search(rf'href="(/sources/{self.source.id})"', memory_body).group(1)

        _, source_body = self.get(source_href)
        back_href = re.search(rf'href="(/memories/{self.sibling.id})"', source_body).group(1)

        status, back_body = self.get(back_href)
        self.assertEqual(status, 200)
        self.assertIn("你更喜欢通过实际项目学习", back_body)
        self.assertIn(f'href="{source_href}"', back_body)   # 又回到同一份来源

    def test_back_link_prefers_history_then_falls_back(self) -> None:
        _, body = self.get(f"/sources/{self.source.id}")
        self.assertIn('id="back-link" href="/memories"', body)
        self.assertIn("history.back()", body)
        self.assertIn("← 返回", body)

    # ---- 空状态 ----------------------------------------------------------
    def test_source_without_memories_shows_a_chinese_empty_state(self) -> None:
        lonely = self.make_source(title="还没有形成记忆的来源", content="这份来源暂时没有形成记忆。")
        status, body = self.get(f"/sources/{lonely.id}")
        self.assertEqual(status, 200)
        self.assertIn("卡比还没有从这里留下记忆", body)
        self.assertNotIn("相关记忆", body)

    def test_source_without_content_shows_a_chinese_empty_state(self) -> None:
        """防御分支：Source 的 content 由 model 约束为非空，所以真实数据走不到这里。"""
        import types

        from personal_memory.web import views

        empty = types.SimpleNamespace(
            id="src_empty", source_type="text", title="没有内容的来源", content="", content_hash="x",
            url=None, metadata={}, created_at="x", updated_at=None,
        )
        page = views.source_detail_page(source=empty, memories=[])
        self.assertIn("暂时没有可以查看的原稿", page)
        self.assertIn("卡比还没有从这里留下记忆", page)

    # ---- 列表页与导航 ----------------------------------------------------
    def test_sources_list_is_cards_without_internals(self) -> None:
        status, body = self.get("/sources")
        self.assertEqual(status, 200)
        self.assertIn("Agent 学习记录", body)
        self.assertNotIn("<table", body)
        self.assertNotIn(self.source.content_hash[:12], body)
        self.assertIn("文字", body)                        # 中文类型标签，而不是 text

    def test_main_nav_has_no_sources_entry(self) -> None:
        for path in ("/", "/memories", f"/sources/{self.source.id}"):
            _, body = self.get(path)
            nav = body[body.index("<header>"):body.index("</header>")]
            self.assertNotIn("/sources", nav, path)
            for label in ("喂知识", "我的记忆", "搜索"):
                self.assertIn(label, nav, path)

    # ---- 回归 ------------------------------------------------------------
    def test_capture_home_and_memories_do_not_regress(self) -> None:
        home_status, home_body = self.get("/")
        self.assertEqual(home_status, 200)
        self.assertIn("把知识喂给我", home_body)
        self.assertEqual(home_body.count('class="kirby kirby-frame'), 6)

        list_status, list_body = self.get("/memories?status=all")
        self.assertEqual(list_status, 200)
        self.assertEqual(list_body.count('class="memory-card"'), 2)
        self.assertIn("我的记忆", list_body)

    def test_long_manuscript_is_readable_with_expand(self) -> None:
        long_source = self.make_source(title="很长的来源", content=("第一段开头。" + "内容" * 2500 + "\n\n第二段结尾。"))
        status, body = self.get(f"/sources/{long_source.id}")
        self.assertEqual(status, 200)
        self.assertIn("原稿", body)
        self.assertIn("展开剩余内容", body)
        self.assertIn("第二段结尾。", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
