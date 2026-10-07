# Design System（KB 1.0 Phase 1）

本文件是 Personal Knowledge Base 1.0 前端的**唯一设计基准**。Phase 2/3 改页面时请直接读这份文档，
不要重新猜风格。

- 代码位置：`personal_memory/web/components.py`（令牌 + 共享组件）、`personal_memory/web/views.py`（页面与页面样式）
- 技术形态：stdlib `http.server` 服务端渲染，**没有**前端框架、构建步骤、CSS 文件、CSS Modules、Tailwind。
  样式以 `<style>` 内联在页面上，交互是少量内联脚本。
- 落地时间：Phase 1（前端审计 + Design System 落地）。业务逻辑、数据模型、API 一律未改。

---

## 1. Design Principles

| # | 原则 | 在代码中的落法 |
| --- | --- | --- |
| 1 | **减少冗余信息** | 一个事实只出现一次：状态只在胶囊里、类型只在图标+胶囊里、出处只在卡片 meta 行里；页头不再重复页面标题以外的信息 |
| 2 | **页面职责清晰** | 主导航只有 喂知识 / 我的记忆 / 搜索；来源只能从记忆的「来自」进入，不做独立入口 |
| 3 | **核心对象优先** | 四个核心对象 = Capture Input / Memory / Source / Search Result；卡片组件 `entity_card` 同时承载记忆、来源、搜索结果 |
| 4 | **状态统一** | Loading / Empty / Error / Success 只有一套：`empty_state`（空）、`_banner`（成功/失败/提示）、`.muted`（弱化文本）；页面不得自造 |
| 5 | **颜色/尺寸只能来自令牌** | 页面样式里 `#hex` 为 0（卡比插画除外），由测试锁死（`tests/test_design_system.py`） |
| 6 | **可扩展不预留** | 卡片预留 `meta_html` / `extra_html` 槽位，但不提前实现图片、合并、演化等功能 |

## 2. Color Tokens

`:root` 里共 **27 个语义色**（不是"越多越专业"，全部来自审计出的真实用色合并）：

| Token | 值 | 用途 |
| --- | --- | --- |
| `--color-bg` | `#f6f6f4` | 页面底色 |
| `--color-surface` | `#fff` | 卡片、输入框、面板 |
| `--color-surface-subtle` | `#fbfbf9` | 极浅面板（表格头、代码块） |
| `--color-surface-muted` | `#e9e9e5` | 胶囊/徽标底色 |
| `--color-border` | `#e6e6e1` | 卡片与面板描边（最常用） |
| `--color-border-subtle` | `#ececea` | 分隔线、表格线 |
| `--color-border-strong` | `#dcdcd8` | 输入框/按钮/虚线空状态 |
| `--color-text` | `#1b1b1b` | 正文与标题 |
| `--color-text-secondary` | `#4a4a46` | 摘要、按钮文字、次要标签 |
| `--color-text-muted` | `#6a6a6a` | 说明文字、结果计数 |
| `--color-text-faint` | `#8a8a84` | 类型行、脚注、返回链接 |
| `--color-accent` | `#f7b8cb` | 主行动按钮/选中胶囊底色 |
| `--color-accent-strong` | `#e79bb4` | 悬停与聚焦描边 |
| `--color-accent-ink` | `#4a2130` | 粉色底上的文字 |
| `--color-accent-link` | `#7a4a5c` | 正文里的链接色 |
| `--color-success` / `--color-success-bg` | `#2f6b34` / `#dff0dd` | 已记住 |
| `--color-warning` / `--color-warning-bg` | `#8a6a1c` / `#fdf0cf` | 待确认 |
| `--color-error` / `--color-error-bg` | `#c62828` / `#fdeaea` | 失败 |
| `--color-info` / `--color-info-bg` | `#1565c0` / `#eaf2fd` | 提示 |
| `--color-header-bg` / `--color-header-text` / `--color-header-link` | `#1f2933` / `#fff` / `#cfe3ff` | 顶部导航 |
| `--color-focus` | `#e79bb4` | `:focus-visible` 焦点环 |

