# 阶段 2：Memory Formation（记忆形成）

> 状态：**已完成并实测通过**（2026-10-04，package 0.2.0，prompt `memory-formation-v1`）
> 唯一目标：让系统接收一段原始输入，通过可配置 LLM 判断哪些信息值得进入长期记忆，并把有价值的信息提取为符合阶段 1 Memory Schema 的 Memory。
> 复用的阶段 1 资产：`Source` / `Memory` / `memory_sources` / SQLite schema / 校验 / `MemoryRepository`。**数据库结构与 Memory 模型未修改。**

---

## 1. 新增文件与唯一一处阶段 1 修改

| 文件 | 作用 |
| --- | --- |
| `personal_memory/llm.py` | LLM 适配层：配置解析、传输、重试、严格 JSON 提取、错误映射、密钥脱敏 |
| `personal_memory/prompts.py` | 版本化 prompt：价值判断标准、information_origin 语义、JSON 契约 |
| `personal_memory/extraction.py` | Memory Formation 服务：RawInput → LLM → 严格校验 → 策略 → 原子落库 |
| `scripts/phase2_real_llm_check.py` | 真实 LLM 端到端验证脚本（可重复运行，不打印密钥） |
| `config/llm.example.json` | LLM 配置示例（真实配置 `config/llm.json` 已 gitignore） |
| `tests/llm_fakes.py`、`tests/test_llm.py`、`tests/test_extraction.py`、`tests/test_formation_atomicity.py`、`tests/test_llm_real.py` | Mock LLM 自动化测试 + 可选真实 LLM 测试 |

**对阶段 1 的唯一修改（最小必要）**：`personal_memory/store.py` 新增

* `MemoryRepository.transaction()`：一个跨越多次写入的事务作用域；
* `MemoryUnitOfWork`：在**同一个连接**上执行 `create_source` / `create_memory` / `link`；
* 把单行写入抽成私有 `_insert_source` / `_insert_memory` / `_insert_link`，公开 API 行为不变（阶段 1 的 84 个测试全部原样通过）。

原因：阶段 1 的每个仓库方法各开自己的事务，无法满足阶段 2「Source + Memory + memory_sources 必须原子写入」的要求。没有改 schema、没有改数据模型、没有改既有公开行为。
另有 `personal_memory/__init__.py`（导出新增对象、版本升到 0.2.0）、`cli.py`（新增 `form` 命令）、`tests/test_cli.py`（版本断言 0.1.0 → 0.2.0，仅断言）。

---

## 2. LLM Adapter 如何工作

```
Memory Formation (extraction.py)
        │  LLMRequest(system/user messages, metadata)
        ▼
   LLMClient（llm.py）── 传输重试 + 严格 JSON 提取
        │
        ▼
   Transport（HttpTransport / 测试注入的 ScriptedTransport）
        │  POST {base_url}/chat/completions（OpenAI 兼容）
        ▼
   已配置模型（本机：deepseek-flash @ https://api.deepseek.com）
```

* **配置来源与优先级**：显式参数 > 环境变量 > JSON 配置文件 > provider 预设。
  * provider：`deepseek` / `openai` / `custom`（`custom` 必须显式给 `base_url`）
  * 模型、base_url、超时、max_tokens、temperature、json_mode、重试次数全部可配置
  * **API Key**：`api_key_env` 指定的变量（默认 `PERSONAL_MEMORY_LLM_API_KEY`）→ provider 约定变量（`DEEPSEEK_API_KEY` / `OPENAI_API_KEY`）→ 配置文件字段。**代码与测试中没有任何真实密钥**。
* **脱敏**：`LLMConfig.__repr__` / `safe_summary()` / `as_public_dict()` 只报告 `api_key=<set|missing>`；
  provider 的错误响应体在进入异常信息前先经 `redact_secrets()` 清洗（即使某个代理把我们的
  `Authorization` 头回显在错误里，密钥也会被替换为 `<redacted>`）。
* **密钥优先级**：显式参数 > 环境变量 > 配置文件。配置文件中写的 `api_key` **不会**覆盖环境变量，
  避免 key 轮换被一个旧配置文件静默压过（独立审查发现并修复）。
