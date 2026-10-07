# 阶段 4：Memory Lifecycle & Quality（记忆生命周期与质量控制）

本文件是阶段 4 的完整说明与实测证据。阶段 1–3 的能力**全部保留、未重做**：

| 阶段 | 能力 | 状态 |
| --- | --- | --- |
| 1 | Memory Model / Source / `memory_sources` / SQLite schema + migration + 校验 + CRUD | ✅ 未改 |
| 2 | Memory Formation（RawInput → LLM → 价值判断 → 结构化校验 → 原子落库） | ✅ 未改（仅新增可选的质量闸门） |
| 3 | Memory Retrieval（FTS5 + 排序 + Source 关联，不调用 LLM） | ✅ 未改 |
| 4 | **Memory Lifecycle + Memory Quality Control** | ✅ 本阶段 |

生成时间：2026-10-04；解释器：`D:\python\python.exe`（Python 3.13.2）；SQLite 3.45.3。

---

## 1. 新增/修改的文件

**新增**

| 文件 | 作用 |
| --- | --- |
| `personal_memory/lifecycle.py` | 生命周期服务：转换表、`activate_memory` / `archive_memory` / `restore_memory` / `set_status` / `update_memory` / `delete_memory` |
| `personal_memory/quality.py` | 质量控制：规范化指纹、精确重复检测、冲突检索+分类策略、`QualityPolicy` / `QualityDecision` / `QualityReport`、`LLMConflictClassifier`。**不含任何 SQL** |
| `tests/test_lifecycle.py` | 生命周期 1–16（转换、非法转换、更新、删除安全、状态统计） |
| `tests/test_quality.py` | 重复 17–20、冲突 21–24、LLM 分类器的 schema 校验与策略接线 |
| `tests/test_lifecycle_retrieval.py` | 与阶段 3 的联动 25–28（archived/pending 默认不召回、restore 后可召回、更新后立即可检索、删除后索引正确） |
| `scripts/phase4_acceptance.py` | 规格书 §二十 的五个验收场景（真实 CLI；A–C 不需要模型，D/E 走真实 LLM） |
| `scripts/phase4_cli_evidence.py` | 生命周期命令的真实 CLI 证据（18 条命令，含退出码与交叉校验） |
| `scripts/phase4_concurrency_probe.py` | 两个进程同时形成同一内容的并发探针（验证事务内去重的竞态结论） |
| `scripts/phase4_conflict_audit_probe.py` | 用裸 SQL 审计触发器证明冲突不会 UPDATE/DELETE 旧记忆 |
| `docs/PHASE4.md` | 本文件 |
| `docs/phase4-acceptance.{json,txt}` | 五场景验收的原始证据（含真实 LLM 的分类理由） |
| `docs/phase4-cli-lifecycle.{json,txt}` | 真实 CLI 运行记录 |
| `docs/phase4-concurrency.txt` | 两进程并发去重的原始输出（3 轮） |
| `docs/phase4-conflict-audit.txt` | 冲突安全性审计的原始输出 |

**修改（必要且最小）**

| 文件 | 修改 | 原因 |
| --- | --- | --- |
| `personal_memory/errors.py` | 新增 `IllegalTransitionError` | 非法状态转换需要可判定的类型化错误 |
| `personal_memory/models.py` | 新增 `coerce_enum()` 并导出 | lifecycle 与 quality 共用同一套枚举校验，避免两份实现漂移 |
| `personal_memory/prompts.py` | 新增 `memory-conflict-v1` 系统提示、`CONFLICT_RELATIONS`、`build_conflict_user_prompt` | 冲突分类只允许输出 `same/compatible/conflict/uncertain` |
| `personal_memory/store.py` | 新增 `iter_memories()`（仓库 **与** `MemoryUnitOfWork` 两个版本）、`status_counts()`、`count_links_for_memory()`；新增私有 `_iter_memories()` | 去重扫描需要流式读取；事务内复检需要同一连接的读取路径 |
| `personal_memory/extraction.py` | 新增可选 `quality=` 参数、`_plan()`、`_Persisted`；`FormationOutcome` 增加 `reused` / `quality`；`_persist()` 支持事务内去重 | 形成链路接入质量闸门；**未配置闸门时 Phase 2 行为逐字不变**（有回归测试） |
| `personal_memory/cli.py` | 新增 `archive` / `activate` / `restore` / `update` / `delete` / `pending` 命令与 `form --no-quality-check`；`version` 增加 `conflict_prompt_version` | 真实验收入口 |
| `personal_memory/__init__.py` | 导出 lifecycle/quality API，版本 → `0.4.0`（`phase-4`） | 公开 API |
| `tests/test_cli.py` | 版本断言 0.3.0 → 0.4.0、phase 断言、新增 `conflict_prompt_version` 断言 | 版本确实变了 |
| `tests/test_persistence.py` | 跨进程测试给子进程加 `PYTHONIOENCODING=utf-8`（并 `import os`） | 见 §8 说明：本机 locale 是 cp936，子进程会把中文写成 GBK，父进程按 UTF-8 解码失败。这是**测试环境的编码问题**，不是产品改动 |

**未修改**：`Source` / `Memory` / `memory_sources` 结构、migration v1 / v2、`db.py`（本阶段**没有**新增 migration，见 §5）、`retrieval.py`、`llm.py`、`demo.py`、阶段 1–3 的行为断言。

**阶段 4 收尾修复（生命周期一致性，见 §12.1）**：`models.py`（转换表唯一权威 + `allowed_transitions` / `can_transition`）、`lifecycle.py`（改为复用同一张表）、`store.py`（`update_memory` 强制转换表）、`tests/test_lifecycle.py`（+10 个仓库层守卫用例）、`tests/test_retrieval.py`（3 处 fixture 从「更新成 pending」改为「创建 pending」）、`scripts/phase4_cli_evidence.py`（新增仓库层守卫步骤、校验项改为按名字索引）。

---

## 2. Lifecycle 最终设计

### 2.1 状态语义（沿用阶段 1 的三个状态，不新增）

| 状态 | 语义 | 默认参与检索？ |
| --- | --- | --- |
| `active` | 正常长期记忆 | ✅ 是 |
| `pending` | 暂时不能当作确定事实：低置信度推断、疑似冲突、等待确认 | ❌ 否（需显式 `--status pending` / `all`） |
| `archived` | 明确不再参与正常召回的历史记忆，**数据保留** | ❌ 否 |

`deleted` **不是状态**：删除是操作（见 2.4）。

### 2.2 转换表（`ALLOWED_TRANSITIONS`，唯一事实来源）

```text
active   -> archived              archive_memory()
pending  -> active, archived      activate_memory() / archive_memory()
archived -> active                restore_memory()
```

