# Knowledge Base 1.0 — Phase 5：PDF Importer

本文件是 KB 1.0 第五阶段的完整说明与实测证据。**主解析后端只增加一个依赖：`pdfminer.six`**，
没有引入 MarkItDown / PyMuPDF / pdfplumber / MinerU / OCR / Tesseract（见 §2 的实际安装记录）。

| 项目 | 状态 |
| --- | --- |
| Memory System Phase 1–4、KB P1–P4、MVP Web UI | **已冻结**，本阶段未改（指纹见 §12） |
| KB 1.0 Phase 5：PDF Importer（文字型 PDF） | ✅ 本阶段 |
| OCR / 扫描件识别 / 表格智能识别 / 公式识别 / 图片文字 / PDF 图片提取 / DOCX / PyMuPDF / MinerU / 多后端抽象 | ❌ 未实现（本阶段明确禁止） |

生成时间：2026-10-05；解释器 `D:\python\python.exe`（3.13.2）。

---

## 1. 实际修改了哪些文件

**新增**

| 文件 | 作用 |
| --- | --- |
| `personal_memory/importers/pdf.py` | `PdfImporter` / `PdfDocument` / `PdfImportResult` / 7 个类型化错误 / 逐页有界提取 / 元数据解码与标题规则 / `to_capture_request()` |
| `tests/pdf_fixtures.py` | 纯标准库合成 PDF fixture 构造器（WinAnsi 文本、Type0/Identity-H + `ToUnicode` 中文、UTF-16BE 元数据、RC4 V1/R2 加密、无文本、截断） |
| `tests/test_pdf_import.py` | 41 个测试：功能 1–7、限制 8–11、异常 12–16、架构与管线 17–22、CLI 23–24、Web UI 25–26 |
| `docs/KB1-PHASE5.md` | 本文件 |
| `docs/kb1p5-pdf-acceptance.{json,txt}` | 真实验收证据（计数/ID/哈希/元数据，刻意不含个人 PDF 正文与 Memory 标题） |

**修改（均在允许清单内，且为最小改动）**

| 文件 | 修改 |
| --- | --- |
| `personal_memory/importers/__init__.py` | 导出 PDF Importer API（含 `is_usable_pdf_title`） |
| `personal_memory/cli.py` | 新增 `import-pdf` 命令 + `_print_pdf_import` + 分发 |
| `personal_memory/web/server.py` | 新增 `POST /import-pdf`（复用 `PdfImporter` 与同一条 Capture 管线）+ 错误映射 `PdfError → 400 "PDF 导入失败"` |
| `personal_memory/web/views.py` | Import 页面新增「Import PDF」表单（复用既有 `attachFileUpload` 上传通道）+ 结果块显示 `page_count` / `extraction_backend` |

**未改（按 §二十二 的文件清单刻意不动）**

* `models.py`、`store.py`、`capture.py`、`extraction.py`、`retrieval.py`、`quality.py`、`lifecycle.py`、`db.py`、`docs/schema.sql` —— 指纹逐一相同；
* **没有新增 migration**（`MIGRATIONS` 长度仍为 2，`SUPPORTED_SCHEMA_VERSION` 仍为 2）；
* **没有新增 `SourceType`**（仍为 `text/chat/article/web/file`；PDF 一律映射为 `source_type=file`）；
* **没有创建 PDF 专用表**；
* **`README.md` 与 `personal_memory/__init__.py` 未修改**：这两个文件不在 §二十二 允许修改的清单里。因此
  顶层 `personal_memory.PdfImporter` 不存在（请用 `from personal_memory.importers import PdfImporter`），
  `version` 命令的版本号/`phase` 字符串仍是 `0.9.0` / `kb-1.0 phase-4 (url importer)`。
  这是刻意的范围遵守，不是遗漏；如需更新请批准后我再改。

## 2. 实际安装了哪些依赖及版本

```powershell
D:\python\python.exe -m pip install pdfminer.six
# -> Successfully installed cffi-2.1.1 cryptography-50.0.2 pdfminer.six-20260107 pycparser-3.0
D:\python\python.exe -c "import pdfminer; print(pdfminer.__version__)"
# -> 20260107
```

