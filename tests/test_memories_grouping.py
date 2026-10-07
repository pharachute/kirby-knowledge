"""Phase 3：Memories 分组测试 —— 状态栏目 → Source 分组 → Memory 卡片。"""

from __future__ import annotations

import re
import unittest

from personal_memory import MemoryStatus

from .llm_fakes import response_for
from .test_web import VALID_CAPTURE_PAYLOAD, WebTestCase

CARD_RE = re.compile(r'<div class="memory-card">|<a class="memory-card"[^>]*>', re.S)


def cards_of(body: str) -> list[str]:
    """Split the page into individual memory-card blocks (non-nested markup)."""
    starts = [match.start() for match in CARD_RE.finditer(body)]
    blocks = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(body)
        blocks.append(body[start:end])
    return blocks


def card_block(body: str, marker: str) -> str:
    for block in cards_of(body):
        if marker in block:
            return block
    raise AssertionError(f"card with {marker!r} not found")


class MemoryGroupingTest(WebTestCase):
    """同一材料产生的记忆聚在一起；不同材料明确分开。"""

    prefix = "pms-memgroup-"

    def setUp(self) -> None:
        super().setUp()
        self.source_a = self.make_source(title="材料 A：RAG 实践要点", content="RAG 的工程要点。")
        self.source_b = self.make_source(title="材料 B：PEP 20 禅意", content="Python 的设计原则。")
        self.a1 = self.make_memory(title="RAG 的三个工程关键点", content="切分、重排、引用。")
        self.a2 = self.make_memory(title="RAG 的评估指标", content="离线召回与在线准确率。")
        self.a3 = self.make_memory(title="RAG 的核心机制", content="检索外部知识补上下文。")
        for memory in (self.a1, self.a2, self.a3):
            self.repo.link(memory.id, self.source_a.id)
        self.b1 = self.make_memory(title="Zen of Python 的禅意", content="简洁优于复杂。")
        self.repo.link(self.b1.id, self.source_b.id)
        self.lonely = self.make_memory(title="我自己说的话", content="没有对应的材料。")
        self.pending = self.make_memory(title="待确认的一条", content="还不确定要不要长期记住。",
                                       status=MemoryStatus.PENDING)
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    @staticmethod
    def section_block(body: str, heading: str) -> str:
        """Return the <section> whose heading matches (status sections are flat)."""
        start = body.index(f"<h2>{heading}")
        end = body.index("</section>", start)
        return body[start:end]

    # -- A/B：同一材料聚合、不同材料分开 --------------------------------
    def test_memories_of_one_source_share_one_group_head(self) -> None:
        _, body = self.get("/memories?status=all")

        # 分组头只出现一次（title 属性里也带标题，所以数 <a> 的可见文本）
        self.assertEqual(body.count(">材料 A：RAG 实践要点</a>"), 1)
        self.assertIn("3 条记忆", body)
        self.assertEqual(body.count(f'href="/sources/{self.source_a.id}"'), 1)
        self.assertIn("材料 B：PEP 20 禅意", body)
        self.assertIn("1 条记忆", body)
        # 三张卡片都在自己的分组里
        for memory in (self.a1, self.a2, self.a3):
            self.assertIn(f'href="/memories/{memory.id}"', body)
        # 「已记住」栏目内按材料分隔：A、B、无来源 三组
        active_section = self.section_block(body, "已记住（")
        self.assertEqual(active_section.count('class="source-group-head"'), 3)
        self.assertEqual(active_section.count('class="source-group"'), 3)

    def test_group_head_shows_type_and_links_to_the_source(self) -> None:
        _, body = self.get("/memories?status=all")
        heads = re.findall(r'<div class="source-group-head">(.*?)</div>', body, re.S)
        head = next(item for item in heads if f'href="/sources/{self.source_a.id}"' in item)

        self.assertIn(f'href="/sources/{self.source_a.id}"', head)
        self.assertIn('class="status status-archived"', head)          # type_chip
        self.assertIn("文字", head)                                     # 中文类型名
        self.assertNotIn("内容", head)                                  # 分组头不塞正文

    def test_memories_without_a_source_are_not_lost(self) -> None:
        _, body = self.get("/memories?status=all")

        self.assertIn("没有来源的记忆", body)
        block = card_block(body, self.lonely.title)
        self.assertIn(f'href="/memories/{self.lonely.id}"', block)

    def test_no_source_group_is_rendered_last(self) -> None:
        _, body = self.get("/memories?status=all")
        self.assertLess(body.index("材料 A：RAG 实践要点"), body.index("没有来源的记忆"))

    # -- C：一个 Memory 多份来源绝不重复渲染 -----------------------------
    def test_memory_with_several_sources_is_rendered_once(self) -> None:
        extra = self.make_source(title="材料 C：补充依据", content="另一份依据。")
        self.repo.link(self.a1.id, extra.id)
        _, body = self.get("/memories?status=all")

        self.assertEqual(body.count(f'href="/memories/{self.a1.id}"'), 1)   # 只出现一次
        block = card_block(body, self.a1.title)
        self.assertIn("另有 1 份来源", block)                                # 提示而不是复制
        self.assertEqual(body.count("材料 C：补充依据"), 0)                  # 副来源不单独成组

    # -- D：状态只在栏目层 ------------------------------------------------
    def test_cards_do_not_repeat_the_status(self) -> None:
        _, body = self.get("/memories?status=all")

        # 卡片只有 标题 / 摘要 /（可选的）多来源提示；没有任何状态胶囊
        self.assertNotIn('class="memory-meta"', body)      # 本 fixture 中没有多来源记忆
        for block in re.findall(r'<div class="memory-title">(.*?)</div>', body, re.S):
            self.assertNotIn('class="status', block)
        # 状态仍然在页面层：栏目标题 + 筛选胶囊
        self.assertIn("已记住（", body)
        self.assertIn("待确认（", body)

    def test_status_sections_follow_the_filter(self) -> None:
        _, active = self.get("/memories?status=active")
        self.assertIn("已记住（", active)
        self.assertNotIn("待确认（", active)

        _, pending = self.get("/memories?status=pending")
        self.assertIn("待确认（", pending)
        self.assertNotIn("已记住（", pending)

        _, all_body = self.get("/memories?status=all")
        self.assertLess(all_body.index("已记住（"), all_body.index("待确认（"))
        self.assertIn('href="/memories?status=archived"', all_body)   # 胶囊仍可切换

    def test_section_kicker_explains_the_status(self) -> None:
        _, body = self.get("/memories?status=all")
        self.assertIn("值得长期留下的记忆", body)
        self.assertIn("不确定要不要长期记住", body)

    # -- 空状态 ----------------------------------------------------------
    def test_empty_states_are_accurate(self) -> None:
        from personal_memory.web import views

        none_at_all = views.memories_page(memories=[], status_filter="active", type_filter="all",
                                         status_counts={"_total": 0}, total_shown=0)
        self.assertIn("这里还没有记忆", none_at_all)

        filtered = views.memories_page(memories=[], status_filter="archived", type_filter="all",
                                       status_counts={"_total": 5, "archived": 0}, total_shown=0)
        self.assertIn("这个状态下还没有记忆", filtered)
        self.assertIn('href="/"', filtered)
        self.assertNotIn("失败", filtered)
        self.assertNotIn("错误", filtered)

    # -- Design System 复用 ----------------------------------------------
    def test_grouping_reuses_design_system_components(self) -> None:
        _, body = self.get("/memories?status=all")
        self.assertIn('class="page-head"', body)          # page_head
        self.assertIn('<section class="section">', body)  # section
        self.assertIn('class="memory-list"', body)        # card_list
        self.assertIn('class="memory-card"', body)        # entity_card
        css = re.search(r"\.source-group-head[^}]*\}", body)
        self.assertIsNotNone(css)
        self.assertIn("var(--space-", css.group(0))
        self.assertNotIn('style="', body)


