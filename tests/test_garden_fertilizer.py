"""第八版施肥系统专项测试（设计稿：迷你小院子第八版施肥系统设计与开工令）。

覆盖范围对齐开工令第六节：
- 产粪线性累积与上限不入账（不整段批记账、到上限不再让时间入账）
- 打扫收走建批、units==0 不建空批次
- 发酵到期入库、事件幂等（重复结算只排一条）
- 治缺肥扣料/无料报错不改任何状态
- 救品质四条分流：可救 / 浸泡中拒绝 / 人祸拒绝 / 无因拒绝，拒绝路径都不扣肥料
- 落盘再读不丢（manure/compost/fertilizer/quality_cause）
- 迁移幂等（旧档缺这些字段时的一次性补齐）

不碰生产 garden.json；全部状态落在 tempfile.TemporaryDirectory() 里。
"""

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

TZ = ZoneInfo("Asia/Shanghai")


def _growing_plot(
    plot_id, *, crop_id="tomato", cycle_id="cycle-1", growth_points=1.0,
    stage="growing", status="growing", now=None,
):
    now = now or datetime.now(TZ)
    return {
        "plot_id": plot_id,
        "crop_id": crop_id,
        "planted_at": (now - timedelta(days=1)).isoformat(),
        "last_settled_at": now.isoformat(),
        "growth_points": growth_points,
        "stage": stage,
        "water_bonus_dates": [],
        "ready_at": now.isoformat() if status == "ready" else None,
        "status": status,
        "cycle_id": cycle_id,
        "stage_events_seen": [],
    }


def _adult_chick(chick_id, *, sex, now, nickname=None, profile_index=0):
    """成鸡（可下蛋母鸡/打鸣公鸡都行），照抄 test_garden_stage_d_v6.py 的写法。

    profile_index 用来在同一个鸡舍里放多只鸡时避免撞上同一份外貌档案
    （_validate_coop 要求 profile_id 互不重复）。
    """
    hatched_at = now - timedelta(days=1)
    matured_at = hatched_at + garden.CHICK_MATURITY_DURATION
    profile = garden_content.CHICKEN_PROFILE_POOL[profile_index]
    next_egg_at = None
    if sex == "hen":
        next_egg_at = (matured_at + garden.HEN_FIRST_EGG_AFTER_MATURITY).isoformat()
    return {
        "id": chick_id,
        "hatched_at": hatched_at.isoformat(),
        "sex": sex,
        "stage": "adult",
        "matures_at": matured_at.isoformat(),
        "matured_at": matured_at.isoformat(),
        "next_egg_at": next_egg_at,
        "eggs_laid": 0,
        "nickname": nickname,
        "feed_count": 0,
        "pet_count": 0,
        "last_fed_at": None,
        "last_petted_at": None,
        **{
            key: profile[key] for key in (
                "profile_id", "chick_appearance", "adult_appearance", "personality", "intro",
            )
        },
    }


def _built_coop_state(now, *, chicks=()):
    state = garden._empty_state()
    state.pop("entries")
    state["plots"] = [garden._empty_plot(pid) for pid in garden._PLOT_IDS]
    state["meta"]["crop_seed_box_initialized"] = True
    state["coop"].update({
        "built": True,
        "built_at": (now - timedelta(days=2)).isoformat(),
        "story_status": "hatched",
        "progress_queued": ["warming", "tapping", "hatched"],
        "chicks": list(chicks),
    })
    if chicks:
        state["coop"]["total_eggs_laid"] = sum(c.get("eggs_laid", 0) for c in chicks)
    return state


class _FileTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "garden.json"
        self.now = datetime(2026, 8, 20, 12, 0, tzinfo=TZ)

    def _write(self, state):
        payload = {k: v for k, v in state.items() if k not in ("entries", "_migrated")}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _raw(self):
        return json.loads(self.path.read_text(encoding="utf-8"))


# ───────────────────────── 产粪线性结算 ─────────────────────────


