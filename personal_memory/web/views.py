"""Server-rendered HTML for the local Knowledge Base UI (standard library only).

The views know nothing about the database: they render Python objects that the HTTP
layer obtained from the frozen modules.  Everything dynamic is escaped with
:func:`html.escape`, so user content (memories, chat text, sources) can never inject
markup into a page.

Styling is deliberately minimal (one small inline stylesheet, no framework, no build
step): the point of this MVP is to be usable, not to look designed.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .components import (
    TOKENS_CSS,
    MEMORY_STATUS_LABELS,
    SOURCE_TYPE_LABELS,
    action_link,
    back_link,
    card_list,
    empty_state,
    entity_card,
    memory_status_label,
    page_head,
    section,
    source_type_label,
    status_chip,
    type_chip,
)

__all__ = [
    "Flash",
    "escape",
    "layout",
    "memories_page",
    "memory_detail_page",
    "sources_page",
    "source_detail_page",
    "search_page",
    "error_page",
    "STATUS_CHOICES",
    "SOURCE_TYPE_CHOICES",
]

#: UI status filter values (``all`` means "no status filter"; the repository rejects
#: ``"all"``, so the HTTP layer maps it to ``None`` before calling).
STATUS_CHOICES: tuple[str, ...] = ("active", "pending", "archived", "all")
SOURCE_TYPE_CHOICES: tuple[str, ...] = ("text", "chat", "article", "web", "file", "all")

#: Metadata keys that must never be rendered (they may hold credentials/config).
_SENSITIVE_KEY = re.compile(r"key|token|secret|password|credential|authorization", re.IGNORECASE)

_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0;
       background: var(--color-bg); color: var(--color-text); font-size: var(--text-body); line-height: var(--leading-normal); }
header { background: var(--color-header-bg); color: var(--color-surface); padding: var(--space-md) var(--space-xl); }
header .brand { font-weight: var(--weight-bold); margin-right: var(--space-xl); }
header a { color: var(--color-header-link); text-decoration: none; margin-right: var(--space-xl); }
header a.active { color: var(--color-surface); font-weight: var(--weight-bold); text-decoration: underline; }
main { max-width: var(--layout-max); margin: 0 auto; padding: var(--space-xl); }
h1 { font-size: var(--text-title); margin: 0 0 var(--space-lg); }
h2 { font-size: var(--text-subtitle); margin: var(--space-xl) 0 var(--space-sm); }
.card { background: var(--color-surface); border: 1px solid var(--color-border-strong); border-radius: var(--radius-lg); padding: var(--space-xl) var(--space-xl);
        margin-bottom: var(--space-xl); }
.ok { background: var(--color-success-bg); border-left: 4px solid var(--color-success); padding: var(--space-md) var(--space-lg); margin-bottom: var(--space-xl); }
.err { background: var(--color-error-bg); border-left: 4px solid var(--color-error); padding: var(--space-md) var(--space-lg); margin-bottom: var(--space-xl); }
.info { background: var(--color-info-bg); border-left: 4px solid var(--color-info); padding: var(--space-md) var(--space-lg); margin-bottom: var(--space-xl); }
button, input[type=submit] { padding: var(--space-xs) var(--space-lg); font-size: var(--text-body-small); cursor: pointer; }
input[type=text], input[type=number], input[type=file], textarea, select {
      padding: var(--space-xs); width: 100%; max-width: var(--layout-control); font-family: inherit; font-size: var(--text-body-small); }
textarea { min-height: 170px; }
label { display: block; margin-top: var(--space-md); font-size: var(--text-caption); color: var(--color-text-secondary); }
.muted { color: var(--color-text-muted); font-size: var(--text-caption); }
.actions form { display: inline; }
.row { display: flex; gap: var(--space-xl); flex-wrap: wrap; }
.row > .card { flex: 1 1 250px; }
nav.filters a { margin-right: var(--space-md); }
footer { max-width: var(--layout-max); margin: 0 auto; padding: 0 var(--space-xl) var(--space-3xl); }
/* 统一交互状态：键盘焦点、禁用态、正文链接（Phase 1 新增，此前完全没有） */
:focus-visible { outline: 2px solid var(--color-focus); outline-offset: 2px; }
button:disabled, input:disabled, select:disabled, textarea:disabled { opacity: .55; cursor: not-allowed; }
main a { color: var(--color-accent-link); }
a.card-title-link { color: inherit; text-decoration: none; }
textarea.compact { min-height: 70px; }
"""


def escape(value: Any) -> str:
    """HTML-escape any value (``None`` becomes an empty string)."""
    return html.escape("" if value is None else str(value), quote=True)


def _text(value: Any, limit: int = 2000) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit:
        text = text[:limit] + f"\n… (共 {len(text)} 字符，已截断显示)"
    return text


@dataclass(frozen=True)
class Flash:
    """A one-shot message shown after a POST (stored in memory, never in the URL body)."""

    kind: str = "info"  # ok | err | info
    title: str = ""
    detail: str = ""
    hint: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)


def _banner(flash: Flash | None) -> str:
    if flash is None:
        return ""
    css = {"ok": "ok", "err": "err", "info": "info"}.get(flash.kind, "info")
    mark = {"ok": "✓", "err": "✗", "info": "•"}.get(flash.kind, "•")
    parts = [f'<div class="{css}"><strong>{mark} {escape(flash.title)}</strong>']
    if flash.detail:
        parts.append(f"<div>{escape(_text(flash.detail, 600))}</div>")
    if flash.hint:
        parts.append(f'<div class="muted">{escape(flash.hint)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _nav(current: str) -> str:
    """Chinese navigation: 喂知识 / 我的记忆 / 我的来源 / 搜索 (spec §十一).

    ``/capture`` and ``/import`` remain reachable from the footer so nothing regresses.
    """
    items = (
        ("feed", "/", "喂知识"),
        ("memories", "/memories", "我的记忆"),
        ("search", "/search", "搜索"),
    )
    links = "".join(
        f'<a href="{href}"{" class=\"active\"" if key == current else ""}>{label}</a>'
        for key, href, label in items
    )
    return f'<header><span class="brand">卡比的记忆小屋</span>{links}</header>'


def layout(
    *,
    title: str,
    body: str,
    current: str = "",
    flash: Flash | None = None,
    footer: str = "",
    heading: bool = True,
) -> str:
    """Shared page chrome.

    ``heading=False`` omits the chrome ``<h1>`` for pages that render their own content
    heading (the Memory detail page shows the memory itself as the first heading).
    """
    heading_html = f"<h1>{escape(title)}</h1>" if heading else ""
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)} · 卡比的记忆小屋</title>"
        f"<style>{TOKENS_CSS}{_STYLE}{_FEED_STYLE}{_MEMORY_STYLE}</style></head><body>"
        f"{_nav(current)}"
        f"<main>{heading_html}{_banner(flash)}{body}</main>"
        f"<footer>{footer}</footer>"
        "</body></html>\n"
    )


def _tags(tags: Iterable[Any]) -> str:
    items = [f'<span class="badge">{escape(tag)}</span>' for tag in tags]
    return " ".join(items) if items else '<span class="muted">—</span>'


