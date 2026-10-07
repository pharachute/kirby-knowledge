# 阶段 3：Memory Retrieval（记忆召回）

> 状态：**已完成并实测通过**（2026-10-04，package `0.3.0`，DB schema `v2`）
> 唯一目标：根据用户查询，从已保存的 Memory 中找到相关记忆，并返回这些 Memory 及其 Source 关系。
> 本阶段**不调用 LLM、不联网**：没有 API Key、没有网络也能完整运行。

---

## 1. 新增/修改的文件

| 文件 | 作用 |
| --- | --- |
| `personal_memory/retrieval.py` | 检索服务：查询归一化 → 分词路由 → 排序 → 载入 Memory → 载入 Sources → `RetrievalResult` |
| `scripts/phase3_fts5_lab.py` | FTS5 中英文行为实验（本阶段设计依据） |
| `scripts/phase3_demo_data.py` | 可复现的演示数据（规格书 §十一 的三条记忆 + 来源） |
| `tests/test_retrieval.py` | 检索行为测试（26 项验收 + CLI） |
| `tests/test_search_index.py` | migration v2 / backfill / 触发器同步 / 守卫 / 错误翻译 |
| `docs/PHASE3.md` | 本文档 |
| `docs/phase3-fts5-lab.{json,txt}`、`docs/phase3-search-*.{txt,json}` | 实测证据 |

修改（都是必要且最小的）：

| 文件 | 修改 | 原因 |
| --- | --- | --- |
| `personal_memory/db.py` | 新增 **migration v2**：两个 FTS5 索引 + backfill + 6 个同步触发器；`Migration` 增加 `triggers` 字段；`_verify_schema` 增加触发器与列校验；迁移失败包装为 `SchemaError`；迁移前先校验"文件自称的 schema" | 阶段 3 需要检索索引，且阶段 1 规定 schema 变更必须新增 migration |
| `personal_memory/store.py` | 新增 `search_index` / `search_like` / `index_row_count` / `get_memories_by_ids` / `get_sources_for_memories` / `get_memory_created_at` | 检索的 SQL 属于数据访问层，retrieval 模块不写散乱 SQL |
| `personal_memory/cli.py` | 新增 `search`、`source` 命令；`version` 区分 `schema_version`(DB) 与 `memory_schema_version`(行) | 真实验收入口 |
| `personal_memory/__init__.py` | 导出检索 API；版本 → 0.3.0 | 公开 API |
| `tests/test_cli.py`、`tests/test_persistence.py`、`tests/test_schema_integrity.py` | 断言从"schema=1"改为 `SUPPORTED_SCHEMA_VERSION`(=2) / `len(MIGRATIONS)` | schema 版本确实从 1 变成 2；其余行为未变 |

**未修改**：`Source` / `Memory` / `memory_sources` 模型、`MemoryRepository` 既有方法、`extraction.py`、`llm.py`、`prompts.py`、migration v1。

---

## 2. Retrieval 数据流

```
query
  │  1. 校验：非空、非纯标点；limit/offset/type/status 合法
  ▼
normalize / tokenize（按空白切分，逐 token 路由）
  │  2. 路由：
  │       含 CJK 且 ≥3 字 → memory_fts_trigram（trigram，子串）
  │       含 CJK 且 <3 字 → LIKE 回退（无 tokenizer 能匹配 2 字中文）
  │       其余（拉丁/数字）→ memory_fts_word（unicode61，前缀匹配 "tok"*）
  ▼
search index（全部参数化：MATCH 表达式与 LIKE 模式都作为绑定参数）
  │  3. 合并候选，去重，保留每个 memory 的最好信号
  ▼
ranking
  │  4. 稳定排序：tier(索引优先) → score 降序 → 字段优先级 → created_at 降序 → id 升序
  ▼
load Memory（批量 IN 查询，无 N+1）
  │  5. 计算 matched fields（title/content/summary/tags 子串命中）
  ▼
load related Sources（memory_sources JOIN sources，批量）
  │  6. 只带来源元数据（id/type/title/url/created_at），不带正文
  ▼
RetrievalResult（hits[] = Memory + score + matched_fields + sources）
```

公开 API：

```python
retriever = MemoryRetriever(repo)
result = retriever.search("RAG", limit=5, offset=0, type=None, status="active")
result.hits[0].memory        # 完整 Memory
result.hits[0].score         # 分数（越大越相关）
result.hits[0].score_kind    # "bm25" | "like"
result.hits[0].matched_fields
result.hits[0].sources       # SourceRef（不含正文）
retriever.resolve_source(id) # 明确读取原文的唯一入口
```

