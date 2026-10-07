# 卡比 Capture Home（喂知识）第一阶段

把已经冻结的 Capture / Import 能力重新组织成一个**卡比喂知识**首页。本阶段**只改 Web 展示层**
（`personal_memory/web/views.py`、`personal_memory/web/server.py`）与对应测试；Memory System /
Formation / 价值判断 / Quality / 生命周期 / Source 关联 / Retrieval / 各 Importer 全部未改（§7 有指纹证据）。

---

## 1. 开发前审计结果（只读检查，实测）

| # | 检查项 | 实际情况 |
| --- | --- | --- |
| 1 | 页面入口 | `web/server.py:_handle_get`；`GET /` 原来渲染 `views.home_page()`（统计仪表盘） |
| 2 | 路由 / 页面切换 | **纯服务端渲染 + 路径分发**，`_handle_get` / `_handle_post` 里一串 `if path == "..."`；无前端框架、无客户端路由、无构建步骤 |
| 3 | Capture 页面 | `GET /capture` → `views.capture_page()`；`POST /capture` → `CaptureService.capture()` |
| 4 | Import 页面 | `GET /import` → `views.import_page()`：四个表单（文件 / 聊天 / URL / PDF） |
| 5 | Capture API / service 调用 | 处理器内部构造 `context.capture_service(captured_from=...)` → `CaptureService` → `MemoryFormationService` → Phase 4 闸门；**没有公开 JSON API**，结果经 303 + 一次性 `Flash`（内存 token）返回 |
| 6 | 文件上传逻辑 | 前端 `attachFileUpload()`：`FileReader.readAsDataURL` → 隐藏 `<input name="content_base64">`；服务端 `_read_form`（请求体上限）→ `materialise_upload`（解码 + 大小上限 + 临时目录）→ Importer，用完 `shutil.rmtree` |
| 7 | URL 导入 | `POST /import-url` → `WebImporter`（DNS 解析 + 私网/保留地址拒绝 + 重定向策略 + 解压上限），**保留不动** |
| 8 | Chat 导入 | `POST /import-chat` → `ChatImporter`（`format=auto/roles/json`，角色文本 / 标准 JSON），**保留不动** |
| 9 | PDF 导入 | `POST /import-pdf` → `PdfImporter`（逐页有界、加密/损坏/无文本类型化错误），**保留不动** |
| 10 | 当前 CSS / JS / HTML | 全部内联：`views._STYLE`（一个字符串）+ 一个 `attachFileUpload` 脚本；**项目内 0 个图片资源**，无外部依赖、无 CDN |
| 11 | 错误 / 成功处理 | 成功：`put_flash(Flash(kind="ok", title="…完成"))` + `303 → /import?flash=<token>`；失败：异常 → `classify_error()` 映射状态码 + `error_page`（含原始异常文本，供本地排障） |
| 12 | 是否已有拖拽能力 | **没有**（`web/*.py` 中 `dragover` / `drop` / `dataTransfer` 匹配数为 0） |

**审计结论**：服务端渲染的结构可以**渐进改造**，不需要换框架；缺三样东西——
① 首页缺少"喂"的交互与角色；② 没有拖拽；③ 没有一个让前端拿到**真实业务结果**（而不是"HTTP 完成"）的通道。

**资产缺口（必须先报告）**：本机（工作区、`~/.dsh`、YourChar、`Documents`、以及整个项目）
**没有找到任何 Kirby / 卡比 素材帧**，项目里也没有任何图片文件。因此本阶段**不能**使用
Kirby Super Star Ultra 真实帧；按 §十二 的要求先报告缺口，并实现为**与素材解耦的状态组件**：
替换素材只需填 `views.KIRBY_FRAMES`（见 §3）。

## 2. 修改了哪些文件

