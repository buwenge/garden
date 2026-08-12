import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import garden
import home


TZ = ZoneInfo("Asia/Shanghai")


class _LifecycleRng:
    def __init__(self, *, condition_type="diseased_leaf", random_value=0.0):
        self.condition_type = condition_type
        self.random_value = random_value

    def random(self):
        return self.random_value

    def choice(self, values):
        if tuple(values) == garden.NATURAL_CONDITION_TYPES:
            return self.condition_type
        return values[0]


class GardenStageFLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        # 这组是第四版生命周期基线；home 现在会正常加载 .env，
        # 测试不能再偶然继承开发机的现实天气开关。
        environment = patch.dict(
            os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "0"},
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.start = datetime(2026, 7, 28, 12, 0, tzinfo=TZ)

    def _write_v3_two_plot_state(self, *, with_animal=False):
        state = garden._empty_state()
        state.pop("entries")
        state["version"] = 3
        state["plots"] = [
            {**garden._empty_plot("p1"), "legacy_plot_note": "保留一号地"},
            {**garden._empty_plot("p2"), "legacy_plot_note": "保留二号地"},
        ]
        state["inventory"]["seeds"] = {"cucumber": 1}
        state["meta"]["crop_seed_box_initialized"] = True
        state["legacy"]["unrecognized_top_level"]["fixture_marker"] = "stage-f"
        if with_animal:
            state["animals"] = [
                garden._normalize_animal(
                    {
                        "id": "a-fixed",
                        "kind": "animal",
                        "species": "橘猫",
                        "category": "猫",
                        "personality": "活泼",
                        "nickname": "暖暖",
                        "trait": "尾巴尖有一小截白",
                        "spot": garden.SPOTS[0],
                        "arrived_at": self.start.isoformat(),
                        "last_cared_at": self.start.isoformat(),
                        "care_count": 2,
                        "last_action": "摸摸",
                        "return_count": 0,
                        "status": "active",
                        "left_at": None,
                        "departure_note": None,
                        "last_note": "在菜畦边绕了一圈",
                        "bond_points": 8,
                        "bond_level": 2,
                        "bond_actions_by_date": {},
                        "bond_milestones_seen": [],
                        "nickname_bonus_claimed": True,
                        "residency": "visitor",
                        "preferred_action": "摸摸",
                        "last_visited_at": self.start.isoformat(),
                        "next_natural_visit_after": "2030-01-01T00:00:00+08:00",
                    },
                    self.start,
                    _LifecycleRng(),
                )
            ]
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _raw(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _confirm_until_condition(self, now, *, condition_type, severity=None):
        rng = _LifecycleRng(condition_type=condition_type)
        for _ in range(8):
            event = garden.run_tick(now, rng=rng, path=self.path)
            self.assertIsNotNone(event)
            confirmed = garden.confirm_pending_event(
                event["event_id"],
                event["delivery_token"],
                now=now,
                path=self.path,
            )
            self.assertTrue(confirmed)
            if (
                event["type"] == "crop_condition"
                and (severity is None or event["severity"] == severity)
            ):
                return event
        self.fail("没有在有限的 pending 队列内拿到作物异常")

    def test_migrated_cucumber_disease_stroll_damage_recovery_and_reduced_harvest(self):
        self._write_v3_two_plot_state(with_animal=True)

        migrated = garden.crop_snapshot(now=self.start, path=self.path)
        raw = self._raw()
        self.assertEqual(raw["version"], 4)
        self.assertEqual(
            [plot["plot_id"] for plot in raw["plots"]],
            ["p1", "p2", "p3", "p4"],
        )
        self.assertEqual(raw["plots"][0]["legacy_plot_note"], "保留一号地")
        self.assertEqual(raw["plots"][1]["legacy_plot_note"], "保留二号地")
        self.assertEqual(
            raw["plots"][2:],
            [
                {"plot_id": "p3", "status": "empty"},
                {"plot_id": "p4", "status": "empty"},
            ],
        )
        self.assertEqual(
            migrated["inventory"]["seeds"],
            {"cucumber": 3, "mini_watermelon": 2, "pepper": 2},
        )
        animal_before = copy.deepcopy(raw["animals"])

        planted = garden.plant_crop(
            "黄瓜", "一号菜畦", now=self.start, path=self.path,
        )
        self.assertEqual(
            (planted["plot_id"], planted["crop_id"]),
            ("p1", "cucumber"),
        )
        sprouted_at = self.start + timedelta(days=1)
        garden.crop_snapshot(now=sprouted_at, path=self.path)
        self.assertEqual(self._raw()["plots"][0]["stage"], "sprout")

        healthy_scene = (
            "白天，一号地的黄瓜刚冒出嫩芽，暖暖贴着菜畦边慢慢绕过，"
            "另外三块地还空着。"
        )
        with patch(
            "garden.garden_generator.generate_stroll",
            return_value=healthy_scene,
        ) as stroll_writer:
            self.assertEqual(
                garden.stroll_scene(now=sprouted_at, path=self.path),
                healthy_scene,
            )
            self.assertEqual(
                garden.stroll_scene(now=sprouted_at, path=self.path),
                healthy_scene,
            )
        stroll_writer.assert_called_once()

        condition_body = "黄瓜的叶片卷起斑驳病边，发黑的一角还贴在嫩藤上。"
        damaged_body = "黄瓜的病叶仍压在藤上，这一茬已经确定要少收一份。"
        with (
            patch.dict(
                os.environ,
                {"GARDEN_NATURAL_CROP_CONDITIONS_ENABLED": "1"},
            ),
            patch(
                "garden.garden_generator.generate_crop_copy",
                side_effect=(condition_body, damaged_body),
            ) as crop_writer,
        ):
            warning = self._confirm_until_condition(
                sprouted_at,
                condition_type="diseased_leaf",
            )
            self.assertEqual(warning["condition_type"], "diseased_leaf")
            self.assertIn(condition_body, warning["text"])
            self.assertIn("需要修剪", warning["text"])
            self.assertEqual(
                self._raw()["plots"][0]["condition"]["announced_at"],
                sprouted_at.isoformat(),
            )

            damaged_at = sprouted_at + timedelta(hours=24)
            damaged = self._confirm_until_condition(
                damaged_at,
                condition_type="diseased_leaf",
            )
            self.assertEqual(damaged["severity"], "damaged")
            self.assertIn(damaged_body, damaged["text"])
            self.assertIn("永久减少1份", damaged["text"])

        self.assertEqual(crop_writer.call_count, 2)
        resolved_at = damaged_at + timedelta(minutes=1)
        resolved = garden.resolve_crop_condition(
            "修剪", "p1", now=resolved_at, path=self.path,
        )
        self.assertEqual(
            (resolved["outcome"], resolved["yield_penalty"]),
            ("resolved", 1),
        )

        ready_at = self.start + timedelta(days=5, minutes=1)
        ready = garden.crop_snapshot(now=ready_at, path=self.path)
        self.assertEqual(
            (ready["plots"][0]["status"], ready["plots"][0]["stage"]),
            ("ready", "ready"),
        )
        harvested = garden.harvest_crop("p1", now=ready_at, path=self.path)
        self.assertEqual(
            (
                harvested["crop_id"],
                harvested["amount"],
                harvested["seed_return"],
                harvested["yield_penalty"],
            ),
            ("cucumber", 2, 1, 1),
        )
        # 第七版阶段E：拖到 damaged 现在也在品相上留疤（设计稿六.2），
        # 这一茬的收成因此整批分流进 produce_poor，不再落在 produce 里。
        self.assertEqual(harvested["quality"], "poor")

        final = self._raw()
        self.assertEqual(
            final["plots"][0],
            {"plot_id": "p1", "status": "empty"},
        )
        self.assertEqual(
            final["plots"][1]["legacy_plot_note"],
            "保留二号地",
        )
        self.assertEqual(final["inventory"]["seeds"]["cucumber"], 3)
        self.assertNotIn("cucumber", final["inventory"].get("produce", {}))
        self.assertEqual(final["inventory"]["produce_poor"]["cucumber"], 2)
        incident = final["journal"]["crop_incidents"][0]
        self.assertEqual(
            (
                incident["type"],
                incident["outcome"],
                incident["yield_penalty"],
                incident["resolved_by"],
            ),
            ("diseased_leaf", "resolved", 1, "修剪"),
        )
        self.assertEqual(
            [node["kind"] for node in incident["nodes"]],
            ["occurred", "announced", "damaged", "resolved"],
        )
        self.assertEqual(
            final["journal"]["crops_harvested"][-1]["yield_penalty"],
            1,
        )
        self.assertEqual(final["animals"], animal_before)

    def test_slapstick_repeat_watering_withered_crop_cannot_harvest_and_clears_without_returns(self):
        self._write_v3_two_plot_state()
        garden.crop_snapshot(now=self.start, path=self.path)
        garden.plant_crop("黄瓜", "p1", now=self.start, path=self.path)
        watered_at = self.start + timedelta(days=1)
        garden.crop_snapshot(now=watered_at, path=self.path)

        first = garden.water_crop("p1", now=watered_at, path=self.path)
        second = garden.water_crop(
            "p1",
            now=watered_at,
            path=self.path,
            rng=_LifecycleRng(random_value=0.5),
        )
        self.assertEqual(
            (first["outcome"], first["watering_count"]),
            ("watered", 1),
        )
        self.assertEqual(
            (
                second["outcome"],
                second["watering_count"],
                second["style"],
            ),
            ("protest", 2, "slapstick"),
        )
        # 黄瓜有专属的 water_repeat 本地文案池（8/2 加入），即便传了
        # use_writer=True，crop_copy 也会优先用本地池而不去问写手；这里
        # 故意把写手 mock 成完全不同的句子，确认本地池确实拿到了优先权。
        with patch(
            "garden.garden_generator.generate_crop_copy",
            return_value="黄瓜猛地一甩藤，把水珠全弹了回来，嚷着今天已经喝过了。",
        ):
            protest_text = home._garden_water_crop_text(
                second,
                use_writer=True,
            )
        body = protest_text.split("\n", 1)[0]
        self.assertIn(body, garden.garden_content.crop_care_pool("cucumber", "water_repeat"))
        self.assertNotIn("猛地一甩藤", protest_text)
        self.assertIn("本次没有生长加成", protest_text)

        third = garden.water_crop("p1", now=watered_at, path=self.path)
        self.assertEqual(
            (
                third["outcome"],
                third["watering_count"],
                third["condition_type"],
            ),
            ("waterlogged", 3, "waterlogged"),
        )
        waterlogged_text = home._garden_water_crop_text(third)
        self.assertIn("结果：一号地已进入积水状态", waterlogged_text)
        before_failure_inventory = copy.deepcopy(self._raw()["inventory"])

        failed_at = watered_at + timedelta(hours=36)
        garden.crop_snapshot(now=failed_at, path=self.path)
        failed = self._raw()
        self.assertEqual(
            (
                failed["plots"][0]["status"],
                failed["plots"][0]["condition"]["status"],
            ),
            ("withered", "failed"),
        )
        self.assertEqual(
            [
                node["kind"]
                for node in failed["journal"]["crop_incidents"][0]["nodes"]
            ],
            ["occurred", "announced", "damaged", "failed"],
        )
        with patch(
            "garden.garden_generator.generate_crop_copy",
            side_effect=(
                "黄瓜的积水还闷在根边，这一茬已经确定少收一份。",
                "黄瓜的藤叶已经彻底枯死，根边只剩一层没有退尽的积水。",
            ),
        ) as crop_writer:
            withered = self._confirm_until_condition(
                failed_at,
                condition_type="waterlogged",
                severity="withered",
            )
        self.assertEqual(crop_writer.call_count, 2)
        self.assertIn("已经彻底枯死", withered["text"])
        self.assertIn("不会返还种子", withered["text"])
        with self.assertRaisesRegex(garden.GardenError, "没找到符合条件"):
            garden.harvest_crop("p1", now=failed_at, path=self.path)

        cleared = garden.clear_withered_crop(
            "一号地",
            now=failed_at,
            path=self.path,
        )
        self.assertEqual(cleared["crop_id"], "cucumber")
        final = self._raw()
        self.assertEqual(
            final["plots"][0],
            {"plot_id": "p1", "status": "empty"},
        )
        self.assertEqual(final["inventory"], before_failure_inventory)
        self.assertEqual(
            final["journal"]["crop_incidents"][0]["outcome"],
            "failed",
        )
        self.assertFalse(
            any(
                event.get("cycle_id") == cleared["cycle_id"]
                for event in final["pending_events"]
                if isinstance(event, dict)
            )
        )


if __name__ == "__main__":
    unittest.main()