**例外（明确记录）**：卡比插画自带 `ILLUSTRATION_HEXES = {#e79bb4, #b5486f, #7d5060, #e7c6d1}`，
属于**插图色板**，只出现在 `_FEED_STYLE` 的 `.kirby-*` 规则里，不纳入 UI 令牌。
（`#e79bb4` 同时是 `--color-accent-strong` 的取值，但语义不同。）

## 3. Typography

字体：`--font-sans: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif`（唯一字体栈）。

| Token | size | 典型用法 |
| --- | --- | --- |
| `--text-display` | 26px | 喂知识首页主标题（weight 600） |
| `--text-heading` | 24px | 页面主标题（`.page-head h1`）、记忆详情标题 |
| `--text-title` | 21px | 通用 `h1`、来源标题 |
| `--text-subtitle` | 16px | 区块标题 `h2`、卡片标题、空状态标题 |
| `--text-body` | 15px | 正文、列表行、搜索按钮 |
| `--text-body-small` | 14px | 摘要、按钮、返回链接、说明 |
| `--text-caption` | 13px | `.muted`、类型行、计数、脚注 |
| `--text-label` | 12px | 徽标（`.badge`，遗留） |

字重：`--weight-regular: 400` / `--weight-medium: 600`（标题、卡片标题、主按钮）/ `--weight-bold: 700`（品牌、选中导航）。
行高：`--leading-tight: 1.5`、`--leading-normal: 1.55`（正文默认）、`--leading-relaxed: 1.7`（卡片摘要）、`--leading-loose: 2.0`（原稿正文）。

## 4. Spacing

4px 节奏，取值覆盖项目真实密度（不照搬外部设计系统）：

| Token | 值 | 典型用法 |
| --- | --- | --- |
| `--space-3xs` | 2px | 胶囊纵向微内边距、吸入标签 |
| `--space-2xs` | 4px | 紧凑偏移 |
| `--space-xs` | 6px | 输入框内边距 |
| `--space-sm` | 8px | 图标间距、卡片列表 gap |
| `--space-md` | 10px | 按钮内边距、行内间距 |
| `--space-lg` | 12px | 列表中 gap、按钮横向内边距 |
| `--space-xl` | 16px | **页面留白**、卡片间距、区块内边距 |
| `--space-2xl` | 20px | 面板内边距 |
| `--space-3xl` | 24px | 卡片内边距、区块间距、页脚 |
| `--space-4xl` | 32px | 大面板内边距（`.source-head`） |
| `--space-5xl` | 48px | 空状态纵向留白 |

原值映射（Phase 1 合并，位移 ≤4px）：`14/16/18 → xl`、`20/22/24/26 → 2xl/3xl`、`28/30/32 → 4xl`、`46 → 5xl`、`7/9/11/13 → 最近的令牌`。
唯一不进 scale 的是 `1px`（徽标上下发丝级内边距），测试里显式豁免。

## 5. Radius

| Token | 值 | 用途 |
| --- | --- | --- |
| `--radius-sm` | 8px | 按钮、遗留 `.card` |
| `--radius-md` | 10px | 徽标、列表行 |
| `--radius-lg` | 12px | 卡片、面板、空状态 |
| `--radius-pill` | 999px | 胶囊、搜索框 |
| `--radius-circle` | 50% | 圆形热区 |

原值 6/8/10/12/999/50% → 现在 5 个令牌（`6px` 的 `.card` 并入 8px）。

## 6. Border / Shadow

| Token | 值 | 说明 |
| --- | --- | --- |
| `--border-width` | 1px | 统一描边宽度 |
| `--border` | `1px solid var(--color-border)` | 卡片/面板 |
| `--border-subtle` | `1px solid var(--color-border-subtle)` | 分隔线 |
| `--border-strong` | `1px solid var(--color-border-strong)` | 输入框/按钮 |
| `--border-dashed` | `1px dashed var(--color-border-strong)` | 空状态 |
| `--shadow-none` | `none` | 默认：全站扁平，**当前没有任何阴影** |
| `--shadow-sm` | `0 1px 2px rgba(0,0,0,.05)` | 预留：卡片悬浮（Phase 2 若需要） |
| `--shadow-md` | `0 4px 12px rgba(0,0,0,.06)` | 预留：弹层/抽屉（Phase 2/3） |