| 文件 | 修改 |
| --- | --- |
| `personal_memory/web/views.py` | 首页改为 `feed_page()`（喂知识）；新增 `KIRBY_FRAMES` / `KIRBY_SIZE_PX` / `KIRBY_STATES` / `kirby_component()` / `_kirby_script()`（状态机 + 喂食流程）/ `FEED_ERROR_COPY` + `feed_error_message()`（中文友好文案）；导航改中文；标题改中文；新增 `_FEED_STYLE` |
| `personal_memory/web/server.py` | `GET /` → `feed_page()`；新增 `_wants_json()` / `_send_json()` / `_finish_post()` / `_outcome_note()`：**同一条管线**的结果按请求方式交付（fetch → JSON，普通表单 → 303 + Flash）；`_render_error` 在 JSON 模式下只回中文友好文案；`import json` |
| `tests/test_feed_home.py`（新） | 12 个测试：首页结构 / 状态机（从页面里解析出转换表并模拟）/ JSON 绑定 / 低价值不算失败 / 文件·聊天·PDF·URL / 失败文案不泄露 / 无模型 / 传统表单路径回归 |
| `tests/test_web.py` | 只改两个与首页相关的断言（旧仪表盘已从首页移除，"pending" 与徽章 CSS 同名） |
| `docs/KIRBY-CAPTURE-HOME.md`（新） | 本文件 |
| `docs/kb9-feed-acceptance.{json,txt}`（新） | 真实验收证据 |

**没有**新增任何 Python 依赖、前端框架、构建步骤或数据库。

## 3. 首页结构与卡比状态组件

```text
GET /（喂知识）
└── 卡比（唯一视觉主体，inline SVG 占位，clamp(280px, 32vw, 360px)，默认 320px）
    ├── #kirby-mouth      ← 点击输入文字；文件拖拽命中区
    ├── #kirby-feedback   ← “吃饱了~”（上浮 + 淡出，约 1.8s）
    └── #kirby-note       ← 第二层轻提示（“记住了 N 件事” / “这次没有留下长期记忆”）
├── 提示一行：「点卡比的嘴输入文字；也可以直接把文件拖进来，或者按 Ctrl+V 粘贴。」
├── 后备入口：「喂一个网址」「喂聊天记录」（点开才出现，不占首屏）
└── 页脚：隐私提示 + 传统采集页 / 传统导入页 / 我的记忆 / 我的来源 / 搜索
```

首页**没有**统计数字、文件列表、最近记录、技术状态（`class="stat"`、数据库路径、模型名、"API Key" 均不在首页）。
导航为中文：`喂知识 / 我的记忆 / 我的来源 / 搜索`；`/capture`、`/import` 仍可从页脚进入（不破坏旧能力）。

**素材解耦**（§三）：

```python
KIRBY_FRAMES = {"idle": None, "open": None, "inhale": None, "closed": None}   # 填入真实帧 URL 即可
KIRBY_SIZE_PX = 320
```

动画逻辑**只**设置 `data-kirby-state`；`kirby_component()` 在 `KIRBY_FRAMES` 四个状态都配置时改用 `<img>` 帧，
否则使用 inline SVG 占位图（笑眼 / 三套嘴型：合嘴、张嘴、吸入 + 气流线）。**换素材不改任何逻辑。**

## 4. 卡比状态机（内部）

```text
IDLE → DETECTED → OPEN → ABSORBING → CLOSED → PROCESSING → FEEDBACK → IDLE
                                     ↘ 任意失败 → IDLE（绝不卡住）
```

用户看到的字样（英文状态不出现在界面）：`闭嘴 → 张嘴 → 吸进去 → 合嘴 → 消化 → 吃饱了~`。

* 转换表 `KIRBY_TRANSITIONS` + 纯函数 `nextKirbyState(state, event)`（`views._kirby_script()`）。
* 每个状态都有 `reset` → `IDLE`；未知事件回落到 `IDLE`（不会停在张嘴 / 吸入）。
* 视觉状态由 CSS `[data-kirby-state]` 规则驱动：张嘴 = 嘴型切换 + 轻微放大；吸入 = 大嘴 + 气流线；
  合嘴 = 嘴型回位；消化 = 轻微下沉；反馈 = 轻微上浮 + 文字上浮淡出。**只有 transition，没有粒子/发光/毛玻璃。**

