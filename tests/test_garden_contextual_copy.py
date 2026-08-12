import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden
import garden_content
import garden_generator


TZ = ZoneInfo("Asia/Shanghai")


class _StubRng:
    def __init__(self, randoms=(), choice_index=0):
        self._randoms = list(randoms)
        self.choice_index = choice_index

    def random(self):
        return self._randoms.pop(0)

    def choice(self, values):
        return values[self.choice_index]


class WeatherContextTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 28, 1, 41, tzinfo=TZ)

    def weather(self, **overrides):
        payload = {
            "updateTime": self.now.isoformat(),
            "text": "晴",
            "temp": "31",
            "feelsLike": "33",
            "humidity": "88",
            "windScale": "3-4",
            "precip": "0.0",
        }
        payload.update(overrides)
        return payload

    def test_fresh_weather_becomes_only_verified_tags(self):
        tags = garden.weather_context_tags(self.weather(), self.now)
        self.assertEqual(tags, frozenset({"clear", "hot", "humid", "windy"}))

        rain = garden.weather_context_tags(
            self.weather(text="小雨", feelsLike="22", humidity="75", windScale="1", precip="0.3"),
            self.now,
        )
        self.assertEqual(rain, frozenset({"rain"}))
        self.assertNotIn(
            "rain",
            garden.weather_context_tags(
                self.weather(text="晴", feelsLike="22", humidity="75", windScale="1", precip="0.3"),
                self.now,
            ),
        )

    def test_stale_missing_or_future_weather_safely_becomes_unknown(self):
        self.assertEqual(
            garden.weather_context_tags(
                self.weather(updateTime=(self.now - timedelta(hours=3)).isoformat()),
                self.now,
            ),
            frozenset(),
        )
        self.assertEqual(garden.weather_context_tags({"text": "大雪"}, self.now), frozenset())
        self.assertEqual(
            garden.weather_context_tags(
                self.weather(updateTime=(self.now + timedelta(hours=1)).isoformat()),
                self.now,
            ),
            frozenset(),
        )

    def test_clear_night_rejects_sun_and_day_copy(self):
        self.assertFalse(
            garden_content.scene_text_compatible(
                "正眯着眼晒太阳。",
                season="summer",
                day_period="night",
                weather_tags=frozenset({"clear"}),
            )
        )
        self.assertFalse(
            garden_content.scene_text_compatible(
                "蝉声最密的午后，墙根有一点动静。",
                season="summer",
                day_period="night",
                weather_tags=frozenset(),
            )
        )
        self.assertFalse(
            garden_content.scene_text_compatible(
                "石阶边落着一层雪花。",
                season="summer",
                day_period="night",
                weather_tags=frozenset({"clear"}),
            )
        )
        self.assertFalse(
            garden_content.scene_text_compatible(
                "烈日把墙根照得发白。",
                season="summer",
                day_period="night",
                weather_tags=frozenset({"clear", "hot"}),
            )
        )
        self.assertFalse(
            garden_content.scene_text_compatible(
                "雨点正在门廊边连成细线。",
                season="summer",
                day_period="night",
                weather_tags=frozenset(),
            )
        )


