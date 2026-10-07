# 第四阶段：搜索 UI 重设计

本阶段只做前端 UI/UX：把 `/search` 从"带技术字段的表单 + 结果卡片"改成
**「快速找回已经记住的知识」**。搜索算法、Retrieval、schema **一行未改**（§8 有指纹证据）。

---

## 1. 搜索页面改前审计（真实实现，2026-10-05 实测）

| # | 检查项 | 真实情况 |
| --- | --- | --- |
| 1 | `/search` 页面 | 一个大表单（关键词 / `Type` / `Status` / `Limit`，标签全英文）+ 结果卡片列表 |
| 2 | 请求如何发起 | `GET /search?q=&type=&status=&limit=`（普通表单 GET，无 JS） |
| 3 | 使用的 Retrieval | `context.retriever.search(text, limit=…, type=…, status=…)` → `MemoryRetriever`（FTS5 index + `search_like` fallback，deterministic ranking） |
| 4 | 搜索参数 | `q`（关键词）、`type`（all/knowledge/experience/event/profile）、`status`（active/pending/archived/all，默认 active）、`limit`（1–100） |
| 5 | 返回结构 | `RetrievalResult`：`query / hits / total / limit / offset / statuses / memory_type / mode / took_ms / index_matches / like_only_matches / truncated`；每个 `MemoryHit`：`memory / score / score_kind / matched_fields / sources(SourceRef…)` |
| 6 | 结果里的 Memory 字段 | 完整 `Memory`（title/content/summary/status/type/tags/importance/confidence/created_at…） |
| 7 | 是否包含 Source | **包含**：`MemoryHit.sources`（`SourceRef`：id/type/title/url/created_at），处理器还会额外查 `get_sources_for_memory()` |
| 8 | 空查询行为 | 处理器里 `if text.strip():` 才检索 → **空查询不调用检索**，页面只有表单（没有"最近记忆"语义） |
| 9 | 空结果行为 | 渲染 `没有匹配 “X” 的 Memory。`（半中文，仍出现英文 Memory） |
| 10 | 英文 / 技术字段 | 标题 `Search`；表单标签 `Type / Status / Limit`；按钮 `Search`；摘要 `mode=index, 3.1 ms, statuses=active, type=all`；每条结果 `score=0.42`、类型徽章（`knowledge` 等原始枚举）、`Source：` |

**结论**：数据足够（连每条结果的 Source 都已经在 hit 里），**不需要任何后端改动**；本阶段只做呈现。

## 2. 修改文件

| 文件 | 修改 |
| --- | --- |
| `personal_memory/web/views.py` | 新增 `MEMORY_TYPE_LABELS`（knowledge→知识 / experience→经历 / event→事件 / profile→画像）；**重写** `search_page()`：搜索框 + 折叠的「更多条件」+ Memory 优先卡片 + 轻量「来自：」出处 + 中文空状态 / 错误态；新增 `.search-box/.conditions/.result-count/.from-line` 样式；出处标题截断到 24 字 |
| `personal_memory/web/server.py` | `_handle_search`：检索调用加 `try/except MemorySystemError` → 记日志 + 给搜索页一句中文提示（不再抛到通用错误页暴露异常名）；把 `error` 传给视图 |
| `tests/test_search_ui.py`（新） | 13 个测试：默认页简洁中文、可见文字无英文/技术词、结果来自真实检索且是卡片、Memory→真实详情、Source 出处可点、无来源时不假造、多来源汇总、空结果不是错误、首页/我的记忆/来源页回归、导航与中文筛选 |
| `tests/test_web.py` | `test_5_search_uses_the_retriever` 跟随：原断言 `score=`/`mode=` **存在** → 改为断言**不存在**（技术字段已移除），并补中文状态与空结果断言 |
| `docs/SEARCH-UI.md`（新） | 本文件 |

**没有新增任何后端接口、没有新依赖、没有新表、没有改 Retrieval。**

## 3. 实际调用的 Retrieval

完全沿用既有实现：`MemoryRetriever.search(text, limit, type, status)`（在 `server._handle_search` 里调用，视图层不碰检索）。
本阶段唯一与检索相关的代码改动是**错误处理**：`MemorySystemError` 被捕获后在搜索页显示中文提示，检索逻辑与排序**未改**。
每条结果的出处直接使用 `hit.sources`（`SourceRef`），不再额外查一次 `get_sources_for_memory()`（处理器仍保留该查询作为兼容回退）。

## 4. 搜索结果如何展示

```text
搜索
你还记得什么？
[ 输入你想找的内容……              🔍 搜索 ]
▸ 更多条件（状态 / 类型 / 最多显示）

找到 4 条记忆

┌ 🧠 RAG 的核心机制是检索外部知识补上下文 ┐
│ RAG 用外部检索补上下文以降低事实性错误。  │
│ 已记住   来自：📝 检索增强生成通过检索外部… │
└────────────────────────────────────────┘
```

* **Memory 优先**，卡片语言与「我的记忆」完全一致（🧠 + 标题 + 一行摘要 + 中文状态胶囊）。
* 不再显示：`score`、`score_kind`、`mode`、`took_ms`、`statuses`、`memory_type`、原始类型枚举、`<table>`。
* 类型/状态只在折叠的「更多条件」里出现，且是**中文选项**（值仍是真实枚举，作为查询参数）。

## 5. Memory 如何跳转

卡片标题是 `<a href="/memories/<id>">`，进入第二阶段完成的 Memory Detail（**没有**为搜索单独做详情页）。
实测：点击后落在 `/memories/mem_9d20…`，标题与搜索结果一致。

## 6. Source 如何跳转