## 7. Layout

| Token | 值 | 说明 |
| --- | --- | --- |
| `--layout-max` | 1120px | `main` / `footer` 最大宽度 |
| `--layout-gutter` | 18px | 页面左右留白（改用 `--space-xl` 后实际 16px，本令牌用于文档化） |
| `--layout-feed` | 720px | 喂知识首页内容宽度 |
| `--layout-form` | 520px | 首页输入区宽度 |
| `--layout-control` | 560px | 输入控件最大宽度 |
| `--layout-card-gap` | 16px | 卡片间距基准 |
| `--layout-section-gap` | 22px | 区块间距基准 |

骨架：`header`（品牌 + 3 个导航） → `main`（页头 + flash 横幅 + 页面内容） → `footer`。

## 8. Components

`personal_memory/web/components.py`（服务端渲染的 HTML 片段，全部转义文本参数）：

| 组件 | 作用 | 使用位置 | 为什么抽象 |
| --- | --- | --- | --- |
| `page_head(title, lead)` | 页面标题 + 一行说明 | 我的记忆 / 来源 / 搜索（3 页） | 三处逐字重复，改一次全站一致 |
| `section(title, body)` | 详情页区块（`h2` + 内容） | 记忆详情（3 个区块）/ 来源详情（2 个） | 5 处重复 |
| `empty_state(title, hint, action)` | 统一空状态 | 我的记忆空 / 来源空 / 搜索无结果 / 搜索出错（4 处） | 状态必须统一，禁止每页自造 |
| `entity_card(...)` | 实体卡片（记忆/来源/搜索结果） | 我的记忆 / 来源 / 搜索（3 页） | 3 处重复的卡片结构，且需要 meta/extra 槽位 |
| `card_list(cards)` | 卡片纵向列表容器 | 同上（3 页） | 容器样式一处定义 |
| `back_link(href, label, id)` | 返回上层链接 | 记忆详情 / 来源详情（2 页） | 两处重复，且返回脚本依赖 `id` |
| `action_link(label, href, primary)` | 链接式动作按钮 | 三个空状态的主行动 | 按钮 class 组合不再手写 |
| `status_chip(status)` | 记忆状态胶囊（中文） | 我的记忆 / 详情 / 搜索 / 来源卡（≥4 页） | 状态文案+颜色唯一来源 |
| `type_chip(source_type)` | 来源类型胶囊（📝 文字…） | 来源列表 | 替换手写 `status-archived` 标记 |

卡片结构（未来扩展点）：

```html
<a class="memory-card" href="…">          <!-- href 有值=整卡可点；无值=div -->
  <div class="memory-title"><span class="glyph">🧠</span>标题</div>
  <p class="memory-excerpt">一行摘要</p>
  <div class="memory-meta">状态胶囊 + 「来自：…」</div>
  <!-- extra_html：预留给标签 / 图片缩略 / 来源预览 / 合并·演化操作 -->
</a>
```

## 9. Interaction States

| 状态 | 规则 | 现状 |
| --- | --- | --- |
| hover | 卡片/列表行 `border-color: var(--color-accent-strong)`；卡片再上浮 1px | 已有，已令牌化 |
| focus（键盘） | `:focus-visible { outline: 2px solid var(--color-focus); outline-offset: 2px }` | **Phase 1 新增**（之前完全没有焦点样式） |
| 输入聚焦 | `.search-box .field:focus-within { border-color: var(--color-accent-strong) }` | 已有 |
| disabled | `opacity: .55; cursor: not-allowed` | **Phase 1 新增**（之前未定义） |
| active（选中） | `.chip.active` 粉色底 + 深色字；导航 `header a.active` 下划线 | 已有 |
| 链接 | 正文链接统一 `--color-accent-link`，hover 下划线（`.from-line a`） | 已统一 |

## 10. Responsive Rules

现状（真实情况，不夸大）：

- `<meta name="viewport" content="width=device-width, initial-scale=1">` ✔
- 布局是**流式**的：`main` 最大 1120px 居中、`.row` `flex-wrap`、`.chips` 换行、卡片列表纵向自适应。
- 卡比尺寸用 `clamp(280px, 32vw, 360px)` ✔ 唯一真正响应式的尺寸。
- **全站没有任何 `@media` 查询**（审计确认 0 个）。