* **不跟随重定向**：默认 opener 使用 `NoRedirectHandler`，遇到 301/302/303 直接以 `LLMRequestError` 失败，
  绝不允许把 `Authorization: Bearer <key>` 转发到重定向指向的其它主机（独立审查发现并修复）。
* **重试分工**：
  * `LLMClient` 只重试**传输类**失败（超时、连接失败、429、5xx），指数退避，`max_transport_retries` 可配；401/400 等立即失败。
  * 内容类问题（不是 JSON、空响应、被截断、schema 非法）由 `MemoryFormationService` 带着**具体校验错误**重试（默认 2 次），仍失败则抛 `ExtractionValidationError`（明确失败）。
* **真实模型特性（实测）**：`deepseek-flash` 是推理模型，响应含 `message.reasoning_content`（不参与结果）与 `message.content`；用 `response_format={"type":"json_object"}` 得到纯 JSON；`max_tokens` 必须留出推理预算（32 token 时会 `finish_reason=length` 且 content 为空）→ 适配层把 `finish_reason=length` 判为 `TruncatedResponseError`，**绝不猜半个 JSON**。

---

## 3. Memory Formation 数据流

```
raw input（RawInput：临时对象，绝不自动持久化）
   │  1. 构造时校验：content 非空、url 形如 http(s)、source_type 属枚举
   ▼
LLM analysis（prompts.py 的 system+user prompt，json_mode）
   │  2. 返回 JSON 对象
   ▼
value judgment（worth_remembering / reason，由 prompt 决定；模型判断）
   │  3. 严格 schema 校验：ExtractionResult.parse（字段类型、枚举、范围、未知字段、布尔一致性）
   │     非法 → 带错误反馈重试 → 仍非法 → ExtractionValidationError（不写库）
   ▼
Memory extraction（MemoryDraft → Memory.create(...)，阶段 1 校验再兜底）
   │  4. FormationPolicy：条数上限、agent_inference 置信度门槛、status 设定
   ▼
structured validation（Memory.create 的 __post_init__ + SQLite CHECK/UNIQUE/FK）
   │
   ▼
persist only when valuable（worth_remembering=false 或全被策略丢弃 → 零写入）
```

关键不变量（均有测试）：

* `worth_remembering = false` → **不开写事务**（测试用 mock 断言 `create_source`/`create_memory` 从未被调用）。
* `worth_remembering = true` 但没抽到 Memory → 视为非法答案并重试。
* 禁止非法 Memory 静默入库：任何校验失败都在写库前抛出。
* LLM 只通过 `LLMClient` 访问，extraction 里**没有一条 SQL**。

---

## 4. 价值判断如何实现

价值判断 = **prompt 中的判定标准**（模型执行）+ **代码中的确定性策略**（不许模型越界）。prompt 版本 `memory-formation-v1`，内容包含：

* 立场：从「未来长期陪伴用户的 Agent」角度判断，而不是摘要或有趣度；
* 高价值清单：稳定专业知识、反复学习的概念、长期目标相关信息、明确偏好、重要经验、项目决策、会影响未来 Agent 行为的信息；
* 低价值清单：一次性闲聊、寒暄、短期状态、完全重复、与未来交互无关；
* 两条显式警告：**「文本很长」≠「值得记忆」**、**「模型觉得有趣」≠「对用户长期有用」**；
* `importance`（对用户的长期价值）与 `confidence`（提取/推断的正确把握）分离。

代码侧的确定性策略（`FormationPolicy`，防止模型自由发挥）：

| 规则 | 默认 | 作用 |
| --- | --- | --- |
| `max_memories` | 5 | 单次输入最多形成 5 条 Memory，超出丢弃并记录原因 |
| `agent_inference_min_confidence` | 0.6 | 纯推断且置信度不足 → **不落库**（记录 dropped 原因） |
| `agent_inference_status` | `pending` | 推断类记忆一律以「提案」状态入库，不冒充事实 |
| `keep_source` | `when_required` | Source 持久化策略（见 §5） |