class ManureSettlementTests(_FileTestCase):
    def test_manure_accrues_linearly_with_adult_count(self):
        state = _built_coop_state(
            self.now,
            chicks=[
                _adult_chick("hen-1", sex="hen", now=self.now),
                _adult_chick("rooster-1", sex="rooster", now=self.now),
            ],
        )
        state["coop"]["manure"] = {"units": 0, "last_settled_at": self.now.isoformat()}

        # 2 只成鸡、过 12 小时 = 2 * 0.5 = 1 份（向下取整）。
        later = self.now + timedelta(hours=12)
        changed = garden._settle_coop_manure(state, later)
        self.assertTrue(changed)
        self.assertEqual(state["coop"]["manure"]["units"], 1)
        # 零头（半份）留给下次结算，游标只推进到刚好折算成 1 份的那一刻，
        # 不是直接推到 later（线性连续累积，不是整段批记账）。
        cursor = datetime.fromisoformat(state["coop"]["manure"]["last_settled_at"])
        self.assertEqual(cursor, self.now + timedelta(hours=12))

    def test_manure_carries_remainder_across_multiple_settlements(self):
        state = _built_coop_state(self.now, chicks=[_adult_chick("hen-1", sex="hen", now=self.now)])
        state["coop"]["manure"] = {"units": 0, "last_settled_at": self.now.isoformat()}
        # 1 只成鸡：18 小时不够 1 天，理应仍是 0 份，但游标要推进到已经折算
        # 的部分（这里折算部分是 0，所以游标不动，返回 changed=False）。
        mid = self.now + timedelta(hours=18)
        changed = garden._settle_coop_manure(state, mid)
        self.assertFalse(changed)
        self.assertEqual(state["coop"]["manure"]["units"], 0)
        # 再结算到 30 小时（累计 30h*1只/24h=1.25 → 1 份）。
        later = self.now + timedelta(hours=30)
        garden._settle_coop_manure(state, later)
        self.assertEqual(state["coop"]["manure"]["units"], 1)

    def test_manure_stops_time_banking_at_cap(self):
        state = _built_coop_state(
            self.now,
            chicks=[_adult_chick(f"hen-{i}", sex="hen", now=self.now) for i in range(6)],
        )
        state["coop"]["manure"] = {"units": 0, "last_settled_at": self.now.isoformat()}
        # 6 只成鸡、过 3 天：理论产出 18 份，远超上限 6；到上限后不能把多出
        # 的时间攒起来，游标必须直接推到 now，不留"银行"。
        far_future = self.now + timedelta(days=3)
        garden._settle_coop_manure(state, far_future)
        self.assertEqual(state["coop"]["manure"]["units"], garden.MANURE_CAP)
        cursor = datetime.fromisoformat(state["coop"]["manure"]["last_settled_at"])
        self.assertEqual(cursor, far_future)

        # 反向验证时间银行：把游标手动往回拨一点（模拟"没有及时打扫"），
        # 打扫后立刻再结算——如果时间银行没堵住，这里会凭空再产出鸡粪；
        # 正确实现下打扫清零后、下一次结算只按"新游标到 now"的真实间隔算。
        state["coop"]["manure"]["units"] = 0
        garden._settle_coop_manure(state, far_future + timedelta(hours=1))
        # 1 小时 * 6 只 / 24h = 0.25 → 仍是 0 份，不会因为之前攒着的时间
        # 银行凭空多算。
        self.assertEqual(state["coop"]["manure"]["units"], 0)

    def test_manure_zero_adults_advances_cursor_without_banking(self):
        state = _built_coop_state(self.now, chicks=[])
        state["coop"]["manure"] = {"units": 0, "last_settled_at": self.now.isoformat()}
        far_future = self.now + timedelta(days=10)
        garden._settle_coop_manure(state, far_future)
        self.assertEqual(state["coop"]["manure"]["units"], 0)
        cursor = datetime.fromisoformat(state["coop"]["manure"]["last_settled_at"])
        self.assertEqual(cursor, far_future)


# ───────────────────────── 打扫鸡舍 ─────────────────────────


