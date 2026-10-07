"""Phase 1 Design System 测试：令牌、共享组件复用、以及“不许回退”的约束。

这些测试的作用是让后续 Phase 2/3 改页面时不能悄悄绕过 Design System：
* 每个页面都必须带令牌层，且样式表里除 ``:root`` 外不允许出现硬编码颜色；
* 共享组件必须真的在 ≥2 个页面被用上（而不是建了文件不用）；
* 组件输出必须转义、且卡片保留 meta / extra 槽位（1.1 扩展用）。
"""

from __future__ import annotations

import re
import unittest

from personal_memory.web import components, views
from personal_memory.web.components import (
    TOKENS_CSS,
    action_link,
    back_link,
    card_list,
    empty_state,
    entity_card,
    page_head,
    section,
    status_chip,
    type_chip,
)

from .llm_fakes import response_for
from .test_web import VALID_CAPTURE_PAYLOAD, WebTestCase

TOKEN_NAMES = (
    "--color-bg", "--color-surface", "--color-text", "--color-text-muted", "--color-border",
    "--color-accent", "--color-accent-strong", "--color-success", "--color-warning", "--color-error",
    "--text-display", "--text-heading", "--text-title", "--text-body", "--text-caption",
    "--space-xs", "--space-md", "--space-xl", "--space-3xl",
    "--radius-sm", "--radius-md", "--radius-lg", "--radius-pill",
    "--border", "--shadow-sm", "--layout-max", "--layout-gutter", "--font-sans",
)


def css_of(body: str) -> str:
    return "\n".join(re.findall(r"<style>(.*?)</style>", body, re.S))


def css_without_root(body: str) -> str:
    css = css_of(body)
    return re.sub(r":root\s*\{.*?\}", "", css, flags=re.S)


class TokenLayerTest(WebTestCase):
    """令牌层：每个页面都有，且页面样式不再硬编码颜色。"""

    prefix = "pms-ds-tokens-"

    def setUp(self) -> None:
        super().setUp()
        self.source = self.make_source(title="来源标题", content="原稿内容。")
        self.memory = self.make_memory(title="一条记忆", content="记忆内容。")
        self.repo.link(self.memory.id, self.source.id)
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    def pages(self) -> dict[str, str]:
        paths = ["/", "/memories?status=all", f"/memories/{self.memory.id}", "/search?q=记忆",
                 "/sources", f"/sources/{self.source.id}", "/nope-404"]
        return {path: self.get(path)[1] for path in paths}

    def test_every_page_carries_the_token_layer(self) -> None:
        for path, body in self.pages().items():
            css = css_of(body)
            self.assertIn(":root {", css, path)
            for token in TOKEN_NAMES:
                self.assertIn(f"{token}:", css, f"{path} 缺少令牌 {token}")

    def test_page_styles_have_no_hardcoded_colors(self) -> None:
        allowed = set(components.ILLUSTRATION_HEXES)
        for path, body in self.pages().items():
            leftovers = [h for h in re.findall(r"#[0-9a-fA-F]{3,6}\b", css_without_root(body))
                         if h.lower() not in {a.lower() for a in allowed}]
            self.assertEqual(leftovers, [], f"{path} 样式里还有硬编码颜色 {leftovers}")

    def test_no_inline_styles_anywhere(self) -> None:
        for path, body in self.pages().items():
            self.assertNotIn('style="', body, path)

    def test_tokens_are_the_only_source_of_radius_and_spacing(self) -> None:
        body = self.get("/memories?status=all")[1]
        css = css_without_root(body)
        for prop in ("border-radius", "padding", "margin-top"):
            for value in re.findall(rf"{prop}:\s*([^;]+);", css):
                stripped = value.strip()
                if stripped in {"0", "auto", "0 auto", "inherit"}:
                    continue
                # 1px 是发丝级度量（徽标上下内边距），不进 spacing scale
                tokens = [part for part in stripped.split() if part != "1px"]
                self.assertTrue(
                    not tokens or all("var(--" in part or part.startswith("0") for part in tokens),
                    f"{prop}: {value} 未走令牌",
                )

    def test_illustration_palette_stays_out_of_the_ui_tokens(self) -> None:
        # 卡比插画的粉色只出现在 _FEED_STYLE 的 .kirby-* 规则里，不进 :root。
        # 例外：#e79bb4 同时是 UI 语义色 --color-accent-strong 的取值，所以只排除另外三个。
        kirby_only = components.ILLUSTRATION_HEXES - {"#e79bb4"}
        self.assertTrue(kirby_only, "插图例外集合不能为空")
        root = re.search(r":root\s*\{.*?\}", TOKENS_CSS, re.S).group(0)
        for hex_value in kirby_only:
            self.assertNotIn(hex_value, root)
        feed = self.get("/")[1]
        self.assertIn("#b5486f", css_of(feed))       # 插画色保留在页面样式里


