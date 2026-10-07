# Knowledge Base 1.0 — Phase 3：Chat Importer（聊天记录导入层）

本文件是 KB 1.0 第三阶段的完整说明与实测证据。

| 系统 | 状态 |
| --- | --- |
| Personal Memory System v0.1（Phase 1–4） | **已冻结**，本阶段未改 |
| KB 1.0 Phase 1（Capture）/ Phase 2（TXT+Markdown） | 已冻结，本阶段**只复用** |
| KB 1.0 Phase 3：Chat Importer | ✅ 本阶段 |
| URL / PDF / OCR / 全平台专用适配器 / UI / 自动摘要·chunk / 聊天监控 | ❌ 未实现（本阶段明确禁止） |

生成时间：2026-10-04；解释器 `D:\python\python.exe`（3.13.2）；SQLite 3.45.3。

---

## 1. 新增/修改的文件

**新增**

| 文件 | 作用 |
| --- | --- |
| `personal_memory/importers/chat.py` | 核心：`ChatRole` / `ChatMessage` / `ChatConversation` / `ParsedChat`、角色文本与 JSON 解析、标题规则、错误层次、`ChatImporter`、`ChatImportResult`、`import_chat_file` |
| `personal_memory/importers/chat_adapters.py` | Provider 适配层：`ChatAdapter` 协议 + 两个 provider-neutral 适配器（角色文本 / JSON）+ 注册表与选择规则 |
| `tests/test_chat_import.py` | 39 个测试：解析 12 项、元数据 5 项、集成 13 项 + 长对话/隐私/适配器接缝等边界 |
| `scripts/kb1p3_chat_acceptance.py` | 验收场景 A/A2/B–F（真实 CLI；A/A2/B 走真实 LLM，C/D/E/F 不调用模型） |
| `docs/KB1-PHASE3.md` | 本文件 |
| `docs/kb1p3-chat-acceptance.{json,txt}` | 验收原始证据（已确认不含对话正文） |
| `data/kb1p3-fixtures/*.txt,*.json` | 验收用的**虚构**聊天 fixture（角色文本 / 低价值 / 知识型 / JSON / 超长） |

**修改（必要且最小）**

| 文件 | 修改 | 原因 |
| --- | --- | --- |
| `personal_memory/importers/files.py` | 把"存在性→类型→扩展名→大小→解码"抽成模块级 `read_text_file()` / `decode_utf8()`，文件导入器改为调用它 | 规格书 §十一要求"大小上限机制优先复用"；聊天导入器复用同一套校验顺序与同一组类型化错误（行为不变，P2 的 28 个测试继续通过） |
| `personal_memory/importers/__init__.py` | 导出聊天 API（`ChatImporter` / `ChatConversation` / 解析函数 / 适配器 / 错误） | 公开 API |
| `personal_memory/cli.py` | 新增 `import-chat`（`--title/--provider/--conversation-id/--format/--max-bytes/--dry-run/--no-quality-check/--keep-source/--config/--json`）；`version` 的 `phase` 指向 KB 1.0 Phase 3 | 真实可跑入口（规格书 §十六） |
| `personal_memory/__init__.py` | 导出聊天 API；版本 0.6.0 → 0.7.0，`__phase__ = "kb-1.0-phase-3"` | 公开 API 与版本 |
| `tests/test_cli.py` | 版本断言 + 3 个 `import-chat` CLI 用例 | 版本确实变了；新命令需要离线路径测试 |

**命名注意（API 兼容）**：`personal_memory.ChatMessage` **仍然是 Phase 2 的 LLM 请求消息**（保持向后兼容）；
本阶段的聊天消息结构在命名空间 `personal_memory.importers`（`from personal_memory.importers import ChatMessage`）。
顶层导出的是无冲突的名字（`ChatRole` / `ChatConversation` / `ChatImporter` / `ChatImportResult` / `parse_role_text` / `parse_chat_json` / `import_chat_file` / 聊天错误类型）。

