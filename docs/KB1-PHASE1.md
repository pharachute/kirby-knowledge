# Knowledge Base 1.0 — Phase 1：Capture Layer（采集层）

本文件是 KB 1.0 第一阶段的完整说明与实测证据。

| 系统 | 状态 |
| --- | --- |
| Personal Memory System v0.1（Phase 1–4） | **已冻结**，本阶段未重新设计、未重构 |
| Knowledge Base 1.0 Phase 1：Capture Layer | ✅ 本阶段 |
| File / Chat / URL / PDF Importer、UI、Embedding、RAG | ❌ 未实现（本阶段明确禁止） |

生成时间：2026-10-04；解释器 `D:\python\python.exe`（3.13.2）；SQLite 3.45.3。

---

## 1. 新增/修改的文件

**新增**

| 文件 | 作用 |
| --- | --- |
| `personal_memory/capture.py` | Capture 层：`CaptureRequest`（统一输入结构）、`CaptureService`（接收 → 校验 → 标准化 → 调用 Memory Formation）、`CaptureResult`（结果对象）。**不含任何 SQL，也不直接使用 LLM** |
| `tests/test_capture.py` | 24 个测试：有效/空/高价值/低价值/标题/元数据/source_type/URL/失败/边界与架构守卫 + 1 个可选真实 LLM 端到端测试 |
| `scripts/kb1_capture_acceptance.py` | 规格书 §十四 的验收场景 A–D（+F）真实 CLI 运行 |
| `docs/KB1-PHASE1.md` | 本文件 |
| `docs/kb1-capture-acceptance.{json,txt}` | 验收原始证据（含真实 LLM 结果、失败场景、跨进程持久化） |
| `docs/kb1-real-llm-capture.txt` | 可选真实 LLM 单元测试（Test 10）的真实输出 |

**修改（必要且最小）**

| 文件 | 修改 | 原因 |
| --- | --- | --- |
| `personal_memory/cli.py` | 新增 `capture` 命令（`--title/--source-type/--url/--captured-from/--dry-run/--no-quality-check/--config/--json`）；`version` 增加 `memory_system` 字段并把 `phase` 指向 KB 1.0 Phase 1 | 真实可跑的入口（规格书 §十） |
| `personal_memory/__init__.py` | 导出 `CaptureService` / `CaptureRequest` / `CaptureResult` / `CAPTURED_FROM_DEFAULT`；版本 0.4.0 → 0.5.0，`__phase__ = "kb-1.0-phase-1"` | 公开 API 与版本 |
| `tests/test_cli.py` | 版本断言（0.4.0 → 0.5.0、`memory_system` 含 phase-4、`phase` 含 capture）+ 3 个 `capture` CLI 用例 | 版本确实变了；新命令需要离线路径测试 |

**冻结层未改（指纹证明）**：`db.py` `F6EE1E73E1C747B9`、`retrieval.py` `FF35E0117691437D`、
`lifecycle.py` `A9BF67FBBE7C5C60`、`quality.py` `4C6D3EA3081E84C4`、`extraction.py` `147D50D908EFA216`、
`models.py` `6F892D6888319836`、`store.py` `0AC465BC62958D3D`、`errors.py` `B62C84CBAEB39AED`、
`prompts.py` `7130F83C7E21856E`、`llm.py` `E3D1DCA0FB619130`、`docs/schema.sql` `160069D9C0D8B396`
以及阶段 4 冻结的测试文件 —— 本阶段实测全部与 Phase 4 收尾时的值**逐一相同**（`sha256[:16].upper()`），
即 KB 1.0 Phase 1 **没有触碰** Memory Model / schema / Retrieval / Lifecycle / Quality / Formation / LLM 适配层。
数据库 schema 仍为 v2，**没有新增 migration**。

本阶段新增/修改文件当前指纹：

| 文件 | 指纹 | 字节 |
| --- | --- | --- |
| `personal_memory/capture.py`（新） | `F33696E7C307F6CC` | 12109 |
| `personal_memory/cli.py`（改） | `245FFB0A9E569E25` | 25819 |
| `personal_memory/__init__.py`（改） | `214A29E5CC15AED3` | 5463 |
| `tests/test_capture.py`（新） | `DD960836D9212F78` | 22468 |
| `tests/test_cli.py`（改） | `7D998963489C32CE` | 12718 |
| `scripts/kb1_capture_acceptance.py`（新） | `110662C83AB08913` | 15060 |