class CoopCleanTests(_FileTestCase):
    def test_clean_coop_collects_manure_and_builds_batch(self):
        state = _built_coop_state(
            self.now,
            chicks=[
                _adult_chick("hen-1", sex="hen", now=self.now, profile_index=0),
                _adult_chick("hen-2", sex="hen", now=self.now, profile_index=1),
            ],
        )
        state["coop"]["manure"] = {"units": 4, "last_settled_at": self.now.isoformat()}
        self._write(state)

        result = garden.care_chicken(None, "clean", now=self.now, path=self.path)
        self.assertEqual(result["units"], 4)

        raw = self._raw()
        self.assertEqual(raw["coop"]["manure"]["units"], 0)
        batches = raw["compost"]["batches"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["units"], 4)
        ready_at = datetime.fromisoformat(batches[0]["ready_at"])
        self.assertEqual(ready_at, self.now + garden.COMPOST_READY_AFTER)

    def test_clean_coop_with_no_manure_does_not_create_empty_batch(self):
        state = _built_coop_state(self.now, chicks=[_adult_chick("hen-1", sex="hen", now=self.now)])
        state["coop"]["manure"] = {"units": 0, "last_settled_at": self.now.isoformat()}
        self._write(state)

        result = garden.care_chicken(None, "clean", now=self.now, path=self.path)
        self.assertEqual(result["units"], 0)
        raw = self._raw()
        self.assertEqual(raw["compost"]["batches"], [])

    def test_clean_also_settles_matured_compost(self):
        """打扫走 _settle_coop_byproducts 成对结算：堆肥角里已到期的批次当场
        转成肥料，不用等下一次无关的结算入口（code-review：三处手抄结算
        清单的漂移风险，统一入口后的行为在这里锁定）。"""
        state = _built_coop_state(
            self.now, chicks=[_adult_chick("hen-1", sex="hen", now=self.now)],
        )
        state["coop"]["manure"] = {"units": 2, "last_settled_at": self.now.isoformat()}
        state["compost"]["batches"] = [{
            "batch_id": "b1", "units": 3,
            "ready_at": (self.now - timedelta(hours=1)).isoformat(),
        }]
        self._write(state)

        result = garden.care_chicken(None, "clean", now=self.now, path=self.path)
        self.assertEqual(result["units"], 2)
        raw = self._raw()
        self.assertEqual(raw["inventory"]["fertilizer"]["fertilizer"], 3)
        batches = raw["compost"]["batches"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["units"], 2)

    def test_clean_coop_requires_built_coop(self):
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [garden._empty_plot(pid) for pid in garden._PLOT_IDS]
        state["meta"]["crop_seed_box_initialized"] = True
        self._write(state)

        with self.assertRaises(garden.GardenError):
            garden.care_chicken(None, "clean", now=self.now, path=self.path)
        # 拒绝分支不改任何状态。
        raw = self._raw()
        self.assertEqual(raw["coop"]["built"], False)


# ───────────────────────── 堆肥发酵到期 ─────────────────────────


def _empty_coop_plots_state(now):
    """堆肥测试不需要鸡舍建好——鸡舍留 unbuilt 默认态即可通过校验。"""
    state = garden._empty_state()
    state.pop("entries")
    state["plots"] = [garden._empty_plot(pid) for pid in garden._PLOT_IDS]
    state["meta"]["crop_seed_box_initialized"] = True
    return state


class CompostSettlementTests(_FileTestCase):
    def test_batch_matures_into_fertilizer_inventory_and_queues_event_once(self):
        state = _empty_coop_plots_state(self.now)
        ready_at = self.now - timedelta(minutes=1)  # 已经到期
        state["compost"]["batches"] = [
            {"batch_id": "batch-1", "units": 3, "ready_at": ready_at.isoformat()},
        ]
        self._write(state)

        # 两次结算（模拟同一批次被结算链撞见两次），事件只应该排一条。
        garden.crop_snapshot(now=self.now, path=self.path)
        garden.crop_snapshot(now=self.now, path=self.path)

        raw = self._raw()
        self.assertEqual(raw["compost"]["batches"], [])
        self.assertEqual(raw["inventory"]["fertilizer"]["fertilizer"], 3)
        compost_events = [
            e for e in raw["pending_events"] if e.get("type") == "compost_ready"
        ]
        self.assertEqual(len(compost_events), 1)
        self.assertEqual(compost_events[0]["units"], 3)
        self.assertTrue(compost_events[0]["event_id"].startswith("compost-ready:batch-1"))

    def test_batch_not_yet_ready_stays_pending(self):
        state = _empty_coop_plots_state(self.now)
        ready_at = self.now + timedelta(hours=1)  # 还没到期
        state["compost"]["batches"] = [
            {"batch_id": "batch-2", "units": 2, "ready_at": ready_at.isoformat()},
        ]
        self._write(state)

        garden.crop_snapshot(now=self.now, path=self.path)

        raw = self._raw()
        self.assertEqual(len(raw["compost"]["batches"]), 1)
        self.assertEqual(raw["inventory"]["fertilizer"].get("fertilizer", 0), 0)
        self.assertFalse(
            any(e.get("type") == "compost_ready" for e in raw["pending_events"]),
        )


