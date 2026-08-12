"""第七版阶段 E：异常联动与提示解法收尾。

覆盖设计稿《迷你小院子第七版内涝惩罚与作物品质设计.md》第十一节阶段 E
验收清单。阶段 A（`2c1cd33`）/B（`c22972c`）/C（`260c821`）/D（`02ac343`）
已经落地了内涝惩罚的纯函数与数据地基、结算接入、排水动作与品质出入库
全链路；本文件只测阶段 E 新增的三件事：

1. `_settle_crop_conditions` 里"damaged → 欠佳"联动（设计稿六.2，用户已
   拍板要做）；
2. 第七节"提示与报错要带解法"里既有异常这一半的收尾——事件文案（重点是
   之前只有 warning 严重度带提示、damaged 分支缺提示的缺口）、查看/详细
   里的异常行、处理动作用错的报错，逐一核对已带解法或补齐；
3. 全部第七版新文案池的事实一致性终检（欠佳收获池不再归因"泡雨/积水"、
   地块品质在详细/单地块查看与网页地块详情里可见但裸查看不缀这一笔）。

全程离线：纯函数测试直接构造最小 state 字典（同 `test_garden_stage_b_v7.
py` 风格），文件路径测试统一 `patch.object(garden, "GARDEN_FILE", ...)`
指向 `tempfile.TemporaryDirectory()`；绝不读写真实 `garden.json`、绝不经
由 `garden-web/server.py` 的 `run_command` 或真实 `home` 二进制。
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


TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]

DAMAGE_AFTER = garden.CONDITION_DAMAGE_AFTER
WITHER_AFTER = garden.CONDITION_WITHER_AFTER


def _bare_state():
    return {
        "plots": [],
        "pending_events": [],
        "journal": {"crop_incidents": []},
        "meta": {"natural_condition_cooldown_until": None},
    }


def _growing_plot(plot_id="p1", *, crop_id="tomato", cycle_id="cycle-1"):
    return {
        "plot_id": plot_id,
        "crop_id": crop_id,
        "cycle_id": cycle_id,
        "status": "growing",
        "stage": "growing",
        "growth_points": 1.0,
        "yield_penalty": 0,
    }


class DamagedQualityLinkPureFunctionTests(unittest.TestCase):
    """直接对 ``_settle_crop_conditions`` 做纯函数测试，不落盘。"""

    def setUp(self):
        self.announced_at = datetime(2026, 8, 10, 8, 0, tzinfo=TZ)

    def _state_with_condition(self, *, condition_type="pest"):
        state = _bare_state()
        plot = _growing_plot()
        state["plots"] = [plot]
        garden._create_crop_condition(
            state, plot, condition_type, self.announced_at, announced=True,
        )
        return state, plot

    def test_warning_treated_in_time_never_gets_quality_flag(self):
        """及时处理：还没拖到 damaged 阈值就结算，不打欠佳标记。"""
        state, plot = self._state_with_condition()
        now = self.announced_at + DAMAGE_AFTER - timedelta(minutes=1)
        changed = garden._settle_crop_conditions(state, now)
        self.assertFalse(changed)
        self.assertNotIn("quality", plot)

    def test_damaged_threshold_flags_quality_poor(self):
        """拖到 damaged：数量（yield_penalty）和品相（quality）双重留疤。"""
        state, plot = self._state_with_condition()
        now = self.announced_at + DAMAGE_AFTER + timedelta(minutes=1)
        changed = garden._settle_crop_conditions(state, now)
        self.assertTrue(changed)
        self.assertEqual(plot["condition"]["severity"], "damaged")
        self.assertEqual(plot["yield_penalty"], 1)
        self.assertEqual(plot["quality"], "poor")

    def test_already_poor_not_rewritten_on_later_settlement(self):
        """已经 poor 的地块，后续结算（仍未到 withered）不重复触发写入。"""
        state, plot = self._state_with_condition()
        first_now = self.announced_at + DAMAGE_AFTER + timedelta(minutes=1)
        self.assertTrue(garden._settle_crop_conditions(state, first_now))
        self.assertEqual(plot["quality"], "poor")
        later_now = first_now + timedelta(hours=1)
        changed = garden._settle_crop_conditions(state, later_now)
        self.assertFalse(changed)
        self.assertEqual(plot["quality"], "poor")

    def test_direct_jump_past_withered_does_not_flag_quality(self):
        """一次长跳跃直接越过 withered 阈值：只枯死，不画蛇添足打欠佳标记。"""
        state, plot = self._state_with_condition()
        now = self.announced_at + WITHER_AFTER + timedelta(minutes=1)
        changed = garden._settle_crop_conditions(state, now)
        self.assertTrue(changed)
        self.assertEqual(plot["status"], "withered")
        self.assertEqual(plot["condition"]["severity"], "withered")
        self.assertNotIn("quality", plot)

    def test_two_step_aging_keeps_quality_set_at_damaged_step(self):
        """分两次结算（先到 damaged 再到 withered）时，damaged 那一步已经
        打过的欠佳标记不会被 withered 步骤撤销——它只是不再"新画蛇添足"，
        不代表要清掉已经写好的事实。"""
        state, plot = self._state_with_condition()
        damaged_now = self.announced_at + DAMAGE_AFTER + timedelta(minutes=1)
        garden._settle_crop_conditions(state, damaged_now)
        self.assertEqual(plot["quality"], "poor")
        withered_now = self.announced_at + WITHER_AFTER + timedelta(minutes=1)
        garden._settle_crop_conditions(state, withered_now)
        self.assertEqual(plot["status"], "withered")
        self.assertEqual(plot["quality"], "poor")


class DamagedQualityFilePathTests(unittest.TestCase):
    """经由公开入口（含锁与校验）的端到端核对：不可逆标记在处理动作成功
    之后仍然保留。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.garden_patcher = patch.object(garden, "GARDEN_FILE", self.path)
        self.garden_patcher.start()
        self.addCleanup(self.garden_patcher.stop)
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)

    def _write_damaged_plot(self):
        # 沿用既有 test_garden_stage_e.py 的写法：不升 v5、不铺陈现实环境
        # 字段——这个场景只需要一块带自然异常的地，与内涝时钟无关。
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [garden._empty_plot(pid) for pid in garden._PLOT_IDS]
        state["meta"]["crop_seed_box_initialized"] = True
        announced_at = self.now - DAMAGE_AFTER - timedelta(minutes=1)
        plot = state["plots"][0]
        plot.update({
            "crop_id": "tomato",
            "planted_at": (announced_at - timedelta(days=1)).isoformat(),
            "last_settled_at": announced_at.isoformat(),
            "growth_points": 1.0,
            "stage": "growing",
            "water_bonus_dates": [],
            "watering_by_date": {},
            "ready_at": None,
            "yield_penalty": 0,
            "status": "growing",
            "cycle_id": "cycle-damaged",
            "stage_events_seen": [],
        })
        garden._create_crop_condition(state, plot, "pest", announced_at, announced=True)
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def test_quality_poor_survives_a_successful_treatment(self):
        self._write_damaged_plot()
        # 先经由只读快照触发结算，让 damaged 分支真正落盘打上 quality。
        garden.crop_snapshot(now=self.now, path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["plots"][0]["quality"], "poor")
        self.assertEqual(raw["plots"][0]["yield_penalty"], 1)

        result = garden.resolve_crop_condition("除虫", "一号地", now=self.now, path=self.path)
        self.assertEqual(result["outcome"], "resolved")
        raw_after = json.loads(self.path.read_text(encoding="utf-8"))
        # 处理成功只解除异常状态；已经发生的品相受损不可逆。
        self.assertEqual(raw_after["plots"][0]["condition"]["status"], "resolved")
        self.assertEqual(raw_after["plots"][0]["quality"], "poor")


