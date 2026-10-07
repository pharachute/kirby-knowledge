"""KB 1.0 Design System：设计令牌 + 共享基础组件（服务端渲染，无框架、无构建步骤）。

Phase 1 只落地**会在多个页面复用**的东西：

* ``TOKENS_CSS``：唯一的设计令牌来源（颜色 / 字体 / 字号 / 间距 / 圆角 / 边框 / 阴影 / 布局）。
* 组件函数：``page_head`` / ``section`` / ``empty_state`` / ``entity_card`` / ``card_list`` /
  ``back_link`` / ``action_link`` / ``status_chip`` / ``type_chip``。

三条约定（详细理由见 ``docs/design-system.md``）：

1. 页面样式表仍然由各自页面持有布局规则，但**颜色与尺寸只能引用 ``var(--...)``**，
   不允许再出现新的硬编码色值。
2. 实体卡片的基类仍然是 ``.memory-card``：它同时承载 Memory / Source / 搜索结果，
   四处测试断言了这个 class 字符串，改名留到 Phase 2/3 真正重排页面时再做（命名债已记录）。
3. 卡比插画（``.kirby-*`` 的粉色系）属于插图色板，不是 UI 调色板，因此不纳入令牌。
"""

from __future__ import annotations

from html import escape
from typing import Any, Mapping

#: 卡比插画自带的颜色：属于插图色板，刻意不纳入 UI 令牌（--color-accent-strong 的取值
#: 恰好与 #e79bb4 相同，但那是 UI 语义，不是插画语义）。
ILLUSTRATION_HEXES = frozenset({"#e79bb4", "#b5486f", "#7d5060", "#e7c6d1"})

__all__ = [
    "TOKENS_CSS",
    "ILLUSTRATION_HEXES",
    "MEMORY_STATUS_LABELS",
    "SOURCE_TYPE_LABELS",
    "memory_status_label",
    "source_type_label",
    "status_chip",
    "type_chip",
    "page_head",
    "section",
    "empty_state",
    "action_link",
    "back_link",
    "card_list",
    "entity_card",
]

# --------------------------------------------------------------------------
# 设计令牌：数值全部取自 audit 出来的现有真实取值（见 docs/design-system.md）
# --------------------------------------------------------------------------
TOKENS_CSS = """
:root {
  color-scheme: light;
  /* -- color：语义色，页面一律引用这些变量 */
  --color-bg: #f6f6f4;
  --color-surface: #fff;
  --color-surface-subtle: #fbfbf9;
  --color-surface-muted: #e9e9e5;
  --color-border: #e6e6e1;
  --color-border-subtle: #ececea;
  --color-border-strong: #dcdcd8;
  --color-text: #1b1b1b;
  --color-text-secondary: #4a4a46;
  --color-text-muted: #6a6a6a;
  --color-text-faint: #8a8a84;
  --color-accent: #f7b8cb;
  --color-accent-strong: #e79bb4;
  --color-accent-ink: #4a2130;
  --color-accent-link: #7a4a5c;
  --color-success: #2f6b34;
  --color-success-bg: #dff0dd;
  --color-warning: #8a6a1c;
  --color-warning-bg: #fdf0cf;
  --color-error: #c62828;
  --color-error-bg: #fdeaea;
  --color-info: #1565c0;
  --color-info-bg: #eaf2fd;
  --color-header-bg: #1f2933;
  --color-header-text: #fff;
  --color-header-link: #cfe3ff;
  --color-focus: #e79bb4;
  /* -- typography */
  --font-sans: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
  --text-display: 26px;
  --text-heading: 24px;
  --text-title: 21px;
  --text-subtitle: 16px;
  --text-body: 15px;
  --text-body-small: 14px;
  --text-caption: 13px;
  --text-label: 12px;
  --weight-regular: 400;
  --weight-medium: 600;
  --weight-bold: 700;
  --leading-tight: 1.5;
  --leading-normal: 1.55;
  --leading-relaxed: 1.7;
  --leading-loose: 2.0;
  /* -- spacing（4px 节奏，取值覆盖现有页面实际用到的尺寸） */
  --space-3xs: 2px;
  --space-2xs: 4px;
  --space-xs: 6px;
  --space-sm: 8px;
  --space-md: 10px;
  --space-lg: 12px;
  --space-xl: 16px;
  --space-2xl: 20px;
  --space-3xl: 24px;
  --space-4xl: 32px;
  --space-5xl: 48px;
  /* -- radius */
  --radius-sm: 8px;
  --radius-md: 10px;
  --radius-lg: 12px;
  --radius-pill: 999px;
  --radius-circle: 50%;
  /* -- border / shadow */
  --border-width: 1px;
  --border: 1px solid var(--color-border);
  --border-subtle: 1px solid var(--color-border-subtle);
  --border-strong: 1px solid var(--color-border-strong);
  --border-dashed: 1px dashed var(--color-border-strong);
  --shadow-none: none;
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, .05);
  --shadow-md: 0 4px 12px rgba(0, 0, 0, .06);
  /* -- layout */
  --layout-max: 1120px;
  --layout-gutter: 18px;
  --layout-feed: 720px;
  --layout-form: 520px;
  --layout-control: 560px;
  --layout-card-gap: 16px;
  --layout-section-gap: 22px;
}
"""

