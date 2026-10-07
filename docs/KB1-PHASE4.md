# Knowledge Base 1.0 — Phase 4：URL / Web Importer

本文件是 KB 1.0 第四阶段的完整说明与实测证据。**没有新增任何运行时依赖**：抓取用
`urllib.request`，正文提取用标准库 `html.parser`，地址校验用 `ipaddress`。
`lxml` / `beautifulsoup4` / `html5lib` / `readability` / `trafilatura` 在本环境**均未安装**
（`requests` 恰好存在但**未使用**），因此正文提取是自研的轻量实现 —— 这一点如实记录在 §6 与 §8。

| 项目 | 状态 |
| --- | --- |
| Memory System Phase 1–4、KB Phase 1–3、MVP 本地 Web UI | **已冻结**，本阶段未改（指纹见 §10） |
| KB 1.0 Phase 4：URL / Web Importer | ✅ 本阶段 |
| PDF / DOCX / OCR / Embedding / Vector DB / Semantic Search / RAG / QA / 多页面爬虫 / Cookie / MCP / 批量 URL | ❌ 未实现（本阶段明确禁止） |

生成时间：2026-10-05；解释器 `D:\python\python.exe`（3.13.2）；真实网络环境（可访问公网 HTTPS）。

---

## 1. 新增/修改的文件

**新增**

| 文件 | 作用 |
| --- | --- |
| `personal_memory/importers/web.py` | Web Importer：URL 校验/SSRF 防护、单跳 HTTP 传输、重定向逐跳复核、编码、HTML→正文提取、`WebDocument`、`WebImporter`、`WebImportResult`、错误层次 |
| `tests/test_web_import.py` | 23 个测试：抓取/安全/提取/管线集成（全部离线，脚本化 transport + 注入 resolver） |
| `scripts/kb1p4_web_acceptance.py` | 真实端到端验收：真实公网抓取 + 真实 CLI + 真实 Web UI + 真实 LLM |
| `docs/KB1-PHASE4.md` | 本文件 |
| `docs/kb1p4-web-acceptance.{json,txt}` | 验收原始证据（真实 URL、真实结果） |

**修改（必要且最小）**

| 文件 | 修改 | 原因 |
| --- | --- | --- |
| `personal_memory/cli.py` | 新增 `import-url` 命令（`--db --title --max-bytes --timeout --min-chars --dry-run --no-quality-check --config --json`）+ `_print_web_import`；`version.phase` 指向 KB 1.0 Phase 4 | 真实可跑入口（§八） |
| `personal_memory/web/server.py` | 新增 `POST /import-url`（调用**同一个** `WebImporter` + `CaptureService`）；错误映射增加 `WebImportError → 400 URL 导入失败` | UI 最小接入（§九） |
| `personal_memory/web/views.py` | Import 页面新增「Import 网页 URL」表单（URL + 可选标题 + 提交按钮）；结果块显示 url/host/提取长度 | §九「不重新设计布局」 |
| `personal_memory/importers/__init__.py`、`personal_memory/__init__.py` | 导出 Web Importer API；版本 0.8.0 → 0.9.0，`__phase__ = "kb-1.0-phase-4"` | 公开 API 与版本 |
| `tests/test_cli.py`、`tests/test_web.py` | 版本断言 + `import-url` CLI 用例（2 个）+ UI URL 导入用例（2 个） | 新命令/新入口需要测试 |

**未改（指纹证明）**：`capture.py` `F33696E7C307F6CC`、`models.py` `6F892D6888319836`、
`store.py` `0AC465BC62958D3D`、`extraction.py` `147D50D908EFA216`、`retrieval.py` `FF35E0117691437D`、
`lifecycle.py` `A9BF67FBBE7C5C60`、`quality.py` `4C6D3EA3081E84C4`、`errors.py` `B62C84CBAEB39AED`、
`prompts.py` `7130F83C7E21856E`、`llm.py` `E3D1DCA0FB619130`、`db.py` `F6EE1E73E1C747B9`、
`importers/{files,markdown,chat,chat_adapters}.py`、`docs/schema.sql` `160069D9C0D8B396`
以及全部既有测试文件 —— 实测与本阶段开始前逐一相同。数据库 schema 仍为 v2，**没有新增 migration**，
Memory / Source 数据模型未改。

## 2. 网页抓取与正文提取流程