class ConditionEventResultLineTests(unittest.TestCase):
    """既有异常事件文案：凡"出事了"都要带处理动作提示（设计稿第七节）。"""

    def _event(self, severity, *, condition_type="pest", yield_penalty=0):
        return {
            "plot_id": "p1", "crop_id": "tomato",
            "condition_type": condition_type, "severity": severity,
            "yield_penalty": yield_penalty,
        }

    def test_warning_result_line_already_names_the_action(self):
        line = garden._condition_event_result_line(self._event("warning"))
        self.assertIn("需要除虫", line)

    def test_damaged_result_line_now_names_the_action_too(self):
        # 阶段E新增的缺口：damaged 仍可处理，之前的结果行只报了减产
        # 事实，没有提示怎么办；本次补上，且不预告任何机制数值。
        line = garden._condition_event_result_line(
            self._event("damaged", yield_penalty=1),
        )
        self.assertIn("永久减少1份", line)
        self.assertIn("除虫", line)
        for forbidden in ("小时", "分钟", "24", "36", "72"):
            self.assertNotIn(forbidden, line)

    def test_damaged_result_line_action_matches_condition_type(self):
        for condition_type, action in garden.CONDITION_ACTIONS.items():
            with self.subTest(condition_type=condition_type):
                line = garden._condition_event_result_line(
                    self._event("damaged", condition_type=condition_type, yield_penalty=1),
                )
                self.assertIn(action, line)

    def test_withered_result_line_only_points_to_clear(self):
        # withered 已成定局，不提供"处理"话术，只给「清理」指引——
        # 这部分是既有行为，本次没有改动，回归锁住不倒退。
        line = garden._condition_event_result_line(
            self._event("withered", yield_penalty=1),
        )
        self.assertIn("清理", line)
        for action in garden.CONDITION_ACTIONS.values():
            self.assertNotIn(f"需要{action}", line)

    def test_result_line_still_round_trips_through_the_state_validator(self):
        # _validate_condition_event_copy 用 endswith 精确比对结果行；damaged
        # 分支的文案变化不能破坏这条既有校验闭环。
        event = self._event("damaged", yield_penalty=1)
        copy = {
            "text": "占位正文。\n" + garden._condition_event_result_line(event),
            "source": "fallback",
        }
        garden._validate_condition_event_copy({**event, "copy": copy})


