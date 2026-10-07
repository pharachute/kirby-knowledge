# 第三阶段：记忆来源追溯（Memory → Source → 原稿 → Memory）

本阶段**不新增"我的来源"导航**，只让用户从一条记忆自然走到它的原稿，再看到这份原稿形成了哪些记忆。
Memory / Source / Formation / Quality / Lifecycle / Retrieval / Capture / schema **一行未改**（§7 有指纹证据）。

---

## 1. 改代码前的 Source UI 审计（真实实现，2026-10-05 实测）

| # | 检查项 | 真实情况 |
| --- | --- | --- |
| 1 | `get_sources_for_memory()` | `store.py` 中真实存在，按 `memory_sources` 关联返回 Source 列表（第二阶段详情页已在用） |
| 2 | `get_memories_for_source()` | 真实存在，反向返回该 Source 形成的 Memory 列表 |
| 3 | `/sources/<id>` 当前页面 | `views.source_detail_page()`：四张卡片——字段表（`ID/Title/Type/URL/content_hash/Created/Updated`）、`Metadata` 原始键值表、`Content`（`<pre>` 代码块，>4000 字折叠）、`该 Source 形成的 Memory`（英文徽章） |
| 4 | Source 真实字段 | `id, source_type, title, content, content_hash, url, metadata, created_at, updated_at` |
| 5 | 保存的原稿 | `Source.content`（真实保存的正文）。文字/文件/网页/对话导入时按 Formation 判断保存；PDF 导入保存的是**提取出的正文**（不是原始二进制） |
| 6 | Source 类型 | `text / chat / article / web / file` 五种（以代码为准） |
| 7 | 详情页能否展示内容 | **能**，但以 `<pre>` 呈现，且与内部字段、原始 metadata 混在一起，第一眼像数据库详情页 |
| 8 | 是否有编辑/删除等真实 Web 操作 | **没有**。POST 路由只有 `/capture`、`/import-*`、`/memories/<id>/<action>`；`store.py` 里虽有 `update_source()/delete_source()`，但**没有对应的 Web 接口**——本次**不为它们加接口**（函数存在 ≠ UI 应暴露） |
| 9 | `/sources` 列表 | 一张表格：`Title / Type / content_hash / Created At`（含哈希与英文表头） |
| 10 | 主导航 | 当时是 `喂知识 / 我的记忆 / 我的来源 / 搜索` |

## 2. 修改文件

| 文件 | 修改 |
| --- | --- |
| `personal_memory/web/views.py` | 主导航去掉「我的来源」（§三）；新增 `_source_excerpt()`；**重写** `source_detail_page()`（原稿优先 + 卡比从这里记住了 + 返回上层的返回逻辑）；**重写** `sources_page()`（卡片式、中文、无哈希）；新增 `.source-head/.manuscript/.source-note/.more` 样式 |
| `tests/test_source_trace.py`（新） | 13 个测试：Memory→Source、原稿真实内容、无内部字段、类型中文映射、Source→Memory、双向闭环、返回逻辑、两种空状态、列表页、导航、首页/记忆页回归、长原稿展开 |
| `tests/test_web.py` | 1 处断言跟随：详情页的「已隐藏疑似凭据字段」→「有 N 项内部信息未展示」（内部 metadata 键值表不再展示） |
| `docs/SOURCE-TRACE.md`（新） | 本文件 |

**没有新增任何后端业务接口、没有新表、没有新依赖。**

## 3. Memory → Source 如何实现

沿用第二阶段的 Memory Detail「来自」区块：数据来自 `get_sources_for_memory(memory_id)`（由既有路由传入，**不重新查询、不另造一套 Source 数据**），
每一项是 `<a href="/sources/<id>">` + 类型图标 + 中文类型名 + 标题；点击进入既有的 `/sources/<id>`。
页面上**不复制** Source 原文。

## 4. Source 原稿如何展示

* 只展示 `Source.content` 里**真实保存的内容**；不重新调用 LLM、不生成摘要替代原稿、不假装保存了完整原文
  （PDF 只展示当时提取出来的正文；如果只保存了部分证据，就只展示那部分）。
* 排版：按空行分段的可读正文（`.manuscript`，行高 2.0），**不再用 `<pre>` 代码块**；下方一句
  「以上是这份来源里真实保存的内容。」
* 超过 4000 字：先显示第一段 + 「展开剩余内容（全文共 N 字）」，避免一屏几万字。
* 有 `url` 时提供「🔗 打开原网页」；有敏感 metadata 时只显示「有 N 项内部信息未展示。」，
  **不再渲染原始 metadata 键值**（那些键是英文内部字段名，会违反中文要求）。
* 没有可展示内容时：「暂时没有可以查看的原稿。」（注意：`Source.content` 由 model 约束为非空，真实数据走不到这个分支，它只是防御性分支）。

## 5. Source → Memory 如何实现

`source_detail_page(memories=…)` 使用既有的 `get_memories_for_source(source_id)`（由路由传入）。
区块标题固定为 **「卡比从这里记住了」**（§十：共享 Source ≠ 语义相关，所以**不写「相关记忆」**），
每条是 🧠 + 标题 + 中文状态，整体链接回 `/memories/<id>`，形成 `Memory → Source → Memory` 闭环。
没有任何记忆时显示「卡比还没有从这里留下记忆。」

## 6. Source 类型中文映射

以真实代码为准（`SourceType` = text/chat/article/web/file）：

