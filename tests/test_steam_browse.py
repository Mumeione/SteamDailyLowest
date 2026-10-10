# -*- coding: utf-8 -*-
"""``SteamBrowseClient``（IStoreBrowseService/GetItems/v1）单元测试（重构 S1）。

GetItems 是 Valve **未公开**接口，**字段名漂移是主要风险**（spec §8）→
本文件用 mock 响应锁住每一个被读取的来源键；任何字段改名都会在这里先红。

不发真实请求，两种手法（同既有测试文件）：

* ``FakeSession`` / ``FakeResp``：走真实 ``BaseHttpClient.request`` 管线
  （验 400 → :class:`HttpError`，不吞错）；
* 覆写 ``request()``：校验请求形状（``input_json`` 结构、批量切片、去重）。

来源键对照（实测样例见 `.workbuddy/tmp/review_api.txt` §1.3）：
``appid`` / ``name`` / ``success`` / ``reviews.summary_filtered.{percent_positive,
review_count}`` / ``basic_info.{publishers,developers}[].{name,creator_clan_account_id}``
/ ``release.steam_release_date``。

⚠️ 2026-10-09（卡片 08）：``purchase_options`` / ``platforms`` / ``tags`` 三个来源键
**已从契约里移除**（生产零消费方，``data_request`` 里对应开关也关了）——
本文件既锁「DATA_REQUEST 不再请求它们」，也锁「响应里带了也一律无视」。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.httpclient import HttpError  # noqa: E402
from src.ratelimit import RateLimiter  # noqa: E402
from src.steam_browse import (  # noqa: E402
    DATA_REQUEST,
    ITEMS_COUNTRY,
    MAX_BATCH_SIZE,
    SteamBrowseClient,
    parse_store_item,
)


def make_limiter() -> RateLimiter:
    return RateLimiter("steam_browse", 150, 300, min_interval=0,
                       clock=lambda: 0.0, sleep=lambda s: None)


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """走真实 BaseHttpClient 管线的假 session（记录每次请求）。"""

    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload
        self.calls: list[dict] = []

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params})
        return FakeResp(self.status, self.payload)


def make_client(session) -> SteamBrowseClient:
    return SteamBrowseClient(make_limiter(), timeout=1, session=session,
                             sleep=lambda s: None)


def body_of(call: dict) -> dict:
    return json.loads(call["params"]["input_json"])


#: 实测响应形态的样本（字段名锁定用）；价格字段是字符串
FULL_ITEM = {
    "appid": 1658920,
    "name": "Crown Wars: The Black Prince",
    "success": 1,
    "reviews": {
        "summary_filtered": {
            "review_count": 543, "percent_positive": 60,
            "review_score": 5, "review_score_label": "褒贬不一",
        },
        "summary_unfiltered": None,
        "summary_language_specific": None,
    },
    "basic_info": {
        "short_description": "王冠纷争愈演愈烈……",
        "publishers": [{"name": "Nacon", "creator_clan_account_id": 33118426}],
        "developers": [{"name": "Artefacts Studio"}],
    },
    "purchase_options": [
        {"packageid": 1003261, "final_price_in_cents": "1730",
         "original_price_in_cents": "17300"},
        {"packageid": 1003262, "final_price_in_cents": "599",
         "original_price_in_cents": "2999"},
    ],
    "release": {"steam_release_date": 1716480182},
    "tags": [{"tagid": 29482, "weight": 1079}],
    "platforms": {"windows": True, "steam_deck_compat_category": 3},
    # include_assets=True 的响应（2026-10-10 封面单源 = library_capsule）
    "assets": {
        "asset_url_format": "steam/apps/1658920/${FILENAME}?t=1716480182",
        "library_capsule": "library_600x900.jpg",
        "library_capsule_2x": "library_600x900_2x.jpg",
        "header": "header.jpg",
        "main_capsule": "capsule_616x353.jpg",
    },
}

INVALID_ITEM = {"appid": 9999999, "success": 15}

RESPONSE = {"response": {"store_items": [FULL_ITEM, INVALID_ITEM]}}


class ParseItemTest(unittest.TestCase):
    """parse_store_item：锁定每个来源键 → GameMeta 字段的映射。"""

    def test_full_item_fields_locked(self):
        meta = parse_store_item(FULL_ITEM)
        self.assertEqual(meta.appid, 1658920)
        self.assertEqual(meta.name, "Crown Wars: The Black Prince")
        # reviews 与 itad.fetch_info 同形：score 0~100 整数 + count
        self.assertEqual(meta.reviews, {"score": 60, "count": 543})
        # 厂商收敛成 {"id", "name"}；缺 creator_clan_account_id 的项 id 为 None
        self.assertEqual(meta.publishers, [{"id": 33118426, "name": "Nacon"}])
        self.assertEqual(meta.developers, [{"id": None, "name": "Artefacts Studio"}])
        self.assertEqual(meta.release_date, 1716480182)
        # 封面：小封面 library_capsule（**不是** _2x / header）→ 相对 ASSET_BASE 的路径，?t= 丢掉
        self.assertEqual(meta.cover, "steam/apps/1658920/library_600x900.jpg")
        # 夹具里带着 purchase_options / platforms / tags（模拟老请求或服务端补全）——
        # 解析层一律无视，GameMeta 里也没有对应字段（卡片 08 已裁掉）
        self.assertFalse(hasattr(meta, "price"))
        self.assertFalse(hasattr(meta, "platforms"))
        self.assertFalse(hasattr(meta, "tags"))

    def test_invalid_appid_skipped_not_raised(self):
        """无效 appid：success != 1 / appid 非法 → None（跳过），不抛错。"""
        self.assertIsNone(parse_store_item(INVALID_ITEM))
        self.assertIsNone(
            parse_store_item({k: v for k, v in FULL_ITEM.items() if k != "success"}))
        self.assertIsNone(parse_store_item({**FULL_ITEM, "appid": 0}))
        self.assertIsNone(parse_store_item({**FULL_ITEM, "appid": "abc"}))

    def test_non_dict_items(self):
        self.assertIsNone(parse_store_item(None))
        self.assertIsNone(parse_store_item([1, 2, 3]))
        self.assertIsNone(parse_store_item("x"))

    # ---- 封面（2026-10-10）：小封面 library_capsule → 相对 ASSET_BASE 的路径 ----

    def _cover(self, assets):
        return parse_store_item({**FULL_ITEM, "assets": assets}).cover

    def test_cover_filenames_are_used_verbatim(self):
        """文件名**不固定**（实测本地化 / png / 带哈希子目录）→ 一律用服务端给的真实值，
        绝不按 appid 现拼（猜错即 404 破图）。"""
        for fn in ("library_600x900_schinese.jpg", "portrait.png",
                   "d3a027c7ea89f8b59f4b060480bdc5ad6e432f12/library_capsule.jpg"):
            cover = self._cover({"asset_url_format": "steam/apps/1658920/${FILENAME}?t=1",
                                 "library_capsule": fn})
            self.assertEqual(cover, "steam/apps/1658920/" + fn)
            self.assertNotIn("?t=", cover)          # 缓存参数去掉

    def test_cover_none_when_assets_incomplete(self):
        self.assertIsNone(self._cover(None))
        self.assertIsNone(self._cover({}))
        self.assertIsNone(self._cover({"library_capsule": "x.jpg"}))              # 缺模板
        self.assertIsNone(
            self._cover({"asset_url_format": "steam/apps/1/${FILENAME}"}))        # 缺文件名
        # 模板里没有 ${FILENAME} 占位 → 不敢猜，回落 None（走灰块占位）
        self.assertIsNone(self._cover({"asset_url_format": "steam/apps/1/fixed.jpg",
                                       "library_capsule": "x.jpg"}))

    def test_cover_ignores_bigger_assets(self):
        """只认小封面：_2x（600×900）与横版 header 存在也不被采用。"""
        cover = self._cover({
            "asset_url_format": "steam/apps/1658920/${FILENAME}",
            "library_capsule": "library_600x900.jpg",
            "library_capsule_2x": "library_600x900_2x.jpg",
            "header": "header.jpg",
        })
        self.assertEqual(cover, "steam/apps/1658920/library_600x900.jpg")
        self.assertNotIn("_2x", cover)

    def test_reviews_reads_only_summary_filtered(self):
        """⚠️ 另两套 summary 实测恒为 None → 只读 summary_filtered，不回退
        （资料库 steam_batch_api.py 的「三套依次尝试」不要照抄）。"""
        raw = {"summary_unfiltered": {"review_count": 9, "percent_positive": 55}}
        meta = parse_store_item({**FULL_ITEM, "reviews": raw})
        self.assertIsNone(meta.reviews)
        meta = parse_store_item({**FULL_ITEM, "reviews": None})
        self.assertIsNone(meta.reviews)

    def test_reviews_zero_count_normalized_to_none(self):
        """count 缺失 / 0/0 → None（P7 实测无评测游戏 GetItems 回 0/0、ITAD 回 None，
        两源必须同口径，否则 S2 派生欠账会把无评测游戏当「已有详情」）。"""
        meta = parse_store_item({**FULL_ITEM, "reviews": {
            "summary_filtered": {"percent_positive": 70}}})
        self.assertIsNone(meta.reviews)
        meta = parse_store_item({**FULL_ITEM, "reviews": {
            "summary_filtered": {"review_count": 0, "percent_positive": 0}}})
        self.assertIsNone(meta.reviews)

    def test_reviews_zero_score_with_reviews_kept(self):
        """有真实评价但好评率 0%：score=0 + count>0 保留（不是「无评测」）。"""
        meta = parse_store_item({**FULL_ITEM, "reviews": {
            "summary_filtered": {"review_count": 12, "percent_positive": 0}}})
        self.assertEqual(meta.reviews, {"score": 0, "count": 12})

    def test_missing_optional_sections(self):
        """reviews / basic_info / release 全缺：对应字段给 None / 空列表，不抛错。"""
        bare = {"appid": 570, "name": "Dota 2", "success": 1}
        meta = parse_store_item(bare)
        self.assertIsNone(meta.reviews)
        self.assertEqual(meta.publishers, [])
        self.assertEqual(meta.developers, [])
        self.assertIsNone(meta.release_date)

    def test_unrequested_fields_in_response_are_ignored(self):
        """响应带了未请求的字段（老请求 / 服务端补全）不炸、也不进结果。"""
        meta = parse_store_item({**FULL_ITEM, "purchase_options": [
            {"final_price_in_cents": "abc"},           # 脏数据
            {"final_price_in_cents": "2500"},
        ], "tags": "not-a-list", "platforms": [1, 2], "ratings": {"esrb": "M"}})
        self.assertEqual(meta.appid, 1658920)
        self.assertEqual(meta.name, "Crown Wars: The Black Prince")


class DataRequestCropTest(unittest.TestCase):
    """data_request 裁剪的**反向锁**（卡片 08）。

    关掉的开关必须**显式存在且为关闭态** —— 直接删键会让服务端按自己的默认值返回
    （不可假设为「关」），所以两个方向都要锁：别顺手加回来、也别漏写。
    """

    OFF_SWITCHES = {
        "include_all_purchase_options": False,
        "include_platforms": False,
        "include_ratings": False,
        "include_screenshots": False,
        "include_trailers": False,
        "include_full_description": False,
    }
    #: 开着的开关（含 2026-10-10 起为封面开的 ``include_assets``）。
    ON_SWITCHES = ("include_basic_info", "include_reviews", "include_release",
                   "include_assets")

    def test_consumed_switches_are_on(self):
        for key in self.ON_SWITCHES:
            self.assertIs(DATA_REQUEST.get(key), True, key)

    def test_zero_consumer_switches_are_explicitly_off(self):
        for key, expected in self.OFF_SWITCHES.items():
            self.assertIn(key, DATA_REQUEST, f"{key} 不能删键，要显式关")
            self.assertIs(DATA_REQUEST[key], expected, key)
        self.assertEqual(DATA_REQUEST.get("include_tag_count"), 0)

    def test_no_unknown_switches(self):
        """键名写错 = 静默无效（服务端不认识的键一律忽略），所以锁全集。"""
        self.assertEqual(set(DATA_REQUEST), set(self.OFF_SWITCHES) | set(self.ON_SWITCHES) | {
            "include_tag_count"})


class FetchTest(unittest.TestCase):
    def test_fetch_keys_by_appid_and_skips_invalid(self):
        client = make_client(FakeSession(200, RESPONSE))
        out = client.fetch([1658920, 9999999])
        self.assertEqual(list(out.keys()), [1658920])
        self.assertEqual(out[1658920].name, "Crown Wars: The Black Prince")

    def test_request_shape(self):
        client = make_client(FakeSession(200, RESPONSE))
        session = client.session
        client.fetch([570])
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertTrue(call["url"].endswith("/IStoreBrowseService/GetItems/v1"))
        body = body_of(call)
        self.assertEqual(body["ids"], [{"appid": 570}])
        # ⚠️ country_code 取 ITEMS_COUNTRY（US）而不是线上 country（CN）——
        # 否则国区不可售的游戏整条 success=15、连资产都拿不到（见常量注释）
        self.assertEqual(body["context"], {
            "language": "schinese", "country_code": ITEMS_COUNTRY, "steam_realm": 1})
        self.assertEqual(body["data_request"], DATA_REQUEST)

    def test_batches_capped_at_250(self):
        """实测 300 → HTTP 400：251 个 appid 必须切成 250 + 1。"""
        appids = list(range(1, 252))
        client = make_client(FakeSession(200, RESPONSE))
        session = client.session
        client.fetch(appids)
        self.assertEqual(len(session.calls), 2)
        first, second = (body_of(c)["ids"] for c in session.calls)
        self.assertEqual(len(first), MAX_BATCH_SIZE)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0], {"appid": 1})
        self.assertEqual(first[-1], {"appid": 250})
        self.assertEqual(second[0], {"appid": 251})

    def test_batch_size_clamped_to_max(self):
        client = SteamBrowseClient(make_limiter(), batch_size=1000,
                                   session=FakeSession(200, RESPONSE))
        self.assertEqual(client.batch_size, MAX_BATCH_SIZE)

    def test_dedup_preserves_order(self):
        client = make_client(FakeSession(200, RESPONSE))
        session = client.session
        client.fetch([570, 570, 123])
        ids = body_of(session.calls[0])["ids"]
        self.assertEqual(ids, [{"appid": 570}, {"appid": 123}])

    def test_empty_input_makes_no_request(self):
        client = make_client(FakeSession(200, RESPONSE))
        session = client.session
        self.assertEqual(client.fetch([]), {})
        self.assertEqual(client.fetch([0, ""]), {})
        self.assertEqual(session.calls, [])

    def test_length_aware_packing(self):
        """上限是**请求长度**而非固定条数（2026-10-04 用户实测反馈）：
        长 appid 单批装得少、更早切批；两种长度都不得超条数上限、不丢 id。"""
        short = [100000 + i for i in range(600)]      # 6 位
        long_ids = [9000000 + i for i in range(600)]  # 7 位
        cs, cl = make_client(FakeSession(200, RESPONSE)), make_client(FakeSession(200, RESPONSE))
        cs.fetch(short)
        cl.fetch(long_ids)
        sizes_s = [len(body_of(c)["ids"]) for c in cs.session.calls]
        sizes_l = [len(body_of(c)["ids"]) for c in cl.session.calls]
        self.assertTrue(all(s <= MAX_BATCH_SIZE for s in sizes_s + sizes_l))
        self.assertEqual(sum(sizes_s), 600)   # 不丢 id
        self.assertEqual(sum(sizes_l), 600)
        self.assertLess(max(sizes_l), max(sizes_s))   # 长 appid 更早切批

    def test_multi_batch_results_merged(self):
        items = [{"appid": a, "name": f"Game {a}", "success": 1} for a in (7, 8)]
        payload = {"response": {"store_items": items}}
        client = SteamBrowseClient(make_limiter(), batch_size=1,
                                   session=FakeSession(200, payload))
        out = client.fetch([7, 8])
        self.assertEqual(sorted(out.keys()), [7, 8])
        self.assertEqual(out[7].name, "Game 7")


class Http400Test(unittest.TestCase):
    def test_400_raises_not_silent(self):
        """300 起实测 HTTP 400 硬拒绝：参数错必须响亮失败，不能当「没数据」。"""
        client = make_client(FakeSession(400, {"error": "too many ids"}))
        with self.assertRaises(HttpError):
            client.fetch([570])


if __name__ == "__main__":
    unittest.main()