class WrongActionReportTests(unittest.TestCase):
    """处理动作用错时的报错要直接报正确动作（设计稿第七节第1点）。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.garden_patcher = patch.object(garden, "GARDEN_FILE", self.path)
        self.garden_patcher.start()
        self.addCleanup(self.garden_patcher.stop)
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)

    def _write_warning_plot(self, condition_type="waterlogged"):
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [garden._empty_plot(pid) for pid in garden._PLOT_IDS]
        state["meta"]["crop_seed_box_initialized"] = True
        plot = state["plots"][0]
        plot.update({
            "crop_id": "tomato",
            "planted_at": (self.now - timedelta(days=1)).isoformat(),
            "last_settled_at": self.now.isoformat(),
            "growth_points": 1.0,
            "stage": "growing",
            "water_bonus_dates": [],
            "watering_by_date": {},
            "ready_at": None,
            "yield_penalty": 0,
            "status": "growing",
            "cycle_id": "cycle-wrong",
            "stage_events_seen": [],
        })
        garden._create_crop_condition(state, plot, condition_type, self.now, announced=True)
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def test_resolve_crop_condition_reports_the_correct_action_on_mismatch(self):
        # 积水异常真正需要「松土」；"排水"在第七版被院级排水占用，
        # 这里确认报错精确点名「松土」，不会被"排水"的语义混淆。
        self._write_warning_plot("waterlogged")
        result = garden.resolve_crop_condition("除虫", "一号地", now=self.now, path=self.path)
        self.assertEqual(result["outcome"], "wrong_action")
        self.assertEqual(result["correct_action"], "松土")

    def test_home_treatment_text_surfaces_the_correct_action(self):
        self._write_warning_plot("waterlogged")
        result = garden.resolve_crop_condition("除虫", "一号地", now=self.now, path=self.path)
        text = home._garden_crop_treatment_text(result)
        self.assertIn("需要松土", text)
        self.assertNotIn("需要排水", text)


class QueryDisplayActionHintTests(unittest.TestCase):
    """查看/详细里的异常行要带处理动作提示（既有能力，本次锁定回归）。"""

    def test_plot_line_active_condition_names_the_action(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)
        plot = _growing_plot()
        garden._create_crop_condition(
            {"journal": {"crop_incidents": []}}, plot, "nutrient_deficiency", now, announced=True,
        )
        crop_state = {"real_environment_enabled": False}
        line = home._garden_plot_line(plot, crop_state, now)
        self.assertIn("请先施肥", line)


class PlotQualityVisibilityTests(unittest.TestCase):
    """地块品质在详细/单地块查看中可见；裸查看（compact 行）不缀这一笔
    （监理对阶段D遗留问题的裁决，见任务书第4点）。"""

    def _poor_plot(self, *, status="growing", stage="growing"):
        return {
            "plot_id": "p2", "crop_id": "cucumber", "cycle_id": "cycle-poor",
            "status": status, "stage": stage, "growth_points": 1.0,
            "yield_penalty": 0, "quality": "poor",
            "watering_by_date": {}, "water_bonus_dates": [],
        }

    def test_detailed_plot_line_shows_poor_quality_tag(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)
        plot = self._poor_plot()
        line = home._garden_plot_line(plot, {"real_environment_enabled": False}, now)
        self.assertIn("品相欠佳", line)

    def test_ready_poor_plot_also_shows_the_tag(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)
        plot = self._poor_plot(status="ready", stage="ready")
        line = home._garden_plot_line(plot, {"real_environment_enabled": False}, now)
        self.assertIn("品相欠佳", line)

    def test_healthy_plot_line_has_no_quality_tag(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)
        plot = _growing_plot()
        line = home._garden_plot_line(plot, {"real_environment_enabled": False}, now)
        self.assertNotIn("品相欠佳", line)

    def test_withered_plot_does_not_get_the_tag_even_if_flagged(self):
        # withered 没有收成，标不标都无所谓；不画蛇添足。
        now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)
        plot = self._poor_plot(status="withered", stage="growing")
        line = home._garden_plot_line(plot, {"real_environment_enabled": False}, now)
        self.assertNotIn("品相欠佳", line)

    def test_bare_view_compact_line_never_mentions_quality(self):
        # 8/6 刚瘦身过的裸查看摘要：不可逆、无动作可做的信息不属于
        # "actionable"，不应该出现在这里。
        plots = [self._poor_plot()]
        line = home._garden_plots_compact_line(plots, {"real_environment_enabled": False})
        self.assertIsNotNone(line)
        self.assertNotIn("欠佳", line)
        self.assertNotIn("quality", line)


class WebPlotQualityWiringTests(unittest.TestCase):
    """网页端地块品质接线：不启动真实服务器，静态核对 server.py 组装的
    字段与 index.html 消费该字段的落点（同既有 `StaticWiringTests` 风格）。"""

    def test_server_plot_payload_derives_quality_with_same_rule_as_home(self):
        source = (ROOT / "garden-web/server.py").read_text(encoding="utf-8")
        self.assertIn(
            'if plot.get("quality") == "poor" and plot.get("status") in ("growing", "ready"):',
            source,
        )
        self.assertIn('payload["quality"] = "poor"', source)

    def test_plot_sheet_renders_the_quality_tag(self):
        html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")
        plot_sheet = html.split("function openPlotSheet(p, n)")[1].split("function openAnimalSheet")[0]
        self.assertIn("p.quality", plot_sheet)
        self.assertIn("quality-tag", plot_sheet)

    def test_scene_map_ambient_view_does_not_render_a_quality_badge(self):
        # 主视图是"裸查看"的网页等价物：不可逆、无动作可做的信息不该常驻
        # 挤占地图上的显眼位置，只在点开地块详情时才看得到。
        html = (ROOT / "garden-web/index.html").read_text(encoding="utf-8")
        scene_render = html.split("function renderSceneView(d)")[1].split("\nfunction ")[0]
        self.assertNotIn("quality", scene_render)


class HarvestPoorTextFactCheckTests(unittest.TestCase):
    """欠佳收获池事实一致性终检：quality="poor" 现在有两个互不可区分的
    来源（内涝浸泡、异常拖到 damaged），plot 上没有留存"是哪一种"的事实
    字段，池子不能再把原因锁死成"雨/积水"（阶段D遗留，阶段E清理）。"""

    _CAUSE_WORDS = ("泡雨", "积水", "雨水", "内涝", "雨泡")
    _PRAISE_WORDS = ("水灵", "饱满", "脆生", "新鲜", "肥美", "圆润", "笔直")

    def test_pool_no_longer_attributes_a_specific_cause(self):
        for line in garden_content.HARVEST_POOR_TEXT:
            for word in self._CAUSE_WORDS:
                with self.subTest(line=line, word=word):
                    self.assertNotIn(word, line)

    def test_pool_still_avoids_praise_words(self):
        for line in garden_content.HARVEST_POOR_TEXT:
            for word in self._PRAISE_WORDS:
                with self.subTest(line=line, word=word):
                    self.assertNotIn(word, line)

    def test_pool_still_has_at_least_six_unique_lines(self):
        pool = garden_content.HARVEST_POOR_TEXT
        self.assertGreaterEqual(len(pool), 6)
        self.assertEqual(len(set(pool)), len(pool))

    def test_pool_lines_still_render_with_a_crop_name(self):
        for line in garden_content.HARVEST_POOR_TEXT:
            rendered = line.format(name="小番茄")
            self.assertIn("小番茄", rendered)
            self.assertTrue(rendered.endswith(("。", "！", "？")))


class FloodPoolFactCheckRegressionTests(unittest.TestCase):
    """内涝相关文案池（阶段B/C已完成，这里只做阶段E要求的终检回归，不
    改动这些池子）：泡烂句不写成可挽救、排水句不声称雨已经停了。"""

    def test_rot_pool_never_implies_it_can_still_be_saved(self):
        rescue_words = ("救回来", "还能救", "补救", "恢复生长", "还会好")
        for line in garden_content.FLOOD_DAMAGE_EVENT_TEXT["rot"]:
            for word in rescue_words:
                with self.subTest(line=line, word=word):
                    self.assertNotIn(word, line)

    def test_drain_pool_never_claims_the_rain_has_stopped(self):
        rain_stop_words = ("雨停了", "不下雨了", "天晴了", "放晴了")
        for line in garden_content.GARDEN_ACTION_TEXT["drain"]:
            for word in rain_stop_words:
                with self.subTest(line=line, word=word):
                    self.assertNotIn(word, line)

    def test_quality_stage_flood_event_gives_a_directional_warning_without_a_countdown(self):
        digits = tuple(str(d) for d in range(10))
        for line in garden_content.FLOOD_DAMAGE_EVENT_TEXT["quality"]:
            self.assertIn("排水", line)
            for digit in digits:
                with self.subTest(line=line, digit=digit):
                    self.assertNotIn(digit, line)


class ActionNameCrossCheckTests(unittest.TestCase):
    """提示句里的动作名必须与真实命令逐字对表，防止提示和命令拼写分叉。"""

    _REAL_COMMANDS = {"松土", "除虫", "修剪", "施肥", "排水", "清理"}

    def test_condition_actions_are_all_real_commands(self):
        for action in garden.CONDITION_ACTIONS.values():
            self.assertIn(action, self._REAL_COMMANDS)

    def test_server_whitelist_contains_every_condition_action_and_drain(self):
        source = (ROOT / "garden-web/server.py").read_text(encoding="utf-8")
        allowed_block = source.split("ALLOWED_COMMANDS = {")[1].split("}")[0]
        for action in (*garden.CONDITION_ACTIONS.values(), "清理", "排水"):
            with self.subTest(action=action):
                self.assertIn(f'"{action}"', allowed_block)


class GeneratorWhitelistNoInventionTests(unittest.TestCase):
    """DeepSeek 写手白名单：确认"可用动作"事实字段传入后，写手没有机会
    把它写进正文（正文提及任何处理动作都会被拒），处理提示只能来自代码
    拥有的"结果："行——这是既有架构（阶段B之前就有），本次不新增字段、
    只核实现状确实堵死了发明动作名的空间。"""

    def test_condition_event_body_may_not_mention_any_treatment_action(self):
        import garden_generator

        facts = garden_generator._public_crop_copy_facts({
            "copy_type": "condition_event", "plot_label": "一号地",
            "crop_name": "小番茄", "condition_type": "pest",
            "condition_name": "虫害", "severity": "damaged",
            "yield_penalty": 1, "correct_action": "除虫",
        })
        with self.assertRaises(garden_generator.GardenGeneratorError):
            garden_generator._validate_crop_copy_text(
                "一号地的小番茄需要除虫，虫眼已经不少了。", facts,
            )

    def test_treatment_body_may_not_mention_the_correct_action_when_it_differs_from_what_happened(self):
        import garden_generator

        facts = garden_generator._public_crop_copy_facts({
            "copy_type": "treatment", "plot_label": "二号地",
            "crop_name": "黄瓜", "condition_type": "waterlogged",
            "condition_name": "积水", "outcome": "wrong_action",
            "action": "除虫", "correct_action": "松土",
            "yield_penalty": 0,
        })
        with self.assertRaises(garden_generator.GardenGeneratorError):
            garden_generator._validate_crop_copy_text(
                "二号地浇了除虫的心思，其实该「松土」才对，黄瓜没什么反应。", facts,
            )


if __name__ == "__main__":
    unittest.main()
