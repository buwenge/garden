"""第六版阶段 D：鸡舍天气反应与恶劣天气下的动物外出边界。"""

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


TZ = ZoneInfo("Asia/Shanghai")


class _FirstRng:
    def choice(self, values):
        return list(values)[0]

    def random(self):
        return 1.0


def _observation(now, *, precip=0, text="晴", wind="1"):
    value = garden_weather.normalize_observation(
        location_id="101190205",
        location_name="南京",
        observed_time=(now - timedelta(minutes=5)).isoformat(),
        received_at=now,
        temp=20,
        feels_like=20,
        humidity=70,
        wind_scale=wind,
        precip=precip,
        condition_text=text,
    )
    assert value is not None
    return value


def _adult_rooster(now):
    hatched_at = now - timedelta(days=1)
    matured_at = hatched_at + garden.CHICK_MATURITY_DURATION
    profile = garden_content.CHICKEN_PROFILE_POOL[0]
    return {
        "id": "rooster-1",
        "hatched_at": hatched_at.isoformat(),
        "sex": "rooster",
        "stage": "adult",
        "matures_at": matured_at.isoformat(),
        "matured_at": matured_at.isoformat(),
        "next_egg_at": None,
        "eggs_laid": 0,
        "nickname": "豆包",
        "feed_count": 0,
        "pet_count": 0,
        "last_fed_at": None,
        "last_petted_at": None,
        **{key: profile[key] for key in (
            "profile_id", "chick_appearance", "adult_appearance", "personality", "intro",
        )},
    }


class PoultryWeatherPoolTests(unittest.TestCase):
    def test_poultry_generic_pool_covers_every_mode_with_six_unique_lines(self):
        for mode in ("sheltering", "cooling", "basking", "muddy", "wind_play", "normal"):
            with self.subTest(mode=mode):
                lines = garden_content.ANIMAL_WEATHER_LINES["家禽"][mode]
                self.assertGreaterEqual(len(lines), 6)
                self.assertEqual(len(lines), len(set(lines)))

    def test_rooster_weather_pools_have_four_variants_each(self):
        self.assertEqual(
            set(garden_content.COOP_ROOSTER_CROW_WEATHER_TEXT),
            {"rain", "windy", "snow", "storm_rain"},
        )
        for lines in garden_content.COOP_ROOSTER_CROW_WEATHER_TEXT.values():
            self.assertGreaterEqual(len(lines), 4)


class RoosterCrowWeatherTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 10, 6, 30, tzinfo=TZ)

    def _state(self):
        state = garden._empty_state()
        state["coop"].update({
            "built": True,
            "story_status": "hatched",
            "progress_queued": ["hatched"],
            "chicks": [_adult_rooster(self.now)],
        })
        return state

    def _crow_text(self, observation):
        state = self._state()
        with patch.dict("os.environ", {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            changed = garden._queue_coop_life(
                state,
                self.now,
                _FirstRng(),
                observations=[] if observation is None else [observation],
            )
        self.assertTrue(changed)
        self.assertEqual(state["coop"]["last_crow_date"], self.now.date().isoformat())
        self.assertEqual(len(state["pending_events"]), 1)
        return state["pending_events"][0]["text"]

    def test_weather_modes_choose_their_own_crow_pool(self):
        cases = (
            ("storm_rain", _observation(self.now, precip=20, text="暴雨", wind="8")),
            ("rain", _observation(self.now, precip=1, text="小雨")),
            ("windy", _observation(self.now, text="晴", wind="7")),
            ("snow", _observation(self.now, text="小雪")),
        )
        for pool_name, observation in cases:
            with self.subTest(pool_name=pool_name):
                text = self._crow_text(observation)
                expected = garden_content.COOP_ROOSTER_CROW_WEATHER_TEXT[pool_name][0].format(name="豆包")
                self.assertEqual(text, expected)

    def test_missing_or_stale_weather_preserves_default_text_exactly(self):
        expected = garden_content.COOP_ROOSTER_CROW_TEXT[0].format(name="豆包")
        self.assertEqual(self._crow_text(None), expected)
        stale = _observation(self.now - timedelta(hours=4), precip=20, text="暴雨", wind="8")
        self.assertEqual(self._crow_text(stale), expected)

    def test_weather_only_changes_words_not_daily_dedup(self):
        state = self._state()
        rain = [_observation(self.now, precip=1, text="小雨")]
        with patch.dict("os.environ", {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            self.assertTrue(garden._queue_coop_life(
                state, self.now, _FirstRng(), observations=rain,
            ))
            state["pending_events"].clear()
            self.assertFalse(garden._queue_coop_life(
                state, self.now + timedelta(hours=1), _FirstRng(), observations=rain,
            ))

    def test_disabled_environment_keeps_default_crow_text(self):
        state = self._state()
        rain = [_observation(self.now, precip=20, text="暴雨", wind="8")]
        with patch.dict("os.environ", {"GARDEN_REAL_ENVIRONMENT_ENABLED": "0"}):
            self.assertTrue(garden._queue_coop_life(
                state, self.now, _FirstRng(), observations=rain,
            ))
        self.assertEqual(
            state["pending_events"][0]["text"],
            garden_content.COOP_ROOSTER_CROW_TEXT[0].format(name="豆包"),
        )


class ChickenInteractionWeatherTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
        self.chick = _adult_rooster(self.now)

    def _care(self, action, observations):
        state = {"coop": {"chicks": [self.chick]}}
        with tempfile.TemporaryDirectory() as tempdir, \
             patch.object(garden, "_read_state_unlocked", return_value=state), \
             patch.object(garden, "_write_state_unlocked"), \
             patch.object(garden, "_environment_observations", return_value=observations), \
             patch.dict("os.environ", {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            return garden.care_chicken(
                "豆包", action, now=self.now, path=Path(tempdir) / "garden.json",
            )

    def test_feed_and_pet_carry_sheltering_mode_in_rain(self):
        rain = [_observation(self.now, precip=5, text="大雨")]
        for action in ("feed", "pet"):
            with self.subTest(action=action):
                result = self._care(action, rain)
                self.assertEqual(result["weather_mode"], "sheltering")
                flavor = garden._animal_weather_flavor("家禽", result["weather_mode"], _FirstRng())
                self.assertIn(flavor, garden_content.ANIMAL_WEATHER_LINES["家禽"]["sheltering"])

    def test_normal_weather_adds_no_chicken_flavor(self):
        result = self._care("feed", [_observation(self.now)])
        self.assertEqual(result["weather_mode"], "normal")
        self.assertIsNone(garden._animal_weather_flavor("家禽", "normal", _FirstRng()))

    def test_home_feed_and_pet_append_the_poultry_weather_sentence(self):
        for command, action in (("喂鸡", "feed"), ("摸摸鸡", "pet")):
            with self.subTest(command=command), \
                 patch.object(garden, "care_chicken", return_value={
                     "action": action,
                     "chick": dict(self.chick),
                     "weather_mode": "sheltering",
                 }), \
                 patch("home.random.choice", side_effect=lambda lines: list(lines)[0]):
                text = home.handle_garden(home.parse_request(["院子", command, "豆包"]))
            flavor = garden_content.ANIMAL_WEATHER_LINES["家禽"]["sheltering"][0]
            self.assertIn(flavor, text)
            self.assertIn("成功", text)


class ShelterStopsNewDepartureTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
        self.due = {
            "id": "animal-1",
            "kind": "animal",
            "status": "active",
            "bond_points": 16,
            "residency": "resident",
            "next_natural_visit_after": (self.now - timedelta(minutes=1)).isoformat(),
        }

    def test_sheltering_leaves_due_animal_present_and_relationship_unchanged(self):
        before = dict(self.due)
        away = garden._advance_entries(
            [self.due], self.now, _FirstRng(), weather_mode="sheltering",
        )
        self.assertEqual(away, [])
        self.assertEqual(self.due, before)

    def test_normal_weather_starts_the_same_due_departure(self):
        away = garden._advance_entries(
            [self.due], self.now, _FirstRng(), weather_mode="normal",
        )
        self.assertEqual(away, [self.due])
        self.assertEqual(self.due["status"], "away")
        self.assertEqual((self.due["bond_points"], self.due["residency"]), (16, "resident"))

    def test_already_away_animal_is_not_rewritten_by_sheltering(self):
        away_entry = {
            **self.due,
            "status": "away",
            "away_at": (self.now - timedelta(hours=2)).isoformat(),
        }
        before = dict(away_entry)
        self.assertEqual(garden._advance_entries(
            [away_entry], self.now, _FirstRng(), weather_mode="sheltering",
        ), [])
        self.assertEqual(away_entry, before)


if __name__ == "__main__":
    unittest.main()