def _source_excerpt(source: Any, limit: int = 110) -> str:
    """来源卡片的一行摘要（Source 没有 summary，只截断真实 content）。"""
    text = " ".join((getattr(source, "content", "") or "").split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text or "（这份来源没有保存内容）"


#: 组件层别名：卡片状态胶囊（保留旧名字，页面与测试的调用点都不用改）。
_memory_status_chip = status_chip


def _memory_excerpt(memory: Any, limit: int = 120) -> str:
    """列表里的一行摘要：优先 summary，否则安全截断 content（不重新生成摘要）。"""
    text = (memory.summary or "").strip() or (memory.content or "").strip()
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


#: 我的记忆页面的样式：安静、留白充足、卡片醒目，不做表格、不做仪表盘。
_MEMORY_STYLE = """
.page-head { margin: var(--space-2xs) 0 var(--space-xl); }
.page-head h1 { font-size: var(--text-heading); margin: 0 0 var(--space-2xs); font-weight: var(--weight-medium); }
.page-head .lead { color: var(--color-text-muted); font-size: var(--text-body-small); margin: 0; }
.chips { display: flex; flex-wrap: wrap; gap: var(--space-sm); margin: 0 0 var(--space-xl); }
.chip { display: inline-block; padding: var(--space-xs) var(--space-xl); border-radius: var(--radius-pill); background: var(--color-surface);
        border: 1px solid var(--color-border-strong); color: var(--color-text-secondary); text-decoration: none; font-size: var(--text-body-small); }
.chip em { font-style: normal; color: var(--color-text-faint); margin-left: var(--space-xs); font-size: var(--text-caption); }
.chip.active { background: var(--color-accent); border-color: var(--color-accent-strong); color: var(--color-accent-ink); font-weight: var(--weight-medium); }
.chip.active em { color: var(--color-accent-link); }
.memory-list { display: flex; flex-direction: column; gap: var(--space-xl); }
.memory-card { display: block; background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg);
               padding: var(--space-2xl) var(--space-3xl); text-decoration: none; color: inherit;
               transition: border-color .15s ease-out, transform .15s ease-out; }
.memory-card:hover { border-color: var(--color-accent-strong); transform: translateY(-1px); }
.memory-card .memory-title { font-size: var(--text-subtitle); font-weight: var(--weight-medium); line-height: var(--leading-tight); color: var(--color-text); }
.memory-card .glyph { margin-right: var(--space-sm); }
.memory-card .memory-excerpt { color: var(--color-text-secondary); font-size: var(--text-body-small); margin: var(--space-sm) 0 0; line-height: var(--leading-relaxed); }
.memory-card .memory-meta { margin-top: var(--space-lg); }
.status { display: inline-block; padding: var(--space-3xs) var(--space-lg); border-radius: var(--radius-pill); font-size: var(--text-caption);
          background: var(--color-surface-muted); color: var(--color-text-secondary); }
.status-active { background: var(--color-success-bg); color: var(--color-success); }
.status-pending { background: var(--color-warning-bg); color: var(--color-warning); }
.status-archived { background: var(--color-border-subtle); color: var(--color-text-faint); }
/* Source 分组：状态栏目 → 材料分组 → 记忆卡片 */
.section-kicker { color: var(--color-text-muted); font-size: var(--text-caption);
                  margin: 0 0 var(--space-xl); }
.source-group { margin: 0 0 var(--space-3xl); }
.source-group-head { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-md);
                     padding: 0 0 var(--space-md); margin: 0 0 var(--space-xl);
                     border-bottom: var(--border-subtle); }
.source-group-title { font-size: var(--text-body); font-weight: var(--weight-medium);
                      color: var(--color-text); text-decoration: none; max-width: 52ch;
                      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
a.source-group-title:hover { color: var(--color-accent-link); }
.source-group-count { color: var(--color-text-faint); font-size: var(--text-caption);
                      margin-left: auto; }
.empty { background: var(--color-surface); border: 1px dashed var(--color-border-strong); border-radius: var(--radius-lg); padding: var(--space-5xl) var(--space-3xl);
         text-align: center; }
.empty h2 { font-size: var(--text-subtitle); margin: 0 0 var(--space-xs); font-weight: var(--weight-medium); }
.empty p { color: var(--color-text-muted); margin: 0 0 var(--space-xl); }
.memory-detail { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg); padding: var(--space-3xl) var(--space-4xl); }
.memory-detail .kicker { color: var(--color-text-faint); font-size: var(--text-caption); margin-bottom: var(--space-md); }
.memory-detail h1 { font-size: var(--text-heading); margin: 0 0 var(--space-xl); line-height: var(--leading-tight); }
.memory-detail .body p { font-size: var(--text-body); line-height: var(--leading-loose); color: var(--color-text); margin: 0 0 var(--space-xl); }
.memory-detail .summary { margin-top: var(--space-xl); padding-top: var(--space-xl); border-top: 1px solid var(--color-border-subtle);
                          color: var(--color-text-secondary); font-size: var(--text-body-small); }
.memory-actions { margin-top: var(--space-3xl); display: flex; flex-wrap: wrap; gap: var(--space-md); align-items: center; }
.memory-actions form { display: inline; }
.btn { padding: var(--space-sm) var(--space-xl); border-radius: var(--radius-sm); border: 1px solid var(--color-border-strong); background: var(--color-surface);
       color: var(--color-text-secondary); font-size: var(--text-body-small); cursor: pointer; }
.btn:hover { border-color: var(--color-accent-strong); }
.btn.primary { background: var(--color-accent); border-color: var(--color-accent-strong); color: var(--color-accent-ink); font-weight: var(--weight-medium); }
.section { margin-top: var(--space-3xl); }
.section h2 { font-size: var(--text-subtitle); margin: 0 0 var(--space-lg); font-weight: var(--weight-medium); color: var(--color-text); }
.source-list, .related-list { list-style: none; margin: 0; padding: 0; display: flex;
                             flex-direction: column; gap: var(--space-md); }
.source-list a, .related-list a { display: flex; align-items: center; gap: var(--space-md); background: var(--color-surface);
    border: 1px solid var(--color-border); border-radius: var(--radius-md); padding: var(--space-lg) var(--space-xl); text-decoration: none;
    color: var(--color-text); font-size: var(--text-body); }
.source-list a:hover, .related-list a:hover { border-color: var(--color-accent-strong); }
.source-list .kind { color: var(--color-text-faint); font-size: var(--text-caption); }
details.edit { margin-top: var(--space-3xl); }
details.edit summary { cursor: pointer; color: var(--color-text-faint); font-size: var(--text-body-small); }
details.edit .edit-body { margin-top: var(--space-xl); background: var(--color-surface); border: 1px solid var(--color-border);
                          border-radius: var(--radius-lg); padding: var(--space-xl) var(--space-2xl); }
.back { display: inline-block; margin-bottom: var(--space-xl); color: var(--color-text-faint); text-decoration: none; font-size: var(--text-body-small); }
.back:hover { color: var(--color-accent-ink); }
.source-head { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg); padding: var(--space-3xl) var(--space-4xl); }
.source-head .kicker { color: var(--color-text-faint); font-size: var(--text-caption); margin-bottom: var(--space-sm); }
.source-head h1 { font-size: var(--text-title); margin: 0; line-height: var(--leading-normal); }
.manuscript { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg); padding: var(--space-3xl) var(--space-4xl); }
.manuscript p { font-size: var(--text-body); line-height: var(--leading-loose); color: var(--color-text); margin: 0 0 var(--space-xl);
                white-space: pre-wrap; overflow-wrap: anywhere; }
.manuscript p:last-child { margin-bottom: 0; }
.source-note { color: var(--color-text-faint); font-size: var(--text-caption); margin: var(--space-xl) 0 0; }
.source-link { display: inline-block; margin-top: var(--space-2xs); color: var(--color-accent-link); font-size: var(--text-body-small); }
details.more { margin-top: var(--space-xl); }
details.more summary { cursor: pointer; color: var(--color-text-faint); font-size: var(--text-body-small); }
.search-box { margin: 0 0 var(--space-xl); }
.search-box .field { display: flex; align-items: center; gap: var(--space-md); background: var(--color-surface);
                     border: 1px solid var(--color-border-strong); border-radius: var(--radius-pill); padding: var(--space-xs) var(--space-sm) var(--space-xs) var(--space-2xl); }
.search-box .field:focus-within { border-color: var(--color-accent-strong); }
.search-box input[type=search] { flex: 1; border: 0; outline: 0; font-size: var(--text-subtitle); padding: var(--space-md) 0;
                                 background: transparent; max-width: none; }
.search-box button { border: 1px solid var(--color-accent-strong); background: var(--color-accent); color: var(--color-accent-ink); font-size: var(--text-body);
                     border-radius: var(--radius-pill); padding: var(--space-sm) var(--space-xl); cursor: pointer; }
details.conditions { margin-top: var(--space-md); }
details.conditions summary { cursor: pointer; color: var(--color-text-faint); font-size: var(--text-caption); }
details.conditions .panel { display: flex; flex-wrap: wrap; gap: var(--space-xl); margin-top: var(--space-lg);
                            background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg); padding: var(--space-xl) var(--space-xl); }
details.conditions label { margin-top: 0; }
details.conditions select, details.conditions input[type=number] { max-width: 160px; }
.result-count { color: var(--color-text-muted); font-size: var(--text-caption); margin: 0 0 var(--space-lg); }
/* idle：还没搜索时的轻量状态 */
.search-idle { margin: var(--space-2xl) 0 0; }
.search-idle-title { font-size: var(--text-subtitle); color: var(--color-text); margin: 0 0 var(--space-2xs); }
/* 最近搜索（浏览器本地存储，最多 8 条） */
.recent { margin: 0 0 var(--space-2xl); }
.recent-head { display: flex; align-items: baseline; gap: var(--space-lg); margin: 0 0 var(--space-sm); }
.recent-head h2 { font-size: var(--text-body-small); font-weight: var(--weight-medium);
                  color: var(--color-text-secondary); margin: 0; }
.recent-list { display: flex; flex-wrap: wrap; gap: var(--space-sm); }
.recent-item { background: var(--color-surface); border: var(--border); border-radius: var(--radius-pill);
               padding: var(--space-2xs) var(--space-lg); color: var(--color-text-secondary);
               font-size: var(--text-caption); text-decoration: none; max-width: 24ch;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.recent-item:hover { border-color: var(--color-accent-strong); color: var(--color-accent-link); }
.link-button { border: 0; background: transparent; color: var(--color-text-faint); cursor: pointer;
               font-size: var(--text-caption); padding: 0; }
.link-button:hover { color: var(--color-accent-link); }
/* 搜索结果卡片：与「我的记忆」一致地整卡可点，但「来自」里的来源链接仍独立可点 */
.memory-card.is-stretched { position: relative; }
.memory-card.is-stretched .card-title-link::after { content: ""; position: absolute; inset: 0;
                                                    border-radius: inherit; }
.memory-card.is-stretched .from-line { position: relative; z-index: 1; }
.from-line { color: var(--color-text-faint); font-size: var(--text-caption); margin-left: var(--space-sm); }
.from-line a { color: var(--color-accent-link); text-decoration: none; }
.from-line a:hover { text-decoration: underline; }
"""


#: 页面级状态栏目：顺序即展示顺序。说明文字让用户理解「这一栏是什么」，
#: 因此单条卡片不再重复显示状态（状态由栏目表达）。
MEMORY_STATUS_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("active", "已记住", "卡比认为值得长期留下的记忆。"),
    ("pending", "待确认", "卡比还不确定要不要长期记住，先放在这里。"),
    ("archived", "已归档", "不再需要经常查看的记忆。"),
)


