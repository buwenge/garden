"""阶段 D：动物对现实天气的行为反应。

命名特意避开历史文件 ``test_garden_stage_d.py``——那是 v4 之前"阶段 D"
作物照顾动作的测试，与本文件的第五版动物天气反应无关。
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden
import garden_content
import garden_scene
import garden_weather
import home
import log_store
from garden_generator import GardenGeneratorError


TZ = ZoneInfo("Asia/Shanghai")

# 复审指出本文件曾在 stroll_scene 兜底路径上意外写过一次生产 logs.jsonl
# （garden_generator.generate_stroll 失败时 stroll_scene 会调用
# log_store.write_log）。整份文件统一在模块级把 LOG_FILE 隔离到临时目录，
# 不依赖每个测试类自己记得 patch，避免同类遗漏再次污染真实日志。
_log_dir = None
_log_patcher = None


def setUpModule():
    global _log_dir, _log_patcher
    _log_dir = tempfile.mkdtemp(prefix="garden_stage_d_animal_weather_logs_")
    _log_patcher = patch.object(log_store, "LOG_FILE", Path(_log_dir) / "logs.jsonl")
    _log_patcher.start()


def tearDownModule():
    _log_patcher.stop()
    shutil.rmtree(_log_dir, ignore_errors=True)


class _StubRng:
    """同款确定性替身：choice 按下标取值、单元素池不受下标影响，不弹出。"""

    def __init__(self, randoms=(), *, choice_index=0):
        self.randoms = list(randoms)
        self.choice_index = choice_index

    def random(self):
        return self.randoms.pop(0)

    def choice(self, values):
        values = list(values)
        return values[0] if len(values) == 1 else values[self.choice_index]


def _observation(at, *, temp=20, feels=20, humidity=50, wind="1", precip=0, text="晴"):
    value = garden_weather.normalize_observation(
        location_id="101190205", location_name="南京", observed_time=at.isoformat(),
        received_at=at, temp=temp, feels_like=feels, humidity=humidity,
        wind_scale=wind, precip=precip, condition_text=text,
    )
    assert value is not None
    return value


class AnimalWeatherModePureFunctionTests(unittest.TestCase):
    """``garden_weather.animal_weather_mode`` / ``ground_recently_rained``：
    纯函数、白名单枚举、缺失信号一律 normal。"""

    def test_storm_or_rain_takes_priority_over_everything_else(self):
        self.assertEqual(
            garden_weather.animal_weather_mode(frozenset({"storm", "hot", "windy"}), day_period="day"),
            "sheltering",
        )
        self.assertEqual(
            garden_weather.animal_weather_mode(frozenset({"rain", "cold", "clear"}), day_period="day"),
            "sheltering",
        )

    def test_hot_maps_to_cooling_when_not_raining_or_storming(self):
        self.assertEqual(
            garden_weather.animal_weather_mode(frozenset({"hot", "windy"}), day_period="day"),
            "cooling",
        )

    def test_cold_and_clear_only_basks_in_daylight(self):
        self.assertEqual(
            garden_weather.animal_weather_mode(frozenset({"cold", "clear"}), day_period="day"),
            "basking",
        )
        self.assertEqual(
            garden_weather.animal_weather_mode(frozenset({"cold", "clear"}), day_period="night"),
            "normal",
        )

    def test_cold_without_clear_does_not_bask(self):
        self.assertEqual(
            garden_weather.animal_weather_mode(frozenset({"cold", "cloudy"}), day_period="day"),
            "normal",
        )

    def test_ground_wet_maps_to_muddy_when_nothing_more_urgent(self):
        self.assertEqual(
            garden_weather.animal_weather_mode(frozenset(), day_period="day", ground_wet=True),
            "muddy",
        )

    def test_ground_wet_loses_to_active_rain_or_heat(self):
        self.assertEqual(
            garden_weather.animal_weather_mode(frozenset({"rain"}), day_period="day", ground_wet=True),
            "sheltering",
        )
        self.assertEqual(
            garden_weather.animal_weather_mode(frozenset({"hot"}), day_period="day", ground_wet=True),
            "cooling",
        )

    def test_windy_alone_is_mild_wind_play_not_sheltering(self):
        self.assertEqual(
            garden_weather.animal_weather_mode(frozenset({"windy"}), day_period="day"),
            "wind_play",
        )

    def test_mild_wind_scale_below_danger_threshold_still_plays(self):
        below = garden_weather.DANGEROUS_WIND_SCALE - 1
        self.assertEqual(
            garden_weather.animal_weather_mode(
                frozenset({"windy"}), day_period="day", wind_scale=below,
            ),
            "wind_play",
        )

    def test_dangerous_wind_scale_shelters_even_without_storm_or_rain_tag(self):
        self.assertEqual(
            garden_weather.animal_weather_mode(
                frozenset({"windy"}), day_period="day",
                wind_scale=garden_weather.DANGEROUS_WIND_SCALE,
            ),
            "sheltering",
        )

    def test_missing_wind_scale_never_forces_shelter(self):
        self.assertEqual(
            garden_weather.animal_weather_mode(frozenset({"windy"}), day_period="day", wind_scale=None),
            "wind_play",
        )

    def test_dangerous_wind_beats_hot_and_ground_wet(self):
        self.assertEqual(
            garden_weather.animal_weather_mode(
                frozenset({"hot", "windy"}), day_period="day",
                wind_scale=garden_weather.DANGEROUS_WIND_SCALE, ground_wet=True,
            ),
            "sheltering",
        )

    def test_no_signal_at_all_is_normal(self):
        self.assertEqual(garden_weather.animal_weather_mode(frozenset(), day_period="day"), "normal")
        self.assertEqual(garden_weather.animal_weather_mode(None, day_period="night"), "normal")

    def test_every_return_value_is_in_the_documented_whitelist(self):
        day_periods = ("dawn", "day", "dusk", "night", "late_night")
        tag_combos = (
            frozenset(), frozenset({"storm"}), frozenset({"rain"}), frozenset({"hot"}),
            frozenset({"cold", "clear"}), frozenset({"windy"}), frozenset({"cloudy"}),
        )
        for period in day_periods:
            for tags in tag_combos:
                for ground_wet in (True, False):
                    mode = garden_weather.animal_weather_mode(tags, day_period=period, ground_wet=ground_wet)
                    self.assertIn(mode, garden_weather.ANIMAL_WEATHER_MODES)

    def test_ground_recently_rained_true_after_a_newer_dry_observation_confirms_it_stopped(self):
        now = datetime(2026, 7, 29, 14, 0, tzinfo=TZ)
        rained_then_cleared = [
            _observation(now - timedelta(hours=1), precip=3, text="小雨"),
            _observation(now - timedelta(minutes=5), precip=0, text="晴"),
        ]
        self.assertTrue(garden_weather.ground_recently_rained(rained_then_cleared, now))

    def test_ground_recently_rained_false_while_still_raining(self):
        now = datetime(2026, 7, 29, 14, 0, tzinfo=TZ)
        still_raining = [_observation(now - timedelta(minutes=10), precip=3, text="小雨")]
        self.assertFalse(garden_weather.ground_recently_rained(still_raining, now))

    def test_ground_recently_rained_false_once_outside_the_window(self):
        now = datetime(2026, 7, 29, 14, 0, tzinfo=TZ)
        long_ago = [_observation(now - timedelta(hours=4), precip=3, text="小雨")]
        self.assertFalse(garden_weather.ground_recently_rained(long_ago, now))

    def test_ground_recently_rained_false_without_timezone(self):
        naive_now = datetime(2026, 7, 29, 14, 0)
        self.assertFalse(garden_weather.ground_recently_rained([], naive_now))


class AnimalWeatherContentSkeletonTests(unittest.TestCase):
    """本地基础池骨架：每个物种 × 模式都必须有安全候选，缺格必须在
    模块导入时就被拦下，不能留到运行时才发现空池。"""

    def test_every_category_and_mode_has_at_least_one_candidate(self):
        modes = ("sheltering", "cooling", "basking", "muddy", "wind_play")
        for category in garden_content.ANIMAL_CATEGORIES:
            for mode in modes:
                with self.subTest(category=category, mode=mode):
                    pool = garden_content.ANIMAL_WEATHER_LINES.get(category, {}).get(mode)
                    self.assertTrue(pool, f"{category}×{mode} 缺少候选")

    def test_hedgehog_basking_stays_subdued_not_actively_sunbathing(self):
        for line in garden_content.ANIMAL_WEATHER_LINES["刺猬"]["basking"]:
            self.assertNotIn("烈日", line)
            # 白天仍尊重原有低可见倾向：每一句都要带克制/未完全出来的措辞。
            self.assertTrue(any(marker in line for marker in ("没有", "只是", "并没有")))

    def test_missing_cell_is_rejected_by_validator(self):
        broken = {
            category: dict(modes) for category, modes in garden_content.ANIMAL_WEATHER_LINES.items()
        }
        broken["猫"] = dict(broken["猫"])
        broken["猫"]["cooling"] = ()
        with patch.object(garden_content, "ANIMAL_WEATHER_LINES", broken):
            with self.assertRaises(ValueError):
                garden_content._validate_animal_weather_lines()

    def test_no_candidate_in_either_pool_names_a_concrete_spot(self):
        # 骨架小句会接在已有的、可能带着真实 spot 的句子后面（或替代它），
        # 点名具体地点会跟真实位置打架（"院门旁……缩到屋檐下/水碗边"）。
        # "水碗"不在 SPOTS 常量里，但复审已经实测复现过同类冲突，一并禁止。
        location_words = ("院门", "信箱", "屋檐", "墙根", "墙角", "门口", "水碗")
        for pool_name, pool in (
            ("ANIMAL_WEATHER_LINES", garden_content.ANIMAL_WEATHER_LINES),
            ("ANIMAL_WEATHER_PLAY_LINES", garden_content.ANIMAL_WEATHER_PLAY_LINES),
        ):
            for category, modes in pool.items():
                if category == "生姜":
                    continue
                for mode, lines in modes.items():
                    for line in lines:
                        with self.subTest(pool=pool_name, category=category, mode=mode):
                            self.assertFalse(
                                any(word in line for word in location_words),
                                f"{pool_name}.{category}×{mode} 出现具体地点：{line}",
                            )

    def test_no_sheltering_candidate_in_either_pool_implies_rain_or_thunder(self):
        # sheltering 可能由雨、雷暴或纯粹的危险强风任一原因触发；晴天强风
        # 场景下如果候选说"舔被打湿的爪子"就是事实错误，因此这一档的候选
        # 一律不能出现只在下雨/打雷时才成立的词。
        cause_words = ("雨", "雷", "水", "湿", "潮", "淋")
        for pool_name, pool in (
            ("ANIMAL_WEATHER_LINES", garden_content.ANIMAL_WEATHER_LINES),
            ("ANIMAL_WEATHER_PLAY_LINES", garden_content.ANIMAL_WEATHER_PLAY_LINES),
        ):
            for category in garden_content.ANIMAL_CATEGORIES:
                for line in pool[category]["sheltering"]:
                    with self.subTest(pool=pool_name, category=category):
                        self.assertFalse(
                            any(word in line for word in cause_words),
                            f"{pool_name}.{category}×sheltering 暗示了雨/雷/水才成立的原因：{line}",
                        )

    def test_missing_sheltering_cause_word_is_rejected_by_validator(self):
        broken = {
            category: dict(modes) for category, modes in garden_content.ANIMAL_WEATHER_LINES.items()
        }
        broken["猫"] = dict(broken["猫"])
        broken["猫"]["sheltering"] = ("缩起身体，尾巴扫过脚边，时不时舔一下被打湿的爪子。",)
        with patch.object(garden_content, "ANIMAL_WEATHER_LINES", broken):
            with self.assertRaises(ValueError):
                garden_content._validate_animal_weather_lines()

    def test_play_pool_also_has_complete_skeleton_and_location_free_content(self):
        modes = ("sheltering", "cooling", "basking", "muddy", "wind_play")
        for category in garden_content.ANIMAL_CATEGORIES:
            for mode in modes:
                with self.subTest(category=category, mode=mode):
                    pool = garden_content.ANIMAL_WEATHER_PLAY_LINES.get(category, {}).get(mode)
                    self.assertTrue(pool, f"{category}×{mode} 缺少陪玩专用候选")

    def test_missing_play_cell_is_also_rejected_by_validator(self):
        broken = {
            category: dict(modes) for category, modes in garden_content.ANIMAL_WEATHER_PLAY_LINES.items()
        }
        broken["狗"] = dict(broken["狗"])
        broken["狗"]["muddy"] = ()
        with patch.object(garden_content, "ANIMAL_WEATHER_PLAY_LINES", broken):
            with self.assertRaises(ValueError):
                garden_content._validate_animal_weather_lines()


class AnimalWeatherModeGateTests(unittest.TestCase):
    """总开关关闭时必须完整退回第四版行为：即使观测里真的有雨，也不派生
    任何模式,不给动物编造天气反应。"""

    def setUp(self):
        self.now = datetime(2026, 7, 29, 14, 0, tzinfo=TZ)
        self.rain = [_observation(self.now - timedelta(minutes=5), precip=5, text="大雨")]

    def test_mode_is_normal_when_switch_is_off_even_with_fresh_rain(self):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "0"}):
            self.assertEqual(garden._animal_weather_mode(self.rain, self.now), "normal")

    def test_mode_reflects_fresh_rain_when_switch_is_on(self):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            self.assertEqual(garden._animal_weather_mode(self.rain, self.now), "sheltering")

    def test_flavor_helper_returns_none_for_normal_mode(self):
        self.assertIsNone(garden._animal_weather_flavor("猫", "normal", _StubRng()))

    def test_flavor_helper_returns_none_for_unknown_category(self):
        self.assertIsNone(garden._animal_weather_flavor("恐龙", "cooling", _StubRng()))

    def test_mode_extracts_wind_scale_from_latest_observation(self):
        gale = [_observation(
            self.now - timedelta(minutes=5), wind=str(garden_weather.DANGEROUS_WIND_SCALE), text="晴",
        )]
        breeze = [_observation(self.now - timedelta(minutes=5), wind="3", text="晴")]
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            self.assertEqual(garden._animal_weather_mode(gale, self.now), "sheltering")
            self.assertEqual(garden._animal_weather_mode(breeze, self.now), "wind_play")

    def test_flavor_helper_routes_play_action_to_dedicated_pool(self):
        rng = _StubRng(choice_index=0)
        flavor = garden._animal_weather_flavor("猫", "cooling", rng, action="陪玩")
        self.assertIn(flavor, garden_content.ANIMAL_WEATHER_PLAY_LINES["猫"]["cooling"])
        self.assertNotIn(flavor, garden_content.ANIMAL_WEATHER_LINES["猫"]["cooling"])

    def test_clear_sky_dangerous_wind_never_produces_wet_or_thunder_wording(self):
        # 端到端复现复审报告的场景：晴天、无雨无雷，只有危险强风，模式会
        # 判成 sheltering；不管从哪个池子、哪个物种、走陪玩还是其它动作，
        # 实际选出来的文案都不能出现雨/雷/水/湿/潮这类词。
        clear_gale = [_observation(
            self.now - timedelta(minutes=5), wind=str(garden_weather.DANGEROUS_WIND_SCALE),
            precip=0, text="晴",
        )]
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            mode = garden._animal_weather_mode(clear_gale, self.now)
        self.assertEqual(mode, "sheltering")
        cause_words = ("雨", "雷", "水", "湿", "潮", "淋")
        for category in garden_content.ANIMAL_CATEGORIES:
            for action in (None, "陪玩"):
                with self.subTest(category=category, action=action):
                    flavor = garden._animal_weather_flavor(
                        category, mode, _StubRng(choice_index=0), action=action,
                    )
                    self.assertIsNotNone(flavor)
                    self.assertFalse(any(word in flavor for word in cause_words))


class DescribeCareWeatherFlavorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 29, 14, 0, tzinfo=TZ)

    def _entry(self, **overrides):
        entry = {
            "kind": "animal", "species": "橘猫", "category": "猫", "personality": "活泼",
            "trait": "", "nickname": "",
        }
        entry.update(overrides)
        return entry

    def test_care_reaction_appends_weather_flavor_when_mode_is_not_normal(self):
        entry = self._entry()
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")):
            text = garden.describe_care(entry, "摸摸", revived=False, weather_mode="cooling")
        pool = garden_content.ANIMAL_WEATHER_LINES["猫"]["cooling"]
        self.assertTrue(any(text.endswith(line) for line in pool))

    def test_care_reaction_unchanged_when_mode_is_normal(self):
        entry = self._entry()
        sample_pool = ("猫活泼摸摸样例反应",)
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")), \
             patch.dict(garden.garden_content.ANIMAL_REACTION_BY_PERSONALITY["猫"]["活泼"], {"摸摸": sample_pool}):
            text = garden.describe_care(entry, "摸摸", revived=False)
        self.assertEqual(text, "猫活泼摸摸样例反应")

    def test_flower_kind_never_gets_animal_weather_flavor(self):
        entry = {"kind": "flower", "species": "蒲公英", "trait": "", "nickname": ""}
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")):
            text = garden.describe_care(entry, "浇水", revived=False, weather_mode="cooling")
        self.assertIn(text, garden._FALLBACK_REACTION["浇水"])

    def test_play_reaction_uses_play_specific_pool_not_the_generic_one(self):
        # 陪玩本身就在描述持续跑动，必须走承接"刚才还在动"的专用池，
        # 不能接通用池那种独立静态姿态描述，否则会读成动作前后矛盾。
        entry = self._entry()
        text = garden.describe_care(entry, "陪玩", revived=False, weather_mode="wind_play")
        play_pool = garden_content.ANIMAL_WEATHER_PLAY_LINES["猫"]["wind_play"]
        generic_pool = garden_content.ANIMAL_WEATHER_LINES["猫"]["wind_play"]
        self.assertTrue(any(text.endswith(line) for line in play_pool))
        self.assertFalse(any(text.endswith(line) for line in generic_pool))

    def test_hot_play_does_not_contradict_just_having_run_around(self):
        # 补高温陪玩对抗用例：基础陪玩反应池里带着"追/跑"这类持续跑动的
        # 描述，天气小句不能直接说"完全不想动"这种孤立静态状态，必须是
        # 承接式的转折句（写在 ANIMAL_WEATHER_PLAY_LINES 里）。
        entry = self._entry()
        base_pool = garden_content.ANIMAL_PLAY_REACTION_BY_PERSONALITY["猫"]["活泼"]
        self.assertTrue(any(word in "".join(base_pool) for word in ("追", "跑")))
        text = garden.describe_care(entry, "陪玩", revived=False, weather_mode="cooling")
        flavor_pool = garden_content.ANIMAL_WEATHER_PLAY_LINES["猫"]["cooling"]
        self.assertTrue(any(text.endswith(line) for line in flavor_pool))
        for line in flavor_pool:
            self.assertTrue(any(marker in line for marker in ("追", "跑", "玩")))

    def test_care_result_carries_derived_weather_mode(self):
        environment = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        rain = [_observation(self.now - timedelta(minutes=5), precip=5, text="大雨")]
        loader = patch.object(garden, "_environment_observations", side_effect=lambda _now: list(rain))
        loader.start()
        self.addCleanup(loader.stop)
        garden.spawn(
            "animal", species="橘猫", intro="来了一只猫。", category="猫", personality="活泼",
            now=self.now, path=self.path,
        )
        entries = garden.active_entries(self.path)
        result = garden.care(entries[0]["id"], "摸摸", now=self.now, path=self.path)
        self.assertEqual(result["weather_mode"], "sheltering")

    def test_care_result_mode_is_normal_when_switch_off(self):
        garden.spawn(
            "animal", species="橘猫", intro="来了一只猫。", category="猫", personality="活泼",
            now=self.now, path=self.path,
        )
        entries = garden.active_entries(self.path)
        result = garden.care(entries[0]["id"], "摸摸", now=self.now, path=self.path)
        self.assertEqual(result["weather_mode"], "normal")


class DescribeSpawnWeatherFlavorTests(unittest.TestCase):
    def test_fallback_encounter_appends_flavor_for_animal_kind(self):
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        with patch("garden.garden_generator.generate_encounter", side_effect=GardenGeneratorError("下线了")):
            species, text = garden._describe_spawn(
                "animal", _StubRng(choice_index=0),
                category="猫", personality="活泼", context=context,
                weather_tags=frozenset(), animal_weather_mode="cooling",
            )
        self.assertIn(species, garden_content.ANIMAL_SPECIES_BY_CATEGORY["猫"])
        pool = garden_content.ANIMAL_WEATHER_LINES["猫"]["cooling"]
        self.assertTrue(any(text.endswith(line) for line in pool))

    def test_fallback_encounter_unchanged_when_mode_is_normal(self):
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        with patch("garden.garden_generator.generate_encounter", side_effect=GardenGeneratorError("下线了")):
            _, text_with_default = garden._describe_spawn(
                "animal", _StubRng(choice_index=0),
                category="猫", personality="活泼", context=context,
                weather_tags=frozenset(),
            )
            _, text_with_normal = garden._describe_spawn(
                "animal", _StubRng(choice_index=0),
                category="猫", personality="活泼", context=context,
                weather_tags=frozenset(), animal_weather_mode="normal",
            )
        self.assertEqual(text_with_default, text_with_normal)

    def test_flower_kind_is_never_affected_by_animal_weather_mode(self):
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        with patch("garden.garden_generator.generate_encounter", side_effect=GardenGeneratorError("下线了")):
            _, text_normal = garden._describe_spawn(
                "flower", _StubRng(choice_index=0), context=context, weather_tags=frozenset(),
            )
            _, text_cooling = garden._describe_spawn(
                "flower", _StubRng(choice_index=0), context=context, weather_tags=frozenset(),
                animal_weather_mode="cooling",
            )
        self.assertEqual(text_normal, text_cooling)


class DescribeRevisitWeatherFlavorTests(unittest.TestCase):
    def _entry(self, **overrides):
        entry = {
            "kind": "animal", "species": "柴犬串串", "category": "狗", "personality": "活泼",
            "nickname": "", "spot": "院门与信箱旁", "bond_points": 0, "last_action": "摸摸",
        }
        entry.update(overrides)
        return entry

    def test_generic_template_path_appends_flavor(self):
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        entry = self._entry()
        text = garden._describe_revisit(entry, "fresh", _StubRng(choice_index=0), context, frozenset(), "basking")
        pool = garden_content.ANIMAL_WEATHER_LINES["狗"]["basking"]
        self.assertTrue(any(text.endswith(line) for line in pool))

    def test_bond_level_pool_path_also_appends_flavor(self):
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        entry = self._entry(bond_points=100)  # 推到最高亲密等级，命中 ANIMAL_BOND_REVISIT_BY_LEVEL
        text = garden._describe_revisit(entry, "fresh", _StubRng(choice_index=0), context, frozenset(), "muddy")
        pool = garden_content.ANIMAL_WEATHER_LINES["狗"]["muddy"]
        self.assertTrue(any(text.endswith(line) for line in pool))

    def test_no_flavor_when_mode_is_normal(self):
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        entry = self._entry()
        with_default = garden._describe_revisit(entry, "fresh", _StubRng(choice_index=0), context, frozenset())
        with_normal = garden._describe_revisit(
            entry, "fresh", _StubRng(choice_index=0), context, frozenset(), "normal",
        )
        self.assertEqual(with_default, with_normal)

    def test_rain_revisit_does_not_contradict_the_entrys_real_spot(self):
        # 雨中回访对抗用例：entry.spot 是"院门与信箱旁"，sheltering 的旧措辞
        # 会说"缩到屋檐下"，跟已经说出的真实位置直接矛盾。
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        entry = self._entry(spot="院门与信箱旁")
        text = garden._describe_revisit(entry, "fresh", _StubRng(choice_index=0), context, frozenset(), "sheltering")
        self.assertIn("院门与信箱旁", text)
        for word in ("屋檐", "墙根", "墙角", "门口", "水碗"):
            self.assertNotIn(word, text)

    def test_hot_revisit_does_not_contradict_the_entrys_real_spot_with_a_second_place(self):
        # 复审实测复现的具体场景：entry 已经"停在院门与信箱旁"，cooling 的
        # 旧措辞又说"缩在水碗边"——水碗不在 SPOTS 里，但一样是第二个具体
        # 地点，一样矛盾。
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        entry = self._entry(spot="院门与信箱旁")
        text = garden._describe_revisit(entry, "fresh", _StubRng(choice_index=0), context, frozenset(), "cooling")
        self.assertIn("院门与信箱旁", text)
        self.assertNotIn("水碗", text)


class DescribeReturnWeatherFlavorTests(unittest.TestCase):
    def _entry(self, **overrides):
        entry = {
            "kind": "animal", "species": "橘猫", "category": "猫", "personality": "活泼",
            "nickname": "", "spot": "院门与信箱旁",
        }
        entry.update(overrides)
        return entry

    def test_return_appends_generic_flavor_not_the_play_pool(self):
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        entry = self._entry()
        text = garden._describe_return(entry, _StubRng(choice_index=0), context, frozenset(), "cooling")
        pool = garden_content.ANIMAL_WEATHER_LINES["猫"]["cooling"]
        self.assertTrue(any(text.endswith(line) for line in pool))

    def test_no_flavor_when_mode_is_normal(self):
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        entry = self._entry()
        with_default = garden._describe_return(entry, _StubRng(choice_index=0), context, frozenset())
        with_normal = garden._describe_return(entry, _StubRng(choice_index=0), context, frozenset(), "normal")
        self.assertEqual(with_default, with_normal)

    def test_flower_kind_never_gets_animal_weather_flavor(self):
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        entry = {"kind": "flower", "species": "蒲公英", "nickname": "", "spot": "院门与信箱旁"}
        with_flower_mode = garden._describe_return(entry, _StubRng(choice_index=0), context, frozenset(), "cooling")
        with_normal = garden._describe_return(entry, _StubRng(choice_index=0), context, frozenset(), "normal")
        self.assertEqual(with_flower_mode, with_normal)

    def test_rain_return_does_not_contradict_the_entrys_real_spot(self):
        # 雨中回归对抗用例：跟回访同理，RETURN_TEMPLATES 本身也带着{spot}。
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        entry = self._entry(spot="院门与信箱旁")
        text = garden._describe_return(entry, _StubRng(choice_index=0), context, frozenset(), "sheltering")
        self.assertIn("院门与信箱旁", text)
        for word in ("屋檐", "墙根", "墙角", "门口", "水碗"):
            self.assertNotIn(word, text)

    def test_hot_return_does_not_add_a_second_concrete_place(self):
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        entry = self._entry(spot="院门与信箱旁")
        text = garden._describe_return(entry, _StubRng(choice_index=0), context, frozenset(), "cooling")
        self.assertIn("院门与信箱旁", text)
        self.assertNotIn("水碗", text)


class WeatherTraceTextTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 29, 14, 0, tzinfo=TZ)
        self.context = garden.calendar_context(self.now)

    def test_prefers_species_flavored_trace_when_animal_present_and_mode_is_not_normal(self):
        animal = {"id": "a1", "category": "兔子", "nickname": "", "species": "灰兔子"}
        rain = [_observation(self.now - timedelta(minutes=5), precip=5, text="大雨")]
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            text = garden._weather_trace_text([animal], self.context, frozenset(), rain, _StubRng(choice_index=0))
        pool = garden_content.ANIMAL_WEATHER_LINES["兔子"]["sheltering"]
        self.assertTrue(any(text == f"{garden.display_name(animal)}{line}" for line in pool))

    def test_falls_back_to_generic_trace_when_no_active_animal(self):
        marker = "占位痕迹场景"
        with patch.object(garden_content, "choose_scene", return_value=marker) as mocked:
            text = garden._weather_trace_text([], self.context, frozenset(), [], _StubRng(choice_index=0))
        mocked.assert_called_once()
        self.assertEqual(text, marker)

    def test_falls_back_to_generic_trace_when_mode_is_normal(self):
        animal = {"id": "a1", "category": "猫", "nickname": "", "species": "橘猫"}
        marker = "占位痕迹场景"
        with patch.object(garden_content, "choose_scene", return_value=marker):
            text = garden._weather_trace_text([animal], self.context, frozenset(), [], _StubRng(choice_index=0))
        self.assertEqual(text, marker)


class SceneSnapshotWeatherModeTests(unittest.TestCase):
    def test_snapshot_field_defaults_to_normal(self):
        state = {"animals": [], "plots": []}
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        snapshot = garden_scene.build_visible_snapshot(state, context)
        self.assertEqual(snapshot["animal_weather_mode"], "normal")

    def test_scene_key_changes_when_only_animal_weather_mode_differs(self):
        state = {"animals": [], "plots": []}
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        normal_snapshot = garden_scene.build_visible_snapshot(state, context, animal_weather_mode="normal")
        cooling_snapshot = garden_scene.build_visible_snapshot(state, context, animal_weather_mode="cooling")
        self.assertNotEqual(
            garden_scene.scene_key(normal_snapshot),
            garden_scene.scene_key(cooling_snapshot),
        )

    def test_writer_snapshot_exposes_animal_weather_mode_to_deepseek(self):
        state = {"animals": [], "plots": []}
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        snapshot = garden_scene.build_visible_snapshot(state, context, animal_weather_mode="basking")
        self.assertEqual(garden_scene.writer_snapshot(snapshot)["animal_weather_mode"], "basking")


class FallbackSceneWeatherModeTests(unittest.TestCase):
    """复审明确要求：fallback_scene() 必须真正读取 animal_weather_mode，
    断言最终正文确实变化，而不是只检查 mode 有没有进入快照。"""

    def _snapshot(self, *, mode, spot="院门与信箱旁"):
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        state = {
            "animals": [{
                "id": "a1", "species": "橘猫", "nickname": "小橘", "personality": "活泼",
                "bond_level": 1, "residency": "visitor", "status": "active", "spot": spot,
            }],
            "plots": [],
        }
        return garden_scene.build_visible_snapshot(state, context, animal_weather_mode=mode)

    def test_each_named_mode_produces_visibly_different_text_from_normal(self):
        normal_text = garden_scene.fallback_scene(self._snapshot(mode="normal"))
        for mode in ("sheltering", "cooling", "basking", "muddy", "wind_play"):
            with self.subTest(mode=mode):
                text = garden_scene.fallback_scene(self._snapshot(mode=mode))
                self.assertNotEqual(text, normal_text)
                self.assertIn("小橘", text)

    def test_weather_flavored_text_drops_conflicting_spot_wording(self):
        # spot 是"院门与信箱旁"；sheltering 兜底整句覆盖后不能再冒出"屋檐"/
        # "墙根"这类跟真实位置矛盾的地点词（覆盖前的版本会直接说"缩到屋檐下"）。
        text = garden_scene.fallback_scene(self._snapshot(mode="sheltering", spot="院门与信箱旁"))
        for word in ("屋檐", "墙根", "墙角", "门口"):
            self.assertNotIn(word, text)

    def test_normal_mode_still_mentions_the_real_spot(self):
        text = garden_scene.fallback_scene(self._snapshot(mode="normal", spot="院门与信箱旁"))
        self.assertIn("院门与信箱旁", text)

    def test_deterministic_no_randomness_same_snapshot_same_text(self):
        snapshot = self._snapshot(mode="basking")
        self.assertEqual(garden_scene.fallback_scene(snapshot), garden_scene.fallback_scene(snapshot))

    def test_unknown_or_missing_mode_falls_back_to_spot_phrase(self):
        snapshot = self._snapshot(mode="normal")
        snapshot.pop("animal_weather_mode", None)
        text = garden_scene.fallback_scene(snapshot)
        self.assertIn("院门与信箱旁", text)


class StrollSceneWeatherModeIntegrationTests(unittest.TestCase):
    """``stroll_scene`` 结算路径：确认它会用真实（哪怕是被测试注入的）
    观测算出模式并传给场景快照，而不是永远停在默认值上。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 29, 14, 0, tzinfo=TZ)

    def test_stroll_scene_snapshot_carries_derived_mode_into_fallback_text(self):
        environment = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        rain = [_observation(self.now - timedelta(minutes=5), precip=5, text="大雨")]
        loader = patch.object(garden, "_environment_observations", side_effect=lambda _now: list(rain))
        loader.start()
        self.addCleanup(loader.stop)
        captured = {}
        real_build = garden_scene.build_visible_snapshot

        def _spy(*args, **kwargs):
            snapshot = real_build(*args, **kwargs)
            captured["mode"] = snapshot["animal_weather_mode"]
            return snapshot

        with patch.object(garden_scene, "build_visible_snapshot", side_effect=_spy), \
             patch("garden.garden_generator.generate_stroll", side_effect=GardenGeneratorError("下线了")):
            garden.stroll_scene(now=self.now, path=self.path)
        self.assertEqual(captured["mode"], "sheltering")


