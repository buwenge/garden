import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden
import garden_content
import garden_generator
import garden_scene
import home
import log_store
from garden_generator import GardenGeneratorError


TZ = ZoneInfo("Asia/Shanghai")


class _ConditionRng:
    def __init__(self, choice_index=0):
        self.choice_index = choice_index

    def random(self):
        return 0.0

    def choice(self, values):
        return values[0] if len(values) == 1 else values[self.choice_index]


class GardenStrollTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        environment = patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        writer = patch(
            "garden.garden_generator.generate_crop_copy",
            side_effect=GardenGeneratorError("离线测试"),
        )
        writer.start()
        self.addCleanup(writer.stop)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.log_path = Path(self.tempdir.name) / "logs.jsonl"
        log_patcher = patch.object(log_store, "LOG_FILE", self.log_path)
        log_patcher.start()
        self.addCleanup(log_patcher.stop)
        self.now = datetime(2026, 7, 28, 18, 30, tzinfo=TZ)

    def _prepare_crop_and_animal(self):
        garden.crop_snapshot(now=self.now, path=self.path)
        garden.plant_crop("小番茄", "1", now=self.now, path=self.path)
        garden.spawn(
            "animal", species="橘猫", intro="x", category="猫", personality="活泼",
            now=self.now, path=self.path,
        )

    def test_same_scene_key_calls_deepseek_only_once_and_reuses_saved_text(self):
        self._prepare_crop_and_animal()
        generated = []

        def select_scene(snapshot):
            generated.append(garden_scene.fallback_scene(snapshot))
            return generated[-1]

        with patch("garden.garden_generator.generate_stroll", side_effect=select_scene) as writer:
            first = garden.stroll_scene(now=self.now, path=self.path)
            second = garden.stroll_scene(now=self.now, path=self.path)

        self.assertEqual(first, generated[0])
        self.assertEqual(second, generated[0])
        writer.assert_called_once()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(raw["meta"]["scene_cache"]), 1)
        self.assertEqual(raw["meta"]["scene_cache"][0]["source"], "deepseek")

    def test_visible_state_change_creates_a_new_key(self):
        garden.crop_snapshot(now=self.now, path=self.path)
        with patch(
            "garden.garden_generator.generate_stroll",
            side_effect=lambda snapshot: garden_scene.fallback_scene(snapshot),
        ) as writer:
            before = garden.stroll_scene(now=self.now, path=self.path)
            garden.plant_crop("小番茄", "1", now=self.now, path=self.path)
            after = garden.stroll_scene(now=self.now, path=self.path)

        self.assertNotEqual(before, after)
        self.assertEqual(writer.call_count, 2)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        keys = [item["scene_key"] for item in raw["meta"]["scene_cache"]]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)

    def test_writer_failure_falls_back_once_and_fallback_stays_stable(self):
        self._prepare_crop_and_animal()
        with patch(
            "garden.garden_generator.generate_stroll",
            side_effect=GardenGeneratorError("超时"),
        ) as writer:
            first = garden.stroll_scene(now=self.now, path=self.path)
            second = garden.stroll_scene(now=self.now, path=self.path)

        self.assertEqual(first, second)
        self.assertIn("小番茄", first)
        self.assertIn("橘猫", first)
        # 失败先重试一次再兜底：第一次 stroll_scene 内部两次调用写手，
        # 第二次 stroll_scene 直接命中缓存，写手不再被调用。
        self.assertEqual(writer.call_count, 2)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["meta"]["scene_cache"][0]["source"], "fallback")
        logs = [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["message"], "小院子逛逛改用本地兜底")
        self.assertEqual(logs[0]["detail"], {"reason": "transport_error"})

    def test_writer_succeeds_on_retry_after_one_failure_and_skips_fallback(self):
        self._prepare_crop_and_animal()
        attempts = []

        def flaky_once(snapshot):
            attempts.append(snapshot)
            if len(attempts) == 1:
                raise GardenGeneratorError("超时")
            return garden_scene.fallback_scene(snapshot)

        with patch(
            "garden.garden_generator.generate_stroll", side_effect=flaky_once
        ) as writer:
            result = garden.stroll_scene(now=self.now, path=self.path)

        self.assertEqual(len(attempts), 2)
        writer.assert_called()
        self.assertEqual(writer.call_count, 2)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["meta"]["scene_cache"][0]["source"], "deepseek")
        self.assertIn("小番茄", result)
        # 重试成功就不算失败，不应该留下兜底日志。
        self.assertFalse(self.log_path.exists())

    def test_incompatible_local_scene_is_filtered_to_minimum_and_cached(self):
        self._prepare_crop_and_animal()
        with (
            patch(
                "garden.garden_generator.generate_stroll",
                side_effect=GardenGeneratorError("离线"),
            ),
            patch(
                "garden.garden_scene.fallback_scene",
                return_value="雨后的小番茄被雨浇透了，橘猫也离开院子再也不来。",
            ),
        ):
            first = garden.stroll_scene(now=self.now, path=self.path)
            second = garden.stroll_scene(now=self.now, path=self.path)
        self.assertEqual(first, second)
        self.assertIn("小番茄", first)
        self.assertIn("橘猫", first)
        self.assertNotIn("雨", first)
        self.assertNotIn("离开", first)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["meta"]["scene_cache"][0]["source"], "fallback")

    def test_stroll_acknowledges_an_unannounced_condition_once(self):
        garden.crop_snapshot(now=self.now, path=self.path)
        garden.plant_crop("小番茄", "1", now=self.now, path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["plots"][0].update({"stage": "sprout", "growth_points": 1.0})
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            event = garden.run_tick(self.now, rng=_ConditionRng(), path=self.path)
        self.assertEqual(event["type"], "crop_condition")
        self.assertIsNone(json.loads(self.path.read_text(encoding="utf-8"))["plots"][0]["condition"]["announced_at"])

        shown_at = self.now.replace(hour=19)
        with patch(
            "garden.garden_generator.generate_stroll",
            side_effect=GardenGeneratorError("离线"),
        ) as writer:
            first = garden.stroll_scene(now=shown_at, path=self.path)
            second = garden.stroll_scene(now=shown_at, path=self.path)

        self.assertEqual(first, second)
        self.assertIn("虫害", first)
        # 第一次 stroll_scene 内部重试一次再兜底，第二次直接命中缓存。
        self.assertEqual(writer.call_count, 2)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["plots"][0]["condition"]["announced_at"], shown_at.isoformat())
        self.assertFalse(any(item.get("event_id") == event["event_id"] for item in raw["pending_events"]))
        nodes = raw["journal"]["crop_incidents"][0]["nodes"]
        self.assertEqual([node["kind"] for node in nodes].count("announced"), 1)

    def test_resolved_condition_returns_to_healthy_key_and_same_type_recurrence_gets_new_key(self):
        garden.crop_snapshot(now=self.now, path=self.path)
        garden.plant_crop("小番茄", "1", now=self.now, path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["plots"][0].update({"stage": "sprout", "growth_points": 1.0})
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        garden.water_crop("p1", now=self.now, path=self.path)
        garden.water_crop("p1", now=self.now, path=self.path, rng=_ConditionRng())

        with patch(
            "garden.garden_generator.generate_stroll",
            side_effect=lambda snapshot: garden_scene.fallback_scene(snapshot),
        ) as writer:
            healthy = garden.stroll_scene(now=self.now, path=self.path)
            with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
                first_event = garden.run_tick(
                    self.now, rng=_ConditionRng(choice_index=2), path=self.path,
                )
            first_scene = garden.stroll_scene(now=self.now, path=self.path)
            first_id = first_event["condition_id"]

            resolved = garden.resolve_crop_condition(
                "松土", "p1", now=self.now, path=self.path,
            )
            restored = garden.stroll_scene(now=self.now, path=self.path)
            third = garden.water_crop("p1", now=self.now, path=self.path)
            second_id = third["plot"]["condition"]["condition_id"]
            second_scene = garden.stroll_scene(now=self.now, path=self.path)

        self.assertEqual(resolved["outcome"], "resolved")
        self.assertEqual(restored, healthy)
        self.assertNotEqual(first_id, second_id)
        self.assertNotEqual(first_scene, second_scene)
        self.assertIn("积水", second_scene)
        self.assertEqual(writer.call_count, 3)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        keys = [item["scene_key"] for item in raw["meta"]["scene_cache"]]
        self.assertEqual(len(keys), 3)
        self.assertEqual(len(set(keys)), 3)

    def test_unconfirmed_weather_from_writer_is_rejected_and_cached_as_fallback(self):
        garden.crop_snapshot(now=self.now, path=self.path)
        with patch(
            "garden.garden_generator.generate_stroll",
            return_value="傍晚，雨点落进四块空菜畦，水洼在土边轻轻晃。",
        ):
            text = garden.stroll_scene(now=self.now, path=self.path)
        self.assertNotIn("雨点", text)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["meta"]["scene_cache"][0]["source"], "fallback")

    def test_network_writer_runs_outside_the_state_lock(self):
        garden.crop_snapshot(now=self.now, path=self.path)
        real_locked = garden._locked
        lock_depth = 0

        @contextmanager
        def tracking_lock(*args, **kwargs):
            nonlocal lock_depth
            with real_locked(*args, **kwargs):
                lock_depth += 1
                try:
                    yield
                finally:
                    lock_depth -= 1

        def writer(_snapshot):
            self.assertEqual(lock_depth, 0)
            return garden_scene.fallback_scene(_snapshot)

        with patch.object(garden, "_locked", tracking_lock), patch(
            "garden.garden_generator.generate_stroll", side_effect=writer,
        ):
            garden.stroll_scene(now=self.now, path=self.path)

    def test_corrupt_cache_is_rejected_without_overwriting_file(self):
        garden.crop_snapshot(now=self.now, path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["meta"]["scene_cache"] = {"not": "a list"}
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()

        with self.assertRaisesRegex(garden.GardenError, "逛逛缓存格式损坏"):
            garden.stroll_scene(now=self.now, path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_condition_and_visible_animal_changes_affect_scene_key(self):
        state = garden._empty_state()
        state["plots"] = [garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS]
        state["plots"][0].update({
            "status": "growing", "crop_id": "cucumber", "stage": "sprout",
            "water_bonus_dates": [],
        })
        context = garden.calendar_context(self.now)
        initial = garden_scene.build_visible_snapshot(state, context)
        initial_key = garden_scene.scene_key(initial)

        state["plots"][0]["condition"] = {
            "condition_id": "secret-audit-id", "type": "pest",
            "status": "active", "severity": "warning",
        }
        with_condition = garden_scene.build_visible_snapshot(state, context)
        self.assertNotEqual(garden_scene.scene_key(with_condition), initial_key)
        self.assertNotIn("secret-audit-id", json.dumps(with_condition, ensure_ascii=False))

        state["animals"].append({
            "id": "a1", "status": "active", "species": "橘猫", "nickname": "暖暖",
            "personality": "活泼", "bond_level": 3, "bond_points": 999,
            "residency": "visitor", "spot": "墙根", "private_note": "never expose",
        })
        with_animal = garden_scene.build_visible_snapshot(state, context)
        self.assertNotEqual(garden_scene.scene_key(with_animal), garden_scene.scene_key(with_condition))
        encoded = json.dumps(with_animal, ensure_ascii=False)
        self.assertNotIn("bond_points", encoded)
        self.assertNotIn("private_note", encoded)

    def test_exact_time_is_ignored_inside_one_period_but_period_change_is_visible(self):
        state = garden._empty_state()
        state["plots"] = [garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS]
        first = garden_scene.build_visible_snapshot(state, garden.calendar_context(self.now))
        one_minute_later = garden_scene.build_visible_snapshot(
            state, garden.calendar_context(self.now.replace(minute=31)),
        )
        night = garden_scene.build_visible_snapshot(
            state, garden.calendar_context(self.now.replace(hour=20)),
        )
        self.assertEqual(garden_scene.scene_key(first), garden_scene.scene_key(one_minute_later))
        self.assertNotEqual(garden_scene.scene_key(first), garden_scene.scene_key(night))

    def test_cache_is_pruned_to_recent_limit(self):
        cache = []
        for index in range(garden_scene.SCENE_CACHE_LIMIT + 7):
            cache = garden_scene.append_cache(cache, {
                "scene_key": f"scene:v1:{index:064x}",
                "text": f"画面{index}",
                "source": "fallback",
                "generated_at": self.now.isoformat(),
            })
        self.assertEqual(len(cache), garden_scene.SCENE_CACHE_LIMIT)
        self.assertEqual(cache[0]["text"], "画面7")

    def test_fallback_keeps_a_complete_sentence_under_the_hard_limit(self):
        state = garden._empty_state()
        state["plots"] = [
            {
                "plot_id": plot_id, "status": "growing", "crop_id": "mini_watermelon",
                "stage": "growing", "water_bonus_dates": [],
                "condition": {"type": "diseased_leaf", "status": "active", "severity": "damaged"},
            }
            for plot_id in garden._PLOT_IDS
        ]
        state["animals"] = [
            {
                "id": f"a{index}", "status": "active", "species": "中华田园犬",
                "nickname": "很长很长的小动物昵称", "personality": "活泼",
                "bond_level": 3, "residency": "visitor", "spot": "院门与信箱旁",
            }
            for index in range(3)
        ]
        snapshot = garden_scene.build_visible_snapshot(state, garden.calendar_context(self.now))
        text = garden_scene.fallback_scene(snapshot)
        self.assertLessEqual(len(text), garden_scene.SCENE_TEXT_MAX_CHARS)
        self.assertTrue(text.endswith("。"))
        self.assertIn("西瓜", text)
        self.assertIn("很长很长的小动物昵称", text)

    def test_home_command_and_dry_run_route_to_stroll(self):
        with patch.object(garden, "GARDEN_FILE", self.path), patch(
            "home.garden.stroll_scene", return_value="院子画面",
        ) as stroll:
            output = home.handle_garden(home.parse_request(["院子", "逛逛"]))
            dry_run = home.handle_garden(home.parse_request(["院子", "--dry-run", "逛逛"]))
        self.assertEqual(output, "院子画面")
        self.assertIn("将查看", dry_run)
        stroll.assert_called_once()
        self.assertIn("home 院子 逛逛", home.garden_help("查看"))


class GardenStrollGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "beijing_date": "2026-07-28", "day_period": "dusk", "season": "summer",
            "solar_term": {"id": "major_heat", "name": "大暑"},
            "festival": {"id": "", "name": ""}, "weather_tags": [],
            "animals": [{"species": "橘猫", "nickname": "暖暖"}],
            "plots": [{"plot_id": "p1", "crop_name": "黄瓜", "stage": "growing"}],
        }

    def _generate_from(self, content):
        with patch.object(garden_generator, "_api_key", return_value="test-key"), patch.object(
            garden_generator, "request_chat",
            return_value={"choices": [{"message": {"content": content}}]},
        ):
            return garden_generator.generate_stroll(self.snapshot)

    def test_payload_contains_only_structured_visible_snapshot(self):
        payload = garden_generator.build_stroll_payload(self.snapshot)
        sent = json.loads(payload["messages"][-1]["content"])
        self.assertEqual(sent["snapshot"], garden_scene.writer_snapshot(self.snapshot))
        self.assertEqual(set(sent), {"snapshot"})
        self.assertNotIn("candidate_id", payload["messages"][-1]["content"])
        self.assertNotIn("candidates", sent)
        self.assertNotIn("工具", payload["messages"][-1]["content"])

    def test_writer_snapshot_is_an_explicit_allowlist(self):
        snapshot = json.loads(json.dumps(self.snapshot, ensure_ascii=False))
        snapshot["private_weather_cache_path"] = "weather_cache.json"
        snapshot["beijing_date"] = "2026-07-28"
        snapshot["plots"][0].update({
            "soil_state": "偏干", "water_source": "rain", "moisture": 42.1,
            "_growth_trend": "slow", "_long_wet": True,
            "condition": {"type": "pest", "status": "active", "severity": "warning", "condition_id": "secret"},
        })
        sent = garden_scene.writer_snapshot(snapshot)
        encoded = json.dumps(sent, ensure_ascii=False)
        self.assertIn("soil_state", encoded)
        self.assertIn("water_source", encoded)
        self.assertNotIn("private_weather_cache_path", encoded)
        self.assertNotIn("beijing_date", encoded)
        self.assertNotIn("42.1", encoded)
        self.assertNotIn("_growth_trend", encoded)
        self.assertNotIn("_long_wet", encoded)
        self.assertNotIn("condition_id", encoded)

    def test_weather_status_soil_and_water_source_change_scene_key(self):
        state = garden._empty_state()
        state["version"] = 5
        state["environment"] = {"status": "fresh"}
        state["plots"] = [garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS]
        state["plots"][0].update({
            "status": "growing", "crop_id": "cucumber", "stage": "sprout",
            "soil": {"moisture": 68.0, "last_water_source": "manual"},
        })
        context = garden.calendar_context(datetime(2026, 7, 28, 18, 30, tzinfo=TZ))
        initial = garden_scene.build_visible_snapshot(state, context, weather_tags=frozenset({"clear"}))
        changed = json.loads(json.dumps(initial, ensure_ascii=False))
        changed["plots"][0]["soil_state"] = "偏干"
        self.assertNotEqual(garden_scene.scene_key(initial), garden_scene.scene_key(changed))
        changed["plots"][0]["water_source"] = "rain"
        self.assertNotEqual(garden_scene.scene_key(initial), garden_scene.scene_key(changed))
        changed["weather_status"] = "stale"
        changed["weather_tags"] = []
        self.assertNotEqual(garden_scene.scene_key(initial), garden_scene.scene_key(changed))

    def test_fallback_weather_line_never_invents_weather_when_status_is_missing(self):
        snapshot = json.loads(json.dumps(self.snapshot, ensure_ascii=False))
        snapshot.update({"weather_status": "missing", "weather_tags": ["rain"]})
        text = garden_scene.fallback_scene(snapshot)
        self.assertIn("天气实况暂缺", text)
        self.assertNotIn("雨意", text)

    def test_environment_pool_combines_each_main_period_with_confirmed_weather(self):
        for period in ("dawn", "day", "dusk", "night"):
            with self.subTest(period=period):
                text = garden_content.environment_scene_line(
                    weather_tags=frozenset({"rain"}), weather_status="fresh",
                    day_period=period, stable_index=3,
                )
                self.assertIn("雨", text)
                self.assertTrue(text.endswith("。"))

    def test_writer_rejects_action_and_unearned_soil_or_rain_claims(self):
        snapshot = json.loads(json.dumps(self.snapshot, ensure_ascii=False))
        snapshot.update({"weather_status": "fresh", "weather_tags": ["clear"]})
        for text in (
            "院里正下着暴雨，黄瓜淋着雨，暖暖蹲在菜畦边。",
            "暖暖替黄瓜施了肥，又在菜畦边把土面拍得平平整整。",
            "傍晚，有人正在给黄瓜浇水，暖暖蹲在菜畦边。",
        ):
            with self.subTest(text=text):
                with self.assertRaises(GardenGeneratorError):
                    self._generate_from(json.dumps({"text": text}, ensure_ascii=False))

    def test_writer_rejects_forecast_animal_harm_departure_and_second_condition(self):
        snapshot = json.loads(json.dumps(self.snapshot, ensure_ascii=False))
        snapshot.update({"weather_status": "fresh", "weather_tags": ["clear"]})
        for text in (
            "预计明天会下雨，黄瓜先缩起叶片；暖暖蹲在菜畦边。",
            "傍晚，黄瓜还在地里；暖暖受伤流血，趴在菜畦边。",
            "傍晚，黄瓜还在地里；暖暖离开院子，以后再也不来。",
            "傍晚，黄瓜突然出现病斑；暖暖蹲在菜畦边。",
        ):
            with self.subTest(text=text):
                with patch.object(garden_generator, "_api_key", return_value="test-key"), patch(
                    "garden_generator.request_chat", return_value={
                    "choices": [{"message": {"content": json.dumps({"text": text}, ensure_ascii=False)}}],
                    },
                ), self.assertRaises(GardenGeneratorError):
                    garden_generator.generate_stroll(snapshot)

    def test_payload_strips_internal_condition_instance(self):
        snapshot = json.loads(json.dumps(self.snapshot, ensure_ascii=False))
        snapshot["plots"][0]["condition"] = {
            "type": "waterlogged", "status": "active", "severity": "warning",
            "_instance": "internal-only",
        }
        payload = garden_generator.build_stroll_payload(snapshot)
        sent = json.loads(payload["messages"][-1]["content"])
        self.assertNotIn("_instance", sent["snapshot"]["plots"][0]["condition"])
        self.assertNotIn("internal-only", payload["messages"][-1]["content"])

    def test_newly_written_json_scene_is_accepted(self):
        text = "傍晚，黄瓜在一号菜畦里安静舒展，暖暖蹲在旁边慢慢看着叶片。"
        self.assertEqual(
            self._generate_from(json.dumps({"text": text}, ensure_ascii=False)),
            text,
        )

    def test_malformed_overlong_banned_and_fact_omission_are_rejected(self):
        cases = (
            "不是JSON",
            json.dumps({"text": "院里正下着暴雨。"}, ensure_ascii=False),
            json.dumps({"candidate_id": "scene_999"}),
            json.dumps({"candidate_id": 1}),
            json.dumps({"candidate_id": "scene_1", "text": "改写"}),
            json.dumps(["scene_1"]),
        )
        for content in cases:
            with self.subTest(content=content[:20]):
                with self.assertRaises(GardenGeneratorError):
                    self._generate_from(content)


if __name__ == "__main__":
    unittest.main()