`information_origin` 严格沿用阶段 1 三种取值：`user_explicit` / `source_content` / `agent_inference`；prompt 明确「推断不得伪装成用户明确表达；只有推断且无直接依据时 confidence ≤ 0.5」。

---

## 5. Source 为什么保存或不保存

原则（阶段 2 第三章）：**用户输入首先是临时对象，不自动成为长期 Source。**

判定顺序（`FormationPolicy.decide_sources`）：

1. 没有保留任何 Memory → **不保存 Source**（低价值输入零写入）。
2. `keep_source="never"` → 不保存；`"always"` → 保存。
3. **硬规则（任何模式都不能绕过）**：只要保留的 Memory 中有 `requires_source=true`，就必须保存 Source 并建立关联。
   独立审查发现早期实现里 `--keep-source never` 能把这种 Memory 落成「无来源」状态，已修复并有回归测试
   （`test_keep_source_never_cannot_delete_a_required_source`）。
4. 默认 `when_required`：
   * 任一 Memory 标记 `requires_source=true` → **必须保存 Source**（见第 3 条硬规则）；
   * 或者任一 Memory 的 `information_origin = source_content`（结论来自资料本身）→ **保存 Source**；
   * 否则（用户明确表达且自洽）→ 只保存 Memory，不保存 Source。
5. `keep_source="never"` 的语义是「不保存非严格必需的 Source」：`source_content` 不再触发保存，
   但 `requires_source=true` 仍然强制保存；`keep_source="always"` 则总是保存。

保存的 Source 使用阶段 1 的 `content_hash` 去重：同一段输入第二次形成记忆时**复用已有 Source**（`source_reused=true`），不会产生重复原文。Source 的 `metadata.formation` 记录 `prompt_version` / `model` / `formed_at`，形成可审计链路。

---

## 6. 事务与失败处理

```
MemoryFormationService._persist
   └─ repo.transaction()  →  BEGIN IMMEDIATE（一个连接）
         ├─ create_source（可选）
         ├─ create_memory × N
         └─ link × N
      COMMIT  或  任何异常 → ROLLBACK
```

失败注入测试（`tests/test_formation_atomicity.py`）验证：link 失败 / 第 2 条 Memory 失败 / Source 失败时，`sources`、`memories`、`memory_sources` 计数全部回到 0（原始 SQL 复核），且数据库在回滚后仍可继续正常使用。

---

## 7. 测试与实测结果

```powershell
python -m unittest discover -s tests -t . -v      # 163 tests（含 1 个可选真实 LLM 测试默认 skip）
```

阶段 2 要求的 9 项验收全部由 Mock LLM 测试覆盖（`tests/test_extraction.py`）：

| 需求 | 测试 | 结果 |
| --- | --- | --- |
| Test 1 高价值输入 | `ValueJudgmentTest::test_1_high_value_input_forms_a_memory` | PASS |
| Test 2 低价值输入 | `::test_2_low_value_input_forms_nothing_and_persists_no_source` | PASS |
| Test 3 知识类型 | `::test_3_professional_knowledge_is_typed_and_traceable`（knowledge + source_content + Source 关联） | PASS |
| Test 4 用户明确表达 | `::test_4_user_explicit_statement_keeps_its_origin` | PASS |
| Test 5 Agent inference | `::test_5_low_confidence_inference_is_not_stored` / `::test_5b_supported_inference_is_stored_as_a_pending_proposal` | PASS |
| Test 6 非法 LLM 输出 | `InvalidOutputTest::test_6_importance_out_of_range...` / `test_6b_illegal_type` / `test_6c_illegal_origin...` | PASS |
| Test 7 LLM API 失败 | `::test_7_api_failure_propagates_and_writes_nothing` | PASS |
| Test 8 多 Memory | `::test_8_one_input_forms_several_memories_linked_to_one_source` | PASS |
| Test 9 无价值不落库 | `::test_9_no_value_input_leaves_existing_data_untouched` + 原子性测试中的「零写入」断言 | PASS |