**未改（指纹证明）**：`capture.py` `F33696E7C307F6CC`、`models.py` `6F892D6888319836`、`store.py` `0AC465BC62958D3D`、
`extraction.py` `147D50D908EFA216`、`retrieval.py` `FF35E0117691437D`、`lifecycle.py` `A9BF67FBBE7C5C60`、
`quality.py` `4C6D3EA3081E84C4`、`errors.py` `B62C84CBAEB39AED`、`prompts.py` `7130F83C7E21856E`、
`llm.py` `E3D1DCA0FB619130`、`db.py` `F6EE1E73E1C747B9`、`docs/schema.sql` `160069D9C0D8B396`，
以及 P1/P2/阶段 1–4 的既有测试文件 —— 本阶段实测指纹与本阶段开始前**逐一相同**。数据库 schema 仍为 v2，**没有新增 migration**。

---

## 2. ChatMessage / ChatConversation 最终结构

```python
ChatMessage(role, content, timestamp=None)          # frozen dataclass
ChatConversation(conversation_id, messages, title=None, provider=None,
                 started_at=None, ended_at=None, metadata={}, title_source="untitled")
```

| 结构 | 字段 | 校验（`__post_init__`，失败抛 `ValidationError`） |
| --- | --- | --- |
| `ChatMessage` | `role` | 必须是已知角色（枚举或别名），未知角色 → `UnsupportedChatRoleError`（同时是 `ChatParseError` 与 `ValidationError`） |
| | `content` | 必须是非空字符串；规范化 = `normalize_content`（LF、NFC、去行尾空白、整体 strip）——**不改写、不摘要、不删减** |
| | `timestamp` | `None` 或合法 ISO-8601 |
| `ChatConversation` | `conversation_id` | 非空字符串（文件没给时由渲染文本的 sha256 前 16 位派生，**稳定**） |
| | `messages` | 非空序列，元素必须是 `ChatMessage`；**顺序即输入顺序** |
| | `title` | `None` 或非空字符串 |
| | `provider` | `None`（未知，**不猜**）或非空字符串 |
| | `started_at` / `ended_at` | `None` 或合法 ISO-8601；文件未给时取首/末消息时间戳（没有就是 `None`） |
| | `metadata` | mapping（JSON 的 `metadata` 原样保留） |
| | `title_source` | `explicit` / `export` / `first_user_message` / `untitled`（记录标题来自哪条规则） |

角色：`user` / `assistant` / `system` / `tool` / `developer`（后三者保留并明确标注）。
`ChatConversation.roles()` 返回按首次出现顺序去重的角色；`render_role_text()` 生成交给 Capture 的角色明确文本。

---

## 3. 支持的输入格式

| 优先级 | 格式 | 识别规则 | 说明 |
| --- | --- | --- | --- |
| 1 | **角色文本** | 扩展名 `.txt` / `.md` / `.markdown` / `.chat`，或内容不是 JSON | `[User]` / `[USER]:` / `## User` / `### Assistant:` 等整行角色头 |
| 2 | **结构化 JSON** | 扩展名 `.json`，或文本以 `{` 开头 | 文档化的 provider-neutral 结构（见下） |
| — | Provider 适配器 | `ChatImporter(adapters=[...])` / `register_adapter()` | **接口已就位**，加一个平台 = 加一个适配器类 |

**Provider-neutral JSON（文档化格式）**：

```json
{
  "conversation_id": "conv-001",
  "title": "Agent 学习",
  "provider": "generic-exporter",
  "started_at": "2026-10-04T10:00:00Z",
  "ended_at": "2026-10-04T10:01:00Z",
  "metadata": {"any": "extra"},
  "messages": [
    {"role": "user", "content": "我最近开始学习 Agent。", "timestamp": "2026-10-04T10:00:00Z"},
    {"role": "assistant", "content": "Agent 可以理解为……", "timestamp": "2026-10-04T10:00:05Z"}
  ]
}
```

顶层未知字段被忽略（格式可扩展）；`messages[i]` 必须是对象且带 `role` 与 `content`；
`content` 必须是字符串（OpenAI 式"block 数组"需要 provider 适配器，本层明确拒绝而不是猜）。

