import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden


TZ = ZoneInfo("Asia/Shanghai")


class GardenTimingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 10, 8, 0, tzinfo=TZ)
        observations = patch.object(garden, "_environment_observations", return_value=[])
        observations.start()
        self.addCleanup(observations.stop)

    def _plant(self, *, environment: bool):
        toggle = patch.dict(
            os.environ,
            {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1" if environment else ""},
        )
        toggle.start()
        self.addCleanup(toggle.stop)
        garden.crop_snapshot(now=self.now, path=self.path)
        return garden.plant_crop("小番茄", "1", now=self.now, path=self.path)

    def test_snapshot_exposes_frontend_ready_timing_contract(self):
        planted = self._plant(environment=False)
        timing = planted["timing"]
        self.assertEqual(timing["schema_version"], 1)
        self.assertEqual(timing["server_now"], self.now.isoformat())
        self.assertEqual(timing["base_duration_seconds"], 4 * 24 * 60 * 60)
        self.assertEqual(timing["growth_target_points"], 4.0)
        # 线性记账：种下那一瞬还没有时间流逝，进度是真实的 0。
        self.assertEqual(timing["growth_points"], 0.0)
        self.assertEqual(timing["progress_ratio"], 0.0)
        self.assertEqual(timing["status"], "growing")
        self.assertIsInstance(timing["current"]["remaining_seconds"], int)
        self.assertGreater(timing["current"]["remaining_seconds"], 0)
        # 小暑期间番茄每天多10%（1.1点/天），4.0 点的目标折合 4/1.1 ≈
        # 3.64 天；预览与结算共用同一套分段线性几何，预计成熟时刻就是
        # 结算真正翻转状态的时刻。
        expected_ready = self.now + timedelta(days=4.0 / 1.1)
        estimated = datetime.fromisoformat(timing["current"]["estimated_ready_at"])
        self.assertAlmostEqual(
            estimated.timestamp(), expected_ready.timestamp(), delta=1,
        )
        snapshot = garden.crop_snapshot(now=self.now, path=self.path)
        self.assertEqual(snapshot["server_now"], self.now.isoformat())
        self.assertEqual(snapshot["timing_schema_version"], 1)
        self.assertEqual(
            snapshot["crop_catalog"]["tomato"]["base_duration_seconds"],
            4 * 24 * 60 * 60,
        )
        event = garden.frontend_state_event(now=self.now, path=self.path)
        self.assertEqual(event["type"], "garden_state")
        self.assertEqual(event["data"]["server_now"], self.now.isoformat())
        self.assertIn("timing", event["data"]["plots"][0])
        json.dumps(event, ensure_ascii=False)

    def test_dry_soil_and_watering_refresh_eta_and_saved_seconds(self):
        self._plant(environment=True)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        soil = raw["plots"][0]["soil"]
        soil.update({
            "moisture": 20.0,
            "anchor_moisture": 20.0,
            "anchor_at": self.now.isoformat(),
            "settled_at": self.now.isoformat(),
        })
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        before = garden.crop_snapshot(now=self.now, path=self.path)["plots"][0]["timing"]
        self.assertEqual(before["current"]["soil_band"], "dry")
        self.assertEqual(before["current"]["multiplier"], 0.7)
        self.assertEqual(before["after_watering"]["multiplier"], 1.0)
        self.assertTrue(before["after_watering"]["would_help"])
        self.assertGreater(before["after_watering"]["time_saved_seconds"], 0)
        self.assertEqual(
            before["continuous_dry"]["remaining_seconds"],
            before["current"]["remaining_seconds"],
        )

        result = garden.water_crop("1", now=self.now, path=self.path)
        self.assertEqual(result["outcome"], "watered")
        self.assertEqual(result["timing_before"]["current"]["multiplier"], 0.7)
        self.assertEqual(result["timing_after"]["current"]["multiplier"], 1.0)
        self.assertEqual(
            result["time_saved_seconds"],
            result["timing_before"]["current"]["remaining_seconds"]
            - result["timing_after"]["current"]["remaining_seconds"],
        )
        self.assertGreater(result["time_saved_seconds"], 0)

    def test_actual_ready_at_is_recorded_at_the_crossing_moment(self):
        self._plant(environment=False)
        checked_at = self.now + timedelta(days=4)
        plot = garden.crop_snapshot(now=checked_at, path=self.path)["plots"][0]
        # 线性记账：跨线时刻在段内插值，不再整点落在某个午夜。
        expected = self.now + timedelta(days=4.0 / 1.1)
        self.assertEqual(plot["status"], "ready")
        self.assertAlmostEqual(
            datetime.fromisoformat(plot["ready_at"]).timestamp(),
            expected.timestamp(), delta=1,
        )
        self.assertAlmostEqual(
            datetime.fromisoformat(plot["timing"]["actual_ready_at"]).timestamp(),
            expected.timestamp(), delta=1,
        )
        self.assertEqual(plot["timing"]["current"]["remaining_seconds"], 0)

    def test_estimated_ready_at_matches_the_actual_flip_moment(self):
        """倒计时预告的成熟时刻必须就是结算真正翻转状态的时刻。

        线性记账下预览与结算共用同一套分段线性几何：上午看到的预计成熟
        时刻，下午作物真熟时记录的 ready_at 必须是同一个时刻（同条件下），
        不会越推越晚，也不会提前翻转。
        """
        self._plant(environment=False)
        with garden._locked(self.path, exclusive=True):
            state = garden._read_state_unlocked(self.path, now=self.now)
            plot = state["plots"][0]
            plot["growth_points"] = 3.7  # 距番茄4点目标只差一点
            garden._write_state_unlocked(state, self.path)

        morning = self.now.replace(hour=9, minute=0)
        timing = garden.crop_snapshot(now=morning, path=self.path)["plots"][0]["timing"]
        self.assertEqual(timing["status"], "growing")
        estimated = datetime.fromisoformat(timing["current"]["estimated_ready_at"])

        afternoon = self.now.replace(hour=15, minute=0)
        ripened = garden.crop_snapshot(now=afternoon, path=self.path)["plots"][0]
        self.assertEqual(ripened["status"], "ready")
        # growth_points 每次结算舍入到4位小数，换算成时间最多漂移约4秒。
        self.assertAlmostEqual(
            datetime.fromisoformat(ripened["ready_at"]).timestamp(),
            estimated.timestamp(), delta=5,
        )

    def test_no_midnight_mass_ripening_when_countdown_says_later(self):
        """8/18 事故回归：23:50 倒计时还剩好几个小时的菜，绝不能一跨午夜就熟。

        整天记账时代，"今天"的全额生长点在跨入当天的第一次结算就一次性
        入账，任何差距不足一天增量的作物都会在下一个午夜齐熟——比倒计时
        显示的时刻最多提前整整 24 小时（8/17 23:50 显示还剩 10 小时和
        1 天的两块地，8/18 00:27 全部可收获，就是这个）。线性记账下午夜
        只是普通的一分钟，跨过午夜只多长 10 分钟的量。
        """
        self._plant(environment=False)
        late_evening = self.now.replace(hour=23, minute=50)
        with garden._locked(self.path, exclusive=True):
            state = garden._read_state_unlocked(self.path, now=self.now)
            plot = state["plots"][0]
            plot["growth_points"] = 3.7  # 距番茄4点目标差约 6.5 小时的量
            plot["last_settled_at"] = late_evening.isoformat()
            garden._write_state_unlocked(state, self.path)

        before = garden.crop_snapshot(now=late_evening, path=self.path)["plots"][0]
        self.assertEqual(before["status"], "growing")
        estimated = datetime.fromisoformat(
            before["timing"]["current"]["estimated_ready_at"],
        )
        self.assertGreater(estimated, late_evening + timedelta(hours=6))

        past_midnight = self.now.replace(hour=0, minute=10) + timedelta(days=1)
        after = garden.crop_snapshot(now=past_midnight, path=self.path)["plots"][0]
        # 跨过午夜只多累积了 20 分钟的生长，离成熟线还远，绝不该翻转。
        self.assertEqual(after["status"], "growing")
        self.assertLess(after["growth_points"], 4.0)
        # 预计成熟时刻保持稳定，不因跨日发生跳变。
        re_estimated = datetime.fromisoformat(
            after["timing"]["current"]["estimated_ready_at"],
        )
        self.assertAlmostEqual(
            re_estimated.timestamp(), estimated.timestamp(), delta=5,
        )

    def test_estimated_ready_at_stays_in_the_future_across_growth_and_time_of_day(self):
        """通用护栏：只要还在 growing，预计成熟时刻不能是过去，不分查看时几点、
        不分距离跨线还差多少。这是给这一小段插值数学（三天内改了三次）补的
        回归防线，而不是只锁一次具体复现场景。"""
        self._plant(environment=False)
        for growth_points in (0.0, 1.0, 2.5, 3.0, 3.7, 3.99):
            for hour in (0, 6, 9, 12, 15, 18, 23):
                with self.subTest(growth_points=growth_points, hour=hour):
                    with garden._locked(self.path, exclusive=True):
                        state = garden._read_state_unlocked(self.path, now=self.now)
                        plot = state["plots"][0]
                        plot["growth_points"] = growth_points
                        plot["last_settled_at"] = self.now.isoformat()
                        garden._write_state_unlocked(state, self.path)
                    checked_at = self.now.replace(hour=hour, minute=0)
                    timing = garden.crop_snapshot(now=checked_at, path=self.path)["plots"][0]["timing"]
                    if timing["status"] != "growing":
                        continue
                    estimated = datetime.fromisoformat(timing["current"]["estimated_ready_at"])
                    self.assertGreater(estimated, checked_at)
                    self.assertGreater(timing["current"]["remaining_seconds"], 0)

    def test_whole_day_ledger_migrates_to_linear_without_double_counting(self):
        """旧档一次性迁移：整天记账时代 last_settled_at 当天的整天生长点
        已经入账，切到线性记账后，当天剩余时段不得再累积一遍（不重复
        入账）；次日起从午夜整点正常线性累积（不留缝）。"""
        self._plant(environment=False)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        # 伪造成旧口径存档：7/10 上午已结算，整天 1.1 点已一次性入账，
        # 且还没有线性记账标记。
        raw["growth_accounting"] = ""
        raw["plots"][0]["growth_points"] = 1.1
        raw["plots"][0]["stage"] = "sprout"
        raw["plots"][0]["last_settled_at"] = self.now.isoformat()
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        # 当天晚些时候查看：这一天已经整天入账过，线性记账不得再累积。
        same_day = garden.crop_snapshot(
            now=self.now.replace(hour=20), path=self.path,
        )["plots"][0]
        self.assertEqual(same_day["growth_points"], 1.1)
        migrated = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["growth_accounting"], "linear")
        # 迁移日当天预付时段已冻结在 growth_points 里，预计成熟必须从
        # 次日午夜的结算游标起算，不得把已预付的今晚再摊一遍虚报提前。
        boundary = garden._date_at_start(self.now.date() + timedelta(days=1))
        estimated = datetime.fromisoformat(
            same_day["timing"]["current"]["estimated_ready_at"],
        )
        self.assertAlmostEqual(
            estimated.timestamp(),
            (boundary + timedelta(days=(4.0 - 1.1) / 1.1)).timestamp(),
            delta=5,
        )

        # 次日清晨：从午夜整点起按 1.1/天 线性累积了 6 小时。
        next_morning = garden.crop_snapshot(
            now=self.now.replace(hour=6) + timedelta(days=1), path=self.path,
        )["plots"][0]
        self.assertAlmostEqual(
            next_morning["growth_points"], 1.1 + 1.1 * (6 / 24), places=3,
        )

    def test_marker_swept_into_legacy_bucket_does_not_retrigger_migration(self):
        """新旧代码混跑保险：旧代码不认识 growth_accounting，会把它扫进
        legacy.unrecognized_top_level 再落盘；新代码必须从那里救回标记，
        绝不能把已迁移的存档再迁移一遍、把结算游标又推后一天冻住生长。"""
        self._plant(environment=False)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["growth_accounting"], "linear")
        # 模拟旧代码落盘：标记被挪进 legacy 桶，顶层字段消失。
        raw.pop("growth_accounting")
        raw["legacy"]["unrecognized_top_level"]["growth_accounting"] = "linear"
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        later = self.now + timedelta(hours=6)
        plot = garden.crop_snapshot(now=later, path=self.path)["plots"][0]
        # 若被误判为未迁移，结算游标会被推到次日午夜、这 6 小时颗粒无收；
        # 正确行为是照常线性累积。
        self.assertAlmostEqual(plot["growth_points"], 1.1 * (6 / 24), places=3)
        restored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(restored["growth_accounting"], "linear")
        self.assertNotIn(
            "growth_accounting", restored["legacy"]["unrecognized_top_level"],
        )

    def test_active_crop_problem_exposes_paused_clock_without_fake_eta(self):
        self._plant(environment=False)
        with garden._locked(self.path, exclusive=True):
            state = garden._read_state_unlocked(self.path, now=self.now)
            garden._create_crop_condition(
                state,
                state["plots"][0],
                "pest",
                self.now,
                announced=True,
            )
            garden._write_state_unlocked(state, self.path)
        timing = garden.crop_snapshot(now=self.now, path=self.path)["plots"][0]["timing"]
        self.assertEqual(timing["status"], "paused_condition")
        self.assertEqual(timing["pause_reason"], "condition:pest")
        self.assertIsNone(timing["current"]["estimated_ready_at"])
        self.assertIsNone(timing["current"]["remaining_seconds"])
        self.assertFalse(timing["after_watering"]["would_help"])


if __name__ == "__main__":
    unittest.main()