def _source_group(source: Any, entries: Sequence[tuple[Any, int]]) -> str:
    """一组来自同一份材料的记忆。

    ``source`` 为 ``None`` 表示这些记忆没有对应的材料（不能因此把它们藏起来）。
    ``entries`` 是 ``(memory, 除本组之外的来源数量)``：同一 Memory 有多份来源时只出现在
    主来源这一组里，并在卡片上提示「另有 N 份来源」，绝不重复渲染成多条 Memory。
    """
    if source is None:
        head = (
            '<span class="glyph">💭</span>'
            '<span class="source-group-title">没有来源的记忆</span>'
            '<span class="source-group-count">这些记忆没有对应的材料</span>'
        )
    else:
        # 类型胶囊自带图标，因此不再另放一个 glyph（避免同一信息出现两次）
        head = (
            f'{type_chip(source.source_type)}'
            f'<a class="source-group-title" href="/sources/{escape(source.id)}" '
            f'title="{escape(source.title or "无标题来源")}">'
            f'{escape(source.title or "无标题来源")}</a>'
            f'<span class="source-group-count">{len(entries)} 条记忆</span>'
        )
    cards = "".join(
        entity_card(
            title=memory.title,
            href=f"/memories/{escape(memory.id)}",
            glyph="🧠",
            excerpt=_memory_excerpt(memory),
            meta_html=(f'<span class="muted">另有 {extra} 份来源</span>' if extra else ""),
        )
        for memory, extra in entries
    )
    return f'<div class="source-group"><div class="source-group-head">{head}</div>{card_list(cards)}</div>'


def memories_page(
    *,
    memories: Sequence[Any],
    status_filter: str,
    type_filter: str,
    status_counts: Mapping[str, int],
    total_shown: int,
    sources_by_memory: Mapping[str, Sequence[Any]] | None = None,
    flash: Flash | None = None,
) -> str:
    """我的记忆：先看状态栏目，再看「这一组来自什么材料」，最后才是单条记忆。

    阅读顺序 = 页面标题 → 状态栏目 → Source 分组 → Memory 卡片。
    状态只在栏目层出现；卡片只有 标题 / 一行内容 /（多来源提示）。
    """
    sources_by_memory = sources_by_memory or {}
    total = status_counts.get("_total", len(memories))
    chips = []
    for key, label in (("all", "全部"), ("pending", "待确认"), ("active", "已记住"), ("archived", "已归档")):
        count = total if key == "all" else status_counts.get(key, 0)
        active = " active" if key == status_filter else ""
        chips.append(
            f'<a class="chip{active}" href="/memories?status={key}">{label}<em>{count}</em></a>'
        )

    sections_html: list[str] = []
    for status_key, label, blurb in MEMORY_STATUS_SECTIONS:
        if status_filter not in ("all", status_key):
            continue
        subset = [memory for memory in memories if str(memory.status) == status_key]
        if not subset:
            continue
        heads: dict[str, Any] = {}
        groups: dict[str, list[tuple[Any, int]]] = {}
        for memory in subset:
            sources = tuple(sources_by_memory.get(memory.id) or ())
            primary = sources[0] if sources else None
            group_key = str(primary.id) if primary is not None else ""
            heads.setdefault(group_key, primary)
            groups.setdefault(group_key, []).append((memory, max(0, len(sources) - 1)))
        # 有材料的先展示（按记忆列表顺序），"没有来源" 永远排在最后
        ordered = [key for key in groups if key != ""] + ([""] if "" in groups else [])
        groups_html = "".join(_source_group(heads[key], groups[key]) for key in ordered)
        sections_html.append(
            section(f"{label}（{len(subset)}）", f'<p class="section-kicker">{escape(blurb)}</p>{groups_html}')
        )

    if sections_html:
        listing = "".join(sections_html)
    elif total == 0:
        listing = empty_state(
            "这里还没有记忆", "去喂一点知识给卡比吧", action_label="去喂知识", action_href="/"
        )
    else:
        listing = empty_state(
            "这个状态下还没有记忆", "换个状态看看，或者去喂一点知识", action_label="去喂知识", action_href="/"
        )

    body = f"""
{page_head("我的记忆", "卡比记住的事情都在这里，按状态和材料分组；点开可以看它记住了什么、是从哪里记住的。")}
<nav class="chips">{"".join(chips)}</nav>
{listing}
"""
    return layout(title="我的记忆", body=body, current="memories", flash=flash, heading=False)


def memory_detail_page(
    *,
    memory: Any,
    sources: Sequence[Any],
    transitions: Sequence[str],
    related: Sequence[Any] = (),
    flash: Flash | None = None,
    learn_href: str | None = None,
) -> str:
    """单条记忆：第一眼是「卡比记住了什么」，然后才是依据与操作。

    「来自」只列真实关联的 Source（点进去是既有 /sources/<id> 页面）；
    「相关记忆」只在存在真实关联（共享来源）时显示，不做相似度算法。
    不提供删除；生命周期操作只使用既有 Web 接口（归档 / 恢复 / 记住 / 先不记）。
    ``learn_href`` 由路由层给出（P2D-2 的 Memory → Teacher 入口）：视图只渲染它，
    既不知道学习逻辑，也不构造学习 URL。
    """
    status = str(memory.status)
    paragraphs = [block.strip() for block in (memory.content or "").split("\n\n") if block.strip()]
    body_html = "".join(f"<p>{escape(block)}</p>" for block in paragraphs) or "<p>（这条记忆没有正文。）</p>"
    summary_html = (
        f'<div class="summary"><strong>一句话摘要</strong>：{escape(memory.summary)}</div>'
        if (memory.summary or "").strip()
        else ""
    )

    if sources:
        items = "".join(
            f'<li><a href="/sources/{escape(source.id)}">'
            f'<span>{icon}</span><span>{escape(source.title or "无标题来源")}</span>'
            f'<span class="kind">{escape(kind)}</span></a></li>'
            for source in sources
            for icon, kind in (source_type_label(source.source_type),)
        )
        from_html = f'<ul class="source-list">{items}</ul>'
    else:
        from_html = '<p class="muted">这条记忆没有关联来源（自我陈述型记忆通常不需要原始依据）。</p>'

    related_html = ""
    if related:
        links = "".join(
            f'<li><a href="/memories/{escape(other.id)}">'
            f'<span class="glyph">🧠</span><span>{escape(other.title)}</span>'
            f'<span class="kind">{escape(memory_status_label(other.status))}</span></a></li>'
            for other in related
        )
        related_html = section(
            "相关记忆",
            f'<ul class="related-list">{links}</ul>'
            '<p class="muted">这些记忆与当前这条来自同一份来源。</p>',
        )

    actions = []
    if "archived" in transitions and status != "archived":
        actions.append(("archive", "归档", ""))
    if "active" in transitions and status == "pending":
        actions.append(("activate", "记住", " primary"))
    if "active" in transitions and status == "archived":
        actions.append(("restore", "恢复", " primary"))
    if "archived" in transitions and status == "pending":
        actions.append(("archive", "先不记", ""))
    lifecycle_html = "".join(
        f'<form method="post" action="/memories/{escape(memory.id)}/{action}">'
        f'<button class="btn{style}" type="submit">{label}</button></form>'
        for action, label, style in actions
    )
    # 学习入口（P2D-2）：这条记忆是第一等公民，所以它排在生命周期操作之前。
    # 页面只拿到一个 URL；真正的会话创建/对话由 /learn 后面的产品入口调用 P2D-1 的
    # TeacherApplication 完成——这里不碰任何学习状态，也不知道学习逻辑。
    learn_html = (
        f'<a class="btn primary" href="{escape(learn_href)}">学习这一条</a>'
        if learn_href
        else ""
    )
    action_html = learn_html + lifecycle_html

    edit_form = f"""
<details class="edit">
  <summary>修改这条记忆</summary>
  <div class="edit-body">
    <p class="muted">校验、去重与状态机仍由既有的记忆逻辑负责；这里只是把既有接口摆出来。</p>
    <form method="post" action="/memories/{escape(memory.id)}/update">
      <label for="title">标题</label>
      <input type="text" id="title" name="title" value="{escape(memory.title)}" maxlength="200" required>
      <label for="content">内容</label>
      <textarea id="content" name="content" required>{escape(memory.content)}</textarea>
      <label for="summary">一句话摘要（可留空）</label>
      <textarea id="summary" name="summary" class="compact">{escape(memory.summary or "")}</textarea>
      <label for="tags">标签（英文逗号分隔）</label>
      <input type="text" id="tags" name="tags" value="{escape(', '.join(memory.tags))}">
      <label for="importance">重要度（0–1）</label>
      <input type="number" id="importance" name="importance" step="0.05" min="0" max="1"
             value="{escape(memory.importance)}">
      <label for="confidence">置信度（0–1）</label>
      <input type="number" id="confidence" name="confidence" step="0.05" min="0" max="1"
             value="{escape(memory.confidence)}">
      <label for="status">状态（只允许生命周期规则允许的转换）</label>
      <select id="status" name="status">
        {''.join(f'<option value="{escape(s)}"{" selected" if s == status else ""}>{escape(memory_status_label(s))}</option>' for s in (status, *transitions))}
      </select>
      <p><button class="btn" type="submit">保存修改</button></p>
    </form>
  </div>
</details>
"""
    # 阅读顺序：卡比记住了什么 → 从哪里记住的 → 相关记忆 → 可以做什么 → （折叠的）修改
    body = f"""
{back_link("/memories", "← 返回我的记忆")}
<article class="memory-detail">
  <div class="kicker"><span class="glyph">🧠</span> 记忆 · {_memory_status_chip(memory.status)}</div>
  <h1>{escape(memory.title)}</h1>
  <div class="body">{body_html}</div>
  {summary_html}
</article>
{section("来自", from_html)}
{related_html}
{section("可以做什么", f'<div class="memory-actions">{action_html}</div>')}
{edit_form}
"""
    return layout(title=memory.title, body=body, current="memories", flash=flash, heading=False)