* **落列之外的转换全部拒绝**：`active → pending`、`archived → pending` 抛 `IllegalTransitionError`，
  错误里带 `memory_id` / `from_status` / `to_status` / `allowed`。理由：`pending` 只在**创建**时由形成链路的保守策略进入，
  一条已被信任的 `active` 记忆不会被"降级"，`archived → pending` 在本模型里无意义。
* **同状态调用是幂等的 no-op**：不写库、不改 `updated_at`、报告 `changed=False`。
* **转换表只有一份，强制点有两处**：表定义在无依赖的 `personal_memory/models.py`（`ALLOWED_TRANSITIONS`），
  `lifecycle.py` 直接复用同一对象（返回的是同一个 dict 对象），`MemoryRepository.update_memory(status=...)` 也读它 ——
  也就是说**绕过生命周期服务、直接调用仓库 API 同样会被拒绝**（阶段 4 收尾修复，见 §12.1）。
* **创建 ≠ 改写**：`create_memory` 与 `MemoryUnitOfWork.create_memory` **不受转换表约束**，
  因为 Memory Formation 必须能够**创建** `pending`（低置信度推断、疑似冲突）；被禁止的是把一条已存在的 Memory
  **改写**成 `pending`。这就是"Formation 创建 pending"与"Lifecycle 更新状态"的区分方式：靠创建/改写这条分界线，
  没有新增开关，也没有第二张表。
* `restore_memory` 是 `set_status(active)` 的语义别名（归档记忆"恢复召回"）；`activate_memory` 用于把 `pending` 确认成 `active`。
* API：`MemoryLifecycle(repository)` 提供 `activate_memory` / `archive_memory` / `restore_memory` / `set_status` /
  `update_memory` / `delete_memory` / `pending` / `status_counts` / `allowed_transitions` / `describe`。
* 所有写入都走阶段 1 的 `MemoryRepository`（本模块 0 行 SQL），因此阶段 1 的校验与类型化错误全部保留。

### 2.3 更新（`update_memory`）

* 可更新：`title` / `content` / `summary` / `tags` / `importance` / `confidence` / `type` /
  `information_origin` / `status`（`id` / `created_at` / `schema_version` 不可变，未知字段直接拒绝）。
* 校验不放松：`importance=2`、`confidence=-1`、`type="invalid"`、`status="deleted"`、空 title、`tags="abc"`、
  未知字段全部抛 `ValidationError`，并且**失败后数据库内容与失败前逐字段相同**（测试断言整行 `as_dict()` 未变）。
* 带 `status` 的更新同样受转换表约束（`active → pending` 仍拒绝），而且**仓库层也强制**这一点：
  `MemoryRepository.update_memory(status=...)` 直接调用同样抛 `IllegalTransitionError`；
  同状态则忽略该字段、继续应用其它字段（仅状态且状态不变的更新连一次 `UPDATE` 都不发）。
* 更新是单条 `UPDATE`，阶段 3 的触发器自动刷新两个 FTS5 索引 → 新内容立即可检索、旧内容立即消失，无需手工重建索引。

### 2.4 删除安全（`delete_memory`）

```text
Memory 行        ❌ 删除
memory_sources   ❌ 级联删除（ON DELETE CASCADE，单事务）
Source 行        ✅ 完整保留
```

返回 `DeleteReport(memory_id, deleted, links_removed, links_remaining, sources_kept, sources_deleted)`，
其中 `sources_kept` 是删除后**重新读取数据库**确认仍存在的 Source id；`sources_intact` 为 `True`。
删除不存在的 Memory 抛 `NotFoundError`（不静默成功）。

---

## 3. Duplicate detection 如何工作

### 3.1 规范化规则（`memory-fingerprint-v1`，确定性，不涉及模型）

```text
canonicalize_text(text) = NFKC → 统一换行 → casefold → 所有空白折叠为一个空格 → strip
fingerprint = sha256( "memory-fingerprint-v1" + 0x1f + type + 0x1f + canonical(title) + 0x1f + canonical(content) )
```

* `type` 参与指纹：`knowledge` 与 `profile` 即使文字相同也是不同记忆。
* **ASCII 标点保留**：`"likes A."` 与 `"likes A"` 视为不同（保守；"几乎相同"不在 v0.1 范围内）。
* **NFKC + casefold 折叠类**（独立审查实测）：兼容字符与部分字形会被视为相同 ——
  `①`≡`1`、`㎡`≡`m2`、`Ⅻ`≡`XII`、`a，b`≡`a,b`、NBSP≡空格、`Straße`≡`STRASSE`。
  这属于「精确重复」定义的一部分（不是误判），已有 `test_nfkc_folding_class_is_documented` 固定该行为。
* `summary` / `tags` / `importance` 不参与指纹（文档化的选择）。

### 3.2 检测与拦截

| 步骤 | 行为 |
| --- | --- |
| 扫描 | `scan_duplicates()` 用 `iter_memories()` **流式**读取（分批 200，不把整表 materialise）并建立 `指纹 → memory_id` 工作集；单条预检按候选的 `type` 收窄，形成事务内的复检扫全表（一次，仍为流式） |
| 范围 | **所有状态**（含 `active`/`pending`/`archived`）：已归档内容的精确副本同样不会被重新建成第二条长期记忆 |
| 策略 | `QualityPolicy.dedupe`（默认 True）；关闭时 `check_duplicate` 直接返回"未检测"且不读表 |
| 决策 | `evaluate_candidate()` → `action="reuse"`、`status=None`、`duplicate_of=<existing id>`：**不写第二行** |
| 形成链路 | `form` 的每条候选先经闸门；被判定重复的候选不进入写入计划 |
| **写入事务内复检** | `_persist()` 在 `BEGIN IMMEDIATE` 事务内重新建立工作集，并在写入前查指纹；每写一条新记忆就把它的指纹登记进同一工作集 |
| 批量内去重 | 同一次 LLM 回答里出现两条完全相同的草稿，只有第一条落库（第二条报告为 reused） |
| 并发 | 事务已持写锁，扫描与插入之间不存在其它写者插队的窗口（TOCTOU 关闭） |
| 结果 | 全部候选都是重复时，formation 状态为 `"duplicate"`，`outcome.reused` 给出已存在记忆，`counts` 不变 |

### 3.3 明确不做

不计算相似度、不做模糊/语义合并；`"RAG 是检索增强生成"` 与 `"RAG 通过 Retrieval-Augmented Generation
为模型提供外部上下文"` 被判为**不同**记忆（`docs/PHASE4.md` §9、测试 18 有断言）。语义级判断由可选的冲突分类器处理，
并且只会得到 `pending`，不会自动合并。

---

## 4. Conflict handling 如何工作

### 4.1 数据流（LLM 只能"分类"，不能写库）