# ───────────────────────── 施肥：治缺肥 ─────────────────────────


class FertilizeConditionTests(_FileTestCase):
    def _state_with_deficiency(self):
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [garden._empty_plot(pid) for pid in garden._PLOT_IDS]
        state["meta"]["crop_seed_box_initialized"] = True
        plot = _growing_plot("p1", now=self.now)
        state["plots"][0] = plot
        garden._create_crop_condition(
            state, plot, "nutrient_deficiency", self.now, announced=True,
        )
        return state

    def test_fertilize_resolves_deficiency_and_consumes_one_fertilizer(self):
        state = self._state_with_deficiency()
        state["inventory"]["fertilizer"]["fertilizer"] = 2
        self._write(state)

        result = garden.fertilize_plot("p1", now=self.now, path=self.path)
        self.assertEqual(result["kind"], "condition")
        self.assertEqual(result["outcome"], "resolved")
        self.assertEqual(result["condition_type"], "nutrient_deficiency")

        raw = self._raw()
        self.assertEqual(raw["inventory"]["fertilizer"]["fertilizer"], 1)
        self.assertEqual(raw["plots"][0]["condition"]["status"], "resolved")

    def test_fertilize_without_fertilizer_raises_and_changes_nothing(self):
        state = self._state_with_deficiency()
        state["inventory"]["fertilizer"]["fertilizer"] = 0
        self._write(state)
        before = self._raw()

        with self.assertRaisesRegex(garden.GardenError, "需要肥料"):
            garden.fertilize_plot("p1", now=self.now, path=self.path)

        after = self._raw()
        # 拒绝分支绝不改任何状态：条件仍是 active，库存分文不动。
        self.assertEqual(after["plots"][0]["condition"]["status"], "active")
        self.assertEqual(
            after["inventory"]["fertilizer"], before["inventory"]["fertilizer"],
        )


# ─────────────────────── 施肥：品质救援四条分流 ───────────────────────


