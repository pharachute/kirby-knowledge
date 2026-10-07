# 卡比首页真实素材（Kirby Squeak Squad "Normal Kirby" 吸入帧）

本文件记录卡比素材的**来源、审计过程、帧映射、动画实现与验证证据**。
代码改动只涉及 UI 展示层：`personal_memory/web/views.py`、`personal_memory/web/server.py`、
`personal_memory/web/assets/*.png` 与测试。**业务后端未改**（见 §7）。

> **本阶段（吸入动画修正）结论先说**：
> **没有找到 KSSU 的真实 Inhale 帧**（审计过程见 §2），因此**不能**在保留 KSSU 待机/张嘴帧的前提下
> 接入"同一套"的吸入帧。为了不混用不同游戏画风，本阶段把整套帧统一换成
> **Kirby Squeak Squad "Normal Kirby"**（就是上一阶段你最初指定的 asset 3342）同一张 sheet 里的
> 同一条吸入动作条：待机 → 张嘴 → 吸入 1/2/3 → 合嘴。

---

## 1. 当前使用的素材（一句话版本）

| 项目 | 内容 |
| --- | --- |
| 游戏 | **Kirby: Squeak Squad**（《星之卡比 参上！多罗奇团》，DS，2006） |
| 素材 | Spriters Resource 上 **asset 3342「Normal Kirby」** sprite sheet（sheet 内自带署名框：*"Kirby Squeak Squad: Normal Kirby / ripped by Jackster"*） |
| 规模 | 1000×515、RGBA（自带透明通道）、301 个连通精灵、87 色 |
| 取帧位置 | **同一条吸入动作条**：y≈179–201，x≈674–868（从左到右依次是闭嘴 → 张嘴 → 口型最大 → 回收） |
| 保存位置 | `personal_memory/web/assets/kirby-idle.png`、`kirby-open.png`、`kirby-inhale-1.png`、`kirby-inhale-2.png`、`kirby-inhale-3.png` |

## 2. 素材审计过程（§四 的 7 个问题）

### 2.1 你指定的页面为什么拿不到

`https://www.spriters-resource.com/ds_dsi/kirbysqueaksquad/asset/3342/`

* 站点对非浏览器客户端一律返回 **HTTP 403**（人机校验）。已尝试：浏览器 UA 的 `Invoke-WebRequest`、
  带完整导航头的 `curl`、Jina Reader 转文本、四个公共代理（weserv / wsrv / allorigins / codetabs）。
* **可行路线（最终采用）**：Wayback Machine 存有该 URL 的历史快照
  （asset 页面快照 20260915154933、sheet 快照 **20250822055428**，均 200），
  再经公共图片代理 `images.weserv.nl` 让**代理端**去取 Wayback 的原始字节（避开本机被限流的 429）。
  最终拿到 **87331 字节、1000×515、RGBA** 的原始 sheet ✔

### 2.2 KSSU 到底有没有真正的 Inhale 帧（**关键结论：没找到**）

上一阶段用的是 WiKirby 的 `KSSU Kirby Sprite.png` / `KSSU Kirby sprite 2.png`。本阶段按 §三 的优先级
先找 **KSSU 同一套**的 Inhale 帧，做法与结果：

1. 用 Wayback CDX API 列到 **KSSU 分区 139 个 asset id**，逐个查 sheet 快照，**下载到 30 张 KSSU sheet**
   （14700–14794 区间），逐张做背景抠除 + 连通域分析。
2. 逐一看过/测过的 sheet 署名与内容：**14729 = Copy Kirby**（蓝帽子）、**14753 = Fighter Kirby**、
   **14764 = Suplex Kirby**、**14738 = Galacta Knight**，其余是能力/敌人/特效 sheet。
3. 我把上一阶段在用的两帧（待机 24×22、张嘴 24×24）拿去**在这 30 张 sheet 里做像素比对**（含 ±2px 容差）：

   | 帧 | 结果 |
   | --- | --- |
   | 待机（WiKirby `KSSU Kirby Sprite.png`） | 在 14764（Suplex）与 14738 里找到 **0.0% 差异**的同一像素（因为无帽能力的站立帧与无帽 Kirby 相同） |
   | 张嘴（WiKirby `KSSU Kirby sprite 2.png`） | **30 张 sheet 全部匹配失败**（最低差异仍有 84%），可推断该文件并非来自我能拿到的任何 KSSU sheet |

4. 结论：**没有可靠找到 KSSU 的 Inhale 动作帧**（也没有找到能与之配套的"同一套"待机/张嘴来源）。
   按 §四 要求：**不猜、不补画、不用 hover 帧冒充**，如实报告缺口。

### 2.3 七个问题的逐条回答（针对最终采用的素材）