# --------------------------------------------------------------------------
# 标签映射（中文文案的唯一来源）
# --------------------------------------------------------------------------
#: 用户可见的状态文案：UI 只说人话，内部枚举仍用 pending / active / archived。
MEMORY_STATUS_LABELS: Mapping[str, str] = {
    "pending": "待确认",
    "active": "已记住",
    "archived": "已归档",
}

#: 来源类型 → （图标, 中文名）。用于「来自」区块，点进去仍是既有 /sources/<id> 页面。
SOURCE_TYPE_LABELS: Mapping[str, tuple[str, str]] = {
    "text": ("📝", "文字"),
    "chat": ("💬", "对话"),
    "article": ("📰", "文章"),
    "web": ("🔗", "网页"),
    "file": ("📄", "文件"),
}


def memory_status_label(status: Any) -> str:
    return MEMORY_STATUS_LABELS.get(str(status), str(status))


def source_type_label(source_type: Any) -> tuple[str, str]:
    return SOURCE_TYPE_LABELS.get(str(source_type), ("📄", str(source_type)))


# --------------------------------------------------------------------------
# 基础组件（HTML 片段）
# --------------------------------------------------------------------------
def status_chip(status: Any) -> str:
    """记忆状态胶囊：``待确认 / 已记住 / 已归档``（颜色由 .status-* 提供）。"""
    return f'<span class="status status-{escape(str(status))}">{escape(memory_status_label(status))}</span>'


def type_chip(source_type: Any) -> str:
    """来源类型胶囊：``📝 文字`` / ``💬 对话`` …（与 status 胶囊同一套外观）。"""
    icon, label = source_type_label(source_type)
    return f'<span class="status status-archived">{escape(icon)} {escape(label)}</span>'


def page_head(title: str, lead: str | None = None) -> str:
    """页面标题 + 一行说明（Memories / Sources / Search 共用）。"""
    lead_html = f'<p class="lead">{escape(lead)}</p>' if lead else ""
    return f'<div class="page-head"><h1>{escape(title)}</h1>{lead_html}</div>'


def section(title: str, body: str) -> str:
    """详情页里的一个语义区块（标题 + 内容）。"""
    return f'<section class="section"><h2>{escape(title)}</h2>{body}</section>'


def action_link(label: str, href: str, *, primary: bool = False) -> str:
    """链接式动作。``primary=True`` 用于空状态里的主行动。"""
    classes = "btn primary" if primary else "btn"
    return f'<a class="{classes}" href="{escape(href)}">{escape(label)}</a>'


def back_link(href: str, label: str = "← 返回", *, link_id: str = "") -> str:
    """返回上层的链接（同源 referrer 时由页面脚本优先走浏览器历史）。"""
    id_attr = f' id="{escape(link_id)}"' if link_id else ""
    return f'<a class="back"{id_attr} href="{escape(href)}">{escape(label)}</a>'


def empty_state(
    title: str,
    hint: str = "",
    *,
    action_label: str | None = None,
    action_href: str | None = None,
) -> str:
    """统一空状态：标题 + 说明 + 可选的下一步动作。"""
    hint_html = f"<p>{escape(hint)}</p>" if hint else ""
    action_html = (
        action_link(action_label, action_href, primary=True)
        if action_label and action_href
        else ""
    )
    return f'<div class="empty"><h2>{escape(title)}</h2>{hint_html}{action_html}</div>'


def card_list(cards: str) -> str:
    """卡片列表容器（Memory / Source / 搜索结果共用同一个纵向列表）。"""
    return f'<div class="memory-list">{cards}</div>'


def entity_card(
    *,
    title: str,
    href: str | None = None,
    title_href: str | None = None,
    glyph: str = "",
    leading_html: str = "",
    modifier: str = "",
    excerpt: str = "",
    meta_html: str = "",
    extra_html: str = "",
) -> str:
    """实体卡片（Memory / Source / 搜索结果）。

    槽位刻意留宽，便于 1.1 扩展而不用改结构：

    * ``leading_html`` 预留：未来的多选控件（Memory Merge）等卡片前置元素
    * ``modifier``   附加 class（例如 ``is-stretched``：标题链接铺满整卡，卡片整体可点）
    * ``glyph``      类型图标（🧠 / 📝 …）
    * ``excerpt``    一行摘要
    * ``meta_html``  状态胶囊 + 「来自：…」等元信息
    * ``extra_html`` 预留：标签、图片缩略、来源预览、合并/演化操作

    ``href`` 让整张卡片可点（列表页）；``title_href`` 只在标题上挂链接，
    用于卡片内还需要放第二个链接的场景（搜索结果里的「来自」）。
    """
    classes = f"memory-card {modifier}".strip()
    if href:
        open_tag = f'<a class="{classes}" href="{escape(href)}">'
        close_tag = "</a>"
    else:
        open_tag = f'<div class="{classes}">'
        close_tag = "</div>"
    if title_href:
        title_html = f'<a class="card-title-link" href="{escape(title_href)}">{escape(title)}</a>'
    else:
        title_html = escape(title)
    glyph_html = f'<span class="glyph">{escape(glyph)}</span>' if glyph else ""
    excerpt_html = f'<p class="memory-excerpt">{escape(excerpt)}</p>' if excerpt else ""
    meta_html = f'<div class="memory-meta">{meta_html}</div>' if meta_html else ""
    return (
        f"{open_tag}"
        f'<div class="memory-title">{leading_html}{glyph_html}{title_html}</div>'
        f"{excerpt_html}{meta_html}{extra_html}{close_tag}"
    )