---

## 3. 为什么选择当前检索方案

先做实验（`scripts/phase3_fts5_lab.py`，SQLite 3.45.3），再定方案。实测结论：

| 查询类型 | `unicode61` | `trigram` | `LIKE` |
| --- | --- | --- | --- |
| `RAG`（英文缩写） | ✅ mem_1, mem_6, mem_4（按 bm25 排序） | ⚠️ mem_5, mem_1, …（**把 storage 也匹配进来**，且 bm25 全为 -0.0） | ⚠️ 子串噪音 |
| `长期记忆`（中文 4 字） | ❌ 完全找不到 | ✅ mem_2, mem_7（有 bm25 区分度） | ✅ |
| `记忆`（中文 2 字） | ❌ | ❌ | ✅ |
| `存储`（中文 2 字） | ❌ | ❌ | ✅ |
| `"` `'` `AND` `local-first`（未加引号直接 MATCH） | ❌ 语法错误 | ❌ 语法错误 | ✅ |
| 同一批字符（每个 token 加引号后） | ✅ 安全（匹配 0 条） | ✅ 安全 | ✅ |

由此得到的设计：

1. **两个 FTS5 索引**，因为一个 tokenizer 无法同时服务两种语言：
   * `memory_fts_word`（unicode61）：拉丁/数字按**词 + 前缀**匹配，`RAG` 不会命中 `storage`，`retriev` 能命中 `retrieval`，bm25 有区分度；
   * `memory_fts_trigram`（trigram）：中文 ≥3 字按子串匹配，uni code61 对中文完全失效。
2. **LIKE 回退只服务 1–2 字中文**：这是 FTS5 无法覆盖的唯一情形；拉丁查询绝不使用子串匹配（避免 `OR` 命中 `Memory` 这类噪音）。
3. **每个 token 强制加引号**再进入 MATCH：实验表明这是让 `"` `'` `*` `AND` 等敌意输入变安全的关键；查询全部参数化，不做字符串拼接。
4. **不引入** PostgreSQL / Elasticsearch / 向量库：个人规模下 SQLite 足够，重点是正确、可解释、可测试。

---

## 4. SQLite FTS5 中文实验结果（原始证据）

完整数据：[`docs/phase3-fts5-lab.json`](phase3-fts5-lab.json)、可读版 [`docs/phase3-fts5-lab.txt`](phase3-fts5-lab.txt)。

```
SQLite 3.45.3  | FTS5 compiled: True
query             tokenizer   ids                     bm25
RAG               unicode61   mem_1,mem_6,mem_4       [-0.3709, -0.3178, -0.2931]
RAG               trigram     mem_5,mem_1,mem_6,...   [-0.0, -0.0, -0.0, -0.0, -0.0]
长期记忆            unicode61   []                      []
长期记忆            trigram     mem_2,mem_7             [-0.9608, -0.9367]
记忆              unicode61   []                      []
记忆              trigram     []                      []
记忆              LIKE        mem_2,mem_6,mem_7
本地存储            unicode61   mem_3                   [-1.1667]
本地存储            trigram     mem_5,mem_3             [-0.8152, -0.6364]
维护成本            unicode61   []                      []
维护成本            trigram     mem_7                   [-1.742]
"                 （未加引号）  ERROR: unterminated string
'                 （未加引号）  ERROR: fts5: syntax error near "'"
AND               （未加引号）  ERROR: fts5: syntax error near "AND"
local-first       （未加引号）  ERROR: no such column: first
（以上全部加引号后：安全，匹配 0 条；bm25 方向：越负越相关，升序=最优在前）
```

**结论**：中文短词（1–2 字）和未加引号的敌意字符是两个真实陷阱；项目用"双索引 + 短中文 LIKE 回退 + 全量加引号"覆盖，而不是把它们记作"FTS5 的限制"后放弃。

---

## 5. Migration 如何工作