```text
URL
 │ ① normalise_url()        只允许 http/https；拒绝凭据、非法端口、空白与控制字符；去掉 fragment
 │ ② assert_public_url()    host 语法检查（localhost/*.local/*.internal/metadata…）
 │                          字面 IP 直接判定；域名解析后**每一个**地址都必须是 is_global
 ▼ （每一跳都重复 ①②）
UrllibTransport.fetch()     单次 GET；**不自带重定向跟随**；超时；Content-Length 预检 + 读取字节上限
 │ ③ 3xx？ → 取 Location → urljoin → 回到 ①（跳数上限，默认 5）
 │ ④ 4xx/5xx → UrlFetchError（带状态码）
 ▼
5. Content-Type 白名单（text/html / application/xhtml+xml / text/plain；缺失时仅在明确是 HTML 时才接受）
6. 解压（gzip/deflate）→ 字节上限复核
7. decode_body()：声明 charset（响应头或 <meta charset>）→ utf-8 → gb18030 → cp1252 → latin-1，
   记录实际编码与是否回退
8. extract_content()：
   ├─ 标题：<title> → 第一个 <h1> → og:title（title_source 记录来源）
   ├─ 正文候选：<article> → <main> → <body> → 整篇文档，取第一个 ≥ min_content_chars 的候选
   ├─ 丢弃：script/style/noscript/template/svg/iframe/object/form/button/select/…
   │        nav/footer/header/aside 以及 role=navigation|banner|contentinfo|complementary|search|…
   ├─ 保留：段落、标题、列表（"- " 前缀）、引用、代码（**保留缩进**）、图片 alt、链接文本 + (href)
   ├─ 实体解码（&amp; &nbsp; &#8217; …）、\xa0/零宽字符清理、空白与空行折叠
   └─ 过短/空/只有导航 → EmptyContentError（带实际字符数与阈值）
 ▼
WebDocument(title, content, original_url, final_url, fetched_at, content_type, metadata, title_source)
 ▼ to_capture_request() → CaptureRequest(source_type=web, url=final_url, metadata=…)
CaptureService → MemoryFormationService → Phase 4 quality gate → Memory System
```

真实页面的提取结果（验收场景 0，实测）：

| URL | 提取根 | 正文字符 | Content-Type | 是否含原始 HTML |
| --- | --- | --- | --- | --- |
| `https://peps.python.org/pep-0020/` | `main` | 1635 | text/html | 否 |
| `https://docs.python.org/3/library/urllib.request.html` | `body` | 53847 | text/html | 否 |
| `https://github.com/psf/requests` | `article` | 2386 | text/html | 否 |
| `https://github.com/python/cpython` | `article` | 8329 | text/html | 否 |
| `https://raw.githubusercontent.com/psf/requests/main/README.md` | `text/plain` | 2893 | text/plain | 否 |

## 3. URL 安全边界（实际实现 vs 未实现）

**已实现（每条都有测试）**

| 保护 | 实现 |
| --- | --- |
| 只允许 http/https | `normalise_url` 拒绝 `file:`/`ftp:`/`data:`/`javascript:`/`gopher:`/相对 URL |
| 拒绝 URL 凭据 | `http://user:pass@host` → `InvalidUrlError` |
| 非法 URL | 空/空白/含空格或控制字符/非法端口（>65535）→ `InvalidUrlError` |
| 本机与内网 | `localhost`、`*.localhost`、`*.local`、`*.internal`、`*.home.arpa`、`metadata*` → `BlockedUrlError` |
| 字面 IP | `127.0.0.1`、`::1`、`10.x`、`192.168.x`、`172.16.x`、`169.254.169.254`、`100.64.x`、`0.0.0.0`、`fd00::` → `BlockedUrlError` |
| 域名解析复核 | 解析出的**每一个**地址都必须 `is_global`，否则拒绝（测试用注入 resolver 验证私有地址被拦） |
| 重定向 | **不自动跟随**：自定义 `HTTPRedirectHandler` 让 3xx 直接返回；每一跳重新做 scheme/凭据/地址校验；默认最多 5 跳；无 `Location`、超限、跳向 `file://` 或内网都明确失败 |
| 超时 | 连接+读取超时（默认 15s，CLI `--timeout`） |
| 大小限制 | `Content-Length` 预检 + 读取上限 + 解压后复核（默认 1 MiB，复用项目既有上限；`--max-bytes` 可配） |
| 类型校验 | Content-Type 白名单；缺失时仅接受“明确是 HTML”的响应；`application/pdf`、`image/png`、`application/json` 等明确失败 |
| 无本地资源 | 不读 `file://`、不访问本机服务（回环被拦）、不带 Cookie、不带凭据、无代理、无 headless 浏览器 |
| 正文不外泄 | 正文不写日志（HTTP 层日志只有 `方法 路径 -> 状态码`）；错误消息只含 URL/状态/计数，不含正文（有测试） |
| 原始响应字节上限 | 传输层读取上限 + **Importer 侧在解压前再次校验** `len(body) > max_bytes` → `ResponseTooLargeError`（不依赖具体 transport） |
| 压缩响应解压上限 | `gzip`/`x-gzip`/`deflate`（含 raw deflate 回退）用 `zlib.decompressobj` **流式**解压，单次调用 `max_length = max(1, limit - len(out))`，**超过上限立即抛 `ResponseTooLargeError`**，从不先物化完整解压结果（见 §11 安全审查） |
| 异常压缩流 | 损坏、截断、空体、与声明不匹配的压缩流，以及 `br`/`zstd`/堆叠编码 → `ContentEncodingError`（`WebImportError` 子类 → HTTP 400），不再静默当作文本处理 |

