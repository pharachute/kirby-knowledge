# Personal Memory System v0.9 — 阶段 1（记忆模型 + SQLite）、阶段 2（Memory Formation）、阶段 3（Memory Retrieval）、阶段 4（Lifecycle & Quality）+ Knowledge Base 1.0（P1 Capture、P2 文件导入、P3 Chat Importer、MVP 本地 Web UI、P4 URL / Web Importer）

> 状态：**三个阶段均已完成并实测通过**（2026-10-04，package `0.3.0`，DB schema `v2`，prompt `memory-formation-v1`）
> 阶段 1：Memory Model / Source Model / 关系模型 / SQLite schema / 数据校验 / CRUD / 关联查询 / 持久化
> 阶段 2：LLM Adapter / 结构化 Extraction / Memory Value Judgment / Schema Validation / Source 选择性持久化 / 原子事务
> 阶段 3：Memory Retrieval（FTS5 双索引 + 中文回退 + 排序 + 来源关联），**不调用 LLM、不联网**
> 详见 [docs/PHASE2.md](docs/PHASE2.md)（§13）、[docs/PHASE3.md](docs/PHASE3.md)（§14）
> 三个阶段都**明确不含**：Embedding、向量库、语义检索、Reranking、RAG、问答（QA）、Web UI、URL 抓取、PDF、
> Markdown/TXT 批量导入、Chat 文件解析、MCP、Agent Tool Calling、主动聊天、Knowledge Base 1.0（见 §11）

代码位置：`D:\DSH-worlp\personal-memory-system`

---

## 1. 环境检查结论（全部为 Observed）

| 检查项 | 实际结果 |
| --- | --- |
| 工作区 | `D:\DSH-worlp`（AI Research & Agent Workbench，**不是** git 仓库，`git rev-parse` 报 `not a git repository`） |
| 现有相关代码 | 无。工作区内只有文档类产物（`_deliverables/personal-kb-init` 是一份**早期架构设计稿**，另有 `ARCHITECTURE.md`，与本阶段要求的 Source/Memory 模型不同）；未覆盖或改动任何既有文件 |
| Python | 3.13.2（`D:\python\python.exe`） |
| SQLite | 3.45.3（`sqlite3` 模块），JSON1 可用（`json_valid` / `json_type` 实测生效） |
| 已装依赖 | 无 pydantic / pytest / sqlalchemy |
| 网络 / PyPI | 不可用：`pip install pydantic pytest` 长时间挂起、`pip download` 同样挂起（无输出直至超时） |
| 结论 | 本阶段不引入任何第三方依赖，校验层用**标准库 dataclass + 显式校验**（需求允许 "Pydantic、dataclass 或其他合理方案"），测试用**标准库 unittest** |

因为工作区不是 git 仓库，本次没有提交记录可引用；改动仅限新增目录 `personal-memory-system/`。

---

## 2. 目录结构

```
personal-memory-system/
├─ README.md                     本文件
├─ pyproject.toml                打包元数据（零运行时依赖；可选 dev 依赖 pytest）
├─ .gitignore
├─ personal_memory/
│  ├─ __init__.py                公开 API 与版本
│  ├─ __main__.py                python -m personal_memory 入口
│  ├─ models.py                  Source / Memory / 枚举 / 校验层
│  ├─ errors.py                  错误层次
│  ├─ db.py                      schema DDL、连接、初始化与迁移
│  ├─ store.py                   数据访问层（CRUD + 关联 + 事务作用域）
│  ├─ demo.py                    阶段 1 最小 Demo
│  ├─ llm.py                     阶段 2：LLM 适配层（配置/传输/重试/JSON 提取/脱敏）
│  ├─ prompts.py                 阶段 2：版本化 prompt（价值判断标准 + JSON 契约）
│  ├─ extraction.py              阶段 2：Memory Formation 服务（RawInput → LLM → 校验 → 原子落库）
│  ├─ retrieval.py               阶段 3：Memory Retrieval（路由 → 索引 → 排序 → Memory + Sources）
│  ├─ lifecycle.py               阶段 4：Memory Lifecycle（转换表 / activate / archive / restore / update / delete）
│  ├─ quality.py                 阶段 4：Memory Quality（精确重复指纹 + 冲突保守策略 + LLM 分类器；无 SQL）
│  ├─ capture.py                 KB 1.0 P1：Capture 层（CaptureRequest → RawInput → Memory Formation；无 SQL、不直连 LLM）
│  ├─ importers/                 KB 1.0 P2：文件导入层（无 SQL、无模型）
│  │  ├─ __init__.py             公开 API（FileImporter / import_file / 错误类型 / Markdown 清洗）
│  │  ├─ files.py                .txt/.md 读取、校验、解码（共享读取管线）、ImportedDocument、ImportResult、错误层次
│  │  ├─ markdown.py             Markdown 标题提取 + 轻量清洗（围栏行删除、空行折叠、其余保留）
│  │  ├─ chat.py                 KB 1.0 P3：ChatMessage / ChatConversation / 角色文本与 JSON 解析 / ChatImporter
│  │  └─ web.py                  KB 1.0 P4：URL / Web Importer（SSRF 防护、正文提取、WebDocument）
│  │  └─ chat_adapters.py        KB 1.0 P3：Provider 适配器接缝（角色文本 / provider-neutral JSON + 注册表）
│  ├─ web/                       KB 1.0 MVP：本地 Web UI（标准库 HTTP + 服务端渲染 HTML；无 SQL、无业务逻辑）
│  └─ cli.py                     命令行入口（web / capture / import-file / import-chat / import-url / form / search / source / archive / activate / restore / update / delete / pending）
├─ config/llm.example.json       LLM 配置示例（真实 config/llm.json 已 gitignore）
├─ scripts/phase2_real_llm_check.py  真实 LLM 端到端验证脚本
├─ scripts/phase3_fts5_lab.py    FTS5 中英文行为实验（阶段 3 设计依据）
├─ scripts/phase3_demo_data.py   阶段 3 演示数据（3 Memory + 2 Source）
├─ scripts/phase4_acceptance.py  阶段 4 五场景验收（A–C 无需模型；D/E 走真实 LLM）
├─ scripts/phase4_cli_evidence.py 阶段 4 生命周期命令的真实 CLI 证据
├─ scripts/phase4_concurrency_probe.py 阶段 4 两进程并发去重探针
├─ scripts/phase4_conflict_audit_probe.py 阶段 4 冲突安全性审计探针（裸 SQL 触发器）
├─ scripts/kb1_capture_acceptance.py KB 1.0 P1 验收 A–D/F（真实 CLI；A/B 走真实 LLM）
├─ scripts/kb1p2_import_acceptance.py KB 1.0 P2 验收 A–E（TXT/Markdown 真实导入 + 新进程检索）
├─ scripts/kb1p3_chat_acceptance.py KB 1.0 P3 验收 A/A2/B–F（聊天真实导入、零写入、隐私、角色边界）
├─ scripts/kb1p_mvp_web_acceptance.py KB 1.0 MVP 验收：真实 `web` 子进程 + 真实 HTTP 走 §二十一 场景
├─ scripts/kb1p4_web_acceptance.py KB 1.0 P4 验收：真实公网抓取 + 真实 CLI/UI + 真实 LLM 端到端
├─ tests/
│  ├─ __init__.py  helpers.py  llm_fakes.py
│  ├─ test_source.py             ｜阶段 1｜ T1 / T8 + Source 校验与更新
│  ├─ test_memory.py             ｜阶段 1｜ T2 / T3 + 四种 type 共用结构
│  ├─ test_relations.py          ｜阶段 1｜ T4 / T5 + 关联语义
│  ├─ test_persistence.py        ｜阶段 1｜ T6 / T7 / T7b（真实第二进程）+ 迁移记录
│  ├─ test_db_constraints.py     ｜阶段 1｜ 数据库层 CHECK / UNIQUE / FK 兜底
│  ├─ test_schema_integrity.py   ｜阶段 1｜ 初始化守卫：缺表 / 缺列 / 库版本过高 / 非 SQLite 文件
│  ├─ test_cli.py                ｜阶段 1+2｜ CLI、Demo、form 命令、数据库路径解析
│  ├─ test_llm.py                ｜阶段 2｜ 配置/脱敏/线格式/重试/严格 JSON
│  ├─ test_extraction.py         ｜阶段 2｜ Mock LLM 的价值判断与 9 项验收
│  ├─ test_formation_atomicity.py｜阶段 2｜ 事务回滚与「零写入」
│  ├─ test_llm_real.py           ｜阶段 2｜ 可选真实 LLM 测试（无凭据时 skip）
│  ├─ test_retrieval.py          ｜阶段 3｜ 26 项检索行为 + search/source CLI
│  ├─ test_search_index.py       ｜阶段 3｜ migration v2 / backfill / 触发器同步 / 守卫
│  ├─ test_lifecycle.py          ｜阶段 4｜ 生命周期转换 1–6 / 更新 7–13 / 删除安全 16
│  ├─ test_quality.py            ｜阶段 4｜ 精确重复 17–20 / 冲突 21–24 / 分类器 schema 校验
│  ├─ test_lifecycle_retrieval.py｜阶段 4｜ 与阶段 3 联动 25–28（默认不召回 / restore / 更新 / 删除）
│  ├─ test_capture.py            ｜KB 1.0 P1｜ Capture 10 项要求 + 边界/架构守卫 + 可选真实 LLM 端到端
│  ├─ test_importers.py          ｜KB 1.0 P2｜ TXT 8 项 + Markdown 5 项 + 集成 7 项 + 边界/架构守卫
│  ├─ test_chat_import.py        ｜KB 1.0 P3｜ 解析 12 项 + 元数据 5 项 + 集成 13 项 + 长对话/隐私/适配器接缝
│  ├─ test_web.py                ｜KB 1.0 MVP｜ 18 项 UI 要求 + 转义/错误映射/上传消毒/隐私/架构守卫
│  └─ test_web_import.py         ｜KB 1.0 P4｜ 抓取/SSRF 防护/重定向/大小/类型/正文提取/管线集成（全离线）
├─ data/                         运行时数据：memory.db / phase2-real.db / phase3-demo.db（.gitignore）
└─ docs/                         实测证据与阶段文档
   ├─ PHASE2.md / PHASE3.md / PHASE4.md   阶段 2 / 3 / 4 完整说明
   ├─ KB1-PHASE1.md                KB 1.0 阶段 1（Capture Layer）完整说明
   ├─ kb1-capture-acceptance.{json,txt} KB 1.0 P1 验收 A–D/F 原始证据
   ├─ kb1-real-llm-capture.txt     KB 1.0 P1 可选真实 LLM 端到端测试输出
   ├─ KB1-PHASE2.md                KB 1.0 阶段 2（TXT/Markdown Importer）完整说明
   ├─ kb1p2-import-acceptance.{json,txt} KB 1.0 P2 验收 A–E 原始证据
   ├─ kb1-capture-cli-demo.txt     KB 1.0 P1 capture 命令的人类可读输出
   ├─ KB1-PHASE3.md                KB 1.0 阶段 3（Chat Importer）完整说明
   ├─ kb1p3-chat-acceptance.{json,txt} KB 1.0 P3 验收 A/A2/B–F 原始证据（不含对话正文）
   ├─ MVP-WEB-UI.md                KB 1.0 MVP 本地 Web UI 完整说明（结构/运行/调用关系/技术选择/验收/限制）
   ├─ kb1-mvp-web-acceptance.{json,txt} KB 1.0 MVP 验收原始证据（真实 web 进程 + 真实 HTTP，8 场景）
   ├─ KB1-PHASE4.md                KB 1.0 阶段 4（URL / Web Importer）完整说明
   ├─ kb1p4-web-acceptance.{json,txt} KB 1.0 P4 验收原始证据（真实公网 URL + 真实结果）
   ├─ phase4-acceptance.{json,txt}  五场景验收原始证据（含真实 LLM 分类理由）
   ├─ phase4-cli-lifecycle.{json,txt} 生命周期命令真实 CLI 记录
   ├─ phase4-concurrency.txt       两进程并发去重原始输出
   ├─ phase4-conflict-audit.txt    冲突安全性审计原始输出
   ├─ phase3-fts5-lab.{json,txt} FTS5 中英文实验原始结果
   ├─ phase3-search-*.{txt,json} 真实 CLI 检索结果（英文 / 中文 / 短中文 / 已有数据）
   ├─ phase2-real-llm.json       真实 LLM 端到端实测证据
   ├─ schema.sql / test-output.txt / cross-process.txt
   ├─ init.json / info.json / demo.json / info_fresh.json / info_after_demo.json
```