| 包 | 版本 | 说明 |
| --- | --- | --- |
| `pdfminer.six` | **20260107** | 唯一新增的直接依赖，纯 Python 包（`py3-none-any`） |
| `cryptography` | 50.0.2 | pdfminer.six 的依赖（加密 PDF 支持） |
| `cffi` | 2.1.1 | cryptography 的依赖 |
| `pycparser` | 3.0 | cffi 的依赖 |
| `charset-normalizer` | 3.4.9 | 已存在（未改动版本），pdfminer.six 的依赖 |

**环境隔离验证（Observed）**：安装只作用于 `D:\python`（KB 的解释器）。
检查前后：

* `D:\python` 的 `site-packages` 条目由 18 项变为 **26** 项（4 个新包各含代码目录与 `*.dist-info`），仅新增上述 4 个包；
* DSH 自带 Python（3.12.14）仍然**没有**任何 PDF 库；
* Codex runtime Python（3.12.14）仍然是 `pdfminer.six 20251230 / pypdf 6.10.0 / pdfplumber 0.11.9 / pypdfium2 5.13.0`，**未变**；
* 未安装 `markitdown`、`pymupdf`、`pdfplumber`、`mineru/magic-pdf`、`tesseract`、`pytesseract`、`easyocr`、`paddleocr`、`onnxruntime`（逐个 import 探测为 absent）；`tesseract` 二进制不在 PATH 上。

## 3. PdfImporter 的实际流程

```text
Path.stat() 大小检查（§五：先取大小，超 max_bytes 立即失败，不读文件）
   ↓  读取字节后再校验一次（防御「读取期间文件变大」）
   ↓  %PDF- header 嗅探（不是 PDF → PdfCorruptError）
   ↓  sha256(raw)
   ↓  PDFDocument(PDFParser(...), password="")          ← 加密检测（不猜密码）
   │     · 无需密码且允许文本提取的加密 PDF → 正常继续（真实行为，见 §4/§8）
   │     · PDFPasswordIncorrect        → PdfEncryptedError（需要密码）
   │     · PDFTextExtractionNotAllowed → PdfEncryptedError（权限禁止文本提取）
   ↓  doc.info[0] → 元数据（Title/Author/Creator/Producer/Subject/Keywords）
   ↓  PDFPage.get_pages(maxpages=max_pages+1, check_extractable=True)   ← 惰性逐页
   │     每页：
   │       page_count += 1；page_count > max_pages → PdfTooManyPagesError（立即停止）
   │       单页提取（TextConverter + PDFPageInterpreter，每页一个 device，内存有界）
   │       total_chars += len(page_text)；total_chars > max_text_chars → PdfTextTooLargeError
   │       normalise_page_text()：换行归一、空白折叠、CJK 字间噪声折叠、控制型 (cid:N) 丢弃
   ↓  页面拼接：[Page 1] … [Page 2] …（空页不产生标记，但仍计数）
   ↓  normalize_content() → 最终 content
   ↓  len(content) < min_chars → PdfEmptyTextError（明确提示可能是扫描件、需要 OCR）
   ↓  标题优先级：--title > PDF /Title（占位标题如 "untitled" 视为缺失） > 首页可识别标题行(4–120 字符) > 无
   ↓  PdfDocument(title, content, page_count, metadata, source_path, pdf_sha256, extraction_backend="pdfminer.six")
   ↓  to_capture_request(source_type=file, metadata={captured_from:"pdf", filename, pdf_page_count,
         pdf_pages_with_text, pdf_title/author/creator/producer/subject/keywords, pdf_sha256,
         content_chars, extraction_backend, title_source})
   ↓  CaptureService → MemoryFormationService → Phase 4 质量闸门 → Memory System
```

Importer 自身的边界（测试断言）：**不 import sqlite3/store/llm/urllib/requests/prompts/extraction/retrieval**，
`load()` 签名里没有 repository/LLM，`import_document()` 只接受 `CaptureService`（传 repository 抛 `ValidationError`）。
`source_path` 只存在于内存对象；进入 Source metadata 的只有 **filename**（无本机绝对路径）。

## 4. 安全限制