**未实现 / 实际边界（不声称绝对防止 SSRF）**

1. **DNS rebinding / TOCTOU**：校验发生在请求之前，若 DNS 答案在校验与连接之间改变，仍可能连到内网地址。
   本阶段没有实现“解析→固定 IP→用 Host/SNI 连接”的完整钉扎，也没有二次校验响应来源。
2. **允许的公网服务可能是反向代理**：如果目标主机本身代理到内网，本工具无法识别。
3. **不解析 robots.txt**、不做限速/重试退避（每次导入只发一轮请求）。
4. 不做 JavaScript 渲染、不管理 Cookie/登录态、不支持代理绕过；动态渲染或需要登录的页面会因“无正文/类型不符”明确失败。
5. 每跳只做一次 DNS 解析校验；同一跳内的多个地址都要求公网，但不强制“只用第一个地址”。
6. 域名黑名单是启发式（后缀/名称），不能替代 DNS 校验（后者是主要防线）。

## 4. Importer → Capture 调用关系

```text
WebImporter.fetch(url) -> WebDocument        （纯网络 + 解析；无 DB、无模型）
WebImporter.import_document(document, capture, title=..., dry_run=...)
   └─ document.to_capture_request(source_type=web) -> CaptureRequest
        └─ CaptureService.capture_request()  （KB Phase 1，未改）
             └─ MemoryFormationService.process() （冻结：价值判断/提取/策略/Phase 4 闸门）
                  └─ MemoryRepository.transaction() （冻结）
```

* `web.py` 中**没有** `sqlite3`、没有 SQL 动词、没有 `store` 导入；`fetch()` 的签名里没有 repository
  （测试 13 断言），`import_document()` 只接受 `CaptureService`（传 repository 抛 `ValidationError`）。
* Importer **不判断价值**、不生成 Memory、不自行持久化：低价值页面在 Formation 判 `worth_remembering=false`
  时 **Memory=0、Source=0**（测试 16 + 验收场景 4 的 `example.com`）。
* 完全复用既有去重：Source `content_hash` 复用 + Phase 4 精确重复（测试：同 URL 二次导入 → Source reused；
  开着闸门导入同一页面 → `status=duplicate`、零新增）。
* CLI 与 Web UI 使用**同一条**管线（同一个 `WebImporter`、同一个 `CaptureService`、同一质量闸门）。

## 5. CLI 与 Web UI 的使用方式

```powershell
# CLI（默认不打印网页正文）
python -m personal_memory import-url "https://peps.python.org/pep-0020/" --db data/memory.db
python -m personal_memory import-url "https://github.com/psf/requests" --db data/memory.db --json
python -m personal_memory import-url "https://example.com/" --db data/memory.db --dry-run   # 只分析不写库
python -m personal_memory import-url "https://docs.python.org/3/library/urllib.request.html" \
       --db data/memory.db --max-bytes 2097152 --timeout 30 --min-chars 200 --no-quality-check
```

人类可读输出（实测）：`url / final url / page(title, title_source, content_type, chars) /
fetch(status, bytes, charset, redirects, root) / import(status) / formation(status, worth_remembering,
attempts, model) / memories / sources / privacy 提示`。

```powershell
# Web UI：Import 页面新增「Import 网页 URL」表单（URL + 可选 Title + Import URL 按钮）
python -m personal_memory web --db data/memory.db --port 8765
```

