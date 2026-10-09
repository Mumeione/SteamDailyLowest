#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代码卫生静态检查（code-audit-2026-10-09 第四节「可自动化检查项」）。

纯标准库实现，**不引入 ruff / stylelint 等新依赖** —— 让 check_all.py 在任何
装了 Python 的机器与 CI 上都能跑（审计报告原建议用 ruff / stylelint，但那是
两套外部工具链；这里用 AST / 正则做等价的最小实现，覆盖面窄一些但零依赖）。

五条检查（对应报告第四节 #2 #3 #5 #6 #7）：

  ① 未用导入（≈ ruff F401）：run.py 与 src/*.py 里 import 进来却从不使用的名字。
     ⚠️ **只覆盖 F401**（未用导入）；报告提到的 F841（未用局部变量）/ vulture 式
     未用定义**未实现** —— 那两类误报面大，留给专用工具，不在这里硬凑。
  ② 注释/文档字符串里的路径引用是否存在（`docs/**.md`，以及本地存在的
     `.scratch/**.md` —— 报告点名 snapshot.py 引用已删除的 v3-plan.md 即此类）；
  ③ CSS 重复选择器（≈ stylelint no-duplicate-selectors）的**相邻**版本：同一
     @media 上下文里紧挨着的两条同名规则（`.msg-until` 那一类手滑重复）；
  ④ 常量单源：report.py 的 `X = DEFAULTS[...]` 键名必须真实存在；模块级数值常量
     必须落在**显式白名单**内（防悄悄新增第二份写死的阈值）；
  ⑤ 前端兜底：templates/static/app.js 不得对 payload 契约键写 `|| 数字` 兜底
     （2026-10-09 卡片 08 的口径：取不到就不挂行为，不在前端留第二张默认值表）。
     其余 `|| 数字`（如 `cut || 0`、`innerWidth || 1024`）只进**清单**供审阅、不判失败 ——
     清单里若出现可疑项（如 `meta.size || 200` 复刻服务的 `ALL_SHARD_SIZE`），
     属需人工裁决的已知事项，不靠本检查一刀切。

退出码：0 = 全过；1 = 有失败；2 = 用法/环境问题。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))     # 供 ④ 常量单源 import src.config

# ---- ④ 常量单源：report.py 模块级数值常量的白名单（新增须在此登记并写明理由）----
# 报告第四节 #6 提到的两个「例外」，以及两个结构性常量（不是可配置阈值）。
REPORT_INT_CONST_ALLOWLIST = {
    "ALL_SHARD_SIZE",          # 分片大小：产物体积/首屏的取舍，不是业务阈值
    "HOME_PICKS_PAGE",         # 大卡一页几张：版面口径，经 payload 下发
    "RECOMMEND_FAME_CAP",      # 名气归一化封顶（20 万评价）
    "RECOMMEND_GAP_CAP_DAYS",  # 间隔归一化封顶（366 天）
}

# ---- ⑤ payload 契约键：前端不得写 `|| 数字` 兜底 ----
PAYLOAD_CONTRACT_KEYS = (
    "stale_warn_hours", "stale_banner_hours", "pick_page", "breakpoint",
    "batch", "auto_max",
)


class Fail(Exception):
    """一条检查失败（消息即报告）。"""


def _py_files() -> list[Path]:
    files = [ROOT / "run.py"]
    files += sorted((ROOT / "src").glob("*.py"))
    return [f for f in files if f.exists()]


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    # ⚠️ 别把 `http://` 里的 `//` 当注释开头（本仓 app.js 有 URL 字面量）：
    # 用「前面不是 `:`」判定，避免把整行 URL 之后的代码一起删掉（review 指出的漏报面）
    return re.sub(r"(?<!:)//[^\n]*", "", src)


# ----------------------------------------------------------------------
# ① 未用导入
# ----------------------------------------------------------------------
def check_unused_imports() -> str:
    problems = []
    for path in _py_files():
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        # 被 noqa 的行整体豁免（本仓用它标注「刻意转发导出」）—— 只认真正的
        # `# noqa`（含 F401 之类码），别拿子串匹配（行文里出现 "noqa" 就豁免 = 漏洞）
        noqa_lines = {i for i, line in enumerate(src.splitlines(), 1)
                      if re.search(r"#\s*noqa\b", line)}

        imported: dict[str, int] = {}          # 名字 → 行号
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if node.lineno in noqa_lines:
                        continue
                    imported[(a.asname or a.name).split(".")[0]] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                if node.module == "__future__" or node.lineno in noqa_lines:
                    continue
                for a in node.names:
                    if a.name == "*":
                        continue
                    imported[a.asname or a.name] = node.lineno

        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        # `__all__ = [...]` 里的名字算「有意导出」
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
                    and isinstance(node.value, (ast.List, ast.Tuple))):
                used |= {e.value for e in node.value.elts
                         if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        # 字符串注解里的前向引用（`dict[str, "Foo"]` 这种）—— **只在注解位置**取，
        # 不能扫全部字符串常量：docstring 里提到一个名字就算「用过」的话，
        # 死导入会被文档遮住（review 指出的漏报面）。
        for node in ast.walk(tree):
            for field in ("annotation", "returns"):
                ann = getattr(node, field, None)
                if ann is None:
                    continue
                for sub in ast.walk(ann):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        for name in imported:
                            if re.search(r"\b%s\b" % re.escape(name), sub.value):
                                used.add(name)

        for name, lineno in sorted(imported.items(), key=lambda kv: kv[1]):
            if name not in used:
                problems.append(f"{path.relative_to(ROOT)}:{lineno} 未用导入：{name}")
    if problems:
        raise Fail("未用导入 %d 处：\n    %s" % (len(problems), "\n    ".join(problems)))
    return "run.py 与 src/*.py 无未用导入"


# ----------------------------------------------------------------------
# ② 注释 / docstring 里的路径引用是否存在
# ----------------------------------------------------------------------
_REF_RE = re.compile(r"(?<![\w/])((?:\.scratch|docs)/[\w./-]+\.md)")


def check_comment_refs() -> str:
    """注释 / docstring 里 `docs/**.md`(与本地 `.scratch/**.md`) 引用是否存在。

    ⚠️ **`.scratch/` 是 gitignore 的**（`.gitignore` 里 `.scratch/`，不进仓库）——
    CI 干净检出里根本没有这个目录。若把它当「必须存在」校验，
    本地绿、CI 必红，直接推翻 check_all 顶部「本地和 CI 是同一套」的承诺
    （首版就是这么写的，被 review 抓到）。故：`.scratch/` 引用**只在目录存在时**
    校验（本地开发），不存在时跳过并如实报出跳过条数。
    """
    problems, checked = [], 0
    scratch_checked = scratch_skipped = 0
    has_scratch = (ROOT / ".scratch").exists()
    for path in _py_files():
        src = path.read_text(encoding="utf-8")
        snippets = [t.string for t in tokenize.generate_tokens(
            iter(src.splitlines(keepends=True)).__next__) if t.type == tokenize.COMMENT]
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                snippets.append(node.value.value)
        for text in snippets:
            for ref in _REF_RE.findall(text):
                if ref.startswith(".scratch/"):
                    if not has_scratch:
                        scratch_skipped += 1
                        continue
                    scratch_checked += 1
                checked += 1
                if not (ROOT / ref).exists():
                    problems.append(f"{path.relative_to(ROOT)} 引用了不存在的 {ref}")
    if problems:
        raise Fail("失效路径引用 %d 处：\n    %s" % (len(problems), "\n    ".join(problems)))
    note = ("" if has_scratch else
            "；.scratch/ 引用 %d 处**跳过**（本地开发目录不在，CI 本就没有）" % scratch_skipped)
    return "docs/.scratch 的 %d 处 .md 路径引用都存在%s" % (checked, note)


# ----------------------------------------------------------------------
# ③ CSS 重复选择器（同一上下文、相邻两条选择器相同）
# ----------------------------------------------------------------------
def _iter_css_rules(css: str):
    """产出 (上下文元组, 选择器, 起始行号)。

    上下文 = 所在 ``@media`` / ``@supports`` / ``@container`` 的**嵌套栈**
    （用栈而非拼接字符串，否则一个 @media 会污染其后所有规则 —— 首版就是这么
    误报的）。只处理本仓 app.css 的形态：无嵌套规则、at-rule 皆带块。
    """
    stack: list[str] = []
    buf: list[str] = []
    selector: str | None = None
    ctx: tuple[str, ...] = ()
    line = 1
    start_line = 1
    for ch in css:
        if ch == "\n":
            line += 1
        if ch == "{":
            head = re.sub(r"\s+", " ", "".join(buf)).strip()
            buf = []
            if head.startswith("@"):
                stack.append(head)
                selector = None
            else:
                selector = head
                ctx = tuple(stack)
                start_line = line
        elif ch == "}":
            buf = []
            if selector is not None:
                yield ctx, selector, start_line
                selector = None
            elif stack:
                stack.pop()
        elif ch == ";" and selector is None:
            buf = []          # `@import url(...);` 这类无块 at-rule，别把它带进下一个 head
        else:
            buf.append(ch)


def check_css_duplicate_selectors() -> str:
    """只查**相邻**两条同名同上下文的规则 —— 即真正的手滑重复。

    为什么不查「全文件同名」：本仓**有意**把同一选择器按关注点拆在文件首尾
    （如 ``.drawer`` 基础配色在 310、定位/投影在 944，两处注释互相指路；
    ``.drawer-head/-body/-foot`` 同理）。全文件比对会把这种既定风格全判成重复
    （首版误报 21 处），收窄到「相邻」才能既抓 ``.msg-until`` 那类手滑、又不误伤。
    """
    path = ROOT / "templates" / "static" / "app.css"
    css = path.read_text(encoding="utf-8")
    # ⚠️ 必须先剥注释：本仓 app.css 注释里出现 `{` `}` `;`（讲版面/断点时引用规则），
    # 不过滤会让字符扫描器错位。用「等长空格替换」保住行号。
    css = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), css, flags=re.S)
    problems = []
    prev_key = None
    prev_line = 0
    for ctx, selector, line in _iter_css_rules(css):
        key = (ctx, selector)
        if key == prev_key:
            problems.append(
                f"app.css:{prev_line} 与 :{line} 相邻两条同选择器"
                f"（上下文 {ctx or '顶层'}）：{selector}")
        prev_key, prev_line = key, line
    if problems:
        raise Fail("相邻重复选择器 %d 处：\n    %s" % (len(problems), "\n    ".join(problems)))
    return "app.css 无相邻重复选择器"


# ----------------------------------------------------------------------
# ④ 常量单源
# ----------------------------------------------------------------------
def check_constant_single_source() -> str:
    from src.config import DEFAULTS

    path = ROOT / "src" / "report.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems = []
    int_consts = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        name = target.id
        # `X = DEFAULTS["key"]` → 键必须存在
        if (isinstance(node.value, ast.Subscript)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "DEFAULTS"
                and isinstance(node.value.slice, ast.Constant)):
            key = node.value.slice.value
            if key not in DEFAULTS:
                problems.append(f"report.py:{node.lineno} {name} = DEFAULTS[{key!r}]，"
                                f"但 DEFAULTS 里没有这个键")
        # 模块级数值常量必须在白名单内
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)) \
                and not isinstance(node.value.value, bool):
            int_consts.append(name)
            if name not in REPORT_INT_CONST_ALLOWLIST:
                problems.append(
                    f"report.py:{node.lineno} 新增写死的数值常量 {name} = {node.value.value}"
                    f"（阈值请收进 config.DEFAULTS，或登记进 REPORT_INT_CONST_ALLOWLIST 并写明理由）")
    if problems:
        raise Fail("常量单源 %d 处：\n    %s" % (len(problems), "\n    ".join(problems)))
    return "report.py 的 DEFAULTS 别名与数值常量白名单（%d 个）都合规" % len(int_consts)


# ----------------------------------------------------------------------
# ⑤ 前端兜底
# ----------------------------------------------------------------------
def check_frontend_fallbacks() -> str:
    path = ROOT / "templates" / "static" / "app.js"
    code = _strip_js_comments(path.read_text(encoding="utf-8"))
    problems = []
    for key in PAYLOAD_CONTRACT_KEYS:
        for m in re.finditer(r"\b%s\s*\|\|" % re.escape(key), code):
            line = code[:m.start()].count("\n") + 1
            problems.append(f"app.js:{line} 对 payload 契约键 {key} 写了 `||` 兜底")
    # 顺带把其余 `|| 数字` 列出来（报告第四节 #7 的「清单」意图），但不判失败
    inventory = [m.group(0) for m in re.finditer(r"\|\|\s*\d+", code)]
    if problems:
        raise Fail("前端兜底 %d 处：\n    %s" % (len(problems), "\n    ".join(problems)))
    return ("对 payload 契约键无兜底；其余 `|| 数字` 兜底 %d 处（清单）：%s"
            % (len(inventory), ", ".join(sorted(set(inventory)))))


CHECKS = [
    ("未用导入", check_unused_imports),
    ("注释路径引用", check_comment_refs),
    ("CSS 重复选择器", check_css_duplicate_selectors),
    ("常量单源", check_constant_single_source),
    ("前端兜底", check_frontend_fallbacks),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="代码卫生静态检查（零依赖）")
    ap.add_argument("--only", action="append", default=None,
                    help="只跑某一项（可重复）：" + " / ".join(s for s, _ in CHECKS))
    args = ap.parse_args(argv)

    wanted = set(args.only) if args.only else None
    failed = 0
    print("代码卫生检查（%d 项）" % len(CHECKS))
    print("-" * 56)
    for title, fn in CHECKS:
        if wanted is not None and title not in wanted:
            continue
        try:
            print("  通过  %-16s %s" % (title, fn()))
        except Fail as exc:
            failed += 1
            print("  失败  %-16s %s" % (title, exc))
    print("-" * 56)
    print("结果：%s" % ("全绿 ✓" if not failed else "失败 %d 项" % failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())