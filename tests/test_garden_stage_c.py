import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden
import garden_crops
import garden_weather
import home


TZ = ZoneInfo("Asia/Shanghai")


class _StubRng:
    """跟既有阶段 B/异常状态机测试同款：choice 按下标取值、不弹出。"""

    def __init__(self, randoms=(), *, choice_index=0):
        self.randoms = list(randoms)
        self.choice_index = choice_index

    def random(self):
        return self.randoms.pop(0)

    def choice(self, values):
        return values[0] if len(values) == 1 else values[self.choice_index]


def _bucket(**overrides) -> dict:
    bucket = {key: 0.0 for key in garden_weather.EXPOSURE_KEYS}
    bucket.update(overrides)
    return bucket


class CropCatalogTests(unittest.TestCase):
    def test_all_crops_have_complete_whitelisted_fields(self):
        self.assertEqual(len(garden_crops.CROPS), 12)
        for crop_id, crop in garden_crops.CROPS.items():
            with self.subTest(crop_id=crop_id):
                self.assertIn("heat_tendency", crop)
                self.assertIn(crop["heat_tendency"], garden_crops.HEAT_TENDENCIES)

    def test_bad_heat_tendency_is_rejected_by_validator(self):
        broken = dict(garden_crops.CROPS)
        broken["tomato"] = dict(broken["tomato"])
        broken["tomato"]["heat_tendency"] = "spicy"
        with patch.object(garden_crops, "CROPS", broken):
            with self.assertRaises(ValueError):
                garden_crops._validate_crop_catalog()


class DailyGrowthMultiplierTests(unittest.TestCase):
    def test_missing_bucket_is_neutral(self):
        self.assertEqual(garden_weather.daily_growth_multiplier(None, heat_tendency="warm_loving"), 1.0)
        self.assertEqual(garden_weather.daily_growth_multiplier({}, heat_tendency="warm_loving"), 1.0)

    def test_insufficient_known_hours_is_neutral_even_with_extreme_weather(self):
        bucket = _bucket(known_hours=5.9, dry_hours=24.0, hot_hours=24.0)
        self.assertEqual(garden_weather.daily_growth_multiplier(bucket, heat_tendency="cool_loving"), 1.0)

    def test_hot_alone_does_not_guarantee_acceleration_when_soil_is_dry(self):
        bucket = _bucket(known_hours=24.0, dry_hours=24.0, hot_hours=24.0)
        multiplier = garden_weather.daily_growth_multiplier(bucket, heat_tendency="warm_loving")
        self.assertLess(multiplier, 1.0)

    def test_warm_loving_gets_small_bonus_when_hot_and_moisture_adequate(self):
        bucket = _bucket(known_hours=24.0, adequate_hours=24.0, hot_hours=24.0)
        multiplier = garden_weather.daily_growth_multiplier(bucket, heat_tendency="warm_loving")
        self.assertGreater(multiplier, 1.0)
        self.assertLessEqual(multiplier, garden_weather.GROWTH_MULTIPLIER_MAX)

    def test_cool_loving_slows_down_more_than_neutral_in_same_heat(self):
        bucket = _bucket(known_hours=24.0, adequate_hours=24.0, hot_hours=24.0)
        cool = garden_weather.daily_growth_multiplier(bucket, heat_tendency="cool_loving")
        neutral = garden_weather.daily_growth_multiplier(bucket, heat_tendency="neutral")
        self.assertLess(cool, neutral)

    def test_dryish_only_slows_never_goes_negative(self):
        bucket = _bucket(known_hours=24.0, dryish_hours=24.0)
        multiplier = garden_weather.daily_growth_multiplier(bucket, heat_tendency="cool_loving")
        self.assertGreaterEqual(multiplier, garden_weather.GROWTH_MULTIPLIER_MIN)
        self.assertGreater(multiplier, 0.0)

    def test_multiplier_always_within_documented_bounds(self):
        extreme_worst = _bucket(known_hours=24.0, dry_hours=24.0, saturated_hours=0.0, hot_hours=24.0)
        extreme_best = _bucket(known_hours=24.0, adequate_hours=24.0, hot_hours=24.0)
        for tendency in ("warm_loving", "cool_loving", "neutral"):
            worst = garden_weather.daily_growth_multiplier(extreme_worst, heat_tendency=tendency)
            best = garden_weather.daily_growth_multiplier(extreme_best, heat_tendency=tendency)
            self.assertGreaterEqual(worst, garden_weather.GROWTH_MULTIPLIER_MIN)
            self.assertLessEqual(best, garden_weather.GROWTH_MULTIPLIER_MAX)

    def test_same_moisture_band_hours_give_same_multiplier_regardless_of_water_source(self):
        # rain_mm 只是审计数值，不参与倍率计算；同样的水分档位小时数无论
        # 来自雨水还是人工浇水，倍率必须完全一致。
        rain_driven = _bucket(known_hours=24.0, adequate_hours=24.0, rain_mm=12.0)
        manual_driven = _bucket(known_hours=24.0, adequate_hours=24.0, rain_mm=0.0)
        for tendency in ("warm_loving", "cool_loving", "neutral"):
            self.assertEqual(
                garden_weather.daily_growth_multiplier(rain_driven, heat_tendency=tendency),
                garden_weather.daily_growth_multiplier(manual_driven, heat_tendency=tendency),
            )

    def test_unknown_heat_tendency_falls_back_to_neutral(self):
        bucket = _bucket(known_hours=24.0, adequate_hours=24.0, hot_hours=24.0)
        self.assertEqual(
            garden_weather.daily_growth_multiplier(bucket, heat_tendency="???"),
            garden_weather.daily_growth_multiplier(bucket, heat_tendency="neutral"),
        )


