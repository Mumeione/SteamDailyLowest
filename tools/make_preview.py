#!/usr/bin/env python
"""生成本地核对用的单文件预览（CSS / JS / data.js 全内联）。

用途：把 `output/index.html` 的静态资源压成一个自包含 HTML，
      方便直接丢进手机浏览器 / 微信「文件传输助手」里看真实效果
      （Windows 上 file:// 打开多文件页面没问题，但发到手机就会丢静态资源）。

产出（都在 output/，属 gitignore 范围，不是正式产物）：
  - preview_single.html   单文件，可直接拖进手机看
  - preview_frames.html   同页并排多个 iframe，对照不同宽度（默认 360/430/769/1024）

用法：
  python tools/make_preview.py
  python tools/make_preview.py --widths 360,390,430,768,769,1024,1440
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_breakpoint(data_js: str, fallback: int = 768) -> int:
    """断点从**渲染出的 payload** 读（config.json → report.py → page_size.breakpoint）。

    不在这里再写死一份 768 —— 否则「布局按一套、量尺按一套、预览按一套」，
    改断点时漏掉哪个就各说各话（批 F3 排错时正是这么踩的，见 fix-round1 步骤 5.2）。
    """
    m = re.search(r'"breakpoint"\s*:\s*(\d+)', data_js)
    return int(m.group(1)) if m else fallback


def build_single(index_html: str, css: str, js: str, data_js: str) -> str:
    """把外链的 css / js / data.js 内联进 HTML。"""
    # <link rel="stylesheet" href="static/app.css?v=...">  → <style>
    html = re.sub(
        r'<link[^>]*rel="stylesheet"[^>]*href="[^"]*app\.css[^"]*"[^>]*>',
        lambda _m: "<style>\n" + css + "\n</style>",
        index_html,
        flags=re.IGNORECASE,
    )
    # <script src="static/app.js?v=..."></script> → 内联
    html = re.sub(
        r'<script[^>]*src="[^"]*app\.js[^"]*"[^>]*>\s*</script>',
        lambda _m: "<script>\n" + js + "\n</script>",
        html,
        flags=re.IGNORECASE,
    )
    # <script src="data.js?v=..."></script> → 内联
    html = re.sub(
        r'<script[^>]*src="[^"]*data\.js[^"]*"[^>]*>\s*</script>',
        lambda _m: "<script>\n" + data_js + "\n</script>",
        html,
        flags=re.IGNORECASE,
    )
    return html


FRAMES_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>宽度对照 · Steam 史低</title>
<style>
  body {{ margin: 0; background: #eef0f3; font: 13px/1.5 system-ui, sans-serif; }}
  h1 {{ font-size: 14px; font-weight: 600; margin: 10px 14px 6px; color: #333; }}
  .row {{ display: flex; gap: 10px; padding: 0 14px 16px; align-items: flex-start;
         overflow-x: auto; }}
  .frame {{ background: #fff; border: 1px solid #ccc; border-radius: 6px;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); flex: 0 0 auto; }}
  .frame > .cap {{ font-size: 12px; color: #555; padding: 4px 8px;
                  border-bottom: 1px solid #eee; background: #fafbfc;
                  border-radius: 6px 6px 0 0; }}
  .frame > iframe {{ display: block; border: 0; width: 100%; height: 78vh; }}
</style>
</head>
<body>
<h1>宽度对照 —— 每档一个 iframe，实际渲染（拖动横向滚动条看更多档）</h1>
<div class="row">
{frames}
</div>
</body>
</html>
"""


def build_frames(widths: list[int], breakpoint: int) -> str:
    blocks = []
    for w in widths:
        tag = "手机" if w <= breakpoint else "平板/桌面"
        blocks.append(
            f'  <div class="frame" style="width:{w}px">\n'
            f'    <div class="cap">{w}px · {tag}</div>\n'
            f'    <iframe src="preview_single.html"></iframe>\n'
            f'  </div>'
        )
    return FRAMES_TMPL.format(frames="\n".join(blocks))


def main() -> int:
    ap = argparse.ArgumentParser(description="生成单文件预览（本地核对用）")
    ap.add_argument(
        "--widths",
        default="360,430,769,1024",
        help="preview_frames.html 里并排的宽度，逗号分隔（默认 360,430,769,1024）",
    )
    args = ap.parse_args()

    index_path = OUTPUT / "index.html"
    css_path = OUTPUT / "static" / "app.css"
    js_path = OUTPUT / "static" / "app.js"
    data_path = OUTPUT / "data.js"

    missing = [p.name for p in (index_path, css_path, js_path, data_path) if not p.exists()]
    if missing:
        print(f"[错误] 缺少产物：{', '.join(missing)}；请先跑 tools/render_report.py", file=sys.stderr)
        return 1

    data_js = _read(data_path)
    single = build_single(_read(index_path), _read(css_path), _read(js_path), data_js)
    (OUTPUT / "preview_single.html").write_text(single, encoding="utf-8")

    breakpoint = load_breakpoint(data_js)
    widths = [int(x) for x in args.widths.split(",") if x.strip()]
    (OUTPUT / "preview_frames.html").write_text(
        build_frames(widths, breakpoint), encoding="utf-8"
    )

    print(f"单文件预览：{OUTPUT / 'preview_single.html'}")
    print(f"宽度对照：  {OUTPUT / 'preview_frames.html'}"
          f"（{', '.join(str(w) for w in widths)}px；手机/桌面按 payload 断点 {breakpoint}px 划分）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