def sources_page(*, sources: Sequence[Any], flash: Flash | None = None) -> str:
    """来源列表（不在主导航里，1.0 通过「我的记忆 → 来自」进入）。

    这里只做同一套视觉语言的卡片，不再显示 content_hash / 类型枚举等内部字段。
    """
    if sources:
        cards = "".join(
            entity_card(
                title=source.title or "无标题来源",
                href=f"/sources/{escape(source.id)}",
                glyph=source_type_label(source.source_type)[0],
                excerpt=_source_excerpt(source),
                meta_html=type_chip(source.source_type),
            )
            for source in sources
        )
        listing = card_list(cards)
    else:
        listing = empty_state(
            "还没有保存下来的来源",
            "卡比只会在需要原始依据时保存来源，去喂一点知识试试。",
            action_label="去喂知识",
            action_href="/",
        )
    body = f"""
{page_head("来源", "这些是卡比记住某些事情时保存下来的原始依据。")}
{listing}
"""
    return layout(title="来源", body=body, current="sources", flash=flash, heading=False)


def _safe_metadata(metadata: Mapping[str, Any]) -> tuple[list[tuple[str, str]], list[str]]:
    rows: list[tuple[str, str]] = []
    hidden: list[str] = []
    for key, value in sorted(metadata.items()):
        if _SENSITIVE_KEY.search(str(key)):
            hidden.append(str(key))
            continue
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            rendered = "" if value is None else str(value)
        rows.append((str(key), _text(rendered, 1200)))
    return rows, hidden


def source_detail_page(*, source: Any, memories: Sequence[Any], flash: Flash | None = None) -> str:
    """来源详情：第一眼是**原稿**，第二眼是「卡比从这里记住了」。

    原稿只展示 Source 里真实保存的 content，不重新生成、不伪造完整原文；
    关联的记忆来自 get_memories_for_source()（由调用方传入），点回去是 /memories/<id>。
    """
    icon, kind_label = source_type_label(source.source_type)
    _, hidden = _safe_metadata(source.metadata or {})

    text_content = (source.content or "").strip()
    if not text_content:
        manuscript = '<p class="source-note">暂时没有可以查看的原稿。</p>'
    else:
        blocks = [block.strip() for block in text_content.split("\n\n") if block.strip()] or [text_content]
        if len(text_content) > 4000:
            head, tail = blocks[0], blocks[1:]
            manuscript = (
                f'<div class="manuscript"><p>{escape(head)}</p>'
                f'<details class="more"><summary>展开剩余内容（全文共 {len(text_content)} 字）</summary>'
                + "".join(f"<p>{escape(block)}</p>" for block in tail)
                + "</details></div>"
            )
        else:
            manuscript = (
                '<div class="manuscript">'
                + "".join(f"<p>{escape(block)}</p>" for block in blocks)
                + "</div>"
            )

    extra = []
    if source.url:
        extra.append(
            f'<a class="source-link" href="{escape(source.url)}" target="_blank" rel="noreferrer noopener">'
            "🔗 打开原网页</a>"
        )
    if hidden:
        extra.append(f'<p class="source-note">有 {len(hidden)} 项内部信息未展示。</p>')
    extra_html = "".join(extra)

    if memories:
        memory_items = "".join(
            f'<li><a href="/memories/{escape(memory.id)}">'
            f'<span class="glyph">🧠</span><span>{escape(memory.title)}</span>'
            f'<span class="kind">{escape(memory_status_label(memory.status))}</span></a></li>'
            for memory in memories
        )
        memories_html = f'<ul class="related-list">{memory_items}</ul>'
    else:
        memories_html = '<p class="muted">卡比还没有从这里留下记忆。</p>'

    body = f"""
{back_link("/memories", "← 返回", link_id="back-link")}
<div class="source-head">
  <div class="kicker">{icon} {escape(kind_label)}</div>
  <h1>{escape(source.title or "无标题来源")}</h1>
</div>
{section("原稿", f'{manuscript}<p class="source-note">以上是这份来源里真实保存的内容。</p>{extra_html}')}
{section("卡比从这里记住了", memories_html)}
<script>
(function () {{
  // 优先返回上一层（从记忆详情点进来时回到那条记忆），直接打开时才回「我的记忆」
  var link = document.getElementById('back-link');
  if (!link) {{ return; }}
  link.addEventListener('click', function (event) {{
    var from = document.referrer || '';
    if (from.indexOf(location.origin) === 0 && history.length > 1) {{
      event.preventDefault();
      history.back();
    }}
  }});
}})();
</script>
"""
    return layout(title=source.title or "来源", body=body, current="memories", flash=flash, heading=False)


#: 记忆类型的中文名（内部枚举仍是 knowledge / experience / event / profile）
MEMORY_TYPE_LABELS: Mapping[str, str] = {
    "knowledge": "知识",
    "experience": "经历",
    "event": "事件",
    "profile": "画像",
}


def search_page(
    *,
    query: str,
    result: Any | None,
    type_filter: str,
    status_filter: str,
    limit: int,
    sources_by_memory: Mapping[str, Sequence[Any]] | None = None,
    error: str = "",
    flash: Flash | None = None,
) -> str:
    """搜索：在「我已经拥有的知识」里找东西。

    状态：idle（还没搜）/ searching（提交后的一瞬间）/ results / empty / error。
    idle 与 empty 都复用 Phase 1 的状态写法（轻量文案 + empty_state），不做 Search 专用组件。
    最近搜索保存在浏览器 localStorage（key = pkb.search.recent，最多 8 条，去重后置顶），
    不涉及数据库与后端。
    """
    conditions = f"""
    <details class="conditions">
      <summary>更多条件</summary>
      <div class="panel">
        <div>
          <label for="status">状态</label>
          <select id="status" name="status">
            {''.join(f'<option value="{escape(value)}"{" selected" if value == status_filter else ""}>{"全部" if value == "all" else escape(memory_status_label(value))}</option>' for value in ("active", "pending", "archived", "all"))}
          </select>
        </div>
        <div>
          <label for="type">类型</label>
          <select id="type" name="type">
            <option value="all"{" selected" if type_filter == "all" else ""}>全部</option>
            {''.join(f'<option value="{escape(value)}"{" selected" if value == type_filter else ""}>{escape(label)}</option>' for value, label in MEMORY_TYPE_LABELS.items())}
          </select>
        </div>
        <div>
          <label for="limit">最多显示</label>
          <input type="number" id="limit" name="limit" min="1" max="100" value="{escape(limit)}">
        </div>
      </div>
    </details>"""
    box = f"""
<form class="search-box" id="search-form" method="get" action="/search">
  <div class="field">
    <input type="search" id="q" name="q" value="{escape(query)}" placeholder="输入你想找的内容……"
           autocomplete="off"{' autofocus' if not query else ''}>
    <button type="submit" id="search-submit">🔍 搜索</button>
  </div>
  {conditions}
</form>"""

    # 最近搜索：内容由页面脚本从 localStorage 渲染；没有记录时整块隐藏
    recent = """
<section class="recent" id="recent-searches" hidden>
  <div class="recent-head">
    <h2>最近搜索</h2>
    <button type="button" class="link-button" id="recent-clear">清除</button>
  </div>
  <div class="recent-list" id="recent-list"></div>
</section>"""

    cards: list[str] = []
    if result is not None:
        for hit in result.hits:
            memory = hit.memory
            sources = tuple(getattr(hit, "sources", ()) or ()) or tuple(
                (sources_by_memory or {}).get(memory.id, ())
            )
            from_items = []
            for source in sources[:2]:
                source_id = getattr(source, "id", source)
                icon, _kind = source_type_label(getattr(source, "source_type", ""))
                # 出处只做轻量提示：来源标题（部分来源的标题就是正文首行）截断显示
                title = " ".join((getattr(source, "title", None) or "无标题来源").split())
                if len(title) > 24:
                    title = title[:24].rstrip() + "…"
                from_items.append(f'<a href="/sources/{escape(source_id)}">{icon} {escape(title)}</a>')
            more = f'<span class="from-line">等 {len(sources)} 份来源</span>' if len(sources) > 2 else ""
            from_line = (
                f'<span class="from-line">来自：{"、".join(from_items)}{more}</span>' if from_items else ""
            )
            url = f"/memories/{escape(memory.id)}"
            cards.append(
                entity_card(
                    title=memory.title,
                    title_href=url,
                    # 与「我的记忆」一致：整卡可点（标题链接铺满卡片），
                    # 「来自」里的来源链接仍然独立可点 —— 不用嵌套 <a>。
                    modifier="is-stretched",
                    glyph="🧠",
                    excerpt=_memory_excerpt(memory),
                    meta_html=f"{_memory_status_chip(memory.status)}{from_line}",
                )
            )

    if error:
        notice = empty_state("搜索暂时出了点问题", error)
    elif result is None:
        # idle：非常轻量，不占满屏幕
        notice = (
            '<div class="search-idle">'
            '<p class="search-idle-title">搜索你的知识</p>'
            '<p class="muted">输入关键词开始寻找</p>'
            "</div>"
        )
    elif result.total == 0:
        notice = empty_state(
            "没有找到相关内容", "换个关键词试试", action_label="去喂知识", action_href="/"
        )
    else:
        notice = f'<p class="result-count">找到 {escape(result.total)} 条记忆</p>'

    results_html = card_list("".join(cards)) if cards else ""

    body = f"""
{page_head("搜索", "你还记得什么？")}
{box}
{recent}
{notice}
{results_html}
{_search_script(query=query, status_filter=status_filter)}
"""
    return layout(title="搜索", body=body, current="search", flash=flash, heading=False)