UI 的表单 POST 到 `/import-url`，服务端调用同一个 `WebImporter` + `CaptureService`；
结果页只显示 host、标题、提取长度、生成的 Memory/Source 链接，**不回显正文**；
内网/本机/非法地址返回 400 + `URL 导入失败`（保留原有的 localhost 监听、Origin/Host 校验与隐私约束）。

## 6. 自动化测试结果

```text
python -m unittest discover -s tests -t . -v
Ran 475 tests ... OK (skipped=2)
```

* 既有 438 个测试**全部继续通过**；本阶段新增 **37** 个：功能实现 27 个（`tests/test_web_import.py` 23 +
  `tests/test_cli.py` 2 + `tests/test_web.py` 2）+ **压缩安全审查 10 个**（`CompressionTest`，见 §11）。
* 全部离线：HTTP 用脚本化 transport，DNS 用注入 resolver，模型用 Mock LLM —— 没有任何测试依赖外部网络。

规格书 §十一 的 20 项要求逐条对应：

| # | 要求 | 测试 | 结果 |
| --- | --- | --- | --- |
| 1 | 静态 HTML 标题与正文 | `ExtractionTest::test_1_static_html_title_and_body` | PASS |
| 2 | `article` / `main` 正文选择 | `::test_2_prefers_article_then_main` | PASS |
| 3 | `script` / `style` 等噪声删除 | `::test_3_script_style_forms_and_furniture_removed`（含代码缩进保留） | PASS |
| 4 | 字符编码与 HTML entities | `::test_4_encoding_and_entities`（gb18030 + `&amp;`/`&nbsp;`/`&#8217;`） | PASS |
| 5 | 空正文与无效 HTML | `::test_5_empty_invalid_and_nav_only_pages`、`::test_5b_text_plain_pages_are_normalised` | PASS |
| 6 | 连接超时、HTTP 错误 | `FetchGuardTest::test_6_timeouts_and_http_errors_are_explicit` | PASS |
| 7 | 响应大小上限 | `::test_7_response_size_limit`（实际字节 + `Content-Length` 两条路径） | PASS |
| 8 | URL scheme 校验 | `::test_8_url_scheme_and_syntax_validation`（并断言从未发出请求） | PASS |
| 9 | localhost / 私有 / 内网拒绝 | `::test_9_localhost_private_and_metadata_targets_are_rejected`（15 个地址 + 注入 resolver） | PASS |
| 10 | 重定向重新校验 | `::test_10_redirects_are_revalidated_and_limited`（安全链跟随、跳内网被拦且未发第二次请求、循环、缺 Location、0 跳） | PASS |
| 11 | 非法 Content-Type | `::test_11_content_type_validation`（含缺失时嗅探与 `text/plain`） | PASS |
| 12 | 空查询/无效参数不绕过校验 | `::test_12_invalid_parameters_cannot_bypass_validation`（构造参数非法 + URL 内嵌内网地址只是数据） | PASS |
| 13 | Importer 不访问数据库 | `WebPipelineTest::test_13_importer_does_not_touch_the_database`（静态扫描 + 签名 + 传 repository 被拒） | PASS |
| 14 | WebDocument → Capture | `::test_14_webdocument_to_capture_request`（source_type=web、url、metadata 全字段、正文非原始 HTML） | PASS |
| 15 | 高价值页面形成 Memory | `::test_15_high_value_page_forms_memories`（Source 类型 web + 关联 + 可检索） | PASS |
| 16 | 低价值页面零写入 | `::test_16_low_value_page_writes_nothing`、`::test_low_value_source_is_not_persisted_unconditionally` | PASS |
| 17 | Formation 失败无半成品 | `::test_17_formation_failure_leaves_nothing_behind`、`::test_17b_dry_run_writes_nothing` | PASS |
| 18 | CLI 失败状态明确 | `test_cli.ImportUrlCommandTest::test_blocked_and_invalid_urls_fail_before_anything_else`（9 个 URL → 退出码 3 + 具体错误类型，且不创建数据库） | PASS |
| 19 | Web UI 调用同一条业务链 | `test_web::test_25_url_import_uses_the_same_chain`（真实 HTTP POST → 同管线 → Memory/Source） | PASS |
| 20 | 敏感正文不进入日志或错误 | `::test_20_sensitive_page_text_stays_out_of_default_views_and_errors` + 场景 6（CLI stdout 无正文/无 Key） | PASS |