结论与建议：手机（375px）下 `main` 的 16px 留白 + 流式卡片可用，但**没有针对窄屏的排版调整**
（例如卡片内边距、区块间距、搜索框按钮文案）。这属于已知缺口，**建议在 Phase 2 引入唯一的断点**
（建议 `@media (max-width: 640px)`）并只调整这三类值，不要在 page 级各写一套。

## 11. Usage Examples

新增一个页面：

```python
from .components import TOKENS_CSS, page_head, empty_state, entity_card, card_list, section  # 令牌层由 layout() 注入

def my_page(*, items, flash=None) -> str:
    if items:
        listing = card_list("".join(
            entity_card(title=item.title, href=f"/items/{escape(item.id)}", glyph="📄",
                        excerpt=item.excerpt, meta_html=status_chip(item.status))
            for item in items
        ))
    else:
        listing = empty_state("还没有内容", "先去喂一点知识", action_label="去喂知识", action_href="/")
    body = f"{page_head('我的页面', '一行说明')}{listing}"
    return layout(title="我的页面", body=body, current="", flash=flash, heading=False)
```

写页面样式（只允许令牌）：

```python
_MY_STYLE = """
.my-card { background: var(--color-surface); border: var(--border); border-radius: var(--radius-lg);
           padding: var(--space-3xl); }
.my-card h2 { font-size: var(--text-subtitle); color: var(--color-text); margin: 0 0 var(--space-lg); }
"""
```

## 12. 审计发现（Phase 1 实测）

**重复组件/结构（已修）**：卡片 3 页 ×3 段、空状态 4 处、页头 3 处、区块 5 处、返回链接 2 处、卡片列表 3 处、
状态胶囊与类型胶囊各 1 处手写。

**重复/散乱样式（已修）**：56 种硬编码颜色（106 处）、12 种字号、8 种描边色、24 种 padding、17 种 margin、
6 种圆角 → 27 色 + 8 级字号 + 11 级间距 + 5 圆角 + 23 个边框/阴影/布局令牌；`var(--)` 引用 **287 处**，页面样式里 UI 硬编码颜色 **0**。

**已有良好实践（保留）**：样式块之间**没有**重复选择器；`!important` 0 处；`z-index` 0 处；
全站扁平无阴影依赖；三个样式块职责清晰（全局 / 内容页 / 喂知识页）；卡片 Hover 只用 border+1px 位移。

**主要问题**：
1. 令牌层缺失（已修）——之前所有颜色/尺寸散落在 3 个样式块里。
2. 组件层缺失（已修）——同一段 HTML 在 3–5 个页面各写一遍。
3. 无内联样式规范（已修）——原有 3 处 `style="…"`，现为 0。
4. **无响应式断点**（未修，见 §10）。
5. **遗留死代码**（未修，建议 Phase 2 随手清理，见下）。

**遗留死代码清单（Phase 1 只报告、不删，避免扩大范围）**：

| 项 | 证据 |
| --- | --- |
| `.stat` / `table` / `th,td` / `pre` 样式 | 旧页面（`/capture`、`/import`）退役后无任何模板引用 |
| `_status_badge()` + `.badge*` | `views.py` 中仅剩定义，全仓无调用、无测试引用 |
| `_kv()` / `_filter_links()` | 同上，仅剩定义 |
| `SOURCE_TYPE_CHOICES` | 仅出现在 `views.py` 导出列表 |

**后续返工风险**：
- `.memory-card` 这个名字同时承载记忆/来源/搜索结果（4 处测试断言该字符串）。改名收益低、风险高，**留到 Phase 2/3 真正重排卡片时一起做**。
- 卡片目前没有“右侧操作区”，未来加“合并/演化”时会需要从左到右的结构调整；`extra_html` 槽位可先承载操作行。
- 搜索结果卡片是“标题可点 + 卡片不可点”，与列表页“整卡可点”不一致；Phase 3 统一时再定。

## 13. 未来 Phase 兼容性评估

