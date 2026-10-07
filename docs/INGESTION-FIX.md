# Ingestion 修复：PDF 与 URL（KB 1.0）

范围：只修**入口与错误提示**，复用既有解析器，不动 Memory/Source 模型、Formation、Quality Gate、
Lifecycle、Retrieval、数据库 schema、Capture/Import 处理语义。无新依赖、无新数据模型、无新业务接口。

---

## 1. 根因

### PDF 根因（解析器本来就是好的）

真实实测：`https://arxiv.org/pdf/2005.11401`（885 KB、19 页）用**既有的 pdfminer.six 解析器**
`PdfImporter.load()` 一次通过：**69534 字符 / 0.4 秒**。所以"正常文本型 PDF 吃不下"不是解析能力问题，
而是入口与提示问题：

1. **唯一可见文件入口不接受 PDF**：首页只有一个 `input[type=file]`，`accept=".txt,.md,.markdown,.chat,.json"`，
   而且它的提交处理器**无条件**发到 `/import-chat`（聊天解析器）——选中 PDF 要么被文件对话框过滤掉，
   要么被聊天解析器拒绝。只有"拖拽到卡比"这条路径会按扩展名分流到 `/import-pdf`，可发现性差。
2. **把 PDF 链接当网址喂**：`WebImporter` 的 `ALLOWED_CONTENT_TYPES` 只有 `text/html|xhtml|text/plain`，
   `application/pdf` 被归为 `UnsupportedContentTypeError` → 提示"这一页不是文字内容 / 只认得网页文字"。
3. **PDF 上传的体积闸门接错了常量**：`_post_import_pdf` 用 `min(context.max_bytes, MAX_PDF_BYTES)`，
   而 `context.max_bytes` 是**网页抓取**上限（1 MB），于是 1 MB–8 MB 的正常 PDF 全部被
   `PdfTooLargeError`（"这一口太大了"）拒绝，尽管 `MAX_PDF_BYTES`/`max_upload_bytes` 都是 8 MB。
4. **提示容易误导**：扫描件/图片型 PDF 的文案"看起来像是扫描件或图片型 PDF"，用户读成"系统只认图片"。
   实际项目里**没有任何**图片/视觉/OCR 能力（全仓 grep 确认：无 vision / MinerU / MarkItDown / pdf2image）。

### URL 根因（静态抓取 + 动态页面 + 文案笼统）

1. `WebImporter` 是纯 **stdlib urllib 静态 HTML** 抓取 + 启发式正文提取（`min_content_chars=200`），
   没有 JS 引擎。
2. Anthropic 那篇是服务端渲染 → 19179 字符 → 成功。
3. Khan Academy 是 **JS 渲染 SPA**：服务器返回的 HTML 里没有正文 → `EmptyContentError`
   （"extracted 0 characters, minimum is 200"）。失败层 = **HTML 正文提取层**（fetch 成功、content-type 合法）。
4. 文案表里 `EmptyContentError` **没有条目**，它继承 `WebImportError` → 只能显示
   "这个网址暂时读不出来 / 可以稍后再试，或者换一个页面。"，用户看不到真实原因。

## 2. 当前真实链路（修复前）

```text
PDF（文件）
用户选择文件 → 可见 picker（accept 无 .pdf）→ 提交 → /import-chat（聊天解析器）
                                                   ↓
                                              失败：这份聊天记录看不懂
PDF（拖拽）
拖到卡比 → route() 按扩展名 → /import-pdf → PdfImporter（pdfminer）→ 成功 ✔
PDF（当网址）
喂一个网址 → /import-url → WebImporter.fetch → content-type=application/pdf
                                                   ↓
                                  失败：UnsupportedContentTypeError（只认得网页文字）
URL（动态页面）
喂一个网址 → /import-url → WebImporter.fetch → 200 HTML → extract_content
                                                   ↓
                            失败：EmptyContentError → 只显示"稍后再试/换个页面"
```

## 3. 最小修复