其他覆盖：配置优先级与脱敏（`tests/test_llm.py`，41 个用例，含密钥优先级、错误体清洗、重定向拒绝、
socket 异常包装、模型输出回显密钥、base_url 脱敏）、
HTTP 线格式与重试分类、严格 JSON（散文/数组/截断全部拒绝）、重试后成功并回传校验反馈、不一致 payload、
策略上限、dry-run、Source 复用、provenance、跨连接可读性、原子回滚、
以及「Phase 1 上限（title/tags）在解析阶段就被拦下并触发重试」

---

## 8. 真实 LLM 端到端结果（实测）

命令：`python scripts/phase2_real_llm_check.py --db data/phase2-real.db --reset`
模型：`deepseek-flash`（provider `deepseek`，`https://api.deepseek.com`），prompt `memory-formation-v1`。
证据：[`docs/phase2-real-llm.json`](../docs/phase2-real-llm.json)。

| 用例 | 输入 | 观察结果 |
| --- | --- | --- |
| 高价值专业知识 | RAG 的定义与作用 | `worth_remembering=true`，1 条 Memory：`knowledge` / `source_content` / importance 0.60 / confidence 0.90 / status `active` / tags `[rag, retrieval, llm, 幻觉]`；Source 已持久化并建立关联；counts `0 → 1/1/1`；1 次尝试；2.3s；1852 tokens（含 237 推理 tokens）；新连接重新读出完全一致 |
| 低价值闲聊 | 「今天下午喝了一杯奶茶。」 | `worth_remembering=false`，0 条 Memory，**Source 不持久化**，counts 保持 `1/1/1` 不变；0.8s；1412 tokens |

脚本内置断言：若密钥出现在证据 JSON 中则拒绝写盘（实测证据中不含任何 key 形态字符串）。

---

## 9. 当前已实现 / 未实现

**已实现（阶段 2 验收项）**：LLM Adapter（可配置 provider/model/URL/key，密钥脱敏与传输重试）、结构化 Extraction（严格 schema 校验 + 重试/明确失败）、Memory Value Judgment、Memory Schema Validation、information_origin、importance/confidence、有价值信息形成 Memory、低价值输入不形成 Memory、Source 选择性持久化、Source-Memory 关联、事务与失败回滚、Mock LLM 自动化测试、真实 LLM 端到端验证。

**仍未实现（本阶段明确禁止）**：Memory Search、FTS5、Embedding、Vector DB、RAG、QA、Web UI、URL 抓取、PDF、Markdown/TXT 批量导入、Chat 文件解析、MCP、Agent Tool Calling、主动聊天、Knowledge Base 1.0。

---

## 10. 独立审查与修复（阶段 2）

独立对抗式审查（只读子代理）报出 4 个真实缺陷，均已修复并有回归测试：

| # | 缺陷 | 影响 | 修复 |
| --- | --- | --- | --- |
| C1 | 配置文件里的 `api_key` 优先于环境变量（与文档相反） | key 轮换被旧配置文件静默压过 | 密钥优先级改为 显式 > 环境变量 > 配置文件 |
| C2 | provider 错误响应体被原样拼进异常信息 | 若上游回显 `Authorization`，密钥会进入日志 | 错误体经 `redact_secrets()` 清洗（密钥与 `Bearer …` 均替换） |
| C3 | 默认 `urlopen` 跟随 302，并把 `Authorization` 转发到新主机 | 凭据可能被重定向目标窃取 | 默认 opener 改为 `NoRedirectHandler`，重定向直接失败 |
| C4 | `--keep-source never` 可把 `requires_source=true` 的记忆落成无来源 | 违反「无法脱离原文解释就必须保留 Source」 | 该规则升级为硬规则，任何模式不可绕过 |

审查另确认：阶段 1 的 84 个测试全部通过、SQLite schema 与 `docs/schema.sql` 完全一致且仍只有迁移 v1、
extraction 无任何 SQL、原始 SQL 绕过仍被 CHECK/UNIQUE/FK 拦下、公开密钥不出现在任何文件中。

第二轮独立复核（针对上述修复）确认 C1–C4 全部通过，并报出 4 个新的同类缺口，均已修复：