**关于平台专用格式（规格书 §四/§五/§二十的要求）**：本阶段**没有**实现 ChatGPT / DSH / Codex / Discord 专用适配器。
先按要求检查了当前工作区：`D:\DSH-worlp` 下**没有任何**导出的聊天样本（没有 `*.json` 对话导出，也没有 `*.jsonl`）。
机器上确实存在 `~/.dsh/sessions/`、`~/.codex/sessions/`（以及 `session_index.jsonl`），但那是**私有的实时运行状态**，
不是工作区提供的导出样本，因此没有从中推断任何格式（也没有读取它们的内容）。
`ChatImporter.load()` 遇到 ChatGPT 常见的 `mapping` 结构会明确报错（`messages` 缺失），不会猜。

---

## 4. 角色解析规则

1. **角色头必须是整行**：`[Label]`（可带尾部 `:`）或 Markdown 标题（`#`~`######`）且标题文字是已知角色。
   行内出现的 `[USER]`、`[1]` 之类**不会被当成角色头**，它是正文内容。
2. **已知角色与别名**（大小写不敏感）：`user/human/me/用户/我`、`assistant/ai/bot/model/gpt/助手/系统助手`、
   `system/sys/系统`、`tool/function/工具`、`developer/dev/开发者`。
3. **未知角色不猜**：整行 `[Moderator]`、`role: "wizard"` → `UnsupportedChatRoleError`，错误里列出可用角色。
   绝不把未标注/未知角色的内容算到上一位说话者或 `user` 头上（这正是规格书 §二 的硬性要求）。
4. **角色头前**只允许"一行标题"或空白；多行前言会报错（避免静默丢弃）。
5. **空消息**（整条只有空白）被丢弃；全部为空 → `EmptyConversationError`。
6. **消息顺序**严格保持；**时间戳**只在 JSON 中出现（角色文本本身没有时间字段，不会凭空生成）。
7. 角色标记固定为大写方括号：`[USER]` / `[ASSISTANT]` / `[SYSTEM]` / `[TOOL]` / `[DEVELOPER]`，
   一条消息一个标记，且**只按该消息真实角色输出**。

---

## 5. Chat → Capture → Formation 数据流

```text
chat file (.txt/.md/.chat 角色文本 或 .json)
   │ ① read_text_file()：存在性/类型/扩展名/大小/UTF-8 解码（与 P2 文件导入器同一套机制与错误）
   ▼
ChatAdapter.parse()                        chat_adapters.py
   │ ② 角色文本解析或 JSON 解析 → ParsedChat(messages, id, title, provider, 时间)
   ▼
ChatConversation                            chat.py
   │ ③ 校验 + 顺序 + 标题规则 + 稳定 conversation_id + provider(未知=None)
   │ ④ render_role_text()：`[USER]\n…\n\n[ASSISTANT]\n…`
   ▼
CaptureRequest(source_type=chat, content=角色明确文本, metadata=聊天元数据)   KB P1（未改）
   ▼
CaptureService.capture_request()            KB P1（未改）
   ▼
MemoryFormationService.process()            冻结：价值判断 / 提取 / FormationPolicy / Phase 4 质量闸门
   ▼
MemoryRepository（Source + Memory + memory_sources 原子写入）   冻结
   ▼
ChatImportResult(conversation, capture_result, import_status)
```

* `chat.py` / `chat_adapters.py` 中**没有** `sqlite3`、没有 SQL 动词、没有 `store` 导入；
  `load()` 签名里没有 repository，`import_conversation()` 只接受 `CaptureService`（传 repository 抛 `ValidationError`）
  —— 测试 `test_28_importer_never_touches_sqlite` 断言这几点。
* 不复制 Formation 逻辑：`worth_remembering` / `importance` / `confidence` / `information_origin` /
  Source 策略 / Phase 4 质量闸门全部由既有代码决定；Importer 从不写 `information_origin` 或任何价值字段。
* CLI 的 `import-chat` 与 `capture` / `form` / `import-file` 使用同一个 Formation（含同一质量闸门）；
  `--keep-source` 与 `form` 是同一个 Phase 2 策略开关。
* 一次聊天可以产生多条 Memory，全部通过**既有** `memory_sources` 关联到同一个 chat Source（不新增关系表）。
* 去重完全复用现有机制：Source `content_hash` 复用 + Phase 4 精确重复/保守冲突策略（测试 30 实测）。

---

## 6. 如何避免 `user_explicit / source_content / agent_inference` 混淆

Importer 侧的保证（结构性、可测）：