def _search_script(*, query: str, status_filter: str) -> str:
    """最近搜索（localStorage）+ 提交态。纯前端，不新增后端接口。"""
    query_json = json.dumps(query, ensure_ascii=False)
    status_json = json.dumps(status_filter, ensure_ascii=False)
    return f"""<script>
(function () {{
  var KEY = "pkb.search.recent";
  var MAX = 8;
  var box = document.getElementById("recent-searches");
  var list = document.getElementById("recent-list");
  var clear = document.getElementById("recent-clear");
  var form = document.getElementById("search-form");
  var query = {query_json};
  var status = {status_json};

  function read() {{
    try {{
      var raw = window.localStorage.getItem(KEY);
      var items = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(items)) {{ return []; }}
      return items.filter(function (item) {{ return typeof item === "string" && item.trim() !== ""; }});
    }} catch (error) {{ return []; }}
  }}
  function write(items) {{
    try {{ window.localStorage.setItem(KEY, JSON.stringify(items.slice(0, MAX))); }} catch (error) {{}}
  }}
  function remember(term) {{
    // 去重后置顶：重复搜索同一个词不会产生第二条历史
    var items = read().filter(function (item) {{ return item !== term; }});
    items.unshift(term);
    write(items);
  }}
  function render() {{
    if (!box || !list) {{ return; }}
    var items = read();
    list.innerHTML = "";
    if (!items.length) {{ box.hidden = true; return; }}
    items.forEach(function (term) {{
      var link = document.createElement("a");
      link.className = "recent-item";
      link.href = "/search?q=" + encodeURIComponent(term) + "&status=" + encodeURIComponent(status);
      link.textContent = term;   // textContent：搜索词不会被当成 HTML
      list.appendChild(link);
    }});
    box.hidden = false;
  }}

  if (query) {{ remember(query); }}
  render();
  if (clear) {{
    clear.addEventListener("click", function () {{
      write([]);   // 只清历史，不动任何 Source / Memory / 搜索数据
      render();
    }});
  }}
  if (form) {{
    form.addEventListener("submit", function () {{
      var button = document.getElementById("search-submit");
      if (button) {{ button.textContent = "搜索中……"; button.disabled = true; }}
    }});
  }}
}})();
</script>"""


def error_page(*, status: int, title: str, message: str, kind: str = "", hint: str = "") -> str:
    # 可见内容只有中文；技术性的错误类型保留为 HTML 注释（不给用户看，排障时仍可查看源码/日志）
    body = f"""
<div class="card">
<p>{escape(message)}</p>
<p class="muted">错误编号：{escape(status)}</p>
{hint and f'<p class="muted">{escape(hint)}</p>'}
</div>
<p><a href="/">← 返回首页</a> · <a href="/memories">我的记忆</a> · <a href="/search">搜索</a></p>
<!-- kind: {escape(kind or "unknown")} -->
"""
    return layout(title=title, body=body, current="", flash=Flash(kind="err", title=title, detail=message, hint=hint))

#: Extra styles for the 喂知识 home page.  Deliberately quiet: no gradients, no glow,
#: no particles -- just a centred creature, a mouth, and a soft feedback line.
_FEED_STYLE = """
.feed { max-width: var(--layout-feed); margin: 6vh auto 0; text-align: center; }
.feed h1 { font-size: var(--text-display); margin: 0 0 var(--space-2xs); font-weight: var(--weight-medium); }
.feed .lead { color: var(--color-text-muted); font-size: var(--text-body); margin: 0 0 var(--space-xl); }
.kirby-wrap { position: relative; display: inline-block; margin: var(--space-sm) auto 0; }
.kirby { width: clamp(280px, 32vw, 360px); height: auto; display: block; margin: 0 auto;
         transition: transform .18s ease-out;
         /* real game sprites are pixel art: keep the pixels crisp when scaled up */
         image-rendering: pixelated; }
/* 真实帧：由 data-kirby-state 决定显示哪一张（内联 display:none 会盖过这里的规则，故不用） */
.kirby-frame { display: none; }
.kirby-wrap[data-kirby-state="idle"] .kirby-frame-idle,
.kirby-wrap[data-kirby-state="detected"] .kirby-frame-idle,
.kirby-wrap[data-kirby-state="processing"] .kirby-frame-idle,
.kirby-wrap[data-kirby-state="feedback"] .kirby-frame-idle,
.kirby-wrap[data-kirby-state="open"] .kirby-frame-open,
.kirby-wrap[data-kirby-state="closed"] .kirby-frame-closed,
.kirby-wrap[data-kirby-state="inhale"] .kirby-frame-inhale1,
.kirby-wrap[data-kirby-state="inhale"] .kirby-frame-inhale2,
.kirby-wrap[data-kirby-state="inhale"] .kirby-frame-inhale3 { display: block; }
/* 吸入：3 帧循环。inhale2 占据布局，另外两帧绝对定位叠加播放，不会把页面撑高 */
.kirby-frame-inhale1, .kirby-frame-inhale3 { position: absolute; left: 50%; top: 0;
                                            transform: translateX(-50%); }
.kirby-frame-inhale1 { animation: kirby-inhale-a .42s steps(1, end) infinite; }
.kirby-frame-inhale3 { animation: kirby-inhale-c .42s steps(1, end) infinite; }
@keyframes kirby-inhale-a { 0% { opacity: 1 } 33.3% { opacity: 0 } 100% { opacity: 0 } }
@keyframes kirby-inhale-c { 0% { opacity: 0 } 33.4% { opacity: 0 } 66.6% { opacity: 1 } 100% { opacity: 0 } }
/* 被喂进来的内容：从卡比身前飞向嘴巴（右侧）并缩小淡出，不是瞬移 */
.kirby-intake { position: absolute; left: 50%; top: 22%; transform: translate(-50%, -50%);
                font-size: var(--text-caption); color: #7d5060; background: var(--color-surface); border: 1px solid #e7c6d1;
                border-radius: var(--radius-md); padding: var(--space-3xs) var(--space-md); white-space: nowrap; opacity: 0;
                pointer-events: none; }
.kirby-intake.fly { animation: kirby-intake .46s ease-in forwards; }
@keyframes kirby-intake {
  0%   { opacity: 0; left: 50%; top: 22%; transform: translate(-50%, -50%) scale(1); }
  18%  { opacity: 1; left: 50%; top: 26%; transform: translate(-50%, -50%) scale(1); }
  100% { opacity: 0; left: 74%; top: 58%; transform: translate(-50%, -50%) scale(.22); }
}
.kirby-body { transition: transform .18s ease-out; }
.kirby-mouth-area { cursor: pointer; }
.kirby-open, .kirby-inhale { opacity: 0; transition: opacity .16s ease-out; }
.kirby-closed-mouth { opacity: 1; transition: opacity .16s ease-out; }
.kirby-air { opacity: 0; transition: opacity .2s ease-out; }
/* 待机 */
.kirby-wrap[data-kirby-state="idle"] .kirby { transform: translateY(0); }
/* 张嘴 */
.kirby-wrap[data-kirby-state="open"] .kirby-closed-mouth { opacity: 0; }
.kirby-wrap[data-kirby-state="open"] .kirby-open { opacity: 1; }
.kirby-wrap[data-kirby-state="open"] .kirby-body { transform: scale(1.02); }
/* 吸入 */
.kirby-wrap[data-kirby-state="inhale"] .kirby-closed-mouth { opacity: 0; }
.kirby-wrap[data-kirby-state="inhale"] .kirby-open { opacity: 1; }
.kirby-wrap[data-kirby-state="inhale"] .kirby-inhale { opacity: 1; }
.kirby-wrap[data-kirby-state="inhale"] .kirby-body { transform: scale(1.05); }
.kirby-wrap[data-kirby-state="inhale"] .kirby-air { opacity: 1; }
/* 合嘴 */
.kirby-wrap[data-kirby-state="closed"] .kirby-closed-mouth { opacity: 1; }
/* 消化：轻微下沉，不阻塞 */
.kirby-wrap[data-kirby-state="processing"] .kirby-body { transform: translateY(2px) scale(.99); }
.kirby-wrap[data-kirby-state="feedback"] .kirby-body { transform: translateY(-3px) scale(1.03); }
.kirby-drop { position: absolute; inset: 22% 6% 8% 28%; border-radius: var(--radius-circle); }
.kirby-wrap[data-kirby-state="detected"] .kirby-drop { outline: 2px dashed #e79bb4; outline-offset: 6px; }
.kirby-feedback { position: absolute; left: 50%; top: 58%; transform: translate(-50%, 0);
                  font-size: var(--text-subtitle); color: #b5486f; white-space: nowrap; opacity: 0; pointer-events: none; }
.kirby-feedback.show { animation: kirby-rise 1.8s ease-out forwards; }
@keyframes kirby-rise {
  0%   { opacity: 0; transform: translate(-50%, 6px); }
  18%  { opacity: 1; transform: translate(-50%, -2px); }
  70%  { opacity: 1; transform: translate(-50%, -14px); }
  100% { opacity: 0; transform: translate(-50%, -24px); }
}
.kirby-note { position: absolute; left: 50%; top: 70%; transform: translateX(-50%);
              font-size: var(--text-caption); color: var(--color-text-faint); white-space: nowrap; opacity: 0; transition: opacity .3s; }
.kirby-note.show { opacity: 1; }
.kirby-hint { color: var(--color-text-faint); font-size: var(--text-caption); margin: var(--space-xl) 0 0; }
.feed-box { max-width: var(--layout-form); margin: var(--space-xl) auto 0; text-align: left; }
.feed-box textarea { min-height: 108px; }
.feed-box .row-actions { margin-top: var(--space-sm); }
/* 统一输入区：一个输入框 + 添加文件 + 提交；尺寸/颜色全部来自 Phase 1 令牌 */
.capture-row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-md);
               margin-top: var(--space-md); }
.capture-file { display: inline-flex; align-items: center; gap: var(--space-xs); max-width: 100%;
                background: var(--color-surface-subtle); border: var(--border);
                border-radius: var(--radius-pill);
                padding: var(--space-3xs) var(--space-md) var(--space-3xs) var(--space-lg);
                color: var(--color-text-secondary); font-size: var(--text-caption); }
.capture-file-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 34ch; }
.capture-file-clear { border: 0; background: transparent; color: var(--color-text-faint);
                      cursor: pointer; font-size: var(--text-caption); padding: 0 var(--space-2xs); }
.capture-file-clear:hover { color: var(--color-accent-link); }
.hidden { display: none; }
"""

