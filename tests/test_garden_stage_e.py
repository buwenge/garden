import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden
import garden_generator
import home
import log_store
from garden_generator import GardenGeneratorError


TZ = ZoneInfo("Asia/Shanghai")


class _ConditionRng:
    def random(self):
        return 0.0

    def choice(self, values):
        return values[0]


class GardenStageEEventTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 28, 18, 0, tzinfo=TZ)
        self._write_crop()

    def _write_crop(self):
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS]
        state["meta"]["crop_seed_box_initialized"] = True
        state["plots"][0].update({
            "crop_id": "tomato",
            "planted_at": (self.now - timedelta(days=1)).isoformat(),
            "last_settled_at": self.now.isoformat(),
            "growth_points": 1.0,
            "stage": "sprout",
            "water_bonus_dates": [],
            "watering_by_date": {},
            "ready_at": None,
            "yield_penalty": 0,
            "status": "growing",
            "cycle_id": "cycle-e",
            "stage_events_seen": [],
        })
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _run_warning(self):
        with patch.dict(os.environ, {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"}):
            return garden.run_tick(self.now, rng=_ConditionRng(), path=self.path)

    def test_condition_copy_is_generated_outside_lock_persisted_and_reused_verbatim(self):
        real_locked = garden._locked
        lock_depth = 0

        @contextmanager
        def tracked_locked(*args, **kwargs):
            nonlocal lock_depth
            with real_locked(*args, **kwargs):
                lock_depth += 1
                try:
                    yield
                finally:
                    lock_depth -= 1

        generated = []

        def writer_call(facts):
            self.assertEqual(lock_depth, 0)
            self.assertEqual(facts["copy_type"], "condition_event")
            self.assertNotIn("condition_id", facts)
            self.assertNotIn("cycle_id", facts)
            generated.append("小番茄叶背冒出细小虫眼，一只小虫正沿着叶脉慢慢挪动。")
            return generated[-1]

        with (
            patch("garden._locked", tracked_locked),
            patch("garden.garden_generator.generate_crop_copy", side_effect=writer_call) as writer,
        ):
            first = self._run_warning()
            self.assertTrue(garden.release_pending_event(
                first["event_id"], first["delivery_token"], path=self.path,
            ))
            second = garden.run_tick(
                self.now + timedelta(minutes=1), rng=_ConditionRng(), path=self.path,
            )

        self.assertEqual(first["text"], second["text"])
        self.assertEqual(writer.call_count, 1)
        self.assertTrue(first["text"].startswith(generated[0] + "\n"))
        self.assertIn("结果：一号地的虫害已出现", first["text"])
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["pending_events"][0]["copy"]["source"], "deepseek")
        self.assertEqual(raw["pending_events"][0]["copy"]["text"], first["text"])

    def test_writer_failure_persists_complete_fallback_without_rolling_back_condition(self):
        with patch(
            "garden.garden_generator.generate_crop_copy",
            side_effect=GardenGeneratorError("离线"),
        ) as writer:
            first = self._run_warning()
            self.assertTrue(garden.release_pending_event(
                first["event_id"], first["delivery_token"], path=self.path,
            ))
            second = garden.run_tick(
                self.now + timedelta(minutes=1), rng=_ConditionRng(), path=self.path,
            )

        self.assertEqual(writer.call_count, 1)
        self.assertEqual(first["text"], second["text"])
        self.assertIn("需要除虫", first["text"])
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["plots"][0]["condition"]["status"], "active")
        self.assertEqual(raw["pending_events"][0]["copy"]["source"], "fallback")

    def test_condition_resolved_during_writer_call_cancels_stale_card(self):
        def resolve_while_writing(_facts):
            result = garden.resolve_crop_condition(
                "除虫", "p1", now=self.now + timedelta(seconds=1), path=self.path,
            )
            self.assertEqual(result["outcome"], "resolved")
            return "小番茄叶背的虫害仍清楚可见，细小啃痕留在原处。"

        with patch(
            "garden.garden_generator.generate_crop_copy",
            side_effect=resolve_while_writing,
        ):
            event = self._run_warning()

        self.assertIsNone(event)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["plots"][0]["condition"]["status"], "resolved")
        self.assertEqual(raw["pending_events"], [])

    def test_corrupt_persisted_copy_refuses_to_overwrite_state(self):
        with patch(
            "garden.garden_generator.generate_crop_copy",
            side_effect=GardenGeneratorError("离线"),
        ):
            self._run_warning()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["pending_events"][0]["copy"]["text"] = "结果被偷偷改掉了。"
        self.path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        before = self.path.read_bytes()

        with self.assertRaisesRegex(garden.GardenError, "正文与结果不一致"):
            garden.build_event(self.now + timedelta(minutes=10), path=self.path)

        self.assertEqual(self.path.read_bytes(), before)

    def test_all_condition_phases_have_code_owned_explicit_results(self):
        base = {
            "plot_id": "p1", "crop_id": "tomato", "condition_type": "pest",
        }
        for severity, penalty, marker in (
            ("warning", 0, "需要除虫"),
            ("damaged", 1, "永久减少1份"),
            ("withered", 1, "不会返还种子"),
        ):
            with self.subTest(severity=severity):
                event = {**base, "severity": severity, "yield_penalty": penalty}
                result = garden._condition_event_result_line(event)
                fallback = garden._condition_event_fallback_body(event)
                self.assertTrue(result.startswith("结果："))
                self.assertIn(marker, result)
                self.assertIn("小番茄", fallback)