## 5. 每种输入如何触发（全部走现有端点，未新增后端能力）

| 输入 | 触发方式 | 端点 |
| --- | --- | --- |
| 文字 | 点卡比嘴 → 浮动文本框 → 回车/「喂给卡比」 | `POST /capture`（`content`） |
| 文字（粘贴） | 页面任意位置 `Ctrl+V`：纯网址 → URL 导入，其它 → 文字 Capture | `POST /capture` / `POST /import-url` |
| TXT / Markdown | 拖到卡比（`drop` → 张嘴 → 吸入）| `POST /import-file` |
| 聊天记录 | 拖入含 `[USER]` / `## User` / `{"messages":…}` 的角色文本 → Chat Importer；或「喂聊天记录」后备入口 | `POST /import-chat`（`format=auto`） |
| URL | 直接粘贴网址；或拖入 `text/uri-list` / 文本中的网址；或「喂一个网址」后备输入框 | `POST /import-url` |
| PDF | 拖入 `.pdf` | `POST /import-pdf` |

拖拽状态与视觉严格对应：`dragenter/dragover` → `DETECTED`（虚线高亮）→ `drop` → `OPEN` → `ABSORBING`（内容缩小淡出由状态切换表达）→ `CLOSED`。
URL 拖拽受浏览器限制，因此**始终保留**「喂一个网址」可点输入框（§四要求）。
一次只处理第一个文件（多文件取首个）；`busy` 期间忽略新的拖拽/粘贴。

## 6. “吃饱了~”如何与真实处理结果绑定（§六）

```text
前端 fetch(POST, X-Requested-With: fetch, Accept: application/json)
   ↓ 服务端照旧执行 CaptureService / Importer / Formation / Phase 4 闸门（管线一行未动）
   ↓ _finish_post()：管线**返回之后**才写出 JSON
   {ok, feedback:"吃饱了~", title, detail, hint, note, memory_count, source_count, worth_remembering}
   ↓ 前端收到 JSON 后：CLOSED → PROCESSING → FEEDBACK →（约 1.8s）IDLE
```

* `feedback`（“吃饱了~”）与 `note`（“记住了 N 件事” / “这次没有留下长期记忆”）**都来自这次真实结果**，
  不存在"上传成功就算成功"的提早反馈：前端在 `fetch` 未返回前最多走到 `PROCESSING`。
* **Memory = 0 / Source = 0 一律是 `ok: true`**（正常业务结果）；真实验收里"低价值内容"与一份 Markdown
  都返回 `ok=true` + “这次没有留下长期记忆”。
* 未配置模型时返回 `ok:false` + “卡比还没准备好”（HTTP 503），不显示技术细节。

## 7. 失败状态如何恢复（§九）

* 失败经既有 `classify_error()` 决定状态码，JSON 模式下由 `feed_error_message()` 只按**异常类名**取中文文案
  （33 条映射：PDF 损坏 / 有密码 / 无文字、网址被拒 / 读不出、聊天格式、文件类型、模型未配置…）。
* 前端 `showError()`：显示“这次没吃下去”类反馈 + 用户层原因 → **强制回到 IDLE**（`processing → fail → idle`），
  `busy` 复位；`.catch()`（网络异常）同样回 IDLE；每个请求结束后还有一次 `if (!busy) setState("idle")` 兜底。
* 不泄露 API / 数据库 / 堆栈 / 模型名 / 内部异常名 / URL 安全策略细节（测试与真实验收都断言无泄露）。
* 传统表单路径（无 fetch 头）**行为不变**：仍是 `303 → /import?flash=…` + 原横幅文案，失败仍是原来的错误页。

## 8. 运行了哪些测试

```text
python -m unittest discover -s tests -t .
Ran 528 tests ... OK (skipped=2)
```

