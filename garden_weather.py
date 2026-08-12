"""小院子现实天气的纯事实规则。

这一模块不联网、不读取或写入院子状态。天气抓取和跨进程缓存由
``weather.py`` 负责；这里仅把允许进入缓存的观测收敛为可审计的最小
结构，供后续环境结算复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from math import isfinite
import re
from typing import Any
from zoneinfo import ZoneInfo


OBSERVATION_SCHEMA_VERSION = 1
OBSERVATION_PROVIDER = "qweather"
MAX_FUTURE_SKEW = timedelta(minutes=15)
ALLOWED_TAGS = frozenset({
    "clear", "cloudy", "rain", "snow", "fog", "frost", "storm",
    "hot", "cold", "humid", "windy",
})
FRESH_OBSERVATION_AGE = timedelta(hours=2)
STALE_OBSERVATION_AGE = timedelta(hours=6)
TZ = ZoneInfo("Asia/Shanghai")
# 一条观测最迟可以在"过去两小时内"仍被当作晚到实况补账；它的雨窗本身还要
# 再向前推一小时。因此严格早于 现在-3小时 的时间点，不可能再被任何未来才
# 抵达的观测修改，可以安全永久结算，不必等待更晚的到达顺序。
REPLAY_HORIZON = FRESH_OBSERVATION_AGE + timedelta(hours=1)
EXPOSURE_KEYS = (
    "known_hours", "dry_hours", "dryish_hours", "adequate_hours",
    "moist_hours", "saturated_hours", "hot_hours", "cold_hours", "rain_mm",
)
MOISTURE_BANDS = (
    (0.0, 25.0, "dry"),
    (25.0, 50.0, "dryish"),
    (50.0, 70.0, "adequate"),
    (70.0, 85.0, "moist"),
    (85.0, 100.0, "saturated"),
)

# 阶段 C：作物环境倾向与自然异常的天气权重。倍率和权重都刻意保守——现实
# 天气只应该让院子“有感”，不应该变成每日值班。
HEAT_TENDENCIES = frozenset({"warm_loving", "cool_loving", "neutral"})
GROWTH_MULTIPLIER_MIN = 0.65
GROWTH_MULTIPLIER_MAX = 1.10
MIN_KNOWN_HOURS_FOR_MULTIPLIER = 6.0
_MOISTURE_BAND_HOUR_KEYS = ("dry_hours", "dryish_hours", "adequate_hours", "moist_hours", "saturated_hours")

# 第六版阶段 A：雨强、出门天气与院子级积水都是读时派生的纯事实。
RAIN_INTENSITIES = ("drizzle", "light", "moderate", "heavy", "rainstorm")
_RAIN_INTENSITY_RANK = {name: rank for rank, name in enumerate(RAIN_INTENSITIES)}
RAIN_LEDGER_DAYS = 10
RAIN_STREAK_THRESHOLD_MM = 2.0
YARD_WATER_WINDOW = timedelta(hours=48)
YARD_DRAIN_RATE_MM_H = 3.0
YARD_SATURATED_DRAIN_RATE_MM_H = 1.0
YARD_WATER_MAX = 300.0
YARD_PUDDLES_THRESHOLD = 25.0
YARD_FLOODED_THRESHOLD = 60.0

# 第七版阶段 A：排水动作加速退水、内涝浸泡惩罚阈值。均是纯数值，只在
# 调用方显式传入排水时刻/浸泡时长时才参与计算，不改变原有默认行为。
YARD_DRAIN_BOOST_WINDOW = timedelta(hours=24)  # 排水后加速退水维持的时长
YARD_DRAIN_BOOST_RATE_MM_H = 3.0  # 排水期间叠加在当前档退水速率上的增量
FLOOD_SOAK_QUALITY_AFTER = timedelta(hours=24)  # 浸泡满这么久：整批作物打欠佳标记
FLOOD_SOAK_ROT_AFTER = timedelta(hours=72)  # 浸泡满这么久：整批作物当场泡烂


def moisture_band(value: float) -> str:
    """返回唯一的、供规则和表现层共用的土壤水分档位。"""
    moisture = max(0.0, min(100.0, float(value)))
    for lower, upper, name in MOISTURE_BANDS:
        if lower <= moisture < upper or (name == "saturated" and moisture <= upper):
            return name
    return "saturated"


def moisture_label(value: float) -> str:
    return {
        "dry": "干燥", "dryish": "偏干", "adequate": "合适",
        "moist": "湿润", "saturated": "湿透",
    }[moisture_band(value)]


def fresh_at(observation: dict[str, Any], moment: datetime) -> bool:
    """实况只向后覆盖两小时，绝不由预报或旧实况倒灌。"""
    observed_at = parse_timestamp(observation.get("observed_at"))
    return bool(
        observed_at is not None
        and moment.tzinfo is not None
        and observed_at <= moment <= observed_at + FRESH_OBSERVATION_AGE
    )


def environment_status(observations: list[dict[str, Any]], now: datetime) -> str:
    """最近一次真实观测的审计状态；不把 stale 当成可结算天气。"""
    valid = [item for item in observations if parse_timestamp(item.get("observed_at"))]
    if not valid or now.tzinfo is None:
        return "missing"
    latest = max(valid, key=observation_sort_key)
    observed_at = parse_timestamp(latest["observed_at"])
    assert observed_at is not None
    if observed_at <= now <= observed_at + FRESH_OBSERVATION_AGE:
        return "fresh"
    if observed_at <= now <= observed_at + STALE_OBSERVATION_AGE:
        return "stale"
    return "missing"


def evaporation_rate(observation: dict[str, Any] | None, moment: datetime) -> float:
    """第五版首轮的游戏蒸发标定；缺失天气走保守中性速率。"""
    daytime = 8 <= moment.hour < 20
    if observation is None:
        return 0.20 if daytime else 0.10
    rate = 1.0 if daytime else 0.35
    temp = observation.get("feels_like_c")
    if temp is None:
        temp = observation.get("temp_c")
    try:
        temperature = float(temp) if temp is not None else None
    except (TypeError, ValueError):
        temperature = None
    if temperature is not None and temperature >= 30:
        rate += 1.5
    if temperature is not None and temperature >= 35:
        rate += 0.8
    tags = set(observation.get("tags") or [])
    if daytime and "clear" in tags:
        rate += 0.5
    if "windy" in tags:
        rate += 0.5
    humidity = observation.get("humidity_pct")
    try:
        humidity_value = float(humidity) if humidity is not None else None
    except (TypeError, ValueError):
        humidity_value = None
    if humidity_value is not None and humidity_value < 40:
        rate += 0.5
    if humidity_value is not None and humidity_value >= 85:
        rate -= 0.4
    return max(0.2, min(4.0, rate))


def rain_gain(observation: dict[str, Any] | None, hours: float) -> tuple[float, float]:
    """按已发生的实况降水积分，返回（水分增量、实际毫米数）。"""
    if observation is None or hours <= 0:
        return 0.0, 0.0
    rate = observation.get("precip_rate_mm_h")
    try:
        mm_per_hour = float(rate) if rate is not None else 0.0
    except (TypeError, ValueError):
        mm_per_hour = 0.0
    if mm_per_hour <= 0 and "rain" in set(observation.get("tags") or []):
        # 文字雨但数值为 0 只按飘雨处理，不能把一场未知雨量当成浇透。
        mm_per_hour = 0.25
    rain_mm = max(0.0, mm_per_hour * hours)
    return min(35.0 * hours, rain_mm * 8.0), rain_mm


def rain_intensity(observation: dict[str, Any] | None) -> str | None:
    """从已校验实况派生唯一雨强；数值路和文字路始终取较强档。"""
    if observation is None:
        return None
    candidates: list[str] = []
    precipitation = observation.get("precip_rate_mm_h")
    try:
        rate = float(precipitation) if precipitation is not None else 0.0
    except (TypeError, ValueError):
        rate = 0.0
    if rate > 0:
        if rate < 0.5:
            candidates.append("drizzle")
        elif rate < 2.5:
            candidates.append("light")
        elif rate < 8.0:
            candidates.append("moderate")
        elif rate < 16.0:
            candidates.append("heavy")
        else:
            candidates.append("rainstorm")

    text = str(observation.get("condition_text") or "")
    text_candidates = []
    if any(word in text for word in ("暴雨", "大暴雨", "特大暴雨")):
        text_candidates.append("rainstorm")
    if "大雨" in text:
        text_candidates.append("heavy")
    if "中雨" in text:
        text_candidates.append("moderate")
    if any(word in text for word in ("小雨", "阵雨")):
        text_candidates.append("light")
    if any(word in text for word in ("毛毛雨", "细雨")):
        text_candidates.append("drizzle")
    candidates.extend(text_candidates)
    if not candidates and "rain" in set(observation.get("tags") or []):
        candidates.append("light")
    if not candidates and "雨" in text:
        candidates.append("light")
    return max(candidates, key=_RAIN_INTENSITY_RANK.__getitem__) if candidates else None


def outing_mode(observation: dict[str, Any] | None) -> str:
    """给一次真实走进院子的动作派生唯一出门天气档位。"""
    if observation is None:
        return "calm"
    intensity = rain_intensity(observation)
    tags = set(observation.get("tags") or [])
    text = str(observation.get("condition_text") or "")
    raw_wind = observation.get("wind_scale_max")
    try:
        wind = int(raw_wind) if raw_wind is not None else None
    except (TypeError, ValueError):
        wind = None
    has_rain = intensity is not None
    at_least_moderate = (
        intensity is not None
        and _RAIN_INTENSITY_RANK[intensity] >= _RAIN_INTENSITY_RANK["moderate"]
    )
    if wind is not None and wind >= 8 and (at_least_moderate or "台风" in text):
        return "typhoon"
    if intensity in ("heavy", "rainstorm") or ("storm" in tags and has_rain):
        return "storm_rain"
    if wind is not None and wind >= 6 and has_rain:
        return "windy_rain"
    if intensity == "moderate":
        return "rain"
    if intensity in ("drizzle", "light"):
        return "light_rain"
    if "snow" in tags:
        return "snow"
    if wind is not None and wind >= 6:
        return "gale"
    return "calm"


def _empty_exposure_bucket() -> dict[str, float]:
    return {key: 0.0 for key in EXPOSURE_KEYS}


def rain_owner(observations: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any] | None:
    """段 (start, end] 的雨量权威观测：谁的过去一小时窗口完整覆盖该段，
    且观测时间最新，谁就代表这段真实发生的降水；不满足完整覆盖的观测不参与，
    避免把两份滑动窗口的重叠部分重复计入。与调用方传入观测的顺序无关。"""
    candidates = []
    for item in observations:
        observed_at = parse_timestamp(item.get("observed_at"))
        if observed_at is None:
            continue
        if observed_at - timedelta(hours=1) <= start and end <= observed_at:
            candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=observation_sort_key)


def replay_boundaries(start: datetime, end: datetime, observations: list[dict[str, Any]]) -> list[datetime]:
    """把 (start, end] 切成互不重叠的确定性小段：切点只取决于观测本身的时间
    （及其雨窗起点、新鲜度到期）与北京时间的 8/20 点、跨日午夜——不取决于
    观测抵达调用方的顺序或批次。"""
    if end <= start:
        return [start, end]
    boundaries = {start, end}
    for item in observations:
        observed_at = parse_timestamp(item.get("observed_at"))
        if observed_at is None:
            continue
        if start < observed_at < end:
            boundaries.add(observed_at)
        window_start = observed_at - timedelta(hours=1)
        if start < window_start < end:
            boundaries.add(window_start)
        expiry = observed_at + FRESH_OBSERVATION_AGE
        if start < expiry < end:
            boundaries.add(expiry)
    day_cursor = start.astimezone(TZ).date()
    end_date = end.astimezone(TZ).date()
    while day_cursor <= end_date:
        for hour in (0, 8, 20):
            boundary = datetime.combine(day_cursor, dtime(hour), tzinfo=TZ)
            if start < boundary < end:
                boundaries.add(boundary)
        day_cursor += timedelta(days=1)
    return sorted(boundaries)


def observed_rain_by_date(
    observations: list[dict[str, Any]], now: datetime,
) -> dict[str, float]:
    """按 ``rain_owner`` 的去重口径汇总当前观测窗口里的北京时间日雨量。"""
    if now.tzinfo is None:
        return {}
    valid = []
    for item in observations:
        observed_at = parse_timestamp(item.get("observed_at"))
        if observed_at is not None and observed_at <= now:
            valid.append(item)
    if not valid:
        return {}
    earliest = min(parse_timestamp(item["observed_at"]) for item in valid)
    assert earliest is not None
    start = max(now - timedelta(days=RAIN_LEDGER_DAYS), earliest - timedelta(hours=1))
    totals: dict[str, float] = {}
    boundaries = replay_boundaries(start, now, valid)
    for seg_start, seg_end in zip(boundaries, boundaries[1:]):
        hours = (seg_end - seg_start).total_seconds() / 3600.0
        if hours <= 0:
            continue
        owner = rain_owner(valid, seg_start, seg_end)
        _, rain_mm = rain_gain(owner, hours)
        if rain_mm <= 0:
            continue
        day_key = seg_start.astimezone(TZ).date().isoformat()
        totals[day_key] = totals.get(day_key, 0.0) + rain_mm
    return {key: round(value, 4) for key, value in sorted(totals.items())}


def _valid_rain_amount(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if isfinite(amount) and amount >= 0 else None


def merge_rain_by_date(
    stored: object, observed: object, now: datetime,
) -> dict[str, float]:
    """逐日取最大合并日账并只保留最近十个北京时间自然日。"""
    if now.tzinfo is None:
        return {}
    today = now.astimezone(TZ).date()
    cutoff = today - timedelta(days=RAIN_LEDGER_DAYS - 1)
    merged: dict[str, float] = {}
    for source in (stored, observed):
        if not isinstance(source, dict):
            continue
        for raw_day, raw_amount in source.items():
            try:
                day = date.fromisoformat(str(raw_day))
            except ValueError:
                continue
            amount = _valid_rain_amount(raw_amount)
            if amount is None or not cutoff <= day <= today:
                continue
            key = day.isoformat()
            merged[key] = max(merged.get(key, 0.0), amount)
    return {key: round(value, 4) for key, value in sorted(merged.items())}


def _rain_amount_for_date(rain_by_date: object, day: date) -> float:
    if not isinstance(rain_by_date, dict):
        return 0.0
    amount = _valid_rain_amount(rain_by_date.get(day.isoformat()))
    return amount if amount is not None else 0.0


def _rain_streak_days_for_date(rain_by_date: object, day: date) -> int:
    anchor = day
    if _rain_amount_for_date(rain_by_date, anchor) < RAIN_STREAK_THRESHOLD_MM:
        anchor -= timedelta(days=1)
    streak = 0
    while _rain_amount_for_date(rain_by_date, anchor) >= RAIN_STREAK_THRESHOLD_MM:
        streak += 1
        anchor -= timedelta(days=1)
    return streak


def rain_streak_days(rain_by_date: object, now: datetime) -> int:
    """从日账派生截至今天（今天未下则截至昨天）的连续有雨天数。"""
    if now.tzinfo is None:
        return 0
    return _rain_streak_days_for_date(rain_by_date, now.astimezone(TZ).date())


def yard_water_index(
    observations: list[dict[str, Any]], rain_by_date: object, now: datetime,
    *, drained_at: datetime | None = None,
) -> float:
    """从最近 48 小时实况重放院子低处积水的毫米当量。

    ``drained_at`` 是可选的最近一次排水动作时刻：排水后
    :data:`YARD_DRAIN_BOOST_WINDOW` 内，退水速率在当前档基础上叠加
    :data:`YARD_DRAIN_BOOST_RATE_MM_H`，窗口一过自动回落，指数本身仍是
    纯函数派生、不落盘。缺省 ``None`` 时与不带这个参数逐字节一致。
    """
    if now.tzinfo is None:
        return 0.0
    start = now - YARD_WATER_WINDOW
    valid = []
    for item in observations:
        observed_at = parse_timestamp(item.get("observed_at"))
        if observed_at is not None and start <= observed_at <= now:
            valid.append(item)
    boundaries = replay_boundaries(start, now, valid)
    if drained_at is not None and drained_at.tzinfo is not None:
        # 加速退水只在窗口内成立；把窗口的起止两端也当切点插入，这样每一段
        # 要么完全在加速期内、要么完全在外，不需要按比例分摊单段内的速率。
        boost_end = drained_at + YARD_DRAIN_BOOST_WINDOW
        extra = {
            point for point in (drained_at, boost_end)
            if start < point < now
        }
        if extra:
            boundaries = sorted(set(boundaries) | extra)
    current = 0.0
    for seg_start, seg_end in zip(boundaries, boundaries[1:]):
        hours = (seg_end - seg_start).total_seconds() / 3600.0
        if hours <= 0:
            continue
        owner = rain_owner(valid, seg_start, seg_end)
        _, rain_mm = rain_gain(owner, hours)
        local_day = seg_start.astimezone(TZ).date()
        streak = _rain_streak_days_for_date(rain_by_date, local_day)
        drain_rate = (
            YARD_SATURATED_DRAIN_RATE_MM_H
            if streak >= 2 else YARD_DRAIN_RATE_MM_H
        )
        if (
            drained_at is not None and drained_at.tzinfo is not None
            and drained_at <= seg_start < drained_at + YARD_DRAIN_BOOST_WINDOW
        ):
            drain_rate += YARD_DRAIN_BOOST_RATE_MM_H
        current = max(
            0.0,
            min(YARD_WATER_MAX, current + rain_mm - drain_rate * hours),
        )
    return round(current, 4)


def yard_water_level(index: float) -> str:
    """把内部积水指数收敛为表现层唯一允许使用的三档事实。"""
    if index >= YARD_FLOODED_THRESHOLD:
        return "flooded"
    if index >= YARD_PUDDLES_THRESHOLD:
        return "puddles"
    return "none"


def flood_soak_hours(
    since: datetime | None, drained_at: datetime | None, now: datetime,
) -> float:
    """从内涝时钟起点推算作物泡在水里的时长（小时，浮点）。

    排水会把浸泡时钟拨回零（起算点前移到 ``drained_at``），但不清除
    ``since`` 本身——内涝这个事实还在，只是这一次浸泡账重新开始算。
    ``drained_at`` 早于 ``since`` 或缺失时，浸泡仍从 ``since`` 起算；
    ``since`` 缺失代表当前没有内涝时钟，浸泡时长恒为 0。
    """
    if since is None or since.tzinfo is None or now.tzinfo is None:
        return 0.0
    effective_start = since
    if drained_at is not None and drained_at.tzinfo is not None and drained_at > since:
        effective_start = drained_at
    return max(0.0, (now - effective_start).total_seconds() / 3600.0)


def exposure_delta(
    start_moisture: float, end_moisture: float, hours: float, *,
    known: bool, observation: dict[str, Any] | None, rain_mm: float,
) -> dict[str, float]:
    """一段时间内的暴露增量，供永久结算或本次实时重算叠加，不直接改状态。"""
    bucket = _empty_exposure_bucket()
    delta = end_moisture - start_moisture
    cuts = {0.0, 1.0}
    if delta:
        for threshold in (25.0, 50.0, 70.0, 85.0):
            point = (threshold - start_moisture) / delta
            if 0.0 < point < 1.0:
                cuts.add(point)
    ordered = sorted(cuts)
    for left, right in zip(ordered, ordered[1:]):
        portion = hours * (right - left)
        moisture = start_moisture + delta * ((left + right) / 2)
        bucket[f"{moisture_band(moisture)}_hours"] += portion
    if known:
        bucket["known_hours"] += hours
        tags = set((observation or {}).get("tags") or [])
        if "hot" in tags:
            bucket["hot_hours"] += hours
        if "cold" in tags:
            bucket["cold_hours"] += hours
    bucket["rain_mm"] += rain_mm
    return bucket


def daily_growth_multiplier(bucket: dict[str, float] | None, *, heat_tendency: str) -> float:
    """一个已完整结束的北京时间日期的生长倍率，纯函数、只读已结算暴露。

    数据不足（天气缺失、这天有效暴露不足 6 小时）一律中性 1.0，绝不凭空
    制造加速或减速；高温只在 warm_loving 且水分同时合适时给小幅加成，不是
    只要热就加速；偏干、湿透只减慢，倍率下限保证生长增量永不为负。
    """
    if heat_tendency not in HEAT_TENDENCIES:
        heat_tendency = "neutral"
    if not bucket:
        return 1.0
    known_hours = float(bucket.get("known_hours", 0.0))
    if known_hours < MIN_KNOWN_HOURS_FOR_MULTIPLIER:
        return 1.0
    day_hours = sum(float(bucket.get(key, 0.0)) for key in _MOISTURE_BAND_HOUR_KEYS)
    if day_hours <= 0:
        return 1.0
    dry_frac = float(bucket.get("dry_hours", 0.0)) / day_hours
    dryish_frac = float(bucket.get("dryish_hours", 0.0)) / day_hours
    adequate_frac = float(bucket.get("adequate_hours", 0.0)) / day_hours
    moist_frac = float(bucket.get("moist_hours", 0.0)) / day_hours
    saturated_frac = float(bucket.get("saturated_hours", 0.0)) / day_hours
    hot_frac = float(bucket.get("hot_hours", 0.0)) / day_hours
    moisture_penalty = 0.30 * dry_frac + 0.15 * dryish_frac + 0.10 * saturated_frac
    moisture_ok = (adequate_frac + moist_frac) >= 0.5
    if heat_tendency == "warm_loving":
        heat_adjustment = 0.10 * hot_frac if moisture_ok else 0.0
    elif heat_tendency == "cool_loving":
        heat_adjustment = -0.15 * hot_frac
    else:
        heat_adjustment = -0.05 * hot_frac
    multiplier = 1.0 - moisture_penalty + heat_adjustment
    return max(GROWTH_MULTIPLIER_MIN, min(GROWTH_MULTIPLIER_MAX, multiplier))


def condition_type_weights(
    soil: dict[str, Any] | None,
    condition_types: tuple[str, ...],
    *,
    yard_water: str = "none",
) -> dict[str, int]:
    """自然异常命中后的类型权重；只在命中后改变选中哪一种，绝不改变是否
    命中的总概率。没有现实土壤数据（现实天气关闭或还没有该地块）时退回
    完全均匀，与第四版行为一致。干燥或偏干的土不给“积水”任何权重——积水
    是水分持续偏高才可信的类型，不能凭空命中。"""
    weights = {condition_type: 1 for condition_type in condition_types}
    if not isinstance(soil, dict):
        return weights
    try:
        band = moisture_band(float(soil.get("moisture")))
    except (TypeError, ValueError):
        return weights
    if band in ("moist", "saturated"):
        if "waterlogged" in weights:
            weights["waterlogged"] = 3 if band == "saturated" else 2
            if band == "saturated" and yard_water == "flooded":
                weights["waterlogged"] += 1
        if "diseased_leaf" in weights:
            weights["diseased_leaf"] = 2
    else:
        if "waterlogged" in weights:
            weights["waterlogged"] = 0
    if band in ("dry", "dryish"):
        if "pest" in weights:
            weights["pest"] = 2
        if "nutrient_deficiency" in weights:
            weights["nutrient_deficiency"] = 2
    return weights


# 阶段 D：动物对现实天气的行为反应。派生规则只依赖当前新鲜天气标签、时段
# 和“地面是否还带着最近一场雨”这三件已确认事实，不读取任何动物本体状态，
# 也不改变亲密度、常住身份或每日事件频率——纯粹是表现层的输入。
ANIMAL_WEATHER_MODES = ("sheltering", "cooling", "basking", "muddy", "wind_play", "normal")
_DAYLIGHT_PERIODS = frozenset({"dawn", "day", "dusk"})
GROUND_WET_WINDOW = timedelta(hours=3)


def ground_recently_rained(observations: list[dict[str, Any]], now: datetime) -> bool:
    """地面是否仍带着最近一场雨的痕迹：过去 ``GROUND_WET_WINDOW`` 内确实
    下过雨，但此刻已经不在下——这样才是“沾着泥”而不是“正在避雨”。

    “此刻是否在下”只看最新一条观测，不能让一条更早、只是恰好还没过期的
    旧雨观测盖过后来已经放晴的更新观测。
    """
    if now.tzinfo is None:
        return False
    valid = [
        item for item in observations
        if (observed_at := parse_timestamp(item.get("observed_at"))) is not None and observed_at <= now
    ]
    if not valid:
        return False
    latest = max(valid, key=observation_sort_key)
    latest_tags = set(latest.get("tags") or [])
    if fresh_at(latest, now) and ("rain" in latest_tags or "storm" in latest_tags):
        return False
    for item in valid:
        observed_at = parse_timestamp(item["observed_at"])
        tags = set(item.get("tags") or [])
        try:
            precip = float(item.get("precip_rate_mm_h") or 0.0)
        except (TypeError, ValueError):
            precip = 0.0
        if now - GROUND_WET_WINDOW <= observed_at and (precip > 0 or "rain" in tags):
            return True
    return False


# 现有 ``windy`` 标签的门槛（风力 ≥3 级）是给蒸发速率用的、刻意偏低的
# “有风就算”阈值，3 级本身只是轻风，不足以让动物避险。危险强风需要单独
# 一条更高的数值门槛：蒲福风级 6 级“强风”起，大树枝摇动、伞难以撑开，
# 是小动物会主动躲避而不是继续玩耍的量级。这个数值是阶段 D 复审后新加的
# 游戏标定，可随 review 调整，但两档必须用不同阈值区分，不能共用
# ``windy`` 一个标签。
DANGEROUS_WIND_SCALE = 6


def animal_weather_mode(
    weather_tags: object,
    *, day_period: str, wind_scale: int | None = None, ground_wet: bool = False,
) -> str:
    """把已确认天气标签收敛成动物可信的行为模式。

    雨、雷暴、达到 ``DANGEROUS_WIND_SCALE`` 的强风优先判定为避雨——不能
    为了有趣让动物在危险天气里兴奋乱跑；冷晴且在白天才可能晒太阳，夜里
    不能凭“冷+晴”编出晒太阳的画面；风力达到 ``windy`` 门槛但未到危险级别
    时才是“有风但非危险天气”的 ``wind_play``。缺失信号一律退回 ``normal``，
    绝不凭空派生模式。
    """
    tags = frozenset(weather_tags) if weather_tags else frozenset()
    dangerous_wind = wind_scale is not None and wind_scale >= DANGEROUS_WIND_SCALE
    if "storm" in tags or "rain" in tags or dangerous_wind:
        return "sheltering"
    if "hot" in tags:
        return "cooling"
    if "cold" in tags and "clear" in tags and day_period in _DAYLIGHT_PERIODS:
        return "basking"
    if ground_wet:
        return "muddy"
    if "windy" in tags:
        return "wind_play"
    return "normal"


@dataclass(frozen=True)
class ReplayResult:
    moisture: float
    exposure_by_day: dict[str, dict[str, float]] = field(default_factory=dict)
    last_rain_at: datetime | None = None
    # 最近一次“确实跌破 50”的精确时刻，而不是一整天的布尔标记：无必要浇水
    # 计数只应清零跌破时刻之前的旧意图，跌破之后的新意图仍要正常递增，
    # 否则同一天只要跌破过一次，后续每次都会被当成第一次。
    last_dip_below_50_at: datetime | None = None


def replay(moisture: float, start: datetime, end: datetime, observations: list[dict[str, Any]]) -> ReplayResult:
    """从 ``moisture``（``start`` 时刻的水分）纯函数地推演到 ``end``。

    是否分批、按什么顺序把 ``observations`` 交给调用方、调用了几次都不影响
    结果——这里只依赖 (moisture, start, end, 观测集合) 本身，任何重放都得到
    同一结果，天然满足幂等、乱序和跨批次一致。
    """
    if end <= start:
        return ReplayResult(moisture=moisture)
    boundaries = replay_boundaries(start, end, observations)
    exposure_by_day: dict[str, dict[str, float]] = {}
    last_rain_at: datetime | None = None
    last_dip_below_50_at: datetime | None = None
    current = moisture
    for seg_start, seg_end in zip(boundaries, boundaries[1:]):
        hours = (seg_end - seg_start).total_seconds() / 3600
        if hours <= 0:
            continue
        # 新鲜度按本段“结束”而非“开始”核对：expiry 本身是切点，若只查
        # seg_start，恰好从到期那一刻开始的下一整段会因为端点重合被判成
        # 仍新鲜，从而把一条早已过期的观测错误地沿用到这段之后的任意长
        # 时间——这与是否额外插入折叠切点无关，纯粹是判断点选错了。
        usable = [
            item for item in observations
            if (observed := parse_timestamp(item.get("observed_at"))) is not None
            and observed <= seg_start and fresh_at(item, seg_end)
        ]
        usable_observation = max(usable, key=observation_sort_key) if usable else None
        owner = rain_owner(observations, seg_start, seg_end)
        evaporation = evaporation_rate(usable_observation, seg_start) * hours
        # 缺失天气只保护原本合适的土不被凭空推入偏干；已经干燥的土不得因为
        # 这个保护下限反向吸水。
        floor = min(current, 25.0) if usable_observation is None else 0.0
        after_evaporation = max(floor, current - evaporation)
        gain, rain_mm = rain_gain(owner, hours)
        after = min(100.0, after_evaporation + gain)
        # 按段的起点归日，而不是终点：跨午夜时终点恰好是次日 00:00，用它
        # 归日会把这一段（属于前一天的最后这段时间）整段错记到第二天。
        day = seg_start.astimezone(TZ).date().isoformat()
        bucket = exposure_by_day.setdefault(day, _empty_exposure_bucket())
        for key, value in exposure_delta(
            current, after, hours, known=usable_observation is not None,
            observation=usable_observation, rain_mm=rain_mm,
        ).items():
            bucket[key] += value
        # 只在这一段真正“从 >=50 蒸发穿越到 <50”时才按恒定蒸发速率算出
        # 精确交点，而不是记成整段的结束时刻；如果这一段开始时就已经在
        # 50 以下（跌破发生在更早的段），这里不是一次新的跌破，不应该把
        # last_dip_below_50_at 继续往后推——它只应该是"最近一次真正下穿
        # 50"的时刻，不是"最近一次仍在 50 以下的段尾"。
        if current >= 50.0 and after_evaporation < 50.0:
            rate_per_hour = evaporation / hours
            crossing_hours = max(0.0, min(hours, (current - 50.0) / rate_per_hour))
            last_dip_below_50_at = seg_start + timedelta(hours=crossing_hours)
        if gain > 0:
            last_rain_at = seg_end
        current = round(after, 4)
    return ReplayResult(
        moisture=current, exposure_by_day=exposure_by_day,
        last_rain_at=last_rain_at, last_dip_below_50_at=last_dip_below_50_at,
    )


def drying_reason(
    start: datetime,
    end: datetime,
    observations: list[dict[str, Any]],
) -> str:
    """裁决 ``start`` 之后导致额外蒸发的已观测主因。

    文案只能使用这里返回的类别，不能拿“当前有风/当前晴热”倒推先前水分
    为什么降低。分段、新鲜度与 :func:`replay` 完全相同；没有可证明的额外
    蒸发就返回 ``normal_dry``。
    """
    if end <= start or start.tzinfo is None or end.tzinfo is None:
        return "normal_dry"
    scores = {"hot": 0.0, "wind": 0.0, "low_humidity": 0.0}
    hot_clear_hours = 0.0
    boundaries = replay_boundaries(start, end, observations)
    for seg_start, seg_end in zip(boundaries, boundaries[1:]):
        hours = (seg_end - seg_start).total_seconds() / 3600
        if hours <= 0:
            continue
        usable = [
            item for item in observations
            if (observed := parse_timestamp(item.get("observed_at"))) is not None
            and observed <= seg_start and fresh_at(item, seg_end)
        ]
        observation = max(usable, key=observation_sort_key) if usable else None
        if observation is None:
            continue
        tags = set(observation.get("tags") or [])
        raw_temp = observation.get("feels_like_c")
        if raw_temp is None:
            raw_temp = observation.get("temp_c")
        try:
            temperature = float(raw_temp) if raw_temp is not None else None
        except (TypeError, ValueError):
            temperature = None
        is_hot = "hot" in tags or (temperature is not None and temperature >= 30)
        if is_hot:
            scores["hot"] += hours * (
                2.3 if temperature is not None and temperature >= 35 else 1.5
            )
            if "clear" in tags and 8 <= seg_start.astimezone(TZ).hour < 20:
                hot_clear_hours += hours
        if "windy" in tags:
            scores["wind"] += hours * 0.5
        try:
            humidity = float(observation.get("humidity_pct"))
        except (TypeError, ValueError):
            humidity = None
        if humidity is not None and humidity < 40:
            scores["low_humidity"] += hours * 0.5
    cause, score = max(scores.items(), key=lambda item: item[1])
    if score <= 0:
        return "normal_dry"
    if cause == "hot":
        return "hot_clear_dry" if hot_clear_hours > 0 else "hot_dry"
    if cause == "wind":
        return "wind_dry"
    return "low_humidity_dry"


def parse_timestamp(value: object) -> datetime | None:
    """解析带时区的 ISO 时间；无时区时间不能伪装成事实时间。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def as_iso(value: datetime) -> str:
    return value.isoformat()