| 未来功能 | 当前 Design System 能否承载 | 说明 |
| --- | --- | --- |
| **图片输入**（Capture） | 能 | 输入区是页面私有结构，卡片 `extra_html` 可放缩略图；只需新增 `--radius-*`/`--color-*` 复用，无结构改动 |
| **Memory Merge** | 基本能，需小改 | 需要“多选卡片 + 批量操作条”。`entity_card` 左侧可加勾选框（用 `meta_html` 或新增 `leading_html` 槽位）；批量条属于新组件，但可直接复用 `action_link` + 令牌 |
| **Memory Evolution** | 能 | 演化历史是详情页的新 `section`，`section(title, body)` 直接复用 |
| **标签** | 能 | `extra_html` 槽位即为此预留；胶囊样式复用 `.status` 家族（建议 Phase 2 抽出 `tag_chip`，现在不做） |
| **Search Filter** | 能 | 已有 `.chip` 家族 + `details.conditions` 折叠面板，扩展筛选项即可 |
| **Source Preview** | 能 | 来源摘要是卡片 `excerpt`；预览面板可复用 `.manuscript` + `--radius-lg`，无需新令牌 |
| **Chat / Graph** | 未验证 | 1.0 没有这两个页面（导航只有三项）。Graph 完全没有后端能力，Phase 后段若要做，必须先有真实能力再上 UI |

**结论**：现有卡片（meta/extra 槽位）+ 统一状态组件 + 令牌层足以承载上述 1.1 方向，
唯一需要提前注意的是 **Merge 需要的“多选 + 批量操作”**，届时扩展 `entity_card` 的 leading 槽位即可，
不必现在动手。

## 14. 验证基线（Phase 1 结束时）

```text
python -m unittest discover -s tests -t .      → 614 tests, 0 failed, 0 errors, 2 skipped
python -m compileall -q personal_memory tests  → 0
python -m personal_memory --help               → 正常
视觉回归（87 个元素 computed styles）           → 1698 项一致 / 129 项按令牌合并变化 / 0 元素消失 / 0 处位移 >6px
浏览器页面回归（6 页 + 交互 + 控制台）          → 全部渲染正常，0 个 JS 错误
```

未安装因此**未验证**：`ruff`、`mypy`、`eslint`、`tsc`、`pytest`（`pyproject.toml` 只在 `dev` extra 里声明 pytest）。

---

## 15. Capture /「喂知识」页面规范（Phase 2）

Capture 页面是全站**最简单、最聚焦**的页面，也是唯一允许把卡比当主角的页面。

### 15.1 交互模型

```text
点卡比的嘴（或拖拽/粘贴）
        ↓
统一输入区（一个输入框）
        ↓
文件优先 → 网址 → 文字（前端自动分流，用户不需要先判断类型）
        ↓
既有 ingestion 端点：/import-pdf · /import-chat · /import-file · /import-url · /capture
```

* 文本与网址共用一个 `<textarea id="feed-text">`：提交时若匹配 `^https?://\S+$` 走 `/import-url`，否则走 `/capture`。
* 文件通过「添加文件」（`#feed-add-file` → 隐藏的 `#feed-chat-file`）、拖拽到卡比、或 Ctrl+V 进入；
  分流只有一处实现 `sendFile(file)`：`.pdf → /import-pdf`，角色文本/`.chat`/`.json` → `/import-chat`，其余 → `/import-file`。
* 选中文件后显示胶囊「已选择：<文件名>」并提供 ✕ 清除；**胶囊用 `.hidden` 类控制显隐**，
  不要用 `[hidden]` 属性（`display: inline-flex` 会覆盖它 —— 这是 Phase 2 修过的真实 bug）。

### 15.2 页面内容规则（禁止回流）

页面上只能有：**标题 + 一行提示 + 统一输入区 + 状态反馈**。以下内容不得再出现在 Capture 页面：

| 禁止项 | 原因 |
| --- | --- |
| 「喂一个网址」「喂一个文件」等类型入口 | 用户不该先判断输入类型；类型识别是系统的职责 |
| 实现说明（模型/数据库/记忆形成过程） | 属于系统实现细节，不是 Capture 的交互 |
| 「我的记忆」「搜索」快捷入口 | 全局导航的职责，不在页面内重复 |
| 类型选择器（`<select>`）、多输入框 | 一个输入区解决所有输入 |