| # | 问题 | 答案 |
| --- | --- | --- |
| 1 | 找到哪一套 | Kirby Squeak Squad「Normal Kirby」全量 sprite sheet（asset 3342） |
| 2 | 是否真有 Inhale 帧 | **有**。同一行里有连续的口型变化：闭嘴 → 小口 → 大口 → 最大 → 回收（含吐星帧） |
| 3 | 有多少连续帧 | 该动作条共 12 个相连精灵（含 x=642 的非 Kirby 特效帧）；其中口型变化 5 帧被我采用 |
| 4 | 每帧尺寸 | 原始像素 21×19 ~ 24×22（见 §3 表） |
| 5 | 是否透明 | **是**，sheet 本身 RGBA（无需抠背景） |
| 6 | 是否与 idle/open 同一套 | **是**：待机/张嘴/吸入 1-3 全部取自**同一条动作条**、同一张 sheet、同一款游戏 |
| 7 | 能否直接下成独立 PNG | 可以直接按坐标裁切（本阶段即如此），无需 sprite sheet 运行时裁切逻辑 |

## 3. 帧映射与尺寸

| 状态 | 文件 | sheet 坐标 | 原始像素 | 输出（×15 最近邻） | 嘴部暗像素（同区域度量） |
| --- | --- | --- | --- | --- | --- |
| 待机 / 合嘴 | `kirby-idle.png` | (674,179,698,201) | 24×22 | 360×330 | 31 |
| 张嘴 | `kirby-open.png` | (726,180,747,201) | 21×21 | 315×315 | 61 |
| 吸入 1 | `kirby-inhale-1.png` | (749,181,771,201) | 22×20 | 330×300 | 42 |
| 吸入 2 | `kirby-inhale-2.png` | (796,180,819,201) | 23×21 | 345×315 | 83 |
| 吸入 3 | `kirby-inhale-3.png` | (845,180,868,201) | 23×21 | 345×315 | 89 |

* 处理只做「去透明外边距 + 整数倍 NEAREST 放大」，**没有重画 / 重上色 / AI 生成 / 改线稿**。
* 吸入 1→2→3 的嘴部暗像素 **42 → 83 → 89** 单调增大（测试断言），配合身体压扁的姿势形成"吸"的脉冲。
* `closed（合嘴）` 复用待机帧（§九 允许）。

## 4. 吸入动画如何播放（只改展示层，状态机不动）

* **状态机一行未改**：`KIRBY_TRANSITIONS` / `nextKirbyState` 仍是 `IDLE→DETECTED→OPEN→INHALE→CLOSED→PROCESSING→FEEDBACK→IDLE`。
* 吸入状态（`data-kirby-state="inhale"`）下，三张吸入帧同时 `display:block`：
  * `inhale2` 参与布局（撑起高度）；
  * `inhale1` / `inhale3` **绝对定位叠加**，用两条 `@keyframes`（`kirby-inhale-a` / `kirby-inhale-c`）以
    `steps(1, end)` 交替显示 1/3 周期；
  * 周期 **0.42s**（≈ KSS 吸入的 ~0.4s 节奏），因此看起来是"张嘴 → 快速连续吸入"的 3 帧循环，
    而不是"打开嘴 → 停很久 → 换图"。
* `KIRBY_INHALE_ORDER = ("inhale1","inhale2","inhale3")`、`KIRBY_INHALE_SECONDS = 0.42` 是数据化配置，
  换素材/换节奏不需要动逻辑。
* 素材仍走既有 `KIRBY_FRAMES` 机制：填路径即可；文件缺失时自动回退到占位绘图。
* 新增的 `/assets/<name>` 路由是**只读白名单**（5 个精确文件名），未知/穿越路径一律 404。

## 5. 吸入对象如何与帧同步（§六）

新增一个纯展示层的"被吸物"元素（`showIntake(label)` → `.kirby-intake`）：

```text
内容靠近（拖拽/粘贴/点击输入）
→ 张嘴（open 帧）
→ 被吸物出现（文件名 / “文字” / “网址”）并沿 CSS 动画飞向右侧嘴巴、缩小淡出（0.46s）
→ 吸入帧 1/2/3 循环播放
→ 合嘴（closed）
→ PROCESSING（真实请求仍在进行中）
→ 真实结果返回后才有“吃饱了~”
```

* 被吸物不显示正文内容，只显示**文件名或类别**（"文字"/"网址"），与既有「正文不回显」策略一致。
* 卡比的嘴部命中区（`.kirby-drop`）已右移对齐素材里嘴巴的位置。
* **§十一 保持成立**：吸入动画结束 ≠ 处理完成；`吃饱了~` 仍然只在 `fetch` 拿到真实业务结果后出现。

## 6. 浏览器真实验证（无头 Edge + CDP，真实页面、真实请求、真实模型）

用 CDP 驱动**真实运行中的页面** `http://127.0.0.1:8765`（不是 `file://` 复制页），
在页面内派发真实的 `DragEvent` / `ClipboardEvent`：