def observation_sort_key(value: dict[str, Any]) -> datetime:
    """所有缓存排序都换成 UTC 瞬间，绝不按原始 ISO 字符串比较。"""
    observed_at = parse_timestamp(value["observed_at"])
    if observed_at is None:
        raise ValueError("观测时间无效")
    return observed_at.astimezone(timezone.utc)


def _number(value: object, *, low: float, high: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(parsed) or not low <= parsed <= high:
        return None
    return parsed


def wind_scale_max(value: object) -> int | None:
    """把和风的 ``3-4``、``3～4`` 等风力写成一个安全上限。"""
    if isinstance(value, bool) or value is None:
        return None
    values = [int(part) for part in re.findall(r"\d+", str(value))]
    if not values or any(number > 17 for number in values):
        return None
    return max(values)


def weather_tags(
    condition_text: object,
    *,
    temp_c: float | None,
    feels_like_c: float | None,
    humidity_pct: float | None,
    wind_scale: int | None,
) -> list[str]:
    """仅从白名单天气文字和已校验数值派生表现标签。"""
    text = str(condition_text or "").strip()
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
    temperature = feels_like_c if feels_like_c is not None else temp_c
    if temperature is not None:
        if temperature >= 30:
            tags.add("hot")
        if temperature <= 5:
            tags.add("cold")
    if humidity_pct is not None and humidity_pct >= 85:
        tags.add("humid")
    if wind_scale is not None and wind_scale >= 3:
        tags.add("windy")
    return sorted(tags & ALLOWED_TAGS)


def normalize_observation(
    *,
    location_id: object,
    location_name: object,
    observed_time: object,
    received_at: datetime,
    temp: object,
    feels_like: object,
    humidity: object,
    wind_scale: object,
    precip: object,
    condition_text: object,
    provider: str = OBSERVATION_PROVIDER,
) -> dict[str, Any] | None:
    """将一次和风实况转为可持久化的最小观测，非法关键时间直接拒绝。"""
    observed_at = parse_timestamp(observed_time)
    if (
        observed_at is None
        or received_at.tzinfo is None
        or observed_at > received_at + MAX_FUTURE_SKEW
    ):
        return None
    city = str(location_id or "").strip()
    text = str(condition_text or "").strip()
    if not city or not text or not provider:
        return None

    temp_c = _number(temp, low=-100, high=70)
    feels_like_c = _number(feels_like, low=-100, high=80)
    humidity_pct = _number(humidity, low=0, high=100)
    precipitation = _number(precip, low=0, high=500)
    wind_max = wind_scale_max(wind_scale)
    # 和风 now.precip 是过去一小时累计毫米数；以一小时窗口归一后，
    # 后续只能按时间片积分，不能在每次读取时整份重复累计。
    precip_rate_mm_h = precipitation
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "location_id": city,
        "location_name": str(location_name or "").strip(),
        "observation_id": f"{provider}:{city}:{as_iso(observed_at)}",
        "observed_at": as_iso(observed_at),
        "received_at": as_iso(received_at),
        "temp_c": temp_c,
        "feels_like_c": feels_like_c,
        "humidity_pct": humidity_pct,
        "wind_scale_max": wind_max,
        "precip_rate_mm_h": precip_rate_mm_h,
        "condition_text": text,
        "tags": weather_tags(
            text,
            temp_c=temp_c,
            feels_like_c=feels_like_c,
            humidity_pct=humidity_pct,
            wind_scale=wind_max,
        ),
    }


def validate_observation(value: object, *, now: datetime | None = None) -> dict[str, Any] | None:
    """验证缓存记录，不让损坏或未来记录作为环境事实被读取。"""
    if not isinstance(value, dict) or value.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
        return None
    received_at = parse_timestamp(value.get("received_at"))
    observed_at = parse_timestamp(value.get("observed_at"))
    if received_at is None or observed_at is None:
        return None
    if now is not None and (now.tzinfo is None or observed_at > now + MAX_FUTURE_SKEW):
        return None
    normalized = normalize_observation(
        location_id=value.get("location_id"),
        location_name=value.get("location_name"),
        observed_time=value.get("observed_at"),
        received_at=received_at,
        temp=value.get("temp_c"),
        feels_like=value.get("feels_like_c"),
        humidity=value.get("humidity_pct"),
        wind_scale=value.get("wind_scale_max"),
        precip=value.get("precip_rate_mm_h"),
        condition_text=value.get("condition_text"),
        provider=str(value.get("observation_id", "")).split(":", 1)[0],
    )
    if normalized is None:
        return None
    # 观测 ID 是去重和审计键，不能因为重新标准化而悄悄改变。
    if value.get("observation_id") != normalized["observation_id"]:
        return None
    return normalized