### 15.3 状态反馈（四态，均由真实业务结果驱动）

| 状态 | 表达 | 来源 |
| --- | --- | --- |
| 处理中 | `#kirby-note` 显示「正在处理……」 | 前端（请求期间） |
| 成功 | 卡比显示「吃饱了~」+ note「记住了 N 件事」 | 后端 `feedback` / `note` |
| 没有形成长期记忆 | note「这次没有留下长期记忆」 | 后端（`ok: true`、`memory_count: 0`）；**不是失败** |
| 失败 | 卡比「这次没吃下去」+ 后端给的中文原因 | 后端 `message` / `hint` |

状态机（`KIRBY_TRANSITIONS`）：`idle → detected → open → inhale → closed → processing → feedback → idle`，
失败时从 `processing` 走 `fail` 回 `idle`，任何情况下都不卡住。

### 15.4 样式与组件复用

* 输入区只用 Phase 1 令牌：`.capture-row`（`--space-*` 间距、flex-wrap）、`.capture-file`（`--color-surface-subtle`、`--border`、`--radius-pill`、`--text-caption`）。
* 按钮复用全局 `.btn` / `.btn.primary`；输入框复用全局 `input/textarea` 规则；没有 Capture 专用调色板。
* 页面不再有内联 `style="…"`；页面样式里没有任何硬编码颜色（由 `tests/test_design_system.py` 锁定）。

### 15.5 未来扩展（本阶段未实现，只确认结构可承载）

| 未来能力 | 承载方式 |
| --- | --- |
| 图片输入 | 复用同一 `sendFile()` 与文件胶囊；后端新增类型即可，前端无需改结构 |
| 更多文件类型 | 只改 `accept` 与 `sendFile` 的分支判断 |
| 多附件 | 胶囊改成多个（同一 `.capture-file` 结构重复渲染） |
| Capture History | 输入区下方新增一个 `section("最近喂过", …)` 区块即可 |
| 批量输入 | 提交按钮旁边加一个「批量」入口；`sendFile` 已是可复用单元 |

---

## 16. Memories /「我的记忆」页面规范（Phase 3）

### 16.1 阅读层级

```text
页面标题（我的记忆）
   ↓
状态栏目（已记住 / 待确认 / 已归档）——状态只在这里出现
   ↓
Source 分组（这一组记忆来自什么材料）
   ↓
Memory 卡片（标题 + 一行内容，可选「另有 N 份来源」）
```

* **状态由栏目表达**：`MEMORY_STATUS_SECTIONS` 定义栏目顺序与说明文案；筛选胶囊保留（`?status=` 仍是后端既有参数）。
* **材料负责组织**：同一 Source 产生的多条 Memory 归入同一个 `.source-group`，分组头显示 `type_chip` + 材料标题（链接到 `/sources/<id>`）+ 「N 条记忆」。
* **单条卡片不重复状态**：卡片里不允许出现 `.status` 胶囊（由 `tests/test_memories_grouping.py` 锁定）。
* 没有来源的记忆（`user_explicit` 等）归入「没有来源的记忆」分组，永远排在每个栏目的最后，**不允许因为分组而丢失**。

### 16.2 多来源 Memory 的规则

一个 Memory 可能有多份 Source。渲染规则：

```text
主来源（列表中的第一份）→ 该 Memory 出现在这一组
其余来源            → 不另建分组，卡片上提示「另有 N 份来源」
```

因此 **一个 Memory 永远只渲染一次**，详情页的「来自」才列出全部材料。真实数据中目前没有多来源 Memory，
该分支由 `test_memory_with_several_sources_is_rendered_once` 覆盖。

### 16.3 空状态

| 情况 | 文案 |
| --- | --- |
| 完全没有记忆 | 「这里还没有记忆」+「去喂一点知识给卡比吧」+ 去喂知识 |
| 某个状态栏目没有记忆 | 「这个状态下还没有记忆」+「换个状态看看，或者去喂一点知识」+ 去喂知识 |
| 某个 Source 没有可展示 Memory | 结构上不存在（分组来自记忆，不来自 Source 列表）；Source 详情的「卡比从这里记住了」保留原空状态 |

