import asyncio
import json
import multiprocessing
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden_weather
import weather


TZ = ZoneInfo("Asia/Shanghai")


def _persist_from_child(path_text: str, observation: dict, now_text: str) -> None:
    weather.persist_weather_observation(
        observation,
        path=Path(path_text),
        now=datetime.fromisoformat(now_text),
    )


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _FakeWeatherClient:
    def __init__(self, payloads, *, raises=False, **_kwargs):
        self.payloads = payloads
        self.raises = raises

    async def __aenter__(self):
        if self.raises:
            raise OSError("offline")
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, **_kwargs):
        if url.endswith("/now"):
            return _FakeResponse(self.payloads["now"])
        if url.endswith("/3d"):
            return _FakeResponse(self.payloads["forecast"])
        return _FakeResponse(self.payloads["city"])


class WeatherFactCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "weather_cache.json"
        # 相对真实当下取值，不用固定字面日期：`test_fetch_persists_only_normalized_now_observation`
        # 会走真实的 fetch_weather()，其 received_at 内部写死用 datetime.now()（这是对的，
        # 抓取的本来就是实时天气），如果这里用固定的历史日期，缓存72小时新鲜度门槛迟早会把
        # 写死日期的观测判定为过期而拒绝写入——8/7 立秋后就撞上了这个问题。
        self.now = datetime.now(TZ)
        self.previous_cache = weather._cache
        self.previous_cache_ts = weather._cache_ts
        self.previous_city_name = weather.QWEATHER_CITY_NAME
        self.addCleanup(self._restore_memory_cache)

    def _restore_memory_cache(self):
        weather._cache = self.previous_cache
        weather._cache_ts = self.previous_cache_ts
        weather.QWEATHER_CITY_NAME = self.previous_city_name

    def observation(self, *, city="101190205", minutes=0, **overrides):
        observed = self.now - timedelta(minutes=minutes)
        values = {
            "location_id": city,
            "location_name": "南京",
            "observed_time": observed.isoformat(),
            "received_at": self.now,
            "temp": "35",
            "feels_like": "39",
            "humidity": "48",
            "wind_scale": "3-4",
            "precip": "0.2",
            "condition_text": "晴",
        }
        values.update(overrides)
        observation = garden_weather.normalize_observation(**values)
        self.assertIsNotNone(observation)
        return observation

    def persist(self, observation, *, now=None):
        return weather.persist_weather_observation(
            observation, path=self.path, now=now or self.now,
        )

    def test_normalizes_minimum_safe_fields_and_hourly_precipitation(self):
        record = self.observation()
        self.assertEqual(record["precip_rate_mm_h"], 0.2)
        self.assertEqual(record["tags"], ["clear", "hot", "windy"])
        self.assertNotIn("key", record)
        self.assertNotIn("url", record)
        self.assertTrue(record["observation_id"].startswith("qweather:101190205:"))

    def test_same_observation_is_deduplicated_and_older_one_never_becomes_latest(self):
        newest = self.observation(minutes=0)
        older = self.observation(minutes=30)
        self.assertTrue(self.persist(newest))
        self.assertFalse(self.persist(newest))
        self.assertTrue(self.persist(older))
        all_records = weather.load_weather_observations(path=self.path, now=self.now)
        self.assertEqual([item["observation_id"] for item in all_records], [
            older["observation_id"], newest["observation_id"],
        ])
        self.assertEqual(
            weather.latest_weather_observation(path=self.path, now=self.now)["observation_id"],
            newest["observation_id"],
        )

    def test_city_observations_are_isolated_for_offline_readers(self):
        nanjing = self.observation(city="101190205")
        suzhou = self.observation(city="101190401", location_name="苏州", minutes=5)
        self.persist(nanjing)
        self.persist(suzhou)
        self.assertEqual(
            [item["location_id"] for item in weather.load_weather_observations(
                location_id="101190205", path=self.path, now=self.now,
            )],
            ["101190205"],
        )
        self.assertEqual(
            weather.latest_weather_observation(
                location_id="101190401", path=self.path, now=self.now,
            )["location_name"],
            "苏州",
        )

    def test_future_observation_is_rejected_and_invalid_number_is_safely_null(self):
        self.assertIsNone(garden_weather.normalize_observation(
            location_id="101190205", location_name="南京",
            observed_time=(self.now + timedelta(minutes=16)).isoformat(),
            received_at=self.now, temp="20", feels_like="20", humidity="50",
            wind_scale="1", precip="0", condition_text="晴",
        ))
        invalid = self.observation(temp="999")
        self.assertIsNone(invalid["temp_c"])
        self.assertIn("hot", invalid["tags"])  # 有效体感温度仍可提供安全标签。
        self.assertFalse(self.path.exists())

    def test_network_failure_equivalent_does_not_replace_existing_valid_cache(self):
        valid = self.observation()
        self.persist(valid)
        before = self.path.read_bytes()
        bad = dict(valid, observation_id="qweather:101190205:bad", observed_at="bad")
        self.assertFalse(self.persist(bad))
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_cache_is_not_overwritten(self):
        self.path.write_text("{not json", encoding="utf-8")
        before = self.path.read_bytes()
        self.assertFalse(self.persist(self.observation()))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(weather.load_weather_observations(path=self.path, now=self.now), [])

    def test_two_processes_write_same_observation_once_without_corruption(self):
        record = self.observation()
        processes = [
            multiprocessing.Process(
                target=_persist_from_child,
                args=(str(self.path), record, self.now.isoformat()),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], 1)
        self.assertEqual(len(raw["observations"]), 1)

    def test_retention_is_bounded_by_age_and_count(self):
        for index in range(145):
            at = self.now - timedelta(minutes=index)
            record = garden_weather.normalize_observation(
                location_id="101190205", location_name="南京", observed_time=at.isoformat(),
                received_at=self.now, temp="20", feels_like="20", humidity="50",
                wind_scale="1", precip="0", condition_text="多云",
            )
            self.assertTrue(weather.persist_weather_observation(record, path=self.path, now=self.now))
        self.assertEqual(len(weather.load_weather_observations(path=self.path, now=self.now)), 144)

        old = garden_weather.normalize_observation(
            location_id="101190205", location_name="南京",
            observed_time=(self.now - timedelta(hours=73)).isoformat(), received_at=self.now,
            temp="20", feels_like="20", humidity="50", wind_scale="1", precip="0", condition_text="多云",
        )
        self.assertFalse(weather.persist_weather_observation(old, path=self.path, now=self.now))
        records = weather.load_weather_observations(path=self.path, now=self.now)
        self.assertTrue(all(
            datetime.fromisoformat(item["observed_at"]) >= self.now - timedelta(hours=72)
            for item in records
        ))

    def test_fetch_persists_only_normalized_now_observation(self):
        payloads = {
            "now": {
                "code": "200", "updateTime": (self.now + timedelta(minutes=8)).isoformat(),
                "now": {
                    "obsTime": self.now.isoformat(), "temp": "35", "feelsLike": "39", "text": "晴", "icon": "100",
                    "humidity": "48", "windDir": "东南风", "windScale": "3-4",
                    "precip": "0.2", "vis": "10",
                },
            },
            "forecast": {"code": "200", "daily": [{"fxDate": "2026-07-29"}]},
            "city": {"code": "200", "location": [{"adm2": "南京", "name": "鼓楼"}]},
        }
        weather._cache = {}
        weather._cache_ts = 0
        weather.QWEATHER_CITY_NAME = ""
        with patch.dict("os.environ", {
            "QWEATHER_API_KEY": "test-key", "QWEATHER_HOST": "example.invalid", "QWEATHER_CITY": "101010100",
            "WEATHER_CACHE_FILE": str(self.path),
        }, clear=False), patch(
            "weather.httpx.AsyncClient", side_effect=lambda **kwargs: _FakeWeatherClient(payloads, **kwargs),
        ):
            result = asyncio.run(weather.fetch_weather())
        self.assertEqual(result["forecast"], [{
            "date": "2026-07-29", "textDay": None, "textNight": None,
            "tempMin": None, "tempMax": None, "iconDay": None,
        }])
        record = weather.latest_weather_observation(path=self.path)
        self.assertEqual(record["location_id"], "101010100")
        self.assertEqual(record["observed_at"], self.now.isoformat())
        self.assertTrue(record["observation_id"].endswith(self.now.isoformat()))
        self.assertEqual(record["precip_rate_mm_h"], 0.2)
        self.assertNotIn("test-key", self.path.read_text(encoding="utf-8"))

    def test_fetch_failure_keeps_the_existing_cache_bytes(self):
        self.persist(self.observation())
        before = self.path.read_bytes()
        weather._cache = {"temp": "20", "text": "多云"}
        weather._cache_ts = 0
        with patch.dict("os.environ", {
            "QWEATHER_API_KEY": "test-key", "QWEATHER_HOST": "example.invalid", "QWEATHER_CITY": "101010100",
            "WEATHER_CACHE_FILE": str(self.path),
        }, clear=False), patch(
            "weather.httpx.AsyncClient", side_effect=lambda **kwargs: _FakeWeatherClient({}, raises=True, **kwargs),
        ):
            self.assertEqual(asyncio.run(weather.fetch_weather()), weather._cache)
        self.assertEqual(self.path.read_bytes(), before)

    def test_absolute_time_ordering_ignores_timezone_text_order(self):
        ordering_now = datetime(2026, 7, 30, 0, 0, tzinfo=TZ)
        nanjing = garden_weather.normalize_observation(
            location_id="101190205", location_name="南京",
            observed_time="2026-07-29T12:00:00+08:00", received_at=ordering_now,
            temp="20", feels_like="20", humidity="50", wind_scale="1", precip="0", condition_text="多云",
        )
        new_york = garden_weather.normalize_observation(
            location_id="101020100", location_name="纽约",
            observed_time="2026-07-29T11:30:00-04:00", received_at=ordering_now,
            temp="20", feels_like="20", humidity="50", wind_scale="1", precip="0", condition_text="多云",
        )
        self.assertTrue(self.persist(nanjing, now=ordering_now))
        self.assertTrue(self.persist(new_york, now=ordering_now))
        self.assertEqual(
            weather.latest_weather_observation(path=self.path, now=ordering_now)["location_id"],
            "101020100",
        )

    def test_cache_write_failure_does_not_hide_successful_weather_result(self):
        payloads = {
            "now": {
                "code": "200", "updateTime": (self.now + timedelta(minutes=5)).isoformat(),
                "now": {
                    "obsTime": self.now.isoformat(), "temp": "35", "feelsLike": "39", "text": "晴",
                    "icon": "100", "humidity": "48", "windDir": "东南风", "windScale": "3-4",
                    "precip": "0.2", "vis": "10",
                },
            },
            "forecast": {"code": "200", "daily": []},
            "city": {"code": "200", "location": [{"adm2": "南京", "name": "鼓楼"}]},
        }
        weather._cache = {}
        weather._cache_ts = 0
        weather.QWEATHER_CITY_NAME = ""
        with patch.dict("os.environ", {
            "QWEATHER_API_KEY": "test-key", "QWEATHER_HOST": "example.invalid", "QWEATHER_CITY": "101010100",
            "WEATHER_CACHE_FILE": str(self.path),
        }, clear=False), patch(
            "weather.httpx.AsyncClient", side_effect=lambda **kwargs: _FakeWeatherClient(payloads, **kwargs),
        ), patch("weather.persist_weather_observation", side_effect=OSError("disk full")):
            result = asyncio.run(weather.fetch_weather())
        self.assertEqual(result["temp"], "35")
        self.assertEqual(result["forecast"], [])
        self.assertEqual(weather._cache, result)


if __name__ == "__main__":
    unittest.main()