* `MIGRATIONS` 现在是 **v1 `initial_schema` + v2 `memory_search_index`**，`SUPPORTED_SCHEMA_VERSION = 2`；v1 一字未改。
* v2 的 10 条语句：建两张 FTS5 表 → backfill 两张表（`INSERT … SELECT id, title, content, COALESCE(summary,''), group_concat(json_each(tags_json)) FROM memories`）→ 每张表 3 个触发器（AFTER INSERT / UPDATE / DELETE）。
* **同步机制是触发器，不是启动时全表重扫**：新增/更新/删除（含归档）Memory 时索引自动跟随；`update` 触发器先删后插，`delete` 触发器按 `memory_id` 清理。
* `initialize()` 顺序：建 `schema_migrations` → 读取当前版本 → **先校验文件自称的 schema**（缺表/缺列/缺触发器 → `SchemaError`，不会让新迁移去"发现"坏文件）→ 需要迁移时，先拒绝同名但结构不对的表 → 逐条执行（任何 sqlite 错误包装成 `SchemaError`）→ 记录 `(version,name,applied_at)` → 事后再次校验。
* **幂等**：重复 `initialize()` → `applied_count = 0`，不重建、不破坏索引（测试 `test_repeated_initialize_does_not_rebuild_or_break_the_index`）。
* **已有数据的 backfill**：测试 `test_existing_phase1_phase2_database_is_backfilled` 手工造一个只应用到 v1、且已有 Memory/Source/关联的库，`initialize()` 只应用 v2 并让旧记忆立刻可检索。

---

## 6. 搜索结果如何排序

排序键（从主到次，全部确定性，绝无随机）：

1. **tier**：索引命中（`memory_fts_word` / `memory_fts_trigram`）排在仅 LIKE 回退命中之前；
2. **score 降序**：`score_kind="bm25"` 时 `score = -bm25(...)`（SQLite bm25 越负越相关，取负后"越大越相关"）；`score_kind="like"` 时是字段权重和（title 3、tags 2、summary 2、content 1）。不做 0–1 归一化，`score_kind` 明确标注量纲。
3. **字段优先级**：title > tags/summary > content。当 bm25 无区分度时（例如术语出现在所有候选里 → bm25 ≈ 0）由它决定顺序，这正是规格书 §十二 的场景：
   ```
   A title="RAG 的基本原理" / B content="…提到 RAG" / C tags=["RAG"]
   查询 RAG → A, C, B（稳定可复现，测试断言三连跑结果一致）
   ```
4. **created_at 降序**；5. **id 升序**（兜底，保证完全可复现）。

`RetrievalResult` 自带 `score_direction` 字符串用于说明方向；每个 hit 还保留原始 `bm25` 与 `index`，便于解释。

---

## 7. Source 如何关联

* 检索走的是阶段 1 的 `memory_sources` 关系模型，没有新的关联结构：`get_sources_for_memories()` 一次 `JOIN` 批量取出（避免 N+1）。
* 每个 hit 带 `tuple[SourceRef, ...]`，字段：`id`、`source_type`、`title`、`url`、`created_at`；**不包含 Source 正文**，避免把大段原文塞进每次搜索结果。
* 需要原文时使用明确入口：`MemoryRetriever.resolve_source(source_id)`（Python）或 `python -m personal_memory source <source_id>`（CLI）；`SourceRef.as_dict()` 里也带 `content_hint` 指向该入口。
* 已验证：1 Memory → 2 Sources 返回两个；1 Source → N Memories 时每个命中都能回到该 Source。

---

## 8. 自动化测试结果

```powershell
python -m unittest discover -s tests -t . -v
# Ran 229 tests … OK (skipped=1)
```

* 阶段 1+2 的 163 个测试**全部继续通过**（其中 3 个文件的断言更新为 `SUPPORTED_SCHEMA_VERSION`/`len(MIGRATIONS)`，因为 schema 版本确实变了）；
* 阶段 3 新增 66 个测试：`tests/test_retrieval.py`（检索行为、过滤、排序、Source 关联、索引一致性、错误处理、CLI）+ `tests/test_search_index.py`（migration v2、backfill、触发器同步、守卫、漂移检测/修复、错误翻译）。

规格书 §十九 的 26 项覆盖情况（测试名可直接搜索）：