---

## 2. RawInput 最终结构

Capture 层的统一输入是 `personal_memory.capture.CaptureRequest`（frozen dataclass）：

| 字段 | 类型 | 默认 | 语义 |
| --- | --- | --- | --- |
| `content` | `str` | 必填 | 用户真正提交的文本；必须是非空字符串 |
| `title` | `str \| None` | `None` | 可选标题（Source 无标题时回退到正文首行，Phase 2 既有行为） |
| `source_type` | `SourceType \| str` | `"text"` | 结构上接受 `text/chat/article/web/file`；**当前只产生 `text`** |
| `url` | `str \| None` | `None` | 可选 http(s) 链接；**只作为数据携带，绝不抓取** |
| `metadata` | `Mapping[str, Any]` | `{}` | Capture 层轻量元数据（如 `captured_from`、`language`） |
| `captured_at` | `str` | `utcnow_iso()` | 本次采集时间（ISO-8601） |

边界校验（`CaptureRequest.__post_init__`）只回答"这是不是一条合法输入"：非法 `content`/`title`/`url`/
`source_type`/`metadata`/`captured_at` 抛 `ValidationError` 并带 `field`。**它不判断价值**——
内容会被 Phase 2 的 `RawInput` 再校验一次，那里仍是权威。

**与 Phase 2 `RawInput` 的关系**：`CaptureRequest.to_raw_input()` 是**唯一**的适配点，只做字段改名：

```text
CaptureRequest.content       -> RawInput.content
CaptureRequest.title         -> RawInput.title
CaptureRequest.source_type   -> RawInput.source_type
CaptureRequest.url           -> RawInput.url
CaptureRequest.metadata      -> RawInput.metadata（并补 captured_from，显式值优先）
CaptureRequest.captured_at   -> RawInput.created_at      # 同一个语义：输入产生的时间
```

为什么会有两条记录：规格书要求 Capture 层有自己的统一输入结构（含 `captured_at`），而 Phase 2 的
`RawInput` 已经冻结。两者之间只有**改名**，没有第二套 Memory 结构、没有第二套校验逻辑、没有第二套 Formation。
`RawInput` 的字段名保持不变（`created_at`），因此冻结层的 100+ 测试无需改动。

---

## 3. Capture → Formation 数据流

```text
用户输入（CLI / Python API / 未来的 Importer）
    │
    ▼
CaptureService.capture(content, title=…, source_type=…, url=…, metadata=…, captured_at=…)
    │  ① CaptureRequest：接收 + 边界校验 + 标准化         （capture.py）
    ▼
CaptureRequest.to_raw_input(captured_from=…)
    │  ② 唯一适配点：字段改名 + captured_from             （capture.py）
    ▼
MemoryFormationService.process(raw_input)                 （extraction.py，冻结）
    │  ③ LLM 价值判断（worth_remembering / importance / confidence / information_origin）
    │  ④ 严格 schema 校验 + FormationPolicy
    │  ⑤ 可选 Phase 4 质量闸门（精确重复 / 冲突保守处理）
    ▼
MemoryRepository.transaction()：Source + Memory + memory_sources 原子写入（store.py，冻结）
    │  ⑥ 返回既有 FormationOutcome
    ▼
CaptureResult(captured, status, memories_created, sources_created, formation_result)
```

职责边界：

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| Capture（`capture.py`） | 接收、边界校验、标准化、调用 Formation、组装结果 | ❌ 价值判断 ❌ 提取 Memory ❌ 调用 LLM ❌ 写 SQLite ❌ 检索 ❌ 生命周期 ❌ 去重 |
| Memory Formation（`extraction.py`，冻结） | 价值判断、结构化提取、策略、Source 选择、原子写入 | —— |
| Memory System（`store/retrieval/lifecycle/quality`，冻结） | 持久化、检索、生命周期、质量 | —— |

