# 批 E/F 审查修复清单（round 1）

> 固定点：`HEAD`（8b1f74d）｜范围：未提交改动 + 4 个未跟踪工具脚本
> 来源：`/code-review` 两轴（Standards / Spec）+ 回归 · 耦合 · 临时文件三项排查
> 约定：本文件即工单清单，**每步做完立刻验证**；**提交需用户明确指示，本轮不提交**。

## 起点结论

`ask-matt` 路由：本批落在主流程第 3 步的「**非多会话**」分支 —— **就地实现，不拆 ticket**。

理由：全部发现集中在同一功能域（报表 UI 批 E/F），单个上下文窗口可完成，达不到
`/to-tickets` 的门槛；spec 已在 `.scratch/report-ui/spec.md` 定稿，无需回 `/grill-with-docs`；
也不是「难 bug」型问题，无需 `/diagnosing-bugs`。

**修改顺序**按「用户可见 > 误导后来者 > 死代码 > 需你定夺 > 结构债」排，
前两步先做是因为它们直接影响页面表现与后续维护判断。

---

## 步骤 1（先做）：统一「距上次史低」标签 —— 唯一用户可见缺口

**事实**：`templates/static/app.js` 的 `lastLowRow(text, date)` 两个分支标签不一致：

| 分支 | 触发档位 | 当前标签 |
| --- | --- | --- |
| 无日期（`if (!date)`） | `new`（本次刷新历史记录） | 「距上次史低」✓ |
| 有日期 | `tie`（N 天，可点按切日期） | 「**上次史低**」✗ |

**影响**：spec E3 要求把标签从「上次史低」改成「距上次史低」（这一行给的是天数，
旧标签会让人以为要给价格或日期）。改名只落了一半 → **`tie` 条目（今天 42 条里的绝大多数）
看到的仍是旧标签**，同一条目两套文案。

**改法**：`app.js` 第 121 行的 `"上次史低"` → `"距上次史低"`。

**验证**：`node --check` + 全仓库 grep「上次史低」不得再有裸用（只剩「距上次史低」与
spec 历史记录） + `render_report.py` 重渲染 + 浏览器展开任一卡片核对。

---

## 步骤 2：更正三处与实现不符的注释 / 文档

| 位置 | 现状（错） | 事实 | 改法 |
| --- | --- | --- | --- |
| `app.js` 头注释 | 「R3 …内容瘦身到 3~4 行后**改回单栏**」 | spec E3 已**恢复两栏**，实现也是两栏（无比价时回落单栏） | 改为「两栏，无比价数据时回落单栏」 |
| `app.js` 筛选注释 | 「只留 **flag=N** 的条目」 | 实现已改 `low_class === "new"`（Steam 口径） | 改为 `low_class === "new"` |
| `README.md` L33 | 「本表与页面上的分组标题**会自动跟着变**」 | 表格是死文本，不会自动变 | 改为「页面分组标题会自动跟着变；**本表需手动同步**」 |

第 3 条是本批自己引入的矛盾：`report.py` 注释写的是「改阈值时记得同步 README」，
README 却声称自动同步。注释/文档写错会让下一个 agent 直接做错事，成本极低，先清。

**验证**：grep `flag=N` 归零；通读 README 该节与 `report.py:92-94` 注释是否口径一致。

---

## 步骤 3：清理死样式 / 死变量

| 位置 | 说明 |
| --- | --- |
| `app.css` `.tag-quality` | 批 E 已删卡片档位标签，全仓库只剩 spec 的历史记录引用它 → 删规则 |
| `app.css` `--equal-low` | 全仓库无引用（`--new-low` / `--store-low` 才是现役）→ 删变量 |

**验证**：grep 两者只命中 `.scratch/report-ui/spec.md`（历史记录，不动）；
`render_report.py` 重渲染后页面无变化（纯删无用声明）。

---

## 步骤 4：定案结果（2026-09-24 傍晚，用户回复）

