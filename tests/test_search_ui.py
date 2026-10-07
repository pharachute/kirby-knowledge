"""Phase 4 tests: 搜索 UI (Chinese, Memory-first cards, real retrieval, source provenance)."""

from __future__ import annotations

import re
import unittest

from personal_memory import MemoryStatus

from .llm_fakes import response_for
from .test_web import WebTestCase, VALID_CAPTURE_PAYLOAD

#: 用户可见文案里禁止出现的内部/英文词汇（§十三）
FORBIDDEN_VISIBLE = (
    "Search", "Memory", "Memories", "Source", "FTS5", "LIKE", "trigram", "deterministic",
    "ranking", "metadata", "score", "bm25", "Content", "Limit",
)


def visible_text(body: str) -> str:
    body = re.sub(r"<style>.*?</style>", " ", body, flags=re.S)
    body = re.sub(r"<script>.*?</script>", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return " ".join(body.split())


class SearchPageTest(WebTestCase):
    """搜索：快速找回已经记住的知识。"""

    prefix = "pms-searchui-"

    def setUp(self) -> None:
        super().setUp()
        self.source = self.make_source(title="学习记录", content="记录：这周在学 Agent 的记忆系统设计。")
        self.memory = self.make_memory(title="你正在持续学习人工智能 Agent",
                                       content="更喜欢通过实际项目理解和掌握新东西。")
        self.repo.link(self.memory.id, self.source.id)
        self.other = self.make_memory(title="你更喜欢通过实际项目学习", content="项目驱动。")
        self.archived = self.make_memory(title="归档的 Agent 笔记", content="Agent 归档内容。",
                                         status=MemoryStatus.ARCHIVED)
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    # ---- 1. 默认页面 / 中文 ------------------------------------------------
    def test_default_page_is_minimal_and_chinese(self) -> None:
        status, body = self.get("/search")

        self.assertEqual(status, 200)
        self.assertIn("搜索", body)
        self.assertIn("你还记得什么？", body)
        self.assertIn('id="q"', body)
        self.assertIn('placeholder="输入你想找的内容……"', body)
        self.assertIn("<title>搜索", body)
        # 不密集：没有统计卡片、没有表格、没有技术信息
        self.assertNotIn("<table", body)
        self.assertNotIn('class="stat"', body)
        # 空查询不调用检索，也没有结果区块
        self.assertNotIn("找到 ", body)
        self.assertNotIn("没有找到", body)

    def test_no_english_technical_text_is_visible(self) -> None:
        for path in ("/search", "/search?q=Agent", "/search?q=zzz-nothing"):
            _, body = self.get(path)
            text = visible_text(body)
            for word in FORBIDDEN_VISIBLE:
                self.assertNotIn(word, text, f"{path}: {word}")
            # 内部枚举值只在 option 的 value 属性里，可见文字必须是中文
            for english in ("knowledge", "experience", "event", "profile", "active", "pending", "archived"):
                self.assertNotIn(english, text, f"{path}: {english}")

    # ---- 2/3. 结果来自真实 Retrieval，且是卡片不是表格 -----------------------
    def test_results_come_from_the_real_retriever_as_cards(self) -> None:
        status, body = self.get("/search?q=Agent")

        self.assertEqual(status, 200)
        self.assertIn("找到", body)
        self.assertIn('class="memory-card is-stretched"', body)   # 与 Memories 一致的整卡可点
        self.assertNotIn("<table", body)
        self.assertIn(self.memory.title, body)
        self.assertIn("更喜欢通过实际项目理解和掌握新东西。", body)
        self.assertIn("已记住", body)
        # 默认只搜 active：归档记忆不出现，status=all 才出现
        self.assertNotIn(self.archived.id, body)
        self.assertIn(self.archived.id, self.get("/search?q=Agent&status=all")[1])

    # ---- 4. Memory 可点击进入真实详情 --------------------------------------
    def test_memory_links_to_the_real_detail(self) -> None:
        _, body = self.get("/search?q=Agent")
        href = f"/memories/{self.memory.id}"
        self.assertIn(f'href="{href}"', body)

        status, detail = self.get(href)
        self.assertEqual(status, 200)
        self.assertIn(self.memory.title, detail)
        self.assertIn("来自", detail)

    # ---- 5. Source 出处轻量且可进入 ----------------------------------------
    def test_source_provenance_is_lightweight_and_clickable(self) -> None:
        _, body = self.get("/search?q=Agent")

        self.assertIn("来自：", body)
        self.assertIn(f'href="/sources/{self.source.id}"', body)
        self.assertIn("学习记录", body)
        source_status, source_body = self.get(f"/sources/{self.source.id}")
        self.assertEqual(source_status, 200)
        self.assertIn("原稿", source_body)

    def test_memory_without_sources_shows_no_provenance_line(self) -> None:
        _, body = self.get("/search?q=项目驱动")
        self.assertIn(self.other.title, body)
        # 那条记忆没有 Source，所以这一行不能出现（不假造出处）
        card = body[body.index(self.other.title):]
        card = card[: card.index("</div></div>") if "</div></div>" in card else 600]
        self.assertNotIn("来自：", card)

    def test_multiple_sources_are_summarised(self) -> None:
        extra_a = self.make_source(title="第二份来源", content="Agent 的另一份依据 A。")
        extra_b = self.make_source(title="第三份来源", content="Agent 的另一份依据 B。")
        self.repo.link(self.memory.id, extra_a.id)
        self.repo.link(self.memory.id, extra_b.id)

        _, body = self.get("/search?q=Agent")
        self.assertIn("来自：", body)
        self.assertIn("等 3 份来源", body)

    # ---- 6. 空结果 --------------------------------------------------------
    def test_empty_result_is_not_an_error(self) -> None:
        status, body = self.get("/search?q=zzz-nothing-matches")

        self.assertEqual(status, 200)
        self.assertIn("没有找到相关内容", body)
        self.assertIn("换个关键词试试", body)
        self.assertIn('href="/"', body)
        self.assertNotIn("失败", body)
        self.assertNotIn("错误", body)
        self.assertNotIn("没有找到相关内容", self.get("/search?q=Agent")[1])

    # ---- 7/8/9. 回归 -----------------------------------------------------
    def test_capture_home_does_not_regress(self) -> None:
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("把知识喂给我", body)
        self.assertEqual(body.count('class="kirby kirby-frame'), 6)

    def test_memories_page_does_not_regress(self) -> None:
        status, body = self.get("/memories?status=all")
        self.assertEqual(status, 200)
        self.assertIn("<h1>我的记忆</h1>", body)
        self.assertGreaterEqual(body.count('class="memory-card"'), 3)
        self.assertNotIn("<table", body)

    def test_source_page_does_not_regress(self) -> None:
        status, body = self.get(f"/sources/{self.source.id}")
        self.assertEqual(status, 200)
        self.assertIn("原稿", body)
        self.assertIn("卡比从这里记住了", body)

    # ---- 导航与筛选 -------------------------------------------------------
    def test_nav_and_filters(self) -> None:
        _, body = self.get("/search?q=Agent")
        nav = body[body.index("<header>"):body.index("</header>")]
        for label in ("喂知识", "我的记忆", "搜索"):
            self.assertIn(label, nav, label)
        self.assertNotIn("我的来源", nav)
        self.assertNotIn("对话", nav)          # Chat 尚未实现，不显示为可用
        # 更多条件里是中文选项，值是真实枚举
        self.assertIn("状态", body)
        self.assertIn('value="pending"', body)      # 真实枚举仍作为查询参数值保留
        for label in ("知识", "经历", "事件", "画像"):
            self.assertIn(label, body, label)

    def test_status_filter_accepts_chinese_labels(self) -> None:
        status, body = self.get("/search?q=Agent&status=all")
        self.assertEqual(status, 200)
        self.assertIn(self.archived.title, body)
        self.assertIn("已归档", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
