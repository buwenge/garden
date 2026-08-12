"""第七版阶段 B：内涝惩罚的结算接入（时钟、欠佳、泡烂、禁播）。

覆盖设计稿《迷你小院子第七版内涝惩罚与作物品质设计.md》第十一节阶段 B
验收清单。阶段 A（`2c1cd33`）已经落地了纯函数与数据地基（
`garden_weather.flood_soak_hours`/`yard_water_index(drained_at=...)`、
`flood_watch`/`produce_poor`/`quality`/`drainage` 字段迁移与校验、
`flood_rot` 枚举扩展），本文件只测阶段 B 新增的结算维护函数
``garden._settle_flood_damage``/``garden._rot_plot``/
``garden._queue_flood_damage_event``、``_validate_crop_condition`` 的
``cause`` 字段扩展、``flood_damage`` 待展示事件类型，以及 ``plant_crop``
的内涝禁播分支。不做排水动作（阶段 C）与品质出入库全链路（阶段 D）。
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
import garden_content
import garden_weather


TZ = ZoneInfo("Asia/Shanghai")

QUALITY_AFTER = garden_weather.FLOOD_SOAK_QUALITY_AFTER
ROT_AFTER = garden_weather.FLOOD_SOAK_ROT_AFTER


def _growing_plot(plot_id="p1", *, crop_id="cucumber", cycle_id="cycle-1", quality=None, condition=None):
    plot = {
        "plot_id": plot_id,
        "crop_id": crop_id,
        "cycle_id": cycle_id,
        "status": "growing",
        "stage": "growing",
        "growth_points": 1.0,
    }
    if quality is not None:
        plot["quality"] = quality
    if condition is not None:
        plot["condition"] = condition
    return plot


def _environment(*, since=None, drained_at=None, level="flooded", cursor_at=None):
    return {
        "flood_watch": {
            "since": since.isoformat() if since else None,
            "drained_at": drained_at.isoformat() if drained_at else None,
        },
        "yard_water_announced": {
            "level": level,
            "at": cursor_at.isoformat() if cursor_at else None,
        },
    }


def _state(plots, *, since=None, drained_at=None, level="flooded", cursor_at=None):
    return {
        "version": garden.STATE_VERSION,
        "plots": plots,
        "environment": _environment(
            since=since, drained_at=drained_at, level=level, cursor_at=cursor_at,
        ),
        "pending_events": [],
        "journal": {"crop_incidents": []},
    }


class FloodClockSettlementTests(unittest.TestCase):
    """内涝时钟自身的维护：设置、抖动容忍、清空。"""

    def setUp(self):
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)

    def test_disabled_outside_real_environment(self):
        state = _state([], level="flooded")
        self.assertFalse(garden._settle_flood_damage(state, self.now))
        self.assertIsNone(state["environment"]["flood_watch"]["since"])

    def test_disabled_for_legacy_state_version(self):
        state = _state([], level="flooded")
        state["version"] = garden.LEGACY_STATE_VERSION
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            self.assertFalse(garden._settle_flood_damage(state, self.now))

    def test_since_set_when_entering_flooded_and_null(self):
        state = _state([], level="flooded")
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            self.assertTrue(garden._settle_flood_damage(state, self.now))
        self.assertEqual(
            state["environment"]["flood_watch"]["since"], self.now.isoformat(),
        )

    def test_since_not_rewritten_once_already_set(self):
        earlier = self.now - timedelta(hours=3)
        state = _state([], since=earlier, level="flooded")
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            changed = garden._settle_flood_damage(state, self.now)
        self.assertFalse(changed)
        self.assertEqual(
            state["environment"]["flood_watch"]["since"], earlier.isoformat(),
        )

    def test_since_persists_while_cursor_still_reports_flooded(self):
        # 游标本身承担 2 小时抖动防抖（既有 `_reconcile_yard_water_event`
        # 冷却机制）：只要游标仍报告 flooded，就代表还没有确认离开满
        # 2 小时；本函数信任这张游标，不重复实现容忍逻辑。
        earlier = self.now - timedelta(hours=3)
        state = _state([], since=earlier, level="flooded")
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            garden._settle_flood_damage(state, self.now)
        self.assertEqual(
            state["environment"]["flood_watch"]["since"], earlier.isoformat(),
        )

    def test_since_cleared_once_cursor_confirms_exit(self):
        earlier = self.now - timedelta(hours=5)
        state = _state([], since=earlier, level="puddles")
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            changed = garden._settle_flood_damage(state, self.now)
        self.assertTrue(changed)
        self.assertIsNone(state["environment"]["flood_watch"]["since"])

    def test_missing_flood_watch_field_degrades_silently(self):
        state = _state([], level="flooded")
        del state["environment"]["flood_watch"]
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            self.assertFalse(garden._settle_flood_damage(state, self.now))


class FloodQualityThresholdTests(unittest.TestCase):
    def setUp(self):
        self.since = datetime(2026, 8, 10, 0, 0, tzinfo=TZ)

    def _settle(self, state, now):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            return garden._settle_flood_damage(state, now)

    def test_quality_marks_all_growing_and_ready_plots_once(self):
        plots = [
            _growing_plot("p1"),
            {**_growing_plot("p2", crop_id="tomato", cycle_id="cycle-2"), "status": "ready", "stage": "ready"},
            {"plot_id": "p3", "status": "empty"},
        ]
        state = _state(plots, since=self.since)
        now = self.since + QUALITY_AFTER + timedelta(minutes=1)
        self.assertTrue(self._settle(state, now))
        self.assertEqual(plots[0]["quality"], "poor")
        self.assertEqual(plots[1]["quality"], "poor")
        self.assertNotIn("quality", plots[2])
        events = [event for event in state["pending_events"] if event["type"] == "flood_damage"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["stage"], "quality")
        self.assertIn(events[0]["text"], garden_content.FLOOD_DAMAGE_EVENT_TEXT["quality"])

    def test_quality_not_reapplied_or_requeued_on_later_settlement(self):
        plots = [_growing_plot("p1")]
        state = _state(plots, since=self.since)
        first_now = self.since + QUALITY_AFTER + timedelta(minutes=1)
        self._settle(state, first_now)
        self.assertEqual(len(state["pending_events"]), 1)
        later_now = first_now + timedelta(hours=6)
        changed = self._settle(state, later_now)
        self.assertFalse(changed)
        self.assertEqual(len(state["pending_events"]), 1)

    def test_before_threshold_no_effect(self):
        plots = [_growing_plot("p1")]
        state = _state(plots, since=self.since)
        now = self.since + QUALITY_AFTER - timedelta(minutes=1)
        changed = self._settle(state, now)
        self.assertFalse(changed)
        self.assertNotIn("quality", plots[0])
        self.assertEqual(state["pending_events"], [])

    def test_no_eligible_plots_produces_no_event(self):
        state = _state([{"plot_id": "p1", "status": "empty"}], since=self.since)
        now = self.since + QUALITY_AFTER + timedelta(hours=1)
        changed = self._settle(state, now)
        self.assertFalse(changed)
        self.assertEqual(state["pending_events"], [])


class FloodRotThresholdTests(unittest.TestCase):
    def setUp(self):
        self.since = datetime(2026, 8, 10, 0, 0, tzinfo=TZ)

    def _settle(self, state, now):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            return garden._settle_flood_damage(state, now)

    def test_rot_withers_plot_and_creates_terminal_condition(self):
        plots = [_growing_plot("p1", crop_id="cucumber", cycle_id="cycle-1")]
        state = _state(plots, since=self.since)
        rot_at = self.since + ROT_AFTER
        now = rot_at + timedelta(minutes=1)
        self.assertTrue(self._settle(state, now))
        plot = plots[0]
        self.assertEqual(plot["status"], "withered")
        self.assertEqual(plot["yield_penalty"], 1)
        condition = plot["condition"]
        self.assertEqual(condition["type"], "flood_rot")
        self.assertEqual(condition["cause"], "yard_flood")
        self.assertEqual(condition["status"], "failed")
        self.assertEqual(condition["severity"], "withered")
        self.assertEqual(condition["yield_penalty"], 1)
        self.assertEqual(condition["worsened_at"], condition["failed_at"])
        self.assertEqual(condition["failed_at"], rot_at.isoformat())
        self.assertEqual(condition["announced_at"], rot_at.isoformat())
        incidents = state["journal"]["crop_incidents"]
        self.assertEqual(len(incidents), 1)
        incident = incidents[0]
        self.assertEqual(incident["condition_id"], condition["condition_id"])
        self.assertEqual(incident["type"], "flood_rot")
        self.assertEqual(incident["outcome"], "failed")
        self.assertEqual(incident["failed_at"], rot_at.isoformat())
        nodes = {node["kind"] for node in incident["nodes"]}
        self.assertEqual(nodes, {"occurred", "failed"})
        events = [event for event in state["pending_events"] if event["type"] == "flood_damage"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["stage"], "rot")
        self.assertIn(events[0]["text"], garden_content.FLOOD_DAMAGE_EVENT_TEXT["rot"])

    def test_rot_clears_stale_pending_events_for_that_cycle(self):
        plots = [_growing_plot("p1", crop_id="cucumber", cycle_id="cycle-1")]
        state = _state(plots, since=self.since)
        state["pending_events"] = [{
            "event_id": "crop:p1:cycle-1:stage:sprout",
            "type": "crop_stage", "plot_id": "p1", "crop_id": "cucumber",
            "cycle_id": "cycle-1", "stage": "sprout",
        }]
        now = self.since + ROT_AFTER + timedelta(minutes=1)
        self._settle(state, now)
        stale = [
            event for event in state["pending_events"]
            if event.get("type") == "crop_stage" and event.get("plot_id") == "p1"
        ]
        self.assertEqual(stale, [])

    def test_long_jump_directly_to_rot_never_marks_quality_or_queues_it(self):
        plots = [_growing_plot("p1")]
        state = _state(plots, since=self.since)
        now = self.since + ROT_AFTER + timedelta(hours=10)
        self._settle(state, now)
        self.assertEqual(plots[0]["status"], "withered")
        self.assertNotIn("quality", plots[0])
        stages = {event["stage"] for event in state["pending_events"] if event["type"] == "flood_damage"}
        self.assertEqual(stages, {"rot"})

    def test_rot_is_idempotent_across_repeated_settlements(self):
        plots = [_growing_plot("p1")]
        state = _state(plots, since=self.since)
        first_now = self.since + ROT_AFTER + timedelta(minutes=1)
        self._settle(state, first_now)
        condition_id_after_first = plots[0]["condition"]["condition_id"]
        incident_count_after_first = len(state["journal"]["crop_incidents"])
        event_count_after_first = len(state["pending_events"])
        later_now = first_now + timedelta(days=1)
        changed = self._settle(state, later_now)
        # 已经枯死的地块不再是 growing/ready，天然被结算排除在外——
        # 幂等靠"没有可罚的地块了"而不是额外的标记字段。
        self.assertFalse(changed)
        self.assertEqual(plots[0]["condition"]["condition_id"], condition_id_after_first)
        self.assertEqual(len(state["journal"]["crop_incidents"]), incident_count_after_first)
        self.assertEqual(len(state["pending_events"]), event_count_after_first)

    def test_existing_active_condition_is_collected_not_replaced(self):
        active_condition = {
            "condition_id": "condition:p1:cycle-1:existing",
            "type": "waterlogged",
            "status": "active",
            "severity": "warning",
            "occurred_at": self.since.isoformat(),
            "announced_at": self.since.isoformat(),
            "yield_penalty": 0,
            "worsened_at": None,
            "resolved_at": None,
            "failed_at": None,
        }
        plots = [_growing_plot("p1", condition=active_condition)]
        state = _state(plots, since=self.since)
        state["journal"]["crop_incidents"] = [{
            "condition_id": "condition:p1:cycle-1:existing",
            "plot_id": "p1", "crop_id": "cucumber", "cycle_id": "cycle-1",
            "type": "waterlogged", "occurred_at": self.since.isoformat(),
            "announced_at": self.since.isoformat(), "yield_penalty": 0,
            "outcome": "active", "resolved_at": None, "failed_at": None,
            "nodes": [
                {"kind": "occurred", "at": self.since.isoformat()},
                {"kind": "announced", "at": self.since.isoformat()},
            ],
        }]
        rot_at = self.since + ROT_AFTER
        now = rot_at + timedelta(minutes=1)
        self._settle(state, now)
        condition = plots[0]["condition"]
        # 单地块单 condition 不变量：没有新建条目，就地收束同一个 condition_id。
        self.assertEqual(condition["condition_id"], "condition:p1:cycle-1:existing")
        self.assertEqual(condition["type"], "waterlogged")
        self.assertEqual(condition["cause"], "yard_flood")
        self.assertEqual(condition["status"], "failed")
        self.assertEqual(condition["severity"], "withered")
        self.assertEqual(condition["announced_at"], self.since.isoformat())
        self.assertEqual(condition["worsened_at"], rot_at.isoformat())
        self.assertEqual(condition["failed_at"], rot_at.isoformat())
        incidents = state["journal"]["crop_incidents"]
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["outcome"], "failed")
        nodes = {node["kind"] for node in incidents[0]["nodes"]}
        self.assertEqual(nodes, {"occurred", "announced", "failed"})

    def test_existing_unannounced_active_condition_backfills_announced_at(self):
        active_condition = {
            "condition_id": "condition:p1:cycle-1:existing",
            "type": "pest",
            "status": "active",
            "severity": "warning",
            "occurred_at": self.since.isoformat(),
            "announced_at": None,
            "yield_penalty": 0,
            "worsened_at": None,
            "resolved_at": None,
            "failed_at": None,
        }
        plots = [_growing_plot("p1", condition=active_condition)]
        state = _state(plots, since=self.since)
        state["journal"]["crop_incidents"] = [{
            "condition_id": "condition:p1:cycle-1:existing",
            "plot_id": "p1", "crop_id": "cucumber", "cycle_id": "cycle-1",
            "type": "pest", "occurred_at": self.since.isoformat(),
            "announced_at": None, "yield_penalty": 0,
            "outcome": "active", "resolved_at": None, "failed_at": None,
            "nodes": [{"kind": "occurred", "at": self.since.isoformat()}],
        }]
        rot_at = self.since + ROT_AFTER
        now = rot_at + timedelta(minutes=1)
        self._settle(state, now)
        condition = plots[0]["condition"]
        # 没被告知就不计惩罚时钟：从未 announced 的异常被内涝收束时，
        # 补的是泡烂理论时刻本身，不是别的时间。
        self.assertEqual(condition["announced_at"], rot_at.isoformat())


class FloodDrainageRecalculationTests(unittest.TestCase):
    """排水拨回时钟（drained_at）：阶段 C 才会有真正的排水命令，这里只测
    ``_settle_flood_damage`` 对已经写好的 ``drained_at`` 的消费逻辑。"""

    def setUp(self):
        self.since = datetime(2026, 8, 10, 0, 0, tzinfo=TZ)

    def _settle(self, state, now):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            return garden._settle_flood_damage(state, now)

    def test_drain_after_original_quality_crossing_pushes_the_threshold_back(self):
        drained_at = self.since + timedelta(hours=30)  # 排水前 since 已经超过 24h
        plots = [_growing_plot("p1")]
        state = _state(plots, since=self.since, drained_at=drained_at)
        just_before_new_threshold = drained_at + QUALITY_AFTER - timedelta(minutes=1)
        self._settle(state, just_before_new_threshold)
        self.assertNotIn("quality", plots[0])
        just_after_new_threshold = drained_at + QUALITY_AFTER + timedelta(minutes=1)
        self._settle(state, just_after_new_threshold)
        self.assertEqual(plots[0]["quality"], "poor")

    def test_drain_before_since_is_ignored(self):
        drained_at = self.since - timedelta(hours=2)
        plots = [_growing_plot("p1")]
        state = _state(plots, since=self.since, drained_at=drained_at)
        now = self.since + QUALITY_AFTER + timedelta(minutes=1)
        self._settle(state, now)
        self.assertEqual(plots[0]["quality"], "poor")


class FloodConditionCauseValidationTests(unittest.TestCase):
    """``_validate_crop_condition`` 的 ``cause`` 字段扩展（监理裁决）。"""

    def _plot(self, condition):
        return {
            "plot_id": "p1", "cycle_id": "cycle-1", "crop_id": "cucumber",
            "status": "withered", "stage": "growing", "growth_points": 1.0,
            "condition": condition,
        }

    def _terminal_flood_condition(self, **overrides):
        rot_at = datetime(2026, 8, 13, 0, 0, tzinfo=TZ)
        condition = {
            "condition_id": "condition:p1:cycle-1:abc",
            "type": "flood_rot",
            "status": "failed",
            "severity": "withered",
            "occurred_at": rot_at.isoformat(),
            "announced_at": rot_at.isoformat(),
            "yield_penalty": 1,
            "worsened_at": rot_at.isoformat(),
            "resolved_at": None,
            "failed_at": rot_at.isoformat(),
            "cause": "yard_flood",
        }
        condition.update(overrides)
        return condition

    def test_self_consistent_yard_flood_condition_is_accepted(self):
        garden._validate_crop_condition(self._plot(self._terminal_flood_condition()))

    def test_unknown_cause_value_is_rejected(self):
        condition = self._terminal_flood_condition(cause="storm")
        with self.assertRaises(garden.GardenError):
            garden._validate_crop_condition(self._plot(condition))

    def test_flood_rot_type_without_cause_is_rejected(self):
        condition = self._terminal_flood_condition()
        del condition["cause"]
        with self.assertRaises(garden.GardenError):
            garden._validate_crop_condition(self._plot(condition))

    def test_non_flood_rot_type_with_yard_flood_cause_is_accepted(self):
        # 监理裁决只反向要求 flood_rot 必须带 cause；被内涝收束的既有异常
        # 类型（如 waterlogged）保留原 type，同样合法。
        condition = self._terminal_flood_condition(type="waterlogged")
        garden._validate_crop_condition(self._plot(condition))

    def test_yard_flood_cause_requires_worsened_equals_failed(self):
        condition = self._terminal_flood_condition()
        condition["worsened_at"] = (
            datetime(2026, 8, 12, 0, 0, tzinfo=TZ).isoformat()
        )
        with self.assertRaises(garden.GardenError):
            garden._validate_crop_condition(self._plot(condition))

    def test_yard_flood_cause_requires_announced_not_after_failed(self):
        condition = self._terminal_flood_condition()
        condition["announced_at"] = (
            datetime(2026, 8, 14, 0, 0, tzinfo=TZ).isoformat()
        )
        with self.assertRaises(garden.GardenError):
            garden._validate_crop_condition(self._plot(condition))

    def test_yard_flood_cause_requires_failed_status(self):
        condition = self._terminal_flood_condition(status="active", severity="warning")
        with self.assertRaises(garden.GardenError):
            garden._validate_crop_condition(self._plot(condition))

    def test_yard_flood_cause_skips_the_24h_36h_time_equalities(self):
        # 换成一个跟 announced_at+24h/36h 完全对不上的泡烂时刻，只要终态
        # 自洽（worsened==failed、failed 存在、announced<=failed）就该通过。
        occurred = datetime(2026, 8, 10, 0, 0, tzinfo=TZ)
        rot_at = occurred + timedelta(hours=5)  # 远小于 24h/36h
        condition = self._terminal_flood_condition(
            occurred_at=occurred.isoformat(),
            announced_at=occurred.isoformat(),
            worsened_at=rot_at.isoformat(),
            failed_at=rot_at.isoformat(),
        )
        garden._validate_crop_condition(self._plot(condition))


class FloodDamagePendingEventTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)

    def _event(self, stage="quality"):
        return {
            "event_id": f"flood-damage:{stage}:{self.now.isoformat()}",
            "type": "flood_damage",
            "stage": stage,
            "occurred_at": self.now.isoformat(),
            "text": garden_content.FLOOD_DAMAGE_EVENT_TEXT[stage][0],
        }

    def test_validate_accepts_well_formed_event_for_each_stage(self):
        for stage in ("quality", "rot"):
            with self.subTest(stage=stage):
                event = self._event(stage)
                self.assertIs(garden._validate_flood_damage_pending_event(event), event)

    def test_validate_rejects_text_not_in_pool(self):
        event = self._event("quality")
        event["text"] = "这不是池子里的句子"
        with self.assertRaises(garden.GardenError):
            garden._validate_flood_damage_pending_event(event)

    def test_validate_rejects_unknown_stage(self):
        event = self._event("quality")
        event["stage"] = "withering"
        with self.assertRaises(garden.GardenError):
            garden._validate_flood_damage_pending_event(event)

    def test_peek_pending_event_dispatches_to_flood_damage_validator(self):
        state = {"pending_events": [self._event("rot")]}
        result = garden._peek_pending_event(state)
        self.assertEqual(result["type"], "flood_damage")
        self.assertEqual(result["stage"], "rot")

    def test_queue_helper_is_idempotent_for_the_same_theoretical_moment(self):
        state = {"pending_events": []}
        garden._queue_flood_damage_event(state, "quality", self.now)
        garden._queue_flood_damage_event(state, "quality", self.now)
        self.assertEqual(len(state["pending_events"]), 1)


class FloodContentPoolTests(unittest.TestCase):
    def test_quality_and_rot_pools_have_six_unique_safe_lines_with_action_markers(self):
        self.assertEqual(set(garden_content.FLOOD_DAMAGE_EVENT_TEXT), {"quality", "rot"})
        for stage, marker in (("quality", "「排水」"), ("rot", "「清理」")):
            lines = garden_content.FLOOD_DAMAGE_EVENT_TEXT[stage]
            with self.subTest(stage=stage):
                self.assertGreaterEqual(len(lines), 6)
                self.assertEqual(len(lines), len(set(lines)))
                for line in lines:
                    self.assertIn(marker, line)
                    self.assertLessEqual(len(line), 90)

    def test_rot_pool_does_not_read_as_salvageable(self):
        # 泡烂句不写成可挽救：不出现"补救/还能救/来得及"这类反悔措辞。
        forbidden = ("还能救", "来得及", "补救", "还有救")
        for line in garden_content.FLOOD_DAMAGE_EVENT_TEXT["rot"]:
            for word in forbidden:
                self.assertNotIn(word, line)

    def test_entered_yard_water_pool_gained_a_drainage_hint(self):
        for line in garden_content.YARD_WATER_EVENT_TEXT["entered"]:
            self.assertIn("「排水」", line)
        self.assertGreaterEqual(len(garden_content.YARD_WATER_EVENT_TEXT["entered"]), 6)


class PlantCropFloodBlockTests(unittest.TestCase):
    """``plant_crop`` 的内涝禁播分支：flooded 拒绝、puddles 放行、不消耗种子。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}), \
                patch.object(garden, "_environment_observations", return_value=[]):
            garden.crop_snapshot(now=self.now, path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["inventory"]["seeds"]["pepper"] = 1
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    def _seed_count(self):
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return raw["inventory"]["seeds"].get("pepper", 0)

    def test_flooded_blocks_planting_and_does_not_consume_the_seed(self):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}), \
                patch.object(garden, "_environment_observations", return_value=[]), \
                patch.object(garden, "_environment_snapshot", return_value={"yard_water": "flooded"}):
            with self.assertRaisesRegex(garden.GardenError, "排水"):
                garden.plant_crop("辣椒", "1", now=self.now, path=self.path)
        self.assertEqual(self._seed_count(), 1)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["plots"][0]["status"], "empty")

    def test_puddles_does_not_block_planting(self):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}), \
                patch.object(garden, "_environment_observations", return_value=[]), \
                patch.object(garden, "_environment_snapshot", return_value={"yard_water": "puddles"}):
            garden.plant_crop("辣椒", "1", now=self.now, path=self.path)
        self.assertEqual(self._seed_count(), 0)

    def test_missing_weather_degrades_to_none_and_does_not_block(self):
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}), \
                patch.object(garden, "_environment_observations", return_value=[]), \
                patch.object(garden, "_environment_snapshot", return_value={}):
            garden.plant_crop("辣椒", "1", now=self.now, path=self.path)
        self.assertEqual(self._seed_count(), 0)


