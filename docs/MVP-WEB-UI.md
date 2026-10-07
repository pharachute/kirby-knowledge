# Knowledge Base 1.0 — MVP：本地 Web UI

本文件是 MVP 的完整说明与实测证据。**没有新增任何运行时依赖**：HTTP 服务、HTML 渲染、
HTTP 客户端、模板与样式全部使用 Python 标准库 + 手写 HTML/CSS。

| 项目 | 状态 |
| --- | --- |
| Memory System Phase 1–4、KB Phase 1–3 | **已冻结**，本阶段未改（指纹见 §10） |
| MVP：本地 Web UI（Capture / Import / Memories / Sources / Search） | ✅ 本阶段 |
| URL / Web Fetch / PDF / OCR / Embedding / Vector DB / Semantic Search / RAG / QA / MCP / Graph / 多用户 / 登录 / 权限 / 云同步 / telemetry | ❌ 未实现（本阶段明确禁止） |

生成时间：2026-10-04；解释器 `D:\python\python.exe`（3.13.2）；SQLite 3.45.3。

---

## 1. Web UI 最终结构

```
personal_memory/web/
├── __init__.py       公开入口：WebContext / create_server / make_handler / serve / run_web / Flash / classify_error
├── server.py         HTTP 层：路由、表单/base64 解析、错误映射、临时文件交接、上传消毒、日志、启动
└── views.py          服务端渲染：layout + 8 个页面 + 转义/徽章/表单小工具 + 一份约 60 行内联 CSS
```

```
浏览器 ──HTTP──▶ KnowledgeBaseHandler ──▶ 既有模块（不复制任何业务逻辑）
                 (GET/POST + HTML)        CaptureService / FileImporter / ChatImporter
                                          MemoryRetriever / MemoryLifecycle / MemoryRepository
```

**路由（全部实测可访问）**

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/` | 首页：数量统计（memories/sources/关联、各状态）、数据库路径、模型信息、索引一致性、隐私提示、快速入口 |
| GET | `/capture` | 文本 Capture 表单 + 处理结果块 |
| POST | `/capture` | `CaptureService.capture(text, source_type=text)` → Formation → 303 重定向 + flash |
| GET | `/import` | 文件导入（`.txt/.md/.markdown`）与聊天导入（角色文本 / JSON）两个表单 |
| POST | `/import-file` | base64 → 临时文件 → `FileImporter.import_file(...)` |
| POST | `/import-chat` | base64 → 临时文件 → `ChatImporter.import_file(...)`（`format` / `provider` / `keep_source`） |
| GET | `/memories?status=active\|pending\|archived\|all&type=…` | Memory 列表 + 状态/类型筛选 |
| GET | `/memories/<id>` | Memory 详情（全字段 + Content/Summary + Sources + 生命周期按钮 + 编辑表单） |
| POST | `/memories/<id>/archive` \| `/restore` \| `/activate` | `MemoryLifecycle.archive_memory/restore_memory/activate_memory` |
| POST | `/memories/<id>/update` | `MemoryLifecycle.update_memory(**changes)`（title/content/summary/tags/importance/confidence/status） |
| POST | `/memories/<id>/delete` | `MemoryLifecycle.delete_memory`（硬删除，Source 保留） |
| GET | `/sources` | Source 列表（标题/类型/content_hash/时间） |
| GET | `/sources/<id>` | Source 详情（Title/Type/URL/Created/Metadata/Content + 关联 Memory） |
| GET | `/search?q=&type=&status=&limit=` | `MemoryRetriever.search(...)` 的结果页 |
| GET | `/healthz` | `ok`（验收脚本用它等待服务就绪） |

**交互模式**：GET 渲染页面；POST 一律走 **POST → 303 → GET + 一次性 flash**（消息存在服务器内存里，
只有随机 token 进 URL），避免刷新重复提交，也不把用户内容塞进 URL。

## 2. 运行方式

```powershell
# 基本启动（默认 127.0.0.1:8765）
python -m personal_memory web --db data/memory.db

# 指定端口
python -m personal_memory web --db data/memory.db --port 8765

# 其它参数
python -m personal_memory web --db data/memory.db --host 127.0.0.1 --port 8765 \
       --config config/llm.json --max-bytes 1048576 [--no-quality-check]
