"""第七版阶段 A：内涝惩罚与作物品质的纯函数与数据地基。

覆盖设计稿《迷你小院子第七版内涝惩罚与作物品质设计.md》第十一节阶段 A
验收清单：排水加速退水、浸泡时长派生、四个新字段的迁移与校验收紧、
flood_rot 枚举扩展不进自然异常候选、resolve_crop_condition 对它的
拒绝语义。本阶段不做结算接入（不创建真正的 flood_watch.since/泡烂
记录），只测数据地基本身。
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden
import garden_weather


TZ = ZoneInfo("Asia/Shanghai")


def observation(at, *, precip=0.0, text="晴", wind="2"):
    value = garden_weather.normalize_observation(
        location_id="test",
        location_name="测试",
        observed_time=at.isoformat(),
        received_at=at,
        temp=24,
        feels_like=24,
        humidity=80,
        wind_scale=wind,
        precip=precip,
        condition_text=text,
    )
    if value is None:
        raise AssertionError("测试观测标准化失败")
    return value


def hourly_rain(end, *, hours, total, text="中雨", wind="4"):
    rate = total / hours
    return [
        observation(
            end - timedelta(hours=hours - index - 1),
            precip=rate,
            text=text,
            wind=wind,
        )
        for index in range(hours)
    ]


class _StubRng:
    def __init__(self, randoms=(), *, choice_index=0):
        self.randoms = list(randoms)
        self.choice_index = choice_index

    def random(self):
        return self.randoms.pop(0)

    def choice(self, values):
        return values[0] if len(values) == 1 else values[self.choice_index]


class YardWaterDrainedAtTests(unittest.TestCase):
    """排水加速退水：只在 drained_at 之后的 24 小时内叠加速率。"""

    def setUp(self):
        self.base = datetime(2026, 8, 1, 0, 0, tzinfo=TZ)
        self.rain_end = self.base + timedelta(hours=1)
        # 单场特大暴雨把积水一次性打到封顶，之后再无降雨，纯看退水曲线。
        self.rain = hourly_rain(
            self.rain_end, hours=1, total=500, text="特大暴雨", wind="10",
        )

    def test_missing_drained_at_matches_pre_existing_signature(self):
        now = self.rain_end + timedelta(hours=10)
        implicit = garden_weather.yard_water_index(self.rain, {}, now)
        explicit_none = garden_weather.yard_water_index(
            self.rain, {}, now, drained_at=None,
        )
        self.assertEqual(implicit, explicit_none)

    def test_cap_at_300_unaffected_by_supplying_drained_at(self):
        no_drain = garden_weather.yard_water_index(self.rain, {}, self.rain_end)
        with_drain = garden_weather.yard_water_index(
            self.rain, {}, self.rain_end, drained_at=self.rain_end,
        )
        self.assertEqual(no_drain, garden_weather.YARD_WATER_MAX)
        self.assertEqual(with_drain, garden_weather.YARD_WATER_MAX)

    def test_boost_lowers_index_by_exactly_the_extra_rate_for_24_hours(self):
        drained_at = self.rain_end
        boundary = drained_at + garden_weather.YARD_DRAIN_BOOST_WINDOW
        no_drain = garden_weather.yard_water_index(self.rain, {}, boundary)
        with_drain = garden_weather.yard_water_index(
            self.rain, {}, boundary, drained_at=drained_at,
        )
        expected_extra_drain = (
            garden_weather.YARD_DRAIN_BOOST_RATE_MM_H
            * garden_weather.YARD_DRAIN_BOOST_WINDOW.total_seconds() / 3600.0
        )
        self.assertAlmostEqual(no_drain - with_drain, expected_extra_drain, places=4)

    def test_boost_stops_exactly_at_the_24_hour_mark(self):
        drained_at = self.rain_end
        at_boundary = drained_at + garden_weather.YARD_DRAIN_BOOST_WINDOW
        one_hour_later = at_boundary + timedelta(hours=1)
        no_drain_boundary = garden_weather.yard_water_index(self.rain, {}, at_boundary)
        no_drain_later = garden_weather.yard_water_index(self.rain, {}, one_hour_later)
        with_drain_boundary = garden_weather.yard_water_index(
            self.rain, {}, at_boundary, drained_at=drained_at,
        )
        with_drain_later = garden_weather.yard_water_index(
            self.rain, {}, one_hour_later, drained_at=drained_at,
        )
        # 一过 24 小时，两条曲线在接下来这一小时里应该按同样的普通速率下降——
        # 加速已经停止，不再产生额外差值。
        self.assertAlmostEqual(
            no_drain_boundary - no_drain_later,
            with_drain_boundary - with_drain_later,
            places=4,
        )

    def test_drained_at_long_expired_before_the_replay_window_has_no_effect(self):
        now = self.base + timedelta(hours=40)
        ancient_drain = self.base - timedelta(hours=200)
        no_drain = garden_weather.yard_water_index(self.rain, {}, now)
        with_ancient_drain = garden_weather.yard_water_index(
            self.rain, {}, now, drained_at=ancient_drain,
        )
        self.assertEqual(no_drain, with_ancient_drain)

    def test_idempotent_and_order_independent_with_drained_at(self):
        drained_at = self.rain_end
        now = drained_at + timedelta(hours=24)
        first = garden_weather.yard_water_index(
            self.rain, {}, now, drained_at=drained_at,
        )
        second = garden_weather.yard_water_index(
            self.rain, {}, now, drained_at=drained_at,
        )
        reversed_order = garden_weather.yard_water_index(
            list(reversed(self.rain)), {}, now, drained_at=drained_at,
        )
        doubled = garden_weather.yard_water_index(
            self.rain * 2, {}, now, drained_at=drained_at,
        )
        self.assertEqual(first, second)
        self.assertEqual(first, reversed_order)
        self.assertEqual(first, doubled)


class FloodSoakHoursTests(unittest.TestCase):
    def setUp(self):
        self.since = datetime(2026, 8, 1, 0, 0, tzinfo=TZ)

    def test_no_flood_watch_means_zero_soak(self):
        now = self.since + timedelta(hours=10)
        self.assertEqual(
            garden_weather.flood_soak_hours(None, None, now), 0.0,
        )
        self.assertEqual(
            garden_weather.flood_soak_hours(None, self.since, now), 0.0,
        )

    def test_no_drain_soaks_straight_from_since(self):
        now = self.since + timedelta(hours=10)
        self.assertEqual(
            garden_weather.flood_soak_hours(self.since, None, now), 10.0,
        )

    def test_drain_before_since_is_ignored(self):
        now = self.since + timedelta(hours=10)
        earlier_drain = self.since - timedelta(hours=5)
        self.assertEqual(
            garden_weather.flood_soak_hours(self.since, earlier_drain, now), 10.0,
        )

    def test_drain_after_since_resets_the_clock(self):
        drained_at = self.since + timedelta(hours=3)
        now = self.since + timedelta(hours=10)
        self.assertEqual(
            garden_weather.flood_soak_hours(self.since, drained_at, now), 7.0,
        )

    def test_drain_exactly_at_since_behaves_like_no_drain(self):
        now = self.since + timedelta(hours=10)
        self.assertEqual(
            garden_weather.flood_soak_hours(self.since, self.since, now), 10.0,
        )

    def test_crosses_quality_and_rot_thresholds(self):
        quality_hours = garden_weather.FLOOD_SOAK_QUALITY_AFTER.total_seconds() / 3600.0
        rot_hours = garden_weather.FLOOD_SOAK_ROT_AFTER.total_seconds() / 3600.0
        just_before_quality = self.since + timedelta(hours=quality_hours - 0.01)
        at_quality = self.since + timedelta(hours=quality_hours)
        just_before_rot = self.since + timedelta(hours=rot_hours - 0.01)
        at_rot = self.since + timedelta(hours=rot_hours)
        self.assertLess(
            garden_weather.flood_soak_hours(self.since, None, just_before_quality),
            quality_hours,
        )
        self.assertGreaterEqual(
            garden_weather.flood_soak_hours(self.since, None, at_quality),
            quality_hours,
        )
        self.assertLess(
            garden_weather.flood_soak_hours(self.since, None, just_before_rot),
            rot_hours,
        )
        self.assertGreaterEqual(
            garden_weather.flood_soak_hours(self.since, None, at_rot),
            rot_hours,
        )

    def test_drain_can_push_soak_back_below_quality_threshold(self):
        quality_hours = garden_weather.FLOOD_SOAK_QUALITY_AFTER.total_seconds() / 3600.0
        now = self.since + timedelta(hours=quality_hours + 5)
        drained_at = self.since + timedelta(hours=quality_hours + 2)
        soaked = garden_weather.flood_soak_hours(self.since, drained_at, now)
        self.assertEqual(soaked, 3.0)
        self.assertLess(soaked, quality_hours)


class FloodRotEnumTests(unittest.TestCase):
    def test_flood_rot_is_a_recognized_condition_type_with_a_label(self):
        self.assertIn("flood_rot", garden.CONDITION_TYPES)
        self.assertEqual(garden.CONDITION_LABELS["flood_rot"], "泡烂")

    def test_flood_rot_has_no_recovery_action(self):
        self.assertNotIn("flood_rot", garden.CONDITION_ACTIONS)

    def test_flood_rot_is_excluded_from_natural_condition_candidates(self):
        self.assertNotIn("flood_rot", garden.NATURAL_CONDITION_TYPES)
        self.assertEqual(
            set(garden.NATURAL_CONDITION_TYPES),
            set(garden.CONDITION_TYPES) - {"flood_rot"},
        )

    def test_choose_condition_type_never_returns_flood_rot(self):
        plot = {"soil": {"moisture": 95.0}}
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            weights = garden_weather.condition_type_weights(
                plot["soil"], garden.NATURAL_CONDITION_TYPES, yard_water="flooded",
            )
            weighted_types = [
                condition_type
                for condition_type in garden.NATURAL_CONDITION_TYPES
                for _ in range(max(0, weights.get(condition_type, 1)))
            ]
            for index in range(len(weighted_types)):
                rng = _StubRng(choice_index=index)
                self.assertNotEqual(
                    garden._choose_condition_type(plot, rng, yard_water="flooded"),
                    "flood_rot",
                )


class GardenStageAV7MigrationTests(unittest.TestCase):
    """老 v5 存档（缺新字段）读入即按中性默认迁移，且通过校验。"""

    def setUp(self):
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)

    def _old_v5_archive(self):
        state = garden._empty_state()
        state.pop("entries")
        state["version"] = garden.STATE_VERSION
        state["environment"] = {
            "location_id": None, "last_settled_at": self.now.isoformat(),
            "last_observation_id": None, "last_fresh_weather_at": None,
            "status": "missing", "rain_finalized_at": self.now.isoformat(),
            "rain_by_date": {},
            # 故意不写 flood_watch，模拟第七版上线前的真实存档。
        }
        # 故意不写 produce_poor / drainage，模拟更早版本落下的旧字段集合。
        del state["inventory"]["produce_poor"]
        del state["journal"]["drainage"]
        return state

    def test_missing_fields_are_backfilled_to_neutral_defaults(self):
        normalized = garden._normalize_v4_state(self._old_v5_archive(), self.now, None)
        self.assertEqual(normalized["inventory"]["produce_poor"], {})
        self.assertEqual(
            normalized["environment"]["flood_watch"],
            {"since": None, "drained_at": None},
        )
        self.assertEqual(normalized["journal"]["drainage"], [])
        self.assertTrue(normalized.get("_migrated"))

    def test_backfill_does_not_touch_unrelated_existing_data(self):
        archive = self._old_v5_archive()
        archive["environment"]["rain_by_date"] = {"2026-08-09": 12.5}
        archive["inventory"]["seeds"] = {"tomato": 3}
        normalized = garden._normalize_v4_state(archive, self.now, None)
        self.assertEqual(normalized["environment"]["rain_by_date"], {"2026-08-09": 12.5})
        self.assertEqual(normalized["inventory"]["seeds"], {"tomato": 3})

    def test_fresh_v4_to_v5_migration_includes_flood_watch_from_the_start(self):
        state = garden._empty_state()
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            garden._migrate_v4_to_v5(state, self.now)
        self.assertEqual(
            state["environment"]["flood_watch"],
            {"since": None, "drained_at": None},
        )
        self.assertEqual(state["version"], garden.STATE_VERSION)

    def test_repeated_reads_of_an_old_archive_stay_idempotent(self):
        first = garden._normalize_v4_state(self._old_v5_archive(), self.now, None)
        # 已经补齐过一次的存档，第二次读入不应再改变结果或报错。
        second = garden._normalize_v4_state(json.loads(json.dumps(
            {key: value for key, value in first.items() if key not in ("entries", "_migrated")}
        )), self.now, None)
        self.assertEqual(second["inventory"]["produce_poor"], {})
        self.assertEqual(
            second["environment"]["flood_watch"],
            {"since": None, "drained_at": None},
        )


class GardenStageAV7RejectionTests(unittest.TestCase):
    """坏字段（负数、未知品质值、flood_watch 形状损坏）必须拒绝写盘。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)
        self._write_v4_growing_plot()

    def _write_v4_growing_plot(self):
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS]
        state["meta"]["crop_seed_box_initialized"] = True
        state["plots"][0].update({
            "crop_id": "tomato",
            "planted_at": (self.now - timedelta(days=1)).isoformat(),
            "last_settled_at": self.now.isoformat(),
            "growth_points": 1.0,
            "stage": "sprout",
            "water_bonus_dates": [],
            "ready_at": None,
            "status": "growing",
            "cycle_id": "cycle-1",
            "stage_events_seen": [],
        })
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def _migrate_to_v5(self):
        """借用一次真实读取把 v4 存档迁到 v5，拿到一份带合法 soil 的基线。"""
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            garden.crop_snapshot(now=self.now, path=self.path)

    def _read_raw(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write_raw(self, raw):
        self.path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def _reread_with_real_environment(self):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            return garden._read_state_unlocked(self.path, now=self.now)

    def test_negative_produce_poor_count_is_rejected(self):
        self._migrate_to_v5()
        raw = self._read_raw()
        raw["inventory"]["produce_poor"] = {"cucumber": -1}
        self._write_raw(raw)
        with self.assertRaises(garden.GardenError):
            self._reread_with_real_environment()

    def test_non_integer_produce_poor_count_is_rejected(self):
        self._migrate_to_v5()
        raw = self._read_raw()
        raw["inventory"]["produce_poor"] = {"cucumber": 1.5}
        self._write_raw(raw)
        with self.assertRaises(garden.GardenError):
            self._reread_with_real_environment()

    def test_unknown_plot_quality_value_is_rejected(self):
        self._migrate_to_v5()
        raw = self._read_raw()
        raw["plots"][0]["quality"] = "excellent"
        self._write_raw(raw)
        with self.assertRaises(garden.GardenError):
            self._reread_with_real_environment()

    def test_quality_poor_on_a_growing_plot_is_accepted(self):
        self._migrate_to_v5()
        raw = self._read_raw()
        raw["plots"][0]["quality"] = "poor"
        self._write_raw(raw)
        normalized = self._reread_with_real_environment()
        self.assertEqual(normalized["plots"][0]["quality"], "poor")

    def test_quality_left_on_an_emptied_plot_is_rejected(self):
        self._migrate_to_v5()
        raw = self._read_raw()
        raw["plots"][0] = {"plot_id": "p1", "status": "empty", "quality": "poor"}
        self._write_raw(raw)
        with self.assertRaises(garden.GardenError):
            self._reread_with_real_environment()

    def test_flood_watch_not_a_dict_is_rejected(self):
        self._migrate_to_v5()
        raw = self._read_raw()
        raw["environment"]["flood_watch"] = ["since", None]
        self._write_raw(raw)
        with self.assertRaises(garden.GardenError):
            self._reread_with_real_environment()

    def test_flood_watch_missing_entirely_is_backfilled_not_rejected(self):
        # 缺失整个字段是"老存档没赶上这个字段"，走迁移补默认值，不是损坏——
        # 与下面"存在但形状不对"的拒绝语义刻意区分开。
        self._migrate_to_v5()
        raw = self._read_raw()
        del raw["environment"]["flood_watch"]
        self._write_raw(raw)
        normalized = self._reread_with_real_environment()
        self.assertEqual(
            normalized["environment"]["flood_watch"],
            {"since": None, "drained_at": None},
        )

    def test_flood_watch_unparseable_since_is_rejected(self):
        self._migrate_to_v5()
        raw = self._read_raw()
        raw["environment"]["flood_watch"] = {"since": "not-a-time", "drained_at": None}
        self._write_raw(raw)
        with self.assertRaises(garden.GardenError):
            self._reread_with_real_environment()

    def test_flood_watch_valid_since_and_drained_at_round_trip(self):
        self._migrate_to_v5()
        raw = self._read_raw()
        raw["environment"]["flood_watch"] = {
            "since": self.now.isoformat(),
            "drained_at": (self.now + timedelta(hours=1)).isoformat(),
        }
        self._write_raw(raw)
        normalized = self._reread_with_real_environment()
        self.assertEqual(
            normalized["environment"]["flood_watch"],
            {
                "since": self.now.isoformat(),
                "drained_at": (self.now + timedelta(hours=1)).isoformat(),
            },
        )


class ResolveConditionRejectsFloodRotGracefullyTests(unittest.TestCase):
    """flood_rot 只以终态出现；resolve_crop_condition 不炸、拒绝语义合理。"""

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
        state["plots"][0].update({
            "crop_id": "tomato",
            "planted_at": (self.now - timedelta(days=1)).isoformat(),
            "last_settled_at": self.now.isoformat(),
            "growth_points": 1.0,
            "stage": "sprout",
            "water_bonus_dates": [],
            "ready_at": None,
            "status": "growing",
            "cycle_id": "cycle-1",
            "stage_events_seen": [],
        })
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def _raw(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_a_flood_rot_labeled_withered_plot_is_reported_as_withered_not_crashed(self):
        # 借用既有自然异常状态机把一块地正常走到 withered/failed 终态
        # （拿到一份内部时间戳全部自洽的记录），再把类型换成 flood_rot——
        # 阶段 A 不实现"内涝直接创建终态记录"的结算逻辑（那是阶段 B），
        # 这里只验证：一旦真的出现 flood_rot 类型的 withered 记录，
        # resolve_crop_condition 不会因为 CONDITION_ACTIONS 缺项而崩溃。
        # 阶段 B 监理裁决给 flood_rot 加了强制的 cause="yard_flood" 与
        # worsened_at==failed_at 时间自洽要求（设计稿第四节第4点），这里
        # 借用的自然恶化记录原本 worsened_at 停在 24h 而非 36h，一并对齐。
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            event = garden.run_tick(
                self.now, rng=_StubRng([0.05], choice_index=0), path=self.path,
            )
            garden.confirm_pending_event(
                event["event_id"], event["delivery_token"],
                now=self.now, path=self.path,
            )
        failed_at = self.now + timedelta(hours=36)
        garden.crop_snapshot(now=failed_at, path=self.path)
        raw = self._raw()
        self.assertEqual(raw["plots"][0]["status"], "withered")
        condition_id = raw["plots"][0]["condition"]["condition_id"]
        raw["plots"][0]["condition"]["type"] = "flood_rot"
        raw["plots"][0]["condition"]["cause"] = "yard_flood"
        raw["plots"][0]["condition"]["worsened_at"] = raw["plots"][0]["condition"]["failed_at"]
        raw["journal"]["crop_incidents"][0]["type"] = "flood_rot"
        for event in raw["pending_events"]:
            if event.get("condition_id") == condition_id:
                event["condition_type"] = "flood_rot"
        self.path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        result = garden.resolve_crop_condition(
            "除虫", "p1", now=failed_at, path=self.path,
        )
        self.assertEqual(result["outcome"], "withered")
        self.assertEqual(result["correct_action"], "清理")
        self.assertEqual(result["condition_type"], "flood_rot")


if __name__ == "__main__":
    unittest.main()