class ContextualCatalogTests(unittest.TestCase):
    def test_every_context_has_a_safe_trace_fallback(self):
        for season in ("spring", "summer", "autumn", "winter"):
            for period in ("dawn", "day", "dusk", "night", "late_night"):
                for weather in (
                    frozenset(),
                    frozenset({"clear"}),
                    frozenset({"rain"}),
                    frozenset({"snow", "cold"}),
                    frozenset({"fog"}),
                    frozenset({"windy"}),
                    frozenset({"hot"}),
                    frozenset({"cold"}),
                ):
                    text = garden_content.choose_scene(
                        garden_content.CONTEXTUAL_TRACES,
                        season=season,
                        day_period=period,
                        weather_tags=weather,
                        rng=_StubRng(),
                    )
                    self.assertTrue(text)
                    self.assertTrue(
                        garden_content.scene_text_compatible(
                            text,
                            season=season,
                            day_period=period,
                            weather_tags=weather,
                        ),
                        text,
                    )

    def test_all_new_scene_lines_declare_their_visible_constraints(self):
        groups = [
            garden_content.CONTEXTUAL_TRACES,
            *garden_content.CROP_STAGE_SCENES.values(),
            *garden_content.CALENDAR_MOMENT_SCENES.values(),
        ]
        for lines in groups:
            self.assertTrue(any(not line.seasons and not line.periods and not line.weather for line in lines))
            for line in lines:
                season = line.seasons[0] if line.seasons else "spring"
                period = line.periods[0] if line.periods else "day"
                self.assertTrue(
                    garden_content.scene_text_compatible(
                        line.text,
                        season=season,
                        day_period=period,
                        weather_tags=frozenset(line.weather),
                    ),
                    line,
                )

    def test_crop_and_calendar_pools_cover_all_day_periods(self):
        for lines in (
            *garden_content.CROP_STAGE_SCENES.values(),
            *garden_content.CALENDAR_MOMENT_SCENES.values(),
        ):
            covered = {period for line in lines for period in line.periods}
            self.assertEqual(covered, {"dawn", "day", "dusk", "night", "late_night"})
            for line in lines:
                self.assertTrue(line.text.format(name="南瓜"))

    def test_action_copy_keeps_placeholders_and_no_random_failure(self):
        required = {
            "plant": ("{plot}", "{name}"),
            "water": ("{name}",),
            "water_repeat": ("{name}",),
            "water_dormant": ("{name}",),
            "harvest": ("{amount}", "{name}", "{seed_return}"),
            "meal": ("{name}",),
            "gift": ("{name}",),
        }
        for key, placeholders in required.items():
            self.assertGreaterEqual(len(garden_content.GARDEN_ACTION_TEXT[key]), 8)
            for text in garden_content.GARDEN_ACTION_TEXT[key]:
                for placeholder in placeholders:
                    self.assertIn(placeholder, text)
                self.assertTrue(text.format(
                    plot="一号",
                    name="南瓜",
                    amount=2,
                    seed_return=1,
                ))
        for text in garden_content.GARDEN_ACTION_TEXT["meal"]:
            self.assertNotIn("失败", text)
            self.assertNotIn("不能吃", text)
            self.assertNotIn("烧焦", text)
        for text in garden_content.GARDEN_ACTION_TEXT["gift"]:
            self.assertIn("user", text)
            self.assertTrue(any(word in text for word in ("院子", "手账")))

    def test_runtime_writer_conflict_falls_back_without_persisting_bad_copy(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "garden.json"
            now = datetime(2026, 7, 28, 21, 0, tzinfo=TZ)
            weather = {
                "updateTime": now.isoformat(),
                "text": "晴",
                "temp": "30",
                "feelsLike": "31",
                "humidity": "60",
                "windScale": "1",
                "precip": "0",
            }
            with patch.object(
                garden_generator,
                "generate_encounter",
                return_value={"species": "橘猫", "text": "正眯着眼晒太阳。"},
            ):
                event = garden.run_tick(
                    now,
                    weather=weather,
                    rng=_StubRng([0.99, 0.05]),
                    path=path,
                )
            self.assertEqual(event["type"], "spawn")
            self.assertNotIn("太阳", event["text"])
            self.assertNotIn("午后", event["text"])
            self.assertNotIn("雪", event["text"])

    def test_generator_prompt_receives_the_same_context_contract(self):
        payload = garden_generator.build_encounter_payload(
            "animal",
            category="猫",
            personality="活泼",
            season="summer",
            day_period="night",
            weather_tags=frozenset({"clear", "hot"}),
        )
        prompt = payload["messages"][-1]["content"]
        self.assertIn("summer", prompt)
        self.assertIn("night", prompt)
        self.assertIn("clear,hot", prompt)


if __name__ == "__main__":
    unittest.main()
