# Knowledge Base 1.0 — Phase 2：TXT + Markdown Importer（文件导入层）

本文件是 KB 1.0 第二阶段的完整说明与实测证据。

| 系统 | 状态 |
| --- | --- |
| Personal Memory System v0.1（Phase 1–4） | **已冻结**，本阶段未改 |
| KB 1.0 Phase 1：Capture Layer | 已冻结，本阶段**只复用**（未改 `capture.py`） |
| KB 1.0 Phase 2：TXT + Markdown Importer | ✅ 本阶段 |
| Chat / URL / PDF / OCR / UI / 批量管理 / 目录扫描 | ❌ 未实现（本阶段明确禁止） |

生成时间：2026-10-04；解释器 `D:\python\python.exe`（3.13.2）；SQLite 3.45.3。

---

## 1. 新增/修改的文件

**新增**

| 文件 | 作用 |
| --- | --- |
| `personal_memory/importers/__init__.py` | 文件导入层的公开 API（`FileImporter` / `import_file` / 错误类型 / Markdown 清洗函数） |
| `personal_memory/importers/files.py` | 文件读取、校验、解码、`ImportedDocument`、`ImportResult`、`FileImporter`、错误层次 |
| `personal_memory/importers/markdown.py` | Markdown 标题提取 + 轻量清洗（`clean_markdown` / `split_front_matter`） |
| `tests/test_importers.py` | 28 个测试：TXT 8 项、Markdown 5 项、集成 7 项 + 边界与架构守卫 |
| `scripts/kb1p2_import_acceptance.py` | 验收场景 A–E（真实 CLI；A/B/C 走真实 LLM，D/E 不调用模型） |
| `docs/KB1-PHASE2.md` | 本文件 |
| `docs/kb1p2-import-acceptance.{json,txt}` | 验收原始证据（含真实 LLM 结果与人类可读 CLI 输出） |
| `data/kb1p2-fixtures/rag.{txt,md}` | 验收用的真实输入文件（其 `file_sha256` 记录在证据里，可逐字节复核） |

**修改（必要且最小）**

| 文件 | 修改 | 原因 |
| --- | --- | --- |
| `personal_memory/cli.py` | 新增 `import-file` 命令（`--title/--source-type/--max-bytes/--dry-run/--no-quality-check/--config/--json`）；把 capture/import 共享的输出尾段抽成 `_print_formation_tail()`；`version` 的 `phase` 指向 KB 1.0 Phase 2 | 真实可跑的入口（规格书 §十） |
| `personal_memory/__init__.py` | 导出 importer API；版本 0.5.0 → 0.6.0，`__phase__ = "kb-1.0-phase-2"` | 公开 API 与版本 |
| `tests/test_cli.py` | 版本断言（0.5.0 → 0.6.0、`phase` 含 importer）+ 4 个 `import-file` CLI 用例 | 版本确实变了；新命令需要离线路径测试 |

**未改（指纹证明）**：`capture.py`、`models.py`、`store.py`、`extraction.py`、`retrieval.py`、`lifecycle.py`、
`quality.py`、`llm.py`、`prompts.py`、`errors.py`、`db.py`、`docs/schema.sql` 与阶段 1–4 / KB P1 的全部既有测试文件
（`test_capture.py` 等）——本阶段实测指纹与本阶段开始前**逐一相同**（见 §11）。数据库 schema 仍为 v2，**没有新增 migration**。

---

## 2. TXT / Markdown 的解析流程

统一入口，两种格式只在"如何得到 title/body"这一步不同：

