import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import garden_weather


TZ = ZoneInfo("Asia/Shanghai")


def observation(at, *, precip=0.0, text="晴", wind="2"):
    value = garden_weather.normalize_observation(
        location_id="test",
        location_name="测试",
        observed_time=at.isoformat(),
        received_at=at,
        temp=24,
        feels_like=24,
        humidity=80,
        wind_scale=wind,
        precip=precip,
        condition_text=text,
    )
    if value is None:
        raise AssertionError("测试观测标准化失败")
    return value


def hourly_rain(end, *, hours, total, text="中雨", wind="4"):
    rate = total / hours
    return [
        observation(
            end - timedelta(hours=hours - index - 1),
            precip=rate,
            text=text,
            wind=wind,
        )
        for index in range(hours)
    ]


class RainIntensityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)

    def test_numeric_boundaries_cover_every_intensity(self):
        cases = (
            (0, None),
            (0.1, "drizzle"),
            (0.5, "light"),
            (2.5, "moderate"),
            (8, "heavy"),
            (16, "rainstorm"),
        )
        for rate, expected in cases:
            with self.subTest(rate=rate):
                item = observation(self.now, precip=rate, text="阴")
                self.assertEqual(garden_weather.rain_intensity(item), expected)

    def test_text_route_covers_every_intensity_and_generic_rain_fallback(self):
        cases = (
            ("毛毛雨", "drizzle"),
            ("阵雨", "light"),
            ("中雨", "moderate"),
            ("大雨", "heavy"),
            ("特大暴雨", "rainstorm"),
            ("雷雨", "light"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                item = observation(self.now, precip=0, text=text)
                self.assertEqual(garden_weather.rain_intensity(item), expected)

    def test_numeric_and_text_routes_take_the_stronger_result(self):
        numeric_stronger = observation(self.now, precip=20, text="小雨")
        text_stronger = observation(self.now, precip=0.1, text="大雨")
        self.assertEqual(garden_weather.rain_intensity(numeric_stronger), "rainstorm")
        self.assertEqual(garden_weather.rain_intensity(text_stronger), "heavy")
        self.assertIsNone(garden_weather.rain_intensity(None))


class OutingModeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)

    def test_priority_table(self):
        cases = (
            ({"precip": 3, "text": "中雨", "wind": "8"}, "typhoon"),
            ({"precip": 0, "text": "台风", "wind": "9"}, "typhoon"),
            ({"precip": 9, "text": "大雨", "wind": "3"}, "storm_rain"),
            ({"precip": 1, "text": "雷阵雨", "wind": "3"}, "storm_rain"),
            ({"precip": 1, "text": "小雨", "wind": "6"}, "windy_rain"),
            ({"precip": 3, "text": "中雨", "wind": "3"}, "rain"),
            ({"precip": 0.1, "text": "细雨", "wind": "2"}, "light_rain"),
            ({"precip": 0, "text": "小雪", "wind": "2"}, "snow"),
            ({"precip": 0, "text": "晴", "wind": "6"}, "gale"),
            ({"precip": 0, "text": "多云", "wind": "2"}, "calm"),
        )
        for values, expected in cases:
            with self.subTest(expected=expected):
                item = observation(self.now, **values)
                self.assertEqual(garden_weather.outing_mode(item), expected)
        self.assertEqual(garden_weather.outing_mode(None), "calm")


class RainLedgerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)

    def test_observation_window_is_idempotent_and_order_independent(self):
        observations = hourly_rain(self.now, hours=2, total=6)
        expected = {"2026-08-10": 6.0}
        self.assertEqual(garden_weather.observed_rain_by_date(observations, self.now), expected)
        self.assertEqual(
            garden_weather.observed_rain_by_date(list(reversed(observations)), self.now),
            expected,
        )
        self.assertEqual(
            garden_weather.observed_rain_by_date(observations + observations, self.now),
            expected,
        )

    def test_merge_takes_daily_max_allows_late_growth_and_never_shrinks(self):
        first = garden_weather.merge_rain_by_date({}, {"2026-08-10": 3}, self.now)
        late = garden_weather.merge_rain_by_date(first, {"2026-08-10": 7}, self.now)
        clipped = garden_weather.merge_rain_by_date(late, {"2026-08-10": 2}, self.now)
        self.assertEqual(first["2026-08-10"], 3)
        self.assertEqual(late["2026-08-10"], 7)
        self.assertEqual(clipped["2026-08-10"], 7)

    def test_merge_keeps_only_ten_days_and_skips_corruption(self):
        stored = {
            "2026-08-01": 1,
            "2026-08-02": 2,
            "2026-08-10": 3,
            "2026-08-11": 4,
            "坏日期": 9,
            "2026-08-09": -1,
        }
        self.assertEqual(
            garden_weather.merge_rain_by_date(stored, None, self.now),
            {"2026-08-01": 1.0, "2026-08-02": 2.0, "2026-08-10": 3.0},
        )

    def test_streak_uses_today_or_yesterday_anchor_and_two_mm_threshold(self):
        ledger = {
            "2026-08-06": 3,
            "2026-08-07": 2,
            "2026-08-08": 2.1,
            "2026-08-09": 1.9,
        }
        self.assertEqual(garden_weather.rain_streak_days(ledger, self.now), 0)
        ledger["2026-08-09"] = 2
        self.assertEqual(garden_weather.rain_streak_days(ledger, self.now), 4)
        ledger["2026-08-10"] = 2
        self.assertEqual(garden_weather.rain_streak_days(ledger, self.now), 5)
        del ledger["2026-08-09"]
        self.assertEqual(garden_weather.rain_streak_days(ledger, self.now), 1)


