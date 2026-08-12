"""第六版阶段 E：内涝异常权重与进退事件。"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import ANY, patch
from zoneinfo import ZoneInfo

import garden
import garden_content
import garden_weather


TZ = ZoneInfo("Asia/Shanghai")


class _FirstRng:
    def choice(self, values):
        return list(values)[0]


class FloodedConditionWeightTests(unittest.TestCase):
    def test_flooded_adds_exactly_one_waterlogged_weight_only_when_saturated(self):
        saturated = {"moisture": 95.0}
        normal = garden_weather.condition_type_weights(
            saturated, garden.CONDITION_TYPES, yard_water="none",
        )
        flooded = garden_weather.condition_type_weights(
            saturated, garden.CONDITION_TYPES, yard_water="flooded",
        )
        self.assertEqual(flooded["waterlogged"], normal["waterlogged"] + 1)
        self.assertEqual(
            {key: value for key, value in flooded.items() if key != "waterlogged"},
            {key: value for key, value in normal.items() if key != "waterlogged"},
        )

    def test_flooded_does_not_make_waterlogging_eligible_for_dry_or_merely_moist_soil(self):
        dry = garden_weather.condition_type_weights(
            {"moisture": 10.0}, garden.CONDITION_TYPES, yard_water="flooded",
        )
        moist = garden_weather.condition_type_weights(
            {"moisture": 80.0}, garden.CONDITION_TYPES, yard_water="flooded",
        )
        moist_baseline = garden_weather.condition_type_weights(
            {"moisture": 80.0}, garden.CONDITION_TYPES, yard_water="none",
        )
        self.assertEqual(dry["waterlogged"], 0)
        self.assertEqual(moist["waterlogged"], moist_baseline["waterlogged"])

    def test_missing_soil_stays_uniform_even_if_yard_is_flooded(self):
        self.assertEqual(
            garden_weather.condition_type_weights(
                None, garden.CONDITION_TYPES, yard_water="flooded",
            ),
            {condition_type: 1 for condition_type in garden.CONDITION_TYPES},
        )

    def test_condition_choice_receives_the_extra_weight_without_changing_hit_probability(self):
        class CaptureRng:
            def __init__(self):
                self.values = []

            def choice(self, values):
                self.values = list(values)
                return self.values[0]

        plot = {"soil": {"moisture": 95.0}}
        with patch.dict("os.environ", {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            normal_rng = CaptureRng()
            garden._choose_condition_type(plot, normal_rng, yard_water="none")
            flooded_rng = CaptureRng()
            garden._choose_condition_type(plot, flooded_rng, yard_water="flooded")
        self.assertEqual(normal_rng.values.count("waterlogged"), 3)
        self.assertEqual(flooded_rng.values.count("waterlogged"), 4)
        self.assertEqual(len(flooded_rng.values), len(normal_rng.values) + 1)

    def test_daily_roll_only_reads_yard_water_after_the_hit_is_decided(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)
        season = garden.calendar_context(now).season
        crop_id = next(
            candidate for candidate, crop in garden.garden_crops.CROPS.items()
            if season in crop["seasons"]
        )
        plot = {
            "plot_id": "p1", "crop_id": crop_id, "status": "growing",
            "stage": "sprout", "soil": {"moisture": 95.0},
        }

        def state():
            return {
                "plots": [dict(plot)],
                "meta": {
                    "natural_condition_cooldown_until": None,
                    "last_natural_condition_roll_date": None,
                },
            }

        class RollRng:
            def __init__(self, roll):
                self.roll = roll

            def random(self):
                return self.roll

            def choice(self, values):
                return list(values)[0]

        with patch.dict("os.environ", {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}), \
             patch.object(garden, "_environment_snapshot", return_value={"yard_water": "flooded"}) as snapshot, \
             patch.object(garden, "_choose_condition_type", return_value="waterlogged") as choose, \
             patch.object(garden, "_create_crop_condition", return_value={}), \
             patch.object(garden, "_queue_condition_event"):
            self.assertTrue(garden._maybe_roll_natural_condition(
                state(), now, RollRng(1.0), observations=[{"synthetic": True}],
            ))
            snapshot.assert_not_called()
            choose.assert_not_called()

            hit_state = state()
            self.assertTrue(garden._maybe_roll_natural_condition(
                hit_state, now, RollRng(0.0), observations=[{"synthetic": True}],
            ))
            choose.assert_called_once_with(
                hit_state["plots"][0], ANY, yard_water="flooded",
            )


class YardWaterEventPoolTests(unittest.TestCase):
    def test_enter_and_exit_pools_each_have_six_unique_safe_lines(self):
        self.assertEqual(set(garden_content.YARD_WATER_EVENT_TEXT), {"entered", "exited"})
        for transition, lines in garden_content.YARD_WATER_EVENT_TEXT.items():
            with self.subTest(transition=transition):
                self.assertGreaterEqual(len(lines), 6)
                self.assertEqual(len(lines), len(set(lines)))


class YardWaterEventCursorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)

    def _state(self):
        return {"environment": {}, "pending_events": []}

    def test_old_save_first_seen_while_flooded_only_sets_baseline(self):
        state = self._state()
        self.assertTrue(garden._reconcile_yard_water_event(state, "flooded", self.now))
        self.assertEqual(
            state["environment"]["yard_water_announced"],
            {"level": "flooded", "at": None},
        )
        self.assertEqual(state["pending_events"], [])

    def test_enter_and_exit_each_queue_once_with_two_hour_guard(self):
        state = self._state()
        garden._reconcile_yard_water_event(state, "none", self.now)

        entered_at = self.now + timedelta(minutes=10)
        self.assertTrue(garden._reconcile_yard_water_event(state, "flooded", entered_at))
        self.assertEqual(len(state["pending_events"]), 1)
        entered = state["pending_events"][0]
        self.assertEqual((entered["transition"], entered["level"]), ("entered", "flooded"))
        self.assertIn(entered["text"], garden_content.YARD_WATER_EVENT_TEXT["entered"])
        self.assertFalse(garden._reconcile_yard_water_event(
            state, "flooded", entered_at + timedelta(minutes=1),
        ))

        # 一小时内短暂跌出 flooded：不发、不移动游标；若状态持续，冷却后补发。
        self.assertFalse(garden._reconcile_yard_water_event(
            state, "puddles", entered_at + timedelta(hours=1),
        ))
        self.assertEqual(state["environment"]["yard_water_announced"]["level"], "flooded")
        self.assertEqual(len(state["pending_events"]), 1)

        exited_at = entered_at + timedelta(hours=2)
        self.assertTrue(garden._reconcile_yard_water_event(state, "puddles", exited_at))
        self.assertEqual(len(state["pending_events"]), 2)
        exited = state["pending_events"][1]
        self.assertEqual((exited["transition"], exited["level"]), ("exited", "puddles"))
        self.assertIn(exited["text"], garden_content.YARD_WATER_EVENT_TEXT["exited"])
        self.assertFalse(garden._reconcile_yard_water_event(
            state, "puddles", exited_at + timedelta(minutes=1),
        ))
        garden._validate_all_pending_events(state)

    def test_none_to_puddles_updates_cursor_without_event(self):
        state = self._state()
        garden._reconcile_yard_water_event(state, "none", self.now)
        self.assertTrue(garden._reconcile_yard_water_event(
            state, "puddles", self.now + timedelta(hours=1),
        ))
        self.assertEqual(state["environment"]["yard_water_announced"]["level"], "puddles")
        self.assertEqual(state["pending_events"], [])

    def test_settlement_uses_the_derived_current_level(self):
        state = self._state()
        with patch.dict("os.environ", {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}), \
             patch.object(garden, "_environment_snapshot", return_value={"yard_water": "flooded"}):
            self.assertTrue(garden._settle_yard_water_event(state, self.now, []))
        self.assertEqual(state["environment"]["yard_water_announced"]["level"], "flooded")


class YardWaterPendingDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)

    def _event(self):
        return {
            "event_id": "yard-water:entered:20260810T120000:abcdef12",
            "type": "yard_water",
            "transition": "entered",
            "level": "flooded",
            "occurred_at": self.now.isoformat(),
            "text": garden_content.YARD_WATER_EVENT_TEXT["entered"][0],
        }

    def test_run_tick_returns_yard_event_through_the_normal_pending_payload(self):
        pending = {**self._event(), "delivery_token": "lease-token"}
        with patch.object(garden, "_environment_observations", return_value=[]), \
             patch.object(garden, "build_event", return_value={"action": "pending", "event": pending}):
            result = garden.run_tick(self.now, rng=_FirstRng())
        self.assertEqual(result["type"], "yard_water")
        self.assertEqual(result["delivery_token"], "lease-token")
        self.assertEqual(result["text"], pending["text"])

    def test_confirm_removes_the_event_once(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "garden.json"
            state = garden._empty_state()
            state.pop("entries")
            event = {**self._event(), "delivery": {
                "token": "lease-token", "leased_at": self.now.isoformat(),
            }}
            state["pending_events"] = [event]
            path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            self.assertTrue(garden.confirm_pending_event(
                event["event_id"], "lease-token", now=self.now, path=path,
            ))
            self.assertFalse(garden.confirm_pending_event(
                event["event_id"], "lease-token", now=self.now, path=path,
            ))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pending_events"], [])

    def test_invalid_transition_level_pair_is_rejected(self):
        broken = {**self._event(), "transition": "exited", "level": "flooded"}
        with self.assertRaises(garden.GardenError):
            garden._validate_yard_water_pending_event(broken)


if __name__ == "__main__":
    unittest.main()