class ConditionTypeWeightsTests(unittest.TestCase):
    def test_missing_soil_is_uniform(self):
        weights = garden_weather.condition_type_weights(None, garden.CONDITION_TYPES)
        self.assertEqual(weights, {condition_type: 1 for condition_type in garden.CONDITION_TYPES})

    def test_dry_soil_gives_waterlogged_zero_weight(self):
        for moisture in (5.0, 40.0):
            with self.subTest(moisture=moisture):
                weights = garden_weather.condition_type_weights({"moisture": moisture}, garden.CONDITION_TYPES)
                self.assertEqual(weights["waterlogged"], 0)

    def test_saturated_soil_boosts_waterlogged_and_diseased_leaf(self):
        weights = garden_weather.condition_type_weights({"moisture": 95.0}, garden.CONDITION_TYPES)
        self.assertGreater(weights["waterlogged"], 1)
        self.assertGreater(weights["diseased_leaf"], 1)

    def test_dry_soil_boosts_pest_and_nutrient_deficiency(self):
        weights = garden_weather.condition_type_weights({"moisture": 10.0}, garden.CONDITION_TYPES)
        self.assertGreater(weights["pest"], 1)
        self.assertGreater(weights["nutrient_deficiency"], 1)


class ChooseConditionTypeCompatibilityTests(unittest.TestCase):
    """确认权重接入没有破坏既有 `_StubRng` 靠 choice_index 强指定类型的测试契约。"""

    def test_disabled_real_environment_reproduces_uniform_choice_order(self):
        # 泡烂（flood_rot）不参与每日自然候选，只在 NATURAL_CONDITION_TYPES
        # 这张子集里按固定下标复现旧版均匀 choice 顺序。
        plot = {"soil": {"moisture": 5.0}}
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": ""}):
            for index, expected in enumerate(garden.NATURAL_CONDITION_TYPES):
                rng = _StubRng(choice_index=index)
                self.assertEqual(garden._choose_condition_type(plot, rng), expected)

    def test_missing_soil_key_reproduces_uniform_choice_order_even_when_enabled(self):
        plot = {}
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            for index, expected in enumerate(garden.NATURAL_CONDITION_TYPES):
                rng = _StubRng(choice_index=index)
                self.assertEqual(garden._choose_condition_type(plot, rng), expected)

    def test_dry_soil_never_selects_waterlogged_across_all_indices(self):
        plot = {"soil": {"moisture": 10.0}}
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            weights = garden_weather.condition_type_weights(plot["soil"], garden.NATURAL_CONDITION_TYPES)
            weighted_types = [
                condition_type
                for condition_type in garden.NATURAL_CONDITION_TYPES
                for _ in range(max(0, weights.get(condition_type, 1)))
            ]
            for index in range(len(weighted_types)):
                rng = _StubRng(choice_index=index)
                self.assertNotEqual(garden._choose_condition_type(plot, rng), "waterlogged")


class GardenEnvironmentStageCTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        writer = patch(
            "garden.garden_generator.generate_crop_copy",
            side_effect=garden.GardenGeneratorError("离线测试"),
        )
        writer.start()
        self.addCleanup(writer.stop)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.environment = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        # 不提供任何观测：_settle_environment 在 location_id 仍为 None 时
        # 直接返回，不会触碰 soil.exposure_by_date，方便直接摆放合成暴露。
        loader = patch.object(garden, "_environment_observations", return_value=[])
        loader.start()
        self.addCleanup(loader.stop)

    def _raw(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write_raw(self, raw):
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    def _plant(self, at):
        garden.crop_snapshot(now=at, path=self.path)
        garden.plant_crop("小番茄", "1", now=at, path=self.path)
        garden.crop_snapshot(now=at + timedelta(seconds=1), path=self.path)

    def _base_for(self, crop, day):
        context = garden.calendar_context(garden._date_at_start(day))
        return 1.0 + (0.1 if context.term_id in crop["term_affinities"] else 0.0)

    def _linear_expected(self, crop, start, now, exposure=None):
        """线性记账参考实现：按天分段累计 [start, now) 的生长。

        第 N 天的速率读取第 N-1 个、已经完整结束的环境日暴露（缺失即
        中性 1.0），一天之内速率恒定，增量按段内时长线性摊分——与
        garden._accrue_linear_growth 同口径，但独立实现以互相印证。
        """
        exposure = exposure or {}
        expected = 0.0
        cursor = start
        while cursor < now:
            day = cursor.astimezone(TZ).date()
            day_end = garden._date_at_start(day) + timedelta(days=1)
            seg_end = min(now, day_end)
            multiplier = garden_weather.daily_growth_multiplier(
                exposure.get((day - timedelta(days=1)).isoformat()),
                heat_tendency=crop["heat_tendency"],
            )
            expected += self._base_for(crop, day) * multiplier * (
                (seg_end - cursor).total_seconds() / 86400.0
            )
            cursor = seg_end
        return expected

    def test_natural_growth_uses_settled_exposure_for_completed_day_only(self):
        """生长日期保持第四版节奏，但只读取它前一个、已经完整结束的
        环境日期；当前日期即使摆了合成暴露也绝不能被提前使用。"""
        planted_at = datetime(2026, 7, 25, 8, 0, tzinfo=TZ)
        self._plant(planted_at)
        crop = garden_crops.CROPS["tomato"]
        hot_adequate = _bucket(known_hours=24.0, adequate_hours=24.0, hot_hours=24.0)
        hot_dry = _bucket(known_hours=24.0, dry_hours=24.0, hot_hours=24.0)
        raw = self._raw()
        raw["plots"][0]["soil"]["exposure_by_date"] = {
            "2026-07-25": hot_adequate,
            "2026-07-26": hot_dry,
            # 07-27 在 now 时仍是今天，即使测试提前摆入也不得读取。
            "2026-07-27": hot_adequate,
        }
        self._write_raw(raw)

        exposure = self._raw()["plots"][0]["soil"]["exposure_by_date"]
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        snapshot = garden.crop_snapshot(now=now, path=self.path)
        actual_growth = snapshot["plots"][0]["growth_points"]

        # 07-27 白天的速率读取已结束的 07-26（hot_dry）；提前摆入的
        # 07-27 桶（hot_adequate）绝不能被当天使用——参考实现按同一
        # 规则累计，二者对得上即证明没有偷读今天。
        expected = self._linear_expected(crop, planted_at, now, exposure)
        self.assertAlmostEqual(actual_growth, round(expected, 4), places=3)
        self.assertNotIn("growth_multiplier_pending", self._raw()["plots"][0])

        # 同一天再次查看：只按流逝的时间线性多累计（仍用 07-26 的桶），
        # 不重复入账、也不改读今天的暴露。
        later_same_day = datetime(2026, 7, 27, 20, 0, tzinfo=TZ)
        repeated = garden.crop_snapshot(now=later_same_day, path=self.path)
        self.assertGreater(repeated["plots"][0]["growth_points"], actual_growth)
        self.assertAlmostEqual(
            repeated["plots"][0]["growth_points"],
            round(self._linear_expected(crop, planted_at, later_same_day, exposure), 4),
            places=3,
        )

        # 到次日，07-27 已完整，才轮到 07-28 这一天读取它。
        next_day = datetime(2026, 7, 28, 8, 0, tzinfo=TZ)
        snapshot2 = garden.crop_snapshot(now=next_day, path=self.path)
        self.assertAlmostEqual(
            snapshot2["plots"][0]["growth_points"],
            round(self._linear_expected(crop, planted_at, next_day, exposure), 4),
            places=3,
        )

    def test_missing_exposure_bucket_is_neutral_not_punitive(self):
        planted_at = datetime(2026, 7, 25, 8, 0, tzinfo=TZ)
        self._plant(planted_at)
        # 不写入任何 exposure_by_date：完整日期读不到暴露，理应中性 1.0。
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        snapshot = garden.crop_snapshot(now=now, path=self.path)
        actual_growth = snapshot["plots"][0]["growth_points"]

        crop = garden_crops.CROPS["tomato"]
        # 天气数据完全缺失时倍率恒为中性 1.0，生长仍按线性记账正常累计。
        expected = self._linear_expected(crop, planted_at, now)
        self.assertAlmostEqual(actual_growth, round(expected, 4), places=3)

    def test_active_condition_still_fully_pauses_growth_regardless_of_weather(self):
        planted_at = datetime(2026, 7, 25, 8, 0, tzinfo=TZ)
        self._plant(planted_at)
        # 先让它正常长两天脱离 seed 阶段，自然异常才有合格候选。
        settle_at = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        garden.crop_snapshot(now=settle_at, path=self.path)
        raw = self._raw()
        # 只清掉生长两天带出的 crop_stage 排队事件，不影响本测试关心的
        # 自然异常与生长暂停，也不去动同一天的“今天是否已判定”状态。
        raw["pending_events"] = [
            event for event in raw["pending_events"]
            if not (isinstance(event, dict) and event.get("type") == "crop_stage")
        ]
        self._write_raw(raw)
        before = raw["plots"][0]["growth_points"]
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            event = garden.run_tick(
                settle_at, rng=_StubRng([0.05], choice_index=0), path=self.path,
            )
        self.assertEqual(event["type"], "crop_condition")
        raw = self._raw()
        raw["plots"][0]["soil"]["exposure_by_date"] = {
            "2026-07-27": _bucket(known_hours=24.0, adequate_hours=24.0, hot_hours=24.0),
        }
        self._write_raw(raw)
        now = datetime(2026, 7, 28, 8, 0, tzinfo=TZ)
        snapshot = garden.crop_snapshot(now=now, path=self.path)
        self.assertEqual(snapshot["plots"][0]["growth_points"], before)

    def test_ready_crop_does_not_keep_accumulating_growth(self):
        planted_at = datetime(2026, 7, 25, 8, 0, tzinfo=TZ)
        self._plant(planted_at)
        raw = self._raw()
        raw["plots"][0]["status"] = "ready"
        raw["plots"][0]["stage"] = "ready"
        raw["plots"][0]["growth_points"] = 4.0
        raw["plots"][0]["soil"]["exposure_by_date"] = {
            "2026-07-26": _bucket(known_hours=24.0, adequate_hours=24.0, hot_hours=24.0),
        }
        self._write_raw(raw)
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        snapshot = garden.crop_snapshot(now=now, path=self.path)
        self.assertEqual(snapshot["plots"][0]["growth_points"], 4.0)
        self.assertEqual(snapshot["plots"][0]["status"], "ready")

    def test_real_environment_disabled_keeps_v4_growth_unaffected(self):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": ""}):
            planted_at = datetime(2026, 7, 25, 8, 0, tzinfo=TZ)
            garden.crop_snapshot(now=planted_at, path=self.path)
            garden.plant_crop("小番茄", "1", now=planted_at, path=self.path)
            now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
            snapshot = garden.crop_snapshot(now=now, path=self.path)
        raw = self._raw()
        self.assertEqual(raw["version"], 4)
        crop = garden_crops.CROPS["tomato"]
        # v4（无环境数据）路径下倍率恒为中性 1.0 的线性记账。
        expected = self._linear_expected(crop, planted_at, now)
        self.assertAlmostEqual(snapshot["plots"][0]["growth_points"], round(expected, 4), places=3)

    def _plant_with_uniform_weather(self, path, *, planted_at, days, bucket_factory):
        """种下一棵番茄，并从播种当天起摆放 ``days`` 个环境日；第 N 个
        生长记账日读取第 N-1 个、已经结束的环境日。"""
        garden.crop_snapshot(now=planted_at, path=path)
        garden.plant_crop("小番茄", "1", now=planted_at, path=path)
        garden.crop_snapshot(now=planted_at + timedelta(seconds=1), path=path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        exposure = {}
        cursor = planted_at.date()
        for _ in range(days):
            exposure[cursor.isoformat()] = bucket_factory(cursor.isoformat())
            cursor += timedelta(days=1)
        raw["plots"][0]["soil"]["exposure_by_date"] = exposure
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    def test_single_long_jump_matches_daily_polling_convergence(self):
        """性质回归 1：完整环境日期无论一次长跳还是逐日进入，最终生长
        点数、阶段与成熟资格必须完全一致——同一批完整环境事实不能因为
        查看频率不同而产生另一种结果。"""
        planted_at = datetime(2026, 7, 25, 8, 0, tzinfo=TZ)
        hot_dry = lambda day: _bucket(known_hours=24.0, dry_hours=24.0, hot_hours=24.0)
        span_days = 3

        long_jump_path = Path(self.tempdir.name) / "long_jump.json"
        self._plant_with_uniform_weather(
            long_jump_path, planted_at=planted_at, days=span_days, bucket_factory=hot_dry,
        )
        jump_now = planted_at + timedelta(days=span_days)
        jump_snapshot = garden.crop_snapshot(now=jump_now, path=long_jump_path)
        jump_growth = jump_snapshot["plots"][0]["growth_points"]

        daily_path = Path(self.tempdir.name) / "daily_polling.json"
        self._plant_with_uniform_weather(
            daily_path, planted_at=planted_at, days=span_days, bucket_factory=hot_dry,
        )
        daily_growth = None
        for offset in range(1, span_days + 1):
            daily_snapshot = garden.crop_snapshot(
                now=planted_at + timedelta(days=offset), path=daily_path,
            )
            daily_growth = daily_snapshot["plots"][0]["growth_points"]

        self.assertAlmostEqual(jump_growth, daily_growth, places=4)
        crop = garden_crops.CROPS["tomato"]
        exposure = {
            (planted_at.date() + timedelta(days=index)).isoformat():
                hot_dry(planted_at.date() + timedelta(days=index))
            for index in range(span_days)
        }
        expected = self._linear_expected(crop, planted_at, jump_now, exposure)
        self.assertAlmostEqual(jump_growth, round(expected, 4), places=3)
        self.assertLess(jump_growth, span_days * 1.1 - 0.01)
        self.assertEqual(jump_snapshot["plots"][0]["status"], "growing")
        self.assertEqual(
            json.loads(daily_path.read_text(encoding="utf-8"))["plots"][0]["status"], "growing",
        )
        self.assertNotIn(
            "growth_multiplier_pending",
            json.loads(daily_path.read_text(encoding="utf-8"))["plots"][0],
        )

    def test_adverse_weather_cannot_unlock_harvest_before_true_threshold(self):
        """真实累计未过成熟线时不能 ready/harvest；达到真实阈值的当天，
        一次长跳与逐日查看必须一起成熟。

        种下当天现在会按比例先记一笔账（见 plant_crop），所以持续干旱下
        跨过成熟线的日子比引入这笔账之前提前了一天：这里的检查点相应从
        第5/6天挪到第4/5天，其余"长跳与逐日查看必须收敛到同一个结果"的
        核心断言不变。
        """
        planted_at = datetime(2026, 7, 25, 8, 0, tzinfo=TZ)
        hot_dry = lambda day: _bucket(known_hours=24.0, dry_hours=24.0, hot_hours=24.0)
        long_jump_path = Path(self.tempdir.name) / "harvest_long_jump.json"
        daily_path = Path(self.tempdir.name) / "harvest_daily.json"
        for path in (long_jump_path, daily_path):
            self._plant_with_uniform_weather(
                path, planted_at=planted_at, days=6, bucket_factory=hot_dry,
            )

        before_ready = planted_at + timedelta(days=4)
        long_before = garden.crop_snapshot(now=before_ready, path=long_jump_path)
        daily_before = None
        for offset in range(1, 5):
            daily_before = garden.crop_snapshot(
                now=planted_at + timedelta(days=offset), path=daily_path,
            )
        assert daily_before is not None
        for snapshot in (long_before, daily_before):
            plot = snapshot["plots"][0]
            self.assertEqual(plot["status"], "growing")
            self.assertLess(plot["growth_points"], garden_crops.CROPS["tomato"]["growth_days"])
            self.assertEqual(plot["stage"], garden._crop_stage("tomato", plot["growth_points"]))
        self.assertAlmostEqual(
            long_before["plots"][0]["growth_points"],
            daily_before["plots"][0]["growth_points"],
            places=4,
        )
        for path in (long_jump_path, daily_path):
            with self.assertRaises(garden.GardenError):
                garden.harvest_crop("1", now=before_ready, path=path)

        ready_at = planted_at + timedelta(days=5)
        long_ready = garden.crop_snapshot(now=ready_at, path=long_jump_path)
        daily_ready = garden.crop_snapshot(now=ready_at, path=daily_path)
        self.assertEqual(long_ready["plots"][0]["status"], "ready")
        self.assertEqual(daily_ready["plots"][0]["status"], "ready")
        self.assertAlmostEqual(
            long_ready["plots"][0]["growth_points"],
            daily_ready["plots"][0]["growth_points"],
            places=4,
        )

    def test_previous_environment_day_applies_exactly_once(self):
        """07-27 全天的生长速率读取已结束的 07-26 环境；同日反复查看只按
        流逝时间累计、不重复入账，也不会改读仍未结束的 07-27 环境。"""
        planted_at = datetime(2026, 7, 25, 8, 0, tzinfo=TZ)
        self._plant(planted_at)
        hot_dry = _bucket(known_hours=24.0, dry_hours=24.0, hot_hours=24.0)
        hot_adequate = _bucket(known_hours=24.0, adequate_hours=24.0, hot_hours=24.0)
        exposure = {
            "2026-07-26": hot_dry,
            "2026-07-27": hot_adequate,
        }
        raw = self._raw()
        raw["plots"][0]["soil"]["exposure_by_date"] = exposure
        self._write_raw(raw)

        crop = garden_crops.CROPS["tomato"]
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        snapshot = garden.crop_snapshot(now=now, path=self.path)
        # 07-26 的速率找不到 07-25 暴露，安全中性；07-27 的速率读取
        # 07-26（hot_dry），即使 07-27 自己的桶已提前摆入也不得使用。
        expected = self._linear_expected(crop, planted_at, now, exposure)
        self.assertAlmostEqual(snapshot["plots"][0]["growth_points"], round(expected, 4), places=3)

        previous = snapshot["plots"][0]["growth_points"]
        for hour in (10, 14, 20):
            check_at = datetime(2026, 7, 27, hour, 0, tzinfo=TZ)
            repeated = garden.crop_snapshot(now=check_at, path=self.path)
            self.assertGreater(repeated["plots"][0]["growth_points"], previous)
            self.assertAlmostEqual(
                repeated["plots"][0]["growth_points"],
                round(self._linear_expected(crop, planted_at, check_at, exposure), 4),
                places=3,
            )
            previous = repeated["plots"][0]["growth_points"]

        next_day_at = datetime(2026, 7, 28, 8, 0, tzinfo=TZ)
        next_day = garden.crop_snapshot(now=next_day_at, path=self.path)
        self.assertAlmostEqual(
            next_day["plots"][0]["growth_points"],
            round(self._linear_expected(crop, planted_at, next_day_at, exposure), 4),
            places=3,
        )

    def test_neutral_v5_keeps_v4_three_four_five_day_maturation_baseline(self):
        """没有可用天气暴露时，v5 的中性倍率不能改变 v4 的成熟日期。

        线性记账下生长从种下那一刻起连续累积，"生长期N天"就真的是约
        N 天（term 加成会稍微提前一点），逐日轮询首次看到 ready 的日子
        相应比整天记账时代晚了：整天时代跨入当天即一次性入账整天，等于
        白捡当天还没过完的时间。v4/v5 二者仍必须相等——这才是这条测试
        真正要守住的不变量。
        """
        cases = (
            ("小萝卜", datetime(2026, 3, 10, 8, 0, tzinfo=TZ), 3),
            ("小番茄", datetime(2026, 7, 10, 8, 0, tzinfo=TZ), 4),
            ("南瓜", datetime(2026, 9, 10, 8, 0, tzinfo=TZ), 5),
        )

        def ready_offset(path, crop_name, planted_at, *, enabled):
            with patch.dict(
                os.environ,
                {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1" if enabled else ""},
            ):
                garden.crop_snapshot(now=planted_at, path=path)
                garden.plant_crop(crop_name, "1", now=planted_at, path=path)
                for offset in range(1, 7):
                    snapshot = garden.crop_snapshot(
                        now=planted_at + timedelta(days=offset), path=path,
                    )
                    if snapshot["plots"][0]["status"] == "ready":
                        return offset
            return None

        for index, (crop_name, planted_at, expected_days) in enumerate(cases):
            with self.subTest(crop=crop_name):
                v4_path = Path(self.tempdir.name) / f"neutral-v4-{index}.json"
                v5_path = Path(self.tempdir.name) / f"neutral-v5-{index}.json"
                v4_ready = ready_offset(
                    v4_path, crop_name, planted_at, enabled=False,
                )
                v5_ready = ready_offset(
                    v5_path, crop_name, planted_at, enabled=True,
                )
                self.assertEqual(v4_ready, expected_days)
                self.assertEqual(v5_ready, v4_ready)


class GrowthEnvironmentNoteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        writer = patch(
            "garden.garden_generator.generate_crop_copy",
            side_effect=garden.GardenGeneratorError("离线测试"),
        )
        writer.start()
        self.addCleanup(writer.stop)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.environment = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        loader = patch.object(garden, "_environment_observations", return_value=[])
        loader.start()
        self.addCleanup(loader.stop)

    def test_note_is_none_when_disabled_or_no_soil_or_not_growing(self):
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        self.assertIsNone(garden.growth_environment_note({"status": "growing"}, now))
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": ""}):
            self.assertIsNone(
                garden.growth_environment_note(
                    {"status": "growing", "soil": {"exposure_by_date": {}}, "crop_id": "tomato"}, now,
                ),
            )
        self.assertIsNone(
            garden.growth_environment_note({"status": "ready", "soil": {"exposure_by_date": {}}, "crop_id": "tomato"}, now),
        )

    def test_note_reflects_yesterdays_multiplier(self):
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        fast_plot = {
            "status": "growing", "crop_id": "tomato",
            "soil": {"exposure_by_date": {
                "2026-07-26": _bucket(known_hours=24.0, adequate_hours=24.0, hot_hours=24.0),
            }},
        }
        self.assertEqual(garden.growth_environment_note(fast_plot, now), "这几天长得比平时快一点")

        slow_plot = {
            "status": "growing", "crop_id": "tomato",
            "soil": {"exposure_by_date": {
                "2026-07-26": _bucket(known_hours=24.0, dry_hours=24.0, hot_hours=24.0),
            }},
        }
        self.assertEqual(garden.growth_environment_note(slow_plot, now), "这几天长得比平时慢一点")

        neutral_plot = {
            "status": "growing", "crop_id": "tomato",
            "soil": {"exposure_by_date": {}},
        }
        self.assertIsNone(garden.growth_environment_note(neutral_plot, now))

    def test_home_view_shows_note_only_without_active_condition(self):
        planted_at = datetime(2026, 7, 25, 8, 0, tzinfo=TZ)
        garden.crop_snapshot(now=planted_at, path=self.path)
        garden.plant_crop("小番茄", "1", now=planted_at, path=self.path)
        garden.crop_snapshot(now=planted_at + timedelta(seconds=1), path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["plots"][0]["soil"]["exposure_by_date"] = {
            "2026-07-26": _bucket(known_hours=24.0, adequate_hours=24.0, hot_hours=24.0),
        }
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        with patch("home.datetime") as mock_datetime:
            mock_datetime.now.return_value = now
            with patch.object(home.garden, "GARDEN_FILE", self.path):
                # 环境快慢备注属于详细信息，裸查看的摘要档不再逐块展开。
                text = home._garden_list_active_detailed()
        self.assertIn("长得比平时快一点", text)

    def test_note_direction_matches_the_multiplier_actually_applied_to_growth(self):
        """性质回归 3：查看页“长得快/慢”不能只是独立算一遍昨天的倍率，
        必须真的对得上本次生长结算读取的同一个昨日环境桶。"""
        planted_at = datetime(2026, 7, 25, 8, 0, tzinfo=TZ)
        buckets = {
            "2026-07-25": _bucket(known_hours=24.0, adequate_hours=24.0, hot_hours=24.0),
            "2026-07-26": _bucket(known_hours=24.0, dry_hours=24.0, hot_hours=24.0),
            "2026-07-27": _bucket(known_hours=24.0, adequate_hours=24.0),
        }
        garden.crop_snapshot(now=planted_at, path=self.path)
        planted_snapshot = garden.plant_crop("小番茄", "1", now=planted_at, path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["plots"][0]["soil"]["exposure_by_date"] = buckets
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        crop = garden_crops.CROPS["tomato"]
        # 线性记账下，日期 D 全天的速率恒定且读取 D-1 的环境桶；备注在
        # D 当天任意时刻查看，方向必须对得上 D 这一天真正入账的倍率。
        # 用相邻两个午夜整点的差值提取 D 全天实际应用的倍率。
        expected_notes = (
            ("2026-07-26", "这几天长得比平时快一点"),   # 读 07-25 hot_adequate
            ("2026-07-27", "这几天长得比平时慢一点"),   # 读 07-26 hot_dry
            ("2026-07-28", None),                        # 读 07-27 中性
        )
        previous_growth = garden.crop_snapshot(
            now=garden._date_at_start(date(2026, 7, 26)), path=self.path,
        )["plots"][0]["growth_points"]
        for day_iso, expected_note in expected_notes:
            day = date.fromisoformat(day_iso)
            environment_day = day - timedelta(days=1)
            expected_multiplier = garden_weather.daily_growth_multiplier(
                buckets[environment_day.isoformat()],
                heat_tendency=crop["heat_tendency"],
            )
            # 备注在 D 当天中午查看，描述的正是当天正在应用的倍率方向。
            midday = garden.crop_snapshot(
                now=garden._date_at_start(day) + timedelta(hours=12), path=self.path,
            )
            self.assertEqual(
                garden.growth_environment_note(
                    midday["plots"][0], garden._date_at_start(day) + timedelta(hours=12),
                ),
                expected_note,
            )
            day_end_growth = garden.crop_snapshot(
                now=garden._date_at_start(day + timedelta(days=1)), path=self.path,
            )["plots"][0]["growth_points"]
            delta = day_end_growth - previous_growth
            previous_growth = day_end_growth
            context = garden.calendar_context(garden._date_at_start(day))
            base = 1.0 + (0.1 if context.term_id in crop["term_affinities"] else 0.0)
            applied_multiplier = delta / base
            self.assertAlmostEqual(applied_multiplier, expected_multiplier, places=3)
        self.assertNotIn(
            "growth_multiplier_pending",
            json.loads(self.path.read_text(encoding="utf-8"))["plots"][0],
        )


class NaturalConditionCooldownStillEnforcedTests(unittest.TestCase):
    """连续雨/湿透只改类型权重，不绕过全院单异常和 48 小时冷却。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        writer = patch(
            "garden.garden_generator.generate_crop_copy",
            side_effect=garden.GardenGeneratorError("离线测试"),
        )
        writer.start()
        self.addCleanup(writer.stop)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 28, 12, 0, tzinfo=TZ)
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS]
        state["meta"]["crop_seed_box_initialized"] = True
        for index, plot in enumerate(state["plots"][:2], start=1):
            plot.update({
                "crop_id": "tomato",
                "planted_at": (self.now - timedelta(days=1)).isoformat(),
                "last_settled_at": self.now.isoformat(),
                "growth_points": 1.0,
                "stage": "sprout",
                "water_bonus_dates": [],
                "ready_at": None,
                "status": "growing",
                "cycle_id": f"cycle-{index}",
                "stage_events_seen": [],
                "soil": {"moisture": 95.0, "exposure_by_date": {}},
            })
        self.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def test_second_saturated_plot_still_blocked_by_single_active_condition(self):
        with patch.dict(
            os.environ,
            {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1", "GARDEN_REAL_ENVIRONMENT_ENABLED": "1"},
        ):
            event = garden.run_tick(self.now, rng=_StubRng([0.05], choice_index=0), path=self.path)
            self.assertEqual(event["type"], "crop_condition")
            garden.confirm_pending_event(
                event["event_id"], event["delivery_token"], now=self.now, path=self.path,
            )
            second = garden.run_tick(
                self.now + timedelta(hours=1), rng=_StubRng([0.0], choice_index=0), path=self.path,
            )
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        active = [plot for plot in raw["plots"] if isinstance(plot.get("condition"), dict) and plot["condition"]["status"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main()