* 原有 516 个全部继续通过；新增 12 个（`tests/test_feed_home.py`）；只改了 2 个与首页相关的旧断言。
* 状态机**真实执行**（不只是代码存在）：把**服务端实际发出的那份 JS**（`views._kirby_script()`）落盘，
  用本机 node 24.19.0 执行：

```text
states declared: 7 idle,detected,open,inhale,closed,processing,feedback
  PASS  happy path -> ["idle","detected","open","inhale","closed","processing","feedback","idle"]
  PASS  failure during processing returns to idle -> ["idle"]
  PASS  failure during absorbing returns to idle -> ["idle"]
  PASS  unknown event never sticks -> ["idle"]
  PASS  reset from every state -> ["idle", ...]
STATE MACHINE: PASS        (node exit 0)
```

## 9. 每种输入是否实际验证

真实验收：真实 `web` 服务器子进程 + 真实 `deepseek-flash` Formation + 与前端 JS **完全相同**的 HTTP 契约
（`X-Requested-With: fetch`、`Accept: application/json`、同样的表单字段）。
证据：[docs/kb9-feed-acceptance.json](kb9-feed-acceptance.json) / [.txt](kb9-feed-acceptance.txt)（**OVERALL PASS**）。

| 输入 | 是否实际验证 | 实测结果 |
| --- | --- | --- |
| 文字 | ✅ 真实 | HTTP 200、`ok=true`、`feedback="吃饱了~"`、`note="记住了 1 件事"`、1 Memory / 1 Source |
| TXT / Markdown | ✅ 真实 | HTTP 200、`ok=true`、`feedback="吃饱了~"`、`note="这次没有留下长期记忆"`（模型判定无需长期记忆，**未显示失败**） |
| Chat | ✅ 真实 | HTTP 200、`ok=true`、`feedback="吃饱了~"`、1 Memory（角色文本 `format=auto`） |
| URL | ✅ 真实 | HTTP 200、`ok=true`、`feedback="吃饱了~"`、2 Memory / 1 Source（真实抓取 `peps.python.org/pep-0020/`） |
| PDF | ✅ 真实 | HTTP 200、`ok=true`、`feedback="吃饱了~"`、1 Memory（真实 1 页英文 PDF） |
| 低价值内容 | ✅ 真实 | HTTP 200、`ok=true`、`note="这次没有留下长期记忆"`、Memory=0 / Source=0，**不是失败** |
| 失败输入 | ✅ 真实 | 4/4：损坏 PDF「这份 PDF 好像坏掉了」、无文字 PDF「这份 PDF 里没有文字」、不允许的网址「这个网址卡比不能去」、空内容「先写点东西再喂」；全部 400 + 中文 + 零泄露；随后 `GET /` 仍 200（可继续喂） |
| 传统表单路径 | ✅ 真实 | 无 fetch 头 → 303 → 横幅「处理完成」，旧页面行为不变 |
| 数据库一致性 | ✅ 真实 | 验收库 3 Sources / 5 Memories / 4 链接，`index_consistency.consistent=true`，schema v2、migration 数 2（未变） |
| 状态机转换 / 失败恢复 | ✅ node 执行 | 见 §8（happy path + fail→idle + unknown→idle + reset） |
| **浏览器中的实际视觉 / 动画渲染** | ❌ **未验证** | 本机没有可用浏览器自动化（无 Playwright / 无交互式浏览器工具）。已验证的只是：服务端发出的 HTML/CSS/JS 契约、状态机在 node 中的真实执行、以及真实 HTTP 链路；**鼠标拖拽、CSS 过渡与动画的观感未经浏览器实测**。 |
| URL 拖拽（浏览器 `text/uri-list` 行为） | ❌ **未验证** | 浏览器对 URL 拖拽的支持差异无法在本机实测；已按要求保留可点击的 URL 后备输入框。 |

## 10. 是否修改了任何后端冻结层

**没有。** 逐文件 `sha256[:16]` 比对（本阶段前后）：

