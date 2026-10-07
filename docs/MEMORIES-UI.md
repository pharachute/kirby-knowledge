# 我的记忆：第二阶段 UI（列表 + 单条记忆）

本阶段只做前端产品化：把「我的记忆」重做成**内容优先、中文、不像后台**的页面。
Memory System / Formation / Quality / Lifecycle / Retrieval / schema **一行未改**（§9 有指纹证据）。

---

## 1. 审计（改代码前的真实实现，2026-10-05 实测）

| 检查项 | 真实情况 |
| --- | --- |
| `/memories` 页面结构 | 服务端渲染的一张 **7 列表格**：`Title / Type / Status / Tags / Importance / Confidence / Created At / Actions`（全部英文表头） |
| 列表数据来源 | `GET /memories?status=&type=` → `repository.list_memories(memory_type=…, status=…, limit=200)`，另取 `status_counts()` |
| 详情数据来源 | `GET /memories/<id>` → `require_memory()` + `get_sources_for_memory()` + `lifecycle.allowed_transitions()` |
| 已支持的操作 | Web 层已存在 `POST /memories/<id>/{archive,restore,activate,update,delete}`（**全部真实可用**，含 CSRF 同源校验） |
| Source 关联 | `repository.get_sources_for_memory(id)`；反向 `get_memories_for_source(id)` |
| lifecycle 调用 | `context.lifecycle.archive_memory / restore_memory / activate_memory / update_memory`；状态机 `pending→active`、`pending→archived`、`active→archived`、`archived→active`，**禁止任何 →pending** |
| 英文文案 | 列表：表头 7 个、`Archive/Restore/Delete/View` 按钮、`Memories` 标题、`active=1 …` 计数；详情：`ID/Title/Type/Status/Information Origin/Importance/Confidence/Tags/Created/Updated/Content/Summary/Sources/Edit` 标签、`Allow transitions` 文本 |
| 可复用的 CSS/HTML/JS | 共享 `_STYLE`（卡片、徽章、表单、按钮）+ `layout()` 页头导航 + `_banner(flash)`；本阶段新增一套 `_MEMORY_STYLE`（卡片列表、筛选 chip、详情排版），与首页的安静风格一致 |

**缺口（当时）**：没有「相关记忆」的现成查询；没有任何中文状态文案。两者都在**不改后端**的前提下解决（见 §4、§5）。

## 2. 修改文件

| 文件 | 修改 |
| --- | --- |
| `personal_memory/web/views.py` | 新增 `MEMORY_STATUS_LABELS`（pending→待确认 / active→已记住 / archived→已归档）、`SOURCE_TYPE_LABELS`（📝文字 / 💬对话 / 📰文章 / 🔗网页 / 📄文件）、`memory_status_label()`、`source_type_label()`、`_memory_status_chip()`、`_memory_excerpt()`、`_MEMORY_STYLE`；**重写** `memories_page()`（卡片列表 + 中文筛选 chip + 空状态）与 `memory_detail_page()`（内容优先 + 来自 + 相关记忆 + 可以做什么 + 折叠修改）；`_status_badge()` 改为中文；`layout()` 增加 `heading=False`（详情页用自己的内容标题） |
| `personal_memory/web/server.py` | 详情路由计算 `related`（共享真实 Source 的其它记忆，**只用既有查询**）；生命周期/更新/删除的 flash 文案改中文 |
| `tests/test_memories_ui.py`（新） | 14 个测试：卡片内容、隐藏内部字段、中文 chip 计数、状态文案、空状态、无英文、详情内容优先、来源链接可打开、相关记忆只在真实关联时出现、四种生命周期操作、折叠编辑 |
| `tests/test_web.py` | 4 处断言跟随中文文案更新（`active=1` 计数 → 中文 chip；`archive 完成`→`已归档`；`restore 完成`→`已恢复`；`Memory 已删除/已更新`→`已删除这条记忆/已保存修改`） |
| `docs/MEMORIES-UI.md`（新） | 本文件 |

## 3. Memory 列表实际实现了什么

```text
我的记忆
卡比记住的事情都在这里，点开可以看它记住了什么、是从哪里记住的。

[全部 9] [待确认 1] [已记住 7] [已归档 1]

┌ 🧠 RAG 的核心机制是检索外部知识补上下文 ┐
│ RAG 用外部检索补上下文以降低事实性错误。  │
│ 已记住                                  │
└────────────────────────────────────────┘
```

