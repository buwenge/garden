"""第七版阶段 C：排水动作。

覆盖设计稿《迷你小院子第七版内涝惩罚与作物品质设计.md》第十一节阶段 C
验收清单。阶段 A（`2c1cd33`）落地了纯函数与数据地基（
`garden_weather.yard_water_index(drained_at=...)`/`flood_soak_hours`、
`flood_watch`/`drainage` 字段迁移与校验），阶段 B（`c22972c`）落地了内涝
时钟结算（`_settle_flood_damage`：欠佳标记、泡烂、禁播）。本文件只测阶段
C 新增的排水动作本体 ``garden.drain_yard``、``home.py`` 的『排水』路由
（含语义兜底与「松土」别名的去冲突）、出门文案接入，以及网页/CLI 展示面
的静态接线。不做品质出入库全链路（阶段 D）与异常联动（阶段 E）。

全程离线：只操作 ``tempfile.TemporaryDirectory()`` 里的临时存档，通过
``patch.object(garden, "_environment_observations", ...)`` 注入合成天气；
绝不读写真实 ``garden.json``/``weather_cache.json``，也绝不
经由 ``garden-web/server.py`` 的 ``run_command`` 或真实 ``home`` 二进制
（那条路径会不受 ``path=`` 参数控制、直接命中生产文件——测试禁止碰它）。
"""

import ast
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden
import garden_content
import garden_weather
import home
import log_store
from garden_generator import GardenGeneratorError


TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]


def observation(at, *, precip=0.0, text="晴", wind="2"):
    value = garden_weather.normalize_observation(
        location_id="test-city", location_name="测试",
        observed_time=at.isoformat(), received_at=at,
        temp=24, feels_like=24, humidity=85, wind_scale=wind,
        precip=precip, condition_text=text,
    )
    if value is None:
        raise AssertionError("测试观测标准化失败")
    return value


def hourly_rain(end, *, hours, total, text="大雨", wind="4"):
    """在 (end - hours, end] 内均匀铺 total 毫米雨量，逐小时一条观测。"""
    rate = total / hours
    return [
        observation(end - timedelta(hours=hours - index - 1), precip=rate, text=text, wind=wind)
        for index in range(hours)
    ]


def sustained_rain(start, end, *, rate, text="大暴雨", wind="4"):
    """(start, end] 每小时一条固定强度观测，用于需要长时间维持内涝的场景。"""
    items = []
    cursor = start
    while cursor <= end:
        items.append(observation(cursor, precip=rate, text=text, wind=wind))
        cursor += timedelta(hours=1)
    return items


def _seed_growing_plot(path: Path, now: datetime, *, crop_id="cucumber") -> None:
    """写一份带一块 growing 地块的 v4 存档，交由后续真实读取迁移到 v5。"""
    state = garden._empty_state()
    state.pop("entries")
    state["plots"] = [garden._empty_plot(pid) for pid in garden._PLOT_IDS]
    state["meta"]["crop_seed_box_initialized"] = True
    state["plots"][0].update({
        "crop_id": crop_id,
        "planted_at": (now - timedelta(days=1)).isoformat(),
        "last_settled_at": now.isoformat(),
        "growth_points": 1.0,
        "stage": "growing",
        "water_bonus_dates": [],
        "watering_by_date": {},
        "ready_at": None,
        "status": "growing",
        "cycle_id": "cycle-1",
        "stage_events_seen": [],
    })
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


