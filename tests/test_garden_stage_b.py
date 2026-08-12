import itertools
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
import home


TZ = ZoneInfo("Asia/Shanghai")


class GardenEnvironmentStageBTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 29, 8, 0, tzinfo=TZ)
        self.environment = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.observations = []
        loader = patch.object(garden, "_environment_observations", side_effect=lambda _now: list(self.observations))
        loader.start()
        self.addCleanup(loader.stop)

    def observation(self, at, *, city="101190205", temp=35, feels=39, humidity=35, wind="3", precip=0, text="晴"):
        value = garden_weather.normalize_observation(
            location_id=city, location_name="南京", observed_time=at.isoformat(), received_at=at,
            temp=temp, feels_like=feels, humidity=humidity, wind_scale=wind,
            precip=precip, condition_text=text,
        )
        self.assertIsNotNone(value)
        return value

    def make_v5_crop(self):
        garden.crop_snapshot(now=self.now, path=self.path)
        garden.plant_crop("小番茄", "1", now=self.now, path=self.path)
        garden.crop_snapshot(now=self.now + timedelta(seconds=1), path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 5)
        return raw

    def set_moisture(self, value):
        """把土壤直接拍板到指定水分：同时改锚点，否则下次结算会用旧锚点
        重放，把这次强制设置的值当场覆盖掉。"""
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        soil = raw["plots"][0]["soil"]
        soil["moisture"] = value
        soil["anchor_moisture"] = value
        soil["anchor_at"] = soil["settled_at"]
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    def test_v4_migration_is_neutral_and_keeps_unknown_fields(self):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "0"}):
            garden.crop_snapshot(now=self.now, path=self.path)
            garden.plant_crop("小番茄", "1", now=self.now, path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["unrecognized_v4"] = {"keep": True}
        raw["plots"][0]["custom_plot_field"] = "keep"
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        snapshot = garden.crop_snapshot(now=self.now + timedelta(hours=1), path=self.path)
        migrated = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["version"], 5)
        self.assertEqual(migrated["plots"][0]["soil"]["moisture"], 55.0)
        self.assertEqual(migrated["plots"][0]["custom_plot_field"], "keep")
        self.assertEqual(migrated["legacy"]["unrecognized_top_level"]["unrecognized_v4"], {"keep": True})
        self.assertEqual(snapshot["environment"]["status"], "missing")

    def test_hot_clear_weather_can_make_second_same_day_watering_effective(self):
        self.observations = [self.observation(self.now + timedelta(hours=index)) for index in range(9)]
        self.make_v5_crop()
        self.set_moisture(45)
        morning = garden.water_crop("1", now=self.now, path=self.path)
        self.assertEqual(morning["outcome"], "watered")
        afternoon = self.now + timedelta(hours=8)
        snapshot = garden.crop_snapshot(now=afternoon, path=self.path)
        self.assertLess(snapshot["plots"][0]["soil"]["moisture"], 50)
        second = garden.water_crop("1", now=afternoon, path=self.path)
        self.assertEqual(second["outcome"], "watered")
        self.assertEqual(second["moisture_after"], 80.0)

    def test_rain_is_not_manual_water_and_first_mistake_is_safe_noop(self):
        self.observations = [
            self.observation(self.now, temp=22, feels=22, humidity=80, wind="1", text="多云"),
            self.observation(self.now + timedelta(hours=1), temp=22, feels=22, humidity=90, wind="1", precip=3, text="小雨"),
            self.observation(self.now + timedelta(hours=2), temp=22, feels=22, humidity=90, wind="1", precip=3, text="小雨"),
        ]
        self.make_v5_crop()
        snapshot = garden.crop_snapshot(now=self.now + timedelta(hours=2), path=self.path)
        soil = snapshot["plots"][0]["soil"]
        self.assertGreaterEqual(soil["moisture"], 50)
        result = garden.water_crop("1", now=self.now + timedelta(hours=2), path=self.path)
        self.assertIn(result["outcome"], ("no_need", "too_wet"))
        self.assertEqual(result["moisture_before"], result["moisture_after"])

    def test_unneeded_watering_keeps_noop_protest_waterlogged_refusal_sequence(self):
        self.make_v5_crop()
        self.set_moisture(70)
        outcomes = [garden.water_crop("1", now=self.now, path=self.path, rng=__import__("random")) ["outcome"] for _ in range(4)]
        self.assertEqual(outcomes, ["too_wet", "protest", "waterlogged", "refused"])
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["plots"][0]["soil"]["moisture"], 100.0)

    def test_same_time_replay_does_not_repeat_evaporation_or_rain(self):
        self.observations = [self.observation(self.now + timedelta(hours=index)) for index in range(3)]
        self.make_v5_crop()
        self.set_moisture(80)
        moment = self.now + timedelta(hours=2)
        first = garden.crop_snapshot(now=moment, path=self.path)["plots"][0]["soil"]
        second = garden.crop_snapshot(now=moment, path=self.path)["plots"][0]["soil"]
        self.assertEqual(first["moisture"], second["moisture"])
        self.assertEqual(first["exposure_by_date"], second["exposure_by_date"])

    def test_single_hour_rain_window_is_not_multiplied_across_a_sampling_gap(self):
        self.observations = [
            self.observation(self.now, temp=22, feels=22, humidity=70, wind="1", text="多云"),
            self.observation(self.now + timedelta(hours=6), temp=22, feels=22, humidity=90, wind="1", precip=3, text="小雨"),
        ]
        self.make_v5_crop()
        snapshot = garden.crop_snapshot(now=self.now + timedelta(hours=6), path=self.path)
        exposure = snapshot["plots"][0]["soil"]["exposure_by_date"][self.now.date().isoformat()]
        self.assertEqual(exposure["rain_mm"], 3.0)

    def test_missing_weather_uses_neutral_floor_instead_of_manufacturing_drought(self):
        self.observations = [self.observation(self.now, temp=22, feels=22, humidity=70, wind="1", text="多云")]
        self.make_v5_crop()
        self.set_moisture(55)
        later = garden.crop_snapshot(now=self.now + timedelta(days=20), path=self.path)
        soil = later["plots"][0]["soil"]
        self.assertGreaterEqual(soil["moisture"], 25)
        self.assertEqual(later["environment"]["status"], "missing")

    def test_late_fresh_rain_observation_is_booked_once_after_cursor(self):
        self.observations = [self.observation(self.now, temp=22, feels=22, humidity=70, wind="1", text="多云")]
        self.make_v5_crop()
        garden.crop_snapshot(now=self.now + timedelta(hours=2), path=self.path)
        late = self.observation(self.now + timedelta(hours=1, minutes=55), temp=22, feels=22, humidity=90, wind="1", precip=3, text="小雨")
        self.observations.append(late)
        first = garden.crop_snapshot(now=self.now + timedelta(hours=2, minutes=5), path=self.path)["plots"][0]["soil"]
        second = garden.crop_snapshot(now=self.now + timedelta(hours=2, minutes=5), path=self.path)["plots"][0]["soil"]
        self.assertGreaterEqual(first["moisture"], 70)
        self.assertEqual(first["moisture"], second["moisture"])

    def test_missing_weather_never_lifts_already_dry_soil(self):
        self.observations = [self.observation(self.now, temp=20, feels=20, humidity=50, wind="1", text="多云")]
        self.make_v5_crop()
        self.set_moisture(20)
        soil = garden.crop_snapshot(now=self.now + timedelta(hours=4), path=self.path)["plots"][0]["soil"]
        self.assertLessEqual(soil["moisture"], 20)
        self.assertNotEqual(soil["moisture"], 25)

    def test_overlapping_late_hourly_rain_windows_do_not_double_count(self):
        # 两份过去一小时报告窗口重叠 30 分钟（[T0,T0+1h] 与 [T0+30m,T0+1h30]）。
        # 规范模型：较新观测对重叠段更权威，因此总量 = 较早观测独占的
        # [T0,T0+30m]（0.5h@3mm/h=1.5mm）+ 较新观测独占其完整窗口
        # [T0+30m,T0+1h30]（1h@3mm/h=3mm）=4.5mm；不是简单认定“同速率就等于
        # 不重复”，也不是旧实现里因边界切法把 [T0,T0+30m] 直接丢弃得到的 3mm。
        self.observations = [self.observation(self.now, temp=22, feels=22, humidity=70, wind="1", text="多云")]
        self.make_v5_crop()
        garden.crop_snapshot(now=self.now + timedelta(hours=2), path=self.path)
        self.observations.extend([
            self.observation(self.now + timedelta(hours=1), temp=22, feels=22, humidity=90, wind="1", precip=3, text="小雨"),
            self.observation(self.now + timedelta(hours=1, minutes=30), temp=22, feels=22, humidity=90, wind="1", precip=3, text="小雨"),
        ])
        soil = garden.crop_snapshot(now=self.now + timedelta(hours=2, minutes=5), path=self.path)["plots"][0]["soil"]
        exposure = soil["exposure_by_date"][self.now.date().isoformat()]
        self.assertEqual(exposure["rain_mm"], 4.5)

    def test_late_overlapping_windows_match_ordered_settlement_when_rates_change(self):
        # 同一对重叠窗口，速率不同（4 then 2）。规范模型：较早观测独占
        # [T0,T0+30m]（0.5h@4mm/h=2mm）+ 重叠段 [T0+30m,T0+1h] 由较新观测
        # 接管（0.5h@2mm/h=1mm）+ 较新观测独占尾段 [T0+1h,T0+1h30]
        # （0.5h@2mm/h=1mm）=4mm。
        self.observations = [self.observation(self.now, temp=22, feels=22, humidity=70, wind="1", text="多云")]
        self.make_v5_crop()
        garden.crop_snapshot(now=self.now + timedelta(hours=2), path=self.path)
        self.observations.extend([
            self.observation(self.now + timedelta(hours=1), temp=22, feels=22, humidity=90, wind="1", precip=4, text="小雨"),
            self.observation(self.now + timedelta(hours=1, minutes=30), temp=22, feels=22, humidity=90, wind="1", precip=2, text="小雨"),
        ])
        soil = garden.crop_snapshot(now=self.now + timedelta(hours=2, minutes=5), path=self.path)["plots"][0]["soil"]
        exposure = soil["exposure_by_date"][self.now.date().isoformat()]
        self.assertEqual(exposure["rain_mm"], 4.0)

    def test_crossing_below_water_line_before_rain_clears_unneeded_counter(self):
        self.observations = [
            self.observation(self.now),
            self.observation(self.now + timedelta(hours=1), precip=1, text="小雨"),
        ]
        self.make_v5_crop()
        self.set_moisture(51)
        self.assertEqual(garden.water_crop("1", now=self.now, path=self.path)["outcome"], "no_need")
        garden.crop_snapshot(now=self.now + timedelta(hours=1), path=self.path)
        self.assertEqual(garden.water_crop("1", now=self.now + timedelta(hours=1), path=self.path)["outcome"], "no_need")

    def test_day_night_boundary_uses_both_evaporation_rates(self):
        start = self.now.replace(hour=19)
        self.observations = [
            self.observation(start, temp=20, feels=20, humidity=50, wind="1", text="晴"),
            self.observation(start + timedelta(hours=1), temp=20, feels=20, humidity=50, wind="1", text="晴"),
            self.observation(start + timedelta(hours=2), temp=20, feels=20, humidity=50, wind="1", text="晴"),
        ]
        self.now = start
        self.make_v5_crop()
        self.set_moisture(80)
        soil = garden.crop_snapshot(now=start + timedelta(hours=2), path=self.path)["plots"][0]["soil"]
        self.assertAlmostEqual(soil["moisture"], 78.15, places=3)

    def test_exposure_across_midnight_splits_one_hour_to_each_day(self):
        # Sol 复审阻断①：replay 曾按 seg_end 归日，把 23:00→01:00 的两小时
        # 整段记到次日；正确应为前一天、后一天各 1 小时。
        start = self.now.replace(hour=23)
        self.observations = [self.observation(start, temp=22, feels=22, humidity=60, wind="1", text="多云")]
        self.now = start
        self.make_v5_crop()
        snapshot = garden.crop_snapshot(now=start + timedelta(hours=2), path=self.path)
        exposure = snapshot["plots"][0]["soil"]["exposure_by_date"]
        first_day = start.date().isoformat()
        second_day = (start.date() + timedelta(days=1)).isoformat()
        self.assertAlmostEqual(exposure[first_day]["known_hours"], 1.0, places=6)
        self.assertAlmostEqual(exposure[second_day]["known_hours"], 1.0, places=6)

    def test_exposure_before_checkpoint_is_not_lost(self):
        # Sol 复审阻断②：_checkpoint_soil 曾只推进水分锚点，没有先把仍处于
        # 尾段的 exposure_by_date 折叠固化；浇水前约 2 小时暴露会在下一次
        # 刷新后凭空消失，只剩浇水后新产生的那部分。
        self.observations = [
            self.observation(self.now + timedelta(hours=index), temp=35, feels=35, humidity=40, wind="2", text="晴")
            for index in range(3)
        ]
        self.make_v5_crop()
        self.set_moisture(45)
        water_at = self.now + timedelta(hours=2)
        result = garden.water_crop("1", now=water_at, path=self.path)
        self.assertEqual(result["outcome"], "watered")
        known_right_after = garden.crop_snapshot(now=water_at, path=self.path)["plots"][0]["soil"]["exposure_by_date"][self.now.date().isoformat()]["known_hours"]
        self.assertGreaterEqual(known_right_after, 1.999)
        later = garden.crop_snapshot(now=water_at + timedelta(minutes=5), path=self.path)
        known_later = later["plots"][0]["soil"]["exposure_by_date"][self.now.date().isoformat()]["known_hours"]
        # 浇水前那 2 小时暴露必须仍然算在总数里，而不是被浇水后新产生的
        # 5 分钟悄悄取代。
        self.assertGreaterEqual(known_later, known_right_after)

    def test_rain_fact_revoked_by_corrected_dry_observation_is_not_stuck(self):
        # Sol 复审阻断③（前半）：尾段天气派生状态只会正向写入。先看到降雨
        # 写 last_rain_at=rain，之后如果同一时刻被更正为干燥观测，尾段纯
        # 重放本身已经不再认为下过雨，持久状态却仍卡在旧的雨。
        self.make_v5_crop()
        rain_at = self.now + timedelta(hours=1)
        self.observations = [self.observation(rain_at, temp=22, feels=22, humidity=90, wind="1", precip=3, text="小雨")]
        with_rain = garden.crop_snapshot(now=rain_at, path=self.path)["plots"][0]["soil"]
        self.assertIsNotNone(with_rain["last_rain_at"])
        self.assertEqual(with_rain["last_water_source"], "rain")
        # 更正：同一次观测被替换为干燥读数（例如供应商纠错），不再有任何降雨。
        self.observations = [self.observation(rain_at, temp=22, feels=22, humidity=60, wind="1", precip=0, text="多云")]
        corrected = garden.crop_snapshot(now=rain_at + timedelta(seconds=1), path=self.path)["plots"][0]["soil"]
        self.assertIsNone(corrected["last_rain_at"])
        self.assertIsNone(corrected["last_water_source"])

    def test_unneeded_watering_count_survives_a_transient_dip_that_gets_corrected(self):
        # Sol 复审阻断③（后半，第一轮）：尾段判定“今天曾跌破 50”曾直接
        # pop 掉 unneeded_watering_by_date 里的真实计数；如果后来更准确的
        # 观测把这天重新算成从未跌破 50，原计数已经永久丢失、无法恢复。
        # 现在真实意图按精确时刻记在 unneeded_watering_events 里，跌破只
        # 影响“清零线”落在哪一刻，不再删除任何历史意图。
        self.make_v5_crop()
        self.set_moisture(70)
        self.assertEqual(garden.water_crop("1", now=self.now, path=self.path)["outcome"], "too_wet")
        self.assertEqual(garden.water_crop("1", now=self.now, path=self.path)["outcome"], "protest")
        day = self.now.date().isoformat()
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["plots"][0]["unneeded_watering_by_date"], {day: 2})
        self.assertEqual(len(raw["plots"][0]["unneeded_watering_events"][day]), 2)

        # 一段持续高温干燥的观测，让土壤在结算窗口内真实跌到 50 以下：清零线
        # 落在跌破那一刻，这两条旧意图从此不再计入有效计数——但底层事件
        # 列表本身完全没有被删除，只是暂时“在清零线之前”。
        hot = [self.observation(self.now + timedelta(hours=i), temp=40, feels=40, humidity=10, wind="4", text="晴") for i in range(9)]
        self.observations = hot
        dipped = garden.crop_snapshot(now=self.now + timedelta(hours=6), path=self.path)["plots"][0]["soil"]
        self.assertLess(dipped["moisture"], 50)
        self.assertIsNotNone(dipped["tail_resolved_dip_at"])
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["plots"][0]["unneeded_watering_by_date"], {day: 0})
        self.assertEqual(len(raw["plots"][0]["unneeded_watering_events"][day]), 2)

        # 更准确的观测（同一天全程温和），纯重放证明这一天其实从未跌破 50；
        # 清零线消失，两条旧意图立刻重新生效，展示计数变回 2。
        mild = [self.observation(self.now + timedelta(hours=i), temp=22, feels=22, humidity=60, wind="1", text="多云") for i in range(9)]
        self.observations = mild
        corrected_at = self.now + timedelta(hours=6, seconds=1)
        corrected = garden.crop_snapshot(now=corrected_at, path=self.path)["plots"][0]["soil"]
        self.assertGreaterEqual(corrected["moisture"], 50)
        self.assertIsNone(corrected["tail_resolved_dip_at"])
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["plots"][0]["unneeded_watering_by_date"], {day: 2})
        self.assertEqual(len(raw["plots"][0]["unneeded_watering_events"][day]), 2)

        # 原计数被完整保留：第三次无必要浇水意图必须是积水，不能从头再来一遍。
        result = garden.water_crop("1", now=corrected_at, path=self.path)
        self.assertEqual(result["outcome"], "waterlogged")

    def test_unneeded_watering_progresses_normally_after_a_dip_clears_old_intents(self):
        # Sol 复审阻断①：清零曾是整日布尔标记，只要今天跌破过一次，同一天
        # 后续每一次无必要浇水都被当成第一次（no_need → no_need）。跌破之后
        # 的连续意图必须正常递增（no_need → protest），不能永远卡在“视为
        # 刚清零”。
        self.observations = [
            self.observation(self.now, temp=35, feels=39, humidity=35, wind="3", text="晴"),
            self.observation(self.now + timedelta(hours=1), precip=1, temp=22, feels=22, humidity=90, wind="1", text="小雨"),
        ]
        self.make_v5_crop()
        self.set_moisture(51)
        self.assertEqual(garden.water_crop("1", now=self.now, path=self.path)["outcome"], "no_need")
        garden.crop_snapshot(now=self.now + timedelta(hours=1, minutes=5), path=self.path)
        first_after_dip = garden.water_crop("1", now=self.now + timedelta(hours=1, minutes=10), path=self.path)
        second_after_dip = garden.water_crop("1", now=self.now + timedelta(hours=1, minutes=15), path=self.path)
        self.assertEqual(first_after_dip["outcome"], "no_need")
        self.assertEqual(second_after_dip["outcome"], "protest")

    def test_city_switch_checkpoint_syncs_live_projection_immediately(self):
        # Sol 复审阻断②：城市切换只把旧城市尾段折进永久账本就直接返回，
        # 没有像浇水那样同步刷新实时投影；永久暴露已经是 2 小时，立即返回
        # 的 exposure_by_date 却仍停在 1 小时，要等下一次结算才会自愈。
        self.observations = [self.observation(self.now)]
        self.make_v5_crop()
        self.observations.append(self.observation(self.now + timedelta(hours=1), city="101190401"))
        snapshot = garden.crop_snapshot(now=self.now + timedelta(hours=2), path=self.path)
        soil = snapshot["plots"][0]["soil"]
        exposure = soil["exposure_by_date"][self.now.date().isoformat()]
        self.assertAlmostEqual(exposure["known_hours"], 2.0, places=6)
        # 落盘的立即返回值必须和永久账本完全一致，不需要再等一次结算。
        raw = json.loads(self.path.read_text())["plots"][0]["soil"]
        self.assertEqual(raw["exposure_by_date"], raw["exposure_finalized_by_date"])

    def test_legacy_v5_state_missing_newer_soil_fields_is_backfilled_not_rejected(self):
        # Sol 复审阻断③：v5 内部字段这几轮一直在增补，缺新字段的旧 v5
        # 存档（例如 1b7c843 落盘的形状）不该被判定损坏；生产开关目前仍
        # 关闭，真实数据不受影响，但同版本旧形状必须能被继续读取。
        self.make_v5_crop()
        raw = json.loads(self.path.read_text())
        soil = raw["plots"][0]["soil"]
        del soil["anchor_at"]
        del soil["anchor_moisture"]
        del soil["exposure_finalized_by_date"]
        del soil["finalized_last_rain_at"]
        del soil["finalized_last_dip_below_50_at"]
        del soil["tail_resolved_dip_at"]
        del raw["plots"][0]["unneeded_watering_events"]
        raw["plots"][0]["unneeded_watering_by_date"] = {self.now.date().isoformat(): 2}
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        snapshot = garden.crop_snapshot(now=self.now + timedelta(hours=1), path=self.path)
        soil_after = snapshot["plots"][0]["soil"]
        self.assertIn("anchor_at", soil_after)
        self.assertIn("anchor_moisture", soil_after)
        # 旧计数原样保留，补齐不能凭空丢掉此前已经真实发生过的意图。
        events = json.loads(self.path.read_text())["plots"][0]["unneeded_watering_events"]
        self.assertEqual(len(events[self.now.date().isoformat()]), 2)

    def test_intent_at_the_exact_settle_instant_still_counts_as_attempt_one(self):
        # Sol 第四轮复审阻断①（后半）：浇水入口总是先结算天气、再记录用户
        # 意图，两者可能落在同一时刻；严格 “>” 比较会把这条意图误判成
        # “跌破之前”而排除，产出非法的 attempt=0。
        self.observations = [
            self.observation(self.now, temp=15, feels=15, humidity=60, wind="1", text="多云"),
            self.observation(self.now + timedelta(hours=1), precip=2, temp=15, feels=15, humidity=90, wind="1", text="小雨"),
        ]
        self.make_v5_crop()
        self.set_moisture(50.5)
        result = garden.water_crop("1", now=self.now + timedelta(hours=1), path=self.path)
        self.assertEqual(result["attempt"], 1)
        self.assertIn(result["outcome"], ("no_need", "too_wet"))

    def test_unneeded_watering_events_are_pruned_to_eight_days(self):
        # Sol 第四轮复审阻断②：unneeded_watering_events 校验最多 8 个日期，
        # 但没有裁剪路径；第 9 天写出的真实存档会被下一次读取判成损坏。
        self.make_v5_crop()
        for day in range(10):
            now = self.now + timedelta(days=day)
            raw = json.loads(self.path.read_text())
            plot = raw["plots"][0]
            plot.update({"status": "growing", "stage": "seed", "growth_points": 0.0, "last_settled_at": now.isoformat()})
            soil = plot["soil"]
            soil.update({
                "moisture": 70.0, "anchor_moisture": 70.0,
                "anchor_at": now.isoformat(), "settled_at": now.isoformat(),
            })
            self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            garden.water_crop("1", now=now, path=self.path)
        events = json.loads(self.path.read_text())["plots"][0]["unneeded_watering_events"]
        self.assertLessEqual(len(events), 8)
        # 关键：这份自己写出来的存档，下一次读取不能把自己判成损坏。
        garden.crop_snapshot(now=self.now + timedelta(days=10), path=self.path)

    def test_backfill_does_not_freeze_a_still_revisable_tail_rain_fact(self):
        # Sol 第四轮复审阻断③：真实 1b7c843 形状本来就有 anchor_at/
        # anchor_moisture/exposure_finalized_by_date，只缺后几轮才加的字段；
        # 这种状态里 last_rain_at 如果还落在锚点之后（仍是尾段结论），补齐
        # 逻辑曾经无条件把它升级成永久事实，导致后续干燥纠正再也撤销不了。
        rain_at = self.now + timedelta(minutes=30)
        self.observations = [self.observation(rain_at, precip=3, temp=22, feels=22, humidity=90, wind="1", text="小雨")]
        self.make_v5_crop()
        snapshot = garden.crop_snapshot(now=self.now + timedelta(hours=1), path=self.path)
        soil = snapshot["plots"][0]["soil"]
        self.assertIsNotNone(soil["last_rain_at"])
        self.assertGreater(
            datetime.fromisoformat(soil["last_rain_at"]), datetime.fromisoformat(soil["anchor_at"]),
        )
        # 模拟真实 1b7c843 形状：保留锚点，只删掉更晚几轮才新增的字段。
        raw = json.loads(self.path.read_text())
        plot_soil = raw["plots"][0]["soil"]
        del plot_soil["finalized_last_rain_at"]
        del plot_soil["finalized_last_dip_below_50_at"]
        del plot_soil["tail_resolved_dip_at"]
        del raw["plots"][0]["unneeded_watering_events"]
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        # 更准确的干燥纠正：这次雨其实没有发生。
        self.observations = [self.observation(rain_at, precip=0, temp=22, feels=22, humidity=60, wind="1", text="多云")]
        corrected = garden.crop_snapshot(now=self.now + timedelta(hours=1, seconds=1), path=self.path)
        self.assertIsNone(corrected["plots"][0]["soil"]["last_rain_at"])

    def test_switch_off_restores_v4_watering_rule_for_existing_v5_state(self):
        self.make_v5_crop()
        self.set_moisture(70)
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "0"}):
            result = garden.water_crop("1", now=self.now, path=self.path)
        self.assertEqual(result["outcome"], "watered")

    def test_city_switch_keeps_stale_observation_stale(self):
        self.observations = [self.observation(self.now)]
        self.make_v5_crop()
        self.observations.append(self.observation(self.now + timedelta(hours=1), city="101190401"))
        snapshot = garden.crop_snapshot(now=self.now + timedelta(hours=4), path=self.path)
        environment = snapshot["environment"]
        self.assertEqual(environment["location_id"], "101190401")
        self.assertEqual(environment["status"], "stale")
        self.assertIsNone(environment["last_fresh_weather_at"])

    def test_batch_noop_does_not_claim_water_was_poured(self):
        output = home._garden_water_batch_text([{
            "plot": {"plot_id": "p1", "crop_id": "tomato"},
            "outcome": "too_wet", "condition_type": None,
        }])
        self.assertTrue(output.startswith("选中的菜畦这次都没有倒水。"))