额外覆盖：`redirect` 的原始/最终 URL 记录、`text/plain` 路径、`dry-run` 零写入、同 URL 二次导入的
Source 复用与 Phase 4 去重、`WebDocument` 结构校验、错误层次继承关系、响应过大/不支持类型/空正文的错误消息不含正文。

## 7. 真实公开网页端到端结果

真实运行：`python scripts/kb1p4_web_acceptance.py` —— 真实公网抓取 + 真实 `python -m personal_memory`
CLI 子进程 + 真实 `web` 服务器（子进程）+ 真实 `deepseek-flash`。
原始证据：[docs/kb1p4-web-acceptance.json](kb1p4-web-acceptance.json)（7/7 场景 PASS）。

| 场景 | 期望 | 实测 |
| --- | --- | --- |
| 0 提取探针 | 真实页面可抓取并提取正文 | 5 个真实页面全部成功（根 `main`/`body`/`article`×2/`text/plain`），提取结果**不含原始 HTML**，耗时 0.22–0.76s |
| 1 真实文章导入 | URL → 提取 → Capture → 真实 LLM → Memory | `https://peps.python.org/pep-0020/`：`formation=persisted`，**2 条 `knowledge` / `source_content` / active**（"PEP 20《The Zen of Python》的基本信息"、confidence 0.95；"Python 设计的核心格言"、0.9）；Source `source_type=web`、`content_chars=1635`、metadata 含 original/final URL、fetched_at、content_type、content_chars、title_source、http_status=200、extraction_root=main、charset=utf-8；计数 `0/0/0 → 2/2/1` |
| 2 GitHub README | GitHub 页面 → article 正文 → Memory | `https://github.com/psf/requests`：`extraction_root=article`、2386 字符、`formation=persisted`，**2 条 `knowledge`/`source_content`/active**；Source `final_url=https://github.com/psf/requests`、metadata 全字段 |
| 3 新进程检索 | 新进程能找到网页形成的 Memory | 4 次关键词检索（Python / Requests / Zen / HTTP），命中的 id 与场景 1+2 的 **4 条 Memory 全部匹配**（total 1–3） |
| 4 非法/低价值输入 | 明确失败且零写入 | 6 个真实输入全部退出码 3 且错误类型符合预期：`https://example.com/` → `EmptyContentError`（真实页面无正文）、`http://127.0.0.1:8765/` → `BlockedUrlError`、`http://169.254.169.254/latest/meta-data/` → `BlockedUrlError`、`http://10.0.0.5/` → `BlockedUrlError`、`file:///etc/passwd` → `InvalidUrlError`、真实 404 URL → `UrlFetchError`；计数前后完全不变 |
| 5 Web UI URL 导入 | 浏览器表单走同一管线 | 真实 `web` 子进程启动（横幅含 URL/数据库/隐私提示），`POST /import-url` 用 `https://raw.githubusercontent.com/psf/requests/main/README.md` → HTTP 200 + 「网页导入完成」，Source `content_type=text/plain`、`extraction_root=text/plain`、2893 字符，计数 `4/4/2 → 6/6/3` |
| 6 隐私 | 默认输出与证据无正文、无 Key | CLI `--json` stdout 中既无已存 Source 的正文片段、也无 API Key；证据文件写入前自检并拒绝含 Key 或 400 字符以上正文的 payload |

最终库状态：`counts = {memories: 6, sources: 3, memory_sources: 6}`、
`status_counts = {active: 4, pending: 2}`、`index_consistency.consistent = true`。
本阶段真实验证消耗 4 次 LLM 调用（场景 1、2、5 各 1 次 + 场景 6 的 dry-run 1 次）。

## 8. 已知限制

1. **正文提取是轻量启发式，不是 readability**：本环境没有 `lxml`/`bs4`/`readability`/`trafilatura`，
   用的是标准库 `html.parser` + 自建节点树。没有语义评分、没有模板聚类；对**没有 `article`/`main` 且
   导航不在语义标签/ARIA landmark 里**的站点，正文可能夹带少量导航文字（Python 文档页即为回到 `body`
   回退的例子，已通过 ARIA role 过滤掉主要导航）。
2. **`min_content_chars` 阈值（默认 200）会拒绝很短的页面**：例如 `https://example.com/` 只有约 156 字符，
   会被判为"没有可用正文"而明确失败（这是测试与验收里记录的真实行为，不是 bug）。需要时可 `--min-chars` 调低。