class DrainYardActionTests(unittest.TestCase):
    """``garden.drain_yard`` 本体：none 拒绝、puddles/flooded 端到端、频控。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
        enabled = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        enabled.start()
        self.addCleanup(enabled.stop)

    def _raw(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_none_level_is_rejected_and_writes_no_state(self):
        with patch.object(garden, "_environment_observations", return_value=[]):
            with self.assertRaises(garden.GardenError) as ctx:
                garden.drain_yard(now=self.now, path=self.path)
        self.assertEqual(str(ctx.exception), "排水沟挺通畅的，院子里也没积水，不用忙活。")
        raw = self._raw()
        self.assertIsNone(raw["environment"]["flood_watch"]["drained_at"])
        self.assertIsNone(raw["environment"]["flood_watch"]["since"])
        self.assertEqual(raw["journal"].get("drainage", []), [])

    def test_puddles_end_to_end(self):
        obs = hourly_rain(self.now, hours=6, total=50, text="中雨", wind="4")
        with patch.object(garden, "_environment_observations", return_value=obs):
            result = garden.drain_yard(now=self.now, path=self.path)
        self.assertEqual(result["yard_water"], "puddles")
        self.assertEqual(result["rain_streak_days"], 1)
        raw = self._raw()
        self.assertEqual(raw["environment"]["flood_watch"]["drained_at"], self.now.isoformat())
        self.assertEqual(raw["journal"]["drainage"], [
            {"at": self.now.isoformat(), "yard_water": "puddles", "rain_streak_days": 1},
        ])

    def test_flooded_end_to_end(self):
        obs = hourly_rain(self.now, hours=10, total=100, text="大雨", wind="4")
        with patch.object(garden, "_environment_observations", return_value=obs):
            result = garden.drain_yard(now=self.now, path=self.path)
        self.assertEqual(result["yard_water"], "flooded")
        raw = self._raw()
        self.assertEqual(raw["environment"]["flood_watch"]["drained_at"], self.now.isoformat())
        self.assertEqual(len(raw["journal"]["drainage"]), 1)
        self.assertEqual(raw["journal"]["drainage"][0]["yard_water"], "flooded")

    def test_frequency_control_rejects_within_two_hours_and_does_not_rewrite_drained_at(self):
        obs = hourly_rain(self.now, hours=10, total=100, text="大雨", wind="4")
        with patch.object(garden, "_environment_observations", return_value=obs):
            garden.drain_yard(now=self.now, path=self.path)
            second_attempt = self.now + timedelta(hours=1, minutes=59)
            with self.assertRaises(garden.GardenError) as ctx:
                garden.drain_yard(now=second_attempt, path=self.path)
        self.assertEqual(str(ctx.exception), "刚清过，沟里还通畅着。")
        raw = self._raw()
        # 拒绝分支不改写时钟：drained_at 仍是第一次成功排水的时刻，不是
        # 被拒绝的第二次尝试时刻。
        self.assertEqual(raw["environment"]["flood_watch"]["drained_at"], self.now.isoformat())
        self.assertEqual(len(raw["journal"]["drainage"]), 1)

    def test_frequency_control_lifts_after_two_hours(self):
        obs = hourly_rain(self.now, hours=10, total=100, text="大雨", wind="4")
        with patch.object(garden, "_environment_observations", return_value=obs):
            garden.drain_yard(now=self.now, path=self.path)
            third_attempt = self.now + timedelta(hours=2, minutes=1)
            result = garden.drain_yard(now=third_attempt, path=self.path)
        self.assertIn(result["yard_water"], ("puddles", "flooded"))
        raw = self._raw()
        self.assertEqual(raw["environment"]["flood_watch"]["drained_at"], third_attempt.isoformat())
        self.assertEqual(len(raw["journal"]["drainage"]), 2)

    def test_old_archive_without_drainage_key_is_backfilled(self):
        """老存档缺 drainage 键：读取即 setdefault，不因为缺键而报错或丢排水。"""
        obs = hourly_rain(self.now, hours=10, total=100, text="大雨", wind="4")
        with patch.object(garden, "_environment_observations", return_value=obs):
            garden.crop_snapshot(now=self.now, path=self.path)
            raw = self._raw()
            del raw["journal"]["drainage"]
            self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            result = garden.drain_yard(now=self.now + timedelta(hours=3), path=self.path)
        self.assertEqual(result["yard_water"], "flooded")
        raw2 = self._raw()
        self.assertEqual(len(raw2["journal"]["drainage"]), 1)

    def test_drained_at_wiring_makes_event_layer_and_penalty_layer_agree(self):
        """阶段 C 的核心验收点：`yard_water_index(drained_at=...)` 接线后，
        画面档位（事件层）与浸泡时钟（惩罚层）读同一份 `flood_watch`，
        排水足够久之后两层应该同时确认"不再内涝"。"""
        obs = hourly_rain(self.now, hours=10, total=100, text="大雨", wind="4")
        with patch.object(garden, "_environment_observations", return_value=obs):
            snap0 = garden.crop_snapshot(now=self.now, path=self.path)
            self.assertEqual(snap0["environment"]["yard_water"], "flooded")
            raw0 = self._raw()
            self.assertEqual(raw0["environment"]["flood_watch"]["since"], self.now.isoformat())

            drained_at = self.now + timedelta(minutes=5)
            garden.drain_yard(now=drained_at, path=self.path)

            # 对照组：不接 drained_at 的话，同一时刻的自然退水指数是多少。
            check_at = drained_at + timedelta(hours=10)
            rain_by_date = garden_weather.observed_rain_by_date(obs, check_at)
            index_without_drain = garden_weather.yard_water_index(obs, rain_by_date, check_at)
            index_with_drain = garden_weather.yard_water_index(
                obs, rain_by_date, check_at, drained_at=drained_at,
            )
            self.assertGreaterEqual(index_without_drain, garden_weather.YARD_PUDDLES_THRESHOLD)
            self.assertLess(index_with_drain, garden_weather.YARD_PUDDLES_THRESHOLD)

            snap1 = garden.crop_snapshot(now=check_at, path=self.path)
        self.assertEqual(snap1["environment"]["yard_water"], "none")
        raw1 = self._raw()
        # 事件层（游标）与惩罚层（since）在同一次结算里一起确认退水。
        self.assertEqual(raw1["environment"]["yard_water_announced"]["level"], "none")
        self.assertIsNone(raw1["environment"]["flood_watch"]["since"])

    def test_drain_rolls_back_the_soak_clock_and_delays_the_quality_threshold(self):
        """排水拨回浸泡时钟：24h 阈值应该从 drained_at 重新起算，而不是卡在
        原来的 since；持续暴雨保证内涝档位在整个观察窗口里不会被自然退水
        提前打断，专注验证时钟本身的联动。"""
        _seed_growing_plot(self.path, self.now)
        obs = sustained_rain(
            self.now - timedelta(hours=8), self.now + timedelta(hours=40), rate=15.0,
        )
        with patch.object(garden, "_environment_observations", return_value=obs):
            snap0 = garden.crop_snapshot(now=self.now, path=self.path)
            self.assertEqual(snap0["environment"]["yard_water"], "flooded")
            raw0 = self._raw()
            self.assertEqual(raw0["environment"]["flood_watch"]["since"], self.now.isoformat())

            drained_at = self.now + timedelta(hours=10)
            garden.drain_yard(now=drained_at, path=self.path)

            # 20 小时过后（= since 之后 30 小时，早该越过没有重置时的 24h
            # 阈值），但只是 drained_at 之后 20 小时——还没到新起点的 24h。
            still_fresh_at = drained_at + timedelta(hours=20)
            garden.crop_snapshot(now=still_fresh_at, path=self.path)
            raw_before = self._raw()
            self.assertIsNone(raw_before["plots"][0].get("quality"))

            # 再过 5 小时，drained_at 之后满 25h，欠佳标记应该出现。
            past_threshold_at = drained_at + timedelta(hours=25)
            garden.crop_snapshot(now=past_threshold_at, path=self.path)
        raw_after = self._raw()
        self.assertEqual(raw_after["plots"][0].get("quality"), "poor")


class DrainSemanticRoutingTests(unittest.TestCase):
    """`home._garden_semantic_command` 的『排水』识别，以及与『松土』别名
    去冲突（"排水"/"排积水"曾经都指向松土，"排水"现在要让给院级排水）。"""

    def test_bare_command_and_synonyms_all_canonicalize_to_drain(self):
        for phrase in ("排水", "清排水沟", "清水沟", "排排水", "疏通排水"):
            with self.subTest(phrase=phrase):
                self.assertEqual(home._garden_semantic_command(phrase), "排水")

    def test_loosen_soil_alias_still_routes_to_loosen_soil_not_drain(self):
        # "排积水"（个别地块治积水异常）仍然是「松土」的别名，不该被新命令
        # 抢走；这条不测的话，前面移除"排水"别名的改动可能顺手误删了它。
        selector = home._garden_semantic_command("二号地排积水")
        self.assertTrue(selector.startswith("松土"))

    def test_explicit_loosen_soil_command_is_not_hijacked_by_drain(self):
        self.assertEqual(home._garden_semantic_command("松土 2"), "松土 2")


class DrainHomeIntegrationTests(unittest.TestCase):
    """通过 `home.handle_garden` 走排水的完整路由：拒绝提示带解法、dry-run
    不改状态、手账渲染、出门文案接入。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        environment = patch.dict("os.environ", {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        writer = patch(
            "garden.garden_generator.generate_crop_copy",
            side_effect=GardenGeneratorError("离线测试"),
        )
        writer.start()
        self.addCleanup(writer.stop)
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
        # 兜底：任何没有在测试内自己再包一层更具体 patch 的 handle_garden
        # 调用，一律拿到空观测，绝不越过这层兜底去读真实 weather_cache.json
        # （真实环境开关在本类整体打开，不兜底的话查看/手账这类调用会真的
        # 触发 `weather.load_weather_observations()`）。
        self.obs_patcher = patch.object(garden, "_environment_observations", return_value=[])
        self.obs_patcher.start()
        self.addCleanup(self.obs_patcher.stop)
        self.now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)

    def _request(self, argv):
        return home.parse_request(argv)

    def test_none_level_rejection_carries_a_matching_hint_not_the_watering_one(self):
        with patch.object(garden, "_environment_observations", return_value=[]):
            with self.assertRaises(home.HomeError) as ctx:
                home.handle_garden(self._request(["院子", "排水"]))
        message = str(ctx.exception)
        self.assertIn("排水沟挺通畅的", message)
        self.assertIn("home 院子 排水", message)
        self.assertNotIn("全部浇水", message)

    def test_dry_run_does_not_touch_state(self):
        with patch.object(garden, "_environment_observations", return_value=[]):
            output = home.handle_garden(self._request(["院子", "--dry-run", "排水"]))
        self.assertIn("将疏通排水沟", output)
        self.assertFalse(self.path.exists())

    def test_successful_drain_reports_facts_and_records_journal(self):
        # `home.handle_garden` 内部一律用真实 `datetime.now(TZ)`，不接受注入；
        # 用相对于调用时刻动态生成的暴雨观测，而不是绑定 `self.now` 的固定
        # 列表，天然避免依赖测试真正执行的墙钟时间与虚构日期是否巧合对齐。
        def fresh_storm(now):
            return hourly_rain(now, hours=10, total=100, text="大雨", wind="4")

        with patch.object(garden, "_environment_observations", side_effect=fresh_storm):
            output = home.handle_garden(self._request(["院子", "排水"]))
        self.assertIn("内涝", output)
        self.assertIn("连雨", output)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(raw["journal"]["drainage"]), 1)

    def test_journal_rendering_includes_a_drainage_line(self):
        obs = hourly_rain(self.now, hours=6, total=50, text="中雨", wind="4")
        with patch.object(garden, "_environment_observations", return_value=obs):
            garden.drain_yard(now=self.now, path=self.path)
        output = home.handle_garden(self._request(["院子", "手账"]))
        self.assertIn("排水记录", output)
        self.assertIn("水洼", output)
        self.assertNotIn("还没有排过水", output)

    def test_journal_rendering_reports_no_drainage_yet_on_a_fresh_garden(self):
        output = home.handle_garden(self._request(["院子", "手账"]))
        self.assertIn("排水记录：还没有排过水", output)

    def test_whitelist_outside_command_still_rejected_via_condition_action_gate(self):
        # 回归：非白名单动作走 handle_garden 其它分支仍然拒绝，不受本次
        # 新增『排水』分支影响。
        with self.assertRaises(home.HomeError):
            home.handle_garden(self._request(["院子", "凭空捏造的动作"]))

    def test_drain_action_claims_outing_flavor_under_synthetic_storm(self):
        """排水是动手类动作，要自动吃第六版出门文案；30 分钟节流沿用既有
        `claim_outing_flavor` 机制，这里只验证排水正确接进了这条链路
        （用后续动作复用同一张节流游标来间接证明），不重新证明节流机制
        本身（已由第六版测试覆盖）。"""
        def fresh_typhoon(now):
            return hourly_rain(now, hours=10, total=100, text="大雨", wind="9")

        with patch.object(garden, "_environment_observations", side_effect=fresh_typhoon):
            output = home.handle_garden(self._request(["院子", "排水"]))
            self.assertGreaterEqual(output.count("\n"), 2)
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            outing = raw["environment"].get("outing_flavor")
            self.assertIsNotNone(outing)
            self.assertEqual(outing["mode"], "typhoon")
            claimed_at = outing["at"]

            # 紧接着的下一次逛逛（同样接出门文案）应该尊重 30 分钟内同档
            # 节流，不重新领取、不刷新游标时间戳。
            home.handle_garden(self._request(["院子", "逛逛"]))
            raw_after = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(raw_after["environment"]["outing_flavor"]["at"], claimed_at)