# --------------------------------------------------------------------------
# 卡比：state component for the 喂知识 home page (spec §三/§五/§六/§七/§八/§九)
# --------------------------------------------------------------------------

#: Real game frames (Kirby Super Star Ultra, DS) served from ``web/assets/``.
#: The *animation logic never changes*: it only sets ``data-kirby-state`` on the wrapper, so
#: replacing or extending the sprite set is a data change only.  Provenance, the exact
#: source files and the per-state mapping are documented in
#: ``docs/KIRBY-SPRITES.md``; if a file is missing the component falls back to the
#: placeholder drawing automatically.
KIRBY_FRAMES: Mapping[str, str | None] = {
    "idle": "/assets/kirby-idle.png",          # 待机：闭嘴站立
    "open": "/assets/kirby-open.png",          # 张嘴：嘴巴开始张开
    "inhale1": "/assets/kirby-inhale-1.png",   # 吸入 1：小口
    "inhale2": "/assets/kirby-inhale-2.png",   # 吸入 2：大口
    "inhale3": "/assets/kirby-inhale-3.png",   # 吸入 3：最大吸入口
    "closed": "/assets/kirby-idle.png",        # 合嘴：沿用待机帧
}

#: 吸入帧循环的播放顺序与时长（游戏里 Kirby 的吸入很快；0.42s/圈 ≈ KSS 的吸入节奏）
KIRBY_INHALE_ORDER: tuple[str, ...] = ("inhale1", "inhale2", "inhale3")
KIRBY_INHALE_SECONDS = 0.42

#: Directory that serves ``/assets/*`` (see ``server.KnowledgeBaseHandler._send_asset``).
KIRBY_ASSET_DIR = Path(__file__).resolve().parent / "assets"

#: Configurable display size (spec §三: desktop 280-360px, default ~320px).
KIRBY_SIZE_PX = 320

#: Internal state machine (spec §五).  The UI never shows these words.
KIRBY_STATES: tuple[str, ...] = ("idle", "detected", "open", "inhale", "closed", "processing", "feedback")

#: Friendly, category-level error copy for the feed page (spec §九).  Keyed by exception
#: class name so no internal exception text, URL policy detail, model name or database
#: detail ever reaches the user.
FEED_ERROR_COPY: Mapping[str, tuple[str, str]] = {
    "PdfEncryptedError": ("这份 PDF 打不开", "它可能有密码，或者不允许读取里面的文字。"),
    "PdfCorruptError": ("这份 PDF 好像坏掉了", "文件不完整，暂时读不出来。"),
    "PdfEmptyTextError": (
        "这份 PDF 里没有可提取的文字",
        "它是扫描件或图片型 PDF，需要图片识别才能读；1.0 还不支持图片识别。",
    ),
    "PdfTooLargeError": ("这一口太大了", "文件超出大小上限，卡比吃不下。"),
    "PdfTooManyPagesError": ("这一口太多了", "页数超出上限，卡比吃不下。"),
    "PdfTextTooLargeError": ("里面的字太多了", "文本超出上限，卡比吃不下。"),
    "PdfBackendUnavailableError": ("卡比还不会看 PDF", "当前环境没有准备好 PDF 的解析能力。"),
    "BlockedUrlError": ("这个网址卡比不能去", "只能喂公开的网页地址。"),
    "InvalidUrlError": ("这个网址看不懂", "请确认是完整的网址。"),
    "RedirectError": ("这个网址绕得太远了", "跳转太多，读不出来。"),
    "ResponseTooLargeError": ("这个网页太大了", "超出大小上限，卡比吃不下。"),
    "UnsupportedContentTypeError": ("这一页不是文字内容", "只认得 HTML 与纯文本网页；二进制文件请直接喂给卡比。"),
    "EmptyContentError": (
        "这个页面读不出正文",
        "它可能是靠脚本动态渲染的页面；可以换成静态页面，或者直接把正文复制过来喂。",
    ),
    "WebEncodingError": ("这一页的编码看不懂", "暂时读不出来。"),
    "WebImportError": ("这个网址暂时读不出来", "可以稍后再试，或者换一个页面。"),
    "ChatParseError": ("这份聊天记录看不懂", "请确认它是角色文本或标准 JSON 格式。"),
    "UnsupportedChatRoleError": ("这份聊天记录看不懂", "里面有认不出的说话人。"),
    "ChatImportError": ("这份聊天记录读不出来", "请确认文件格式。"),
    "FileEncodingError": ("这个文件的编码看不懂", "请确认它是 UTF-8 文本。"),
    "UnsupportedFileTypeError": ("这种文件卡比还不认识", "现在可以喂 .txt / .md / 聊天记录 / 网页 / PDF。"),
    "FileTooLargeError": ("这一口太大了", "文件超出大小上限，卡比吃不下。"),
    "FileMissingError": ("没有找到这个文件", "请重新选择一次。"),
    "NotAFileError": ("这个不是文件", "请选择单个文件再喂。"),
    "FileImportError": ("这个文件读不出来", "请确认文件内容。"),
    "LLMConfigError": ("卡比还没准备好", "模型服务还没有配置好，暂时没办法消化。"),
    "LLMRequestError": ("现在消化不了", "模型服务暂时没有回应，稍后再试。"),
    "LLMResponseError": ("这次没吃明白", "可以再喂一次。"),
    "LLMError": ("现在消化不了", "稍后再试一次。"),
    "ExtractionValidationError": ("这次没吃明白", "内容没能形成可用的记忆，可以再喂一次。"),
    "ValidationError": ("这份内容不太对", "请检查后重新喂一次。"),
    "ConflictError": ("这份内容已经吃过了", "没有重复记录。"),
    "DuplicateContentHashError": ("这份内容已经吃过了", "没有重复记录。"),
    "RetrievalError": ("现在翻不动记忆", "稍后再试。"),
    "MemorySystemError": ("这次没吃下去", "可以再试一次。"),
}