| 要求 | 测试 |
| --- | --- |
| 1 FTS5/检索基础 | `test_migration_list_contains_v1_and_v2`、`test_fresh_database_has_indexes_and_triggers`、`test_index_definition_is_fts5_trigram_and_word` |
| 2 英文查询 | `test_2_english_query`、`test_2b_english_query_is_case_insensitive` |
| 3 中文查询 | `test_3_chinese_query` |
| 4 中文短查询 | `test_4_chinese_short_query_uses_the_like_fallback`、`test_4b_latin_tokens_use_word_prefix_not_substring` |
| 5–8 title/content/summary/tags | `test_5_search_title`、`test_6_search_content`、`test_7_search_summary`、`test_8_search_tags` |
| 9 默认 active | `test_9_default_status_is_active_only` |
| 10 status 过滤 | `test_10_explicit_status_filters` |
| 11 type 过滤 | `test_11_type_filter`、`test_type_and_status_combine` |
| 12 limit/offset | `test_12_limit_and_offset` |
| 13 稳定排序 | `test_13_ordering_is_deterministic_and_field_aware`、`test_score_ties_are_broken_by_field_priority_not_by_chance`、`test_updated_field_changes_its_rank` |
| 14–16 Source 关联 | `test_14_sources_are_returned_with_the_hit`、`test_15_one_memory_with_two_sources`、`test_16_one_source_with_multiple_memories` |
| 17 创建后可检索 | `test_17_created_memory_is_immediately_searchable` |
| 18 更新后索引正确 | `test_18_updated_memory_index_is_correct` |
| 19 删除后不再返回 | `test_19_deleted_memory_is_not_returned` |
| 20 归档后默认不返回 | `test_20_archived_memory_is_hidden_then_restored`、`test_archiving_and_restoring_keeps_the_index` |
| 21 backfill | `test_existing_phase1_phase2_database_is_backfilled` |
| 22 重复 initialize | `test_repeated_initialize_does_not_rebuild_or_break_the_index` |
| 23 空查询拒绝 | `test_23_empty_query_is_rejected`、`test_23b_punctuation_only_query_is_rejected` |
| 24 非法过滤参数 | `test_24_invalid_filters_are_rejected` |
| 25 特殊字符安全 | `test_25_hostile_query_characters_do_not_break_search`、`test_like_metacharacters_are_escaped_not_wildcards` |
| 26 SQLite/FTS 错误翻译 | `test_missing_index_raises_retrieval_error_not_sqlite_error`、`test_index_definition_is_verified_by_required_columns` |

---

## 9. 实际 CLI 搜索结果（真实运行，无 LLM、无网络）

演示数据：`python scripts/phase3_demo_data.py --db data/phase3-demo.db --reset`
（3 条 Memory、2 个 Source、3 条关联，索引 3/3）

```
> python -m personal_memory search "RAG" --db data/phase3-demo.db
matches : total=1  mode=index  index=1 like_only=0  took=4.119ms
1. RAG 的基本原理   [knowledge]   score=0.8162 (bm25)
   matched   : ['title', 'content', 'tags']
   sources (2):
     - src_demo_rag_intro  [text]  《RAG 入门》  url=None
     - src_demo_chat  [text]  某次聊天记录  url=None

> python -m personal_memory search "长期记忆" --db data/phase3-demo.db
matches : total=1  mode=index  index=1  took=2.907ms
1. Agent Memory   [knowledge]   score=0.5281 (bm25)
   matched   : ['content']
   sources (1): - src_demo_chat  [text]  某次聊天记录

> python -m personal_memory search "记忆" --db data/phase3-demo.db      # 2 字中文 → LIKE 回退
matches : total=1  mode=like  index=0 like_only=1  took=3.393ms
1. Agent Memory   [knowledge]   score=1.0000 (like)

> python -m personal_memory search "SQLite" --db data/memory.db          # 已有阶段 1/2 数据
matches : total=2  mode=index  took=2.891ms
1. SQLite 适合个人规模的本地优先存储   [knowledge]  score=0.0000 (bm25)  sources(1)
2. 启动 Personal Memory System 阶段 1  [event]      score=0.0000 (bm25)  sources(2)

> python -m personal_memory source src_demo_rag_intro --db data/phase3-demo.db
{"id": "src_demo_rag_intro", "title": "《RAG 入门》", "content": "RAG 是 Retrieval-Augmented Generation 的缩写：先检索，再生成。", ...}
```

证据文件：[phase3-search-rag.txt](phase3-search-rag.txt)、[phase3-search-chinese.txt](phase3-search-chinese.txt)、[phase3-search-short-cjk.txt](phase3-search-short-cjk.txt)、[phase3-search-rag.json](phase3-search-rag.json)、[phase3-search-existing.txt](phase3-search-existing.txt)、[phase3-info-existing.txt](phase3-info-existing.txt)。

（`SQLite` 那两条 bm25 都是 0.0：该术语出现在所有候选里，bm25 无区分度，于是由"字段优先级 → created_at"决定顺序——这正是排序设计的预期行为，也是可复现的。）

---

## 9.1 独立审查与修复（阶段 3）

独立对抗式审查（只读子代理）报出 1 个真实缺陷与 4 个健壮性缺口，均已修复并有回归测试：

