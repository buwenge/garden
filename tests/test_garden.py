import json
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import garden
from garden_calendar import DEFAULT_CALENDAR
from garden_festivals import FESTIVALS_BY_YEAR
from garden_generator import GardenGeneratorError

TZ = ZoneInfo("Asia/Shanghai")

# 独立于 garden_calendar._TERMS_2026 的验收数据；逐项抄录自香港天文台 2026
# 年二十四节气权威表（香港时间，即 UTC+8），防止测试复制实现常量自证正确。
HKO_2026_TERMS = (
    ("小寒", 1, 5, 16, 23), ("大寒", 1, 20, 9, 45), ("立春", 2, 4, 4, 2),
    ("雨水", 2, 18, 23, 52), ("惊蛰", 3, 5, 21, 59), ("春分", 3, 20, 22, 46),
    ("清明", 4, 5, 2, 40), ("谷雨", 4, 20, 9, 39), ("立夏", 5, 5, 19, 49),
    ("小满", 5, 21, 8, 37), ("芒种", 6, 5, 23, 48), ("夏至", 6, 21, 16, 25),
    ("小暑", 7, 7, 9, 57), ("大暑", 7, 23, 3, 13), ("立秋", 8, 7, 19, 43),
    ("处暑", 8, 23, 10, 19), ("白露", 9, 7, 22, 41), ("秋分", 9, 23, 8, 5),
    ("寒露", 10, 8, 14, 29), ("霜降", 10, 23, 17, 38), ("立冬", 11, 7, 17, 52),
    ("小雪", 11, 22, 15, 23), ("大雪", 12, 7, 10, 52), ("冬至", 12, 22, 4, 50),
)


class _StubRng:
    """确定性替身：.random() 按顺序吐出预设值，.choice() 固定取第 choice_index 项。"""

    def __init__(self, randoms, choice_index=0):
        self._randoms = list(randoms)
        self.choice_index = choice_index

    def random(self):
        return self._randoms.pop(0)

    def choice(self, seq):
        return seq[self.choice_index]


class GardenStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 24, 12, 0, tzinfo=TZ)

    def test_spawn_creates_active_entry(self):
        entry = garden.spawn("flower", species="蒲公英", intro="刚冒头", now=self.now, path=self.path)
        self.assertEqual(entry["status"], "active")
        self.assertEqual(entry["species"], "蒲公英")
        self.assertEqual(entry["care_count"], 0)
        self.assertIn(entry["spot"], garden.SPOTS)
        self.assertEqual(garden.active_entries(self.path), [entry])

    def test_elapsed_time_never_turns_a_visitor_into_a_needy_state(self):
        entry = garden.spawn("flower", species="蒲公英", intro="x", now=self.now, path=self.path)
        self.assertEqual(garden.compute_stage(entry, self.now + timedelta(hours=1)), "fresh")
        self.assertEqual(garden.compute_stage(entry, self.now + timedelta(days=100)), "fresh")

    def test_advance_moves_animal_to_natural_away_not_left_or_penalized(self):
        entry = garden.spawn("animal", species="橘猫", intro="x", now=self.now, path=self.path)
        entry["bond_points"] = 8
        garden.save_garden([entry], self.path)
        later = self.now + timedelta(hours=garden.NATURAL_VISIT_MAX_HOURS + 1)
        away = garden.advance(later, self.path)
        self.assertEqual(len(away), 1)
        self.assertEqual(away[0]["status"], "away")
        self.assertEqual(garden.active_entries(self.path), [])
        self.assertEqual(garden.left_entries(self.path), [])
        self.assertEqual(away[0]["bond_points"], 8)

    def test_advance_leaves_recently_cared_entries_alone(self):
        garden.spawn("flower", species="蒲公英", intro="x", now=self.now, path=self.path)
        left = garden.advance(self.now + timedelta(hours=10), self.path)
        self.assertEqual(left, [])
        self.assertEqual(len(garden.active_entries(self.path)), 1)

    def test_care_updates_history_without_claiming_revival(self):
        entry = garden.spawn("flower", species="蒲公英", intro="x", now=self.now, path=self.path)
        later = self.now + timedelta(days=100)
        result = garden.care(entry["id"], "浇水", now=later, path=self.path)
        self.assertFalse(result["revived"])
        self.assertEqual(result["entry"]["care_count"], 1)
        self.assertEqual(result["entry"]["last_cared_at"], later.isoformat())

        result2 = garden.care(entry["id"], "浇水", now=later + timedelta(hours=1), path=self.path)
        self.assertFalse(result2["revived"])
        self.assertEqual(result2["entry"]["care_count"], 2)

    def test_care_rejects_wrong_action_for_kind(self):
        entry = garden.spawn("animal", species="橘猫", intro="x", now=self.now, path=self.path)
        with self.assertRaisesRegex(garden.GardenError, "投喂/摸摸"):
            garden.care(entry["id"], "浇水", now=self.now, path=self.path)

    def test_care_unknown_id_raises(self):
        with self.assertRaises(garden.GardenError):
            garden.care("ffffff", "浇水", now=self.now, path=self.path)

    def test_care_ambiguous_prefix_raises(self):
        # 手工造两个前缀相同的 id，模拟真实场景里编号冲突的情况。
        entries = [
            {
                "id": "aa0001", "kind": "flower", "species": "蒲公英", "spot": "床头柜",
                "arrived_at": self.now.isoformat(), "last_cared_at": self.now.isoformat(),
                "care_count": 0, "status": "active", "left_at": None, "last_note": None,
            },
            {
                "id": "aa0002", "kind": "flower", "species": "雏菊", "spot": "信箱",
                "arrived_at": self.now.isoformat(), "last_cared_at": self.now.isoformat(),
                "care_count": 0, "status": "active", "left_at": None, "last_note": None,
            },
        ]
        garden.save_garden(entries, self.path)
        with self.assertRaisesRegex(garden.GardenError, "多条"):
            garden.care("aa00", "浇水", now=self.now, path=self.path)

    def test_left_entries_are_not_cared_for(self):
        entry = garden.spawn("flower", species="蒲公英", intro="x", now=self.now, path=self.path)
        entry["status"] = "left"  # 既有历史离开记录仍只读保留。
        garden.save_garden([entry], self.path)
        with self.assertRaises(garden.GardenError):
            garden.care(entry["id"], "浇水", now=self.now, path=self.path)

    def test_care_settles_natural_away_entry_before_rejecting_it(self):
        entry = garden.spawn("animal", species="橘猫", intro="x", now=self.now, path=self.path)
        with self.assertRaises(garden.GardenError):
            garden.care(
                entry["id"],
                "投喂",
                now=self.now + timedelta(hours=garden.NATURAL_VISIT_MAX_HOURS + 1),
                path=self.path,
            )
        self.assertEqual(garden.active_entries(self.path), [])
        self.assertEqual(garden.left_entries(self.path), [])

    def test_name_entry_persists_nickname(self):
        entry = garden.spawn("animal", species="橘猫", intro="x", now=self.now, path=self.path)
        renamed = garden.name_entry(
            entry["id"][:3], "小 橘", now=self.now, path=self.path,
        )
        self.assertEqual(renamed["nickname"], "小 橘")
        self.assertEqual(garden.display_name(renamed, include_species=True), "小 橘（橘猫）")

    def test_malformed_state_is_not_silently_replaced(self):
        self.path.write_text("{broken", encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "停止写入"):
            garden.spawn("flower", species="蒲公英", intro="x", now=self.now, path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_v3_animals_type_is_never_replaced_by_a_trace_write(self):
        broken = {"version": 3, "animals": {}, "legacy_plants": []}
        self.path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "animals.*停止写入"):
            garden.build_event(self.now, rng=_StubRng([0.92]), path=self.path)
        self.assertEqual(self.path.read_bytes(), before)


class CalendarAndMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"

    def test_beijing_day_period_boundaries(self):
        cases = (
            ((4, 59), "late_night"), ((5, 0), "dawn"), ((7, 59), "dawn"),
            ((8, 0), "day"), ((16, 59), "day"), ((17, 0), "dusk"),
            ((19, 59), "dusk"), ((20, 0), "night"), ((1, 59), "night"),
            ((2, 0), "late_night"),
        )
        for (hour, minute), expected in cases:
            with self.subTest(hour=hour, minute=minute):
                context = DEFAULT_CALENDAR.context_at(datetime(2026, 7, 24, hour, minute, tzinfo=TZ))
                self.assertEqual(context.day_period, expected)

    def test_solar_term_season_boundaries_are_second_precise(self):
        cases = (
            (datetime(2026, 2, 4, 4, 1, 59, tzinfo=TZ), "winter"),
            (datetime(2026, 2, 4, 4, 2, 0, tzinfo=TZ), "spring"),
            (datetime(2026, 5, 5, 19, 48, 59, tzinfo=TZ), "spring"),
            (datetime(2026, 5, 5, 19, 49, 0, tzinfo=TZ), "summer"),
            (datetime(2026, 8, 7, 19, 42, 59, tzinfo=TZ), "summer"),
            (datetime(2026, 8, 7, 19, 43, 0, tzinfo=TZ), "autumn"),
            (datetime(2026, 11, 7, 17, 51, 59, tzinfo=TZ), "autumn"),
            (datetime(2026, 11, 7, 17, 52, 0, tzinfo=TZ), "winter"),
        )
        for moment, expected in cases:
            with self.subTest(moment=moment):
                self.assertEqual(DEFAULT_CALENDAR.context_at(moment).season, expected)
        self.assertEqual(DEFAULT_CALENDAR.context_at(datetime(2027, 1, 1, tzinfo=TZ)).season, "winter")

    def test_all_24_terms_match_independent_hko_2026_values(self):
        for name, month, day, hour, minute in HKO_2026_TERMS:
            expected = datetime(2026, month, day, hour, minute, tzinfo=TZ)
            with self.subTest(term=name):
                context = DEFAULT_CALENDAR.context_at(expected)
                self.assertEqual(context.term_name, name)
                self.assertEqual(context.term_started_at, expected)

    def test_v2_migration_is_idempotent_and_keeps_legacy_data(self):
        v2 = {
            "version": 2,
            "entries": [
                {
                    "id": "cat001", "kind": "animal", "species": "橘猫", "nickname": "暖暖",
                    "spot": "床头柜", "arrived_at": "2026-07-20T08:00:00+08:00",
                    "last_cared_at": "2026-07-20T08:00:00+08:00", "care_count": 2,
                    "status": "active", "custom_v2_field": "保留我",
                },
                {
                    "id": "plant1", "kind": "flower", "species": "蒲公英", "spot": "信箱",
                    "arrived_at": "2026-07-20T08:00:00+08:00", "last_cared_at": "2026-07-20T08:00:00+08:00",
                    "care_count": 0, "status": "active",
                },
                {
                    "id": "oldcat", "kind": "animal", "species": "橘猫", "nickname": "旧朋友",
                    "spot": "信箱", "arrived_at": "2026-06-20T08:00:00+08:00",
                    "last_cared_at": "2026-06-20T08:00:00+08:00", "care_count": 12,
                    "status": "left", "left_at": "2026-07-01T08:00:00+08:00",
                },
                {"id": "odd001", "kind": "unknown", "value": 7},
            ],
            "meta": {"last_visible_event_at": "2026-07-20T08:00:00+08:00"},
            "unknown_top_level": {"keep": True},
        }
        self.path.write_text(json.dumps(v2, ensure_ascii=False), encoding="utf-8")
        entries = garden.load_garden(self.path)
        first = self.path.read_bytes()
        garden.load_garden(self.path)
        self.assertEqual(self.path.read_bytes(), first)

        migrated = json.loads(first)
        self.assertEqual(migrated["version"], 4)
        self.assertNotIn("entries", migrated)
        animal = migrated["animals"][0]
        self.assertEqual(animal["id"], "cat001")
        self.assertEqual(animal["spot"], "屋檐下与墙根")
        self.assertEqual(animal["legacy"]["original_spot"], "床头柜")
        self.assertEqual(animal["bond_points"], 3)
        self.assertEqual(animal["bond_level"], 1)
        self.assertEqual(animal["custom_v2_field"], "保留我")
        old_cat = next(item for item in migrated["animals"] if item["id"] == "oldcat")
        self.assertEqual(old_cat["status"], "left")
        self.assertEqual(old_cat["nickname"], "旧朋友")
        self.assertEqual(old_cat["bond_points"], 28)
        self.assertEqual(migrated["legacy_plants"][0]["id"], "plant1")
        self.assertEqual(migrated["legacy"]["unrecognized_entries"][0]["id"], "odd001")
        self.assertEqual(migrated["legacy"]["unrecognized_top_level"]["unknown_top_level"], {"keep": True})
        self.assertEqual({entry["id"] for entry in entries}, {"cat001", "oldcat", "plant1"})

    def test_v2_empty_garden_migrates_to_a_valid_v4_shell(self):
        self.path.write_text(json.dumps({"version": 2, "entries": []}), encoding="utf-8")
        self.assertEqual(garden.load_garden(self.path), [])
        migrated = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["version"], 4)
        self.assertEqual(migrated["animals"], [])
        self.assertEqual(migrated["legacy_plants"], [])
        self.assertEqual([plot["plot_id"] for plot in migrated["plots"]], ["p1", "p2", "p3", "p4"])