```text
path
  │
  ├─ ① 存在性 / 类型检查        exists() → is_file()
  ├─ ② 扩展名检查               .txt / .md / .markdown
  ├─ ③ 读取字节                 read_bytes()（OSError → FileReadError）
  ├─ ④ 大小上限                 默认 1 MiB（可配置）
  ├─ ⑤ 解码                    UTF-8 优先（BOM 走 utf-8-sig；UTF-16/32 给出明确错误）
  ├─ ⑥ 文本解析（唯一分叉点）
  │     .txt  → normalize_content(text)                 （NFC + LF + 行尾空白 + 整体 strip）
  │     .md   → clean_markdown(text) → (title, body)   （§4）
  ├─ ⑦ 空内容检查               只有空白 → EmptyFileError
  ├─ ⑧ title 决定              显式 title > Markdown H1 > front matter title > 文件名（不含扩展名）
  └─ ⑨ metadata 组装            filename / extension / size_bytes / file_sha256 / source_format / encoding
                                + captured_from="file"（**不含本机绝对路径**）
        ↓
    ImportedDocument（纯数据）→ to_capture_request() → CaptureRequest(source_type=file)
```

`source_type` 固定为 `file`（`SourceType.FILE`，阶段 1 既有枚举）；`url` 恒为 `None`
（本地路径不是 URL，阶段 1 的 `Source` 也只接受 http(s) 链接）。

---

## 3. Importer → Capture 的调用关系

```text
FileImporter.load(path)              ← 纯文件工作：无 DB、无模型
        ↓  ImportedDocument
FileImporter.import_document(doc, capture)   /   FileImporter.import_file(path, capture)
        ↓  document.to_capture_request(source_type="file")
CaptureService.capture_request(request)      ← KB 1.0 Phase 1（未改）
        ↓  RawInput
MemoryFormationService.process(raw_input)    ← Memory System Phase 2（冻结）
        ↓  价值判断 → 提取 → FormationPolicy → Phase 4 质量闸门
MemoryRepository.transaction()               ← Memory System Phase 1（冻结）
        ↓
ImportResult(document, capture_result, import_status)
```

* **Importer 不写 SQLite**：`files.py` / `markdown.py` 中没有 `sqlite3`、没有 `SELECT/INSERT/UPDATE/DELETE`、
  没有 `store` 导入；`load()` 的签名里没有 repository，`import_document()` 只接受 `CaptureService`
  （传 repository 会抛 `ValidationError`）——测试 `test_18_importer_never_touches_sqlite` 断言这四点。
* **Importer 不调用 LLM**：它不持有任何 LLM client；模型只可能通过 Formation 被调用。
* **不复制 Formation 逻辑**：`worth_remembering` / `importance` / `confidence` / `information_origin` /
  Source 策略 / Phase 4 质量闸门全部继续由既有代码决定；Importer 只产出文本与元数据。
* **CLI 的同一条管线**：`import-file` 与 `capture`、`form` 使用同一个 `MemoryFormationService`
  （含同一个 Phase 4 质量闸门，`--no-quality-check` 可关闭），没有第二条链路。
* **失败不会产生长期数据**：文件层错误发生在任何 DB/模型调用之前；形成阶段的错误按既有类型化错误上抛，
  Phase 2 的原子事务回滚（实测场景 E + 测试 19）。

---

## 4. Markdown 清洗规则

只做"结构无关"的清洗，规则全部可测：

| # | 规则 | 例子 |
| --- | --- | --- |
| 1 | 换行统一（CRLF/CR → LF）+ Unicode 归一化（`normalize_content`：NFC、行尾空白、整体 strip） | `"a\r\n"` → `"a"` |
| 2 | **代码围栏行被删除，围栏内的代码文本保留** | ` ```python ` / ` ``` ` 行删除，`def rag(...)` 保留 |
| 3 | **连续空行折叠为一个空行** | 4 个空行 → 1 个 |
| 4 | 其余内容**原样保留**：标题（含 `#` 标记）、段落、列表标记、引用、表格、链接、图片、行内强调、行内代码、HTML 注释、YAML front matter | `# RAG 基础`、`- 要点一`、`## 原理` 全部保留 |

**删除策略**：本阶段只删除"纯语法、且可证明不是内容"的东西（围栏行）与折叠空白，**不删除**任何可能是内容的文本。
front matter 也**不删除**——它只被用来提取 title 兜底（`title: xxx`），仍留在正文里。

