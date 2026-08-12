#!/usr/bin/env python3
"""小院子“逛逛”的纯场景层：可见快照、稳定键与本地兜底。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import garden_content
import garden_crops
import garden_weather


SCENE_KEY_VERSION = 2
SCENE_CACHE_LIMIT = 48
SCENE_TEXT_MAX_CHARS = 180

_PERIOD_LABELS = {
    "dawn": "早晨",
    "day": "白天",
    "dusk": "傍晚",
    "night": "夜间",
    "late_night": "深夜",
}
_STAGE_LABELS = {
    "seed": "刚播下",
    "sprout": "刚发芽",
    "growing": "正在长大",
    "ready": "已经成熟",
    "withered": "已经枯死",
}
_CONDITION_LABELS = {
    "pest": "正闹着虫害",
    "diseased_leaf": "叶片正带着病斑",
    "waterlogged": "土面正积着水",
    "nutrient_deficiency": "正显出缺肥",
}
_CONDITION_ENVIRONMENT_LABELS = {
    "pest": "虫害",
    "diseased_leaf": "病斑",
    "waterlogged": "积水",
    "nutrient_deficiency": "缺肥迹象",
}
_PLOT_LABELS = {"p1": "一号地", "p2": "二号地", "p3": "三号地", "p4": "四号地"}
_VIEW_ACTION_MARKERS = (
    "施肥", "施了肥", "追肥", "撒肥", "撒了肥",
    "松土", "松了土", "翻土", "翻了土",
    "除虫", "除了虫", "捉虫", "捉了虫", "喷药", "打药",
    "修剪", "修剪了", "剪叶", "剪了叶", "剪掉",
)
# 浇水/播种/收获类词面上既是动作，也是"刚浇过/种子/成熟"阶段最自然的
# 中文状态描述（本地兜底自己都用"刚播下"）；只有快照里确实没有对应事实
# 时，出现这些词才当作写手擅自杜撰了动作。
_WATERING_MARKERS = (
    "浇水", "浇了", "浇过", "浇进", "倒水", "倒了水", "水瓢", "灌水",
    "灌溉", "正在添水", "添了水", "正在补水", "补了水",
)
_SOWING_MARKERS = ("播种", "播了种", "种下", "种进", "移栽")
_HARVEST_MARKERS = ("收获", "采摘", "摘下", "摘了", "拔掉")
_IMMERSION_BLOCKED = (
    "主人", "人工智能", "ai", "模型", "程序", "游戏", "指令", "数据",
    "结果：",
)


def _visible_condition(plot: dict) -> dict | None:
    """只投影阶段 C 公开的可见字段，不把 condition_id 等审计值送给写手。"""
    raw = plot.get("condition")
    if not isinstance(raw, dict):
        # 阶段 B/C 并行期间兼容候选命名；最终联调可收敛为正式字段。
        raw = plot.get("active_condition")
    if not isinstance(raw, dict):
        return None
    condition_type = raw.get("type")
    status = raw.get("status")
    severity = raw.get("severity")
    if not all(isinstance(value, str) and value for value in (condition_type, status, severity)):
        return None
    if status not in ("active", "failed"):
        return None
    visible = {"type": condition_type, "status": status, "severity": severity}
    condition_id = raw.get("condition_id")
    if isinstance(condition_id, str) and condition_id:
        # 同一茬后来再次发生同型、同严重度异常时也必须换场景键；只保留单向摘要，
        # 并在写手 payload 里剥离，condition_id 本身仍不离开本地状态层。
        visible["_instance"] = hashlib.sha256(condition_id.encode("utf-8")).hexdigest()
    return visible


def build_visible_snapshot(
    state: dict,
    context,
    *,
    weather_tags: frozenset[str] = frozenset(),
    animal_weather_mode: str = "normal",
    yard_water: str = "none",
    rain_streak_days: int = 0,
) -> dict:
    """把完整存档收敛成写手可见且可稳定哈希的最少事实。"""
    today = context.now.date().isoformat()
    environment = state.get("environment") if isinstance(state.get("environment"), dict) else {}
    weather_status = str(environment.get("status") or "missing")
    if weather_status not in ("fresh", "stale", "missing"):
        weather_status = "missing"
    if yard_water not in ("none", "puddles", "flooded"):
        yard_water = "none"
    rain_streak_days = max(0, int(rain_streak_days))
    animals = []
    for animal in state.get("animals", []):
        if not isinstance(animal, dict) or animal.get("status") != "active":
            continue
        animals.append({
            "id": str(animal.get("id") or ""),
            "species": str(animal.get("species") or "小动物"),
            "nickname": str(animal.get("nickname") or ""),
            "personality": str(animal.get("personality") or ""),
            "bond_level": int(animal.get("bond_level") or 0),
            "residency": str(animal.get("residency") or "visitor"),
            "visit_status": "active",
            "spot": str(animal.get("spot") or ""),
        })
    animals.sort(key=lambda item: item["id"])

    plots = []
    for plot in state.get("plots", []):
        if not isinstance(plot, dict):
            continue
        visible = {
            "plot_id": str(plot.get("plot_id") or ""),
            "status": str(plot.get("status") or "empty"),
        }
        crop_id = plot.get("crop_id")
        if isinstance(crop_id, str) and crop_id in garden_crops.CROPS:
            watering = plot.get("watering_by_date")
            watering_count = (
                int(watering.get(today, 0))
                if isinstance(watering, dict) and isinstance(watering.get(today, 0), int)
                else int(today in plot.get("water_bonus_dates", []))
            )
            visible.update({
                "crop_id": crop_id,
                "crop_name": garden_crops.crop_name(crop_id),
                "stage": (
                    "withered"
                    if plot.get("status") == "withered"
                    else str(plot.get("stage") or plot.get("status") or "")
                ),
                "watered_today": watering_count > 0,
            })
            soil = plot.get("soil")
            if isinstance(soil, dict):
                try:
                    moisture = float(soil.get("moisture"))
                except (TypeError, ValueError):
                    moisture = None
                if moisture is not None:
                    # 不把百分比送进场景层；五档可见状态才是写手事实。
                    visible["soil_state"] = garden_weather.moisture_label(moisture)
                source = soil.get("last_water_source")
                visible["water_source"] = source if source in ("manual", "rain") else "none"
                visible["_long_wet"] = bool(
                    moisture is not None
                    and garden_weather.moisture_label(moisture) == "湿透"
                    and float(soil.get("saturated_hours") or 0.0) >= 12.0
                )
                yesterday = (context.now.date() - timedelta(days=1)).isoformat()
                bucket = soil.get("exposure_by_date", {}).get(yesterday)
                multiplier = garden_weather.daily_growth_multiplier(
                    bucket,
                    heat_tendency=garden_crops.CROPS[crop_id].get("heat_tendency", "neutral"),
                )
                visible["_growth_trend"] = (
                    "fast" if plot.get("status") == "growing" and multiplier >= 1.05
                    else "slow" if plot.get("status") == "growing" and multiplier <= 0.85
                    else "steady"
                )
            condition = _visible_condition(plot)
            if condition is not None:
                visible["condition"] = condition
        plots.append(visible)
    plots.sort(key=lambda item: item["plot_id"])

    return {
        "beijing_date": today,
        "day_period": context.day_period,
        "season": context.season,
        "solar_term": {
            "id": context.term_id or "",
            "name": context.term_name or "",
        },
        "festival": {
            "id": context.festival_id or "",
            "name": context.festival_name or "",
        },
        "weather_status": weather_status,
        "weather_tags": sorted(weather_tags) if weather_status == "fresh" else [],
        "animal_weather_mode": str(animal_weather_mode or "normal"),
        "yard_water": yard_water,
        "rain_streak_days": rain_streak_days,
        "animals": animals,
        "plots": plots,
    }


def scene_key(snapshot: dict) -> str:
    # 连雨天数只是一条补充事实；同一积水档位内每天递增不应让缓存失效。
    key_snapshot = dict(snapshot)
    key_snapshot.pop("rain_streak_days", None)
    canonical = json.dumps(
        key_snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"scene:v{SCENE_KEY_VERSION}:{digest}"


def writer_snapshot(snapshot: dict) -> dict:
    """严格白名单，而非“去掉下划线字段”：新内部字段默认绝不外发。"""
    allowed_top = ("day_period", "season", "solar_term", "festival", "weather_status",
                   "weather_tags", "animal_weather_mode", "yard_water",
                   "rain_streak_days", "animals", "plots")
    result = {key: snapshot[key] for key in allowed_top if key in snapshot}
    result["solar_term"] = {key: str(snapshot.get("solar_term", {}).get(key) or "") for key in ("id", "name")}
    result["festival"] = {key: str(snapshot.get("festival", {}).get(key) or "") for key in ("id", "name")}
    result["animals"] = [{key: animal.get(key, "") for key in ("species", "nickname", "personality", "bond_level", "residency", "visit_status", "spot")}
                         for animal in snapshot.get("animals", []) if isinstance(animal, dict)]
    result["plots"] = [{key: plot.get(key, "") for key in ("plot_id", "status", "crop_name", "stage", "watered_today", "soil_state", "water_source", "condition") if key in plot}
                       for plot in snapshot.get("plots", []) if isinstance(plot, dict)]
    for plot in result["plots"]:
        if isinstance(plot.get("condition"), dict):
            plot["condition"] = {key: plot["condition"].get(key, "") for key in ("type", "status", "severity")}
    return result


def validate_writer_text(text: str, snapshot: dict) -> None:
    """写手只可表现代码已经确定的环境事实，不能借文案获得规则权限。"""
    if not isinstance(text, str):
        raise ValueError("小院子写手返回的逛逛画面格式不对")
    text = " ".join(text.split()).strip()
    if len(text) < 20 or len(text) > SCENE_TEXT_MAX_CHARS:
        raise ValueError("小院子写手返回的逛逛画面越界")
    if text[-1] not in "。！？…」』”":
        raise ValueError("小院子写手返回的逛逛画面像是被截断了")
    lowered = text.lower()
    if any(word in lowered for word in _IMMERSION_BLOCKED):
        raise ValueError("小院子写手返回的逛逛画面含有禁用表达")
    weather_status = str(snapshot.get("weather_status") or "missing")
    tags = frozenset(str(tag) for tag in snapshot.get("weather_tags") or ())
    recent_rain = any(
        isinstance(plot, dict)
        and plot.get("water_source") == "rain"
        and plot.get("soil_state") in ("湿润", "湿透")
        for plot in snapshot.get("plots", [])
    )
    if not garden_content.scene_text_compatible(
        text,
        season=str(snapshot.get("season") or ""),
        day_period=str(snapshot.get("day_period") or ""),
        weather_tags=tags if weather_status == "fresh" else frozenset(),
        recent_rain=recent_rain,
        yard_water=str(snapshot.get("yard_water") or "none"),
    ):
        raise ValueError("小院子写手返回了与当前环境冲突的逛逛画面")
    yard_water = str(snapshot.get("yard_water") or "none")
    if yard_water == "none" and any(word in text for word in (
        "水洼", "院子内涝", "小径汪着水", "低处的水连成一片",
    )):
        raise ValueError("小院子写手编造了院级积水")
    if yard_water == "puddles" and any(word in text for word in (
        "院子内涝", "低处的水连成一片", "低处全被漫住",
    )):
        raise ValueError("小院子写手擅自把水洼升级成内涝")
    plots_list = [plot for plot in snapshot.get("plots", []) if isinstance(plot, dict)]
    stages_present = {str(plot.get("stage") or "") for plot in plots_list}
    watered_present = any(plot.get("water_source") in ("manual", "rain") for plot in plots_list)
    action_markers = _VIEW_ACTION_MARKERS
    if not watered_present:
        action_markers += _WATERING_MARKERS
    if "seed" not in stages_present:
        action_markers += _SOWING_MARKERS
    if "ready" not in stages_present:
        action_markers += _HARVEST_MARKERS
    if any(word in text for word in action_markers):
        raise ValueError("小院子写手把查看写成了动作")
    visible_crops = {
        str(plot.get("crop_name") or "")
        for plot in snapshot.get("plots", [])
        if isinstance(plot, dict) and plot.get("crop_name")
    }
    known_crops = {
        str(crop.get("name") or "")
        for crop in garden_crops.CROPS.values()
        if isinstance(crop, dict) and crop.get("name")
    }
    if any(name in text for name in known_crops - visible_crops):
        raise ValueError("小院子写手写入了当前不存在的作物")
    soil_states = {str(plot.get("soil_state") or "") for plot in snapshot.get("plots", []) if isinstance(plot, dict)}
    if any(word in text for word in ("土干了", "土壤干燥", "干裂的土")) and not soil_states.intersection({"偏干", "干燥"}):
        raise ValueError("小院子写手把土壤写得过干")
    rain_soil = any(
        isinstance(plot, dict)
        and plot.get("water_source") == "rain"
        and plot.get("soil_state") in ("湿润", "湿透")
        for plot in snapshot.get("plots", [])
    )
    if "雨浇透" in text and not rain_soil:
        raise ValueError("小院子写手编造了降雨浇透")
    if any(word in text for word in ("天气预报", "预计明天", "明天将", "稍后会下", "即将下")):
        raise ValueError("小院子写手把预报写进了当前实况")
    if any(word in text for word in (
        "受伤", "流血", "死了", "病倒", "骨折", "奄奄一息",
        "亲密下降", "好感下降", "不再信任", "讨厌起来",
    )):
        raise ValueError("小院子写手擅自改变了动物安全或关系")
    if any(word in text for word in (
        "离开院子", "离开小院", "跑出院子", "跑出小院", "走出院子",
        "再也不来", "不再回来", "被赶走", "消失不见",
    )):
        raise ValueError("小院子写手擅自让动物离开")
    visible_conditions = {
        str(plot.get("condition", {}).get("type") or "")
        for plot in snapshot.get("plots", [])
        if isinstance(plot, dict) and isinstance(plot.get("condition"), dict)
    }
    condition_markers = {
        "pest": ("虫害", "虫眼", "啃痕", "虫子", "小虫", "虫卵", "被虫咬"),
        "diseased_leaf": ("病叶", "病斑", "叶片发黑", "叶子发黑", "叶片染病", "病害"),
        "waterlogged": ("积水", "浮水", "水涝", "涝了", "泡在水里", "根部泡水"),
        "nutrient_deficiency": ("缺肥", "缺少养分", "养分不足", "营养不足", "肥力不足"),
    }
    for condition_type, markers in condition_markers.items():
        if condition_type == "waterlogged" and yard_water in ("puddles", "flooded"):
            markers = tuple(marker for marker in markers if marker not in ("积水", "水涝", "涝了"))
        if condition_type not in visible_conditions and any(marker in text for marker in markers):
            raise ValueError("小院子写手擅自生成了第二个作物异常")


def condition_label(condition_type: str) -> str:
    return _CONDITION_LABELS.get(condition_type, "异常")


def _fallback_animal_weather_phrase(species: str, mode: str, stable_index: int) -> str | None:
    """按场景键稳定选物种专属候选；对应物种池缺失才退回旧画面。"""
    category = garden_content.category_from_species(str(species or ""))
    pool = garden_content.ANIMAL_WEATHER_LINES.get(category, {}).get(mode)
    if not pool:
        return None
    return pool[stable_index % len(pool)].rstrip("。")


def _fallback_scene(snapshot: dict, *, selection_offset: int = 0) -> str:
    """确定性兜底；不读取随机源，同一个快照即使缓存丢失也仍返回同一句。

    动物那句在天气模式不是 normal 时改用阶段 D 骨架整句覆盖（而不是接在
    “在 xx 转悠”后面追加），因为这里本来就没有已建立的“上一句动作”可以
    承接，直接整句写清楚当下的天气行为最不容易冲突。
    """
    term = str(snapshot.get("festival", {}).get("name") or snapshot.get("solar_term", {}).get("name") or "")
    opening = f"{term}，" if term else ""

    digest = (
        int(hashlib.sha256(scene_key(snapshot).encode("utf-8")).hexdigest()[:8], 16)
        + selection_offset * 104729
    )
    weather_tags = frozenset(snapshot.get("weather_tags") or ())
    recent_rain = any(
        isinstance(plot, dict)
        and plot.get("water_source") == "rain"
        and plot.get("soil_state") in ("湿润", "湿透")
        for plot in snapshot.get("plots", [])
    )
    soil_wet = any(
        isinstance(plot, dict) and plot.get("soil_state") in ("湿润", "湿透")
        for plot in snapshot.get("plots", [])
    )
    environment_line = garden_content.environment_scene_line(
        weather_tags=weather_tags,
        weather_status=str(snapshot.get("weather_status") or "missing"),
        day_period=str(snapshot.get("day_period") or "day"),
        season=str(snapshot.get("season") or "spring"),
        recent_rain=recent_rain,
        soil_wet=soil_wet,
        stable_index=digest,
    )
    yard_water_line = garden_content.yard_water_scene_line(
        str(snapshot.get("yard_water") or "none"),
        int(snapshot.get("rain_streak_days") or 0),
        stable_index=digest,
    )
    crop_phrases = []
    for plot in snapshot.get("plots", []):
        crop_name = plot.get("crop_name")
        if not crop_name:
            continue
        stage = _STAGE_LABELS.get(str(plot.get("stage") or ""), "待在地里")
        plot_label = _PLOT_LABELS.get(str(plot.get("plot_id") or ""), "菜畦")
        phrase = f"{plot_label}的{crop_name}{stage}"
        condition = plot.get("condition")
        condition_name = ""
        if isinstance(condition, dict):
            condition_type = str(condition.get("type") or "")
            condition_name = _CONDITION_ENVIRONMENT_LABELS.get(
                condition_type, condition_label(condition_type),
            )
            phrase += f"，{condition_name}"
        elif plot.get("watered_today"):
            phrase += "，土色还湿着"
        environment = garden_content.crop_environment_line(
            crop=str(crop_name),
            condition=condition_name,
            soil_state=str(plot.get("soil_state") or ""),
            water_source=str(plot.get("water_source") or "none"),
            weather_tags=weather_tags,
            growth_trend=str(plot.get("_growth_trend") or "steady"),
            long_wet=bool(plot.get("_long_wet")),
            stable_index=digest + len(crop_phrases),
        )
        if environment:
            # 环境模板本身已经带作物名；由它承担主语，健康作物再补阶段，
            # 避免“小番茄……小番茄……”紧邻复读。异常模板已经说明暂停，
            # 不再接“正在长大”制造自相矛盾。
            phrase = f"{plot_label}的{environment.rstrip('。')}"
            if not condition_name:
                phrase += f"，目前{stage}"
        crop_phrases.append(phrase)

    animal_mode = str(snapshot.get("animal_weather_mode") or "normal")
    animal_phrases = []
    for index, animal in enumerate(snapshot.get("animals", [])):
        name = str(animal.get("nickname") or animal.get("species") or "小动物")
        flavor = _fallback_animal_weather_phrase(
            str(animal.get("species") or ""), animal_mode, digest + index,
        )
        if flavor:
            if animal_mode == "normal":
                spot = str(animal.get("spot") or "菜畦边")
                animal_phrases.append(f"{name}在{spot}，{flavor}")
            else:
                animal_phrases.append(f"{name}{flavor}")
        else:
            spot = str(animal.get("spot") or "菜畦边")
            animal_phrases.append(f"{name}在{spot}慢慢转悠")

    def compose() -> str:
        if crop_phrases:
            result = f"{opening}{environment_line}{yard_water_line}四块菜畦排在院子里：{'，'.join(crop_phrases)}。"
        else:
            result = f"{opening}{environment_line}{yard_water_line}四块菜畦都还空着，翻松的土安静排在院子里。"
        if animal_phrases:
            result = result[:-1] + "；" + "，".join(animal_phrases) + "。"
        return result

    text = compose()
    while len(text) > SCENE_TEXT_MAX_CHARS and len(animal_phrases) > 1:
        animal_phrases.pop()
        text = compose()
    while len(text) > SCENE_TEXT_MAX_CHARS and len(crop_phrases) > 1:
        crop_phrases.pop()
        text = compose()
    if len(text) > SCENE_TEXT_MAX_CHARS:
        text = text[:SCENE_TEXT_MAX_CHARS - 1].rstrip("，；：") + "。"
    return text


def fallback_scene(snapshot: dict) -> str:
    return _fallback_scene(snapshot)


def minimal_fallback_scene(snapshot: dict) -> str:
    """本地丰富池若被同级闸门拒绝，只保留最小、可由快照直接证明的画面。"""
    period = _PERIOD_LABELS.get(str(snapshot.get("day_period") or ""), "这会儿")
    crops = [
        str(plot.get("crop_name") or "")
        for plot in snapshot.get("plots", [])
        if isinstance(plot, dict) and plot.get("crop_name")
    ]
    animals = [
        str(animal.get("nickname") or animal.get("species") or "")
        for animal in snapshot.get("animals", [])
        if isinstance(animal, dict) and (animal.get("nickname") or animal.get("species"))
    ]
    parts = [f"{period}，四块菜畦安静排在院子里"]
    yard_water_line = garden_content.yard_water_scene_line(
        str(snapshot.get("yard_water") or "none"),
        int(snapshot.get("rain_streak_days") or 0),
    )
    if yard_water_line:
        parts.append(yard_water_line.rstrip("。"))
    if crops:
        parts.append(f"{'、'.join(crops[:2])}还在各自的地里")
    if animals:
        parts.append(f"{'、'.join(animals[:2])}也在院里")
    text = "；".join(parts) + "。"
    if len(text) > SCENE_TEXT_MAX_CHARS:
        text = text[:SCENE_TEXT_MAX_CHARS - 1].rstrip("，；：") + "。"
    return text


def validate_cache(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ValueError("场景缓存不是列表")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("场景缓存条目不是对象")
        key = item.get("scene_key")
        text = item.get("text")
        source = item.get("source")
        generated_at = item.get("generated_at")
        prefixes = tuple(f"scene:v{version}:" for version in (1, SCENE_KEY_VERSION))
        prefix = next((item for item in prefixes if isinstance(key, str) and key.startswith(item)), "")
        digest = key[len(prefix):] if prefix else ""
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("场景键无效")
        if not isinstance(text, str) or not text.strip() or len(text) > SCENE_TEXT_MAX_CHARS:
            raise ValueError("场景正文无效")
        if source not in ("deepseek", "fallback"):
            raise ValueError("场景来源无效")
        if not isinstance(generated_at, str):
            raise ValueError("场景时间无效")
        parsed = datetime.fromisoformat(generated_at)
        if parsed.tzinfo is None:
            raise ValueError("场景时间缺少时区")


def cached_text(cache: object, expected_key: str) -> str | None:
    if not isinstance(cache, list):
        return None
    return next(
        (
            str(item["text"])
            for item in reversed(cache)
            if isinstance(item, dict) and item.get("scene_key") == expected_key
        ),
        None,
    )


def append_cache(cache: list[dict], item: dict) -> list[dict]:
    without_duplicate = [entry for entry in cache if entry.get("scene_key") != item["scene_key"]]
    without_duplicate.append(item)
    return without_duplicate[-SCENE_CACHE_LIMIT:]