| # | 发现 | 影响 | 修复 |
| --- | --- | --- | --- |
| S1 | `MemoryHit.bm25` / `.index` 恒为 `None`（排序时被丢弃） | 与文档/CLI 宣称的"保留原始 bm25"不符，无法解释分数 | `_Candidate` 携带 `bm25`/`index` 并传入 `MemoryHit`；测试断言 `score == -bm25` |
| S2 | 裸 SQL 的 `INSERT OR REPLACE` 会静默让索引漂移（REPLACE 的隐式删除默认不触发 DELETE 触发器） | 外部/脚本写入后索引与表不一致且无报错 | 每个连接 `PRAGMA recursive_triggers = ON`（隐式删除也会触发同步） |
| S3 | 索引被外部清空/篡改后无检测、无修复路径 | 检索静默返回空结果 | 新增 `index_consistency()`（漂移检测）与 `rebuild_search_index()`（显式重建/修复） |
| S4 | LIKE 回退直接匹配 `tags_json` 原文，JSON 标点也会命中（出现 `matched_fields == ()` 的命中） | 少量假阳性、结果自相矛盾 | 标签改用 `EXISTS (SELECT 1 FROM json_each(m.tags_json) WHERE value LIKE ?)` 匹配 |
| S5 | 查询中的控制字符（如 NUL）会以原始 FTS 错误（`RetrievalError`）暴露 | 错误类型不对，输入校验不完整 | 查询校验拒绝控制字符 → `ValidationError` |

审查同时确认：检索路径无 LLM/网络（把 socket/urllib 全部替换为抛异常的桩后仍能检索）、migration v2 行为正确、
查询全程参数化（sqlite trace 里用户输入从不出现在 SQL 文本中）、敌意查询不会产生 sqlite 错误或破坏表、
空查询 0 条 SQL 执行、LIKE 元字符被转义、223/229 个测试通过。

## 10. 已知限制

1. **关键词检索，不是语义检索**：没有 Embedding / 向量 / reranker / hybrid；同义改写（"检索增强" vs "RAG"）不会互相召回，除非文本里真的出现该词。
2. **中文 1–2 字查询走 LIKE 回退**：功能正确（能命中），但没有相关性排序信号，分数是字段权重和；规模很大时回退查询是全表 `LIKE`（个人规模可接受，不做提前优化）。
3. **拉丁查询是词/前缀匹配**：`retriev` 能命中 `retrieval`（前缀），但任意子串（如 `iev`）不会命中；中文 ≥3 字才是子串匹配。
4. **bm25 在"术语出现在所有候选"时退化为 0**：此时由字段优先级与时间决定顺序（已在文档与测试中明示）。
5. **候选上限**：单次检索最多收集 1000 个候选（`MAX_CANDIDATES`），超出会截断并在结果里置 `truncated=True`，此时 `total` 是"候选数上限"而不是精确总数；个人规模远达不到。
6. **不返回 Source 正文**：搜索结果只带来源元数据；读原文需要显式 `resolve_source()` / `source` 命令（这是有意的，避免结果膨胀）。
7. **索引与表的一致性依靠触发器 + 漂移检测**：正常写入（含裸 SQL 的 `INSERT OR REPLACE`，因为连接已开启 `recursive_triggers`）由触发器同步；
   人为直接改 FTS 影子表或崩溃中途会漂移，可用 `index_consistency()` 检测、`rebuild_search_index()` 修复；
   `initialize()` 只校验索引表/触发器是否存在，不会每次启动重扫全表。
8. **无分页游标/无高亮**：只提供 limit/offset 与 `matched_fields`，没有 snippet/highlight 片段。
9. **`status` 过滤在 SQL 层完成**（不依赖应用过滤），因此 `archived` 不会被"搜出来再丢掉"。
10. **CLI 副作用**：`search --db <不存在的路径>` 会像 `init/demo` 一样创建该库并以 0 条结果退出 0（与其他命令一致，但拼错路径不会报错）。

---

## 11. 当前明确未实现

Embedding、Vector DB、Semantic Search、Reranking、Hybrid Search、RAG、QA（自然语言回答）、LLM 调用（本阶段完全无模型调用）、Web UI、URL 抓取、PDF、Markdown/TXT 批量导入、Chat 文件解析、MCP、Agent Tool Calling、Knowledge Base 1.0。

本阶段只返回 **Memory + Source 关系 + score**——找到相关记忆，不负责替用户生成答案。