3. **只处理 `text/html` / `application/xhtml+xml` / `text/plain`**：JSON、XML、PDF、图片等明确失败。
4. **不做 JavaScript 渲染**：纯前端渲染的 SPA、需要登录/验证码/付费墙的页面会失败或只得到空壳文本。
5. **大小上限 1 MiB（可配）**：超限明确失败而不是截断；解压后的内容同样受该上限约束。
6. **编码回退是启发式**：声明编码 → utf-8 → gb18030 → cp1252 → latin-1（可能带替换字符），
   `encoding_fallback` 会记录在 metadata 中；不引入 `charset_normalizer` 以保持零依赖。
   压缩解压已有界（§11），但**只对 `gzip`/`x-gzip`/`deflate`（含 raw deflate）生效**；
   `br`/`zstd`/堆叠编码明确失败而不是尝试解码。
7. **链接以 `文本 (href)` 形式保留**，图片保留 `alt`；不做链接重写、不下载附件、不抓取页面内的其它链接。
8. **不解析 robots.txt、不做限速与重试**（每次导入一轮请求）；不做多页面爬虫/目录遍历。
9. **安全边界见 §3**：DNS rebinding/TOCTOU 未完全防止，反向代理型目标无法识别 —— 不声称绝对 SSRF 防护。
10. **确定性 CLI 输出**：`--json` 默认不含正文（需程序化调用 `as_dict(include_content=True)` 才能拿到）；
    Web UI 与人类可读输出同样默认不回显正文。
11. **大页面会让 Formation 的 prompt 很大**（例如 53k 字符的文档页 ≈ 20k tokens），会变慢/变贵；
   本阶段不做摘要或分块（规格明确留待以后）。

## 9. 明确未实现的功能（本阶段禁止）

PDF / DOCX、OCR、Embedding / 向量数据库、语义检索、RAG / QA、Web UI 视觉重设计、多页面爬虫与目录遍历、
登录态与 Cookie 管理、代理绕过、MCP / Agent Tool Calling、批量 URL 导入、无头浏览器、
站点专用适配器（GitHub API / 知乎 / 微信公众号等）、robots.txt 解析、限速与重试策略。

## 10. 指纹与完成标准

**新增/修改文件指纹（`sha256[:16].upper()` / 字节）**

| 文件 | 指纹 | 字节 |
| --- | --- | --- |
| `personal_memory/importers/web.py`（新；安全审查后） | `482C509E4E0C517A` | 47226 |
| `personal_memory/importers/__init__.py`（改；安全审查后） | `14DBDDB322E62A61` | 5271 |
| `personal_memory/cli.py`（改） | `0C646092A1759497` | 43548 |
| `personal_memory/__init__.py`（改；安全审查后） | `7AC340FB3EBED1F9` | 7933 |
| `personal_memory/web/server.py`（改） | `6F954824C2260B40` | 36254 |
| `personal_memory/web/views.py`（改） | `F6C70CD4DA833A96` | 32431 |
| `tests/test_web_import.py`（新；安全审查后） | `E47B575670D78B69` | 39938 |
| `tests/test_cli.py`（改） | `4A55151EB4CA5EB8` | 25469 |
| `tests/test_web.py`（改） | `BDFDD011F1D3BDE8` | 33695 |
| `scripts/kb1p4_web_acceptance.py`（新） | `35248BF5D9F92BFD` | 23268 |

**未改（指纹与本阶段开始前逐一相同）**：`capture.py F33696E7C307F6CC`、`models.py 6F892D6888319836`、
`store.py 0AC465BC62958D3D`、`extraction.py 147D50D908EFA216`、`retrieval.py FF35E0117691437D`、
`lifecycle.py A9BF67FBBE7C5C60`、`quality.py 4C6D3EA3081E84C4`、`errors.py B62C84CBAEB39AED`、
`prompts.py 7130F83C7E21856E`、`llm.py E3D1DCA0FB619130`、`db.py F6EE1E73E1C747B9`、
`importers/files.py 4BBD2B96DA66630F`、`importers/markdown.py 5AF1086B299DBB46`、
`importers/chat.py 8BA8D408A65ADE5F`、`importers/chat_adapters.py 868D07DAE05EB852`、
`docs/schema.sql 160069D9C0D8B396` 及 Memory System / KB / MVP 的全部既有测试文件。
Schema 仍为 v2，**没有新增 migration**，Memory / Source 模型未改。

**完成标准对照（§一、§七、§十、§十一、§十二）**