| 限制 | 默认值 | 语义 |
| --- | --- | --- |
| 文件字节上限 | `max_bytes = 8 MiB` | **读取前**用 `Path.stat()` 判定；读取后再判定一次。CLI `--max-bytes` |
| 页数上限 | `max_pages = 200` | **逐页迭代时**判定：第 201 页出现即失败（不会解析完整文件）；并用 `get_pages(maxpages=max_pages+1)` 双保险 |
| 提取文本上限 | `max_text_chars = 2 MiB` | **每页累计后**判定，超限立即失败 |
| 最小正文 | `min_chars = 200` | 低于此值 → `PdfEmptyTextError`（扫描件/图片型 PDF 的明确提示，不自动 OCR） |
| 加密/密码/权限 | — | **当前实现允许读取"无需密码且允许文本提取"的加密 PDF；只有"需要密码"或"禁止文本提取"的 PDF 会明确失败**（`PdfEncryptedError`）。**不尝试破解、不猜密码、不绕过权限**；`load()` 没有 password 参数 |
| 损坏输入 | — | 非 PDF / header 异常 / 缺 Root / 截断 / 语法错误 / stream 异常 → `PdfCorruptError` |
| 错误信息 | — | 只含文件名、失败阶段、解析器异常类型（截断 120 字符）；**绝不含二进制 payload 或正文** |
| 输出默认值 | — | CLI 与 Web UI 默认都不回显 PDF 正文（需程序化 `as_dict(include_content=True)`） |
| Web 上传三层 | — | ① base64 请求体上限 ② 解码后大小上限（`materialise_upload`）③ `PdfImporter.max_bytes`；三层互不替代（有测试） |

**失败不留半成品**：所有错误都在写入前抛出（`CaptureRequest` 尚未交给 Capture），实测 9 个失败场景的
`counts` 前后完全不变。

## 5. CLI 用法

```powershell
# 基本用法（默认 8 MiB / 200 页 / 2 MiB 文本 / 200 字符最小正文）
python -m personal_memory import-pdf "D:\path\document.pdf" --db data\memory.db
python -m personal_memory import-pdf "D:\path\document.pdf" --db data\memory.db --json

# 限制与覆盖
python -m personal_memory import-pdf doc.pdf --db data\memory.db `
       --title "自定义标题" --max-bytes 8388608 --max-pages 200 `
       --max-text-chars 2097152 --min-chars 200 --dry-run --no-quality-check
```

默认输出（实测）：`file / title(from …) / pages(pages with text) / text(chars, extraction_backend) /
metadata(title, author, creator, producer, subject, keywords) / sha256 / import / formation /
memories / sources / privacy`。**不打印 PDF 正文**；`--json` 同样不含正文（有测试断言）。

## 6. UI 用法

Import 页面新增「Import PDF（本地文字型 PDF）」卡片：选择 `.pdf` →（可选）填 Title → `Import PDF`。
复用既有上传通道（浏览器 `FileReader.readAsDataURL` → base64 隐藏字段），服务端
`POST /import-pdf` 调**同一个** `PdfImporter` + `CaptureService`；结果块显示
`page_count` / `content_chars` / `extraction_backend` / 生成的 Memory 与 Source 链接，**不回显正文**。
失败时经既有错误映射返回 400「PDF 导入失败：<具体原因>」；本机监听、Origin/Host 校验与隐私提示保持不变。

## 7. 自动化测试结果

```text
python -m unittest discover -s tests -t . -v
Ran 516 tests ... OK (skipped=2)
```

* 既有 475 个测试**全部继续通过**；本阶段新增 **41** 个（`tests/test_pdf_import.py`）；
* 全部离线：PDF fixture 由标准库在内存中合成，模型用脚本化 Mock LLM，**不依赖外网 / WSL / YourChar / MinerU / 用户私人文件**；
* 2 个 skip 仍是没有真实凭据时被跳过的可选真实 LLM 测试。

§十八 的 26 项逐条对应：

