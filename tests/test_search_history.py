"""Phase 4：最近搜索（localStorage）+ Search 状态测试。

搜索历史是纯前端能力：这些测试锁定「页面脚本契约」与「历史链接指向的 URL 真的能搜到东西」，
浏览器端的真实交互（去重 / 上限 8 / 刷新保留 / 点击执行）由真实浏览器验收覆盖。
"""

from __future__ import annotations

import re
import unittest
from urllib.parse import quote

from personal_memory import MemoryStatus

from .llm_fakes import response_for
from .test_web import VALID_CAPTURE_PAYLOAD, WebTestCase


class RecentSearchContractTest(WebTestCase):
    """最近搜索的标记与脚本契约（localStorage，最多 8 条，去重置顶）。"""

    prefix = "pms-searchhist-"

    def setUp(self) -> None:
        super().setUp()
        self.memory = self.make_memory(title="Agent 的核心能力", content="根据环境反馈调整下一步行动。")
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    def script(self) -> str:
        _, body = self.get("/search")
        return max(re.findall(r"<script>(.*?)</script>", body, re.S), key=len)

    def test_recent_markup_is_present_and_hidden_by_default(self) -> None:
        _, body = self.get("/search")

        self.assertIn('id="recent-searches"', body)
        self.assertIn("最近搜索", body)
        self.assertIn('id="recent-list"', body)
        self.assertIn('id="recent-clear"', body)
        # 没有记录（首次访问）时不显示整块
        self.assertIn('id="recent-searches" hidden', body)
        # 它只是搜索页的辅助信息，不进全局导航
        header = body[body.index("<header>"):body.index("</header>")]
        self.assertNotIn("最近搜索", header)

    def test_history_lives_in_localstorage_with_a_cap_of_eight(self) -> None:
        script = self.script()
        self.assertIn('"pkb.search.recent"', script)      # 本地存储，不落库
        self.assertIn("var MAX = 8;", script)
        self.assertIn("items.slice(0, MAX)", script)      # 超出后丢最旧
        self.assertNotIn("fetch(", script)                # 不新增任何后端接口

    def test_duplicate_terms_are_deduped_and_moved_to_front(self) -> None:
        script = self.script()
        self.assertIn("filter(function (item) {{ return item !== term; }})".replace("{{", "{").replace("}}", "}")
                      if False else "item !== term", script)
        self.assertIn("items.unshift(term)", script)      # 重复搜索：去重后置顶

    def test_clicking_a_history_item_navigates_and_executes(self) -> None:
        script = self.script()
        self.assertIn('link.href = "/search?q=" + encodeURIComponent(term)', script)
        self.assertIn("textContent = term", script)       # 搜索词不会被当成 HTML 注入
        # 历史链接指向的 URL 必须真的能搜到东西（服务端执行）
        _, body = self.get(f"/search?q={quote('Agent')}")
        self.assertIn(self.memory.title, body)

    def test_clear_only_clears_the_history(self) -> None:
        script = self.script()
        clear_block = script[script.index("clear.addEventListener"):script.index("if (form)")]
        self.assertIn("write([])", clear_block)
        self.assertIn("render()", clear_block)
        self.assertNotIn("fetch(", clear_block)           # 不碰 Source / Memory / 搜索数据
        # 清除按钮不会被当成搜索表单的一部分（type=button）
        _, body = self.get("/search")
        self.assertIn('<button type="button" class="link-button" id="recent-clear">清除</button>', body)


class SearchStateTest(WebTestCase):
    """Search 的五个状态：idle / searching / results / empty / error。"""

    prefix = "pms-searchstate-"

    def setUp(self) -> None:
        super().setUp()
        self.active = self.make_memory(title="向量检索的取舍", content="召回与精度的权衡。")
        self.pending = self.make_memory(title="待确认的量化笔记", content="量化让模型更小。",
                                        status=MemoryStatus.PENDING)
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    def test_idle_state_is_light_and_chinese(self) -> None:
        status, body = self.get("/search")

        self.assertEqual(status, 200)
        self.assertIn("搜索你的知识", body)
        self.assertIn("输入关键词开始寻找", body)
        self.assertIn('id="q"', body)
        self.assertNotIn("找到 0 条", body)
        self.assertNotIn('class="empty"', body)          # idle 不是空结果

    def test_searching_state_is_wired_to_submit(self) -> None:
        _, body = self.get("/search")
        self.assertIn('id="search-form"', body)
        self.assertIn('id="search-submit"', body)
        self.assertIn("搜索中……", body)

    def test_results_state_shows_count_and_cards(self) -> None:
        _, body = self.get("/search?q=向量")

        self.assertIn("找到", body)
        self.assertIn("条记忆", body)
        self.assertIn('class="memory-card is-stretched"', body)
        self.assertIn("向量检索的取舍", body)

    def test_empty_state_is_not_an_error(self) -> None:
        _, body = self.get("/search?q=zzz-nothing")

        self.assertIn("没有找到相关内容", body)
        self.assertIn("换个关键词试试", body)
        self.assertIn('href="/"', body)
        self.assertNotIn("失败", body)
        self.assertNotIn("错误", body)

    def test_error_state_uses_the_shared_empty_state(self) -> None:
        from personal_memory.web import views

        page = views.search_page(query="x", result=None, type_filter="all", status_filter="active",
                                 limit=10, error="换个关键词，或者稍后再试。")
        self.assertIn("搜索暂时出了点问题", page)
        self.assertIn('class="empty"', page)             # 复用 Phase 1 的状态组件
        self.assertNotIn("Traceback", page)

    def test_status_and_type_filters_still_work(self) -> None:
        _, active = self.get("/search?q=量化&status=active")
        _, all_status = self.get("/search?q=量化&status=all")

        self.assertNotIn(self.pending.title, active)     # 默认只搜已记住
        self.assertIn(self.pending.title, all_status)
        self.assertIn("待确认", all_status)

    def test_long_keyword_is_handled(self) -> None:
        long_term = "量子" * 200
        status, body = self.get(f"/search?q={quote(long_term)}")

        self.assertEqual(status, 200)
        self.assertIn("没有找到相关内容", body)           # 长词不报错，只是没结果


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