每条结果下方一行很轻的 **「来自：📄 学习记录」**：数据来自 `hit.sources`（真实关联），
每份来源是 `<a href="/sources/<id>">`，点进去就是第三阶段的来源页（原稿 + 卡比从这里记住了）。
一份记忆最多显示 2 份，多于此显示「等 N 份来源」；**没有来源时整行不显示**（不假造出处）。
搜索结果页**不展开原稿**，保持 `搜索 → Memory → Source → 原稿` 的层级。

## 7. 空结果如何处理

真实检索返回 0 条时显示：

```text
没有找到相关记忆
换个关键词试试
[ 去喂知识 ]   →  /
```

不出现「失败 / 错误 / 搜索失败」等措辞（测试断言这几类词不出现）。
空查询（没输入关键词）**不调用检索**，只显示搜索框 + 一句「输入关键词，从卡比记住的事情里找回来。」——
没有新增"最近记忆"之类的后端语义。
真检索异常时显示「搜索暂时出了点问题 / 换个关键词，或者稍后再试。」，不暴露 SQL / FTS5 / trigram / 异常名 / traceback。

## 8. 是否修改 Retrieval

**没有。** `retrieval.py` 指纹与之前完全一致（见 §9）；未新增向量/语义/RAG/Embedding，未改 deterministic ranking，
未改 `MemoryRetriever` 的任何查询逻辑。

## 9. 是否修改冻结层

**未修改**：

```
FROZEN/BACKEND CHANGED: none
```

覆盖 `models.py`、`store.py`、`capture.py`、`extraction.py`、`retrieval.py`、`quality.py`、`lifecycle.py`、`db.py`、
`docs/schema.sql`、`errors.py`、`llm.py`、`prompts.py`、`importers/*.py`、`cli.py`、`personal_memory/__init__.py`。

本阶段文件指纹：`web/views.py 42C88EB2B81D8966`、`web/server.py 30C1364434FB34D1`、
`tests/test_search_ui.py 9D7F3422B7AEAF33`、`tests/test_web.py 30D680F6C0ECC0A9`。

## 10. 浏览器实际验证（无头 Edge + CDP，真实页面与真实数据）

| 检查 | 结果 |
| --- | --- |
| 打开 `/search` | 标题 `搜索`、副标题 `你还记得什么？`、圆角搜索框 + `🔍 搜索`、折叠「更多条件」；卡片 0、表格 0、统计卡片 0 → **不密集** |
| 输入真实关键词 `RAG` | **4 条真实记忆卡片**（标题/摘要/已记住/来自），「找到 4 条记忆」，表格 0，出处行形如 `来自：📄 检索增强生成（RAG）实践要点` |
| 点击 Memory | 落在真实详情 `/memories/mem_9d20…`，标题一致 |
| 点击「来自」 | 落在真实来源 `/sources/src_4c53…`，页面有「原稿」与「卡比从这里记住了」 |
| 空结果（`zzz-绝对没有的关键词`） | 显示 `没有找到相关记忆` +「换个关键词试试」+「去喂知识」；**失败/错误/异常/traceback/Error 命中数 = 0** |
| 中文扫描 | 三个状态页面的**用户可见文字**对 `Search/Memory/Source/FTS5/LIKE/trigram/deterministic/ranking/metadata/score/bm25/knowledge/experience/Limit` **0 命中** |
| 回归 | `/` 仍 200（6 张卡比帧 +「把知识喂给我」）；`/memories`、`/sources/<id>` 均 200 |
| 导航 | `卡比的记忆小屋 喂知识 我的记忆 搜索`（没有「我的来源」，也没有 Chat/Graph） |

截图：`_workbench-recon/kb14-search-default.png`、`kb14-search-results.png`、`kb14-search-empty.png`。

## 11. 测试结果

```text
python -m unittest discover -s tests -t .
Ran 578 tests ... OK (skipped=2)
```

基线 565 → **578（+13）**：增量全部来自新增 `tests/test_search_ui.py`；
`tests/test_web.py` 的 `test_5_search_uses_the_retriever` 有 1 处断言**跟随设计变更**（原来断言 `score=`/`mode=` 存在，
现在断言这两个技术字段**不再出现**，并补上中文状态与空结果断言）。没有删除任何测试。

## 12. 当前遗留问题

1. **空查询没有"最近记忆"**：后端对空查询没有可靠语义（不调用检索），所以默认页只给搜索框——按 §十一 未改后端。
2. **结果没有分页**：沿用既有 `limit`（1–100，默认 20）与结果内的 `total`；没有"加载更多"。
3. **没有高亮匹配词**：结果卡片不带关键词高亮（`matched_fields` 是真实存在的，但高亮会引入新的展示逻辑，本阶段未做）。
4. **出处行最多显示 2 份来源**：其余以「等 N 份来源」概括，完整列表在记忆详情页。
5. **卡片标题可点、卡片本身不可整块点**：因为卡片里还有来源链接，`<a>` 不能嵌套；「我的记忆」页是整卡可点。
6. **搜索条件在折叠面板里**：随机能保留 `type/status/limit`，但没有做"记住上次条件"。
7. **`/capture`、`/import` 仍是旧外观与英文标签**（本阶段未纳入范围）。

## 13. 下一阶段建议

若继续做"产品化收尾"，建议：
1. 统一收尾 `/capture`、`/import` 的旧页面（中文标签 + 与喂知识/我的记忆同一套卡片与空状态），或明确把它们标记为"传统页面"；
2. 给搜索结果加**关键词高亮**（`matched_fields` 已在返回结构里，不需要改检索）；
3. 结果分页或"加载更多"（同样只改展示层）；
4. 若要做 Chat / Graph，必须先有真实后端能力，UI 不得先于能力出现（当前导航里刻意没有它们）。