* 卡片第一层只有 **标题 + 一行摘要 + 中文状态**；摘要优先用 `summary`，否则**安全截断 `content`**（不重新调用 LLM）。
* 不显示：ID、类型、information_origin、置信度、重要度、schema_version、创建/更新时间、原始 JSON、标签。ID 只出现在链接地址里，不作为文字出现。
* 不使用 `<table>`；卡片间距充足（一屏约 4 条）。
* 筛选保留 `全部 / 待确认 / 已记住 / 已归档`，直接复用既有 `status` 查询参数；类型筛选从界面移除（后端参数仍兼容）。
* 空状态：`这里还没有记忆 / 去喂一点知识给卡比吧` + 「去喂知识」按钮（回首页），不出现"暂无数据库记录"这类文案。

## 4. Memory Detail 实际实现了什么

阅读顺序（实测 DOM 顺序）：**内容 → 状态 → 来自 → 相关记忆 → 可以做什么 → 修改**。

```text
← 返回我的记忆

🧠 记忆 · 已记住
RAG 的核心机制是检索外部知识补上下文
检索增强生成（RAG）通过检索外部知识为模型补充上下文，从而减少事实性错误……
──────
一句话摘要：RAG 用外部检索补上下文以降低事实性错误。

来自
📝 检索增强生成通过检索外部知识……（文字）

可以做什么
[归档]

▸ 修改这条记忆（折叠）
```

* 详情页不再有 chrome 大标题（`layout(heading=False)`），**第一个标题就是记忆本身**。
* 长正文按空行分段，用正常排版显示全文；`summary` 作为「一句话摘要」单独一行。
* **相关记忆**：只有在**真实存在**（与该记忆共享同一 Source）时才显示，并注明"这些记忆与当前这条来自同一份来源"。没有真实关联就**不显示该区块**；没有引入任何相似度/图算法。

## 5. Source 跳转如何实现

「来自」列出 `get_sources_for_memory()` 的真实结果，每条是 `<a href="/sources/<id>">` + 类型图标 + 中文类型名 + 标题；
点击进入**既有** Source 详情页（实测 HTTP 200）。记忆页不复制 Source 原文，用户路径是
`记忆 → 来自（列表）→ 来源详情页 → 原稿`。

## 6. 生命周期操作（哪些真实可用）

| 操作 | 状态 | 说明 |
| --- | --- | --- |
| 归档 | ✅ 真实可用 | `POST /memories/<id>/archive`（既有接口）→ 状态变 `archived`，横幅「已归档」 |
| 恢复 | ✅ 真实可用 | `POST /memories/<id>/restore` → 变 `active`，横幅「已恢复」 |
| 记住（待确认） | ✅ 真实可用 | `POST /memories/<id>/activate`（pending→active），横幅「已记住」 |
| 先不记（待确认） | ✅ 真实可用 | `POST /memories/<id>/archive`（pending→archived） |
| 修改 | ⚠️ 提供但**折叠** | 复用**既有** `POST /memories/<id>/update`（校验、去重、状态机仍由现有逻辑负责）；没有新增任何编辑能力，也没有自由"随手改"入口 |
| 删除 | ❌ 界面不提供 | 后端 `delete` 接口保留（既有测试仍在用），但**详情页/列表页不再出现删除按钮**（§十四） |

按钮只按 `lifecycle.allowed_transitions()` 的真实结果渲染：active→只有「归档」；archived→只有「恢复」；pending→「记住」+「先不记」。

## 7. 中文检查

* 列表页、详情页**用户可见文字全部中文**；状态一律 `待确认 / 已记住 / 已归档`。
* 浏览器实测对两页做了禁用词扫描（`Memory / Memories / Pending / Active / Archived / Confidence / Importance / Metadata / Schema / Database / Source`）：**0 命中**。
  （实现过程中确实漏过一个 "Memory"——折叠编辑区的说明文字，已在测试中被抓出并改掉。）
* 仍存在的英文（本阶段范围外，属于其他页面）：`我的来源` 页与 `搜索` 页仍是旧外观，含英文标签；`/capture`、`/import` 也仍是旧页面。→ 建议下一阶段处理「我的来源」。

## 8. 浏览器实测

无头 Edge + CDP 打开**真实运行中的页面**（writable 真实数据：9 条记忆、5 个来源）：