| # | 要求 | 测试 | 结果 |
| --- | --- | --- | --- |
| 1 | 正常 PDF | `test_1_normal_pdf_extracts_text` | PASS |
| 2 | 中文 PDF | `test_2_chinese_pdf_extracts_cjk`（合成 Type0/Identity-H + `ToUnicode`） | PASS |
| 3 | 多页 PDF | `test_3_multi_page_and_page_markers`、`test_3b`（空页跳过但计数） | PASS |
| 4 | metadata | `test_4_metadata_is_decoded_and_json_serialisable`（UTF-16BE 中文元数据、bytes 不得进入 metadata） | PASS |
| 5 | 用户自定义 title | `test_5_title_priority`（含占位标题 `untitled` 视为缺失） | PASS |
| 6 | 页分隔 | `test_3`、`test_6_normalisation_rules` | PASS |
| 7 | `CaptureRequest` | `test_7_document_to_capture_request`、`test_7b`（默认视图无正文） | PASS |
| 8 | 超过 max_bytes | `test_8_file_over_max_bytes_is_rejected_before_reading`（用 spy 证明**未调用 read_bytes**）、`test_8b`（读后防御） | PASS |
| 9 | 超过 max_pages | `test_9_pages_over_max_pages_stops_early`（400 页文件在第 6 页停止，<5s） | PASS |
| 10 | 超过 max_text_chars | `test_10_text_over_max_text_chars` | PASS |
| 11 | 恰好达到上限通过 | `test_11_exact_limits_pass`（字节/页数恰好等于上限通过，-1 立即失败） | PASS |
| 12 | 非 PDF | `test_12_non_pdf_file` | PASS |
| 13 | 损坏 PDF | `test_13_corrupt_pdf` | PASS |
| 14 | 截断 PDF | `test_14_truncated_pdf`（0.6 / 0.9 / 0.97 三种截断） | PASS |
| 15 | 加密 PDF | `test_15_encrypted_pdf`（需密码 / 禁止提取 / 允许提取三种；且 `load()` 无 password 参数） | PASS |
| 16 | 无文本 PDF | `test_16_no_text_pdf`、`test_16b`（提示含 OCR，page_count 正确） | PASS |
| 17 | Importer 不导入 sqlite/store | `test_17_importer_does_not_touch_the_database`（静态扫描 + 签名 + 传 repository 被拒） | PASS |
| 18 | 不直接调用 LLM | `test_18_importer_does_not_call_an_llm`（AST 级 import/符号检查） | PASS |
| 19 | 高价值 PDF → Memory | `test_19_high_value_pdf_forms_memories`（Source `source_type=file`、`captured_from=pdf`、可检索、有关联） | PASS |
| 20 | 低价值 PDF → Memory=0/Source=0 | `test_20_low_value_pdf_writes_nothing` | PASS |
| 21 | Formation 失败无半成品 | `test_21_formation_failure_leaves_nothing_behind`、`test_21b`（dry-run 零写入） | PASS |
| 22 | 重复导入复用现有去重 | `test_22_reimport_reuses_the_existing_dedupe`（Source 复用 + Phase 4 `duplicate`）、`test_22b` | PASS |
| 23 | CLI 成功 | `test_23_cli_success_with_mock_model`、`test_23b`（人类输出字段齐全且无正文） | PASS |
| 24 | CLI 失败 | `test_24_cli_failures_are_explicit`（5 类 + 限制类 + 不创建数据库）、`test_24b`、`test_24c` | PASS |
| 25 | UI PDF 上传成功 | `test_25_ui_pdf_upload_success`、`test_25b`（表单存在） | PASS |
| 26 | UI PDF 失败映射 | `test_26_ui_pdf_failures_are_mapped`（4 类 400 + 零写入 + 零模型调用）、`test_26b`（三层大小限制） | PASS |

## 8. 真实 E2E 结果（Observed）

真实运行：真实 **CLI 子进程** + 真实 **`web` 服务器子进程** + 真实 `deepseek-flash` Formation。
证据：[docs/kb1p5-pdf-acceptance.json](kb1p5-pdf-acceptance.json) / [.txt](kb1p5-pdf-acceptance.txt)（**总体 PASS**）。

**本地真实 PDF（未下载、未复制进仓库，直接就地读取）**