class YardWaterTests(unittest.TestCase):
    def setUp(self):
        self.base = datetime(2026, 8, 1, 0, 0, tzinfo=TZ)

    def test_index_is_idempotent_order_independent_and_capped(self):
        now = self.base + timedelta(days=2)
        rain = hourly_rain(now, hours=1, total=500, text="特大暴雨", wind="10")
        ledger = {"2026-08-01": 30, "2026-08-02": 500}
        expected = garden_weather.yard_water_index(rain, ledger, now)
        self.assertEqual(expected, 300)
        self.assertEqual(garden_weather.yard_water_index(rain * 2, ledger, now), expected)
        self.assertEqual(garden_weather.yard_water_index(list(reversed(rain)), ledger, now), expected)

    def test_missing_segments_only_drain_and_old_rain_is_outside_window(self):
        rain_end = self.base + timedelta(days=2)
        rain = hourly_rain(rain_end, hours=2, total=64, text="暴雨", wind="9")
        ledger = {"2026-08-01": 30, "2026-08-02": 64}
        wet = garden_weather.yard_water_index(rain, ledger, rain_end)
        later = garden_weather.yard_water_index(rain, ledger, rain_end + timedelta(hours=12))
        self.assertGreater(wet, later)
        old = [observation(rain_end - timedelta(hours=49), precip=100, text="暴雨")]
        self.assertEqual(garden_weather.yard_water_index(old, ledger, rain_end), 0)

    def test_rain_streak_slows_drainage(self):
        now = self.base + timedelta(days=2)
        rain = hourly_rain(now, hours=4, total=40, text="大雨")
        fast = garden_weather.yard_water_index(rain, {"2026-08-02": 40}, now)
        slow = garden_weather.yard_water_index(
            rain,
            {"2026-08-01": 30, "2026-08-02": 40},
            now,
        )
        self.assertGreater(slow, fast)

    def test_typhoon_week_reaches_flooded_then_clears_about_day_and_half_later(self):
        daily_totals = (30, 64, 80, 40, 50)
        observations = []
        ledger = {}
        levels = []
        for offset, total in enumerate(daily_totals, start=1):
            end = self.base + timedelta(days=offset)
            hours = 2 if total >= 60 else 1
            observations.extend(
                hourly_rain(end, hours=hours, total=total, text="暴雨", wind="9")
            )
            ledger[(end - timedelta(hours=1)).date().isoformat()] = total
            levels.append(
                garden_weather.yard_water_level(
                    garden_weather.yard_water_index(observations, ledger, end)
                )
            )
        self.assertEqual(levels[0], "puddles")
        self.assertIn("flooded", levels[1:])
        stopped = self.base + timedelta(days=5, hours=36)
        self.assertEqual(
            garden_weather.yard_water_level(
                garden_weather.yard_water_index(observations, ledger, stopped)
            ),
            "none",
        )

    def test_mild_week_stabilizes_at_puddles_without_flooding(self):
        observations = []
        ledger = {}
        levels = []
        for offset in range(1, 6):
            end = self.base + timedelta(days=offset)
            observations.extend(hourly_rain(end, hours=4, total=30, text="中雨"))
            ledger[(end - timedelta(hours=1)).date().isoformat()] = 30
            levels.append(
                garden_weather.yard_water_level(
                    garden_weather.yard_water_index(observations, ledger, end)
                )
            )
        self.assertEqual(levels[2:], ["puddles", "puddles", "puddles"])
        self.assertNotIn("flooded", levels)

    def test_level_thresholds(self):
        self.assertEqual(garden_weather.yard_water_level(24.999), "none")
        self.assertEqual(garden_weather.yard_water_level(25), "puddles")
        self.assertEqual(garden_weather.yard_water_level(59.999), "puddles")
        self.assertEqual(garden_weather.yard_water_level(60), "flooded")


if __name__ == "__main__":
    unittest.main()
