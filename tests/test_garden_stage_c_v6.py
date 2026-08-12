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
        humidity=85,
        wind_scale=wind,
        precip=precip,
        condition_text=text,
    )
    if value is None:
        raise AssertionError("测试观测标准化失败")
    return value


class FirstRng:
    def choice(self, values):
        return values[0]


class OutingFlavorClaimTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
        self.observations = []
        enabled = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"})
        enabled.start()
        self.addCleanup(enabled.stop)
        loader = patch.object(
            garden,
            "_environment_observations",
            side_effect=lambda _now: list(self.observations),
        )
        loader.start()
        self.addCleanup(loader.stop)

    def test_each_non_calm_mode_can_claim_one_complete_line(self):
        cases = (
            ({"precip": 3, "text": "中雨", "wind": "8"}, "typhoon"),
            ({"precip": 9, "text": "大雨", "wind": "3"}, "storm_rain"),
            ({"precip": 1, "text": "小雨", "wind": "6"}, "windy_rain"),
            ({"precip": 3, "text": "中雨", "wind": "3"}, "rain"),
            ({"precip": 0.1, "text": "细雨", "wind": "2"}, "light_rain"),
            ({"precip": 0, "text": "小雪", "wind": "2"}, "snow"),
            ({"precip": 0, "text": "晴", "wind": "6"}, "gale"),
        )
        for index, (values, expected) in enumerate(cases):
            with self.subTest(mode=expected):
                path = Path(self.tempdir.name) / f"mode-{index}.json"
                self.observations = [observation(self.now, **values)]
                claimed = garden.claim_outing_flavor(
                    now=self.now, path=path, rng=FirstRng(),
                )
                self.assertEqual(claimed["mode"], expected)
                self.assertEqual(claimed["position"], "prefix")
                self.assertTrue(claimed["text"].endswith("——"))

    def test_same_mode_is_suppressed_for_30_minutes_but_facts_remain(self):
        self.observations = [observation(self.now, precip=3, text="中雨")]
        first = garden.claim_outing_flavor(
            now=self.now, path=self.path, rng=FirstRng(),
        )
        second = garden.claim_outing_flavor(
            now=self.now + timedelta(minutes=29), path=self.path, rng=FirstRng(),
        )
        boundary = garden.claim_outing_flavor(
            now=self.now + timedelta(minutes=30), path=self.path, rng=FirstRng(),
        )
        self.assertIn("text", first)
        self.assertEqual(second["mode"], "rain")
        self.assertNotIn("text", second)
        self.assertIn("text", boundary)

    def test_mode_change_bypasses_cooldown_and_replaces_cursor(self):
        self.observations = [observation(self.now, precip=0.1, text="细雨")]
        garden.claim_outing_flavor(now=self.now, path=self.path, rng=FirstRng())
        changed_at = self.now + timedelta(minutes=5)
        self.observations = [observation(changed_at, precip=9, text="大雨")]
        changed = garden.claim_outing_flavor(
            now=changed_at, path=self.path, rng=FirstRng(),
        )
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(changed["mode"], "storm_rain")
        self.assertIn("text", changed)
        self.assertEqual(raw["environment"]["outing_flavor"]["mode"], "storm_rain")

    def test_missing_stale_and_corrupt_cursor_degrade_safely(self):
        self.assertIsNone(garden.claim_outing_flavor(
            now=self.now, path=self.path, rng=FirstRng(),
        ))
        self.assertFalse(self.path.exists())

        stale_path = Path(self.tempdir.name) / "stale.json"
        self.observations = [
            observation(self.now - timedelta(hours=3), precip=9, text="大雨"),
        ]
        self.assertIsNone(garden.claim_outing_flavor(
            now=self.now, path=stale_path, rng=FirstRng(),
        ))
        self.assertFalse(stale_path.exists())

        self.observations = []
        garden.crop_snapshot(now=self.now, path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["environment"]["outing_flavor"] = {"mode": 3, "at": "bad"}
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.observations = [observation(self.now, precip=3, text="中雨")]
        recovered = garden.claim_outing_flavor(
            now=self.now, path=self.path, rng=FirstRng(),
        )
        self.assertIn("text", recovered)
        self.assertEqual(recovered["mode"], "rain")

    def test_yard_water_is_folded_into_the_same_outing_sentence(self):
        for amount, expected in ((30, "puddles"), (64, "flooded")):
            with self.subTest(level=expected):
                path = Path(self.tempdir.name) / f"{expected}.json"
                self.observations = [
                    observation(self.now, precip=amount, text="暴雨", wind="9"),
                ]
                claimed = garden.claim_outing_flavor(
                    now=self.now, path=path, rng=FirstRng(),
                )
                self.assertEqual(claimed["yard_water"], expected)
                self.assertEqual(claimed["text"].count("——"), 1)
                self.assertIn("水", claimed["text"])

    def test_missing_copy_pool_skips_line_without_consuming_cursor(self):
        self.observations = [observation(self.now, precip=3, text="中雨")]
        with patch.object(garden_content, "outing_flavor_line", return_value=None):
            claimed = garden.claim_outing_flavor(
                now=self.now, path=self.path, rng=FirstRng(),
            )
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(claimed["mode"], "rain")
        self.assertNotIn("text", claimed)
        self.assertNotIn("outing_flavor", raw["environment"])


class OutingFlavorHomeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        path = Path(self.tempdir.name) / "garden.json"
        garden_file = patch.object(garden, "GARDEN_FILE", path)
        garden_file.start()
        self.addCleanup(garden_file.stop)
        view_file = patch.object(
            garden,
            "GARDEN_VIEW_STATE_FILE",
            Path(self.tempdir.name) / "view.json",
        )
        view_file.start()
        self.addCleanup(view_file.stop)
        disabled = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "0"})
        disabled.start()
        self.addCleanup(disabled.stop)

    def request(self, *parts):
        return home.parse_request(["院子", *parts])

    def test_each_mode_reaches_an_end_to_end_action_result(self):
        modes = (
            "typhoon", "storm_rain", "windy_rain", "rain",
            "light_rain", "snow", "gale",
        )
        plot = {"plot_id": "p1", "crop_id": "tomato"}
        with patch.object(garden, "plant_crop", return_value=plot):
            for mode in modes:
                with self.subTest(mode=mode), patch.object(
                    garden,
                    "claim_outing_flavor",
                    return_value={
                        "mode": mode,
                        "yard_water": "none",
                        "rain_streak_days": 0,
                        "position": "prefix",
                        "text": f"{mode}出门——",
                    },
                ):
                    output = home.handle_garden(self.request("播种", "小番茄", "1"))
                    self.assertTrue(output.startswith(f"{mode}出门——\n"))
                    self.assertIn("小番茄", output)

    def test_reading_routes_never_claim_an_outing_line(self):
        with patch.object(garden, "claim_outing_flavor") as claim:
            for parts in (
                (), ("查看",), ("查看详细",), ("仓库",),
                ("能种什么",), ("食谱",),
            ):
                with self.subTest(parts=parts):
                    home.handle_garden(self.request(*parts))
            claim.assert_not_called()

    def test_no_need_and_protest_keep_outing_separate_from_watering_result(self):
        outing = {
            "mode": "storm_rain", "yard_water": "puddles",
            "rain_streak_days": 2, "position": "prefix",
            "text": "冒着大雨绕过水洼跑进院子——",
        }
        base = {
            "plot": {"plot_id": "p1", "crop_id": "tomato"},
            "moisture_before": 75,
        }
        for outcome, extra in (
            ("no_need", {}),
            ("protest", {"style": "physical"}),
        ):
            with self.subTest(outcome=outcome), patch("home.random.choice", side_effect=lambda values: values[0]):
                body = home._garden_water_crop_text(
                    {**base, **extra, "outcome": outcome},
                )
                output = home._garden_with_outing(body, outing)
                first_line = output.splitlines()[0]
                self.assertEqual(first_line, outing["text"])
                self.assertNotIn("浇了", first_line)
                self.assertNotIn("倒了水", first_line)
                self.assertIn("没有", output)