| 要求 | 结果 |
| --- | --- |
| 安全访问公开网页（scheme/凭据/内网/解析/重定向/超时/大小/类型） | ✅（§3 + 测试 8/9/10/11 + CLI 场景 4） |
| 提取标题与主要正文 | ✅（`article`/`main` 优先 + ARIA 过滤 + 实测 5 个真实页面） |
| 保存网页来源元数据 | ✅（`captured_from=web`、original/final URL、fetched_at、Content-Type、正文哈希、http_status、extraction_root、charset…） |
| 转换为现有 CaptureRequest | ✅（`source_type=web`；Importer 不碰 SQLite/LLM） |
| 走 Capture → Formation → 质量闸门 → Memory System | ✅（测试 15–17、22 + 场景 1/2/5） |
| 在现有 Web UI 中查看导入结果 | ✅（场景 5：真实 HTTP 200 + 结果块 + Source 详情） |
| 明确失败：非法/不允许/DNS/HTTP/重定向/超时/过大/类型/无正文/Formation 失败 | ✅（测试 6–12、17 + 场景 4） |
| 失败不留半成品、不无条件保存低价值 Source | ✅（测试 16/17 + 场景 4 计数不变） |
| 既有测试继续通过 | ✅ 438 → 465（全部通过，2 个可选真实 LLM 测试 skip） |
| 至少一个真实静态文章 + 一个 GitHub README 端到端 | ✅（场景 1 PEP 20、场景 2 GitHub README、场景 3 新进程检索、场景 5 UI） |
| 真实低价值/无效页面不产生 Memory/Source | ✅（场景 4：`example.com`、回环、元数据地址、私网、`file://`、真实 404） |
| 明确禁止项未实现 | ✅（§9） |

**停止点**：本阶段完成后即停止，不继续做 PDF / OCR / Embedding / RAG / QA / MCP / 爬虫 / 批量 URL。

## 11. 安全审查修正：HTTP 压缩响应的解压上限（2026-10-05）

### 11.1 发现（先复现，再修）

审查对象是本文件 §2 第 6 步「解压（gzip/deflate）」的实现。原实现是：

```python
def _decompress(body, headers):
    encoding = (_header(headers, "content-encoding") or "").lower()
    try:
        if encoding == "gzip":
            return gzip.decompress(body)          # ← 一次性完整解压
        if encoding == "deflate":
            try:    return zlib.decompress(body)
            except zlib.error: return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return body                               # ← 静默回退成原始字节
    return body
```

调用点在 `WebImporter.fetch()` 中，**大小检查发生在解压之后**：

```python
body = _decompress(response.body, response.headers)
if len(body) > self.max_bytes:                    # ← 太晚了
    raise ResponseTooLargeError(...)
```

**发现 1（高危，已修）：解压炸弹可造成内存耗尽。** 用合成的 gzip 炸弹实测（65,250 字节 → 64 MiB，
`max_bytes=64 KiB`）：

| | 修复前 | 修复后 |
| --- | --- | --- |
| `ResponseTooLargeError.size_bytes` | `67,108,864`（完整解压结果） | `65,537`（= 上限 + 1，停在上限处） |
| `tracemalloc` 峰值内存 | **141.4 MiB** | **0.24 MiB** |
| 耗时 | 0.07s | 0.00s |

即：原实现把**完整解压结果**分配出来之后才检查上限，1 GB / 10 GB 级炸弹会直接尝试分配对应内存。

**发现 2（中危，已修）：损坏/不匹配的压缩流被静默当作正文。** `except (OSError, zlib.error): return body`
把无法解压的流原样交回，于是二进制垃圾进入解码与正文提取：
用 zlib 包装的数据配 `Content-Encoding: gzip` 时，实测**成功生成了 WebDocument**（把乱码当正文导入）；
换成另一种内容时则报 `EmptyContentError`（"没有可用正文"），错误信息具有误导性。
未知编码（`br`、`zstd`、`gzip, br`）也只会被忽略。

### 11.2 修复

1. **有界流式解压**（新 `decompress_bounded()` / `_inflate_bounded()`）：用 `zlib.decompressobj(wbits)`
   分块解压，单次调用 `max_length = max(1, limit - len(out))`；
   - `room` 只剩余 0 字节时仍请求 1 字节 —— 因此**恰好等于上限**可以成功，而**超过 1 字节**立即
     `raise ResponseTooLargeError(size_bytes=上限+1)`；任何单次调用都不可能产出无界数据；
   - `unconsumed_tail` 用于继续消费被 `max_length` 截下的输入；末尾再以同样有界的方式抽干内部缓冲；
   - 始终不调用 `gzip.decompress` / `zlib.decompress` 这类一次性接口（`import gzip` 已移除）。