| 验证 | 结果（Observed） |
| --- | --- |
| 首页待机 | 6 张帧 `<img>`，`data-kirby-state="idle"`，真实 Kirby、透明背景、无白边 |
| **拖入真实 Markdown** | 状态 `inhale`（截图里能看到 `inhale-check.md` 被吸向张开的嘴）→ `feedback`，DOM 读到 **`吃饱了~` + `记住了 3 件事`** → 回到 `idle` |
| **Ctrl+V 粘贴文字** | 状态 `inhale` → `feedback`，DOM 读到 **`吃饱了~` + `记住了 1 件事`** → 回到 `idle` |
| **拖入损坏 PDF** | 状态直接回到 `idle`，DOM 读到 **`这次没吃下去` + `文件不完整，暂时读不出来。`**（中文、无异常名/无堆栈/无路径） |
| 真实写入 | 验收库由 `3 sources / 5 memories / 4 links` 变为 **`5 / 9 / 8`**，`index_consistency.consistent=true` |
| 6 状态对照页 | 待机 / 张嘴 / 吸入帧1 / 吸入帧2 / 吸入帧3 / 合嘴 全部为同一只 Kirby、同一画风、像素均匀、无跳帧闪烁、无 sprite 泄漏 |

截图证据（工作区）：`_workbench-recon/kb11-kirby-states6.png`（六态对照）、`kb11-live-idle.png`、
`kb11-drop-inhale.png`（吸入中 + 被吸物）、`kb11-drop-feedback.png`（吃饱了~ + 记住了 3 件事）、
`kb11-paste-feedback.png`、`kb11-fail-feedback.png`。

## 7. 是否修改后端

**未修改任何冻结后端。** 本阶段前后逐文件 `sha256[:16]` 比对一致：
`models.py`、`store.py`、`capture.py`、`extraction.py`、`retrieval.py`、`quality.py`、`lifecycle.py`、`db.py`、
`docs/schema.sql`、`errors.py`、`llm.py`、`prompts.py`、`importers/*.py`、`cli.py`、`personal_memory/__init__.py`。
无新依赖、无新框架、无数据库/schema 变更、Capture API 契约与"吃饱了~"绑定逻辑未动。

本阶段改动：`web/views.py`（帧数据 + 帧 CSS 翻转动画 + 被吸物动效）、`web/server.py`（白名单加入 3 个吸入帧文件名）、
`web/assets/*.png`（替换 5 张帧）、`tests/test_feed_home.py`、`tests/test_kirby_inhale.py`（新）。

## 8. 测试结果

```text
python -m unittest discover -s tests -t .
Ran 538 tests ... OK (skipped=2)
```

其中与卡比素材直接相关的 10 个测试（`test_feed_home.py` 6 个 + `test_kirby_inhale.py` 4 个）覆盖：

* 5 张帧都是合法 RGBA PNG、尺寸足够（≥300px）、有透明区域（不是整块白底）；
* 帧 → 文件映射完整，三张吸入帧互不相同，`closed` 复用 `idle`；
* `/assets/*` 精确返回文件字节，未知/穿越路径 404，已删除的旧 hover 帧也 404；
* **吸入帧口型逐级张开**（用标准库 zlib 解 PNG 后统计嘴部暗像素，42 < 83 < 89）；
* 首页包含 6 张帧、翻转动画 keyframes、被吸物动画 keyframes，且 `inhale1/3` 是绝对定位（不撑高页面）。

## 9. 当前问题（遗留）

1. **视角变了**：这套吸入动作是**侧视（脸朝右）**，所以整套帧都是侧视，不再是上一阶段的正面视角。
   这是为了保证"同一套素材"（§三）而做的取舍；若更想要正面视角，需要另找正面吸入帧的素材源。
2. **不是 KSSU**：KSSU 的真实吸入帧未找到（§2.2），所以从"优先 KSSU"退回为"同一套 Squeak Squad"。
3. **只有 3 帧吸入**，没有完整 7 帧吸入序列，也没有"吞咽/鼓起"的后续帧（合嘴复用待机帧）。
4. **像素块明显**：原始帧只有 ~21-24px，放大 15 倍后在 360px 显示为大像素块；这是真实像素素材的固有特征。
5. **侧视 + 被吸物飞向右侧嘴部**：窄屏（`clamp` 收缩到 280px）时被吸物终点位置略显偏右，未做响应式微调。
6. 吸入帧循环表现"持续吸气"；真实请求耗时较长时（大 PDF）会一直循环，没有"吸满/喘气"变化。

## 10. 下一步建议（只限卡比视觉 / 动画）

1. 若拿到 **KSSU 或 Squeak Squad 的完整吸入 sheet（含 5-7 连帧）**：把 `KIRBY_INHALE_ORDER` 扩到 5-7 帧，
   周期仍保持 ~0.4-0.5s，其余代码不动。
2. 用同款素材给 `PROCESSING（消化）` 加 1-2 帧（吞咽/鼓起），并把 `FEEDBACK` 换成"满足"的表情帧。
3. 若要保留正面视角：寻找正面吸入帧素材（同一 sheet 中正面朝镜头的吸气动作），目前这张 sheet 没有。
4. 加 `prefers-reduced-motion` 降级（关闭被吸物飞行动画与帧翻转，只留静态帧切换）。
5. 给帧 `<img>` 补 `width/height` 属性，避免切换帧时的轻微布局抖动。