class FloodSyntheticSequenceIntegrationTests(unittest.TestCase):
    """端到端合成序列：进 flooded → 24h 欠佳 → 72h 泡烂 → 清理。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.t0 = datetime(2026, 8, 1, 8, 0, tzinfo=TZ)
        self.env_patcher = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        self.observations_patcher = patch.object(
            garden, "_environment_observations", return_value=[],
        )
        self.observations_patcher.start()
        self.addCleanup(self.observations_patcher.stop)

    def _snapshot(self, level, now):
        with patch.object(garden, "_environment_snapshot", return_value={"yard_water": level}):
            return garden.crop_snapshot(now=now, path=self.path)

    def _raw(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_full_sequence_enters_quality_then_rot_then_clear(self):
        # 先在 none 档种下作物（避免刚种下就撞上禁播）。
        with patch.object(garden, "_environment_snapshot", return_value={"yard_water": "none"}):
            garden.plant_crop("辣椒", "1", now=self.t0, path=self.path)

        entered_at = self.t0 + timedelta(minutes=5)
        self._snapshot("flooded", entered_at)
        since = self._raw()["environment"]["flood_watch"]["since"]
        self.assertEqual(since, entered_at.isoformat())

        quality_at = entered_at + QUALITY_AFTER + timedelta(minutes=1)
        self._snapshot("flooded", quality_at)
        raw = self._raw()
        self.assertEqual(raw["plots"][0]["quality"], "poor")
        self.assertEqual(raw["plots"][0]["status"], "growing")
        quality_events = [
            event for event in raw["pending_events"] if event.get("type") == "flood_damage"
        ]
        self.assertEqual(len(quality_events), 1)
        self.assertEqual(quality_events[0]["stage"], "quality")

        rot_at = entered_at + ROT_AFTER + timedelta(minutes=1)
        self._snapshot("flooded", rot_at)
        raw = self._raw()
        self.assertEqual(raw["plots"][0]["status"], "withered")
        self.assertEqual(raw["plots"][0]["condition"]["type"], "flood_rot")
        self.assertEqual(raw["plots"][0]["condition"]["cause"], "yard_flood")
        flood_events = [
            event for event in raw["pending_events"] if event.get("type") == "flood_damage"
        ]
        self.assertEqual({event["stage"] for event in flood_events}, {"quality", "rot"})
        self.assertEqual(len(raw["journal"]["crop_incidents"]), 1)

        # 清理命令原样可用；不返种不返物。
        seeds_before = raw["inventory"]["seeds"].get("pepper", 0)
        produce_before = dict(raw["inventory"]["produce"])
        with patch.object(garden, "_environment_snapshot", return_value={"yard_water": "flooded"}):
            garden.clear_withered_crop("1", now=rot_at + timedelta(minutes=1), path=self.path)
        raw = self._raw()
        self.assertEqual(raw["plots"][0]["status"], "empty")
        self.assertEqual(raw["inventory"]["seeds"].get("pepper", 0), seeds_before)
        self.assertEqual(raw["inventory"]["produce"], produce_before)

        # 重新读取一次（`_read_state_unlocked` 内部会跑满全套校验），证明
        # 整条链路留下的存档形状是自洽的，不是靠碰巧没被校验拦到。
        reread = garden._read_state_unlocked(self.path, now=rot_at + timedelta(minutes=2))
        self.assertEqual(reread["plots"][0]["status"], "empty")

    def test_long_jump_end_to_end_skips_quality_and_only_rots(self):
        with patch.object(garden, "_environment_snapshot", return_value={"yard_water": "none"}):
            garden.plant_crop("辣椒", "1", now=self.t0, path=self.path)
        entered_at = self.t0 + timedelta(minutes=5)
        self._snapshot("flooded", entered_at)
        far_future = entered_at + ROT_AFTER + timedelta(hours=20)
        self._snapshot("flooded", far_future)
        raw = self._raw()
        self.assertEqual(raw["plots"][0]["status"], "withered")
        self.assertNotIn("quality", raw["plots"][0])
        stages = {
            event["stage"] for event in raw["pending_events"] if event.get("type") == "flood_damage"
        }
        self.assertEqual(stages, {"rot"})

    def test_existing_active_condition_on_a_real_plot_is_collected_by_rot(self):
        # 这枚异常必须"晚于内涝开始announce"，比如内涝进行到一半才冒出的虫害：
        # 若跟内涝同时起算，它自己的 36h 自然枯死点（比内涝 72h 早得多）会先
        # 一步把地块结算成 withered，根本轮不到内涝去收束——那是另一码事
        # （两个独立时钟谁先到谁先生效），不是这条用例想测的"内涝把还活着的
        # 异常收束"场景。
        with patch.object(garden, "_environment_snapshot", return_value={"yard_water": "none"}):
            garden.plant_crop("辣椒", "1", now=self.t0, path=self.path)
        entered_at = self.t0 + timedelta(minutes=5)
        self._snapshot("flooded", entered_at)
        rot_at = entered_at + ROT_AFTER + timedelta(minutes=1)
        mid_flood_announced_at = rot_at - timedelta(hours=20)
        raw = self._raw()
        raw["plots"][0]["condition"] = {
            "condition_id": f"condition:p1:{raw['plots'][0]['cycle_id']}:hand",
            "type": "waterlogged", "status": "active", "severity": "warning",
            "occurred_at": mid_flood_announced_at.isoformat(),
            "announced_at": mid_flood_announced_at.isoformat(),
            "yield_penalty": 0, "worsened_at": None, "resolved_at": None, "failed_at": None,
        }
        raw["journal"]["crop_incidents"].append({
            "condition_id": raw["plots"][0]["condition"]["condition_id"],
            "plot_id": "p1", "crop_id": "pepper", "cycle_id": raw["plots"][0]["cycle_id"],
            "type": "waterlogged", "occurred_at": mid_flood_announced_at.isoformat(),
            "announced_at": mid_flood_announced_at.isoformat(), "yield_penalty": 0,
            "outcome": "active", "resolved_at": None, "failed_at": None,
            "nodes": [
                {"kind": "occurred", "at": mid_flood_announced_at.isoformat()},
                {"kind": "announced", "at": mid_flood_announced_at.isoformat()},
            ],
        })
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self._snapshot("flooded", rot_at)
        raw = self._raw()
        condition = raw["plots"][0]["condition"]
        self.assertEqual(condition["type"], "waterlogged")
        self.assertEqual(condition["cause"], "yard_flood")
        self.assertEqual(condition["status"], "failed")
        self.assertEqual(len(raw["journal"]["crop_incidents"]), 1)

    def test_run_tick_delivers_flood_damage_event_through_the_normal_payload(self):
        with patch.object(garden, "_environment_snapshot", return_value={"yard_water": "none"}):
            garden.plant_crop("辣椒", "1", now=self.t0, path=self.path)
        entered_at = self.t0 + timedelta(minutes=5)
        # 进入 flooded 本身也会排一条既有的 yard_water「entered」事件，队列
        # 是先进先出的——先正常认领并确认它，不然它会一直挡在 flood_damage
        # 事件前面，这条用例真正想验的是后者能不能被正常投递管线送出去。
        with patch.object(garden, "_environment_snapshot", return_value={"yard_water": "flooded"}):
            entered_event = garden.run_tick(entered_at, path=self.path)
            self.assertEqual(entered_event["type"], "yard_water")
            garden.confirm_pending_event(
                entered_event["event_id"], entered_event["delivery_token"],
                now=entered_at, path=self.path,
            )
        rot_at = entered_at + ROT_AFTER + timedelta(minutes=1)
        with patch.object(garden, "_environment_snapshot", return_value={"yard_water": "flooded"}):
            event = garden.run_tick(rot_at, path=self.path)
        self.assertIsNotNone(event)
        self.assertEqual(event["type"], "flood_damage")
        self.assertEqual(event["stage"], "rot")
        self.assertIn("text", event)
        self.assertTrue(event["text"])

    def test_confirm_pending_event_dismisses_flood_damage_notice(self):
        """confirm 必须能正常摘除 flood_damage 通知，不然它会反复重投。"""
        with patch.object(garden, "_environment_snapshot", return_value={"yard_water": "none"}):
            garden.plant_crop("辣椒", "1", now=self.t0, path=self.path)
        entered_at = self.t0 + timedelta(minutes=5)
        with patch.object(garden, "_environment_snapshot", return_value={"yard_water": "flooded"}):
            entered_event = garden.run_tick(entered_at, path=self.path)
            garden.confirm_pending_event(
                entered_event["event_id"], entered_event["delivery_token"],
                now=entered_at, path=self.path,
            )
        quality_at = entered_at + QUALITY_AFTER + timedelta(minutes=1)
        with patch.object(garden, "_environment_snapshot", return_value={"yard_water": "flooded"}):
            event = garden.run_tick(quality_at, path=self.path)
        self.assertEqual(event["type"], "flood_damage")
        confirmed = garden.confirm_pending_event(
            event["event_id"], event["delivery_token"], now=quality_at, path=self.path,
        )
        self.assertTrue(confirmed)
        raw = self._raw()
        remaining_ids = [item.get("event_id") for item in raw["pending_events"]]
        self.assertNotIn(event["event_id"], remaining_ids)


if __name__ == "__main__":
    unittest.main()
