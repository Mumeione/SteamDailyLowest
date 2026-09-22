# Issue tracker: Local Markdown

本仓库的 issue 与 spec 以 markdown 文件形式存放在 `.scratch/` 下。

## 约定

- 一个功能一个目录：`.scratch/<feature-slug>/`
- spec 文件为 `.scratch/<feature-slug>/spec.md`
- 实现类 issue 一票一文件：`.scratch/<feature-slug>/issues/<NN>-<slug>.md`，
  从 `01` 开始编号，**不合并成单个 tickets 文件**
- 每个 issue 文件顶部有一行 `Status:` 记录状态（角色字符串见 `triage-labels.md`）
- 评论与讨论追加到文件底部 `## Comments` 标题之下

## 当某个 skill 说「publish to the issue tracker」

在 `.scratch/<feature-slug>/` 下新建文件（目录不存在则先创建）。

## 当某个 skill 说「fetch the relevant ticket」

读取对应路径的文件即可。用户通常会直接给出路径或 issue 编号。

## Wayfinding operations

供 `/wayfinder` 使用。**map** 是一个文件，每张 ticket 对应一个 **child** 文件。

- **Map**：`.scratch/<effort>/map.md`（存放 Notes / Decisions-so-far / Fog 正文）
- **Child ticket**：`.scratch/<effort>/issues/NN-<slug>.md`，从 `01` 编号，问题写在正文里。
  `Type:` 行记录 ticket 类型（`research`/`prototype`/`grilling`/`task`）；
  `Status:` 行记录 `claimed`/`resolved`
- **Blocking**：顶部附近的 `Blocked by: NN, NN` 行。当它列出的每个文件都是
  `resolved` 时，该 ticket 解除阻塞
- **Frontier**：扫描 `.scratch/<effort>/issues/`，挑出未关闭、未阻塞、未被认领的文件，
  编号最小者优先
- **Claim**：先设 `Status: claimed` 并保存，再开始工作
- **Resolve**：在 `## Answer` 标题下追加答案，设 `Status: resolved`，
  然后把一条上下文指针（要点 + 链接）追加到 `map.md` 的 Decisions-so-far
