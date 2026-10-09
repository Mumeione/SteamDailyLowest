#!/usr/bin/env python
"""生成本地核对用的单文件预览（CSS / JS / data.js 全内联）。

用途：把 `output/index.html` 的静态资源压成一个自包含 HTML，
      方便直接丢进手机浏览器 / 微信「文件传输助手」里看真实效果
      （Windows 上 file:// 打开多文件页面没问题，但发到手机就会丢静态资源）。

产出（都在 output/，属 gitignore 范围，不是正式产物）：
  - preview_single.html   单文件，可直接拖进手机看
  - preview_frames.html   同页并排多个 iframe，对照**三档各一个代表宽度**
                          （默认 390 手机 / 1024 平板 / 1440 电脑）

  ⚠️ 默认只出三档：档位间距要按「手机 / 平板 / 电脑」三档看，宽度堆多了反而看不出重点
     （用户 2026-10-07：「不是三个档位吗，frames 里面怎么有这么多」）。
     要看边界行为（768 / 1100 附近）再手动传更多宽度。

用法：
  python tools/make_preview.py
  python tools/make_preview.py --widths 390,768,768,1100,1101,1440
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


def load_breakpoints(data_js: str, fallback: tuple[int, int] = (600, 1100)) -> tuple[int, int]:
    """两个断点都从**渲染出的 payload** 读（config.json → report.py → payload.list）。

    不在这里再写死一份 —— 否则「布局按一套、量尺按一套、预览按一套」，
    改断点时漏掉哪个就各说各话（批 F3 排错时正是这么踩的，见 fix-round1 步骤 5.2）。

    返回 ``(手机上限, 平板上限)``：≤ 前者是手机、两者之间是平板、> 后者是 PC
    （用户 2026-10-07：只要手机 / 平板 / PC 三档）。
    """
    mob = re.search(r'"breakpoint"\s*:\s*(\d+)', data_js)
    tab = re.search(r'"tablet_breakpoint"\s*:\s*(\d+)', data_js)
    return (int(mob.group(1)) if mob else fallback[0],
            int(tab.group(1)) if tab else fallback[1])


def build_single(index_html: str, css: str, js: str, data_js: str,
                 all_shards: str | None = None) -> str:
    """把外链的 css / js / data.js 内联进 HTML。

    顺带内联板块列表的分片（``output/all/*.js``，``window.ALL_S``）——
    app.js 的 loadShard 会先查 window.ALL_S，命中就不注入 <script>，
    单文件预览里点分类照样出列表（2026-10-08 起取代原先的单个 all.js）。
    """
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
    # 分片（window.ALL_S）内联：放在 data.js 之后、app.js 之前均可 ——
    # loadShard 是点击时才读 window.ALL_S
    if all_shards:
        html = html.replace(
            "</head>",
            "<script>\n" + all_shards + "\n</script>\n</head>",
            1,
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


def build_frames(widths: list[int], mobile: int, tablet: int) -> str:
    blocks = []
    for w in widths:
        if w <= mobile:
            tag = "手机（竖屏）"
        elif w <= tablet:
            tag = "平板（横竖屏）"
        else:
            tag = "电脑（横屏）"
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
        default="390,1024,1440",
        help="preview_frames.html 里并排的宽度，逗号分隔（默认 390,1024,1440 三档各一个）",
    )
    args = ap.parse_args()

    index_path = OUTPUT / "index.html"
    css_path = OUTPUT / "static" / "app.css"
    js_path = OUTPUT / "static" / "app.js"
    data_path = OUTPUT / "data.js"
    all_dir = OUTPUT / "all"      # 板块列表的分片，缺失不阻断（首页照常能看）

    missing = [p.name for p in (index_path, css_path, js_path, data_path) if not p.exists()]
    if missing:
        print(f"[错误] 缺少产物：{', '.join(missing)}；请先跑 tools/render_report.py", file=sys.stderr)
        return 1

    data_js = _read(data_path)
    # 分片内联：按文件名排序（<key>_<n> 字典序即可，前端按 slot 查表、不依赖顺序）。
    # 大促时这一堆可能十几 MB —— 预览是本地单文件，不上传，不心疼。
    shard_files = sorted(all_dir.glob("*.js")) if all_dir.exists() else []
    all_shards = "\n".join(_read(p) for p in shard_files) or None
    if all_shards is None:
        print("[warn] 没有 output/all/ 分片：预览里点进分类会停在「加载失败」"
              "（先跑一次带 all_cards 的报表）")
    else:
        print(f"[info] 已内联 {len(shard_files)} 个列表分片")
    single = build_single(_read(index_path), _read(css_path), _read(js_path),
                          data_js, all_shards=all_shards)
    (OUTPUT / "preview_single.html").write_text(single, encoding="utf-8")

    mobile, tablet = load_breakpoints(data_js)
    widths = [int(x) for x in args.widths.split(",") if x.strip()]
    (OUTPUT / "preview_frames.html").write_text(
        build_frames(widths, mobile, tablet), encoding="utf-8"
    )

    print(f"单文件预览：{OUTPUT / 'preview_single.html'}")
    print(f"宽度对照：  {OUTPUT / 'preview_frames.html'}"
          f"（{', '.join(str(w) for w in widths)}px；"
          f"手机 ≤{mobile} / 平板 {mobile + 1}~{tablet} / 电脑 >{tablet}"
          f"，读自 payload，不是写死的）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
