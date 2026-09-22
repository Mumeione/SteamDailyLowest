# Domain Docs

规定工程类 skill 在探索本仓库代码时，应如何消费本仓库的领域文档。

## 探索之前，先读这些

- 仓库根目录的 **`CONTEXT.md`**，或
- 若存在仓库根目录的 **`CONTEXT-MAP.md`**：它指向每个 context 一份 `CONTEXT.md`，
  读取与当前主题相关的每一份
- **`docs/adr/`**：读取与你即将动工的区域相关的 ADR。
  多 context 仓库中，还要检查 `src/<context>/docs/adr/` 里的 context 级决策

若上述文件不存在，**静默继续**。不要指出它们缺失，也不要主动建议先创建它们。
`/domain-modeling` skill（经由 `/grill-with-docs` 与 `/improve-codebase-architecture` 触达）
会在术语或决策真正定案时按需惰性创建。

## 文件结构

单 context 仓库（绝大多数仓库）：

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

多 context 仓库（根目录存在 `CONTEXT-MAP.md`）：

```
/
├── CONTEXT-MAP.md
├── docs/adr/                          ← 系统级决策
└── src/
    ├── ordering/
    │   ├── CONTEXT.md
    │   └── docs/adr/                  ← context 专属决策
    └── billing/
        ├── CONTEXT.md
        └── docs/adr/
```

## 使用术语表里的词汇

当你的产出提到某个领域概念时（issue 标题、重构提案、假设、测试名），
使用 `CONTEXT.md` 中定义的那个词。不要漂移到术语表明确规避的同义词。

如果你需要的概念还不在术语表里，这是一个信号：要么你在发明项目并未使用的语言
（重新考虑），要么确实存在一个空白（记下来交给 `/domain-modeling`）。

## 标记 ADR 冲突

如果你的产出与现有 ADR 相矛盾，明确暴露出来，而不是静默覆盖：

> _与 ADR-0007（event-sourced orders）冲突，但值得重新讨论，因为……_
