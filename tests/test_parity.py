# -*- coding: utf-8 -*-
"""跨语言判据对拍（code-audit-2026-10-09 #7）。

列表页有两套「等价」判据：

* **Python**：:func:`src.report.section_agg` 的预聚合计数表（写进第 0 片的
  ``agg.counts``，前端据此在只加载一片时也能给出精确「共 N 条」）；
* **前端**：``templates/static/app.js`` 的 ``cardPredicate``（板块归属 + ``liveOk`` +
  条件 ``dateOk`` + ``filterOk``）。

两者以前只对拍了**键序 / 字面量**（``AGG_KEY_ORDER`` 与 ``tools/check_payload.py``），
**判据逻辑本身没有跨语言等价性测试** —— 这条测试就是补它：把 app.js 的真判据在 jsdom 里
跑起来（走 app.js 末尾的 test-only seam），对同一批卡片逐组合比计数。

⚠️ **不静默跳过**：node 或 jsdom 找不到时**直接失败**并打印修复命令（本仓策略）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify, report  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "jsdom" / "parity_filters.js"

#: 与 section_agg 的日期位图 / 前端 dateOk 同一套口径的配置（档位由它派生）。
CFG = {
    "home_new_low_days": 7,
    "big_cut_percent": 80,
    "notable_review_count": 10000,
}

#: 对拍的板块 = 四个真实板块 + 「全部折扣」（``__all__`` 有**独立分支**：
#: 不叠加 liveOk、且池是 ``pool_items`` 而非四板块并集 —— 故必须单独对拍）。
SECTION_KEYS = ["new_low", "expiring", "popular", "big_cut", "__all__"]


class FilterParityTest(unittest.TestCase):
    """Python ``section_agg`` 与前端 ``cardPredicate`` 的逐组合计数必须完全一致。"""

    @staticmethod
    def _card(game_id, *, low="new", ago=1, days_left=5, cut=50,
              reviews=(90, 500), views=("active",)):
        """造一张字段齐全的卡片。

        ``reviews=None`` → 详情待补（评 0）；``views=None`` → **整键缺失**
        （走 ``days_left`` 近似「即将到期」，且 liveOk/``_is_live`` 视为在期内）。
        """
        card = {
            "game_id": game_id, "title": game_id, "title_zh": game_id,
            "start_days_ago": ago, "days_left": days_left, "cut": cut,
            "low_class": low, "price_int": 100, "price_text": "¥1.00",
            "regular_int": 200, "regular_text": "¥2.00",
            "tier": classify.TIER_QUALITY, "sections": [], "compare": [],
        }
        card["reviews"] = ({"score": reviews[0], "count": reviews[1]}
                           if reviews is not None else None)
        if views is not None:
            card["views"] = list(views)
        return card

    def _cards(self):
        """刻意覆盖边界的卡片（约 20~40 张）：ago=0/1/2/dN 边界、无 views、过期、
        reviews 为 None、cut 缺失、折扣/评价数正好压在档位线上，以及
        **「板块属性命中但已出窗口」**的卡（g24）—— 夹具里没有出窗口的卡，
        对拍就覆盖不到「全部 vs 近 N 天」档位的分叉（2026-10-10 那次回归的形状，
        见 docs/CHANGELOG.md 当日条目）。
        """
        raw = [
            # ① ago 边界：今天 / 昨天 / 前天 / 正好 dN（=7）/ 无 views / 出窗口
            self._card("g01", ago=0, cut=90, reviews=(90, 20000)),            # 新+热+大折
            self._card("g02", ago=1, cut=50, reviews=(70, 500)),             # 折扣/评价数压线 50/500
            self._card("g03", low="tie", ago=2, cut=80, reviews=(80, 100)),  # 平史低 + 折扣压线 80
            self._card("g04", ago=7, cut=80, reviews=(90, 10000)),          # dN 边界 + 评价数压线 10000
            self._card("g05", low="tie", ago=8, cut=50, reviews=(85, 100),
                       views=("upcoming",)),                                # 出窗口、只在 expiring、非 live
            self._card("g06", ago=1, days_left=2, cut=90, reviews=None,
                       views=None),                                         # 无 views、reviews 为 None
            self._card("g07", ago=1, cut=None, reviews=(75, 300)),          # cut 缺失
            # ② 各板块多寡不同的组合
            self._card("g08", ago=3, cut=95, reviews=(95, 50000)),
            self._card("g09", low="tie", ago=1, cut=90, reviews=(88, 30000)),
            self._card("g10", ago=2, cut=50, reviews=(60, 150)),
            self._card("g11", ago=0, days_left=0, cut=80, reviews=(80, 5000),
                       views=("active", "upcoming")),                       # 同属 新/热(不)/大折/临期
            self._card("g12", low="tie", ago=5, cut=90, reviews=(90, 9999)),  # 评价数 9999 = 差 1 差一档
            self._card("g13", ago=7, cut=50, reviews=(70, 200)),
            self._card("g14", ago=1, cut=90, reviews=(90, 20000),
                       views=("week",)),                                    # 过期：无 active/upcoming → 不属任何板块
            self._card("g15", ago=1, cut=70, reviews=(90, 10000)),          # 折扣落在两档之间
            self._card("g16", low="tie", ago=0, days_left=2, cut=80, reviews=(92, 20000)),
            self._card("g17", ago=2, cut=90, reviews=(89, 5000)),
            self._card("g18", ago=6, cut=50, reviews=(95, 10000)),
            self._card("g19", low="tie", ago=4, cut=50, reviews=(80, 10000)),
            self._card("g20", ago=7, days_left=0, cut=95, reviews=(97, 200000)),
            self._card("g21", low="tie", ago=5, cut=90, reviews=(90, 20000)),
            self._card("g22", low="tie", ago=3, cut=80, reviews=(80, 100),
                       views=("active", "upcoming")),                       # 大折 + 临期（live）
            self._card("g23", ago=3, days_left=10, cut=50, reviews=(50, 100)),
            self._card("g24", ago=30, cut=90, reviews=(90, 20000)),   # 出窗口：新+热+大折
        ]
        for card in raw:
            card["sections"] = report.section_keys(card, CFG)
        return raw

    def _assert_window_invariant(self, cards):
        """见 :meth:`_cards` 的构造约束：夹具必须含「板块属性命中但已出窗口」的卡
        （否则 date="all" 与 "dN" 档的跨语言等价没有被真正测到）。"""
        days = CFG["home_new_low_days"]
        out_of_window = [
            c["game_id"] for c in cards
            if any(key in c["sections"] for key in ("new_low", "popular", "big_cut"))
            and c.get("start_days_ago") is not None
            and c["start_days_ago"] > days
        ]
        self.assertTrue(out_of_window,
                        "夹具缺「出窗口但板块属性命中」的卡，对拍覆盖不到日期档位分叉")

    def test_python_agg_matches_frontend_card_predicate(self):
        cards = self._cards()
        self._assert_window_invariant(cards)

        orders = report.all_section_orders(cards, CFG)
        members = {key: orders[key] for key in SECTION_KEYS}
        for key in SECTION_KEYS:
            self.assertTrue(members[key], f"板块 {key} 没有成员，夹具没覆盖到")
        # 「全部折扣」的池必须与四板块并集**不是同一批**，否则这条对拍抓不到它的独立语义
        union = {c["game_id"] for key in SECTION_KEYS if key != "__all__"
                 for c in members[key]}
        pool = {c["game_id"] for c in members["__all__"]}
        self.assertNotEqual(union, pool, "__all__ 池与四板块并集相同，夹具没覆盖到它的差异")

        # 逐组合：Python 侧计数（section_agg）+ 同序 job 列表。
        # ⚠️ 每个 job 带**该板块自己的成员集** —— 与生产分片一致（打开某板块只加载该板块
        #    分片）；不能用并集：`__all__` 的池是 pool_items，与并集不是同一批卡片。
        dims = report.filter_dim_values(CFG)
        jobs, expected = [], []
        agg_by_section = {key: report.section_agg(members[key], CFG, key)["counts"]
                          for key in SECTION_KEYS}
        for key in SECTION_KEYS:
            counts = agg_by_section[key]
            for date in dims["date"]:
                for cut in dims["cut"]:
                    for reviews in dims["reviews"]:
                        for only_new in dims["only_new"]:
                            combo = (date, cut, reviews, only_new)
                            jobs.append({"section": key,
                                         "filters": {"date": date, "cut": cut,
                                                     "reviews": reviews,
                                                     "only_new": only_new},
                                         "cards": members[key]})
                            expected.append(counts["|".join(combo)])

        payload = {
            "sections": [{"key": s["key"], "label": s["label"],
                          "date_window": s["date_window"]} for s in report.SECTIONS],
            "list": {"breakpoint": 600, "batch": 30, "auto_max": 300},
            "assets_version": "parity-test",
        }
        fixture = {"payload": payload, "jobs": jobs}

        got = self._run_frontend(fixture)
        self.assertEqual(len(got), len(jobs),
                         f"前端返回 {len(got)} 个结果，期望 {len(jobs)}")

        mismatches = []
        for job, want, have in zip(jobs, expected, got):
            if want != have:
                mismatches.append((job, want, have))
        if mismatches:
            lines = [f"{len(mismatches)}/{len(jobs)} 个组合两侧计数不一致："]
            for job, want, have in mismatches[:10]:
                key = job["section"]
                titles = ", ".join(
                    f"{c['game_id']}(ago={c['start_days_ago']},cut={c['cut']},"
                    f"rev={report.review_count(c)},new={c['low_class']},live="
                    f"{'active' in (c.get('views') or []) or c.get('views') is None},"
                    f"sec={'|'.join(c['sections'])})"
                    for c in members[key])
                lines.append(
                    f"  板块 {key} 组合 {job['filters']}：Python={want} JS={have}\n"
                    f"    该板块成员：{titles}")
            self.fail("\n".join(lines))

    def _run_frontend(self, fixture: dict) -> list[int]:
        node = shutil.which("node") or os.environ.get("SDL_NODE")
        if not node or not Path(node).exists():
            self.fail(
                "找不到 node —— 跨语言判据对拍跑不了。安装 node，或设 SDL_NODE=<node 路径>。\n"
                "  （本仓策略：不静默跳过；判据漂移必须能被抓到）")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            json.dump(fixture, fh, ensure_ascii=False)
            fixture_path = fh.name
        try:
            proc = subprocess.run(
                [node, str(SCRIPT), fixture_path],
                cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300)
        finally:
            try:
                os.unlink(fixture_path)
            except OSError:
                pass
        if proc.returncode != 0:
            self.fail(
                "前端对拍脚本失败（退出码 %d）。\n"
                "  jsdom 缺失时请在仓库根 `npm install --no-save jsdom`，"
                "或设 SDL_NODE_MODULES=<含 jsdom 的 node_modules>。\n"
                "---- stderr ----\n%s" % (proc.returncode, (proc.stderr or "").strip()))
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"前端输出不是合法 JSON：{exc}\nstdout={proc.stdout!r}\n"
                      f"stderr={(proc.stderr or '').strip()}")


if __name__ == "__main__":
    unittest.main()
