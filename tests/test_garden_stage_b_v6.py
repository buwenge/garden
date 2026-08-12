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
import garden_generator
import garden_scene
import garden_weather
import home


TZ = ZoneInfo("Asia/Shanghai")


def observation(at, *, precip=0.0, text="晴", wind="2"):
    value = garden_weather.normalize_observation(
        location_id="test-city",
        location_name="测试",
        observed_time=at.isoformat(),
        received_at=at,
        temp=24,
        feels_like=24,
        humidity=90,
        wind_scale=wind,
        precip=precip,
        condition_text=text,
    )
    if value is None:
        raise AssertionError("测试观测标准化失败")
    return value


class GardenStageBV6Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)
        self.observations = []
        environment = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        loader = patch.object(
            garden, "_environment_observations",
            side_effect=lambda _now: list(self.observations),
        )
        loader.start()
        self.addCleanup(loader.stop)

    def test_old_v5_state_backfills_an_empty_rain_ledger(self):
        garden.crop_snapshot(now=self.now, path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["environment"].pop("rain_by_date")
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        snapshot = garden.crop_snapshot(now=self.now, path=self.path)
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["environment"]["rain_by_date"], {})
        self.assertEqual(persisted["environment"]["rain_by_date"], {})

    def test_daily_ledger_replay_is_idempotent_and_late_observation_only_grows_it(self):
        self.observations = [observation(self.now, precip=3, text="中雨")]
        first = garden.crop_snapshot(now=self.now, path=self.path)
        first_bytes = self.path.read_bytes()
        repeated = garden.crop_snapshot(now=self.now, path=self.path)
        self.assertEqual(first["environment"]["rain_by_date"], {"2026-08-10": 3.0})
        self.assertEqual(repeated["environment"]["rain_by_date"], {"2026-08-10": 3.0})
        self.assertEqual(self.path.read_bytes(), first_bytes)

        self.observations = [observation(self.now, precip=7, text="大雨")]
        late = garden.crop_snapshot(now=self.now, path=self.path)
        self.assertEqual(late["environment"]["rain_by_date"], {"2026-08-10": 7.0})

        self.observations = [observation(self.now, precip=2, text="小雨")]
        clipped = garden.crop_snapshot(now=self.now, path=self.path)
        self.assertEqual(clipped["environment"]["rain_by_date"], {"2026-08-10": 7.0})

    def test_crop_snapshot_exposes_all_three_yard_levels_and_neutral_missing(self):
        missing = garden.crop_snapshot(now=self.now, path=self.path)
        self.assertEqual(missing["environment"]["yard_water"], "none")
        self.assertEqual(missing["environment"]["rain_streak_days"], 0)

        self.observations = [observation(self.now, precip=30, text="大雨")]
        puddles = garden.crop_snapshot(now=self.now, path=self.path)
        self.assertEqual(puddles["environment"]["yard_water"], "puddles")

        flooded_path = Path(self.tempdir.name) / "flooded.json"
        self.observations = [observation(self.now, precip=64, text="暴雨", wind="9")]
        flooded = garden.crop_snapshot(now=self.now, path=flooded_path)
        self.assertEqual(flooded["environment"]["yard_water"], "flooded")

    def test_scene_key_tracks_water_level_but_ignores_streak_count(self):
        state = garden._empty_state()
        state["plots"] = [garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS]
        context = garden.calendar_context(self.now)
        dry = garden_scene.build_visible_snapshot(
            state, context, yard_water="none", rain_streak_days=0,
        )
        puddles = garden_scene.build_visible_snapshot(
            state, context, yard_water="puddles", rain_streak_days=3,
        )
        longer = garden_scene.build_visible_snapshot(
            state, context, yard_water="puddles", rain_streak_days=7,
        )
        self.assertNotEqual(garden_scene.scene_key(dry), garden_scene.scene_key(puddles))
        self.assertEqual(garden_scene.scene_key(puddles), garden_scene.scene_key(longer))

    def test_writer_allowlist_contains_water_facts_but_not_rain_ledger(self):
        snapshot = {
            "day_period": "day", "season": "summer",
            "solar_term": {"id": "", "name": ""},
            "festival": {"id": "", "name": ""},
            "weather_status": "fresh", "weather_tags": ["rain"],
            "animal_weather_mode": "sheltering",
            "yard_water": "flooded", "rain_streak_days": 5,
            "rain_by_date": {"2026-08-10": 64},
            "animals": [], "plots": [],
        }
        public = garden_scene.writer_snapshot(snapshot)
        self.assertEqual(public["yard_water"], "flooded")
        self.assertEqual(public["rain_streak_days"], 5)
        self.assertNotIn("rain_by_date", public)
        payload = garden_generator.build_stroll_payload(snapshot)
        sent = json.loads(payload["messages"][-1]["content"])["snapshot"]
        self.assertEqual(sent, public)

    def test_fallback_pool_is_fact_compatible_for_each_level(self):
        state = garden._empty_state()
        state["plots"] = [garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS]
        state["environment"] = {"status": "missing"}
        context = garden.calendar_context(self.now)
        texts = {}
        for level in ("none", "puddles", "flooded"):
            snapshot = garden_scene.build_visible_snapshot(
                state, context, yard_water=level, rain_streak_days=4,
            )
            text = garden_scene.fallback_scene(snapshot)
            garden_scene.validate_writer_text(text, snapshot)
            texts[level] = text
        self.assertNotIn("水洼", texts["none"])
        self.assertIn("水", texts["puddles"])
        self.assertTrue(any(word in texts["flooded"] for word in ("内涝", "连成一片", "漫")))

    def test_stroll_receives_and_renders_derived_water_facts_without_network(self):
        self.observations = [observation(self.now, precip=64, text="暴雨", wind="9")]
        captured = {}

        def local_writer(snapshot):
            captured.update(snapshot)
            return garden_scene.fallback_scene(snapshot)

        with patch("garden.garden_generator.generate_stroll", side_effect=local_writer):
            text = garden.stroll_scene(now=self.now, path=self.path)
        self.assertEqual(captured["yard_water"], "flooded")
        self.assertEqual(captured["rain_streak_days"], 1)
        self.assertTrue(any(word in text for word in ("内涝", "连成一片", "连片", "漫")))

    def test_home_view_helpers_show_water_and_report_level_changes(self):
        crop_state = {
            "environment": {"yard_water": "puddles", "rain_streak_days": 4},
        }
        line = home._garden_yard_water_line(crop_state)
        self.assertIn("4天", line)
        self.assertIn("水", line)
        self.assertIn(
            "院子里起了几处水洼",
            home._garden_view_changes(
                {"yard_water": "none"}, {"yard_water": "puddles"},
            ),
        )

    def test_invalid_persisted_rain_ledger_is_rejected_without_overwrite(self):
        garden.crop_snapshot(now=self.now, path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["environment"]["rain_by_date"] = {"坏日期": 4}
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "院级雨量日期损坏"):
            garden.crop_snapshot(now=self.now, path=self.path)
        self.assertEqual(self.path.read_bytes(), before)


class YardWaterContentValidationTests(unittest.TestCase):
    def test_every_water_pool_has_six_unique_lines(self):
        for level, lines in garden_content.YARD_WATER_SCENE_LINES.items():
            with self.subTest(level=level):
                self.assertGreaterEqual(len(lines), 6)
                self.assertEqual(len(lines), len(set(lines)))


if __name__ == "__main__":
    unittest.main()
