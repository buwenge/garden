import tempfile
import json
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import garden
import home
import log_store
from garden_generator import GardenGeneratorError

TZ = ZoneInfo("Asia/Shanghai")


class HandleGardenTests(unittest.TestCase):
    """production 的 handle_garden/_garden_list_* 内部都用真实 datetime.now(TZ)，
    跟 reminders 的 handle_reminder 是同一个风格，不额外注入 now。测试里需要控制
    "距离上次照顾过去了多久"时，一律相对真实当下往前推一个安全的偏移量，
    不使用固定字面日期，避免依赖测试实际运行的那一天。
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        # 通用 home 院子用例锁定第四版基线；需要验证现实环境规则的用例会在
        # 自己的作用域显式打开开关，不能再隐式依赖开发机有没有加载 .env。
        environment = patch.dict("os.environ", {"GARDEN_REAL_ENVIRONMENT_ENABLED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        writer = patch(
            "garden.garden_generator.generate_crop_copy",
            side_effect=GardenGeneratorError("离线测试"),
        )
        writer.start()
        self.addCleanup(writer.stop)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.patcher = patch.object(garden, "GARDEN_FILE", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        # 整院查看的 delta 指纹也必须隔离，避免带 SESSION_ID 的用例
        # 写进生产状态文件。
        self.view_state_patcher = patch.object(
            garden, "GARDEN_VIEW_STATE_FILE",
            Path(self.tempdir.name) / "garden_view_state.json",
        )
        self.view_state_patcher.start()
        self.addCleanup(self.view_state_patcher.stop)
        # handle_garden 在真正浇水/投喂/摸摸/取名成功后会写 activity 日志，
        # 不隔离会污染生产 logs.jsonl（7/25 曾误写入过一次，已手动清理）。
        self.log_path = Path(self.tempdir.name) / "logs.jsonl"
        self.log_patcher = patch.object(log_store, "LOG_FILE", self.log_path)
        self.log_patcher.start()
        self.addCleanup(self.log_patcher.stop)

    def _request(self, argv):
        return home.parse_request(argv)

    def _seasonal_crop(self):
        """挑一个当前真实季节能种的作物，而不是写死某个季节独占的作物，避免测试
        每次换季（8/7 立秋后就撞上过一次）就跟着挂。不检查具体成菜文案的用例
        应优先用这个，而不是硬编码"小番茄"（只在夏季能种）。优先选没有专属
        文案池（`CROP_VARIETY_TEXT`）的作物，让"播下/成熟"这类通用措辞断言
        不会因为随机抽到该作物的专属文案而偶发失败；找不到就退回随便一个。"""
        season = garden.calendar_context(datetime.now(TZ)).season
        candidates = [
            (crop_id, crop["name"])
            for crop_id, crop in garden.garden_crops.CROPS.items()
            if season in crop["seasons"]
        ]
        if not candidates:
            raise AssertionError(f"没有作物覆盖当前季节 {season}，测试夹具需要更新")
        generic = [
            (crop_id, name) for crop_id, name in candidates
            if crop_id not in garden.garden_content.CROP_VARIETY_TEXT
        ]
        return (generic or candidates)[0]

    def test_empty_garden_listing(self):
        request = self._request(["院子"])
        output = home.handle_garden(request)
        self.assertIn("一切都是从空白开始", output)
        self.assertIn("某个自然的一次唤醒", output)

    def test_listing_keeps_natural_away_animal_visible(self):
        old_arrival = datetime.now(TZ) - timedelta(hours=garden.NATURAL_VISIT_MAX_HOURS + 1)
        entry = garden.spawn("animal", species="橘猫", intro="x", now=old_arrival)
        entry["nickname"] = "暖暖"
        garden.save_garden([entry])
        output = home.handle_garden(self._request(["院子", "查看"]))
        self.assertIn("暖暖", output)
        self.assertIn("出去转转了", output)
        self.assertNotIn("一切都是从空白开始", output)

    def test_listing_merges_animals_with_the_same_away_status(self):
        old_arrival = datetime.now(TZ) - timedelta(
            hours=garden.NATURAL_VISIT_MAX_HOURS + 1,
        )
        first = garden.spawn("animal", species="橘猫", intro="x", now=old_arrival)
        first["nickname"] = "暖暖"
        second = garden.spawn("animal", species="奶牛猫", intro="x", now=old_arrival)
        second["nickname"] = "小奶牛"
        garden.save_garden([first, second])

        output = home.handle_garden(self._request(["院子", "查看"]))

        self.assertIn("暖暖（橘猫）、小奶牛（奶牛猫）出去转转了", output)
        self.assertEqual(output.count("出去转转了"), 1)

    def test_view_lists_active_entry_with_stage(self):
        garden.spawn("flower", species="蒲公英", intro="x")
        request = self._request(["院子", "查看"])
        output = home.handle_garden(request)
        self.assertIn("蒲公英", output)
        self.assertIn("精神奕奕", output)

    def test_plan_alias_routes_to_garden_domain(self):
        request = self._request(["小院子"])
        self.assertEqual(request.domain, "garden")

    def test_water_action_with_explicit_id(self):
        entry = garden.spawn("flower", species="蒲公英", intro="x")
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")):
            output = home.handle_garden(self._request(["院子", f"浇水{entry['id']}"]))
        self.assertIn("蒲公英", output)
        updated = garden.active_entries()[0]
        self.assertEqual(updated["care_count"], 1)

    def test_water_action_without_id_when_unambiguous(self):
        garden.spawn("flower", species="蒲公英", intro="x")
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")):
            output = home.handle_garden(self._request(["院子", "浇水"]))
        self.assertIn("蒲公英", output)

    def test_water_action_without_id_when_ambiguous_raises(self):
        garden.spawn("flower", species="蒲公英", intro="x")
        garden.spawn("flower", species="雏菊", intro="x")
        with self.assertRaisesRegex(home.HomeError, "不止一个"):
            home.handle_garden(self._request(["院子", "浇水"]))

    def test_wrong_action_for_kind_raises_home_error(self):
        entry = garden.spawn("animal", species="橘猫", intro="x")
        with self.assertRaises(home.HomeError):
            home.handle_garden(self._request(["院子", f"浇水{entry['id']}"]))

    def test_unknown_id_raises_home_error(self):
        with self.assertRaises(home.HomeError):
            home.handle_garden(self._request(["院子", "浇水ffffff"]))

    def test_dry_run_does_not_persist(self):
        entry = garden.spawn("flower", species="蒲公英", intro="x")
        output = home.handle_garden(self._request(["院子", "--dry-run", f"浇水{entry['id']}"]))
        self.assertIn("将对", output)
        self.assertEqual(garden.active_entries()[0]["care_count"], 0)

    def test_late_interaction_never_claims_to_revive_an_animal(self):
        old_arrival = datetime.now(TZ) - timedelta(hours=48)
        entry = garden.spawn("animal", species="橘猫", intro="x", now=old_arrival)
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")):
            output = home.handle_garden(self._request(["院子", f"投喂{entry['id']}"]))
        normal_pool = garden.garden_content.ANIMAL_REACTION_BY_PERSONALITY["猫"][entry["personality"]]["投喂"]
        self.assertTrue(any(line in output for line in normal_pool))
        self.assertNotIn("精神好多了", output)

    def test_repeated_interaction_stays_a_normal_response(self):
        old_arrival = datetime.now(TZ) - timedelta(hours=48)
        entry = garden.spawn("animal", species="橘猫", intro="x", now=old_arrival)
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")):
            home.handle_garden(self._request(["院子", f"投喂{entry['id']}"]))
            output = home.handle_garden(self._request(["院子", f"投喂{entry['id']}"]))
        normal_pool = garden.garden_content.ANIMAL_REACTION_BY_PERSONALITY["猫"][entry["personality"]]["投喂"]
        self.assertTrue(any(line in output for line in normal_pool))

    def test_play_command_is_parsed_and_reports_bond_without_network(self):
        entry = garden.spawn("animal", species="橘猫", intro="x")
        entry["bond_points"] = 8
        garden.save_garden([entry])
        with patch("garden.garden_generator.generate_reaction") as generator:
            output = home.handle_garden(self._request(["院子", f"陪玩{entry['id']}"]))
        generator.assert_not_called()
        self.assertIn("结果：已经陪", output)
        self.assertIn("亲密 9｜愿意靠近", output)

    def test_play_help_and_low_bond_message_are_clear(self):
        entry = garden.spawn("animal", species="橘猫", intro="x")
        self.assertIn("陪", home.garden_help("动物"))
        with self.assertRaisesRegex(home.HomeError, "慢慢熟悉"):
            home.handle_garden(self._request(["院子", f"陪玩{entry['id']}"]))

    def test_name_entry_command_and_listing(self):
        entry = garden.spawn("animal", species="橘猫", intro="x")
        output = home.handle_garden(
            self._request(["院子", "取名", entry["id"], "小橘"])
        )
        self.assertIn("小橘", output)
        listing = home.handle_garden(self._request(["院子", "查看"]))
        self.assertIn("小橘", listing)
        detailed = home.handle_garden(self._request(["院子", "查看", "详细"]))
        self.assertIn("小橘（橘猫）", detailed)

    def test_nickname_and_unique_species_can_target_animal_actions(self):
        first = garden.spawn("animal", species="橘猫", intro="x")
        garden.name_entry(first["id"], "暖暖")
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")):
            by_nickname = home.handle_garden(self._request(["院子", "摸摸", "暖暖"]))
        self.assertIn("暖暖", by_nickname)

        second = garden.spawn("animal", species="奶牛猫", intro="x")
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")):
            by_species = home.handle_garden(self._request(["院子", "摸摸", "奶牛猫"]))
        self.assertIn("奶牛猫", by_species)

    def test_ambiguous_species_still_requires_a_nickname_or_id(self):
        garden.spawn("animal", species="橘猫", intro="x")
        garden.spawn("animal", species="橘猫", intro="x")
        with self.assertRaisesRegex(home.HomeError, "匹配到多条"):
            home.handle_garden(self._request(["院子", "摸摸", "橘猫"]))

    def test_natural_batch_animal_care_targets_specific_named_subset(self):
        # 不是"全摸"也不是"只摸一个"——按名字连续点名几个特定动物，
        # 每个都要真实执行且各自返回结果，跟"浇水 2 4"是同一种批量思路。
        cat = garden.spawn("animal", species="橘猫", intro="x", category="猫", personality="粘人")
        hedgehog = garden.spawn("animal", species="刺猬", intro="x", category="刺猬", personality="慢热")
        garden.name_entry(cat["id"], "暖暖")
        garden.name_entry(hedgehog["id"], "栗子")
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")):
            result = home.handle_garden(self._request(["院子", "摸摸暖暖和栗子"]))
        self.assertIn("暖暖", result)
        self.assertIn("栗子", result)
        self.assertEqual(result.count("结果：已经摸了摸"), 2)
        updated = {entry["nickname"]: entry for entry in garden.active_entries()}
        self.assertEqual(updated["暖暖"]["care_count"], 1)
        self.assertEqual(updated["栗子"]["care_count"], 1)

    def test_batch_animal_care_does_not_hijack_treat_feeding(self):
        # "投喂团团一点南瓜"这类单目标+物品的说法不能被误拆成两个目标。
        dog = garden.spawn("animal", species="小狗", intro="x", category="狗", personality="活泼")
        garden.name_entry(dog["id"], "团团")
        state = garden._read_state_unlocked(garden.GARDEN_FILE, now=datetime.now(TZ))
        state["inventory"]["produce"]["pumpkin"] = 2
        garden._write_state_unlocked(state, garden.GARDEN_FILE)
        result = home.handle_garden(self._request(["院子", "投喂", "团团", "南瓜"]))
        self.assertIn("留了一小份南瓜", result)

    def test_name_entry_dry_run_does_not_persist(self):
        entry = garden.spawn("animal", species="橘猫", intro="x")
        output = home.handle_garden(
            self._request(["院子", "--dry-run", "取名", entry["id"], "小橘"])
        )
        self.assertIn("将把", output)
        self.assertIsNone(garden.active_entries()[0]["nickname"])

    def test_history_lists_left_entries(self):
        entry = garden.spawn("flower", species="蒲公英", intro="x")
        entry["status"] = "left"
        garden.save_garden([entry])
        output = home.handle_garden(self._request(["院子", "记录"]))
        self.assertIn("蒲公英", output)

    def test_journal_command_shows_memories_without_progress_or_urging(self):
        garden.spawn("animal", species="橘猫", intro="x")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["journal"].update({
            "crops_harvested": [{"crop_id": "tomato"}],
            "meals_made": [{"recipe_id": "tomato_mint_salad"}],
            "gifts_given": [{"display_name": "一份番茄薄荷小沙拉"}],
            "bond_milestones": [{"animal_name": "暖暖", "level_name": "愿意靠近"}],
            "calendar_moments": [{"name": "立夏"}],
        })
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        output = home.handle_garden(self._request(["院子", "手账"]))
        for expected in ("已发现物种", "橘猫", "小番茄", "番茄薄荷小沙拉", "暖暖", "立夏"):
            self.assertIn(expected, output)
        self.assertNotIn("完成率", output)
        self.assertNotIn("还差", output)

    def test_help_via_flag_is_short_and_routes_to_categories(self):
        output = home.handle_garden(self._request(["院子", "--help"]))
        for category in ("查看", "种地", "动物", "食物", "鸡舍"):
            self.assertIn(category, output)
        self.assertLessEqual(len(output), 180)
        self.assertNotIn("凉拌番茄", output)

        food = home.handle_garden(self._request(["院子", "食物", "--help"]))
        # 8/6 起帮助页不再摊开全部菜谱：只列篮子现在够做的，
        # 其余交给"食谱 <作物>"和"食谱大全"按需检索。
        self.assertIn("凑不成一道菜", food)
        self.assertIn("食谱大全", food)
        self.assertNotIn("给二号地浇点水", food)
        self.assertLessEqual(len(food), 200)

        garden.crop_snapshot()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["produce"]["cucumber"] = 2
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        stocked = home.handle_garden(self._request(["院子", "食物", "--help"]))
        self.assertIn("篮子现在够做", stocked)
        self.assertIn("拍黄瓜", stocked)
        self.assertNotIn("辣椒炒蛋", stocked)

        crop = home.handle_garden(self._request(["院子", "种地", "怎么用"]))
        self.assertIn("全部浇水", crop)
        self.assertNotIn("摸摸团团", crop)
        self.assertLessEqual(len(crop), 220)

    def test_cli_intro_is_once_per_session_and_help_does_not_consume_it(self):
        intro_path = Path(self.tempdir.name) / "session-intros.json"

        def run(argv, session_id):
            stream = io.StringIO()
            with patch.object(garden, "GARDEN_SESSION_INTRO_FILE", intro_path), patch.dict(
                "os.environ", {"SESSION_ID": session_id}, clear=False,
            ), redirect_stdout(stream):
                self.assertEqual(home.main(argv), 0)
            return stream.getvalue()

        help_text = run(["院子", "--help"], "uuid-a")
        first = run(["院子", "查看"], "uuid-a")
        repeated = run(["院子", "查看"], "uuid-a")
        next_session = run(["院子", "查看"], "uuid-b")
        self.assertNotIn(garden.GARDEN_SESSION_INTRO, help_text)
        self.assertIn(garden.GARDEN_SESSION_INTRO, first)
        self.assertNotIn(garden.GARDEN_SESSION_INTRO, repeated)
        self.assertIn(garden.GARDEN_SESSION_INTRO, next_session)

    def test_repeat_view_in_same_session_reports_only_changes(self):
        crop_id, crop_name = self._seasonal_crop()
        garden.crop_snapshot()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["seeds"][crop_id] = 4
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        home.handle_garden(self._request(["院子", "播种", crop_name, "1"]))
        with patch.dict("os.environ", {"SESSION_ID": "uuid-view"}, clear=False):
            first = home.handle_garden(self._request(["院子", "查看"]))
            self.assertIn("小院子现在有", first)
            self.assertIn(f"1号{crop_name}刚播下", first)
            repeated = home.handle_garden(self._request(["院子", "查看"]))
            self.assertNotIn("小院子现在有", repeated)
            self.assertIn("跟上次看的时候差不多", repeated)
            home.handle_garden(self._request(["院子", "播种", crop_name, "2"]))
            after_change = home.handle_garden(self._request(["院子", "查看"]))
            self.assertIn(f"上次看过之后：2号地种下了{crop_name}。", after_change)
            self.assertNotIn("1号", after_change)
        # 新 session 重新给完整概览。
        with patch.dict("os.environ", {"SESSION_ID": "uuid-view-2"}, clear=False):
            fresh = home.handle_garden(self._request(["院子", "查看"]))
            self.assertIn("小院子现在有", fresh)
        # 没有 session 标识（人工 shell/测试）时永远给完整概览，也不消费指纹。
        anonymous = home.handle_garden(self._request(["院子", "查看"]))
        self.assertIn("小院子现在有", anonymous)

    def test_recipe_lookup_by_ingredient_and_catalog(self):
        output = home.handle_garden(self._request(["院子", "食谱", "黄瓜"]))
        self.assertIn("黄瓜可以做", output)
        self.assertIn("拍黄瓜", output)
        self.assertIn("凉拌黄瓜", output)
        self.assertNotIn("薄荷水", output)
        eggs = home.handle_garden(self._request(["院子", "鸡蛋能做什么"]))
        self.assertIn("辣椒炒蛋", eggs)
        self.assertIn("番茄炒蛋", eggs)
        self.assertIn("蛋1~3", eggs)
        catalog = home.handle_garden(self._request(["院子", "食谱大全"]))
        for recipe in garden.garden_crops.RECIPES.values():
            self.assertIn(recipe["name"], catalog)
        with self.assertRaisesRegex(home.HomeError, "没认出这种食材"):
            home.handle_garden(self._request(["院子", "食谱", "月光"]))

    def test_harvest_appends_recipe_hint_and_surprise_keeps_facts(self):
        # 断言绑定黄瓜专属食谱文案（凉拌黄瓜/拍黄瓜），换成别的作物就对不上了；
        # 冻结在一个固定的夏季时间点，不依赖测试实际运行是哪一天/哪个季节。
        now = datetime(2026, 7, 20, 10, 0, tzinfo=TZ)
        garden.crop_snapshot(now=now)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["seeds"]["cucumber"] = 2
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        def ripen_plot(index):
            data = json.loads(self.path.read_text(encoding="utf-8"))
            data["plots"][index]["status"] = "ready"
            data["plots"][index]["stage"] = "ready"
            data["plots"][index]["growth_points"] = 4.0
            data["plots"][index]["ready_at"] = now.isoformat()
            self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        garden.plant_crop("黄瓜", "1", now=now)
        ripen_plot(0)
        with patch("home.random.random", return_value=0.99):
            plain = home.handle_garden(self._request(["院子", "收获", "1"]))
        self.assertIn("可以做：凉拌黄瓜、拍黄瓜。", plain)

        # 换到二号地继续验证"惊喜收获"分支，跟一号地互不影响。
        garden.plant_crop("黄瓜", "2", now=now)
        ripen_plot(1)
        with patch("home.random.random", return_value=0.01):
            surprised = home.handle_garden(self._request(["院子", "收获", "2"]))
        self.assertIn("结果：黄瓜×", surprised)
        self.assertIn("种子返还×", surprised)
        self.assertIn("可以做：凉拌黄瓜、拍黄瓜。", surprised)
        self.assertTrue(
            any(
                template.format(name="黄瓜") in surprised
                for template in garden.garden_content.HARVEST_SURPRISE_TEXT
            )
        )

    def test_basket_labels_animal_products_as_eggs(self):
        garden.crop_snapshot()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["animal_products"] = {"egg": 3}
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        output = home.handle_garden(self._request(["院子", "篮子"]))
        self.assertIn("蛋类：鸡蛋×3", output)
        self.assertNotIn("动物带来的东西", output)

    def test_warehouse_holds_off_season_seeds_hidden_from_view_and_basket(self):
        now = datetime.now(TZ)
        season = garden.calendar_context(now).season
        off_season_crop_id, off_season_crop = next(
            (crop_id, crop) for crop_id, crop in garden.garden_crops.CROPS.items()
            if season not in crop["seasons"]
        )
        in_season_crop_id, in_season_crop = next(
            (crop_id, crop) for crop_id, crop in garden.garden_crops.CROPS.items()
            if season in crop["seasons"]
        )
        garden.crop_snapshot()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["seeds"] = {in_season_crop_id: 2}
        raw["inventory"]["warehouse_seeds"] = {off_season_crop_id: 3}
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        view = home.handle_garden(self._request(["院子", "查看"]))
        self.assertNotIn(off_season_crop["name"], view)
        detailed = home.handle_garden(self._request(["院子", "查看", "详细"]))
        self.assertNotIn(off_season_crop["name"], detailed)
        basket = home.handle_garden(self._request(["院子", "篮子"]))
        self.assertIn(in_season_crop["name"], basket)
        self.assertNotIn(off_season_crop["name"], basket)

        for phrase in ("仓库", "查看仓库", "看看仓库", "检查仓库"):
            self.assertEqual(home._garden_semantic_command(phrase), "仓库")
        warehouse = home.handle_garden(self._request(["院子", "仓库"]))
        self.assertIn(off_season_crop["name"], warehouse)
        self.assertIn("×3", warehouse)
        self.assertNotIn(in_season_crop["name"], warehouse)

        for phrase in ("现在能种什么", "还能种啥", "查看能种的"):
            self.assertEqual(home._garden_semantic_command(phrase), "能种什么")
        plantable = home.handle_garden(self._request(["院子", "能种什么"]))
        self.assertIn(in_season_crop["name"], plantable)
        self.assertNotIn(off_season_crop["name"], plantable)
        self.assertIn("仓库", plantable)

    def test_warehouse_empty_message(self):
        output = home.handle_garden(self._request(["院子", "仓库"]))
        self.assertIn("仓库现在是空的", output)

    def test_garden_semantic_fallback_canonicalizes_common_natural_phrases(self):
        self.assertEqual(home._garden_semantic_command("给二号地浇点水"), "浇水 2")
        self.assertEqual(home._garden_semantic_command("把辣椒种到三号地"), "播种 辣椒 3")
        self.assertEqual(home._garden_semantic_command("种 黄瓜 3"), "播种 黄瓜 3")
        self.assertEqual(home._garden_semantic_command("浇水 2"), "浇水 2")
        self.assertEqual(home._garden_semantic_command("收获 3"), "收获 3")
        self.assertEqual(home._garden_semantic_command("查看 2 4"), "查看 2 4")
        self.assertEqual(home._garden_semantic_command("黄瓜能做什么"), "食谱 黄瓜")
        self.assertEqual(home._garden_semantic_command("鸡蛋可以做什么菜"), "食谱 鸡蛋")
        self.assertEqual(home._garden_semantic_command("看看菜谱大全"), "食谱 大全")
        self.assertEqual(home._garden_semantic_command("看看院子的详细情况"), "查看 详细")
        self.assertEqual(home._garden_semantic_command("用黄瓜拍一个"), "做点吃的 拍黄瓜")
        self.assertEqual(home._garden_semantic_command("制作薄荷水"), "做点吃的 薄荷水")
        self.assertEqual(
            home._garden_semantic_command("制作辣椒炒蛋，放两个蛋"),
            "做点吃的 辣椒炒蛋 2个蛋",
        )
        self.assertEqual(
            home._garden_semantic_command("番茄炒蛋放三个鸡蛋"),
            "做点吃的 番茄炒蛋 3个蛋",
        )
        self.assertEqual(
            home._garden_semantic_command("把篮子里的可孵化鸡蛋孵了"),
            "可孵化鸡蛋 孵化",
        )

    def test_natural_pepper_eggs_uses_xiaoyus_chosen_egg_count(self):
        garden.crop_snapshot()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["inventory"]["produce"] = {"pepper": 1}
        payload["inventory"]["animal_products"] = {"egg": 3}
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        output = home.handle_garden(
            self._request(["院子", "制作辣椒炒蛋，放两个蛋"])
        )
        self.assertIn("辣椒×1、鸡蛋×2", output)
        inventory = garden.crop_snapshot()["inventory"]
        self.assertEqual(inventory["animal_products"], {"egg": 1})
        self.assertEqual(inventory["prepared_food"], {"pepper_eggs": 1})

    def test_natural_tomato_eggs_uses_specific_copy_and_chosen_egg_count(self):
        garden.crop_snapshot()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["inventory"]["produce"] = {"tomato": 1}
        payload["inventory"]["animal_products"] = {"egg": 3}
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        with patch("home.random.choice", side_effect=lambda lines: lines[0]):
            output = home.handle_garden(
                self._request(["院子", "番茄炒蛋放三个鸡蛋"])
            )
        self.assertIn("小番茄的汁水裹住了刚炒好的蛋块", output)
        self.assertIn("小番茄×1、鸡蛋×3", output)
        inventory = garden.crop_snapshot()["inventory"]
        self.assertEqual(inventory["animal_products"], {})
        self.assertEqual(inventory["prepared_food"], {"tomato_eggs": 1})

    def test_new_summer_crops_use_their_specific_plant_and_harvest_copy(self):
        for crop_id, crop_name in (
            ("cucumber", "黄瓜"), ("mini_watermelon", "西瓜"), ("pepper", "辣椒"),
        ):
            with self.subTest(crop=crop_name):
                with patch("home.random.choice", side_effect=lambda lines: lines[0]):
                    planted = home._garden_action_text(
                        "plant", crop_id=crop_id, plot="一号", name=crop_name,
                    )
                self.assertEqual(
                    planted,
                    garden.garden_content.CROP_VARIETY_TEXT[crop_id]["plant"][0].format(
                        plot="一号",
                    ),
                )
                with patch("home.random.choice", side_effect=lambda lines: lines[0]):
                    harvested = home._garden_action_text(
                        "harvest", crop_id=crop_id, amount=3, seed_return=1,
                        name=crop_name,
                    )
                self.assertIn(f"{crop_name}3份", harvested)
                self.assertIn("种子1份", harvested)

    def test_new_autumn_crops_use_their_specific_plant_and_harvest_copy(self):
        for crop_id, crop_name in (
            ("chestnut", "板栗"), ("osmanthus", "桂花"), ("sweet_potato", "红薯"),
        ):
            with self.subTest(crop=crop_name):
                with patch("home.random.choice", side_effect=lambda lines: lines[0]):
                    planted = home._garden_action_text(
                        "plant", crop_id=crop_id, plot="一号", name=crop_name,
                    )
                self.assertEqual(
                    planted,
                    garden.garden_content.CROP_VARIETY_TEXT[crop_id]["plant"][0].format(
                        plot="一号",
                    ),
                )
                with patch("home.random.choice", side_effect=lambda lines: lines[0]):
                    harvested = home._garden_action_text(
                        "harvest", crop_id=crop_id, amount=2, seed_return=1,
                        name=crop_name,
                    )
                self.assertIn(crop_name, harvested)
                self.assertIn("2份", harvested)
                self.assertIn("种子1份", harvested)

    def test_recipe_catalog_text_shows_prepared_food_as_an_ingredient(self):
        catalog = home._garden_recipe_catalog_text()
        self.assertIn("蘸水白菜（小白菜+辣椒粉）", catalog)
        self.assertIn("桂花蜜泡水（桂花蜜）", catalog)
        self.assertIn("板栗南瓜羹（板栗+南瓜）", catalog)

    def test_failed_action_is_explicit_and_only_teaches_the_related_intent(self):
        with self.assertRaises(home.HomeError) as caught:
            home.handle_garden(self._request(["院子", "播种"]))
        message = str(caught.exception)
        self.assertTrue(message.startswith("未执行："))
        self.assertIn("把辣椒种到三号地", message)
        self.assertNotIn("摸摸团团", message)
        self.assertLessEqual(len(message), 260)

    def test_unknown_action_failure_stays_short(self):
        with self.assertRaises(home.HomeError) as caught:
            home.handle_garden(self._request(["院子", "把云搬到月亮背面"]))
        message = str(caught.exception)
        self.assertIn("未执行：没听懂", message)
        self.assertIn("home 院子 --help", message)
        self.assertLessEqual(len(message), 220)

    def _mute_calendar_moments(self):
        # 2026-08-19 七夕实锤：这几个鸡舍用例用真实当前日期驱动 run_tick，
        # 撞上节日/节气当天时 calendar_moment 会先入队占住 pending 租约，
        # 鸡蛋事件永远排不进来，用例在节日当天必挂。日历时刻有自己的
        # 专项用例（显式指定日期），这里按住不让它入队，只考察鸡舍链路。
        muted = patch.object(garden, "_queue_calendar_moments", lambda state, context: False)
        muted.start()
        self.addCleanup(muted.stop)

    def test_natural_egg_offer_precedes_coop_and_incubation_builds_it(self):
        self._mute_calendar_moments()
        premature = home.handle_garden(self._request(["院子", "搭个鸡窝吧"]))
        self.assertIn("不会提前搭鸡舍", premature)
        self.assertFalse(garden.coop_snapshot()["built"])
        animal = garden.spawn(
            "animal", species="荷兰侏儒兔", intro="x", category="兔子", personality="活泼",
        )
        animal["nickname"] = "团团"
        garden.save_garden([animal])
        event = garden.run_tick(datetime.now(TZ) + timedelta(hours=1), rng=Mock(choice=lambda items: items[0]))
        self.assertEqual(event["type"], "coop_egg_offer")

        result = home.handle_garden(self._request(["院子", "我来孵那三枚蛋吧"]))
        self.assertIn("搭好了鸡舍", result)
        self.assertIn("整个过程21小时", result)
        coop = garden.coop_snapshot()
        self.assertEqual(
            (coop["story_status"], coop["egg_count"], coop["built"]),
            ("incubating", 0, True),
        )

    def test_natural_chicken_interactions_target_one_chicken_and_return_results(self):
        self._mute_calendar_moments()
        now = datetime.now(TZ)
        animal = garden.spawn(
            "animal", species="荷兰侏儒兔", intro="x", category="兔子",
            personality="活泼", now=now,
        )
        garden.run_tick(now + timedelta(hours=1), rng=Mock(choice=lambda items: items[0]))
        started = now + timedelta(hours=1, minutes=1)
        garden.resolve_coop_egg_choice("incubate", now=started)
        garden.run_tick(started + timedelta(hours=21), rng=Mock(choice=lambda items: items[0]))
        chick = garden.coop_snapshot(now=started + timedelta(hours=21))["chicks"][0]

        with patch("home.random.choice", side_effect=lambda lines: lines[0]):
            named = home.handle_garden(self._request(["院子", "鸡取名", chick["id"], "豆包"]))
            fed = home.handle_garden(self._request(["院子", "给豆包喂食"]))
            petted = home.handle_garden(self._request(["院子", "摸摸豆包"]))
        self.assertIn("取名成功", named)
        self.assertIn("喂食成功", fed)
        self.assertIn("摸摸成功", petted)
        updated = next(x for x in garden.coop_snapshot()["chicks"] if x["id"] == chick["id"])
        self.assertEqual((updated["nickname"], updated["feed_count"], updated["pet_count"]), ("豆包", 1, 1))
        coop_view = home.handle_garden(self._request(["院子", "查看鸡舍"]))
        self.assertIn("成员：", coop_view)
        self.assertIn("豆包", coop_view)
        self.assertIn("性格：", coop_view)
        self.assertIn(updated["chick_appearance"], coop_view)

    def test_natural_batch_chicken_rename_before_any_nickname_exists(self):
        # 8/3 真实事故：三只鸡都还没取名（只有短编号），一条自然语句里
        # 连续给三只鸡取名（"9e3b叫霜 077e叫桂圆 6253叫芝麻"）此前完全解析不出来，
        # 落到"没听懂要做什么"；旧的单只鸡分支还依赖 `_garden_chicken_mention`
        # 按昵称/完整 id 子串匹配，孵出还没取名的鸡拿短编号前缀根本对不上。
        self._mute_calendar_moments()
        now = datetime.now(TZ)
        garden.spawn(
            "animal", species="荷兰侏儒兔", intro="x", category="兔子",
            personality="活泼", now=now,
        )
        garden.run_tick(now + timedelta(hours=1), rng=Mock(choice=lambda items: items[0]))
        started = now + timedelta(hours=1, minutes=1)
        garden.resolve_coop_egg_choice("incubate", now=started)
        garden.run_tick(started + timedelta(hours=21), rng=Mock(choice=lambda items: items[0]))
        chicks = garden.coop_snapshot(now=started + timedelta(hours=21))["chicks"]
        self.assertEqual(len(chicks), 3)
        prefixes = [chick["id"][:4] for chick in chicks]
        names = ["霜", "桂圆", "芝麻"]
        phrase = " ".join(f"{prefix}叫{name}" for prefix, name in zip(prefixes, names))

        with patch("home.random.choice", side_effect=lambda lines: lines[0]):
            result = home.handle_garden(self._request(["院子", "给小鸡取名", *phrase.split()]))
        self.assertEqual(result.count("取名成功"), 3)
        updated = {chick["id"]: chick["nickname"] for chick in garden.coop_snapshot()["chicks"]}
        for prefix, name in zip(prefixes, names):
            matching = next(nickname for chick_id, nickname in updated.items() if chick_id.startswith(prefix))
            self.assertEqual(matching, name)

    def test_crop_commands_show_plots_and_dry_run_never_initializes_state(self):
        crop_id, crop_name = self._seasonal_crop()
        dry_path = Path(self.tempdir.name) / "dry-garden.json"
        with patch.object(garden, "GARDEN_FILE", dry_path):
            output = home.handle_garden(self._request(["院子", "--dry-run", "播种", crop_name, "1"]))
        self.assertIn("将把", output)
        self.assertFalse(dry_path.exists())

        # 用当前真实季节能种的作物初始化种子盒，再走和 CLI 相同的命令解析。
        garden.crop_snapshot()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["seeds"][crop_id] = raw["inventory"]["seeds"].get(crop_id, 0) + 1
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        planted = home.handle_garden(self._request(["院子", "播种", crop_name, "1"]))
        self.assertIn("播下", planted)
        watered = home.handle_garden(self._request(["院子", "浇水", "1"]))
        self.assertIn(crop_name, watered)
        self.assertIn(crop_name, home.handle_garden(self._request(["院子", "篮子"])))

    def test_visible_plot_labels_are_valid_planting_selectors(self):
        crop_id, crop_name = self._seasonal_crop()
        garden.crop_snapshot()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["seeds"][crop_id] = 4
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        for selector, label in (
            ("一号菜畦", "一号菜畦"),
            ("二号地", "二号菜畦"),
            ("三号地块", "三号菜畦"),
            ("p4", "四号菜畦"),
        ):
            with self.subTest(selector=selector):
                output = home.handle_garden(
                    self._request(["院子", "播种", crop_name, selector])
                )
                self.assertIn(label, output)

    def test_listing_marks_a_crop_already_watered_today(self):
        crop_id, crop_name = self._seasonal_crop()
        garden.crop_snapshot()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["seeds"][crop_id] = raw["inventory"]["seeds"].get(crop_id, 0) + 1
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        home.handle_garden(self._request(["院子", "播种", crop_name, "一号菜畦"]))
        # 播种当天剩余时间会按比例先记一笔初始生长加成（见 garden.plant_crop），
        # 越早的真实运行时刻剩的时间越多、加成越大，偶尔会让作物一种下就直接
        # 越过发芽门槛，导致本用例断言的"刚播下"跟测试运行的真实时刻绑定、
        # 时灵时不灵（8/13 发现的老毛病）。这里直接把生长点数清零，只固定
        # "种下当天就是种子阶段"这一个事实，不引入冻结真实时钟的新依赖。
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        next(p for p in raw["plots"] if p["plot_id"] == "p1")["growth_points"] = 0.0
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        home.handle_garden(self._request(["院子", "浇水", "一号菜畦"]))
        output = home.handle_garden(self._request(["院子", "查看", "详细"]))
        self.assertIn(f"{crop_name} · 刚播下 · 今日已浇水，土还湿着", output)
        compact = home.handle_garden(self._request(["院子", "查看"]))
        self.assertIn(f"1号{crop_name}刚播下", compact)
        self.assertNotIn("基础成熟期", compact)

    def test_single_plot_view_is_short_and_reports_other_actionable_plots(self):
        now = datetime.now(TZ)
        crop_id, crop_name = self._seasonal_crop()
        # 隔离真实天气缓存：这个用例只是要打开现实环境总开关来构造带 soil
        # 字段的地块，不关心天气事实本身。不隔离会读到本机真实
        # weather_cache.json——一旦真实院子当下恰好内涝，第七版的播种
        # 禁播规则会让下面的 plant_crop 意外报错，跟这条用例的意图无关。
        with patch.dict("os.environ", {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}), \
                patch.object(garden, "_environment_observations", return_value=[]):
            garden.crop_snapshot(now=now)
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            raw["inventory"]["seeds"][crop_id] = 4
            self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            for selector in ("1", "2", "3"):
                garden.plant_crop(crop_name, selector, now=now)
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            raw["plots"][0]["soil"]["moisture"] = 20.0
            raw["plots"][0]["soil"]["anchor_moisture"] = 20.0
            raw["plots"][0]["soil"]["anchor_at"] = raw["plots"][0]["soil"]["settled_at"]
            raw["plots"][1]["status"] = "ready"
            raw["plots"][1]["stage"] = "ready"
            raw["plots"][1]["growth_points"] = 4.0
            raw["plots"][1]["ready_at"] = now.isoformat()
            self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            output = home.handle_garden(self._request(["院子", "查看", "3"]))
        self.assertIn("三号菜畦", output)
        self.assertIn(f"提示：1号地缺水，2号地的{crop_name}已成熟，可以收获。", output)
        self.assertNotIn("一号菜畦", output)
        self.assertNotIn("小院子现在有", output)

    def test_single_plot_view_rejects_unknown_selector(self):
        with self.assertRaisesRegex(home.HomeError, "地块编号不明确"):
            home.handle_garden(self._request(["院子", "查看", "9"]))

    def test_natural_multi_plot_view_shows_only_the_requested_plots(self):
        # 8/4 真实事故：`查看 2 4`这类不带"号/地"后缀的裸数字批量说法，
        # 之前会被语义兜底悄悄吞成不带参数的整院查看，不报错但也没按
        # 说的批量看两块地。
        now = datetime.now(TZ)
        crop_id, crop_name = self._seasonal_crop()
        garden.crop_snapshot(now=now)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["seeds"][crop_id] = 4
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        garden.plant_crop(crop_name, "2", now=now)
        garden.plant_crop(crop_name, "4", now=now)
        for phrase in ("查看 2 4", "查看2和4", "查看二号地和四号地"):
            output = home.handle_garden(self._request(["院子", phrase]))
            self.assertIn("二号菜畦", output)
            self.assertIn("四号菜畦", output)
            self.assertNotIn("一号菜畦", output)
            self.assertNotIn("小院子现在有", output)

    def test_natural_batch_watering_splits_on_and_connective(self):
        # 8/4 真实事故：`浇水一号地和三号地`里的"和"不算分隔符，
        # 两块地名被当成一个不存在的选择器，报错"现在没有能浇水的...菜畦"。
        now = datetime.now(TZ)
        crop_id, crop_name = self._seasonal_crop()
        garden.crop_snapshot(now=now)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["seeds"][crop_id] = 4
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        garden.plant_crop(crop_name, "1", now=now)
        garden.plant_crop(crop_name, "3", now=now)
        result = home.handle_garden(self._request(["院子", "浇水一号地和三号地"]))
        self.assertIn("一号地", result)
        self.assertIn("三号地", result)
        watered = {p["plot_id"]: p.get("watering_by_date", {}) for p in garden.crop_snapshot()["plots"]}
        today = now.astimezone(TZ).date().isoformat()
        self.assertEqual(watered["p1"].get(today), 1)
        self.assertEqual(watered["p3"].get(today), 1)

    def test_unknown_seed_lists_current_available_choices_without_deduction(self):
        before = garden.crop_snapshot()["inventory"]["seeds"]
        with self.assertRaisesRegex(home.HomeError, "现在可选") as caught:
            home.handle_garden(self._request(["院子", "播种", "不存在的种子", "1"]))
        after = garden.crop_snapshot()["inventory"]["seeds"]
        self.assertEqual(after, before)
        message = str(caught.exception)
        for crop_id, amount in before.items():
            if amount > 0:
                self.assertIn(garden.garden_crops.crop_name(crop_id), message)

    def test_summer_crop_success_copy_uses_local_pool_without_runtime_writer(self):
        treatment = {
            "plot_id": "p1", "crop_id": "cucumber", "action": "除虫",
            "outcome": "resolved", "condition_type": "pest", "yield_penalty": 0,
        }
        water = {
            "plot": {"plot_id": "p1", "crop_id": "pepper"},
            "outcome": "protest", "style": "physical",
        }
        with patch("home.random.choice", side_effect=lambda lines: lines[0]), patch(
            "garden.crop_treatment_copy",
        ) as treatment_writer, patch("garden.watering_copy") as water_writer:
            treatment_text = home._garden_crop_treatment_text(treatment, use_writer=True)
            water_text = home._garden_water_crop_text(water, use_writer=True)
        self.assertIn("黄瓜", treatment_text)
        self.assertIn("虫", treatment_text)
        self.assertIn("辣椒", water_text)
        self.assertIn("没有", water_text)
        treatment_writer.assert_not_called()
        water_writer.assert_not_called()

    def test_old_crop_success_copy_keeps_runtime_writer_path(self):
        treatment = {
            "plot_id": "p1", "crop_id": "tomato", "action": "除虫",
            "outcome": "resolved", "condition_type": "pest", "yield_penalty": 0,
        }
        with patch("garden.crop_treatment_copy", return_value="旧作物写手句。") as writer:
            output = home._garden_crop_treatment_text(treatment, use_writer=True)
        self.assertIn("旧作物写手句。", output)
        writer.assert_called_once()

    def test_water_requires_explicit_target_when_legacy_flower_and_plot_coexist(self):
        now = datetime.now(TZ)
        crop_id, crop_name = self._seasonal_crop()
        garden.crop_snapshot(now=now)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["seeds"][crop_id] = raw["inventory"]["seeds"].get(crop_id, 0) + 1
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        flower = garden.spawn("flower", species="蒲公英", intro="x", now=now)
        flower["id"] = "1flower"
        garden.save_garden([flower])
        garden.plant_crop(crop_name, "1", now=now)
        with self.assertRaisesRegex(home.HomeError, "不止一个"):
            home.handle_garden(self._request(["院子", "浇水"]))
        with self.assertRaisesRegex(home.HomeError, "不止一个"):
            home.handle_garden(self._request(["院子", "浇水", "1"]))
        self.assertIn(crop_name, home.handle_garden(self._request(["院子", "浇水", "p1"])))

    def test_coop_clean_result_line_reports_fertilizer_yield(self):
        """第九版：打扫鸡舍结果行报"约三天沤出肥料×N"（六份鸡粪沤三天出
        一份肥料），不是旧版"两天发酵好"的口径。"""
        now = datetime.now(TZ)
        garden.crop_snapshot(now=now)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["coop"].update({
            "built": True,
            "built_at": now.isoformat(),
            "story_status": "incubating",
            "clutch_id": "clutch-test",
            "incubation_started_at": now.isoformat(),
            "hatch_at": (now + garden.COOP_INCUBATION_DURATION).isoformat(),
            "incubating_egg_count": garden.COOP_EGG_COUNT,
            "manure": {"units": 6, "last_settled_at": now.isoformat()},
        })
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        with patch("home.random.choice", side_effect=lambda lines: lines[0]):
            output = home.handle_garden(self._request(["院子", "打扫鸡舍"]))
        self.assertIn("约三天沤出肥料×1", output)

    def test_fertilize_growth_boost_result_line_reports_progress_and_estimate(self):
        """第九版：追肥结果行报"已追肥 n/5 次，预计…熟"（非催熟分支）。"""
        now = datetime.now(TZ)
        crop_id, crop_name = self._seasonal_crop()
        garden.crop_snapshot(now=now)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["seeds"][crop_id] = raw["inventory"]["seeds"].get(crop_id, 0) + 1
        raw["inventory"]["fertilizer"] = {"fertilizer": 1}
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        garden.plant_crop(crop_name, "1", now=now)

        with patch("home.random.choice", side_effect=lambda lines: lines[0]):
            output = home.handle_garden(self._request(["院子", "施肥", "1"]))
        self.assertIn(f"{crop_name}这茬已追肥1/5次，预计", output)
        self.assertIn("熟。", output)

    def test_fertilize_growth_boost_ripened_branch_reports_ready_to_harvest(self):
        """第九版：剩余生长时间很小时追肥能直接催熟，结果行改成"这一下
        直接催熟了，可以收获"，不报次数/预计时刻。"""
        now = datetime.now(TZ)
        crop_id, crop_name = self._seasonal_crop()
        growth_days = garden.garden_crops.CROPS[crop_id]["growth_days"]
        garden.crop_snapshot(now=now)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["seeds"][crop_id] = raw["inventory"]["seeds"].get(crop_id, 0) + 1
        raw["inventory"]["fertilizer"] = {"fertilizer": 1}
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        garden.plant_crop(crop_name, "1", now=now)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        plot = next(p for p in raw["plots"] if p["plot_id"] == "p1")
        # 剩最后一丁点生长点数，施肥的一成加成足够把它推过成熟线（见
        # garden.py 追肥分支：bonus = (target-growth_points)*0.10）。
        # last_settled_at 顺手拨到未来一小时：handle_garden 内部用真实
        # datetime.now() 结算，不这样拨的话，从写盘到真正调用施肥之间
        # 流逝的哪怕几毫秒真实时间，也可能先把这点残余生长量自然结算掉，
        # 让"催熟"变成"结算已经熟了"，不是"这次施肥催熟的"。
        plot["growth_points"] = growth_days - 0.00001
        plot["last_settled_at"] = (now + timedelta(hours=1)).isoformat()
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        with patch("home.random.choice", side_effect=lambda lines: lines[0]):
            output = home.handle_garden(self._request(["院子", "施肥", "1"]))
        self.assertIn(f"{crop_name}这一下直接催熟了，可以收获。", output)


if __name__ == "__main__":
    unittest.main()