**title 提取优先级**：调用方显式 title > 第一个 ATX 一级标题（`# X`）> front matter 的 `title:` > 文件名。
`title_source` 会记录实际来源（`explicit` / `heading` / `front_matter` / `filename`），便于验证。

**明确不做**：Markdown → HTML 渲染、自动摘要、自动分类、自动分 chunk、自动生成 Memory、自动判断重要性、
删除 `#`/`-`/`**` 标记、重写或丢弃链接、删除 HTML 注释。

---

## 5. 错误处理

所有失败都在**写库之前**抛出，并带明确类型与原因（都继承 `FileImportError`，进而继承 `MemorySystemError`；
CLI 退出码 3，配置类错误 2）：

| 情况 | 异常 | 消息要点 |
| --- | --- | --- |
| 文件不存在 | `FileMissingError` | `file not found: <path>` |
| 路径是目录 / 特殊文件 | `NotAFileError` | `not a regular file (directory or special file)` |
| 扩展名不支持 | `UnsupportedFileTypeError` | `unsupported file type '.pdf' …; this phase imports ['.txt', '.md', '.markdown']`（带 `.extension` / `.supported`） |
| 文件无法读取（权限 / I/O） | `FileReadError` | `could not read <path>: <cause>` |
| 编码无法解码 | `FileEncodingError` | `could not decode … as utf-8: … ; convert the file to UTF-8 and import it again`（UTF-16/32 会指出 BOM） |
| 文件为空（只有空白） | `EmptyFileError` | `file is empty (no content to capture)` |
| 文件过大 | `FileTooLargeError` | `file is too large: N bytes > limit M bytes`（带 `size_bytes` / `max_bytes`） |

失败后果（实测）：`counts` 不变、该内容没有 Source/Memory、索引一致；
**且 CLI 对文件错误发生在打开数据库之前**，所以一个坏路径连空数据库文件都不会创建（验收 E 实测 `database_created=false`）。

---

## 6. 测试结果

```text
cd D:\DSH-worlp\personal-memory-system
python -m unittest discover -s tests -t . -v
Ran 367 tests ... OK (skipped=2)
```

* Phase 1–4 与 KB Phase 1 的 335 个测试**全部继续通过**（唯一改动是 `test_cli.py` 的版本断言与新增的 4 个导入 CLI 用例）。
* KB 1.0 Phase 2 新增 **32** 个测试：`tests/test_importers.py` 28 + `tests/test_cli.py` 4。
* 2 个 skip = 两个可选真实 LLM 测试（`test_llm_real.py`、`RealLLMCaptureTest`）；导入层测试全部离线运行。

规格书 §十一 的 20 项要求逐条对应：