```text
新候选 Memory（MemoryDraft）
   │  title + content[:300] 作为查询
   ▼
阶段 3 关键词检索（status = active + pending，limit = related_limit）
   ├─ 没有相关记忆 → relation = "none"  → 保持原定状态（active）
   └─ 找到相关记忆
        ▼
   冲突分类器（可选；复用 llm.py + memory-conflict-v1 提示）
        ▼  严格 schema 校验（只允许 relation/reason 两个字段）
   same | compatible | conflict | uncertain
        ▼
   策略（代码，确定性）
     same / conflict / uncertain → status = pending   （保守）
     compatible                  → 保持原定状态（active）
        ▼
   MemoryRepository 写入（新记忆）；**旧记忆一个字节都不改**
```

* 没有配置分类器但检索到相关记忆时，按 `QualityPolicy.unclassified_relation`（默认 `uncertain`）→ `pending`：
  "不确定就不要激活"。该配置**只允许 `same`/`conflict`/`uncertain`**：`unclassified_relation="compatible"`
  会被 `ValidationError` 拒绝（"无法分类"绝不等于"兼容"）——这条限制是独立审查后的收紧。
* 候选记忆**不会与自己冲突**：`check_conflict()` 会自动排除候选自身的 `id`（独立审查后的修复）。
* 自定义分类器必须返回 `ConflictVerdict`；返回其它类型（例如裸 `dict`）得到的是 `QualityError`，不是 `AttributeError`。
* `QualityPolicy.conservative_status` **不允许设为 `active`**（构造时即抛 `ValidationError`）。
* `conflict_check=False` 时关系为 `unchecked`，不改变原定状态（显式关闭即"不要用质量策略改状态"）。
* 归档记忆不参与冲突检索（`related_statuses` 默认 active + pending），因为它本来就不参与正常召回。
* 独立审计：用裸 SQL 在 `memories` 上挂 `AFTER UPDATE`/`AFTER DELETE` 触发器后跑一次真实冲突形成，审计表为空、旧记忆整行未变（§8.4）。
* 分类器的输出进不了数据库：`ConflictClassifier.classify()` 只返回 `ConflictVerdict`；
  写库由 `QualityDecision` + repository 完成。测试 `test_24b_a_classifier_alone_cannot_write` 断言分类前后整库计数不变。
* 失败语义：分类答案非法（非法 relation、缺字段、多字段、空 reason、非 JSON）按阶段 2 的方式**带反馈重试**，
  达到 `max_attempts` 仍失败则抛 `ConflictClassificationError`（错误里带 `problems` 与 `attempts`）——
  此时**不会**写入任何降级结果，也不会误标为 `compatible`。

### 4.2 四个标签与状态的映射

| 分类结果 | 新记忆状态 | 旧记忆 | 说明 |
| --- | --- | --- | --- |
| `same` | `pending` | 不变 | 同义重复：既不自动合并，也不作为可信事实激活 |
| `compatible` | 保持原定状态（通常 `active`） | 不变 | 补充信息，两条并存 |
| `conflict` | `pending` | 不变 | **不删除、不覆盖、不降级旧记忆** |
| `uncertain` | `pending` | 不变 | 证据不足时宁可待确认 |

真实运行中 `deepseek-flash` 对「用户偏好 A 方案存储个人知识库」/「用户更喜欢 B 方案」给出的理由
（逐字引自 [docs/phase4-acceptance.json](phase4-acceptance.json)，`prompt_version=memory-conflict-v1`，
`attempts=1`，`latency_seconds≈1.01`）：

> 两条记忆都针对「个人知识库存储方案」这一同一对象的排他性偏好，一条说偏好 A 方案并实际使用，
> 另一条说更偏好 B 方案并打算使用，二者不能同时为真。

---

## 5. 是否新增 migration：**没有新增**（schema 仍为 v2）

阶段 4 的全部能力都能用既有结构表达，因此**不新增 v3**，理由逐条如下：

| 需要的能力 | 既有结构是否足够 |
| --- | --- |
| 状态转换 | `memories.status` 列（既有 CHECK `active/pending/archived`）+ 转换表在代码里 |
| 字段更新 | 阶段 1 `update_memory` |
| 删除安全 | `memory_sources` 的 `ON DELETE CASCADE` + `PRAGMA foreign_keys=ON` |
| 精确重复 | 规范化指纹在 `quality.py` 计算；扫描用既有 `memories` 表 |
| 冲突关系 | 新记忆的 `status=pending` 已表达"待确认"；关系与理由随 `QualityReport` 返回，不下库 |
| 转换时间 | 既有 `updated_at` |

**为什么不加"指纹列"或"冲突表"**：那会引入第二套必须与写入同步的派生状态（阶段 3 刚刚为索引漂移做过加固），
而收益只是"少一次线性扫描"。个人规模下扫描是流式、按 type 收窄、且真实并发由事务写锁覆盖。

验证（实测）：

```text
python -m personal_memory version  -> schema_version: 2, memory_schema_version: 1, phase-4
python -m personal_memory init     -> applied: [1 initial_schema, 2 memory_search_index]（applied_count = 2）
旧库升级：阶段 3 的 data/memory.db 仍为 v2，直接可读（阶段 4 未触碰 db.py）
新库初始化：applied_count = 2，与阶段 3 完全一致
```

`db.py` 在本阶段**一行未改**（证据强度见 §5.1）；`docs/schema.sql` 的内容与阶段 3 一致（见 §5.1 D）。

### 5.1 "未改动"的证据强度（如实分级）

项目没有 git 仓库，所以"某个文件没改"无法用 `git diff` 证明。这里列出**能给的证据**，并标明每条有多强。

**A. 指纹算法与标定样本。** 交付报告里的指纹是 `sha256(文件内容)[:16].upper()`。阶段 3 的交付报告记录了 6 个文件的指纹，
其中 3 个（`db.py`、`retrieval.py`、`tests/test_search_index.py`）今日仍算出与记录相同的值。这确认了算法，
也说明这 3 个文件自阶段 3 结束以来没有再被写过。**但这 3 个文件同时是"算法标定样本"，所以这一条不是"未改动"的独立证据**
（独立审查指出了这个循环性，这里如实保留）。第 4 个历史样本 `tests/test_retrieval.py` 在**阶段 4 收尾修复**中被改动过
（3 处 fixture 由"更新成 pending"改为"创建 pending"，见 §12.1），因此不再列入未改动清单。

**B. mtime（辅助证据）。** 阶段 4 修改的第一个源文件是 `errors.py`（22:38:37，本机时间）；`db.py` / `retrieval.py` 的 mtime 是
22:26:30、`llm.py` 是 22:06:21，都早于阶段 4 的任何一次写入。mtime 可被工具改写，故只是辅助。