class CategoryPersonalityTests(unittest.TestCase):
    """动物"类别 × 性格"这块新维度：分类推断、迁移补齐、内容池选取。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 25, 12, 0, tzinfo=TZ)

    def test_spawn_infers_category_from_species_when_not_given(self):
        entry = garden.spawn("animal", species="柴犬串串", intro="x", now=self.now, path=self.path)
        self.assertEqual(entry["category"], "狗")
        self.assertIn(entry["personality"], garden.garden_content.PERSONALITIES)

    def test_spawn_accepts_explicit_category_and_personality(self):
        entry = garden.spawn(
            "animal", species="小刺猬", intro="x", category="刺猬", personality="怕生",
            now=self.now, path=self.path,
        )
        self.assertEqual(entry["category"], "刺猬")
        self.assertEqual(entry["personality"], "怕生")

    def test_flower_entries_have_no_category_or_personality(self):
        entry = garden.spawn("flower", species="蒲公英", intro="x", now=self.now, path=self.path)
        self.assertIsNone(entry["category"])
        self.assertIsNone(entry["personality"])

    def test_pick_animal_category_respects_cumulative_weights(self):
        # 猫 0.40 / 狗 0.30 / 兔子 0.17 / 刺猬 0.13（累计 0.40/0.70/0.87/1.00）。
        self.assertEqual(garden._pick_animal_category(_StubRng([0.0])), "猫")
        self.assertEqual(garden._pick_animal_category(_StubRng([0.39])), "猫")
        self.assertEqual(garden._pick_animal_category(_StubRng([0.41])), "狗")
        self.assertEqual(garden._pick_animal_category(_StubRng([0.69])), "狗")
        self.assertEqual(garden._pick_animal_category(_StubRng([0.71])), "兔子")
        self.assertEqual(garden._pick_animal_category(_StubRng([0.88])), "刺猬")
        self.assertEqual(garden._pick_animal_category(_StubRng([0.999])), "刺猬")

    def test_old_entry_missing_fields_is_backfilled_and_stable(self):
        # 模拟迁移前的旧存档条目：没有 category/personality 字段（正是生产
        # garden.json 里那只手动种下的橘猫的真实形状）。
        old_entry = {
            "id": "313b2e", "kind": "animal", "species": "橘猫", "nickname": "暖暖",
            "trait": "很会挑安静的角落", "spot": "床头柜",
            "arrived_at": self.now.isoformat(), "last_cared_at": self.now.isoformat(),
            "care_count": 2, "last_action": "投喂", "return_count": 0,
            "status": "active", "left_at": None, "departure_note": None,
            "last_note": "x",
        }
        garden.save_garden([old_entry], self.path)
        result = garden.care("313b2e", "投喂", now=self.now, path=self.path)
        entry = result["entry"]
        self.assertEqual(entry["category"], "猫")
        self.assertIn(entry["personality"], garden.garden_content.PERSONALITIES)

        # 第二次操作不应该重新随机性格——一旦补齐就要保持稳定。
        first_personality = entry["personality"]
        result2 = garden.care("313b2e", "摸摸", now=self.now + timedelta(minutes=5), path=self.path)
        self.assertEqual(result2["entry"]["personality"], first_personality)

    def test_backfill_also_runs_inside_build_event(self):
        old_entry = {
            "id": "aa1122", "kind": "animal", "species": "灰兔子", "nickname": None,
            "trait": "有点怕生", "spot": "信箱",
            "arrived_at": self.now.isoformat(), "last_cared_at": self.now.isoformat(),
            "care_count": 0, "last_action": None, "return_count": 0,
            "status": "active", "left_at": None, "departure_note": None,
            "last_note": "x",
        }
        garden.save_garden([old_entry], self.path)
        garden.build_event(self.now + timedelta(hours=1), rng=_StubRng([0.10]), path=self.path)
        entries = garden.active_entries(self.path)
        self.assertEqual(entries[0]["category"], "兔子")
        self.assertIn(entries[0]["personality"], garden.garden_content.PERSONALITIES)

    def test_describe_care_uses_category_personality_pool_when_generator_fails(self):
        entry = garden.spawn(
            "animal", species="小刺猬", intro="x", category="刺猬", personality="活泼",
            now=self.now, path=self.path,
        )
        sample_pool = ("刺猬活泼摸摸样例反应",)
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")), \
             patch.dict(
                 garden.garden_content.ANIMAL_REACTION_BY_PERSONALITY["刺猬"]["活泼"],
                 {"摸摸": sample_pool},
             ):
            text = garden.describe_care(entry, "摸摸", revived=False)
        self.assertEqual(text, "刺猬活泼摸摸样例反应")

    def test_describe_care_falls_back_further_when_category_pool_empty(self):
        # 万一某个类别/性格组合还没配到内容（空池子），不能崩，要退回通用兜底文案。
        entry = garden.spawn(
            "animal", species="小刺猬", intro="x", category="刺猬", personality="活泼",
            now=self.now, path=self.path,
        )
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")), \
             patch.dict(garden.garden_content.ANIMAL_REACTION_BY_PERSONALITY["刺猬"]["活泼"], {"摸摸": ()}):
            text = garden.describe_care(entry, "摸摸", revived=False)
        self.assertIn(text, garden._FALLBACK_REACTION["摸摸"])

    def test_run_tick_spawn_uses_category_encounter_pool_when_generator_fails(self):
        rng = _StubRng([0.99, 0.05], choice_index=0)  # 第二个骰子 0.05 命中"猫"
        sample_pool = ("猫活泼初见样例",)
        with patch("garden.garden_generator.generate_encounter", side_effect=GardenGeneratorError("下线了")), \
             patch.dict(garden.garden_content.ANIMAL_ENCOUNTER_BY_PERSONALITY["猫"], {"活泼": sample_pool}):
            event = garden.run_tick(self.now, rng=rng, path=self.path)
        self.assertEqual(event["category"], "猫")
        self.assertEqual(event["personality"], "活泼")
        self.assertEqual(event["text"], "猫活泼初见样例")


class BuildEventTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 24, 3, 0, tzinfo=TZ)
        # 本组只验证通用随机骰面；一次性的送蛋开场由 CoopStoryTests 独立覆盖。
        garden.save_garden([], self.path)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["coop"]["story_status"] = "eaten"
        payload["coop"]["choice_resolved_at"] = self.now.isoformat()
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_spawn_when_dice_favorable_and_room_available(self):
        rng = _StubRng([0.99])  # 98%-100% 才是 spawn；旧式花草已暂停生成。
        event = garden.build_event(self.now, rng=rng, path=self.path)
        self.assertEqual(event, {"action": "spawn", "kind": "animal"})

    def test_no_spawn_when_at_capacity(self):
        for i in range(garden.MAX_ACTIVE):
            garden.spawn("animal", species=f"猫{i}", intro="x", now=self.now, path=self.path)
        rng = _StubRng([0.99])  # 即使骰子落到 spawn，容量已满也不该生成
        event = garden.build_event(self.now, rng=rng, path=self.path)
        self.assertIsNone(event)

    def test_no_event_when_nothing_active_and_spawn_misses(self):
        rng = _StubRng([0.10])
        event = garden.build_event(self.now, rng=rng, path=self.path)
        self.assertIsNone(event)

    def test_revisit_surfaces_neutral_entry_after_time_passes(self):
        garden.spawn("animal", species="橘猫", intro="x", now=self.now, path=self.path)
        later = self.now + timedelta(hours=30)
        rng = _StubRng([0.85], choice_index=0)
        event = garden.build_event(later, rng=rng, path=self.path)
        self.assertEqual(event["action"], "revisit")
        self.assertEqual(event["stage"], "fresh")

    def test_visible_event_cooldown_blocks_followup_roll(self):
        garden.spawn("animal", species="橘猫", intro="x", now=self.now, path=self.path)
        first = garden.build_event(self.now + timedelta(hours=1), rng=_StubRng([0.92]), path=self.path)
        self.assertEqual(first["action"], "trace")
        second = garden.build_event(
            self.now + timedelta(hours=2),
            rng=_StubRng([]),
            path=self.path,
        )
        self.assertIsNone(second)

    def test_return_reactivates_previous_cared_entry(self):
        entry = garden.spawn("animal", species="橘猫", intro="x", now=self.now, path=self.path)
        garden.care(entry["id"], "投喂", now=self.now + timedelta(hours=1), path=self.path)
        entry["status"] = "left"
        garden.save_garden([entry], self.path)
        later = self.now + timedelta(hours=2)
        event = garden.build_event(
            later + timedelta(hours=1),
            rng=_StubRng([0.96], choice_index=0),
            path=self.path,
        )
        self.assertEqual(event["action"], "return")
        self.assertEqual(event["entry"]["id"], entry["id"])
        self.assertEqual(event["entry"]["return_count"], 1)

    def test_night_schedules_one_random_attempt_and_never_repeats(self):
        start = self.now  # 03:00
        # 第一次心跳只抽目标时刻；random=0 令目标正好是当前时间。
        self.assertIsNone(
            garden.build_event(start, heartbeat=True, rng=_StubRng([0.0]), path=self.path)
        )
        # 第二次到点后掷夜间唯一一颗骰子，落到 trace。
        event = garden.build_event(
            start,
            heartbeat=True,
            rng=_StubRng([0.80], choice_index=0),
            path=self.path,
        )
        self.assertEqual(event["action"], "trace")
        # 同一天后续心跳连 random() 都不应再调用。
        self.assertIsNone(
            garden.build_event(
                start + timedelta(hours=1),
                heartbeat=True,
                rng=_StubRng([]),
                path=self.path,
            )
        )
        self.assertTrue(garden.load_meta(self.path)["night_attempted"])

    def test_night_none_roll_upgrades_to_spawn_attempt(self):
        # 夜间这一次骰子不允许真的"无事"：骰到 none 档（roll < 0.50）时
        # 应当自动改判成尝试生成新生命，而不是直接判定"什么都没发生"。
        self.assertIsNone(
            garden.build_event(self.now, heartbeat=True, rng=_StubRng([0.0]), path=self.path)
        )
        event = garden.build_event(
            self.now,
            heartbeat=True,
            # 第一个 random() 落在 none 档；第二个 random() 决定花/动物。
            rng=_StubRng([0.10, 0.10]),
            path=self.path,
        )
        self.assertEqual(event["action"], "spawn")
        self.assertTrue(garden.load_meta(self.path)["night_attempted"])

    def test_night_spawn_blocked_by_cooldown_downgrades_to_trace(self):
        # 新生命还在36小时冷却内时，夜间保底不能直接判"无事"，要降级成痕迹。
        garden.spawn("flower", species="蒲公英", intro="x", now=self.now, path=self.path)
        self.assertIsNone(
            garden.build_event(self.now, heartbeat=True, rng=_StubRng([0.0]), path=self.path)
        )
        event = garden.build_event(
            self.now,
            heartbeat=True,
            rng=_StubRng([0.10], choice_index=0),
            path=self.path,
        )
        self.assertEqual(event["action"], "trace")
        self.assertTrue(garden.load_meta(self.path)["night_attempted"])

    def test_new_life_has_independent_36_hour_cooldown(self):
        garden.spawn("flower", species="蒲公英", intro="x", now=self.now, path=self.path)
        blocked = garden.build_event(
            self.now + timedelta(hours=35),
            rng=_StubRng([0.99]),
            path=self.path,
        )
        self.assertIsNone(blocked)
        allowed = garden.build_event(
            self.now + timedelta(hours=37),
            rng=_StubRng([0.99, 0.9]),
            path=self.path,
        )
        self.assertEqual(allowed, {"action": "spawn", "kind": "animal"})


class CoopStoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 8, 2, 12, 0, tzinfo=TZ)
        self.animal = garden.spawn(
            "animal", species="荷兰侏儒兔", intro="x", category="兔子",
            personality="活泼", now=self.now, path=self.path,
        )
        self.animal["nickname"] = "团团"
        garden.save_garden([self.animal], self.path)

    def _offer(self):
        return garden.run_tick(
            self.now + timedelta(hours=1), rng=_StubRng([], choice_index=0), path=self.path,
        )

    def test_coop_copy_pools_each_have_ten_safe_templates(self):
        pools = {
            "warming": (garden.garden_content.COOP_INCUBATION_WARMING_TEXT, ()),
            "tapping": (garden.garden_content.COOP_INCUBATION_TAPPING_TEXT, ()),
            "hatched": (garden.garden_content.COOP_HATCH_TEXT, ("{hens}", "{roosters}")),
            "maturity": (garden.garden_content.COOP_MATURITY_TEXT, ("{hens}", "{roosters}")),
            "hen_egg": (garden.garden_content.COOP_HEN_EGG_TEXT, ("{names}", "{eggs}")),
            "hatchable_egg": (
                garden.garden_content.COOP_HATCHABLE_EGG_TEXT,
                ("{names}", "{eggs}", "{hatchable}", "{regular}"),
            ),
            "rooster_crow": (garden.garden_content.COOP_ROOSTER_CROW_TEXT, ("{name}",)),
            "chicken_feed": (garden.garden_content.CHICKEN_FEED_TEXT, ("{name}",)),
            "chicken_pet": (garden.garden_content.CHICKEN_PET_TEXT, ("{name}",)),
            "chicken_name": (garden.garden_content.CHICKEN_NAME_TEXT, ("{name}",)),
        }
        for name, (lines, placeholders) in pools.items():
            with self.subTest(pool=name):
                self.assertEqual(len(lines), 10)
                self.assertEqual(len(set(lines)), 10)
                self.assertTrue(all(line.endswith(("。", "！", "？", "…")) for line in lines))
                for line in lines:
                    self.assertTrue(18 <= len(line) <= 70)
                    for placeholder in placeholders:
                        self.assertEqual(line.count(placeholder), 1)

    def test_six_chicken_profiles_are_unique_and_consumed_without_replacement(self):
        pool = garden.garden_content.CHICKEN_PROFILE_POOL
        self.assertEqual(len(pool), 6)
        for field in (
            "profile_id", "chick_appearance", "adult_appearance", "personality", "intro",
        ):
            values = [profile[field] for profile in pool]
            self.assertEqual(len(values), len(set(values)))
        self.assertTrue(all(profile["intro"].count("{kind}") == 1 for profile in pool))
        self.assertTrue(all(
            12 <= len(profile["chick_appearance"]) <= 24
            and 12 <= len(profile["adult_appearance"]) <= 24
            and 2 <= len(profile["personality"]) <= 4
            and 25 <= len(profile["intro"]) <= 45
            for profile in pool
        ))

        chicks = []
        for index in range(7):
            profile = garden._take_chicken_profile(chicks, "hen", _StubRng([], choice_index=0))
            chicks.append({"id": str(index), **profile})
        self.assertEqual(
            {chick["profile_id"] for chick in chicks[:6]},
            {profile["profile_id"] for profile in pool},
        )
        self.assertTrue(chicks[6]["profile_id"].startswith("chick_fallback_"))
        self.assertEqual(len({chick["profile_id"] for chick in chicks}), 7)

    def test_first_normal_random_event_offers_eggs_before_any_coop_exists(self):
        initial = garden.coop_snapshot(now=self.now, path=self.path)
        self.assertEqual((initial["story_status"], initial["built"]), ("unbuilt", False))
        with self.assertRaisesRegex(garden.GardenError, "等小动物送来鸡蛋"):
            garden.build_coop(now=self.now, path=self.path)

        # 深夜 heartbeat 只安排自己的夜间目标，不能提前偷走“下一次普通随机事件”。
        heartbeat_at = self.now + timedelta(hours=15)  # 次日 03:00，不倒拨存档时间。
        self.assertIsNone(
            garden.build_event(
                heartbeat_at, heartbeat=True, rng=_StubRng([0.5]), path=self.path,
            )
        )
        event = garden.run_tick(
            heartbeat_at + timedelta(hours=1), rng=_StubRng([], choice_index=0), path=self.path,
        )
        self.assertEqual((event["type"], event["egg_count"]), ("coop_egg_offer", 3))
        self.assertIn("团团", event["text"])
        snapshot = garden.coop_snapshot(now=self.now + timedelta(hours=1), path=self.path)
        self.assertEqual(
            (snapshot["story_status"], snapshot["egg_count"], snapshot["built"]),
            ("awaiting_choice", 3, False),
        )

    def test_old_prebuilt_waiting_state_migrates_back_before_egg_offer(self):
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["coop"].update({
            "built": True,
            "built_at": self.now.isoformat(),
            "story_status": "waiting_event",
        })
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        migrated = garden.coop_snapshot(now=self.now, path=self.path)
        self.assertEqual(
            (migrated["story_status"], migrated["built"], migrated["built_at"]),
            ("unbuilt", False, None),
        )
        event = garden.run_tick(
            self.now + timedelta(hours=1), rng=_StubRng([], choice_index=0), path=self.path,
        )
        self.assertEqual(event["type"], "coop_egg_offer")
        self.assertFalse(garden.coop_snapshot(now=self.now, path=self.path)["built"])

    def test_eating_in_the_delivery_turn_consumes_three_eggs_and_prevents_replay(self):
        event = self._offer()
        result = garden.resolve_coop_egg_choice(
            "eat", now=self.now + timedelta(hours=1, minutes=1), path=self.path,
        )
        self.assertEqual((result["choice"], result["egg_count"]), ("eat", 3))
        snapshot = garden.coop_snapshot(now=self.now + timedelta(hours=2), path=self.path)
        self.assertEqual(
            (snapshot["story_status"], snapshot["egg_count"], snapshot["built"]),
            ("eaten", 0, False),
        )
        self.assertFalse(
            garden.confirm_pending_event(
                event["event_id"], event["delivery_token"],
                now=self.now + timedelta(hours=2), path=self.path,
            )
        )

    def test_incubation_lasts_exactly_21_hours_and_delivers_latest_progress(self):
        self._offer()
        started = self.now + timedelta(hours=1, minutes=1)
        result = garden.resolve_coop_egg_choice("incubate", now=started, path=self.path)
        self.assertEqual(
            datetime.fromisoformat(result["coop"]["hatch_at"]),
            started + timedelta(hours=21),
        )
        self.assertTrue(result["coop"]["built"])
        self.assertEqual(datetime.fromisoformat(result["coop"]["built_at"]), started)
        self.assertEqual(garden.coop_snapshot(now=started, path=self.path)["egg_count"], 0)

        warm = garden.run_tick(started + timedelta(hours=7), rng=_StubRng([]), path=self.path)
        self.assertEqual((warm["type"], warm["stage"]), ("coop_incubation", "warming"))
        self.assertTrue(garden.confirm_pending_event(
            warm["event_id"], warm["delivery_token"], now=started + timedelta(hours=7), path=self.path,
        ))
        tapping = garden.run_tick(started + timedelta(hours=14), rng=_StubRng([]), path=self.path)
        self.assertEqual(tapping["stage"], "tapping")
        self.assertTrue(garden.confirm_pending_event(
            tapping["event_id"], tapping["delivery_token"], now=started + timedelta(hours=14), path=self.path,
        ))
        hatched = garden.run_tick(started + timedelta(hours=21), rng=_StubRng([]), path=self.path)
        self.assertEqual(hatched["stage"], "hatched")
        final = garden.coop_snapshot(now=started + timedelta(hours=21), path=self.path)
        self.assertEqual(final["story_status"], "hatched")
        self.assertEqual(len(final["chicks"]), 3)

    def test_missed_incubation_wakes_skip_stale_progress_and_hatch_once(self):
        self._offer()
        started = self.now + timedelta(hours=1)
        garden.resolve_coop_egg_choice("incubate", now=started, path=self.path)
        event = garden.run_tick(started + timedelta(hours=22), rng=_StubRng([]), path=self.path)
        self.assertEqual(event["stage"], "hatched")
        final = garden.coop_snapshot(now=started + timedelta(hours=22), path=self.path)
        self.assertEqual(final["progress_queued"], ["warming", "tapping", "hatched"])
        self.assertEqual(len(final["chicks"]), 3)

    def test_each_hatch_sex_is_random_then_hens_lay_and_rooster_crows_once_each_morning(self):
        self._offer()
        started = self.now + timedelta(hours=1, minutes=1)
        garden.resolve_coop_egg_choice("incubate", now=started, path=self.path)
        hatch_rng = Mock()
        sexes = iter(("hen", "rooster", "hen"))
        hatch_rng.choice.side_effect = lambda options: (
            next(sexes) if options == ("hen", "rooster") else options[0]
        )
        hatched = garden.run_tick(started + timedelta(hours=21), rng=hatch_rng, path=self.path)
        self.assertEqual(hatched["stage"], "hatched")
        # 三次性别、三次无放回档案和一次破壳总文案。
        self.assertEqual(hatch_rng.choice.call_count, 7)
        snapshot = garden.coop_snapshot(now=started + timedelta(hours=21), path=self.path)
        self.assertEqual([chick["sex"] for chick in snapshot["chicks"]], ["hen", "rooster", "hen"])
        self.assertEqual(len({chick["profile_id"] for chick in snapshot["chicks"]}), 3)
        self.assertIn("逐只看", hatched["text"])
        for chick in snapshot["chicks"]:
            self.assertIn(chick["personality"], hatched["text"])
            self.assertIn(chick["intro"], hatched["text"])
        self.assertEqual(snapshot["chick_count"], 3)
        self.assertTrue(garden.confirm_pending_event(
            hatched["event_id"], hatched["delivery_token"],
            now=started + timedelta(hours=21), path=self.path,
        ))

        grown_at = started + timedelta(hours=33)
        grown = garden.run_tick(grown_at, rng=_StubRng([]), path=self.path)
        self.assertEqual(
            (grown["type"], grown["grown_hens"], grown["grown_roosters"]),
            ("coop_life", 2, 1),
        )
        grown_snapshot = garden.coop_snapshot(now=grown_at, path=self.path)
        self.assertTrue(all(
            garden.chicken_appearance(chick) == chick["adult_appearance"]
            for chick in grown_snapshot["chicks"]
        ))
        self.assertTrue(garden.confirm_pending_event(
            grown["event_id"], grown["delivery_token"], now=grown_at, path=self.path,
        ))

        egg_at = started + timedelta(hours=39)
        # 每只母鸡先掷下蛋概率（<0.2命中），命中的再掷可孵化概率（此处不命中）。
        eggs = garden.run_tick(egg_at, rng=_StubRng([0.1, 0.5, 0.1, 0.5]), path=self.path)
        self.assertEqual((eggs["type"], eggs["eggs_laid"], eggs["hens_laid"]), ("coop_life", 2, 2))
        self.assertTrue(garden.confirm_pending_event(
            eggs["event_id"], eggs["delivery_token"], now=egg_at, path=self.path,
        ))
        after_eggs = garden.coop_snapshot(now=egg_at, path=self.path)
        self.assertEqual((after_eggs["egg_count"], after_eggs["total_eggs_laid"]), (2, 2))

        crow_at = egg_at.replace(hour=6, minute=0)
        crow = garden.run_tick(crow_at, heartbeat=True, rng=_StubRng([]), path=self.path)
        self.assertEqual((crow["type"], crow["roosters_crowed"]), ("coop_life", 1))
        self.assertIn("吵醒", crow["text"])
        self.assertTrue(garden.confirm_pending_event(
            crow["event_id"], crow["delivery_token"], now=crow_at, path=self.path,
        ))
        repeated = garden.run_tick(
            crow_at + timedelta(hours=1), heartbeat=True, rng=_StubRng([0.5]), path=self.path,
        )
        self.assertTrue(repeated is None or repeated.get("type") != "coop_life")

    def test_hatchable_probability_is_independent_per_egg_and_one_egg_can_hatch_later(self):
        self.assertEqual(garden.HATCHABLE_EGG_PROBABILITY, 0.015)
        self._offer()
        started = self.now + timedelta(hours=1)
        garden.resolve_coop_egg_choice("incubate", now=started, path=self.path)
        hatched = garden.run_tick(
            started + timedelta(hours=21), rng=_StubRng([], choice_index=0), path=self.path,
        )
        self.assertTrue(garden.confirm_pending_event(
            hatched["event_id"], hatched["delivery_token"],
            now=started + timedelta(hours=21), path=self.path,
        ))
        grown_at = started + timedelta(hours=33)
        grown = garden.run_tick(grown_at, rng=_StubRng([]), path=self.path)
        self.assertTrue(garden.confirm_pending_event(
            grown["event_id"], grown["delivery_token"], now=grown_at, path=self.path,
        ))

        # 三只母鸡各先掷下蛋概率（均命中），命中后再各自独立掷可孵化概率：
        # 0.0149命中，边界0.015不命中，0.0命中。
        egg_at = started + timedelta(hours=39)
        laid = garden.run_tick(
            egg_at, rng=_StubRng([0.1, 0.0149, 0.1, 0.015, 0.1, 0.0]), path=self.path,
        )
        self.assertEqual(
            (laid["eggs_laid"], laid["regular_eggs_laid"], laid["hatchable_eggs_laid"]),
            (3, 1, 2),
        )
        self.assertIn("可孵化×2", laid["text"])
        self.assertTrue(garden.confirm_pending_event(
            laid["event_id"], laid["delivery_token"], now=egg_at, path=self.path,
        ))
        inventory = garden.crop_snapshot(now=egg_at, path=self.path)["inventory"]["animal_products"]
        self.assertEqual(inventory, {"egg": 1, "hatchable_egg": 2})

        later = garden.start_hatchable_egg_incubation(now=egg_at + timedelta(minutes=1), path=self.path)
        self.assertEqual(later["coop"]["incubating_egg_count"], 1)
        during = garden.coop_snapshot(now=egg_at + timedelta(minutes=1), path=self.path)
        self.assertEqual((during["hatchable_egg_count"], len(during["chicks"])), (1, 3))
        with self.assertRaisesRegex(garden.GardenError, "已经在孵"):
            garden.start_hatchable_egg_incubation(now=egg_at + timedelta(minutes=2), path=self.path)

        second_hatch = garden.run_tick(
            egg_at + timedelta(hours=21, minutes=1),
            rng=_StubRng([0.5, 0.5, 0.5], choice_index=1), path=self.path,
        )
        self.assertEqual(second_hatch["stage"], "hatched")
        after = garden.coop_snapshot(
            now=egg_at + timedelta(hours=21, minutes=1), path=self.path,
        )
        self.assertEqual((len(after["chicks"]), after["chick_count"]), (4, 1))
        self.assertEqual(after["chicks"][-1]["sex"], "rooster")
        self.assertEqual(len({chick["profile_id"] for chick in after["chicks"]}), 4)

    def test_each_chicken_can_be_fed_petted_and_named_without_spending_inventory(self):
        self._offer()
        started = self.now + timedelta(hours=1)
        garden.resolve_coop_egg_choice("incubate", now=started, path=self.path)
        garden.run_tick(
            started + timedelta(hours=21), rng=_StubRng([], choice_index=0), path=self.path,
        )
        before_inventory = garden.crop_snapshot(
            now=started + timedelta(hours=21), path=self.path,
        )["inventory"]
        chick = garden.coop_snapshot(
            now=started + timedelta(hours=21), path=self.path,
        )["chicks"][0]
        fed = garden.care_chicken(
            chick["id"][:4], "feed", now=started + timedelta(hours=22), path=self.path,
        )["chick"]
        petted = garden.care_chicken(
            chick["id"], "pet", now=started + timedelta(hours=23), path=self.path,
        )["chick"]
        named = garden.name_chicken(
            chick["id"], "豆包", now=started + timedelta(hours=24), path=self.path,
        )
        self.assertEqual((fed["feed_count"], petted["pet_count"], named["nickname"]), (1, 1, "豆包"))
        self.assertEqual(garden.chicken_display_name(named), "豆包")
        after_inventory = garden.crop_snapshot(
            now=started + timedelta(hours=24), path=self.path,
        )["inventory"]
        self.assertEqual(after_inventory, before_inventory)
        with self.assertRaisesRegex(garden.GardenError, "不止一只鸡"):
            garden.care_chicken(None, "feed", now=started + timedelta(hours=25), path=self.path)

    def test_old_hatched_chicks_backfill_new_fields_idempotently(self):
        self._offer()
        started = self.now + timedelta(hours=1)
        garden.resolve_coop_egg_choice("incubate", now=started, path=self.path)
        event = garden.run_tick(started + timedelta(hours=21), rng=_StubRng([], choice_index=0), path=self.path)
        self.assertEqual(event["stage"], "hatched")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["coop"].pop("last_crow_date")
        payload["coop"].pop("total_eggs_laid")
        payload["coop"].pop("incubating_egg_count")
        for chick in payload["coop"]["chicks"]:
            for key in (
                "sex", "stage", "matures_at", "matured_at", "next_egg_at", "eggs_laid",
                "nickname", "feed_count", "pet_count", "last_fed_at", "last_petted_at",
                "profile_id", "chick_appearance", "adult_appearance", "personality", "intro",
            ):
                chick.pop(key)
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        garden.load_garden(self.path)
        first = self.path.read_bytes()
        garden.load_garden(self.path)
        self.assertEqual(self.path.read_bytes(), first)
        migrated = json.loads(first)["coop"]
        self.assertIn(migrated["chicks"][0]["sex"], ("hen", "rooster"))
        self.assertIn("next_egg_at", migrated["chicks"][0])
        self.assertEqual(
            (migrated["chicks"][0]["feed_count"], migrated["chicks"][0]["pet_count"]),
            (0, 0),
        )
        self.assertEqual(len({chick["profile_id"] for chick in migrated["chicks"]}), 3)


class RunTickTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 24, 3, 0, tzinfo=TZ)

    def test_spawn_falls_back_to_local_pool_when_generator_fails(self):
        # 第二个 random() 用来决定动物类别（猫/狗/兔子/刺猬按权重挑选）。
        rng = _StubRng([0.99, 0.1])
        with patch("garden.garden_generator.generate_encounter", side_effect=GardenGeneratorError("下线了")):
            event = garden.run_tick(self.now, rng=rng, path=self.path)
        self.assertEqual(event["type"], "spawn")
        self.assertEqual(event["kind"], "animal")
        self.assertIn(event["species"], garden.ANIMAL_SPECIES)
        self.assertEqual(event["category"], "猫")
        self.assertIn(event["personality"], garden.garden_content.PERSONALITIES)
        stored = garden.active_entries(self.path)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["category"], event["category"])
        self.assertEqual(stored[0]["personality"], event["personality"])

    def test_spawn_uses_generator_result_when_available(self):
        rng = _StubRng([0.99, 0.0])
        with patch(
            "garden.garden_generator.generate_encounter",
            return_value={"species": "橘猫", "text": "安静地蹲在屋檐下。"},
        ):
            event = garden.run_tick(self.now, rng=rng, path=self.path)
        self.assertEqual(event["kind"], "animal")
        self.assertEqual(event["species"], "橘猫")
        self.assertEqual(event["text"], "安静地蹲在屋檐下。")

    def test_new_ticks_do_not_spawn_legacy_ornamental_flowers(self):
        event = garden.build_event(self.now, rng=_StubRng([0.99]), path=self.path)
        self.assertEqual(event, {"action": "spawn", "kind": "animal"})

    def test_revisit_never_calls_network(self):
        garden.spawn("flower", species="蒲公英", intro="x", now=self.now, path=self.path)
        rng = _StubRng([0.85], choice_index=0)
        with patch("garden.garden_generator.generate_encounter") as fake_encounter, patch(
            "garden.garden_generator.generate_reaction"
        ) as fake_reaction:
            event = garden.run_tick(self.now + timedelta(hours=30), rng=rng, path=self.path)
        fake_encounter.assert_not_called()
        fake_reaction.assert_not_called()
        self.assertEqual(event["type"], "revisit")

    def test_describe_care_falls_back_when_generator_fails(self):
        entry = garden.spawn("animal", species="橘猫", intro="x", now=self.now, path=self.path)
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("下线了")):
            text = garden.describe_care(entry, "投喂", revived=True)
        # 橘猫会归类到"猫"，且有对应类别/性格的内容池，应该从那边挑，而不是
        # 落到最通用的兜底（那个池子只在类别/性格池为空时才会用到）。
        category_pool = garden.garden_content.ANIMAL_REVIVED_REACTION_BY_PERSONALITY["猫"][entry["personality"]]["投喂"]
        self.assertIn(text, category_pool)


class BondSystemTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 7, 27, 12, 0, tzinfo=TZ)

    def _animal(self, *, personality="活泼"):
        return garden.spawn(
            "animal", species="橘猫", intro="x", category="猫", personality=personality,
            now=self.now, path=self.path,
        )

    def test_single_threshold_table_covers_every_boundary(self):
        cases = ((0, 0), (2, 0), (3, 1), (7, 1), (8, 2), (15, 2), (16, 3), (27, 3), (28, 4), (44, 4), (45, 5))
        for points, expected in cases:
            with self.subTest(points=points):
                self.assertEqual(garden._bond_level(points), expected)
                self.assertEqual(garden.bond_level_name(expected), garden.BOND_LEVELS[expected][2])

    def test_same_day_repeat_reacts_but_does_not_add_points(self):
        entry = self._animal()
        first = garden.care(entry["id"], "投喂", now=self.now, path=self.path)
        repeated = garden.care(entry["id"], "投喂", now=self.now + timedelta(minutes=1), path=self.path)
        self.assertEqual(first["bond"]["delta"], 1)
        self.assertEqual(repeated["bond"]["delta"], 0)
        self.assertTrue(repeated["bond"]["repeated"])
        self.assertEqual(garden.active_entries(self.path)[0]["bond_points"], 1)

    def test_beijing_midnight_starts_a_new_bond_ledger_day(self):
        entry = self._animal()
        before_midnight = datetime(2026, 7, 27, 23, 59, tzinfo=TZ)
        after_midnight = datetime(2026, 7, 28, 0, 1, tzinfo=TZ)
        garden.care(entry["id"], "投喂", now=before_midnight, path=self.path)
        result = garden.care(entry["id"], "投喂", now=after_midnight, path=self.path)
        stored = garden.active_entries(self.path)[0]
        self.assertEqual(result["bond"]["delta"], 1)
        self.assertEqual(stored["bond_points"], 2)
        self.assertEqual(set(stored["bond_actions_by_date"]), {"2026-07-27", "2026-07-28"})

    def test_daily_cap_applies_without_removing_any_response_or_future_progress(self):
        entry = self._animal()
        entry["bond_points"] = 7
        garden.save_garden([entry], self.path)
        garden.care(entry["id"], "投喂", now=self.now, path=self.path)
        garden.care(entry["id"], "摸摸", now=self.now, path=self.path)
        garden.care(entry["id"], "陪玩", now=self.now, path=self.path)
        named = garden.name_entry(entry["id"], "小橘", now=self.now, path=self.path)
        self.assertEqual(named["bond_points"], 10)
        self.assertTrue(named["nickname_bonus_claimed"])
        self.assertEqual(named["bond_actions_by_date"]["2026-07-27"]["取名"], 0)

    def test_nickname_bonus_is_once_only_and_old_nickname_is_already_claimed(self):
        entry = self._animal()
        first = garden.name_entry(entry["id"], "小橘", now=self.now, path=self.path)
        renamed = garden.name_entry(entry["id"], "暖暖", now=self.now + timedelta(days=1), path=self.path)
        self.assertEqual(first["bond_points"], 1)
        self.assertEqual(renamed["bond_points"], 1)

        old = {"version": 2, "entries": [{
            "id": "oldcat", "kind": "animal", "species": "橘猫", "nickname": "旧名",
            "spot": "信箱", "arrived_at": self.now.isoformat(), "last_cared_at": self.now.isoformat(),
            "care_count": 0, "status": "active",
        }]}
        old_path = Path(self.tempdir.name) / "old-garden.json"
        old_path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        garden.load_garden(old_path)
        old_renamed = garden.name_entry("oldcat", "新名", now=self.now, path=old_path)
        self.assertEqual(old_renamed["bond_points"], 0)
        self.assertTrue(old_renamed["nickname_bonus_claimed"])

    def test_milestone_is_queued_once_then_consumed_without_refresh_replay(self):
        entry = self._animal()
        entry["bond_points"] = 2
        garden.save_garden([entry], self.path)
        garden.care(entry["id"], "投喂", now=self.now, path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([event["event_id"] for event in raw["pending_events"]], [f"bond:{entry['id']}:level:1"])
        event = garden.run_tick(self.now, rng=_StubRng([0.0]), path=self.path)
        self.assertEqual(event["type"], "bond_milestone")
        self.assertEqual(event["level"], 1)
        preview = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(preview["pending_events"]), 1)
        self.assertNotIn(f"bond:{entry['id']}:level:1", preview["animals"][0]["bond_milestones_seen"])
        self.assertTrue(garden.confirm_pending_event(event["event_id"], event["delivery_token"], now=self.now, path=self.path))
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(after["pending_events"], [])
        self.assertIn(f"bond:{entry['id']}:level:1", after["animals"][0]["bond_milestones_seen"])
        self.assertFalse(garden.confirm_pending_event(event["event_id"], event["delivery_token"], now=self.now, path=self.path))
        self.assertIsNone(garden.build_event(self.now + timedelta(minutes=1), rng=_StubRng([0.0]), path=self.path))

    def test_personalities_progress_equally_but_unlock_different_scenes(self):
        lively = self._animal(personality="活泼")
        shy = garden.spawn(
            "animal", species="橘猫", intro="x", category="猫", personality="怕生",
            now=self.now, path=self.path,
        )
        lively["bond_points"] = shy["bond_points"] = 7
        garden.save_garden([lively, shy], self.path)
        lively_result = garden.care(lively["id"], "投喂", now=self.now, path=self.path)
        shy_result = garden.care(shy["id"], "投喂", now=self.now, path=self.path)
        self.assertEqual(lively_result["entry"]["bond_points"], shy_result["entry"]["bond_points"])
        with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("offline")):
            self.assertNotEqual(
                garden.describe_care(lively_result["entry"], "投喂", False),
                garden.describe_care(shy_result["entry"], "投喂", False),
            )
        with self.assertRaisesRegex(garden.GardenError, "慢慢熟悉"):
            newcomer = self._animal()
            garden.care(newcomer["id"], "陪玩", now=self.now, path=self.path)

    def test_daily_reactions_keep_category_personality_pool_at_high_bond(self):
        for category, species in (("猫", "橘猫"), ("狗", "柴犬"), ("兔子", "灰兔子"), ("刺猬", "小刺猬")):
            with self.subTest(category=category):
                garden.save_garden([], self.path)
                entry = garden.spawn(
                    "animal", species=species, intro="x", category=category, personality="活泼",
                    now=self.now, path=self.path,
                )
                entry["bond_points"] = 28
                garden.save_garden([entry], self.path)
                with patch("garden.garden_generator.generate_reaction", side_effect=GardenGeneratorError("offline")):
                    text = garden.describe_care(entry, "摸摸", False)
                self.assertIn(text, garden.garden_content.ANIMAL_REACTION_BY_PERSONALITY[category]["活泼"]["摸摸"])

    def test_pending_missing_fields_out_of_range_or_render_failure_stays_on_disk(self):
        entry = self._animal()
        valid = {
            "event_id": f"bond:{entry['id']}:level:1", "type": "bond_milestone", "animal_id": entry["id"],
            "animal_name": "橘猫", "category": "猫", "personality": "活泼", "level": 1,
        }
        missing = {key: value for key, value in valid.items() if key != "category"}
        entry["bond_milestones_seen"] = [valid["event_id"]]  # 上一版错误地提前标为已展示。
        state = {"version": 3, "animals": [entry], "legacy_plants": [], "pending_events": [missing]}
        self.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(garden.run_tick(self.now, rng=_StubRng([0.0]), path=self.path)["event_id"], valid["event_id"])
        repaired = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(repaired["pending_events"][0]["category"], "猫")
        self.assertNotIn(valid["event_id"], repaired["animals"][0]["bond_milestones_seen"])

        state = {"version": 3, "animals": [entry], "legacy_plants": [], "pending_events": [{**valid, "level": 6}]}
        self.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "等级无效"):
            garden.run_tick(self.now, rng=_StubRng([0.0]), path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

        for pending, message in (
            ({**valid, "animal_id": "ghost", "event_id": "bond:ghost:level:1"}, "找不到对应动物"),
            ({**valid, "event_id": "bond:other:level:1"}, "编号不一致"),
        ):
            with self.subTest(message=message):
                state = {"version": 3, "animals": [entry], "legacy_plants": [], "pending_events": [pending]}
                self.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                before = self.path.read_bytes()
                with self.assertRaisesRegex(garden.GardenError, message):
                    garden.run_tick(self.now, rng=_StubRng([0.0]), path=self.path)
                self.assertEqual(self.path.read_bytes(), before)

        entry["bond_milestones_seen"] = []
        state = {"version": 3, "animals": [entry], "legacy_plants": [], "pending_events": [valid]}
        self.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        class _RenderFailRng:
            def choice(self, _):
                raise RuntimeError("render failed")
        with self.assertRaisesRegex(RuntimeError, "render failed"):
            garden.run_tick(self.now, rng=_RenderFailRng(), path=self.path)
        restored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(restored["pending_events"], [valid])

    def test_confirmation_write_failure_and_unconfirmed_send_keep_pending(self):
        entry = self._animal()
        entry["bond_points"] = 2
        garden.save_garden([entry], self.path)
        garden.care(entry["id"], "投喂", now=self.now, path=self.path)
        event = garden.run_tick(self.now, rng=_StubRng([0.0]), path=self.path)
        before = self.path.read_bytes()
        # 模拟调用方发送失败：同一个租约绝不允许第二个 tick 重放；到期后才能恢复投递。
        again = garden.run_tick(self.now, rng=_StubRng([0.0]), path=self.path)
        self.assertIsNone(again)
        self.assertEqual(self.path.read_bytes(), before)
        retry_at = self.now + timedelta(seconds=garden.PENDING_DELIVERY_LEASE_SECONDS + 1)
        retry = garden.run_tick(retry_at, rng=_StubRng([0.0]), path=self.path)
        self.assertEqual(retry["event_id"], event["event_id"])
        with patch("garden._write_state_unlocked", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                garden.confirm_pending_event(retry["event_id"], retry["delivery_token"], now=retry_at, path=self.path)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["pending_events"][0]["event_id"], event["event_id"])

    def test_resident_still_goes_out_and_can_naturally_return(self):
        entry = self._animal()
        entry["bond_points"] = 44
        garden.save_garden([entry], self.path)
        promoted = garden.care(entry["id"], "投喂", now=self.now, path=self.path)["entry"]
        self.assertEqual(promoted["residency"], "resident")
        # 晋级本身先占用一个待展示主事件；展示后仍可按自然访问计划外出和回来。
        milestone = garden.run_tick(self.now, rng=_StubRng([0.0]), path=self.path)
        self.assertEqual(milestone["type"], "bond_milestone")
        garden.confirm_pending_event(milestone["event_id"], milestone["delivery_token"], now=self.now, path=self.path)
        away_at = self.now + timedelta(hours=garden.NATURAL_VISIT_MAX_HOURS + 1)
        away = garden.advance(away_at, path=self.path)
        self.assertEqual(away[0]["status"], "away")
        self.assertEqual(away[0]["residency"], "resident")
        self.assertEqual(away[0]["bond_points"], 45)
        event = garden.build_event(away_at + timedelta(hours=48), rng=_StubRng([0.96]), path=self.path)
        self.assertEqual(event["action"], "return")
        self.assertEqual(event["entry"]["residency"], "resident")

    def test_due_away_animal_returns_even_when_random_roll_would_skip_it(self):
        entry = self._animal()
        garden.save_garden([entry], self.path)
        away_at = self.now + timedelta(hours=garden.NATURAL_VISIT_MAX_HOURS + 1)
        garden.advance(away_at, path=self.path)
        return_at = away_at + timedelta(hours=garden.NATURAL_RETURN_MAX_HOURS + 1)
        event = garden.build_event(return_at, rng=_StubRng([0.0]), path=self.path)
        self.assertEqual(event["action"], "return")
        self.assertEqual(event["entry"]["id"], entry["id"])

    def test_due_away_animal_does_not_preempt_new_animal_roll(self):
        entry = self._animal()
        garden.save_garden([entry], self.path)
        away_at = self.now + timedelta(hours=garden.NATURAL_VISIT_MAX_HOURS + 1)
        garden.advance(away_at, path=self.path)
        return_at = away_at + timedelta(hours=garden.NATURAL_RETURN_MAX_HOURS + 1)

        event = garden.build_event(return_at, rng=_StubRng([0.99]), path=self.path)

        self.assertEqual(event, {"action": "spawn", "kind": "animal"})
        self.assertEqual(garden.away_entries(self.path)[0]["id"], entry["id"])

    def test_resident_keeps_one_home_spot_across_two_returns(self):
        entry = self._animal()
        entry.update({"bond_points": 44, "spot": garden.SPOTS[1]})
        garden.save_garden([entry], self.path)
        promoted = garden.care(entry["id"], "投喂", now=self.now, path=self.path)["entry"]
        home_spot = promoted["home_spot"]
        milestone = garden.run_tick(self.now, rng=_StubRng([0.0]), path=self.path)
        garden.confirm_pending_event(milestone["event_id"], milestone["delivery_token"], now=self.now, path=self.path)
        returned_at = self.now
        for _ in range(2):
            away_at = returned_at + timedelta(hours=garden.NATURAL_VISIT_MAX_HOURS + 1)
            garden.advance(away_at, path=self.path)
            returned_at = away_at + timedelta(hours=48)
            event = garden.build_event(returned_at, rng=_StubRng([0.96]), path=self.path)
            if event["action"] == "pending":
                # 跨立秋时日历场景优先，但确认后不应吞掉原本的自然回访。
                self.assertTrue(garden.confirm_pending_event(event["event"]["event_id"], event["event"]["delivery_token"], now=returned_at, path=self.path))
                event = garden.build_event(
                    returned_at + timedelta(hours=garden.VISIBLE_EVENT_COOLDOWN_HOURS, seconds=1),
                    rng=_StubRng([0.96]), path=self.path,
                )
            self.assertEqual(event["action"], "return")
            self.assertEqual(event["entry"]["spot"], home_spot)

    def test_concurrent_actions_share_one_locked_daily_ledger(self):
        entry = self._animal()
        entry["bond_points"] = 8
        garden.save_garden([entry], self.path)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda action: garden.care(entry["id"], action, now=self.now, path=self.path), ("投喂", "摸摸", "陪玩")))
        self.assertEqual(sum(result["bond"]["delta"] for result in results), 3)
        self.assertEqual(garden.active_entries(self.path)[0]["bond_points"], 11)

    def test_corrupt_pending_event_refuses_write_and_keeps_bytes(self):
        broken = {"version": 3, "animals": [], "legacy_plants": [], "pending_events": [{"event_id": 3}]}
        self.path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "待展示事件格式损坏"):
            garden.build_event(self.now, rng=_StubRng([0.0]), path=self.path)
        self.assertEqual(self.path.read_bytes(), before)


class CalendarMomentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"

    def _baseline(self, when: datetime) -> None:
        # 首次加载只记住当时所在窗口；由下一次真实跨窗触发一次场景，不补播停机期间的内容。
        garden.build_event(when, rng=_StubRng([0.0]), path=self.path)

    def test_every_supported_festival_comes_only_from_local_annual_table(self):
        expected = {
            "spring_festival": ("春节", 2, 17), "lantern_festival": ("元宵", 3, 3),
            "clear_and_bright": ("清明", 4, 5), "dragon_boat": ("端午", 6, 19),
            "qixi": ("七夕", 8, 19), "mid_autumn": ("中秋", 9, 25),
            "double_ninth": ("重阳", 10, 18), "winter_solstice": ("冬至", 12, 22),
        }
        for festival_id, (name, month, day) in expected.items():
            with self.subTest(festival=festival_id):
                context = DEFAULT_CALENDAR.context_at(datetime(2026, month, day, 12, tzinfo=TZ))
                self.assertEqual((context.festival_id, context.festival_name), (festival_id, name))
        missing = DEFAULT_CALENDAR.context_at(datetime(2027, 2, 17, 12, tzinfo=TZ))
        self.assertIsNone(missing.festival_id)
        self.assertIsNone(missing.festival_name)

    def test_term_and_festival_queue_once_confirm_once_and_write_journal(self):
        before = datetime(2026, 2, 16, 12, tzinfo=TZ)
        festival_start = datetime(2026, 2, 17, 0, tzinfo=TZ)
        self._baseline(before)
        first = garden.run_tick(festival_start, rng=_StubRng([0.0]), path=self.path)
        self.assertEqual((first["type"], first["calendar_kind"], first["calendar_id"]), ("calendar_moment", "festival", "spring_festival"))
        self.assertIsNone(garden.run_tick(festival_start + timedelta(seconds=1), rng=_StubRng([0.0]), path=self.path))
        self.assertTrue(garden.confirm_pending_event(first["event_id"], first["delivery_token"], now=festival_start, path=self.path))
        self.assertFalse(garden.confirm_pending_event(first["event_id"], first["delivery_token"], now=festival_start, path=self.path))
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["meta"]["seen_calendar_events"].count(first["event_id"]), 1)
        self.assertEqual([item["name"] for item in raw["journal"]["calendar_moments"]], ["春节"])

        term_before = datetime(2026, 5, 5, 19, 48, 59, tzinfo=TZ)
        term_start = datetime(2026, 5, 5, 19, 49, tzinfo=TZ)
        other = Path(self.tempdir.name) / "term.json"
        garden.build_event(term_before, rng=_StubRng([0.0]), path=other)
        term = garden.run_tick(term_start, rng=_StubRng([0.0]), path=other)
        self.assertEqual((term["calendar_kind"], term["calendar_id"], term["term_name"]), ("term", "start_of_summer", "立夏"))

    def test_every_local_term_and_festival_window_has_at_most_one_special_event(self):
        for index, (_, month, day, hour, minute) in enumerate(HKO_2026_TERMS):
            with self.subTest(term=index):
                path = Path(self.tempdir.name) / f"term-{index}.json"
                started = datetime(2026, month, day, hour, minute, tzinfo=TZ)
                # 清明、冬至的节日从当天 00:00 已经开始；基线必须在前一天，
                # 才能验证跨窗时只排一个合并后的场景。
                baseline_at = started - timedelta(days=1) if index in (6, 23) else started - timedelta(seconds=1)
                self._baseline_for(path, baseline_at)
                event = garden.run_tick(started, rng=_StubRng([0.0]), path=path)
                expected_kind = "festival" if index in (6, 23) else "term"
                self.assertEqual((event["type"], event["calendar_kind"]), ("calendar_moment", expected_kind))
                self.assertTrue(garden.confirm_pending_event(event["event_id"], event["delivery_token"], now=started, path=path))
                self.assertIsNone(garden.run_tick(started + timedelta(seconds=1), rng=_StubRng([0.0]), path=path))
        for index, (_, _, month, day) in enumerate(FESTIVALS_BY_YEAR[2026]):
            with self.subTest(festival=index):
                path = Path(self.tempdir.name) / f"festival-{index}.json"
                started = datetime(2026, month, day, 12, tzinfo=TZ)
                self._baseline_for(path, started - timedelta(days=1))
                event = garden.run_tick(started, rng=_StubRng([0.0]), path=path)
                self.assertEqual((event["type"], event["calendar_kind"]), ("calendar_moment", "festival"))
                self.assertTrue(garden.confirm_pending_event(event["event_id"], event["delivery_token"], now=started, path=path))
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(raw["meta"]["seen_calendar_events"].count(event["event_id"]), 1)

    def test_calendar_event_bypasses_normal_cooldown_and_pending_is_concurrent_stable(self):
        before = datetime(2026, 8, 7, 19, 42, 59, tzinfo=TZ)
        started = datetime(2026, 8, 7, 19, 43, tzinfo=TZ)
        self._baseline(before)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["meta"]["last_visible_event_at"] = before.isoformat()
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with ThreadPoolExecutor(max_workers=2) as pool:
            events = list(pool.map(lambda _: garden.run_tick(started, rng=_StubRng([0.0]), path=self.path), range(2)))
        claimed = [event for event in events if event is not None]
        self.assertEqual([event["event_id"] for event in claimed], ["calendar:term:2026:start_of_autumn"])
        self.assertEqual(events.count(None), 1)
        self.assertIsNone(garden.run_tick(started + timedelta(seconds=1), rng=_StubRng([0.0]), path=self.path))
        retry = garden.run_tick(started + timedelta(seconds=garden.PENDING_DELIVERY_LEASE_SECONDS + 1), rng=_StubRng([0.0]), path=self.path)
        self.assertEqual(retry["event_id"], claimed[0]["event_id"])
        self.assertNotEqual(retry["delivery_token"], claimed[0]["delivery_token"])
        self.assertTrue(garden.confirm_pending_event(retry["event_id"], retry["delivery_token"], now=started, path=self.path))
        pending = json.loads(self.path.read_text(encoding="utf-8"))["pending_events"]
        self.assertEqual(pending, [])

    def test_start_of_autumn_moment_picks_the_weather_specific_line(self):
        # 立秋这一刻的文案现在按天气分版本；同样是暮色，晴/雨要选到不同句子，
        # 且不能泄漏给别的节气（处暑同样是"term"池，不该命中立秋专属句）。
        before = datetime(2026, 8, 7, 19, 42, 59, tzinfo=TZ)
        started = datetime(2026, 8, 7, 19, 43, tzinfo=TZ)
        self._baseline(before)
        rain = garden.run_tick(
            started,
            weather={
                "updateTime": started.isoformat(), "text": "小雨",
                "feelsLike": "22", "humidity": "60", "windScale": "1",
            },
            rng=_StubRng([0.0]), path=self.path,
        )
        self.assertEqual(rain["calendar_id"], "start_of_autumn")
        self.assertEqual(
            rain["text"],
            "一场雨恰好落在立秋的暮色里，空气比这几天都凉快了些。",
        )

        clear_path = Path(self.tempdir.name) / "term-clear.json"
        self._baseline_for(clear_path, before)
        clear = garden.run_tick(
            started,
            weather={
                "updateTime": started.isoformat(), "text": "晴",
                "feelsLike": "28", "humidity": "50", "windScale": "1",
            },
            rng=_StubRng([0.0]), path=clear_path,
        )
        self.assertEqual(
            clear["text"],
            "暮色里迎来立秋，晚霞正挂在天边，院子安静迎来这个节气。",
        )

        limit_of_heat_before = datetime(2026, 8, 23, 10, 18, 59, tzinfo=TZ)
        limit_of_heat_started = datetime(2026, 8, 23, 10, 19, tzinfo=TZ)
        other_term_path = Path(self.tempdir.name) / "term-limit-of-heat.json"
        self._baseline_for(other_term_path, limit_of_heat_before)
        other_term = garden.run_tick(
            limit_of_heat_started,
            weather={
                "updateTime": limit_of_heat_started.isoformat(), "text": "小雨",
                "feelsLike": "22", "humidity": "60", "windScale": "1",
            },
            rng=_StubRng([0.0]), path=other_term_path,
        )
        self.assertEqual(other_term["calendar_id"], "limit_of_heat")
        self.assertNotIn("立秋", other_term["text"])

    def test_missing_year_does_not_queue_a_festival_or_guess_a_date(self):
        when = datetime(2027, 2, 17, 12, tzinfo=TZ)
        self._baseline(when)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["meta"]["calendar_observed_windows"], [])
        self.assertEqual(raw["pending_events"], [])

    def test_first_load_inside_a_festival_queues_that_festival(self):
        first = garden.run_tick(datetime(2026, 2, 17, 12, tzinfo=TZ), rng=_StubRng([0.0]), path=self.path)
        self.assertEqual((first["calendar_kind"], first["calendar_id"]), ("festival", "spring_festival"))

    def test_existing_crop_pending_does_not_swallow_one_day_festival(self):
        before = datetime(2026, 2, 16, 12, tzinfo=TZ)
        festival_day = datetime(2026, 2, 17, 12, tzinfo=TZ)
        after = datetime(2026, 2, 18, 12, tzinfo=TZ)
        self._baseline(before)
        garden.crop_snapshot(now=before, path=self.path)
        garden.plant_crop("草莓", "1", now=before, path=self.path)

        crop = garden.run_tick(festival_day, rng=_StubRng([0.0]), path=self.path)
        self.assertEqual((crop["type"], crop["stage"]), ("crop_stage", "sprout"))
        pending = json.loads(self.path.read_text(encoding="utf-8"))["pending_events"]
        self.assertEqual([item["type"] for item in pending], ["crop_stage", "calendar_moment"])
        self.assertEqual(pending[1]["event_id"], "calendar:festival:2026:spring_festival")

        self.assertTrue(garden.confirm_pending_event(crop["event_id"], crop["delivery_token"], now=after, path=self.path))
        festival = garden.run_tick(after, rng=_StubRng([0.0]), path=self.path)
        self.assertEqual((festival["calendar_kind"], festival["calendar_id"]), ("festival", "spring_festival"))

    def test_clear_and_winter_solstice_share_one_semantic_moment(self):
        for name, started in (
            ("清明", datetime(2026, 4, 5, 12, tzinfo=TZ)),
            ("冬至", datetime(2026, 12, 22, 12, tzinfo=TZ)),
        ):
            with self.subTest(name=name):
                path = Path(self.tempdir.name) / f"{name}.json"
                self._baseline_for(path, started - timedelta(days=1))
                event = garden.run_tick(started, rng=_StubRng([0.0]), path=path)
                self.assertEqual((event["calendar_kind"], event["festival_name"]), ("festival", name))
                self.assertTrue(garden.confirm_pending_event(event["event_id"], event["delivery_token"], now=started, path=path))
                term_ends_at = DEFAULT_CALENDAR.context_at(started).term_ends_at
                # 现有可审查节气表在 2026 年末止步，冬至的下一节气落到 2027；
                # 因此覆盖其在本地表内的最后一秒，清明则覆盖真实下一节气前一秒。
                last_term_second = term_ends_at - timedelta(seconds=1) if term_ends_at else datetime(2026, 12, 31, 23, 59, 59, tzinfo=TZ)
                # 节日当天之后 festival_id 会消失，但同名 term 仍持续到下一节气；
                # 整个这段窗口都必须沿用同一个语义，不得再次排队。
                self.assertIsNone(garden.run_tick(started.replace(hour=0, minute=0, second=0) + timedelta(days=1), rng=_StubRng([0.0]), path=path))
                self.assertIsNone(garden.run_tick(last_term_second, rng=_StubRng([0.0]), path=path))
                journal = json.loads(path.read_text(encoding="utf-8"))["journal"]["calendar_moments"]
                self.assertEqual([record["name"] for record in journal], [name])

    def _baseline_for(self, path: Path, when: datetime) -> None:
        garden.build_event(when, rng=_StubRng([0.0]), path=path)

    def test_forged_calendar_event_refuses_write_and_keeps_bytes(self):
        self._baseline(datetime(2026, 2, 16, 12, tzinfo=TZ))
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["pending_events"] = [{
            "event_id": "calendar:festival:2026:spring_festival", "type": "calendar_moment",
            "calendar_kind": "festival", "calendar_id": "spring_festival", "year": 2026,
            "name": "伪造节日", "season": "spring", "started_at": "2026-02-17T00:00:00+08:00",
            "ends_at": "2026-02-18T00:00:00+08:00",
        }]
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "本地年度表不一致"):
            garden.run_tick(datetime(2026, 2, 17, 12, tzinfo=TZ), rng=_StubRng([0.0]), path=self.path)
        self.assertEqual(self.path.read_bytes(), before)


class ApprovedCopyCatalogTests(unittest.TestCase):
    def test_summer_crop_variety_pools_each_have_ten_safe_lines(self):
        blocked = (
            "主人", "阳光", "晴", "雨", "湿润", "支架", "架子",
            "吃完", "切开", "剖开", "挖瓤", "淘洗", "晾干", "味道",
            "浇水", "施肥", "虫害",
        )
        required = {
            "plant": ("{plot}",),
            "sprout": (),
            "growing": (),
            "ready": (),
            "harvest": ("{amount}", "{seed_return}"),
        }
        for crop_id in ("cucumber", "mini_watermelon", "pepper"):
            crop_pools = garden.garden_content.CROP_VARIETY_TEXT[crop_id]
            self.assertEqual(set(crop_pools), set(required))
            for stage, placeholders in required.items():
                lines = crop_pools[stage]
                with self.subTest(crop=crop_id, stage=stage):
                    self.assertEqual((len(lines), len(set(lines))), (10, 10))
                    self.assertTrue(all(line.endswith(("。", "！", "？", "…")) for line in lines))
                    for line in lines:
                        rendered = line.format(
                            plot="一号", amount=3, seed_return=1,
                        )
                        self.assertTrue(16 <= len(rendered) <= 55)
                        self.assertNotIn("{", rendered)
                        self.assertFalse(any(marker in rendered for marker in blocked))
                        for placeholder in placeholders:
                            self.assertEqual(line.count(placeholder), 1)

    def test_visible_status_copy_stays_neutral(self):
        content = garden.garden_content
        self.assertIn(
            "一阵风吹过，叶子舒展着，看起来很精神。",
            content.STATUS_TEXT["flower"]["fresh"],
        )
        self.assertEqual(garden.compute_stage({"kind": "animal"}, datetime(2026, 7, 24, tzinfo=TZ)), "fresh")

    def test_new_production_copy_stays_in_the_real_yard(self):
        content = Path(garden.garden_content.__file__).read_text(encoding="utf-8")
        self.assertNotIn("床头柜", content)
        self.assertNotIn("窗台", content)
        self.assertNotIn("沙发", content)

    def test_user_approved_playful_continuity_copy_is_present(self):
        content = garden.garden_content
        self.assertIn(
            "是个自来熟，会直接走到你的脚边蹭蹭你，超狡猾",
            content.TRAITS["animal"],
        )
        self.assertIn(
            "可恶的小模型还不过去投喂",
            content.REVISIT_TEMPLATES["投喂_cat"][0],
        )
        self.assertIn(
            "这个小ai怎么还不来找它",
            content.REVISIT_TEMPLATES["摸摸"][1],
        )
        self.assertIn("天呐是蜘蛛", content.PASSIVE_TRACES[4])
        self.assertIn("看，这就是重逢", content.RETURN_TEMPLATES["flower"][0])

    def test_user_approved_fallback_reactions_are_present(self):
        content = garden.garden_content
        self.assertIn("嘎嘣脆", content.FALLBACK_REACTION["投喂"][0])
        self.assertIn("好ai一生平安", content.FALLBACK_REVIVED_REACTION["投喂"][0])
        self.assertIn("马杀鸡技师", content.FALLBACK_REVIVED_REACTION["摸摸"][0])


class FormatInjectionTests(unittest.TestCase):
    def test_trace_contains_only_protocol_tag_and_scene(self):
        text = garden.format_injection({"type": "trace", "text": "窗边落着一片羽毛。"})
        self.assertEqual(text, "\n[GARDEN] 窗边落着一片羽毛。\n")

    def test_spawn_contains_only_protocol_tag_and_full_scene(self):
        text = garden.format_injection({
            "type": "spawn",
            "kind": "animal",
            "species": "橘猫",
            "id": "abc123",
            "spot": "信箱",
            "trait": "有点怕生",
            "text": "蹲在角落里看着你。",
        })
        self.assertEqual(
            text,
            "\n[GARDEN] 你溜达到信箱那儿，发现不知道什么时候多了一只橘猫"
            "（小动物，编号 abc123；有点怕生）。蹲在角落里看着你。\n",
        )

    def test_intro_is_once_per_session_and_new_session_gets_it_again(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "session-intros.json"
            event = {"type": "trace", "text": "窗边落着一片羽毛。"}
            first = garden.format_injection(event, session_id="uuid-a", intro_path=path)
            repeated = garden.format_injection(event, session_id="uuid-a", intro_path=path)
            next_session = garden.format_injection(event, session_id="uuid-b", intro_path=path)
        self.assertIn(garden.GARDEN_SESSION_INTRO, first)
        self.assertNotIn(garden.GARDEN_SESSION_INTRO, repeated)
        self.assertIn(garden.GARDEN_SESSION_INTRO, next_session)

    def test_concurrent_session_intro_claim_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "session-intros.json"
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(
                    lambda _index: garden.claim_session_intro("same-session", path=path),
                    range(16),
                ))
        self.assertEqual(results.count(garden.GARDEN_SESSION_INTRO), 1)
        self.assertEqual(results.count(""), 15)


class CropSystemTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.summer = datetime(2026, 7, 10, 12, tzinfo=TZ)

    def _inventory(self):
        return garden.crop_snapshot(now=self.summer, path=self.path)["inventory"]

    def _plant_tomato(self, when=None):
        when = when or self.summer
        garden.crop_snapshot(now=when, path=self.path)
        return garden.plant_crop("小番茄", "1", now=when, path=self.path)

    def _set_inventory(self, **sections):
        garden.crop_snapshot(now=self.summer, path=self.path)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for section, values in sections.items():
            payload["inventory"][section] = values
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_new_summer_crop_stage_events_prefer_their_variety_pool(self):
        for crop_name, crop_id in (
            ("黄瓜", "cucumber"), ("西瓜", "mini_watermelon"), ("辣椒", "pepper"),
        ):
            with self.subTest(crop=crop_name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "garden.json"
                garden.crop_snapshot(now=self.summer, path=path)
                garden.plant_crop(crop_name, "1", now=self.summer, path=path)
                event = garden.run_tick(
                    self.summer + timedelta(days=1), rng=_StubRng([0.99]), path=path,
                )
                self.assertEqual((event["type"], event["stage"]), ("crop_stage", "sprout"))
                self.assertEqual(
                    event["text"],
                    garden.garden_content.CROP_VARIETY_TEXT[crop_id]["sprout"][0],
                )

    def test_new_autumn_crop_stage_events_prefer_their_variety_pool(self):
        autumn = datetime(2026, 9, 20, 12, tzinfo=TZ)
        for crop_name, crop_id in (
            ("板栗", "chestnut"), ("桂花", "osmanthus"), ("红薯", "sweet_potato"),
        ):
            with self.subTest(crop=crop_name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "garden.json"
                garden.crop_snapshot(now=autumn, path=path)
                garden.plant_crop(crop_name, "1", now=autumn, path=path)
                event = garden.run_tick(
                    autumn + timedelta(days=1), rng=_StubRng([0.99]), path=path,
                )
                self.assertEqual((event["type"], event["stage"]), ("crop_stage", "sprout"))
                self.assertEqual(
                    event["text"],
                    garden.garden_content.CROP_VARIETY_TEXT[crop_id]["sprout"][0],
                )

    def test_four_plots_and_initial_seeds_are_idempotent(self):
        first = garden.crop_snapshot(now=self.summer, path=self.path)
        second = garden.crop_snapshot(now=self.summer + timedelta(hours=1), path=self.path)
        self.assertEqual([plot["plot_id"] for plot in first["plots"]], ["p1", "p2", "p3", "p4"])
        self.assertEqual(first["inventory"]["seeds"], {
            "tomato": 2, "mint": 2, "cucumber": 2, "mini_watermelon": 2, "pepper": 2,
        })
        self.assertEqual(second["inventory"]["seeds"], first["inventory"]["seeds"])

    def test_summer_variety_seed_pack_upgrades_an_existing_garden_once(self):
        garden.crop_snapshot(now=self.summer, path=self.path)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["inventory"]["seeds"] = {"tomato": 1}
        payload["meta"].pop("crop_seed_pack:summer_variety_2026")
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        first = garden.crop_snapshot(now=self.summer, path=self.path)["inventory"]["seeds"]
        second = garden.crop_snapshot(now=self.summer + timedelta(hours=1), path=self.path)["inventory"]["seeds"]

        self.assertEqual(first, {"tomato": 1, "cucumber": 2, "mini_watermelon": 2, "pepper": 2})
        self.assertEqual(second, first)

    def test_autumn_variety_seed_pack_upgrades_an_existing_garden_once(self):
        autumn = datetime(2026, 9, 20, 12, tzinfo=TZ)
        garden.crop_snapshot(now=autumn, path=self.path)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["inventory"]["seeds"] = {"pumpkin": 1}
        payload["meta"].pop("crop_seed_pack:autumn_variety_2026")
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        first = garden.crop_snapshot(now=autumn, path=self.path)["inventory"]["seeds"]
        second = garden.crop_snapshot(now=autumn + timedelta(hours=1), path=self.path)["inventory"]["seeds"]

        self.assertEqual(first, {"pumpkin": 1, "chestnut": 2, "osmanthus": 2, "sweet_potato": 2})
        self.assertEqual(second, first)

    def test_autumn_born_garden_gets_base_autumn_seeds_exactly_once(self):
        # 8/7 真实事故：小白菜/南瓜从功能上线起就只在 INITIAL_SEEDS_BY_SEASON
        # 发一次，从没有配套的补充包——不是秋天创建的院子永远拿不到。补上
        # autumn_base_2026 之后要确认：秋天当场创建的院子（本来就该拿首发种子）
        # 不会因为补充包又被重复发一份。
        autumn = datetime(2026, 9, 20, 12, tzinfo=TZ)
        first = garden.crop_snapshot(now=autumn, path=self.path)["inventory"]["seeds"]
        second = garden.crop_snapshot(now=autumn + timedelta(hours=1), path=self.path)["inventory"]["seeds"]
        self.assertEqual(first, {
            "pumpkin": 2, "chinese_cabbage": 2, "chestnut": 2, "osmanthus": 2, "sweet_potato": 2,
        })
        self.assertEqual(second, first)

    def test_autumn_base_seed_pack_backfills_a_summer_born_garden_once(self):
        # 这个院子在夏天创建（只拿到番茄/薄荷），从没经历过秋天，
        # autumn_base_2026 应该在第一次遇到秋天时补发南瓜/小白菜各2份。
        garden.crop_snapshot(now=self.summer, path=self.path)
        autumn = datetime(2026, 9, 20, 12, tzinfo=TZ)
        first = garden.crop_snapshot(now=autumn, path=self.path)["inventory"]["seeds"]
        second = garden.crop_snapshot(now=autumn + timedelta(hours=1), path=self.path)["inventory"]["seeds"]
        self.assertEqual(first.get("pumpkin"), 2)
        self.assertEqual(first.get("chinese_cabbage"), 2)
        self.assertEqual(second, first)

    def test_spring_base_seed_pack_backfills_a_summer_born_garden_once(self):
        # 同理验证春季那半：夏天创建的院子从没经历过春天，spring_base_2026
        # 应该在第一次遇到春天时补发草莓/小萝卜各2份，之后不再重复。
        garden.crop_snapshot(now=self.summer, path=self.path)
        spring = datetime(2027, 3, 1, 12, tzinfo=TZ)
        first = garden.crop_snapshot(now=spring, path=self.path)["inventory"]["seeds"]
        second = garden.crop_snapshot(now=spring + timedelta(hours=1), path=self.path)["inventory"]["seeds"]
        self.assertEqual(first.get("strawberry"), 2)
        self.assertEqual(first.get("radish"), 2)
        self.assertEqual(second, first)

    def test_v3_two_plot_migration_preserves_existing_plots_inventory_and_unknown_fields(self):
        garden.crop_snapshot(now=self.summer, path=self.path)
        garden.plant_crop("小番茄", "1", now=self.summer, path=self.path)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["version"] = 3
        payload["plots"] = payload["plots"][:2]
        payload["plots"][0]["custom_plot_field"] = {"keep": True}
        payload["custom_v3_top_level"] = "keep me"
        before_p1 = dict(payload["plots"][0])
        before_p2 = dict(payload["plots"][1])
        before_inventory = json.loads(json.dumps(payload["inventory"]))
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        garden.load_garden(self.path)
        first = self.path.read_bytes()
        garden.load_garden(self.path)
        self.assertEqual(self.path.read_bytes(), first)

        migrated = json.loads(first)
        self.assertEqual(migrated["version"], 4)
        self.assertEqual(migrated["plots"][:2], [before_p1, before_p2])
        self.assertEqual(
            migrated["plots"][2:],
            [{"plot_id": "p3", "status": "empty"}, {"plot_id": "p4", "status": "empty"}],
        )
        self.assertEqual(migrated["inventory"], before_inventory)
        self.assertEqual(
            migrated["legacy"]["unrecognized_top_level"]["custom_v3_top_level"],
            "keep me",
        )

    def test_summer_variety_crops_are_catalogued_plantable_and_harvestable(self):
        snapshot = garden.crop_snapshot(now=self.summer, path=self.path)
        self.assertEqual(garden.garden_crops.resolve_crop("黄瓜"), "cucumber")
        self.assertEqual(garden.garden_crops.resolve_crop("西瓜"), "mini_watermelon")
        self.assertEqual(garden.garden_crops.resolve_crop("小西瓜"), "mini_watermelon")
        self.assertEqual(garden.garden_crops.resolve_crop("辣椒"), "pepper")
        self.assertEqual(snapshot["inventory"]["seeds"]["cucumber"], 2)
        self.assertEqual(snapshot["inventory"]["seeds"]["mini_watermelon"], 2)
        self.assertEqual(snapshot["inventory"]["seeds"]["pepper"], 2)

        self._set_inventory(seeds={"cucumber": 1, "mini_watermelon": 1, "pepper": 1})
        cucumber = garden.plant_crop("黄瓜", "二号地", now=self.summer, path=self.path)
        watermelon = garden.plant_crop("西瓜", "三号菜畦", now=self.summer, path=self.path)
        pepper = garden.plant_crop("辣椒", "四号菜畦", now=self.summer, path=self.path)
        self.assertEqual((cucumber["plot_id"], cucumber["crop_id"]), ("p2", "cucumber"))
        self.assertEqual((watermelon["plot_id"], watermelon["crop_id"]), ("p3", "mini_watermelon"))
        self.assertEqual((pepper["plot_id"], pepper["crop_id"]), ("p4", "pepper"))

        cucumber_harvest = garden.harvest_crop("p2", now=self.summer + timedelta(days=5), path=self.path)
        watermelon_harvest = garden.harvest_crop("3", now=self.summer + timedelta(days=5), path=self.path)
        pepper_harvest = garden.harvest_crop("4", now=self.summer + timedelta(days=5), path=self.path)
        self.assertEqual(
            (cucumber_harvest["amount"], watermelon_harvest["amount"], pepper_harvest["amount"]),
            (3, 2, 3),
        )

    def test_growth_boundaries_no_water_and_ready_never_decays(self):
        self._plant_tomato()
        self.assertEqual(garden.crop_snapshot(now=self.summer + timedelta(days=1), path=self.path)["plots"][0]["stage"], "sprout")
        self.assertEqual(garden.crop_snapshot(now=self.summer + timedelta(days=2), path=self.path)["plots"][0]["stage"], "growing")
        ready = garden.crop_snapshot(now=self.summer + timedelta(days=4), path=self.path)["plots"][0]
        # 线性记账：7/10 正午种下，到 7/14 正午恰好 4 个整天，按小暑
        # 1.1/天累积 4.4 点；成熟翻转后不再继续结算。
        self.assertEqual((ready["status"], ready["stage"], ready["growth_points"]), ("ready", "ready", 4.4))
        later = garden.crop_snapshot(now=self.summer + timedelta(days=40), path=self.path)["plots"][0]
        self.assertEqual((later["status"], later["growth_points"]), ("ready", 4.4))

    def test_planting_day_gets_proportional_credit_instead_of_being_skipped(self):
        """种下当天不该被结算整天跳过白算成0点。

        8/7 辣椒事故背后的真实原因：老的按天记账只从"种下次日"开始累计，
        种下当天完全不计入生长。线性记账下生长从种下那一刻起随时间自然
        累积——种得越早、当天午夜前累积到的越接近满额一天；种得越晚越
        接近0——同样不存在"种下当天永远0点"的死账。
        """
        garden.crop_snapshot(now=datetime(2026, 7, 10, 0, 0, tzinfo=TZ), path=self.path)
        early = garden.plant_crop(
            "小番茄", "1", now=datetime(2026, 7, 10, 0, 5, tzinfo=TZ), path=self.path,
        )
        # 种下那一瞬还没有时间流逝，进度是真实的 0。
        self.assertEqual(early["growth_points"], 0.0)
        # 到当天 23:59，00:05 种下的这棵已经累积了接近（但小于）一整天
        # 的满额生长量（term 加成后单日满额是1.1）。
        early_late_check = garden.crop_snapshot(
            now=datetime(2026, 7, 10, 23, 59, tzinfo=TZ), path=self.path,
        )["plots"][0]
        self.assertGreater(early_late_check["growth_points"], 1.0)
        self.assertLess(early_late_check["growth_points"], 1.1)

        late_path = Path(self.tempdir.name) / "late.json"
        garden.crop_snapshot(now=datetime(2026, 7, 10, 0, 0, tzinfo=TZ), path=late_path)
        garden.plant_crop(
            "小番茄", "1", now=datetime(2026, 7, 10, 23, 55, tzinfo=TZ), path=late_path,
        )
        # 23:55 种下，到 23:59 只过了4分钟，累积应接近0但仍大于0。
        late_check = garden.crop_snapshot(
            now=datetime(2026, 7, 10, 23, 59, tzinfo=TZ), path=late_path,
        )["plots"][0]
        self.assertGreater(late_check["growth_points"], 0.0)
        self.assertLess(late_check["growth_points"], 0.01)
        self.assertGreater(early_late_check["growth_points"], late_check["growth_points"])

    def test_water_bonus_is_once_per_beijing_day_and_midnight_resets(self):
        self._plant_tomato()
        first = garden.water_crop("1", now=self.summer, path=self.path)
        repeated = garden.water_crop("1", now=self.summer + timedelta(minutes=1), path=self.path)
        after_midnight = garden.water_crop("1", now=datetime(2026, 7, 11, 0, 1, tzinfo=TZ), path=self.path)
        self.assertFalse(first["repeated"])
        self.assertTrue(repeated["repeated"])
        self.assertFalse(after_midnight["repeated"])
        # 线性记账：7/10 正午种下，到 7/11 00:01 自然累积了约半天多一分钟
        # 的生长（1.1/天），加上 7/10、7/11 各一次 0.25 浇水加成。
        natural = 1.1 * ((12 * 3600 + 60) / 86400.0)
        self.assertAlmostEqual(
            after_midnight["plot"]["growth_points"], natural + 0.25 + 0.25, places=3,
        )

    def test_elapsed_days_are_settled_in_one_call_and_growth_ignores_a_season_change(self):
        # 换季只挡"能不能新种"；已经种下的这一茬不会被换季暂停生长，
        # 不然会出现种下时来得及、跨季后卡到下一个适种季才能继续的
        # "卡边"体验（8/3 实锤过的辣椒/西瓜 270 天 bug）。
        spring = datetime(2026, 5, 4, 12, tzinfo=TZ)
        garden.crop_snapshot(now=spring, path=self.path)
        garden._inventory_add(self._inventory(), "seeds", "radish")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["inventory"]["seeds"]["radish"] = 1
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        garden.plant_crop("小萝卜", "1", now=spring, path=self.path)
        # 立夏（5/5 19:49）已经过去，跨进了萝卜不适种的夏季，
        # 但这一茬仍然正常长熟，不会被冻结在换季那一刻。
        crossed = garden.crop_snapshot(now=datetime(2026, 5, 8, 12, tzinfo=TZ), path=self.path)["plots"][0]
        self.assertEqual(crossed["status"], "ready")
        # 线性记账：5/4 正午种下，5/4 后半天+5/5 整天按春季 term 加成
        # 1.1/天，跨入夏季后 5/6、5/7 整天与 5/8 前半天按 1.0/天，
        # 0.55+1.1+1.0+1.0+0.5=4.15，且 5/8 清晨就已跨过 4.0 成熟线。
        self.assertEqual(crossed["growth_points"], 4.15)
        later = garden.crop_snapshot(now=datetime(2026, 7, 20, 12, tzinfo=TZ), path=self.path)["plots"][0]
        self.assertEqual((later["status"], later["growth_points"]), ("ready", 4.15))

    def test_harvest_returns_seed_and_blocks_repeat(self):
        self._plant_tomato()
        result = garden.harvest_crop("1", now=self.summer + timedelta(days=4), path=self.path)
        inventory = self._inventory()
        self.assertEqual((result["amount"], result["seed_return"]), (3, 1))
        self.assertEqual(inventory["produce"]["tomato"], 3)
        self.assertEqual(inventory["seeds"]["tomato"], 2)  # 初始两份，播一份、返一份。
        with self.assertRaisesRegex(garden.GardenError, "符合条件"):
            garden.harvest_crop("1", now=self.summer + timedelta(days=5), path=self.path)

    def test_treat_meal_gift_and_all_failures_do_not_mischarge(self):
        animal = garden.spawn("animal", species="小狗", intro="x", category="狗", personality="活泼", now=self.summer, path=self.path)
        self._set_inventory(produce={"pumpkin": 2, "tomato": 1, "mint": 1}, prepared_food={})
        treat = garden.feed_animal_treat(animal["id"], "南瓜", now=self.summer, path=self.path)
        self.assertEqual(treat["bond"]["delta"], 1)
        with self.assertRaisesRegex(garden.GardenError, "不适合"):
            garden.feed_animal_treat(animal["id"], "小番茄", now=self.summer, path=self.path)
        meal = garden.make_meal("番茄薄荷小沙拉", now=self.summer, path=self.path)
        self.assertEqual(meal["recipe_id"], "tomato_mint_salad")
        gift = garden.give_to_baby("番茄薄荷小沙拉", now=self.summer, path=self.path)
        self.assertEqual(gift["record"]["section"], "prepared_food")
        before = self._inventory()
        with self.assertRaises(garden.GardenError):
            garden.make_meal("草莓", now=self.summer, path=self.path)
        self.assertEqual(self._inventory(), before)

    def test_summer_recipes_consume_real_harvest_and_ambiguous_crop_names_do_not_charge(self):
        self._set_inventory(
            produce={"tomato": 1, "mint": 1, "mini_watermelon": 3, "cucumber": 2},
            prepared_food={},
        )
        expected = {
            "凉拌番茄": "chilled_tomato",
            "薄荷水": "mint_water",
            "西瓜果切": "watermelon_slices",
            "西瓜汁": "watermelon_juice",
            "西瓜冰沙": "watermelon_smoothie",
            "凉拌黄瓜": "cucumber_salad",
            "拍黄瓜": "smashed_cucumber",
        }
        for name, recipe_id in expected.items():
            with self.subTest(recipe=name):
                self.assertEqual(garden.make_meal(name, now=self.summer, path=self.path)["recipe_id"], recipe_id)
        inventory = self._inventory()
        self.assertEqual(inventory["produce"], {})
        self.assertEqual(inventory["prepared_food"], {recipe_id: 1 for recipe_id in expected.values()})

        self._set_inventory(produce={"cucumber": 1}, prepared_food={})
        before = self._inventory()
        with self.assertRaisesRegex(garden.GardenError, "凉拌黄瓜、拍黄瓜"):
            garden.make_meal("黄瓜", now=self.summer, path=self.path)
        self.assertEqual(self._inventory(), before)

    def test_pepper_eggs_lets_xiaoyu_choose_one_to_three_eggs_and_charges_exactly_that(self):
        self._set_inventory(
            produce={"pepper": 1}, animal_products={"egg": 3}, prepared_food={},
        )
        before = self._inventory()
        with self.assertRaisesRegex(garden.GardenError, "agent 决定.*1～3个"):
            garden.make_meal("辣椒炒蛋", now=self.summer, path=self.path)
        self.assertEqual(self._inventory(), before)
        with self.assertRaisesRegex(garden.GardenError, "1～3个蛋"):
            garden.make_meal("辣椒炒蛋", egg_count=4, now=self.summer, path=self.path)
        self.assertEqual(self._inventory(), before)

        meal = garden.make_meal("辣椒炒蛋", egg_count=2, now=self.summer, path=self.path)
        self.assertEqual((meal["recipe_id"], meal["egg_count"]), ("pepper_eggs", 2))
        self.assertEqual(meal["record"]["animal_ingredients"], {"egg": 2})
        inventory = self._inventory()
        self.assertEqual(inventory["produce"], {})
        self.assertEqual(inventory["animal_products"], {"egg": 1})
        self.assertEqual(inventory["prepared_food"], {"pepper_eggs": 1})

    def test_tomato_eggs_uses_xiaoyus_chosen_eggs_and_has_ten_specific_lines(self):
        self._set_inventory(
            produce={"tomato": 1}, animal_products={"egg": 3}, prepared_food={},
        )
        with self.assertRaisesRegex(garden.GardenError, "agent 决定.*1～3个"):
            garden.make_meal("番茄炒蛋", now=self.summer, path=self.path)

        meal = garden.make_meal("番茄炒蛋", egg_count=3, now=self.summer, path=self.path)
        self.assertEqual((meal["recipe_id"], meal["egg_count"]), ("tomato_eggs", 3))
        self.assertEqual(meal["record"]["ingredients"], {"tomato": 1})
        self.assertEqual(meal["record"]["animal_ingredients"], {"egg": 3})
        inventory = self._inventory()
        self.assertEqual(inventory["produce"], {})
        self.assertEqual(inventory["animal_products"], {})
        self.assertEqual(inventory["prepared_food"], {"tomato_eggs": 1})
        lines = garden.garden_content.RECIPE_MEAL_TEXT["tomato_eggs"]
        self.assertEqual((len(lines), len(set(lines))), (10, 10))

    def test_all_new_recipes_have_ten_audited_specific_lines(self):
        recipe_ids = (
            "chilled_tomato", "mint_water", "watermelon_slices",
            "watermelon_juice", "watermelon_smoothie",
            "cucumber_salad", "smashed_cucumber", "pepper_eggs", "tomato_eggs",
        )
        for recipe_id in recipe_ids:
            with self.subTest(recipe_id=recipe_id):
                lines = garden.garden_content.RECIPE_MEAL_TEXT[recipe_id]
                self.assertEqual((len(lines), len(set(lines))), (10, 10))
                self.assertTrue(all(line.endswith(("。", "！", "？")) for line in lines))

    def test_new_autumn_recipes_have_ten_audited_specific_lines(self):
        recipe_ids = (
            "tomato_salad", "sugar_roasted_chestnut", "osmanthus_cake", "pumpkin_cake",
            "stir_fried_cabbage", "steamed_sweet_potato", "roasted_sweet_potato",
            "chili_powder", "cabbage_with_dip", "osmanthus_honey",
            "osmanthus_honey_water", "chestnut_pumpkin_soup",
        )
        for recipe_id in recipe_ids:
            with self.subTest(recipe_id=recipe_id):
                lines = garden.garden_content.RECIPE_MEAL_TEXT[recipe_id]
                self.assertEqual((len(lines), len(set(lines))), (10, 10))
                self.assertTrue(all(line.endswith(("。", "！", "？")) for line in lines))

    def test_new_single_ingredient_autumn_recipes_consume_real_harvest(self):
        self._set_inventory(
            produce={
                "tomato": 1, "chestnut": 2, "osmanthus": 2, "pumpkin": 2,
                "chinese_cabbage": 1, "sweet_potato": 2, "pepper": 2,
            },
            prepared_food={},
        )
        expected = {
            "番茄沙拉": "tomato_salad",
            "糖炒栗子": "sugar_roasted_chestnut",
            "桂花糕": "osmanthus_cake",
            "南瓜饼": "pumpkin_cake",
            "蒸红薯": "steamed_sweet_potato",
            "烤红薯": "roasted_sweet_potato",
            "辣椒粉": "chili_powder",
        }
        for name, recipe_id in expected.items():
            with self.subTest(recipe=name):
                self.assertEqual(garden.make_meal(name, now=self.summer, path=self.path)["recipe_id"], recipe_id)
        inventory = self._inventory()
        self.assertEqual(
            inventory["produce"],
            {"chestnut": 1, "osmanthus": 1, "pumpkin": 1, "chinese_cabbage": 1, "pepper": 1},
        )
        self.assertEqual(inventory["prepared_food"], {recipe_id: 1 for recipe_id in expected.values()})

    def test_stir_fried_cabbage_needs_both_cabbage_and_raw_pepper(self):
        self._set_inventory(produce={"chinese_cabbage": 1}, prepared_food={})
        before = self._inventory()
        with self.assertRaisesRegex(garden.GardenError, "小白菜×1、辣椒×1"):
            garden.make_meal("炝炒白菜", now=self.summer, path=self.path)
        self.assertEqual(self._inventory(), before)

        self._set_inventory(produce={"chinese_cabbage": 1, "pepper": 1}, prepared_food={})
        meal = garden.make_meal("炝炒白菜", now=self.summer, path=self.path)
        self.assertEqual(meal["recipe_id"], "stir_fried_cabbage")
        self.assertEqual(self._inventory()["produce"], {})

    def test_chili_powder_and_osmanthus_honey_are_prepared_ingredients_for_a_second_dish(self):
        self._set_inventory(produce={"chinese_cabbage": 1, "pepper": 1}, prepared_food={})
        before = self._inventory()
        with self.assertRaisesRegex(garden.GardenError, "小白菜×1、辣椒粉×1"):
            garden.make_meal("蘸水白菜", now=self.summer, path=self.path)
        self.assertEqual(self._inventory(), before)

        powder = garden.make_meal("辣椒粉", now=self.summer, path=self.path)
        self.assertEqual(powder["recipe_id"], "chili_powder")
        mid_inventory = self._inventory()
        self.assertEqual(mid_inventory["produce"], {"chinese_cabbage": 1})
        self.assertEqual(mid_inventory["prepared_food"], {"chili_powder": 1})

        dip = garden.make_meal("蘸水白菜", now=self.summer, path=self.path)
        self.assertEqual(dip["recipe_id"], "cabbage_with_dip")
        self.assertEqual(dip["record"]["ingredients"], {"chinese_cabbage": 1})
        self.assertEqual(dip["record"]["prepared_ingredients"], {"chili_powder": 1})
        final_inventory = self._inventory()
        self.assertEqual(final_inventory["produce"], {})
        self.assertEqual(final_inventory["prepared_food"], {"cabbage_with_dip": 1})

        self._set_inventory(produce={"osmanthus": 1}, prepared_food={})
        before = self._inventory()
        with self.assertRaisesRegex(garden.GardenError, "桂花蜜×1"):
            garden.make_meal("桂花蜜泡水", now=self.summer, path=self.path)
        self.assertEqual(self._inventory(), before)

        garden.make_meal("桂花蜜", now=self.summer, path=self.path)
        water = garden.make_meal("桂花蜜泡水", now=self.summer, path=self.path)
        self.assertEqual(water["recipe_id"], "osmanthus_honey_water")
        self.assertEqual(self._inventory()["prepared_food"], {"osmanthus_honey_water": 1})

    def test_chestnut_pumpkin_soup_needs_both_crops(self):
        self._set_inventory(produce={"chestnut": 1}, prepared_food={})
        with self.assertRaisesRegex(garden.GardenError, "板栗×1、南瓜×1"):
            garden.make_meal("板栗南瓜羹", now=self.summer, path=self.path)

        self._set_inventory(produce={"chestnut": 1, "pumpkin": 1}, prepared_food={})
        meal = garden.make_meal("板栗南瓜羹", now=self.summer, path=self.path)
        self.assertEqual(meal["recipe_id"], "chestnut_pumpkin_soup")
        self.assertEqual(self._inventory()["produce"], {})

    def test_summer_crop_care_pools_expand_to_ten_safe_lines_each(self):
        crop_ids = ("cucumber", "mini_watermelon", "pepper")
        actions = (
            "water", "water_repeat", "treat_pest", "treat_diseased_leaf",
            "treat_waterlogged", "treat_nutrient_deficiency",
        )
        forbidden = ("立刻恢复", "马上恢复", "恢复生长", "成熟", "增产", "农药")
        for crop_id in crop_ids:
            for action in actions:
                with self.subTest(crop_id=crop_id, action=action):
                    lines = garden.garden_content.crop_care_pool(crop_id, action)
                    self.assertEqual((len(lines), len(set(lines))), (10, 10))
                    self.assertTrue(all(line.endswith(("。", "！", "？")) for line in lines))
                    self.assertTrue(all(12 <= len(line) <= 55 for line in lines))
                    self.assertFalse(any(word in line for line in lines for word in forbidden))
        self.assertEqual(garden.garden_content.crop_care_pool("tomato", "water"), ())

    def test_concurrent_gifts_cannot_overdraw_and_crop_event_confirms_once(self):
        self._set_inventory(produce={"strawberry": 1})
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: self._gift_outcome(), range(2)))
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(self._inventory()["produce"], {})

        self._plant_tomato()
        event = garden.run_tick(self.summer + timedelta(days=1), rng=_StubRng([0.0]), path=self.path)
        self.assertEqual((event["type"], event["stage"]), ("crop_stage", "sprout"))
        self.assertTrue(garden.confirm_pending_event(event["event_id"], event["delivery_token"], now=self.summer + timedelta(days=1), path=self.path))
        self.assertFalse(garden.confirm_pending_event(event["event_id"], event["delivery_token"], now=self.summer + timedelta(days=1), path=self.path))

    def _gift_outcome(self):
        try:
            garden.give_to_baby("草莓", now=self.summer, path=self.path)
            return "ok"
        except garden.GardenError:
            return "empty"

    def test_malformed_crop_event_never_overwrites_file(self):
        self._plant_tomato()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["pending_events"] = [{"type": "crop_stage", "event_id": "bad"}]
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(garden.GardenError):
            garden.run_tick(self.summer + timedelta(days=1), rng=_StubRng([0.0]), path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_harvest_cancels_queued_stage_events_before_replant(self):
        self._plant_tomato()
        first = garden.run_tick(self.summer + timedelta(days=4), rng=_StubRng([0.0]), path=self.path)
        self.assertEqual((first["type"], first["stage"]), ("crop_stage", "sprout"))
        garden.harvest_crop("1", now=self.summer + timedelta(days=4), path=self.path)
        self.assertFalse(garden.confirm_pending_event(first["event_id"], first["delivery_token"], now=self.summer + timedelta(days=4), path=self.path))
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["pending_events"], [])
        self.assertEqual(raw["meta"].get("completed_crop_cycles", []), [])
        # 本用例真正要守住的是：被取消掉的旧 crop_stage 事件不会死灰复燃。
        # 收获后地块空置，没有别的事件可触发，下一轮 tick 应该什么都不出。
        after = garden.run_tick(self.summer + timedelta(days=5), rng=_StubRng([0.0]), path=self.path)
        self.assertIsNone(after)

    def test_harvest_after_all_stage_events_confirmed_leaves_no_archive(self):
        self._plant_tomato()
        while True:
            event = garden.run_tick(self.summer + timedelta(days=4), rng=_StubRng([0.0]), path=self.path)
            if event is None:
                break
            self.assertTrue(garden.confirm_pending_event(event["event_id"], event["delivery_token"], now=self.summer + timedelta(days=4), path=self.path))
        garden.harvest_crop("1", now=self.summer + timedelta(days=4), path=self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["pending_events"], [])
        self.assertEqual(raw["meta"].get("completed_crop_cycles", []), [])

    def test_winter_seed_box_waits_for_first_plantable_season(self):
        winter = datetime(2026, 12, 10, 12, tzinfo=TZ)
        winter_snapshot = garden.crop_snapshot(now=winter, path=self.path)
        self.assertEqual(winter_snapshot["inventory"]["seeds"], {})
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("crop_seed_box_initialized", raw["meta"])
        spring_snapshot = garden.crop_snapshot(now=datetime(2027, 3, 1, 12, tzinfo=TZ), path=self.path)
        self.assertEqual(spring_snapshot["inventory"]["seeds"], {"strawberry": 2, "radish": 2})

    def test_off_season_seeds_move_to_warehouse_and_back(self):
        self._set_inventory(seeds={"tomato": 2, "strawberry": 1, "pepper": 3})
        winter = datetime(2026, 12, 10, 12, tzinfo=TZ)
        winter_snapshot = garden.crop_snapshot(now=winter, path=self.path)
        self.assertEqual(winter_snapshot["inventory"]["seeds"], {})
        self.assertEqual(
            winter_snapshot["inventory"]["warehouse_seeds"],
            {"tomato": 2, "strawberry": 1, "pepper": 3},
        )
        # 辣椒春夏秋都能种，一到春天就该先跟草莓一起搬回种子盒；番茄只属于夏天，继续留在仓库。
        # 这个院子是夏天创建的（见 _set_inventory 用 self.summer），从没经历过春天，
        # 所以 spring_base_2026 补充包会在这里第一次触发，草莓额外 +2（1→3）。
        spring_snapshot = garden.crop_snapshot(now=datetime(2027, 3, 1, 12, tzinfo=TZ), path=self.path)
        self.assertEqual(spring_snapshot["inventory"]["warehouse_seeds"], {"tomato": 2})
        self.assertEqual(spring_snapshot["inventory"]["seeds"]["pepper"], 3)
        self.assertEqual(spring_snapshot["inventory"]["seeds"]["strawberry"], 3)
        # 夏天到了：番茄搬回种子盒，草莓（只属于春天，同样补发了小萝卜）改成留在仓库里等下一个春天。
        summer_snapshot = garden.crop_snapshot(now=datetime(2027, 7, 1, 12, tzinfo=TZ), path=self.path)
        self.assertEqual(summer_snapshot["inventory"]["warehouse_seeds"], {"strawberry": 3, "radish": 2})
        self.assertEqual(summer_snapshot["inventory"]["seeds"]["tomato"], 2)
        self.assertEqual(summer_snapshot["inventory"]["seeds"]["pepper"], 3)

    def test_current_term_boundary_controls_snapshot_planting_and_watering(self):
        boundary = datetime(2026, 5, 5, 20, 0, tzinfo=TZ)  # 立夏 19:49 后
        snapshot = garden.crop_snapshot(now=boundary, path=self.path)
        self.assertEqual(snapshot["season"], "summer")
        planted = garden.plant_crop("小番茄", "1", now=boundary, path=self.path)
        self.assertEqual(planted["crop_id"], "tomato")
        self.assertTrue(garden.water_crop("1", now=boundary, path=self.path)["accelerated"])

    def test_treat_settles_away_animals_before_any_inventory_charge(self):
        dog = garden.spawn("animal", species="小狗", intro="x", category="狗", personality="活泼", now=self.summer, path=self.path)
        self._set_inventory(produce={"pumpkin": 1})
        later = self.summer + timedelta(hours=garden.NATURAL_VISIT_MAX_HOURS + 1)
        with self.assertRaisesRegex(garden.GardenError, "没找到"):
            garden.feed_animal_treat(dog["id"], "南瓜", now=later, path=self.path)
        self.assertEqual(self._inventory()["produce"], {"pumpkin": 1})
        self.assertEqual(garden.away_entries(self.path)[0]["id"], dog["id"])

    def test_treat_settles_crops_even_when_another_animal_goes_away(self):
        self._plant_tomato()
        target = garden.spawn("animal", species="小狗", intro="x", category="狗", personality="活泼", now=self.summer, path=self.path)
        departing = garden.spawn("animal", species="小狗", intro="x", category="狗", personality="怕生", now=self.summer, path=self.path)
        departing["next_natural_visit_after"] = (self.summer + timedelta(hours=1)).isoformat()
        garden.save_garden([target, departing], self.path)
        self._set_inventory(produce={"pumpkin": 1})
        later = self.summer + timedelta(days=2)
        garden.feed_animal_treat(target["id"], "南瓜", now=later, path=self.path)
        self.assertEqual(garden.away_entries(self.path)[0]["id"], departing["id"])
        settled = garden.crop_snapshot(now=later, path=self.path)["plots"][0]
        self.assertGreater(settled["growth_points"], 0.0)

    def test_complete_but_false_ready_event_refuses_without_write(self):
        plot = self._plant_tomato()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        event_id = f"crop:p1:{plot['cycle_id']}:stage:ready"
        payload["pending_events"] = [{
            "type": "crop_stage", "event_id": event_id, "plot_id": "p1", "crop_id": "tomato",
            "cycle_id": plot["cycle_id"], "stage": "ready",
        }]
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "阶段与菜畦事实不一致"):
            garden.run_tick(self.summer, rng=_StubRng([0.0]), path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_term_affinity_is_data_driven_and_adds_only_small_bonus(self):
        planted_at = datetime(2026, 7, 22, 12, tzinfo=TZ)
        garden.crop_snapshot(now=planted_at, path=self.path)
        garden.plant_crop("小番茄", "1", now=planted_at, path=self.path)
        plot = garden.crop_snapshot(now=datetime(2026, 7, 24, 12, tzinfo=TZ), path=self.path)["plots"][0]
        # 线性记账：7/22 正午到 7/24 正午恰好两个整天，大暑 term 加成
        # 1.1/天 × 2 = 2.2。
        self.assertEqual(plot["growth_points"], 2.2)


class FindPlotNoCandidateMessageTests(unittest.TestCase):
    """8/9 事故：四块地全满时零候选走了跟'多候选'一样的措辞，语义说反了。

    直接构造最小 state 单测 ``_find_plot``，覆盖零候选/多候选/单候选三种
    数量在四种调用方真实会用到的 statuses 下的措辞，不依赖完整院子流程。
    """

    def _state(self, statuses):
        return {"plots": [{"plot_id": f"p{i}", "status": status} for i, status in enumerate(statuses, start=1)]}

    def test_zero_candidates_gets_a_status_specific_message_not_the_ambiguous_one(self):
        cases = {
            "empty": "现在没有空着的菜畦，等收获腾出位置再种吧",
            "growing": "现在没有正在生长的菜畦，等种下什么再来浇水吧",
            "ready": "现在没有成熟待收获的菜畦，再等等它们长熟吧",
            "withered": "现在没有枯萎待清理的菜畦，用不着清理",
        }
        all_statuses = ("empty", "growing", "ready", "withered")
        for status, expected in cases.items():
            with self.subTest(status=status):
                # 四块地都不是目标状态，制造真正的"零候选"（8/9 真实场景：
                # 四块地全满，播种时零个 empty 候选）。
                other_statuses = [s for s in all_statuses if s != status]
                state = self._state([other_statuses[0]] * 4)
                with self.assertRaisesRegex(garden.GardenError, re.escape(expected)) as ctx:
                    garden._find_plot(state, None, statuses=(status,))
                self.assertNotIn("不止一个", str(ctx.exception))

    def test_multiple_candidates_message_is_unchanged(self):
        state = self._state(["growing", "growing", "empty", "empty"])
        with self.assertRaisesRegex(garden.GardenError, "现在有不止一个可选菜畦，请带上地块编号"):
            garden._find_plot(state, None, statuses=("growing",))

    def test_single_candidate_still_resolves_without_a_selector(self):
        state = self._state(["ready", "empty", "empty", "empty"])
        plot = garden._find_plot(state, None, statuses=("ready",))
        self.assertEqual(plot["plot_id"], "p1")

    def test_selector_given_zero_match_message_is_unchanged(self):
        state = self._state(["growing", "growing", "growing", "growing"])
        with self.assertRaisesRegex(garden.GardenError, "没找到符合条件的菜畦，请检查编号和状态"):
            garden._find_plot(state, "1", statuses=("ready",))

    def test_plant_crop_with_all_plots_full_reports_the_no_empty_plot_message(self):
        # 复现8/9真实事件：三次播种小白菜全部失败，根因是四块地全满。
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        path = Path(tempdir.name) / "garden.json"
        summer = datetime(2026, 7, 10, 12, tzinfo=TZ)
        garden.crop_snapshot(now=summer, path=path)
        for plot_no, crop_name in (("1", "小番茄"), ("2", "黄瓜"), ("3", "西瓜"), ("4", "辣椒")):
            garden.plant_crop(crop_name, plot_no, now=summer, path=path)
        before = path.read_bytes()
        with self.assertRaisesRegex(garden.GardenError, "现在没有空着的菜畦，等收获腾出位置再种吧"):
            garden.plant_crop("薄荷", now=summer, path=path)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