FEED_FALLBACK_ERROR = ("这次没吃下去", "可以再试一次。")


def feed_error_message(exc: BaseException) -> tuple[str, str]:
    """Map an exception to ``(title, hint)`` in user language (spec §九).

    Uses the exception *class name* only: no message text, no stack, no API/database/model
    detail, no URL policy internals.
    """
    name = type(exc).__name__
    if name in FEED_ERROR_COPY:
        return FEED_ERROR_COPY[name]
    for base in type(exc).__mro__[1:]:
        if base.__name__ in FEED_ERROR_COPY:
            return FEED_ERROR_COPY[base.__name__]
    return FEED_FALLBACK_ERROR


def _kirby_placeholder_svg(size: int) -> str:
    """Placeholder drawing used only while ``KIRBY_FRAMES`` has no real sprite.

    Pure inline SVG (no external file, no network): a round pink body with feet, arms,
    eyes and three mouth variants (closed / open / inhale) that CSS cross-fades by state.
    """
    return f"""
<svg class="kirby" viewBox="0 0 320 300" width="{size}" height="{int(size * 300 / 320)}" role="img"
     aria-label="卡比">
  <g class="kirby-body">
    <ellipse cx="160" cy="272" rx="58" ry="20" fill="#e9a3b8" opacity=".85"/>
    <path d="M78 150c0-52 37-86 82-86s82 34 82 86c0 54-37 88-82 88s-82-34-82-88z" fill="#f7b8cb"/>
    <ellipse cx="96" cy="128" rx="26" ry="34" fill="#f7b8cb" transform="rotate(-18 96 128)"/>
    <ellipse cx="224" cy="128" rx="26" ry="34" fill="#f7b8cb" transform="rotate(18 224 128)"/>
    <ellipse cx="62" cy="196" rx="20" ry="16" fill="#f7b8cb"/>
    <ellipse cx="258" cy="196" rx="20" ry="16" fill="#f7b8cb"/>
    <ellipse cx="108" cy="120" rx="7" ry="16" fill="#2b2b33"/>
    <ellipse cx="212" cy="120" rx="7" ry="16" fill="#2b2b33"/>
    <ellipse cx="110" cy="114" rx="3" ry="6" fill="#fff"/>
    <ellipse cx="214" cy="114" rx="3" ry="6" fill="#fff"/>
    <ellipse cx="88" cy="176" rx="14" ry="9" fill="#f293ad" opacity=".7"/>
    <ellipse cx="232" cy="176" rx="14" ry="9" fill="#f293ad" opacity=".7"/>
    <g class="kirby-closed-mouth">
      <path d="M138 172q22 12 44 0" stroke="#8d3350" stroke-width="5" fill="none" stroke-linecap="round"/>
    </g>
    <g class="kirby-open">
      <ellipse cx="160" cy="182" rx="30" ry="24" fill="#7a2440"/>
      <ellipse cx="160" cy="196" rx="16" ry="10" fill="#c56283" opacity=".9"/>
    </g>
    <g class="kirby-inhale">
      <ellipse cx="160" cy="186" rx="38" ry="30" fill="#6d1f39"/>
    </g>
    <g class="kirby-air">
      <path d="M40 186h44" stroke="#d9b7c4" stroke-width="4" stroke-linecap="round"/>
      <path d="M28 206h38" stroke="#d9b7c4" stroke-width="4" stroke-linecap="round"/>
      <path d="M236 186h44" stroke="#d9b7c4" stroke-width="4" stroke-linecap="round"/>
      <path d="M254 206h38" stroke="#d9b7c4" stroke-width="4" stroke-linecap="round"/>
    </g>
  </g>
</svg>"""


def kirby_assets_available() -> bool:
    """True when every configured frame file is actually on disk."""
    for url in KIRBY_FRAMES.values():
        if not url or not url.startswith("/assets/"):
            return False
        if not (KIRBY_ASSET_DIR / url.rsplit("/", 1)[-1]).is_file():
            return False
    return True


def kirby_component(*, size: int = KIRBY_SIZE_PX) -> str:
    """Render the Kirby state component (real sprite frames, placeholder as fallback).

    Which frame is visible is decided purely by ``data-kirby-state`` in CSS, so the state
    machine and the feed logic below are independent of the artwork.
    """
    if kirby_assets_available():
        # visibility is CSS-driven (an inline display:none would win over the state rules)
        drawing = "".join(
            f'<img class="kirby kirby-frame kirby-frame-{name}" src="{escape(str(KIRBY_FRAMES[name]))}" '
            f'alt="卡比" data-frame="{name}" draggable="false">'
            for name in ("idle", "open", "inhale1", "inhale2", "inhale3", "closed")
        )
    else:
        drawing = _kirby_placeholder_svg(size)
    return f"""
<div class="kirby-wrap" id="kirby-zone" data-kirby-state="idle">
  {drawing}
  <div class="kirby-drop" id="kirby-mouth" title="点我喂文字；也可以把文件拖进来"></div>
  <div class="kirby-feedback" id="kirby-feedback"></div>
  <div class="kirby-note" id="kirby-note"></div>
</div>"""