**C. 结构证据（与 VCS 无关）。** 阶段 3 的 `data/phase3-demo.db` 的 `sqlite_master` DDL 与今天新建库的 DDL
（归一化后）逐条相同；`MIGRATIONS` 恰好 2 条（`initial_schema`、`memory_search_index`）、
`SUPPORTED_SCHEMA_VERSION = 2`、`db.py` 当前指纹 `F6EE1E73E1C747B9`。
如果阶段 4 新增过 migration，这三条不可能同时成立。另有一条同类的结构事实：转换表只有一个对象
（`models.ALLOWED_TRANSITIONS is lifecycle.ALLOWED_TRANSITIONS`，测试用 `is` 断言）。

**D. 不能被证明的（如实列出）。** `db.py` / `retrieval.py` / `llm.py` "一行未改"在无 VCS 时无法被第三方独立证明，
只能靠 A+B+C；“阶段 1–3 的 229 个测试继续通过”只有同一次会话的 `docs/test-output.txt` 作为记录
（当前 308 tests 全绿是实测的）；`docs/schema.sql` 在阶段 4 曾被重写过一次（加注释后立即还原），
还原后的指纹 `160069D9C0D8B396` / 5972 B 与本阶段开始前实测值一致，内容也与两个数据库的 DDL 一致。

阶段 4 新增/修改文件的**最终**指纹（`sha256[:16]` / 字节数；已包含独立审查修复轮与收尾修复）：

| 文件 | 指纹 | 字节 |
| --- | --- | --- |
| `personal_memory/lifecycle.py`（新） | `A9BF67FBBE7C5C60` | 11392 |
| `personal_memory/quality.py`（改） | `4C6D3EA3081E84C4` | 31125 |
| `personal_memory/extraction.py`（改） | `147D50D908EFA216` | 35894 |
| `personal_memory/models.py`（改） | `6F892D6888319836` | 24258 |
| `personal_memory/store.py`（改） | `0AC465BC62958D3D` | 37786 |
| `personal_memory/errors.py`（改） | `B62C84CBAEB39AED` | 5042 |
| `personal_memory/prompts.py`（改） | `7130F83C7E21856E` | 12869 |
| `personal_memory/cli.py`（改） | `FFC5652FB8EDFFD8` | 21056 |
| `personal_memory/__init__.py`（改） | `E99BE89255F59AA1` | 5227 |
| `personal_memory/db.py`（未改） | `F6EE1E73E1C747B9` | 23859 |
| `personal_memory/retrieval.py`（未改） | `FF35E0117691437D` | 23450 |
| `personal_memory/llm.py`（未改） | `E3D1DCA0FB619130` | 26109 |
| `tests/test_lifecycle.py`（新） | `E69637C73C264F22` | 28416 |
| `tests/test_quality.py`（新） | `855B616361903BCB` | 27884 |
| `tests/test_lifecycle_retrieval.py`（新） | `447B124FA6F2C552` | 4983 |
| `tests/test_retrieval.py`（收尾修复改 3 处 fixture） | `9606E99B3B4DB2E5` | 26305 |
| `tests/test_cli.py`（改） | `DA4C1F855F4D6269` | 10002 |
| `tests/test_persistence.py`（改） | `9613FC28B662A2BF` | 9132 |
| `tests/test_formation_atomicity.py`（改） | `6666067E3C0A9DB6` | 6955 |
| `docs/schema.sql` | `160069D9C0D8B396` | 5972 |

## 6. Retrieval 与 Lifecycle 如何联动

* 状态只存在于 `memories.status`；阶段 3 的检索过滤发生在 JOIN `memories` 时（`m.status IN (...)`），
  所以状态一变，检索结果**立即**变化，不需要重建索引。
* 内容/标题/标签的更新是 `UPDATE memories`，阶段 3 的 6 个触发器自动维护两张 FTS5 表；
  删除是 `DELETE memories`，INSERT/UPDATE/DELETE 触发器让索引行同步消失。
* 本阶段**没有**新增任何索引同步机制，也没有调用 `rebuild_search_index()` 来"修复"日常写入
  （它只在测试里作为"确实无需修复"的证明被调用一次）。

实测结果（`tests/test_lifecycle_retrieval.py` + `docs/phase4-cli-lifecycle.txt`）：

```text
active   --archive-->  archived : 默认 search total=1 → 0；--status archived → 1
archived --restore-->  active   : 默认 search → 1
pending  --activate--> active   : 默认 search 0 → 1
update  content                 : 旧关键词 total=1 → 0，新关键词 0 → 1，index_consistency.consistent = true
delete                          : active/pending/archived/all 四种 scope 全部 0；word_index=trigram_index=0；Source 仍在
```

---

## 7. 自动化测试结果

```text
cd D:\DSH-worlp\personal-memory-system
python -m unittest discover -s tests -t . -v
Ran 308 tests ... OK (skipped=1)
```

* 阶段 1–3 的 229 个测试**全部继续通过**（改动只有三类：版本断言、跨进程测试的编码环境修正、
  `test_retrieval.py` 的 3 处 fixture 由"更新成 pending"改为"创建 pending"，见 §1 与 §12.1）。
* 阶段 4 新增 **79** 个测试 = 首轮 59 + 独立审查修复轮 10 + 收尾生命周期一致性修复轮 10。
  按模块单独运行核实：`tests/test_lifecycle.py` 33 + `tests/test_quality.py` 36 +
  `tests/test_lifecycle_retrieval.py` 6 = 75（三个阶段 4 模块），加上 `tests/test_cli.py` +3 与
  `tests/test_formation_atomicity.py` +1，合计 79。
* 唯一 skip 是没有凭据时被跳过的可选真实 LLM 测试（阶段 2 的 `test_llm_real.py`）。
* 新测试全部使用 Mock LLM（`tests/llm_fakes.py`），**不联网、不使用凭据**；真实 LLM 只在 §8 的验收脚本里联网。

规格书 §十五 的 28 项要求逐条对应：