1. 每条消息的正文**只**出现在它自己角色标记之下；行内 `[USER]` 之类字样不会新建用户块（测试 23，含对抗样本）。
2. Importer **不写入**、也不建议任何 `information_origin` / `importance` / `confidence`；
   `CaptureRequest` 里根本没有这些字段（测试 24a）。
3. `assistant` 内容不会因为"在聊天里"就被当作"用户明确表达"——判断权完全在 Phase 2 的 Formation prompt：
   用户原话 → `user_explicit`；助手提供的知识 → `source_content`；助手的推断 → `agent_inference`
   且继续受 Phase 2 置信度策略约束（低于阈值不落库，测试 25 用 chat 路径复验）。
4. 真实 LLM 实测（本次验收，`deepseek-flash`）：

| 输入内容 | 实际落库结果 |
| --- | --- |
| 用户自述目标/计划（agent-chat） | 3 条 `profile` / **`user_explicit`** / active |
| 助手解释的检索排序知识（knowledge-chat，`--keep-source always`） | 1 条 `knowledge` / **`source_content`** / active |

即：同一套管线里，用户的话和助手的话被正确区分，助手内容没有被提升为 `user_explicit`。

---

## 7. Source metadata

Capture 时写入（`ChatConversation.to_capture_request()`）：

```json
{
  "captured_from": "chat",
  "provider": "generic-exporter",          // 未知时为 null（不猜）
  "conversation_id": "conv-001",
  "message_count": 2,
  "roles": ["user", "assistant"],
  "started_at": "2026-10-04T10:00:00Z",    // 文件没给时间时为 null
  "ended_at": "2026-10-04T10:01:00Z"
}
```

实测（验收 A2 的 Source）：`content_chars=139`、`role_marker_sequence=["[USER]","[ASSISTANT]"]`、
`metadata` 键 = `captured_from / conversation_id / ended_at / formation / message_count / provider / roles / started_at`
（`formation` 块由 Phase 2 追加：prompt 版本、模型、形成时间）。

**不保存**本机路径、不保存对话正文到 metadata（正文只作为 Source 的 `content`，且只在 Formation 需要时保存）。

**隐私说明（必须明确）**：聊天正文在真实 LLM Formation 时**会发送给当前配置的模型服务**；
本地 SQLite 存储不代表本地推断。CLI 人类可读输出会打印这一行提示。

---

## 8. 测试结果

```text
cd D:\DSH-worlp\personal-memory-system
python -m unittest discover -s tests -t . -v
Ran 409 tests ... OK (skipped=2)
```

* Phase 1–4 + KB Phase 1–2 的 367 个测试**全部继续通过**（`importers/files.py` 的抽取式重构由 P2 的 28 个测试守住）。
* KB 1.0 Phase 3 新增 **42** 个测试：`tests/test_chat_import.py` 39 + `tests/test_cli.py` 3。
* 2 个 skip = 两个可选真实 LLM 测试；聊天导入测试全部离线运行。

规格书 §十七 的 30 项要求逐条对应：