def _pure_observation(at, *, city="101190205", precip=0, text="多云", temp=22, feels=22, humidity=70, wind="1"):
    value = garden_weather.normalize_observation(
        location_id=city, location_name="南京", observed_time=at.isoformat(), received_at=at,
        temp=temp, feels_like=feels, humidity=humidity, wind_scale=wind,
        precip=precip, condition_text=text,
    )
    assert value is not None
    return value


class GardenRainReplayPropertyTests(unittest.TestCase):
    """Sol 复审要求的性质/矩阵测试：直接证明降雨结算与观测抵达的顺序、
    批次、调用节奏无关，且同一份观测集合总能收敛到唯一结果。"""

    def setUp(self):
        self.t0 = datetime(2026, 7, 29, 8, 0, tzinfo=TZ)

    def test_pure_replay_is_order_independent_across_permutations(self):
        # 两段互相重叠、速率不同的滑动一小时窗口（4mm/h 与 2mm/h），外加一头
        # 一尾两条零降水观测。手算规范结果：B 独占 [T0,T0+30m]=2mm；
        # 重叠段 [T0+30m,T0+1h] 由更新的 C 接管=1mm；[T0+1h,T0+2h] 全部被
        # 更晚的 D（零降水）接管=0mm；合计 3mm，last_rain_at 落在 T0+1h。
        observations = [
            _pure_observation(self.t0, precip=0, text="多云"),
            _pure_observation(self.t0 + timedelta(hours=1), precip=4, text="小雨"),
            _pure_observation(self.t0 + timedelta(hours=1, minutes=30), precip=2, text="小雨"),
            _pure_observation(self.t0 + timedelta(hours=2), precip=0, text="多云"),
        ]
        results = []
        for ordering in itertools.permutations(observations):
            result = garden_weather.replay(60.0, self.t0, self.t0 + timedelta(hours=2), list(ordering))
            total_rain = sum(bucket["rain_mm"] for bucket in result.exposure_by_day.values())
            results.append((round(result.moisture, 6), round(total_rain, 6), result.last_rain_at))
        self.assertEqual(len(set(results)), 1, results)
        moisture, total_rain, last_rain_at = results[0]
        self.assertEqual(total_rain, 3.0)
        self.assertEqual(last_rain_at, self.t0 + timedelta(hours=1))
        # rain_mm、水分与 last_rain_at 三者一致：既然确实结算到降水，水分理应
        # 高于“完全没有降水、只有蒸发”的对照值，且 last_rain_at 必须存在。
        dry_only = garden_weather.replay(
            60.0, self.t0, self.t0 + timedelta(hours=2),
            [_pure_observation(self.t0, precip=0, text="多云")],
        )
        self.assertGreater(moisture, dry_only.moisture)
        self.assertIsNotNone(last_rain_at)

    def test_idempotent_repeated_replay_over_the_same_window(self):
        observations = [
            _pure_observation(self.t0, precip=0, text="多云"),
            _pure_observation(self.t0 + timedelta(hours=1), precip=3, text="小雨"),
        ]
        end = self.t0 + timedelta(hours=3)
        first = garden_weather.replay(70.0, self.t0, end, observations)
        for _ in range(4):
            again = garden_weather.replay(70.0, self.t0, end, observations)
            self.assertEqual(again.moisture, first.moisture)
            self.assertEqual(again.exposure_by_day, first.exposure_by_day)
            self.assertEqual(again.last_rain_at, first.last_rain_at)