| # | 要求 | 测试方法 | 结果 |
| --- | --- | --- | --- |
| 1 | active → archived | `test_lifecycle.py::LifecycleTransitionTest::test_1_active_to_archived_hides_the_memory_from_default_search` | PASS |
| 2 | archived → active | `…::test_2_restore_brings_an_archived_memory_back`（+ `test_2b`） | PASS |
| 3 | pending → active | `…::test_3_pending_to_active_makes_the_memory_trusted` | PASS |
| 4 | pending → archived | `…::test_4_pending_to_archived_is_allowed` | PASS |
| 5 | 非法状态转换 | `…::test_5_transitions_to_pending_and_unknown_ids_are_refused`（+ `test_5b` no-op、`test_5c` 转换表） | PASS |
| 6 | 删除 Memory 不删除 Source | `…::test_6_deleting_a_memory_keeps_its_sources` | PASS |
| 7 | 更新 content | `MemoryUpdateTest::test_7_update_content` | PASS |
| 8 | 更新 title | `…::test_8_update_title` | PASS |
| 9 | 更新 tags | `…::test_9_update_tags` | PASS |
| 10 | 更新 importance | `…::test_10_update_importance` | PASS |
| 11 | 更新 confidence | `…::test_11_update_confidence`（+ `test_11b` summary/origin/type/status） | PASS |
| 12 | 非法更新被拒绝 | `…::test_12_invalid_updates_are_rejected_and_change_nothing`（9 种非法输入，断言整行未变） | PASS |
| 13 | 更新后 Retrieval Index 正确 | `…::test_13_update_keeps_the_search_index_consistent`（+ `13b` 标题/标签、`13c` 归档中更新） | PASS |
| 14 | 删除后 Search 找不到 | `test_lifecycle_retrieval.py::…::test_28b_delete_removes_it_from_every_search_scope` | PASS |
| 15 | Source 仍存在 | `test_lifecycle.py::…::test_6_…`（重新读取 Source 正文）+ `test_28b` | PASS |
| 16 | memory_sources 正确清理 | `test_lifecycle.py::…::test_16_deleting_a_memory_cleans_its_relation_rows` | PASS |
| 17 | 完全相同 Memory 检测到重复 | `test_quality.py::DuplicateDetectionTest::test_17_exact_duplicate_is_detected`（+ `17b`） | PASS |
| 18 | 不同 Memory 不误判 | `…::test_18_different_memories_are_not_flagged`（异 type / 异正文 / 近似改写 / 异标题） | PASS |
| 19 | 重复检测不创建第二条记录 | `…::test_19_formation_does_not_create_a_second_identical_row`（+ `19b` 单次回答内两条相同草稿、`19c` 无闸门时 Phase 2 行为不变的回归） | PASS |
| 20 | Source 不因去重被误删 | `…::test_20_sources_survive_memory_deduplication` | PASS |
| 21 | 明确冲突 → 新 Memory pending | `ConflictHandlingTest::test_21_a_conflict_becomes_pending_and_the_old_memory_stays` | PASS |
| 22 | 兼容信息不被误标 conflict | `…::test_22_compatible_information_is_not_flagged_as_a_conflict` | PASS |
| 23 | 不确定 → 不覆盖已有 Memory | `…::test_23_uncertain_never_overwrites_the_old_memory`（+ `23b` same → pending） | PASS |
| 24 | 旧 Memory 始终保留 | `…::test_24_the_old_memory_is_never_modified_by_any_relation`（4 种关系逐一断言整行未变、无写入） | PASS |
| 25 | archived 默认不返回 | `test_lifecycle_retrieval.py::…::test_25_archived_is_not_returned_by_default` | PASS |
| 26 | pending 默认不返回 | `…::test_26_pending_is_not_returned_by_default` | PASS |
| 27 | restore 后可以返回 | `…::test_27_restore_makes_it_searchable_again` | PASS |
| 28 | 更新后立即用新内容检索 | `…::test_28_update_is_visible_to_retrieval_immediately`（+ `28c` 无索引漂移） | PASS |

额外覆盖（不属于 28 项但同样重要）：`QualityPolicy` 非法配置拒绝、无分类器时的保守默认、
`exclude_ids` 自排除、pending 记忆可作为冲突对象、分类器重试后成功/失败、
`ConflictClassifierTest` 的四个标签与非法答案、`LifecycleReadTest` 的 pending 队列与状态统计。

---

## 8. 实际验收结果

### 8.1 五个验收场景（规格书 §二十）

证据：[docs/phase4-acceptance.json](phase4-acceptance.json)、[docs/phase4-acceptance.txt](phase4-acceptance.txt)。
运行方式：`python scripts/phase4_acceptance.py --reset`。该脚本驱动的是**同一个 CLI 入口**
（`cli_main()` + 捕获 stdout，见 `scripts/phase4_acceptance.py:82-92`）并对真实 SQLite 文件读写；
真正的独立子进程 CLI 证据见 §8.2 与 [docs/phase4-cli-lifecycle.txt](phase4-cli-lifecycle.txt)。D/E 使用真实 LLM。

| 场景 | 期望 | 实测 | 结果 |
| --- | --- | --- | --- |
| A 归档 | archive 后默认 search 找不到 | `active -> archived`，默认 `search "检索外部知识"` total=1 → **0**，`--status archived` → 1 | ✅ |
| B 恢复 | restore 后 search 重新找到 | `archived -> active`，默认 search → **1**（同一 memory id） | ✅ |
| C 修改 | 旧关键词不命中、新关键词命中 | `update` 后旧关键词 total=1 → **0**，新关键词（`追溯到原文出处`）→ **1** | ✅ |
| D 重复 | 不能产生第二条完全相同的长期 Memory | 第二次 `form` 相同输入：模型**改写了措辞** → 分类器给 `same` → 新记忆 **pending**（未激活）；全库指纹检查 `duplicated = {}`（5 条记忆 5 个唯一指纹）；确定性精确重复检测（构造与已存记忆逐字相同的候选）→ `action="reuse"`、`duplicate_of=<已有 id>`、`counts` 不变 | ✅ |
| E 潜在冲突 | 旧 A 保留、新 B 不能直接激活 | 真实分类器返回 **`conflict`**；B 落库为 **pending**；A 整行 `as_dict()` 未变；A 仍可被默认检索到 | ✅ |

场景 D 的诚实说明：真实模型在两次相同输入下**改写**了标题/正文，所以"确定性精确重复"路径没有被真实模型触发；
作为补偿，同一次验收里增加了 `exact_duplicate_probe`（用已落库记忆的 type/title/content 逐字构造候选），
它确定性地证明"完全相同的 Memory 会被 reuse，不会写入第二行"。真实 LLM 走到的路径（`same` → `pending`）
同样是保守结果，并且额外证明了模型不能把近似重复升级成第二条 active 记忆。

### 8.2 真实 CLI 证据

证据：[docs/phase4-cli-lifecycle.txt](phase4-cli-lifecycle.txt)（含 19 条命令、退出码与逐条交叉校验；其中一条直接调用仓库 API 验证转换表，见下）。
运行方式：`python scripts/phase4_cli_evidence.py --reset`。全部子进程均为真实 `python -m personal_memory ...`。

交叉校验结果（`checks` 里 11 个布尔项全为 true，另外 3 项是记录值 `counts`/`status_counts`/`schema_version`）：