`CaptureService` 的构造函数**只接受一个 `MemoryFormationService`**（外加 provenance 字符串），
它没有 repository、没有 database、没有 LLM client，因此"Capture 直接写 SQLite"在结构上就不可能发生
（测试 `test_capture_service_signature_is_formation_only` 与 `test_capture_module_has_no_sql_and_no_repository_access` 断言这两点）。

CLI 的 `capture` 与 `form` 使用**同一条管线**（同一 Formation + 同一个 Phase 4 质量闸门，`--no-quality-check` 可关闭），
没有复制任何 Formation 逻辑。

---

## 4. 为什么 Capture 不直接写 Source

这是本阶段最重要的一条原则，直接继承 Phase 2：**原始输入不是记忆，只有形成链路判定有价值才落库。**

```text
capture("今天喝了一杯奶茶。")
  -> MemoryFormationService.process(RawInput(...))
  -> worth_remembering = false
  -> memories = []            （不形成 Memory）
  -> decide_sources([]) = False（连 Source 都不保存）
  -> counts 完全不变
```

实测（验收场景 B，真实 LLM）：`capture_status = skipped`、`worth_remembering = false`、
`memory_count = 0`、`source_count = 0`、`counts` 前后都是 `{memories:1, sources:1, memory_sources:1}`（无任何写入）。

如果 Capture 自己"因为收到了输入"就把原文写成 Source，就会出现："用户随口一句话"永久占据长期库，
且 Source 与 Memory 的关系被断开——这正是 Phase 2 已经解决并冻结的问题，Capture 只复用不改写。
`CaptureResult.captured` 因此表示"输入被采集并被交给 Formation"（接收语义），
**不代表**"已经持久化"；是否落库看 `status` / `memories_created` / `sources_created`。

---

## 5. 自动化测试结果

```text
cd D:\DSH-worlp\personal-memory-system
python -m unittest discover -s tests -t . -v
Ran 335 tests ... OK (skipped=2)
```

* 阶段 1–4 的 308 个测试**全部继续通过**（唯一改动是 `test_cli.py` 的版本断言与新增的 3 个 capture CLI 用例）。
* KB 1.0 Phase 1 新增 **27** 个测试：`tests/test_capture.py` 24 个（其中 1 个可选真实 LLM 测试在无凭据时跳过）
  + `tests/test_cli.py` 3 个 capture CLI 用例。2 个 skip = 两个可选真实 LLM 测试（Phase 2 的 `test_llm_real.py` 与本阶段的 `RealLLMCaptureTest`）。
* 全部新测试默认离线运行（Mock LLM，`tests/llm_fakes.py`），不联网、不使用凭据。

规格书 §十一 的 10 项要求逐条对应：

| # | 要求 | 测试方法 | 结果 |
| --- | --- | --- | --- |
| 1 | 有效文本进入 Formation | `test_capture.py::CaptureInputTest::test_1_valid_text_reaches_memory_formation`（断言 LLM 恰好被调用 1 次、prompt 含正文） | PASS |
| 2 | 空文本 → ValidationError，且不调用 LLM | `…::test_2_empty_or_invalid_content_is_rejected_without_calling_the_model`（6 种非法输入，`transport.call_count == 0`，零写入）+ `test_2b`（title/url/source_type/metadata/captured_at） | PASS |
| 3 | 高价值文本最终形成 Memory | `CaptureFormationFlowTest::test_3_high_value_text_ends_up_as_a_memory`（新连接可读 + Phase 3 检索命中） | PASS |
| 4 | 低价值文本不形成 Memory、Source 不长期保存 | `…::test_4_low_value_text_creates_nothing_and_persists_no_source`（含新连接复查计数） | PASS |
| 5 | title 传递到 Formation | `CaptureInputTest::test_5_capture_title_reaches_formation_and_the_source`（prompt 含"标题：…"，Source.title 正确）+ `test_5b`（无标题回退首行） | PASS |
| 6 | metadata 传递并保留，不改 Memory Schema | `…::test_6_metadata_reaches_formation_and_the_source_without_touching_the_memory_schema`（prompt 含 metadata、Source.metadata 保留、`Memory` 字段清单与 `CURRENT_SCHEMA_VERSION` 不变、Memory 行里没有 capture 元数据）+ `test_6b`（captured_at → Source.created_at） | PASS |
| 7 | source_type：text 可用，其它类型结构上接受/明确拒绝 | `…::test_7_text_works_and_the_future_kinds_are_structurally_accepted`（`text/chat/article/web/file` 均能产生对应 `source_type` 的 Source；`youtube` 明确 `ValidationError`） | PASS |
| 8 | url 只作为元数据进入流程，绝不抓取 | `…::test_8_url_is_carried_as_data_and_never_fetched`（把 `socket.socket` 与 `urllib.request.urlopen` 换成抛异常的桩后仍成功，url 落到 `Source.url`）+ `test_8b`（进 prompt 上下文） | PASS |
| 9 | Formation 失败不产生半完成长期数据 | `CaptureFailureTest`：`test_9`（传输失败 → `LLMRequestError`，计数不变、该内容无 Source）、`test_9b`（模型连续输出非法 JSON → `ExtractionValidationError`，零写入）、`test_9c`（写入阶段注入 link 失败 → Source/Memory/关联全部回滚）、`test_9d`（Capture 没有 repository 句柄） | PASS |
| 10 | 真实端到端（真实 LLM）至少一次 | `RealLLMCaptureTest::test_real_capture_forms_a_memory_end_to_end`（opt-in，1 次真实调用）+ 验收场景 A（真实 CLI） | PASS |