| # | 事项 | 结果 |
| --- | --- | --- |
| a | 「史低待确认」同语义两种颜色（`.stat-value.stat-unknown` 紫 vs `.point-unknown` 灰） | **已定案：统一灰** → 数字、色点、卡片灰虚线色条、`.tag-low-unknown` pill 全部 `var(--store-low)`。**已完成**。`.tag-pending`（详情待补）是另一个概念，仍保留紫色 |
| b | `new` 档「标签「距上次史低」+ 文本「本次刷新历史记录」」读起来矛盾 | **已定案：只改文本不改标签** → 文本改为「本次新史低」。**已完成**，连带同步 `test_storelow.py`（断言 / 用例名 / docstring）与 `DEVELOPMENT.md` §3.6 两处 |
| c | `classify.steam_low_class(..., window_hours=)` 生产代码从不传，仅测试用 | **已定案：删除参数** —— 用户：24h 容错已经很宽，再放大会不准确。24h 回归纯常量；原「窗口 1h~72h 结果一致」测试换成**边界测试**（恰好 24h=new / 25h=tie，该边界此前从未被测）。**已完成** |
| d | 临时脚本 / 文件 | 用户自己删了 `tools/_run.py`、`tools/ui_probe.py`；**保留** `make_preview.py`（用户要的单文件手机预览）、`measure_layout.js`、`steam_flag_probe.py`。我清掉 data/ 下 10 个一次性产物（`heihe_*` ×6、`heybox_logo_ref.png`、`sec36.txt`、`render_run.txt`、`ui_probe.txt`），并把 `steam_flag_probe.py` 的 flag 取数对齐到 state |

**临时文件清单（最终结果）**

| 路径 | 用途 | 结果 |
| --- | --- | --- |
| `tools/make_preview.py` | 单文件手机预览 | **保留**（用户决定）→ 需按 5.2 改读断点 |
| `tools/measure_layout.js` | Edge headless 量 F3 布局 | **保留**（spec L411 / DEVELOPMENT.md:952 引用）→ 同 5.2 |
| `tools/steam_flag_probe.py` | 离线 flag×Steam 交叉制表 | **保留并修好**：flag 改从 state 取（卡片字段批 E 已删，原来恒 None、整表塌掉）；输出「店史低」改「平史低」；同步 `classify.py` / `test_classify.py` 里写错的输出路径引用 |
| `tools/ui_probe.py` | 卡片现状体检 | **已删**（用户删）；`spec.md` L177 的引用已清 |
| `tools/_run.py` | stdout 落盘 wrapper | **已删**（用户删；已跟踪文件，git 里可恢复） |
| `data/` 一次性产物 | `heihe_*` ×6、`heybox_logo_ref.png`、`sec36.txt`、`render_run.txt`、`ui_probe.txt` | **已清 10 个**。保留 `state.json` / `fx_cache.json` / `ca_bundle.pem`（生产）、`runlog.txt` / `logs/` / `probe/` |

---

## 步骤 5：结构债（先看懂再决定）

> 步骤 1~4 改的是「错了的东西」；步骤 5 不同 —— **现在的行为是对的，只是写法上有隐患**。
> 它不改变任何页面表现，做不做取决于你愿不愿意为「将来少踩坑」付一点复杂度。

### 5.1 `low_class` 的三个取值，在两种语言里各写了一份

**现状**：`"new"` / `"tie"` / `"unknown"` 这组字符串 ——
- Python 侧：`classify.py` 的常量 `STEAM_LOW_NEW` / `STEAM_LOW_TIE` / `STEAM_LOW_UNKNOWN`
- JS 侧：`app.js` 直接写死字面量 —— `item.low_class === "new"`、`lowClass === "tie"`、`"unknown"`

**问题**：两边没有任何机制保证一致。哪天 Python 侧把 `"tie"` 改名成 `"equal"`，
Python 测试全过（测试读常量），但前端会**静默失配** ——
`lowClass === "tie"` 恒 false → 全落到 else 分支 → 页面不报错，只是标签与色条全错。

**怎么改**：`tools/check_payload.py` 加一条校验 —— 扫 `output/static/app.js` 里出现的
`low_class` 字面量，必须都在 `classify` 常量集内。**只动校验脚本，不碰生产代码。**
**成本**：低。**收益**：把「静默错」变成「跑 check_payload 就红」。

### 5.2 断点 768 现在有 6 处

**现状**：`config.json`（`mobile_breakpoint_px`）→ `report.py` 塞进 payload → `app.js` 从 payload 读；
但 `app.css` 的 `@media` 读不到 payload，只能手工写死 768；本批又新增两处 ——
`make_preview.py` 与 `measure_layout.js` 各自写死 768。

**问题**：哪天把断点从 768 改成 700，得记住同时改 CSS 和这两个脚本。漏一个就是
「布局按一套、量尺按一套、预览按一套」三方各说各话（批 F3 排错时就吃过这种亏）。