```json
{
  "search_missed_after_archive": true,
  "archived_visible_when_asked": true,
  "restored_searchable": true,
  "old_keyword_gone": true,
  "new_keyword_hits": true,
  "illegal_transition_exit_3": true,
  "invalid_update_exit_3": true,
  "repository_refuses_active_to_pending": true,
  "deleted_memory_unsearchable": true,
  "source_survives_memory_delete": true,
  "index_consistent": true,
  "counts": {"sources": 1, "memories": 1, "memory_sources": 1},
  "status_counts": {"active": 1, "pending": 0, "archived": 0},
  "schema_version": 2
}
```

CLI 片段（真实输出）：

```text
$ python -m personal_memory --db data\phase4-cli.db archive mem_be3010...
change  : active -> archived   changed=True
reason  : active -> archived

$ python -m personal_memory --db data\phase4-cli.db search zebra
matches : total=0  mode=none  index=0 like_only=0
(no memory matched)

$ python -m personal_memory --db data\phase4-cli.db update mem_be3010... --status pending
[exit 3] "error_type": "IllegalTransitionError"  illegal Memory status transition: active -> pending; allowed from active: ['archived']

$ python -c <repository guard probe> data\phase4-cli.db            # 绕过 lifecycle，直接调仓库 API
{"error": "IllegalTransitionError", "from_status": "active", "to_status": "pending",
 "allowed": ["archived"], "row_unchanged": true, "status_after": "active"}

$ python -m personal_memory --db data\phase4-cli.db update mem_be3010... --importance 2
[exit 3] {"error": "Memory validation failed -> importance: must be within [0.0, 1.0], got 2.0",
         "error_type": "ValidationError", "db_path": "data\\phase4-cli.db"}

$ python -m personal_memory --db data\phase4-cli.db delete mem_... --json
"links_removed": 1, "links_remaining": 0, "sources_kept": ["src_25a9..."], "sources_intact": true

$ python -m personal_memory --db data\phase4-cli.db source src_25a9...
{"id": "src_25a9...", "title": "《RAG 入门》", "content": "RAG 是 Retrieval-Augmented Generation 的缩写：先检索，再生成。"}
```

### 8.3 并发（两进程）实测

`python scripts/phase4_concurrency_probe.py --rounds 3`：两个**独立进程**同时对同一内容执行 `form`
（第二个故意晚 50ms 启动），3/3 轮结果一致 —— 一个进程写入，另一个报告重复：

```text
round 0..2（每轮的 memory id 见证据文件，此处不引用易变的 id）：
  进程1  status=persisted  memories=1  reused=[]
  进程2  status=duplicate  memories=0  reused=[<进程1 写入的 memory_id>]
  最终库 memories=1, word_index=1, trigram_index=1, orphan_word_rows=0, orphan_trigram_rows=0,
        consistent=true
verdict: PASS (3 rounds)
```

原始输出见 [docs/phase4-concurrency.txt](phase4-concurrency.txt)（含每轮真实 id 与完整 JSON）。这说明文档里
"事务内复检 + `BEGIN IMMEDIATE` 写锁"的结论不只是同进程推断：真正的第二个进程会阻塞在写锁上，
拿到锁后重新扫描，看到已提交的那一行并报告 `duplicate`。独立审查代理也用 4 线程 + 3 进程复现了同一结论。

### 8.4 冲突安全性的独立审计（原始 SQL 触发器）

`python scripts/phase4_conflict_audit_probe.py`：先用**裸 SQL** 在 `memories` 上建 `AFTER UPDATE` / `AFTER DELETE`
审计触发器，再让一条被分类为 `conflict` 的候选走完整的质量闸门 + 仓库写入路径，然后检查审计表——它必须是空的：

```json
{ "old_memory_id": "mem_6d525b9de1904005b73a30bf72c4012c", "new_memory_id": "mem_f6a15d9e515d466aa5ba1b9405317e7c",
  "outcome_status": "persisted", "relation": "conflict", "new_memory_status": "pending",
  "old_memory_unchanged": true, "update_or_delete_statements_on_memories": [],
  "memories_after": 2, "index_consistent": true, "ok": true }
```

原始输出见 [docs/phase4-conflict-audit.txt](phase4-conflict-audit.txt)。这条证据不依赖包自身的账本：
只要任何路径 UPDATE 或 DELETE 了旧记忆，触发器都会记录（含自增主键 `id`）。

### 8.5 真实 LLM 使用量（最小 smoke test）

`phase4_acceptance.py` 的场景 D 与 E 各自：2 次 formation 调用 +（存在相关记忆时）1 次冲突分类调用 ≈ 共 **6 次**请求，
模型 `deepseek-flash`（`https://api.deepseek.com`），prompt `memory-formation-v1` + `memory-conflict-v1`。

请求计数来自脚本结构（D/E 各 3 次）；**证据文件本身只为 2 次分类调用保存了 `usage`/`request_id`**，
formation 调用的用量没有落盘，所以"≈6 次"是推算而不是凭证级证据。
API Key 从 `C:\Users\Pharachute\.dsh\.credentials.yaml` 的 `refs.DEEPSEEK_API_KEY` 读入**运行验收脚本的父进程环境**，
**从未打印、从未写入任何证据文件**（脚本保留"密钥若进入 payload 则拒绝写文件"的断言）。

---

## 9. 已知限制

1. **精确重复 ≠ 语义重复**：规范化只覆盖 NFKC/casefold/空白折叠；**ASCII 标点差异、语序差异、
   同义改写都算不同记忆**（`"likes A."` ≠ `"likes A"`）。反过来，NFKC 折叠会让 `①`≡`1`、`㎡`≡`m2`、NBSP≡空格、
   `Straße`≡`STRASSE`（§3.1）。语义等同交给可选的 LLM 分类器，而且结果只会是 `pending`（不自动合并、不删除）。
2. **重复检测是应用层保证，不是数据库唯一约束**：`fingerprints` 没有列、没有唯一索引。
   绕过本包（裸 SQL、其它进程直接写库）仍可能写入精确重复；形成链路内部则由"事务内复检 + 写锁"覆盖。
   代价是每次形成时**线性扫描**（流式、分批 200）：单条预检按候选 `type` 收窄，形成事务内的复检扫全表一次；
   个人规模可接受，但没有索引加速。
3. **冲突判断依赖模型**：没有配置分类器时，只要检索到相关记忆就会按 `uncertain → pending` 保守处理
   （可能出现"过度 pending"）；配置了分类器时，结论质量取决于模型，代码只保证"绝不会自动覆盖旧记忆"。
4. **冲突不落库为关系**：`QualityReport` 只在调用返回中存在，数据库里只留 `status=pending`。
   若将来需要"待确认队列 + 冲突对象"持久化，需要新增迁移（本阶段刻意不做）。