| 文件 | 改动 | 为什么 |
| --- | --- | --- |
| `personal_memory/importers/web.py` | 新增 `PdfUrlContentError`（**继承** `UnsupportedContentTypeError`，携带下载好的 PDF 字节，错误文本不含正文）+ `_looks_like_pdf()`；`fetch()` 的 content-type 分支在遇到 `application/pdf`（或未标注类型但正文以 `%PDF-` 开头）时抛出它 | 复用**同一套** URL 策略（SSRF 校验、重定向、大小上限、UA），一次下载把字节交给 PDF 解析器；HTML 路径行为完全不变 |
| `personal_memory/importers/__init__.py` | 导出 `PdfUrlContentError` | 保持导入面一致 |
| `personal_memory/web/server.py` | `/import-url` 捕获 `PdfUrlContentError` → `_import_pdf_bytes()`；把 PDF 上传的核心抽成 `_import_pdf_upload()`（上传与 URL 共用一条链）；`_import_pdf_upload` 的 importer 上限改为 `min(context.max_upload_bytes, MAX_PDF_BYTES)`（**修掉 1 MB 误限**）；`_render_error` 对 `WebImportError`/`PdfError` 改用同一句精确中文原因 | 一条 PDF 链、一处 URL 策略；大小闸门与 `MAX_PDF_BYTES`/`max_upload_bytes` 一致；无脚本表单路径也给出准确原因 |
| `personal_memory/web/views.py` | 首页可见文件入口 `accept` 增加 `.pdf`，提交按扩展名分流：`.pdf→/import-pdf`、`.chat/.json→/import-chat`、其余（txt/md）→`/import-file`；入口名"喂聊天记录"→"喂一个文件"；新增 `EmptyContentError` 文案（动态渲染说明），并明确 `PdfEmptyTextError`（没有文字层、1.0 不支持图片识别）与 `UnsupportedContentTypeError`（只认得 HTML/纯文本） | 入口与真实能力一致；txt/md 不再被误当聊天记录；失败原因可理解 |
| `tests/test_ingestion_entry.py`（新） | 11 个测试：入口 accept/分流、PDF 上传端到端、PDF 当网址（含 `application/octet-stream` 嗅探）、损坏 PDF 当网址不写库、HTML 回归、四类文案、异常兼容性、动态页面真实链路 | 锁住修复 |
| `tests/test_pdf_import.py`、`tests/test_web.py` | 跟随更新 3 处断言（accept 含 .pdf；层 3 体积闸门改为断言"不再被网页上限 1 MB 误限"；失败文案改为精确中文） | 旧断言固化了 bug 行为 |

**是否复用现有 parser**：是（pdfminer.six 的 `PdfImporter`，一行未改）。
**是否新增依赖**：否。**是否涉及数据库**：否（schema/模型未动）。
**是否影响 Chat / TXT / Markdown / URL / Memory**：HTML URL 行为不变（仅新增 PDF 分支）；
`.chat/.json` 仍走聊天解析；`.txt/.md` 从"误走聊天解析"改为走文件导入（这是修复，不是语义变更——
文件导入器本来就负责 txt/md）；Memory/Formation/Source 链完全复用。

## 4. 验证（真实运行服务 + 真实模型 + 真实数据库）

| 场景 | 结果 |
| --- | --- |
| `https://arxiv.org/pdf/2005.11401` 当**网址**喂 | `/import-url` → 200，「吃饱了~ / 记住了 5 件事」；Source 69534 字符（真实提取正文，`captured_from=pdf`），memories +5 |
| 同一个 PDF 从**可见文件入口**喂 | `/import-pdf` → 200，「记住了 5 件事」 |
| 同一个 PDF **拖拽**喂 | `/import-pdf` → 200，「记住了 4 件事」 |
| `regression.md` / `regression.txt` 从文件入口 | `/import-file` → 200（md 判定为"没有长期价值"= 正常 0 记忆结果；txt 记住 1 件） |
| `regression.chat` 从文件入口 | `/import-chat` → 200，记住 1 件（聊天语义保持） |
| Anthropic 页面 | `/import-url` → 200，「记住了 5 件事」（回归通过） |
| Khan 页面 | `/import-url` → 400，提示 **"这个页面读不出正文 / 它可能是靠脚本动态渲染的页面；可以换成静态页面，或者直接把正文复制过来喂。"**，数据库无变化 |
| 纯文本 / 文本回归 | `/capture` → 200，记住 1 件 |
| >1 MB PDF（1.25 MB，真实 HTTP） | 通过大小闸门，只在解析层报"这份 PDF 好像坏掉了"（修复前会误报"这一口太大了"） |
| 损坏 PDF（885 KB 之外的小文件） | `/import-pdf` → 400「这份 PDF 好像坏掉了 / 文件不完整，暂时读不出来。」，无 Traceback，数据库不变 |

## 5. 已知限制与观察（未改代码）

1. **PDF 当网址的上限仍是 1 MB**：`/import-url` 的抓取上限沿用 `context.max_bytes`（1 MB）。
   arXiv 这篇 885 KB 通过；更大的 PDF 走**文件入口**（上限 8 MB）。
2. **arXiv PDF 的 Source 标题是 `[Page 1]`**：该 PDF 没有可用的 `/Title`，标题回退取了第一行 = 导入器自己加的页标记。
   内容与检索都正常；标题的更好回退属于 Capture/Source 层（冻结），本次未改。
3. **arXiv 首页有少量水印乱序字符**：pdfminer 的排版产物（arXiv 侧边水印），不影响正文与记忆形成。
4. **动态页面不做 JS 渲染**：1.0 明确不引入浏览器渲染；现在会给出准确原因。
5. **没有 OCR**：扫描件/图片型 PDF 依然读不了，但提示已明确说清原因。

## 6. 测试

```text
python -m unittest discover -s tests -t .
Ran 597 tests ... OK (skipped=2)
```

基线 586 → 597（+11，全部来自 `tests/test_ingestion_entry.py`）；3 处旧断言跟随修复更新，未删除任何测试。