| # | 要求 | 测试方法 | 结果 |
| --- | --- | --- | --- |
| 1 | 正常 TXT 导入 | `TxtImporterTest::test_1_normal_txt_import` | PASS |
| 2 | UTF-8 正常读取 | `…::test_2_utf8_is_read_first_and_chinese_survives`（无 BOM / 有 BOM / CRLF） | PASS |
| 3 | 空 TXT 拒绝 | `…::test_3_empty_txt_is_rejected`（`""`、纯空白、只有 BOM） | PASS |
| 4 | 不存在文件拒绝 | `…::test_4_missing_file_is_rejected` | PASS |
| 5 | 目录拒绝 | `…::test_5_directory_is_rejected` | PASS |
| 6 | 非 UTF-8 编码错误明确 | `…::test_6_non_utf8_encoding_fails_with_a_clear_error`（GBK / UTF-16） | PASS |
| 7 | title 默认使用文件名 | `…::test_7_title_defaults_to_the_file_name`（含显式 title 覆盖） | PASS |
| 8 | `source_type=file` | `…::test_8_source_type_is_file`（请求 + 持久化后的 Source） | PASS |
| 9 | 正常 Markdown 导入 | `MarkdownImporterTest::test_9_normal_markdown_import` | PASS |
| 10 | 一级标题提取 | `…::test_10_h1_heading_becomes_the_title`（且标题行保留在正文） | PASS |
| 11 | 无标题时使用文件名 | `…::test_11_without_h1_the_file_name_is_used_and_front_matter_is_a_fallback` | PASS |
| 12 | Markdown 正文正确进入 Capture | `…::test_12_markdown_body_reaches_capture`（断言 prompt 里出现正文、列表、`标题：…`） | PASS |
| 13 | 代码块内容不被无理由删除 | `…::test_13_code_block_text_is_not_removed`（代码保留、围栏行删除） | PASS |
| 14 | TXT → Capture | `ImporterIntegrationTest::test_14_txt_file_reaches_capture` | PASS |
| 15 | Markdown → Capture | `…::test_15_markdown_file_reaches_capture` | PASS |
| 16 | 高价值文件形成 Memory | `…::test_16_high_value_file_forms_a_memory`（新连接可读 + Phase 3 检索命中） | PASS |
| 17 | 低价值文件不形成 Memory | `…::test_17_low_value_file_forms_nothing`（零写入） | PASS |
| 18 | Importer 不直接访问 SQLite | `…::test_18_importer_never_touches_sqlite`（静态扫描 + 签名 + 传 repository 被拒） | PASS |
| 19 | Formation 失败时无半成品 | `…::test_19_formation_failure_leaves_no_partial_data`（+ `test_19b` dry-run 零写入） | PASS |
| 20 | 重复导入复用既有去重逻辑 | `…::test_20_reimporting_the_same_file_reuses_the_frozen_dedupe`（Source hash 复用 + Phase 4 精确重复） | PASS |

额外覆盖（边界与守卫）：不支持的扩展名（`.pdf/.rst/.docx/无扩展名`）、`.markdown` 别名、
大小上限（含可配置与非法值）、`metadata`/`Source.metadata` **不含本机路径**、`file_sha256` 稳定、
错误类型都在同一基类下、标题优先级（显式 > H1）、`clean_markdown` 规则本身。

---

## 7. TXT 真实端到端结果

输入 `data/kb1p2-fixtures/rag.txt`（365 字节，`file_sha256=fbb45ddb…`，内容为 RAG 机制与实践要点）。

```text
python -m personal_memory --db data/kb1p2-import.db import-file data\kb1p2-fixtures\rag.txt --json
→ exit 0
  import_status  : imported
  formation_status: persisted
  file           : rag.txt  (.txt, 365 bytes, utf-8)  title="rag"  (from filename)
  memories_created: 2 条 knowledge/active（"RAG 的核心机制"、"RAG 效果上限由检索质量决定"）
  sources_created : 1 个 source_type=file 的 Source（title="rag"，metadata 含 filename/extension/
                    size_bytes/file_sha256/encoding/source_format + formation 块）
  counts          : 0/0/0 → 2/2/1
```

证据：[docs/kb1p2-import-acceptance.json](kb1p2-import-acceptance.json) 场景 `A_txt_import`。

---

## 8. Markdown 真实端到端结果

输入 `data/kb1p2-fixtures/rag.md`（515 字节，`file_sha256=af17e36c…`，含 front matter、H1、列表、代码块）。

```text
python -m personal_memory --db data/kb1p2-import.db import-file data\kb1p2-fixtures\rag.md --json
→ exit 0
  import_status  : imported
  formation_status: persisted
  file           : rag.md  (.md, 515 bytes, utf-8)  title="向量检索与重排序"  (from heading)
  memories_created: 2 条 knowledge/active（"向量检索与 rerank 精排的关系"、"RAG 检索的实践顺序"）
  sources_created : 1 个 source_type=file 的 Source（title="向量检索与重排序"，source_format=markdown）
```

