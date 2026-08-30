import copy
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden
import home
import log_store


TZ = ZoneInfo("Asia/Shanghai")


class _StubRng:
    def __init__(self, value=0.0, *, choice_index=0):
        self.value = value
        self.choice_index = choice_index

    def random(self):
        return self.value

    def choice(self, values):
        return values[0] if len(values) == 1 else values[self.choice_index]


class GardenStageDActionsTests(unittest.TestCase):
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
        self._write_crops()

    def _write_crops(self, *, count=1, legacy_water_days=()):
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [
            garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS
        ]
        state["meta"]["crop_seed_box_initialized"] = True
        for index, plot in enumerate(state["plots"][:count], start=1):
            plot.update({
                "crop_id": "tomato",
                "planted_at": (self.now - timedelta(days=1)).isoformat(),
                "last_settled_at": self.now.isoformat(),
                "growth_points": 1.0,
                "stage": "sprout",
                "water_bonus_dates": list(legacy_water_days),
                "ready_at": None,
                "status": "growing",
                "cycle_id": f"cycle-{index}",
                "stage_events_seen": [],
            })
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def _raw(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _create_natural_condition(self, condition_type_index=0, *, plot_index=0):
        raw = self._raw()
        temporarily_hidden = []
        growing = [
            plot for plot in raw["plots"] if plot.get("status") == "growing"
        ]
        if len(growing) > 1:
            target_id = growing[plot_index]["plot_id"]
            for plot in growing:
                if plot["plot_id"] != target_id:
                    temporarily_hidden.append(plot["plot_id"])
                    plot["stage"] = "seed"
        self.path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        with patch.dict(
            os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"},
        ):
            event = garden.run_tick(
                self.now,
                rng=_StubRng(0.05, choice_index=condition_type_index),
                path=self.path,
            )
        if temporarily_hidden:
            raw = self._raw()
            for plot in raw["plots"]:
                if plot["plot_id"] in temporarily_hidden:
                    plot["stage"] = "sprout"
            self.path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        return event

    def test_legacy_bonus_day_migrates_to_second_watering_and_resets_next_day(self):
        today = self.now.date().isoformat()
        self._write_crops(legacy_water_days=(today,))
        before_growth = self._raw()["plots"][0]["growth_points"]

        second = garden.water_crop(
            "p1", now=self.now, path=self.path, rng=_StubRng(0.0),
        )
        self.assertEqual(
            (second["watering_count"], second["outcome"], second["style"]),
            (2, "protest", "physical"),
        )
        self.assertEqual(self._raw()["plots"][0]["growth_points"], before_growth)
        self.assertEqual(self._raw()["plots"][0]["watering_by_date"][today], 2)

        tomorrow = self.now + timedelta(days=1)
        first = garden.water_crop("p1", now=tomorrow, path=self.path)
        self.assertEqual((first["watering_count"], first["outcome"]), (1, "watered"))
        self.assertTrue(first["accelerated"])
        self.assertEqual(
            self._raw()["plots"][0]["watering_by_date"][tomorrow.date().isoformat()],
            1,
        )

    def test_watering_history_keeps_only_recent_days(self):
        raw = self._raw()
        days = [
            (self.now.date() - timedelta(days=offset)).isoformat()
            for offset in range(20)
        ]
        raw["plots"][0]["water_bonus_dates"] = list(days)
        raw["plots"][0]["watering_by_date"] = {
            day: 1 for day in days
        }
        self.path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        garden.crop_snapshot(now=self.now, path=self.path)
        plot = self._raw()["plots"][0]
        expected = sorted(days)[-garden.WATERING_HISTORY_DAYS:]
        self.assertEqual(list(plot["watering_by_date"]), expected)
        self.assertEqual(plot["water_bonus_dates"], expected)

    def test_second_watering_has_injectable_physical_and_slapstick_styles(self):
        for random_value, expected in ((0.49, "physical"), (0.5, "slapstick")):
            with self.subTest(expected=expected):
                self._write_crops()
                garden.water_crop("p1", now=self.now, path=self.path)
                result = garden.water_crop(
                    "p1", now=self.now, path=self.path,
                    rng=_StubRng(random_value),
                )
                self.assertEqual(result["style"], expected)
                self.assertEqual(result["outcome"], "protest")
                self.assertFalse(result["accelerated"])

    def test_third_watering_creates_announced_waterlogging_and_fourth_refuses(self):
        garden.water_crop("p1", now=self.now, path=self.path)
        garden.water_crop(
            "p1", now=self.now, path=self.path, rng=_StubRng(0.0),
        )
        third = garden.water_crop("p1", now=self.now, path=self.path)
        raw = self._raw()
        condition = raw["plots"][0]["condition"]
        self.assertEqual(
            (third["outcome"], third["watering_count"]),
            ("waterlogged", 3),
        )
        self.assertEqual(
            (condition["type"], condition["status"], condition["severity"]),
            ("waterlogged", "active", "warning"),
        )
        self.assertEqual(condition["announced_at"], self.now.isoformat())
        self.assertEqual(
            [node["kind"] for node in raw["journal"]["crop_incidents"][0]["nodes"]],
            ["occurred", "announced"],
        )
        self.assertEqual(raw["pending_events"], [])

        condition_id = condition["condition_id"]
        fourth = garden.water_crop(
            "p1", now=self.now + timedelta(minutes=1), path=self.path,
        )
        self.assertEqual(fourth["outcome"], "already_waterlogged")
        self.assertEqual(self._raw()["plots"][0]["condition"]["condition_id"], condition_id)
        self.assertEqual(self._raw()["plots"][0]["watering_by_date"][self.now.date().isoformat()], 3)

    def test_three_concurrent_waterings_serialize_into_one_of_each_outcome(self):
        def water(_):
            return garden.water_crop(
                "p1", now=self.now, path=self.path, rng=_StubRng(0.0),
            )["outcome"]

        with ThreadPoolExecutor(max_workers=3) as pool:
            outcomes = list(pool.map(water, range(3)))
        self.assertCountEqual(outcomes, ["watered", "protest", "waterlogged"])
        raw = self._raw()
        self.assertEqual(
            raw["plots"][0]["watering_by_date"][self.now.date().isoformat()],
            3,
        )
        self.assertEqual(raw["plots"][0]["condition"]["type"], "waterlogged")
        self.assertEqual(len(raw["journal"]["crop_incidents"]), 1)

    def test_water_all_crops_updates_every_plot_and_supports_grouped_summary(self):
        self._write_crops(count=4)
        for plot_id in ("p1", "p2", "p4"):
            garden.water_crop(plot_id, now=self.now, path=self.path)

        results = garden.water_all_crops(
            now=self.now, path=self.path, rng=_StubRng(0.0),
        )

        self.assertEqual(
            [(result["plot"]["plot_id"], result["outcome"]) for result in results],
            [("p1", "protest"), ("p2", "protest"), ("p3", "watered"), ("p4", "protest")],
        )
        self.assertTrue(results[2]["accelerated"])
        raw = self._raw()
        today = self.now.date().isoformat()
        self.assertEqual(
            [plot["watering_by_date"][today] for plot in raw["plots"]],
            [2, 2, 1, 2],
        )
        text = home._garden_water_batch_text(results)
        self.assertIn("一、二、四号地：今天已经浇过一次", text)
        self.assertIn("三号地：今天第一次浇水，获得一次小幅生长加成", text)

    def test_water_selected_crops_only_updates_requested_plots(self):
        self._write_crops(count=4)
        results = garden.water_crops(
            ["1", "3", "4"], now=self.now, path=self.path,
        )

        self.assertEqual(
            [result["plot"]["plot_id"] for result in results],
            ["p1", "p3", "p4"],
        )
        raw = self._raw()
        today = self.now.date().isoformat()
        self.assertEqual(
            [plot.get("watering_by_date", {}).get(today, 0) for plot in raw["plots"]],
            [1, 0, 1, 1],
        )

    def test_existing_condition_blocks_a_second_condition_on_third_watering(self):
        self._write_crops(count=2)
        event = self._create_natural_condition(condition_type_index=0)
        conditioned_plot = event["plot_id"]
        target_plot = "p2" if conditioned_plot == "p1" else "p1"
        garden.water_crop(target_plot, now=self.now, path=self.path)
        garden.water_crop(
            target_plot, now=self.now, path=self.path, rng=_StubRng(0.0),
        )
        third = garden.water_crop(target_plot, now=self.now, path=self.path)
        raw = self._raw()
        active = [
            plot for plot in raw["plots"]
            if isinstance(plot.get("condition"), dict)
            and plot["condition"].get("status") == "active"
        ]
        self.assertEqual(third["outcome"], "blocked_by_condition")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["plot_id"], conditioned_plot)
        target = next(plot for plot in raw["plots"] if plot["plot_id"] == target_plot)
        self.assertNotIn("condition", target)

    def test_all_correct_actions_resolve_atomically_and_start_cooldown(self):
        for index, condition_type in enumerate(garden.NATURAL_CONDITION_TYPES):
            with self.subTest(condition_type=condition_type):
                self._write_crops()
                event = self._create_natural_condition(index)
                before = copy.deepcopy(self._raw()["inventory"])
                action = garden.CONDITION_ACTIONS[condition_type]
                result = garden.resolve_crop_condition(
                    action, "p1", now=self.now + timedelta(minutes=1),
                    path=self.path,
                )
                raw = self._raw()
                condition = raw["plots"][0]["condition"]
                incident = raw["journal"]["crop_incidents"][0]
                self.assertEqual(result["outcome"], "resolved")
                self.assertEqual(condition["status"], "resolved")
                self.assertEqual(
                    condition["resolved_at"],
                    (self.now + timedelta(minutes=1)).isoformat(),
                )
                self.assertEqual(incident["resolved_by"], action)
                self.assertEqual(incident["outcome"], "resolved")
                self.assertEqual(
                    [node["kind"] for node in incident["nodes"]],
                    ["occurred", "announced", "resolved"],
                )
                self.assertEqual(raw["pending_events"], [])
                self.assertEqual(raw["inventory"], before)
                self.assertEqual(
                    raw["meta"]["natural_condition_cooldown_until"],
                    (self.now + timedelta(hours=48, minutes=1)).isoformat(),
                )
                self.assertEqual(event["condition_id"], condition["condition_id"])

    def test_omitted_selector_chooses_the_only_active_condition_and_preserves_animals(self):
        self._write_crops(count=2)
        animal = garden.spawn(
            "animal", species="橘猫", intro="x", category="猫",
            personality="活泼", now=self.now, path=self.path,
        )
        before_animal = copy.deepcopy(self._raw()["animals"][0])
        event = self._create_natural_condition(3)
        result = garden.resolve_crop_condition(
            "施肥", now=self.now + timedelta(minutes=1), path=self.path,
        )
        raw = self._raw()
        self.assertEqual(result["outcome"], "resolved")
        self.assertEqual(result["plot_id"], event["plot_id"])
        self.assertEqual(raw["animals"][0], before_animal)
        self.assertEqual(raw["animals"][0]["id"], animal["id"])

    def test_wrong_and_healthy_actions_leave_state_unchanged(self):
        event = self._create_natural_condition(0)
        garden.confirm_pending_event(
            event["event_id"], event["delivery_token"],
            now=self.now, path=self.path,
        )
        garden.crop_snapshot(now=self.now, path=self.path)
        before = self.path.read_bytes()
        wrong = garden.resolve_crop_condition(
            "施肥", "p1", now=self.now, path=self.path,
        )
        self.assertEqual(
            (wrong["outcome"], wrong["correct_action"]),
            ("wrong_action", "除虫"),
        )
        self.assertEqual(self.path.read_bytes(), before)

        self._write_crops()
        garden.crop_snapshot(now=self.now, path=self.path)
        before = self.path.read_bytes()
        healthy = garden.resolve_crop_condition(
            "修剪", "p1", now=self.now, path=self.path,
        )
        self.assertEqual(healthy["outcome"], "healthy")
        self.assertEqual(self.path.read_bytes(), before)

    def test_wrong_action_reliably_showing_an_unannounced_condition_starts_timer_once(self):
        event = self._create_natural_condition(0)
        result = garden.resolve_crop_condition(
            "施肥", "p1", now=self.now + timedelta(minutes=1),
            path=self.path,
        )
        raw = self._raw()
        condition = raw["plots"][0]["condition"]
        self.assertEqual(result["outcome"], "wrong_action")
        self.assertEqual(condition["status"], "active")
        self.assertEqual(
            condition["announced_at"],
            (self.now + timedelta(minutes=1)).isoformat(),
        )
        self.assertEqual(raw["pending_events"], [])
        self.assertIsNone(raw["meta"]["natural_condition_cooldown_until"])
        self.assertFalse(
            garden.confirm_pending_event(
                event["event_id"], event["delivery_token"],
                now=self.now + timedelta(minutes=1), path=self.path,
            ),
        )

    def test_treating_after_damage_keeps_penalty_but_resumes_future_growth(self):
        event = self._create_natural_condition(1)
        garden.confirm_pending_event(
            event["event_id"], event["delivery_token"],
            now=self.now, path=self.path,
        )
        damaged_at = self.now + timedelta(hours=24)
        garden.crop_snapshot(now=damaged_at, path=self.path)
        result = garden.resolve_crop_condition(
            "修剪", "p1", now=damaged_at + timedelta(minutes=1),
            path=self.path,
        )
        self.assertEqual(
            (result["outcome"], result["yield_penalty"]),
            ("resolved", 1),
        )
        before_growth = self._raw()["plots"][0]["growth_points"]
        later = damaged_at + timedelta(days=1, minutes=1)
        plot = garden.crop_snapshot(now=later, path=self.path)["plots"][0]
        self.assertGreater(plot["growth_points"], before_growth)
        self.assertEqual(plot["condition"]["yield_penalty"], 1)

    def test_withered_crop_only_clears_and_never_returns_inventory(self):
        event = self._create_natural_condition(0)
        garden.confirm_pending_event(
            event["event_id"], event["delivery_token"],
            now=self.now, path=self.path,
        )
        failed_at = self.now + timedelta(hours=36)
        garden.crop_snapshot(now=failed_at, path=self.path)
        inventory = copy.deepcopy(self._raw()["inventory"])
        treatment = garden.resolve_crop_condition(
            "除虫", "p1", now=failed_at, path=self.path,
        )
        self.assertEqual(treatment["outcome"], "withered")
        garden.clear_withered_crop("p1", now=failed_at, path=self.path)
        raw = self._raw()
        self.assertEqual(raw["plots"][0], {"plot_id": "p1", "status": "empty"})
        self.assertEqual(raw["inventory"], inventory)

    def test_clear_refuses_a_healthy_crop_without_changing_bytes(self):
        garden.crop_snapshot(now=self.now, path=self.path)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "没找到符合条件"):
            garden.clear_withered_crop("p1", now=self.now, path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failure_lifecycle_from_repeat_watering_to_empty_plot_is_complete(self):
        garden.water_crop("p1", now=self.now, path=self.path)
        garden.water_crop(
            "p1", now=self.now, path=self.path, rng=_StubRng(1.0),
        )
        third = garden.water_crop("p1", now=self.now, path=self.path)
        condition_id = self._raw()["plots"][0]["condition"]["condition_id"]
        self.assertEqual(third["outcome"], "waterlogged")

        failed_at = self.now + timedelta(hours=36)
        failed = garden.crop_snapshot(now=failed_at, path=self.path)["plots"][0]
        self.assertEqual((failed["status"], failed["condition"]["status"]), ("withered", "failed"))
        cleared = garden.clear_withered_crop(
            "p1", now=failed_at, path=self.path,
        )
        raw = self._raw()
        self.assertEqual(cleared["condition_id"], condition_id)
        self.assertEqual(raw["plots"][0], {"plot_id": "p1", "status": "empty"})
        incident = raw["journal"]["crop_incidents"][0]
        self.assertEqual(incident["outcome"], "failed")
        self.assertEqual(
            [node["kind"] for node in incident["nodes"]],
            ["occurred", "announced", "damaged", "failed"],
        )
        self.assertNotIn("tomato", raw["inventory"]["produce"])
        self.assertNotIn("tomato", raw["inventory"]["seeds"])

    def test_repeated_human_waterlogging_preserves_cycle_penalty_and_old_incident(self):
        first_day = self.now
        garden.water_crop("p1", now=first_day, path=self.path)
        garden.water_crop(
            "p1", now=first_day, path=self.path, rng=_StubRng(0.0),
        )
        first_waterlogging = garden.water_crop(
            "p1", now=first_day, path=self.path,
        )
        first_condition_id = self._raw()["plots"][0]["condition"]["condition_id"]
        self.assertEqual(first_waterlogging["outcome"], "waterlogged")

        damaged_at = first_day + garden.CONDITION_DAMAGE_AFTER
        garden.crop_snapshot(now=damaged_at, path=self.path)
        first_resolved = garden.resolve_crop_condition(
            "松土", "p1", now=damaged_at, path=self.path,
        )
        first_incident = copy.deepcopy(
            self._raw()["journal"]["crop_incidents"][0],
        )
        self.assertEqual(first_resolved["yield_penalty"], 1)
        self.assertEqual(first_incident["yield_penalty"], 1)
        self.assertEqual(first_incident["outcome"], "resolved")

        second_day = damaged_at + timedelta(days=1)
        garden.water_crop("p1", now=second_day, path=self.path)
        garden.water_crop(
            "p1", now=second_day, path=self.path, rng=_StubRng(1.0),
        )
        second_waterlogging = garden.water_crop(
            "p1", now=second_day, path=self.path,
        )
        raw = self._raw()
        second_condition_id = raw["plots"][0]["condition"]["condition_id"]
        self.assertEqual(second_waterlogging["outcome"], "waterlogged")
        self.assertNotEqual(second_condition_id, first_condition_id)
        self.assertEqual(raw["plots"][0]["yield_penalty"], 1)
        self.assertEqual(raw["plots"][0]["condition"]["yield_penalty"], 0)
        self.assertEqual(raw["journal"]["crop_incidents"][0], first_incident)
        self.assertEqual(
            [incident["condition_id"] for incident in raw["journal"]["crop_incidents"]],
            [first_condition_id, second_condition_id],
        )

        second_resolved = garden.resolve_crop_condition(
            "松土", "p1", now=second_day, path=self.path,
        )
        raw = self._raw()
        self.assertEqual(second_resolved["yield_penalty"], 1)
        self.assertEqual(raw["plots"][0]["yield_penalty"], 1)
        self.assertEqual(
            [incident["yield_penalty"] for incident in raw["journal"]["crop_incidents"]],
            [1, 0],
        )
        self.assertEqual(
            [incident["outcome"] for incident in raw["journal"]["crop_incidents"]],
            ["resolved", "resolved"],
        )
        self.assertIn(
            "已经发生的减产仍保留",
            home._garden_crop_treatment_text(second_resolved),
        )

        raw["plots"][0].update({"status": "ready", "stage": "ready"})
        self.path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        crop = garden.garden_crops.CROPS["tomato"]
        harvested = garden.harvest_crop(
            "p1", now=second_day + timedelta(hours=1), path=self.path,
        )
        self.assertEqual(harvested["yield_penalty"], 1)
        self.assertEqual(
            harvested["amount"], max(1, crop["harvest_amount"] - 1),
        )

    def test_corrupt_watering_count_refuses_without_overwriting_bytes(self):
        garden.crop_snapshot(now=self.now, path=self.path)
        raw = self._raw()
        raw["plots"][0]["watering_by_date"] = {
            self.now.date().isoformat(): 99,
        }
        self.path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        before = self.path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "浇水次数损坏"):
            garden.water_crop("p1", now=self.now, path=self.path)
        self.assertEqual(self.path.read_bytes(), before)