| # | 要求 | 测试方法 | 结果 |
| --- | --- | --- | --- |
| 1 | 正常角色文本 | `ChatParsingTest::test_1_role_text_parsing` | PASS |
| 2 | Markdown 风格角色文本 | `…::test_2_markdown_style_role_text`（含"非角色标题留在正文"与整行角色头规则） | PASS |
| 3 | JSON 对话 | `…::test_3_json_conversation` | PASS |
| 4 | user / assistant 正确区分 | `…::test_4_user_and_assistant_are_distinguished` | PASS |
| 5 | system / tool / developer 与未知角色 | `…::test_5_system_tool_developer_and_unknown_roles`（未知 → `UnsupportedChatRoleError`，绝不默认成 user） | PASS |
| 6 | 消息顺序保持 | `…::test_6_message_order_is_preserved`（角色文本 12 条 + JSON 6 条） | PASS |
| 7 | 时间戳保留 | `…::test_7_timestamps_are_preserved`（含 started/ended 推导与显式覆盖、角色文本无时间戳） | PASS |
| 8 | 空消息处理 | `…::test_8_blank_messages_are_dropped`（丢弃 / 全空报错 / 直接构造空消息报错） | PASS |
| 9 | 非法 JSON | `…::test_9_invalid_json_is_reported` | PASS |
| 10 | 缺 role | `…::test_10_missing_role_is_reported`（错误定位到 `messages[0].role`） | PASS |
| 11 | 缺 content | `…::test_11_missing_content_is_reported`（含 block 数组 → 提示需要 adapter） | PASS |
| 12 | 不合法 timestamp | `…::test_12_invalid_timestamp_is_reported` | PASS |
| 13 | conversation_id | `ChatMetadataTest::test_13_conversation_id`（文件值 / 显式覆盖 / 派生且稳定） | PASS |
| 14 | title | `…::test_14_title_priority`（显式 > 文件 > 首条用户消息（≤60 字）> "Untitled Conversation"；标题不调用模型） | PASS |
| 15 | provider | `…::test_15_provider`（文件值 / 未知为 None / 显式覆盖 / 进入 metadata） | PASS |
| 16 | message_count | `…::test_16_message_count` | PASS |
| 17 | started_at / ended_at | `…::test_17_started_and_ended_at_reach_the_capture_metadata` | PASS |
| 18 | Chat → Capture | `ChatIntegrationTest::test_18_chat_to_capture`（`source_type=chat`、角色标记、聊天元数据） | PASS |
| 19 | Chat → Formation | `…::test_19_chat_to_formation`（Formation prompt 里能看到两侧原话与角色标记） | PASS |
| 20 | 高价值聊天 → Memory | `…::test_20_high_value_chat_forms_a_memory`（新连接可读 + Phase 3 检索命中） | PASS |
| 21 | 低价值聊天 → Memory=0 | `…::test_21_low_value_chat_forms_no_memory` | PASS |
| 22 | 低价值聊天 → Source 不保存 | `…::test_22_low_value_chat_does_not_persist_a_source`（零写入） | PASS |
| 23 | user explicit 不被误标 | `…::test_23_user_words_stay_under_the_user_marker`（对抗样本：正文里出现 `[USER]` 字样） | PASS |
| 24 | Assistant 内容不自动变 user_explicit | `…::test_24_assistant_content_is_never_promoted_to_user_explicit`（Importer 无 origin 字段；`source_content` 原样落库） | PASS |
| 25 | agent_inference 走 Phase 2 置信度策略 | `…::test_25_agent_inference_keeps_the_phase_2_confidence_policy`（0.4 丢弃 / 0.9 → pending） | PASS |
| 26 | 一个聊天 → 多 Memory | `…::test_26_one_chat_can_produce_several_memories`（3 条不同 type + 1 Source + 3 关联） | PASS |
| 27 | Memory 正确关联 Chat Source | `…::test_27_memories_are_linked_to_the_chat_source`（双向可查、`memory_sources` 计数） | PASS |
| 28 | Importer 不访问 SQLite | `…::test_28_importer_never_touches_sqlite`（静态扫描 + 签名 + 传 repository 被拒） | PASS |
| 29 | Formation 失败零残留 | `…::test_29_formation_failure_leaves_nothing_behind`（无 Source/Memory、索引一致） | PASS |
| 30 | 重复导入复用既有去重 | `…::test_30_duplicate_chat_reuses_the_existing_dedupe`（Source hash 复用 + Phase 4 精确重复） | PASS |

额外覆盖（边界/隐私/接缝）：长对话超限**明确失败且不截断**（限内不丢消息）、默认视图不含任何正文字符、
`include_preview` / `include_content` 为显式选项、错误消息不回显正文、适配器接缝（自定义平台适配器 +
`select_adapter` + 全局注册表不被测试污染）、provider 专用结构不被猜测、`--format` 覆盖、dry-run 零写入、
结构校验（空 messages/非法 provider/非法时间戳/非法 `max_bytes`/空 adapters）。

---

## 9. 真实 Chat → Memory 结果

运行：`python scripts/kb1p3_chat_acceptance.py --reset`（真实 `python -m personal_memory import-chat …` 子进程；
A/A2/B 使用真实 `deepseek-flash`，C/D/E/F 不调用模型；chat fixture 全部为虚构内容）。

