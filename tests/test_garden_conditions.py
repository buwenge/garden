import copy
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


class _StubRng:
    def __init__(self, randoms, *, choice_index=0):
        self.randoms = list(randoms)
        self.choice_index = choice_index

    def random(self):
        return self.randoms.pop(0)

    def choice(self, values):
        return values[0] if len(values) == 1 else values[self.choice_index]


class GardenConditionStateMachineTests(unittest.TestCase):
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
        self._write_healthy_state()

    def _write_healthy_state(self, *, two_crops=False):
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS]
        state["meta"]["crop_seed_box_initialized"] = True
        crop_plots = state["plots"][:2] if two_crops else state["plots"][:1]
        for index, plot in enumerate(crop_plots, start=1):
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
            })
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _raw(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _create_condition(self, *, at=None, choice_index=0):
        at = at or self.now
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            event = garden.run_tick(
                at, rng=_StubRng([0.05], choice_index=choice_index), path=self.path,
            )
        self.assertEqual(event["type"], "crop_condition")
        self.assertEqual(event["severity"], "warning")
        return event

    def _confirm(self, event, *, at=None):
        return garden.confirm_pending_event(
            event["event_id"], event["delivery_token"], now=at or self.now, path=self.path,
        )

    def test_default_switch_does_not_roll_or_change_existing_crop(self):
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": ""}):
            self.assertFalse(garden.natural_crop_conditions_enabled())
            before = self._raw()["plots"][0]
            garden.build_event(self.now, rng=_StubRng([0.0]), path=self.path)
        raw = self._raw()
        self.assertNotIn("condition", raw["plots"][0])
        self.assertEqual(
            {
                key: value for key, value in raw["plots"][0].items()
                if key not in ("watering_by_date", "yield_penalty")
            },
            before,
        )
        self.assertEqual(raw["plots"][0]["watering_by_date"], {})
        self.assertEqual(raw["plots"][0]["yield_penalty"], 0)
        self.assertIsNone(raw["meta"]["last_natural_condition_roll_date"])
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            self.assertTrue(garden.natural_crop_conditions_enabled())

    def test_hit_creates_one_bound_condition_and_one_valid_pending_event(self):
        event = self._create_condition()
        raw = self._raw()
        condition = raw["plots"][0]["condition"]
        self.assertTrue(condition["condition_id"].startswith("condition:p1:cycle-1:"))
        self.assertEqual((condition["type"], condition["status"], condition["severity"]), ("pest", "active", "warning"))
        self.assertIsNone(condition["announced_at"])
        self.assertEqual(raw["meta"]["last_natural_condition_roll_date"], "2026-07-28")
        self.assertEqual(raw["journal"]["crop_incidents"][0]["condition_id"], condition["condition_id"])
        self.assertEqual(raw["pending_events"][0]["event_id"], event["event_id"])
        self.assertIsNone(garden.run_tick(self.now + timedelta(seconds=1), rng=_StubRng([]), path=self.path))

    def test_all_four_condition_types_have_fixed_recovery_actions(self):
        for index, expected_type in enumerate(garden.NATURAL_CONDITION_TYPES):
            with self.subTest(condition_type=expected_type):
                self._write_healthy_state()
                event = self._create_condition(choice_index=index)
                self.assertEqual(event["condition_type"], expected_type)
                self.assertIn(garden.CONDITION_ACTIONS[expected_type], event["text"])

    def test_probability_miss_and_no_candidate_are_each_once_per_beijing_day(self):
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            garden.build_event(self.now, rng=_StubRng([0.10, 0.0]), path=self.path)
            garden.build_event(self.now + timedelta(hours=1), rng=_StubRng([0.0]), path=self.path)
        self.assertNotIn("condition", self._raw()["plots"][0])

        raw = self._raw()
        raw["plots"][0]["stage"] = "seed"
        raw["meta"]["last_natural_condition_roll_date"] = None
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            garden.build_event(self.now, rng=_StubRng([0.0]), path=self.path)
        raw = self._raw()
        self.assertEqual(raw["meta"]["last_natural_condition_roll_date"], "2026-07-28")
        raw["plots"][0]["stage"] = "sprout"
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            garden.build_event(self.now + timedelta(hours=2), rng=_StubRng([0.0]), path=self.path)
        self.assertNotIn("condition", self._raw()["plots"][0])

    def test_unannounced_condition_pauses_growth_but_never_punishes(self):
        event = self._create_condition()
        initial_growth = self._raw()["plots"][0]["growth_points"]
        garden.crop_snapshot(now=self.now + timedelta(days=5), path=self.path)
        condition = self._raw()["plots"][0]["condition"]
        self.assertEqual((condition["severity"], condition["yield_penalty"]), ("warning", 0))
        self.assertIsNone(condition["failed_at"])
        self.assertEqual(self._raw()["plots"][0]["growth_points"], initial_growth)
        self.assertTrue(self._confirm(event, at=self.now + timedelta(days=5)))

    def test_watering_bonus_cannot_bypass_condition_growth_pause(self):
        event = self._create_condition()
        before = self._raw()["plots"][0]["growth_points"]
        result = garden.water_crop("p1", now=self.now + timedelta(minutes=1), path=self.path)
        self.assertFalse(result["accelerated"])
        self.assertEqual(self._raw()["plots"][0]["growth_points"], before)
        self.assertEqual(
            self._raw()["plots"][0]["condition"]["announced_at"],
            (self.now + timedelta(minutes=1)).isoformat(),
        )
        self.assertEqual(self._raw()["pending_events"], [])
        self.assertFalse(self._confirm(event))

    def test_exact_24_and_36_hour_boundaries_and_idempotency(self):
        event = self._create_condition()
        self.assertTrue(self._confirm(event))
        garden.crop_snapshot(now=self.now + timedelta(hours=24) - timedelta(microseconds=1), path=self.path)
        self.assertEqual(self._raw()["plots"][0]["condition"]["severity"], "warning")
        garden.crop_snapshot(now=self.now + timedelta(hours=24), path=self.path)
        damaged = self._raw()
        self.assertEqual(damaged["plots"][0]["condition"]["severity"], "damaged")
        self.assertEqual(damaged["plots"][0]["condition"]["yield_penalty"], 1)
        self.assertEqual([node["kind"] for node in damaged["journal"]["crop_incidents"][0]["nodes"]], ["occurred", "announced", "damaged"])
        garden.crop_snapshot(now=self.now + timedelta(hours=36) - timedelta(microseconds=1), path=self.path)
        self.assertEqual(self._raw()["plots"][0]["status"], "growing")
        garden.crop_snapshot(now=self.now + timedelta(hours=36), path=self.path)
        failed = self._raw()
        self.assertEqual((failed["plots"][0]["status"], failed["plots"][0]["condition"]["status"]), ("withered", "failed"))
        self.assertEqual(failed["meta"]["natural_condition_cooldown_until"], (self.now + timedelta(hours=84)).isoformat())
        before = self.path.read_bytes()
        garden.crop_snapshot(now=self.now + timedelta(hours=36), path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_long_jump_records_damage_and_failure_once(self):
        event = self._create_condition()
        self._confirm(event)
        garden.crop_snapshot(now=self.now + timedelta(hours=37), path=self.path)
        raw = self._raw()
        self.assertEqual(
            [node["kind"] for node in raw["journal"]["crop_incidents"][0]["nodes"]],
            ["occurred", "announced", "damaged", "failed"],
        )
        severities = [
            pending["severity"] for pending in raw["pending_events"]
            if pending["type"] == "crop_condition"
        ]
        self.assertEqual(severities, ["damaged", "withered"])
        garden.crop_snapshot(now=self.now + timedelta(days=10), path=self.path)
        raw = self._raw()
        self.assertEqual(
            [node["kind"] for node in raw["journal"]["crop_incidents"][0]["nodes"]].count("damaged"),
            1,
        )
        self.assertEqual(
            [node["kind"] for node in raw["journal"]["crop_incidents"][0]["nodes"]].count("failed"),
            1,
        )

    def test_explicit_view_acknowledgement_uses_same_timer_and_removes_warning_pending(self):
        event = self._create_condition()
        condition_id = event["condition_id"]
        seen_at = self.now + timedelta(hours=2)
        self.assertTrue(garden.acknowledge_crop_condition(condition_id, now=seen_at, path=self.path))
        raw = self._raw()
        self.assertEqual(raw["plots"][0]["condition"]["announced_at"], seen_at.isoformat())
        self.assertEqual(raw["pending_events"], [])
        self.assertFalse(self._confirm(event, at=seen_at))
        self.assertFalse(garden.acknowledge_crop_condition(condition_id, now=seen_at, path=self.path))

    def test_global_single_condition_and_48_hour_cooldown(self):
        self._write_healthy_state(two_crops=True)
        event = self._create_condition()
        self._confirm(event)
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            garden.build_event(self.now + timedelta(days=1), rng=_StubRng([0.0]), path=self.path)
        self.assertNotIn("condition", self._raw()["plots"][1])
        garden.crop_snapshot(now=self.now + timedelta(hours=36), path=self.path)
        garden.clear_withered_crop("p1", now=self.now + timedelta(hours=36), path=self.path)
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            garden.build_event(self.now + timedelta(hours=83), rng=_StubRng([0.0]), path=self.path)
        self.assertNotIn("condition", self._raw()["plots"][1])
        raw = self._raw()
        # 把第二块地固定回发芽阶段，隔离验证“冷却结束”本身，不让多日自然成熟
        # 把它从候选集中合法移除。
        raw["plots"][1].update({
            "status": "growing", "stage": "sprout", "growth_points": 1.0,
            "last_settled_at": (self.now + timedelta(hours=85)).isoformat(),
        })
        raw["pending_events"] = [
            pending for pending in raw["pending_events"]
            if not (pending.get("type") == "crop_stage" and pending.get("plot_id") == "p2")
        ]
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            next_event = garden.run_tick(self.now + timedelta(hours=85), rng=_StubRng([0.05]), path=self.path)
        self.assertEqual((next_event["type"], next_event["plot_id"]), ("crop_condition", "p2"))

    def test_same_crop_can_get_another_natural_condition_after_resolution_cooldown(self):
        first = self._create_condition()
        resolved_at = self.now + timedelta(hours=1)
        result = garden.resolve_crop_condition(
            "除虫", "p1", now=resolved_at, path=self.path,
        )
        self.assertEqual(result["outcome"], "resolved")

        second_at = resolved_at + garden.NATURAL_CONDITION_COOLDOWN + timedelta(hours=1)
        raw = self._raw()
        raw["plots"][0].update({
            "status": "growing", "stage": "sprout", "growth_points": 1.0,
            "last_settled_at": second_at.isoformat(),
        })
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        second = self._create_condition(at=second_at)
        raw = self._raw()

        self.assertNotEqual(first["condition_id"], second["condition_id"])
        self.assertEqual(raw["plots"][0]["condition"]["condition_id"], second["condition_id"])
        self.assertEqual(
            [incident["condition_id"] for incident in raw["journal"]["crop_incidents"]],
            [first["condition_id"], second["condition_id"]],
        )
        self.assertEqual(raw["journal"]["crop_incidents"][0]["outcome"], "resolved")
        self.assertEqual(raw["journal"]["crop_incidents"][1]["outcome"], "active")

    def test_second_condition_cannot_erase_first_permanent_cycle_penalty(self):
        first = self._create_condition()
        self._confirm(first)
        damaged_at = self.now + garden.CONDITION_DAMAGE_AFTER
        garden.crop_snapshot(now=damaged_at, path=self.path)
        first_resolved_at = damaged_at + timedelta(hours=1)
        first_result = garden.resolve_crop_condition(
            "除虫", "p1", now=first_resolved_at, path=self.path,
        )
        self.assertEqual(first_result["yield_penalty"], 1)
        self.assertEqual(self._raw()["plots"][0]["yield_penalty"], 1)

        second_at = (
            first_resolved_at + garden.NATURAL_CONDITION_COOLDOWN
            + timedelta(hours=1)
        )
        raw = self._raw()
        raw["plots"][0].update({
            "status": "growing", "stage": "sprout", "growth_points": 1.0,
            "last_settled_at": second_at.isoformat(),
        })
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        second = self._create_condition(at=second_at)
        second_result = garden.resolve_crop_condition(
            "除虫", "p1", now=second_at + timedelta(hours=1), path=self.path,
        )
        self.assertEqual(second_result["yield_penalty"], 1)

        raw = self._raw()
        self.assertEqual(raw["plots"][0]["yield_penalty"], 1)
        self.assertEqual(raw["plots"][0]["condition"]["yield_penalty"], 0)
        self.assertEqual(
            [incident["yield_penalty"] for incident in raw["journal"]["crop_incidents"]],
            [1, 0],
        )
        self.assertEqual(
            [incident["condition_id"] for incident in raw["journal"]["crop_incidents"]],
            [first["condition_id"], second["condition_id"]],
        )
        raw["plots"][0].update({"status": "ready", "stage": "ready"})
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        crop = garden.garden_crops.CROPS["tomato"]
        harvested = garden.harvest_crop(
            "p1", now=second_at + timedelta(hours=2), path=self.path,
        )
        self.assertEqual(harvested["yield_penalty"], 1)
        self.assertEqual(harvested["amount"], max(1, crop["harvest_amount"] - 1))

    def test_legacy_cycle_penalty_backfills_from_earlier_matching_incident(self):
        first = self._create_condition()
        self._confirm(first)
        damaged_at = self.now + garden.CONDITION_DAMAGE_AFTER
        garden.crop_snapshot(now=damaged_at, path=self.path)
        first_resolved_at = damaged_at + timedelta(hours=1)
        garden.resolve_crop_condition(
            "除虫", "p1", now=first_resolved_at, path=self.path,
        )

        second_at = (
            first_resolved_at + garden.NATURAL_CONDITION_COOLDOWN
            + timedelta(hours=1)
        )
        raw = self._raw()
        raw["plots"][0].update({
            "status": "growing", "stage": "sprout", "growth_points": 1.0,
            "last_settled_at": second_at.isoformat(),
        })
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self._create_condition(at=second_at)
        garden.resolve_crop_condition(
            "除虫", "p1", now=second_at + timedelta(hours=1), path=self.path,
        )

        raw = self._raw()
        plot = raw["plots"][0]
        self.assertEqual(plot["condition"]["yield_penalty"], 0)
        self.assertEqual(
            [incident["yield_penalty"] for incident in raw["journal"]["crop_incidents"]],
            [1, 0],
        )
        plot.pop("yield_penalty", None)
        plot.update({"status": "ready", "stage": "ready"})
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        # 首次结算必须从同一轮较早事故回填，不能只看当前零减产事故。
        garden.crop_snapshot(
            now=second_at + timedelta(hours=2), path=self.path,
        )
        migrated = self._raw()
        self.assertEqual(migrated["plots"][0]["yield_penalty"], 1)
        self.assertEqual(migrated["plots"][0]["condition"]["yield_penalty"], 0)

        crop = garden.garden_crops.CROPS["tomato"]
        harvested = garden.harvest_crop(
            "p1", now=second_at + timedelta(hours=3), path=self.path,
        )
        self.assertEqual(harvested["yield_penalty"], 1)
        self.assertEqual(harvested["amount"], max(1, crop["harvest_amount"] - 1))

    def test_failure_and_clear_do_not_touch_inventory_or_animal_fields(self):
        animal = garden.spawn(
            "animal", species="橘猫", intro="x", category="猫", personality="活泼",
            now=self.now, path=self.path,
        )
        before_animal = copy.deepcopy(self._raw()["animals"][0])
        before_inventory = copy.deepcopy(self._raw()["inventory"])
        event = self._create_condition()
        self._confirm(event)
        garden.crop_snapshot(now=self.now + timedelta(hours=37), path=self.path)
        result = garden.clear_withered_crop("一号地", now=self.now + timedelta(hours=37), path=self.path)
        raw = self._raw()
        self.assertEqual(result["crop_id"], "tomato")
        self.assertEqual(raw["plots"][0], {"plot_id": "p1", "status": "empty"})
        self.assertEqual(raw["inventory"], before_inventory)
        self.assertEqual(raw["animals"][0], before_animal)
        self.assertEqual(raw["journal"]["crop_incidents"][0]["outcome"], "failed")
        self.assertEqual(raw["pending_events"], [])
        self.assertEqual(animal["id"], raw["animals"][0]["id"])

    def test_resolved_damage_reduces_produce_once_but_not_seed_return(self):
        event = self._create_condition()
        self._confirm(event)
        garden.crop_snapshot(now=self.now + timedelta(hours=24), path=self.path)
        raw = self._raw()
        plot = raw["plots"][0]
        resolved_at = (self.now + timedelta(hours=25)).isoformat()
        plot["condition"].update({"status": "resolved", "resolved_at": resolved_at})
        plot.update({"status": "ready", "stage": "ready"})
        incident = raw["journal"]["crop_incidents"][0]
        incident.update({"outcome": "resolved", "resolved_at": resolved_at})
        incident["nodes"].append({"kind": "resolved", "at": resolved_at})
        raw["pending_events"] = []
        # 模拟 C 初版已经发生减产、但尚无轮次级字段的旧状态；首次结算必须
        # 从当前事故回填，不能把既有永久减产当成 0。
        plot.pop("yield_penalty", None)
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        crop = garden.garden_crops.CROPS["tomato"]
        result = garden.harvest_crop("p1", now=self.now + timedelta(hours=25), path=self.path)
        self.assertEqual(result["amount"], max(1, crop["harvest_amount"] - 1))
        self.assertEqual(result["seed_return"], crop["seed_return"])
        # 第七版阶段E：结算到 damaged 那一刻（本测试卡在恰好 +24h）同时打
        # 上了品相欠佳标记，收成因此整批分流进 produce_poor，不影响本测试
        # 原本要锁住的减产份额/种子返还逻辑。
        self.assertEqual(result["quality"], "poor")
        stored = self._raw()
        self.assertEqual(stored["inventory"]["produce_poor"]["tomato"], result["amount"])
        self.assertEqual(stored["inventory"]["seeds"]["tomato"], crop["seed_return"])

    def test_corrupt_condition_or_pending_refuses_to_overwrite_bytes(self):
        event = self._create_condition()
        raw = self._raw()
        raw["plots"][0]["condition"]["condition_id"] = "condition:p2:other:forged"
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "作物轮次不一致"):
            garden.crop_snapshot(now=self.now, path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

        self._write_healthy_state()
        event = self._create_condition()
        raw = self._raw()
        raw["pending_events"][0]["cycle_id"] = "ghost-cycle"
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "找不到对应菜畦"):
            garden.build_event(self.now, rng=_StubRng([]), path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cycle_yield_penalty_is_strictly_capped_at_one(self):
        raw = self._raw()
        raw["plots"][0]["yield_penalty"] = 2
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "轮次减产记录无效"):
            garden.crop_snapshot(now=self.now, path=self.path)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