class DrainWebSurfaceTests(unittest.TestCase):
    """网页/白名单静态接线；不启动真实服务器、不经由 `run_command`
    （那条路径会调用真实 `home` 二进制，命中生产 `garden.json`，测试
    绝不允许触碰）。全部走源码文本/AST 结构检查，跟既有
    `test_garden_web_security.py` 同款风格。"""

    def test_allowed_commands_literal_includes_drain_and_excludes_junk(self):
        source = (ROOT / "garden-web/server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        allowed = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "ALLOWED_COMMANDS"
            ):
                allowed = ast.literal_eval(node.value)
                break
        self.assertIsNotNone(allowed, "没能在 server.py 里静态解析出 ALLOWED_COMMANDS")
        self.assertIn("排水", allowed)
        self.assertNotIn("凭空捏造的动作", allowed)

    def test_state_payload_passes_environment_through_for_the_frontend(self):
        server = (ROOT / "garden-web/server.py").read_text(encoding="utf-8")
        self.assertIn('"environment": snapshot.get("environment")', server)

    def test_drain_button_conditionally_rendered_and_wired_to_the_action(self):
        html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")
        self.assertIn('id="btn-drain"', html)
        self.assertIn('style="display:none"', html)
        self.assertIn('doAction("排水"', html)
        self.assertIn("renderDrainButton", html)
        self.assertIn('level !== "none"', html)

    def test_journal_popup_renders_a_drainage_section(self):
        html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")
        self.assertIn("排水记录", html)
        self.assertIn("j.drainage", html)

    def test_drain_pool_passes_the_same_style_self_check_as_other_pools(self):
        pool = garden_content.GARDEN_ACTION_TEXT["drain"]
        self.assertGreaterEqual(len(pool), 8)
        self.assertEqual(len(set(pool)), len(pool))


if __name__ == "__main__":
    unittest.main()