class AnimalWeatherDoesNotTouchRelationshipSafetyTests(unittest.TestCase):
    """验收硬约束：天气不扣亲密、不改常住身份，也不会让 active/away 之外的
    字段被悄悄改写。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 29, 14, 0, tzinfo=TZ)
        self.environment = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.observations = [_observation(self.now - timedelta(minutes=5), precip=5, text="大雨")]
        loader = patch.object(garden, "_environment_observations", side_effect=lambda _now: list(self.observations))
        loader.start()
        self.addCleanup(loader.stop)

    def test_care_under_active_weather_mode_leaves_bond_and_identity_fields_untouched_besides_bond_award(self):
        garden.spawn(
            "animal", species="橘猫", intro="来了一只猫。", category="猫", personality="活泼",
            now=self.now, path=self.path,
        )
        before = garden.active_entries(self.path)[0]
        result = garden.care(before["id"], "摸摸", now=self.now, path=self.path)
        after = result["entry"]
        self.assertEqual(after["status"], "active")
        self.assertEqual(after["residency"], before["residency"])
        # 亲密只应由既有 _award_bond_points 逐日账本决定，天气模式本身不再额外加分。
        self.assertLessEqual(after["bond_points"] - before["bond_points"], 1)


class RealEnvironmentDisabledFullFallbackTests(unittest.TestCase):
    """总开关整体关闭时，四个改动点都必须与阶段 D 之前完全一致。"""

    def test_all_four_surfaces_ignore_animal_weather_mode_when_switch_off(self):
        context = garden.calendar_context(datetime(2026, 7, 29, 14, 0, tzinfo=TZ))
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "0"}):
            mode = garden._animal_weather_mode(
                [_observation(context.now - timedelta(minutes=5), precip=5, text="大雨")], context.now,
            )
        self.assertEqual(mode, "normal")

        entry = {
            "kind": "animal", "species": "橘猫", "category": "猫", "personality": "活泼",
            "trait": "", "nickname": "",
        }
        sample_pool = ("猫活泼摸摸样例反应",)
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")), \
             patch.dict(garden.garden_content.ANIMAL_REACTION_BY_PERSONALITY["猫"]["活泼"], {"摸摸": sample_pool}):
            care_text_default = garden.describe_care(entry, "摸摸", revived=False)
            care_text_explicit_normal = garden.describe_care(entry, "摸摸", revived=False, weather_mode=mode)
        self.assertEqual(care_text_default, care_text_explicit_normal)
        self.assertEqual(care_text_default, "猫活泼摸摸样例反应")


if __name__ == "__main__":
    unittest.main()