class SharedComponentTest(unittest.TestCase):
    """组件本身的输出契约（转义、槽位、可扩展）。"""

    def test_entity_card_slots(self) -> None:
        card = entity_card(
            title="标题", href="/memories/1", glyph="🧠", excerpt="摘要",
            meta_html=status_chip("active"), extra_html='<div class="tags">#标签</div>',
        )
        self.assertIn('class="memory-card"', card)
        self.assertIn('href="/memories/1"', card)
        self.assertIn('class="glyph"', card)
        self.assertIn('class="memory-excerpt"', card)
        self.assertIn('class="memory-meta"', card)
        self.assertIn('class="tags"', card)          # extra 槽位未来放标签/预览/合并
        self.assertIn("已记住", card)

    def test_entity_card_is_a_div_when_not_clickable(self) -> None:
        card = entity_card(title="标题", title_href="/memories/1")
        self.assertTrue(card.startswith('<div class="memory-card">'))
        self.assertIn('class="card-title-link"', card)

    def test_components_escape_untrusted_text(self) -> None:
        payload = '<script>alert("x")</script>'
        text_slots = (
            page_head(payload, payload),
            empty_state(payload, payload),
            entity_card(title=payload, excerpt=payload, glyph=payload, title_href="/m/1"),
            section(payload, ""),
            action_link(payload, "/"),
            back_link("/", payload),
        )
        for html in text_slots:
            self.assertNotIn("<script>", html)
        # body 槽位是模板参数（由调用方拼装安全 HTML），这一点在文档里写明
        self.assertIn("<p>x</p>", section("标题", "<p>x</p>"))

    def test_status_and_type_chips_are_chinese(self) -> None:
        self.assertIn("待确认", status_chip("pending"))
        self.assertIn("已归档", status_chip("archived"))
        self.assertIn("📝", type_chip("text"))
        self.assertIn("网页", type_chip("web"))

    def test_card_list_wraps_in_the_shared_container(self) -> None:
        self.assertEqual(card_list("<p>x</p>"), '<div class="memory-list"><p>x</p></div>')

    def test_labels_live_in_the_component_layer(self) -> None:
        self.assertIs(views.memory_status_label, components.memory_status_label)
        self.assertIs(views.source_type_label, components.source_type_label)
        self.assertIs(views._memory_status_chip, components.status_chip)


class ComponentReuseTest(WebTestCase):
    """共享组件必须真的在 ≥2 个页面被使用（而不是建了文件不用）。"""

    prefix = "pms-ds-reuse-"

    def setUp(self) -> None:
        super().setUp()
        self.source = self.make_source(title="来源标题", content="原稿内容。")
        self.memory = self.make_memory(title="一条记忆", content="记忆内容。")
        self.repo.link(self.memory.id, self.source.id)
        self.memory2 = self.make_memory(title="另一条记忆", content="内容二。")
        self.base = self.start(self.make_context(response_for(VALID_CAPTURE_PAYLOAD)))

    def usage(self, marker: str) -> list[str]:
        paths = {
            "memories": "/memories?status=all",
            "memory-detail": f"/memories/{self.memory.id}",
            "sources": "/sources",
            "source-detail": f"/sources/{self.source.id}",
            "search": "/search?q=记忆",
            "feed": "/",
        }
        return [name for name, path in paths.items() if marker in self.get(path)[1]]

    def test_page_head_is_reused_on_three_pages(self) -> None:
        users = self.usage('class="page-head"')
        self.assertGreaterEqual(len(users), 3, users)
        self.assertIn("memories", users)
        self.assertIn("search", users)

    def test_card_and_list_are_reused_across_pages(self) -> None:
        for marker in ('class="memory-card"', 'class="memory-list"', 'class="memory-excerpt"'):
            users = self.usage(marker)
            self.assertGreaterEqual(len(users), 2, f"{marker} -> {users}")

    def test_section_component_is_reused_on_both_detail_pages(self) -> None:
        users = self.usage('<section class="section">')
        self.assertIn("memory-detail", users)
        self.assertIn("source-detail", users)

    def test_back_link_is_reused_on_both_detail_pages(self) -> None:
        users = self.usage('class="back"')
        self.assertIn("memory-detail", users)
        self.assertIn("source-detail", users)

    def test_empty_state_is_reused(self) -> None:
        # 搜不到结果 + 空“我的记忆”都要走同一个空状态组件
        empty_search = self.get("/search?q=zzz-绝对搜不到")[1]
        self.assertIn('class="empty"', empty_search)
        self.assertIn("没有找到相关内容", empty_search)
        for memory in (self.memory, self.memory2):
            self.repo.delete_memory(memory.id)
        body = self.get("/memories?status=all")[1]
        self.assertIn('class="empty"', body)
        self.assertIn("这里还没有记忆", body)
        self.assertIn('class="btn primary"', body)     # 空状态里的主行动来自 action_link

    def test_type_chip_replaces_the_handwritten_source_badge(self) -> None:
        body = self.get("/sources")[1]
        self.assertIn('class="status status-archived"', body)
        self.assertIn("文字", body)
        self.assertNotIn("status-archived\">📄 文件<", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