def _kirby_script() -> str:
    """The Kirby state machine + feed wiring (spec §五/§六/§八/§九).

    ``nextKirbyState`` is a pure function so the transitions (including the error path back
    to idle) can be executed and checked outside a browser.
    """
    return r"""
<script>
// ---- 卡比状态机（内部状态，界面不显示这些英文） ----
var KIRBY_TRANSITIONS = {
  idle:       {detect: "detected", reset: "idle"},
  detected:   {open: "open", reset: "idle"},
  open:       {absorb: "inhale", reset: "idle"},
  inhale:     {close: "closed", reset: "idle"},
  closed:     {process: "processing", reset: "idle"},
  processing: {feedback: "feedback", fail: "idle", reset: "idle"},
  feedback:   {done: "idle", reset: "idle"}
};
function nextKirbyState(state, event) {
  var row = KIRBY_TRANSITIONS[state] || {};
  return row[event] || KIRBY_TRANSITIONS.idle[event] || "idle";
}
function kirbySetState(state) {
  var zone = document.getElementById("kirby-zone");
  if (zone) { zone.setAttribute("data-kirby-state", state); }
  return state;
}
// ---- 喂知识的交互 ----
(function () {
  var zone = document.getElementById("kirby-zone");
  var mouth = document.getElementById("kirby-mouth");
  var feedback = document.getElementById("kirby-feedback");
  var note = document.getElementById("kirby-note");
  var textBox = document.getElementById("feed-text");
  var fileInput = document.getElementById("feed-chat-file");
  var fileName = document.getElementById("feed-file-name");
  var fileLabel = document.getElementById("feed-file-label");
  var addFileButton = document.getElementById("feed-add-file");
  var clearFileButton = document.getElementById("feed-file-clear");
  var busy = false;
  var IDLE_WORDS = "把知识喂给我";

  function say(text, cls) {
    if (!feedback) { return; }
    feedback.textContent = text;
    feedback.className = "kirby-feedback" + (cls ? " " + cls : "");
    // restart the animation
    void feedback.offsetWidth;
    feedback.classList.add("show");
    window.setTimeout(function () { feedback.classList.remove("show"); }, 1900);
  }
  function setNote(text) {
    if (!note) { return; }
    note.textContent = text || "";
    note.className = "kirby-note" + (text ? " show" : "");
    if (text) { window.setTimeout(function () { note.className = "kirby-note"; }, 3200); }
  }
  function showError(title, hint) {
    say(title, "");              // 角色反馈：这次没吃下去
    setNote(hint || "");         // 用户层原因（来自后端的友好文案）
    kirbySetState(nextKirbyState(kirbySetState("processing"), "fail"));  // 回到待机，绝不卡住
  }
  // 真实业务结果的绑定：只有 fetch 返回后（且服务端已完成 Capture/Formation）才显示“吃饱了~”
  function selectedFile() {
    return fileInput && fileInput.files && fileInput.files[0] ? fileInput.files[0] : null;
  }
  function renderSelectedFile() {
    if (!fileName || !fileLabel) { return; }
    var file = selectedFile();
    if (!file) {
      fileName.classList.add("hidden");
      fileLabel.textContent = "";
      return;
    }
    fileName.classList.remove("hidden");
    fileLabel.textContent = "已选择：" + file.name;
  }
  function clearSelectedFile() {
    if (fileInput) { fileInput.value = ""; }
    renderSelectedFile();
  }
  function showIntake(label) {
    // 让"内容"从卡比身前飞进嘴里，而不是瞬移；纯展示层，业务请求不受影响。
    if (!zone) { return; }
    var token = document.createElement("span");
    token.className = "kirby-intake";
    token.textContent = label || "知识";
    zone.appendChild(token);
    void token.offsetWidth;
    token.classList.add("fly");
    window.setTimeout(function () {
      if (token.parentNode) { token.parentNode.removeChild(token); }
    }, 620);
  }
  function feed(url, payload, label) {
    if (busy) { return; }
    busy = true;
    kirbySetState("detected");
    kirbySetState(nextKirbyState("detected", "open"));
    showIntake(label);
    kirbySetState(nextKirbyState("open", "absorb"));
    var finished = false;
    setNote("正在处理……");
    var body = new URLSearchParams(payload).toString();
    fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "X-Requested-With": "fetch", "Accept": "application/json"},
      body: body
    }).then(function (response) {
      return response.json().catch(function () { return {ok: false, message: "这次没吃下去", hint: "可以再试一次。"}; })
        .then(function (data) { return {status: response.status, data: data}; });
    }).then(function (result) {
      finished = true;
      var data = result.data || {};
      kirbySetState(nextKirbyState("inhale", "close"));
      kirbySetState(nextKirbyState("closed", "process"));
      if (data.ok) {
        kirbySetState(nextKirbyState("processing", "feedback"));
        say(data.feedback || "吃饱了~", "");   // 只有真实处理完成后才会走到这里
        setNote(data.note || "");
        window.setTimeout(function () { kirbySetState("idle"); busy = false; }, 1800);
      } else {
        showError(data.message || "这次没吃下去", data.hint || "");
        busy = false;
      }
    }).catch(function () {
      if (!finished) { showError("这次没吃下去", "网络或服务暂时没有回应，可以再试一次。"); }
      busy = false;
    }).then(function () {
      // 无论成功/失败，一定回到可继续喂的状态（不卡在张嘴/吸入）
      if (!busy) { kirbySetState("idle"); }
    });
  }
  // A. 点嘴输入文字
  if (mouth && textBox) {
    mouth.addEventListener("click", function () {
      var wrap = document.getElementById("feed-text-box");
      if (wrap) { wrap.classList.toggle("hidden"); }
      if (textBox) { textBox.focus(); }
      kirbySetState("open");
    });
  }
  // 统一入口：文件优先，其次网址，最后当普通文字（用户不需要先判断类型）
  var textForm = document.getElementById("feed-text-form");
  if (textForm) {
    textForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var file = selectedFile();
      if (file) {
        var box = document.getElementById("feed-text-box");
        if (box) { box.classList.add("hidden"); }
        clearSelectedFile();
        sendFile(file);
        return;
      }
      var value = (textBox && textBox.value || "").trim();
      if (!value) { say("先写点东西，或者添加一个文件", ""); return; }
      textBox.value = "";
      document.getElementById("feed-text-box").classList.add("hidden");
      if (/^https?:\/\/\S+$/.test(value)) { feed("/import-url", {url: value, title: ""}, "网址"); }
      else { feed("/capture", {content: value, title: ""}, "文字"); }
    });
  }
  if (addFileButton && fileInput) {
    addFileButton.addEventListener("click", function () { fileInput.click(); });
  }
  if (fileInput) {
    fileInput.addEventListener("change", renderSelectedFile);
  }
  if (clearFileButton) {
    clearFileButton.addEventListener("click", function (event) {
      event.preventDefault();
      clearSelectedFile();
    });
  }
  // 方式 B：直接粘贴（Ctrl+V）。纯网址走 URL 导入，其余走文字 Capture。
  document.addEventListener("paste", function (event) {
    if (busy) { return; }
    var target = event.target || {};
    if (target.tagName === "TEXTAREA" || target.tagName === "INPUT") { return; }
    var text = (event.clipboardData || window.clipboardData || {}).getData
      ? (event.clipboardData || window.clipboardData).getData("text") : "";
    if (!text || !text.trim()) { return; }
    event.preventDefault();
    var trimmed = text.trim();
    if (/^https?:\/\/\S+$/.test(trimmed)) { feed("/import-url", {url: trimmed, title: ""}, "网址"); }
    else { feed("/capture", {content: trimmed, title: ""}, "文字"); }
  });
  // 唯一的文件分流：PDF → PDF 解析；角色文本/聊天 JSON → 聊天解析；其余 → 文件导入
  function sendFile(file) {
    var name = (file && file.name) || "";
    var lower = name.toLowerCase();
    if (lower.endsWith(".pdf")) {
      readFile(file, function (fileName2, base64) {
        feed("/import-pdf", {filename: fileName2, content_base64: base64, title: ""}, fileName2);
      });
      return;
    }
    readFileText(file, function (content) {
      var looksLikeChat = /^\s*(\[(user|assistant|system|tool|developer)\]|#{1,6}\s*(user|assistant)\b)/im.test(content)
        || /^\s*\{[\s\S]*"messages"\s*:/.test(content)
        || lower.endsWith(".chat") || lower.endsWith(".json");
      readFile(file, function (fileName2, base64) {
        if (looksLikeChat) {
          feed("/import-chat", {filename: fileName2, content_base64: base64, format: "auto", provider: "", title: ""}, fileName2);
        } else {
          feed("/import-file", {filename: fileName2, content_base64: base64, title: "", source_type: "file"}, fileName2);
        }
      });
    });
  }
  function readFile(file, done) {
    var reader = new FileReader();
    reader.onload = function () {
      var text = String(reader.result);
      var comma = text.indexOf(",");
      done(file.name, comma >= 0 ? text.slice(comma + 1) : "");
    };
    reader.readAsDataURL(file);
  }
  function readFileText(file, done) {
    var reader = new FileReader();
    reader.onload = function () { done(String(reader.result || "")); };
    reader.readAsText(file);
  }
  // 拖拽到卡比嘴部：命中 → 张嘴 → 吸入 → 合嘴 → 真实处理
  if (zone) {
    ["dragenter", "dragover"].forEach(function (type) {
      zone.addEventListener(type, function (event) {
        event.preventDefault();
        if (busy) { return; }
        if (zone.getAttribute("data-kirby-state") === "idle") { kirbySetState("detected"); }
      });
    });
    zone.addEventListener("dragleave", function () {
      if (!busy) { kirbySetState("idle"); }
    });
    zone.addEventListener("drop", function (event) {
      event.preventDefault();
      if (busy) { return; }
      var files = event.dataTransfer && event.dataTransfer.files;
      var text = event.dataTransfer ? (event.dataTransfer.getData("text/uri-list") || event.dataTransfer.getData("text/plain")) : "";
      if (files && files.length) {
        kirbySetState("detected");
        kirbySetState(nextKirbyState("detected", "open"));
        sendFile(files[0]);
        return;
      }
      var candidate = (text || "").trim();
      if (/^https?:\/\/\S+$/.test(candidate)) { feed("/import-url", {url: candidate, title: ""}, "网址"); return; }
      if (candidate) { feed("/capture", {content: candidate, title: ""}, "文字"); return; }
      showError("这次没吃下去", "可以喂文字、文件、网页地址或聊天记录。");
    });
  }
})();
</script>"""


def feed_page(*, flash: Flash | None = None, kirby_size: int = KIRBY_SIZE_PX) -> str:
    """Capture /「喂知识」：卡比是唯一的入口，其余都交给统一输入区。

    设计约束（Phase 2）：
    * 卡比是视觉与交互中心（点嘴 → 出现输入区）；
    * 没有按类型拆分的独立入口——文字、网址、文件都从同一个输入区进来，
      系统按内容与扩展名自己分流到既有 ingestion 端点；
    * 不解释实现（模型、数据库、记忆形成过程都不出现在页面上）；
    * 页面内不再重复放"我的记忆 / 搜索"快捷入口（那是全局导航的职责）。
    """
    body = f"""
<div class="feed">
  <h1>卡比</h1>
  <p class="lead">把知识喂给我</p>
  {kirby_component(size=kirby_size)}
  <div class="kirby-hint">点卡比的嘴开始输入：文字和网址直接粘贴，文件拖进来或点「添加文件」。</div>
  <div class="feed-box hidden" id="feed-text-box">
    <form id="feed-text-form">
      <textarea id="feed-text" placeholder="写点什么，或者粘贴一段文字或网址…"></textarea>
      <div class="row-actions capture-row">
        <button type="button" class="btn" id="feed-add-file">添加文件</button>
        <input type="file" id="feed-chat-file" class="hidden"
               accept=".txt,.md,.markdown,.chat,.json,.pdf">
        <span class="capture-file hidden" id="feed-file-name">
          <span class="capture-file-label" id="feed-file-label"></span>
          <button type="button" class="capture-file-clear" id="feed-file-clear"
                  aria-label="移除已选文件">✕</button>
        </span>
        <button type="submit" class="btn primary" id="feed-submit">喂给卡比</button>
      </div>
    </form>
  </div>
</div>
{_kirby_script()}
"""
    return layout(title="喂知识", body=body, current="feed", flash=flash, heading=False)


