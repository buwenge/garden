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

    def _planting_day_partial(self, crop_id, planted_at):
        """种下当天剩余小时按比例记的那一笔账，跟 garden.plant_crop 同口径。"""
        crop = garden.garden_crops.CROPS[crop_id]
        day_start = garden._date_at_start(planted_at.date())
        fraction_remaining = (day_start + timedelta(days=1) - planted_at).total_seconds() / 86400.0
        context = garden.calendar_context(day_start)
        base = 1.0 + (0.1 if context.term_id in crop["term_affinities"] else 0.0)
        return round(base * fraction_remaining, 4)

    def test_snapshot_exposes_frontend_ready_timing_contract(self):
        planted = self._plant(environment=False)
        timing = planted["timing"]
        self.assertEqual(timing["schema_version"], 1)
        self.assertEqual(timing["server_now"], self.now.isoformat())
        self.assertEqual(timing["base_duration_seconds"], 4 * 24 * 60 * 60)
        self.assertEqual(timing["growth_target_points"], 4.0)
        # 种下当天剩余小时已经按比例先记了一笔账（见 plant_crop），种下
        # 那一刻起进度就不是 0。
        planting_partial = self._planting_day_partial("tomato", self.now)
        self.assertEqual(timing["growth_points"], planting_partial)
        self.assertEqual(timing["progress_ratio"], round(planting_partial / 4.0, 6))
        self.assertEqual(timing["status"], "growing")
        self.assertIsInstance(timing["current"]["remaining_seconds"], int)
        self.assertGreater(timing["current"]["remaining_seconds"], 0)
        # 小暑期间番茄每天多10%；种下当天先记的那笔账，加上之后每一整天
        # 满额的增量，第3天（7/13）就跨线，比没有这笔账时提前了一天。
        # 预览按跨线那一整天的增量线性插值出具体时刻，而不是永远整点报到
        # 下一个午夜，这样浇不浇水在预览上才能算出真实的小时/分钟差。
        # 插值窗口必须锚定在跨线那一天自己的0点到次日0点（而不是提前一
        # 天），否则算出的时刻会比真正结算翻转状态的时刻还早。
        after_two_full_days = planting_partial + 1.1 * 2
        deficit = 4.0 - after_two_full_days
        self.assertEqual(
            datetime.fromisoformat(timing["current"]["estimated_ready_at"]),
            datetime(2026, 7, 13, tzinfo=TZ) + timedelta(days=1) * (deficit / 1.1),
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

    def test_actual_ready_at_is_recorded_at_the_crossing_boundary(self):
        self._plant(environment=False)
        checked_at = self.now + timedelta(days=4)
        plot = garden.crop_snapshot(now=checked_at, path=self.path)["plots"][0]
        # 种下当天先记的那笔账让跨线提前了一天（见 plant_crop）。
        expected = datetime(2026, 7, 13, 0, 0, tzinfo=TZ)
        self.assertEqual(plot["status"], "ready")
        self.assertEqual(datetime.fromisoformat(plot["ready_at"]), expected)
        self.assertEqual(datetime.fromisoformat(plot["timing"]["actual_ready_at"]), expected)
        self.assertEqual(plot["timing"]["current"]["remaining_seconds"], 0)

    def test_estimated_ready_at_does_not_recede_on_repeated_same_day_checks(self):
        """今天已经结算过的作物，条件不变时反复查看不该把成熟时刻越推越晚。

        之前的 bug：成熟当天第一段窗口用“现在到午夜”而不是“当天0点到
        午夜”插值，导致同样的生长点数/倍率下，越晚查看窗口越短、算出的
        成熟时刻反而越往后退，形成“永远还有大半窗口”的追不上假象。
        """
        self._plant(environment=False)
        with garden._locked(self.path, exclusive=True):
            state = garden._read_state_unlocked(self.path, now=self.now)
            plot = state["plots"][0]
            plot["growth_points"] = 3.7  # 距番茄4点目标只差一点
            garden._write_state_unlocked(state, self.path)

        morning = self.now.replace(hour=9, minute=0)
        afternoon = self.now.replace(hour=15, minute=0)
        ready_morning = garden.crop_snapshot(now=morning, path=self.path)["plots"][0]["timing"]
        ready_afternoon = garden.crop_snapshot(now=afternoon, path=self.path)["plots"][0]["timing"]
        self.assertEqual(
            ready_morning["current"]["estimated_ready_at"],
            ready_afternoon["current"]["estimated_ready_at"],
        )

    def test_estimated_ready_at_never_lands_before_the_settlement_that_will_actually_flip_it(self):
        """预计成熟时刻不能早于结算真正翻转状态的那一刻。

        8/7 15:45 辣椒复现的 bug：_settle_crops 已经用同一个 now 结算过
        “今天”这一整天，growth_points 已经包含今天的账；真正还没入账的
        第一天永远是明天。如果插值窗口误用“今天0点到明天0点”而不是“明天
        0点到后天0点”，会把明天才会真正入账的增量提前摊进今天已经过去的
        时段里，算出一个比“现在”还早的成熟时刻——倒计时显示归零，但
        status 仍是 growing，收获继续被拒。这里同一天当天已经结算过一次
        （last_settled_at 就是今天），且距离跨线只差一点点，专门复现这种
        “当天账已经记完、只差明天一点点”的场景。
        """
        self._plant(environment=False)
        with garden._locked(self.path, exclusive=True):
            state = garden._read_state_unlocked(self.path, now=self.now)
            plot = state["plots"][0]
            plot["growth_points"] = 3.7  # 距番茄4点目标只差一点
            plot["last_settled_at"] = self.now.isoformat()  # 今天的账已经记完
            garden._write_state_unlocked(state, self.path)

        checked_late_in_day = self.now.replace(hour=23, minute=0)
        timing = garden.crop_snapshot(now=checked_late_in_day, path=self.path)["plots"][0]["timing"]
        self.assertEqual(timing["status"], "growing")
        estimated = datetime.fromisoformat(timing["current"]["estimated_ready_at"])
        self.assertGreater(estimated, checked_late_in_day)
        self.assertGreater(timing["current"]["remaining_seconds"], 0)
        self.assertEqual(estimated.date(), self.now.date() + timedelta(days=1))

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
