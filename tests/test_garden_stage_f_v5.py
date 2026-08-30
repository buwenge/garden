"""第五版阶段 F：完整生命周期、开关与上线终审。

对应设计稿《迷你小院子第五版现实天气与环境系统.md》"阶段 F：完整生命周期、
开关与上线终审"一节列出的 20 条必测天气回放场景。这里不重复阶段 A-E 已经
在纯函数/单模块层面证明过的性质（幂等、乱序、锚点折叠等——那些在
``test_garden_stage_b.py``/``test_garden_weather.py`` 等专项里逐条覆盖），
而是通过 ``garden.py`` 的公共入口（``crop_snapshot``/``plant_crop``/
``water_crop``/``water_crops``/``stroll_scene``/``run_tick``/``care`` 等）
端到端跑通真实使用场景，证明第五版整体可迁移、可重启、可降级、可长期运行。

命名避开已存在的 ``test_garden_stage_f.py``——那是第四版 A-F 施工时的
v3→v4 完整生命周期测试，与本文件的第五版现实天气验收无关。
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden
import garden_weather
import log_store
from garden_generator import GardenGeneratorError


TZ = ZoneInfo("Asia/Shanghai")

# 复审历史（阶段 D 动物天气一轮）证明 stroll_scene 的兜底路径会调用
# log_store.write_log；本文件也会驱动 DeepSeek 全失败的完整逛逛流程，
# 必须在模块级把 LOG_FILE 隔离到临时目录，不依赖每个测试类自己记得 patch。
_log_dir = None
_log_patcher = None


def setUpModule():
    global _log_dir, _log_patcher
    _log_dir = tempfile.mkdtemp(prefix="garden_stage_f_v5_logs_")
    _log_patcher = patch.object(log_store, "LOG_FILE", Path(_log_dir) / "logs.jsonl")
    _log_patcher.start()


def tearDownModule():
    _log_patcher.stop()
    shutil.rmtree(_log_dir, ignore_errors=True)


def _water_from_child(path_text: str, now_text: str, result_path: str, cache_text: str) -> None:
    """真实子进程：验证 garden.py 自己的跨进程文件锁不重复计算/不丢写入。

    只依赖显式传入的路径与时间，不依赖父进程里的 unittest.mock 补丁——
    父进程对内存对象的 patch 不保证被 forkserver 派生的子进程继承。
    ``WEATHER_CACHE_FILE`` 显式指向一个不存在的临时路径，确保子进程绝不
    触碰真实的生产天气缓存文件（哪怕只是读取）。
    """
    import os as _os

    _os.environ["WEATHER_CACHE_FILE"] = cache_text
    _os.environ["GARDEN_REAL_ENVIRONMENT_ENABLED"] = "1"
    import garden as _garden

    result = _garden.water_crop("1", now=datetime.fromisoformat(now_text), path=Path(path_text))
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")


class GardenStageFV5Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 29, 8, 0, tzinfo=TZ)
        self.environment = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.observations: list[dict] = []
        loader = patch.object(
            garden, "_environment_observations", side_effect=lambda _now: list(self.observations)
        )
        loader.start()
        self.addCleanup(loader.stop)

    # ---- 共用小工具 -------------------------------------------------

    def observation(
        self, at, *, city="101190205", city_name="南京", temp=24, feels=24,
        humidity=60, wind="1", precip=0, text="晴",
    ):
        value = garden_weather.normalize_observation(
            location_id=city, location_name=city_name, observed_time=at.isoformat(),
            received_at=at, temp=temp, feels_like=feels, humidity=humidity,
            wind_scale=wind, precip=precip, condition_text=text,
        )
        self.assertIsNotNone(value)
        return value

    def plant(self, crop, plot, *, now=None):
        # 迁移/新建土壤会把 environment.last_settled_at 直接拍到种下的那一刻；
        # 紧接着在同一个时间戳上结算会被 `now <= last` 挡住而跳过。种在
        # “基准时刻前一秒”，让测试里后续默认用 self.now 起手的调用天然已经
        # 晚于这个初始化时刻，不必每个用例都手动加偏移。
        plant_at = (now or self.now) - timedelta(seconds=1)
        return garden.plant_crop(crop, plot, now=plant_at, path=self.path)

    def snapshot(self, now):
        return garden.crop_snapshot(now=now, path=self.path)

    def soil_of(self, plot_selector, now=None):
        plot_id = garden.plot_id_from_selector(plot_selector)
        state = self.snapshot(now or self.now)
        for plot in state["plots"]:
            if plot["plot_id"] == plot_id:
                return plot["soil"]
        raise AssertionError(f"未找到地块 {plot_selector}")

    def set_moisture(self, plot_selector, value, *, now=None):
        """把土壤直接拍板到指定水分，同步锚点，避免下次结算被旧锚点重放覆盖。"""
        plot_id = garden.plot_id_from_selector(plot_selector)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for plot in raw["plots"]:
            if plot["plot_id"] == plot_id:
                soil = plot["soil"]
                soil["moisture"] = value
                soil["anchor_moisture"] = value
                soil["anchor_at"] = soil["settled_at"]
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    # ---- 1. 普通晴天：早晨浇水，下午仍合适 ---------------------------

    def test_01_normal_clear_day_watered_morning_still_adequate_afternoon(self):
        self.observations = [
            self.observation(self.now + timedelta(hours=index), temp=23, feels=23, text="晴")
            for index in range(10)
        ]
        self.plant("小番茄", "1")
        self.set_moisture("1", 45)
        morning = garden.water_crop("1", now=self.now, path=self.path)
        self.assertEqual(morning["outcome"], "watered")
        afternoon = self.now + timedelta(hours=8)
        soil = self.soil_of("1", afternoon)
        self.assertGreaterEqual(soil["moisture"], 50)
        result = garden.water_crop("1", now=afternoon, path=self.path)
        self.assertIn(result["outcome"], ("no_need", "too_wet"))

    # ---- 2. 高温晴天：早晨浇水，下午偏干，第二次有效 -------------------

    def test_02_hot_clear_day_second_watering_effective_in_afternoon(self):
        self.observations = [
            self.observation(self.now + timedelta(hours=index), temp=35, feels=39, text="晴")
            for index in range(10)
        ]
        self.plant("小番茄", "1")
        self.set_moisture("1", 45)
        morning = garden.water_crop("1", now=self.now, path=self.path)
        self.assertEqual(morning["outcome"], "watered")
        self.assertEqual(morning["moisture_after"], 80.0)
        afternoon = self.now + timedelta(hours=8)
        soil = self.soil_of("1", afternoon)
        self.assertLess(soil["moisture"], 50)
        second = garden.water_crop("1", now=afternoon, path=self.path)
        self.assertEqual(second["outcome"], "watered")
        self.assertEqual(second["moisture_after"], 80.0)
        self.assertEqual(second["reason"], "hot_clear_dry")

    # ---- 3. 高温但高湿：干燥速度低于干热天 ----------------------------

    def test_03_hot_humid_dries_slower_than_hot_dry(self):
        dry_path = Path(self.tempdir.name) / "dry.json"
        humid_path = Path(self.tempdir.name) / "humid.json"

        def run(path, *, humidity):
            self.observations = [
                self.observation(
                    self.now + timedelta(hours=index), temp=33, feels=36,
                    humidity=humidity, text="晴",
                )
                for index in range(10)
            ]
            garden.crop_snapshot(now=self.now, path=path)
            garden.plant_crop("小番茄", "1", now=self.now, path=path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["plots"][0]["soil"]["moisture"] = 80.0
            raw["plots"][0]["soil"]["anchor_moisture"] = 80.0
            raw["plots"][0]["soil"]["anchor_at"] = raw["plots"][0]["soil"]["settled_at"]
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            later = self.now + timedelta(hours=6)
            return garden.crop_snapshot(now=later, path=path)["plots"][0]["soil"]["moisture"]

        dry_moisture = run(dry_path, humidity=25)
        humid_moisture = run(humid_path, humidity=90)
        self.assertLess(dry_moisture, humid_moisture)

    # ---- 4. 大风低湿：蒸发明显加快 -----------------------------------

    def test_04_windy_low_humidity_speeds_up_evaporation(self):
        calm_path = Path(self.tempdir.name) / "calm.json"
        windy_path = Path(self.tempdir.name) / "windy.json"

        def run(path, *, wind, humidity):
            self.observations = [
                self.observation(
                    self.now + timedelta(hours=index), temp=26, feels=26,
                    humidity=humidity, wind=wind, text="晴",
                )
                for index in range(10)
            ]
            garden.crop_snapshot(now=self.now, path=path)
            garden.plant_crop("小番茄", "1", now=self.now, path=path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["plots"][0]["soil"]["moisture"] = 80.0
            raw["plots"][0]["soil"]["anchor_moisture"] = 80.0
            raw["plots"][0]["soil"]["anchor_at"] = raw["plots"][0]["soil"]["settled_at"]
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            later = self.now + timedelta(hours=6)
            return garden.crop_snapshot(now=later, path=path)["plots"][0]["soil"]["moisture"]

        calm_moisture = run(calm_path, wind="1", humidity=55)
        windy_moisture = run(windy_path, wind="5", humidity=30)
        self.assertLess(windy_moisture, calm_moisture)

    # ---- 5. 小雨：只补少量，不一定浇透 --------------------------------

    def test_05_light_rain_only_partial_replenish(self):
        self.observations = [
            self.observation(self.now, temp=22, feels=22, humidity=70, text="多云"),
            self.observation(
                self.now + timedelta(hours=1), temp=22, feels=22, humidity=75,
                precip=0.5, text="小雨",
            ),
        ]
        self.plant("小番茄", "1")
        self.set_moisture("1", 30)
        later = self.now + timedelta(hours=1)
        soil = self.soil_of("1", later)
        self.assertGreater(soil["moisture"], 30)
        self.assertLess(soil["moisture"], 70)

    # ---- 6. 连续有效降雨：无需手动浇 ----------------------------------

    def test_06_continuous_effective_rain_needs_no_manual_watering(self):
        self.observations = [
            self.observation(
                self.now + timedelta(hours=index), temp=21, feels=21, humidity=90,
                precip=4, text="中雨",
            )
            for index in range(4)
        ]
        self.plant("小番茄", "1")
        self.set_moisture("1", 30)
        later = self.now + timedelta(hours=3)
        soil = self.soil_of("1", later)
        self.assertGreaterEqual(soil["moisture"], 50)
        result = garden.water_crop("1", now=later, path=self.path)
        self.assertIn(result["outcome"], ("no_need", "too_wet"))
        self.assertEqual(result["moisture_before"], result["moisture_after"])

    # ---- 7. 连续雨后湿透：自然积水仅能在每日异常骰命中后出现 -----------

    def test_07_saturated_rain_alone_never_creates_condition_only_natural_roll_can(self):
        self.observations = [
            self.observation(
                self.now + timedelta(hours=index), temp=21, feels=21, humidity=95,
                precip=10, text="大雨",
            )
            for index in range(6)
        ]
        self.plant("小番茄", "1")
        later = self.now + timedelta(hours=5)
        state = self.snapshot(later)
        soil = state["plots"][0]["soil"]
        self.assertEqual(garden_weather.moisture_band(soil["moisture"]), "saturated")
        self.assertIsNone(state["plots"][0].get("condition"))

        # 自然异常骰只挑选 stage != "seed" 的地块；直接把这块地推进到
        # "growing"，只是为了让它满足骰子的资格条件，不影响本条要证明的
        # "光靠饱和土壤本身绝不会自动生成异常，只有命中每日骰才会"这件事。
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["plots"][0]["stage"] = "growing"
        raw["plots"][0]["growth_points"] = 2.0
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        class _HitRng:
            """自然异常骰：random() 永远命中，choice() 优先选中目标类型。"""

            def random(self):
                return 0.0

            def choice(self, values):
                values = list(values)
                if "waterlogged" in values:
                    return "waterlogged"
                return values[0]

        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            garden.run_tick(later, rng=_HitRng(), path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        condition = raw["plots"][0]["condition"]
        self.assertIsInstance(condition, dict)
        self.assertEqual(condition["type"], "waterlogged")
        self.assertEqual(condition["status"], "active")

    # ---- 8. 预报有雨但实况未下：不补水 --------------------------------

    def test_08_forecast_only_rain_never_moves_soil_moisture(self):
        # 观测事实里没有任何雨迹象（只是干燥晴天），即便调用方（如 daemon
        # 转发的聊天摘要）额外声称"预报有雨"，soil 也只能读 `_environment_
        # observations` 里的真实实况，架构上不存在"预报"进入 replay 的通道。
        self.observations = [
            self.observation(self.now + timedelta(hours=index), temp=24, feels=24, text="晴")
            for index in range(4)
        ]
        self.plant("小番茄", "1")
        self.set_moisture("1", 60)
        later = self.now + timedelta(hours=2)
        # weather_tags 是仅供文案展示的可选参数，不应该反向影响土壤结算。
        garden.stroll_scene(now=later, path=self.path, weather_tags=frozenset({"rain"}))
        soil = self.soil_of("1", later)
        self.assertLess(soil["moisture"], 60)
        self.assertEqual(soil["last_water_source"], None)

    # ---- 9. 天气接口断开：中性降级，不凭空干旱 -------------------------

    def test_09_weather_interface_disconnected_neutral_not_arbitrary_drought(self):
        self.observations = []
        self.plant("小番茄", "1")
        self.set_moisture("1", 60)
        later = self.now + timedelta(hours=48)
        state = self.snapshot(later)
        self.assertEqual(state["environment"]["status"], "missing")
        soil = state["plots"][0]["soil"]
        # 保守中性蒸发（白天0.20/小时、夜间0.10/小时）不会把合适的土壤
        # 在两天内推到干燥线以下，更不会制造无中生有的旱情。
        self.assertGreaterEqual(soil["moisture"], garden_weather.MOISTURE_BANDS[1][0])

    # ---- 10. daemon 停机超过缓存窗口：不倒推过去天气 --------------------

    def test_10_downtime_beyond_cache_window_does_not_backdate_stale_weather(self):
        hot_burst = [
            self.observation(self.now + timedelta(minutes=index * 10), temp=38, feels=42, text="晴")
            for index in range(3)
        ]
        self.observations = hot_burst
        self.plant("小番茄", "1")
        self.set_moisture("1", 80)
        # 只有这一小段炎热实况；随后天气接口"断开"整整 5 天没有任何新观测
        # （早已超过 FRESH/STALE 窗口，也超过 72 小时缓存最大保留期）。
        dark_end = self.now + timedelta(days=5)
        during_dark = self.snapshot(dark_end)["plots"][0]["soil"]
        # 停机期间必须走保守中性蒸发（白天0.20/小时、夜间0.10/小时），不能
        # 把 5 天前那次炎热实况（速率会到 3.0+/小时）的高蒸发一直沿用到
        # 5 天后——否则等同于凭空倒推了一场持续 5 天的旱情。中性速率上限
        # 0.20/小时 * 120 小时 = 24，留出一点浮动空间但不能接近炎热速率
        # 可能造成的降幅（炎热速率下 6 小时就能把 80 打到 25 的地板）。
        self.assertGreaterEqual(during_dark["moisture"], 25.0)
        self.assertLessEqual(80.0 - during_dark["moisture"], 30.0)

        # 5 天后天气恢复；恢复瞬间不应该把断线期间的旧实况拿来"倒灌"降水。
        self.observations = hot_burst + [
            self.observation(dark_end, temp=24, feels=24, humidity=70, text="多云"),
        ]
        recovered = self.snapshot(dark_end + timedelta(minutes=1))["plots"][0]["soil"]
        self.assertEqual(recovered["last_rain_at"], None)

    # ---- 11. 同一观测反复读取：只结算一次 ------------------------------

    def test_11_same_observation_replayed_across_multiple_entry_points_settles_once(self):
        self.observations = [
            self.observation(self.now + timedelta(hours=index), temp=32, feels=35, text="晴")
            for index in range(4)
        ]
        self.plant("小番茄", "1")
        moment = self.now + timedelta(hours=3)
        first = self.snapshot(moment)["plots"][0]["soil"]
        with patch(
            "garden.garden_generator.generate_stroll",
            return_value="院子里安安静静的，四块地看起来都很规律。",
        ):
            garden.stroll_scene(now=moment, path=self.path)
        second = self.snapshot(moment)["plots"][0]["soil"]
        third = self.snapshot(moment)["plots"][0]["soil"]
        self.assertEqual(first["moisture"], second["moisture"])
        self.assertEqual(second["moisture"], third["moisture"])
        self.assertEqual(first["exposure_by_date"], third["exposure_by_date"])

    # ---- 12. 两进程并发：无重复降雨、无丢失浇水 -------------------------

    def test_12_two_real_processes_never_duplicate_or_lose_a_watering(self):
        self.plant("小番茄", "1")
        self.set_moisture("1", 45)
        now_text = self.now.isoformat()
        result_a = Path(self.tempdir.name) / "result_a.json"
        result_b = Path(self.tempdir.name) / "result_b.json"
        missing_cache = str(Path(self.tempdir.name) / "no_such_weather_cache.json")
        processes = [
            multiprocessing.Process(
                target=_water_from_child,
                args=(str(self.path), now_text, str(result_a), missing_cache),
            ),
            multiprocessing.Process(
                target=_water_from_child,
                args=(str(self.path), now_text, str(result_b), missing_cache),
            ),
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        outcomes = sorted(
            json.loads(result_a.read_text(encoding="utf-8"))["outcome"],
        ) + sorted(
            json.loads(result_b.read_text(encoding="utf-8"))["outcome"],
        )
        # garden.py 自己的独立子进程受同一把 flock 串行化：不管谁先谁后，
        # 必然恰好一次真正"浇透"、另一次读到已经补足的土壤而不再重复生效。
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        soil = raw["plots"][0]["soil"]
        self.assertEqual(soil["moisture"], 80.0)
        history = raw["plots"][0]["watering_history"]
        self.assertEqual(len(history), 2)
        watered_count = sum(1 for record in history if record["outcome"] == "watered")
        self.assertEqual(watered_count, 1)

    # ---- 13. 北京时间跨日：暴露和浇水记录正确分日 ------------------------

    def test_13_beijing_midnight_crossing_splits_exposure_and_watering_by_correct_date(self):
        before_midnight = datetime(2026, 7, 29, 23, 30, tzinfo=TZ)
        after_midnight = datetime(2026, 7, 30, 0, 30, tzinfo=TZ)
        self.observations = [
            self.observation(before_midnight, temp=26, feels=26, text="晴"),
        ]
        self.plant("小番茄", "1", now=before_midnight)
        self.set_moisture("1", 45, now=before_midnight)
        result = garden.water_crop("1", now=after_midnight, path=self.path)
        self.assertEqual(result["outcome"], "watered")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        watered_by_date = raw["plots"][0]["watering_by_date"]
        self.assertNotIn("2026-07-29", watered_by_date)
        self.assertEqual(watered_by_date.get("2026-07-30"), 1)
        soil = raw["plots"][0]["soil"]
        exposure_dates = set(soil["exposure_by_date"])
        self.assertTrue(exposure_dates)
        self.assertNotIn("2026-07-31", exposure_dates)

    # ---- 14. 城市切换：不把两地天气连续积分 -----------------------------

    def test_14_city_switch_does_not_accumulate_rain_across_cities(self):
        self.observations = [
            self.observation(
                self.now + timedelta(hours=index), city="101190205", city_name="南京",
                temp=22, feels=22, humidity=90, precip=6, text="大雨",
            )
            for index in range(3)
        ]
        self.plant("小番茄", "1")
        self.set_moisture("1", 60)
        switch_moment = self.now + timedelta(hours=2)
        before_switch = self.soil_of("1", switch_moment)
        self.assertEqual(before_switch["last_water_source"], "rain")

        switch_at = self.now + timedelta(hours=3)
        self.observations = self.observations + [
            self.observation(
                switch_at, city="101210101", city_name="杭州",
                temp=25, feels=25, humidity=45, precip=0, text="晴",
            ),
        ]
        after_switch = self.soil_of("1", switch_at + timedelta(minutes=1))
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["environment"]["location_id"], "101210101")
        # 切换后水分保留在切换那一刻的值，不因为旧城市继续下雨而无限累积。
        self.assertLessEqual(after_switch["moisture"], 100.0)
        later_dry = self.soil_of("1", switch_at + timedelta(hours=4))
        self.assertLess(later_dry["moisture"], after_switch["moisture"] + 0.01)

    # ---- 15. v4 正常作物迁移 --------------------------------------------

    def test_15_v4_normal_growing_crop_migrates_to_neutral_v5_soil(self):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "0"}):
            self.plant("小番茄", "1")
            watered = garden.water_crop("1", now=self.now, path=self.path)
            self.assertEqual(watered["outcome"], "watered")
        raw_before = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw_before["version"], 4)

        migrated = self.snapshot(self.now + timedelta(minutes=1))
        raw_after = json.loads(self.path.read_text(encoding="utf-8"))
        # `_migrated` 只是这一次调用内部用来强制写盘的临时标记，
        # `_write_state_unlocked` 会在落盘前把它剔除，磁盘上不会保留；
        # 版本号从 4 变成 5 本身就是迁移确实发生过的证据。
        self.assertEqual(raw_after["version"], 5)
        # 今天已经浇过水，迁移应按“今天已浇水”给出接近湿润的中性起点。
        self.assertEqual(raw_after["plots"][0]["soil"]["moisture"], 72.0)
        self.assertEqual(migrated["plots"][0]["stage"], "seed")
        # 迁移后 v5 机制立即可用。
        self.set_moisture("1", 45)
        second = garden.water_crop("1", now=self.now + timedelta(hours=1), path=self.path)
        self.assertEqual(second["outcome"], "watered")

    # ---- 16. v4 正在积水/已减产/枯死的作物迁移 ---------------------------

    def test_16_v4_active_waterlogged_condition_migrates_to_saturated_soil(self):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "0"}):
            self.plant("小番茄", "1")
            outcomes = [
                garden.water_crop("1", now=self.now, path=self.path)["outcome"]
                for _ in range(3)
            ]
            self.assertEqual(outcomes, ["watered", "protest", "waterlogged"])
        raw_before = json.loads(self.path.read_text(encoding="utf-8"))
        condition = raw_before["plots"][0]["condition"]
        self.assertEqual((condition["type"], condition["status"]), ("waterlogged", "active"))

        self.snapshot(self.now + timedelta(minutes=1))
        raw_after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw_after["version"], 5)
        self.assertEqual(raw_after["plots"][0]["soil"]["moisture"], 100.0)
        # 迁移不打断既有异常状态机：处理动作在 v5 下继续正常工作。
        resolved = garden.resolve_crop_condition(
            "松土", "1", now=self.now + timedelta(hours=1), path=self.path,
        )
        self.assertEqual(resolved["outcome"], "resolved")

    def test_16b_v4_withered_crop_migrates_safely_and_can_still_be_cleared(self):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "0"}):
            self.plant("小番茄", "1")
            for _ in range(3):
                garden.water_crop("1", now=self.now, path=self.path)
            wither_at = self.now + timedelta(hours=37)
            self.snapshot(wither_at)
        raw_before = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw_before["plots"][0]["status"], "withered")

        migrate_at = wither_at + timedelta(minutes=1)
        self.snapshot(migrate_at)
        raw_after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw_after["version"], 5)
        self.assertEqual(raw_after["plots"][0]["status"], "withered")
        self.assertIn("soil", raw_after["plots"][0])
        cleared = garden.clear_withered_crop("1", now=migrate_at, path=self.path)
        self.assertEqual(cleared["crop_id"], "tomato")
        raw_final = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw_final["plots"][0], {"plot_id": "p1", "status": "empty"})

    # ---- 17. DeepSeek 全部失败：本地兜底完成完整生命周期 ------------------

    def test_17_deepseek_fully_down_completes_full_lifecycle_on_local_fallback(self):
        import home

        def always_fails(*_args, **_kwargs):
            raise GardenGeneratorError("模拟写手完全不可用")

        with (
            patch("garden.garden_generator.generate_stroll", side_effect=always_fails),
            patch("garden.garden_generator.generate_crop_copy", side_effect=always_fails),
        ):
            self.plant("小番茄", "1")

            stroll_text = garden.stroll_scene(now=self.now, path=self.path)
            self.assertTrue(stroll_text)
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(raw["meta"]["scene_cache"][0]["source"], "fallback")

            self.set_moisture("1", 45, now=self.now)
            watered = garden.water_crop("1", now=self.now, path=self.path)
            self.assertEqual(watered["outcome"], "watered")
            watered_text = home._garden_water_crop_text(watered, use_writer=True)
            self.assertIn("结果：", watered_text)
            self.assertTrue(watered_text.strip())

            outcomes = [
                garden.water_crop("1", now=self.now, path=self.path)["outcome"]
                for _ in range(3)
            ]
            self.assertEqual(outcomes, ["too_wet", "protest", "waterlogged"])
            condition_at = self.now + timedelta(minutes=1)
            resolved = garden.resolve_crop_condition(
                "松土", "1", now=condition_at, path=self.path,
            )
            self.assertEqual(resolved["outcome"], "resolved")
            treatment_text = garden.crop_treatment_copy(
                resolved, now=condition_at,
                fallback="小番茄根边的水已经被松开的土吸走了大半。",
            )
            self.assertTrue(treatment_text.strip())

            ready_at = self.now + timedelta(days=5)
            ready = self.snapshot(ready_at)
            self.assertEqual(ready["plots"][0]["status"], "ready")
            harvested = garden.harvest_crop("1", now=ready_at, path=self.path)
            self.assertEqual(harvested["crop_id"], "tomato")

            final_stroll = garden.stroll_scene(now=ready_at + timedelta(minutes=1), path=self.path)
            self.assertTrue(final_stroll)

    # ---- 18. 场景状态不变：缓存稳定 --------------------------------------

    def test_18_scene_cache_stable_when_visible_state_unchanged(self):
        self.observations = [
            self.observation(self.now, temp=23, feels=23, text="晴"),
        ]
        self.plant("小番茄", "1")
        with patch(
            "garden.garden_generator.generate_stroll",
            return_value="院子里静悄悄的，四块地看起来都很规律，没什么特别变化。",
        ) as writer:
            first = garden.stroll_scene(now=self.now, path=self.path)
            second = garden.stroll_scene(now=self.now + timedelta(minutes=5), path=self.path)
            third = garden.stroll_scene(now=self.now + timedelta(minutes=10), path=self.path)
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        writer.assert_called_once()

    # ---- 19. 水分跨档位：生成新场景键但不主动刷屏 -------------------------

    def test_19_moisture_band_change_creates_new_key_without_spamming(self):
        self.observations = [
            self.observation(self.now + timedelta(hours=index), temp=34, feels=38, text="晴")
            for index in range(12)
        ]
        self.plant("小番茄", "1")
        self.set_moisture("1", 80)
        with patch(
            "garden.garden_generator.generate_stroll",
            side_effect=[
                "土壤还很湿润，四块地看起来都很规律，没什么特别变化。",
                "土已经偏干了，四块地看起来都很规律，没什么特别变化。",
            ],
        ) as writer:
            adequate = garden.stroll_scene(now=self.now, path=self.path)
            self.assertEqual(writer.call_count, 1)
            later = self.now + timedelta(hours=8)
            dryish = garden.stroll_scene(now=later, path=self.path)
            self.assertEqual(writer.call_count, 2)
            self.assertNotEqual(adequate, dryish)
            # 同一新档位内再逛，不应该再次触发写手——不刷屏。
            garden.stroll_scene(now=later + timedelta(minutes=5), path=self.path)
            garden.stroll_scene(now=later + timedelta(minutes=10), path=self.path)
            self.assertEqual(writer.call_count, 2)

    # ---- 20. 动物在雨、热、冷、风中的行为不改亲密与身份 --------------------

    def test_20_animal_weather_modes_never_change_bond_or_identity(self):
        import random as random_module

        animal = garden.spawn(
            "animal", species="橘猫", intro="蹲在墙根打量了好一会儿。",
            category="猫", personality="活泼",
            now=self.now, path=self.path, rng=random_module.Random(7),
        )
        animal_id = animal["id"]

        regimes = [
            ("投喂", self.now, [self.observation(self.now, temp=22, feels=22, humidity=90, precip=6, text="大雨")], "sheltering"),
            ("摸摸", self.now + timedelta(hours=1), [self.observation(self.now + timedelta(hours=1), temp=35, feels=39, text="晴")], "cooling"),
            ("投喂", self.now + timedelta(days=1, hours=2), [self.observation(self.now + timedelta(days=1, hours=2), temp=3, feels=1, text="晴")], "basking"),
            ("摸摸", self.now + timedelta(days=1, hours=3), [self.observation(self.now + timedelta(days=1, hours=3), temp=20, feels=20, wind="4", text="晴")], "wind_play"),
        ]
        previous_points = 0
        for action, moment, observations, expected_mode in regimes:
            self.observations = observations
            result = garden.care(animal_id, action, now=moment, path=self.path, rng=random_module.Random(1))
            self.assertEqual(result["weather_mode"], expected_mode)
            entry = result["entry"]
            self.assertEqual(entry["id"], animal_id)
            self.assertEqual(entry["species"], "橘猫")
            self.assertEqual(entry["nickname"], animal["nickname"])
            self.assertGreaterEqual(entry["bond_points"], previous_points)
            previous_points = entry["bond_points"]
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        final_animal = next(item for item in raw["animals"] if item["id"] == animal_id)
        self.assertEqual(final_animal["status"], "active")
        self.assertEqual(final_animal["species"], "橘猫")

    # ---- 21. 浇水升级为积水锁是逐地块的，不是全院共享一把 ------------------

    def test_21_watering_escalation_lock_is_per_plot_not_yard_wide(self):
        """8/13 真实事故：批量浇水时一号地在这一轮率先攒够第三次无必要
        浇水、创建了自己的积水异常；二号地在同一次批量调用里独立攒够
        第三次时，不应该被一号地刚建立的异常拦住（不该拦，因为它们是
        两块独立的地，各自处理各自的浇水历史）。"""
        self.plant("小番茄", "1")
        self.plant("小番茄", "2")
        self.set_moisture("1", 60)
        self.set_moisture("2", 60)
        first = garden.water_crops(["1", "2"], now=self.now, path=self.path)
        self.assertEqual([r["outcome"] for r in first], ["no_need", "no_need"])
        second = garden.water_crops(["1", "2"], now=self.now, path=self.path)
        self.assertEqual([r["outcome"] for r in second], ["protest", "protest"])
        third = garden.water_crops(["1", "2"], now=self.now, path=self.path)
        self.assertEqual([r["outcome"] for r in third], ["waterlogged", "waterlogged"])
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for plot in raw["plots"][:2]:
            condition = plot["condition"]
            self.assertEqual((condition["type"], condition["status"]), ("waterlogged", "active"))

    def test_21b_watering_escalation_still_blocked_when_same_plot_already_has_a_condition(self):
        """逐地块的锁仍然要挡同一块地上叠第二个异常：这块地已经有一个
        （比如虫害）异常在身，第三次无必要浇水不能再叠一个积水异常
        把原来的异常记录覆盖掉。"""
        self.plant("小番茄", "1")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["plots"][0].update({"stage": "growing", "growth_points": 2.0})
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        class _PestRng:
            def random(self):
                return 0.0

            def choice(self, values):
                values = list(values)
                return "pest" if "pest" in values else values[0]

        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            garden.run_tick(self.now, rng=_PestRng(), path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["plots"][0]["condition"]["type"], "pest")

        self.set_moisture("1", 60)
        outcomes = [
            garden.water_crop("1", now=self.now, path=self.path)["outcome"]
            for _ in range(3)
        ]
        self.assertEqual(outcomes, ["no_need", "protest", "blocked_by_condition"])
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["plots"][0]["condition"]["type"], "pest")


if __name__ == "__main__":
    unittest.main()