```text
text    → 📝 文字
chat    → 💬 对话
article → 📰 文章
web     → 🔗 网页
file    → 📄 文件
```

界面上**只出现中文 + 图标**，不出现 `text/chat/article/web/file` 这些枚举值（测试逐类型断言）。

## 7. 是否增加了新的业务接口

**没有。** 本阶段只改展示层：没有新的 GET/POST 路由，没有新增 Source 编辑/删除/重处理/下载/AI 总结等能力
（这些在 Web 层本来就不存在，也没有为它们开口子）；`store.py` 的 `update_source()/delete_source()` 保持"有函数、无 UI"的状态。

## 8. 是否修改冻结层

**未修改。** 逐文件 `sha256[:16]` 比对（本阶段前后）：

```
FROZEN/BACKEND CHANGED: none
```

覆盖 `models.py`、`store.py`、`capture.py`、`extraction.py`、`retrieval.py`、`quality.py`、`lifecycle.py`、`db.py`、
`docs/schema.sql`、`errors.py`、`llm.py`、`prompts.py`、`importers/*.py`、`cli.py`、`personal_memory/__init__.py`。

本阶段文件指纹：`web/views.py 5D6ADF8798B55422`、`web/server.py 40C8DE6F9F71650C`（未改）、
`tests/test_source_trace.py 23403FCFB61EBFD6`、`tests/test_web.py 25FCB32741AA2591`。

## 9. 浏览器实际验证（无头 Edge + CDP，真实页面与真实数据）

真实数据：5 个 Source / 9 条 Memory（含一个 4098 字的 PDF 来源、一个 1635 字的网页来源）。

| 检查 | 结果 |
| --- | --- |
| 导航 | `卡比的记忆小屋 喂知识 我的记忆 搜索`（**没有**「我的来源」） |
| Memory → Source | 打开真实记忆 → 「来自」有 1 条来源链接 → **在浏览器里点击该链接** → 落在 `/sources/src_4c53…` |
| 原稿 | 详情页首个区块就是「原稿」，显示真实保存内容（该来源 49 字），下面一句「以上是这份来源里真实保存的内容。」 |
| Source → Memory | 「卡比从这里记住了」显示记忆卡片（🧠 + 标题 + 已记住），**没有**出现「相关记忆」 |
| 双向闭环 | 在来源页点击那条记忆 → 回到 `/memories/mem_9d20…`，该记忆的「来自」又指回同一来源 |
| 中文/无内部字段 | 页面可见文字禁用词扫描（Source/Sources/Memory/Memories/metadata/content_hash/source_type/active/pending/archived）**0 命中**；`<table>`=0、`<pre>`=0 |
| 长原稿 | 4098 字的 PDF 来源：kicker `📄 文件`、标题 `Find any action inside the IDE`、显示首段 + **「展开剩余内容（全文共 4098 字）」**；PDF 内部字段（`pdf_page_count`/`captured_from` 等）**没有出现** |
| 返回 | `← 返回` 的 href 是 `/memories`，并带同源 referrer 判断优先 `history.back()`（从记忆详情进来时回到那条记忆） |
| 来源列表 | `/sources` 已是 5 张卡片、**0 个表格**、无哈希 |
| 首页回归 | `/` 仍 200，6 张卡比帧 + 「把知识喂给我」 |

截图：`_workbench-recon/kb13-memory-with-from.png`、`kb13-source-detail.png`、`kb13-source-long.png`。

## 10. 测试结果

```text
python -m unittest discover -s tests -t .
Ran 565 tests ... OK (skipped=2)
```

基线 552 → **565（+13）**，增量全部来自新增 `tests/test_source_trace.py`；`tests/test_web.py` 仅 1 处断言跟随
（详情页不再展示原始 metadata 键值表 → 改断言中文提示）。没有删除任何测试。

## 11. 遗留问题

1. **空原稿分支不可达**：`Source.content` 由 model 约束为非空，所以「暂时没有可以查看的原稿」只在防御场景生效（测试用轻量对象覆盖）。
2. **PDF/网页来源只保存提取后的正文**：看不到原始 PDF 文件或网页快照；这是当前后端真实的保存范围，UI 不伪造。
3. **`/sources` 列表页没有入口**（不在主导航），只能通过直接 URL 或记忆详情进入；这是 §三/§十七 的刻意选择。
4. **没有分页**：来源列表 `limit=200`、记忆列表 `limit=200`。
5. **返回按钮基于 referrer 判断**：从外部直接打开来源页时回到「我的记忆」，而不是浏览器上一页（避免跳出应用）。
6. **仍然没有来源层面的任何操作**（编辑/删除/重新处理）：本阶段按 §十一 不新增，且 Web 层本来就没有。
7. **其他页面仍是旧外观**（`搜索`、`/capture`、`/import`）与部分英文标签，尚未与本阶段统一。

## 12. 下一阶段建议

下一阶段若要做「搜索」，建议：
1. 复用本阶段的卡片语言：搜索结果直接显示记忆卡片（🧠 标题 + 一行摘要 + 中文状态），点击进入记忆详情；
2. 搜索结果里保留「来自」的轻量提示（例如「来自：📄 学习记录」），让用户一眼看到出处，再点进来源追溯原稿；
3. 不做语义搜索/向量检索/RAG（超出当前冻结能力），只做好现有 `retrieval` 的关键词结果的呈现；
4. 中文与空状态统一（`没有找到相关记忆` + 「去喂知识」入口）；
5. 顺带把 `/capture`、`/import` 的英文标签整理掉，或明确保留为"传统页面"。