`models.py 6F892D6888319836`、`store.py 0AC465BC62958D3D`、`capture.py F33696E7C307F6CC`、
`extraction.py 147D50D908EFA216`、`retrieval.py FF35E0117691437D`、`quality.py 4C6D3EA3081E84C4`、
`lifecycle.py A9BF67FBBE7C5C60`、`db.py F6EE1E73E1C747B9`、`docs/schema.sql 160069D9C0D8B396`、
`errors.py B62C84CBAEB39AED`、`llm.py E3D1DCA0FB619130`、`prompts.py 7130F83C7E21856E`、
`importers/files.py 4BBD2B96DA66630F`、`importers/chat.py 8BA8D408A65ADE5F`、`importers/web.py 482C509E4E0C517A`、
`importers/pdf.py 46E11F67A62FC456`、`cli.py 63AF1E68BA4B3DF2`、`personal_memory/__init__.py 7AC340FB3EBED1F9`
→ **全部一致**。也没有新建数据库、没有新 schema / migration、没有第二套 Capture 逻辑、没有重写 URL/PDF 导入。

本阶段改动文件指纹：`web/views.py 3E39C2F2E08393B2`、`web/server.py D8BC675E44604998`、
`tests/test_feed_home.py 7E0275287A09DED6`、`tests/test_web.py 990E77AB91A6A83B`。

## 11. 遗留问题

1. **卡比素材**：已在本阶段接入真实游戏帧（Kirby Super Star Ultra，3 张独立 PNG + 合嘴复用待机帧），详见 `docs/KIRBY-SPRITES.md`；SVG 占位仍保留为素材缺失时的自动回退。仍未解决的是**逐帧吸入动画**（目前 4 个状态各 1 帧）。
2. **浏览器视觉未实测**：拖拽手感、动画节奏、320px 在真实窗口中的比例都未在浏览器里看过（无自动化浏览器）。
3. **URL 拖拽依赖浏览器行为**：只有可点击的「喂一个网址」后备入口是确定性可用的。
4. **多文件只取第一个**；没有队列、没有进度条、没有取消按钮。
5. **消化过程无进度**：`processing` 期间只有轻微下沉，没有百分比/时长提示（大 PDF / 长网页可能等待数十秒）。
6. **其它页面仍是旧外观**：`/memories`、`/sources`、`/search`、`/capture`、`/import` 未重做（按要求本轮不做），其中仍有英文标签（如 `Capture` / `Sources`、表头 `Title/Status`），与首页"不出现英文 UI 文案"的要求尚未统一。
7. **首页无历史/最近喂过什么**：刻意保持安静（§二），但用户无法从首页回看刚喂的内容（要点进"我的记忆"）。
8. **登录/多用户**不存在（仍是本机单用户工具）；`/capture`、`/import` 的原始异常文本仍在传统错误页显示（首页 JSON 路径已脱敏）。

## 12. 下一阶段建议

1. **补齐卡比素材帧**（待用户确认版权来源）：SSU 待机 / 张嘴 / 吸入 / 合嘴四帧 + 2~3 个中间帧，填入 `KIRBY_FRAMES`，并加一层极短的帧序列（不用 sprite sheet 引擎）。
2. **浏览器实测一轮**（若环境允许引入 Playwright 或人工走查）：拖拽、Ctrl+V、动画节奏、320px 比例、失败恢复。
3. **统一其它页面的中文与视觉**（导航已中文化，页面内部标签尚未），把"我的记忆/来源/搜索"做成同一套安静风格。
4. **首页增加一层极轻的"刚喂过"回执**（例如卡比头上短暂显示上次结果的一句话 + 一个"看看"链接），仍然不堆列表。
5. **消化过程的耐心感**：为长任务加一句"卡比还在嚼…"或极简进度点，避免用户以为卡住。
6. **素材与状态进一步解耦**：把 `KIRBY_FRAMES` 移到独立配置/JSON，让非开发者也能换素材；考虑 `prefers-reduced-motion` 降级（关闭动画只留文字反馈）。
