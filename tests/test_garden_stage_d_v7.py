"""第七版阶段 D：品质出入库全链路。

覆盖设计稿《迷你小院子第七版内涝惩罚与作物品质设计.md》第十一节阶段 D
验收清单。阶段 A（`2c1cd33`）落地了 ``produce_poor``/``plot.quality`` 的数
据地基，阶段 B（`c22972c`）落地了内涝浸泡打欠佳标记的结算逻辑，阶段 C
（`260c821`）落地了排水动作。本文件只测阶段 D 新增的四个出库口顺序规则
（做菜/送礼/投喂欠佳优先或好品相优先）、收获分流、CLI 与网页展示面，
以及做菜可做性判断合并成 ``garden._recipe_ingredients_available`` 单一
实现后的口径一致性。不重复测阶段 A/B/C 已覆盖的内涝时钟/排水本体。

全程离线：只操作 ``tempfile.TemporaryDirectory()`` 里的临时存档，直接给
``quality``/``produce_poor`` 造合成数据（不经由内涝72小时/24小时结算，那
部分已由阶段B专项覆盖）；绝不读写真实 ``garden.json``，也绝
不经由 ``garden-web/server.py`` 的 ``run_command`` 或真实 ``home`` 二进
制、绝不调用 ``home._deliver_gift_to_basket``（它会写真实
``gift_basket.md``/``.pending-gifts.json``，测试只用源码静态
检查确认它接收的是已经带好标注的 ``display_name``，不实际执行）。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden
import garden_content
import home
import log_store


TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]


class QualityFunctionsTests(unittest.TestCase):
    """直接测 garden.py 的品质出入库函数，不经 home.py，显式 ``path=`` 隔离。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.summer = datetime(2026, 7, 10, 12, tzinfo=TZ)

    def _inventory(self, now=None):
        return garden.crop_snapshot(now=now or self.summer, path=self.path)["inventory"]

    def _set_inventory(self, **sections):
        garden.crop_snapshot(now=self.summer, path=self.path)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for section, values in sections.items():
            payload["inventory"][section] = values
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _mark_quality(self, plot_id: str, quality: str | None) -> None:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for plot in payload["plots"]:
            if plot.get("plot_id") == plot_id:
                plot["quality"] = quality
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _ripen_tomato(self, *, quality: str | None, plot_selector="1", plot_id="p1"):
        garden.crop_snapshot(now=self.summer, path=self.path)
        garden.plant_crop("小番茄", plot_selector, now=self.summer, path=self.path)
        if quality:
            self._mark_quality(plot_id, quality)
        ready_at = self.summer + timedelta(days=4)
        garden.crop_snapshot(now=ready_at, path=self.path)
        return ready_at

    # ---- 收获分流（设计稿六.3） ----

    def test_poor_plot_harvest_goes_to_produce_poor_and_journal_flags_it(self):
        initial_seeds = self._inventory()["seeds"].get("tomato", 0)
        ready_at = self._ripen_tomato(quality="poor")
        result = garden.harvest_crop("1", now=ready_at, path=self.path)
        self.assertEqual(result["quality"], "poor")
        inventory = self._inventory(ready_at)
        self.assertEqual(inventory.get("produce", {}).get("tomato", 0), 0)
        self.assertEqual(inventory["produce_poor"]["tomato"], result["amount"])
        # 种子返还不分品质：初始 - 种下1份 + 返还量。
        self.assertEqual(inventory["seeds"]["tomato"], initial_seeds - 1 + result["seed_return"])
        journal = garden.journal_snapshot(now=ready_at, path=self.path)
        self.assertEqual(journal["crops_harvested"][-1]["quality"], "poor")

    def test_normal_plot_harvest_is_unaffected_regression(self):
        ready_at = self._ripen_tomato(quality=None)
        result = garden.harvest_crop("1", now=ready_at, path=self.path)
        self.assertIsNone(result["quality"])
        inventory = self._inventory(ready_at)
        self.assertEqual(inventory["produce"]["tomato"], result["amount"])
        self.assertEqual(inventory.get("produce_poor", {}).get("tomato", 0), 0)
        journal = garden.journal_snapshot(now=ready_at, path=self.path)
        self.assertNotIn("quality", journal["crops_harvested"][-1])

    # ---- 做菜：合计判可做 + 欠佳优先消耗（设计稿六.4） ----

    def test_recipe_ingredients_available_uses_combined_quantities(self):
        recipe = garden.garden_crops.RECIPES["strawberry_compote"]
        self.assertFalse(garden._recipe_ingredients_available(
            {"produce": {"strawberry": 1}, "produce_poor": {}, "prepared_food": {}, "animal_products": {}},
            recipe,
        ))
        self.assertTrue(garden._recipe_ingredients_available(
            {"produce": {"strawberry": 1}, "produce_poor": {"strawberry": 1}, "prepared_food": {}, "animal_products": {}},
            recipe,
        ))

    def test_make_meal_prefers_poor_ingredients_and_records_amount_used(self):
        # 好1+欠佳2，需2：应先扣欠佳（全部2份），好的1份原样保留。
        self._set_inventory(produce={"strawberry": 1}, produce_poor={"strawberry": 2}, prepared_food={})
        result = garden.make_meal("草莓小果酱", now=self.summer, path=self.path)
        self.assertEqual(result["record"]["poor_ingredients"], {"strawberry": 2})
        inventory = self._inventory()
        self.assertEqual(inventory["produce"]["strawberry"], 1)
        self.assertEqual(inventory.get("produce_poor", {}).get("strawberry", 0), 0)

    def test_make_meal_without_poor_stock_leaves_poor_ingredients_field_absent(self):
        self._set_inventory(produce={"strawberry": 2}, produce_poor={}, prepared_food={})
        result = garden.make_meal("草莓小果酱", now=self.summer, path=self.path)
        self.assertNotIn("poor_ingredients", result["record"])

    def test_make_meal_insufficient_combined_stock_raises_and_does_not_touch_inventory(self):
        self._set_inventory(produce={"strawberry": 1}, produce_poor={}, prepared_food={})
        with self.assertRaisesRegex(garden.GardenError, "食材不够"):
            garden.make_meal("草莓小果酱", now=self.summer, path=self.path)
        inventory = self._inventory()
        self.assertEqual(inventory["produce"]["strawberry"], 1)
        self.assertEqual(inventory.get("produce_poor", {}).get("strawberry", 0), 0)

    # ---- 送礼：好品相优先，好的不够才动欠佳（设计稿六.4） ----

    def test_give_gift_prefers_good_then_poor_with_annotation(self):
        self._set_inventory(produce={"tomato": 1}, produce_poor={"tomato": 1})
        first = garden.give_to_baby("小番茄", now=self.summer, path=self.path)
        self.assertNotIn("欠佳", first["display_name"])
        self.assertNotIn("quality", first["record"])
        second = garden.give_to_baby("小番茄", now=self.summer, path=self.path)
        self.assertIn("（品相欠佳）", second["display_name"])
        self.assertEqual(second["record"]["quality"], "poor")
        inventory = self._inventory()
        self.assertEqual(inventory.get("produce", {}).get("tomato", 0), 0)
        self.assertEqual(inventory.get("produce_poor", {}).get("tomato", 0), 0)
        with self.assertRaisesRegex(garden.GardenError, "数量不够"):
            garden.give_to_baby("小番茄", now=self.summer, path=self.path)

    def test_give_gift_to_friend_also_gets_poor_annotation(self):
        self._set_inventory(produce={}, produce_poor={"tomato": 1})
        result = garden.give_to_friend("小番茄", now=self.summer, path=self.path)
        self.assertIn("（品相欠佳）", result["display_name"])
        self.assertEqual(result["record"]["recipient"], "friend")
        self.assertEqual(result["record"]["quality"], "poor")

    def test_give_gift_prepared_food_is_unaffected_by_quality_logic(self):
        self._set_inventory(prepared_food={"tomato_mint_salad": 1})
        result = garden.give_to_baby("番茄薄荷小沙拉", now=self.summer, path=self.path)
        self.assertNotIn("quality", result["record"])
        self.assertNotIn("欠佳", result["display_name"])

    # ---- 投喂：欠佳优先（设计稿六.4） ----

    def test_feed_animal_treat_prefers_poor_stock(self):
        rabbit = garden.spawn(
            "animal", species="兔子", intro="路过的兔子", category="兔子",
            personality="活泼", now=self.summer, path=self.path,
        )
        self._set_inventory(produce={"radish": 1}, produce_poor={"radish": 1})
        garden.feed_animal_treat(rabbit["id"], "小萝卜", now=self.summer, path=self.path)
        inventory = self._inventory()
        self.assertEqual(inventory.get("produce", {}).get("radish", 0), 1)
        self.assertEqual(inventory.get("produce_poor", {}).get("radish", 0), 0)