2. **异常压缩流明确失败**：新增 `ContentEncodingError(WebImportError)`（`.encoding`），覆盖
   损坏流、截断流（`decompressor.eof` 为假）、空体、与声明不匹配的流（如 zlib 数据配 `gzip`）、
   未知编码与堆叠编码（`br`、`zstd`、`gzip, br`）。它经既有映射返回 HTTP 400「URL 导入失败」。
3. **原始响应字节上限前移**：在解压之前先校验 `len(response.body) > max_bytes`
   → `ResponseTooLargeError`，不再依赖 transport 是否做了读取上限（防御性、可测试）。
4. **保留两条上限**：原始响应字节上限（读取 + 显式校验）与解压后字节上限（流式）都使用 `max_bytes`；
   metadata 新增 `content_encoding` 与 `decompressed_bytes` 便于取证。

未改动：Memory System、Capture、Quality、数据库 schema、冻结层任何文件；**未新增任何运行时依赖**
（只用标准库 `zlib`；对 `br`/`zstd` 明确失败而不是引入解压库）。

### 11.3 新增测试（`tests/test_web_import.py::CompressionTest`，10 个，全部使用合成数据）

| 测试 | 断言 |
| --- | --- |
| `test_gzip_bomb_is_stopped_during_decompression` | 32 MiB 炸弹 + 64 KiB 上限 → `ResponseTooLargeError`，`size_bytes ≤ 上限+1` 且 `< 展开大小`，**`tracemalloc` 峰值 < 8 MiB**（证明没有物化完整展开） |
| `test_deflate_bombs_are_stopped` | zlib 包装与 raw deflate 两种炸弹同样在上限处停止 |
| `test_decompressed_size_exactly_at_the_limit_is_accepted` | 解压后**恰好等于**上限（gzip 与 deflate）→ 正常导入，`decompressed_bytes == 上限` |
| `test_one_byte_over_the_limit_is_rejected` | 解压后上限 + 1 → `ResponseTooLargeError.size_bytes == 上限 + 1`（两种编码） |
| `test_corrupt_truncated_and_mismatched_streams_fail_explicitly` | 随机字节、截断流、去掉 gzip trailer、空体、zlib 数据配 gzip、垃圾配 deflate、截断 deflate → 全部 `ContentEncodingError`，`.encoding` 正确且消息含 URL |
| `test_unknown_and_stacked_content_encodings_are_rejected` | `br`/`zstd`/`compress`/`gzip, br`/`gzip, gzip`/未知 token → `ContentEncodingError` |
| `test_identity_and_missing_encoding_pass_through` | 无编码头、`identity`、`IDENTITY` → 原样通过并标记 `identity` |
| `test_raw_response_byte_cap_still_applies_to_compressed_bodies` | 压缩体本身超上限 → `ResponseTooLargeError`；`Content-Length` 预检依旧生效 |
| `test_encoding_errors_are_typed_and_body_free` | `ContentEncodingError` 是 `WebImportError`（UI/CLI → 400），消息中不含载荷字节 |
| `test_decompress_bounded_is_usable_standalone` | 直接调用 `decompress_bounded` 的正常/超限/未知编码/无编码四种路径 |

### 11.4 真实数据验证（非合成）

对一个真实站点强制请求压缩响应：`https://peps.python.org/pep-0020/` 带 `Accept-Encoding: gzip, deflate`
→ 服务端返回 `Content-Encoding: gzip`（3189 字节）。用 `decompress_bounded` 解出 10,656 字节：

* 与 `gzip.decompress` 的**完整参考解压结果逐字节相同**；
* 与同一页面 `identity` 响应的长度一致（10,656 = 10,656）；
* 同一个真实压缩流配 `max_bytes=1000` → `ResponseTooLargeError(size_bytes=1001 ≤ 1001)`。

### 11.5 结论与剩余边界

* 解压上限**现在确实在解压过程中强制生效**（有测试与内存峰值证据），不是"先解压后检查"。
* 两条上限都保留且都可测：原始响应字节上限（含 `Content-Length` 预检）与解压后字节上限。
* 剩余边界（如实记录）：上限按**单次响应**计算，不存在跨响应/跨请求的累计预算；
  不支持 `br`/`zstd`（明确失败，不假装支持）；DNS rebinding/TOCTOU 仍未完全防止（§3）；
  并发抓取没有全局内存上限（本工具是单用户本地服务，没有批量/并发导入入口）。