**重复导入（场景 C）**：再导入同一个 `rag.md`，人类可读输出为

```text
import    : status=imported   content_chars=254
formation : status=persisted  worth_remembering=True  attempts=1  model=deepseek-flash
memories  : 2
sources   : 1   reused=True
```

实测结果与解读（如实记录）：Source 被**复用**（`reused=True`，`sources` 行数 2 → 2，没有第二个 Source）；
新增的 2 条 Memory 是 **pending**（模型第二次改写了措辞，Phase 4 质量闸门按"同义/不确定"保守处理，
不激活、不复制），全库 **6 条记忆 6 个唯一指纹**（`duplicated = {}`）。
如果模型给出逐字相同的抽取，则会得到 `status=duplicate` 且 0 条新记忆——两条路径都不产生第二条
"完全相同的长期 Memory"，这正是规格书 §八 要求的"复用现有机制、不重写去重"。

**新进程检索（场景 D）**：`python -m personal_memory --db data/kb1p2-import.db search 检索 --status all --limit 10`
→ `total=6`，命中 6 条由文件导入形成的 Memory（含上面两次 `rag.md` 的 pending 记忆），
证明文件导入的产物完全落在冻结的 Memory System 里、可被 Phase 3 检索到。

---

## 9. 当前未实现功能（本阶段明确不做）

Chat Importer、URL 抓取 / Web Fetcher / 网页解析、PDF、OCR、Embedding、Vector DB、Semantic Search、RAG、QA、
MCP、UI、批量文件管理、文件监听、自动目录扫描；Markdown → HTML 渲染；任何自动摘要 / 分类 / 切分 / 重要性判断。

结构上已预留、但**没有实现**：`SourceType` 的 `chat/article/web/file` 中只有 `file` 被导入层使用；
`CaptureRequest` 的 `url` 仍只是数据（导入层恒为 `None`）。

---

## 10. 已知限制

1. **只支持 `.txt` / `.md` / `.markdown`**；其它扩展名一律明确拒绝（不猜测格式）。
2. **只按 UTF-8 解码**（含 BOM）：GBK/UTF-16/UTF-32 会给出明确错误而不是猜测编码——避免静默乱码。
3. **不做 Markdown 渲染与语义清洗**：不删 `#`/`-`/`**`，不改写链接，不删 HTML 注释，保留 front matter
   （只有围栏行被删除、空行被折叠）；因此正文里会有一些 Markdown 标记，这是刻意的"不过度清洗"。
4. **空行折叠对代码块内部同样生效**（代码块内多个连续空行会被压成一个）。
5. **默认 1 MiB 上限**：这是一个"提示词规模"的限制，不是数据加载器；需要更大可调 `--max-bytes` / `FileImporter(max_bytes=…)`。
   超限文件不会被截断，而是明确失败。
6. **文件去重依赖既有机制**：Importer 不记"已导入过的路径/文件"，重复导入会再次经过 Formation；
   Source 由 `content_hash` 复用，Memory 由 Phase 4 精确重复/保守冲突策略处理（代价：可能产生 pending 记录，如 §8 实测）。
7. **单文件、单次处理**：没有批量、没有目录扫描、没有断点续传、没有并发控制。
8. **`file_sha256` 是原始字节摘要**，记录在 Source 元数据里；本机绝对路径**不落库**（只在 CLI 输出与错误消息里出现），
   因此换机器/换目录后无法从 Memory 反查原始路径。
9. **标题兜底是启发式**：第一个 `# ` 行优先于 front matter 的 `title:`；没有 H1 且 front matter 无 title 时用文件名。
10. **`import-file` 会初始化目标数据库**（与其它命令一致）；但文件校验错误发生在打开数据库之前，所以坏路径不会创建 DB。

---

## 11. 完成标准对照（规格书 §十四）与冻结层指纹