| 场景 | 输入 | 实测 |
| --- | --- | --- |
| A1 中文 + 元数据 | `课表-文字型.pdf`（Codex 工作目录，1 页） | exit 0，`persisted`，**1 Memory / 1 Source**；227 字符（CJK 103）；`title_source=first_page_line`（该 PDF 的 `/Title` 是占位值 **"untitled"**，按设计被忽略）；Source `source_type=file`、`captured_from=pdf`、`pdf_page_count=1`、`extraction_backend=pdfminer.six`、`local_path_in_metadata=false`、正文无 PDF 结构 |
| A2 中文 + 多页 | `高性价比人生指南-对我的启示与发展点.pdf`（KB 工作区，**20 页**） | exit 0，`persisted`，**4 Memory / 1 Source**；19,129 字符（**CJK 8,358**）；Source 含 `[Page 1]`…`[Page 20]`（20 个页标记）；无原始 PDF 结构与本机路径 |
| B 英文/技术 | `D:\pycharm\...\help\ReferenceCard.pdf`（PyCharm 快捷键参考卡，1 页） | exit 0，`persisted`，**2 Memory / 1 Source**；4,098 字符（Latin 2,838、CJK 0） |

**新进程检索（每个 `search` 都是独立 OS 进程）**：7 条导入产生的 Memory **7/7 全部被新进程检索命中**。

**真实 Web UI 上传**：真实 `web` 子进程 + `POST /import-pdf`（ReferenceCard.pdf）→ **HTTP 200 + 「PDF 导入完成」**，
Memory 7 → 9（+2），Source 未新增（因为同一文件此前已导入 → **Source 复用**，正是既有去重机制在工作）。

**失败场景（9/9 正确）**：合成 PDF 写到 `%TEMP%`（仓库内不落文件），全部 exit 3 且 `counts` 前后不变：

| 场景 | 实际错误类型 |
| --- | --- |
| 非 PDF 字节 | `PdfCorruptError` |
| 截断 PDF | `PdfCorruptError` |
| 需密码的加密 PDF | `PdfEncryptedError` |
| 权限禁止文本提取的加密 PDF | `PdfEncryptedError` |
| 无文本（扫描件替代品） | `PdfEmptyTextError` |
| `--max-bytes 1000`（真实 PDF） | `PdfTooLargeError` |
| `--max-pages 3`（真实 20 页 PDF） | `PdfTooManyPagesError` |
| `--max-text-chars 500`（真实 PDF） | `PdfTextTooLargeError` |
| `--min-chars 100000`（真实 PDF） | `PdfEmptyTextError` |

最终状态：`counts = {sources: 3, memories: 9, memory_sources: 9}`、`statuses = {active: 7, pending: 2}`、
`index_consistency.consistent = true`。本阶段真实验证共 4 次导入（A1/A2/B + UI 各 1 次），每次至少触发 1 次真实
Formation 调用；**质量闸门可能追加的冲突分类调用未逐次统计**（证据文件只记录导入结果，不记录调用次数）。

**Observed / Inference 分级（§二十一）**

* Observed：上面每一条都是本次真实运行的结果（含页数、字符数、CJK 计数、错误类型、计数变化、新进程检索命中）。
* Observed：中文 PDF *在本次测试中*（合成 Type0 fixture + 两个真实中文 PDF）成功提取；**不能**据此声称"所有中文 PDF 都能正确提取"。
* Observed：扫描件类型被识别为「无可用文本」并提示 OCR；**没有**实现 OCR，也不声称支持。
* **Inference（未实测）**：真正的**双栏论文**本次**没有**测试样本，因此不声称"双栏支持良好"；表格/公式/多栏阅读顺序同理不属于本阶段承诺。
* Inference：`_diag.pdf`（工作区里的英文 20 页文档）在**上一轮**实测中被 Formation 判为 `skipped`（0 Memory/0 Source，正文仍成功提取）；这属于 Formation 的价值判断，不是导入失败。

## 9. 已验证能力

