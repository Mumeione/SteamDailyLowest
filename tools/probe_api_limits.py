#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""受控探针：测 Steam `IStoreBrowseService/GetItems/v1` 与 ITAD `/lookup/shop/61/id/v1`
的参数边界与限流表现。

设计原则（务必遵守）：
  * **不碰 ITAD `/deals/v2`** —— 那是每日依赖的主接口，不拿它冒险
  * 总请求数有硬上限（``--hard-cap``，默认 120），超了就停并如实记录
  * 一遇 429 / 403 立即停止该类测试并把已得结果写出来
  * 只在非大促日手动跑；不做"打满限流"式的压测

用法（在 GitHub Actions 或任意 Linux 机器上）：
  python tools/probe_api_limits.py --state statedata/data/state.json \
      --probes batch,ua,rate,lookup,split --interval 2 --out probe_report.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid as uuidlib
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# data_request **只有一份**（2026-10-09 卡片 08）：探针测的是「生产那套请求形态」
# 的参数边界，自己再抄一份必然漂移（生产裁掉价格/平台/标签之后，探针还在按老的
# 响应体测批大小 —— 量出来的结论对不上生产）。要改请求形态就改客户端那一处。
from src.steam_browse import DATA_REQUEST  # noqa: E402

# 必须带 UA：不带会被拒/超时（项目已实测 HTTP 000）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
GET_ITEMS = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1"
APPDETAILS = "https://store.steampowered.com/api/appdetails"
ITAD_LOOKUP = "https://api.isthereanydeal.com/lookup/shop/61/id/v1"
ITAD_INFO = "https://api.isthereanydeal.com/games/info/v2"


def sanitize(text: str, secret: str | None) -> str:
    """把密钥从任何将要落盘的文本里抹掉（报告会提交进仓库）。"""
    if not secret:
        return text
    return text.replace(secret, "***ITAD_KEY***")