**怎么改**：让两个工具脚本从 `config.json` 读 `mobile_breakpoint_px`
（`make_preview.py` 是 Python 可直接读；`measure_layout.js` 由调用方把断点作为参数传进去）。
**成本**：中低（都是工具脚本，不碰生产代码）。
**⚠️ 前置**：这条**只因为你保留 `make_preview.py` 才成立**；若它被删，只剩 `measure_layout.js` 一处，收益大幅下降。

### 5.3 解析 `data.js` 的写法重复

**现状**：`render_report.py`、`steam_flag_probe.py`（原还有已删的 `ui_probe.py`）各自写一遍
`raw[raw.index("=") + 1:]`，把 `window.REPORT_DATA = {...};` 剥出来。

**问题**：`data.js` 的格式一改（例如加一行前导注释），这几处一起坏。

**怎么改**：抽一个公共解析函数。
**成本**：中。**建议：不做** —— `ui_probe.py` 删掉后只剩 2 处，且都是本地排查工具，
坏了立刻能看出来，不值得为它引入共享模块。

### 结论与结果（2026-09-24：5.1 / 5.2 已实施，5.3 不做）

**5.1 已做** —— `check_payload.py` 新增两个方向的校验：

- 主：每个 classify 常量都要以**完整引号字面量**出现在 app.js
  （不能查子串 —— `"new"` 会被 `tag-low-new` 这类 CSS 类名假命中）
- 辅：与 `low_class` 比较、或取其兜底默认值的字面量必须 ⊆ 常量集
- **负向实测**：把 `output/static/app.js` 的 `=== "tie"` 改成 `=== "equal"`
  → 两个方向都报 ✗、脚本退出码 2
- ⚠️ 实施中抓到一个真 bug：正则写成 `low[Cc]lass` 只匹配 `lowClass`，
  **漏掉带下划线的 `low_class`**（`low_class || "unknown"` 那处因此没被检出）→ 已改 `low_?[Cc]lass`

**5.2 已做** —— 两个脚本改为从**渲染出的 payload** 读 `page_size.breakpoint`。
读 payload 而不是 config.json：页面才是被量的对象，若配置改了却没重渲染，读数应跟页面走。

- `make_preview.py`：新增 `load_breakpoint()`，`build_frames(widths, breakpoint)`，打印断点来源
- `measure_layout.js`：启动时解析 `output/data.js`，并打印「断点 = N（读自 output/data.js，不是写死的）」
- **实测**：把 `output/data.js` 临时改成 `"breakpoint": 600` → make_preview 打印
  「按 payload 断点 600px 划分」、768px 帧标签变「平板/桌面」；measure_layout 打印「断点 = 600」
- **端到端**：`node tools/measure_layout.js` 8 档全部量出
  （≤768 → 3+2、≥820 → 一行 5 个），与批 F3 结论一致

**5.3 不做** —— 维持原判断（只剩 2 处、都是本地排查工具，不值得抽公共模块）。

**状态**：步骤 1~4 已在本地提交 `85e9745`（未推送）；5.1 / 5.2 是其后的独立改动，**尚未提交**。

---

## 明确不改的项（附理由，避免后续重复讨论）

- **`tier_label` payload 字段保留** —— 不是死字段：`check_payload.py:48`、`test_pipeline.py:111`、
  `test_report.py` 都在用它校验「分组标题 == 卡片标签」这条一致性约束。
- **测试断言裸 CSS 类名 / `hasattr(report, "conditions")`** —— spec 批 F 第 5 条**明确要求**
  改成这种负向断言（「口径别跑回 payload / 页面」）。仓库 spec 优先于基线告警。
- **`low_kind`（ITAD 口径）与 `low_class`（Steam 口径）命名相近** —— spec E1 原文就用这两个名字，
  改名会偏离 spec；`classify.py` 已有注释区分。
- **`is_expired()` 与 `expired_retention_days` 保留** —— spec F2 明确要求（留存清理仍用），
  只是不再作为页面分类。
- **`check_payload.py` 新增的三组校验** —— 属加法式校验，不改变产物，接受。

---

## 通用验证命令（每步跑一遍）

```powershell
node --check templates/static/app.js
& "D:\Program Files\Python313\python.exe" -m unittest discover -s tests
& "D:\Program Files\Python313\python.exe" tools\check_payload.py
& "D:\Program Files\Python313\python.exe" tools\render_report.py
```

**改动前基线（本轮实测）**：118 tests OK / `check_payload.py` 全 ✓ / 进列表 51 条 / 零网络。

**收尾**：按既有惯例，UI 改动先经你在浏览器验收（`Ctrl+F5`）；确认后由你指示是否提交。