---

## 3. 数据模型

### 3.1 Source（原始输入，独立实体）

| 字段 | 类型 | 说明 | 校验 |
| --- | --- | --- | --- |
| `id` | TEXT | 主键，`src_<uuid4hex>` | 1–128 字符 `[A-Za-z0-9_.:-]`，首字符字母数字；唯一 |
| `source_type` | TEXT | `text` / `chat` / `article` / `web` / `file` | 枚举内，越界拒绝（本阶段只用 `text`） |
| `title` | TEXT | 标题 | 去空白后非空，≤512 字符 |
| `content` | TEXT | 原文 | 去空白后非空 |
| `url` | TEXT NULL | 来源链接 | `None` 或 `http(s)://...`，≤2048 |
| `content_hash` | TEXT | `sha256(normalize_content(content))`，64 位小写十六进制 | 格式校验 + **UNIQUE**（去重键） |
| `metadata` | dict | 任意 JSON 对象（DB 中为 `metadata_json`） | 必须是 Mapping、键为非空字符串、可 JSON 序列化（`allow_nan=False`） |
| `created_at` | TEXT | UTC ISO-8601，毫秒精度，`...Z` | 必须可解析 |
| `updated_at` | TEXT | 同上，更新时刷新 | 必须可解析 |

`normalize_content()`：统一 CRLF/LF、Unicode NFC、去每行行尾空白、去首尾空行——因此「仅换行/空白不同」的内容会被识别为同一个 Source。

### 3.2 Memory（四种 type 共用同一套结构）

| 字段 | 类型 | 说明 | 校验 |
| --- | --- | --- | --- |
| `id` | TEXT | 主键，`mem_<uuid4hex>` | 同 Source id 规则；唯一 |
| `type` | TEXT | `knowledge` / `experience` / `event` / `profile` | 枚举内 |
| `title` | TEXT | 标题 | 非空，≤512 |
| `content` | TEXT | 正文 | 非空 |
| `summary` | TEXT NULL | 摘要 | 可为空；空白串归一为 `NULL` |
| `tags` | list[str] | 标签（DB 中为 `tags_json` JSON 数组） | 序列、元素为非空字符串、去重、≤64 个、每个 ≤64 字符 |
| `importance` | REAL | 重要度 | `[0.0, 1.0]`；拒绝 bool / NaN / Inf / 非数字 |
| `confidence` | REAL | 置信度 | 同上 |
| `information_origin` | TEXT | `user_explicit` / `source_content` / `agent_inference` | 枚举内 |
| `status` | TEXT | `active` / `pending` / `archived`（本阶段定义，默认 `active`） | 枚举内 |
| `created_at` | TEXT | UTC ISO-8601 | 可解析 |
| `updated_at` | TEXT | UTC ISO-8601 | 可解析 |
| `schema_version` | INTEGER | 默认 = `CURRENT_SCHEMA_VERSION` = 1 | 整数且 `1 <= v <= CURRENT`（拒绝未来版本） |

**关键约束（实测）**：四种 type 或三种 information_origin 只作为**列值**存在，不存在四套独立模型——`tests/test_memory.py::test_all_four_types_share_one_structure` 与 `test_all_information_origins_and_statuses_are_accepted` 验证四种 type × 三种 origin × 三种 status 全部写入同一张 `memories` 表并可读回。

### 3.3 校验层如何工作

* 校验在 dataclass 的 `__post_init__` 内执行，**非法实例根本无法构造**；`Memory.create(...)` / `Source.create(...)` 会自动补 id、时间戳与 content_hash。
* 一次校验收集**全部**问题再抛出 `ValidationError`，异常带 `.problems = ((field, message), ...)` 与 `.fields`，便于定位（示例见 `tests/test_memory.py::test_validation_error_reports_every_offending_field`）。
* 错误层次（`errors.py`）：`MemorySystemError` → `ValidationError` / `NotFoundError` / `ConflictError` → `DuplicateContentHashError` / `ReferentialIntegrityError`。
* 数据库层同样兜底：应用层校验被绕过时，SQLite 的 CHECK/UNIQUE/FK 会拒绝，`MemoryRepository` 再把 `sqlite3.IntegrityError` 翻译回上述类型（`test_db_constraints.py::RepositoryIntegrityTranslationTest`）。

---

## 4. Source ↔ Memory 关系模型

* Source 与 Memory 是**两个独立实体**，各自可以被独立创建、读取、更新（Memory 还可删除）。
* 关系使用**独立关联表** `memory_sources(memory_id, source_id, created_at)`，复合主键 `(memory_id, source_id)`，两个外键 `ON DELETE CASCADE`；**没有**任何逗号拼接字符串来模拟关联。
* 支持一个 Source → 多个 Memory（Test 4）与一个 Memory → 多个 Source（Test 5），双向均可查询。
* 语义：删除 Memory 只级联删除关联行，Source 原样保留（Test 6）；删除 Source 同理不影响 Memory。
* 重复关联幂等：`link()` 返回 `True` 表示新建、`False` 表示已存在；重复插入由复合主键拒绝。

---

## 5. 数据库结构

实际 DDL 已从 `sqlite_master` 导出至 [`docs/schema.sql`](docs/schema.sql)（SQLite 3.45.3）。核心：

```sql
CREATE TABLE sources (
    id            TEXT PRIMARY KEY,
    source_type   TEXT NOT NULL CHECK (source_type IN ('text','chat','article','web','file')),
    title         TEXT NOT NULL CHECK (length(trim(title)) > 0),
    content       TEXT NOT NULL CHECK (length(trim(content)) > 0),
    url           TEXT CHECK (url IS NULL OR url LIKE 'http%'),
    content_hash  TEXT NOT NULL UNIQUE CHECK (length(content_hash) = 64 AND content_hash = lower(content_hash)),
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
    created_at    TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE memories (
    id                 TEXT PRIMARY KEY,
    type               TEXT NOT NULL CHECK (type IN ('knowledge','experience','event','profile')),
    title              TEXT NOT NULL CHECK (length(trim(title)) > 0),
    content            TEXT NOT NULL CHECK (length(trim(content)) > 0),
    summary            TEXT,
    tags_json          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags_json) AND json_type(tags_json) = 'array'),
    importance         REAL NOT NULL DEFAULT 0.5 CHECK (typeof(importance) IN ('integer','real') AND importance BETWEEN 0.0 AND 1.0),
    confidence         REAL NOT NULL DEFAULT 0.5 CHECK (typeof(confidence) IN ('integer','real') AND confidence BETWEEN 0.0 AND 1.0),
    information_origin TEXT NOT NULL CHECK (information_origin IN ('user_explicit','source_content','agent_inference')),
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','pending','archived')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    schema_version     INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
);

CREATE TABLE memory_sources (
    memory_id  TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    source_id  TEXT NOT NULL REFERENCES sources(id)  ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, source_id)
);

CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
```

索引（实际存在于库中）：`idx_sources_type_created`、`idx_sources_created_at`、`idx_memories_type_status`、`idx_memories_status_created`、`idx_memories_created_at`、`idx_memories_importance`、`idx_memory_sources_source`，加上 `sources.content_hash` 的 UNIQUE 隐式索引。

连接层每次 `PRAGMA foreign_keys = ON`（否则 SQLite 会静默忽略外键，`test_db_constraints.py::ForeignKeyPragmaTest` 实测为 1），并对每个连接设置 `busy_timeout`，库文件使用 WAL 模式。

**初始化与迁移**：`personal_memory/db.py` 中的 `MIGRATIONS` 是有序的 `Migration(version, name, statements, tables, triggers)` 列表，当前为 **v1 `initial_schema`（阶段 1 的四张表） + v2 `memory_search_index`（阶段 3 的两个 FTS5 索引 + backfill + 6 个同步触发器）**，`SUPPORTED_SCHEMA_VERSION = 2`；`Database.initialize()` 建库、写 `schema_migrations`、按版本增量应用**未执行**的迁移，**幂等**（重复调用 `applied_count = 0`）。已应用的迁移不修改，后续 schema 变更通过追加新迁移完成；`Database.applied_migrations()` / `schema_version()` 可随时查询状态。迁移在单个事务中执行，失败整体回滚。

`initialize()` 不盲信 `schema_migrations`，另做三项校验（独立审查发现的缺陷，已修复并有回归测试 `tests/test_schema_integrity.py`）：

1. **库版本高于代码** → 抛 `SchemaError`（提示升级代码而不是降级数据），而不是静默什么都不做。
2. **记录与实际表不一致** → 抛 `SchemaError`：`schema_migrations` 声称已应用某版本、但该版本负责的**表缺失或必需列缺失**（文件被截断、结构不对、手工造或来自其它工具）时立即报错，而不是等到第一次查询/写入才抛原始 `sqlite3.OperationalError: no such table / no such column`。
3. **文件根本不是 SQLite 库** → 抛 `SchemaError`（`not a usable SQLite database`），而不是原始 `sqlite3.DatabaseError`。

同样的校验由 `Database.verify_ready()` 提供，`MemoryRepository(Database(path), initialize=False)` 会调用它，因此"跳过初始化"不等于"跳过结构校验"。

0 字节的残留文件会被正常迁移（`applied_count = 1`）。守卫检查的是表与列**是否存在**，不校验列类型或 CHECK 表达式本身（见 §12.1 的边界说明）。

---

## 6. 基础操作（`MemoryRepository`）

| 操作 | 方法 |
| --- | --- |
| 创建 Source | `create_source(source)`（hash 撞库抛 `DuplicateContentHashError`，携带 `existing_source_id`） |
| 读取 Source | `get_source(id)` / `require_source(id)` / `find_source_by_content_hash(h)` / `source_exists_by_hash(h)` / `list_sources(limit, offset)` |
| 更新 Source | `update_source(id, source_type=…, title=…, content=…, url=…, metadata=…)`（改 content 自动重算 hash；`updated_at` 刷新；`metadata=None` 清空；未知/不可变字段或非 mapping metadata 拒绝） |
| 删除 Source | `delete_source(id)`（关联行级联，Memory 保留） |
| 创建 Memory | `create_memory(memory)` |
| 读取 Memory | `get_memory(id)` / `require_memory(id)` / `list_memories(memory_type=…, status=…, limit, offset)`（非法过滤值抛 `ValidationError`） |
| 更新 Memory | `update_memory(id, type/title/content/summary/tags/importance/confidence/information_origin/status)`（`tags=None` 清空；非序列 tags 拒绝而不是拆成字符） |
| 删除 Memory | `delete_memory(id)`（真删；关联行级联；Source 不动） |
| 建立关联 | `link(memory_id, source_id)`（幂等）/ `link_many(memory_id, source_ids)`（先校验全部 id，整批原子）/ `unlink(…)` / `is_linked(…)` |
| 查某 Memory 的 Sources | `get_sources_for_memory(memory_id)` |
| 查某 Source 的 Memories | `get_memories_for_source(source_id)` |
| 计数 | `counts()` / `link_count()` |