class FertilizeQualityRescueTests(_FileTestCase):
    def _migrate_to_v5_growing_plot(self):
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [garden._empty_plot(pid) for pid in garden._PLOT_IDS]
        state["meta"]["crop_seed_box_initialized"] = True
        state["plots"][0] = _growing_plot("p1", now=self.now)
        self._write(state)
        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            garden.crop_snapshot(now=self.now, path=self.path)

    def _set_plot_quality(self, *, quality, quality_cause, flood_since=None):
        raw = self._raw()
        raw["plots"][0]["quality"] = quality
        raw["plots"][0]["quality_cause"] = quality_cause
        raw["environment"]["flood_watch"] = {
            "since": flood_since.isoformat() if flood_since else None,
            "drained_at": None if flood_since else self.now.isoformat(),
        }
        # _settle_flood_damage 每次结算都会按 yard_water_announced 的档位
        # 重新推导 flood_watch.since（档位不是 "flooded" 就会把 since 清回
        # None）——必须让游标口径跟上面手写的 flood_watch 一致，否则
        # fertilize_plot 内部先跑一次 _settle_crops 就会把手写的 since 冲掉。
        raw["environment"]["yard_water_announced"] = {
            "level": "flooded" if flood_since else "none",
            "at": self.now.isoformat(),
        }
        raw["inventory"]["fertilizer"] = {"fertilizer": 2}
        self.path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_rescue_succeeds_after_drain(self):
        self._migrate_to_v5_growing_plot()
        self._set_plot_quality(quality="poor", quality_cause="flood", flood_since=None)

        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            result = garden.fertilize_plot("p1", now=self.now, path=self.path)
        self.assertEqual(result["kind"], "quality_rescue")

        raw = self._raw()
        self.assertIsNone(raw["plots"][0]["quality"])
        self.assertIsNone(raw["plots"][0]["quality_cause"])
        self.assertEqual(raw["inventory"]["fertilizer"]["fertilizer"], 1)
        self.assertEqual(len(raw["journal"]["fertilizer_rescues"]), 1)

    def test_rescue_rejected_while_still_soaking_and_spends_no_fertilizer(self):
        self._migrate_to_v5_growing_plot()
        self._set_plot_quality(
            quality="poor", quality_cause="flood", flood_since=self.now - timedelta(hours=1),
        )
        before = self._raw()

        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}), \
                self.assertRaisesRegex(garden.GardenError, "地还泡着"):
            garden.fertilize_plot("p1", now=self.now, path=self.path)

        after = self._raw()
        self.assertEqual(after["plots"][0]["quality"], "poor")
        self.assertEqual(after["plots"][0]["quality_cause"], "flood")
        self.assertEqual(
            after["inventory"]["fertilizer"], before["inventory"]["fertilizer"],
        )

    def test_condition_caused_poor_quality_is_not_rescuable_and_spends_no_fertilizer(self):
        self._migrate_to_v5_growing_plot()
        self._set_plot_quality(quality="poor", quality_cause="condition", flood_since=None)
        before = self._raw()

        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            result = garden.fertilize_plot("p1", now=self.now, path=self.path)
        self.assertEqual(result["kind"], "quality_unrecoverable")

        after = self._raw()
        # 不是错误，是正常结果——但同样不扣肥料、不改品质标记。
        self.assertEqual(after["plots"][0]["quality"], "poor")
        self.assertEqual(after["plots"][0]["quality_cause"], "condition")
        self.assertEqual(
            after["inventory"]["fertilizer"], before["inventory"]["fertilizer"],
        )

    def test_legacy_poor_quality_without_cause_is_not_rescuable(self):
        """存量旧档没有 quality_cause 字段 → 读出来是 None，按人祸口径处理
        （不可救），不是"因为没记录成因就默认可救"（设计稿第一节第2点）。"""
        self._migrate_to_v5_growing_plot()
        raw = self._raw()
        raw["plots"][0]["quality"] = "poor"
        raw["plots"][0].pop("quality_cause", None)  # 模拟真正的旧档：字段整个不存在
        raw["environment"]["flood_watch"] = {"since": None, "drained_at": self.now.isoformat()}
        raw["inventory"]["fertilizer"] = {"fertilizer": 2}
        self.path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        before = self._raw()

        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}):
            result = garden.fertilize_plot("p1", now=self.now, path=self.path)
        self.assertEqual(result["kind"], "quality_unrecoverable")
        after = self._raw()
        self.assertEqual(
            after["inventory"]["fertilizer"], before["inventory"]["fertilizer"],
        )

    def test_selectorless_fertilize_targets_the_only_rescuable_plot(self):
        """院子里多块地健康、只有一块内涝欠佳时，不带编号的"施肥"应当自动
        选中那块可救的地，而不是报"不止一个可处理菜畦"——code-review 实锤：
        第一版收窄谓词只认 active 异常，救品质路径在多作物院子里永远选不中。"""
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [garden._empty_plot(pid) for pid in garden._PLOT_IDS]
        state["meta"]["crop_seed_box_initialized"] = True
        state["plots"][0] = _growing_plot("p1", now=self.now)
        state["plots"][1] = _growing_plot("p2", now=self.now)
        state["plots"][0]["quality"] = "poor"
        state["plots"][0]["quality_cause"] = "flood"
        state["inventory"]["fertilizer"] = {"fertilizer": 1}
        self._write(state)

        result = garden.fertilize_plot(None, now=self.now, path=self.path)
        self.assertEqual(result["kind"], "quality_rescue")
        self.assertEqual(result["plot_id"], "p1")
        raw = self._raw()
        self.assertIsNone(raw["plots"][0]["quality"])
        # 扣到 0 时 _inventory_take 会把键整个移除（与其他分区一致）。
        self.assertEqual(raw["inventory"]["fertilizer"].get("fertilizer", 0), 0)

    def test_plot_with_no_issue_is_rejected_and_spends_no_fertilizer(self):
        self._migrate_to_v5_growing_plot()
        raw = self._raw()
        raw["inventory"]["fertilizer"] = {"fertilizer": 2}
        self.path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        before = self._raw()

        with patch.dict(os.environ, {"GARDEN_REAL_ENVIRONMENT_ENABLED": "1"}), \
                self.assertRaisesRegex(garden.GardenError, "用不上肥料"):
            garden.fertilize_plot("p1", now=self.now, path=self.path)

        after = self._raw()
        self.assertEqual(
            after["inventory"]["fertilizer"], before["inventory"]["fertilizer"],
        )


# ───────── 监理复核补测：其他异常在场时施肥要指路，不误判救不回 ─────────