* 本地**文字型 PDF**：单页/多页/中文/英文/带元数据/加密但允许提取 —— 逐页有界提取 → 干净文本（页标记 + 空行折叠 + CJK 碎片折叠 + 控制型 `(cid:N)` 丢弃）。
* PDF `/Title`、`/Author`、`/Creator`、`/Producer`、`/Subject`、`/Keywords` 读取（UTF-16BE 带 BOM 的中文元数据实测正确解码），页数记录；**bytes 不会进入 Source metadata**（JSON 可序列化，有测试）。
* 标题优先级：`--title` > `/Title`（占位值忽略）> 首页可识别标题行 > 无（允许缺失，不失败）。
* 三条硬限制（字节/页数/文本）**在过程中**生效：超限文件不读、超限页数不解析完、超限文本立即停。
* 加密 PDF：**无需密码且允许文本提取 → 正常导入**；**需要密码或权限禁止文本提取 → `PdfEncryptedError`**；
  损坏/截断/非 PDF → `PdfCorruptError`；无文本 → `PdfEmptyTextError` 并提示 OCR；全部零残留。
* `PdfDocument → CaptureRequest(source_type=file)` → 既有 Capture → Formation → Phase 4 闸门 → Memory/Source；高价值形成 Memory（含关联），低价值 0/0，重复导入复用既有 Source 去重。
* CLI `import-pdf`（7 个开关）与 Web UI `POST /import-pdf` 走**同一条**管线；默认都不回显正文。

## 10. 未实现能力（本阶段禁止，均未做）

OCR / 扫描件识别 / Tesseract / easyocr / paddleocr、表格智能识别（不引入 pdfplumber）、公式识别、
图片内文字、PDF 图片提取、复杂阅读顺序重建、PyMuPDF、MarkItDown、MinerU、DOCX/XLSX 等其它格式、
多后端抽象层、Embedding / 向量库 / 语义检索 / RAG / QA / Graph / MCP / UI 重设计、批量 PDF 导入。

## 11. 仍存在的边界（如实记录）

1. **只支持文字型 PDF**：扫描件/图片型 PDF 会被判为「无可提取文本」并提示需要 OCR——不会自动 OCR。
2. **多栏/表格/公式不做版面还原**：单栏散文效果最好；简单多栏通常可读但顺序不保证；表格会被拆成文本行（本阶段只承诺"表格存在不导致导入失败"）。本机**没有双栏论文样本**，因此该场景未实测。
3. **逐页提取依赖 PDF 自身的 `ToUnicode`/编码表**：完全没有 ToUnicode 的正文字体会退化为 `(cid:N)`（控制型已被丢弃，但非控制型未映射字形会保留为标记）。
4. **文本是"重排文本"，不是原始版式**：多空格、缩进、图表位置等版式信息会丢失；这是纯文本导入的固有代价。
5. **页标记是 `[Page N]`**：会进入 Source 正文与 Formation 输入（本阶段的有意设计，数量与页数一致）；空页不产生标记。
6. **单文件、单次、无并发控制/无续传**：一次导入一个 PDF；不扫描目录、不批量。
7. **限制是"每文件"预算**：没有跨文件的累计预算；`max_text_chars` 是字符数而非 token 数（大 PDF 会让 Formation 的 prompt 变大、变慢、变贵，本阶段不做摘要或分块）。
8. **`pdfminer.six` 会向 stderr 打印字体警告**（例如 "Could not get FontBBox from font descriptor…"），属非致命警告；不会写入正文，也不影响导入结果。
9. **加密 PDF 的"允许提取"分支**：能正常导入（自动化测试 `test_15_encrypted_pdf` 覆盖；本次 E2E 证据文件
   `kb1p5-pdf-acceptance.json` 只覆盖"需要密码"与"禁止提取"两个失败分支，未包含允许提取的加密样本）。
   该分支不做任何解密后的额外校验（例如不校验签名）。
10. **未更新 `README.md` / 顶层 `__init__.py`**（不在本阶段允许的文件清单内）：顶层没有 `PdfImporter`，版本号与 `phase` 字符串仍是上一阶段的值。
11. **测试 fixture 是合成 PDF**：它们覆盖编码/加密/损坏等分支，但不能替代真实世界的 PDF 多样性；真实 E2E 只覆盖了本机现有的 3 份真实 PDF（1 页中文带占位标题、20 页中文、1 页英文参考卡）。

## 12. 指纹与完成标准

**本阶段文件指纹（`sha256[:16].upper()` / 字节）**