5. **`pending` 不会自动过期**：没有 TTL、没有自动复查、没有自动遗忘/衰减；需要人工 `activate` 或 `archive`。
6. **归档不参与冲突检索**：`related_statuses` 默认 active + pending。若一条记忆被归档后又出现相反信息，
   新信息会按普通新记忆处理（不会因归档旧记忆而 pending）。
7. **`delete_memory` 是物理删除**：没有软删除、没有回收站、没有撤销；`DeleteReport` 只是运行后的证据。
8. **状态转换的强制范围（收尾修复后）**：`active → pending` / `archived → pending` 在**两处**被拒绝 ——
   生命周期服务与 CLI，以及 `MemoryRepository.update_memory(status=...)`（见 §12.1）。
   仍不设防的只有：`create_memory` / `MemoryUnitOfWork.create_memory`（Formation 的合法用法，刻意保留）
   与绕过本包的裸 SQL 写入。
9. **并发**：同一文件的并发写靠 SQLite 事务（`BEGIN IMMEDIATE` + `busy_timeout=5s`）串行化；
   两个进程同时 `form` 同一内容时，第二个会在事务内看到第一个的提交并按重复处理（§8.3 实测 3/3 轮）；
   但**没有**分布式锁、跨文件协调或"多写者队列"，高并发下表现为等待而非并行。
10. **`search --db <拼错路径>` 仍会创建空库并退出 0**（阶段 3 既有行为，未改）。
11. **孤立代理项（lone surrogate）**：写入/形成/更新路径已在修复轮改为 `ValidationError`/`ExtractionValidationError`
   （不再让 sqlite3 抛原始 `UnicodeEncodeError`）；但阶段 3 的 `search` 查询参数**没有**加这道检查，
   程序化传入含孤立代理项的字符串仍会在 FTS 绑定处抛原始 `UnicodeEncodeError`
   （CLI 无法构造这种字符串）。修 `retrieval.py` 会破坏"阶段 3 文件未改"的证据，因此选择记录而不是改动。
12. **自定义分类器**：返回非 `ConflictVerdict` 会得到 `QualityError`；`ConflictClassifier` 是 Protocol，
   不做运行时类型强制（鸭子类型仍可能绕过）。

---

## 10. Memory System v0.1 的完整能力边界（阶段 1–4）

### 10.1 已实现（全部有自动化测试 + 实测证据）

| 层 | 能力 |
| --- | --- |
| 模型 | Source / Memory（四 type 共用结构）/ `memory_sources` 多对多；dataclass 校验 + SQLite CHECK 双保险；`content_hash` 去重 |
| 存储 | SQLite 单文件、`schema_migrations`、幂等 `initialize()`、结构守卫（缺表/缺列/缺触发器/库版本过高）、`foreign_keys=ON`、原子事务 |
| 形成 | 版本化 prompt + LLM 适配层（配置优先级、脱敏、不跟重定向、传输重试、严格 JSON）→ 价值判断 → 严格 schema 校验 → 策略（条数/推断置信度/Source 必要性）→ 原子落库；低价值输入零写入 |
| 检索 | FTS5 双索引（unicode61 词/前缀 + trigram 中文）+ 1–2 字中文 LIKE 回退；确定性排序；limit/offset；type/status 过滤；Source 关联返回；索引漂移检测与重建；**不调用 LLM、不需要网络/密钥** |
| 生命周期 | `pending/active/archived` 状态语义与转换表；`activate` / `archive` / `restore` / `set_status`；验证过的字段更新；安全删除（Memory + 关联删除、Source 保留）；pending 复查队列 |
| 质量 | 确定性精确重复检测（形成链路事务内拦截 + 批量内去重）；冲突保守策略（检索关联 → LLM 四分类 → 严格校验 → pending/active）；旧记忆永不被自动修改 |
| 接口 | CLI：`init / migrate / info / demo / form / search / source / archive / activate / restore / update / delete / pending / version`；包级 Python API；JSON 输出；退出码 0/2/3 |

### 10.2 明确未实现（v0.1 不做）

Embedding、向量数据库、语义检索（Semantic Search）、Reranker、Hybrid Search、RAG、问答（QA）、
Web UI、URL 抓取、PDF 解析、Markdown/TXT 批量导入、Chat 文件导入、MCP、Agent Tool Calling、
主动聊天、Knowledge Base 1.0；以及本阶段明确排除的：自动遗忘、自动衰减、复杂个人画像演化、行为预测、
记忆图谱、语义合并、自动重写整条 Memory、"智能语义去重"。

`retrieval.py` 内没有任何 provider/LLM 代码；冲突分类是**唯一的**新增模型调用点，且只输出四类标签。

---

## 11. 阶段完成条件对照（规格书 §二十一）

| 类别 | 条件 | 证据 | 结果 |
| --- | --- | --- | --- |
| 生命周期 | 状态语义明确 | 本文件 §2.1、`lifecycle.py` 文档字符串 | ✅ |
| | 状态转换正确 | 转换表 + 测试 1–5c + CLI `archive/restore/activate` + 仓库层守卫 10 例（`RepositoryTransitionGuardTest`，`active/archived → pending` 被拒且整行不变） | ✅ |
| | Archive / Restore 正常 | 场景 A/B、测试 1/2/27 | ✅ |
| | Delete 安全 | 测试 6/16/28b、`DeleteReport.sources_intact`、[docs/phase4-cli-lifecycle.txt](phase4-cli-lifecycle.txt) 的 `delete --json`（links_removed=1、sources_kept 非空） | ✅ |
| | Update 正常 | 测试 7–13c、CLI `update` | ✅ |
| 质量 | Exact duplicate detection | 测试 17/18/19/19b + 验收 `exact_duplicate_probe` | ✅ |
| | Conflict conservative handling | 测试 21–24 + 真实分类器 `conflict → pending` | ✅ |
| | 不自动覆盖冲突 Memory | 测试 24（4 种关系断言旧行未变）、`test_24b`（分类器无写权限） | ✅ |
| | Pending 不污染普通 Retrieval | 测试 26、`test_21`（pending 的冲突候选不被默认召回，旧 active 记忆仍可检索） | ✅ |
| 一致性 | 更新后索引正确 | 测试 13/13b/28、`index_consistency=true` | ✅ |
| | 删除后索引正确 | 测试 28b/28c | ✅ |
| | Source 不被误伤 | 测试 6/15/16/20 | ✅ |
| | 事务失败不留半成品 | 阶段 2 的 3 个注入测试继续通过，并新增 1 个**开着质量闸门**的注入测试（`test_failure_on_the_second_memory_with_the_quality_gate_rolls_back_too`） | ✅ |
| 测试 | 旧测试全通过 | `Ran 308 tests ... OK (skipped=1)`（298 = 收尾修复前，229 为阶段 1–3） | ✅ |
| | 新增 Lifecycle/Quality 测试全通过 | 79 个新测试（首轮 59 + 审查修复轮 10 + 收尾修复轮 10） | ✅ |
| | 至少一次真实 CLI 场景验证 | `docs/phase4-cli-lifecycle.txt`（19 条命令，含仓库层守卫）+ 五场景验收 | ✅ |
| | 真实 LLM 最小 smoke test | 场景 D/E；分类调用有 `usage`/`request_id` 证据，总量为脚本推算（见 §8.5） | ✅ |

