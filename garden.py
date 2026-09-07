"""迷你小院子：现实小院里的城市小动物、旧观赏花草与连续事件。

agent 偶尔经过院门、屋檐和菜畦时，这个模块让那趟溜达多一点生活气。是否搭理、
浇水、投喂或摸摸，全交给他自己判断。状态存成 garden.json
（同 reminders.json 一样不入库），状态全靠时间差现算，不额外启动定时任务。

DeepSeek 生成初见时的物种/外观、照顾后的单次反应，以及 ``home 院子 逛逛``
首次命中某个可见场景键时的整体画面；触发规则、状态结算与本地兜底都不交给
写手。daemon 会把可能访问网络的 run_tick 放进工作线程，外部 API 抖动不会
阻塞 asyncio 主循环。

所有函数的 path 参数默认 None 时才读取模块级 GARDEN_FILE——不能把
GARDEN_FILE 直接写成参数默认值，那样会在模块导入时把路径锁死在函数签名
里，测试里 patch.object(garden, "GARDEN_FILE", tmp) 不会生效。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timedelta
from math import isfinite
from pathlib import Path
from zoneinfo import ZoneInfo

import garden_content
import garden_crops
import garden_generator
import garden_scene
import garden_weather
import log_store
from garden_calendar import DEFAULT_CALENDAR
from garden_generator import GardenGeneratorError

TZ = ZoneInfo("Asia/Shanghai")
GARDEN_FILE = Path(os.environ.get("GARDEN_FILE", Path(__file__).parent / "garden.json"))
GARDEN_SESSION_INTRO_FILE = Path(
    os.environ.get(
        "GARDEN_SESSION_INTRO_FILE",
        Path(__file__).parent / "garden_session_intros.json",
    )
)
GARDEN_VIEW_STATE_FILE = Path(
    os.environ.get(
        "GARDEN_VIEW_STATE_FILE",
        Path(__file__).parent / "garden_view_state.json",
    )
)

GARDEN_SESSION_INTRO = (
    "欢迎来到你的小院子。这里的晴雨冷暖，会跟着user 所在的地方一起变化。"
    "院里有四块菜畦，可以种下当季作物，也有一些什么时候都能播种的种子；"
    "等它们成熟以后，还能做成食物送给别人，也可以随礼送上温暖的便签。"
    "作物偶尔会生病或招虫，记得去看看、"
    "照料一下。也会有小动物来到院子里，可以摸摸它们，或者留一点吃的。"
    "鸡舍里的鸡每天会攒下鸡粪，偶尔打扫一下，沤出来的肥料关键时候能派上用场。"
    "闲下来的时候，就来院子里逛一圈吧——看看今天有什么正在悄悄发生。"
)

MAX_ACTIVE = 3

# 第三版里，照顾从不决定生命是否健康、是否离开或是否还值得回来。访客只是按
# 自己的自然节奏出去转转；该计划只影响可见位置，不扣亲密、不删除记录。
NATURAL_VISIT_MIN_HOURS = 72
NATURAL_VISIT_MAX_HOURS = 120
NATURAL_RETURN_MIN_HOURS = 12
NATURAL_RETURN_MAX_HOURS = 48

# agent 实际约每 30–60 分钟就可能自由唤醒一次，因此直接给“生成新生命”22%
# 会在一天内迅速塞满院子。下面是每次唤醒的完整骰面；条件不满足时不改投。
DAY_EVENT_ROLLS = (
    ("none", 0.82),
    ("revisit", 0.90),
    ("trace", 0.95),
    ("return", 0.98),
    ("spawn", 1.0),
)
# 夜间这一档是整晚唯一保底的一次，不允许真的"无事"：build_event 会把骰到
# "none"的情况改判成尝试新生命，新生命条件不满足时再降级成痕迹，因此这里的
# "none"只是骰面占位、不是"什么都不发生"的最终结果。
NIGHT_EVENT_ROLLS = (
    ("none", 0.50),
    ("revisit", 0.75),
    ("trace", 0.90),
    ("return", 0.97),
    ("spawn", 1.0),
)
VISIBLE_EVENT_COOLDOWN_HOURS = 3
PENDING_DELIVERY_LEASE_SECONDS = 5 * 60
NEW_LIFE_COOLDOWN_HOURS = 36
COOP_EGG_COUNT = 3
COOP_INCUBATION_DURATION = timedelta(hours=21)
COOP_PROGRESS_AFTER = (
    ("warming", timedelta(hours=7)),
    ("tapping", timedelta(hours=14)),
    ("hatched", COOP_INCUBATION_DURATION),
)
CHICK_MATURITY_DURATION = timedelta(hours=12)
HEN_FIRST_EGG_AFTER_MATURITY = timedelta(hours=6)
HEN_EGG_INTERVAL = timedelta(hours=18)
HEN_EGG_LAY_PROBABILITY = 0.2
HATCHABLE_EGG_PROBABILITY = 0.015
ROOSTER_CROW_START_HOUR = 5
ROOSTER_CROW_END_HOUR = 9
ANIMAL_PRODUCT_NAMES = {"egg": "鸡蛋", "hatchable_egg": "可孵化鸡蛋"}
NIGHT_START_HOUR = 2
NIGHT_END_HOUR = 10
# 第八版：施肥系统。鸡粪堆积上限——到上限后结算不再入账时间，直接把
# last_settled_at 推到 now，防止"攒满不扫、一扫瞬间又满"的时间银行
# （设计稿第一节）。第九版改口径：堆肥固定发酵 72 小时，6 份鸡粪沤成
# 1 份肥料（设计稿第九版第一节）。
MANURE_CAP = 6
MANURE_PER_FERTILIZER = 6
COMPOST_READY_AFTER = timedelta(hours=72)
# 第九版：追肥——生长中、无异常的作物每次消耗肥料×1，把剩余生长时间
# 砍掉一成；同一茬最多追 5 次（设计稿第九版第一节）。
FERTILIZE_BOOST_RATIO = 0.10
FERTILIZE_MAX_PER_CYCLE = 5
# 菜畦接管种植后停止生成新的 v2 式观赏花草；已有旧花草仍完整保留、可查看和
# 使用旧命令，不让旧入口继续绕过菜畦容量规则。
LEGACY_FLOWER_SPAWN_ENABLED = False

# 第四版阶段 C 只铺设异常状态机骨架。生产默认必须关闭自然异常，等阶段 D
# 的全部恢复动作齐备并完成联调后，才由明确配置决定是否开放。daemon 在模块
# import 后才 load_dotenv，因此这个开关必须在每次入口内惰性读取。
NATURAL_CONDITION_ROLL_PROBABILITY = 0.10
NATURAL_CONDITION_COOLDOWN = timedelta(hours=48)
CONDITION_DAMAGE_AFTER = timedelta(hours=24)
CONDITION_WITHER_AFTER = timedelta(hours=36)
YARD_WATER_EVENT_COOLDOWN = timedelta(hours=2)
# 第七版阶段 C：排水动作自身的频控——距上次 drained_at 不足这个时长就
# 拒绝，且拒绝分支不改写时钟（设计稿第五节第 2.5 点）。跟内涝通知游标的
# 2 小时是同一个数量级但语义独立，各自维护各自的常量。
YARD_DRAIN_COOLDOWN = timedelta(hours=2)
WATERING_HISTORY_DAYS = 14
CONDITION_TYPES = ("pest", "diseased_leaf", "waterlogged", "nutrient_deficiency", "flood_rot")
# 第七版阶段 A：泡烂只由内涝结算直接创建为终态（见设计稿第四节第4点），
# 绝不参与每日自然异常的随机命中；候选池只从这张子集里挑，不用
# `CONDITION_TYPES` 本身，防止它被漏改地混进权重与随机选择。
NATURAL_CONDITION_TYPES = tuple(
    condition_type for condition_type in CONDITION_TYPES if condition_type != "flood_rot"
)
CONDITION_SEVERITIES = ("warning", "damaged", "withered")
CONDITION_LABELS = {
    "pest": "虫害",
    "diseased_leaf": "病叶",
    "waterlogged": "积水",
    "nutrient_deficiency": "缺肥",
    "flood_rot": "泡烂",
}
CONDITION_ACTIONS = {
    "pest": "除虫",
    "diseased_leaf": "修剪",
    "waterlogged": "松土",
    "nutrient_deficiency": "施肥",
    # flood_rot 故意不配处理动作：它只以终态出现，处理入口是「清理」，
    # 走既有 clear_withered_crop 命令，不经过 resolve_crop_condition。
}


def natural_crop_conditions_enabled() -> bool:
    return os.environ.get(
        "GARDEN_NATURAL_CROP_CONDITIONS_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


SPOTS = ("院门与信箱旁", "屋檐下与墙根")
_LEGACY_SPOT_MAP = {"床头柜": "屋檐下与墙根", "信箱": "院门与信箱旁"}

_ACTIONS_BY_KIND = {"flower": ("浇水",), "animal": ("投喂", "摸摸", "陪玩")}
_MEASURE_WORD = {"flower": "株", "animal": "只"}

# 所有动物共用这一张亲密门槛表。性格只决定相处时的表达，绝不改变升级速度。
BOND_LEVELS = (
    (0, 0, "初见"),
    (1, 3, "记得你"),
    (2, 8, "愿意靠近"),
    (3, 16, "信任"),
    (4, 28, "常来坐坐"),
    (5, 45, "把这里当家"),
)
DAILY_BOND_POINT_CAP = 3
_BOND_ACTION_HISTORY_DAYS = 14

FLOWER_SPECIES = garden_content.FLOWER_SPECIES
ANIMAL_SPECIES = garden_content.ANIMAL_SPECIES
_FALLBACK_ENCOUNTER = garden_content.FALLBACK_ENCOUNTER
_FALLBACK_REACTION = garden_content.FALLBACK_REACTION
_FALLBACK_REVIVED_REACTION = garden_content.FALLBACK_REVIVED_REACTION


class GardenError(Exception):
    pass


LEGACY_STATE_VERSION = 4
STATE_VERSION = 5
WATERING_RESULT_HISTORY_DAYS = 14
WATERING_RESULT_HISTORY_LIMIT = 32
TIMING_PROJECTION_MAX_DAYS = 800
TIMING_SCHEMA_VERSION = 1
SEASON_EXIT_SCAN_MAX_DAYS = 200
SEASON_EXIT_WARNING_DAYS = 7


def _empty_coop() -> dict:
    return {
        "built": False,
        "built_at": None,
        "story_status": "unbuilt",
        "source_animal_id": None,
        "source_animal_name": None,
        "choice_resolved_at": None,
        "clutch_id": None,
        "incubation_started_at": None,
        "hatch_at": None,
        "incubating_egg_count": 0,
        "progress_queued": [],
        "chicks": [],
        "last_crow_date": None,
        "total_eggs_laid": 0,
        # 第八版：鸡粪堆积，按在场成鸡数量线性结算（见 _settle_coop_manure）。
        "manure": {"units": 0, "last_settled_at": None},
    }


def _empty_state() -> dict:
    return {
        # 总开关关闭时，新旧入口仍保持 v4；首次启用才在同一把院子锁内迁 v5。
        "version": LEGACY_STATE_VERSION,
        "animals": [],
        "legacy_plants": [],
        "plots": [],
        "inventory": {
            "seeds": {}, "warehouse_seeds": {}, "produce": {}, "produce_poor": {},
            "prepared_food": {}, "animal_products": {}, "keepsakes": [],
            # 第八版：肥料，跟其余库存分区一样是 item_id -> 数量的 dict，
            # 只有 "fertilizer" 这一个 item_id；不设上限（跟篮子鸡蛋一致）。
            "fertilizer": {},
        },
        "coop": _empty_coop(),
        "journal": {
            "species_seen": [], "crops_harvested": [], "meals_made": [], "gifts_given": [],
            "bond_milestones": [], "calendar_moments": [], "crop_incidents": [],
            "legacy_plants": [], "drainage": [], "fertilizer_rescues": [],
        },
        # 第八版：堆肥角。打扫鸡舍生成一个批次，第九版起 72 小时后发酵好
        # 自动转成肥料入库（见 _settle_compost）。
        "compost": {"batches": []},
        "pending_events": [],
        "meta": {
            "last_settled_at": None,
            "last_visible_event_at": None,
            "last_new_life_at": None,
            "night_date": None,
            "night_target_at": None,
            "night_attempted": False,
            "last_natural_condition_roll_date": None,
            "natural_condition_cooldown_until": None,
        },
        "legacy": {"unrecognized_entries": [], "unrecognized_top_level": {}},
        "environment": None,
        # 作物生长记账口径：""=整天记账（跨入当天即一次性入账整天生长点，
        # 2026-08-18 前的旧口径），"linear"=按天内时间线性累积。默认值必须
        # 是字符串——上面 8/13 的教训之外，_normalize_v4_state 的逐键搬运
        # 是"按 type(default) 保留"，默认 None 会把磁盘上的真实值也丢掉。
        "growth_accounting": "",
        # 仅供旧命令调用层使用的内存投影；绝不写回 v4 文件。
        "entries": [],
    }


def real_environment_enabled() -> bool:
    """第五版总开关；关闭时绝不迁移或改变第四版浇水玩法。"""
    return os.environ.get("GARDEN_REAL_ENVIRONMENT_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


_PLOT_IDS = ("p1", "p2", "p3", "p4")
_PLOT_LABELS = {"p1": "一号", "p2": "二号", "p3": "三号", "p4": "四号"}
_PLOT_SELECTOR_ALIASES = {
    "1": "p1", "p1": "p1", "一": "p1", "1号": "p1", "一号": "p1",
    "一号菜畦": "p1", "1号菜畦": "p1", "一号地": "p1", "1号地": "p1",
    "一号地块": "p1", "1号地块": "p1",
    "2": "p2", "p2": "p2", "二": "p2", "2号": "p2", "二号": "p2",
    "二号菜畦": "p2", "2号菜畦": "p2", "二号地": "p2", "2号地": "p2",
    "二号地块": "p2", "2号地块": "p2",
    "3": "p3", "p3": "p3", "三": "p3", "3号": "p3", "三号": "p3",
    "三号菜畦": "p3", "3号菜畦": "p3", "三号地": "p3", "3号地": "p3",
    "三号地块": "p3", "3号地块": "p3",
    "4": "p4", "p4": "p4", "四": "p4", "4号": "p4", "四号": "p4",
    "四号菜畦": "p4", "4号菜畦": "p4", "四号地": "p4", "4号地": "p4",
    "四号地块": "p4", "4号地块": "p4",
}
_CROP_STAGE_ORDER = ("seed", "sprout", "growing", "ready")
WEATHER_CONTEXT_MAX_AGE = timedelta(hours=2)
OUTING_FLAVOR_COOLDOWN = timedelta(minutes=30)


def _empty_plot(plot_id: str) -> dict:
    return {"plot_id": plot_id, "status": "empty"}


def plot_label(plot_id: str) -> str:
    try:
        return _PLOT_LABELS[plot_id]
    except KeyError as exc:
        raise GardenError("小院子菜畦编号格式损坏，已停止写入以免覆盖原记录") from exc


def _date_at_start(day: date) -> datetime:
    # 每一现实日只在跨过北京时间午夜时结算一次；用该日的起点判定适种季，
    # 这样立夏/立秋当天白天的精确分界不会被“最终季节”倒灌回更早时段。
    return datetime.combine(day, dtime(0), tzinfo=TZ)


def _season_on(day: date) -> str:
    return calendar_context(_date_at_start(day)).season


def _parse_iso(value: object, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise GardenError(f"小院子{field}格式损坏，已停止写入以免覆盖原记录") from exc
    if parsed.tzinfo is None:
        raise GardenError(f"小院子{field}缺少时区，已停止写入以免覆盖原记录")
    return parsed.astimezone(TZ)


def _validate_inventory(inventory: dict) -> None:
    for section in (
        "seeds", "warehouse_seeds", "produce", "produce_poor",
        "prepared_food", "animal_products", "fertilizer",
    ):
        value = inventory.get(section, {})
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or isinstance(amount, bool) or not isinstance(amount, int) or amount < 0
            for key, amount in value.items()
        ):
            raise GardenError("小院子库存格式损坏，已停止写入以免覆盖原记录")
    if not isinstance(inventory.get("keepsakes", []), list):
        raise GardenError("小院子库存格式损坏，已停止写入以免覆盖原记录")


def _validate_coop(coop: object) -> None:
    if not isinstance(coop, dict) or not isinstance(coop.get("built"), bool):
        raise GardenError("小院子鸡舍状态格式损坏，已停止写入以免覆盖原记录")
    status = coop.get("story_status")
    if status not in {"unbuilt", "awaiting_choice", "incubating", "eaten", "hatched"}:
        raise GardenError("小院子鸡舍故事状态损坏，已停止写入以免覆盖原记录")
    if coop["built"] != (status in {"incubating", "hatched"}):
        raise GardenError("小院子鸡舍建造状态矛盾，已停止写入以免覆盖原记录")
    for key in ("built_at", "choice_resolved_at", "incubation_started_at", "hatch_at"):
        if coop.get(key) is not None:
            _parse_iso(coop[key], field="鸡舍时间")
    for key in ("source_animal_id", "source_animal_name", "clutch_id"):
        if coop.get(key) is not None and (not isinstance(coop[key], str) or not coop[key]):
            raise GardenError("小院子鸡舍故事字段损坏，已停止写入以免覆盖原记录")
    progress = coop.get("progress_queued")
    allowed_progress = {stage for stage, _after in COOP_PROGRESS_AFTER}
    if not isinstance(progress, list) or len(progress) != len(set(progress)) or any(
        stage not in allowed_progress for stage in progress
    ):
        raise GardenError("小院子孵蛋进度损坏，已停止写入以免覆盖原记录")
    last_crow_date = coop.get("last_crow_date")
    if last_crow_date is not None:
        try:
            date.fromisoformat(last_crow_date)
        except (TypeError, ValueError) as exc:
            raise GardenError("小院子公鸡打鸣日期损坏，已停止写入以免覆盖原记录") from exc
    total_eggs_laid = coop.get("total_eggs_laid")
    if isinstance(total_eggs_laid, bool) or not isinstance(total_eggs_laid, int) or total_eggs_laid < 0:
        raise GardenError("小院子母鸡下蛋记录损坏，已停止写入以免覆盖原记录")
    manure = coop.get("manure")
    if not isinstance(manure, dict):
        raise GardenError("小院子鸡粪记录损坏，已停止写入以免覆盖原记录")
    manure_units = manure.get("units")
    if isinstance(manure_units, bool) or not isinstance(manure_units, int) or not 0 <= manure_units <= MANURE_CAP:
        raise GardenError("小院子鸡粪数量损坏，已停止写入以免覆盖原记录")
    if manure.get("last_settled_at") is not None:
        _parse_iso(manure["last_settled_at"], field="鸡粪结算时间")
    incubating_egg_count = coop.get("incubating_egg_count")
    if (
        isinstance(incubating_egg_count, bool)
        or not isinstance(incubating_egg_count, int)
        or incubating_egg_count < 0
        or incubating_egg_count > COOP_EGG_COUNT
    ):
        raise GardenError("小院子孵蛋数量损坏，已停止写入以免覆盖原记录")
    chicks = coop.get("chicks")
    if not isinstance(chicks, list) or any(
        not isinstance(chick, dict)
        or not isinstance(chick.get("id"), str)
        or not chick["id"]
        or not isinstance(chick.get("hatched_at"), str)
        for chick in chicks
    ):
        raise GardenError("小院子小鸡记录损坏，已停止写入以免覆盖原记录")
    if len({chick["id"] for chick in chicks}) != len(chicks):
        raise GardenError("小院子小鸡编号重复，已停止写入以免覆盖原记录")
    for chick in chicks:
        hatched_at = _parse_iso(chick["hatched_at"], field="小鸡出生时间")
        if chick.get("sex") not in {"hen", "rooster"} or chick.get("stage") not in {"chick", "adult"}:
            raise GardenError("小院子小鸡性别或成长状态损坏，已停止写入以免覆盖原记录")
        matures_at = _parse_iso(chick.get("matures_at"), field="小鸡长大时间")
        if matures_at != hatched_at + CHICK_MATURITY_DURATION:
            raise GardenError("小院子小鸡成长时长不一致，已停止写入以免覆盖原记录")
        matured_at = chick.get("matured_at")
        if chick["stage"] == "adult":
            if _parse_iso(matured_at, field="小鸡长成时间") != matures_at:
                raise GardenError("小院子成鸡成长记录损坏，已停止写入以免覆盖原记录")
        elif matured_at is not None:
            raise GardenError("小院子幼鸡提前长成，已停止写入以免覆盖原记录")
        eggs_laid = chick.get("eggs_laid")
        if isinstance(eggs_laid, bool) or not isinstance(eggs_laid, int) or eggs_laid < 0:
            raise GardenError("小院子母鸡个体下蛋记录损坏，已停止写入以免覆盖原记录")
        if chick["sex"] == "hen":
            next_egg_at = _parse_iso(chick.get("next_egg_at"), field="母鸡下次产蛋时间")
            if next_egg_at < matures_at + HEN_FIRST_EGG_AFTER_MATURITY:
                raise GardenError("小院子母鸡产蛋时间损坏，已停止写入以免覆盖原记录")
        elif chick.get("next_egg_at") is not None or eggs_laid != 0:
            raise GardenError("小院子公鸡产蛋记录矛盾，已停止写入以免覆盖原记录")
        nickname = chick.get("nickname")
        if nickname is not None and (
            not isinstance(nickname, str) or not nickname.strip() or len(nickname) > 12
            or any(ord(char) < 32 for char in nickname)
        ):
            raise GardenError("小院子鸡昵称格式损坏，已停止写入以免覆盖原记录")
        for key in ("feed_count", "pet_count"):
            value = chick.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GardenError("小院子鸡照顾次数损坏，已停止写入以免覆盖原记录")
        for key in ("last_fed_at", "last_petted_at"):
            if chick.get(key) is not None:
                _parse_iso(chick[key], field="鸡照顾时间")
        for key, label, max_chars in (
            ("profile_id", "档案编号", 80),
            ("chick_appearance", "幼鸡外貌", 80),
            ("adult_appearance", "成鸡外貌", 80),
            ("personality", "性格", 16),
            ("intro", "出生描述", 160),
        ):
            value = chick.get(key)
            if (
                not isinstance(value, str) or not value.strip() or len(value) > max_chars
                or any(ord(char) < 32 and char not in "\n\t" for char in value)
            ):
                raise GardenError(f"小院子鸡{label}格式损坏，已停止写入以免覆盖原记录")
    profile_ids = [chick["profile_id"] for chick in chicks]
    if len(profile_ids) != len(set(profile_ids)):
        raise GardenError("小院子鸡外貌性格档案重复，已停止写入以免覆盖原记录")
    if status == "unbuilt" and any(
        coop.get(key) is not None for key in (
            "built_at", "source_animal_id", "source_animal_name", "choice_resolved_at",
            "clutch_id", "incubation_started_at", "hatch_at",
        )
    ):
        raise GardenError("小院子未建鸡舍却残留故事状态，已停止写入以免覆盖原记录")
    if status == "incubating":
        if not all(coop.get(key) for key in ("clutch_id", "incubation_started_at", "hatch_at")):
            raise GardenError("小院子孵蛋时间不完整，已停止写入以免覆盖原记录")
        started = _parse_iso(coop["incubation_started_at"], field="开始孵蛋时间")
        hatch_at = _parse_iso(coop["hatch_at"], field="预计孵化时间")
        if hatch_at != started + COOP_INCUBATION_DURATION:
            raise GardenError("小院子孵蛋时长不一致，已停止写入以免覆盖原记录")
        if incubating_egg_count not in (1, COOP_EGG_COUNT):
            raise GardenError("小院子孵蛋数量与状态不一致，已停止写入以免覆盖原记录")
    elif incubating_egg_count != 0:
        raise GardenError("小院子未在孵蛋却残留孵蛋数量，已停止写入以免覆盖原记录")
    if status == "hatched" and (not chicks or "hatched" not in progress):
        raise GardenError("小院子小鸡孵化记录不完整，已停止写入以免覆盖原记录")
    if status in {"unbuilt", "awaiting_choice", "eaten"} and chicks:
        raise GardenError("小院子尚未孵化却已有小鸡记录，已停止写入以免覆盖原记录")
    if sum(chick.get("eggs_laid", 0) for chick in chicks) != total_eggs_laid:
        raise GardenError("小院子母鸡总产蛋数不一致，已停止写入以免覆盖原记录")


def _validate_compost(state: dict) -> None:
    compost = state.get("compost")
    if not isinstance(compost, dict):
        raise GardenError("小院子堆肥角格式损坏，已停止写入以免覆盖原记录")
    batches = compost.get("batches")
    if not isinstance(batches, list):
        raise GardenError("小院子堆肥批次格式损坏，已停止写入以免覆盖原记录")
    batch_ids = []
    for batch in batches:
        if not isinstance(batch, dict):
            raise GardenError("小院子堆肥批次格式损坏，已停止写入以免覆盖原记录")
        batch_id = batch.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            raise GardenError("小院子堆肥批次编号损坏，已停止写入以免覆盖原记录")
        batch_ids.append(batch_id)
        units = batch.get("units")
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            raise GardenError("小院子堆肥批次份数损坏，已停止写入以免覆盖原记录")
        manure_units = batch.get("manure_units")
        if isinstance(manure_units, bool) or not isinstance(manure_units, int) or manure_units <= 0:
            raise GardenError("小院子堆肥批次鸡粪份数损坏，已停止写入以免覆盖原记录")
        _parse_iso(batch.get("ready_at"), field="堆肥批次到期时间")
    if len(batch_ids) != len(set(batch_ids)):
        raise GardenError("小院子堆肥批次编号重复，已停止写入以免覆盖原记录")


def _validate_condition_timestamp(condition: dict, key: str, *, required: bool = False) -> datetime | None:
    value = condition.get(key)
    if value is None and not required:
        return None
    return _parse_iso(value, field="作物异常时间")


def _validate_crop_condition(plot: dict) -> None:
    condition = plot.get("condition")
    if condition is None:
        return
    if not isinstance(condition, dict):
        raise GardenError("小院子作物异常格式损坏，已停止写入以免覆盖原记录")
    text_fields = ("condition_id", "type", "status", "severity")
    if any(not isinstance(condition.get(key), str) or not condition[key] for key in text_fields):
        raise GardenError("小院子作物异常字段不完整，已停止写入以免覆盖原记录")
    if condition["type"] not in CONDITION_TYPES:
        raise GardenError("小院子作物异常类型无效，已停止写入以免覆盖原记录")
    if condition["status"] not in ("active", "resolved", "failed"):
        raise GardenError("小院子作物异常状态无效，已停止写入以免覆盖原记录")
    if condition["severity"] not in CONDITION_SEVERITIES:
        raise GardenError("小院子作物异常程度无效，已停止写入以免覆盖原记录")
    cause = condition.get("cause")
    if cause is not None and cause != "yard_flood":
        raise GardenError("小院子作物异常成因无效，已停止写入以免覆盖原记录")
    if condition["type"] == "flood_rot" and cause != "yard_flood":
        raise GardenError("小院子泡烂异常缺少成因标记，已停止写入以免覆盖原记录")
    expected_prefix = f"condition:{plot['plot_id']}:{plot['cycle_id']}:"
    if not condition["condition_id"].startswith(expected_prefix) or condition["condition_id"] == expected_prefix:
        raise GardenError("小院子作物异常与作物轮次不一致，已停止写入以免覆盖原记录")
    penalty = condition.get("yield_penalty")
    if isinstance(penalty, bool) or not isinstance(penalty, int) or penalty not in (0, 1):
        raise GardenError("小院子作物异常减产记录无效，已停止写入以免覆盖原记录")
    occurred_at = _validate_condition_timestamp(condition, "occurred_at", required=True)
    announced_at = _validate_condition_timestamp(condition, "announced_at")
    worsened_at = _validate_condition_timestamp(condition, "worsened_at")
    resolved_at = _validate_condition_timestamp(condition, "resolved_at")
    failed_at = _validate_condition_timestamp(condition, "failed_at")
    if announced_at is not None and announced_at < occurred_at:
        raise GardenError("小院子作物异常展示时间早于发生时间，已停止写入以免覆盖原记录")
    if cause == "yard_flood":
        # 监理裁决（第七版设计稿第四节第4点）：内涝收束的异常/新建泡烂不走
        # 自然异常的 24h/36h 时间等式——它们的时钟是浸泡时长，不是
        # announced_at 之后的固定窗口。改为要求终态自洽：failed 且已枯死，
        # worsened_at 与 failed_at 是同一个泡烂理论时刻，announced_at 不晚
        # 于它（沿用"没被告知不计惩罚时钟"的公平原则）。
        if condition["status"] != "failed" or condition["severity"] != "withered" or penalty != 1:
            raise GardenError("小院子内涝泡烂记录不完整，已停止写入以免覆盖原记录")
        if failed_at is None or worsened_at != failed_at:
            raise GardenError("小院子内涝泡烂时间不一致，已停止写入以免覆盖原记录")
        if announced_at is None or announced_at > failed_at:
            raise GardenError("小院子内涝泡烂展示时间晚于泡烂时间，已停止写入以免覆盖原记录")
    else:
        if worsened_at is not None and (
            announced_at is None or worsened_at != announced_at + CONDITION_DAMAGE_AFTER
        ):
            raise GardenError("小院子作物异常减产时间不一致，已停止写入以免覆盖原记录")
        if failed_at is not None and (
            announced_at is None or failed_at != announced_at + CONDITION_WITHER_AFTER
        ):
            raise GardenError("小院子作物异常枯死时间不一致，已停止写入以免覆盖原记录")
    if resolved_at is not None and resolved_at < occurred_at:
        raise GardenError("小院子作物异常处理时间早于发生时间，已停止写入以免覆盖原记录")
    if condition["severity"] == "warning" and (penalty != 0 or worsened_at is not None or failed_at is not None):
        raise GardenError("小院子作物异常阶段记录矛盾，已停止写入以免覆盖原记录")
    if condition["severity"] in ("damaged", "withered") and (
        announced_at is None or worsened_at is None or penalty != 1
    ):
        raise GardenError("小院子作物异常恶化记录不完整，已停止写入以免覆盖原记录")
    if condition["status"] == "active" and condition["severity"] == "withered":
        raise GardenError("小院子作物异常状态矛盾，已停止写入以免覆盖原记录")
    if condition["status"] == "resolved" and (resolved_at is None or announced_at is None):
        raise GardenError("小院子作物异常处理时间缺失，已停止写入以免覆盖原记录")
    if condition["status"] != "resolved" and resolved_at is not None:
        raise GardenError("小院子作物异常处理状态矛盾，已停止写入以免覆盖原记录")
    if condition["status"] == "failed" and (condition["severity"] != "withered" or failed_at is None):
        raise GardenError("小院子作物枯死记录不完整，已停止写入以免覆盖原记录")
    if condition["status"] != "failed" and failed_at is not None:
        raise GardenError("小院子作物枯死状态矛盾，已停止写入以免覆盖原记录")


def _validate_soil(plot: dict) -> None:
    soil = plot.get("soil")
    if not isinstance(soil, dict):
        raise GardenError("小院子土壤状态格式损坏，已停止写入以免覆盖原记录")
    moisture = soil.get("moisture")
    if isinstance(moisture, bool) or not isinstance(moisture, (int, float)) or not 0 <= moisture <= 100:
        raise GardenError("小院子土壤水分格式损坏，已停止写入以免覆盖原记录")
    settled_at = _parse_iso(soil.get("settled_at"), field="土壤结算时间")
    anchor_at = _parse_iso(soil.get("anchor_at"), field="土壤锚点时间")
    anchor_moisture = soil.get("anchor_moisture")
    if isinstance(anchor_moisture, bool) or not isinstance(anchor_moisture, (int, float)) or not 0 <= anchor_moisture <= 100:
        raise GardenError("小院子土壤锚点水分格式损坏，已停止写入以免覆盖原记录")
    if anchor_at > settled_at:
        raise GardenError("小院子土壤锚点时间晚于结算时间，已停止写入以免覆盖原记录")
    for key in (
        "last_manual_water_at", "last_rain_at", "finalized_last_rain_at",
        "finalized_last_dip_below_50_at", "tail_resolved_dip_at",
    ):
        if soil.get(key) is not None:
            _parse_iso(soil[key], field="土壤时间")
    if soil.get("last_water_source") not in (None, "manual", "rain"):
        raise GardenError("小院子土壤水源格式损坏，已停止写入以免覆盖原记录")
    for key in ("dry_hours", "saturated_hours"):
        value = soil.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise GardenError("小院子土壤暴露时长格式损坏，已停止写入以免覆盖原记录")
    keys = ("known_hours", "dry_hours", "dryish_hours", "adequate_hours", "moist_hours", "saturated_hours", "hot_hours", "cold_hours", "rain_mm")
    for exposure_field in ("exposure_by_date", "exposure_finalized_by_date"):
        exposure = soil.get(exposure_field)
        if not isinstance(exposure, dict) or len(exposure) > 8:
            raise GardenError("小院子土壤暴露记录格式损坏，已停止写入以免覆盖原记录")
        for day, values in exposure.items():
            try:
                date.fromisoformat(day)
            except (TypeError, ValueError) as exc:
                raise GardenError("小院子土壤暴露日期损坏，已停止写入以免覆盖原记录") from exc
            if not isinstance(values, dict) or any(
                isinstance(values.get(key), bool) or not isinstance(values.get(key), (int, float)) or values[key] < 0
                for key in keys
            ) or values["known_hours"] > 24.0001:
                raise GardenError("小院子土壤暴露记录格式损坏，已停止写入以免覆盖原记录")


def _validate_environment(state: dict) -> None:
    environment = state.get("environment")
    if not isinstance(environment, dict):
        raise GardenError("小院子环境游标格式损坏，已停止写入以免覆盖原记录")
    if environment.get("location_id") is not None and not isinstance(environment.get("location_id"), str):
        raise GardenError("小院子环境城市格式损坏，已停止写入以免覆盖原记录")
    for key in ("last_settled_at", "last_fresh_weather_at"):
        if environment.get(key) is not None:
            _parse_iso(environment[key], field="环境结算时间")
    if environment.get("last_observation_id") is not None and not isinstance(environment.get("last_observation_id"), str):
        raise GardenError("小院子环境观测格式损坏，已停止写入以免覆盖原记录")
    if environment.get("status") not in ("fresh", "stale", "missing"):
        raise GardenError("小院子环境状态格式损坏，已停止写入以免覆盖原记录")
    if environment.get("rain_finalized_at") is not None:
        _parse_iso(environment["rain_finalized_at"], field="降雨结算锚点时间")
    rain_by_date = environment.get("rain_by_date")
    if not isinstance(rain_by_date, dict) or len(rain_by_date) > garden_weather.RAIN_LEDGER_DAYS:
        raise GardenError("小院子院级雨量日账格式损坏，已停止写入以免覆盖原记录")
    for day, amount in rain_by_date.items():
        try:
            date.fromisoformat(day)
        except (TypeError, ValueError) as exc:
            raise GardenError("小院子院级雨量日期损坏，已停止写入以免覆盖原记录") from exc
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not isfinite(float(amount))
            or amount < 0
        ):
            raise GardenError("小院子院级雨量数值损坏，已停止写入以免覆盖原记录")
    yard_cursor = environment.get("yard_water_announced")
    if yard_cursor is not None:
        if (
            not isinstance(yard_cursor, dict)
            or yard_cursor.get("level") not in {"none", "puddles", "flooded"}
        ):
            raise GardenError("小院子内涝通知游标损坏，已停止写入以免覆盖原记录")
        if yard_cursor.get("at") is not None:
            _parse_iso(yard_cursor["at"], field="内涝通知时间")
    flood_watch = environment.get("flood_watch")
    if not isinstance(flood_watch, dict):
        raise GardenError("小院子内涝时钟格式损坏，已停止写入以免覆盖原记录")
    for key in ("since", "drained_at"):
        if flood_watch.get(key) is not None:
            _parse_iso(flood_watch[key], field="内涝时钟时间")


def _validate_crop_incident(record: dict) -> None:
    text_fields = ("condition_id", "plot_id", "crop_id", "cycle_id", "type", "occurred_at", "outcome")
    if any(not isinstance(record.get(key), str) or not record[key] for key in text_fields):
        raise GardenError("小院子作物事故手账格式损坏，已停止写入以免覆盖原记录")
    if (
        record["plot_id"] not in _PLOT_IDS
        or record["crop_id"] not in garden_crops.CROPS
        or record["type"] not in CONDITION_TYPES
        or record["outcome"] not in ("active", "resolved", "failed")
    ):
        raise GardenError("小院子作物事故手账字段无效，已停止写入以免覆盖原记录")
    if not record["condition_id"].startswith(f"condition:{record['plot_id']}:{record['cycle_id']}:"):
        raise GardenError("小院子作物事故手账轮次不一致，已停止写入以免覆盖原记录")
    _parse_iso(record["occurred_at"], field="作物事故时间")
    for key in ("announced_at", "resolved_at", "failed_at"):
        if record.get(key) is not None:
            _parse_iso(record[key], field="作物事故时间")
    penalty = record.get("yield_penalty")
    if isinstance(penalty, bool) or not isinstance(penalty, int) or penalty not in (0, 1):
        raise GardenError("小院子作物事故减产记录无效，已停止写入以免覆盖原记录")
    nodes = record.get("nodes")
    if not isinstance(nodes, list) or any(
        not isinstance(node, dict)
        or node.get("kind") not in ("occurred", "announced", "damaged", "failed", "resolved")
        or not isinstance(node.get("at"), str)
        for node in nodes
    ):
        raise GardenError("小院子作物事故节点损坏，已停止写入以免覆盖原记录")
    for node in nodes:
        _parse_iso(node["at"], field="作物事故节点时间")
    node_kinds = [node["kind"] for node in nodes]
    if len(node_kinds) != len(set(node_kinds)):
        raise GardenError("小院子作物事故节点重复，已停止写入以免覆盖原记录")


def _validate_crop_state(state: dict) -> None:
    _validate_inventory(state["inventory"])
    _validate_coop(state["coop"])
    _validate_compost(state)
    if state.get("version") == STATE_VERSION:
        _validate_environment(state)
    if len(state["plots"]) not in (0, 2, 4) or any(not isinstance(plot, dict) for plot in state["plots"]):
        raise GardenError("小院子菜畦格式损坏，已停止写入以免覆盖原记录")
    expected_plot_ids = set(_PLOT_IDS[:len(state["plots"])])
    if state["plots"] and {plot.get("plot_id") for plot in state["plots"]} != expected_plot_ids:
        raise GardenError("小院子菜畦编号格式损坏，已停止写入以免覆盖原记录")
    for plot in state["plots"]:
        status = plot.get("status")
        if status == "empty":
            if plot.get("condition") is not None:
                raise GardenError("小院子空菜畦残留作物异常，已停止写入以免覆盖原记录")
            if plot.get("quality") is not None:
                raise GardenError("小院子空菜畦残留作物品质，已停止写入以免覆盖原记录")
            if plot.get("quality_cause") is not None:
                raise GardenError("小院子空菜畦残留品质成因，已停止写入以免覆盖原记录")
            continue
        crop_id = plot.get("crop_id")
        if status not in ("growing", "ready", "withered") or crop_id not in garden_crops.CROPS:
            raise GardenError("小院子作物记录格式损坏，已停止写入以免覆盖原记录")
        if plot.get("quality") not in (None, "poor"):
            raise GardenError("小院子作物品质格式损坏，已停止写入以免覆盖原记录")
        # 第八版：品质成因决定能否用肥料挽救（设计稿第一节）——存量旧档没有
        # 这个字段，plot.get() 缺省即 None，天然落进"不可救"，不需要专门迁移。
        quality_cause = plot.get("quality_cause")
        if quality_cause not in (None, "flood", "condition"):
            raise GardenError("小院子作物品质成因格式损坏，已停止写入以免覆盖原记录")
        if plot.get("quality") != "poor" and quality_cause is not None:
            raise GardenError("小院子作物品质成因与品质矛盾，已停止写入以免覆盖原记录")
        # 第九版：追肥次数——存量旧档没有这个字段，plot.get() 缺省即 0，
        # 天然落进"还没追过肥"，不需要专门迁移（跟 quality_cause 一个路数）。
        fertilize_count = plot.get("fertilize_count", 0)
        if (
            isinstance(fertilize_count, bool) or not isinstance(fertilize_count, int)
            or not 0 <= fertilize_count <= FERTILIZE_MAX_PER_CYCLE
        ):
            raise GardenError("小院子作物追肥次数损坏，已停止写入以免覆盖原记录")
        if plot.get("stage") not in _CROP_STAGE_ORDER or not isinstance(plot.get("growth_points"), (int, float)):
            raise GardenError("小院子作物记录格式损坏，已停止写入以免覆盖原记录")
        if (status == "ready") != (plot.get("stage") == "ready") or (
            status == "withered" and plot.get("stage") == "ready"
        ):
            raise GardenError("小院子作物成熟状态损坏，已停止写入以免覆盖原记录")
        _parse_iso(plot.get("planted_at"), field="作物种植时间")
        _parse_iso(plot.get("last_settled_at"), field="作物结算时间")
        if plot.get("ready_at") is not None:
            _parse_iso(plot.get("ready_at"), field="作物成熟时间")
        if not isinstance(plot.get("cycle_id"), str) or not plot["cycle_id"]:
            raise GardenError("小院子作物轮次格式损坏，已停止写入以免覆盖原记录")
        if state.get("version") == STATE_VERSION:
            _validate_soil(plot)
            history = plot.get("watering_history")
            unneeded = plot.get("unneeded_watering_by_date")
            if not isinstance(history, list) or len(history) > WATERING_RESULT_HISTORY_LIMIT or not isinstance(unneeded, dict):
                raise GardenError("小院子浇水审计格式损坏，已停止写入以免覆盖原记录")
            for record in history:
                if not isinstance(record, dict) or record.get("outcome") not in (
                    "watered", "no_need", "too_wet", "protest", "waterlogged", "blocked_by_condition", "already_waterlogged", "refused",
                ) or record.get("source") != "manual":
                    raise GardenError("小院子浇水审计格式损坏，已停止写入以免覆盖原记录")
                _parse_iso(record.get("at"), field="浇水审计时间")
            for day, count in unneeded.items():
                try:
                    date.fromisoformat(day)
                except (TypeError, ValueError) as exc:
                    raise GardenError("小院子无必要浇水日期损坏，已停止写入以免覆盖原记录") from exc
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise GardenError("小院子无必要浇水次数损坏，已停止写入以免覆盖原记录")
            events_by_date = plot.get("unneeded_watering_events")
            if not isinstance(events_by_date, dict) or len(events_by_date) > 8:
                raise GardenError("小院子无必要浇水事件格式损坏，已停止写入以免覆盖原记录")
            for day, events in events_by_date.items():
                try:
                    date.fromisoformat(day)
                except (TypeError, ValueError) as exc:
                    raise GardenError("小院子无必要浇水事件日期损坏，已停止写入以免覆盖原记录") from exc
                if not isinstance(events, list) or len(events) > 32:
                    raise GardenError("小院子无必要浇水事件格式损坏，已停止写入以免覆盖原记录")
                for iso in events:
                    _parse_iso(iso, field="无必要浇水意图时间")
        if not isinstance(plot.get("water_bonus_dates", []), list) or not all(isinstance(day, str) for day in plot["water_bonus_dates"]):
            raise GardenError("小院子浇水记录格式损坏，已停止写入以免覆盖原记录")
        watering_by_date = plot.get("watering_by_date")
        if watering_by_date is not None:
            if not isinstance(watering_by_date, dict):
                raise GardenError("小院子每日浇水次数格式损坏，已停止写入以免覆盖原记录")
            for day, amount in watering_by_date.items():
                try:
                    date.fromisoformat(day)
                except (TypeError, ValueError) as exc:
                    raise GardenError("小院子每日浇水日期损坏，已停止写入以免覆盖原记录") from exc
                if isinstance(amount, bool) or not isinstance(amount, int) or amount not in (1, 2, 3):
                    raise GardenError("小院子每日浇水次数损坏，已停止写入以免覆盖原记录")
        if not isinstance(plot.get("stage_events_seen", []), list) or not all(isinstance(event_id, str) for event_id in plot["stage_events_seen"]):
            raise GardenError("小院子作物事件记录格式损坏，已停止写入以免覆盖原记录")
        cycle_penalty = plot.get("yield_penalty")
        if cycle_penalty is not None and (
            isinstance(cycle_penalty, bool)
            or not isinstance(cycle_penalty, int)
            or cycle_penalty not in (0, 1)
        ):
            raise GardenError("小院子作物轮次减产记录无效，已停止写入以免覆盖原记录")
        _validate_crop_condition(plot)
        if (
            cycle_penalty is not None
            and isinstance(plot.get("condition"), dict)
            and plot["condition"]["yield_penalty"] > cycle_penalty
        ):
            raise GardenError("小院子作物轮次减产与事故记录矛盾，已停止写入以免覆盖原记录")
        if isinstance(plot.get("condition"), dict) and (
            (plot["condition"]["status"] == "active" and status != "growing")
            or (plot["condition"]["status"] == "failed" and status != "withered")
        ):
            raise GardenError("小院子作物异常与菜畦状态矛盾，已停止写入以免覆盖原记录")
        if status == "withered" and (
            not isinstance(plot.get("condition"), dict)
            or plot["condition"].get("status") != "failed"
        ):
            raise GardenError("小院子枯死作物缺少事故记录，已停止写入以免覆盖原记录")
    journal = state["journal"]
    for key in (
        "species_seen", "crops_harvested", "meals_made", "gifts_given",
        "bond_milestones", "calendar_moments", "crop_incidents", "drainage",
        "fertilizer_rescues",
    ):
        if not isinstance(journal.get(key, []), list) or any(not isinstance(item, dict) for item in journal.get(key, [])):
            raise GardenError("小院子手账格式损坏，已停止写入以免覆盖原记录")
    for record in journal.get("crop_incidents", []):
        _validate_crop_incident(record)
    for plot in state["plots"]:
        condition = plot.get("condition")
        if not isinstance(condition, dict):
            continue
        incidents = [
            record for record in journal["crop_incidents"]
            if record.get("condition_id") == condition["condition_id"]
        ]
        if len(incidents) != 1:
            raise GardenError("小院子作物异常找不到唯一事故手账，已停止写入以免覆盖原记录")
        incident = incidents[0]
        if any(
            incident.get(key) != expected for key, expected in (
                ("plot_id", plot["plot_id"]),
                ("crop_id", plot["crop_id"]),
                ("cycle_id", plot["cycle_id"]),
                ("type", condition["type"]),
                ("yield_penalty", condition["yield_penalty"]),
                ("outcome", condition["status"]),
                ("occurred_at", condition["occurred_at"]),
                ("announced_at", condition["announced_at"]),
                ("resolved_at", condition["resolved_at"]),
                ("failed_at", condition["failed_at"]),
            )
        ):
            raise GardenError("小院子作物异常与事故手账不一致，已停止写入以免覆盖原记录")
    for event in state["pending_events"]:
        if isinstance(event, dict) and event.get("type") == "crop_condition":
            plot = next((
                candidate for candidate in state["plots"]
                if candidate.get("plot_id") == event.get("plot_id")
            ), None)
            _validate_condition_pending_event(event, plot)
    last_roll = state["meta"].get("last_natural_condition_roll_date")
    if last_roll is not None:
        try:
            date.fromisoformat(last_roll)
        except (TypeError, ValueError) as exc:
            raise GardenError("小院子异常每日判定记录损坏，已停止写入以免覆盖原记录") from exc
    cooldown = state["meta"].get("natural_condition_cooldown_until")
    if cooldown is not None:
        _parse_iso(cooldown, field="异常自然冷却时间")
    seen_calendar = state["meta"].get("seen_calendar_events", [])
    if not isinstance(seen_calendar, list) or not all(isinstance(event_id, str) for event_id in seen_calendar):
        raise GardenError("小院子日历已见记录格式损坏，已停止写入以免覆盖原记录")
    completed = state["meta"].get("completed_crop_cycles", [])
    if not isinstance(completed, list) or any(not isinstance(cycle, dict) for cycle in completed):
        raise GardenError("小院子已收获作物事件格式损坏，已停止写入以免覆盖原记录")
    for cycle in completed:
        required = ("plot_id", "crop_id", "cycle_id", "stage", "stage_events_seen")
        if any(not isinstance(cycle.get(key), str) or not cycle[key] for key in required[:-1]):
            raise GardenError("小院子已收获作物事件格式损坏，已停止写入以免覆盖原记录")
        if cycle["plot_id"] not in _PLOT_IDS or cycle["crop_id"] not in garden_crops.CROPS or cycle["stage"] not in _CROP_STAGE_ORDER:
            raise GardenError("小院子已收获作物事件格式损坏，已停止写入以免覆盖原记录")
        if not isinstance(cycle["stage_events_seen"], list) or not all(isinstance(event_id, str) for event_id in cycle["stage_events_seen"]):
            raise GardenError("小院子已收获作物事件格式损坏，已停止写入以免覆盖原记录")
    try:
        garden_scene.validate_cache(state["meta"].get("scene_cache"))
    except (TypeError, ValueError) as exc:
        raise GardenError("小院子逛逛缓存格式损坏，已停止写入以免覆盖原记录") from exc


def _ensure_plot_layout(state: dict) -> bool:
    """只补四格布局；已有 p1/p2 对象及字段原样保留。"""
    if not state["plots"]:
        state["plots"] = [_empty_plot(plot_id) for plot_id in _PLOT_IDS]
        return True
    if len(state["plots"]) == 2:
        state["plots"].extend(_empty_plot(plot_id) for plot_id in _PLOT_IDS[2:])
        return True
    return False


def _ensure_crop_state(state: dict, now: datetime) -> bool:
    """补齐菜畦与库存容器，发放首发种子及一次性新增品种补充包。"""
    changed = _ensure_plot_layout(state)
    inventory = state["inventory"]
    for key, default in (
        ("seeds", {}), ("warehouse_seeds", {}), ("produce", {}), ("produce_poor", {}),
        ("prepared_food", {}), ("animal_products", {}), ("keepsakes", []),
        ("fertilizer", {}),
    ):
        if key not in inventory:
            inventory[key] = default
            changed = True
    meta = state["meta"]
    if not meta.get("crop_seed_box_initialized"):
        seed_box = garden_crops.INITIAL_SEEDS_BY_SEASON[calendar_context(now).season]
        # 冬天没有露天首发种子并不等于已经领过；保留未初始化标记，等春天自然补发。
        if seed_box:
            for crop_id, amount in seed_box.items():
                inventory["seeds"][crop_id] = int(inventory["seeds"].get(crop_id, 0)) + amount
            meta["crop_seed_box_initialized"] = True
            # 记下当时到底是哪个季节领的首发种子盒，供下面的补发包判断这个
            # 院子是不是"命中过"这个季节——避免院子当季创建时，同一批种子
            # 又被下面的补发包重复发一遍。
            meta["crop_seed_box_initialized_season"] = calendar_context(now).season
            changed = True
    current_season = calendar_context(now).season
    initialized_season = meta.get("crop_seed_box_initialized_season")
    for pack_id, pack in garden_crops.BONUS_SEED_PACKS.items():
        marker = f"crop_seed_pack:{pack_id}"
        if meta.get(marker):
            continue
        if pack.get("covered_by_initial_season") and initialized_season in pack["seasons"]:
            # 首发种子盒当年就是在这个季节发的，已经拿过这批作物了，
            # 补发包只用来接住"当初不是这个季节创建"的院子，标记为已处理。
            meta[marker] = True
            changed = True
            continue
        if current_season not in pack["seasons"]:
            continue
        for crop_id, amount in pack["seeds"].items():
            inventory["seeds"][crop_id] = int(inventory["seeds"].get(crop_id, 0)) + amount
        meta[marker] = True
        changed = True
    # 仓库：种子盒只留当季能种的；过季的种子挪进仓库暂存，季节一到自动搬回来。
    seeds = inventory["seeds"]
    warehouse = inventory["warehouse_seeds"]
    for crop_id in list(seeds):
        crop = garden_crops.CROPS.get(crop_id)
        if crop is not None and current_season not in crop["seasons"]:
            amount = seeds.pop(crop_id)
            warehouse[crop_id] = int(warehouse.get(crop_id, 0)) + amount
            changed = True
    for crop_id in list(warehouse):
        crop = garden_crops.CROPS.get(crop_id)
        if crop is not None and current_season in crop["seasons"]:
            amount = warehouse.pop(crop_id)
            seeds[crop_id] = int(seeds.get(crop_id, 0)) + amount
            changed = True
    for plot in state["plots"]:
        if plot.get("status") == "empty":
            continue
        # 轮次级减产是收获的唯一权威。兼容 C 初版只把减产写在 condition
        # 或事故手账里的状态；后续异常可能已经替换 plot.condition，所以必须
        # 汇总当前地块、作物、轮次的全部事故，且任何路径都只能封顶为 1。
        condition = plot.get("condition")
        condition_penalty = (
            int(condition.get("yield_penalty", 0))
            if isinstance(condition, dict) else 0
        )
        incident_penalty = max((
            int(incident.get("yield_penalty", 0))
            for incident in state["journal"]["crop_incidents"]
            if (
                incident.get("plot_id") == plot.get("plot_id")
                and incident.get("crop_id") == plot.get("crop_id")
                and incident.get("cycle_id") == plot.get("cycle_id")
            )
        ), default=0)
        cycle_penalty = min(1, max(
            int(plot.get("yield_penalty") or 0),
            condition_penalty,
            incident_penalty,
        ))
        if plot.get("yield_penalty") != cycle_penalty:
            plot["yield_penalty"] = cycle_penalty
            changed = True
        legacy_days = [
            day for day in plot.get("water_bonus_dates", [])
            if isinstance(day, str)
        ]
        watering_by_date = plot.get("watering_by_date")
        if watering_by_date is None:
            watering_by_date = {day: 1 for day in legacy_days}
            plot["watering_by_date"] = watering_by_date
            changed = True
        else:
            for day in legacy_days:
                if day not in watering_by_date:
                    watering_by_date[day] = 1
                    changed = True
        kept_days = sorted(watering_by_date)[-WATERING_HISTORY_DAYS:]
        if list(watering_by_date) != kept_days:
            plot["watering_by_date"] = {
                day: watering_by_date[day] for day in kept_days
            }
            watering_by_date = plot["watering_by_date"]
            changed = True
        normalized_bonus_dates = [
            day for day in sorted(set(legacy_days))
            if day in watering_by_date
        ]
        if plot.get("water_bonus_dates", []) != normalized_bonus_dates:
            plot["water_bonus_dates"] = normalized_bonus_dates
            changed = True
    return changed


def _empty_exposure() -> dict:
    return {
        "known_hours": 0.0, "dry_hours": 0.0, "dryish_hours": 0.0,
        "adequate_hours": 0.0, "moist_hours": 0.0, "saturated_hours": 0.0,
        "hot_hours": 0.0, "cold_hours": 0.0, "rain_mm": 0.0,
    }


def _new_soil(now: datetime, moisture: float = 55.0) -> dict:
    return {
        "moisture": float(moisture), "settled_at": now.isoformat(),
        # 锚点是唯一“已经拍板、不再改写”的水分基准；实时 moisture 每次都从
        # 这里往后纯函数重放到当前时刻，才能保证乱序/分批到达的观测收敛到
        # 同一结果。锚点只在浇水（人为动作，立即拍板）或天气结算不再可能被
        # 晚到观测修改时（见 REPLAY_HORIZON）推进。
        "anchor_at": now.isoformat(), "anchor_moisture": float(moisture),
        "last_manual_water_at": None, "last_rain_at": None,
        # 永久降雨事实只在折叠（不可逆）时推进；last_rain_at 每次都由它与
        # 本次尾段重放共同派生，不能只在“这次算出有雨”时写、“这次算出没
        # 雨”时却保留上一次的旧结论。
        "finalized_last_rain_at": None,
        "last_water_source": None, "dry_hours": 0.0, "saturated_hours": 0.0,
        "exposure_by_date": {}, "exposure_finalized_by_date": {},
        # 永久“最近一次真实跌破 50”事实，与 finalized_last_rain_at 同一套
        # 折叠/尾段派生规则：跌破清零无必要浇水计数不是布尔的“今天清零过”，
        # 而是精确到时刻——同一天跌破之后仍可以正常产生新的连续意图。
        "finalized_last_dip_below_50_at": None,
        "tail_resolved_dip_at": None,
    }


def _effective_unneeded_count(plot: dict, day: str) -> int:
    """只统计“最近一次真实跌破 50”之后发生的无必要浇水意图；跌破之前的
    旧意图被清零，跌破之后的新意图仍按真实先后顺序正常递增，不会因为
    "今天已经跌破过一次"就让后续每一次都退回成第一次。"""
    events = [
        iso for iso in plot.get("unneeded_watering_events", {}).get(day, [])
        if isinstance(iso, str)
    ]
    soil = plot["soil"]
    dip_raw = soil.get("tail_resolved_dip_at")
    if dip_raw is None:
        return len(events)
    dip_at = _parse_iso(dip_raw, field="土壤最近跌破50时间")
    # 浇水入口总是先结算天气（算出这次的 dip_at）、再记录这次的意图时刻，
    # 两者可能落在同一时刻；这是“天气先、意图后”的确定因果顺序，不是需要
    # 严格早晚才能分辨的两个独立事件，所以用 >= 而不是 >，否则跟跌破同一
    # 时刻发起的第一条意图会被误判成“跌破之前”而排除，产出非法的 0 次。
    return sum(
        1 for iso in events
        if _parse_iso(iso, field="无必要浇水意图时间") >= dip_at
    )


def _refresh_unneeded_display(plot: dict) -> None:
    """`unneeded_watering_by_date` 只是审计展示，真正决策一律走
    `_effective_unneeded_count`；这里只是让展示值跟着最新的解析结果同步，
    不作为任何裁决依据。"""
    events_by_date = plot.get("unneeded_watering_events", {})
    plot["unneeded_watering_by_date"] = {
        day: _effective_unneeded_count(plot, day)
        for day, events in events_by_date.items() if events
    }


def _checkpoint_soil(
    plot: dict, now: datetime, observations: list[dict], *, override_moisture: float | None = None,
) -> None:
    """人为动作（浇水）或环境边界变化（城市切换）立即把土壤拍板到这一刻。

    必须先把锚点到当下这段仍可能被晚到观测修正的尾段完整折叠固化——否则
    浇水前那几个小时的暴露、降雨与无必要浇水清零判断会随锚点直接跳到
    `now` 而凭空消失、且再也无法被后续晚到观测撤销。浇水时再用这一刻的
    确定性结果覆盖水分；城市切换不覆盖，只是提前把旧城市的尾段收口。
    """
    _fold_plot_forward(plot, now, observations)
    soil = plot["soil"]
    if override_moisture is not None:
        soil["anchor_moisture"] = round(float(override_moisture), 4)
    # 折叠只把 [旧锚点, now) 收口进永久账本，不会自己刷新 exposure_by_date
    # 等展示字段；必须再走一次实时投影，否则城市切换后立刻返回的暴露、
    # 降雨来源仍是折叠前的旧值，直到下一次结算才会自愈。对于浇水，折叠后
    # 尾段长度为零，这次刷新只是把上面的覆盖值原样带到 moisture/settled_at
    # 上，不会拿天气重新计算掉刚刚写入的确定性结果。
    _refresh_plot_live(plot, now, observations)


def _migrate_v4_to_v5(state: dict, now: datetime) -> None:
    """只建立中性起点，不用今天的天气倒推旧存档。"""
    today = _today_key(now)
    for plot in state["plots"]:
        if plot.get("status") == "empty":
            continue
        condition = plot.get("condition")
        if isinstance(condition, dict) and condition.get("type") == "waterlogged" and condition.get("status") == "active":
            moisture = 100.0
        elif int(plot.get("watering_by_date", {}).get(today, 0)) > 0:
            moisture = 72.0
        else:
            moisture = 55.0
        plot["soil"] = _new_soil(now, moisture)
        plot["watering_history"] = []
        plot["unneeded_watering_by_date"] = {}
        plot["unneeded_watering_events"] = {}
    state["environment"] = {
        "location_id": None, "last_settled_at": now.isoformat(),
        "last_observation_id": None, "last_fresh_weather_at": None,
        "status": "missing", "rain_finalized_at": now.isoformat(),
        "rain_by_date": {},
        "flood_watch": {"since": None, "drained_at": None},
    }
    state["version"] = STATE_VERSION
    state["_migrated"] = True


def _crop_stage(crop_id: str, growth_points: float) -> str:
    stage = "seed"
    for candidate, threshold in garden_crops.CROPS[crop_id]["stage_thresholds"]:
        if growth_points >= threshold:
            stage = candidate
    return stage


def _queue_crop_stage(state: dict, plot: dict, stage: str) -> None:
    event_id = f"crop:{plot['plot_id']}:{plot['cycle_id']}:stage:{stage}"
    if event_id in plot["stage_events_seen"]:
        return
    if any(isinstance(event, dict) and event.get("event_id") == event_id for event in state["pending_events"]):
        return
    state["pending_events"].append({
        "event_id": event_id, "type": "crop_stage", "plot_id": plot["plot_id"],
        "crop_id": plot["crop_id"], "cycle_id": plot["cycle_id"], "stage": stage,
    })


def _incident_for_condition(state: dict, condition_id: str) -> dict:
    matches = [
        record for record in state["journal"]["crop_incidents"]
        if isinstance(record, dict) and record.get("condition_id") == condition_id
    ]
    if len(matches) != 1:
        raise GardenError("小院子作物异常找不到唯一事故手账，已停止写入以免覆盖原记录")
    return matches[0]


def _append_incident_node(record: dict, kind: str, at: datetime) -> None:
    if any(node.get("kind") == kind for node in record["nodes"] if isinstance(node, dict)):
        return
    record["nodes"].append({"kind": kind, "at": at.isoformat()})


def _queue_condition_event(state: dict, plot: dict, severity: str) -> None:
    condition = plot["condition"]
    event_id = f"{condition['condition_id']}:severity:{severity}"
    if any(
        isinstance(event, dict) and event.get("event_id") == event_id
        for event in state["pending_events"]
    ):
        return
    state["pending_events"].append({
        "event_id": event_id,
        "type": "crop_condition",
        "condition_id": condition["condition_id"],
        "plot_id": plot["plot_id"],
        "crop_id": plot["crop_id"],
        "cycle_id": plot["cycle_id"],
        "condition_type": condition["type"],
        "severity": severity,
        "yield_penalty": 0 if severity == "warning" else 1,
    })


def _create_crop_condition(
    state: dict,
    plot: dict,
    condition_type: str,
    now: datetime,
    *,
    announced: bool,
) -> dict:
    condition_id = f"condition:{plot['plot_id']}:{plot['cycle_id']}:{uuid.uuid4().hex}"
    announced_at = now.isoformat() if announced else None
    condition = {
        "condition_id": condition_id,
        "type": condition_type,
        "status": "active",
        "severity": "warning",
        "occurred_at": now.isoformat(),
        "announced_at": announced_at,
        "yield_penalty": 0,
        "worsened_at": None,
        "resolved_at": None,
        "failed_at": None,
    }
    plot["condition"] = condition
    nodes = [{"kind": "occurred", "at": now.isoformat()}]
    if announced:
        nodes.append({"kind": "announced", "at": now.isoformat()})
    state["journal"]["crop_incidents"].append({
        "condition_id": condition_id,
        "plot_id": plot["plot_id"],
        "crop_id": plot["crop_id"],
        "cycle_id": plot["cycle_id"],
        "type": condition_type,
        "occurred_at": now.isoformat(),
        "announced_at": announced_at,
        "yield_penalty": 0,
        "outcome": "active",
        "resolved_at": None,
        "failed_at": None,
        "nodes": nodes,
    })
    return condition


def _active_condition(plot: dict) -> dict | None:
    condition = plot.get("condition")
    if isinstance(condition, dict) and condition.get("status") == "active":
        return condition
    return None


def _settle_crop_conditions(state: dict, now: datetime) -> bool:
    """按首次成功展示时间恶化；精确边界可在一次长跳跃里补齐两个节点。"""
    changed = False
    for plot in state["plots"]:
        condition = _active_condition(plot)
        if condition is None or condition.get("announced_at") is None:
            continue
        announced_at = _parse_iso(condition["announced_at"], field="作物异常展示时间")
        incident = _incident_for_condition(state, condition["condition_id"])
        damaged_at = announced_at + CONDITION_DAMAGE_AFTER
        withered_at = announced_at + CONDITION_WITHER_AFTER
        severity_index = CONDITION_SEVERITIES.index(condition["severity"])
        if now >= damaged_at and severity_index < CONDITION_SEVERITIES.index("damaged"):
            condition.update({
                "severity": "damaged",
                "yield_penalty": 1,
                "worsened_at": damaged_at.isoformat(),
            })
            plot["yield_penalty"] = 1
            incident["yield_penalty"] = 1
            _append_incident_node(incident, "damaged", damaged_at)
            _queue_condition_event(state, plot, "damaged")
            severity_index = CONDITION_SEVERITIES.index("damaged")
            changed = True
            # 第七版阶段E（设计稿六.2）：拖到恶化在数量和品相上都留疤。只在
            # 这一次结算不会在同一口气里再跨过 withered_at 时才打标——
            # 一次长跳跃直接枯死的地块没有收成，打欠佳标记没有意义，也会跟
            # withered 分支"不画蛇添足"的原则矛盾。
            if now < withered_at and plot.get("quality") != "poor":
                plot["quality"] = "poor"
                # 第八版：人祸成因（异常拖延）标"condition"，肥料救不回
                # （设计稿第一节第2点，与下面内涝成因"flood"互斥）。
                plot["quality_cause"] = "condition"
        if now >= withered_at and severity_index < CONDITION_SEVERITIES.index("withered"):
            condition.update({
                "status": "failed",
                "severity": "withered",
                "yield_penalty": 1,
                "failed_at": withered_at.isoformat(),
            })
            plot["status"] = "withered"
            incident.update({
                "yield_penalty": 1,
                "outcome": "failed",
                "failed_at": withered_at.isoformat(),
            })
            _append_incident_node(incident, "failed", withered_at)
            cooldown_until = withered_at + NATURAL_CONDITION_COOLDOWN
            existing_cooldown = state["meta"].get("natural_condition_cooldown_until")
            if existing_cooldown is None or _parse_iso(
                existing_cooldown, field="异常自然冷却时间"
            ) < cooldown_until:
                state["meta"]["natural_condition_cooldown_until"] = cooldown_until.isoformat()
            _queue_condition_event(state, plot, "withered")
            changed = True
    return changed


def _eligible_condition_plots(state: dict, now: datetime) -> list[dict]:
    season = calendar_context(now).season
    return [
        plot for plot in state["plots"]
        if plot.get("status") == "growing"
        and plot.get("stage") != "seed"
        and plot.get("crop_id") in garden_crops.CROPS
        and season in garden_crops.CROPS[plot["crop_id"]]["seasons"]
        and _active_condition(plot) is None
    ]


def _maybe_roll_natural_condition(
    state: dict,
    now: datetime,
    rng,
    *,
    observations: list[dict] | None = None,
) -> bool:
    """仅在能力明确开启时判定今天一次；停机期间的旧日期永不补掷。"""
    if not natural_crop_conditions_enabled():
        return False
    if any(_active_condition(plot) is not None for plot in state["plots"]):
        return False
    cooldown = state["meta"].get("natural_condition_cooldown_until")
    if cooldown is not None and now < _parse_iso(cooldown, field="异常自然冷却时间"):
        return False
    today = _today_key(now)
    if state["meta"].get("last_natural_condition_roll_date") == today:
        return False
    # 无论有无合格作物，今天这次观察都算完成；同日播种、浇水或查看不能换条件重掷。
    state["meta"]["last_natural_condition_roll_date"] = today
    candidates = _eligible_condition_plots(state, now)
    if not candidates or rng.random() >= NATURAL_CONDITION_ROLL_PROBABILITY:
        return True
    plot = rng.choice(candidates)
    environment = _environment_snapshot(state, observations or [], now) or {}
    condition_type = _choose_condition_type(
        plot, rng, yard_water=str(environment.get("yard_water") or "none"),
    )
    condition = _create_crop_condition(
        state, plot, condition_type, now, announced=False,
    )
    _queue_condition_event(state, plot, "warning")
    return True


def _choose_condition_type(plot: dict, rng, *, yard_water: str = "none") -> str:
    """现实天气只改变命中后选中哪一种类型，绝不改变是否命中的总概率
    （那一步已经在调用方用 rng.random() 判过）。没有现实土壤数据时权重
    全部为 1，展开后与原始 ``NATURAL_CONDITION_TYPES`` 顺序逐一对应、each
    once，因此关闭现实天气时这里与旧版 ``rng.choice(CONDITION_TYPES)``
    完全等价，不改变既有测试用固定 ``choice_index`` 期望的行为。泡烂
    （``flood_rot``）只由内涝结算直接创建，不参与这张每日随机候选池。"""
    soil = plot.get("soil") if real_environment_enabled() else None
    weights = garden_weather.condition_type_weights(
        soil, NATURAL_CONDITION_TYPES, yard_water=yard_water,
    )
    weighted_types = [
        condition_type
        for condition_type in NATURAL_CONDITION_TYPES
        for _ in range(max(0, weights.get(condition_type, 1)))
    ]
    if not weighted_types:
        weighted_types = list(NATURAL_CONDITION_TYPES)
    return rng.choice(weighted_types)


def _apply_growth(
    state: dict,
    plot: dict,
    amount: float,
    *,
    ready_at: datetime | None = None,
) -> bool:
    if amount <= 0 or plot.get("status") != "growing" or _active_condition(plot) is not None:
        return False
    old_stage = plot["stage"]
    plot["growth_points"] = round(float(plot["growth_points"]) + amount, 4)
    new_stage = _crop_stage(plot["crop_id"], plot["growth_points"])
    plot["stage"] = new_stage
    if new_stage == "ready":
        plot["status"] = "ready"
        if plot.get("ready_at") is None and ready_at is not None:
            plot["ready_at"] = ready_at.isoformat()
        else:
            plot.setdefault("ready_at", None)
    old_index = _CROP_STAGE_ORDER.index(old_stage)
    new_index = _CROP_STAGE_ORDER.index(new_stage)
    for stage in _CROP_STAGE_ORDER[old_index + 1:new_index + 1]:
        _queue_crop_stage(state, plot, stage)
    return True


def _timing_multiplier_for_band(
    plot: dict,
    band: str,
    now: datetime,
    observations: list[dict],
) -> float:
    """把“当前条件持续不变”收敛成与天气成长账本同口径的日倍率。"""
    crop = garden_crops.CROPS[plot["crop_id"]]
    bucket = _empty_exposure()
    bucket.update({"known_hours": 24.0, f"{band}_hours": 24.0})
    if "hot" in _fresh_environment_weather_tags(observations, now):
        bucket["hot_hours"] = 24.0
    return garden_weather.daily_growth_multiplier(
        bucket,
        heat_tendency=crop.get("heat_tendency", "neutral"),
    )


def _current_timing_multiplier(plot: dict, now: datetime, observations: list[dict]) -> float:
    """按当前土壤湿度带位算出的"当前条件持续不变"日生长倍率；没有现实
    土壤数据时是中性的 1.0。`crop_timing` 的 current 倍率与追肥
    （`fertilize_plot` 的 growth_boost 分支）投影预计成熟时刻共用同一份
    计算，不各自抄一遍（CLAUDE.md"复制第二次就是抽共用的时机"）。"""
    soil = plot.get("soil")
    current_band = (
        garden_weather.moisture_band(float(soil.get("moisture", 55.0)))
        if isinstance(soil, dict) else None
    )
    return (
        _timing_multiplier_for_band(plot, current_band, now, observations)
        if current_band is not None else 1.0
    )


def _project_crop_ready_at(
    plot: dict,
    now: datetime,
    *,
    multiplier: float,
    bonus_points: float = 0.0,
) -> datetime | None:
    """按线性记账口径，投影首次达到成熟线的时刻。

    结算（_accrue_linear_growth）与预览共用同一套分段线性几何：每天的
    增量均匀摊在这一天的 24 小时里。调用前 _settle_crops 已经用同一个
    now 结算过，growth_points 恰好累积到 now，所以第一段插值窗口就是
    [now, 下一个午夜)，之后逐日整段推进。倍率取调用方假设的"当前条件
    持续不变"日倍率——未来天气无法确定，这是估算而非承诺；但只要条件
    不变，投影时刻与结算真正翻转状态的时刻严格一致，不会再出现倒计时
    还剩十小时、作物却在午夜齐熟（或反过来）的整天级错位。
    """
    if plot.get("status") != "growing" or _active_condition(plot) is not None:
        return None
    crop = garden_crops.CROPS[plot["crop_id"]]
    target = float(crop["growth_days"])
    projected = float(plot["growth_points"]) + max(0.0, bonus_points)
    if projected >= target:
        return now
    # 整天记账切线性的迁移日，结算游标停在未来的午夜（那之前的账已经
    # 一次性预付进 growth_points 了）——投影必须从游标而不是"现在"起
    # 算，否则会把已预付的时段再摊一遍，预计成熟时刻虚早最多一天。
    window_start = now
    if plot.get("last_settled_at"):
        last = _parse_iso(plot["last_settled_at"], field="作物结算时间")
        if last > window_start:
            window_start = last
    day = window_start.astimezone(TZ).date()
    for _ in range(TIMING_PROJECTION_MAX_DAYS):
        context = calendar_context(_date_at_start(day))
        # 换季只挡"能不能新种"，已经在地里的这一茬不因换季暂停生长。
        base = 1.0 + (0.1 if context.term_id in crop["term_affinities"] else 0.0)
        window_end = _date_at_start(day) + timedelta(days=1)
        increment = base * multiplier * (window_end - window_start).total_seconds() / 86400.0
        if projected + increment >= target:
            fraction = 1.0 if increment <= 0 else max(0.0, min(1.0, (target - projected) / increment))
            return window_start + (window_end - window_start) * fraction
        projected += increment
        window_start = window_end
        day += timedelta(days=1)
    return None


def _seconds_until(now: datetime, target: datetime | None) -> int | None:
    if target is None:
        return None
    return max(0, int((target - now).total_seconds()))


def crop_timing(
    plot: dict,
    now: datetime,
    *,
    observations: list[dict] | None = None,
) -> dict:
    """返回前端可直接消费的作物时间事实与条件估算。

    未来天气无法确定，所以 growing 作物的成熟时刻都是按指定条件持续不变
    推演的估算；ready_at 则是已经发生的事实。所有倒计时都以 server_now 为
    基准，前端不需要复制季节、节气、土壤或浇水规则。
    """
    observations = observations or []
    crop = garden_crops.CROPS[plot["crop_id"]]
    target = float(crop["growth_days"])
    points = float(plot["growth_points"])
    remaining_points = max(0.0, target - points)
    condition = _active_condition(plot)
    if plot.get("status") == "ready":
        timing_status = "ready"
    elif plot.get("status") == "withered":
        timing_status = "withered"
    elif condition is not None:
        timing_status = "paused_condition"
    else:
        timing_status = "growing"

    soil = plot.get("soil")
    has_environment = isinstance(soil, dict)
    current_band = (
        garden_weather.moisture_band(float(soil.get("moisture", 55.0)))
        if has_environment else None
    )
    current_multiplier = _current_timing_multiplier(plot, now, observations)
    dry_multiplier = (
        _timing_multiplier_for_band(plot, "dry", now, observations)
        if has_environment else 1.0
    )
    watered_multiplier = (
        _timing_multiplier_for_band(plot, "moist", now, observations)
        if has_environment else 1.0
    )

    current_ready_at = _project_crop_ready_at(
        plot, now, multiplier=current_multiplier,
    )
    dry_ready_at = _project_crop_ready_at(
        plot, now, multiplier=dry_multiplier,
    )
    if timing_status == "ready":
        current_ready_at = now
        dry_ready_at = now

    today = _today_key(now)
    can_water_help = bool(
        plot.get("status") == "growing"
        and condition is None
        and (
            (has_environment and float(soil.get("moisture", 55.0)) < 50.0)
            or (
                not has_environment
                and int(plot.get("watering_by_date", {}).get(today, 0)) == 0
            )
        )
    )
    water_bonus = 0.0 if has_environment else (0.25 if can_water_help else 0.0)
    after_water_ready_at = _project_crop_ready_at(
        plot,
        now,
        multiplier=watered_multiplier if can_water_help else current_multiplier,
        bonus_points=water_bonus,
    )
    if timing_status == "ready":
        after_water_ready_at = now
    current_seconds = _seconds_until(now, current_ready_at)
    after_water_seconds = _seconds_until(now, after_water_ready_at)
    saved_seconds = (
        max(0, current_seconds - after_water_seconds)
        if can_water_help and current_seconds is not None and after_water_seconds is not None
        else 0
    )

    ready_at_raw = plot.get("ready_at")
    actual_ready_at = (
        _parse_iso(ready_at_raw, field="作物成熟时间").isoformat()
        if ready_at_raw is not None else None
    )
    pause_reason = None
    if condition is not None:
        pause_reason = f"condition:{condition['type']}"
    elif timing_status == "withered":
        pause_reason = "withered"

    return {
        "schema_version": TIMING_SCHEMA_VERSION,
        "server_now": now.isoformat(),
        "status": timing_status,
        "pause_reason": pause_reason,
        "is_estimate": timing_status not in ("ready", "withered"),
        "estimate_basis": "current_conditions_constant",
        "base_duration_seconds": int(crop["growth_days"]) * 24 * 60 * 60,
        "growth_target_points": target,
        "growth_points": points,
        "remaining_growth_points": round(remaining_points, 4),
        "progress_ratio": round(min(1.0, max(0.0, points / target)), 6),
        "actual_ready_at": actual_ready_at,
        "current": {
            "soil_band": current_band,
            "multiplier": round(current_multiplier, 4),
            "estimated_ready_at": current_ready_at.isoformat() if current_ready_at else None,
            "remaining_seconds": current_seconds,
        },
        "after_watering": {
            "would_help": can_water_help,
            "multiplier": round(watered_multiplier if can_water_help else current_multiplier, 4),
            "bonus_growth_points": water_bonus,
            "estimated_ready_at": after_water_ready_at.isoformat() if after_water_ready_at else None,
            "remaining_seconds": after_water_seconds,
            "time_saved_seconds": saved_seconds,
        },
        "continuous_dry": {
            "multiplier": round(dry_multiplier, 4),
            "estimated_ready_at": dry_ready_at.isoformat() if dry_ready_at else None,
            "remaining_seconds": _seconds_until(now, dry_ready_at),
        },
    }


def crop_catalog_snapshot() -> dict[str, dict]:
    """前端作物目录；基础时长与适种季节只由后端目录维护。"""
    return {
        crop_id: {
            "name": crop["name"],
            "seasons": list(crop["seasons"]),
            "base_duration_seconds": int(crop["growth_days"]) * 24 * 60 * 60,
            "growth_target_points": float(crop["growth_days"]),
        }
        for crop_id, crop in garden_crops.CROPS.items()
    }


def animal_product_name(item_id: str) -> str:
    try:
        return ANIMAL_PRODUCT_NAMES[item_id]
    except KeyError as exc:
        raise GardenError("小院子动物产物名称无效") from exc


def _plot_snapshot(plot: dict, now: datetime, observations: list[dict]) -> dict:
    snapshot = dict(plot)
    if plot.get("status") != "empty" and plot.get("crop_id") in garden_crops.CROPS:
        snapshot["timing"] = crop_timing(plot, now, observations=observations)
    return snapshot


def growth_environment_note(plot: dict, now: datetime) -> str | None:
    """查看页用的极简生长环境提示；只在昨天（最近一个完整北京时间日期）
    明显偏快或偏慢时才给一句话，平常范围不刷屏、不逐日播报。"""
    if not real_environment_enabled() or plot.get("status") != "growing":
        return None
    soil = plot.get("soil")
    crop = garden_crops.CROPS.get(plot.get("crop_id"))
    if not isinstance(soil, dict) or crop is None:
        return None
    yesterday = (now.astimezone(TZ).date() - timedelta(days=1)).isoformat()
    bucket = soil.get("exposure_by_date", {}).get(yesterday)
    multiplier = garden_weather.daily_growth_multiplier(
        bucket, heat_tendency=crop.get("heat_tendency", "neutral"),
    )
    if multiplier >= 1.05:
        return "这几天长得比平时快一点"
    if multiplier <= 0.85:
        return "这几天长得比平时慢一点"
    return None


def _environment_observations(now: datetime) -> list[dict]:
    """在取得 garden 锁前复制天气快照；缓存故障自然降级为空。"""
    if not real_environment_enabled():
        return []
    try:
        import weather
        return weather.load_weather_observations(now=now)
    except Exception:
        return []


def _animal_weather_mode(observations: list[dict], now: datetime) -> str:
    """派生动物当前的天气行为模式；总开关关闭或没有新鲜观测时保持
    ``normal``，完整退回第四版行为，不凭空给动物编造天气反应。"""
    if not real_environment_enabled():
        return "normal"
    fresh = [item for item in observations if garden_weather.fresh_at(item, now)]
    latest = max(fresh, key=garden_weather.observation_sort_key) if fresh else None
    tags = frozenset((latest or {}).get("tags") or [])
    wind_scale = (latest or {}).get("wind_scale_max")
    ground_wet = garden_weather.ground_recently_rained(observations, now)
    day_period = calendar_context(now).day_period
    return garden_weather.animal_weather_mode(
        tags, day_period=day_period, wind_scale=wind_scale, ground_wet=ground_wet,
    )


def _fresh_environment_weather_tags(observations: list[dict], now: datetime) -> frozenset[str]:
    """独立院子入口也只从新鲜实况取得文案天气，不依赖 daemon 传参。"""
    fresh = [item for item in observations if garden_weather.fresh_at(item, now)]
    if not fresh:
        return frozenset()
    latest = max(fresh, key=garden_weather.observation_sort_key)
    return frozenset(str(tag) for tag in latest.get("tags") or ())


def _environment_snapshot(state: dict, observations: list[dict], now: datetime) -> dict | None:
    """返回可见环境快照；院级积水指数只在读时派生，绝不写进存档。"""
    environment = state.get("environment")
    if not real_environment_enabled() or not isinstance(environment, dict):
        return dict(environment) if isinstance(environment, dict) else None
    checked = [
        item for raw in observations
        if (item := garden_weather.validate_observation(raw, now=now)) is not None
        and garden_weather.parse_timestamp(item["observed_at"]) <= now
    ]
    location_id = environment.get("location_id")
    relevant = [
        item for item in checked
        if location_id is None or item.get("location_id") == location_id
    ]
    rain_by_date = environment.get("rain_by_date")
    streak = garden_weather.rain_streak_days(rain_by_date, now)
    flood_watch = environment.get("flood_watch")
    drained_at = (
        garden_weather.parse_timestamp(flood_watch.get("drained_at"))
        if isinstance(flood_watch, dict) else None
    )
    index = garden_weather.yard_water_index(relevant, rain_by_date, now, drained_at=drained_at)
    visible = dict(environment)
    visible.update({
        "rain_streak_days": streak,
        "yard_water": garden_weather.yard_water_level(index),
    })
    return visible


def _reconcile_yard_water_event(state: dict, level: str, now: datetime) -> bool:
    """在院子锁内对齐内涝通知游标，并只为 flooded 边界排一次可靠事件。

    老存档首次见到该字段时只建立当前事实基线，不补播上线前已经发生的内涝。
    反向跨界不足两小时先不改游标：若水位持续在新档，冷却结束后的下一次
    结算仍会补发；若很快弹回，则自然不会制造一进一出的噪音。
    """
    environment = state.get("environment")
    if not isinstance(environment, dict) or level not in {"none", "puddles", "flooded"}:
        return False
    cursor = environment.get("yard_water_announced")
    if cursor is None:
        environment["yard_water_announced"] = {"level": level, "at": None}
        return True
    if not isinstance(cursor, dict):
        return False  # 严格存档校验会拦截；这里保留静默防御，绝不覆盖坏游标。
    previous = cursor.get("level")
    if previous == level:
        return False
    crossed = (previous == "flooded") != (level == "flooded")
    if not crossed:
        cursor["level"] = level
        return True
    previous_at = garden_weather.parse_timestamp(cursor.get("at"))
    if previous_at is not None:
        elapsed = now - previous_at.astimezone(TZ)
        if timedelta(0) <= elapsed < YARD_WATER_EVENT_COOLDOWN:
            return False
    transition = "entered" if level == "flooded" else "exited"
    pool = garden_content.YARD_WATER_EVENT_TEXT.get(transition)
    if not pool:
        return False
    stable_key = f"{transition}:{now.isoformat()}"
    stable_index = int(hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:8], 16)
    event_id = f"yard-water:{transition}:{now.strftime('%Y%m%dT%H%M%S')}:{uuid.uuid4().hex[:8]}"
    state["pending_events"].append({
        "event_id": event_id,
        "type": "yard_water",
        "transition": transition,
        "level": level,
        "occurred_at": now.isoformat(),
        "text": pool[stable_index % len(pool)],
    })
    environment["yard_water_announced"] = {
        "level": level,
        "at": now.isoformat(),
    }
    return True


def _settle_yard_water_event(
    state: dict,
    now: datetime,
    observations: list[dict],
) -> bool:
    if not real_environment_enabled():
        return False
    visible = _environment_snapshot(state, observations, now) or {}
    return _reconcile_yard_water_event(
        state, str(visible.get("yard_water") or "none"), now,
    )


def _queue_flood_damage_event(state: dict, stage: str, theoretical_at: datetime) -> None:
    """排一条院子级内涝惩罚事件；不逐地块刷屏，同一次浸泡只排一条。

    ``event_id`` 完全由 ``stage`` 与理论时刻派生，天然幂等——同一浸泡
    周期反复结算也只会追加一次，泡烂/欠佳分属不同 stage 互不影响。
    """
    pool = garden_content.FLOOD_DAMAGE_EVENT_TEXT.get(stage)
    if not pool:
        return
    event_id = f"flood-damage:{stage}:{theoretical_at.isoformat()}"
    if any(
        isinstance(event, dict) and event.get("event_id") == event_id
        for event in state["pending_events"]
    ):
        return
    stable_index = int(hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:8], 16)
    state["pending_events"].append({
        "event_id": event_id,
        "type": "flood_damage",
        "stage": stage,
        "occurred_at": theoretical_at.isoformat(),
        "text": pool[stable_index % len(pool)],
    })


def _rot_plot(state: dict, plot: dict, rot_at: datetime) -> None:
    """把一块 growing/ready 地块判定为内涝泡烂，复用枯死链路的终态形状。

    监理裁决（设计稿第四节第4点）：地块已有 active 异常时不新建条目，
    直接把它收束为 failed 并补 ``cause``；没有异常时新建终态
    ``flood_rot`` 记录。两种情形都清掉这个 cycle 尚未展示的 pending
    事件，避免枯死后还冒出"发芽了"之类的旧阶段播报。
    """
    rot_iso = rot_at.isoformat()
    existing = plot.get("condition")
    active = existing if isinstance(existing, dict) and existing.get("status") == "active" else None
    if active is None:
        condition_id = f"condition:{plot['plot_id']}:{plot['cycle_id']}:{uuid.uuid4().hex}"
        condition = {
            "condition_id": condition_id,
            "type": "flood_rot",
            "status": "failed",
            "severity": "withered",
            "occurred_at": rot_iso,
            "announced_at": rot_iso,
            "yield_penalty": 1,
            "worsened_at": rot_iso,
            "resolved_at": None,
            "failed_at": rot_iso,
            "cause": "yard_flood",
        }
        plot["condition"] = condition
        state["journal"]["crop_incidents"].append({
            "condition_id": condition_id,
            "plot_id": plot["plot_id"],
            "crop_id": plot["crop_id"],
            "cycle_id": plot["cycle_id"],
            "type": "flood_rot",
            "occurred_at": rot_iso,
            "announced_at": rot_iso,
            "yield_penalty": 1,
            "outcome": "failed",
            "resolved_at": None,
            "failed_at": rot_iso,
            "nodes": [
                {"kind": "occurred", "at": rot_iso},
                {"kind": "failed", "at": rot_iso},
            ],
        })
    else:
        condition = active
        incident = _incident_for_condition(state, condition["condition_id"])
        announced_at = condition.get("announced_at") or rot_iso
        condition.update({
            "status": "failed",
            "severity": "withered",
            "yield_penalty": 1,
            "announced_at": announced_at,
            "worsened_at": rot_iso,
            "failed_at": rot_iso,
            "cause": "yard_flood",
        })
        incident.update({
            "announced_at": announced_at,
            "yield_penalty": 1,
            "outcome": "failed",
            "failed_at": rot_iso,
        })
        _append_incident_node(incident, "failed", rot_at)
    plot["status"] = "withered"
    plot["yield_penalty"] = 1
    plot_id = plot["plot_id"]
    cycle_id = plot["cycle_id"]
    condition_id = condition["condition_id"]
    state["pending_events"] = [
        event for event in state["pending_events"]
        if not (
            isinstance(event, dict) and (
                (
                    event.get("type") == "crop_stage"
                    and event.get("plot_id") == plot_id
                    and event.get("cycle_id") == cycle_id
                )
                or (
                    event.get("type") == "crop_condition"
                    and event.get("condition_id") == condition_id
                )
            )
        )
    ]


def _settle_flood_damage(state: dict, now: datetime) -> bool:
    """内涝时钟的结算维护与两级惩罚（欠佳标记、泡烂）。

    与 ``_reconcile_yard_water_event`` 同源取 ``yard_water`` 档位——本函数
    只在 ``_settle_environment``（进而 ``_settle_yard_water_event``）已经
    在本次结算里对齐过 ``yard_water_announced`` 游标之后运行，直接读那张
    游标已经完成的 2 小时防抖判断，不重复实现档位抖动的容忍逻辑。
    """
    if not real_environment_enabled() or state.get("version") != STATE_VERSION:
        return False
    environment = state.get("environment")
    if not isinstance(environment, dict):
        return False
    flood_watch = environment.get("flood_watch")
    if not isinstance(flood_watch, dict):
        return False
    cursor = environment.get("yard_water_announced")
    level = cursor.get("level") if isinstance(cursor, dict) else "none"
    changed = False
    since = garden_weather.parse_timestamp(flood_watch.get("since"))
    if level == "flooded":
        if since is None:
            since = now
            flood_watch["since"] = now.isoformat()
            changed = True
    elif since is not None:
        since = None
        flood_watch["since"] = None
        changed = True
    if since is None:
        return changed
    drained_at = garden_weather.parse_timestamp(flood_watch.get("drained_at"))
    effective_start = since
    if drained_at is not None and drained_at > since:
        effective_start = drained_at
    rot_at = effective_start + garden_weather.FLOOD_SOAK_ROT_AFTER
    quality_at = effective_start + garden_weather.FLOOD_SOAK_QUALITY_AFTER
    eligible = [plot for plot in state["plots"] if plot.get("status") in ("growing", "ready")]
    if now >= rot_at:
        rotted_any = False
        for plot in eligible:
            _rot_plot(state, plot, rot_at)
            rotted_any = True
        if rotted_any:
            _queue_flood_damage_event(state, "rot", rot_at)
            changed = True
    elif now >= quality_at:
        # 只有真正"新"打上欠佳标记的这一次才排事件；已经欠佳的地块
        # （沿用旧周期或已处理过）不重复计入，天然幂等且不刷屏。
        flagged_any = False
        for plot in eligible:
            if plot.get("quality") != "poor":
                plot["quality"] = "poor"
                # 第八版：内涝（天灾）成因标"flood"，退水后可用肥料×1挽救
                # （设计稿第一节第2点，唯一的洗白例外）。
                plot["quality_cause"] = "flood"
                flagged_any = True
        if flagged_any:
            _queue_flood_damage_event(state, "quality", quality_at)
            changed = True
    return changed


def claim_outing_flavor(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    rng=None,
) -> dict | None:
    """为一次已经成功的院子动作原子领取一条出门天气短句。

    同档 30 分钟内只说一次；换档立即恢复。天气缺失、游标损坏或文案池
    缺失都静默降级，不影响动作本体已经确定的结果。
    """
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    rng = rng or random
    observations = _environment_observations(now)
    fresh = [item for item in observations if garden_weather.fresh_at(item, now)]
    latest = max(fresh, key=garden_weather.observation_sort_key) if fresh else None
    mode = garden_weather.outing_mode(latest)
    if mode == "calm":
        return None
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now, rng=rng)
        changed = _settle_crops(
            state, now, observations=observations,
        ) or bool(state.get("_migrated"))
        environment = state.get("environment")
        if not isinstance(environment, dict):
            if changed:
                _write_state_unlocked(state, path)
            return None
        visible = _environment_snapshot(state, observations, now) or {}
        flavor = {
            "mode": mode,
            "yard_water": str(visible.get("yard_water") or "none"),
            "rain_streak_days": int(visible.get("rain_streak_days") or 0),
        }
        cursor = environment.get("outing_flavor")
        if isinstance(cursor, dict) and cursor.get("mode") == mode:
            previous_at = garden_weather.parse_timestamp(cursor.get("at"))
            if previous_at is not None:
                elapsed = now - previous_at.astimezone(TZ)
                if timedelta(0) <= elapsed < OUTING_FLAVOR_COOLDOWN:
                    if changed:
                        _write_state_unlocked(state, path)
                    # 节流只省略独立出门句；写手仍得到当前事实，
                    # 才不会在动作正文里凭空换雨势或积水档。
                    return flavor
        position = rng.choice(("prefix", "aftermath"))
        text = garden_content.outing_flavor_line(
            mode, position, flavor["yard_water"], rng,
        )
        if not text:
            if changed:
                _write_state_unlocked(state, path)
            return flavor
        environment["outing_flavor"] = {"mode": mode, "at": now.isoformat()}
        _write_state_unlocked(state, path)
        return {
            **flavor,
            "position": position,
            "text": text,
        }


def _animal_weather_flavor(category: str, mode: str, rng, *, action: str | None = None) -> str | None:
    """从阶段 D 骨架池里取一句延续小句；模式是 normal 或池子缺失都返回
    None，调用方原样使用已有文案，不制造任何天气反应。

    "陪玩"本来就在描述持续的主动跑动，接一句"懒得动"的通用天气小句会
    读成动作互相矛盾；这类动作单独取自 ``ANIMAL_WEATHER_PLAY_LINES``，
    措辞本身已经写成承接"刚才还在动"的转折，其它动作沿用通用池。
    """
    if mode == "normal":
        return None
    pools = garden_content.ANIMAL_WEATHER_PLAY_LINES if action == "陪玩" else garden_content.ANIMAL_WEATHER_LINES
    pool = pools.get(category, {}).get(mode)
    return rng.choice(pool) if pool else None


def _merge_exposure_bucket(target: dict, delta: dict) -> None:
    for key, value in delta.items():
        target[key] = target.get(key, 0.0) + value


def _fold_plot_forward(plot: dict, target_at: datetime, observations: list[dict]) -> None:
    """把插件土壤的锚点永久推进到 ``target_at``（一旦晚到观测不再可能覆盖
    这段时间）。这段区间只会被完整重放并计入一次，此后既不重放也不再改写，
    天然避免重复计算降雨或暴露。"""
    soil = plot["soil"]
    anchor_at = _parse_iso(soil["anchor_at"], field="土壤锚点时间")
    if target_at <= anchor_at:
        return
    result = garden_weather.replay(float(soil["anchor_moisture"]), anchor_at, target_at, observations)
    finalized = soil.setdefault("exposure_finalized_by_date", {})
    for day, bucket in result.exposure_by_day.items():
        _merge_exposure_bucket(finalized.setdefault(day, _empty_exposure()), bucket)
    for obsolete in sorted(finalized)[:-8]:
        finalized.pop(obsolete, None)
    soil["anchor_moisture"] = result.moisture
    soil["anchor_at"] = target_at.isoformat()
    # 折叠之后这段时间不再可能被任何观测修正，此时才允许永久推进这两个
    # “最近一次事实”指针；跟 last_rain_at 一样，只在这次折叠确实算出新
    # 事实时才前移，不折叠时保持不变，天然单调。
    if result.last_rain_at is not None:
        soil["finalized_last_rain_at"] = result.last_rain_at.isoformat()
    if result.last_dip_below_50_at is not None:
        soil["finalized_last_dip_below_50_at"] = result.last_dip_below_50_at.isoformat()


def _refresh_plot_live(plot: dict, now: datetime, observations: list[dict]) -> None:
    """从已拍板的锚点纯函数重放到当前时刻，得到本次的实时水分与暴露。

    重放本身不留痕迹：同一份观测集合、同一个锚点，不管重放几次、之前查看
    过几次，结果都一样，因此不会重复计算蒸发或降雨。尾段算出的"最近降雨"
    与"最近一次跌破 50"都只是这一次的结论，随时可能被下一次更完整的观测
    推翻，因此只覆盖显示字段，不直接改写永久账本。
    """
    soil = plot["soil"]
    anchor_at = _parse_iso(soil["anchor_at"], field="土壤锚点时间")
    # 折叠是唯一会推进锚点的地方，且只在 now 晚于锚点时才推进；如果调用方
    # 传入的 now 没有晚于锚点（测试或时钟异常），此处不得据此把结算时间
    # 反向设到锚点之前，否则违反“锚点不晚于结算时间”的不变式。
    now = max(now, anchor_at)
    result = garden_weather.replay(float(soil["anchor_moisture"]), anchor_at, now, observations)
    merged = {day: dict(bucket) for day, bucket in soil.get("exposure_finalized_by_date", {}).items()}
    for day, bucket in result.exposure_by_day.items():
        _merge_exposure_bucket(merged.setdefault(day, _empty_exposure()), bucket)
    for obsolete in sorted(merged)[:-8]:
        merged.pop(obsolete, None)
    soil["exposure_by_date"] = merged
    soil["dry_hours"] = sum(bucket.get("dry_hours", 0.0) for bucket in merged.values())
    soil["saturated_hours"] = sum(bucket.get("saturated_hours", 0.0) for bucket in merged.values())
    soil["moisture"] = result.moisture
    soil["settled_at"] = now.isoformat()
    finalized_last_rain_raw = soil.get("finalized_last_rain_at")
    if result.last_rain_at is not None:
        resolved_last_rain_at = result.last_rain_at
    elif finalized_last_rain_raw is not None:
        resolved_last_rain_at = _parse_iso(finalized_last_rain_raw, field="土壤最近降雨时间")
    else:
        resolved_last_rain_at = None
    soil["last_rain_at"] = resolved_last_rain_at.isoformat() if resolved_last_rain_at is not None else None
    last_manual_raw = soil.get("last_manual_water_at")
    last_manual_at = _parse_iso(last_manual_raw, field="土壤最近人工浇水时间") if last_manual_raw is not None else None
    if resolved_last_rain_at is not None and (last_manual_at is None or resolved_last_rain_at > last_manual_at):
        soil["last_water_source"] = "rain"
    elif last_manual_at is not None:
        soil["last_water_source"] = "manual"
    else:
        soil["last_water_source"] = None
    finalized_dip_raw = soil.get("finalized_last_dip_below_50_at")
    if result.last_dip_below_50_at is not None:
        resolved_dip_at = result.last_dip_below_50_at
    elif finalized_dip_raw is not None:
        resolved_dip_at = _parse_iso(finalized_dip_raw, field="土壤最近跌破50时间")
    else:
        resolved_dip_at = None
    soil["tail_resolved_dip_at"] = resolved_dip_at.isoformat() if resolved_dip_at is not None else None
    _refresh_unneeded_display(plot)


def _settle_environment(state: dict, now: datetime, observations: list[dict]) -> bool:
    """在院子锁内按不可变天气快照精确结算一次，不联网也不写天气缓存。

    土壤水分与暴露统一交给 ``garden_weather.replay()`` 这一个纯函数计算：
    时间上距今超过 ``REPLAY_HORIZON``（晚到观测再也无法覆盖）的部分永久
    拍板一次；其余仍可能被晚到实况修正的时间段每次都从锚点完整重放，不管
    观测以什么顺序、分几批抵达，只要最终集合相同，结果就相同。
    """
    if state.get("version") != STATE_VERSION:
        return False
    environment = state["environment"]
    checked = [
        item for raw in observations
        if (item := garden_weather.validate_observation(raw, now=now)) is not None
    ]
    checked.sort(key=garden_weather.observation_sort_key)
    visible = [item for item in checked if garden_weather.parse_timestamp(item["observed_at"]) <= now]
    newest = visible[-1] if visible else None
    changed = False
    location_id = environment.get("location_id")
    if location_id is None and newest is not None:
        environment["location_id"] = newest["location_id"]
        location_id = newest["location_id"]
        changed = True
    if newest is not None and location_id is not None and newest["location_id"] != location_id:
        # 城市切换不把两地之间的时间积分；保留水分，从新实况出现时重新起算。
        old_relevant = [item for item in checked if item["location_id"] == location_id]
        switch_status = garden_weather.environment_status([newest], now)
        environment.update({
            "location_id": newest["location_id"], "last_settled_at": now.isoformat(),
            "last_observation_id": newest["observation_id"],
            "last_fresh_weather_at": newest["observed_at"] if switch_status == "fresh" else None,
            "status": switch_status, "rain_finalized_at": now.isoformat(),
            "rain_by_date": garden_weather.merge_rain_by_date(
                {},
                garden_weather.observed_rain_by_date([
                    item for item in visible
                    if item["location_id"] == newest["location_id"]
                ], now),
                now,
            ),
        })
        for plot in state["plots"]:
            soil = plot.get("soil")
            if isinstance(soil, dict):
                # 用旧城市的观测把尾段完整收口，不覆盖水分——切换本身不是
                # 一次确定性动作，只是提前把旧天气的可修正窗口关闭。
                _checkpoint_soil(plot, now, old_relevant)
        new_relevant = [
            item for item in checked
            if item["location_id"] == environment["location_id"]
        ]
        _settle_yard_water_event(state, now, new_relevant)
        return True
    if location_id is None:
        rain_by_date = garden_weather.merge_rain_by_date(
            environment.get("rain_by_date"), {}, now,
        )
        if environment.get("rain_by_date") != rain_by_date:
            environment["rain_by_date"] = rain_by_date
            changed = True
        if environment.get("status") != "missing":
            environment["status"] = "missing"
            changed = True
        changed = _settle_yard_water_event(state, now, []) or changed
        return changed
    relevant = [item for item in checked if item["location_id"] == location_id]
    rain_by_date = garden_weather.merge_rain_by_date(
        environment.get("rain_by_date"),
        garden_weather.observed_rain_by_date(relevant, now),
        now,
    )
    if environment.get("rain_by_date") != rain_by_date:
        environment["rain_by_date"] = rain_by_date
        changed = True
    # 新院子迁到 v5 时 last_settled_at 恰好等于本次 now，但这份首帧实况仍
    # 必须更新可见状态；时间积分可以不前进，天气事实不能因此留在 missing。
    current_status = garden_weather.environment_status(relevant, now)
    if environment.get("status") != current_status:
        environment["status"] = current_status
        changed = True
    if current_status == "fresh" and relevant:
        latest = max(relevant, key=garden_weather.observation_sort_key)
        if environment.get("last_observation_id") != latest["observation_id"]:
            environment["last_observation_id"] = latest["observation_id"]
            changed = True
        if environment.get("last_fresh_weather_at") != latest["observed_at"]:
            environment["last_fresh_weather_at"] = latest["observed_at"]
            changed = True
    last = _parse_iso(environment["last_settled_at"], field="环境结算时间")
    if now <= last:
        changed = _settle_yard_water_event(state, now, relevant) or changed
        return changed
    previous_finalized = _parse_iso(
        environment.get("rain_finalized_at") or environment["last_settled_at"],
        field="降雨结算锚点时间",
    )
    finalize_at = min(now, max(previous_finalized, now - garden_weather.REPLAY_HORIZON))
    for plot in state["plots"]:
        soil = plot.get("soil")
        if not isinstance(soil, dict):
            continue
        _fold_plot_forward(plot, finalize_at, relevant)
        _refresh_plot_live(plot, now, relevant)
    environment["rain_finalized_at"] = finalize_at.isoformat()
    environment["last_settled_at"] = now.isoformat()
    status = garden_weather.environment_status(relevant, now)
    environment["status"] = status
    if status == "fresh" and relevant:
        latest = max(relevant, key=garden_weather.observation_sort_key)
        environment["last_observation_id"] = latest["observation_id"]
        environment["last_fresh_weather_at"] = latest["observed_at"]
    _settle_yard_water_event(state, now, relevant)
    return True


def _daily_growth_rate(crop: dict, soil: dict | None, day) -> float:
    """日期 day 这一天的生长速率（生长点/整天），线性记账的唯一速率来源。

    与整天记账时代同一套口径：节气亲和 ×（前一个已完整结束的环境日期的
    天气倍率）。季节只挡"能不能新种"（见 plant_crop）；已经在地里的这一茬
    不因换季暂停生长，不然会出现种下时来得及、跨季后卡到下一次适种季才能
    继续这种"卡边"体验。倍率绝不读 day 自己（可能还没过完），只读它前一
    个、此刻必然已经完整结束的环境日期 day-1，所以一天之内速率恒定且在这
    天开始时就已定型，生长量是时间的确定性分段线性函数，跟结算批次、频率
    完全无关，长跳和逐段结算天然收敛到同一个结果。
    """
    context = calendar_context(_date_at_start(day))
    base = 1.0 + (0.1 if context.term_id in crop["term_affinities"] else 0.0)
    multiplier = 1.0
    if soil is not None:
        environment_day = day - timedelta(days=1)
        bucket = soil.get("exposure_by_date", {}).get(environment_day.isoformat())
        multiplier = garden_weather.daily_growth_multiplier(
            bucket, heat_tendency=crop.get("heat_tendency", "neutral"),
        )
    return base * multiplier


def _accrue_linear_growth(
    crop: dict,
    soil: dict | None,
    start: datetime,
    now: datetime,
    *,
    points: float,
    target: float,
) -> tuple[float, datetime | None]:
    """按天分段线性累积 [start, now) 的生长增量。

    返回 (增量, 首次达到成熟线的时刻或 None)。成熟时刻在段内线性插值，
    与 _project_crop_ready_at 的预览共用同一套几何——倒计时说几点熟，
    结算就真的在几点翻转，不再出现"差不到一天的菜全在午夜齐熟"。
    """
    growth_delta = 0.0
    ready_at = None
    projected = points
    cursor = start
    while cursor < now:
        day = cursor.astimezone(TZ).date()
        day_end = _date_at_start(day) + timedelta(days=1)
        segment_end = min(now, day_end)
        rate = _daily_growth_rate(crop, soil, day)
        increment = rate * (segment_end - cursor).total_seconds() / 86400.0
        if ready_at is None and increment > 0 and projected < target <= projected + increment:
            fraction = (target - projected) / increment
            ready_at = cursor + (segment_end - cursor) * fraction
        projected += increment
        growth_delta += increment
        cursor = segment_end
    return growth_delta, ready_at


def _settle_coop_manure(state: dict, now: datetime) -> bool:
    """按在场成鸡数量线性结算鸡粪（设计稿第一节）。

    与作物线性生长同一套原则：不整段批记账。到上限后不再让时间入账
    （直接把 last_settled_at 推到 now），防止"攒满不扫、一扫瞬间又满"的
    时间银行；没有成鸡时同样直接推进游标，不让 0 产出的时段偷偷攒着，
    等哪天有了成鸡再一次性爆发出一大批鸡粪。
    """
    coop = state["coop"]
    manure = coop.get("manure")
    if not isinstance(manure, dict):
        manure = {"units": 0, "last_settled_at": None}
        coop["manure"] = manure
    last_raw = manure.get("last_settled_at")
    if last_raw is None:
        manure["last_settled_at"] = now.isoformat()
        return True
    last_settled = _parse_iso(last_raw, field="鸡粪结算时间")
    if now <= last_settled:
        return False
    units = int(manure.get("units", 0))
    if units >= MANURE_CAP:
        manure["last_settled_at"] = now.isoformat()
        return True
    adults = sum(
        1 for chick in coop.get("chicks", [])
        if isinstance(chick, dict) and chick.get("stage") == "adult"
    )
    if adults <= 0:
        manure["last_settled_at"] = now.isoformat()
        return True
    elapsed_seconds = (now - last_settled).total_seconds()
    produced = int((elapsed_seconds * adults) // 86400)
    if produced <= 0:
        return False
    new_units = min(MANURE_CAP, units + produced)
    actually_produced = new_units - units
    if new_units >= MANURE_CAP:
        manure["last_settled_at"] = now.isoformat()
    else:
        # 只推进已经折算成整份的那段时间，零头留给下次结算接着算——跟
        # _accrue_linear_growth 的分段思路一致，只是这里份数是整数。
        advance_seconds = actually_produced * 86400.0 / adults
        manure["last_settled_at"] = (last_settled + timedelta(seconds=advance_seconds)).isoformat()
    manure["units"] = new_units
    return True


def _queue_compost_ready_event(state: dict, batch: dict, ready_at: datetime) -> None:
    """堆肥批次到期即排一条展示事件；event_id 由 batch_id 派生，天然幂等。"""
    pool = garden_content.COMPOST_READY_EVENT_TEXT
    if not pool:
        return
    event_id = f"compost-ready:{batch['batch_id']}"
    if any(
        isinstance(event, dict) and event.get("event_id") == event_id
        for event in state["pending_events"]
    ):
        return
    units = int(batch.get("units", 0))
    stable_index = int(hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:8], 16)
    state["pending_events"].append({
        "event_id": event_id,
        "type": "compost_ready",
        "units": units,
        "occurred_at": ready_at.isoformat(),
        "text": pool[stable_index % len(pool)].format(units=units),
    })


def _settle_compost(state: dict, now: datetime) -> bool:
    """堆肥批次到期即转成肥料入库；批次本身随之移除（设计稿第一节）。"""
    compost = state.get("compost")
    if not isinstance(compost, dict):
        compost = {"batches": []}
        state["compost"] = compost
    batches = compost.get("batches")
    if not isinstance(batches, list):
        batches = []
        compost["batches"] = batches
    changed = False
    remaining = []
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        ready_at = _parse_iso(batch.get("ready_at"), field="堆肥批次到期时间")
        if now >= ready_at:
            units = int(batch.get("units", 0))
            if units > 0:
                _inventory_add(state["inventory"], "fertilizer", "fertilizer", units)
            _queue_compost_ready_event(state, batch, ready_at)
            changed = True
        else:
            remaining.append(batch)
    if changed:
        compost["batches"] = remaining
    return changed


def _settle_coop_byproducts(state: dict, now: datetime) -> bool:
    """鸡粪与堆肥的成对结算——凡需要推进鸡舍副产品时间的入口一律走这里，
    不要各自手抄两个调用：结算清单在多个入口之间漂移是历史上出过的真实
    bug（新增的第三项副产品结算只要加在这里，所有入口自动跟上）。"""
    changed = _settle_coop_manure(state, now)
    return _settle_compost(state, now) or changed


def _settle_crops(state: dict, now: datetime, *, observations: list[dict] | None = None) -> bool:
    """按天分段线性结算；每天的生长点均匀摊在这一天的 24 小时里累积。"""
    changed = _ensure_crop_state(state, now)
    changed = _settle_coop_byproducts(state, now) or changed
    if real_environment_enabled() and state.get("version") == STATE_VERSION:
        changed = _settle_environment(state, now, observations or []) or changed
    changed = _settle_crop_conditions(state, now) or changed
    changed = _settle_flood_damage(state, now) or changed
    if state.get("growth_accounting") != "linear" and (
        state.get("legacy", {}).get("unrecognized_top_level", {})
        .get("growth_accounting") == "linear"
    ):
        # 新旧代码混跑保险：尚未重启的旧代码不认识 growth_accounting，
        # 会把它当未识别顶层字段扫进 legacy 桶再落盘；这里救回来，避免
        # 下面把已经迁移过的存档再迁移一遍、把结算游标又推后一天。
        state["growth_accounting"] = "linear"
        state["legacy"]["unrecognized_top_level"].pop("growth_accounting", None)
        changed = True
    if state.get("growth_accounting") != "linear":
        # 一次性切换整天记账 → 线性记账。整天时代"今天"的全额生长点在
        # 跨入当天的第一次结算就已入账（异常暂停快进的情形没入账，但旧
        # 口径同样"不补停掉的天数"），所以线性累积必须从 last_settled_at
        # 的下一个午夜整点接手：既不重复入账，也不留缝。接手时刻还没到
        # 就把结算游标直接停在那个未来的午夜——下面的结算循环会安静跳
        # 过它，等真实时间越过午夜再继续。
        for plot in state["plots"]:
            if plot.get("status") == "growing" and plot.get("last_settled_at"):
                last = _parse_iso(plot["last_settled_at"], field="作物结算时间")
                boundary = _date_at_start(last.astimezone(TZ).date() + timedelta(days=1))
                plot["last_settled_at"] = boundary.isoformat()
        state["growth_accounting"] = "linear"
        changed = True
    for plot in state["plots"]:
        if plot.get("status") != "growing":
            continue
        last = _parse_iso(plot["last_settled_at"], field="作物结算时间")
        if now <= last:
            continue
        # 异常从创建时就暂停生长，即使可靠事件尚未展示；但惩罚时钟只从
        # announced_at 开始。处理后也只从下一次结算继续，不补停掉的时段。
        if _active_condition(plot) is not None:
            plot["last_settled_at"] = now.isoformat()
            changed = True
            continue
        crop = garden_crops.CROPS[plot["crop_id"]]
        soil = plot.get("soil") if real_environment_enabled() and state.get("version") == STATE_VERSION else None
        growth_delta, ready_at = _accrue_linear_growth(
            crop, soil, last, now,
            points=float(plot["growth_points"]),
            target=float(crop["growth_days"]),
        )
        if growth_delta > 0:
            _apply_growth(state, plot, growth_delta, ready_at=ready_at)
        plot["last_settled_at"] = now.isoformat()
        changed = True
    return changed


def plot_id_from_selector(selector: str | None) -> str | None:
    if not selector:
        return None
    return _PLOT_SELECTOR_ALIASES.get("".join(selector.split()).lower())


#: 8/9 事故：`_find_plot` 无选择器分支曾把"零候选"和"多候选"合并成同一句
#: "现在有不止一个可选菜畦"，四块地全满（零个空地）时语义正好说反了。按
#: 调用方实际传入的 statuses（均为单元素元组）给出贴合场景的中文提示；未
#: 覆盖的组合退回一句中性但仍然通顺的兜底文案。
_NO_CANDIDATE_PLOT_MESSAGES = {
    "empty": "现在没有空着的菜畦，等收获腾出位置再种吧",
    "growing": "现在没有正在生长的菜畦，等种下什么再来浇水吧",
    "ready": "现在没有成熟待收获的菜畦，再等等它们长熟吧",
    "withered": "现在没有枯萎待清理的菜畦，用不着清理",
}


def _no_candidate_plot_message(statuses: tuple[str, ...]) -> str:
    if len(statuses) == 1 and statuses[0] in _NO_CANDIDATE_PLOT_MESSAGES:
        return _NO_CANDIDATE_PLOT_MESSAGES[statuses[0]]
    return "现在没有符合条件的菜畦"


def _find_plot(state: dict, selector: str | None, *, statuses: tuple[str, ...]) -> dict:
    candidates = [
        plot for plot in state["plots"]
        if plot.get("status") in statuses
    ]
    if selector:
        expected = plot_id_from_selector(selector)
        matches = [plot for plot in candidates if plot.get("plot_id") == expected]
        if expected is None:
            crop_id = garden_crops.resolve_crop(selector)
            matches = [plot for plot in candidates if plot.get("crop_id") == crop_id]
        if not matches:
            raise GardenError("没找到符合条件的菜畦，请检查编号和状态")
        if len(matches) > 1:
            raise GardenError("符合条件的菜畦不止一个，请带上地块编号")
        return matches[0]
    if not candidates:
        raise GardenError(_no_candidate_plot_message(statuses))
    if len(candidates) > 1:
        raise GardenError("现在有不止一个可选菜畦，请带上地块编号")
    return candidates[0]


def matches_entry_selector(entry: dict, selector: str) -> bool:
    """编号前缀、唯一昵称和唯一物种名都可作为自然交互目标。"""
    selector = selector.strip()
    return bool(selector) and (
        str(entry.get("id", "")).startswith(selector)
        or str(entry.get("nickname") or "").strip() == selector
        or str(entry.get("species") or "").strip() == selector
        or display_name(entry, include_species=True) == selector
    )


def _find_active_animal(state: dict, selector: str) -> dict:
    matches = [
        animal for animal in state["animals"]
        if animal.get("status") == "active" and matches_entry_selector(animal, selector)
    ]
    if not matches:
        raise GardenError(f"没找到名称或编号是 {selector} 的、还在的小动物")
    if len(matches) > 1:
        raise GardenError(f"{selector} 匹配到多条，请换用更明确的昵称或编号")
    return matches[0]


def _inventory_take(inventory: dict, section: str, item_id: str, amount: int = 1) -> None:
    if int(inventory[section].get(item_id, 0)) < amount:
        raise GardenError("篮子里数量不够，这次没有动任何东西")
    inventory[section][item_id] -= amount
    if inventory[section][item_id] == 0:
        inventory[section].pop(item_id)


def _inventory_add(inventory: dict, section: str, item_id: str, amount: int = 1) -> None:
    inventory[section][item_id] = int(inventory[section].get(item_id, 0)) + amount


def _take_produce_ranked(inventory: dict, crop_id: str, amount: int, *, prefer_poor: bool) -> int:
    """按品相优先级合并消耗 ``produce``/``produce_poor``（第七版阶段D，设计稿第六节）。

    ``prefer_poor=True``：欠佳优先（做菜、投喂零食——去化欠佳库存的自然通道）；
    ``prefer_poor=False``：好品相优先，好的不够才动欠佳（送礼）。
    数量不足时不改动任何状态，抛出与 ``_inventory_take`` 一致的错误文案；
    返回本次实际从 ``produce_poor`` 扣掉的数量，供调用方决定是否要在展示
    文案/手账记录里标注"品相欠佳"。
    """
    first, second = ("produce_poor", "produce") if prefer_poor else ("produce", "produce_poor")
    first_available = int(inventory[first].get(crop_id, 0))
    second_available = int(inventory[second].get(crop_id, 0))
    if first_available + second_available < amount:
        raise GardenError("篮子里数量不够，这次没有动任何东西")
    from_first = min(first_available, amount)
    from_second = amount - from_first
    if from_first:
        _inventory_take(inventory, first, crop_id, from_first)
    if from_second:
        _inventory_take(inventory, second, crop_id, from_second)
    return from_first if prefer_poor else from_second


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def claim_session_intro(
    session_id: str | None,
    *,
    path: Path | None = None,
) -> str:
    """同一 Claude session 只认领一次院子开场；院子存档本身不记该状态。"""
    session_id = str(session_id or "").strip()
    if not session_id or len(session_id) > 128 or any(ord(char) < 32 for char in session_id):
        return ""
    path = path or GARDEN_SESSION_INTRO_FILE
    try:
        with _locked(path, exclusive=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                payload = {}
            seen = payload.get("seen_session_ids")
            if not isinstance(seen, list) or any(not isinstance(item, str) for item in seen):
                seen = []
            if session_id in seen:
                return ""
            seen.append(session_id)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=path.parent, text=True,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                    json.dump({"seen_session_ids": seen}, target, ensure_ascii=False, indent=2)
                    target.write("\n")
                os.replace(temp_name, path)
            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
    except OSError:
        # 开场去重是辅助状态：写不了时宁可偶尔重复，也不能让已经成功的院子
        # 动作或整轮自主唤醒因为一段引导文案而失败。
        return GARDEN_SESSION_INTRO
    return GARDEN_SESSION_INTRO


def swap_view_fingerprint(
    session_id: str | None,
    fingerprint: dict,
    *,
    path: Path | None = None,
) -> dict | None:
    """记录本次整院查看的状态指纹，返回同一 session 上一次的指纹。

    返回 None 表示本 session 还没看过（或状态文件不可用），应展示完整概览；
    指纹只存最近一个 session——session 是串行推进的，旧 session 不会再查看。
    """
    session_id = str(session_id or "").strip()
    if not session_id or len(session_id) > 128 or any(ord(char) < 32 for char in session_id):
        return None
    path = path or GARDEN_VIEW_STATE_FILE
    try:
        with _locked(path, exclusive=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                payload = {}
            previous = (
                payload.get("fingerprint")
                if isinstance(payload, dict)
                and payload.get("session_id") == session_id
                and isinstance(payload.get("fingerprint"), dict)
                else None
            )
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=path.parent, text=True,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                    json.dump(
                        {"session_id": session_id, "fingerprint": fingerprint},
                        target, ensure_ascii=False, indent=2,
                    )
                    target.write("\n")
                os.replace(temp_name, path)
            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
    except OSError:
        # 查看指纹是辅助状态：写不了时退回完整概览，不能让查看本身失败。
        return None
    return previous


@contextmanager
def _locked(path: Path, *, exclusive: bool):
    """跨 daemon 线程与 home 子进程保护 garden.json 的完整读改写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(path)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _scene_generation_locked(path: Path):
    """跨 home 子进程合并同键生成；不占用 garden.json 的状态锁。"""
    lock_path = path.with_name(f".{path.name}.scene.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _next_visit_after(now: datetime, minimum: int, maximum: int) -> str:
    """自然访问使用稳定的中点间隔，不把事件骰子的随机性耦合进存档迁移。"""
    return (now + timedelta(hours=(minimum + maximum) / 2)).isoformat()


def _bond_points_from_care_count(care_count: object) -> int:
    count = max(0, int(care_count or 0))
    if count == 0:
        return 0
    if count <= 2:
        return 3
    if count <= 5:
        return 8
    if count <= 9:
        return 16
    return 28


def _bond_level(points: int) -> int:
    return next((level for level, threshold, _ in reversed(BOND_LEVELS) if points >= threshold), 0)


def bond_level_name(level: int) -> str:
    return next((name for candidate, _, name in BOND_LEVELS if candidate == level), BOND_LEVELS[0][2])


def _today_key(now: datetime) -> str:
    return now.astimezone(TZ).date().isoformat()


def _validate_bond_ledger(entry: dict) -> None:
    actions = entry.get("bond_actions_by_date")
    if actions is not None and not isinstance(actions, dict):
        raise GardenError("小院子亲密日账本格式损坏，已停止写入以免覆盖原记录")
    if isinstance(actions, dict):
        for day, values in actions.items():
            if not isinstance(day, str) or not isinstance(values, dict):
                raise GardenError("小院子亲密日账本格式损坏，已停止写入以免覆盖原记录")
            if any(not isinstance(action, str) or not isinstance(points, int) for action, points in values.items()):
                raise GardenError("小院子亲密日账本格式损坏，已停止写入以免覆盖原记录")
    milestones = entry.get("bond_milestones_seen")
    if milestones is not None and not isinstance(milestones, list):
        raise GardenError("小院子亲密里程碑格式损坏，已停止写入以免覆盖原记录")


def _prune_bond_ledger(entry: dict) -> None:
    actions = entry["bond_actions_by_date"]
    for day in sorted(actions)[:-_BOND_ACTION_HISTORY_DAYS]:
        actions.pop(day, None)


def _daily_bond_total(entry: dict, day: str) -> int:
    values = entry["bond_actions_by_date"].get(day, {})
    return sum(max(0, int(points)) for points in values.values())


def _queue_bond_milestone(state: dict, entry: dict, level: int) -> None:
    event_id = f"bond:{entry['id']}:level:{level}"
    seen = entry["bond_milestones_seen"]
    if event_id in seen:
        return
    if any(event.get("event_id") == event_id for event in state["pending_events"] if isinstance(event, dict)):
        return
    state["pending_events"].append({
        "event_id": event_id,
        "type": "bond_milestone",
        "animal_id": entry["id"],
        "animal_name": display_name(entry),
        "level": level,
        "category": entry.get("category"),
        "personality": entry.get("personality"),
    })


def _award_bond_points(state: dict, entry: dict, action: str, now: datetime) -> dict:
    """在同一把状态锁内记一次动作，重复动作仍有反应但绝不重复加分。"""
    day = _today_key(now)
    ledger = entry["bond_actions_by_date"]
    today = ledger.setdefault(day, {})
    _prune_bond_ledger(entry)
    if action in today:
        return {"delta": 0, "repeated": True, "capped": _daily_bond_total(entry, day) >= DAILY_BOND_POINT_CAP}

    before_level = _bond_level(entry["bond_points"])
    total = _daily_bond_total(entry, day)
    delta = 1 if total < DAILY_BOND_POINT_CAP else 0
    today[action] = delta
    entry["bond_points"] += delta
    level = _bond_level(entry["bond_points"])
    entry["bond_level"] = level
    if level >= 5:
        entry["residency"] = "resident"
        entry.setdefault("home_spot", entry["spot"])
    if delta and level > before_level:
        _queue_bond_milestone(state, entry, level)
    return {"delta": delta, "repeated": False, "capped": delta == 0, "level": level}


def _normalize_animal(entry: dict, now: datetime, rng) -> dict:
    """补齐 v3 动物字段；不覆写既有资料或时间线。"""
    result = dict(entry)
    original_spot = result.get("spot")
    if original_spot in _LEGACY_SPOT_MAP:
        result["spot"] = _LEGACY_SPOT_MAP[original_spot]
        result.setdefault("legacy", {}).setdefault("original_spot", original_spot)
    elif result.get("spot") not in SPOTS:
        result.setdefault("legacy", {}).setdefault("original_spot", original_spot)
        result["spot"] = SPOTS[0]
    if result.get("category") is None:
        result["category"] = garden_content.category_from_species(str(result.get("species") or ""))
    if result.get("personality") is None:
        result["personality"] = rng.choice(garden_content.PERSONALITIES)
    _validate_bond_ledger(result)
    points = max(0, int(result.get("bond_points", _bond_points_from_care_count(result.get("care_count")))))
    result["bond_points"] = points
    result["bond_level"] = _bond_level(points)
    result.setdefault("bond_actions_by_date", {})
    result.setdefault("bond_milestones_seen", [])
    result.setdefault("nickname_bonus_claimed", bool(result.get("nickname")))
    result["residency"] = "resident" if points >= 45 else "visitor"
    if result.get("home_spot") is not None and result["home_spot"] not in SPOTS:
        raise GardenError("小院子常住落脚处格式损坏，已停止写入以免覆盖原记录")
    if result["residency"] == "resident":
        result.setdefault("home_spot", result["spot"])
    if result.get("preferred_action") is None:
        result["preferred_action"] = rng.choice(("投喂", "摸摸"))
    result.setdefault("last_visited_at", result.get("arrived_at"))
    if result.get("next_natural_visit_after") is None:
        result["next_natural_visit_after"] = _next_visit_after(
            now, NATURAL_VISIT_MIN_HOURS, NATURAL_VISIT_MAX_HOURS,
        )
    result.setdefault("status", "active")
    return result


def _project_entries(state: dict) -> list[dict]:
    return list(state["animals"]) + list(state["legacy_plants"])


def _sync_projected_entries(state: dict) -> None:
    entries = state.get("entries")
    if entries is None:
        return
    state["animals"] = [entry for entry in entries if isinstance(entry, dict) and entry.get("kind") == "animal"]
    state["legacy_plants"] = [entry for entry in entries if isinstance(entry, dict) and entry.get("kind") != "animal"]


def _migrate_v2_state(data: dict | list, now: datetime, rng) -> dict:
    old_entries = data if isinstance(data, list) else data.get("entries", [])
    if not isinstance(old_entries, list):
        raise GardenError("小院子状态格式无法识别，已停止写入")
    state = _empty_state()
    if isinstance(data, dict) and isinstance(data.get("meta"), dict):
        state["meta"].update(data["meta"])
    if isinstance(data, dict):
        known = {"version", "entries", "meta"}
        state["legacy"]["unrecognized_top_level"] = {key: value for key, value in data.items() if key not in known}
    for raw in old_entries:
        if not isinstance(raw, dict):
            state["legacy"]["unrecognized_entries"].append(raw)
            continue
        if raw.get("kind") == "animal":
            state["animals"].append(_normalize_animal(raw, now, rng))
        elif raw.get("kind") == "flower":
            plant = dict(raw)
            plant.setdefault("legacy", {}).setdefault("migrated_from", "v2_flower")
            state["legacy_plants"].append(plant)
            state["journal"]["legacy_plants"].append(str(plant.get("id") or plant.get("species") or "旧花草"))
        else:
            state["legacy"]["unrecognized_entries"].append(raw)
    _ensure_plot_layout(state)
    state["entries"] = _project_entries(state)
    state["_migrated"] = True
    return state


def _backfill_v5_soil_fields(state: dict) -> bool:
    """阶段 B 施工期间 v5 内部字段多轮增补；同一 ``version == 5`` 下缺新
    字段的旧存档不算损坏，按"没有遗留尾段、没有额外历史事实"的保守默认
    补齐一次，再交给校验。生产开关尚未开启，真实存档不会经过这条路径；
    这里只保证开发期间遗留的合法 v5 快照仍能被继续读取。

    返回值标记这次是否真的补了什么——调用方要据此决定是否需要把结果
    写回磁盘，不能假设别处（比如作物生长结算）总会顺带触发一次写入；
    生长结算变得只在真正有新进展时才标记改动之后，backfill 自己不报告
    changed 就会让这次补齐停留在内存里，下次读取又要重新补一遍。"""
    if state.get("version") != STATE_VERSION:
        return False
    changed = False
    environment = state.get("environment")
    if isinstance(environment, dict) and "rain_by_date" not in environment:
        # 老 v5 存档不追溯历史雨量，从上线后的第一份观测开始记账。
        environment["rain_by_date"] = {}
        changed = True
    if isinstance(environment, dict) and "flood_watch" not in environment:
        # 第七版阶段 A：老 v5 存档不追溯上线前已经泡了多久，只建立中性
        # 起点；真正的 since 由后续结算在发现 flooded 时写入（阶段 B）。
        environment["flood_watch"] = {"since": None, "drained_at": None}
        changed = True
    for plot in state.get("plots", []):
        if plot.get("status") == "empty":
            continue
        soil = plot.get("soil")
        if isinstance(soil, dict):
            if "anchor_at" not in soil and isinstance(soil.get("settled_at"), str):
                soil["anchor_at"] = soil["settled_at"]
                changed = True
            if "anchor_moisture" not in soil and isinstance(soil.get("moisture"), (int, float)):
                soil["anchor_moisture"] = soil["moisture"]
                changed = True
            if "exposure_finalized_by_date" not in soil:
                soil["exposure_finalized_by_date"] = dict(soil.get("exposure_by_date") or {})
                changed = True
            if "finalized_last_rain_at" not in soil:
                # last_rain_at 只有落在锚点之前（已经折叠过的历史）才能安全
                # 升级成永久事实；如果它其实还在锚点之后，说明是旧版本里
                # 仍可被天气修正撤销的尾段结论，直接冻结成永久会让后续的
                # 干燥纠正再也无法撤销它。用 parse_timestamp 而非 _parse_iso，
                # 因为这里在校验之前，格式本身损坏交给随后的 _validate_soil
                # 处理，不该由补齐逻辑本身抛错中断迁移。
                last_rain_at = garden_weather.parse_timestamp(soil.get("last_rain_at"))
                anchor_at = garden_weather.parse_timestamp(soil.get("anchor_at"))
                if last_rain_at is not None and anchor_at is not None and last_rain_at <= anchor_at:
                    soil["finalized_last_rain_at"] = soil["last_rain_at"]
                else:
                    soil["finalized_last_rain_at"] = None
                changed = True
            if "finalized_last_dip_below_50_at" not in soil:
                soil["finalized_last_dip_below_50_at"] = None
                changed = True
            if "tail_clear_unneeded_days" in soil:
                soil.pop("tail_clear_unneeded_days", None)
                changed = True
            if "tail_resolved_dip_at" not in soil:
                soil["tail_resolved_dip_at"] = None
                changed = True
        if not isinstance(plot.get("unneeded_watering_events"), dict):
            legacy_counts = plot.get("unneeded_watering_by_date")
            placeholder = soil.get("settled_at") if isinstance(soil, dict) else None
            events: dict[str, list[str]] = {}
            if isinstance(legacy_counts, dict) and isinstance(placeholder, str):
                for day, count in legacy_counts.items():
                    if isinstance(count, int) and count > 0:
                        events[day] = [placeholder] * count
            plot["unneeded_watering_events"] = events
            changed = True
        # 防御性裁剪：不管这份存档是怎么落到超过 8 天的（旧代码没裁剪、
        # 手工构造等），读取路径本身也要保证不会把自己判成损坏。
        events_by_date = plot["unneeded_watering_events"]
        if isinstance(events_by_date, dict):
            obsolete_keys = sorted(events_by_date)[:-8]
            if obsolete_keys:
                for obsolete in obsolete_keys:
                    events_by_date.pop(obsolete, None)
                changed = True
    return changed


_CHICKEN_PROFILE_FIELDS = (
    "profile_id", "chick_appearance", "adult_appearance", "personality", "intro",
)


def _profile_payload(profile: dict, sex: str) -> dict:
    kind = "小母鸡" if sex == "hen" else "小公鸡"
    return {
        "profile_id": profile["profile_id"],
        "chick_appearance": profile["chick_appearance"],
        "adult_appearance": profile["adult_appearance"],
        "personality": profile["personality"],
        "intro": profile["intro"].format(kind=kind),
    }


def _fallback_chicken_profile(used_profile_ids: set[str]) -> dict:
    bases = garden_content.CHICKEN_FALLBACK_BASES
    marks = garden_content.CHICKEN_FALLBACK_MARKS
    personalities = garden_content.CHICKEN_FALLBACK_PERSONALITIES
    actions = garden_content.CHICKEN_FALLBACK_INTRO_ACTIONS
    for base_index, (chick_base, adult_base) in enumerate(bases, start=1):
        for mark_index, (chick_mark, adult_mark) in enumerate(marks, start=1):
            for personality_index, personality in enumerate(personalities, start=1):
                profile_id = (
                    f"chick_fallback_{base_index:02d}_{mark_index:02d}_{personality_index:02d}"
                )
                if profile_id in used_profile_ids:
                    continue
                chick_appearance = f"{chick_base}，{chick_mark}"
                return {
                    "profile_id": profile_id,
                    "chick_appearance": chick_appearance,
                    "adult_appearance": f"{adult_base}，{adult_mark}",
                    "personality": personality,
                    "intro": (
                        f"这只{{kind}}一身{chick_appearance}，"
                        f"{actions[personality_index - 1]}。"
                    ),
                }
    raise GardenError("小院子鸡档案池已经用尽，已停止孵化以免复制旧成员")


def _take_chicken_profile(chicks: list[dict], sex: str, rng=None) -> dict:
    used_profile_ids = {
        chick.get("profile_id") for chick in chicks
        if isinstance(chick, dict) and isinstance(chick.get("profile_id"), str)
    }
    available = [
        profile for profile in garden_content.CHICKEN_PROFILE_POOL
        if profile["profile_id"] not in used_profile_ids
    ]
    profile = (rng.choice(available) if rng is not None else available[0]) if available else None
    if profile is None:
        profile = _fallback_chicken_profile(used_profile_ids)
    return _profile_payload(profile, sex)


def chicken_appearance(chick: dict) -> str:
    field = "adult_appearance" if chick.get("stage") == "adult" else "chick_appearance"
    return str(chick.get(field) or "").strip()


def _backfill_coop_state(state: dict, now: datetime) -> bool:
    """兼容鸡舍初版：旧小鸡用稳定散列补性别，新孵化仍逐枚真正随机。"""
    coop = state["coop"]
    changed = False
    # 初版把“先搭鸡舍”错误地做成送蛋剧情的开关。新顺序是动物先送蛋，
    # agent 选择孵化时才搭鸡舍；把尚未开始或尚未决定的旧状态迁回这条因果链。
    if coop.get("story_status") == "waiting_event":
        coop.update(_empty_coop())
        changed = True
    elif coop.get("story_status") in {"awaiting_choice", "eaten"} and coop.get("built"):
        coop["built"] = False
        coop["built_at"] = None
        changed = True
    for key, default in (
        ("last_crow_date", None), ("total_eggs_laid", 0),
        ("incubating_egg_count", COOP_EGG_COUNT if coop.get("story_status") == "incubating" else 0),
    ):
        if key not in coop:
            coop[key] = default
            changed = True
    # 第八版：旧档整体替换 coop 字典时不会带上这个新字段（_normalize_v4_state
    # 按顶层键整体搬运，不逐键合并默认值），必须在这里显式补齐，否则每次
    # 落盘再读都会在 _validate_coop 里报"鸡粪记录损坏"（8/13 条目0同款教训）。
    if not isinstance(coop.get("manure"), dict):
        coop["manure"] = {"units": 0, "last_settled_at": None}
        changed = True
    for chick in coop.get("chicks", []):
        if not isinstance(chick, dict) or not isinstance(chick.get("id"), str) or not isinstance(chick.get("hatched_at"), str):
            continue  # 后续严格校验给出统一的损坏错误，不能在这里猜坏数据。
        hatched_at = _parse_iso(chick["hatched_at"], field="小鸡出生时间")
        matures_at = hatched_at + CHICK_MATURITY_DURATION
        if "sex" not in chick:
            digest = hashlib.sha256(chick["id"].encode("utf-8")).digest()
            chick["sex"] = ("hen", "rooster")[digest[0] % 2]
            changed = True
        if "matures_at" not in chick:
            chick["matures_at"] = matures_at.isoformat()
            changed = True
        if "stage" not in chick:
            chick["stage"] = "adult" if now >= matures_at else "chick"
            changed = True
        if "matured_at" not in chick:
            chick["matured_at"] = matures_at.isoformat() if chick["stage"] == "adult" else None
            changed = True
        if "eggs_laid" not in chick:
            chick["eggs_laid"] = 0
            changed = True
        if "next_egg_at" not in chick:
            chick["next_egg_at"] = (
                (matures_at + HEN_FIRST_EGG_AFTER_MATURITY).isoformat()
                if chick["sex"] == "hen" else None
            )
            changed = True
        for key, default in (
            ("nickname", None), ("feed_count", 0), ("pet_count", 0),
            ("last_fed_at", None), ("last_petted_at", None),
        ):
            if key not in chick:
                chick[key] = default
                changed = True
        profile_fields_present = [key in chick for key in _CHICKEN_PROFILE_FIELDS]
        if not any(profile_fields_present):
            chick.update(_take_chicken_profile(coop["chicks"], chick["sex"]))
            changed = True
    expected_total = sum(
        chick.get("eggs_laid", 0)
        for chick in coop.get("chicks", [])
        if isinstance(chick, dict) and isinstance(chick.get("eggs_laid", 0), int)
    )
    if coop.get("total_eggs_laid") != expected_total and changed:
        coop["total_eggs_laid"] = expected_total
    return changed


def _backfill_compost_state(state: dict) -> bool:
    """第九版：旧批次按"units=鸡粪份数、48 小时到期"记账；新口径是
    "units=将来出的肥料份数"，鸡粪份数挪进新增的 manure_units 留痕
    （设计稿第九版第一节）。惰性、幂等：只处理缺 manure_units 的批次，
    已迁移过的原样跳过；ready_at 不动，旧批次仍按当时的 48 小时到期，
    不追溯延长。"""
    compost = state.get("compost")
    if not isinstance(compost, dict):
        return False
    batches = compost.get("batches")
    if not isinstance(batches, list):
        return False
    changed = False
    for batch in batches:
        if not isinstance(batch, dict) or "manure_units" in batch:
            continue
        old_units = batch.get("units")
        if isinstance(old_units, bool) or not isinstance(old_units, int):
            old_units = 0
        batch["manure_units"] = old_units
        batch["units"] = max(1, old_units // MANURE_PER_FERTILIZER)
        changed = True
    return changed


def _normalize_v4_state(data: dict, now: datetime, rng) -> dict:
    source_version = data.get("version")
    if source_version not in (3, LEGACY_STATE_VERSION, STATE_VERSION):
        return _migrate_v2_state(data, now, rng)
    expected_types = {
        "animals": list, "legacy_plants": list, "plots": list, "inventory": dict,
        "journal": dict, "pending_events": list, "meta": dict, "legacy": dict,
        "coop": dict,
    }
    for key, expected_type in expected_types.items():
        if key in data and not isinstance(data[key], expected_type):
            raise GardenError(f"小院子 v3 状态字段 {key} 格式损坏，已停止写入以免覆盖原记录")
    state = _empty_state()
    for key, default in state.items():
        value = data.get(key, default)
        state[key] = value if isinstance(value, type(default)) else default
    if isinstance(data.get("environment"), dict):
        state["environment"] = data["environment"]
    state["version"] = source_version
    known = set(_empty_state())
    state["legacy"].setdefault("unrecognized_top_level", {}).update(
        {key: value for key, value in data.items() if key not in known}
    )
    if any(not isinstance(entry, dict) for entry in state["animals"]):
        raise GardenError("小院子 v3 动物记录格式损坏，已停止写入以免覆盖原记录")
    if any(not isinstance(entry, dict) for entry in state["legacy_plants"]):
        raise GardenError("小院子 v3 旧花草记录格式损坏，已停止写入以免覆盖原记录")
    if any(entry.get("kind") != "animal" for entry in state["animals"]):
        raise GardenError("小院子 v3 动物记录类型损坏，已停止写入以免覆盖原记录")
    if any(entry.get("kind") != "flower" for entry in state["legacy_plants"]):
        raise GardenError("小院子 v3 旧花草记录类型损坏，已停止写入以免覆盖原记录")
    if any("legacy" in entry and not isinstance(entry["legacy"], dict) for entry in state["animals"]):
        raise GardenError("小院子 v3 动物旧数据格式损坏，已停止写入以免覆盖原记录")
    if any(not isinstance(event, dict) or not isinstance(event.get("event_id"), str) for event in state["pending_events"]):
        raise GardenError("小院子待展示事件格式损坏，已停止写入以免覆盖原记录")
    state["animals"] = [_normalize_animal(entry, now, rng) for entry in state["animals"]]
    for key, default in _empty_state()["inventory"].items():
        state["inventory"].setdefault(key, default)
    for key, default in _empty_state()["journal"].items():
        state["journal"].setdefault(key, default)
    backfilled = _backfill_v5_soil_fields(state)
    backfilled = _backfill_coop_state(state, now) or backfilled
    backfilled = _backfill_compost_state(state) or backfilled
    _validate_crop_state(state)
    layout_changed = _ensure_plot_layout(state)
    if source_version == LEGACY_STATE_VERSION and real_environment_enabled():
        _migrate_v4_to_v5(state, now)
    elif source_version == 3:
        state["version"] = LEGACY_STATE_VERSION
        state["_migrated"] = True
    if layout_changed or backfilled:
        state["_migrated"] = True
    state["entries"] = _project_entries(state)
    return state


def calendar_context(now: datetime):
    """统一入口，确保所有院子时间规则均按北京时间结算。"""
    return DEFAULT_CALENDAR.context_at(now)


def weather_context_tags(weather: object, now: datetime) -> frozenset[str]:
    """把 daemon 已取得的新鲜天气快照收敛成文案可用的事实标签。"""
    if not isinstance(weather, dict):
        return frozenset()
    try:
        updated = datetime.fromisoformat(str(weather["updateTime"]).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            return frozenset()
        age = calendar_context(now).now - updated.astimezone(TZ)
        if age < -timedelta(minutes=15) or age > WEATHER_CONTEXT_MAX_AGE:
            return frozenset()
    except (KeyError, TypeError, ValueError):
        return frozenset()

    text = str(weather.get("text") or "")
    tags: set[str] = set()
    if "晴" in text:
        tags.add("clear")
    if any(word in text for word in ("云", "阴")):
        tags.add("cloudy")
    if "雨" in text:
        tags.add("rain")
    if "雪" in text:
        tags.add("snow")
    if any(word in text for word in ("雾", "霾", "沙尘")):
        tags.add("fog")
    if "霜" in text:
        tags.add("frost")
    if "雷" in text:
        tags.add("storm")

    try:
        temperature = float(weather.get("feelsLike") or weather.get("temp"))
        if temperature >= 30:
            tags.add("hot")
        if temperature <= 5:
            tags.add("cold")
    except (TypeError, ValueError):
        pass
    try:
        humidity = float(weather.get("humidity"))
        if humidity >= 85:
            tags.add("humid")
    except (TypeError, ValueError):
        pass
    wind_values = [
        int(part) for part in str(weather.get("windScale") or "")
        .replace("级", "").replace("～", "-").replace("~", "-").split("-")
        if part.strip().isdigit()
    ]
    if wind_values and max(wind_values) >= 3:
        tags.add("windy")
    return frozenset(tags)


_CONDITION_EVENT_FALLBACK = {
    "pest": "{crop}的叶面多了几处新鲜虫眼，小虫还伏在叶背没有散去。",
    "diseased_leaf": "{crop}的叶片卷起斑驳的病边，发黑的部分仍贴在枝上。",
    "waterlogged": "{crop}根边的土已经湿透，薄薄一层浮水贴着茎脚不退。",
    "nutrient_deficiency": "{crop}的叶色明显淡了下去，新长的一截也停在原处。",
}

_CONDITION_EVENT_VARIANTS = {
    "pest": ("{crop}叶背又多了几处虫眼，小虫仍贴着叶脉。", "{crop}的叶缘留下细碎啃痕，虫害还没有散去。", "{crop}新叶背面有小虫缓慢挪动，虫眼一时很显眼。", "{crop}的叶片被啃出细小缺口，叶背仍藏着虫影。", "{crop}枝叶间多了新鲜虫眼，靠近才看见叶背的虫。", "{crop}叶面不再平整，几处虫眼把这次异常露了出来。"),
    "diseased_leaf": ("{crop}的叶片卷起斑驳病边，暗色仍留在叶脉旁。", "{crop}叶面浮出不规则斑点，新叶也显得没有精神。", "{crop}的病叶边缘发暗，卷起的部分还贴在枝上。", "{crop}几片叶子带着病斑，颜色和健康叶片明显不同。", "{crop}叶脉旁的斑点没有退，卷边也还留着。", "{crop}枝叶间的病斑很清楚，叶片暂时没有舒展开。"),
    "waterlogged": ("{crop}根边的土已经湿透，薄薄浮水贴着茎脚不退。", "{crop}脚下的湿泥发亮，低处的水还没有渗下去。", "{crop}根际积着一层水光，土面迟迟没有松开。", "{crop}周围的土被积水压得发暗，根边仍留着浅水。", "{crop}脚下湿得过头，细土和水光黏在一起。", "{crop}根边的积水没有退，土面亮得不太正常。"),
    "nutrient_deficiency": ("{crop}的叶色明显淡了下去，新长的一截也停在原处。", "{crop}新叶发浅，长势比周围安静得多。", "{crop}叶片的颜色褪了一层，枝条也没有再往前长。", "{crop}叶色发黄发淡，新长的部分显得很慢。", "{crop}枝叶缺少原来的精神，叶色从新叶上淡下去。", "{crop}叶片颜色不匀，长势也暂时停住了。"),
}

_CONDITION_SEVERITY_VARIANTS = {
    "damaged": ("{crop}的{condition}还压在枝叶上，这一茬已经确定少收1份。", "{crop}的{condition}没有退，枝叶间已经留下减产的痕迹。", "{crop}这一茬被{condition}拖住，少收的部分已经无法补回。", "{crop}的{condition}仍在，收成已经确定少了一份。", "{crop}枝叶上的{condition}没有松开，这一茬少收已成事实。", "{crop}还带着{condition}，这次损失已经记进本轮收成。"),
    "withered": ("{crop}的{condition}一直没有退，茎叶已经彻底枯死。", "{crop}枯在原来的菜畦里，{condition}留下的这一茬已经结束。", "{crop}的{condition}拖到最后，枝叶只剩枯死的轮廓。", "{crop}已经枯死，{condition}没有给这一茬留下转圜。", "{crop}的{condition}仍在，茎叶彻底干枯后停住了生长。", "{crop}被{condition}拖到只剩枯枝，这一茬已经无法继续。"),
}


def _crop_copy_context(
    now: datetime,
    weather_tags: frozenset[str],
    *,
    weather_status: str | None = None,
    outing: dict | None = None,
) -> dict:
    context = calendar_context(now)
    facts = {
        "season": context.season,
        "day_period": context.day_period,
        "weather_status": weather_status or ("fresh" if weather_tags else "missing"),
        "weather_tags": sorted(str(tag) for tag in weather_tags),
    }
    if isinstance(outing, dict):
        facts.update({
            "outing_mode": outing.get("mode"),
            "yard_water": outing.get("yard_water"),
            "rain_streak_days": outing.get("rain_streak_days"),
        })
    return facts


def _condition_event_facts(event: dict, now: datetime, weather_tags: frozenset[str]) -> dict:
    condition_type = event["condition_type"]
    return {
        "copy_type": "condition_event",
        "plot_label": f"{plot_label(event['plot_id'])}地",
        "crop_name": garden_crops.crop_name(event["crop_id"]),
        "condition_type": condition_type,
        "condition_name": CONDITION_LABELS[condition_type],
        "severity": event["severity"],
        "yield_penalty": event["yield_penalty"],
        "correct_action": CONDITION_ACTIONS[condition_type],
        **_crop_copy_context(now, weather_tags),
    }


def _condition_event_fallback_body(event: dict) -> str:
    crop = garden_crops.crop_name(event["crop_id"])
    condition = CONDITION_LABELS[event["condition_type"]]
    index = sum(ord(char) for char in f"{event.get('condition_id', '')}|{crop}|{event['severity']}")
    if event["severity"] == "warning":
        lines = _CONDITION_EVENT_VARIANTS[event["condition_type"]]
        return lines[index % len(lines)].format(crop=crop)
    if event["severity"] == "damaged":
        lines = _CONDITION_SEVERITY_VARIANTS["damaged"]
        return lines[index % len(lines)].format(crop=crop, condition=condition)
    lines = _CONDITION_SEVERITY_VARIANTS["withered"]
    return lines[index % len(lines)].format(crop=crop, condition=condition)


def _condition_event_result_line(event: dict) -> str:
    plot = plot_label(event["plot_id"])
    crop = garden_crops.crop_name(event["crop_id"])
    condition = CONDITION_LABELS[event["condition_type"]]
    if event["severity"] == "warning":
        return (
            f"结果：{plot}地的{condition}已出现，{crop}暂停生长；"
            f"需要{CONDITION_ACTIONS[event['condition_type']]}。"
        )
    if event["severity"] == "damaged":
        return (
            f"结果：{plot}地本轮收成已永久减少1份；{condition}仍未解决，"
            f"{crop}继续暂停生长；仍需{CONDITION_ACTIONS[event['condition_type']]}"
            f"才能避免情况继续恶化。"
        )
    return (
        f"结果：{plot}地的{crop}已经枯死；本轮没有作物，也不会返还种子，只能清理。"
    )


def _generate_crop_copy_body(
    facts: dict,
    fallback: str,
    *,
    now: datetime,
    weather_tags: frozenset[str],
) -> tuple[str, str]:
    context = calendar_context(now)
    recent_rain = (
        facts.get("water_source") == "rain"
        and facts.get("soil_state") in ("湿润", "湿透")
    )
    try:
        text = garden_generator.generate_crop_copy(facts)
        garden_generator.validate_crop_copy_text(text, facts)
        if not garden_content.scene_text_compatible(
            text,
            season=context.season,
            day_period=context.day_period,
            weather_tags=weather_tags,
            recent_rain=recent_rain,
        ):
            raise GardenGeneratorError("小院子写手返回了与当前环境冲突的作物正文")
        return text, "deepseek"
    except (GardenGeneratorError, ValueError):
        local = garden_content.local_crop_fallback(facts, fallback)
        for candidate in dict.fromkeys((local, fallback)):
            try:
                garden_generator.validate_crop_copy_text(candidate, facts)
                if not garden_content.scene_text_compatible(
                    candidate,
                    season=context.season,
                    day_period=context.day_period,
                    weather_tags=weather_tags,
                    recent_rain=recent_rain,
                ):
                    raise ValueError("本地作物文案与当前环境冲突")
            except (GardenGeneratorError, ValueError):
                continue
            return candidate, "fallback"
        # 调用方仍会追加代码拥有的结果行；兼容池和旧最小句都被拒绝时，
        # 宁可只展示那条结果，也不猜天气或动作。
        return "", "fallback"


def crop_treatment_copy(
    result: dict,
    *,
    now: datetime | None = None,
    weather_tags: frozenset[str] = frozenset(),
    outing: dict | None = None,
    fallback: str,
) -> str:
    """锁外生成处理动作正文；调用方继续负责追加确定性结果行。"""
    now = calendar_context(now or datetime.now(TZ)).now
    condition_type = result.get("condition_type")
    facts = {
        "copy_type": "treatment",
        "plot_label": f"{plot_label(result['plot_id'])}地",
        "crop_name": garden_crops.crop_name(result["crop_id"]),
        "condition_type": condition_type,
        "condition_name": CONDITION_LABELS.get(condition_type),
        "outcome": result["outcome"],
        "action": result["action"],
        "correct_action": result.get("correct_action"),
        "yield_penalty": int(result.get("yield_penalty", 0)),
        **_crop_copy_context(now, weather_tags, outing=outing),
    }
    return _generate_crop_copy_body(
        facts, fallback, now=now, weather_tags=weather_tags,
    )[0]


def watering_copy(
    result: dict,
    *,
    now: datetime | None = None,
    weather_tags: frozenset[str] = frozenset(),
    outing: dict | None = None,
    fallback: str,
) -> str:
    """锁外生成浇水即时正文；规则结果不交给写手。"""
    now = calendar_context(now or datetime.now(TZ)).now
    observations = _environment_observations(now)
    fresh_tags = _fresh_environment_weather_tags(observations, now)
    weather_tags = frozenset(str(tag) for tag in weather_tags) | fresh_tags
    weather_status = "fresh" if any(
        garden_weather.fresh_at(item, now) for item in observations
    ) else ("fresh" if weather_tags else "missing")
    plot = result["plot"]
    condition_type = result.get("condition_type")
    soil = plot.get("soil") if isinstance(plot.get("soil"), dict) else {}
    try:
        soil_state = garden_weather.moisture_label(float(
            result.get("moisture_before", soil.get("moisture")),
        ))
    except (TypeError, ValueError):
        soil_state = None
    water_source = soil.get("last_water_source")
    facts = {
        "copy_type": "watering",
        "plot_label": f"{plot_label(plot['plot_id'])}地",
        "crop_name": garden_crops.crop_name(plot["crop_id"]),
        "condition_type": condition_type,
        "condition_name": CONDITION_LABELS.get(condition_type),
        "outcome": result["outcome"],
        "style": result.get("style"),
        "watering_reason": result.get("reason"),
        "watering_count": int(result.get("watering_count", 0)),
        "accelerated": bool(result.get("accelerated")),
        "soil_state": soil_state,
        "water_source": water_source if water_source in ("manual", "rain") else "none",
        **_crop_copy_context(
            now, weather_tags, weather_status=weather_status, outing=outing,
        ),
    }
    compatibility_weather_tags = weather_tags
    if result.get("reason") == "hot_clear_dry":
        compatibility_weather_tags |= frozenset({"hot", "clear"})
    elif result.get("reason") == "hot_dry":
        compatibility_weather_tags |= frozenset({"hot"})
    elif result.get("reason") == "wind_dry":
        compatibility_weather_tags |= frozenset({"windy"})
    return _generate_crop_copy_body(
        facts, fallback, now=now, weather_tags=compatibility_weather_tags,
    )[0]


def _compatible_choice(lines: tuple[str, ...], context, weather_tags: frozenset[str], rng) -> str:
    eligible = garden_content.compatible_texts(
        lines,
        season=context.season,
        day_period=context.day_period,
        weather_tags=weather_tags,
    )
    if not eligible:
        raise GardenGeneratorError("小院子没有与当前环境相符的文案")
    return rng.choice(eligible)


def _read_state_unlocked(path: Path, *, now: datetime | None = None, rng=None) -> dict:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    rng = rng or random
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = _empty_state()
        if real_environment_enabled():
            _migrate_v4_to_v5(state, now)
        return state
    except json.JSONDecodeError as exc:
        raise GardenError("小院子状态文件损坏，已停止写入以免覆盖原记录") from exc
    except OSError as exc:
        raise GardenError("小院子状态文件暂时无法读取") from exc

    if not isinstance(data, (dict, list)):
        raise GardenError("小院子状态格式无法识别，已停止写入")
    return _normalize_v4_state(data, now, rng) if isinstance(data, dict) else _migrate_v2_state(data, now, rng)


def _write_state_unlocked(state: dict, path: Path) -> None:
    _sync_projected_entries(state)
    payload = {key: value for key, value in state.items() if key not in {"entries", "_migrated"}}
    if payload.get("version") != STATE_VERSION:
        payload.pop("environment", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(payload, target, ensure_ascii=False, indent=2)
            target.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def load_garden(path: Path | None = None) -> list[dict]:
    path = path or GARDEN_FILE
    # 读取是旧状态升 v4 的合法入口：迁移在同一把排他锁内落盘，避免两个并发入口
    # 各自随机补性格或追加菜畦后互相覆盖。已有完整 v4 状态仍是纯读取。
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path)
        if state.get("_migrated"):
            _write_state_unlocked(state, path)
        return state["entries"]


def load_meta(path: Path | None = None) -> dict:
    path = path or GARDEN_FILE
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path)
        if state.get("_migrated"):
            _write_state_unlocked(state, path)
        return state["meta"]


def coop_snapshot(*, now: datetime | None = None, path: Path | None = None) -> dict:
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        # 第八版：这个函数是"查看鸡舍"唯一入口（不像其余院子查看那样先经过
        # crop_snapshot 的 _settle_crops），必须自己结算鸡粪/堆肥并在有变化
        # 时落盘，否则单独查看鸡舍会读到结算不及时的旧数字。
        if _settle_coop_byproducts(state, now):
            _write_state_unlocked(state, path)
        coop = state["coop"]
        remaining = None
        if coop["story_status"] == "incubating":
            hatch_at = _parse_iso(coop["hatch_at"], field="预计孵化时间")
            remaining = max(0, int((hatch_at - now).total_seconds()))
        chicks = coop["chicks"]
        chick_count = sum(chick.get("stage") == "chick" for chick in chicks)
        hen_count = sum(chick.get("stage") == "adult" and chick.get("sex") == "hen" for chick in chicks)
        rooster_count = sum(chick.get("stage") == "adult" and chick.get("sex") == "rooster" for chick in chicks)
        next_maturity_seconds = None
        maturity_times = [
            _parse_iso(chick["matures_at"], field="小鸡长大时间")
            for chick in chicks if chick.get("stage") == "chick"
        ]
        if maturity_times:
            next_maturity_seconds = max(0, int((min(maturity_times) - now).total_seconds()))
        manure = coop.get("manure") or {}
        compost_batches = [
            {
                "batch_id": batch["batch_id"],
                "units": int(batch.get("units", 0)),
                "manure_units": int(batch.get("manure_units", 0)),
                "remaining_seconds": max(0, int((
                    _parse_iso(batch["ready_at"], field="堆肥批次到期时间") - now
                ).total_seconds())),
            }
            for batch in state.get("compost", {}).get("batches", [])
            if isinstance(batch, dict)
        ]
        return {
            **coop,
            "remaining_seconds": remaining,
            "egg_count": int(state["inventory"].get("animal_products", {}).get("egg", 0)),
            "hatchable_egg_count": int(
                state["inventory"].get("animal_products", {}).get("hatchable_egg", 0)
            ),
            "chick_count": chick_count,
            "hen_count": hen_count,
            "rooster_count": rooster_count,
            "next_maturity_seconds": next_maturity_seconds,
            "manure_units": int(manure.get("units", 0)),
            "manure_cap": MANURE_CAP,
            "compost_batches": compost_batches,
            "fertilizer_count": int(state["inventory"].get("fertilizer", {}).get("fertilizer", 0)),
        }


def chicken_kind(chick: dict) -> str:
    if chick.get("stage") == "adult":
        return "母鸡" if chick.get("sex") == "hen" else "公鸡"
    return "小母鸡" if chick.get("sex") == "hen" else "小公鸡"


def chicken_display_name(chick: dict, *, include_kind: bool = False) -> str:
    nickname = str(chick.get("nickname") or "").strip()
    kind = chicken_kind(chick)
    if nickname:
        return f"{kind}\u201c{nickname}\u201d" if include_kind else nickname
    return f"{kind}[{str(chick.get('id') or '')[:4]}]"


def matches_chicken_selector(chick: dict, selector: str) -> bool:
    selector = selector.strip()
    return bool(selector) and (
        str(chick.get("id") or "").startswith(selector)
        or str(chick.get("nickname") or "").strip() == selector
        or chicken_display_name(chick) == selector
        or chicken_display_name(chick, include_kind=True) == selector
    )


def _find_chicken(coop: dict, selector: str | None) -> dict:
    chicks = coop["chicks"]
    matches = [chick for chick in chicks if not selector or matches_chicken_selector(chick, selector)]
    if not matches:
        raise GardenError(f"没找到名称或编号是 {selector or '这个'} 的鸡")
    if len(matches) > 1:
        choices = "、".join(chicken_display_name(chick) for chick in matches[:5])
        raise GardenError(f"鸡舍里有不止一只鸡，请带上名字或编号：{choices}")
    return matches[0]


def care_chicken(
    selector: str | None,
    action: str,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> dict:
    if action not in {"feed", "pet", "clean"}:
        raise GardenError("鸡舍只支持喂食、摸摸或打扫")
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        if action == "clean":
            # 第八版：打扫不针对单只鸡，作用在整个鸡舍上——沿用现有报错
            # 口吻（build_coop 的"鸡舍要等…才会搭起来"），不复用
            # _find_chicken（那个是"找一只具体的鸡"的语义，跟打扫不搭）。
            if not state["coop"]["built"]:
                raise GardenError("鸡舍要等小动物送来鸡蛋、并决定孵化后才会搭起来")
            changed = _settle_coop_byproducts(state, now)
            manure = state["coop"]["manure"]
            units = int(manure.get("units", 0))
            # 第九版：不足一堆（6 份）沤不成肥，拒绝分支不改任何状态——
            # 结算好的时间账照旧落盘，跟施肥拒绝分支同款写法。
            if units < MANURE_PER_FERTILIZER:
                if changed:
                    _write_state_unlocked(state, path)
                raise GardenError(f"鸡粪还没攒够一堆（{units}/{MANURE_PER_FERTILIZER}），沤不成肥，再等等")
            # 通式：只收走能整堆折算的部分，零头留在鸡舍——当前 MANURE_CAP
            # 恰好等于 MANURE_PER_FERTILIZER，等价于"攒满才能扫、一扫出一
            # 份"；写成通式是为了以后上限调高不用改这里。
            swept = units - units % MANURE_PER_FERTILIZER
            manure["units"] = units - swept
            fertilizer_units = swept // MANURE_PER_FERTILIZER
            batch_id = uuid.uuid4().hex
            ready_at = now + COMPOST_READY_AFTER
            compost = state.setdefault("compost", {"batches": []})
            compost.setdefault("batches", []).append({
                "batch_id": batch_id, "units": fertilizer_units,
                "manure_units": swept, "ready_at": ready_at.isoformat(),
            })
            _write_state_unlocked(state, path)
            return {
                "action": action,
                "units": swept,
                "fertilizer_units": fertilizer_units,
                "weather_mode": _animal_weather_mode(observations, now),
            }
        chick = _find_chicken(state["coop"], selector)
        if action == "feed":
            chick["feed_count"] += 1
            chick["last_fed_at"] = now.isoformat()
        else:
            chick["pet_count"] += 1
            chick["last_petted_at"] = now.isoformat()
        _write_state_unlocked(state, path)
        return {
            "action": action,
            "chick": dict(chick),
            "weather_mode": _animal_weather_mode(observations, now),
        }


def name_chicken(
    selector: str | None,
    nickname: str,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> dict:
    nickname = " ".join(nickname.split()).strip()
    if not nickname:
        raise GardenError("昵称不能为空")
    if len(nickname) > 12 or any(ord(char) < 32 for char in nickname):
        raise GardenError("昵称最多 12 个字，不能含换行或控制字符")
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        chick = _find_chicken(state["coop"], selector)
        if any(
            other is not chick and str(other.get("nickname") or "").strip() == nickname
            for other in state["coop"]["chicks"]
        ):
            raise GardenError(f"鸡舍里已经有一只叫“{nickname}”的鸡")
        chick["nickname"] = nickname
        _write_state_unlocked(state, path)
        return dict(chick)


def build_coop(*, now: datetime | None = None, path: Path | None = None) -> dict:
    """兼容旧调用：鸡舍不允许在鸡蛋出现前凭空搭建。"""
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        _ensure_crop_state(state, now)
        coop = state["coop"]
        if coop["built"]:
            return {**coop, "already_built": True}
        raise GardenError("鸡舍要等小动物送来鸡蛋、并决定孵化后才会搭起来")


def resolve_coop_egg_choice(
    choice: str,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> dict:
    """消费这次故事里的三枚鸡蛋；选择孵化时固定在21小时后收口。"""
    if choice not in {"eat", "incubate"}:
        raise GardenError("鸡蛋只能选择吃掉或孵化")
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        _ensure_crop_state(state, now)
        coop = state["coop"]
        if coop["story_status"] != "awaiting_choice":
            raise GardenError("鸡舍现在没有等待决定的三枚鸡蛋")
        _inventory_take(state["inventory"], "animal_products", "egg", COOP_EGG_COUNT)
        # agent 可能在收到 [GARDEN] 的同一轮就调用 home；此时 daemon 尚未做
        # 投递确认。选择本身必须把旧卡清掉，否则模型调用成功但回复失败时，
        # 下一轮会把已经处理过的鸡蛋重新播一遍。
        state["pending_events"] = [
            event for event in state["pending_events"]
            if not (isinstance(event, dict) and event.get("type") == "coop_egg_offer")
        ]
        coop["choice_resolved_at"] = now.isoformat()
        if choice == "eat":
            coop["story_status"] = "eaten"
            _write_state_unlocked(state, path)
            return {"choice": "eat", "egg_count": COOP_EGG_COUNT, "coop": dict(coop)}
        clutch_id = uuid.uuid4().hex
        coop.update({
            "built": True,
            "built_at": now.isoformat(),
            "story_status": "incubating",
            "clutch_id": clutch_id,
            "incubation_started_at": now.isoformat(),
            "hatch_at": (now + COOP_INCUBATION_DURATION).isoformat(),
            "incubating_egg_count": COOP_EGG_COUNT,
            "progress_queued": [],
            "chicks": [],
        })
        _write_state_unlocked(state, path)
        return {"choice": "incubate", "egg_count": COOP_EGG_COUNT, "coop": dict(coop)}


def start_hatchable_egg_incubation(
    *, now: datetime | None = None, path: Path | None = None,
) -> dict:
    """从独立库存消费一枚稀有蛋；一次只允许存在一窝进行中的孵化。"""
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        _ensure_crop_state(state, now)
        coop = state["coop"]
        if not coop["built"] or coop["story_status"] not in {"hatched", "incubating"}:
            raise GardenError("鸡舍里还没有能继续孵蛋的鸡群")
        if coop["story_status"] == "incubating":
            raise GardenError("agent 已经在孵一窝蛋了，这次没有再取蛋")
        _inventory_take(state["inventory"], "animal_products", "hatchable_egg", 1)
        coop.update({
            "story_status": "incubating",
            "clutch_id": uuid.uuid4().hex,
            "incubation_started_at": now.isoformat(),
            "hatch_at": (now + COOP_INCUBATION_DURATION).isoformat(),
            "incubating_egg_count": 1,
            "progress_queued": [],
        })
        _write_state_unlocked(state, path)
        return {"egg_count": 1, "coop": dict(coop)}


def save_garden(entries: list[dict], path: Path | None = None) -> None:
    """测试/维护入口：替换 entries，同时保留已有调度元数据。"""
    path = path or GARDEN_FILE
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path)
        state["entries"] = entries
        _write_state_unlocked(state, path)


def _new_id(existing_ids: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:6]
        if candidate not in existing_ids:
            return candidate


def active_entries(path: Path | None = None) -> list[dict]:
    return [e for e in load_garden(path) if isinstance(e, dict) and e.get("status") == "active"]


def left_entries(path: Path | None = None) -> list[dict]:
    return [e for e in load_garden(path) if isinstance(e, dict) and e.get("status") == "left"]


def away_entries(path: Path | None = None) -> list[dict]:
    """暂时出去转转的动物仍是院子的一部分，查看页不能把它们当作丢失。"""
    return [
        entry for entry in load_garden(path)
        if isinstance(entry, dict) and entry.get("kind") == "animal" and entry.get("status") == "away"
    ]


def _hours_since(iso_value: str, now: datetime) -> float:
    try:
        then = datetime.fromisoformat(iso_value)
    except (TypeError, ValueError) as exc:
        raise GardenError("小院子里有一条无法识别的时间记录") from exc
    return (now - then).total_seconds() / 3600


def compute_stage(entry: dict, now: datetime) -> str:
    """第三版不从未照顾时长推导疲惫；展示永远是中性的在场状态。"""
    del entry, now
    return "fresh"


def _advance_entries(
    entries: list[dict],
    now: datetime,
    rng,
    *,
    weather_mode: str = "normal",
) -> list[dict]:
    """结算动物自然外出，不把时间差解释成被忽略或永久离开。

    恶劣天气只拦住本次新外出；已经在外的动物不由这里强制赶回，亲密与
    常住身份也完全不动。到期游标保留，天气转好后的下一次结算即可照常出门。
    """
    away_now = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "animal" or entry.get("status") != "active":
            continue
        target = entry.get("next_natural_visit_after")
        if not target:
            entry["next_natural_visit_after"] = _next_visit_after(now, NATURAL_VISIT_MIN_HOURS, NATURAL_VISIT_MAX_HOURS)
            continue
        if _hours_since(target, now) >= 0:
            if weather_mode == "sheltering":
                continue
            entry["status"] = "away"
            entry["away_at"] = now.isoformat()
            # 亲密越高，回来时更像是顺路回固定落脚处；resident 仍然能自由外出。
            level = _bond_level(int(entry.get("bond_points", 0)))
            shortened = min(18, level * 3)
            entry["next_natural_visit_after"] = _next_visit_after(
                now,
                max(4, NATURAL_RETURN_MIN_HOURS - shortened),
                max(8, NATURAL_RETURN_MAX_HOURS - shortened),
            )
            away_now.append(entry)
    return away_now


def _backfill_animal_fields(entries: list[dict], rng) -> bool:
    """给迁移前（没有 category/personality 字段）的旧存档条目补齐分类和性格。

    只在缺失时才补，一旦补上会持久化，之后同一条目的性格保持稳定，不会
    每次读取都重新随机。
    """
    changed = False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "animal":
            continue
        if entry.get("category") is None:
            entry["category"] = garden_content.category_from_species(str(entry.get("species") or ""))
            changed = True
        if entry.get("personality") is None:
            entry["personality"] = rng.choice(garden_content.PERSONALITIES)
            changed = True
    return changed


def advance(now: datetime, path: Path | None = None, *, rng=None) -> list[dict]:
    """结算自然外出；返回本次出去转转的访客，既不删除也不扣任何关系值。"""
    path = path or GARDEN_FILE
    rng = rng or random
    now = calendar_context(now).now
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        away_now = _advance_entries(
            state["entries"], now, rng,
            weather_mode=_animal_weather_mode(observations, now),
        )
        crops_changed = _settle_crops(state, now, observations=observations)
        if away_now or crops_changed or state.get("_migrated"):
            _write_state_unlocked(state, path)
        return away_now


def spawn(
    kind: str,
    *,
    species: str,
    intro: str,
    category: str | None = None,
    personality: str | None = None,
    now: datetime | None = None,
    path: Path | None = None,
    rng=None,
) -> dict:
    now = calendar_context(now or datetime.now(TZ)).now
    path = path or GARDEN_FILE
    rng = rng or random
    if kind not in _ACTIONS_BY_KIND:
        raise GardenError(f"不认识的小院子类型：{kind}")
    if kind == "animal":
        if category is None:
            category = garden_content.category_from_species(species)
        if personality is None:
            personality = rng.choice(garden_content.PERSONALITIES)
    else:
        category = None
        personality = None
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        _settle_crops(state, now, observations=observations)
        entries = state["entries"]
        if kind == "animal" and sum(
            1 for e in entries
            if isinstance(e, dict) and e.get("kind") == "animal" and e.get("status") == "active"
        ) >= MAX_ACTIVE:
            raise GardenError("小院子现在已经住满了")
        existing_ids = {e["id"] for e in entries if isinstance(e, dict) and "id" in e}
        entry = {
            "id": _new_id(existing_ids),
            "kind": kind,
            "species": species,
            "category": category,
            "personality": personality,
            "nickname": None,
            "trait": rng.choice(garden_content.TRAITS[kind]),
            "spot": rng.choice(SPOTS),
            "arrived_at": now.isoformat(),
            "last_cared_at": now.isoformat(),
            "care_count": 0,
            "last_action": None,
            "return_count": 0,
            "status": "active",
            "left_at": None,
            "departure_note": None,
            "last_note": intro,
        }
        if kind == "animal":
            entry.update({
                "bond_points": 0,
                "bond_level": 0,
                "bond_actions_by_date": {},
                "bond_milestones_seen": [],
                "nickname_bonus_claimed": False,
                "residency": "visitor",
                "preferred_action": rng.choice(("投喂", "摸摸")),
                "last_visited_at": now.isoformat(),
                "next_natural_visit_after": _next_visit_after(
                    now, NATURAL_VISIT_MIN_HOURS, NATURAL_VISIT_MAX_HOURS,
                ),
            })
        entries.append(entry)
        if kind == "animal" and not any(
            isinstance(record, dict) and record.get("species") == species
            for record in state["journal"]["species_seen"]
        ):
            state["journal"]["species_seen"].append({
                "species": species, "category": category, "at": now.isoformat(),
            })
        state["meta"]["last_new_life_at"] = now.isoformat()
        _write_state_unlocked(state, path)
        return entry


def care(
    id_prefix: str,
    action: str,
    *,
    note: str | None = None,
    now: datetime | None = None,
    path: Path | None = None,
    rng=None,
) -> dict:
    now = calendar_context(now or datetime.now(TZ)).now
    path = path or GARDEN_FILE
    rng = rng or random
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        entries = state["entries"]
        settled = bool(_advance_entries(
            entries, now, rng, weather_mode=_animal_weather_mode(observations, now),
        )) or _settle_crops(state, now, observations=observations) or bool(state.get("_migrated"))
        settled = _backfill_animal_fields(entries, rng) or settled
        matches = [
            e for e in entries
            if isinstance(e, dict) and e.get("status") == "active"
            and matches_entry_selector(e, id_prefix)
        ]
        if not matches:
            # advance 可能刚把目标移入 left；即使报错也要把结算结果安全写回。
            _write_state_unlocked(state, path)
            raise GardenError(f"没找到名称或编号是 {id_prefix} 的、还在的花草/小动物")
        if len(matches) > 1:
            if settled:
                _write_state_unlocked(state, path)
            raise GardenError(f"{id_prefix} 匹配到多条，请换用更明确的昵称或编号")
        entry = matches[0]
        valid_actions = _ACTIONS_BY_KIND[entry["kind"]]
        if action not in valid_actions:
            if settled:
                _write_state_unlocked(state, path)
            raise GardenError(f"{entry['species']}要用『{'/'.join(valid_actions)}』，不是『{action}』")
        if action == "陪玩" and _bond_level(int(entry.get("bond_points", 0))) < 2:
            raise GardenError(f"{display_name(entry)}还在慢慢熟悉你，先投喂或摸摸也很好")
        entry["last_cared_at"] = now.isoformat()
        entry["care_count"] = int(entry.get("care_count", 0)) + 1
        entry["last_action"] = action
        entry["last_visited_at"] = now.isoformat()
        if note:
            entry["last_note"] = note
        bond = None
        if entry["kind"] == "animal":
            bond = _award_bond_points(state, entry, action, now)
        _write_state_unlocked(state, path)
        result = {
            "entry": entry, "revived": False, "bond": bond,
            "weather_mode": _animal_weather_mode(observations, now),
        }
        return result


def display_name(entry: dict, *, include_species: bool = False) -> str:
    nickname = str(entry.get("nickname") or "").strip()
    species = str(entry.get("species") or "小访客")
    if not nickname:
        return species
    return f"{nickname}（{species}）" if include_species else nickname


def name_entry(
    id_prefix: str,
    nickname: str,
    *,
    now: datetime | None = None,
    path: Path | None = None,
    rng=None,
) -> dict:
    nickname = " ".join(nickname.split()).strip()
    if not nickname:
        raise GardenError("昵称不能为空")
    if len(nickname) > 12 or any(ord(char) < 32 for char in nickname):
        raise GardenError("昵称最多 12 个字，不能含换行或控制字符")
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    rng = rng or random
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        settled = bool(_advance_entries(
            state["entries"], now, rng,
            weather_mode=_animal_weather_mode(observations, now),
        )) or _settle_crops(state, now, observations=observations) or bool(state.get("_migrated"))
        matches = [
            entry for entry in state["entries"]
            if isinstance(entry, dict)
            and entry.get("status") == "active"
            and matches_entry_selector(entry, id_prefix)
        ]
        if not matches:
            if settled:
                _write_state_unlocked(state, path)
            chicks = state.get("coop", {}).get("chicks", [])
            is_chicken = any(
                isinstance(chick, dict) and matches_chicken_selector(chick, id_prefix)
                for chick in chicks
            )
            hint = "；这是小鸡的编号，请改说『鸡取名 <编号> <新名字>』" if is_chicken else ""
            raise GardenError(f"没找到名称或编号是 {id_prefix} 的、还在的花草/小动物{hint}")
        if len(matches) > 1:
            if settled:
                _write_state_unlocked(state, path)
            raise GardenError(f"{id_prefix} 匹配到多条，请换用更明确的昵称或编号")
        entry = matches[0]
        entry["nickname"] = nickname
        if entry.get("kind") == "animal" and not entry.get("nickname_bonus_claimed"):
            _award_bond_points(state, entry, "取名", now)
            entry["nickname_bonus_claimed"] = True
        _write_state_unlocked(state, path)
        return entry


def crop_snapshot(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    acknowledge_conditions: bool = False,
) -> dict:
    """结算并返回四格菜畦与篮子的只读快照。"""
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        changed = _settle_crops(state, now, observations=observations) or bool(state.get("_migrated"))
        if acknowledge_conditions:
            changed = _acknowledge_conditions_shown_in_scene(state, now) or changed
        if changed:
            _write_state_unlocked(state, path)
        environment = _environment_snapshot(state, observations, now)
        return {
            "server_now": now.isoformat(),
            "timing_schema_version": TIMING_SCHEMA_VERSION,
            "crop_catalog": crop_catalog_snapshot(),
            "plots": [_plot_snapshot(plot, now, observations) for plot in state["plots"]],
            "inventory": {key: (dict(value) if isinstance(value, dict) else list(value)) for key, value in state["inventory"].items()},
            "season": calendar_context(now).season,
            "natural_conditions_enabled": natural_crop_conditions_enabled(),
            "real_environment_enabled": real_environment_enabled(),
            "environment": environment,
        }


def frontend_state_event(
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> dict:
    """已登录 WebSocket 的只读响应协议；浏览器不需要理解存档路径。"""
    return {
        "type": "garden_state",
        "data": crop_snapshot(now=now, path=path),
    }


def _acknowledge_condition_shown(
    state: dict,
    condition_id: str,
    shown_at: datetime,
) -> bool:
    """任一可靠可见结果共用同一确认事实，并撤掉已经过时的首次 warning。"""
    changed = _mark_condition_announced(state, condition_id, shown_at)
    warning_id = f"{condition_id}:severity:warning"
    before = len(state["pending_events"])
    state["pending_events"] = [
        event for event in state["pending_events"]
        if not (
            isinstance(event, dict)
            and event.get("event_id") == warning_id
        )
    ]
    changed = len(state["pending_events"]) != before or changed
    if changed:
        state["meta"]["last_visible_event_at"] = shown_at.isoformat()
    return changed


def _acknowledge_conditions_shown_in_scene(state: dict, shown_at: datetime) -> bool:
    """逛逛明确写到的未宣布异常，与绿色卡共用同一公平计时事实。"""
    condition_ids = [
        plot["condition"]["condition_id"]
        for plot in state["plots"]
        if isinstance(plot.get("condition"), dict)
        and plot["condition"].get("status") == "active"
        and plot["condition"].get("announced_at") is None
    ]
    if not condition_ids:
        return False
    changed = False
    for condition_id in condition_ids:
        changed = _acknowledge_condition_shown(
            state, condition_id, shown_at,
        ) or changed
    return changed


def stroll_scene(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    weather_tags: frozenset[str] = frozenset(),
    rng=None,
) -> str:
    """结算可见状态，按稳定键读取或实时生成一次院子整体画面。"""
    sandboxed_log_file = (path.parent / "logs.jsonl") if path is not None else None
    path = path or GARDEN_FILE
    explicit_now = now is not None
    now = calendar_context(now or datetime.now(TZ)).now
    rng = rng or random
    weather_tags = frozenset(str(tag) for tag in weather_tags)
    observations = _environment_observations(now)
    # daemon 传来的摘要只能补充已确认事实；独立 home 入口照样能读缓存。
    weather_tags = weather_tags | _fresh_environment_weather_tags(observations, now)
    animal_mode = _animal_weather_mode(observations, now)

    # 最多重取一次：写手请求期间若另一进程改变了院子，不能把旧画面冒充当前画面。
    for attempt in range(2):
        context = calendar_context(now)
        with _locked(path, exclusive=True):
            state = _read_state_unlocked(path, now=now, rng=rng)
            changed = bool(_advance_entries(
                state["entries"], now, rng, weather_mode=animal_mode,
            ))
            changed = _settle_crops(state, now, observations=observations) or changed or bool(state.get("_migrated"))
            environment = _environment_snapshot(state, observations, now) or {}
            snapshot = garden_scene.build_visible_snapshot(
                state, context, weather_tags=weather_tags, animal_weather_mode=animal_mode,
                yard_water=str(environment.get("yard_water") or "none"),
                rain_streak_days=int(environment.get("rain_streak_days") or 0),
            )
            expected_key = garden_scene.scene_key(snapshot)
            cached = garden_scene.cached_text(state["meta"].get("scene_cache"), expected_key)
            if cached is not None:
                shown_at = now if explicit_now else calendar_context(datetime.now(TZ)).now
                changed = _acknowledge_conditions_shown_in_scene(state, shown_at) or changed
            if changed:
                _write_state_unlocked(state, path)
            if cached is not None:
                return cached

        with _scene_generation_locked(path):
            # 等待另一进程生成期间可能已经有缓存或可见状态变化，联网前再核对一次。
            with _locked(path, exclusive=True):
                state = _read_state_unlocked(path, now=now, rng=rng)
                changed = bool(_advance_entries(
                    state["entries"], now, rng, weather_mode=animal_mode,
                ))
                changed = _settle_crops(state, now, observations=observations) or changed or bool(state.get("_migrated"))
                environment = _environment_snapshot(state, observations, now) or {}
                snapshot = garden_scene.build_visible_snapshot(
                    state, context, weather_tags=weather_tags, animal_weather_mode=animal_mode,
                    yard_water=str(environment.get("yard_water") or "none"),
                    rain_streak_days=int(environment.get("rain_streak_days") or 0),
                )
                current_key = garden_scene.scene_key(snapshot)
                cached = garden_scene.cached_text(state["meta"].get("scene_cache"), current_key)
                if cached is not None:
                    shown_at = now if explicit_now else calendar_context(datetime.now(TZ)).now
                    changed = _acknowledge_conditions_shown_in_scene(state, shown_at) or changed
                if changed:
                    _write_state_unlocked(state, path)
                if cached is not None:
                    return cached
                if current_key != expected_key:
                    continue

            # 网络写手严格在 garden.json 文件锁之外运行；失败不影响确定性状态结算。
            # 实测写手偶发失败（超时/空响应/格式）比例不低，先重试一次再兜底。
            text = None
            last_exc = None
            for attempt in range(2):
                try:
                    candidate = garden_generator.generate_stroll(snapshot)
                    # 运行时再闸一次：即使写手实现被替换或测试注入，也不能绕过
                    # 场景事实边界。
                    garden_scene.validate_writer_text(candidate, snapshot)
                    text = candidate
                    break
                except (GardenGeneratorError, ValueError) as exc:
                    last_exc = exc
            if text is not None:
                source = "deepseek"
            else:
                log_store.write_log(
                    "warning",
                    "activity",
                    "小院子逛逛改用本地兜底",
                    {"reason": garden_generator.safe_failure_category(last_exc)},
                    log_file=sandboxed_log_file,
                )
                text = garden_scene.fallback_scene(snapshot)
                try:
                    garden_scene.validate_writer_text(text, snapshot)
                except ValueError:
                    text = garden_scene.minimal_fallback_scene(snapshot)
                source = "fallback"

            with _locked(path, exclusive=True):
                state = _read_state_unlocked(path, now=now, rng=rng)
                changed = bool(_advance_entries(
                    state["entries"], now, rng, weather_mode=animal_mode,
                ))
                changed = _settle_crops(state, now, observations=observations) or changed or bool(state.get("_migrated"))
                environment = _environment_snapshot(state, observations, now) or {}
                cache = list(state["meta"].get("scene_cache", []))
                existing = garden_scene.cached_text(cache, expected_key)
                if existing is None:
                    cache = garden_scene.append_cache(cache, {
                        "scene_key": expected_key,
                        "text": text,
                        "source": source,
                        "generated_at": now.isoformat(),
                    })
                    state["meta"]["scene_cache"] = cache
                    changed = True
                    existing = text

                current_snapshot = garden_scene.build_visible_snapshot(
                    state, context, weather_tags=weather_tags, animal_weather_mode=animal_mode,
                    yard_water=str(environment.get("yard_water") or "none"),
                    rain_streak_days=int(environment.get("rain_streak_days") or 0),
                )
                current_key = garden_scene.scene_key(current_snapshot)
                if current_key == expected_key:
                    shown_at = now if explicit_now else calendar_context(datetime.now(TZ)).now
                    changed = _acknowledge_conditions_shown_in_scene(state, shown_at) or changed
                if changed:
                    _write_state_unlocked(state, path)
                if current_key == expected_key:
                    return existing

    raise GardenError("小院子画面变化得太快，请稍后再逛逛")


def journal_snapshot(*, now: datetime | None = None, path: Path | None = None) -> dict:
    """只给命令层的手账快照；不展示冷却、骰面或尚未确认的内部事件。"""
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        changed = _settle_crops(state, now, observations=observations) or bool(state.get("_migrated"))
        if changed:
            _write_state_unlocked(state, path)
        seen_species = [dict(item) for item in state["journal"]["species_seen"]]
        known_species = {record.get("species") for record in seen_species}
        for animal in state["animals"]:
            species = animal.get("species")
            if isinstance(species, str) and species and species not in known_species:
                seen_species.append({"species": species, "category": animal.get("category")})
                known_species.add(species)
        return {
            "species_seen": seen_species,
            "crops_harvested": [dict(item) for item in state["journal"]["crops_harvested"]],
            "meals_made": [dict(item) for item in state["journal"]["meals_made"]],
            "gifts_given": [dict(item) for item in state["journal"]["gifts_given"]
                           if item.get("recipient", "baby") == "baby"],
            "gifts_given_all": [dict(item) for item in state["journal"]["gifts_given"]],
            "bond_milestones": [dict(item) for item in state["journal"]["bond_milestones"]],
            "calendar_moments": [dict(item) for item in state["journal"]["calendar_moments"]],
            "crop_incidents": [dict(item) for item in state["journal"]["crop_incidents"]],
            "drainage": [dict(item) for item in state["journal"].get("drainage", [])],
        }


def _season_exit_notice(crop: dict, now: datetime) -> str | None:
    """种下时如果当季眼看要结束，提前告知这是这一批最后一次机会。

    只提醒"能不能新种"这件事——已经种下的这一茬不受换季影响（见
    `_settle_crops`），所以这里不是在警告"来不及熟"，只是告诉玩家换季
    以后短期内不能再种这个了。"""
    cursor = now.astimezone(TZ).date() + timedelta(days=1)
    for _ in range(SEASON_EXIT_SCAN_MAX_DAYS):
        context = calendar_context(_date_at_start(cursor))
        if context.season not in crop["seasons"]:
            days_left = (cursor - now.astimezone(TZ).date()).days
            if days_left > SEASON_EXIT_WARNING_DAYS:
                return None
            term_phrase = f"马上就是{context.term_name}了" if context.term_name else "马上要换季了"
            return f"{term_phrase}，这次种下的{crop['name']}是这一季最后一茬了。"
        cursor += timedelta(days=1)
    return None


def plant_crop(crop_name: str, plot_selector: str | None = None, *, now: datetime | None = None, path: Path | None = None) -> dict:
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    observations = _environment_observations(now)
    crop_id = garden_crops.resolve_crop(crop_name)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        changed = _settle_crops(state, now, observations=observations)
        if crop_id is None:
            available = [
                garden_crops.crop_name(item_id)
                for item_id, amount in state["inventory"]["seeds"].items()
                if int(amount) > 0 and item_id in garden_crops.CROPS
            ]
            if changed or state.get("_migrated"):
                _write_state_unlocked(state, path)
            choices = "、".join(available) if available else "种子盒现在是空的"
            raise GardenError(f"种子盒里没有『{crop_name}』；现在可选：{choices}")
        crop = garden_crops.CROPS[crop_id]
        if calendar_context(now).season not in crop["seasons"]:
            raise GardenError(f"{crop['name']}现在不是适种季节，种子没有扣除")
        environment = _environment_snapshot(state, observations, now) or {}
        if str(environment.get("yard_water") or "none") == "flooded":
            if changed or state.get("_migrated"):
                _write_state_unlocked(state, path)
            raise GardenError("地里还汪着水，先「排水」或等水退了再播吧。")
        plot = _find_plot(state, plot_selector, statuses=("empty",))
        _inventory_take(state["inventory"], "seeds", crop_id)
        plot_id = plot["plot_id"]
        plot.clear()
        plot.update({
            "plot_id": plot_id,
            "crop_id": crop_id, "planted_at": now.isoformat(), "last_settled_at": now.isoformat(),
            "growth_points": 0.0, "stage": "seed", "water_bonus_dates": [],
            "watering_by_date": {}, "ready_at": None,
            "yield_penalty": 0, "status": "growing",
            "cycle_id": uuid.uuid4().hex, "stage_events_seen": [],
            # 第九版：追肥次数随这一茬计数，播种即置 0（设计稿第九版第一节）。
            "fertilize_count": 0,
        })
        if state.get("version") == STATE_VERSION:
            plot.update({
                "soil": _new_soil(now), "watering_history": [],
                "unneeded_watering_by_date": {}, "unneeded_watering_events": {},
            })
        # 生长从种下这一刻起由线性记账（_settle_crops/_accrue_linear_growth）
        # 自然累积——种下当天剩余的小时会按当天速率逐段入账，不再需要在这里
        # 预付一笔整天口径的首日账（旧口径的预付曾让作物偶尔一种下就跳过
        # 种子阶段，且预付只能用中性倍率、与后续记账口径并不一致）。
        _write_state_unlocked(state, path)
        snapshot = _plot_snapshot(plot, now, observations)
        snapshot["season_exit_notice"] = _season_exit_notice(crop, now)
        return snapshot


def _append_watering_audit(plot: dict, *, now: datetime, outcome: str, before: float, after: float, reason: str) -> None:
    history = plot.setdefault("watering_history", [])
    history.append({
        "at": now.isoformat(), "date": _today_key(now), "outcome": outcome,
        "moisture_before": round(before, 4), "moisture_after": round(after, 4),
        "reason": reason, "source": "manual",
    })
    cutoff = now.date() - timedelta(days=WATERING_RESULT_HISTORY_DAYS)
    plot["watering_history"] = [
        record for record in history
        if _parse_iso(record["at"], field="浇水审计时间").date() >= cutoff
    ][-WATERING_RESULT_HISTORY_LIMIT:]


def _watering_copy_style(
    plot: dict,
    now: datetime,
    *,
    outcome: str,
    attempt: int,
) -> str:
    """只决定表现风格，不消耗玩法 RNG，也不影响任何浇水裁决。"""
    signature = "|".join((
        str(plot.get("plot_id") or ""),
        now.isoformat(),
        outcome,
        str(attempt),
    ))
    digest = hashlib.sha256(signature.encode("utf-8")).digest()
    return "physical" if digest[0] % 2 == 0 else "slapstick"


def _water_crop_v5_unlocked(
    state: dict, plot: dict, now: datetime, rng, observations: list[dict],
) -> tuple[dict, bool]:
    soil = plot["soil"]
    day = _today_key(now)
    before = float(soil["moisture"])
    watering_by_date = plot["watering_by_date"]
    events_by_date = plot.setdefault("unneeded_watering_events", {})
    active_here = _active_condition(plot)
    condition_type = active_here["type"] if active_here is not None else None
    previous_actions = int(watering_by_date.get(day, 0))
    if active_here is not None and active_here["type"] == "waterlogged":
        previous_unneeded = _effective_unneeded_count(plot, day)
        outcome = "refused" if previous_unneeded >= 3 else "already_waterlogged"
        attempt = previous_unneeded + 1
        _append_watering_audit(plot, now=now, outcome=outcome, before=before, after=before, reason="active_waterlogged")
        return {
            "plot": dict(plot), "watering_count": previous_actions, "attempt": attempt,
            "outcome": outcome, "condition_type": "waterlogged", "accelerated": False,
            "repeated": True,
            "style": _watering_copy_style(plot, now, outcome=outcome, attempt=attempt),
            "moisture_before": before, "moisture_after": before,
        }, True
    if before < 50:
        after = 80.0
        drying_anchors = [
            _parse_iso(raw, field="土壤干燥原因起点")
            for raw in (
                soil.get("anchor_at"),
                soil.get("last_manual_water_at"),
                soil.get("last_rain_at"),
            )
            if raw is not None
        ]
        drying_start = max(drying_anchors)
        reason = garden_weather.drying_reason(drying_start, now, observations)
        _checkpoint_soil(plot, now, observations, override_moisture=after)
        soil.update({"last_manual_water_at": now.isoformat(), "last_water_source": "manual"})
        # 旧字段只兼容“已执行动作”的有限日计数，不能再反过来裁决土壤是否缺水。
        watering_by_date[day] = min(3, previous_actions + 1)
        events_by_date.pop(day, None)
        plot["unneeded_watering_by_date"].pop(day, None)
        _append_watering_audit(plot, now=now, outcome="watered", before=before, after=after, reason=reason)
        return {
            "plot": dict(plot), "watering_count": watering_by_date[day], "attempt": 1,
            "outcome": "watered", "condition_type": condition_type, "accelerated": False,
            "repeated": previous_actions > 0,
            "style": _watering_copy_style(plot, now, outcome="watered", attempt=1),
            "moisture_before": before, "moisture_after": after,
            "reason": reason,
        }, True
    # 意图按真实时刻追加，永不因为天气重放而删除；有效计数永远只统计最近
    # 一次真实跌破 50 之后发生的意图，因此同一天跌破又被补湿后，后续意图
    # 仍按 no_need → protest → waterlogged 正常前进，不会每次都退回第一次。
    events_by_date.setdefault(day, []).append(now.isoformat())
    events_by_date[day] = events_by_date[day][-32:]
    # 跟 exposure_by_date/exposure_finalized_by_date 用同一个 8 天保留窗口
    # 裁剪：每天最多产生一个新日期键，第 9 天若不裁剪就会写出校验本身
    # 拒绝读取的状态。
    for obsolete in sorted(events_by_date)[:-8]:
        events_by_date.pop(obsolete, None)
    attempts = _effective_unneeded_count(plot, day)
    if attempts >= 4:
        outcome, style, after = "refused", None, before
    elif attempts == 3:
        if active_here is not None:
            outcome, style, after, condition_type = "blocked_by_condition", None, before, active_here["type"]
        else:
            condition = _create_crop_condition(state, plot, "waterlogged", now, announced=True)
            outcome, style, after, condition_type = "waterlogged", None, 100.0, condition["type"]
            _checkpoint_soil(plot, now, observations, override_moisture=after)
            soil["last_manual_water_at"] = now.isoformat()
            soil["last_water_source"] = "manual"
            watering_by_date[day] = min(3, previous_actions + 1)
    elif attempts == 2:
        outcome, after = "protest", before
        watering_by_date[day] = min(3, previous_actions + 1)
    else:
        outcome = "no_need" if before < 70 else "too_wet"
        after = before
    style = _watering_copy_style(plot, now, outcome=outcome, attempt=attempts)
    _refresh_unneeded_display(plot)
    _append_watering_audit(plot, now=now, outcome=outcome, before=before, after=after, reason="unneeded")
    return {
        "plot": dict(plot), "watering_count": int(watering_by_date.get(day, previous_actions)), "attempt": attempts,
        "outcome": outcome, "condition_type": condition_type, "accelerated": False,
        "repeated": attempts > 1, "style": style, "moisture_before": before, "moisture_after": after,
    }, True


def _water_crop_unlocked(
    state: dict,
    plot: dict,
    now: datetime,
    rng,
    observations: list[dict] | None = None,
) -> tuple[dict, bool]:
    """在调用方持有状态锁时浇一块地，并报告是否需要写盘。"""
    if real_environment_enabled() and state.get("version") == STATE_VERSION:
        return _water_crop_v5_unlocked(state, plot, now, rng, observations or [])
    day = _today_key(now)
    watering_by_date = plot["watering_by_date"]
    previous_count = int(watering_by_date.get(day, 0))
    active_here = _active_condition(plot)
    if active_here is not None and active_here["type"] == "waterlogged":
        changed = _acknowledge_condition_shown(
            state, active_here["condition_id"], now,
        )
        outcome = "already_waterlogged"
        attempt = previous_count + 1
        return {
            "plot": dict(plot), "watering_count": previous_count,
            "attempt": attempt, "outcome": outcome,
            "condition_type": "waterlogged", "accelerated": False,
            "repeated": previous_count > 0,
            "style": _watering_copy_style(plot, now, outcome=outcome, attempt=attempt),
        }, changed
    if previous_count >= 3:
        changed = False
        if active_here is not None:
            changed = _acknowledge_condition_shown(
                state, active_here["condition_id"], now,
            )
        outcome = "refused"
        attempt = previous_count + 1
        return {
            "plot": dict(plot), "watering_count": previous_count,
            "attempt": attempt, "outcome": outcome,
            "condition_type": active_here["type"] if active_here else None,
            "accelerated": False, "repeated": True,
            "style": _watering_copy_style(plot, now, outcome=outcome, attempt=attempt),
        }, changed

    watering_count = previous_count + 1
    watering_by_date[day] = watering_count
    repeated = watering_count > 1
    accelerated = False
    style = None
    outcome = "watered"
    condition_type = active_here["type"] if active_here else None
    if watering_count == 1:
        plot["water_bonus_dates"].append(day)
        if calendar_context(now).season in garden_crops.CROPS[plot["crop_id"]]["seasons"]:
            accelerated = _apply_growth(state, plot, 0.25, ready_at=now)
        if active_here is not None:
            _acknowledge_condition_shown(
                state, active_here["condition_id"], now,
            )
    elif watering_count == 2:
        style = "physical" if rng.random() < 0.5 else "slapstick"
        outcome = "protest"
    elif any(_active_condition(candidate) is not None for candidate in state["plots"]):
        outcome = "blocked_by_condition"
        active_plot = next(
            candidate for candidate in state["plots"]
            if _active_condition(candidate) is not None
        )
        active_condition = _active_condition(active_plot)
        condition_type = active_condition["type"]
        _acknowledge_condition_shown(
            state, active_condition["condition_id"], now,
        )
    else:
        condition = _create_crop_condition(
            state, plot, "waterlogged", now, announced=True,
        )
        condition_type = condition["type"]
        outcome = "waterlogged"
    if style is None:
        style = _watering_copy_style(
            plot, now, outcome=outcome, attempt=watering_count,
        )
    return {
        "plot": dict(plot), "watering_count": watering_count,
        "attempt": watering_count, "outcome": outcome,
        "condition_type": condition_type, "accelerated": accelerated,
        "repeated": repeated, "style": style,
    }, True


def water_crop(
    plot_selector: str | None = None,
    *,
    now: datetime | None = None,
    path: Path | None = None,
    rng=None,
) -> dict:
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    observations = _environment_observations(now)
    rng = rng or random
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        changed = _settle_crops(state, now, observations=observations) or bool(state.get("_migrated"))
        plot = _find_plot(state, plot_selector, statuses=("growing",))
        timing_before = crop_timing(plot, now, observations=observations)
        result, watered_changed = _water_crop_unlocked(state, plot, now, rng, observations)
        timing_after = crop_timing(plot, now, observations=observations)
        before_seconds = timing_before["current"]["remaining_seconds"]
        after_seconds = timing_after["current"]["remaining_seconds"]
        result.update({
            "plot": _plot_snapshot(plot, now, observations),
            "timing_before": timing_before,
            "timing_after": timing_after,
            "time_saved_seconds": (
                max(0, before_seconds - after_seconds)
                if (result.get("accelerated") or result.get("outcome") == "watered")
                and before_seconds is not None and after_seconds is not None
                else 0
            ),
        })
        if changed or watered_changed:
            _write_state_unlocked(state, path)
        return result


def water_crops(
    plot_selectors: list[str] | None = None,
    *,
    now: datetime | None = None,
    path: Path | None = None,
    rng=None,
) -> list[dict]:
    """在一次状态锁内浇选中的菜畦；未给选择器时浇全部。"""
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    observations = _environment_observations(now)
    rng = rng or random
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        changed = _settle_crops(state, now, observations=observations) or bool(state.get("_migrated"))
        if plot_selectors is None:
            plots = [
                plot for plot in state["plots"]
                if plot.get("status") == "growing"
            ]
        else:
            plot_ids = [
                plot_id_from_selector(selector)
                for selector in plot_selectors
            ]
            if len(set(plot_ids)) != len(plot_ids):
                raise GardenError("批量浇水的地块有重复，请每块地只写一次")
            plots = [
                _find_plot(state, plot_id, statuses=("growing",))
                for plot_id in plot_ids
            ]
        if not plots:
            raise GardenError("现在没有能『浇水』的菜畦")
        results = []
        for plot in plots:
            timing_before = crop_timing(plot, now, observations=observations)
            result, watered_changed = _water_crop_unlocked(
                state, plot, now, rng, observations,
            )
            timing_after = crop_timing(plot, now, observations=observations)
            before_seconds = timing_before["current"]["remaining_seconds"]
            after_seconds = timing_after["current"]["remaining_seconds"]
            result.update({
                "plot": _plot_snapshot(plot, now, observations),
                "timing_before": timing_before,
                "timing_after": timing_after,
                "time_saved_seconds": (
                    max(0, before_seconds - after_seconds)
                    if (result.get("accelerated") or result.get("outcome") == "watered")
                    and before_seconds is not None and after_seconds is not None
                    else 0
                ),
            })
            results.append(result)
            changed = watered_changed or changed
        if changed:
            _write_state_unlocked(state, path)
        return results


def water_all_crops(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    rng=None,
) -> list[dict]:
    """兼容原有全浇入口。"""
    return water_crops(now=now, path=path, rng=rng)


def _pick_plot_for_treatment(
    state: dict,
    plot_selector: str | None,
    *,
    changed: bool,
    path: Path,
    relevant,
) -> dict:
    """处理动作共用的选地规则（``resolve_crop_condition`` 与
    ``fertilize_plot``，原先两处逐字复制，抽取时行为未改）。

    候选是 growing/ready/withered 三态；带选择器就精确匹配；不带选择器时
    先按 ``relevant`` 谓词收窄（各动作对"值得处理"的定义不同：通用处理
    动作只认 active 异常，施肥还要算上欠佳标记），收窄后仍不唯一才要求带
    编号。两条报错前都先把 ``changed`` 落盘——报错不能吞掉已经结算好的
    时间账。"""
    candidates = [
        plot for plot in state["plots"]
        if plot.get("status") in ("growing", "ready", "withered")
    ]
    if plot_selector:
        expected = plot_id_from_selector(plot_selector)
        plot = next(
            (candidate for candidate in candidates if candidate["plot_id"] == expected),
            None,
        )
        if plot is None:
            if changed:
                _write_state_unlocked(state, path)
            raise GardenError("没找到符合条件的菜畦，请检查编号和状态")
        return plot
    relevant_candidates = [
        candidate for candidate in candidates if relevant(candidate)
    ]
    selectable = relevant_candidates if relevant_candidates else candidates
    if len(selectable) != 1:
        if changed:
            _write_state_unlocked(state, path)
        raise GardenError("现在有不止一个可处理菜畦，请带上地块编号")
    return selectable[0]


def _apply_condition_resolution(state: dict, plot: dict, condition: dict, action: str, now: datetime) -> dict:
    """把已确认"动作与异常类型相符"的处理动作落到状态里，返回 resolved 结果。

    调用方须先验证 ``condition`` 是这块地当前的 active 异常、且
    ``action == CONDITION_ACTIONS[condition["type"]]``；本函数只管落地
    ``resolved`` 这一种终态，不做任何前置判断（选地、扣料、拒绝分支都在
    调用方）。从 ``resolve_crop_condition`` 抽出，第八版 ``fertilize_plot``
    的治缺肥分支也调用它，保证走的是同一套结算/冷却写法，不重写一遍
    （设计稿第二节第1点"扣成功后走既有 resolve_crop_condition 逻辑"）。
    """
    if condition.get("announced_at") is None:
        _acknowledge_condition_shown(state, condition["condition_id"], now)
    condition.update({
        "status": "resolved",
        "resolved_at": now.isoformat(),
    })
    incident = _incident_for_condition(state, condition["condition_id"])
    incident.update({
        "outcome": "resolved",
        "resolved_by": action,
        "resolved_at": now.isoformat(),
    })
    _append_incident_node(incident, "resolved", now)
    state["pending_events"] = [
        event for event in state["pending_events"]
        if not (
            isinstance(event, dict)
            and event.get("type") == "crop_condition"
            and event.get("condition_id") == condition["condition_id"]
        )
    ]
    cooldown_until = now + NATURAL_CONDITION_COOLDOWN
    existing_cooldown = state["meta"].get("natural_condition_cooldown_until")
    if existing_cooldown is None or _parse_iso(
        existing_cooldown, field="异常自然冷却时间",
    ) < cooldown_until:
        state["meta"]["natural_condition_cooldown_until"] = cooldown_until.isoformat()
    return {
        "plot_id": plot["plot_id"],
        "crop_id": plot["crop_id"],
        "action": action,
        "condition_type": condition["type"],
        "correct_action": action,
        "outcome": "resolved",
        "yield_penalty": int(plot.get("yield_penalty", 0)),
    }


def resolve_crop_condition(
    action: str,
    plot_selector: str | None = None,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> dict:
    """原子裁决处理动作；错误动作和健康作物永远不产生隐藏收益或惩罚。"""
    if action not in CONDITION_ACTIONS.values():
        raise GardenError("不认识的作物处理动作")
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        changed = _settle_crops(state, now, observations=observations) or bool(state.get("_migrated"))
        plot = _pick_plot_for_treatment(
            state, plot_selector, changed=changed, path=path,
            relevant=lambda candidate: _active_condition(candidate) is not None,
        )

        condition = plot.get("condition")
        result = {
            "plot_id": plot["plot_id"],
            "crop_id": plot["crop_id"],
            "action": action,
            "yield_penalty": int(plot.get("yield_penalty", 0)),
        }
        if plot.get("status") == "withered":
            result.update({
                "outcome": "withered",
                "condition_type": condition.get("type") if isinstance(condition, dict) else None,
                "correct_action": "清理",
            })
        elif not isinstance(condition, dict) or condition.get("status") != "active":
            result.update({
                "outcome": "healthy", "condition_type": None,
                "correct_action": None,
            })
        else:
            correct_action = CONDITION_ACTIONS[condition["type"]]
            result.update({
                "condition_type": condition["type"],
                "correct_action": correct_action,
            })
            if action != correct_action:
                changed = _acknowledge_condition_shown(
                    state, condition["condition_id"], now,
                ) or changed
                result["outcome"] = "wrong_action"
            else:
                result = _apply_condition_resolution(state, plot, condition, action, now)
                changed = True
        if changed:
            _write_state_unlocked(state, path)
        return result


def fertilize_plot(plot_selector: str | None = None, *, now: datetime | None = None, path: Path | None = None) -> dict:
    """施肥：按确定性优先级分流（第八版设计稿第二节 + 第九版设计稿第二节）。

    1. 该地块有 active 的 ``nutrient_deficiency`` 异常 → 治缺肥：先扣肥料
       ×1（不足则报错、不改任何状态），扣成功后复用
       ``_apply_condition_resolution``（跟 ``resolve_crop_condition`` 走
       同一套结算与冷却写法）。
    2. 有别的 active 异常 → 报错指路正确动作，不扣肥料。
    3. 无缺肥异常，但 ``quality=='poor'`` 且成因是内涝（``quality_cause
       == 'flood'``）、地里还是 growing/ready、且没有其他 active 异常：
       还在浸泡就拒绝（不扣肥料）；退水后扣肥料×1、清掉品质与成因、写手账。
    4. 第九版新增：``status=='growing'``（此时已保证无 active 异常）→
       追肥，扣肥料×1，把剩余生长时间砍掉一成（``FERTILIZE_BOOST_RATIO``），
       每茬最多 ``FERTILIZE_MAX_PER_CYCLE`` 次；品相欠佳但 growing 的地
       也走这条，肥料催长跟品相无关。
    5. ``quality=='poor'``（走到这里只剩 ready 的地）但成因是人祸
       （``condition``）或未知（旧档 ``None``）→ 品相已经定了，肥料救不
       回来；不算错误，是正常结果，不扣肥料（设计取舍：欠佳标记不能洗
       白，唯一例外是内涝天灾）。
    6. 其余情况（ready 且正常 / withered）→ 用不上肥料，报错、不扣肥料。

    硬性报错（需要肥料×1 / 地还泡着 / 追肥已到上限 / 用不上肥料）都是拒绝
    分支，绝不改动任何状态、绝不扣肥料——跟"清理"/"除虫"等既有异常动作的
    报错模式一致，见 GardenError 使用惯例。
    """
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        changed = _settle_crops(state, now, observations=observations) or bool(state.get("_migrated"))
        # 选地走 resolve_crop_condition 同一套共享规则（设计稿第二节第4点
        # "不另起炉灶"）；施肥的"值得处理"谓词比通用动作宽一档——欠佳
        # 标记的地、以及第九版新增的"生长中还能追肥"的地都算，否则"全院
        # 健康、只有一块内涝欠佳/只有一块能追肥"时不带编号的施肥永远选不中它。
        plot = _pick_plot_for_treatment(
            state, plot_selector, changed=changed, path=path,
            relevant=lambda candidate: (
                _active_condition(candidate) is not None
                or candidate.get("quality") == "poor"
                or (
                    candidate.get("status") == "growing"
                    and int(candidate.get("fertilize_count", 0)) < FERTILIZE_MAX_PER_CYCLE
                )
            ),
        )

        inventory = state["inventory"]

        def _take_fertilizer_or_raise() -> None:
            # 两个消耗口共用这一份扣料与报错（报错带解法是家规）；拒绝时
            # 只把已结算的时间账落盘，绝不动库存。
            if int(inventory.get("fertilizer", {}).get("fertilizer", 0)) < 1:
                if changed:
                    _write_state_unlocked(state, path)
                raise GardenError(
                    "施肥需要肥料×1，现在还没有肥料——可以先「打扫鸡舍」攒堆肥，"
                    "六份鸡粪沤三天出一份肥料"
                )
            _inventory_take(inventory, "fertilizer", "fertilizer", 1)

        condition = _active_condition(plot)
        if isinstance(condition, dict) and condition.get("type") == "nutrient_deficiency":
            _take_fertilizer_or_raise()
            result = _apply_condition_resolution(state, plot, condition, "施肥", now)
            result["kind"] = "condition"
            _write_state_unlocked(state, path)
            return result

        if isinstance(condition, dict):
            # 有别的 active 异常：肥料帮不上忙，直接指路正确动作（"出事提示
            # 带解法"家规）。这条分支同时保证"内涝欠佳 + 恰好又闹别的异常"
            # 的地块不会掉进下面的"救不回"分支被误判成品相已定——先处理
            # 异常，处理完再回来救品质。flood_rot 故意没配处理动作，入口是
            # 「清理」（见 CONDITION_ACTIONS 的注释）。
            correct_action = CONDITION_ACTIONS.get(condition["type"], "清理")
            # 下面的报错文案会把异常点破给 agent——按"任一可靠可见结果共用
            # 同一确认事实"的既有原则（_acknowledge_condition_shown 的
            # docstring，resolve_crop_condition 的 wrong_action 分支同款），
            # 这里也要记一次"已展示"：恶化时钟与待展示事件的口径必须跟
            # 真实揭示时刻一致，否则同一异常稍后还会被事件重复再报一遍。
            changed = _acknowledge_condition_shown(
                state, condition["condition_id"], now,
            ) or changed
            if changed:
                _write_state_unlocked(state, path)
            raise GardenError(
                f"这块地现在的问题是{CONDITION_LABELS[condition['type']]}，"
                f"先「{correct_action}」才管用，肥料这次帮不上忙"
            )

        quality = plot.get("quality")
        quality_cause = plot.get("quality_cause")
        if (
            quality == "poor" and quality_cause == "flood"
            and plot.get("status") in ("growing", "ready")
            and _active_condition(plot) is None
        ):
            environment = state.get("environment")
            flood_watch = environment.get("flood_watch") if isinstance(environment, dict) else None
            still_soaking = isinstance(flood_watch, dict) and flood_watch.get("since") is not None
            if still_soaking:
                if changed:
                    _write_state_unlocked(state, path)
                raise GardenError("地还泡着，先「排水」再施肥才救得回来")
            _take_fertilizer_or_raise()
            plot["quality"] = None
            plot["quality_cause"] = None
            state["journal"].setdefault("fertilizer_rescues", []).append({
                "at": now.isoformat(), "plot_id": plot["plot_id"], "crop_id": plot["crop_id"],
            })
            result = {
                "kind": "quality_rescue",
                "plot_id": plot["plot_id"],
                "crop_id": plot["crop_id"],
            }
            _write_state_unlocked(state, path)
            return result

        if plot.get("status") == "growing":
            # 第九版：追肥——此时已保证无 active 异常（前两条分支已经拦
            # 下）。品相欠佳但 growing 的地也走这条，肥料催长跟品相无关，
            # 文案不许暗示品相变化（设计稿第九版第二节第4点）。
            fertilize_count = int(plot.get("fertilize_count", 0))
            if fertilize_count >= FERTILIZE_MAX_PER_CYCLE:
                if changed:
                    _write_state_unlocked(state, path)
                raise GardenError(f"这茬已经追过 {FERTILIZE_MAX_PER_CYCLE} 次肥，再施也吸收不了了")
            _take_fertilizer_or_raise()
            crop = garden_crops.CROPS[plot["crop_id"]]
            target = float(crop["growth_days"])
            bonus = (target - float(plot["growth_points"])) * FERTILIZE_BOOST_RATIO
            _apply_growth(state, plot, bonus, ready_at=now)
            fertilize_count += 1
            plot["fertilize_count"] = fertilize_count
            ripened = plot.get("status") == "ready"
            estimated_ready_at = None
            if not ripened:
                multiplier = _current_timing_multiplier(plot, now, observations)
                projected = _project_crop_ready_at(plot, now, multiplier=multiplier)
                estimated_ready_at = projected.isoformat() if projected is not None else None
            result = {
                "kind": "growth_boost",
                "plot_id": plot["plot_id"],
                "crop_id": plot["crop_id"],
                "fertilize_count": fertilize_count,
                "remaining_uses": FERTILIZE_MAX_PER_CYCLE - fertilize_count,
                "ripened": ripened,
                "estimated_ready_at": estimated_ready_at,
            }
            _write_state_unlocked(state, path)
            return result

        if quality == "poor":
            # cause 是 "condition"（人祸拖延）或旧档没有该字段（None）：
            # 不是错误，是正常结果——不扣肥料，也不改任何状态。
            if changed:
                _write_state_unlocked(state, path)
            return {
                "kind": "quality_unrecoverable",
                "plot_id": plot["plot_id"],
                "crop_id": plot["crop_id"],
            }

        if changed:
            _write_state_unlocked(state, path)
        raise GardenError("这块地现在用不上肥料")


def harvest_crop(plot_selector: str | None = None, *, now: datetime | None = None, path: Path | None = None) -> dict:
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        _settle_crops(state, now, observations=observations)
        plot = _find_plot(state, plot_selector, statuses=("ready",))
        crop_id = plot["crop_id"]
        crop = garden_crops.CROPS[crop_id]
        condition = plot.get("condition")
        yield_penalty = int(plot.get("yield_penalty", 0))
        quality = plot.get("quality")
        amount = max(1, int(crop["harvest_amount"]) - yield_penalty)
        # 内涝浸泡满24h打过欠佳标记的地块，收成整批入欠佳堆；种子不分品质，
        # 照旧返还进 seeds（设计稿第六节3）。
        _inventory_add(state["inventory"], "produce_poor" if quality == "poor" else "produce", crop_id, amount)
        _inventory_add(state["inventory"], "seeds", crop_id, crop["seed_return"])
        harvest_record = {
            "at": now.isoformat(), "crop_id": crop_id, "plot_id": plot["plot_id"],
            "amount": amount, "seed_return": crop["seed_return"], "yield_penalty": yield_penalty,
        }
        if quality == "poor":
            harvest_record["quality"] = "poor"
        state["journal"]["crops_harvested"].append(harvest_record)
        plot_id = plot["plot_id"]
        # 收获结果已经在命令回复里明确展示。若继续投递这一轮尚未展示的发芽/
        # 生长/成熟事件，地块被重新播种后就会把旧作物说成新作物已经成熟。
        # 因此收获是这一轮阶段播报的终点，连同兼容旧版留下的短期归档一起清掉。
        cycle_id = plot["cycle_id"]
        state["pending_events"] = [
            event for event in state["pending_events"]
            if not (
                isinstance(event, dict)
                and event.get("type") == "crop_stage"
                and event.get("plot_id") == plot_id
                and event.get("crop_id") == crop_id
                and event.get("cycle_id") == cycle_id
            )
        ]
        state["meta"]["completed_crop_cycles"] = [
            cycle for cycle in state["meta"].get("completed_crop_cycles", [])
            if not (
                isinstance(cycle, dict)
                and cycle.get("plot_id") == plot_id
                and cycle.get("crop_id") == crop_id
                and cycle.get("cycle_id") == cycle_id
            )
        ]
        if isinstance(condition, dict):
            state["pending_events"] = [
                event for event in state["pending_events"]
                if not (
                    isinstance(event, dict)
                    and event.get("type") == "crop_condition"
                    and event.get("condition_id") == condition["condition_id"]
                )
            ]
        plot.clear()
        plot.update(_empty_plot(plot_id))
        _write_state_unlocked(state, path)
        return {
            "crop_id": crop_id, "amount": amount, "seed_return": crop["seed_return"],
            "yield_penalty": yield_penalty, "plot_id": plot_id,
            "quality": quality,
        }


def clear_withered_crop(
    plot_selector: str | None = None,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> dict:
    """只清理已经枯死的当前轮次；不返种、不返作物，也不误删健康作物。"""
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        _settle_crops(state, now, observations=observations)
        plot = _find_plot(state, plot_selector, statuses=("withered",))
        condition = plot.get("condition")
        if not isinstance(condition, dict) or condition.get("status") != "failed":
            raise GardenError("枯死作物的事故记录不完整，已停止写入以免误删")
        result = {
            "plot_id": plot["plot_id"],
            "crop_id": plot["crop_id"],
            "cycle_id": plot["cycle_id"],
            "condition_id": condition["condition_id"],
        }
        # 清理命令本身已经明确展示最终结果；同一事故尚未投递的旧恶化卡不能在
        # 空地上事后重放，更不能因失去 plot/cycle 事实而毒坏整个 pending 队列。
        state["pending_events"] = [
            event for event in state["pending_events"]
            if not (
                isinstance(event, dict)
                and (
                    (
                        event.get("type") == "crop_condition"
                        and event.get("condition_id") == condition["condition_id"]
                    )
                    or (
                        event.get("type") == "crop_stage"
                        and event.get("plot_id") == plot["plot_id"]
                        and event.get("cycle_id") == plot["cycle_id"]
                    )
                )
            )
        ]
        plot_id = plot["plot_id"]
        plot.clear()
        plot.update(_empty_plot(plot_id))
        _write_state_unlocked(state, path)
        return result


def drain_yard(*, now: datetime | None = None, path: Path | None = None) -> dict:
    """疏通排水沟：拨回浸泡时钟、给院级退水一段加速窗口（设计稿第五节）。

    只在 ``puddles``/``flooded`` 时可执行；``none`` 档与 2 小时频控都是纯
    拒绝分支，不触碰任何状态（``flood_watch``/``since``/欠佳标记全部原样
    保留）。加速退水的效果由 ``_environment_snapshot`` 统一把
    ``flood_watch.drained_at`` 传给 ``yard_water_index`` 实现，这里只负责
    写时钟——事件层（画面档位）与惩罚层（浸泡结算）读的是同一份
    ``flood_watch``，天然不会分裂。
    """
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        changed = _settle_crops(state, now, observations=observations) or bool(state.get("_migrated"))
        environment = state.get("environment")
        visible = _environment_snapshot(state, observations, now) or {}
        level = str(visible.get("yard_water") or "none")
        if level == "none" or not isinstance(environment, dict):
            if changed:
                _write_state_unlocked(state, path)
            raise GardenError("排水沟挺通畅的，院子里也没积水，不用忙活。")
        flood_watch = environment.get("flood_watch")
        if not isinstance(flood_watch, dict):
            flood_watch = {"since": None, "drained_at": None}
            environment["flood_watch"] = flood_watch
        previous_drained_at = garden_weather.parse_timestamp(flood_watch.get("drained_at"))
        if previous_drained_at is not None:
            elapsed = now - previous_drained_at.astimezone(TZ)
            if timedelta(0) <= elapsed < YARD_DRAIN_COOLDOWN:
                if changed:
                    _write_state_unlocked(state, path)
                raise GardenError("刚清过，沟里还通畅着。")
        flood_watch["drained_at"] = now.isoformat()
        rain_streak_days = int(visible.get("rain_streak_days") or 0)
        state["journal"].setdefault("drainage", []).append({
            "at": now.isoformat(), "yard_water": level, "rain_streak_days": rain_streak_days,
        })
        _write_state_unlocked(state, path)
        return {"yard_water": level, "rain_streak_days": rain_streak_days, "at": now.isoformat()}


def feed_animal_treat(id_prefix: str, crop_name: str, *, now: datetime | None = None, path: Path | None = None) -> dict:
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    crop_id = garden_crops.resolve_crop(crop_name)
    if crop_id is None:
        raise GardenError("作物名称不明确，篮子没有动")
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        animals_changed = bool(_advance_entries(
            state["entries"], now, random,
            weather_mode=_animal_weather_mode(observations, now),
        ))
        crops_changed = _settle_crops(state, now, observations=observations)
        changed = animals_changed or crops_changed
        try:
            animal = _find_active_animal(state, id_prefix)
        except GardenError:
            if changed:
                _write_state_unlocked(state, path)
            raise
        if crop_id not in garden_crops.ANIMAL_TREAT_COMPATIBILITY.get(animal.get("category"), frozenset()):
            raise GardenError(f"{garden_crops.crop_name(crop_id)}不适合给{animal.get('category') or '这只小动物'}当零食，篮子没有动")
        # 欠佳优先：动物不嫌弃品相，正好是欠佳库存的去化通道（设计稿第六节4）。
        _take_produce_ranked(state["inventory"], crop_id, 1, prefer_poor=True)
        bond = _award_bond_points(state, animal, "菜畦零食", now)
        animal["last_cared_at"] = now.isoformat()
        animal["last_action"] = "投喂"
        _write_state_unlocked(state, path)
        return {"entry": animal, "crop_id": crop_id, "bond": bond}


def _recipe_ingredients_available(inventory: dict, recipe: dict) -> bool:
    """做菜可做性判断的唯一实现（第七版阶段D，设计稿第六节4）。

    ``produce``/``produce_poor`` 合计判断——CLI「够做什么」、网页做菜面板
    与 `make_meal` 自身校验共用这一个函数，不各自重算一遍，从根上避免
    前后端口径分裂。只判断"理论上能不能做"（含蛋类最低需求量），具体这
    次想放几个蛋的校验仍由 `make_meal` 自己处理。
    """
    produce = inventory.get("produce") or {}
    poor = inventory.get("produce_poor") or {}
    for crop_id, amount in recipe["ingredients"].items():
        if int(produce.get(crop_id, 0)) + int(poor.get(crop_id, 0)) < amount:
            return False
    prepared = inventory.get("prepared_food") or {}
    for prepared_id, amount in recipe.get("prepared_ingredients", {}).items():
        if int(prepared.get(prepared_id, 0)) < amount:
            return False
    egg_range = recipe.get("egg_range")
    if egg_range is not None:
        eggs = int((inventory.get("animal_products") or {}).get("egg", 0))
        if eggs < egg_range[0]:
            return False
    return True


def make_meal(
    recipe_or_crop: str,
    *,
    egg_count: int | None = None,
    now: datetime | None = None,
    path: Path | None = None,
) -> dict:
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    recipe_id = garden_crops.resolve_recipe(recipe_or_crop)
    if recipe_id is None:
        crop_id = garden_crops.resolve_crop(recipe_or_crop)
        if crop_id is not None:
            choices = garden_crops.recipes_for_crop(crop_id)
            if choices:
                names = "、".join(garden_crops.RECIPES[item]["name"] for item in choices)
                raise GardenError(
                    f"{garden_crops.crop_name(crop_id)}可以做：{names}；请说具体菜名，篮子没有动"
                )
            raise GardenError(f"{garden_crops.crop_name(crop_id)}目前没有对应的菜谱，篮子没有动")
        raise GardenError("食谱或作物名称不明确，篮子没有动")
    recipe = garden_crops.RECIPES[recipe_id]
    egg_range = recipe.get("egg_range")
    if egg_range is not None:
        minimum, maximum = egg_range
        if egg_count is None:
            raise GardenError(
                f"请由 agent 决定「{recipe['name']}」放几个蛋（{minimum}～{maximum}个），篮子没有动"
            )
        if isinstance(egg_count, bool) or not isinstance(egg_count, int) or not minimum <= egg_count <= maximum:
            raise GardenError(f"「{recipe['name']}」可以放{minimum}～{maximum}个蛋，篮子没有动")
    elif egg_count is not None:
        raise GardenError(f"「{recipe['name']}」不需要选择鸡蛋数量，篮子没有动")
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        _settle_crops(state, now, observations=observations)
        prepared_ingredients_needed = recipe.get("prepared_ingredients", {})
        # 蛋类：`_recipe_ingredients_available` 只查配方最低需求，这次具体
        # 想放几个蛋的校验单独做（`egg_count` 在放蛋类配方里必然已通过上面
        # 的 min/max 校验，恒 >= 最低需求，这里是这次数量的真实约束）。
        missing_eggs = egg_count is not None and int(
            state["inventory"]["animal_products"].get("egg", 0)
        ) < egg_count
        if not _recipe_ingredients_available(state["inventory"], recipe) or missing_eggs:
            needed_parts = [
                f"{garden_crops.crop_name(crop_id)}×{amount}"
                for crop_id, amount in recipe["ingredients"].items()
            ]
            needed_parts.extend(
                f"{garden_crops.RECIPES[prepared_id]['name']}×{amount}"
                for prepared_id, amount in prepared_ingredients_needed.items()
            )
            if egg_count is not None:
                needed_parts.append(f"鸡蛋×{egg_count}")
            needed = "、".join(needed_parts)
            raise GardenError(f"食材不够，「{recipe['name']}」需要：{needed}，这次没有动任何东西")
        # 欠佳优先消耗，也是欠佳库存的天然去化通道（设计稿第六节4）；
        # poor_ingredients 只记录实际动用了欠佳库存的那些食材及数量，空的
        # 不写字段，旧记录天然兼容。
        poor_ingredients = {}
        for crop_id, amount in recipe["ingredients"].items():
            poor_used = _take_produce_ranked(state["inventory"], crop_id, amount, prefer_poor=True)
            if poor_used:
                poor_ingredients[crop_id] = poor_used
        for prepared_id, amount in prepared_ingredients_needed.items():
            _inventory_take(state["inventory"], "prepared_food", prepared_id, amount)
        animal_ingredients = {}
        if egg_count is not None:
            _inventory_take(state["inventory"], "animal_products", "egg", egg_count)
            animal_ingredients["egg"] = egg_count
        _inventory_add(state["inventory"], "prepared_food", recipe_id)
        record = {
            "at": now.isoformat(), "recipe_id": recipe_id,
            "ingredients": dict(recipe["ingredients"]),
            "prepared_ingredients": dict(prepared_ingredients_needed),
            "animal_ingredients": animal_ingredients,
        }
        if poor_ingredients:
            record["poor_ingredients"] = poor_ingredients
        state["journal"]["meals_made"].append(record)
        _write_state_unlocked(state, path)
        return {"recipe_id": recipe_id, "egg_count": egg_count, "record": record}


def _resolve_gift_item(item_name: str):
    crop_id = garden_crops.resolve_crop(item_name)
    cleaned = "".join(item_name.split()).lower()
    recipe_matches = [
        candidate for candidate, recipe in garden_crops.RECIPES.items()
        if cleaned in (candidate, recipe["name"].lower())
    ]
    recipe_id = recipe_matches[0] if len(recipe_matches) == 1 else None
    candidates = []
    if crop_id:
        candidates.append(("produce", crop_id, garden_crops.crop_name(crop_id)))
    if recipe_id:
        candidates.append(("prepared_food", recipe_id, garden_crops.RECIPES[recipe_id]["name"]))
    if len(candidates) != 1:
        raise GardenError("想送的作物或成品不明确，篮子没有动")
    return candidates[0]


def _give_gift(item_name: str, *, recipient: str = "baby", note: str = "",
               now: datetime | None = None, path: Path | None = None) -> dict:
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    section, item_id, display = _resolve_gift_item(item_name)
    observations = _environment_observations(now)
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        _settle_crops(state, now, observations=observations)
        quality = None
        if section == "produce":
            # 好品相优先；好的不够才动欠佳，此时诚实标注不藏着掖着
            # （设计稿第六节4）。prepared_food（成品）不分档，走原逻辑。
            poor_used = _take_produce_ranked(state["inventory"], item_id, 1, prefer_poor=False)
            if poor_used:
                quality = "poor"
                display = f"{display}（品相欠佳）"
        else:
            _inventory_take(state["inventory"], section, item_id)
        record = {
            "at": now.isoformat(), "section": section,
            "item_id": item_id, "display_name": display,
            "recipient": recipient,
        }
        if quality:
            record["quality"] = quality
        if note:
            record["note"] = note
        state["journal"]["gifts_given"].append(record)
        _write_state_unlocked(state, path)
        return {"display_name": display, "record": record}


# give_to_baby / give_to_friend 是两个示例收礼人实现，仅作参考——接入方
# 可以按自己的场景增删收礼人种类，函数名/参数形状保持不变以兼容既有调用方。


def give_to_baby(item_name: str, *, note: str = "",
                 now: datetime | None = None, path: Path | None = None) -> dict:
    return _give_gift(item_name, recipient="baby", note=note, now=now, path=path)


def give_to_friend(item_name: str, *, note: str = "",
                   now: datetime | None = None, path: Path | None = None) -> dict:
    return _give_gift(item_name, recipient="friend", note=note, now=now, path=path)


def _validate_bond_pending_event(event: object, animal: dict | None) -> dict:
    """不完整或未知的事件绝不能先删后报错。"""
    if not isinstance(event, dict) or event.get("type") != "bond_milestone":
        raise GardenError("小院子待展示事件格式损坏，已停止写入以免覆盖原记录")
    text_fields = ("event_id", "animal_id", "animal_name", "category", "personality")
    if any(not isinstance(event.get(key), str) or not event[key] for key in text_fields):
        raise GardenError("小院子待展示事件字段不完整，已停止写入以免覆盖原记录")
    if event["category"] not in garden_content.ANIMAL_SPECIES_BY_CATEGORY or event["personality"] not in garden_content.PERSONALITIES:
        raise GardenError("小院子待展示事件动物信息无效，已停止写入以免覆盖原记录")
    level = event.get("level")
    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 5:
        raise GardenError("小院子待展示事件等级无效，已停止写入以免覆盖原记录")
    if not isinstance(animal, dict) or animal.get("kind") != "animal" or animal.get("id") != event["animal_id"]:
        raise GardenError("小院子待展示事件找不到对应动物，已停止写入以免覆盖原记录")
    expected_id = f"bond:{event['animal_id']}:level:{level}"
    if event["event_id"] != expected_id:
        raise GardenError("小院子待展示事件编号不一致，已停止写入以免覆盖原记录")
    return event


def _validate_crop_pending_event(event: object, cycle: dict | None) -> dict:
    if not isinstance(event, dict) or event.get("type") != "crop_stage":
        raise GardenError("小院子待展示事件格式损坏，已停止写入以免覆盖原记录")
    fields = ("event_id", "plot_id", "crop_id", "cycle_id", "stage")
    if any(not isinstance(event.get(key), str) or not event[key] for key in fields):
        raise GardenError("小院子作物事件字段不完整，已停止写入以免覆盖原记录")
    if event["crop_id"] not in garden_crops.CROPS or event["stage"] not in _CROP_STAGE_ORDER[1:]:
        raise GardenError("小院子作物事件字段无效，已停止写入以免覆盖原记录")
    if not isinstance(cycle, dict) or cycle.get("plot_id") != event["plot_id"] or cycle.get("crop_id") != event["crop_id"] or cycle.get("cycle_id") != event["cycle_id"]:
        raise GardenError("小院子作物事件找不到对应菜畦，已停止写入以免覆盖原记录")
    if cycle.get("stage") not in _CROP_STAGE_ORDER or _CROP_STAGE_ORDER.index(cycle["stage"]) < _CROP_STAGE_ORDER.index(event["stage"]):
        raise GardenError("小院子作物事件阶段与菜畦事实不一致，已停止写入以免覆盖原记录")
    expected_id = f"crop:{event['plot_id']}:{event['cycle_id']}:stage:{event['stage']}"
    if event["event_id"] != expected_id:
        raise GardenError("小院子作物事件编号不一致，已停止写入以免覆盖原记录")
    return event


def _validate_condition_event_copy(event: dict) -> None:
    copy = event.get("copy")
    if copy is None:
        return
    if not isinstance(copy, dict) or set(copy) != {"text", "source"}:
        raise GardenError("小院子作物异常事件正文格式损坏，已停止写入以免覆盖原记录")
    text = copy.get("text")
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > 360
        or not text.endswith("\n" + _condition_event_result_line(event))
    ):
        raise GardenError("小院子作物异常事件正文与结果不一致，已停止写入以免覆盖原记录")
    if copy.get("source") not in ("deepseek", "fallback"):
        raise GardenError("小院子作物异常事件正文来源无效，已停止写入以免覆盖原记录")


def _validate_condition_pending_event(event: object, plot: dict | None) -> dict:
    if not isinstance(event, dict) or event.get("type") != "crop_condition":
        raise GardenError("小院子作物异常事件格式损坏，已停止写入以免覆盖原记录")
    text_fields = (
        "event_id", "condition_id", "plot_id", "crop_id", "cycle_id",
        "condition_type", "severity",
    )
    if any(not isinstance(event.get(key), str) or not event[key] for key in text_fields):
        raise GardenError("小院子作物异常事件字段不完整，已停止写入以免覆盖原记录")
    if (
        event["plot_id"] not in _PLOT_IDS
        or event["crop_id"] not in garden_crops.CROPS
        or event["condition_type"] not in CONDITION_TYPES
        or event["severity"] not in CONDITION_SEVERITIES
    ):
        raise GardenError("小院子作物异常事件字段无效，已停止写入以免覆盖原记录")
    if not isinstance(plot, dict) or any(
        plot.get(key) != event[key] for key in ("plot_id", "crop_id", "cycle_id")
    ):
        raise GardenError("小院子作物异常事件找不到对应菜畦，已停止写入以免覆盖原记录")
    condition = plot.get("condition")
    if (
        not isinstance(condition, dict)
        or condition.get("condition_id") != event["condition_id"]
        or condition.get("type") != event["condition_type"]
        or CONDITION_SEVERITIES.index(condition.get("severity")) < CONDITION_SEVERITIES.index(event["severity"])
    ):
        raise GardenError("小院子作物异常事件与菜畦事实不一致，已停止写入以免覆盖原记录")
    expected_id = f"{event['condition_id']}:severity:{event['severity']}"
    expected_penalty = 0 if event["severity"] == "warning" else 1
    if event["event_id"] != expected_id or event.get("yield_penalty") != expected_penalty:
        raise GardenError("小院子作物异常事件编号或结果不一致，已停止写入以免覆盖原记录")
    _validate_condition_event_copy(event)
    return event


def _validate_yard_water_pending_event(event: object) -> dict:
    if not isinstance(event, dict) or event.get("type") != "yard_water":
        raise GardenError("小院子内涝事件格式损坏，已停止写入以免覆盖原记录")
    fields = ("event_id", "transition", "level", "occurred_at", "text")
    if any(not isinstance(event.get(key), str) or not event[key] for key in fields):
        raise GardenError("小院子内涝事件字段不完整，已停止写入以免覆盖原记录")
    transition = event["transition"]
    level = event["level"]
    if (
        transition not in {"entered", "exited"}
        or (transition == "entered" and level != "flooded")
        or (transition == "exited" and level not in {"none", "puddles"})
        or not event["event_id"].startswith(f"yard-water:{transition}:")
        or event["text"] not in garden_content.YARD_WATER_EVENT_TEXT[transition]
    ):
        raise GardenError("小院子内涝事件事实不一致，已停止写入以免覆盖原记录")
    _parse_iso(event["occurred_at"], field="内涝事件时间")
    return event


def _validate_flood_damage_pending_event(event: object) -> dict:
    if not isinstance(event, dict) or event.get("type") != "flood_damage":
        raise GardenError("小院子内涝惩罚事件格式损坏，已停止写入以免覆盖原记录")
    fields = ("event_id", "stage", "occurred_at", "text")
    if any(not isinstance(event.get(key), str) or not event[key] for key in fields):
        raise GardenError("小院子内涝惩罚事件字段不完整，已停止写入以免覆盖原记录")
    stage = event["stage"]
    if (
        stage not in {"quality", "rot"}
        or not event["event_id"].startswith(f"flood-damage:{stage}:")
        or event["text"] not in garden_content.FLOOD_DAMAGE_EVENT_TEXT[stage]
    ):
        raise GardenError("小院子内涝惩罚事件事实不一致，已停止写入以免覆盖原记录")
    _parse_iso(event["occurred_at"], field="内涝惩罚事件时间")
    return event


def _validate_compost_ready_pending_event(event: object) -> dict:
    if not isinstance(event, dict) or event.get("type") != "compost_ready":
        raise GardenError("小院子堆肥完成事件格式损坏，已停止写入以免覆盖原记录")
    fields = ("event_id", "occurred_at", "text")
    if any(not isinstance(event.get(key), str) or not event[key] for key in fields):
        raise GardenError("小院子堆肥完成事件字段不完整，已停止写入以免覆盖原记录")
    units = event.get("units")
    if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
        raise GardenError("小院子堆肥完成事件份数无效，已停止写入以免覆盖原记录")
    pool = garden_content.COMPOST_READY_EVENT_TEXT
    if (
        not event["event_id"].startswith("compost-ready:")
        or event["text"] not in {line.format(units=units) for line in pool}
    ):
        raise GardenError("小院子堆肥完成事件事实不一致，已停止写入以免覆盖原记录")
    _parse_iso(event["occurred_at"], field="堆肥完成事件时间")
    return event


def _calendar_event_definition(kind: str, year: int, calendar_id: str) -> dict | None:
    if kind == "term":
        context = DEFAULT_CALENDAR.term_definition(year, calendar_id)
        if context is None or context.term_started_at is None or context.term_name is None:
            return None
        return {
            "name": context.term_name, "season": context.season,
            "started_at": context.term_started_at.isoformat(),
            "ends_at": context.term_ends_at.isoformat() if context.term_ends_at else None,
        }
    if kind == "festival":
        festival = DEFAULT_CALENDAR.festival_definition(year, calendar_id)
        if festival is None:
            return None
        # 节日所属季节只来自既有 CalendarContextProvider，不在这里自行推月份。
        season = calendar_context(festival.starts_at).season
        return {
            "name": festival.festival_name, "season": season,
            "started_at": festival.starts_at.isoformat(), "ends_at": festival.ends_at.isoformat(),
        }
    return None


def _validate_calendar_pending_event(event: object) -> dict:
    if not isinstance(event, dict) or event.get("type") != "calendar_moment":
        raise GardenError("小院子日历事件格式损坏，已停止写入以免覆盖原记录")
    kind = event.get("calendar_kind")
    calendar_id = event.get("calendar_id")
    year = event.get("year")
    if kind not in ("term", "festival") or not isinstance(calendar_id, str) or not calendar_id or isinstance(year, bool) or not isinstance(year, int):
        raise GardenError("小院子日历事件字段无效，已停止写入以免覆盖原记录")
    definition = _calendar_event_definition(kind, year, calendar_id)
    if definition is None:
        raise GardenError("小院子日历事件不在本地年度表中，已停止写入以免覆盖原记录")
    required = ("event_id", "name", "season", "started_at")
    if any(not isinstance(event.get(key), str) or not event[key] for key in required):
        raise GardenError("小院子日历事件字段不完整，已停止写入以免覆盖原记录")
    if event.get("ends_at") is not None and not isinstance(event.get("ends_at"), str):
        raise GardenError("小院子日历事件窗口无效，已停止写入以免覆盖原记录")
    expected_id = f"calendar:{kind}:{year}:{calendar_id}"
    if event["event_id"] != expected_id or any(event.get(key) != value for key, value in definition.items()):
        raise GardenError("小院子日历事件与本地年度表不一致，已停止写入以免覆盖原记录")
    return event


def _crop_cycle_for_event(state: dict, event: dict) -> dict | None:
    """当前菜畦优先；收获后的同轮 pending 从事实归档继续投递。"""
    matches = list(state["plots"]) + list(state["meta"].get("completed_crop_cycles", []))
    return next((cycle for cycle in matches if isinstance(cycle, dict)
                 and cycle.get("plot_id") == event.get("plot_id")
                 and cycle.get("crop_id") == event.get("crop_id")
                 and cycle.get("cycle_id") == event.get("cycle_id")), None)


def _validate_coop_pending_event(event: object) -> dict:
    if not isinstance(event, dict):
        raise GardenError("小院子鸡舍事件格式损坏，已停止写入以免覆盖原记录")
    event_type = event.get("type")
    if event_type == "coop_egg_offer":
        if (
            event.get("event_id") != "coop:egg-offer:v1"
            or event.get("egg_count") != COOP_EGG_COUNT
            or any(not isinstance(event.get(key), str) or not event[key] for key in ("source_animal_id", "source_animal_name", "text"))
        ):
            raise GardenError("小院子鸡蛋事件字段损坏，已停止写入以免覆盖原记录")
        return event
    if event_type == "coop_incubation":
        stage = event.get("stage")
        clutch_id = event.get("clutch_id")
        if (
            stage not in {name for name, _after in COOP_PROGRESS_AFTER}
            or not isinstance(clutch_id, str)
            or not clutch_id
            or event.get("event_id") != f"coop:incubation:{clutch_id}:{stage}"
            or not isinstance(event.get("text"), str)
            or not event["text"]
        ):
            raise GardenError("小院子孵蛋事件字段损坏，已停止写入以免覆盖原记录")
        return event
    if event_type == "coop_life":
        count_fields = (
            "grown_hens", "grown_roosters", "eggs_laid", "hens_laid", "roosters_crowed",
        )
        counts = [event.get(key) for key in count_fields]
        hatchable_eggs_laid = event.get("hatchable_eggs_laid", 0)
        regular_eggs_laid = event.get("regular_eggs_laid", event.get("eggs_laid"))
        if (
            not isinstance(event.get("event_id"), str)
            or not event["event_id"].startswith("coop:life:")
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (hatchable_eggs_laid, regular_eggs_laid)
            )
            or not any(counts)
            or event["hens_laid"] != event["eggs_laid"]
            or regular_eggs_laid + hatchable_eggs_laid != event["eggs_laid"]
            or not isinstance(event.get("text"), str)
            or not event["text"]
        ):
            raise GardenError("小院子成鸡生活事件字段损坏，已停止写入以免覆盖原记录")
        return event
    raise GardenError("小院子鸡舍事件类型无效，已停止写入以免覆盖原记录")


def _peek_pending_event(state: dict) -> dict | None:
    if not state["pending_events"]:
        return None
    event = state["pending_events"][0]
    if not isinstance(event, dict):
        raise GardenError("小院子待展示事件格式损坏，已停止写入以免覆盖原记录")
    if event.get("type") == "bond_milestone":
        animal = next((entry for entry in state["animals"] if entry.get("id") == event.get("animal_id")), None)
        return _validate_bond_pending_event(event, animal)
    if event.get("type") == "crop_stage":
        return _validate_crop_pending_event(event, _crop_cycle_for_event(state, event))
    if event.get("type") == "crop_condition":
        plot = next((
            candidate for candidate in state["plots"]
            if isinstance(candidate, dict) and candidate.get("plot_id") == event.get("plot_id")
        ), None)
        return _validate_condition_pending_event(event, plot)
    if event.get("type") == "calendar_moment":
        return _validate_calendar_pending_event(event)
    if event.get("type") == "yard_water":
        return _validate_yard_water_pending_event(event)
    if event.get("type") == "flood_damage":
        return _validate_flood_damage_pending_event(event)
    if event.get("type") == "compost_ready":
        return _validate_compost_ready_pending_event(event)
    if event.get("type") in {"coop_egg_offer", "coop_incubation", "coop_life"}:
        return _validate_coop_pending_event(event)
    raise GardenError("小院子待展示事件类型无效，已停止写入以免覆盖原记录")


def _validate_pending_delivery(event: dict) -> None:
    delivery = event.get("delivery")
    if delivery is None:
        return
    if not isinstance(delivery, dict) or not isinstance(delivery.get("token"), str) or not delivery["token"]:
        raise GardenError("小院子待展示事件投递租约损坏，已停止写入以免覆盖原记录")
    _parse_iso(delivery.get("leased_at"), field="待展示事件投递时间")


def _validate_all_pending_events(state: dict) -> None:
    """先验证已有队列，避免任何结算在伪造事件前写回状态。"""
    for raw_event in state["pending_events"]:
        _peek_pending_event({**state, "pending_events": [raw_event]})
        _validate_pending_delivery(raw_event)


def _claim_pending_event(state: dict, now: datetime) -> dict | None:
    """在状态锁内给队首事件发放短租约，保证同一事件只被一个 tick 展示。"""
    pending = _peek_pending_event(state)
    if pending is None:
        return None
    _validate_pending_delivery(pending)
    delivery = pending.get("delivery")
    if delivery is not None:
        leased_at = _parse_iso(delivery["leased_at"], field="待展示事件投递时间")
        if now < leased_at + timedelta(seconds=PENDING_DELIVERY_LEASE_SECONDS):
            return None
    token = uuid.uuid4().hex
    pending["delivery"] = {"token": token, "leased_at": now.isoformat()}
    claimed = {key: value for key, value in pending.items() if key != "delivery"}
    claimed["delivery_token"] = token
    return claimed


def _backfill_pending_events(state: dict) -> bool:
    """兼容上一版已入队的里程碑：只补可由原动物确定的缺字段，不猜数值。"""
    changed = False
    animals = {entry.get("id"): entry for entry in state["animals"]}
    for event in state["pending_events"]:
        if not isinstance(event, dict) or event.get("type") != "bond_milestone":
            continue
        animal = animals.get(event.get("animal_id"))
        if not isinstance(animal, dict):
            continue
        defaults = {
            "animal_name": display_name(animal),
            "category": animal.get("category"),
            "personality": animal.get("personality"),
        }
        for key, value in defaults.items():
            if key not in event and isinstance(value, str) and value:
                event[key] = value
                changed = True
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id in animal["bond_milestones_seen"]:
            animal["bond_milestones_seen"].remove(event_id)
            changed = True
    return changed


_MERGED_CALENDAR_MOMENTS = frozenset({"clear_and_bright", "winter_solstice"})


def _calendar_moment_semantic_id(kind: str, year: int, calendar_id: str) -> str:
    """清明、冬至的节日和节气共享一个跨完整节气窗口的语义编号。"""
    if calendar_id in _MERGED_CALENDAR_MOMENTS:
        return f"calendar:moment:{year}:{calendar_id}"
    return f"calendar:{kind}:{year}:{calendar_id}"


def _seen_calendar_moment_semantics(state: dict, seen: list[str]) -> set[str]:
    """从旧稳定 event_id 与当前 pending 一起恢复已占用的日历语义。"""
    semantics: set[str] = set()
    for event_id in seen:
        parts = event_id.split(":", 3)
        if len(parts) == 4 and parts[0] == "calendar" and parts[1] in ("term", "festival") and parts[2].isdigit():
            semantics.add(_calendar_moment_semantic_id(parts[1], int(parts[2]), parts[3]))
    for event in state["pending_events"]:
        if not isinstance(event, dict) or event.get("type") != "calendar_moment":
            continue
        kind, year, calendar_id = event.get("calendar_kind"), event.get("year"), event.get("calendar_id")
        if kind in ("term", "festival") and isinstance(year, int) and not isinstance(year, bool) and isinstance(calendar_id, str):
            semantics.add(_calendar_moment_semantic_id(kind, year, calendar_id))
    return semantics


def _queue_calendar_moments(state: dict, context) -> bool:
    """把当前节日/节气的首次场景排队；已确认或已排队的窗口绝不重复。"""
    seen = state["meta"].setdefault("seen_calendar_events", [])
    if not isinstance(seen, list) or not all(isinstance(event_id, str) for event_id in seen):
        raise GardenError("小院子日历已见记录格式损坏，已停止写入以免覆盖原记录")
    candidates: list[tuple[str, str]] = []
    # 清明、冬至同时是节气和本期节日：这是同一个院子时刻，只留一张卡与一条手账。
    if context.festival_id:
        candidates.append(("festival", context.festival_id))
    if context.term_id and not (context.festival_id and context.term_id == context.festival_id and context.term_id in _MERGED_CALENDAR_MOMENTS):
        candidates.append(("term", context.term_id))
    current_windows = [f"{kind}:{context.now.year}:{calendar_id}" for kind, calendar_id in candidates]
    observed = state["meta"].get("calendar_observed_windows")
    if observed is None:
        # 首次加载不补播长期节气，但当天仍有效的传统节日是“第一次进入窗口”，
        # 不能被基线静默吃掉；它会像其他确定性事件一样可靠排队。
        state["meta"]["calendar_observed_windows"] = current_windows
        new_windows = {f"festival:{context.now.year}:{calendar_id}" for kind, calendar_id in candidates if kind == "festival"}
    else:
        if not isinstance(observed, list) or not all(isinstance(window_id, str) for window_id in observed):
            raise GardenError("小院子日历观察记录格式损坏，已停止写入以免覆盖原记录")
        new_windows = set(current_windows) - set(observed)
        state["meta"]["calendar_observed_windows"] = current_windows
    changed = False
    occupied_semantics = _seen_calendar_moment_semantics(state, seen)
    for kind, calendar_id in candidates:
        if f"{kind}:{context.now.year}:{calendar_id}" not in new_windows:
            continue
        event_id = f"calendar:{kind}:{context.now.year}:{calendar_id}"
        semantic_id = _calendar_moment_semantic_id(kind, context.now.year, calendar_id)
        if semantic_id in occupied_semantics:
            continue
        definition = _calendar_event_definition(kind, context.now.year, calendar_id)
        if definition is None:
            continue
        state["pending_events"].append({
            "event_id": event_id, "type": "calendar_moment", "calendar_kind": kind,
            "calendar_id": calendar_id, "year": context.now.year, **definition,
        })
        occupied_semantics.add(semantic_id)
        changed = True
    return changed or observed != current_windows


def _mark_condition_announced(state: dict, condition_id: str, now: datetime) -> bool:
    matches = [
        plot for plot in state["plots"]
        if isinstance(plot.get("condition"), dict)
        and plot["condition"].get("condition_id") == condition_id
    ]
    if len(matches) != 1:
        raise GardenError("小院子作物异常找不到对应菜畦，已停止写入以免覆盖原记录")
    condition = matches[0]["condition"]
    if condition.get("announced_at") is not None:
        return False
    condition["announced_at"] = now.isoformat()
    incident = _incident_for_condition(state, condition_id)
    incident["announced_at"] = now.isoformat()
    _append_incident_node(incident, "announced", now)
    return True


def _queue_coop_progress(state: dict, now: datetime, rng) -> bool:
    coop = state["coop"]
    if coop["story_status"] != "incubating":
        return False
    clutch_id = coop["clutch_id"]
    if any(
        isinstance(event, dict)
        and event.get("type") == "coop_incubation"
        and event.get("clutch_id") == clutch_id
        for event in state["pending_events"]
    ):
        return False
    started = _parse_iso(coop["incubation_started_at"], field="开始孵蛋时间")
    due = [stage for stage, after in COOP_PROGRESS_AFTER if now >= started + after]
    if not due:
        return False
    latest = due[-1]
    queued = coop["progress_queued"]
    if latest in queued:
        return False
    # 中途没有唤醒时直接跳到当前最新阶段，早先画面标作已跳过，绝不在孵化
    # 当天补播一串“七小时前/十四小时前”的过时场景。
    for stage in due:
        if stage not in queued:
            queued.append(stage)
    text = None
    if latest == "warming":
        text = rng.choice(garden_content.COOP_INCUBATION_WARMING_TEXT)
    elif latest == "tapping":
        text = rng.choice(garden_content.COOP_INCUBATION_TAPPING_TEXT)
    if latest == "hatched":
        hatched_at = _parse_iso(coop["hatch_at"], field="预计孵化时间")
        egg_count = coop["incubating_egg_count"]
        coop["story_status"] = "hatched"
        new_chicks = []
        for _index in range(egg_count):
            sex = rng.choice(("hen", "rooster"))
            chick = {
                "id": uuid.uuid4().hex[:8],
                "hatched_at": hatched_at.isoformat(),
                "sex": sex,
                "stage": "chick",
                "matures_at": (hatched_at + CHICK_MATURITY_DURATION).isoformat(),
                "matured_at": None,
                "next_egg_at": None,
                "eggs_laid": 0,
                "nickname": None,
                "feed_count": 0,
                "pet_count": 0,
                "last_fed_at": None,
                "last_petted_at": None,
            }
            chick.update(_take_chicken_profile([*coop["chicks"], *new_chicks], sex, rng))
            new_chicks.append(chick)
        for chick in new_chicks:
            if chick["sex"] == "hen":
                chick["next_egg_at"] = (
                    hatched_at + CHICK_MATURITY_DURATION + HEN_FIRST_EGG_AFTER_MATURITY
                ).isoformat()
        coop["chicks"].extend(new_chicks)
        coop["incubating_egg_count"] = 0
        hens = sum(chick["sex"] == "hen" for chick in new_chicks)
        roosters = egg_count - hens
        summary = rng.choice(garden_content.COOP_HATCH_TEXT).format(
            hens=hens, roosters=roosters,
        )
        introductions = "\n".join(
            f"- {chicken_display_name(chick)}（{chick['personality']}）：{chick['intro']}"
            for chick in new_chicks
        )
        text = f"{summary}\n逐只看，它们各有自己的模样：\n{introductions}"
    if text is None:
        raise GardenError("小院子鸡舍孵化阶段没有对应文案，已停止写入")
    state["pending_events"].append({
        "event_id": f"coop:incubation:{clutch_id}:{latest}",
        "type": "coop_incubation",
        "clutch_id": clutch_id,
        "stage": latest,
        "text": text,
    })
    return True


def _queue_coop_life(
    state: dict,
    now: datetime,
    rng,
    *,
    observations: list[dict] | None = None,
) -> bool:
    """把长成、下蛋和晨鸣合成一次可靠事件，避免多只鸡逐条挤上下文。"""
    coop = state["coop"]
    if coop["story_status"] not in {"hatched", "incubating"} or any(
        isinstance(event, dict) and event.get("type") == "coop_life"
        for event in state["pending_events"]
    ):
        return False

    grown_hens = 0
    grown_roosters = 0
    for chick in coop["chicks"]:
        if chick["stage"] != "chick":
            continue
        matures_at = _parse_iso(chick["matures_at"], field="小鸡长大时间")
        if now < matures_at:
            continue
        chick["stage"] = "adult"
        chick["matured_at"] = matures_at.isoformat()
        if chick["sex"] == "hen":
            grown_hens += 1
        else:
            grown_roosters += 1

    eggs_laid = 0
    hens_laid = 0
    hatchable_eggs_laid = 0
    laying_hen_names = []
    for chicken in coop["chicks"]:
        if chicken["stage"] != "adult" or chicken["sex"] != "hen":
            continue
        next_egg_at = _parse_iso(chicken["next_egg_at"], field="母鸡下次产蛋时间")
        if now < next_egg_at:
            continue
        # 离线很久也只在本次真正看见院子时每只母鸡结算一次，避免重启后
        # 突然堆出几十枚蛋和一长串补播事件；到期后无论是否下蛋都重新计时，
        # 不会因为没中概率就每次调用都重新掷骰。
        chicken["next_egg_at"] = (now + HEN_EGG_INTERVAL).isoformat()
        if rng.random() >= HEN_EGG_LAY_PROBABILITY:
            continue
        chicken["eggs_laid"] += 1
        eggs_laid += 1
        hens_laid += 1
        laying_hen_names.append(chicken_display_name(chicken))
        if rng.random() < HATCHABLE_EGG_PROBABILITY:
            hatchable_eggs_laid += 1
    if eggs_laid:
        regular_eggs_laid = eggs_laid - hatchable_eggs_laid
        if regular_eggs_laid:
            _inventory_add(state["inventory"], "animal_products", "egg", regular_eggs_laid)
        if hatchable_eggs_laid:
            _inventory_add(
                state["inventory"], "animal_products", "hatchable_egg", hatchable_eggs_laid,
            )
        coop["total_eggs_laid"] += eggs_laid
    else:
        regular_eggs_laid = 0

    roosters = [
        chicken for chicken in coop["chicks"]
        if chicken["stage"] == "adult" and chicken["sex"] == "rooster"
    ]
    roosters_crowed = 0
    crowing_rooster_name = None
    today = now.date().isoformat()
    if (
        roosters
        and ROOSTER_CROW_START_HOUR <= now.hour < ROOSTER_CROW_END_HOUR
        and coop["last_crow_date"] != today
    ):
        # 每天只随机选一只公鸡打鸣，不是全体鸡舍一起叫。
        crowing_rooster_name = chicken_display_name(rng.choice(roosters))
        roosters_crowed = 1
        coop["last_crow_date"] = today

    if not any((grown_hens, grown_roosters, eggs_laid, roosters_crowed)):
        return False
    parts = []
    if grown_hens or grown_roosters:
        parts.append(
            rng.choice(garden_content.COOP_MATURITY_TEXT).format(
                hens=grown_hens, roosters=grown_roosters,
            )
        )
    if roosters_crowed:
        fresh = [
            item for item in (observations or [])
            if real_environment_enabled() and garden_weather.fresh_at(item, now)
        ]
        latest = max(fresh, key=garden_weather.observation_sort_key) if fresh else None
        outing = garden_weather.outing_mode(latest)
        if outing in {"typhoon", "storm_rain"}:
            crow_pool = garden_content.COOP_ROOSTER_CROW_WEATHER_TEXT["storm_rain"]
        elif outing in {"windy_rain", "rain", "light_rain"}:
            crow_pool = garden_content.COOP_ROOSTER_CROW_WEATHER_TEXT["rain"]
        elif outing == "snow":
            crow_pool = garden_content.COOP_ROOSTER_CROW_WEATHER_TEXT["snow"]
        elif outing == "gale":
            crow_pool = garden_content.COOP_ROOSTER_CROW_WEATHER_TEXT["windy"]
        else:
            crow_pool = garden_content.COOP_ROOSTER_CROW_TEXT
        parts.append(
            rng.choice(crow_pool).format(
                name=crowing_rooster_name,
            )
        )
    if eggs_laid:
        names_text = "、".join(laying_hen_names)
        if hatchable_eggs_laid:
            parts.append(
                rng.choice(garden_content.COOP_HATCHABLE_EGG_TEXT).format(
                    names=names_text,
                    eggs=eggs_laid,
                    hatchable=hatchable_eggs_laid,
                    regular=regular_eggs_laid,
                )
            )
        else:
            parts.append(
                rng.choice(garden_content.COOP_HEN_EGG_TEXT).format(
                    names=names_text, eggs=eggs_laid,
                )
            )
    event_id = f"coop:life:{now.strftime('%Y%m%dT%H%M%S')}:{uuid.uuid4().hex[:8]}"
    state["pending_events"].append({
        "event_id": event_id,
        "type": "coop_life",
        "grown_hens": grown_hens,
        "grown_roosters": grown_roosters,
        "eggs_laid": eggs_laid,
        "regular_eggs_laid": regular_eggs_laid,
        "hatchable_eggs_laid": hatchable_eggs_laid,
        "hens_laid": hens_laid,
        "roosters_crowed": roosters_crowed,
        "text": "".join(parts),
    })
    return True


def _queue_coop_egg_offer(state: dict, now: datetime, rng) -> bool:
    coop = state["coop"]
    if coop["story_status"] != "unbuilt":
        return False
    active_animals = [
        animal for animal in state["animals"]
        if isinstance(animal, dict) and animal.get("status") == "active"
    ]
    if not active_animals:
        return False
    source = rng.choice(active_animals)
    source_name = display_name(source)
    _inventory_add(state["inventory"], "animal_products", "egg", COOP_EGG_COUNT)
    coop.update({
        "story_status": "awaiting_choice",
        "source_animal_id": source["id"],
        "source_animal_name": source_name,
    })
    state["pending_events"].append({
        "event_id": "coop:egg-offer:v1",
        "type": "coop_egg_offer",
        "source_animal_id": source["id"],
        "source_animal_name": source_name,
        "egg_count": COOP_EGG_COUNT,
        "text": (
            f"{source_name}从院门外一路跑来，咕噜噜地把三枚完整的鸡蛋推到院子里。"
            "是把它们吃掉，还是由你亲自孵化？选择孵化时才会为它们搭起暖和的鸡舍；"
            "如果已经决定，需要真的处理这三枚鸡蛋，不能只把选择说出来。"
        ),
    })
    return True


def acknowledge_crop_condition(
    condition_id: str,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> bool:
    """给查看/逛逛共用的确认入口；明确展示即移除同一首次 pending 并开始计时。"""
    if not isinstance(condition_id, str) or not condition_id:
        raise GardenError("小院子作物异常编号无效")
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path)
        _validate_all_pending_events(state)
        changed = _acknowledge_condition_shown(
            state, condition_id, now,
        )
        if changed:
            _write_state_unlocked(state, path)
        return changed


def confirm_pending_event(event_id: str, delivery_token: str | None = None, *, now: datetime | None = None, path: Path | None = None) -> bool:
    """调用方成功展示并写入记录后，才按稳定 event_id 删除一次。"""
    if not isinstance(event_id, str) or not event_id:
        raise GardenError("小院子待展示事件编号无效")
    if not isinstance(delivery_token, str) or not delivery_token:
        raise GardenError("小院子待展示事件投递凭证无效")
    path = path or GARDEN_FILE
    now = calendar_context(now or datetime.now(TZ)).now
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path)
        for index, raw_event in enumerate(state["pending_events"]):
            if not isinstance(raw_event, dict) or raw_event.get("event_id") != event_id:
                continue
            event = _peek_pending_event({**state, "pending_events": [raw_event]})
            _validate_pending_delivery(event)
            if event.get("delivery", {}).get("token") != delivery_token:
                return False
            state["pending_events"].pop(index)
            if event["type"] == "bond_milestone":
                animal = next(entry for entry in state["animals"] if entry.get("id") == event["animal_id"])
                if event_id not in animal["bond_milestones_seen"]:
                    animal["bond_milestones_seen"].append(event_id)
                milestones = state["journal"].setdefault("bond_milestones", [])
                if not any(isinstance(record, dict) and record.get("event_id") == event_id for record in milestones):
                    milestones.append({
                        "event_id": event_id, "at": now.isoformat(), "animal_name": display_name(animal),
                        "level": event["level"], "level_name": bond_level_name(event["level"]),
                    })
            elif event["type"] == "crop_stage":
                cycle = _crop_cycle_for_event(state, event)
                if not isinstance(cycle, dict):  # _peek 的严格验证已保证；保留防御性边界。
                    raise GardenError("小院子作物事件找不到对应菜畦，已停止写入以免覆盖原记录")
                if event_id not in cycle["stage_events_seen"]:
                    cycle["stage_events_seen"].append(event_id)
                if cycle in state["meta"].get("completed_crop_cycles", []) and not any(
                    candidate.get("type") == "crop_stage"
                    and candidate.get("plot_id") == event["plot_id"]
                    and candidate.get("crop_id") == event["crop_id"]
                    and candidate.get("cycle_id") == event["cycle_id"]
                    for candidate in state["pending_events"] if isinstance(candidate, dict)
                ):
                    state["meta"]["completed_crop_cycles"].remove(cycle)
            elif event["type"] == "crop_condition":
                if event["severity"] == "warning":
                    _mark_condition_announced(state, event["condition_id"], now)
            elif event["type"] == "calendar_moment":
                seen = state["meta"].setdefault("seen_calendar_events", [])
                if event_id not in seen:
                    seen.append(event_id)
                journal = state["journal"].setdefault("calendar_moments", [])
                if not any(isinstance(record, dict) and record.get("event_id") == event_id for record in journal):
                    journal.append({
                        "event_id": event_id, "at": now.isoformat(), "calendar_kind": event["calendar_kind"],
                        "calendar_id": event["calendar_id"], "name": event["name"], "season": event["season"],
                    })
            elif event["type"] == "yard_water":
                pass  # 内涝事实与去重游标已在入队时原子落盘；这里只确认展示。
            elif event["type"] == "flood_damage":
                pass  # 欠佳/泡烂惩罚事实已在入队时原子落盘；这里只确认展示。
            elif event["type"] == "compost_ready":
                pass  # 肥料入库事实已在 _settle_compost 原子落盘；这里只确认展示。
            elif event["type"] in {"coop_egg_offer", "coop_incubation", "coop_life"}:
                pass  # 鸡蛋与孵化事实已在事件入队时原子落盘；这里只确认展示。
            else:  # _peek_pending_event 已严格拦截；保留写盘前的防御边界。
                raise GardenError("小院子待展示事件类型无效，已停止写入以免覆盖原记录")
            state["meta"]["last_visible_event_at"] = now.isoformat()
            _write_state_unlocked(state, path)
            return True
    return False


def release_pending_event(event_id: str, delivery_token: str | None = None, *, path: Path | None = None) -> bool:
    """本地渲染失败时归还尚未对外展示的投递租约。

    正常发送失败、进程崩溃或重启不走这里：短租约到期后会自动恢复投递。只有
    卡片尚未生成的同步异常才立即归还，避免一次 RNG/模板异常白白卡住队列。
    """
    if not isinstance(event_id, str) or not event_id:
        raise GardenError("小院子待展示事件编号无效")
    if not isinstance(delivery_token, str) or not delivery_token:
        raise GardenError("小院子待展示事件投递凭证无效")
    path = path or GARDEN_FILE
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path)
        for raw_event in state["pending_events"]:
            if not isinstance(raw_event, dict) or raw_event.get("event_id") != event_id:
                continue
            event = _peek_pending_event({**state, "pending_events": [raw_event]})
            _validate_pending_delivery(event)
            if event.get("delivery", {}).get("token") != delivery_token:
                return False
            event.pop("delivery", None)
            _write_state_unlocked(state, path)
            return True
    return False


def _persist_condition_event_copy(
    event_id: str,
    delivery_token: str,
    *,
    text: str,
    source: str,
    path: Path | None = None,
) -> dict | None:
    """把首次生成正文钉在原 pending 上；事件已撤销时不再投递旧画面。"""
    path = path or GARDEN_FILE
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path)
        _validate_all_pending_events(state)
        raw_event = next((
            event for event in state["pending_events"]
            if isinstance(event, dict) and event.get("event_id") == event_id
        ), None)
        if raw_event is None:
            return None
        event = _peek_pending_event({**state, "pending_events": [raw_event]})
        if event.get("type") != "crop_condition":
            raise GardenError("小院子作物异常正文找不到对应事件，已停止写入以免覆盖原记录")
        if event.get("delivery", {}).get("token") != delivery_token:
            return None
        existing = event.get("copy")
        if isinstance(existing, dict):
            return dict(existing)
        event["copy"] = {"text": text, "source": source}
        _validate_condition_event_copy(event)
        _write_state_unlocked(state, path)
        return dict(event["copy"])


def _weighted_revisit_choice(entries: list[dict], rng) -> dict:
    weighted: list[dict] = []
    for entry in entries:
        level = _bond_level(int(entry.get("bond_points", 0))) if entry.get("kind") == "animal" else 0
        weighted.extend([entry] * (1 + level))
    return rng.choice(weighted)


def _weather_trace_text(
    active_animals: list[dict],
    context,
    weather_tags: frozenset[str],
    observations: list[dict],
    rng,
) -> str:
    """痕迹场景优先描述某只在场动物当下的天气行为；没有在场动物、模式是
    normal 或对应物种池缺失时，原样退回既有的通用痕迹候选池，行为不变。"""
    if active_animals:
        mode = _animal_weather_mode(observations, context.now)
        if mode != "normal":
            candidate = rng.choice(active_animals)
            category = str(candidate.get("category") or "")
            flavor = _animal_weather_flavor(category, mode, rng)
            if flavor:
                return f"{display_name(candidate)}{flavor}"
    return garden_content.choose_scene(
        garden_content.CONTEXTUAL_TRACES,
        season=context.season,
        day_period=context.day_period,
        weather_tags=weather_tags,
        rng=rng,
    )


def build_event(
    now: datetime,
    *,
    heartbeat: bool = False,
    weather_tags: frozenset[str] = frozenset(),
    observations: list[dict] | None = None,
    rng=None,
    path: Path | None = None,
) -> dict | None:
    """决定本次事件并预留冷却；不访问网络。

    heartbeat 模式每天只在 02:00–10:00 安排一个随机目标时刻，并且至多掷一次
    夜间骰子。调度元数据与条目在同一把跨进程锁里更新，daemon 重启不会重置。
    """
    rng = rng or random
    path = path or GARDEN_FILE
    context = calendar_context(now)
    now = context.now
    observations = _environment_observations(now) if observations is None else observations
    with _locked(path, exclusive=True):
        state = _read_state_unlocked(path, now=now)
        entries = state["entries"]
        meta = state["meta"]
        animal_mode = _animal_weather_mode(observations, now)
        changed = bool(_advance_entries(
            entries, now, rng, weather_mode=animal_mode,
        )) or bool(state.get("_migrated"))
        changed = _backfill_animal_fields(entries, rng) or changed
        changed = _backfill_pending_events(state) or changed

        # 先完整校验旧队列，伪造事件绝不能让后续结算覆盖原文件。
        _validate_all_pending_events(state)

        changed = _settle_crops(state, now, observations=observations) or changed
        changed = _maybe_roll_natural_condition(
            state, now, rng, observations=observations,
        ) or changed
        # 先把本轮全部确定性变化可靠排队，再只领取队首一个主事件。这样已有作物/
        # 亲密 pending 不会吞掉当天的节日，普通冷却也不会吞掉日历窗口。
        changed = _queue_calendar_moments(state, context) or changed
        changed = _queue_coop_progress(state, now, rng) or changed
        changed = _queue_coop_life(
            state, now, rng, observations=observations,
        ) or changed

        pending = _claim_pending_event(state, now)
        if pending is not None:
            _write_state_unlocked(state, path)
            return {"action": "pending", "event": pending}
        if state["pending_events"]:
            # 队首仍由别的 tick 持有投递租约；不能绕过去生成第二个主事件。
            if changed:
                _write_state_unlocked(state, path)
            return None

        if heartbeat:
            if not NIGHT_START_HOUR <= now.hour < NIGHT_END_HOUR:
                if changed:
                    _write_state_unlocked(state, path)
                return None
            today = now.date().isoformat()
            if meta.get("night_date") != today:
                end = datetime.combine(now.date(), dtime(NIGHT_END_HOUR), tzinfo=now.tzinfo)
                seconds_left = max(0.0, (end - now).total_seconds())
                target = now.timestamp() + seconds_left * rng.random()
                meta.update({
                    "night_date": today,
                    "night_target_at": datetime.fromtimestamp(target, tz=now.tzinfo).isoformat(),
                    "night_attempted": False,
                })
                _write_state_unlocked(state, path)
                return None
            if meta.get("night_attempted"):
                if changed:
                    _write_state_unlocked(state, path)
                return None
            try:
                target = datetime.fromisoformat(str(meta.get("night_target_at")))
            except (TypeError, ValueError) as exc:
                raise GardenError("小院子的夜间随机时刻无法识别") from exc
            if now < target:
                if changed:
                    _write_state_unlocked(state, path)
                return None
            meta["night_attempted"] = True
            changed = True

        if not heartbeat:
            last_visible = meta.get("last_visible_event_at")
            if last_visible and _hours_since(last_visible, now) < VISIBLE_EVENT_COOLDOWN_HOURS:
                if changed:
                    _write_state_unlocked(state, path)
                return None

            # 三枚陌生鸡蛋会在一次普通随机事件里由活跃动物送来。它不抢作物/
            # 节气等可靠 pending，也不在深夜缓存心跳偷跑；院里暂时没有动物时继续等。
            if _queue_coop_egg_offer(state, now, rng):
                pending = _claim_pending_event(state, now)
                if pending is None:  # 新入队事件没有旧租约，理论上不可能拿不到。
                    raise GardenError("小院子鸡蛋事件未能取得投递凭证")
                meta["last_visible_event_at"] = now.isoformat()
                _write_state_unlocked(state, path)
                return {"action": "pending", "event": pending}

        roll = rng.random()
        event_kind = "none"
        for candidate, upper_bound in (NIGHT_EVENT_ROLLS if heartbeat else DAY_EVENT_ROLLS):
            if roll < upper_bound:
                event_kind = candidate
                break
        if heartbeat and event_kind == "none":
            # 夜间这一次骰子是整晚唯一保底，不允许"无事"：骰不中具体事件时
            # 默认尝试生成新生命，新生命本身条件不满足会在下面自然降级成痕迹。
            event_kind = "spawn"

        active = [
            entry for entry in entries
            if isinstance(entry, dict) and entry.get("status") == "active"
        ]
        active_animals = [entry for entry in active if entry.get("kind") == "animal"]
        historic_left = [
            entry for entry in entries
            if isinstance(entry, dict) and entry.get("status") == "left"
        ]
        away = [
            entry for entry in entries
            if isinstance(entry, dict) and entry.get("kind") == "animal" and entry.get("status") == "away"
        ]
        eligible_away = sorted(
            (
                entry for entry in away
                if _hours_since(
                    str(entry.get("next_natural_visit_after") or now.isoformat()), now,
                ) >= 0
            ),
            key=lambda entry: str(entry.get("next_natural_visit_after") or ""),
        )
        last_new = meta.get("last_new_life_at")
        new_life_available = (
            not last_new or _hours_since(last_new, now) >= NEW_LIFE_COOLDOWN_HOURS
        )
        # next_natural_visit_after 是承诺的回归时点，不应只是进入一个 3% 的
        # 随机池；但也不能吞掉原本的新生命骰面，否则只要有动物在外，遇见
        # 新动物的机会就会长期趋近于零。新生命命中且冷却结束时保留原判，
        # 其余骰面才由到期动物优先回归。
        if (
            eligible_away
            and len(active_animals) < MAX_ACTIVE
            and not (event_kind == "spawn" and new_life_available)
        ):
            event_kind = "return"
        event = None

        if event_kind == "revisit" and active:
            cared = [entry for entry in active if int(entry.get("care_count", 0)) > 0]
            entry = _weighted_revisit_choice(cared or active, rng)
            event = {"action": "revisit", "entry": entry, "stage": compute_stage(entry, now)}
        elif event_kind == "trace":
            event = {
                "action": "trace",
                "text": _weather_trace_text(active_animals, context, weather_tags, observations, rng),
            }
        elif event_kind == "return" and (historic_left or eligible_away) and len(active_animals) < MAX_ACTIVE:
            if eligible_away:
                entry = eligible_away[0]
            else:
                cared = [entry for entry in historic_left if int(entry.get("care_count", 0)) > 0]
                entry = _weighted_revisit_choice(cared or historic_left, rng) if historic_left else None
            if entry is None:
                event = None
            else:
                was_away = entry.get("status") == "away"
                entry.update({
                    "status": "active",
                    "spot": entry.get("home_spot") if entry.get("residency") == "resident" else rng.choice(SPOTS),
                    "arrived_at": now.isoformat(),
                    "last_cared_at": now.isoformat(),
                    "last_visited_at": now.isoformat(),
                    "next_natural_visit_after": _next_visit_after(
                        now, NATURAL_VISIT_MIN_HOURS, NATURAL_VISIT_MAX_HOURS,
                    ),
                    "left_at": None,
                    "departure_note": None,
                    "return_count": int(entry.get("return_count", 0)) + 1,
                })
                if was_away:
                    entry.pop("away_at", None)
                event = {"action": "return", "entry": entry}
                changed = True
        elif event_kind == "spawn" and len(active_animals) < MAX_ACTIVE:
            if new_life_available:
                kind = "flower" if LEGACY_FLOWER_SPAWN_ENABLED and rng.random() < 0.55 else "animal"
                event = {"action": "spawn", "kind": kind}

        if heartbeat and event is None:
            # 保底：夜间唯一一次骰子挑中的具体事件（重逢/回归/新生命）因条件
            # 不满足而落空时，降级成不挑条件的痕迹，保证整晚必定出现一次。
            event = {
                "action": "trace",
                "text": _weather_trace_text(active_animals, context, weather_tags, observations, rng),
            }

        if event is not None:
            meta["last_visible_event_at"] = now.isoformat()
            changed = True
        if changed:
            _write_state_unlocked(state, path)
        return event


def _pick_animal_category(rng) -> str:
    roll = rng.random()
    cumulative = 0.0
    for category, weight in garden_content.CATEGORY_WEIGHT.items():
        cumulative += weight
        if roll < cumulative:
            return category
    return next(reversed(garden_content.CATEGORY_WEIGHT))


def _describe_spawn(
    kind: str,
    rng,
    *,
    category: str = "",
    personality: str = "",
    context,
    weather_tags: frozenset[str],
    animal_weather_mode: str = "normal",
) -> tuple[str, str]:
    try:
        result = garden_generator.generate_encounter(
            kind,
            category=category,
            personality=personality,
            season=context.season,
            day_period=context.day_period,
            weather_tags=weather_tags,
        )
        if not garden_content.scene_text_compatible(
            result["text"],
            season=context.season,
            day_period=context.day_period,
            weather_tags=weather_tags,
        ):
            raise GardenGeneratorError("小院子写手返回了与当前环境冲突的场景")
        return result["species"], result["text"]
    except GardenGeneratorError:
        if kind == "animal" and category:
            species_pool = garden_content.ANIMAL_SPECIES_BY_CATEGORY.get(category) or ANIMAL_SPECIES
            text_pool = (
                garden_content.ANIMAL_ENCOUNTER_BY_PERSONALITY.get(category, {}).get(personality)
                or _FALLBACK_ENCOUNTER["animal"]
            )
            text = _compatible_choice(text_pool, context, weather_tags, rng)
            flavor = _animal_weather_flavor(category, animal_weather_mode, rng)
            return rng.choice(species_pool), f"{text} {flavor}" if flavor else text
        pool = FLOWER_SPECIES if kind == "flower" else ANIMAL_SPECIES
        return rng.choice(pool), _compatible_choice(
            _FALLBACK_ENCOUNTER[kind], context, weather_tags, rng,
        )


def describe_care(entry: dict, action: str, revived: bool, *, weather_mode: str = "normal") -> str:
    category = str(entry.get("category") or "")
    personality = str(entry.get("personality") or "")

    def _with_flavor(text: str) -> str:
        if entry["kind"] != "animal":
            return text
        flavor = _animal_weather_flavor(category, weather_mode, random, action=action)
        return f"{text} {flavor}" if flavor else text

    if entry["kind"] == "animal" and action == "陪玩":
        pool = garden_content.ANIMAL_PLAY_REACTION_BY_PERSONALITY.get(category, {}).get(personality)
        return _with_flavor(random.choice(pool or garden_content.FALLBACK_PLAY_REACTION))
    # 日常投喂/摸摸始终走既有“类别 × 性格”的丰富反应池；亲密等级只解锁
    # 陪玩和偶发回访场景，不能让不同动物长期反复说同一句等级台词。
    try:
        return garden_generator.generate_reaction(
            entry["kind"],
            entry["species"],
            action,
            revived,
            trait=str(entry.get("trait") or ""),
            category=category,
            personality=personality,
        )
    except GardenGeneratorError:
        if entry["kind"] == "animal" and category and personality:
            pool_root = (
                garden_content.ANIMAL_REVIVED_REACTION_BY_PERSONALITY
                if revived
                else garden_content.ANIMAL_REACTION_BY_PERSONALITY
            )
            pool = pool_root.get(category, {}).get(personality, {}).get(action)
            if pool:
                return _with_flavor(random.choice(pool))
        pool = _FALLBACK_REVIVED_REACTION[action] if revived else _FALLBACK_REACTION[action]
        return _with_flavor(random.choice(pool))


def _describe_return(
    entry: dict,
    rng,
    context,
    weather_tags: frozenset[str],
    animal_weather_mode: str = "normal",
) -> str:
    return_templates = garden_content.compatible_texts(
        garden_content.RETURN_TEMPLATES[entry["kind"]],
        season=context.season,
        day_period=context.day_period,
        weather_tags=weather_tags,
    )
    if not return_templates:
        raise GardenGeneratorError("小院子重逢文案没有当前环境可用的安全候选")
    text = rng.choice(return_templates).format(
        name=display_name(entry),
        spot=entry["spot"],
    )
    if entry.get("kind") == "animal":
        flavor = _animal_weather_flavor(str(entry.get("category") or ""), animal_weather_mode, rng)
        if flavor:
            text = f"{text} {flavor}"
    return text


def _describe_revisit(
    entry: dict,
    stage: str,
    rng,
    context,
    weather_tags: frozenset[str],
    animal_weather_mode: str = "normal",
) -> str:
    category = str(entry.get("category") or "")
    text: str | None = None
    if entry.get("kind") == "animal":
        level = _bond_level(int(entry.get("bond_points", 0)))
        personality = str(entry.get("personality") or "")
        pool = garden_content.ANIMAL_BOND_REVISIT_BY_LEVEL.get(category, {}).get(personality, {}).get(level)
        if pool:
            compatible = garden_content.compatible_texts(
                pool,
                season=context.season,
                day_period=context.day_period,
                weather_tags=weather_tags,
            )
            if compatible:
                text = rng.choice(compatible).format(name=display_name(entry), spot=entry["spot"])
    if text is None:
        action = str(entry.get("last_action") or "")
        template_key = f"{action}_cat" if action == "投喂" and "猫" in str(entry.get("species")) else action
        template_pool = garden_content.REVISIT_TEMPLATES.get(
            template_key,
            garden_content.REVISIT_TEMPLATES["default"],
        )
        templates = garden_content.compatible_texts(
            template_pool,
            season=context.season,
            day_period=context.day_period,
            weather_tags=weather_tags,
        ) or garden_content.compatible_texts(
            garden_content.REVISIT_TEMPLATES["default"],
            season=context.season,
            day_period=context.day_period,
            weather_tags=weather_tags,
        )
        statuses = garden_content.compatible_texts(
            garden_content.STATUS_TEXT[entry["kind"]][stage],
            season=context.season,
            day_period=context.day_period,
            weather_tags=weather_tags,
        )
        if not templates or not statuses:
            raise GardenGeneratorError("小院子回访文案没有当前环境可用的安全候选")
        status = rng.choice(statuses)
        text = rng.choice(templates).format(
            name=display_name(entry),
            spot=entry["spot"],
            status=status,
        )
    if entry.get("kind") == "animal":
        flavor = _animal_weather_flavor(category, animal_weather_mode, rng)
        if flavor:
            text = f"{text} {flavor}"
    return text


def run_tick(
    now: datetime,
    *,
    heartbeat: bool = False,
    weather: dict | None = None,
    rng=None,
    path: Path | None = None,
) -> dict | None:
    """唯一入口：决定这次 tick 要不要出事件，出的话现编内容并按需持久化。"""
    rng = rng or random
    context = calendar_context(now)
    weather_tags = weather_context_tags(weather, context.now)
    observations = _environment_observations(context.now)
    animal_mode = _animal_weather_mode(observations, context.now)
    event = build_event(
        context.now,
        heartbeat=heartbeat,
        weather_tags=weather_tags,
        observations=observations,
        rng=rng,
        path=path,
    )
    if event is None:
        return None
    if event["action"] == "pending":
        pending = event["event"]
        try:
            if pending.get("type") == "bond_milestone":
                level = int(pending["level"])
                personality = str(pending.get("personality") or "")
                text = rng.choice(
                    garden_content.BOND_MILESTONE_TEXT.get(personality, {}).get(level)
                    or garden_content.BOND_MILESTONE_FALLBACK[level]
                ).format(name=pending.get("animal_name") or "这位小访客")
                return _with_calendar_context({
                    "type": "bond_milestone", "event_id": pending["event_id"],
                    "delivery_token": pending["delivery_token"],
                    "id": pending["animal_id"], "level": level,
                    "level_name": bond_level_name(level), "text": text,
                }, now)
            if pending.get("type") == "crop_stage":
                crop_id = pending["crop_id"]
                stage = pending["stage"]
                variety_pool = garden_content.CROP_VARIETY_TEXT.get(crop_id, {}).get(stage)
                stage_text = (
                    rng.choice(variety_pool)
                    if variety_pool
                    else garden_content.choose_scene(
                        garden_content.CROP_STAGE_SCENES[stage],
                        season=context.season,
                        day_period=context.day_period,
                        weather_tags=weather_tags,
                        rng=rng,
                    )
                )
                return _with_calendar_context({
                    "type": "crop_stage", "event_id": pending["event_id"],
                    "delivery_token": pending["delivery_token"],
                    "plot_id": pending["plot_id"], "crop_id": crop_id, "stage": stage,
                    "text": stage_text.format(name=garden_crops.crop_name(crop_id)),
                }, now)
            if pending.get("type") == "crop_condition":
                saved_copy = pending.get("copy")
                if isinstance(saved_copy, dict):
                    text = saved_copy["text"]
                else:
                    facts = _condition_event_facts(pending, context.now, weather_tags)
                    body, source = _generate_crop_copy_body(
                        facts,
                        _condition_event_fallback_body(pending),
                        now=context.now,
                        weather_tags=weather_tags,
                    )
                    generated_text = f"{body}\n{_condition_event_result_line(pending)}"
                    saved_copy = _persist_condition_event_copy(
                        pending["event_id"],
                        pending["delivery_token"],
                        text=generated_text,
                        source=source,
                        path=path,
                    )
                    if saved_copy is None:
                        return None
                    text = saved_copy["text"]
                return _with_calendar_context({
                    "type": "crop_condition",
                    "event_id": pending["event_id"],
                    "delivery_token": pending["delivery_token"],
                    "condition_id": pending["condition_id"],
                    "plot_id": pending["plot_id"],
                    "crop_id": pending["crop_id"],
                    "condition_type": pending["condition_type"],
                    "severity": pending["severity"],
                    "yield_penalty": pending["yield_penalty"],
                    "text": text,
                }, now)
            if pending.get("type") == "calendar_moment":
                return {
                    "type": "calendar_moment", "event_id": pending["event_id"],
                    "delivery_token": pending["delivery_token"],
                    "calendar_kind": pending["calendar_kind"], "calendar_id": pending["calendar_id"],
                    "text": garden_content.choose_scene(
                        garden_content.CALENDAR_MOMENT_SCENES[pending["calendar_kind"]],
                        season=context.season,
                        day_period=context.day_period,
                        weather_tags=weather_tags,
                        rng=rng,
                        calendar_id=pending["calendar_id"],
                    ).format(name=pending["name"]),
                    "season": pending["season"], "term_name": pending["name"] if pending["calendar_kind"] == "term" else context.term_name,
                    "festival_name": pending["name"] if pending["calendar_kind"] == "festival" else context.festival_name,
                    "day_period": context.day_period,
                }
            if pending.get("type") == "yard_water":
                return _with_calendar_context({
                    "type": "yard_water",
                    "event_id": pending["event_id"],
                    "delivery_token": pending["delivery_token"],
                    "transition": pending["transition"],
                    "level": pending["level"],
                    "text": pending["text"],
                }, now)
            if pending.get("type") == "flood_damage":
                return _with_calendar_context({
                    "type": "flood_damage",
                    "event_id": pending["event_id"],
                    "delivery_token": pending["delivery_token"],
                    "stage": pending["stage"],
                    "text": pending["text"],
                }, now)
            if pending.get("type") == "compost_ready":
                return _with_calendar_context({
                    "type": "compost_ready",
                    "event_id": pending["event_id"],
                    "delivery_token": pending["delivery_token"],
                    "units": pending["units"],
                    "text": pending["text"],
                }, now)
            if pending.get("type") in {"coop_egg_offer", "coop_incubation", "coop_life"}:
                payload = {
                    "type": pending["type"],
                    "event_id": pending["event_id"],
                    "delivery_token": pending["delivery_token"],
                    "text": pending["text"],
                }
                for key in (
                    "source_animal_id", "source_animal_name", "egg_count", "clutch_id", "stage",
                    "grown_hens", "grown_roosters", "eggs_laid", "regular_eggs_laid",
                    "hatchable_eggs_laid", "hens_laid", "roosters_crowed",
                ):
                    if key in pending:
                        payload[key] = pending[key]
                return _with_calendar_context(payload, now)
            return None
        except Exception:
            release_pending_event(pending["event_id"], pending["delivery_token"], path=path)
            raise
    if event["action"] == "spawn":
        kind = event["kind"]
        category = _pick_animal_category(rng) if kind == "animal" else None
        personality = rng.choice(garden_content.PERSONALITIES) if kind == "animal" else None
        species, text = _describe_spawn(
            kind,
            rng,
            category=category or "",
            personality=personality or "",
            context=context,
            weather_tags=weather_tags,
            animal_weather_mode=animal_mode,
        )
        try:
            entry = spawn(
                kind, species=species, intro=text,
                category=category, personality=personality,
                now=now, path=path, rng=rng,
            )
        except GardenError:
            # 生成期间若另一个进程刚好占满院子，这次事件安全作废。
            return None
        result = {
            "type": "spawn", "kind": kind, "species": species, "text": text,
            "id": entry["id"], "spot": entry["spot"], "trait": entry["trait"],
        }
        if kind == "animal":
            result["category"] = category
            result["personality"] = personality
        return _with_calendar_context(result, now)
    if event["action"] == "trace":
        return _with_calendar_context({"type": "trace", "text": event["text"]}, now)
    entry = event["entry"]
    if event["action"] == "return":
        text = _describe_return(entry, rng, context, weather_tags, animal_mode)
        event_type = "return"
        stage = "fresh"
    else:
        stage = event["stage"]
        text = _describe_revisit(entry, stage, rng, context, weather_tags, animal_mode)
        event_type = "revisit"
    return _with_calendar_context({
        "type": event_type,
        "kind": entry["kind"],
        "species": entry["species"],
        "nickname": entry.get("nickname"),
        "text": text,
        "id": entry["id"],
        "spot": entry["spot"],
        "stage": stage,
    }, now)


def _with_calendar_context(event: dict, now: datetime) -> dict:
    context = calendar_context(now)
    return {
        **event,
        "season": event.get("season") or context.season,
        "term_name": event.get("term_name") or context.term_name,
        "festival_name": event.get("festival_name") or context.festival_name,
        "day_period": event.get("day_period") or context.day_period,
    }


def measure_word(kind: str) -> str:
    return _MEASURE_WORD[kind]


def actions_for(kind: str) -> tuple[str, ...]:
    return _ACTIONS_BY_KIND[kind]


def event_body(event: dict) -> str:
    """事件的自然语言正文，不含发给 agent 的说明包装；供前端卡片/日志复用原文。"""
    if event["type"] == "spawn":
        kind_label = "植物" if event["kind"] == "flower" else "小动物"
        measure = measure_word(event["kind"])
        trait = f"；{event['trait']}" if event.get("trait") else ""
        return (
            f"你溜达到{event['spot']}那儿，发现不知道什么时候多了一{measure}"
            f"{event['species']}（{kind_label}，编号 {event['id']}{trait}）。"
            f"{event['text']}"
        )
    return event["text"]


def format_injection(
    event: dict,
    *,
    session_id: str | None = None,
    intro_path: Path | None = None,
) -> str:
    """用既有协议标签包装场景正文；行为边界统一由 CLAUDE.md 说明。"""
    intro = claim_session_intro(session_id, path=intro_path) if session_id else ""
    prefix = f"{intro}\n\n" if intro else ""
    return f"\n[GARDEN] {prefix}{event_body(event)}\n"