```

启动后打印（实测输出）：

```text
Personal Knowledge Base
http://127.0.0.1:60985
database: data\ui-acceptance.db
model: deepseek / deepseek-flash (API key: configured)
privacy: 真实 Formation 会把输入内容发送给配置的模型服务；UI 不显示 API Key，也不写入日志。
Press Ctrl+C to stop.
```

* 不自动打开浏览器（只打印 URL）。
* `--port 0` 会绑定随机端口并把真实端口打印出来。
* 若把 `--host` 改成非 loopback，启动横幅会插入一行**局域网暴露警告**。
* 数据库不存在时按 `init` 相同的迁移路径自动创建/升级。

## 3. 页面 → 现有业务模块的调用关系

| 页面动作 | 调用的既有代码 | 是否存在 UI 侧副本 |
| --- | --- | --- |
| Capture 文本 | `CaptureService.capture()` → `MemoryFormationService.process()` →（Phase 4）`MemoryQualityGate` | 无 |
| Import `.txt/.md` | `FileImporter.import_file()`（→ `read_text_file()` 校验 → `CaptureService`） | 无 |
| Import 聊天 | `ChatImporter.import_file()`（→ `ChatAdapter` → `CaptureService`） | 无 |
| Memories 列表/详情 | `MemoryRepository.list_memories/require_memory/get_sources_for_memory/status_counts` | 无（连筛选值都直接传给 repository） |
| Sources 列表/详情 | `MemoryRepository.list_sources/require_source/get_memories_for_source` | 无 |
| Search | `MemoryRetriever.search()`（含 `STATUS_ALL` 映射为 `tuple(MemoryStatus)`） | 无 |
| Archive/Restore/Activate/Delete | `MemoryLifecycle.archive_memory/restore_memory/activate_memory/delete_memory` | 无 |
| Edit | `MemoryLifecycle.update_memory()`（状态机与校验在既有代码里；表单只在**状态真的变化**时才提交 status） | 无 |
| 状态转换按钮 | `MemoryLifecycle.allowed_transitions()` | 无（不复制 `ALLOWED_TRANSITIONS`） |
| 错误信息 | `classify_error()` 把既有异常映射为 (HTTP 状态, 友好说明) | 无 |
| LLM | 只通过 `CaptureService`；UI 从不直接调用模型 | 无 |

`personal_memory/web/*.py` 中**没有** `sqlite3`、没有 `SELECT/INSERT/UPDATE/DELETE`、没有
`ALLOWED_TRANSITIONS`、没有 prompt、没有排序算法（测试 15 静态扫描 + 对象同一性断言）。

## 4. 为什么选择当前前端/后端技术

* **后端 `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler`**：标准库自带，
  零新增依赖、零构建步骤；本项目必须能在**离线 / PyPI 不可达**的环境里跑，而这个工具是
  单人本地服务，只需要少量路由和表单提交，用框架不会带来任何必要能力。
* **服务端渲染 + 一份内联 CSS + 纯 `<form>`**：不需要 Node/React/Vue/Tailwind 构建链，
  页面在无 JavaScript 的降级情况下仍能完成除“选择文件”以外的全部操作。
* **唯一的一段 JavaScript（约 20 行）**只做一件事：把用户选择的文件用 `FileReader.readAsDataURL`
  读成 base64 后放进隐藏字段。**没有**引入 multipart 解析：浏览器给出的是**原始字节的 base64**，
  服务端解码后落成临时文件并交给**既有**的路径式 importer，所以 filename / extension /
  大小 / UTF-8 编码 / `file_sha256` 校验全部原样生效（这正是规格书 §七 要求的“不要绕过 importer”）。
* **HTTP 客户端**：测试与验收脚本用 `urllib.request`（标准库），没有 requests/httpx。
* 没有引入任何第三方分析、字体、CDN 或 telemetry；所有页面离线可用。

## 5. 测试结果

```text
python -m unittest discover -s tests -t . -v
Ran 438 tests ... OK (skipped=2)
```

* 既有 409 个测试**全部继续通过**（Memory System Phase 1–4、KB Phase 1–3）。
* 本阶段新增 **29** 个：`tests/test_web.py` 27 个 + `tests/test_cli.py` 的 2 个 `web` 命令用例。
* 2 个 skip 仍是没有真实凭据时跳过的可选真实 LLM 测试。
* Web 测试全部**离线**运行：真实 `ThreadingHTTPServer` 绑 `127.0.0.1:0`，用真实 HTTP 驱动，
  模型侧注入脚本化 Mock LLM。

规格书 §二十 的 18 项要求逐条对应：

| # | 要求 | 测试 | 结果 |
| --- | --- | --- | --- |
| 1 | Web server 可以启动 | `ServerLifecycleTest::test_1_server_starts_and_binds_loopback` | PASS |
| 2 | 首页返回 200 | `::test_2_home_shows_counts_and_health`（含 `/healthz`） | PASS |
| 3 | Memories 页面读取数据 | `ReadPagesTest::test_3_memories_page_reads_and_filters`（active 默认 + all/archived 筛选 + 非法 status → 400） | PASS |
| 4 | Sources 页面读取数据 | `::test_4_sources_page_and_detail`（含敏感 metadata 字段隐藏） | PASS |
| 5 | Search 返回已有 Memory | `::test_5_search_uses_the_retriever`（默认 active，`status=all` 可见归档） | PASS |
| 6 | Capture 进入现有 Formation | `WritePathsTest::test_6_capture_uses_capture_service_and_formation` + `test_6b`（303/flash） | PASS |
| 7 | TXT/Markdown 通过 UI 进入 Importer | `::test_7_file_import_goes_through_file_importer`（metadata 全字段）+ `test_7b`（.pdf 被拒、零写入、零模型调用） | PASS |
| 8 | Chat 通过 UI 进入 ChatImporter | `::test_8_chat_import_goes_through_chat_importer_and_hides_the_body` + `test_8b`（低价值零写入） | PASS |
| 9 | Archive 后 UI 状态正确 | `LifecycleTest::test_9_archive_then_10_restore` | PASS |
| 10 | Restore 后 UI 状态正确 | 同上 | PASS |
| 11 | Delete 后 Memory 消失而 Source 保留 | `::test_11_delete_removes_the_memory_but_keeps_the_source` | PASS |
| 12 | 编辑后 Retrieval 立即反映 | `::test_12_edit_is_immediately_visible_to_retrieval` | PASS |
| 13 | 非法 lifecycle 显示友好错误 | `::test_13_illegal_lifecycle_operations_show_friendly_errors`（409 + `IllegalTransitionError` + 状态未变）、`test_13b`（错误映射表） | PASS |
| 14 | 空输入不调用 LLM | `::test_14_empty_input_never_calls_the_model`（`transport.call_count == 0`） | PASS |
| 15 | UI 层没有直接 SQL | `ArchitectureAndPrivacyTest::test_15_web_layer_has_no_sql_and_reuses_the_modules`（静态扫描 + `MemoryRepository/Retriever/Lifecycle/CaptureService` 同一性） | PASS |
| 16 | 默认只绑定 localhost | `ServerLifecycleTest::test_16_default_bind_is_localhost_only`（常量、签名默认值、CLI 默认值、真实 socket） | PASS |
| 17 | API Key 不出现在 HTML/response/logs | `ArchitectureAndPrivacyTest::test_17_api_key_never_appears_in_responses_or_logs` | PASS |
| 18 | 既有测试继续通过 | `Ran 438 tests … OK` | PASS |

额外覆盖：`test_19`（HTML 转义：`<script>` 不会执行）、`test_20`（404 友好页、无 traceback）、
`test_21`（跨站 Origin 的 POST 被 403 拒绝）、`test_22`（Archive 确实调用 `MemoryLifecycle.archive_memory`）、
`test_23`（文件名消毒 `../../evil.md` → `evil.md`、非法 base64、空文件名、临时目录零残留）、
`test_24`（模型未配置时只读页面仍可用、写操作 503 友好提示）。

## 6. 实际人工验收结果

真实运行（不是模拟）：验收脚本以子进程启动**真实的** `python -m personal_memory web`，
再用真实 HTTP 走完 §二十一 的全部场景。原始证据：
[docs/kb1-mvp-web-acceptance.json](kb1-mvp-web-acceptance.json)（20 次 HTTP 交换，8/8 场景 PASS）。

| 场景 | 期望 | 实测 |
| --- | --- | --- |
| 0 启动与绑定 | 打印 URL/数据库/隐私提示；只在 127.0.0.1 | 横幅 6 行齐全；loopback 200；本机 LAN 地址 `192.168.1.212` 连接**被拒绝**（未暴露到局域网） |
| A 文本 Capture | Memory created（Formation 判断需要时才有 Source） | `0/0/0 → 1 Memory, 1 Source`；Memory：`knowledge / active / information_origin=source_content`，标题「RAG 是通过检索提供上下文的增强生成」 |
| B 低价值文本（"今天喝了一杯奶茶。"） | Memory = 0，Source = 0，数量不增长 | `1/1/1 → 1/1/1`（完全不变），页面显示 `0 Memory / 0 Source` |
| C Markdown | Importer → Capture → Formation → Memory | `+2 Memory`；Source `source_type=file`、`filename=vector-search.md`、`extension=.md`、`encoding=utf-8`、`file_sha256=84580aca3067…`、92 字符 |
| D Chat | Chat → Memory，并可见 `information_origin` | `+3 Memory`；Source `source_type=chat`、角色边界 `["[USER]","[ASSISTANT]","[USER]"]`、`metadata={captured_from: chat, provider: null, conversation_id: conv_a45b9e58…, message_count: 3, roles: [user, assistant]}`；`information_origin ∈ {source_content, user_explicit}`；导入页**没有**回显聊天正文 |
| E Search | 搜 "RAG" 找到 A 的 Memory | 命中 A 的 `mem_1325277d…`，页面渲染 `score=`；检索完全由 `MemoryRetriever` 完成 |
| F Lifecycle | Edit→可搜 / Archive→消失 / Restore→回来 / Delete→Memory 没了 Source 还在 | Edit 后新内容（注入唯一关键词「星河检索标记」）立即可搜；Archive 后 `archived`、默认列表与默认搜索都消失、`status=all` 仍可见；Restore 后 `active`、列表与搜索恢复；Delete 后 DB 与 UI 都没有该 Memory，其 chat Source 仍在（`source_count_after_delete=3`，Source 详情页 200） |
| G 隐私 | Key 不泄漏、正文不回显 | 19 个响应中均无 API Key；原始对话渲染**只**出现在用户主动打开的 Source 详情页（§十一 允许）；无 telemetry；**服务端日志 35 行中既无 Key 也无聊天正文**；首页显示隐私提示 |

验收脚本在写证据前会自检：若 API Key 或聊天正文出现在 payload 中则拒绝落盘（实测均未触发）。

## 7. 当前工具能做什么

* 浏览器里完成完整闭环：**Capture / 文件导入 / 聊天导入 → Formation → Memory → Search → View → Edit / Archive / Restore / Delete → Source**。
* 文本 Capture（含 title）、`.txt/.md/.markdown` 上传（保留 filename/extension/hash/encoding）、
  聊天记录上传（角色文本与 provider-neutral JSON，`format=auto/roles/json`，可选保留原始 Source）。
* Memory 列表（status/type 筛选）、详情（含 Information Origin、Sources 链接）、
  生命周期按钮按当前状态动态显示、编辑（title/content/summary/tags/importance/confidence/status）。
* Source 列表与详情（正文、metadata；疑似凭据字段会被隐藏；超长正文默认折叠）。
* 关键词检索（type/status/limit，默认只搜 active）。
* 清晰的成功/失败/加载/空数据状态：`✓ 处理完成`、`✗ 操作失败：<友好说明>：<原始消息>`、进度与计数。
* 与 CLI 完全一致的行为：同一个 Formation、同一个 Phase 4 质量闸门、同一套去重。

## 8. 当前不能做什么

URL / 网页抓取、PDF、OCR、Embedding、向量库、语义检索、RAG、问答式聊天、MCP、Agent/Tool Calling、
知识图谱、多用户、登录、权限、云同步、后台监听、目录扫描、自动摘要、自动 chunk、
Dashboard 图表、动画与主题定制、浏览器自动打开；
界面文案为中文、无国际化；没有批量上传（一次一个文件）；没有 Memory 的手动新建（只能由 Formation 产生）。

## 9. 已知限制

1. **Firefox/Chrome 之外的极老浏览器**若不支持 `FileReader.readAsDataURL`，文件导入会退化失败（错误提示明确）；文本 Capture 不受影响。
2. **服务端渲染，无 websocket/轮询**：LLM 处理期间只有一个“提交中”的状态，页面在请求返回前不会显示增量进度。
3. **POST 后刷新**：因为使用 303 + flash，成功操作不会再提交；但**表单页本身**（如 Capture 输入框）在刷新时可能触发浏览器的“重新提交”提示（Flash 已消费，不会重复写入）。
4. **CSRF 防护是轻量的**：仅校验 `Origin`/`Host` 一致（本机工具足够）；没有 token、没有登录，因为规格书明确不要求多用户/权限。因此**只监听 127.0.0.1**，改成对外监听等于把知识库开放给能访问该端口的人。
5. **上传经 base64**：HTTP 体积约为文件的 1.37 倍，请求体上限 8 MiB（文件本身仍受 importer 的 1 MiB / `--max-bytes` 限制）。
6. **Memory 列表最多 200 条、Source 列表最多 200 条、Search limit ≤ 100**，没有分页 UI（MVP 简化）。
7. **编辑表单不含 `information_origin`**（来源性质由 Formation 决定，不应手工改写）；`status` 只在真正变化时才提交给生命周期状态机。
8. **删除是硬删除 Memory**，Source 保留；没有回收站/撤销。
9. **Source 正文可全文查看**（§十一 的设计），因此查看 Source 详情页会显示已持久化的原始材料（含聊天原文）——这是有意的，不是泄漏。
10. **无并发写入控制**：单用户本地使用，SQLite 依赖既有事务；多人同时用同一个 DB 文件不在本 MVP 范围内。
11. **日志**只记录 `方法 路径 -> 状态码`（无查询串、无请求体）；异常细节截断到 200 字符写入 stderr。

## 10. 指纹与完成标准

**新增/修改文件指纹（`sha256[:16].upper()` / 字节）**

| 文件 | 指纹 | 字节 |
| --- | --- | --- |
| `personal_memory/web/__init__.py`（新） | `B3EE21076D3A6C92` | 1638 |
| `personal_memory/web/server.py`（新） | `14ECFBAE71E3B58D` | 34739 |
| `personal_memory/web/views.py`（新） | `F614AF1C01A7C377` | 31671 |
| `personal_memory/cli.py`（改：`web` 命令、版本 phase） | `BAA0372279B00BA1` | 38262 |
| `personal_memory/__init__.py`（改：0.8.0、导出 WebContext/create_server） | `144A65CAFD52474D` | 7166 |
| `tests/test_web.py`（新） | `6AB567D25656A5C1` | 30994 |
| `tests/test_cli.py`（改） | `6F2AD3A35F5AAB17` | 22308 |
| `scripts/kb1p_mvp_web_acceptance.py`（新） | `088A0B4141795268` | 29136 |

**未改（指纹与本阶段开始前逐一相同）**：`db.py F6EE1E73E1C747B9`、`retrieval.py FF35E0117691437D`、
`lifecycle.py A9BF67FBBE7C5C60`、`quality.py 4C6D3EA3081E84C4`、`extraction.py 147D50D908EFA216`、
`models.py 6F892D6888319836`、`store.py 0AC465BC62958D3D`、`errors.py B62C84CBAEB39AED`、
`prompts.py 7130F83C7E21856E`、`llm.py E3D1DCA0FB619130`、`capture.py F33696E7C307F6CC`、
`importers/chat.py 8BA8D408A65ADE5F`、`importers/files.py 4BBD2B96DA66630F`、`docs/schema.sql 160069D9C0D8B396`
以及 Memory System / KB 的全部既有测试文件。数据库 schema 仍为 v2，**没有新增 migration**。

**完成标准（规格书 §二十三）**

| UI | 结果 | 架构 | 结果 |
| --- | --- | --- | --- |
| 本地 Web server | ✅ | UI 不直接操作 SQLite | ✅（静态扫描 + 对象同一性） |
| Capture | ✅ | UI 不复制 Formation | ✅ |
| TXT / Markdown import | ✅ | UI 不复制 Retrieval | ✅ |
| Chat import | ✅ | UI 不复制 Lifecycle | ✅（调用 `MemoryLifecycle`） |
| Memories / Sources | ✅ | 复用已有 API | ✅ |
| Search / Memory detail | ✅ | 默认 localhost | ✅（LAN 连不上） |
| Memory lifecycle | ✅ | API Key 不泄漏 / 错误不泄漏聊天正文 / 无 telemetry | ✅ |
| 既有测试继续通过 | ✅ 409 → 438 | 文本/TXT/Markdown/Chat/Search/Lifecycle 真实端到端 | ✅（§6） |

**停止点**：MVP 到此为止，不继续做 URL / PDF / Embedding / RAG / QA / MCP / Graph / Agent。