### 16.4 扩展预留

* `entity_card(leading_html=…)` 槽位已存在（**未启用**）：Memory Merge 未来可在此放多选控件。
* 详情页由 `section(...)` 组成，Memory Evolution / History 未来直接追加一个区块即可。
* 标签可放 `extra_html`；多来源证据已由详情页的「来自」承担。

---

## 17. Search /「搜索」页面规范 + 全局一致性（Phase 4）

### 17.1 Search 的五个状态

| 状态 | 表达 | 来源 |
| --- | --- | --- |
| `idle` | 「搜索你的知识 / 输入关键词开始寻找」（轻量两行，不占满屏） | 无 query |
| `searching` | 提交瞬间按钮变「搜索中……」并禁用（防重复提交） | 页面脚本 |
| `results` | 「找到 N 条记忆」+ 记忆卡片（标题 → 摘要 → 状态/来自 → 点击进详情） | 既有 Retrieval |
| `empty` | `empty_state("没有找到相关内容", "换个关键词试试")` + 去喂知识 | 检索返回 0 |
| `error` | `empty_state("搜索暂时出了点问题", <中文原因>)` | 检索异常 |

搜索结果与「我的记忆」使用同一套卡片（`entity_card` + `card_list`），只有**点击行为**不同：

```text
我的记忆：整张卡片是 <a>（href=/memories/<id>）
搜索结果：卡片是 <div class="memory-card is-stretched">，标题链接用 ::after 铺满整卡，
          「来自」里的来源链接 position: relative + z-index 保持独立可点
```

这样两边都是「整卡可点」，同时不用嵌套 `<a>`（搜索结果卡片里还有来源链接）。

### 17.2 最近搜索（浏览器本地存储）

| 规则 | 实现 |
| --- | --- |
| 存储位置 | `localStorage["pkb.search.recent"]` —— **不落库、不加接口** |
| 最多保留 | **8 条** |
| 去重 | 重复搜索同一个词：先 `filter(term)` 再 `unshift`，移动到最前面，不产生第二条 |
| 记录时机 | 页面加载时若 `?q=` 非空即记录（等价于「搜索成功后记录」） |
| 点击 | 历史项是 `<a href="/search?q=<encodeURIComponent(term)>&status=…">` —— 恢复关键词并**立即执行** |
| 无记录 | 整块 `#recent-searches` 保持 `hidden` |
| 清除 | `#recent-clear` 只写空数组并重渲染；**不触碰** Source / Memory / 检索数据 |
| 安全 | 用 `textContent` 渲染关键词，搜索词不会被当成 HTML |
| 位置 | 只在搜索页内，**不进全局导航** |

### 17.3 全局一致性规则（Phase 1–4 收口结论）

1. **令牌**：任何页面的颜色/字号/间距/圆角都必须写 `var(--…)`；页面样式里出现 `#hex` 即视为回归
   （唯一例外是 `components.ILLUSTRATION_HEXES` 里的卡比插画色）。测试：`tests/test_global_consistency.py`。
2. **一个 h1**：每页只有一个 `<h1>`（Capture=卡比，Memories=我的记忆，详情=对象标题，Search=搜索）。
3. **导航**：所有页面共用 `_nav()` 的 `喂知识 / 我的记忆 / 搜索`，页面内不得再放这些入口。
4. **卡片**：`.memory-card` 与 `.card` 使用同一个 `--radius-lg`，描边来自 `--color-border*`。
5. **状态**：空状态统一 `empty_state()`，提示统一 `_banner()`（`.ok/.err/.info`），页面不得自造。
6. **无内联样式**：`style="…"` 在整个前端为 0 处。
7. **死代码**：`.badge*`、`.stat`、`table/th,td`、`pre` 与 `_status_badge()/_kv()/_filter_links()` 已在 Phase 4 删除；
   新增页面前先确认是否真的需要新样式。
8. **响应式**：仍然是流式布局（无 `@media`）；390 / 768 / 1180 三个宽度实测无横向溢出。