class FertilizeOtherConditionTests(_FileTestCase):
    def _state_with_condition(self, condition_type, *, announced=True):
        state = garden._empty_state()
        state.pop("entries")
        state["plots"] = [garden._empty_plot(pid) for pid in garden._PLOT_IDS]
        state["meta"]["crop_seed_box_initialized"] = True
        plot = _growing_plot("p1", now=self.now)
        state["plots"][0] = plot
        garden._create_crop_condition(
            state, plot, condition_type, self.now, announced=announced,
        )
        state["inventory"]["fertilizer"] = {"fertilizer": 2}
        return state, plot

    def test_fertilize_on_other_condition_points_to_correct_action(self):
        state, _ = self._state_with_condition("pest")
        self._write(state)
        before = self._raw()

        with self.assertRaisesRegex(garden.GardenError, "除虫"):
            garden.fertilize_plot("p1", now=self.now, path=self.path)

        after = self._raw()
        # 拒绝分支不产生任何收益或惩罚：异常仍 active，肥料分文不动
        # （"已展示"确认是唯一的合法状态变化，见下一条测试）。
        self.assertEqual(after["plots"][0]["condition"]["status"], "active")
        self.assertEqual(
            after["inventory"]["fertilizer"], before["inventory"]["fertilizer"],
        )

    def test_fertilize_on_other_condition_counts_as_reveal(self):
        """报错文案把异常点破给了 agent → 按"任一可靠可见结果共用同一确认
        事实"的既有原则（_acknowledge_condition_shown docstring，
        resolve_crop_condition 的 wrong_action 分支同款），必须记一次
        "已展示"：恶化时钟从真实揭示时刻起算，事件也不会稍后重复再报。"""
        state, _ = self._state_with_condition("pest", announced=False)
        self._write(state)

        with self.assertRaisesRegex(garden.GardenError, "除虫"):
            garden.fertilize_plot("p1", now=self.now, path=self.path)

        after = self._raw()
        condition = after["plots"][0]["condition"]
        self.assertEqual(condition["status"], "active")
        self.assertIsNotNone(condition["announced_at"])

    def test_flood_poor_plot_with_other_condition_is_not_misjudged_unrecoverable(self):
        """内涝欠佳 + 恰好又闹别的异常：必须指路先处理异常，绝不能落进
        "救不回"分支——那句"品相已经定了"对天灾成因的地块是错误事实
        （处理完异常、退水后它仍然可救，quality_cause 必须原样保留）。"""
        state, plot = self._state_with_condition("pest")
        plot["quality"] = "poor"
        plot["quality_cause"] = "flood"
        self._write(state)
        before = self._raw()

        with self.assertRaisesRegex(garden.GardenError, "除虫"):
            garden.fertilize_plot("p1", now=self.now, path=self.path)

        after = self._raw()
        self.assertEqual(after["plots"][0]["quality"], "poor")
        self.assertEqual(after["plots"][0]["quality_cause"], "flood")
        self.assertEqual(
            after["inventory"]["fertilizer"], before["inventory"]["fertilizer"],
        )


# ───────── 监理复核补测：打扫鸡舍的语义路由（home 侧确定性收窄） ─────────


class CoopCleanRoutingTests(unittest.TestCase):
    def test_manure_phrases_route_to_coop_clean(self):
        import home
        for raw in ("清理粪便", "打扫粑粑", "铲屎", "清粪", "打扫鸡舍", "清扫鸡窝", "清理团团的粑粑"):
            with self.subTest(raw=raw):
                self.assertEqual(home._garden_semantic_command(raw), "打扫鸡舍")

    def test_manure_mentions_without_clean_verb_do_not_hijack(self):
        """打扫是会改状态的动作，只有清扫类动词与粪类名词同现才路由过去：
        "看看鸡粪"是查询、"施粪肥/撒鸡粪"是施肥说法——统统不能被截胡成
        真的执行打扫（code-review 实锤的第一版漏洞：裸"粪"字命中就路由）。
        落进别的分支或报错都行，就是不能变成打扫。"""
        import home
        for raw in ("施粪肥", "给二号地撒粪肥", "看看鸡粪", "把鸡粪撒到一号地"):
            with self.subTest(raw=raw):
                try:
                    result = home._garden_semantic_command(raw)
                except home.HomeError:
                    continue  # 报错也算没被截胡——宁可报错不猜
                self.assertNotEqual(result, "打扫鸡舍")

    def test_ambiguous_coop_clear_asks_instead_of_guessing(self):
        import home
        with self.assertRaisesRegex(home.HomeError, "打扫鸡舍"):
            home._garden_semantic_command("清理鸡窝")