额外覆盖：`CaptureRequest` 不可变且 metadata 为副本、`captured_from` 校验与优先级、
dry-run 只预览不写库、同一文本二次采集交给**冻结的 Phase 4 去重**（`status="duplicate"`、不写第二行）、
以及架构守卫（`capture.py` 无 `sqlite3`/无 SQL 动词、`CaptureService` 签名只有 formation）。

---

## 6. 真实端到端结果

### 6.1 验收场景（真实 CLI，[docs/kb1-capture-acceptance.json](kb1-capture-acceptance.json)）

运行：`python scripts/kb1_capture_acceptance.py --reset`（真实 `python -m personal_memory capture …` 子进程；
A/B 使用真实 `deepseek-flash`，C/D/F 不调用模型）。

| 场景 | 期望 | 实测 | 结果 |
| --- | --- | --- | --- |
| A 高价值文本 | capture → formation → persisted → Memory created | `status=persisted`，1 条 `knowledge` Memory（`RAG 的核心机制与价值`）+ 1 个 `text` Source（`RAG 基础`）+ 1 条关联，`counts 0/0/0 → 1/1/1` | ✅ |
| B 低价值文本 | capture → formation → skipped → Memory=0, Source=0 | `status=skipped`、`worth_remembering=false`、`memory_count=0`、`source_count=0`、`counts` 前后完全相同 | ✅ |
| C 失败 | 模拟 Formation/LLM failure，长期库无半成品 | 用指向 `http://127.0.0.1:9/v1` 的配置：`exit=3`、`error_type=LLMRequestError`（"could not reach …: timed out"）；该输入的 Source/Memory 都不存在、`counts` 不变、索引一致 | ✅ |
| D 持久化 | 关闭进程后重新查询仍存在 | **新进程** `search "RAG"` → `total=1`、命中 `RAG 的核心机制与价值`（带 1 个 Source 引用）；另用全新连接读到 `counts = 1/1/1` | ✅ |
| F 空输入 | 直接拒绝、不调用模型 | `capture "   "` → `exit=3`、`error_type=ValidationError`（"captured content must be a non-empty string"）、`counts` 不变 | ✅ |

最终库状态：`counts = {memories: 1, sources: 1, memory_sources: 1}`，`status_counts = {active: 1, pending: 0, archived: 0}`，
`index_consistency.consistent = true`。

### 6.2 真实 LLM 单元测试（Test 10）

`python -m unittest tests.test_capture.RealLLMCaptureTest -v` → `Ran 1 test … OK`（2.86s，1 次真实调用），
原始输出见 [docs/kb1-real-llm-capture.txt](kb1-real-llm-capture.txt)。

### 6.3 真实 LLM 使用量