| 文件 | 指纹 | 字节 |
| --- | --- | --- |
| `personal_memory/importers/pdf.py`（新） | `46E11F67A62FC456` | 30244 |
| `personal_memory/importers/__init__.py`（改） | `44E3D89A7DB63072` | 6359 |
| `personal_memory/cli.py`（改） | `63AF1E68BA4B3DF2` | 49507 |
| `personal_memory/web/server.py`（改） | `90E1ED6845F2550C` | 38145 |
| `personal_memory/web/views.py`（改） | `076065448F264443` | 33425 |
| `tests/pdf_fixtures.py`（新） | `55F99C013B9A6EC1` | 14265 |
| `tests/test_pdf_import.py`（新） | `B2B1352394492714` | 40305 |

**冻结层指纹（实测未变）**：`models.py 6F892D6888319836`、`store.py 0AC465BC62958D3D`、
`capture.py F33696E7C307F6CC`、`extraction.py 147D50D908EFA216`、`retrieval.py FF35E0117691437D`、
`quality.py 4C6D3EA3081E84C4`、`lifecycle.py A9BF67FBBE7C5C60`、`db.py F6EE1E73E1C747B9`、
`docs/schema.sql 160069D9C0D8B396`、`errors.py B62C84CBAEB39AED`、`llm.py E3D1DCA0FB619130`、
`prompts.py 7130F83C7E21856E`、`importers/files.py 4BBD2B96DA66630F`、`importers/chat.py 8BA8D408A65ADE5F`、
`importers/web.py 482C509E4E0C517A`、`personal_memory/__init__.py 7AC340FB3EBED1F9`、`README.md`（未改）。

**完成标准（§二十三）**

| 项目 | 结果 |
| --- | --- |
| pdfminer.six 安装并记录版本 | ✅ 20260107（+ cryptography 50.0.2 / cffi 2.1.1 / pycparser 3.0），仅装在 `D:\python` |
| PdfImporter 完成 | ✅ 逐页有界提取 + 元数据 + 标题规则 + `to_capture_request` |
| 有界文件大小 | ✅ `stat` 先判定（spy 证明未读取）+ 读后复核 |
| 有界页数 | ✅ 逐页判定（400 页在第 6 页停止） |
| 有界文本 | ✅ 每页累计判定 |
| 加密 PDF 明确失败 | ✅ **需要密码 → `PdfEncryptedError`**、**权限禁止文本提取 → `PdfEncryptedError`**（E2E 证据覆盖）； **无需密码且允许提取 → 正常导入**（自动化测试覆盖，E2E 证据未含该样本） |
| 损坏 PDF 明确失败 | ✅ 非 PDF / 截断 / 语法错误 |
| 空文本明确失败 | ✅ 含 OCR 提示 |
| PDF → CaptureRequest | ✅ `source_type=file`，无 `SourceType` 改动 |
| 高价值 PDF → Memory | ✅ 测试 + 真实（A1 1 条 / A2 4 条 / B 2 条） |
| 低价值 PDF → 无 Memory/无 Source | ✅ 测试 + 真实（`_diag.pdf` 被 Formation 判 `skipped`） |
| 重复机制复用现有系统 | ✅ Source `content_hash` 复用 + Phase 4 `duplicate`（真实 UI 上传亦复用既有 Source） |
| CLI 完成 | ✅ `import-pdf` + 7 个开关 + 成功/失败路径 |
| UI PDF 上传完成 | ✅ Import 页表单 + `POST /import-pdf` + 真实 200 |
| 自动化测试全部通过 | ✅ 516（既有 475 全通过 + 新增 41） |
| 中文真实 PDF E2E | ✅ 1 页带元数据 + 20 页多页 |
| 英文/技术真实 PDF E2E | ✅ PyCharm ReferenceCard（2 Memory） |
| 新进程检索通过 | ✅ 7/7 |
| 无半成品 | ✅ 9/9 失败场景计数不变 |
| 无 schema/migration 改动 | ✅ `MIGRATIONS` 长度 2、schema v2、无新表、无新 `SourceType` |

**停止点**：Phase 5 到此为止。未开始 OCR / DOCX / 表格识别 / PyMuPDF / MinerU / Embedding / RAG / Graph / UI 重设计 / 多后端抽象 / MCP。