| 完成标准 | 证据 | 结果 |
| --- | --- | --- |
| TXT Importer ✅ | `TxtImporterTest` 8 项 + 验收 A | ✅ |
| Markdown Importer ✅ | `MarkdownImporterTest` 5 项 + 验收 B | ✅ |
| 统一 Importer → Capture ✅ | §3 调用关系；两种格式共用 `CaptureRequest` 与同一条 Formation 管线 | ✅ |
| 不复制 Formation 逻辑 ✅ | `files.py`/`markdown.py` 无 prompt/无 LLM/无价值判断；只调用 `CaptureService` | ✅ |
| 文件失败不产生长期数据 ✅ | 测试 19/19b/边界；验收 E（含"连数据库都没创建"） | ✅ |
| 去重继续复用现有机制 ✅ | 测试 20；验收 C（Source `reused=True`、无重复指纹） | ✅ |
| CLI 可真实运行 ✅ | `import-file`（验收 A–E 全部通过真实子进程） | ✅ |
| 自动化测试全部通过 ✅ | `Ran 367 tests … OK (skipped=2)` | ✅ |
| 至少一次真实 TXT → Memory ✅ | 验收 A（2 条 Memory + 1 个 file Source） | ✅ |
| 至少一次真实 Markdown → Memory ✅ | 验收 B（H1 作标题，2 条 Memory + 1 个 Source） | ✅ |
| 新进程 Retrieval 能找到结果 ✅ | 验收 D（`total=6`） | ✅ |
| Phase 1–4 与 KB Phase 1 旧测试全部通过 ✅ | 335 → 367，原有 335 全部继续通过 | ✅ |

**冻结层未改（指纹证明）**：`db.py` `F6EE1E73E1C747B9`、`retrieval.py` `FF35E0117691437D`、
`lifecycle.py` `A9BF67FBBE7C5C60`、`quality.py` `4C6D3EA3081E84C4`、`extraction.py` `147D50D908EFA216`、
`models.py` `6F892D6888319836`、`store.py` `0AC465BC62958D3D`、`errors.py` `B62C84CBAEB39AED`、
`prompts.py` `7130F83C7E21856E`、`llm.py` `E3D1DCA0FB619130`、`capture.py` `F33696E7C307F6CC`、
`docs/schema.sql` `160069D9C0D8B396` —— 本阶段实测全部与本阶段开始前逐一相同。

本阶段新增/修改文件的实测指纹见 §12。

---

## 12. 本阶段文件的实测指纹

用同一算法（`sha256(文件内容)[:16].upper()`，与 Phase 1–4 的报告一致）在本阶段最后一轮实测：

| 文件 | 指纹 | 字节 |
| --- | --- | --- |
| `personal_memory/importers/__init__.py`（新） | `76DC4C1D5E1A0504` | 2618 |
| `personal_memory/importers/files.py`（新） | `572132F58F627354` | 16363 |
| `personal_memory/importers/markdown.py`（新） | `5AF1086B299DBB46` | 4724 |
| `tests/test_importers.py`（新） | `AAE5491B09CDF078` | 23256 |
| `scripts/kb1p2_import_acceptance.py`（新） | `EE02E03CDDC03B60` | 17937 |
| `personal_memory/cli.py`（改） | `9A61F53DF021E335` | 30872 |
| `personal_memory/__init__.py`（改） | `D260FC72F0DE89ED` | 6202 |
| `tests/test_cli.py`（改） | `4A988814A71F0492` | 16570 |

（这些值由本阶段最后一轮实测得出；报告之后若再次改动文件，指纹会不同，属预期。
**已知变化**：KB 1.0 Phase 3 把读取管线抽成共享的 `read_text_file()`/`decode_utf8()`，`personal_memory/importers/files.py` 因此变化（新指纹见 `docs/KB1-PHASE3.md` §12），行为不变——本文件的 28 个导入器测试在改动后仍全部通过。）

**停止点**：本阶段完成后即停止，不实现 Chat Importer、URL、Web Fetcher、PDF、OCR、UI、批量管理、目录扫描。