class OutingFlavorWriterFactTests(unittest.TestCase):
    def test_watering_writer_receives_only_validated_outing_facts(self):
        captured = {}
        result = {
            "outcome": "no_need",
            "plot": {
                "plot_id": "p1", "crop_id": "tomato",
                "soil": {"last_water_source": None},
            },
            "moisture_before": 70,
            "watering_count": 0,
            "accelerated": False,
            "reason": "unneeded",
        }
        outing = {
            "mode": "rain", "yard_water": "puddles", "rain_streak_days": 3,
        }

        def writer(facts):
            captured.update(facts)
            return "小番茄根边的土仍然湿润，水壶在旁边停住了。"

        with patch.object(garden, "_environment_observations", return_value=[]), patch.object(
            garden.garden_generator, "generate_crop_copy", side_effect=writer,
        ):
            garden.watering_copy(result, outing=outing, fallback="小番茄还不需要浇水。")
        self.assertEqual(captured["outing_mode"], "rain")
        self.assertEqual(captured["yard_water"], "puddles")
        self.assertEqual(captured["rain_streak_days"], 3)
        public = garden_generator._public_crop_copy_facts(captured)
        self.assertEqual(public["outing_mode"], "rain")


class OutingFlavorPoolTests(unittest.TestCase):
    def test_all_mode_and_yard_pools_have_six_unique_safe_lines(self):
        self.assertEqual(len(garden_content.OUTING_FLAVOR_LINES), 7)
        for mode, pools in garden_content.OUTING_FLAVOR_LINES.items():
            for position, lines in pools.items():
                with self.subTest(mode=mode, position=position):
                    self.assertGreaterEqual(len(lines), 6)
                    self.assertEqual(len(lines), len(set(lines)))
        for level, pools in garden_content.OUTING_YARD_INSERTS.items():
            for position, lines in pools.items():
                with self.subTest(level=level, position=position):
                    self.assertGreaterEqual(len(lines), 6)
                    self.assertEqual(len(lines), len(set(lines)))


if __name__ == "__main__":
    unittest.main()