不存在时：`get_*` 返回 `None`，`require_*` 与关联查询抛 `NotFoundError`（不静默返回空）。

---

## 7. 如何启动与运行最小 Demo

无需安装任何依赖，Python ≥ 3.11 即可（本机 3.13.2）。在项目根目录执行：

```powershell
cd D:\DSH-worlp\personal-memory-system

# 1) 初始化数据库（幂等；默认路径 data/memory.db，可用 --db 或 $env:PERSONAL_MEMORY_DB 覆盖）
python -m personal_memory init

# 2) 查看 schema 版本、表 DDL、行数
python -m personal_memory info

# 3) 最小端到端 Demo：2 个 text Source、4 种 type 的 Memory、5 条关联，并打印双向查询结果
python -m personal_memory demo

# 4) 想从零开始 / 换个文件
python -m personal_memory demo --db D:\temp\pms.db --reset
python -m personal_memory version
```

### 阶段 2：用 LLM 形成记忆

```powershell
# 配置（二选一）：环境变量，或 config/llm.json（参考 config/llm.example.json）
$env:DEEPSEEK_API_KEY = "<你的 key>"        # 或 PERSONAL_MEMORY_LLM_API_KEY

# 原始输入 -> 价值判断 -> 结构化 Memory -> 校验 -> 落库（只输出观察到的结果，不打印密钥）
python -m personal_memory form --text "我正在系统学习 Agent，希望深入理解底层原理，而不是只会调用现成工具。"

# 只看分析、不写库；或强制/禁止保存来源原文
python -m personal_memory form --text "RAG 把检索与生成结合…" --dry-run
python -m personal_memory form --text "…" --keep-source never --agent-inference-min-confidence 0.6

# 真实 LLM 端到端验证（高价值 + 低价值两个用例，写出 docs/phase2-real-llm.json）
python scripts/phase2_real_llm_check.py --db data/phase2-real.db --reset
```

### 阶段 3：检索记忆（不需要 API Key、不需要网络）

```powershell
# 准备演示数据（3 条 Memory + 2 个 Source + 关联）
python scripts/phase3_demo_data.py --db data/phase3-demo.db --reset

# 英文/缩写查询、中文查询、2 字中文（LIKE 回退）
python -m personal_memory search "RAG" --db data/phase3-demo.db
python -m personal_memory search "长期记忆" --db data/phase3-demo.db
python -m personal_memory search "记忆" --db data/phase3-demo.db

# 过滤与分页；JSON 输出
python -m personal_memory search "记忆" --status all --limit 1 --type event --json --db data/phase3-demo.db

# 需要原文时显式读取（搜索结果只带来源元数据）
python -m personal_memory source src_demo_rag_intro --db data/phase3-demo.db
```

### 阶段 4：记忆生命周期与质量控制（不需要 API Key、不需要网络）

```powershell
# 归档 / 恢复：默认 search 立即跟着变（pending 与 archived 默认不参与召回）
python -m personal_memory archive <memory_id> --db data/memory.db
python -m personal_memory search "RAG" --db data/memory.db          # 找不到
python -m personal_memory restore <memory_id> --db data/memory.db
python -m personal_memory search "RAG" --db data/memory.db          # 重新找到

# 确认一条 pending 记忆（低置信度推断 / 疑似冲突）
python -m personal_memory pending --db data/memory.db
python -m personal_memory activate <memory_id> --db data/memory.db

# 校验过的字段更新（非法值会被拒绝，且不会写入任何东西）
python -m personal_memory update <memory_id> --title "新标题" --content "新正文" --tags "a,b" --db data/memory.db
python -m personal_memory update <memory_id> --status pending --db data/memory.db   # 退出码 3（非法转换）

# 删除：Memory 与关联删除，Source 原文保留
python -m personal_memory delete <memory_id> --db data/memory.db

# 形成时的质量控制（默认开启）：精确重复 → 复用已有记忆；冲突/不确定 → pending
python -m personal_memory form --text "用户喜欢 A 方案。" --db data/memory.db
python -m personal_memory form --text "用户更喜欢 B 方案。" --db data/memory.db     # B 落库为 pending
python -m personal_memory form --text "..." --no-quality-check --db data/memory.db  # 显式关闭质量闸门

# 五场景验收 + 真实 CLI 证据（D/E 需要 LLM 凭据）
python scripts/phase4_acceptance.py --reset
python scripts/phase4_cli_evidence.py --reset
```

### Knowledge Base 1.0 阶段 1：采集文本（Capture Layer）

```powershell
# 采集一段文本：Capture → RawInput → Memory Formation → 冻结的 Memory System
python -m personal_memory capture "RAG 是检索增强生成：先检索，再生成。" --db data/memory.db
python -m personal_memory capture "RAG 是检索增强生成：先检索，再生成。" --title "RAG 基础" --json --db data/memory.db
python -m personal_memory capture --stdin --title "粘贴的长文" --db data/memory.db      # 从 stdin 读取

# source_type 结构上接受 chat/article/web/file（无 Importer 时由调用方直接指定）
python -m personal_memory capture "网页正文摘要" --source-type web --url "https://example.com/article" --db data/memory.db

# 只分析不写库（capture 仍会调用一次模型）；关闭 Phase 4 质量闸门
python -m personal_memory capture "临时看看" --dry-run --db data/memory.db
python -m personal_memory capture "..." --no-quality-check --db data/memory.db

# 验收脚本（A/B 走真实 LLM，C/D/F 不调用模型）
python scripts/kb1_capture_acceptance.py --reset
```

低价值输入（例如"今天喝了一杯奶茶。"）会在 Formation 处得到 `worth_remembering=false`：
不形成 Memory，**也不会因为"被采集过"而长期保存 Source**（零写入）。

### Knowledge Base 1.0 阶段 2：导入 TXT / Markdown 文件

```powershell
# 导入一个文件：FileImporter -> Capture -> Memory Formation -> 冻结的 Memory System
python -m personal_memory import-file D:
otes
ag.md --db data/memory.db
python -m personal_memory import-file notes
ag.md --title "覆盖标题" --json --db data/memory.db
python -m personal_memory import-file notes
ag.txt --dry-run --db data/memory.db     # 只分析不写库
python -m personal_memory import-file notes
ag.md --max-bytes 200000 --db data/memory.db

# 验收脚本（A/B/C 走真实 LLM，D/E 不调用模型）
python scripts/kb1p2_import_acceptance.py --reset
```

支持 `.txt` / `.md` / `.markdown`；标题取"显式 `--title` > Markdown H1 > front matter `title:` > 文件名"；
`source_type` 固定为 `file`；`metadata` 记录文件名、扩展名、大小、`file_sha256`、编码，**不记录本机绝对路径**。
失败（文件不存在 / 目录 / 扩展名不支持 / 编码非 UTF-8 / 空文件 / 超过 `--max-bytes`）都是类型化错误、
退出码 3，且在打开数据库之前发生——不会留下半成品数据。

### Knowledge Base 1.0 阶段 3：导入聊天记录（Chat Importer）

```powershell
# 角色文本：整行 [User] / [Assistant]（或 ## User / ## Assistant）
python -m personal_memory import-chat notesgent-chat.txt --db data/memory.db

# provider-neutral JSON（conversation_id / title / provider / messages[role,content,timestamp]）
python -m personal_memory import-chat notes\conv.json --db data/memory.db
python -m personal_memory import-chat notes\conv.json --title "覆盖标题" --provider "my-exporter" --json --db data/memory.db

# 保留原始聊天作为 Source；只分析不写库；关闭 Phase 4 质量闸门
python -m personal_memory import-chat notes\conv.json --keep-source always --db data/memory.db
python -m personal_memory import-chat notes\conv.json --dry-run --db data/memory.db
python -m personal_memory import-chat notes\conv.json --no-quality-check --db data/memory.db

# 验收脚本（A/A2/B 走真实 LLM，C/D/E/F 不调用模型）
python scripts/kb1p3_chat_acceptance.py --reset
```

角色标记固定为 `[USER]` / `[ASSISTANT]` / `[SYSTEM]` / `[TOOL]` / `[DEVELOPER]`，一条消息一个标记，
**行内出现的 `[USER]` 字样不会被当成角色头**；未知角色整行（如 `[Moderator]`）会明确报错而不是算到用户头上。
`source_type=chat`；默认输出**不回显对话正文**（默认视图只有 role/timestamp/长度，连 60 字预览都需要显式选项）。

> **隐私**：聊天正文在真实 LLM Formation 时**会发送给当前配置的模型服务**；本地 SQLite 存储不代表本地推断。

### Knowledge Base 1.0 MVP：本地 Web UI

```powershell
# 默认只监听 127.0.0.1:8765，不自动打开浏览器
python -m personal_memory web --db data/memory.db
python -m personal_memory web --db data/memory.db --port 8765

# 其它参数：--host（默认 127.0.0.1，改成对外会打印局域网暴露警告）、
#           --config、--max-bytes、--no-quality-check、--port 0（随机端口）
```

启动后打印 URL、数据库路径、模型与隐私提示，浏览器打开即可使用四个区域
（Capture / Import / Memories / Sources / Search）完成闭环：
粘贴文本或上传 `.txt/.md/.markdown` 或聊天记录 → Formation → 浏览/检索/查看 Memory 与其 Source →
Edit / Archive / Restore / Delete。所有写操作都调用既有模块（UI 不含 SQL、不复制业务逻辑）；
文件上传由浏览器读成 base64 后落成临时文件再交给既有 importer，因此 filename/扩展名/大小/编码/hash 校验照旧生效。

### Knowledge Base 1.0 阶段 4：导入公开网页（URL / Web Importer）

```powershell
# CLI：默认不打印网页正文
python -m personal_memory import-url "https://peps.python.org/pep-0020/" --db data/memory.db
python -m personal_memory import-url "https://github.com/psf/requests" --db data/memory.db --json
python -m personal_memory import-url "https://example.com/" --db data/memory.db --dry-run
python -m personal_memory import-url "https://docs.python.org/3/library/urllib.request.html" `
       --db data/memory.db --max-bytes 2097152 --timeout 30 --no-quality-check

# Web UI：Import 页面新增「Import 网页 URL」表单（URL + 可选 Title + Import URL）
python -m personal_memory web --db data/memory.db --port 8765
```

只抓取**公开的 http(s)** 页面：拒绝 `file:` 等 scheme、URL 凭据、非法端口、localhost/回环/私有/链路本地/
保留地址与云元数据地址（含域名解析复核），重定向**逐跳重新校验**且限制跳数，带超时、大小上限与
Content-Type 白名单；动态渲染/登录/验证码/付费墙页面会明确失败而不是假装成功。
正文提取优先 `article`/`main`，过滤 `script`/`style`/导航/页脚/表单/ARIA landmark，
保留段落、列表与代码文本（含缩进），交给 Capture 的是**纯文本而不是原始 HTML**。
`gzip`/`x-gzip`/`deflate` 响应使用**流式有界解压**：超过上限立即失败，不会先物化完整解压结果
（压缩炸弹防护，见 [docs/KB1-PHASE4.md](docs/KB1-PHASE4.md) §11）；损坏/截断/未知（`br`/`zstd`/堆叠）编码
明确报 `ContentEncodingError` 而不是当作文本处理。安全边界（未实现的部分）如实写在
[docs/KB1-PHASE4.md](docs/KB1-PHASE4.md) §3。

`demo` 的实际输出（[`docs/demo.json`](docs/demo.json)）包含：
`counts = {"sources": 2, "memories": 4, "memory_sources": 5}`、
`memory_to_sources["mem_demo_event_01"] = ["src_demo_text_01", "src_demo_text_02"]`（一 Memory 多 Source）、
`source_to_memories["src_demo_text_01"] = [3 条 Memory]`（一 Source 多 Memory）、
`duplicate_hash_check.already_known = true`、`persistence_check.identical = true`。
Demo 是幂等的，重复运行不会产生重复行。

以库的方式调用：

```python
from personal_memory import Database, Memory, MemoryRepository, Source