| 检查 | 结果 |
| --- | --- |
| 列表第一眼是否简洁 | 卡片式，一屏约 4 条，无表格（`<table>` 计数 0） |
| 卡片是否醒目 | 标题加粗 + 一行摘要 + 中文状态胶囊；卡片边框轻、间距充足 |
| 是否有英文漏出 | 两页禁用词扫描 0 命中 |
| 详情页是否先看到内容 | 第一个 `<h1>` 就是记忆标题；DOM 顺序 内容 < 来自 < 可以做什么 |
| Source 是否容易找到 | 「来自」卡片带图标与中文类型，点击 → `/sources/<id>` 实测 200 |
| 操作按钮是否过多 | active 1 个（归档）、archived 1 个（恢复）、pending 2 个（记住/先不记） |
| 空状态 | 空库实例实测：`这里还没有记忆` + `去喂一点知识给卡比吧` + 「去喂知识」按钮 |
| 首页不回归 | `喂知识` 首页仍 200，6 张卡比帧 + "把知识喂给我" |
| 生命周期真实生效 | 浏览器内归档后，`已归档` 计数由 0→1、列表出现该条；随后恢复回原状态 |

截图：`_workbench-recon/kb12-memories-list.png`、`kb12-memories-detail.png`、`kb12-memories-empty.png`。

## 9. 后端冻结检查

**未修改** Memory / Source / Formation / Quality / Lifecycle / Retrieval / schema：

```
FROZEN/BACKEND CHANGED: none
```

（逐文件 `sha256[:16]` 比对：`models.py`、`store.py`、`capture.py`、`extraction.py`、`retrieval.py`、`quality.py`、
`lifecycle.py`、`db.py`、`docs/schema.sql`、`errors.py`、`llm.py`、`prompts.py`、`importers/*.py`、`cli.py`、
`personal_memory/__init__.py`。）

本阶段文件指纹：`web/views.py D715F4852014AC92`、`web/server.py 40C8DE6F9F71650C`、
`tests/test_memories_ui.py A02AEE5C99334C81`、`tests/test_web.py 8D511C25A94E2C19`。

## 10. 测试

```text
python -m unittest discover -s tests -t .
Ran 552 tests ... OK (skipped=2)
```

基线 538 → 552（+14：新增 `tests/test_memories_ui.py`）；数量变化仅来自本阶段新增的 UI 测试，
没有删除任何既有测试（`test_web.py` 只有 4 处断言跟随中文文案更新）。

## 11. 遗留问题

1. **「查看来源」没有独立按钮**：它由「来自」区块承担（点来源卡片进入来源页）。若希望像 §十三 那样在操作区也放一个「查看来源」按钮，需要指定跳到第一条来源还是来源列表页。
2. **列表无分页**：`limit=200`，一次渲染全部（当前数据量下没问题）；没有下拉加载。
3. **排序固定**：沿用 `list_memories` 的既有顺序（未做"按时间/相关度"排序，避免新增查询）。
4. **摘要来自 `summary` 或截断 `content`**：部分记忆没有 summary，列表里显示的是正文前 120 字；没有生成式摘要（按 §五 要求）。
5. **详情页展示全文**：超长记忆会让页面很长，暂无折叠/更多。
6. **其他页面仍是旧外观与英文**（`我的来源`、`搜索`、`/capture`、`/import`），与本阶段两页风格尚未统一。
7. **空状态未在真实空库上人工浏览**：用独立空库实例 + 无头浏览器验证过（截图在证据里），但没有手工点击「去喂知识」确认跳转后的首页（首页本身已单独验证 200）。

## 12. 下一阶段建议（只限「我的来源」）

1. 用**同一套**视觉语言重做 `/sources`：来源卡片（📝/💬/🔗/📄 + 标题 + 一行内容 + 时间），中文文案，去掉表格与英文标签。
2. 来源详情页做「这份来源让卡比记住了什么」反向列表，与 Memory Detail 的「来自」互为闭环：`记忆 ↔ 来源`。
3. 不再显示内部字段（content_hash、metadata 原始 JSON 等）；`captured_from`、`pdf_page_count` 这类真实元数据可用**中文小标签**呈现（例如「来自 PDF · 20 页」），而不是键值表格。
4. 来源列表同样保留筛选（全部 / 文字 / 对话 / 网页 / 文件），复用既有 `source_type`。
5. 统一 `我的来源` 与 `我的记忆` 的页头、卡片、状态胶囊与空状态样式，并补一次无头浏览器视觉检查。