| # | 缺口 | 影响 | 修复 |
| --- | --- | --- | --- |
| NC1 | `http.client` 在读取状态行时抛出的 `ConnectionAbortedError` 等 socket 异常未包装 | 无类型的 OSError 逃逸（不重试）；重定向回归测试约 1/9 概率失败 | `HttpTransport` 捕获 `OSError` 并包装为可重试的 `LLMRequestError`；测试改为先读完请求体再响应 |
| NC2 | 模型 `message.content` 回显密钥时，`extract_json_object` 的报错文本未脱敏 | 密钥可能出现在 CLI stdout / 日志 | `extract_json_object(text, secret=…)`，`complete_json` 传入密钥并清洗片段 |
| NC3 | `URLError.reason` 与异常信息中的 `base_url` 未脱敏 | 代理/网关在错误信息中回显密钥时可能泄漏 | 两处均经 `redact_secrets()` 清洗，bearer 正则扩展到 `+/=` 字符 |
| NC4 | `_persist` 在事务外做 `content_hash` 查重（TOCTOU） | 极端并发下第二次写入会因 UNIQUE 冲突整体回滚（无脏数据，但记忆丢失并报错） | 查重移入同一事务（`MemoryUnitOfWork.find_source_by_content_hash`），共享写锁 |

复核同时给出仍未覆盖的残余面（已记录为限制）：`redact_secrets` 是逐字节替换 + bearer 正则，
对「被换行拆开 / base64 / percent-encoded」的密钥回显不保证覆盖（真实回显通常是逐字节的，已被覆盖）。

## 11. 已知限制

1. **跨会话去重未实现**：只做「同一段输入 → 同一 Source」的 `content_hash` 复用；判定「这条新记忆与库里已有记忆重复」需要检索能力（阶段 3+），当前不做，prompt 只在单次输入内部去重。
2. **Source 生命周期极简**：只实现「要不要保存」，没有归档、过期、合并、清理策略；删除 Memory 不影响 Source（阶段 1 行为）。
3. **并发写入**：同一段输入被两个进程同时形成记忆时，第二个可能拿到 `DuplicateContentHashError`（事务与唯一约束保证不会写坏，但没有针对该冲突的自动合并重试）。
4. **模型判断不可完全约束**：价值判断质量取决于 prompt 与模型；`worth_remembering` 的最终解释权在模型，代码只能约束结构（类型/范围/枚举/一致性）与「推断不得高置信」等边界，不能保证语义正确。
5. **成本**：真实调用按 token 计费；自动化测试全部使用 Mock，不产生费用；真实验证脚本每次 2 次调用（实测约 3.3k tokens）。
6. **reasoning 模型预算**：`max_tokens` 默认 2048；推理模型会消耗推理 token，极长的记忆抽取可能触发 `TruncatedResponseError`（此时明确失败或重试，不会写半个结果）。
7. **单一 provider 协议**：实现的是 OpenAI 兼容的 `/chat/completions`；Anthropic 原生 Messages API 未实现（`provider=custom` + 兼容网关可用）。
8. **密钥管理**：密钥只从环境变量或 gitignore 的配置文件读取，不落盘到代码与测试；环境变量优先于配置文件，
   便于轮换。项目本身不做密钥加密、轮换或审计（这些属于运维层）。
9. **不跟随重定向**：若 provider/网关对 `{base_url}/chat/completions` 返回 3xx，调用会明确失败而不是自动跟随——
   这是有意的安全取舍（凭据不跨主机转发）；因此 `base_url` 必须写最终端点。
10. **脱敏的边界**：`redact_secrets` 是逐字节替换 + bearer 正则，能覆盖 provider/代理逐字节回显密钥的情况，
    但**不保证**覆盖换行拆分、base64、percent-encoding 等变形（不做通用 DLP）。
11. **并发**：同一段输入被两个进程同时形成记忆时，由于查重与插入在同一事务（`BEGIN IMMEDIATE`）内完成，
    第二个进程会复用它看到的 Source；如果它抢在第一个进程提交前进入，则会在 UNIQUE 冲突上明确失败并回滚，
    不会产生半成品或脏数据。