class Probe:
    """带请求计数上限的探针运行器。"""

    def __init__(self, *, interval: float, hard_cap: int, log) -> None:
        self.interval = interval
        self.hard_cap = hard_cap
        self.count = 0
        self.stopped = False
        self.stop_reason = ""
        self.log = log

    def allow(self) -> bool:
        if self.stopped:
            return False
        if self.count >= self.hard_cap:
            self.stopped = True
            self.stop_reason = f"达到请求硬上限 {self.hard_cap}"
            self.log(f"  ⛔ {self.stop_reason}，停止后续测试")
            return False
        return True

    def call(self, method: str, url: str, *, headers: dict | None = None,
             json_body=None, params=None, timeout: float = 30.0) -> dict:
        self.count += 1
        t0 = time.time()
        out = {"status": None, "elapsed": None, "body": None, "error": None}
        try:
            resp = requests.request(method, url, headers=headers or {"User-Agent": UA},
                                    json=json_body, params=params, timeout=timeout)
            out["status"] = resp.status_code
            out["elapsed"] = time.time() - t0
            try:
                out["body"] = resp.json()
            except ValueError:
                out["body"] = resp.text[:200]
        except Exception as exc:                                    # noqa: BLE001
            out["elapsed"] = time.time() - t0
            out["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        if out["status"] in (429, 403):
            self.stopped = True
            self.stop_reason = f"遇到 HTTP {out['status']}（限流/封禁），立即停止"
            self.log(f"  ⛔ {self.stop_reason}")
        if self.interval and not self.stopped:
            time.sleep(self.interval)
        return out


def get_items_body(appids: list[int]) -> dict:
    return {
        "ids": [{"appid": int(a)} for a in appids],
        "context": {"language": "schinese", "country_code": "CN", "steam_realm": 1},
        "data_request": DATA_REQUEST,
    }


def parse_items(body) -> tuple[int, int, int]:
    """返回 (store_items 条数, success==1 的条数, 总条数)。"""
    if not isinstance(body, dict):
        return 0, 0, 0
    items = ((body.get("response") or {}).get("store_items")) or []
    ok = sum(1 for it in items if isinstance(it, dict) and it.get("success") == 1)
    return len(items), ok, len(items)


def load_material(state_path: Path) -> tuple[list[int], list[str], list[tuple[str, int, str]]]:
    """返回 (appids, uuids, pairs)，pairs = [(uuid, appid, title)]。"""
    data = json.loads(state_path.read_text(encoding="utf-8"))
    meta = data.get("game_meta") or {}
    seen, appids = set(), []
    for m in meta.values():
        a = m.get("appid")
        if a and a not in seen:
            seen.add(a)
            appids.append(int(a))
    uuids, seen_u = [], set()
    titles: dict[str, str] = {}
    for entry in (data.get("seen_deal") or {}).values():
        g = entry.get("game_id")
        if g:
            titles.setdefault(g, entry.get("title") or "")
            if g not in seen_u:
                seen_u.add(g)
                uuids.append(g)
    pairs = [(g, int(m["appid"]), titles.get(g, ""))
             for g, m in meta.items() if m.get("appid")]
    return appids, uuids, pairs


def pad(seq: list, n: int) -> list:
    """取 n 个（不够就循环补齐，保证请求真的带够 n 个 id）。"""
    if not seq:
        return []
    out = []
    while len(out) < n:
        out.extend(seq)
    return out[:n]


# --------------------------------------------------------------------------- #
# 各探针
# --------------------------------------------------------------------------- #
def probe_batch(p: Probe, appids: list[int], rep: list[str]) -> None:
    rep.append("### P1 · `GetItems` 单批上限\n")
    rep.append("| 请求 id 数 | HTTP | 返回 store_items | success==1 | 耗时 | 备注 |")
    rep.append("|---|---|---|---|---|---|")
    last_ok = 0
    for n in (50, 100, 200, 250, 300, 400, 500, 800, 1000):
        if not p.allow():
            break
        r = p.call("GET", GET_ITEMS, params={"input_json": json.dumps(
            get_items_body(pad(appids, n)), separators=(",", ":"), ensure_ascii=False)})
        cnt, ok, _ = parse_items(r["body"])
        note = ""
        if r["error"]:
            note = "请求异常"
        elif r["status"] != 200:
            note = "**非 200**"
        elif cnt < n:
            note = "**有截断/丢弃**"
        else:
            note = "完整"
        if r["status"] == 200 and cnt >= n:
            last_ok = n
        rep.append(f"| {n} | {r['status'] or '—'} | {cnt} | {ok} | "
                   f"{(r['elapsed'] or 0):.2f}s | {note} |")
        p.log(f"  P1 n={n} → {r['status']} items={cnt}")
    rep.append(f"\n→ 实测完整通过的最大批量：**{last_ok}**\n")


def probe_ua(p: Probe, appids: list[int], rep: list[str]) -> None:
    rep.append("### P2 · `User-Agent` 是否必需\n")
    rep.append("| 场景 | HTTP | 耗时 | 错误 |")
    rep.append("|---|---|---|---|")
    if p.allow():
        r = p.call("GET", GET_ITEMS, headers={}, timeout=15.0,
                   params={"input_json": json.dumps(get_items_body(appids[:5]),
                                                    separators=(",", ":"), ensure_ascii=False)})
        rep.append(f"| 不带 UA | {r['status'] or '—'} | {(r['elapsed'] or 0):.2f}s | {r['error'] or '—'} |")
        p.log(f"  P2 无 UA → {r['status']} {r['error'] or ''}")
    if p.allow():
        r = p.call("GET", GET_ITEMS,
                   params={"input_json": json.dumps(get_items_body(appids[:5]),
                                                    separators=(",", ":"), ensure_ascii=False)})
        rep.append(f"| 带 UA | {r['status'] or '—'} | {(r['elapsed'] or 0):.2f}s | {r['error'] or '—'} |")
    rep.append("")


def probe_rate(p: Probe, appids: list[int], rep: list[str], max_batches: int) -> None:
    rep.append(f"### P3 · `GetItems` 连续 {max_batches} 次（200 条/次，间隔 {p.interval}s）\n")
    rep.append("不是压测——只为看**在这点量级上会不会出现 429**。\n")
    rep.append("| 第几次 | HTTP | store_items | 耗时 |")
    rep.append("|---|---|---|---|")
    n429 = 0
    body = pad(appids, 200)
    for i in range(1, max_batches + 1):
        if not p.allow():
            break
        r = p.call("GET", GET_ITEMS, params={"input_json": json.dumps(
            get_items_body(body), separators=(",", ":"), ensure_ascii=False)})
        cnt, _, _ = parse_items(r["body"])
        if r["status"] == 429:
            n429 += 1
        rep.append(f"| {i} | {r['status'] or '—'} | {cnt} | {(r['elapsed'] or 0):.2f}s |")
    rep.append(f"\n→ 429 次数：**{n429}**（0 说明该量级安全，不代表上限）\n")


def probe_lookup(p: Probe, uuids: list[str], rep: list[str]) -> None:
    rep.append("### P4 · ITAD `/lookup/shop/61/id/v1` 单批上限（免鉴权）\n")
    rep.append("| 请求 uuid 数 | HTTP | 返回条目 | 其中含 app/ | 耗时 |")
    rep.append("|---|---|---|---|---|")
    for n in (1000, 2000, 5000, 10000, 20000):
        if not p.allow():
            break
        batch = pad(uuids, n)
        r = p.call("POST", ITAD_LOOKUP, json_body=batch,
                   headers={"User-Agent": UA, "Content-Type": "application/json"})
        body = r["body"] if isinstance(r["body"], dict) else {}
        with_app = sum(1 for v in body.values()
                       if any(str(s).startswith("app/") for s in (v or [])))
        rep.append(f"| {n} | {r['status'] or '—'} | {len(body)} | {with_app} | "
                   f"{(r['elapsed'] or 0):.2f}s |")
        p.log(f"  P4 n={n} → {r['status']} 条目={len(body)} 含app={with_app}")
    rep.append("")


def probe_split(p: Probe, uuids: list[str], rep: list[str]) -> None:
    rep.append("### P5 · 无效 uuid 导致整批 500 + 二分降级验证\n")
    rep.append("| 场景 | HTTP | 返回条目 |")
    rep.append("|---|---|---|")
    good = [u for u in uuids[:4]]
    bad = str(uuidlib.uuid4())          # 格式合法但几乎必定不存在

    def post(batch):
        r = p.call("POST", ITAD_LOOKUP, json_body=batch,
                   headers={"User-Agent": UA, "Content-Type": "application/json"})
        body = r["body"] if isinstance(r["body"], dict) else {}
        return r["status"], len(body)

    if good and p.allow():
        s, c = post(good[:3])
        rep.append(f"| 3 个真实 uuid | {s or '—'} | {c} |")
    if p.allow():
        s, c = post([good[0], bad] if good else [bad])
        rep.append(f"| 1 真实 + 1 无效 | {s or '—'} | {c} |")
    # 二分降级：拆到单条，坏 uuid 自然被丢弃
    if good and p.allow():
        batch = good + [bad]
        mid = len(batch) // 2
        s1, c1 = post(batch[:mid])
        s2, c2 = post(batch[mid:])
        rep.append(f"| 二分后左半 {mid} 个 | {s1 or '—'} | {c1} |")
        rep.append(f"| 二分后右半 {len(batch) - mid} 个 | {s2 or '—'} | {c2} |")
    rep.append("")


def probe_mix(p: Probe, appids: list[int], rep: list[str], rounds: int = 6) -> None:
    rep.append(f"### P6 · `appdetails` 与 `GetItems` 是否共用一个限流桶（观察，不定论）\n")
    rep.append("两者轮换各打 "
               f"{rounds} 次，看是否有一方先出现 429。**样本太小，只能作线索。**\n")
    rep.append("| 轮次 | appdetails | GetItems |")
    rep.append("|---|---|---|")
    for i in range(1, rounds + 1):
        if not p.allow():
            break
        r1 = p.call("GET", APPDETAILS, params={
            "appids": ",".join(str(a) for a in pad(appids, 5)),
            "cc": "cn", "filters": "price_overview"})
        r2 = p.call("GET", GET_ITEMS, params={"input_json": json.dumps(
            get_items_body(pad(appids, 20)), separators=(",", ":"), ensure_ascii=False)})
        rep.append(f"| {i} | {r1['status'] or '—'} | {r2['status'] or '—'} |")
    rep.append("")


def probe_reviews(p: Probe, pairs: list[tuple[str, int, str]], rep: list[str],
                  itad_key: str | None, sample: int = 25) -> None:
    rep.append("### P7 · 好评率口径对照（ITAD `info/v2` vs `GetItems`）—— 上线前硬阻塞\n")
    if not itad_key:
        rep.append("⚠️ 环境变量 `ITAD_API_KEY` 未设置，跳过本项。\n")
        return
    picked = pairs[:sample]
    appids = [a for _, a, _ in picked]

    # 1) 一次批量拿 GetItems
    r = p.call("GET", GET_ITEMS, params={"input_json": json.dumps(
        get_items_body(appids), separators=(",", ":"), ensure_ascii=False)})
    items: dict[int, dict] = {}
    if isinstance(r.get("body"), dict):
        for it in ((r["body"].get("response") or {}).get("store_items") or []):
            if isinstance(it, dict) and it.get("success") == 1 and it.get("appid"):
                items[int(it["appid"])] = it
    rep.append(f"- `GetItems` 一次批量（{len(appids)} 个 appid）返回 **{len(items)}** 条\n")

    rep.append("| appid | 标题 | ITAD score | ITAD count | GM filtered | count | unfiltered | lang_specific | 判定 |")
    rep.append("|---|---|---|---|---|---|---|---|---|")

    def pick(rev: dict, key: str, field: str):
        v = (rev.get(key) or {}).get(field)
        return int(v) if v is not None else None

    diffs: list[int] = []
    for uuid, appid, title in picked:
        if not p.allow():
            break
        # key 只走 header，绝不进 URL（报告会提交进仓库）
        r2 = p.call("GET", ITAD_INFO,
                    headers={"User-Agent": UA, "ITAD-API-Key": itad_key},
                    params={"id": uuid})
        itad_score = itad_count = None
        if isinstance(r2.get("body"), dict):
            for rv in (r2["body"].get("reviews") or []):
                if rv.get("source") == "Steam":
                    itad_score, itad_count = rv.get("score"), rv.get("count")
                    break
        rev = (items.get(appid) or {}).get("reviews") or {}
        f = pick(rev, "summary_filtered", "percent_positive")
        u = pick(rev, "summary_unfiltered", "percent_positive")
        l = pick(rev, "summary_language_specific", "percent_positive")
        fc = pick(rev, "summary_filtered", "review_count")
        if itad_score is None or f is None:
            verdict = "数据缺失"
        else:
            d = f - int(itad_score)
            diffs.append(abs(d))
            verdict = f"一致（{d:+d}）" if abs(d) <= 1 else f"**偏差 {d:+d}**"
        rep.append(f"| {appid} | {str(title)[:28]} | {itad_score} | {itad_count} | "
                   f"{f} | {fc} | {u} | {l} | {verdict} |")

    rep.append("")
    if diffs:
        le1 = sum(1 for x in diffs if x <= 1)
        rep.append(f"→ 可比样本 **{len(diffs)}** 个：\\|偏差\\| ≤1 的占 **{le1}/{len(diffs)}**，"
                   f"平均 \\|偏差\\| **{sum(diffs) / len(diffs):.2f}** 点\n")
    rep.append("> `summary_filtered` = Steam 页面默认展示口径（已剔除异常刷评时段）。"
               "本项目现有阈值（好评率 ≥70% / 评价数 ≥100 / 高热度 ≥10000）建立在 ITAD 口径上，"
               "若平均偏差 >2 点就需要重标定。\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="statedata/data/state.json")
    ap.add_argument("--probes", default="batch,ua,rate,lookup,split")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--max-batches", type=int, default=10)
    ap.add_argument("--hard-cap", type=int, default=120)
    ap.add_argument("--out", default="probe_report.md")
    ap.add_argument("--itad-key", default=os.environ.get("ITAD_API_KEY", ""))
    ap.add_argument("--reviews-sample", type=int, default=25)
    args = ap.parse_args()

    log = lambda m: print(m, flush=True)  # noqa: E731

    state_path = Path(args.state)
    if not state_path.exists():
        raise SystemExit(f"找不到状态库：{state_path}")
    appids, uuids, pairs = load_material(state_path)

    rep: list[str] = []
    rep.append("# 接口边界探针报告\n")
    rep.append(f"- 材料来源：`{args.state}`（真实 appid {len(appids):,} 个 / uuid {len(uuids):,} 个）")
    rep.append(f"- 请求间隔：{args.interval}s　请求硬上限：{args.hard_cap}")
    rep.append(f"- 要跑的探针：`{args.probes}`")
    rep.append("- ⚠️ 全程**不请求 ITAD `/deals/v2`**\n")

    p = Probe(interval=args.interval, hard_cap=args.hard_cap, log=log)
    want = {s.strip() for s in args.probes.split(",") if s.strip()}

    if "batch" in want:
        probe_batch(p, appids, rep)
    if "ua" in want:
        probe_ua(p, appids, rep)
    if "rate" in want:
        probe_rate(p, appids, rep, args.max_batches)
    if "lookup" in want:
        probe_lookup(p, uuids, rep)
    if "split" in want:
        probe_split(p, uuids, rep)
    if "mix" in want:
        probe_mix(p, appids, rep)
    if "reviews" in want:
        probe_reviews(p, pairs, rep, args.itad_key or None, args.reviews_sample)

    rep.append("---\n")
    rep.append("## 运行元信息\n")
    rep.append(f"- 实际发出的 HTTP 请求数：**{p.count}**（上限 {args.hard_cap}）")
    if p.stopped:
        rep.append(f"- ⛔ 提前停止：{p.stop_reason}")
    else:
        rep.append("- ✅ 全部探针跑完，未触发停止条件")
    rep.append("\n> 说明：本报告只记录**观测事实**。未出现 429 不等于「上限就是这么多」，"
               "只说明在这个量级上安全。")

    report = sanitize("\n".join(rep) + "\n", args.itad_key or None)
    Path(args.out).write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