repo = MemoryRepository(Database("data/memory.db"))
source = repo.create_source(Source.create(source_type="text", title="标题", content="正文"))
memory = repo.create_memory(Memory.create(
    type="knowledge", title="标题", content="正文", information_origin="source_content",
    tags=["demo"], importance=0.8, confidence=0.9))
repo.link(memory.id, source.id)
print([s.id for s in repo.get_sources_for_memory(memory.id)])
print([m.id for m in repo.get_memories_for_source(source.id)])
```

### 运行测试

```powershell
cd D:\DSH-worlp\personal-memory-system
python -m unittest discover -s tests -t . -v
```

---

## 8. 测试与实测结果

命令：`python -m unittest discover -s tests -t . -v`
实际结果：**Ran 475 tests … OK（skipped=2）**（2 个 skip 都是没有真实凭据时被跳过的可选真实 LLM 测试：
阶段 2 的 `test_llm_real.py` 与 KB 1.0 的 `tests/test_capture.py::RealLLMCaptureTest`）。
KB 1.0 阶段 4 新增 **37** 个测试（功能 27 个：`tests/test_web_import.py` 23 + `tests/test_cli.py` 2 +
`tests/test_web.py` 2；压缩安全审查 10 个：`CompressionTest`），全部离线：
脚本化 HTTP transport + 注入 DNS resolver + Mock LLM）；
KB 1.0 MVP 新增 **29** 个测试（`tests/test_web.py` 27 + `tests/test_cli.py` 的 2 个 web 用例），
KB 1.0 阶段 3 新增 42 个、阶段 2 新增 32 个、阶段 1 新增 27 个，阶段 1–4 的 308 个测试全部继续通过。以下为阶段 4 的小结（阶段 4 共新增 79 个测试，
含 `tests/test_capture.py` 之外的阶段 4 测试）：阶段 1 的 84、阶段 2 的 79、阶段 3 的 66 个测试全部继续通过，阶段 4 新增 79 个测试
（首轮 59 + 独立审查修复轮 10 + 收尾生命周期一致性修复轮 10）：`tests/test_lifecycle.py`（33）、
`tests/test_quality.py`（36）、`tests/test_lifecycle_retrieval.py`（6）、
`tests/test_cli.py`（+3，阶段 4 CLI 表面）、`tests/test_formation_atomicity.py`（+1，开着质量闸门的回滚注入）。
阶段 4 对旧测试文件的改动只有三类：`test_cli.py` 的版本断言（0.3.0 → 0.4.0、phase-4、conflict prompt 版本）、
`test_persistence.py` 跨进程测试给子进程加 `PYTHONIOENCODING=utf-8`（本机 locale 为 cp936，
子进程原本把中文写成 GBK 而父进程按 UTF-8 解码——测试环境编码问题，不是产品行为改动）、
以及 `test_retrieval.py` 的 3 处 fixture 从「更新成 pending」改为「创建 pending」
（收尾修复禁止 `active → pending` 更新，详见 [docs/PHASE4.md](docs/PHASE4.md) §12.1）；行为断言一律未改。

需求指定的 8 项验收全部由真实运行的测试覆盖：

| 需求测试 | 对应测试方法 | 结果 |
| --- | --- | --- |
| Test 1 创建 Source 成功 | `test_source.py::SourceCreationTest::test_1_create_source_succeeds` | PASS |
| Test 2 创建合法 Memory 成功 | `test_memory.py::MemoryCreationTest::test_2_create_valid_memory_succeeds` | PASS |
| Test 3 非法 Memory 被拒绝 | `test_memory.py::MemoryValidationTest::test_3_invalid_memory_is_rejected`（19 种非法输入） | PASS |
| Test 4 一个 Source 关联多个 Memory | `test_relations.py::RelationDirectionTest::test_4_one_source_can_be_linked_to_many_memories` | PASS |
| Test 5 一个 Memory 关联多个 Source | `test_relations.py::RelationDirectionTest::test_5_one_memory_can_be_linked_to_many_sources` | PASS |
| Test 6 删除 Memory 不误删 Source | `test_persistence.py::DeleteSemanticsTest::test_6_deleting_a_memory_does_not_delete_its_sources` | PASS |
| Test 7 重新初始化后数据仍在 | `test_persistence.py::PersistenceTest::test_7_data_survives_reinitialisation`（同进程重新初始化）<br>`test_7b_data_survives_a_real_second_process`（真实第二进程，见 [`docs/cross-process.txt`](docs/cross-process.txt)） | PASS |
| Test 8 重复 content_hash 可识别 | `test_source.py::SourceCreationTest::test_8_duplicate_content_hash_is_recognised` | PASS |

补充测试（同样全部通过）：

* **模型/校验**：Source 11 种非法输入、Memory 19 种非法输入、错误字段定位、四种 type × 三种 origin × 三种 status、边界值 0.0/1.0、tags 去重与修剪、`schema_version` 未来版本拒绝、id 规则、时间戳格式、content_hash 归一化（CRLF/行尾空白）。
* **更新语义**：Source/Memory 正常更新、改 content 重算 hash、改到与他人相同内容时抛 `DuplicateContentHashError`、非法更新被拒且**不写入**、未知/不可变字段拒绝、更新不存在的 id 抛 `NotFoundError`、`update_memory(tags=None)` 与 `update_source(metadata=None)` 语义一致（清空）。
* **列表过滤校验**：`list_memories(memory_type=…)` / `status=…` 传入非法值时抛 `ValidationError`，不再静默返回空列表；大小写与首尾空白会被正常归一化。
* **更新边界**：`update_memory(tags=None)` / `update_source(metadata=None)` 清空对应字段（语义一致）；`tags="abc"`、`metadata="ab"` 这类错误类型抛 `ValidationError`，不会静默拆成字符或抛原始 `ValueError`。
* **关联语义**：幂等 link、`link_many` 计数且整批原子（批量中任一 id 不存在则一条都不写）、unlink 只删关联、关联不存在的实体被拒、3×3 全连接矩阵、`memory_sources` 是带复合主键的真实表。
* **数据库兜底（绕过应用层直接 SQL）**：Source 8 种非法行、Memory 11 种非法行全部被 CHECK/UNIQUE 拒绝；重复 content_hash、重复 id、重复关联、指向不存在实体的关联全部被 SQLite 拒绝；删除 Memory 后关联行级联消失而 Source 保留；外键 PRAGMA 实测为 ON；应用层被 `mock` 绕过时仓库把 CHECK 失败翻译为 `ValidationError`、重复 id 翻译为 `ConflictError`。
* **迁移/持久化/schema 完整性**：迁移只记录一次、重复初始化 applied=0、表/索引实际存在于文件、自动创建缺失的父目录、0 字节残留文件可迁移、`schema_migrations` 撒谎（记录版本但表缺失）被 `SchemaError` 拦下、同名表但列结构不对被 `SchemaError` 拦下、非 SQLite 垃圾文件被 `SchemaError` 拦下、库版本高于代码被 `SchemaError` 拦下、`initialize=False` 也走同一套校验。
* **CLI/Demo**：`init` / `migrate` / `info` / `demo` / `version`、`--db` 在子命令前后都可用、`init` 幂等、Demo 幂等、`--reset` 生效、环境变量与默认路径解析。

额外的人工实测（非测试套件）：

* `python -m personal_memory init --db data/memory.db` → `applied_count = 1`（[`docs/init.json`](docs/init.json)）
* `python -m personal_memory info --db data/memory.db` → `schema_version = 1`，`counts = {sources: 2, memories: 4, memory_sources: 5}`（[`docs/info_after_demo.json`](docs/info_after_demo.json)）
* `python -m personal_memory demo --db data/memory.db` → 计数与双向查询如上（[`docs/demo.json`](docs/demo.json)）
* 从真实库导出 DDL 至 [`docs/schema.sql`](docs/schema.sql)
* 跨进程持久化：进程 A 写入后退出，另一个解释器进程 B 读回全部数据与关联（[`docs/cross-process.txt`](docs/cross-process.txt)）

---

## 9. 阶段 1 完成标准对照

| 完成标准 | 状态 | 证据 |
| --- | --- | --- |
| Source Model | 完成 | `models.py::Source` + `test_source.py` |
| Memory Model | 完成 | `models.py::Memory`（四 type 共用） + `test_memory.py` |
| Source / Memory 关系模型 | 完成 | `memory_sources` 表 + `store.py` + `test_relations.py` |
| SQLite schema | 完成 | `db.py` DDL + `docs/schema.sql` |
| 数据校验 | 完成 | dataclass 校验 + SQLite CHECK/UNIQUE/FK + 错误翻译 |
| 基础 CRUD | 完成 | `store.py`（Source CRUD、Memory CRUD、关联增删） |
| 关联查询 | 完成 | `get_sources_for_memory` / `get_memories_for_source` |
| 数据持久化 | 完成 | Test 7（同进程）+ Test 7b（真实第二进程）+ `persistence_check.identical = true` |
| 自动化测试 | 完成 | 84 tests（阶段 1）；163 tests（含阶段 2）；229 tests（含阶段 3，`docs/test-output.txt`） |

---

## 10. 本次修改/新增的文件

全部为**新增**，未修改工作区中任何既有文件：

阶段 1 新增：`README.md`、`pyproject.toml`、`.gitignore`、
`personal_memory/{__init__,__main__,models,errors,db,store,demo,cli}.py`、
`tests/{__init__,helpers,test_source,test_memory,test_relations,test_persistence,test_db_constraints,test_schema_integrity,test_cli}.py`、
`docs/{schema.sql,test-output.txt,cross-process.txt,init.json,info.json,info_fresh.json,info_after_demo.json,demo.json}`。

阶段 2 新增：`personal_memory/{llm,prompts,extraction}.py`、`scripts/phase2_real_llm_check.py`、
`config/llm.example.json`、`docs/PHASE2.md`、`docs/phase2-real-llm.json`、
`tests/{llm_fakes,test_llm,test_extraction,test_formation_atomicity,test_llm_real}.py`。

阶段 3 新增：`personal_memory/retrieval.py`、`scripts/{phase3_fts5_lab,phase3_demo_data}.py`、
`tests/{test_retrieval,test_search_index}.py`、`docs/PHASE3.md`、`docs/phase3-fts5-lab.{json,txt}`、
`docs/phase3-search-*.{txt,json}`。
阶段 4 新增：`personal_memory/{lifecycle,quality}.py`、
`scripts/{phase4_acceptance,phase4_cli_evidence,phase4_concurrency_probe,phase4_conflict_audit_probe}.py`、
`tests/{test_lifecycle,test_quality,test_lifecycle_retrieval}.py`、`docs/PHASE4.md`、
`docs/phase4-acceptance.{json,txt}`、`docs/phase4-cli-lifecycle.{json,txt}`、`docs/phase4-concurrency.txt`、`docs/phase4-conflict-audit.txt`。
阶段 4 修改（必要且最小）：`errors.py`（`IllegalTransitionError`）、`models.py`（`coerce_enum`）、
`prompts.py`（`memory-conflict-v1` 提示）、`store.py`（流式 `iter_memories` / `status_counts` / `count_links_for_memory`）、
`extraction.py`（可选质量闸门 + `reused`/`quality` 报告 + 事务内去重复检 + 闸门期校验错误包装）、
`cli.py`（`archive`/`activate`/`restore`/`update`/`delete`/`pending` 命令与 `form --no-quality-check`）、
`__init__.py`（导出 + 0.4.0）、`tests/{test_cli,test_formation_atomicity,test_persistence}.py`。
独立对抗式审查后又做了一轮最小加固（全部有回归测试）：`models.py` 拒绝无法绑定到 SQLite 的孤立代理项、
`quality.py`（`canonicalize_text` 同样拒绝、`check_conflict` 排除候选自身、`unclassified_relation` 只允许保守三值、
分类器返回值做类型检查）。
KB 1.0 阶段 4（URL / Web Importer）新增：`personal_memory/importers/web.py`、`tests/test_web_import.py`、
`scripts/kb1p4_web_acceptance.py`、`docs/KB1-PHASE4.md`、`docs/kb1p4-web-acceptance.{json,txt}`；
修改（必要且最小）：`cli.py`（`import-url` + 版本 phase）、`web/server.py`（`POST /import-url` + 错误映射）、
`web/views.py`（Import 页 URL 表单）、`importers/__init__.py`/`__init__.py`（导出 + 0.9.0）、
`tests/test_cli.py`、`tests/test_web.py`。**零新增运行时依赖**（`urllib.request` + `html.parser` + `ipaddress`）。

KB 1.0 MVP（本地 Web UI）新增：`personal_memory/web/{__init__,server,views}.py`、`tests/test_web.py`、
`scripts/kb1p_mvp_web_acceptance.py`、`docs/MVP-WEB-UI.md`、`docs/kb1-mvp-web-acceptance.{json,txt}`；
修改（必要且最小）：`cli.py`（`web` 命令 + 版本 phase）、`__init__.py`（0.8.0 + 导出 `WebContext`/`create_server`）、
`tests/test_cli.py`（版本断言 + 2 个 web 用例）。**零新增运行时依赖**（只用标准库 `http.server`/`urllib`）。
**未改**：Memory System 全部模块与 KB 导入层（指纹逐一相同）。

KB 1.0 阶段 3（Chat Importer）新增：`personal_memory/importers/{chat,chat_adapters}.py`、
`tests/test_chat_import.py`、`scripts/kb1p3_chat_acceptance.py`、`docs/KB1-PHASE3.md`、
`docs/kb1p3-chat-acceptance.{json,txt}`、`data/kb1p3-fixtures/*`（虚构聊天）；
修改（必要且最小）：`importers/files.py`（抽出共享的 `read_text_file()`/`decode_utf8()`，
供文件与聊天导入器复用同一套校验与错误）、`importers/__init__.py`（导出聊天 API）、
`cli.py`（`import-chat` 命令 + `--keep-source`）、`__init__.py`（导出 + 0.7.0）、
`tests/test_cli.py`（版本断言 + 3 个 import-chat 用例）。
**未改**：`capture.py`、`models.py`、`store.py`、`extraction.py`、`retrieval.py`、`lifecycle.py`、`quality.py`、
`llm.py`、`prompts.py`、`errors.py`、`db.py`、`docs/schema.sql` 与阶段 1–4 / KB P1/P2 的既有测试（指纹逐一相同）。
命名注意：顶层 `personal_memory.ChatMessage` 仍是 Phase 2 的 LLM 请求消息；聊天消息结构在
`personal_memory.importers` 命名空间下（向后兼容，见 docs/KB1-PHASE3.md §1）。

KB 1.0 阶段 2（TXT + Markdown Importer）新增：`personal_memory/importers/{__init__,files,markdown}.py`、
`tests/test_importers.py`、`scripts/kb1p2_import_acceptance.py`、`docs/KB1-PHASE2.md`、
`docs/kb1p2-import-acceptance.{json,txt}`、`data/kb1p2-fixtures/rag.{txt,md}`；
修改（必要且最小）：`cli.py`（`import-file` 命令 + 输出尾段抽取）、`__init__.py`（导出 + 0.6.0）、
`tests/test_cli.py`（版本断言 + 4 个 import-file 用例）。
**未改**：`capture.py`、`models.py`、`store.py`、`extraction.py`、`retrieval.py`、`lifecycle.py`、`quality.py`、
`llm.py`、`prompts.py`、`errors.py`、`db.py`、`docs/schema.sql` 与阶段 1–4 / KB P1 的既有测试（指纹逐一相同）。

KB 1.0 阶段 1（Capture Layer）新增：`personal_memory/capture.py`、`tests/test_capture.py`、
`scripts/kb1_capture_acceptance.py`、`docs/KB1-PHASE1.md`、`docs/kb1-capture-acceptance.{json,txt}`、
`docs/kb1-real-llm-capture.txt`；修改（必要且最小）：`cli.py`（`capture` 命令 + `version` 的 `memory_system` 字段）、
`__init__.py`（导出 + 0.5.0）、`tests/test_cli.py`（版本断言 + 3 个 capture 用例）。
**未改**：`models.py` / `store.py` / `extraction.py` / `retrieval.py` / `lifecycle.py` / `quality.py` / `llm.py` /
`prompts.py` / `errors.py` / `db.py` / `docs/schema.sql` 与阶段 1–4 的全部既有测试 —— 指纹逐一与阶段 4 收尾时相同。

最后又追加了一次收尾修复（生命周期一致性）：转换表唯一化到 `models.py`（`ALLOWED_TRANSITIONS`），
`lifecycle.py` 与 `store.py` 读同一个对象，`MemoryRepository.update_memory(status=…)` 现在也强制转换表；
`tests/test_lifecycle.py` 增加 10 个仓库层守卫用例，`tests/test_retrieval.py` 的 3 处 fixture 与
`scripts/phase4_cli_evidence.py`（新增仓库层守卫步骤）随之调整。
**未改**：数据模型、`memory_sources`、migration v1/v2、`db.py`、`retrieval.py`、`llm.py`；**本阶段没有新增 migration**（schema 仍为 v2）。

阶段 3 修改（必要且最小）：`db.py`（migration v2 + 触发器/列校验 + 迁移错误包装）、`store.py`（检索原语与批量加载）、
`cli.py`（`search`/`source` 命令、version 区分 DB 与行级 schema 版本）、`__init__.py`（导出 + 0.3.0）、
`tests/{test_cli,test_persistence,test_schema_integrity}.py`（版本断言 1 → 2）。**未改** migration v1、数据模型、CRUD、extraction、llm。

阶段 2 唯一的既有文件修改（最小必要）：`personal_memory/store.py` 增加 `MemoryRepository.transaction()` /
`MemoryUnitOfWork` 并把单行写入抽为私有方法（公开行为不变）；`personal_memory/__init__.py` 增加导出并把版本升到 0.2.0；
`cli.py` 增加 `form` 命令；`tests/test_cli.py` 的版本断言 0.1.0 → 0.2.0。**数据库结构与 Memory/Source 模型未改**。

运行时生成（已 gitignore）：`data/memory.db`、`data/phase2-real.db`、`config/llm.json`。

---

## 11. 尚未实现（截至 KB 1.0 阶段 4 明确不做）

以下内容**完全没有实现**，代码中不存在相关调用或依赖：

Embedding、向量数据库、语义检索（Semantic Search）、Reranking、Hybrid Search、RAG、问答（QA）、
PDF / DOCX 解析、OCR、多页面爬虫与目录遍历、登录态与 Cookie 管理、代理绕过、
批量 URL 导入、无头浏览器、站点专用适配器（GitHub API / 知乎 / 微信公众号等）、robots.txt 解析、
文件监听、自动目录扫描、MCP、Agent Tool Calling、多用户、登录、权限、云同步、知识图谱、Dashboard 图表；
Markdown → HTML 渲染；自动摘要、自动长对话压缩、自动 chunk；全平台专用适配器
（ChatGPT/DSH/Codex/Discord）、聊天监控、自动读取微信/浏览器/DSH 数据目录；检索仍然完全不调用模型。

**注意**：Knowledge Base 1.0 已完成 P1（Capture）、P2（TXT/Markdown Importer）、P3（Chat Importer）、
**MVP 本地 Web UI**、**P4 URL / Web Importer**。
现在**可以在浏览器或 CLI 里实际使用**：粘贴文本、上传 `.txt/.md/.markdown` 与聊天记录、
**导入公开 http(s) 网页**（含 GitHub README 页面）、浏览/检索/查看 Memory 与 Source、编辑与生命周期操作。
但**不能**解析 PDF/DOCX、不能抓取需要登录或 JS 渲染的页面、不做语义检索或 RAG、
没有批量 URL 导入与多页面爬虫、没有多用户/登录/云同步。

阶段 3 **已经实现**：Memory Retrieval（`retrieval.py`）、关键词检索、
SQLite FTS5 索引（migration v2）、中文检索与 1–2 字中文回退、相关性排序、limit/offset、type/status 过滤、
Source 关联返回、索引自动同步。

KB 1.0 阶段 1 **已经实现**：Capture Layer（`capture.py`：统一 `CaptureRequest` + `CaptureService` + `CaptureResult`）、
`capture` CLI、Capture → Memory Formation 接通、低价值零写入、`captured_at`/`metadata`/`source_type`/`url` 传递。

KB 1.0 阶段 2 **已经实现**：文件导入层（`importers/`：`.txt` / `.md` / `.markdown` → 统一 `CaptureRequest`）、
`import-file` CLI、UTF-8 优先 + 明确编码错误、标题提取（显式 > H1 > front matter > 文件名）、
轻量 Markdown 清洗、文件元数据（不含绝对路径）、类型化失败且零写入、复用既有 Source/Memory 去重。
**明确不做**：PDF、OCR、URL 抓取、网页解析、批量/目录扫描、KB UI。

KB 1.0 阶段 3 **已经实现**：Chat Importer（`importers/chat.py` + `chat_adapters.py`）、
角色标注与顺序保持、会话边界（conversation_id/title/provider/message_count/started_at/ended_at）、
角色文本与 provider-neutral JSON 两种格式、Provider 适配器接缝、`import-chat` CLI、
标题规则（显式 > 文件 > 首条用户消息 > Untitled，不调用模型）、超长对话明确失败、
默认输出零正文（隐私），以及完全复用既有 Source/Memory 去重。
**明确不做**：平台专用适配器、URL/PDF/OCR、自动摘要与 chunk、聊天监控。

阶段 4 **已经实现**：Memory Lifecycle（状态语义 + 转换表 + `activate`/`archive`/`restore`/`update`/`delete`）、
精确重复检测（指纹 + 形成链路事务内拦截 + 批量内去重）、冲突保守处理（关键词检索关联 → LLM 四分类 → 严格校验 → pending）、
安全删除（Memory 与关联删除、Source 保留）、pending 复查队列、CLI 生命周期命令。
本阶段**明确不做**：自动遗忘、自动衰减、复杂个人画像演化、行为预测、记忆图谱、语义合并、自动重写整条 Memory、
"智能语义去重"。

具体说明：

* 检索只有 SQLite（FTS5 + 参数化 LIKE），没有网络调用；`retrieval.py` 里没有任何 provider/LLM 代码。
* 没有语义层：同义改写不会互相召回；没有 embedding 列、没有向量、没有 rerank、没有把结果拼进 prompt。
* 没有 Web 界面；CLI 为 `init / migrate / info / demo / capture / form / search / source / archive / activate / restore / update / delete / pending / version`。
* 没有实现 Source 内容级高亮/snippet、没有分页游标。
* 记忆层只有**确定性精确去重**（规范化指纹）与保守冲突处理；没有语义去重、没有自动合并。

---

## 12. 设计决策与已知限制

1. **零依赖**：PyPI 不可用，校验层用 dataclass。若将来可用 pydantic，可替换 `models.py` 的校验实现而不改 schema 与数据访问层（`ValidationError` 契约保持不变）。
2. **`content_hash` 唯一**：同一份内容只允许一个 Source 行，重复写入抛 `DuplicateContentHashError` 并给出 `existing_source_id`，由调用方决定复用还是放弃——这使去重可判定，而不是静默产生副本。副作用：若将来需要"同一内容、多个来源渠道"，需要把唯一约束改为 `UNIQUE(content_hash, source_type)` 之类的组合键（属于后续阶段的迁移）。
3. **`updated_at` 由应用层刷新**（SQLite 无内置更新时间触发），未使用触发器；`updated_at >= created_at` 未做 CHECK，以避免用户显式传入更早的 `created_at` 时被误拒。
4. **`status` 取值由本阶段定义**（`active`/`pending`/`archived`），需求未枚举；作为 CHECK 约束固定下来，扩展需新增迁移。
5. **tags 以 JSON 数组存于 `tags_json`**（含 `json_valid` + `json_type='array'` CHECK），没有拆出 `tags` 表——本阶段无检索需求；将来做标签检索时可加 `memory_tags` 关系表并保持 `Memory.tags` 接口不变。
6. **删除 Memory 是物理删除**，`memory_sources` 级联删除；Source 不受影响。无回收站/软删除。
7. **`schema_version` 写在每行 Memory 上**（=1），同时全局迁移版本记录在 `schema_migrations`；两者用途不同：前者标记"这条记忆由哪版模型写入"，后者标记"这个库已应用哪些迁移"。
8. **Demo 使用固定 id** 以保证可重复运行；真实调用方应让 `Source.create` / `Memory.create` 自动生成 id。
9. 本机沙箱限制：`tempfile.mkdtemp()` 创建的目录在当前 Windows 沙箱下既无法被 sqlite 写入也无法清理（实测 `unable to open database file` / WinError 5），因此测试用 `tests/helpers.make_temp_dir()`（普通 `mkdir`）。这是测试基础设施问题，不影响库本身。

### 12.1 数据库层「能保证 / 不能保证」的精确边界

应用层（dataclass）与数据库层（CHECK/UNIQUE/FK）**不是**完全对等，这里明确列出不对称之处，避免把"两层都校验"理解成"两层规则完全相同"。需求列出的项目全部两层都覆盖；下列差异都是需求之外的字段细节：

| 规则 | 应用层 | 数据库层 | 说明 |
| --- | --- | --- | --- |
| `type` / `source_type` / `information_origin` / `status` 枚举 | 拒绝 | 拒绝（CHECK IN） | 两层一致 |
| `importance` / `confidence` 范围 | 拒绝 | 拒绝（CHECK 0..1 + `typeof`） | 数值越界一致；但纯数字**字符串**（如 `'0.5'`）会被 REAL 列亲和性先转成数字，DB 接受而应用层拒绝 |
| 必填字段非空 | 拒绝 | 拒绝（CHECK `length(trim(x))>0`） | 两层一致 |
| id 唯一、`content_hash` 唯一 | 拒绝（`ConflictError` / `DuplicateContentHashError`） | 拒绝（PRIMARY KEY / UNIQUE） | 两层一致；DB 是最终防线 |
| Source-Memory 关联合法 | 拒绝（`NotFoundError`） | 拒绝（FOREIGN KEY）**但依赖 `PRAGMA foreign_keys=ON`** | 见下方注意事项 |
| `tags` / `metadata` 必须是 JSON 数组 / 对象 | 拒绝 | 拒绝（`json_valid` + `json_type`） | 两层一致 |
| 时间戳**格式** | 拒绝（ISO-8601 解析） | 仅要求非空（`length>0`） | 应用层更严；DB 会接受 `'garbage'`，只拒绝 `''` |
| `url` **语法与长度** | 拒绝（`^https?://\S+$`，≤2048） | 仅 `LIKE 'http%'`，无长度限制 | 应用层更严；`'httpfoo'` 或 3000 字符的 http URL DB 会接受 |
| `summary` | 拒绝非字符串（如 `123`） | 仅要求 TEXT 亲和（整数会被存成文本） | 应用层更严 |
| `id` **格式** | 拒绝（`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`） | 仅 PRIMARY KEY（`'bad id !!!'`、超长 id 都接受） | 应用层更严 |
| `title` / `tags` **长度上限** | 拒绝（title ≤512、tag ≤64 且 ≤64 个） | 无长度上限 | 应用层更严 |
| `schema_version` **上限** | 拒绝（> `CURRENT_SCHEMA_VERSION`） | 仅 `>= 1` | 应用层更严；未来版本号在 DB 层不被拒绝（但 `initialize()` 的版本守卫见 §5） |

**外键注意事项（重要）**：SQLite 的 `PRAGMA foreign_keys` 是**连接级**开关，且默认为 OFF。本包每次 `Database.connect()` 都显式打开它，所以通过本包写入时外键一定生效；但用其它工具（或裸 `sqlite3.connect()`）直接写同一个文件时，外键**不会**被强制，`memory_sources` 里可能留下孤儿行。这是 SQLite 的既定行为，不是本项目的缺陷——任何绕过本包的外部写入都不在保证范围内。库结构本身（`foreign_key_list`、`PRAGMA foreign_key_check`）仍然正确。

**结构守卫（`initialize()` / `verify_ready()`）的边界**：守卫检查的是**表是否存在**与**必需列是否存在**，不检查列类型、CHECK 表达式、索引是否被人为替换。因此一个"表名与列名都对、但约束被改坏"的伪造文件仍可能通过守卫。另外，绕过本包直接写入 `schema_version=99` 这类非法行后，`get_memory` / `list_memories` 读取时会抛 `ValidationError`（模型校验拒绝读出非法数据），这是有意为之：宁可报错也不静默返回坏数据。
**已知未做**（阶段 1 范围内）：`updated_at >= created_at` 的 CHECK（见 §12 第 3 条）、时间戳/URL/summary/id 长度与格式的数据库级强校验（只能靠应用层或后续迁移加触发器）、连接级并发写控制、外部工具写入的防御。

## 13. 阶段 2：Memory Formation（已完成）

完整说明见 [docs/PHASE2.md](docs/PHASE2.md)。要点：

* **链路**：`RawInput`（临时对象，不自动持久化）→ LLM Adapter → 价值判断 → 严格 schema 校验 →
  `MemoryDraft` → 阶段 1 `Memory.create()` 校验 → `FormationPolicy` → `repo.transaction()` 原子写入；
  低价值输入与全部被策略丢弃的输入**零写入**。
* **价值判断**：由版本化 prompt（`memory-formation-v1`）给出高/低价值标准与「长 ≠ 值得记、有趣 ≠ 长期有用」警告，
  模型输出 `worth_remembering` / `reason` / `memories[]`；代码侧 `FormationPolicy` 只管条数上限、
  `agent_inference` 置信度门槛与 status、以及 Source 是否必须保留——模型不越界，结构必须合法。
* **Source 选择性持久化**：无 Memory → 不保存；`requires_source=true` 或 `information_origin=source_content` → 必须保存并关联；
  自洽的用户明确表达 → 只保存 Memory。同一输入重复形成时复用已有 Source（`content_hash` 去重）。
* **失败处理**：传输失败重试（429/5xx/超时）后明确抛错；内容非法带反馈重试，仍非法则 `ExtractionValidationError`；
  写入阶段任何异常整体 `ROLLBACK`（link 失败 / 第 2 条 Memory 失败 / Source 失败均有注入测试）。
* **密钥与凭据安全**：只从环境变量或 gitignore 的配置文件读取，且**环境变量优先于配置文件**（key 轮换不会被旧配置压过）；
  `LLMConfig` 的 repr/summary/公开字典、异常信息（含 provider 回显的错误体，经 `redact_secrets`）全部脱敏；
  默认 opener **不跟随重定向**，凭据不会被转发到其它主机；真实 LLM 证据文件不含密钥（脚本内置拒写断言）。
* **可追溯性硬规则**：只要保留的记忆无法脱离原文解释（`requires_source=true`），Source 必须保存并建立关联，
  任何 `--keep-source` 模式都不能绕过（独立审查发现该漏洞后修复）；查重与插入在同一事务内完成，无 TOCTOU 窗口。
* **两轮独立对抗式审查**共报出 4 + 4 个真实缺陷（密钥优先级、错误体/模型输出回显密钥、重定向转发凭据、
  `--keep-source never` 绕过硬规则、socket 异常未包装、`URLError`/`base_url` 未脱敏、事务外查重），
  全部修复并补充回归测试；详见 [docs/PHASE2.md](docs/PHASE2.md) §10。
* **真实 LLM 实测**：[docs/phase2-real-llm.json](docs/phase2-real-llm.json)（模型 `deepseek-flash`，prompt `memory-formation-v1`）
  高价值知识输入 → 1 条 `knowledge`/`source_content` Memory + Source 关联（counts `0 → 1/1/1`，1 次尝试，1852 tokens）；
  低价值闲聊「今天下午喝了一杯奶茶。」→ 不形成 Memory、不保存 Source、库中计数不变。
* **阶段 2 完成条件**：LLM Adapter ✅ / 结构化 Extraction ✅ / Memory Value Judgment ✅ / Memory Schema Validation ✅ /
  information_origin ✅ / importance·confidence ✅ / 有价值信息形成 Memory ✅ / 低价值不形成 Memory ✅ /
  Source 选择性持久化 ✅ / Source-Memory 关联 ✅ / 事务与失败处理 ✅ / Mock LLM 自动化测试 ✅ / 真实 LLM 端到端验证 ✅

---

## 14. 阶段 3：Memory Retrieval（已完成）

完整说明见 [docs/PHASE3.md](docs/PHASE3.md)。要点：

* **数据流**：query → 校验/分词/路由 → FTS5 索引（+ 短中文 LIKE 回退）→ 合并排序 → 批量载入 Memory →
  批量载入 Sources → `RetrievalResult(memory, score, matched_fields, sources)`；全程无 LLM、无网络。
* **检索方案来自实验**（[docs/phase3-fts5-lab.json](docs/phase3-fts5-lab.json)，SQLite 3.45.3）：
  `unicode61` 对中文完全无效；`trigram` 对英文会把 `storage` 匹配成 `RAG` 且 bm25 恒为 0；
  2 字中文任何 tokenizer 都匹配不到；未加引号的 `"`/`'`/`AND` 会让 MATCH 语法报错。
  因此实现为 **双 FTS5 索引**（word=unicode61 前缀匹配拉丁；trigram 匹配中文 ≥3 字）
  **+ 1–2 字中文的 LIKE 回退 + 所有 token 强制加引号 + 全参数化 SQL**。
* **migration v2**：新增两张 FTS5 表、对已有 Memory 做 backfill、并为每张表建 3 个触发器
  （INSERT/UPDATE/DELETE 自动同步，不依赖启动时全表重扫）；重复 `initialize()` → `applied_count = 0`。
* **排序**：索引命中优先于 LIKE 回退；再按 score 降序（bm25 取负，越大越相关）；
  再按字段优先级 title > tags/summary > content；再按 created_at 降序、id 升序（完全确定性）。
* **Source 关联**：复用 `memory_sources`，命中带 `SourceRef`（id/type/title/url/created_at，不含正文）；
  需要原文用 `resolve_source()` 或 `python -m personal_memory source <id>`。
* **实测**：`search "RAG"` 命中 RAG 记忆并带 2 个来源；`search "长期记忆"` 命中 Agent Memory；
  `search "记忆"`（2 字）走 LIKE 回退；在阶段 1/2 的既有 `data/memory.db` 上 `search "SQLite"` 命中 2 条。
* **阶段 3 完成条件**：检索服务 ✅ / 关键词检索 ✅ / 中文验证 ✅ / 排序 ✅ / limit·offset ✅ /
  type·status 过滤 ✅ / Source 关联 ✅ / migration v2 ✅ / backfill ✅ / 索引一致 ✅ /
  空查询·非法参数·特殊字符 ✅ / 迁移幂等 ✅ / 旧测试全通过 ✅ / 真实 CLI 中英文检索 ✅
* **独立对抗式审查**报出 1 个真实缺陷（命中未携带原始 `bm25`/索引名）与 4 个健壮性缺口
  （裸 SQL `INSERT OR REPLACE` 索引漂移、无漂移检测/修复、LIKE 命中 JSON 标点、控制字符错误类型），
  全部修复：`PRAGMA recursive_triggers=ON`、`index_consistency()`、`rebuild_search_index()`、
  标签改用 `json_each` 匹配、控制字符 → `ValidationError`；详见 [docs/PHASE3.md](docs/PHASE3.md) §9.1。

---

## 15. 阶段 4：Memory Lifecycle & Quality（已完成）

完整说明见 [docs/PHASE4.md](docs/PHASE4.md)。要点：

* **生命周期**：沿用既有三个状态，只定义**转换表**——`pending→active/archived`、`active→archived`、`archived→active`；
  转到 `pending` 抛 `IllegalTransitionError`，**未知目标值**抛 `ValidationError`；同状态调用是幂等 no-op（不写库、不改 `updated_at`）；
  `deleted` 不是状态，删除是操作。
  **强制点有两处、表只有一份**：表定义在 `personal_memory/models.py`，`MemoryLifecycle` 与
  `MemoryRepository.update_memory(status=…)` 读同一个对象（`is` 断言防副本），所以绕过生命周期服务直接调仓库 API
  同样会被拒绝；而 `create_memory` / `MemoryUnitOfWork.create_memory` **刻意不加门禁** ——
  Memory Formation 必须能创建 `pending`（低置信度推断/疑似冲突），被禁止的是把已有 Memory 改写成 `pending`。
* **更新**：`title/content/summary/tags/importance/confidence/type/information_origin/status` 可更新，
  阶段 1 校验一条不放松，非法更新**整行不变**；带 `status` 的更新同样受转换表约束。
* **删除安全**：Memory 行 + `memory_sources` 关联在单事务内删除，Source 原文完整保留
  （`DeleteReport` 会重新读取数据库确认 `sources_kept` / `sources_intact`）。
* **精确重复**：`sha256(memory-fingerprint-v1 | type | NFKC+casefold+空白折叠(title) | …(content))`；
  流式扫描建立"指纹 → id"工作集（单条预检按候选 `type` 收窄，**形成写入事务内的复检扫全表一次**，写锁关掉 TOCTOU），
  同一次回答内的相同草稿也只写第一条；命中 → `action="reuse"`，绝不写第二行。
  注意 NFKC 折叠类：`①`≡`1`、`㎡`≡`m2`、NBSP≡空格、`Straße`≡`STRASSE` 视为同一条（属定义的一部分），
  而 ASCII 标点/语序/同义改写算不同记忆。
* **冲突保守处理**：用阶段 3 检索（active+pending）找相关记忆 → 可选 LLM 分类器（复用 `llm.py` + `memory-conflict-v1`）
  只输出 `same/compatible/conflict/uncertain` → 严格 schema 校验 → 代码策略决定状态：
  `same/conflict/uncertain → pending`、`compatible → 保持原定状态`；**旧记忆永不被自动修改**（4 种关系逐一断言），
  分类器没有任何写库能力，答案非法时带反馈重试、最终明确抛错而不是降级；
  候选不会与自己冲突（`check_conflict` 排除自身 id），`unclassified_relation` 只允许保守三值。
* **无新增 migration**（schema 仍为 v2）：所有能力都用既有结构表达；`db.py` 一行未改，
  新库与阶段 3 一样 `applied_count = 2`，旧库直接可用。
* **与检索联动**：状态过滤发生在 JOIN `memories` 时，内容变更由既有触发器同步索引 →
  archive 后默认 search 立即为 0、restore 后立即为 1、更新后旧关键词立即消失新关键词立即可检索、删除后四种 scope 全部为 0 且 `index_consistency.consistent = true`。
* **实测**：`docs/phase4-cli-lifecycle.txt`（19 条真实子进程命令 + 11 项布尔交叉校验全 true；
  其中一条直接调用仓库 API，实测 `IllegalTransitionError`、`row_unchanged=true`、`status_after=active`）、
  `docs/phase4-acceptance.json`（五场景全 PASS；真实 `deepseek-flash` 对 A/B 偏好给出 `conflict`，
  新记忆落库为 `pending`、旧记忆整行未变；5 条记忆 5 个唯一指纹，无重复）、
  `docs/phase4-concurrency.txt`（2 进程并发 3/3 轮只落 1 行）、
  `docs/phase4-conflict-audit.txt`（裸 SQL 审计触发器：冲突过程 0 次 UPDATE/DELETE 旧记忆）。
  验收脚本驱动的是同一个 CLI 入口（同进程、捕获 stdout），独立子进程 CLI 证据是 `phase4_cli_evidence.py`。
* **独立对抗式审查**：代码声明 9/10 PASS；报出 1 个真实缺口（孤立代理项绕过校验后在 sqlite3 层抛原始
  `UnicodeEncodeError`）与 3 个健壮性缺口（自比较、`unclassified_relation="compatible"`、分类器返回非
  `ConflictVerdict`），全部修复并补测试；同时修正了文档中 2 处不实引文与若干表述（详见
  [docs/PHASE4.md](docs/PHASE4.md) §12）。
* **收尾修复（生命周期一致性）**：审查记录的"通用 CRUD 可绕过转换表"已消除 ——
  `update_memory(status=…)` 现在与生命周期服务共用同一张表并抛 `IllegalTransitionError`，
  同状态更新是真正的 no-op（裸 SQL `AFTER UPDATE` 触发器证明零写入）；创建路径不受影响（Formation 仍能创建 pending）。
  复验：`Ran 308 tests … OK (skipped=1)`，CLI 生命周期验证重跑通过（详见
  [docs/PHASE4.md](docs/PHASE4.md) §12.1）。
* **阶段 4 完成条件**：状态语义 ✅ / 转换正确 ✅ / Archive·Restore ✅ / Delete 安全 ✅ / Update ✅ /
  Exact duplicate ✅ / Conflict 保守处理 ✅ / 不自动覆盖 ✅ / Pending 不污染检索 ✅ /
  更新·删除后索引正确 ✅ / Source 不被误伤 ✅ / 事务原子 ✅ / 旧测试全通过 ✅ / 新测试全通过 ✅ /
  真实 CLI 验收 ✅ / 真实 LLM 最小 smoke test ✅。**随后停止，不开发 Knowledge Base。**

---

---

## 16. Knowledge Base 1.0 阶段 1：Capture Layer（已完成）

完整说明见 [docs/KB1-PHASE1.md](docs/KB1-PHASE1.md)。要点：

* **只解决一个问题**：把一段原始信息通过统一的 `CaptureRequest` 交给已经冻结的 Memory System：
  `User Input → Capture → CaptureRequest → RawInput → Memory Formation → Memory System`。
* **独立一层**：`CaptureService` 的构造函数只接受 `MemoryFormationService`（外加 provenance 字符串），
  没有 repository / database / LLM client —— "Capture 直接写 SQLite"或"Capture 直接调 LLM"在结构上不可能发生
  （`capture.py` 里没有 `sqlite3`、没有任何 SQL 动词，测试有架构守卫断言）。
* **统一输入结构**：`CaptureRequest(content, title, source_type, url, metadata, captured_at)`；
  `to_raw_input()` 是**唯一**适配点，只做字段改名（`captured_at → created_at`）并补 `metadata["captured_from"]`，
  不复制任何 Formation 逻辑。
* **低价值输入零写入**：`capture("今天喝了一杯奶茶。")` → Formation 判 `worth_remembering=false` →
  不形成 Memory，**Source 也不长期保存**（Capture 不会因为"收到了输入"就写库）。
* **失败不留半成品**：LLM/传输失败抛既有类型化错误（CLI 退出码 3、配置错误 2），
  写入阶段任何异常整体回滚；实测用不可达端点验证 `LLMRequestError` + 该输入无 Source/Memory + 计数不变。
* **不引入下一步功能**：没有文件导入、没有 URL 抓取、没有 Importer、没有 UI；
  `source_type` 的 `chat/article/web/file` 只是结构预留。
* **实测**：[docs/kb1-capture-acceptance.json](docs/kb1-capture-acceptance.json)
  （A 高价值 → 1 条 Memory + 1 个 Source，counts `0/0/0 → 1/1/1`；B 低价值 → `skipped`、零写入；
  C 失败 → `exit 3`/`LLMRequestError`、无半成品；D 新进程 `search "RAG"` 仍命中；F 空输入 → `ValidationError`）、
  [docs/kb1-real-llm-capture.txt](docs/kb1-real-llm-capture.txt)（真实 LLM 单元测试 `Ran 1 test … OK`）。
* **完成标准**：RawInput ✅ / Capture Service ✅ / Capture → Formation ✅ / 未复制 Formation 逻辑 ✅ /
  低价值行为 ✅ / Source 不自动长期保存 ✅ / CLI 真实可跑 ✅ / 自动化测试全通过（335 tests）✅ /
  阶段 1–4 测试全通过 ✅ / 真实 LLM 端到端 ✅ / 未引入下一阶段功能 ✅。**随后停止。**

---

## 17. Knowledge Base 1.0 阶段 2：TXT + Markdown Importer（已完成）

完整说明见 [docs/KB1-PHASE2.md](docs/KB1-PHASE2.md)。要点：

* **只做一件事**：把 `.txt` / `.md` / `.markdown` 文件变成"干净、稳定、可交给 Capture 的文本"，
  然后走已经完全冻结的管线：`path → FileImporter → CaptureRequest → CaptureService → MemoryFormationService → Memory System`。
* **独立一层**：`importers/files.py`、`importers/markdown.py` 中没有 `sqlite3`、没有任何 SQL 动词、没有 store 导入；
  `load()` 签名里没有 repository，`import_document()` 只接受 `CaptureService`（传 repository 会被拒绝）——
  架构守卫测试断言这几点。
* **统一输出**：两种格式最终都产生同一个 `CaptureRequest`（`source_type=file`）；没有第二套 Memory/Formation 逻辑。
* **标题与清洗**：title 取"显式 > Markdown H1 > front matter `title:` > 文件名"；清洗只删代码围栏行、折叠空行，
  其余（标题标记、段落、列表、引用、代码文本、front matter、HTML 注释）全部保留——不过度清洗。
* **元数据**：记录 `filename` / `extension` / `size_bytes` / `file_sha256` / `source_format` / `encoding` /
  `captured_from="file"`，**不记录本机绝对路径**（避免路径泄露与跨设备问题）。
* **失败明确**：文件不存在 / 目录 / 扩展名不支持 / 无法读取 / 编码非 UTF-8 / 空文件 / 超过上限 →
  类型化 `FileImportError` 子类，退出码 3，且因为文件校验发生在打开数据库之前，坏输入连空数据库都不会创建。
* **去重复用现有机制**：Source 由 `content_hash` 复用、Memory 由 Phase 4 精确重复/保守冲突策略处理，Importer 不写第三套算法。
* **实测**：[docs/kb1p2-import-acceptance.json](docs/kb1p2-import-acceptance.json) ——
  A `rag.txt` → `persisted`（2 条 Memory + 1 个 file Source，counts `0/0/0 → 2/2/1`）；
  B `rag.md` → 标题取 H1「向量检索与重排序」，2 条 Memory + 1 个 Source；
  C 再次导入 `rag.md` → Source `reused=True`、新增记忆全部 `pending`、6 条记忆 6 个唯一指纹（无重复）；
  D 新进程 `search 检索` → `total=6`；E 四种坏输入 → 对应类型化错误且未创建数据库。
* **完成标准**：TXT Importer ✅ / Markdown Importer ✅ / 统一 Importer → Capture ✅ / 未复制 Formation 逻辑 ✅ /
  失败零长期数据 ✅ / 去重复用现有机制 ✅ / CLI 真实可跑 ✅ / 测试全通过（367 tests）✅ /
  真实 TXT → Memory ✅ / 真实 Markdown → Memory ✅ / 新进程 Retrieval 命中 ✅ / 旧测试全通过 ✅。**随后停止。**

---

## 19. Knowledge Base 1.0 阶段 3：Chat Importer（已完成）

完整说明见 [docs/KB1-PHASE3.md](docs/KB1-PHASE3.md)。要点：

* **只做一件事**：把聊天记录变成"角色明确、顺序稳定、带会话边界"的文本，交给冻结的管线：
  `聊天数据 → ChatImporter → ChatConversation → CaptureRequest(source_type=chat) → Capture → Formation → Memory System`。
* **两个数据结构**：`ChatMessage(role, content, timestamp)` 与
  `ChatConversation(conversation_id, messages, title, provider, started_at, ended_at, metadata, title_source)`；
  校验在 `__post_init__`（角色合法、内容非空、时间戳合法、顺序保持）。
* **两种输入格式 + 适配器接缝**：角色文本（`[User]` / `## Assistant`，整行角色头）与文档化的 provider-neutral JSON；
  `chat_adapters.py` 定义 `ChatAdapter` 协议与注册表 —— 加一个平台只需要加一个适配器。**本阶段没有平台专用适配器**：
  工作区没有任何导出样本（已按要求检查），机器上的 `~/.dsh/sessions`、`~/.codex/sessions` 是私有实时状态，
  不是样本，未从中推断格式。
* **角色语义不混淆**：角色标记固定为 `[USER]` / `[ASSISTANT]` / `[SYSTEM]` / `[TOOL]` / `[DEVELOPER]`，
  只按消息真实角色输出；行内 `[USER]` 字样留在正文里；未知角色整行明确报错，绝不默认算给用户；
  Importer 不写 `information_origin`/`confidence`/`importance` —— 用户原话仍是 `user_explicit`、
  助手知识是 `source_content`、助手推断是 `agent_inference`（真实实测：用户学习目标 3 条 `user_explicit`，
  助手解释的知识 1 条 `source_content`）。
* **会话元数据**：`captured_from=chat`、`provider`（未知为 `null`，不猜）、`conversation_id`、`message_count`、
  `roles`、`started_at`/`ended_at`；不保存本机路径，不把正文写进 metadata。
* **超长对话**：超过可配置上限（默认 1 MiB，与文件导入器同一机制）**明确失败**，绝不静默截断；
  本阶段不实现 chunk / 摘要压缩。
* **隐私**（规格书 §十九）：默认视图不含任何正文字符（只有 role/timestamp/长度）；
  ≤60 字预览与全文都是显式选项；证据文件写入前自检，含对话正文即拒绝（实测 `body_in_evidence = []`）；
  CLI 人类可读输出不打印 Source 正文；文档明确说明"聊天正文经真实 LLM Formation 会发送给配置的模型服务"。
* **实测**：[docs/kb1p3-chat-acceptance.json](docs/kb1p3-chat-acceptance.json) ——
  A 高价值聊天 → `persisted`，3 条 `profile/user_explicit`；
  A2 `--keep-source always` → Source `source_type=chat`、`content_chars=139`、
  角色边界 `["[USER]","[ASSISTANT]"]`，同时 1 条 `knowledge/source_content` 并关联该 Source；
  B 低价值聊天 → `skipped`，Memory=0、Source=0（零写入）；
  C JSON 格式 → 同一规范化会话（无模型）；D 超长对话 → `FileTooLargeError`、零写入；
  E 默认视图不含正文；F 新进程 `search Agent` 命中场景 A 的 3 条 Memory（id 完全一致）。
* **完成标准**：ChatMessage/ChatConversation ✅ / 角色解析 ✅ / 时间与会话边界 ✅ / Chat → Capture ✅ /
  source_type=chat ✅ / user·assistant 语义边界 ✅ / 高价值 → Memory ✅ / 低价值 → Memory=0 ✅ /
  低价值不保存 Source ✅ / Source-Memory 关联 ✅ / 复用途径去重 ✅ / CLI 真实可跑 ✅ /
  测试全通过（409）✅ / 真实 Chat → Memory ✅ / 真实低价值零写入 ✅ / 旧测试全通过 ✅。**随后停止。**

---

## 20. Knowledge Base 1.0 MVP：本地 Web UI（已完成）

完整说明见 [docs/MVP-WEB-UI.md](docs/MVP-WEB-UI.md)。要点：

* **零新增运行时依赖**：`http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler` + 服务端渲染 HTML，
  一份内联 CSS，唯一的一段 JavaScript（约 20 行）只用来把选中的文件读成 base64。
  没有 React/Vue/Node/Tailwind，没有构建步骤，离线可用。
* **四（五）个区域**：Capture、Import（文件 + 聊天）、Memories（列表/详情/编辑/生命周期）、
  Sources（列表/详情）、Search。首页给出计数、数据库路径、模型信息、索引一致性与隐私提示。
* **HTTP 层只做请求/响应与展示**：所有写入都调用既有模块 ——
  `CaptureService` / `FileImporter` / `ChatImporter` / `MemoryRetriever` /
  `MemoryLifecycle` / `MemoryRepository`；`personal_memory/web/` 里没有 `sqlite3`、没有 SQL、
  没有 `ALLOWED_TRANSITIONS`、没有 prompt（测试静态扫描 + 对象同一性断言）。
* **安全**：默认只监听 `127.0.0.1`（验收实测本机 LAN 地址连不上）；API Key 不出现在任何响应或日志；
  原始对话渲染只在用户主动打开的 Source 详情页出现；服务端日志只有 `方法 路径 -> 状态码`；
  无 telemetry；跨站 `Origin` 的 POST 返回 403。
* **错误映射**：`ValidationError→400 输入不合法`、`FileImportError→400 文件导入失败`、
  `IllegalTransitionError→409 当前状态不能执行该操作`、`NotFoundError→404`、
  `ExtractionValidationError→502`、`LLMRequestError→502`、`Conflict/Duplicate→409`；
  永远不把 traceback 抛给浏览器。
* **实测**：[docs/kb1-mvp-web-acceptance.json](docs/kb1-mvp-web-acceptance.json) —— 真实 `web` 子进程 +
  真实 HTTP（20 次请求）走完 §二十一 场景，8/8 PASS：文本 Capture 生成 1 Memory/1 Source；
  低价值文本零增长；`.md` 上传保留 filename/extension/encoding/file_sha256；
  聊天导入 Source `source_type=chat`、角色边界 `["[USER]","[ASSISTANT]","[USER]"]`、`information_origin` 可见；
  搜 "RAG" 命中；Edit→可搜 / Archive→从默认列表与搜索消失 / Restore→恢复 / Delete→Memory 消失而 Source 保留；
  LAN 连接被拒绝、响应与日志无 API Key、无聊天正文。
* **测试**：`Ran 438 tests … OK（skipped=2）`（既有 409 全部继续通过 + 新增 29）。
* **完成标准（§二十三）**：UI 九项 ✅、架构五项 ✅、安全四项 ✅、验证七项 ✅。**随后停止**，
  不继续做 URL / PDF / Embedding / RAG / QA / MCP / Graph / Agent。

---

## 21. Knowledge Base 1.0 阶段 4：URL / Web Importer（已完成）

完整说明见 [docs/KB1-PHASE4.md](docs/KB1-PHASE4.md)。要点：

* **独立模块** `personal_memory/importers/web.py`：只做「抓取 → 解析 → 清洗 → 标准化」，
  返回 `WebDocument`，**不碰 SQLite、不调用 LLM、不生成 Memory**；持久化只经 `CaptureRequest` → `CaptureService`。
* **零新增依赖**：`urllib.request`（单跳 GET，不自带重定向跟随）+ 标准库 `html.parser`（自建节点树）+
  `ipaddress`。本环境没有 `lxml`/`bs4`/`readability`/`trafilatura`（`requests` 存在但未使用）。
* **安全边界（每条都有测试）**：http/https only、拒绝 URL 凭据与非法端口、拒绝
  localhost/`*.local`/`*.internal`/回环/私有/链路本地/CGNAT/保留地址与云元数据地址、
  域名解析后每个地址都必须 `is_global`、重定向逐跳重新校验且限 5 跳、超时、`Content-Length`+读取字节上限、
  Content-Type 白名单、正文不进日志也不进错误消息。
  **未实现**（如实记录）：DNS rebinding/TOCTOU 未完全防止、反向代理型目标无法识别 —— 不声称绝对 SSRF 防护。
* **正文提取**：`article` → `main` → `body` → 整篇，取第一个达到 `--min-chars`（默认 200）的候选；
  过滤 `script`/`style`/`form`/`nav`/`footer`/`header`/`aside` 与 ARIA landmark；
  实体解码、编码回退（声明 → utf-8 → gb18030 → cp1252）、空白折叠；
  保留段落、列表（`- `）、代码（含缩进）、链接 `文本 (href)`、图片 alt；过短/只有导航 → 明确失败。
* **来源 metadata**：`captured_from=web`、original/final URL、`fetched_at`、`content_type`、正文 `content_sha256`、
  `http_status`、`bytes`、`charset`、`extraction_root`、`title_source`、重定向链 —— 不含本机路径。
* **CLI**：`python -m personal_memory import-url <url> [--db --title --max-bytes --timeout --min-chars --dry-run --no-quality-check --config --json]`；
  **Web UI**：Import 页面新增「Import 网页 URL」表单，POST `/import-url` 走同一个 Importer 与 Capture 管线；
  两者默认都不回显网页正文。
* **实测**：[docs/kb1p4-web-acceptance.json](docs/kb1p4-web-acceptance.json) —— 真实公网抓取 + 真实 CLI/UI 子进程 +
  真实 `deepseek-flash`，7/7 场景 PASS：5 个真实页面提取成功且不含 HTML；
  PEP 20 → 2 条 `knowledge`/`source_content` Memory + `source_type=web` Source（1635 字符）；
  GitHub README 页面 → `extraction_root=article`、2 条 Memory；
  新进程检索命中全部 4 条导入 Memory；6 个非法/低价值 URL（含真实 `example.com` 无正文、真实 404、
  回环、云元数据地址、私网、`file://`）全部退出码 3 且零写入；UI `POST /import-url` 200 + 建库；
  CLI 输出与证据中无正文、无 API Key。
* **测试**：`Ran 475 tests … OK（skipped=2）`（既有 438 全部继续通过 + 新增 37，全部离线；
  含 10 个压缩炸弹/损坏流测试）。**安全审查修正**见
  [docs/KB1-PHASE4.md](docs/KB1-PHASE4.md) §11：原实现对 gzip/deflate 是"先完整解压再检查上限"
  （实测 65 KB 炸弹 → 峰值 141 MiB），现改为流式有界解压（同一炸弹 → 峰值 0.24 MiB，停在上限+1），
  并新增 `ContentEncodingError`（损坏/截断/未知编码）。
* **完成标准**：安全访问 ✅ / 正文提取 ✅ / 来源元数据 ✅ / → CaptureRequest ✅ / 管线复用 ✅ /
  UI 可查看 ✅ / 失败明确且零残留 ✅ / 旧测试全通过 ✅ / 真实文章 + GitHub README 端到端 ✅ /
  真实低价值输入不产生 Memory·Source ✅。**随后停止**，不做 PDF / OCR / Embedding / RAG / 爬虫 / 批量 URL。