# ───────────────────────── 落盘再读不丢 ─────────────────────────


class PersistenceRoundTripTests(_FileTestCase):
    def test_manure_compost_fertilizer_quality_cause_survive_save_and_reload(self):
        state = _built_coop_state(self.now, chicks=[_adult_chick("hen-1", sex="hen", now=self.now)])
        state["coop"]["manure"] = {"units": 3, "last_settled_at": self.now.isoformat()}
        state["compost"]["batches"] = [
            {"batch_id": "batch-x", "units": 2, "ready_at": (self.now + timedelta(hours=10)).isoformat()},
        ]
        state["inventory"]["fertilizer"]["fertilizer"] = 5
        plot = _growing_plot("p1", now=self.now)
        plot["quality"] = "poor"
        plot["quality_cause"] = "flood"
        state["plots"][0] = plot
        self._write(state)

        reloaded = garden._read_state_unlocked(self.path, now=self.now)
        self.assertEqual(reloaded["coop"]["manure"], {"units": 3, "last_settled_at": self.now.isoformat()})
        self.assertEqual(len(reloaded["compost"]["batches"]), 1)
        self.assertEqual(reloaded["compost"]["batches"][0]["units"], 2)
        self.assertEqual(reloaded["inventory"]["fertilizer"]["fertilizer"], 5)
        self.assertEqual(reloaded["plots"][0]["quality"], "poor")
        self.assertEqual(reloaded["plots"][0]["quality_cause"], "flood")

        # 再走一次完整写回，确认二次落盘同样不丢（防止只有内存态正确、
        # 落盘反而漏字段的假象）。
        garden._write_state_unlocked(reloaded, self.path)
        twice_reloaded = garden._read_state_unlocked(self.path, now=self.now)
        self.assertEqual(twice_reloaded["coop"]["manure"]["units"], 3)
        self.assertEqual(len(twice_reloaded["compost"]["batches"]), 1)
        self.assertEqual(twice_reloaded["inventory"]["fertilizer"]["fertilizer"], 5)
        self.assertEqual(twice_reloaded["plots"][0]["quality_cause"], "flood")


# ───────────────────────── 旧档迁移幂等 ─────────────────────────


class MigrationIdempotenceTests(_FileTestCase):
    def test_legacy_state_missing_v8_fields_backfills_and_is_idempotent(self):
        state = _built_coop_state(self.now, chicks=[_adult_chick("hen-1", sex="hen", now=self.now)])
        payload = {k: v for k, v in state.items() if k not in ("entries", "_migrated")}
        # 模拟真正的旧档：整个不存在这些第八版字段。
        payload.pop("compost", None)
        payload["coop"].pop("manure", None)
        payload["inventory"].pop("fertilizer", None)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        first = garden._read_state_unlocked(self.path, now=self.now)
        self.assertEqual(first["coop"]["manure"], {"units": 0, "last_settled_at": None})
        self.assertEqual(first["compost"], {"batches": []})
        self.assertEqual(first["inventory"]["fertilizer"], {})

        # 通过真实结算入口落盘一次，再落盘一次，两次之间除了鸡粪结算游标
        # 本身会往前走（时间流逝的正常结果）之外，不应该发生第二次"迁移"。
        garden.crop_snapshot(now=self.now, path=self.path)
        after_first_settle = self._raw()
        garden.crop_snapshot(now=self.now, path=self.path)
        after_second_settle = self._raw()
        self.assertEqual(
            after_first_settle["coop"]["manure"]["units"],
            after_second_settle["coop"]["manure"]["units"],
        )
        self.assertEqual(after_first_settle["compost"], after_second_settle["compost"])
        self.assertEqual(
            after_first_settle["inventory"]["fertilizer"],
            after_second_settle["inventory"]["fertilizer"],
        )
        # 既有数据（小鸡记录）没有被迁移过程动过。
        self.assertEqual(
            [c["id"] for c in after_second_settle["coop"]["chicks"]], ["hen-1"],
        )


if __name__ == "__main__":
    unittest.main()