| 场景 | 期望 | 实测 | 结果 |
| --- | --- | --- | --- |
| A 高价值聊天（默认策略） | ≥1 条有价值 Memory | `formation_status=persisted`，**3 条 `profile` / `user_explicit` / active**（"用户系统学习 Agent 并重原理"、"用户认可的 Agent 学习三阶段路线"、"每周至少投入十小时学习"）；`title_source=first_user_message`；`provider=null`（未知不猜）；`message_count=3`；`source_count=0`（三条记忆都自洽，Phase 2 策略下无需保留原文——这是既有设计的正确行为） | ✅ |
| A2 高价值聊天（`--keep-source always`） | Source 类型为 `chat`、内容仍能看到角色边界 | 新 Source：`source_type=chat`、`content_chars=139`、`role_marker_sequence=["[USER]","[ASSISTANT]"]`、metadata 含 `captured_from=chat / conversation_id / message_count / provider / roles / started_at / ended_at`；同时形成 1 条 `knowledge` / `source_content` Memory 并关联该 Source | ✅ |
| B 低价值聊天 | Memory = 0、Source = 0 | `status=skipped`、`memory_count=0`、`source_count=0`、`counts` 前后完全相同 | ✅ |
| C JSON 格式（无模型） | 同样的规范化会话 | `conv-accept-001 / "RAG 讨论" / provider=fictional-exporter / 2 条消息 / roles=[user,assistant] / markers=["[USER]","[ASSISTANT]"] / source_type=chat / started_at=09:00:00Z / ended_at=09:00:03Z` | ✅ |
| D 超长对话（无模型） | 明确失败，不静默截断 | 7242 字节的对话在 `--max-bytes 400` 下 → `exit 3` / `FileTooLargeError`，`counts` 不变 | ✅ |
| E 隐私（无模型） | 默认视图不含正文 | 默认 `as_dict()` 的消息键只有 `content_chars / role / timestamp`，探针句不在其中；`include_preview` / `include_content` 才分别暴露预览与全文 | ✅ |
| F 新进程检索 | 新进程能找到聊天产生的 Memory | 新进程 `search Agent --status all` → `total=3`，命中的 3 个 id 与场景 A 的 3 条 Memory **完全一致** | ✅ |

最终库状态：`counts = {memories: 4, sources: 1, memory_sources: 1}`、`status_counts = {active: 4}`、
`index_consistency.consistent = true`；证据文件自检 `body_in_evidence = []`（不含任何对话正文），
API Key 未出现在任何位置。本阶段真实 LLM 调用共 **3 次**（A、A2、B），另有 1 次人工探测不写入证据。

---

## 10. 已知限制

1. **只支持两种输入格式**（角色文本 / provider-neutral JSON），没有平台专用适配器；ChatGPT 的 `mapping`
   等结构会明确报错而不是猜测（工作区没有真实样本，见 §3）。
2. **角色文本不带时间戳**：时间信息只从 JSON 或显式元数据获得；不会从行内文本猜时间。
3. **标题默认取第一条用户消息的前 60 字符**（规格书 §九 要求，不调用模型）——因此标题里可能出现一小段用户原话，
   这是设计行为；如需完全避免，可显式传 `--title` 或用 `--conversation-id` 之外的标题覆盖。
4. **`provider` 未知就是 `null`**：不猜；角色文本永远无 provider，除非调用方用 `--provider` 指定。
5. **超长对话明确失败、不截断**：默认上限与文件导入器一致（1 MiB），
   本阶段不实现自动 chunk / 摘要压缩 / hierarchical memory（规格书 §十一 明确留待以后）。
6. **不做语义去重**：完全复用 Source `content_hash` 与 Phase 4 精确重复；改写后的相似聊天仍可能产生
   `pending` 记录（保守设计，不假装已实现语义去重）。
7. **不解析富内容**：`content` 必须是字符串；图片/附件/工具调用块需要 provider 适配器。
8. **不保存本机路径**；Source metadata 只有 §7 列出的字段。CLI 人类可读输出会打印文件路径（临时显示，不入库）。
9. **默认红acted**：API 默认视图不含任何正文字符（连 ≤60 字预览都需要显式 `include_preview=True`）；
   若调用方打印 `include_content=True` 的结果，隐私责任在调用方。
10. **单会话单次处理**：没有批量、目录扫描、聊天监控、自动读取微信/浏览器/DSH 数据目录。