class GardenEnvironmentDeliveryScheduleTests(unittest.TestCase):
    """在 garden.py 层面证明：观测一次性出现、分批出现、按时或较旧观测
    后到，只要最终集合相同，最终土壤状态必须相同——不依赖内部结算调用
    发生在哪些中间时刻。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.now = datetime(2026, 7, 29, 8, 0, tzinfo=TZ)
        self.environment = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.observations: list[dict] = []
        self.loader = patch.object(garden, "_environment_observations", side_effect=lambda _now: list(self.observations))
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def _fresh_garden(self, name):
        path = Path(self.tempdir.name) / name
        garden.crop_snapshot(now=self.now, path=path)
        garden.plant_crop("小番茄", "1", now=self.now, path=path)
        garden.crop_snapshot(now=self.now + timedelta(seconds=1), path=path)
        return path

    def _run_schedule(self, name, deliveries):
        """``deliveries`` 是 (可见观测子集, 结算时刻) 的顺序列表。"""
        path = self._fresh_garden(name)
        snapshot = None
        for subset, at in deliveries:
            self.observations = subset
            snapshot = garden.crop_snapshot(now=at, path=path)
        soil = snapshot["plots"][0]["soil"]
        return {
            "moisture": soil["moisture"],
            "rain_mm": sum(bucket["rain_mm"] for bucket in soil["exposure_by_date"].values()),
            "last_rain_at": soil["last_rain_at"],
            "last_water_source": soil["last_water_source"],
        }

    def test_batch_and_arrival_order_do_not_change_final_soil_state(self):
        obs1 = _pure_observation(self.now, precip=0, text="多云")
        obs2 = _pure_observation(self.now + timedelta(hours=1), precip=4, text="小雨", humidity=90)
        obs3 = _pure_observation(self.now + timedelta(hours=1, minutes=30), precip=2, text="小雨", humidity=90)
        final_at = self.now + timedelta(hours=2)

        schedules = {
            "all_at_once": [([obs1, obs2, obs3], final_at)],
            "progressive_in_order": [
                ([obs1], self.now + timedelta(minutes=10)),
                ([obs1, obs2], self.now + timedelta(hours=1, minutes=5)),
                ([obs1, obs2, obs3], final_at),
            ],
            "older_observation_arrives_late": [
                ([obs1, obs3], self.now + timedelta(hours=1, minutes=35)),
                ([obs1, obs2, obs3], final_at),
            ],
            "repeated_entry_before_final_read": [
                ([obs1, obs2, obs3], self.now + timedelta(hours=1, minutes=45)),
                ([obs1, obs2, obs3], final_at),
                ([obs1, obs2, obs3], final_at),
            ],
        }
        outcomes = {name: self._run_schedule(name, steps) for name, steps in schedules.items()}
        reference = outcomes["all_at_once"]
        for name, outcome in outcomes.items():
            self.assertEqual(outcome, reference, f"schedule {name} diverged from all_at_once")
        self.assertEqual(reference["rain_mm"], 4.0)
        self.assertEqual(reference["last_water_source"], "rain")

    def test_settlement_cadence_does_not_change_total_rain(self):
        # 同一段 26 小时、跨越多次午夜与 8/20 蒸发边界的历史，一次结算到底
        # 和沿途多次结算（迫使折叠边界落在不同时刻），总降雨与终值必须一致。
        obs1 = _pure_observation(self.now, precip=0, text="多云")
        obs2 = _pure_observation(self.now + timedelta(hours=1), precip=5, text="小雨", humidity=90)
        final_at = self.now + timedelta(hours=26)
        self.observations = [obs1, obs2]

        single_call = self._run_schedule("single_call", [([obs1, obs2], final_at)])
        many_calls = self._run_schedule("many_calls", [
            ([obs1, obs2], self.now + timedelta(hours=3)),
            ([obs1, obs2], self.now + timedelta(hours=9)),
            ([obs1, obs2], self.now + timedelta(hours=15)),
            ([obs1, obs2], self.now + timedelta(hours=21)),
            ([obs1, obs2], final_at),
        ])
        self.assertEqual(single_call, many_calls)
        self.assertEqual(single_call["rain_mm"], 5.0)
