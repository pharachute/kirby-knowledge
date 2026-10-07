"""Phase 4：全局前端一致性收口测试。

只锁定「跨页面必须一致」的事实：令牌、无硬编码颜色、单一 h1、统一导航、无内联样式、
卡片圆角同一套令牌、状态组件复用、死代码不再回流。
"""

from __future__ import annotations

import re
import unittest

from personal_memory.web import components, views

from .llm_fakes import response_for
from .test_web import VALID_CAPTURE_PAYLOAD, WebTestCase

TOKENS = ("--color-bg", "--color-text", "--color-border", "--color-accent",
          "--text-heading", "--text-body", "--space-xl", "--radius-lg")


def css_of(body: str) -> str:
    return "\n".join(re.findall(r"<style>(.*?)</style>", body, re.S))


def page_css(body: str) -> str:
    return re.sub(r":root\s*\{.*?\}", "", css_of(body), flags=re.S)


class GlobalConsistencyTest(WebTestCase):
    prefix = "pms-global-"

    def setUp(self) -> None:
        super().setUp()
        self.source = self.make_source(title="材料标题", content="材料内容。")
        self.memory = self.make_memory(title="一条记忆", content="记忆内容。")
        self.repo.link(self.memory.id, self.source.id)
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    def pages(self) -> dict[str, str]:
        paths = {
            "capture": "/",
            "memories": "/memories?status=all",
            "memory-detail": f"/memories/{self.memory.id}",
            "sources": "/sources",
            "source-detail": f"/sources/{self.source.id}",
            "search-idle": "/search",
            "search-results": "/search?q=记忆",
            "search-empty": "/search?q=zzz-nothing",
            "error404": "/nope-404",
        }
        return {name: self.get(path)[1] for name, path in paths.items()}

    def test_every_page_uses_the_same_tokens(self) -> None:
        for name, body in self.pages().items():
            css = css_of(body)
            for token in TOKENS:
                self.assertIn(f"{token}:", css, f"{name} 缺少 {token}")

    def test_no_hardcoded_colors_outside_root_anywhere(self) -> None:
        allowed = {value.lower() for value in components.ILLUSTRATION_HEXES}
        for name, body in self.pages().items():
            leftovers = [hex_value for hex_value in re.findall(r"#[0-9a-fA-F]{3,6}\b", page_css(body))
                         if hex_value.lower() not in allowed]
            self.assertEqual(leftovers, [], f"{name}: {leftovers}")

    def test_every_page_has_exactly_one_h1(self) -> None:
        for name, body in self.pages().items():
            self.assertEqual(body.count("<h1>"), 1, name)

    def test_navigation_is_identical_everywhere(self) -> None:
        for name, body in self.pages().items():
            header = body[body.index("<header>"):body.index("</header>")]
            self.assertEqual(re.findall(r'<a [^>]*>([^<]+)</a>', header), ["喂知识", "我的记忆", "搜索"], name)

    def test_no_inline_styles_anywhere(self) -> None:
        for name, body in self.pages().items():
            self.assertNotIn('style="', body, name)

    def test_cards_share_one_radius_token(self) -> None:
        _, body = self.get("/memories?status=all")
        css = page_css(body)
        for selector in (".memory-card {", ".card {"):
            rule = re.search(re.escape(selector) + r"[^}]*\}", css)
            self.assertIsNotNone(rule, selector)
            self.assertIn("var(--radius-lg)", rule.group(0), selector)

    def test_status_and_empty_states_come_from_shared_components(self) -> None:
        _, memories = self.get("/memories?status=all")
        _, search = self.get("/search?q=zzz-nothing")

        self.assertIn(".status-active", page_css(memories))      # 状态色只有一处定义
        self.assertIn("var(--color-success-bg)", page_css(memories))
        self.assertIn('class="empty"', search)                    # 空状态复用 empty_state
        self.assertIn('class="btn primary"', search)

    def test_dead_legacy_css_and_helpers_are_gone(self) -> None:
        _, body = self.get("/memories?status=all")
        css = page_css(body)
        for dead in (".badge {", ".stat {", "table {", "pre {"):
            self.assertNotIn(dead, css, dead)
        for helper in ("_status_badge", "_kv", "_filter_links"):
            self.assertFalse(hasattr(views, helper), helper)

    def test_search_page_does_not_invent_new_visual_system(self) -> None:
        _, body = self.get("/search?q=记忆")
        css = page_css(body)
        # 最近搜索 / idle / stretched 卡片都只用令牌值
        for selector in (".recent-item", ".link-button", ".search-idle-title", ".memory-card.is-stretched"):
            self.assertIn(selector, css, selector)
        for rule in re.findall(r"\.recent-item[^}]*\}|\.link-button[^}]*\}|\.search-idle-title[^}]*\}", css):
            for value in re.findall(r"(?:color|background|border-radius|padding|font-size):\s*([^;]+);", rule):
                self.assertTrue("var(--" in value or value.strip() in {"0", "transparent", "inherit"}, f"{value} 未走令牌")

    def test_focus_and_disabled_states_exist_globally(self) -> None:
        _, body = self.get("/")
        css = css_of(body)
        self.assertIn(":focus-visible", css)
        self.assertIn("button:disabled", css)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