---

## 11. 当前未实现功能（本阶段禁止）

URL、PDF、OCR、Embedding、Vector DB、Semantic Search、RAG、QA、Web UI、MCP、Agent Tool Calling、
自动摘要、自动长对话压缩、自动 chunk、全平台专用适配器、聊天监控、自动读取微信/浏览器/DSH 数据目录；
以及 Markdown → HTML 渲染、任何自动分类/重要性判断。

---

## 12. 完成标准对照（规格书 §二十一）与本阶段指纹

| 完成标准 | 证据 | 结果 |
| --- | --- | --- |
| ChatMessage / ChatConversation 规范化结构 | §2 + `test_1/3/8` + 结构校验测试 | ✅ |
| 角色解析 | §4 + `test_2/4/5`（未知角色明确拒绝） | ✅ |
| 时间与会话边界保留 | `test_7/17` + 场景 C（时间戳、conversation_id、message_count） | ✅ |
| Chat → Capture | `test_18` + `test_28`（唯一出口是 CaptureService） | ✅ |
| source_type=chat | `test_8`（P2 同款）→ 本阶段 `test_18/27` + 场景 A2 Source 实测 | ✅ |
| user / assistant 语义边界保持 | §6 + `test_4/23/24` + 真实结果（用户目标 `user_explicit`、助手知识 `source_content`） | ✅ |
| 高价值聊天 → Memory | `test_20` + 场景 A（3 条）/ A2（1 条） | ✅ |
| 低价值聊天 → Memory=0 | `test_21` + 场景 B | ✅ |
| 低价值聊天不长期保存 Source | `test_22` + 场景 B（Source=0） | ✅ |
| Source-Memory 关联 | `test_26/27`（`memory_sources` 双向可查）+ 场景 A2（1 条关联） | ✅ |
| 复用现有去重 | `test_30` + 未新增任何去重算法 | ✅ |
| CLI 可真实运行 | 场景 A/A2/B 全部通过真实子进程 | ✅ |
| 自动化测试全部通过 | `Ran 409 tests … OK (skipped=2)` | ✅ |
| 至少一次真实 Chat → Memory | 场景 A / A2 | ✅ |
| 至少一次真实低价值 Chat → 零写入 | 场景 B | ✅ |
| Phase 1～4 + KB Phase 1～2 全部旧测试继续通过 | 367 → 409，原有 367 全部继续通过 | ✅ |

**安全与隐私（规格书 §十九）**：不打印完整聊天正文（默认视图零正文字符）；证据文件写入前自检并拒绝含对话正文的 payload
（实测 `body_in_evidence = []`）；fixture 全部虚构、无 API Key/Token；错误消息不含正文；CLI 默认不回显 Source 全文；
文档明确说明"聊天内容经真实 LLM Formation 会发送给配置的模型服务"。

本阶段新增/修改文件的实测指纹（`sha256[:16].upper()` / 字节）：

| 文件 | 指纹 | 字节 |
| --- | --- | --- |
| `personal_memory/importers/chat.py`（新） | `8BA8D408A65ADE5F` | 35987 |
| `personal_memory/importers/chat_adapters.py`（新） | `868D07DAE05EB852` | 5861 |
| `personal_memory/importers/__init__.py`（改） | `5D1889BF55BF87F5` | 4193 |
| `personal_memory/importers/files.py`（改：抽取共享读取管线） | `4BBD2B96DA66630F` | 17988 |
| `personal_memory/cli.py`（改） | `689097E3DA6CA982` | 37073 |
| `personal_memory/__init__.py`（改） | `12D319AF721C0F1E` | 7064 |
| `tests/test_chat_import.py`（新） | `9FB7025C34A97C41` | 37618 |
| `tests/test_cli.py`（改） | `C7B45718F88D3B1D` | 21030 |
| `scripts/kb1p3_chat_acceptance.py`（新） | `ED562857106A67AD` | 21864 |

（`KB1-PHASE2.md` §12 里 `importers/files.py` 的旧指纹因本阶段的共享读取管线抽取已变化，以上表为准。）

**停止点**：本阶段完成后即停止，不实现 URL / PDF / OCR / UI / 全平台适配器 / 自动摘要与压缩 / 聊天监控。