class GardenStageDHomeTests(unittest.TestCase):
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
        self.log_path = Path(self.tempdir.name) / "logs.jsonl"
        self.garden_patcher = patch.object(garden, "GARDEN_FILE", self.path)
        self.log_patcher = patch.object(log_store, "LOG_FILE", self.log_path)
        self.garden_patcher.start()
        self.log_patcher.start()
        self.addCleanup(self.garden_patcher.stop)
        self.addCleanup(self.log_patcher.stop)

    def _request(self, *parts):
        return home.parse_request(["院子", *parts])

    def test_help_lists_every_recovery_command(self):
        output = home.garden_help("种地")
        for action in (*garden.CONDITION_ACTIONS.values(), "清理"):
            self.assertIn(action, output)
        self.assertIn("全部浇水", output)

    def test_home_routes_all_watering_once_and_dry_run_is_read_only(self):
        results = [
            {
                "plot": {"plot_id": "p1", "crop_id": "tomato"},
                "outcome": "watered", "accelerated": True,
                "condition_type": None,
            },
        ]
        with patch("home.garden.water_crops", return_value=results) as water_all:
            output = home.handle_garden(self._request("浇水", "全部"))
        self.assertIn("一号地：今天第一次浇水", output)
        water_all.assert_called_once_with(None)

        with patch("home.garden.water_crops") as dry_water_all:
            dry = home.handle_garden(
                home.parse_request(["院子", "--dry-run", "浇水", "全部"]),
            )
        self.assertIn("按结果归类", dry)
        dry_water_all.assert_not_called()

    def test_home_accepts_multiple_plot_variables_in_one_watering_call(self):
        results = [
            {
                "plot": {"plot_id": "p1", "crop_id": "tomato"},
                "outcome": "watered", "accelerated": True,
                "condition_type": None,
            },
        ]
        for parts in (("浇水", "1", "2", "4"), ("浇水", "124")):
            with self.subTest(parts=parts), patch(
                "home.garden.water_crops", return_value=results,
            ) as water_selected:
                home.handle_garden(self._request(*parts))
            water_selected.assert_called_once_with(["1", "2", "4"])

    def test_treatment_and_watering_text_always_has_explicit_result_line(self):
        resolved = {
            "plot_id": "p1", "crop_id": "tomato", "action": "除虫",
            "outcome": "resolved", "condition_type": "pest",
            "yield_penalty": 0,
        }
        self.assertIn("结果：", home._garden_crop_treatment_text(resolved))
        watered = {
            "plot": {"plot_id": "p1", "crop_id": "tomato"},
            "outcome": "protest", "style": "slapstick",
            "accelerated": False, "condition_type": None,
        }
        self.assertIn("结果：", home._garden_water_crop_text(watered))

    def test_home_clear_reports_direct_loss_and_dry_run_does_not_write(self):
        with patch(
            "home.garden.clear_withered_crop",
            return_value={"plot_id": "p1", "crop_id": "tomato"},
        ) as clear:
            output = home.handle_garden(self._request("清理", "一号地"))
        self.assertIn("已清理为空地", output)
        self.assertIn("没有返还种子或作物", output)
        clear.assert_called_once_with("一号地")

        with patch("home.garden.clear_withered_crop") as dry_clear:
            dry = home.handle_garden(
                home.parse_request(["院子", "--dry-run", "清理", "p1"]),
            )
        self.assertIn("将对p1执行『清理』", dry)
        dry_clear.assert_not_called()

    def test_home_routes_each_treatment_action_and_keeps_result_explicit(self):
        # 第八版：「施肥」改走 garden.fertilize_plot，不再经过
        # resolve_crop_condition（见 tests/test_garden_fertilizer.py 的
        # 治缺肥专项覆盖），这里只保留其余三个仍走旧路径的处理动作。
        for action, condition_type in (
            ("除虫", "pest"),
            ("修剪", "diseased_leaf"),
            ("松土", "waterlogged"),
        ):
            with self.subTest(action=action), patch(
                "home.garden.resolve_crop_condition",
                return_value={
                    "plot_id": "p1", "crop_id": "tomato",
                    "action": action, "outcome": "resolved",
                    "condition_type": condition_type, "yield_penalty": 0,
                },
            ) as resolve:
                output = home.handle_garden(self._request(action, "一号地"))
            self.assertIn("结果：", output)
            resolve.assert_called_once_with(action, "一号地")

    def test_home_routes_fertilize_action_to_fertilize_plot(self):
        with patch(
            "home.garden.fertilize_plot",
            return_value={
                "kind": "condition",
                "plot_id": "p1", "crop_id": "tomato",
                "action": "施肥", "outcome": "resolved",
                "condition_type": "nutrient_deficiency", "correct_action": "施肥",
                "yield_penalty": 0,
            },
        ) as fertilize:
            output = home.handle_garden(self._request("施肥", "一号地"))
        self.assertIn("结果：", output)
        fertilize.assert_called_once_with("一号地")

    def test_view_acknowledges_and_displays_active_condition(self):
        now = datetime.now(TZ)
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [
            garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS
        ]
        state["meta"]["crop_seed_box_initialized"] = True
        plot = state["plots"][0]
        plot.update({
            "crop_id": "tomato",
            "planted_at": (now - timedelta(days=1)).isoformat(),
            "last_settled_at": now.isoformat(),
            "growth_points": 1.0,
            "stage": "sprout",
            "water_bonus_dates": [],
            "watering_by_date": {},
            "ready_at": None,
            "status": "growing",
            "cycle_id": "cycle-view",
            "stage_events_seen": [],
        })
        condition = garden._create_crop_condition(
            state, plot, "pest", now, announced=False,
        )
        garden._queue_condition_event(state, plot, "warning")
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
        )

        output = home.handle_garden(self._request("查看"))
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        # 裸查看现在把异常放进"要留意"并直接给处理动作；完整"生长暂停"
        # 描述在详细/单地查看里。确认作废逻辑（acknowledge）不受摘要档影响。
        self.assertIn("1号地虫害中，先除虫", output)
        detailed = home._garden_list_active_detailed()
        self.assertIn("虫害 · 生长暂停", detailed)
        self.assertIsNotNone(raw["plots"][0]["condition"]["announced_at"])
        self.assertEqual(raw["pending_events"], [])
        self.assertEqual(
            raw["plots"][0]["condition"]["condition_id"],
            condition["condition_id"],
        )

    def test_view_merges_identical_attention_notes_across_plots(self):
        """8/13 反馈：两块地的提醒文字完全一样时（比如都缺水）不应该
        各占一句用"；"接起来，应该合并成"1、2号地缺水"这样一句。"""
        now = datetime.now(TZ)
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [
            garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS
        ]
        state["meta"]["crop_seed_box_initialized"] = True
        for plot_id in ("p1", "p2"):
            plot = next(p for p in state["plots"] if p["plot_id"] == plot_id)
            plot.update({
                "crop_id": "tomato",
                "planted_at": (now - timedelta(days=1)).isoformat(),
                "last_settled_at": now.isoformat(),
                "growth_points": 1.0,
                "stage": "sprout",
                "water_bonus_dates": [],
                "watering_by_date": {},
                "ready_at": None,
                "status": "growing",
                "cycle_id": f"cycle-{plot_id}",
                "stage_events_seen": [],
            })
            condition = garden._create_crop_condition(
                state, plot, "pest", now, announced=False,
            )
            garden._queue_condition_event(state, plot, "warning")
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
        )

        output = home.handle_garden(self._request("查看"))
        self.assertIn("1、2号地虫害中，先除虫", output)
        self.assertNotIn("1号地虫害中，先除虫；2号地虫害中，先除虫", output)

    def test_view_uses_cycle_penalty_after_a_new_zero_penalty_condition(self):
        plot = {
            "plot_id": "p1", "crop_id": "tomato", "status": "growing",
            "stage": "sprout", "water_bonus_dates": [],
            "watering_by_date": {}, "yield_penalty": 1,
            "condition": {
                "type": "waterlogged", "status": "resolved",
                "yield_penalty": 0,
            },
        }
        with (
            patch("home.garden.advance"),
            patch(
                "home.garden.crop_snapshot",
                return_value={"plots": [
                    plot,
                    *[
                        garden._empty_plot(plot_id)
                        for plot_id in garden._PLOT_IDS[1:]
                    ],
                ]},
            ),
            patch("home.garden.active_entries", return_value=[]),
            patch("home.garden.away_entries", return_value=[]),
        ):
            output = home._garden_list_active_detailed()
        self.assertIn("本轮预计少收1份", output)


if __name__ == "__main__":
    unittest.main()