class MemoryDetailRegressionTest(WebTestCase):
    """详情页保持：内容 → 来自 → 相关记忆 → 返回。"""

    prefix = "pms-memdetail3-"

    def setUp(self) -> None:
        super().setUp()
        self.source = self.make_source(title="学习记录", content="记录内容。")
        self.memory = self.make_memory(title="主记忆", content="主记忆内容。")
        self.sibling = self.make_memory(title="同材料的另一条", content="兄弟内容。")
        self.repo.link(self.memory.id, self.source.id)
        self.repo.link(self.sibling.id, self.source.id)
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    def test_detail_keeps_source_evidence_and_related(self) -> None:
        status, body = self.get(f"/memories/{self.memory.id}")

        self.assertEqual(status, 200)
        self.assertIn("来自", body)
        self.assertIn(f'href="/sources/{self.source.id}"', body)
        self.assertIn("相关记忆", body)
        self.assertIn("同材料的另一条", body)
        self.assertIn('class="back"', body)

    def test_related_memories_never_include_the_current_one(self) -> None:
        _, body = self.get(f"/memories/{self.memory.id}")
        related = re.search(r"相关记忆</h2>(.*?)</section>", body, re.S).group(1)

        self.assertNotIn(f'href="/memories/{self.memory.id}"', related)
        self.assertIn(f'href="/memories/{self.sibling.id}"', related)

    def test_detail_status_still_visible_there(self) -> None:
        """状态在列表页交给栏目表达；详情页仍需要它（决定可执行的操作）。"""
        _, body = self.get(f"/memories/{self.memory.id}")
        self.assertIn('class="status status-active"', body)
        self.assertIn("已记住", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