**停止点**：阶段 4 完成后即停止，不继续开发 Knowledge Base 或任何 §十九 列出的禁止项。
Memory System v0.1 冻结，等待人工验收。

---

## 12. 独立对抗式审查与修复记录（阶段 4）

阶段 4 完成后，用一个**只读**的独立审查代理（不同上下文、不看实现过程）对代码与文档做了对抗式验证：
它在 `%TEMP%` 里用哈希固定的副本跑探针，不接触本目录，也没有联网。结论（原文照录要点）：

* **代码声明 9/10 PASS**：转换表与 no-op、更新校验与失败不变性、删除安全（含用 abort 触发器验证原子性）、
  精确重复（批量内、写窗口内、4 线程 + 3 进程）、冲突不写库与四标签映射、无新增 migration / Phase 2 路径不变、
  检索联动（含原始 SQL 复核 FTS 行内容为新内容）、`quality.py`/`lifecycle.py` 零 SQL（含运行时 caller 追踪）、
  测试与证据一致性（并对 5 处代码做突变，全部被测试捕获）。
* **第 10 条 PARTIAL**：测试数字与 A–C 数字正确，但有若干**文档表述不实**（见下）。
* 审查报出的**真实缺口**（已在本文件对应章节修正）：
  1. `evaluate_candidate()` 评估一条已存在的 Memory 时会把它自己当成"相关记忆"；
  2. `QualityPolicy(unclassified_relation="compatible")` 被接受，削弱了保守默认；
  3. 自定义分类器返回非 `ConflictVerdict` 时会泄漏 `AttributeError`；
  4. **孤立代理项**（lone surrogate）绕过全部应用层校验，直到 sqlite3 绑定时才抛原始 `UnicodeEncodeError`
     （通过 `form` 时是 traceback 而不是退出码 3）。
* **修复（代码，均已补回归测试）**：
  * `models.py` 增 `_check_encodable()`：title/content/summary/tags/url 拒绝非 UTF-8 文本；
  * `quality.py`：`canonicalize_text()` 同样拒绝；`check_conflict()` 自动排除候选自身 `id`；
    `unclassified_relation` 只允许保守三值；分类器返回值做 `isinstance(verdict, ConflictVerdict)` 检查；
  * `extraction.py`：闸门期的 `ValidationError` 统一包装为阶段 2 的 `ExtractionValidationError`。
* **修复（文档）**：删除两处"原文引用"式的不实引文（CLI 输出、分类器理由）并换成逐字实测文本；
  §5.1 改为证据强度分级（明确标定样本的循环性、mtime 的局限、无法证明的清单）；
  §8.1 说明验收脚本是**同进程调用 CLI 入口**而不是真实子进程；"按 type 扫描"改为精确描述；
  §9.8 补记"阶段 1 通用 CRUD 仍可直接写 pending"这一反例；§11 修正 Delete / Pending 的证据指向。
* **修复后复验**：`Ran 298 tests ... OK (skipped=1)`；重新生成全部免费证据（CLI 18 条命令、并发 3 轮、审计探针）
  并重跑真实 LLM 五场景验收（全 PASS）。
* **仍然存在、已如实记录的边界**：`search` 查询参数的孤立代理项未加检查（改 `retrieval.py` 会破坏
  "阶段 3 文件未改"的证据）；阶段 1 通用 CRUD 可绕过状态转换表；无 VCS 时"未改动"只能弱证明。

### 12.1 收尾修复：仓库层也必须遵守转换表

独立审查把"转换表只在生命周期服务/CLI 生效、通用 CRUD 仍可写 `pending`"记为限制（当时的 §9.8），随后做了一次最小收尾修复：

* **问题**：`MemoryRepository.update_memory(status=...)` 能把 `active` 直接写成 `pending`，绕过 `ALLOWED_TRANSITIONS`；
  同一张表在生命周期层被强制执行，仓库层却不一定，属于"同一规则两种结果"。
* **修复（不新增第二张表）**：转换表**只有一个定义**，放在无依赖的 `personal_memory/models.py`（`ALLOWED_TRANSITIONS`）；
  `lifecycle.py` 与 `store.py` 读同一个对象，测试用 `is` 断言防止出现副本（`test_10_there_is_exactly_one_transition_table`）。
  `update_memory` 在写库前校验目标状态：非法目标抛 `IllegalTransitionError`（带 `from_status`/`to_status`/`allowed`），
  同状态视为 no-op（不写状态；仅状态且状态不变的更新连一次 `UPDATE` 都不发，`updated_at` 不变）。
* **创建 ≠ 改写**：`create_memory` 与 `MemoryUnitOfWork.create_memory` **刻意不加门禁** ——
  Memory Formation 必须能创建 `pending` Memory（低置信度推断 / 疑似冲突）。没有新增开关、没有第二张表。
* **测试**：`tests/test_lifecycle.py::RepositoryTransitionGuardTest` 10 个用例，逐条覆盖本次要求的 10 项
  （active→pending 拒绝、archived→pending 拒绝、四条合法转换、同状态 no-op、失败后整行不变、Formation 仍能创建 pending、旧测试全通过）；
  "没有发生写入"用裸 SQL `AFTER UPDATE` 触发器证明，而不是靠时间戳推断。
  `tests/test_retrieval.py` 有 3 处 fixture 原本用 `active→pending` 更新构造 pending 状态，已改为**创建**一条 pending 记忆 ——
  这正是本次修复要禁止的非法转换，所以该 Phase 3 测试文件在收尾中有 4 行改动。
* **复验**：`Ran 308 tests ... OK (skipped=1)`；CLI 生命周期验证重跑（19 条命令、11 项布尔校验全 true），
  新增的 `repository_refuses_active_to_pending` 步骤直接调用仓库 API，实测输出：
  `{"error": "IllegalTransitionError", "from_status": "active", "to_status": "pending", "allowed": ["archived"], "row_unchanged": true, "status_after": "active"}`。
  两个免费探针（并发去重、冲突审计）也在收尾后的修订上重新跑过。

**停止点**：阶段 4 完成后即停止，不继续开发 Knowledge Base 或任何 §十九 列出的禁止项。
Memory System v0.1 冻结，等待人工验收。