class GardenStageEGeneratorTests(unittest.TestCase):
    def test_weather_animal_pools_have_six_safe_local_candidates_per_species_and_mode(self):
        for pool, required_modes in (
            (
                garden.garden_content.ANIMAL_WEATHER_LINES,
                ("sheltering", "cooling", "basking", "muddy", "wind_play", "normal"),
            ),
            (
                garden.garden_content.ANIMAL_WEATHER_PLAY_LINES,
                ("sheltering", "cooling", "basking", "muddy", "wind_play"),
            ),
        ):
            for species, modes in pool.items():
                for mode in required_modes:
                    lines = modes[mode]
                    with self.subTest(species=species, mode=mode, pool=id(pool)):
                        self.assertGreaterEqual(len(lines), 6)
                        self.assertEqual(len(lines), len(set(lines)))
            for mode in required_modes:
                category_lines = [set(pool[category][mode]) for category in garden.garden_content.ANIMAL_CATEGORIES]
                for left in range(len(category_lines)):
                    for right in range(left + 1, len(category_lines)):
                        self.assertFalse(category_lines[left] & category_lines[right])

    def test_stage_e_local_pool_matrix_meets_every_minimum(self):
        content = garden.garden_content
        self.assertEqual(
            set(content.WATERING_FALLBACKS),
            {
                "watered", "no_need", "too_wet", "protest", "waterlogged",
                "blocked_by_condition", "already_waterlogged", "refused",
            },
        )
        for outcome, lines in content.WATERING_FALLBACKS.items():
            with self.subTest(kind="watering", outcome=outcome):
                self.assertGreaterEqual(len(lines), 8)
                self.assertGreaterEqual(len(content._WATERING_LIGHT_COMEDY[outcome]), 2)
        for context, lines in content.WATERING_CONTEXT_FALLBACKS.items():
            with self.subTest(kind="watering_context", context=context):
                self.assertGreaterEqual(len(lines), 8)
                self.assertGreaterEqual(len(content._WATERING_CONTEXT_LIGHT_COMEDY[context]), 2)
        for outcome, condition_pools in content.TREATMENT_FALLBACKS.items():
            for condition_type, lines in condition_pools.items():
                with self.subTest(kind="treatment", outcome=outcome, condition=condition_type):
                    self.assertGreaterEqual(len(lines), 6)
        for category, lines in content.CROP_ENVIRONMENT_FALLBACKS.items():
            with self.subTest(kind="crop_environment", category=category):
                self.assertGreaterEqual(len(lines), 6)
                for crop in garden.garden_crops.CROPS.values():
                    rendered = lines[0].format(crop=crop["name"], condition="虫害")
                    self.assertIn(crop["name"], rendered)
        for condition_type, lines in garden._CONDITION_EVENT_VARIANTS.items():
            with self.subTest(kind="condition", condition_type=condition_type):
                self.assertGreaterEqual(len(lines), 6)
        for severity, lines in garden._CONDITION_SEVERITY_VARIANTS.items():
            with self.subTest(kind="severity", severity=severity):
                self.assertGreaterEqual(len(lines), 6)

    def test_environment_matrix_has_four_composable_lines_per_mode_and_main_period(self):
        content = garden.garden_content
        expected_modes = {
            "clear", "hot_clear", "cloudy", "rain", "rain_after",
            "humid_wet", "windy", "cold_clear", "unsafe", "neutral",
        }
        self.assertEqual(set(content.ENVIRONMENT_SCENE_LINES), expected_modes)
        self.assertTrue(all(len(lines) >= 4 for lines in content.ENVIRONMENT_SCENE_LINES.values()))
        self.assertTrue(all(len(content.ENVIRONMENT_PERIOD_LINES[period]) >= 4 for period in ("dawn", "day", "dusk", "night")))
        self.assertTrue(all(len(content.ENVIRONMENT_SEASON_DEFAULT_LINES[season]) >= 4 for season in ("spring", "summer", "autumn", "winter")))

        cases = (
            ("clear", {"clear"}, "fresh", False, False),
            ("hot_clear", {"clear", "hot"}, "fresh", False, False),
            ("cloudy", {"cloudy"}, "fresh", False, False),
            ("rain", {"rain"}, "fresh", False, False),
            ("rain_after", {"clear"}, "fresh", True, True),
            ("humid_wet", {"humid"}, "fresh", False, True),
            ("windy", {"windy"}, "fresh", False, False),
            ("cold_clear", {"clear", "cold"}, "fresh", False, False),
            ("unsafe", {"storm"}, "fresh", False, False),
            ("neutral", set(), "fresh", False, False),
            ("season_default", set(), "missing", False, False),
        )
        for expected, tags, status, recent_rain, soil_wet in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    content.environment_scene_mode(
                        weather_tags=frozenset(tags), weather_status=status,
                        recent_rain=recent_rain, soil_wet=soil_wet,
                    ),
                    expected,
                )
                for period in ("dawn", "day", "dusk", "night"):
                    for stable_index in range(4):
                        text = content.environment_scene_line(
                            weather_tags=frozenset(tags), weather_status=status,
                            day_period=period, season="summer",
                            recent_rain=recent_rain, soil_wet=soil_wet,
                            stable_index=stable_index,
                        )
                        self.assertTrue(content.scene_text_compatible(
                            text, season="summer", day_period=period,
                            weather_tags=frozenset(tags) if status == "fresh" else frozenset(),
                            recent_rain=recent_rain,
                        ))

    def test_missing_weather_season_defaults_fit_an_empty_yard_and_day_never_claims_noon(self):
        forbidden_crop_claims = ("枝叶", "浓绿", "藤叶", "新芽", "嫩绿", "返青", "枝梗")
        for season in ("spring", "summer", "autumn", "winter"):
            snapshot = {
                "day_period": "day", "season": season,
                "solar_term": {"id": "", "name": ""},
                "festival": {"id": "", "name": ""},
                "weather_status": "missing", "weather_tags": [],
                "animal_weather_mode": "normal", "animals": [],
                "plots": [
                    {"plot_id": plot_id, "status": "empty"}
                    for plot_id in ("p1", "p2", "p3", "p4")
                ],
            }
            for stable_index, line in enumerate(
                garden.garden_content.ENVIRONMENT_SEASON_DEFAULT_LINES[season]
            ):
                with self.subTest(season=season, stable_index=stable_index):
                    self.assertFalse(any(marker in line for marker in forbidden_crop_claims))
                    scene = garden.garden_scene._fallback_scene(
                        snapshot, selection_offset=stable_index,
                    )
                    self.assertIn("四块菜畦都还空着", scene)
                    self.assertFalse(any(marker in scene for marker in forbidden_crop_claims))
        self.assertFalse(any(
            "午间" in line
            for line in garden.garden_content.ENVIRONMENT_PERIOD_LINES["day"]
        ))

    def test_all_watering_local_candidates_pass_the_runtime_fact_filter(self):
        content = garden.garden_content
        base = self._facts(
            copy_type="watering", condition_type=None, condition_name=None,
            style=None, watering_count=1, accelerated=False,
            weather_status="missing", weather_tags=[], soil_state="偏干",
            water_source="none",
        )
        outcome_facts = {
            "watered": base,
            "no_need": {**base, "outcome": "no_need", "soil_state": "合适"},
            "too_wet": {**base, "outcome": "too_wet", "soil_state": "湿润"},
            "protest": {**base, "outcome": "protest", "style": "slapstick", "soil_state": "湿润", "watering_count": 2},
            "waterlogged": {**base, "outcome": "waterlogged", "soil_state": "湿透", "watering_count": 3},
            "blocked_by_condition": {
                **base, "outcome": "blocked_by_condition", "condition_type": "pest",
                "condition_name": "虫害", "soil_state": "湿润", "watering_count": 2,
            },
            "already_waterlogged": {**base, "outcome": "already_waterlogged", "soil_state": "湿透"},
            "refused": {**base, "outcome": "refused", "soil_state": "湿透"},
        }
        for outcome, lines in content.WATERING_FALLBACKS.items():
            facts = {**outcome_facts[outcome], "outcome": outcome}
            for line in lines:
                with self.subTest(outcome=outcome, line=line):
                    text = line.format(crop="小番茄", condition=facts.get("condition_name") or "异常")
                    self.assertEqual(garden_generator.validate_crop_copy_text(text, facts), text)
                    self.assertTrue(content.scene_text_compatible(
                        text, season="summer", day_period="day",
                        weather_tags=frozenset(), recent_rain=False,
                    ))

        contextual = {
            "hot_same_day_watered": {
                **base, "outcome": "watered", "watering_count": 2,
                "weather_status": "fresh", "weather_tags": ["clear", "hot"],
                "watering_reason": "hot_clear_dry",
            },
            "hot_clear_dry_watered": {
                **base, "outcome": "watered", "watering_count": 1,
                "weather_status": "fresh", "weather_tags": ["clear", "hot"],
                "watering_reason": "hot_clear_dry",
            },
            "heat_dry_watered": {
                **base, "outcome": "watered", "weather_status": "fresh",
                "weather_tags": ["hot"], "watering_reason": "hot_dry",
            },
            "wind_dry_watered": {
                **base, "outcome": "watered", "weather_status": "fresh",
                "weather_tags": ["windy"], "watering_reason": "wind_dry",
            },
            "low_humidity_dry_watered": {
                **base, "outcome": "watered", "weather_status": "fresh",
                "weather_tags": [], "watering_reason": "low_humidity_dry",
            },
            "rain_no_need": {
                **base, "outcome": "no_need", "soil_state": "湿润",
                "water_source": "rain", "weather_status": "fresh",
                "weather_tags": ["clear"],
            },
            "weather_missing": {
                **base, "outcome": "no_need", "soil_state": "合适",
            },
        }
        for category, lines in content.WATERING_CONTEXT_FALLBACKS.items():
            facts = contextual[category]
            recent_rain = facts["water_source"] == "rain"
            tags = frozenset(facts["weather_tags"])
            for line in lines:
                with self.subTest(category=category, line=line):
                    text = line.format(crop="小番茄", condition="异常")
                    self.assertEqual(garden_generator.validate_crop_copy_text(text, facts), text)
                    self.assertTrue(content.scene_text_compatible(
                        text, season="summer", day_period="day",
                        weather_tags=tags, recent_rain=recent_rain,
                    ))

    def test_all_condition_and_treatment_candidates_pass_the_runtime_result_filter(self):
        content = garden.garden_content
        condition_names = {
            "pest": "虫害", "diseased_leaf": "病叶",
            "waterlogged": "积水", "nutrient_deficiency": "缺肥",
        }
        for condition_type, lines in garden._CONDITION_EVENT_VARIANTS.items():
            facts = self._facts(
                condition_type=condition_type,
                condition_name=condition_names[condition_type],
            )
            for line in lines:
                with self.subTest(phase="warning", condition_type=condition_type, line=line):
                    text = line.format(crop="小番茄")
                    self.assertEqual(garden_generator.validate_crop_copy_text(text, facts), text)
        for severity, lines in garden._CONDITION_SEVERITY_VARIANTS.items():
            for condition_type, condition_name in condition_names.items():
                facts = self._facts(
                    condition_type=condition_type, condition_name=condition_name,
                    severity=severity, yield_penalty=1,
                )
                for line in lines:
                    with self.subTest(phase=severity, condition_type=condition_type, line=line):
                        text = line.format(crop="小番茄", condition=condition_name)
                        self.assertEqual(garden_generator.validate_crop_copy_text(text, facts), text)
        actions = {
            "pest": "除虫", "diseased_leaf": "修剪",
            "waterlogged": "松土", "nutrient_deficiency": "施肥",
        }
        for outcome, condition_pools in content.TREATMENT_FALLBACKS.items():
            for condition_key, lines in condition_pools.items():
                condition_types = (
                    tuple(condition_names) if condition_key == "_any"
                    else (None,) if condition_key == "_none"
                    else (condition_key,)
                )
                for condition_type in condition_types:
                    action = actions.get(condition_type, "除虫")
                    facts = self._facts(
                        copy_type="treatment", outcome=outcome, action=action,
                        condition_type=condition_type,
                        condition_name=condition_names.get(condition_type),
                        yield_penalty=0,
                    )
                    for line in lines:
                        with self.subTest(
                            phase="treatment", outcome=outcome,
                            condition=condition_type, line=line,
                        ):
                            text = line.format(
                                crop="小番茄", action=action,
                                condition=condition_names.get(condition_type, "异常"),
                            )
                            self.assertEqual(
                                garden_generator.validate_crop_copy_text(text, facts), text,
                            )

    def test_crop_environment_candidates_are_compatible_with_their_triggering_facts(self):
        content = garden.garden_content
        cases = {
            "moisture_ok": ("合适", "none", set(), "steady", False, ""),
            "hot_dry": ("偏干", "none", {"clear", "hot"}, "slow", False, ""),
            "rain_watered": ("湿润", "rain", {"clear"}, "steady", False, ""),
            "long_wet": ("湿透", "manual", {"cloudy"}, "steady", True, ""),
            "growth_good": ("合适", "manual", {"clear"}, "fast", False, ""),
            "growth_slow": ("偏干", "manual", {"cloudy"}, "slow", False, ""),
            "weather_condition": ("湿透", "rain", {"cloudy"}, "steady", True, "积水"),
        }
        for expected, args in cases.items():
            soil_state, source, tags, trend, long_wet, condition = args
            snapshot = {
                "season": "summer", "day_period": "day", "weather_status": "fresh",
                "weather_tags": sorted(tags),
                "animals": [],
                "plots": [{
                    "crop_name": "小番茄", "soil_state": soil_state,
                    "water_source": source,
                    **({"condition": {"type": "waterlogged"}} if condition else {}),
                }],
            }
            for stable_index, template in enumerate(content.CROP_ENVIRONMENT_FALLBACKS[expected]):
                selected = content.crop_environment_line(
                    crop="小番茄", condition=condition, soil_state=soil_state,
                    water_source=source, weather_tags=frozenset(tags),
                    growth_trend=trend, long_wet=long_wet, stable_index=stable_index,
                )
                self.assertEqual(selected, template.format(
                    crop="小番茄", condition=condition or "异常",
                ))
                with self.subTest(category=expected, stable_index=stable_index):
                    garden.garden_scene.validate_writer_text(selected, snapshot)

    def test_writer_failure_uses_weather_facts_loaded_by_independent_watering_entry(self):
        now = datetime(2026, 7, 28, 14, 0, tzinfo=TZ)
        hot_observation = {
            "observed_at": now.isoformat(), "tags": ["clear", "hot"],
        }
        watered = {
            "plot": {"plot_id": "p1", "crop_id": "tomato", "soil": {"moisture": 80.0, "last_water_source": "manual"}},
            "outcome": "watered", "watering_count": 2, "accelerated": False,
            "condition_type": None, "style": None, "moisture_before": 40.0,
            "moisture_after": 80.0, "reason": "hot_dry",
        }
        with (
            patch("garden._environment_observations", return_value=[hot_observation]),
            patch("garden.garden_generator.generate_crop_copy", side_effect=GardenGeneratorError("离线")),
        ):
            text = garden.watering_copy(watered, now=now, fallback="旧兜底")
        self.assertTrue(any(word in text for word in ("高温", "晴热", "晴晒", "烈日", "日头", "太阳", "热意", "晒热")))

        rain_no_need = {
            **watered,
            "plot": {"plot_id": "p1", "crop_id": "tomato", "soil": {"moisture": 76.0, "last_water_source": "rain"}},
            "outcome": "no_need", "watering_count": 0, "moisture_before": 76.0,
            "moisture_after": 76.0, "reason": "unneeded",
        }
        clear_observation = {"observed_at": now.isoformat(), "tags": ["clear"]}
        with (
            patch("garden._environment_observations", return_value=[clear_observation]),
            patch("garden.garden_generator.generate_crop_copy", side_effect=GardenGeneratorError("离线")),
        ):
            text = garden.watering_copy(rain_no_need, now=now, fallback="旧兜底")
        self.assertIn("雨", text)

    def test_local_crop_pool_is_filtered_before_legacy_minimum_fallback(self):
        facts = self._facts(
            copy_type="watering", outcome="no_need", condition_type=None,
            condition_name=None, style=None, watering_count=0, accelerated=False,
            weather_status="missing", weather_tags=[], soil_state="合适",
            water_source="none",
        )
        minimum = "小番茄根边的状态保持不变，这次没有倒水。"
        with (
            patch("garden.garden_generator.generate_crop_copy", side_effect=GardenGeneratorError("离线")),
            patch("garden.garden_content.local_crop_fallback", return_value="雨后的小番茄已经被雨浇透了。"),
        ):
            text, source = garden._generate_crop_copy_body(
                facts, minimum, now=datetime(2026, 7, 28, 12, 0, tzinfo=TZ),
                weather_tags=frozenset(),
            )
        self.assertEqual(text, minimum)
        self.assertEqual(source, "fallback")

    def test_failure_reasons_are_reduced_to_safe_categories(self):
        cases = {
            "小院子写手暂时连不上": "transport_error",
            "超时": "transport_error",
            "小院子写手暂时拒绝服务（HTTP 503）": "http_error",
            "小院子写手没有返回正文": "empty_response",
            "小院子写手返回的逛逛正文越界": "length_error",
            "小院子写手漏掉了主要作物": "fact_omission",
            "任意注入异常正文": "validation_error",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(
                    garden_generator.safe_failure_category(GardenGeneratorError(message)),
                    expected,
                )

    def test_all_writer_failure_classes_land_on_compatible_local_copy(self):
        facts = self._facts(
            copy_type="watering", outcome="no_need", condition_type=None,
            condition_name=None, style=None, watering_count=0, accelerated=False,
            weather_status="missing", weather_tags=[], soil_state="合适",
            water_source="none",
        )
        for message in (
            "超时",
            "小院子写手没有返回正文",
            "小院子写手暂时拒绝服务（HTTP 503）",
            "小院子写手返回的作物正文越界",
            "小院子写手漏掉了当前作物",
        ):
            with self.subTest(message=message), patch(
                "garden.garden_generator.generate_crop_copy",
                side_effect=GardenGeneratorError(message),
            ):
                text, source = garden._generate_crop_copy_body(
                    facts,
                    "小番茄根边的状态保持不变，这次没有倒水。",
                    now=datetime(2026, 7, 28, 12, 0, tzinfo=TZ),
                    weather_tags=frozenset(),
                )
                self.assertEqual(source, "fallback")
                self.assertIn("小番茄", text)
                self.assertNotIn("雨", text)

    def _facts(self, **changes):
        facts = {
            "copy_type": "condition_event",
            "plot_label": "一号地",
            "crop_name": "小番茄",
            "condition_type": "pest",
            "condition_name": "虫害",
            "severity": "warning",
            "yield_penalty": 0,
            "correct_action": "除虫",
            "season": "summer",
            "day_period": "dusk",
            "weather_tags": [],
            "condition_id": "private-condition-id",
            "cycle_id": "private-cycle-id",
        }
        facts.update(changes)
        return facts

    def _generate(self, text, facts=None):
        response = {
            "choices": [{"message": {"content": json.dumps({"text": text}, ensure_ascii=False)}}],
        }
        with (
            patch("garden_generator._api_key", return_value="test-key"),
            patch("garden_generator.request_chat", return_value=response),
        ):
            return garden_generator.generate_crop_copy(facts or self._facts())

    def test_payload_only_contains_allowlisted_visible_facts(self):
        payload = garden_generator.build_crop_copy_payload(self._facts())
        sent = json.loads(payload["messages"][1]["content"])
        self.assertEqual(set(sent), {"facts"})
        self.assertNotIn("candidate_id", payload["messages"][1]["content"])
        self.assertNotIn("candidates", sent)
        public = sent["facts"]
        self.assertNotIn("condition_id", public)
        self.assertNotIn("cycle_id", public)
        self.assertNotIn("result", public)
        self.assertEqual(public["correct_action"], "除虫")
        watering = self._facts(
            copy_type="watering", outcome="no_need", condition_type=None,
            condition_name=None, style=None, watering_count=0, accelerated=False,
            weather_status="missing", weather_tags=[], soil_state="合适",
            water_source="none", watering_reason="normal_dry",
        )
        public_watering = json.loads(
            garden_generator.build_crop_copy_payload(watering)["messages"][1]["content"]
        )["facts"]
        self.assertEqual(public_watering["watering_reason"], "normal_dry")
        self.assertEqual(public_watering["soil_state"], "合适")
        self.assertEqual(public_watering["water_source"], "none")

    def test_valid_copy_is_accepted_but_result_rewrites_and_comfort_are_rejected(self):
        valid = "小番茄叶背多了几处虫眼，小虫沿着叶脉缓慢爬动。"
        self.assertEqual(self._generate(valid), valid)
        for bad in (
            "小番茄的虫害已经解决，叶片重新恢复生长。",
            "小番茄长了虫眼，不过没关系，一切都会好起来。",
            "小番茄叶背都是虫眼。结果：请立刻除虫。",
        ):
            with self.subTest(text=bad), self.assertRaises(GardenGeneratorError):
                self._generate(bad)

    def test_physical_watering_rejects_dialogue_but_slapstick_allows_it(self):
        physical = self._facts(
            copy_type="watering", outcome="protest", style="physical",
            watering_count=2, accelerated=False, condition_type=None,
            condition_name=None,
        )
        text = "小番茄猛甩叶片，冲着水壶嚷：“今天已经喝过啦！”"
        with self.assertRaises(GardenGeneratorError):
            self._generate(text, physical)
        slapstick = {**physical, "style": "slapstick"}
        self.assertEqual(self._generate(text, slapstick), text)

    def test_model_must_submit_new_text_and_cannot_change_the_event_action(self):
        facts = self._facts()
        for payload in (
            {"text": "暖暖替小番茄施了肥。"},
            {"candidate_id": "copy_999"},
        ):
            response = {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
            with (
                patch("garden_generator._api_key", return_value="test-key"),
                patch("garden_generator.request_chat", return_value=response),
                self.assertRaises(GardenGeneratorError),
            ):
                garden_generator.generate_crop_copy(facts)

    def test_watering_writer_failure_uses_stable_local_pool_without_result_line(self):
        facts = self._facts(
            copy_type="watering", outcome="no_need", style=None, watering_count=1,
            accelerated=False, condition_type=None, condition_name=None,
        )
        fallback = garden.garden_content.local_crop_fallback(facts, "旧兜底")
        self.assertIn("小番茄", fallback)
        self.assertNotIn("结果：", fallback)
        self.assertEqual(fallback, garden.garden_content.local_crop_fallback(facts, "另一个旧兜底"))

    def test_contextual_watering_fallback_never_guesses_unconfirmed_weather(self):
        facts = self._facts(
            copy_type="watering", outcome="watered", style=None, watering_count=2,
            accelerated=False, condition_type=None, condition_name=None,
            weather_tags=["clear", "hot"],
            watering_reason="hot_clear_dry",
        )
        hot = garden.garden_content.local_crop_fallback(facts, "旧兜底")
        self.assertIn("{crop}".replace("{crop}", "小番茄"), hot)
        self.assertTrue(any(word in hot for word in ("高温", "晴晒", "晴热", "太阳", "热意", "干热")))
        missing = garden.garden_content.local_crop_fallback({
            **facts, "weather_tags": [], "watering_reason": "normal_dry",
        }, "旧兜底")
        self.assertNotIn("高温", missing)
        self.assertNotIn("晴晒", missing)

    def test_watering_reason_comes_from_observed_interval_not_current_weather(self):
        start = datetime(2026, 7, 28, 10, 0, tzinfo=TZ)
        end = start + timedelta(hours=2)
        hot_clear = [{
            "observed_at": start.isoformat(), "tags": ["clear", "hot"],
            "temp_c": 34.0, "feels_like_c": 36.0, "humidity_pct": 55.0,
        }]
        windy = [{
            "observed_at": start.isoformat(), "tags": ["windy"],
            "temp_c": 25.0, "humidity_pct": 60.0,
        }]
        low_humidity = [{
            "observed_at": start.isoformat(), "tags": [],
            "temp_c": 25.0, "humidity_pct": 25.0,
        }]
        self.assertEqual(
            garden.garden_weather.drying_reason(start, end, hot_clear),
            "hot_clear_dry",
        )
        self.assertEqual(
            garden.garden_weather.drying_reason(start, end, windy),
            "wind_dry",
        )
        self.assertEqual(
            garden.garden_weather.drying_reason(start, end, low_humidity),
            "low_humidity_dry",
        )
        self.assertEqual(
            garden.garden_weather.drying_reason(start, end, []),
            "normal_dry",
        )

    def test_heavy_rain_resets_earlier_hot_drying_attribution(self):
        start = datetime(2026, 7, 20, 10, 0, tzinfo=TZ)
        now = start + timedelta(hours=72)
        observations = [{
            "observed_at": start.isoformat(),
            "tags": ["clear", "hot"],
            "temp_c": 36.0, "feels_like_c": 38.0,
            "humidity_pct": 45.0, "precip_rate_mm_h": 0.0,
        }, {
            "observed_at": (start + timedelta(hours=3)).isoformat(),
            "tags": ["rain"], "temp_c": 26.0, "humidity_pct": 95.0,
            "precip_rate_mm_h": 6.0,
        }]
        observations.extend({
            "observed_at": (start + timedelta(hours=hour)).isoformat(),
            "tags": [], "temp_c": 26.0, "humidity_pct": 60.0,
            "precip_rate_mm_h": 0.0,
        } for hour in range(4, 73, 2))

        state = garden._empty_state()
        state["version"] = 5
        plot = garden._empty_plot("p1")
        plot.update({
            "status": "growing", "crop_id": "tomato", "cycle_id": "rain-reset",
            "soil": garden._new_soil(start, 60.0),
            "watering_history": [], "watering_by_date": {},
            "unneeded_watering_by_date": {}, "unneeded_watering_events": {},
        })
        state["plots"] = [plot] + [
            garden._empty_plot(plot_id) for plot_id in ("p2", "p3", "p4")
        ]
        garden._refresh_plot_live(plot, now, observations)
        self.assertLess(plot["soil"]["moisture"], 50.0)
        self.assertEqual(
            plot["soil"]["last_rain_at"],
            (start + timedelta(hours=3)).isoformat(),
        )
        # 若错误地从最初锚点累计，短时晴热仍会赢；真实动作必须从后来大雨
        # 补湿的时刻重新建立因果起点。
        self.assertEqual(
            garden.garden_weather.drying_reason(start, now, observations),
            "hot_clear_dry",
        )
        result, changed = garden._water_crop_v5_unlocked(
            state, plot, now, _ConditionRng(), observations,
        )
        self.assertTrue(changed)
        self.assertEqual(result["outcome"], "watered")
        self.assertEqual(result["reason"], "normal_dry")

    def test_effective_watering_carries_past_wind_reason_into_copy_when_now_is_calm(self):
        start = datetime(2026, 7, 28, 10, 0, tzinfo=TZ)
        now = start + timedelta(hours=3)
        state = garden._empty_state()
        state["version"] = 5
        plot = garden._empty_plot("p1")
        plot.update({
            "status": "growing", "crop_id": "tomato",
            "soil": garden._new_soil(start, 40.0),
            "watering_history": [], "watering_by_date": {},
            "unneeded_watering_by_date": {}, "unneeded_watering_events": {},
        })
        state["plots"] = [plot] + [
            garden._empty_plot(plot_id) for plot_id in ("p2", "p3", "p4")
        ]
        past_wind = [{
            "observed_at": start.isoformat(), "tags": ["windy"],
            "temp_c": 25.0, "humidity_pct": 60.0,
        }]
        result, changed = garden._water_crop_v5_unlocked(
            state, plot, now, _ConditionRng(), past_wind,
        )
        self.assertTrue(changed)
        self.assertEqual(result["reason"], "wind_dry")

        calm_now = [{"observed_at": now.isoformat(), "tags": []}]
        with (
            patch("garden._environment_observations", return_value=calm_now),
            patch(
                "garden.garden_generator.generate_crop_copy",
                side_effect=GardenGeneratorError("离线"),
            ),
        ):
            text = garden.watering_copy(result, now=now, fallback="旧兜底")
        self.assertIn("风", text)
        self.assertNotIn("风还没停", text)

    def test_watering_style_and_missing_weather_keep_separate_candidate_pools(self):
        facts = self._facts(
            copy_type="watering", outcome="protest", watering_count=2,
            accelerated=False, condition_type=None, condition_name=None,
            weather_status="missing", weather_tags=[], soil_state="湿润",
            water_source="manual",
        )
        physical = garden.garden_content.local_crop_candidates(
            {**facts, "style": "physical"}, "旧兜底",
        )
        slapstick = garden.garden_content.local_crop_candidates(
            {**facts, "style": "slapstick"}, "旧兜底",
        )
        self.assertTrue(physical)
        self.assertTrue(slapstick)
        self.assertFalse(set(physical) & set(slapstick))
        self.assertTrue(all("免浇牌" not in line for line in physical))
        self.assertTrue(any("免浇牌" in line or "软钉子" in line for line in slapstick))

    def test_every_watering_outcome_and_context_selects_both_real_and_comedy_pools(self):
        base = self._facts(
            copy_type="watering", condition_type=None, condition_name=None,
            watering_count=1, accelerated=False, weather_status="fresh",
            weather_tags=[], soil_state="偏干", water_source="manual",
            watering_reason="normal_dry",
        )
        outcomes = {
            "watered": base,
            "no_need": {**base, "soil_state": "合适"},
            "too_wet": {**base, "soil_state": "湿润"},
            "protest": {**base, "soil_state": "湿润", "watering_count": 2},
            "waterlogged": {**base, "soil_state": "湿透", "watering_count": 3},
            "blocked_by_condition": {
                **base, "condition_type": "pest", "condition_name": "虫害",
                "soil_state": "湿润", "watering_count": 2,
            },
            "already_waterlogged": {
                **base, "condition_type": "waterlogged", "condition_name": "积水",
                "soil_state": "湿透",
            },
            "refused": {**base, "soil_state": "湿透"},
        }
        for outcome, facts in outcomes.items():
            with self.subTest(kind="outcome", outcome=outcome):
                physical = garden.garden_content.local_crop_candidates(
                    {**facts, "outcome": outcome, "style": "physical"}, "旧兜底",
                )
                comedy = garden.garden_content.local_crop_candidates(
                    {**facts, "outcome": outcome, "style": "slapstick"}, "旧兜底",
                )
                self.assertTrue(physical)
                self.assertTrue(comedy)
                self.assertFalse(set(physical) & set(comedy))

        contexts = {
            "hot_same_day_watered": {
                **base, "outcome": "watered", "watering_count": 2,
                "weather_tags": ["clear", "hot"], "watering_reason": "hot_clear_dry",
            },
            "hot_clear_dry_watered": {
                **base, "outcome": "watered",
                "weather_tags": ["clear", "hot"], "watering_reason": "hot_clear_dry",
            },
            "heat_dry_watered": {
                **base, "outcome": "watered",
                "weather_tags": ["hot"], "watering_reason": "hot_dry",
            },
            "wind_dry_watered": {
                **base, "outcome": "watered",
                "weather_tags": ["windy"], "watering_reason": "wind_dry",
            },
            "low_humidity_dry_watered": {
                **base, "outcome": "watered", "watering_reason": "low_humidity_dry",
            },
            "rain_no_need": {
                **base, "outcome": "no_need", "soil_state": "湿润",
                "water_source": "rain", "weather_tags": ["rain"],
            },
            "weather_missing": {
                **base, "outcome": "no_need", "soil_state": "合适",
                "water_source": "none", "weather_status": "missing",
            },
        }
        for context, facts in contexts.items():
            with self.subTest(kind="context", context=context):
                physical = garden.garden_content.local_crop_candidates(
                    {**facts, "style": "physical"}, "旧兜底",
                )
                comedy = garden.garden_content.local_crop_candidates(
                    {**facts, "style": "slapstick"}, "旧兜底",
                )
                self.assertTrue(physical)
                self.assertTrue(comedy)
                self.assertFalse(set(physical) & set(comedy))
                rendered_context = (
                    garden.garden_content.WATERING_CONTEXT_FALLBACKS[context]
                )
                self.assertTrue(any(
                    line.format(crop="小番茄") in set(physical) | set(comedy)
                    for line in rendered_context
                ))

    def test_every_v5_watering_outcome_can_reach_both_styles_through_the_action_path(self):
        def actual_result(expected: str, now: datetime) -> dict:
            state = garden._empty_state()
            state["version"] = 5
            moisture = 40.0 if expected == "watered" else 75.0 if expected in {
                "too_wet", "already_waterlogged", "refused",
            } else 60.0
            plot = garden._empty_plot("p1")
            plot.update({
                "status": "growing", "crop_id": "tomato", "cycle_id": "style-path",
                "soil": garden._new_soil(now - timedelta(hours=1), moisture),
                "watering_history": [], "watering_by_date": {},
                "unneeded_watering_by_date": {}, "unneeded_watering_events": {},
            })
            state["plots"] = [plot] + [
                garden._empty_plot(plot_id) for plot_id in ("p2", "p3", "p4")
            ]
            event_count = {
                "protest": 1, "waterlogged": 2, "blocked_by_condition": 2,
                "refused": 3,
            }.get(expected, 0)
            if event_count:
                plot["unneeded_watering_events"][now.date().isoformat()] = [
                    (now - timedelta(minutes=event_count - index + 1)).isoformat()
                    for index in range(event_count)
                ]
            if expected in ("already_waterlogged", "refused"):
                garden._create_crop_condition(
                    state, plot, "waterlogged", now - timedelta(minutes=10),
                    announced=True,
                )
            elif expected == "blocked_by_condition":
                other = state["plots"][1]
                other.update({
                    "status": "growing", "crop_id": "cucumber",
                    "cycle_id": "other-condition",
                })
                garden._create_crop_condition(
                    state, other, "pest", now - timedelta(minutes=10),
                    announced=True,
                )
            result, _ = garden._water_crop_v5_unlocked(
                state, plot, now, _ConditionRng(), [],
            )
            self.assertEqual(result["outcome"], expected)
            return result

        start = datetime(2026, 7, 28, 12, 0, tzinfo=TZ)
        for outcome in (
            "watered", "no_need", "too_wet", "protest", "waterlogged",
            "blocked_by_condition", "already_waterlogged", "refused",
        ):
            styles = {
                actual_result(outcome, start + timedelta(minutes=offset))["style"]
                for offset in range(32)
            }
            with self.subTest(outcome=outcome):
                self.assertEqual(styles, {"physical", "slapstick"})

    def test_treatment_candidates_match_each_real_condition_action_pair(self):
        pairs = {
            "pest": ("虫害", "除虫", "小虫"),
            "diseased_leaf": ("病叶", "修剪", "病叶"),
            "waterlogged": ("积水", "松土", "积水"),
            "nutrient_deficiency": ("缺肥", "施肥", "养分"),
        }
        for condition_type, (condition_name, action, marker) in pairs.items():
            facts = self._facts(
                copy_type="treatment", outcome="resolved", action=action,
                condition_type=condition_type, condition_name=condition_name,
                yield_penalty=0,
            )
            candidates = garden.garden_content.local_crop_candidates(facts, "旧兜底")
            with self.subTest(condition_type=condition_type):
                self.assertEqual(len(candidates), 6)
                self.assertTrue(any(marker in line for line in candidates))
                self.assertFalse(any("枝叶被仔细松土" in line for line in candidates))


class GardenStageEHomeActionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.log_path = Path(self.tempdir.name) / "logs.jsonl"
        self.now = datetime.now(TZ)
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [garden._empty_plot(plot_id) for plot_id in garden._PLOT_IDS]
        state["meta"]["crop_seed_box_initialized"] = True
        state["plots"][0].update({
            "crop_id": "tomato", "planted_at": (self.now - timedelta(days=1)).isoformat(),
            "last_settled_at": self.now.isoformat(), "growth_points": 1.0,
            "stage": "sprout", "water_bonus_dates": [], "watering_by_date": {},
            "ready_at": None, "yield_penalty": 0, "status": "growing",
            "cycle_id": "cycle-home-e", "stage_events_seen": [],
        })
        self.state = state
        self._write_state()
        garden_patch = patch.object(garden, "GARDEN_FILE", self.path)
        log_patch = patch.object(log_store, "LOG_FILE", self.log_path)
        garden_patch.start()
        log_patch.start()
        self.addCleanup(garden_patch.stop)
        self.addCleanup(log_patch.stop)

    def _write_state(self):
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_correct_treatment_generates_after_state_lock_and_cannot_replace_result(self):
        condition = garden._create_crop_condition(
            self.state, self.state["plots"][0], "pest", self.now, announced=False,
        )
        garden._queue_condition_event(self.state, self.state["plots"][0], "warning")
        self._write_state()
        real_locked = garden._locked
        lock_depth = 0

        @contextmanager
        def tracked_locked(*args, **kwargs):
            nonlocal lock_depth
            with real_locked(*args, **kwargs):
                lock_depth += 1
                try:
                    yield
                finally:
                    lock_depth -= 1

        def writer_call(facts):
            self.assertEqual(lock_depth, 0)
            self.assertEqual(facts["outcome"], "resolved")
            self.assertNotIn("condition_id", facts)
            return "除虫后，小番茄叶背已经看不到活动的小虫。"

        with (
            patch("garden._locked", tracked_locked),
            patch("garden.garden_generator.generate_crop_copy", side_effect=writer_call),
        ):
            output = home.handle_garden(home.parse_request(["院子", "除虫", "一号地"]))

        self.assertIn("叶背已经看不到活动的小虫", output)
        self.assertIn("结果：一号地的虫害已解决", output)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["plots"][0]["condition"]["condition_id"], condition["condition_id"])
        self.assertEqual(raw["plots"][0]["condition"]["status"], "resolved")

    def test_writer_failure_does_not_roll_back_third_watering_or_hide_result(self):
        with patch(
            "garden.garden_generator.generate_crop_copy",
            side_effect=GardenGeneratorError("离线"),
        ):
            home.handle_garden(home.parse_request(["院子", "浇水", "一号地"]))
            home.handle_garden(home.parse_request(["院子", "浇水", "一号地"]))
            third = home.handle_garden(home.parse_request(["院子", "浇水", "一号地"]))

        self.assertIn("结果：一号地已进入积水状态", third)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["plots"][0]["condition"]["type"], "waterlogged")
        self.assertEqual(raw["plots"][0]["condition"]["status"], "active")

    def test_batch_blocked_result_does_not_claim_water_was_poured(self):
        output = home._garden_water_batch_text([{
            "plot": {"plot_id": "p1", "crop_id": "tomato"},
            "outcome": "blocked_by_condition", "condition_type": "pest",
        }])
        self.assertIn("浇水的意图已被拦住", output)
        self.assertNotIn("又浇了一遍", output)


if __name__ == "__main__":
    unittest.main()