class HomeIntegrationTests(unittest.TestCase):
    """经 ``home.handle_garden``/CLI 私有渲染函数走的展示面（篮子/够做什么/
    手账/收获文案），全部通过 ``patch.object(garden, "GARDEN_FILE", ...)``
    隔离——这些函数内部不接受 ``path=`` 参数。不测『送给朋友』的完整
    ``home.handle_garden`` 路径（会真的写 ``gift_basket.md``），
    那部分改在 ``StaticWiringTests`` 里做源码级核对。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.file_patcher = patch.object(garden, "GARDEN_FILE", self.path)
        self.file_patcher.start()
        self.addCleanup(self.file_patcher.stop)
        self.view_state_patcher = patch.object(
            garden, "GARDEN_VIEW_STATE_FILE",
            Path(self.tempdir.name) / "garden_view_state.json",
        )
        self.view_state_patcher.start()
        self.addCleanup(self.view_state_patcher.stop)
        self.log_path = Path(self.tempdir.name) / "logs.jsonl"
        self.log_patcher = patch.object(log_store, "LOG_FILE", self.log_path)
        self.log_patcher.start()
        self.addCleanup(self.log_patcher.stop)
        self.summer = datetime(2026, 7, 10, 12, tzinfo=TZ)

    def _request(self, argv):
        return home.parse_request(argv)

    def _set_inventory(self, **sections):
        garden.crop_snapshot(now=self.summer, path=self.path)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for section, values in sections.items():
            payload["inventory"][section] = values
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _mark_quality(self, plot_id: str, quality: str | None) -> None:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for plot in payload["plots"]:
            if plot.get("plot_id") == plot_id:
                plot["quality"] = quality
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _ripen_tomato(self, *, quality: str | None):
        garden.crop_snapshot(now=self.summer, path=self.path)
        garden.plant_crop("小番茄", "1", now=self.summer, path=self.path)
        if quality:
            self._mark_quality("p1", quality)
        garden.crop_snapshot(now=self.summer + timedelta(days=4), path=self.path)

    # ---- 篮子（设计稿六.5） ----

    def test_basket_shows_poor_line_only_when_nonempty(self):
        garden.crop_snapshot(now=self.summer, path=self.path)
        self.assertNotIn("收获(欠佳)", home._garden_basket())
        self._set_inventory(produce_poor={"tomato": 2})
        output = home._garden_basket()
        self.assertIn("收获(欠佳)：小番茄×2", output)

    # ---- 够做什么（设计稿六.4） ----

    def test_makeable_recipes_uses_combined_quantities(self):
        self._set_inventory(produce={}, produce_poor={"strawberry": 2})
        inventory = garden.crop_snapshot(path=self.path)["inventory"]
        self.assertIn("草莓小果酱", home._garden_makeable_recipes(inventory))

    # ---- 手账（设计稿六.5） ----

    def test_journal_appends_quality_suffix_for_poor_harvest(self):
        self._ripen_tomato(quality="poor")
        garden.harvest_crop("1", now=self.summer + timedelta(days=4), path=self.path)
        self.assertIn("小番茄（品相欠佳）", home._garden_journal())

    def test_journal_normal_harvest_has_no_quality_suffix(self):
        self._ripen_tomato(quality=None)
        garden.harvest_crop("1", now=self.summer + timedelta(days=4), path=self.path)
        output = home._garden_journal()
        self.assertIn("小番茄", output)
        self.assertNotIn("小番茄（品相欠佳）", output)

    # ---- 收获文案（设计稿六.3：欠佳池、彩蛋不触发、"结果"行说明入欠佳堆） ----

    def test_home_harvest_poor_uses_poor_pool_and_never_the_surprise_pool(self):
        self._ripen_tomato(quality="poor")
        with patch("home.random.random", return_value=0.0):
            output = home.handle_garden(self._request(["院子", "收获", "1"]))
        self.assertTrue(
            any(text.format(name="小番茄") in output for text in garden_content.HARVEST_POOR_TEXT)
        )
        self.assertIn("欠佳", output)
        for text in garden_content.HARVEST_SURPRISE_TEXT:
            self.assertNotIn(text.format(name="小番茄"), output)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["inventory"]["produce_poor"]["tomato"], 3)
        self.assertNotIn("tomato", raw["inventory"].get("produce", {}))

    def test_home_harvest_normal_quality_still_allows_the_surprise_pool(self):
        self._ripen_tomato(quality=None)
        with patch("home.random.random", return_value=0.0):
            output = home.handle_garden(self._request(["院子", "收获", "1"]))
        self.assertTrue(
            any(text.format(name="小番茄") in output for text in garden_content.HARVEST_SURPRISE_TEXT)
        )

    def test_home_harvest_normal_quality_default_pool_when_surprise_not_rolled(self):
        self._ripen_tomato(quality=None)
        with patch("home.random.random", return_value=0.99):
            output = home.handle_garden(self._request(["院子", "收获", "1"]))
        for text in garden_content.HARVEST_SURPRISE_TEXT:
            self.assertNotIn(text.format(name="小番茄"), output)
        for text in garden_content.HARVEST_POOR_TEXT:
            self.assertNotIn(text.format(name="小番茄"), output)

    # ---- 送给user：CLI 端到端（不涉及 朋友/真实文件） ----

    def test_home_gift_to_baby_surfaces_poor_annotation_and_journal_field(self):
        self._set_inventory(produce_poor={"tomato": 1})
        output = home.handle_garden(self._request(["院子", "送给user", "小番茄"]))
        self.assertIn("（品相欠佳）", output)
        journal = garden.journal_snapshot(path=self.path)
        self.assertEqual(journal["gifts_given"][-1]["quality"], "poor")


class StaticWiringTests(unittest.TestCase):
    """网页/CLI 静态接线核对；不启动真实服务器、不经由 ``run_command``
    （会调用真实 ``home`` 二进制命中生产 ``garden.json``），不执行前端 JS
    （用 Python 侧对照测试证明 CLI 与网页共用同一个可做性判断，见
    ``QualityFunctionsTests``/``HomeIntegrationTests`` 里对
    ``garden._recipe_ingredients_available`` 的直接覆盖；这里只核对
    server.py 确实把它算好传给了前端、前端确实读的是这个字段而不是自己
    重算——跟既有 ``test_garden_stage_c_v7.py`` 同款风格）。"""

    def test_server_recipes_payload_uses_the_shared_availability_function(self):
        source = (ROOT / "garden-web/server.py").read_text(encoding="utf-8")
        self.assertIn(
            '"makeable": garden._recipe_ingredients_available(inventory, recipe)', source,
        )

    def test_cook_sheet_reads_the_makeable_field_instead_of_recomputing(self):
        html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")
        cook_sheet = html.split("function openCookSheet()")[1].split("function openEggCountSheet")[0]
        self.assertIn("r.makeable", cook_sheet)
        # 旧的本地重算（逐条 produce[c]/preparedFood[p] 门槛比较）不应该
        # 再出现，否则又会退回第七版之前"两边各算一遍"的分裂风险。
        self.assertNotIn("produce[c]", cook_sheet)
        self.assertNotIn("preparedFood[p]", cook_sheet)

    def test_basket_popup_renders_poor_section_conditionally_with_a_tag(self):
        html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")
        basket = html.split("function openBasketPopup()")[1].split("function openCoopSheet")[0]
        self.assertIn("produce_poor", basket)
        self.assertIn("quality-tag", basket)

    def test_journal_popup_appends_quality_suffix(self):
        html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")
        journal_popup = html.split("function openJournalPopup()")[1].split('qs("#btn-journal")')[0]
        self.assertIn('r.quality === "poor"', journal_popup)

    def test_gift_box_and_cli_journal_get_the_annotation_for_free_via_display_name(self):
        # 送礼的品相欠佳标注在 `_give_gift` 写入 display_name 那一刻就已经
        # 确定；礼物盒/手账渲染只是原样展示 display_name，不需要再单独判
        # 断 quality 字段——这里核对确实没有另起一套判断分叉（否则两处
        # 措辞可能不一致）。
        html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")
        gift_box = html.split("function openGiftBoxPopup()")[1].split('qs("#btn-giftbox")')[0]
        self.assertIn("g.display_name", gift_box)
        self.assertNotIn("g.quality", gift_box)

    def test_deliver_gift_to_basket_receives_the_already_annotated_display_name(self):
        # `_deliver_gift_to_basket` 本身不需要改：`give_to_friend` 返回的
        # display_name 已经在 garden.py 里带好"（品相欠佳）"标注，这里只
        # 核对调用点确实传的是 result["display_name"]，没有另起一份文案。
        source = (ROOT / "home.py").read_text(encoding="utf-8")
        self.assertIn('_deliver_gift_to_basket(result["display_name"], note)', source)

    def test_harvest_poor_pool_style_and_placeholder_check(self):
        pool = garden_content.HARVEST_POOR_TEXT
        self.assertGreaterEqual(len(pool), 6)
        self.assertEqual(len(set(pool)), len(pool))
        # 欠佳池禁止出现夸品相的词——这些是"品相意外地好"的彩蛋池
        # （HARVEST_SURPRISE_TEXT）和作物专属池常用的措辞，用在欠佳收获
        # 上会跟"结果：进了欠佳堆"的事实自相矛盾。
        banned = ("水灵", "饱满", "脆生", "新鲜", "肥美", "圆润", "笔直", "香甜", "多汁", "周正")
        for text in pool:
            for word in banned:
                self.assertNotIn(word, text)
            self.assertIn("{name}", text)
            self.assertTrue(text.format(name="番茄"))


if __name__ == "__main__":
    unittest.main()