验收 A/B 各 1 次 formation 调用（C/D/F 不调用模型），Test 10 再 1 次 —— 本次阶段共 **3 次**真实请求，
模型 `deepseek-flash`（`https://api.deepseek.com`），prompt `memory-formation-v1` +（如命中相关记忆时）`memory-conflict-v1`。
API Key 只在脚本进程环境内使用，**从未打印、从未写入任何证据文件**（脚本保留"密钥进入 payload 即拒绝写文件"的断言）。

---

## 7. 当前未实现的输入来源（本阶段明确不做）

Markdown 导入、TXT 文件导入、PDF 解析、URL 抓取、网页解析、Chat Importer、Embedding、Vector DB、
Semantic Search、RAG、问答（QA）、Web UI、MCP、Agent Tool Calling、Knowledge Base UI。

结构上已为此预留、但**没有实现任何 Importer**：`CaptureRequest.source_type` 接受
`chat/article/web/file`，`url` 字段会被原样保存到 `Source.url`（`SourceType` 是阶段 1 就存在的枚举），
但没有任何代码去读取文件、解析网页或访问网络。

---

## 8. 已知限制

1. **只处理文本**：`source_type` 的其它取值只是结构性预留；传入 `file` 也不会读文件，`content` 仍需调用方提供。
2. **URL 不抓取**：`url` 只是随输入保存的元数据（且当前只有形成 Source 时才会留存）。
3. **不做采集去重**：Capture 不缓存、不比对历史输入；同一内容重复采集会再次经过 Formation，
   由**冻结的** Phase 4 精确重复检测决定是复用还是写入（`status="duplicate"`）。
4. **`captured` 的语义是"接收"**：低价值输入 `captured=true` 但零写入；判断是否落库请看
   `status` / `memories_created` / `sources_created`。
5. **失败以异常呈现**：Formation/LLM 失败按既有类型化错误上抛（CLI 退出码 3 / 配置错误 2），
   `CaptureResult` 只在管线成功返回时存在——这样不会出现"半成功"的结果对象。
6. **元数据落点有限**：capture 元数据只随 Formation 的既有约定进入 `Source.metadata`（且只有形成 Source 时）；
   未形成 Source 的输入不会有任何持久化痕迹（这正是不写 Source 原则的代价，符合设计）。
7. **没有批量采集 API**：一次 `capture()` 处理一条输入；批量导入属于后续阶段。
8. **`captured_at` 只在形成 Source 时可见**：Memory 行沿用 Phase 1 的 `created_at`（形成时间），
   Capture 时间不会写进 Memory（避免修改已冻结的 Memory Schema）。

---

## 9. 完成标准对照（规格书 §十五）

| 完成标准 | 证据 | 结果 |
| --- | --- | --- |
| RawInput 已实现 | `CaptureRequest`（6 字段 + 边界校验）+ `to_raw_input()` 适配 Phase 2 `RawInput`（§2） | ✅ |
| Capture Service / API 已实现 | `CaptureService.capture()` / `capture_request()`，支持 `content`/`title`/`source_type`/`metadata`/`url`/`captured_at`/`dry_run` | ✅ |
| Capture → Formation 接通 | §3 数据流 + 测试 1/3/4/5/6/7/8 + 验收 A/B | ✅ |
| 没有复制 Memory Formation 逻辑 | `capture.py` 无 prompt、无 LLM 调用、无价值判断；只调用 `MemoryFormationService.process()`；`Memory`/`FormationPolicy`/`FormationOutcome` 全部复用 | ✅ |
| 低价值输入行为正确 | 测试 4 + 验收 B（零写入） | ✅ |
| Source 不因 Capture 自动长期保存 | 测试 4/9（`sources=0`）、验收 B | ✅ |
| CLI 可真实运行 | `python -m personal_memory capture …`（§6，含 `--title/--source-type/--url/--json`） | ✅ |
| 自动化测试全部通过 | `Ran 335 tests … OK (skipped=2)` | ✅ |
| Phase 1～4 原有测试全部通过 | 308 → 335，原有 308 全部继续通过 | ✅ |
| 至少一次真实 LLM Capture → Memory 端到端验证 | 验收 A + Test 10 | ✅ |
| 不引入下一阶段功能 | §7 清单中无任何实现；无 Importer、无抓取、无 UI | ✅ |

**停止点**：本阶段完成后即停止，不实现文件导入、Chat Importer、URL Importer、PDF、UI。
