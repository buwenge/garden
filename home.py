#!/usr/bin/env python3
"""小院子 CLI 入口——agent 的院子操作统一命令行。

用法：home 院子 查看 / home 院子 浇水 1 / home 院子 播种 番茄 2 / …
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import garden
import log_store

TZ = ZoneInfo("Asia/Shanghai")

DOMAIN_ALIASES = {
    "garden": ("小院子", "院子", "花园"),
}


class HomeError(Exception):
    pass


@dataclass(frozen=True)
class Request:
    domain: str
    text: str
    raw_text: str = ""
    help: bool = False
    dry_run: bool = False

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).strip().lower()
    return re.sub(r"[\s，。！？、；：:]+", "", text)


def text_similarity(text: str, alias: str) -> float:
    """无模型的文字相似度；兼顾长句中包含短意图和少量错别字。"""
    text, alias = normalize(text), normalize(alias)
    if not text or not alias:
        return 0.0
    if alias in text:
        return 1.0 + min(len(alias), 8) * 0.01
    if text in alias:
        return 0.92
    ratio = SequenceMatcher(None, text, alias).ratio()
    block = SequenceMatcher(None, text, alias).find_longest_match().size / len(alias)
    # 长口语句会拉低整句 ratio；最长公共片段能保住其中的动作词。
    return max(ratio, block * 0.9)


def compact_intent_text(text: str) -> str:
    text = normalize(text)
    text = re.sub(r"(?:最近|过去|近)?(?:\d{1,5}|[零一二两三四五六七八九十]{1,3})(?:天|日|周|星期|%|k|度)?", "", text)
    for filler in ("帮我", "给我", "查一下", "看一下", "查查", "看看", "查看", "查询", "请", "当前", "今天"):
        text = text.replace(filler, "")
    return text


def best_match(text: str, catalog: dict[str, tuple[str, ...]], threshold: float = 0.45) -> str | None:
    raw = normalize(text)
    compact = compact_intent_text(text)
    variants = ((raw, 0.0), (compact, 0.04 if compact != raw else 0.0))
    ranked: list[tuple[float, int, str]] = []
    for intent, aliases in catalog.items():
        score, specificity = max(
            (text_similarity(variant, alias) + bonus, len(alias))
            for alias in aliases for variant, bonus in variants if variant
        )
        ranked.append((score, specificity, intent))
    score, _, intent = max(ranked)
    return intent if score >= threshold else None


def remove_normalized_phrase(raw: str, phrase: str) -> str:
    """Remove one compact/normalized phrase while preserving the rest verbatim."""
    compact_chars: list[str] = []
    raw_indexes: list[int] = []
    for index, char in enumerate(raw):
        normalized = unicodedata.normalize("NFKC", char).lower()
        for normalized_char in normalized:
            if re.match(r"[\s，。！？、；：:]", normalized_char):
                continue
            compact_chars.append(normalized_char)
            raw_indexes.append(index)
    compact = "".join(compact_chars)
    target = normalize(phrase)
    position = compact.find(target)
    if position < 0:
        return raw
    start = raw_indexes[position]
    end = raw_indexes[position + len(target) - 1] + 1
    return (raw[:start] + " " + raw[end:]).strip()


def parse_request(argv: list[str]) -> Request:
    help_requested = any(arg in ("-h", "--help", "帮助", "怎么用") for arg in argv)
    dry_run = "--dry-run" in argv
    raw_tokens = [arg for arg in argv if arg not in ("-h", "--help", "--dry-run")]
    raw = " ".join(raw_tokens).strip()
    if not raw or normalize(raw) in ("帮助", "怎么用"):
        raise HomeError("用法：home 院子 <命令>")
    head = normalize(raw_tokens[0]) if raw_tokens else ""
    garden_aliases = DOMAIN_ALIASES["garden"]
    for alias in garden_aliases:
        if normalize(alias) in head:
            raw_remainder = remove_normalized_phrase(raw, alias)
            return Request(
                domain="garden",
                text=normalize(raw_remainder),
                raw_text=raw_remainder,
                help=help_requested,
                dry_run=dry_run,
            )
    return Request(
        domain="garden",
        text=normalize(raw),
        raw_text=raw,
        help=help_requested,
        dry_run=dry_run,
    )


def run(command: list[str], timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HomeError("操作超时，请原样重试一次") from exc
    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        raise HomeError(output or f"命令执行失败（{result.returncode}）")
    return output


def _garden_help_topic(value: str) -> str | None:
    compact = normalize(value).replace("帮助", "").replace("怎么用", "")
    if any(word in compact for word in ("鸡舍", "鸡窝", "鸡蛋", "孵化")):
        return "coop"
    if any(word in compact for word in ("食物", "吃的", "做饭", "制作", "烹饪", "送给user")):
        return "food"
    if any(word in compact for word in ("动物", "投喂", "摸摸", "陪玩", "取名")):
        return "animal"
    if any(word in compact for word in (
        "种地", "种植", "作物", "播种", "浇水", "收获", "除虫", "修剪", "松土", "施肥", "清理",
        "能种什么", "能种", "仓库", "库房",
    )):
        return "crop"
    if any(word in compact for word in ("查看", "逛逛", "篮子", "库存", "手账", "记录")):
        return "view"
    return None


def _garden_recipe_brief(recipe: dict) -> str:
    bits = [
        garden.garden_crops.crop_name(crop_id) if amount == 1 else f"{garden.garden_crops.crop_name(crop_id)}×{amount}"
        for crop_id, amount in recipe["ingredients"].items()
    ]
    for prepared_id, amount in recipe.get("prepared_ingredients", {}).items():
        prepared_name = garden.garden_crops.RECIPES[prepared_id]["name"]
        bits.append(prepared_name if amount == 1 else f"{prepared_name}×{amount}")
    egg_range = recipe.get("egg_range")
    if egg_range:
        bits.append(f"蛋{egg_range[0]}~{egg_range[1]}")
    return f"{recipe['name']}（{'+'.join(bits)}）"


def _garden_recipe_catalog_text() -> str:
    return "、".join(
        _garden_recipe_brief(recipe) for recipe in garden.garden_crops.RECIPES.values()
    )


def _garden_recipe_lookup(value: str) -> str:
    """按单一食材检索菜谱；不带参数或说"大全"时给全部目录。只读，不动篮子。"""
    compact = normalize(value)
    if not compact or compact in ("大全", "全部", "所有", "目录"):
        return "全部食谱：" + _garden_recipe_catalog_text()
    if compact in ("鸡蛋", "蛋"):
        recipes = [r for r in garden.garden_crops.RECIPES.values() if r.get("egg_range")]
        header = "鸡蛋"
    else:
        crop_id = garden.garden_crops.resolve_crop(value)
        if crop_id is None:
            raise HomeError(
                "没认出这种食材；可说『home 院子 食谱 <作物名>』或『home 院子 食谱大全』"
            )
        recipes = [
            garden.garden_crops.RECIPES[recipe_id]
            for recipe_id in garden.garden_crops.recipes_for_crop(crop_id)
        ]
        header = garden.garden_crops.crop_name(crop_id)
    if not recipes:
        return f"{header}目前没有对应的菜谱。"
    return f"{header}可以做：" + "、".join(_garden_recipe_brief(r) for r in recipes)


def _garden_makeable_recipes(inventory: dict) -> list[str]:
    # 可做性判断只有 garden._recipe_ingredients_available 这一份实现
    # （produce + produce_poor 合计），CLI「够做什么」/收获后提示与网页做
    # 菜面板共用，避免第七版阶段D品质合计口径前后端各算一遍出现分裂。
    return [
        recipe["name"] for recipe in garden.garden_crops.RECIPES.values()
        if garden._recipe_ingredients_available(inventory, recipe)
    ]


def _garden_food_help() -> str:
    try:
        makeable = _garden_makeable_recipes(garden.crop_snapshot()["inventory"])
    except garden.GardenError:
        makeable = []
    if makeable:
        shown = "、".join(makeable[:8])
        stock_line = f"篮子现在够做：{shown}" + (
            f"等{len(makeable)}道" if len(makeable) > 8 else ""
        )
    else:
        stock_line = "篮子里的材料暂时凑不成一道菜"
    return f"""食物：
home 院子 制作 <食谱名>（缺食材会说明需要什么）
{stock_line}
查某样食材：home 院子 食谱 <作物名>；全部：home 院子 食谱大全
home 院子 送给user <食物或成品名> [便签内容]（便签可省略；例：送给user 凉拌黄瓜 这是刚拌的）
home 院子 送给另一只agent <食物或成品名> [便签内容]"""


def garden_help(topic: str | None = None) -> str:
    topic = _garden_help_topic(topic or "")
    if topic == "food":
        return _garden_food_help()
    pages = {
        "view": """查看院子：
home 院子 查看 / 查看二号地 / 查看 详细
home 院子 逛逛院子
home 院子 篮子 / 手账 / 记录""",
        "crop": """种地：
home 院子 把辣椒种到三号地
home 院子 全部浇水 / 给二号地浇点水
home 院子 收获二号地
有异常时直接说：给二号地除虫、修剪、松土、施肥或清理。
施肥要消耗肥料×1（打扫鸡舍攒堆肥获得，六份鸡粪沤三天出一份肥料）：
治缺肥、退水后救内涝欠佳的地块，或给生长中的作物追肥（每茬最多5次，每次快一成）。
home 院子 现在能种什么（查当季能种的种子）
home 院子 仓库（看不是当季、暂存起来的种子）""",
        "animal": """动物：
home 院子 喂团团一点黄瓜
home 院子 摸摸团团 / 陪团团玩
home 院子 取名 <动物名字或编号> <新名字>
鸡住进来后也可以说：喂鸡 <名字或编号> / 摸摸鸡 <名字或编号> / 鸡取名 <编号> <新名字>""",
        "coop": """鸡舍：
小动物送来鸡蛋后可说：我来孵那三枚蛋 / 吃掉那三枚蛋。
选择孵化时才会搭起鸡舍；之后可说：查看鸡舍。
母鸡偶尔会下出可孵化鸡蛋，可说：孵化可孵化鸡蛋。
成鸡每天会攒鸡粪，可说：打扫鸡舍——六份鸡粪沤三天出一份肥料。
施肥三用：治缺肥、退水后救内涝欠佳的地块、给生长中的作物追肥（每茬最多5次）。""",
    }
    if topic in pages:
        return pages[topic]
    return """小院子可以直接说动作，不必背固定格式：
查看｜种地｜动物｜食物｜鸡舍
分类说明：home 院子 <分类> --help
例如：home 院子 种地 --help"""


def _garden_failure_hint(raw: str, reason: str) -> str:
    """失败只补一条最相关示例，不把整页帮助塞进当前上下文。"""
    compact = normalize(raw)
    if any(word in compact for word in ("鸡舍", "鸡窝", "鸡蛋", "孵")):
        hint = "可试：home 院子 查看鸡舍；分类说明：home 院子 鸡舍 --help"
    elif any(word in compact for word in ("制作", "做", "吃的", "食物", "送给user")):
        hint = "可试：home 院子 制作拍黄瓜；分类说明：home 院子 食物 --help"
    elif any(word in compact for word in ("投喂", "喂", "摸", "陪", "取名")):
        hint = "可试：home 院子 摸摸团团；分类说明：home 院子 动物 --help"
    elif any(word in compact for word in ("播", "种", "栽")):
        hint = "可试：home 院子 把辣椒种到三号地"
    elif any(word in compact for word in ("排水", "水沟")):
        hint = "可试：home 院子 排水（只有院子有积水时才能用）"
    elif any(word in compact for word in ("浇", "水")):
        hint = "可试：home 院子 全部浇水 / 给二号地浇点水"
    elif any(word in compact for word in ("收", "摘", "采")):
        hint = "可试：home 院子 收获二号地"
    elif any(word in compact for word in ("除虫", "修剪", "松土", "施肥", "清理")):
        hint = "可试：home 院子 给二号地除虫；分类说明：home 院子 种地 --help"
    else:
        hint = "可试：home 院子 查看；分类说明：home 院子 --help"
    if len(reason) > 180 and not any(word in reason for word in ("损坏", "停止写入")):
        reason = reason[:177] + "…"
    return f"未执行：{reason}\n{hint}"


_GARDEN_STAGE_LABEL = {"fresh": "精神奕奕", "thirsty": "状态还行", "wilted": "有些疲惫"}
_GARDEN_ACTIONS = ("浇水", "投喂", "摸摸", "陪玩")
_GARDEN_BATCH_SEP = re.compile(r"[，,、/\s]+|和|与|跟")


def _garden_animal_batch_selectors(value: str) -> list[str] | None:
    """把"暖暖和栗子"这类自然的多目标说法拆成一份可分别执行的选择器列表。

    不是"全部"也不是单个——每个词都必须真实、唯一地对上一只当前活跃的
    花草/小动物才算数，否则不当批量处理，原样交回给单目标逻辑，避免把
    "投喂暖暖一点黄瓜"这类单目标+物品的说法误拆成两个目标。"""
    tokens = [token for token in _GARDEN_BATCH_SEP.split(value) if token]
    if len(tokens) < 2:
        return None
    try:
        entries = garden.active_entries()
    except garden.GardenError:
        return None
    for token in tokens:
        matches = [e for e in entries if garden.matches_entry_selector(e, token)]
        if len(matches) != 1:
            return None
    return tokens


def _garden_care_result_line(action: str, result: dict) -> str:
    entry = result["entry"]
    reaction = garden.describe_care(
        entry, action, result["revived"],
        weather_mode=result.get("weather_mode", "normal"),
    )
    revived_note = "，精神好多了" if result["revived"] else ""
    log_store.write_log("info", "activity", f"小院子：给 {garden.display_name(entry)} {action}了{revived_note}")
    bond = result.get("bond")
    bond_note = ""
    if bond is not None:
        bond_note = f" · 亲密 {entry['bond_points']}｜{garden.bond_level_name(entry['bond_level'])}"
    name = garden.display_name(entry)
    completed = {
        "浇水": f"已经给{name}浇水",
        "投喂": f"已经投喂{name}",
        "摸摸": f"已经摸了摸{name}",
        "陪玩": f"已经陪{name}玩过",
    }[action]
    return f"结果：{completed}。{reaction}{bond_note}"


def _garden_kind_label(kind: str) -> str:
    return "植物" if kind == "flower" else "小动物"


def _garden_plot_line(plot: dict, crop_state: dict, now: datetime) -> str:
    label = garden.plot_label(plot["plot_id"])
    if plot.get("status") == "empty":
        return f"{label}菜畦：空着"
    crop = garden.garden_crops.CROPS[plot["crop_id"]]
    if plot.get("status") == "withered":
        state = "已经枯死，需要清理"
    elif plot.get("status") == "ready":
        state = "已经成熟，随时可以收获"
    else:
        state = {
            "seed": "刚播下", "sprout": "发芽了", "growing": "正在长大",
        }[plot["stage"]]
    soil = plot.get("soil")
    today = now.date().isoformat()
    if crop_state.get("real_environment_enabled") and isinstance(soil, dict):
        moisture = float(soil.get("moisture", 0))
        level = garden.garden_weather.moisture_label(moisture)
        advice = "需要浇水" if moisture < 25 else "可以浇水" if moisture < 50 else "暂时不用浇" if moisture < 70 else "不要再浇" if moisture < 85 else "明确不应浇"
        watered = f" · 土壤{level}，{advice}"
    else:
        watered = (
            " · 今日已浇水，土还湿着"
            if int(plot.get("watering_by_date", {}).get(today, 0)) > 0
            or today in plot.get("water_bonus_dates", [])
            else ""
        )
    condition = plot.get("condition")
    condition_note = ""
    if isinstance(condition, dict):
        condition_name = garden.CONDITION_LABELS[condition["type"]]
        if condition.get("status") == "active":
            penalty = " · 本轮预计少收1份" if int(plot.get("yield_penalty", 0)) else ""
            action = garden.CONDITION_ACTIONS[condition["type"]]
            condition_note = f" · {condition_name} · 生长暂停{penalty} · 请先{action}"
            watered = ""
        elif condition.get("status") == "resolved" and int(plot.get("yield_penalty", 0)):
            condition_note = " · 本轮预计少收1份"
    growth_note = ""
    if not (isinstance(condition, dict) and condition.get("status") == "active"):
        note = garden.growth_environment_note(plot, now)
        if note:
            growth_note = f" · {note}"
    timing_note = ""
    timing = plot.get("timing")
    if isinstance(timing, dict) and plot.get("status") == "growing":
        remaining = timing.get("current", {}).get("remaining_seconds")
        if isinstance(remaining, int):
            timing_note = f" · 按当前条件预计还有{_garden_duration_text(remaining)}成熟"
    # 第七版阶段E：品质标记只在 growing/ready 有意义（withered 没有收成，
    # 标不标都无所谓，不额外画蛇添足）；不可逆、无动作可做，属于"知道就
    # 好"的信息，因此只在详细/单地块查看里出现，裸查看摘要不缀这一笔。
    quality_note = (
        " · 品相欠佳"
        if plot.get("quality") == "poor" and plot.get("status") in ("growing", "ready")
        else ""
    )
    return (
        f"{label}菜畦：{crop['name']} · {state}"
        f"{condition_note}{quality_note}{watered}{growth_note}{timing_note}"
        f" · 基础成熟期{crop['growth_days']}天"
    )


def _garden_plot_attention(plot: dict, crop_state: dict) -> str | None:
    if plot.get("status") == "empty":
        return None
    label = f"{plot['plot_id'][1:]}号地"
    crop_name = garden.garden_crops.crop_name(plot["crop_id"]) if plot.get("crop_id") else ""
    condition = plot.get("condition")
    if isinstance(condition, dict) and condition.get("status") == "active":
        return (
            f"{label}{garden.CONDITION_LABELS[condition['type']]}中，"
            f"先{garden.CONDITION_ACTIONS[condition['type']]}"
        )
    if plot.get("status") == "withered":
        return f"{label}的{crop_name}枯死了，需清理"
    if plot.get("status") == "ready":
        return f"{label}的{crop_name}已成熟，可以收获"
    soil = plot.get("soil")
    if crop_state.get("real_environment_enabled") and isinstance(soil, dict):
        band = garden.garden_weather.moisture_band(float(soil.get("moisture", 0)))
        if band == "dry":
            return f"{label}缺水"
        if band == "dryish":
            return f"{label}有点缺水"
        if band == "saturated":
            return f"{label}土壤湿透，先别浇水"
    return None


_GARDEN_EMPTY_TEXT = (
    "小院子现在空空的，什么都没有，这很正常，一切都是从空白开始，"
    "但不会永远空白，也许下一秒，也许某个自然的一次唤醒。"
)

_GARDEN_STAGE_TEXT = {"seed": "刚播下", "sprout": "发芽了", "growing": "正在长大"}


def _garden_attention_items(
    plots: list[dict], crop_state: dict, entries: list[dict], coop: dict, now: datetime,
) -> list[str]:
    items = [
        note for plot in plots
        for note in [_garden_plot_attention(plot, crop_state)]
        if note
    ]
    for entry in entries:
        if garden.compute_stage(entry, now) == "wilted":
            items.append(f"{garden.display_name(entry)}有些疲惫，想被照顾一下")
    if coop["story_status"] == "awaiting_choice":
        items.append(
            f"{coop['source_animal_name']}送来的鸡蛋×{coop['egg_count']}"
            "还在等 agent 决定吃掉还是孵化"
        )
    # 鸡粪攒满提醒；单句既有模式，不用池，也不以"X号地"开头，因此不会被
    # 下面的合并逻辑意外并进地块提示里。
    manure_units = int(coop.get("manure_units") or 0)
    manure_cap = int(coop.get("manure_cap") or 0)
    if manure_cap > 0 and manure_units >= manure_cap:
        items.append("鸡舍攒了不少鸡粪，该打扫了")
    return items


_PLOT_ATTENTION_LABEL_RE = re.compile(r"^(\d)号地(.*)$")


def _merge_plot_attention_notes(notes: list[str]) -> list[str]:
    """多块地提醒文字完全相同时合并成一行：
    "1号地土壤湿透，先别浇水"+"4号地土壤湿透，先别浇水" → "1、4号地土壤湿透，先别浇水"。
    不是"X号地"开头的提醒（动物/鸡舍）原样保留，相对顺序不变。"""
    buckets: dict[str, list[str]] = {}
    order: list[str] = []
    others: list[str] = []
    for note in notes:
        match = _PLOT_ATTENTION_LABEL_RE.match(note)
        if match is None:
            others.append(note)
            continue
        number, suffix = match.groups()
        if suffix not in buckets:
            buckets[suffix] = []
            order.append(suffix)
        buckets[suffix].append(number)
    merged = [f"{'、'.join(buckets[suffix])}号地{suffix}" for suffix in order]
    return merged + others


def _garden_plots_compact_line(plots: list[dict], crop_state: dict) -> str | None:
    """健康地块按同阶段合并成一行；有异常/成熟/枯死的地块交给"要留意"描述。"""
    buckets: dict[str, list[str]] = {}
    empties = []
    for plot in plots:
        number = plot["plot_id"][1:]
        if plot.get("status") == "empty":
            empties.append(number)
            continue
        if _garden_plot_attention(plot, crop_state) and (
            plot.get("status") in ("ready", "withered")
            or (isinstance(plot.get("condition"), dict) and plot["condition"].get("status") == "active")
        ):
            continue
        stage_text = (
            _GARDEN_STAGE_TEXT.get(plot.get("stage") or "", "正在长大")
        )
        name = garden.garden_crops.crop_name(plot["crop_id"])
        buckets.setdefault(stage_text, []).append(f"{number}号{name}")
    parts = [
        f"{'、'.join(labels)}{stage_text}"
        for stage_text in ("正在长大", "发芽了", "刚播下")
        for labels in [buckets.get(stage_text)]
        if labels
    ]
    if empties:
        parts.append(f"{'、'.join(empties)}号空着")
    if not parts:
        return None
    return "菜地：" + "，".join(parts)


def _garden_animals_compact_line(entries: list[dict], away: list[dict]) -> str | None:
    animals = [e for e in entries if e.get("kind") == "animal"]
    away_animals = [e for e in away if e.get("kind") == "animal"]
    parts = []
    if animals:
        parts.append(f"{'、'.join(garden.display_name(e, include_species=True) for e in animals)}在院子里")
    if away_animals:
        parts.append(
            f"{'、'.join(garden.display_name(e, include_species=True) for e in away_animals)}出去转转了"
        )
    if not parts:
        return None
    return "动物：" + "；".join(parts)


def _garden_plants_compact_line(entries: list[dict], now: datetime) -> str | None:
    plants = [e for e in entries if e.get("kind") != "animal"]
    if not plants:
        return None
    buckets: dict[str, list[str]] = {}
    for entry in plants:
        label = _GARDEN_STAGE_LABEL[garden.compute_stage(entry, now)]
        buckets.setdefault(label, []).append(garden.display_name(entry))
    parts = [f"{'、'.join(names)}{label}" for label, names in buckets.items()]
    return "花草：" + "，".join(parts)


def _garden_coop_compact_line(coop: dict) -> str | None:
    if not coop["built"]:
        return None
    status = coop["story_status"]
    if status == "incubating":
        seconds = int(coop.get("remaining_seconds") or 0)
        count = int(coop.get("incubating_egg_count") or 0)
        return f"鸡舍：孵蛋中×{count}，约剩{seconds // 3600}小时{seconds % 3600 // 60}分"
    juveniles = sum(
        chick.get("stage") == "chick" for chick in coop.get("chicks", [])
    )
    if juveniles:
        seconds = int(coop.get("next_maturity_seconds") or 0)
        return f"鸡舍：小鸡×{juveniles}，约剩{seconds // 3600}小时{seconds % 3600 // 60}分长大"
    line = (
        f"鸡舍：母鸡×{coop.get('hen_count', 0)}、公鸡×{coop.get('rooster_count', 0)}，"
        f"篮子鸡蛋×{coop.get('egg_count', 0)}"
    )
    if int(coop.get("hatchable_egg_count") or 0):
        line += f"、可孵化×{coop['hatchable_egg_count']}"
    return line


def _garden_yard_water_line(crop_state: dict) -> str | None:
    environment = crop_state.get("environment")
    if not isinstance(environment, dict):
        return None
    line = garden.garden_content.yard_water_scene_line(
        str(environment.get("yard_water") or "none"),
        int(environment.get("rain_streak_days") or 0),
    )
    return line or None


def _garden_view_fingerprint(
    plots: list[dict], entries: list[dict], away: list[dict], coop: dict,
    environment: dict | None = None,
) -> dict:
    return {
        "plots": {
            plot["plot_id"]: {
                "crop": plot.get("crop_id"),
                "status": plot.get("status"),
                "stage": plot.get("stage"),
            }
            for plot in plots
        },
        "active": {e["id"]: garden.display_name(e) for e in entries},
        "away": {e["id"]: garden.display_name(e) for e in away},
        "coop": {
            "story": coop["story_status"],
            "adults": int(coop.get("hen_count", 0)) + int(coop.get("rooster_count", 0)),
            "juveniles": sum(
                chick.get("stage") == "chick" for chick in coop.get("chicks", [])
            ),
            "laid": int(coop.get("total_eggs_laid", 0)),
            "hatchable": int(coop.get("hatchable_egg_count") or 0),
        },
        "yard_water": str((environment or {}).get("yard_water") or "none"),
    }


def _garden_view_changes(old: dict, new: dict) -> list[str]:
    """两次查看之间值得一提的变化；成熟/枯死/异常交给"要留意"，不重复报。"""
    changes = []
    stage_order = {"seed": 0, "sprout": 1, "growing": 2}
    old_plots = old.get("plots") or {}
    for plot_id, info in (new.get("plots") or {}).items():
        prev = old_plots.get(plot_id) or {}
        label = f"{plot_id[1:]}号"
        crop = info.get("crop")
        name = garden.garden_crops.crop_name(crop) if crop in garden.garden_crops.CROPS else ""
        if prev.get("crop") and not crop:
            changes.append(f"{label}地空出来了")
        elif crop and not prev.get("crop"):
            changes.append(f"{label}地种下了{name}")
        elif crop and prev.get("crop") and prev["crop"] != crop:
            changes.append(f"{label}地换种了{name}")
        elif (
            crop
            and info.get("status") == "growing"
            and prev.get("status") == "growing"
            and stage_order.get(info.get("stage"), 0) > stage_order.get(prev.get("stage"), 0)
        ):
            changes.append(
                f"{label}{name}发芽了"
                if info.get("stage") == "sprout"
                else f"{label}{name}长起来了"
            )
    old_active = old.get("active") or {}
    old_away = old.get("away") or {}
    for entry_id, name in (new.get("active") or {}).items():
        if entry_id in old_away:
            changes.append(f"{name}回来了")
        elif entry_id not in old_active:
            changes.append(f"{name}来到了院子")
    for entry_id, name in (new.get("away") or {}).items():
        if entry_id in old_active:
            changes.append(f"{name}出去转转了")
        elif entry_id not in old_away:
            changes.append(f"{name}来过院子，现在出去转转了")
    for entry_id, name in {**old_active, **old_away}.items():
        if entry_id not in (new.get("active") or {}) and entry_id not in (new.get("away") or {}):
            changes.append(f"{name}悄悄离开了")
    old_coop = old.get("coop") or {}
    new_coop = new.get("coop") or {}
    laid_delta = int(new_coop.get("laid", 0)) - int(old_coop.get("laid", 0))
    if laid_delta > 0:
        changes.append(f"母鸡下了{laid_delta}枚蛋")
    if int(new_coop.get("juveniles", 0)) > int(old_coop.get("juveniles", 0)):
        changes.append("有小鸡出壳了")
    if (
        int(new_coop.get("adults", 0)) > int(old_coop.get("adults", 0))
        and int(new_coop.get("juveniles", 0)) < int(old_coop.get("juveniles", 0))
    ):
        changes.append("小鸡长大了")
    hatchable_delta = int(new_coop.get("hatchable", 0)) - int(old_coop.get("hatchable", 0))
    if hatchable_delta > 0:
        changes.append(f"多了{hatchable_delta}枚可孵化鸡蛋")
    old_water = str(old.get("yard_water") or "none")
    new_water = str(new.get("yard_water") or "none")
    if old_water != new_water:
        if new_water == "flooded":
            changes.append("院子低处的水连成一片，内涝了")
        elif new_water == "puddles":
            changes.append(
                "院子里的内涝退成了几处水洼"
                if old_water == "flooded" else "院子里起了几处水洼"
            )
        elif old_water in ("puddles", "flooded"):
            changes.append("院子里的水退净了")
    return changes


def _garden_list_active() -> str:
    """裸查看默认给摘要；同一 session 第二次起只报上次以来的变化。"""
    now = datetime.now(TZ)
    garden.advance(now)
    crop_state = garden.crop_snapshot(now=now, acknowledge_conditions=True)
    entries = garden.active_entries()
    away = garden.away_entries()
    coop = garden.coop_snapshot(now=now)
    plots = crop_state["plots"]
    yard_water_line = _garden_yard_water_line(crop_state)
    if not entries and not away and not coop["built"] and not any(
        plot.get("status") != "empty" for plot in plots
    ) and not yard_water_line:
        return _GARDEN_EMPTY_TEXT
    attention = _merge_plot_attention_notes(
        _garden_attention_items(plots, crop_state, entries, coop, now)
    )
    attention_line = "要留意：" + "；".join(attention) + "。" if attention else None
    fingerprint = _garden_view_fingerprint(
        plots, entries, away, coop, crop_state.get("environment"),
    )
    previous = garden.swap_view_fingerprint(
        os.environ.get("SESSION_ID"), fingerprint,
    )
    if previous is not None:
        changes = _garden_view_changes(previous, fingerprint)
        if not changes and not attention_line:
            return "跟上次看的时候差不多，一切都好。"
        lines = [
            "上次看过之后：" + "；".join(changes) + "。"
            if changes else "跟上次看的时候差不多。"
        ]
        if attention_line:
            lines.append(attention_line)
        return "\n".join(lines)
    body_lines = [
        line for line in (
            yard_water_line,
            _garden_plots_compact_line(plots, crop_state),
            _garden_animals_compact_line(entries, away),
            _garden_plants_compact_line(entries, now),
            _garden_coop_compact_line(coop),
        ) if line
    ]
    lines = [attention_line] if attention_line else []
    lines.append("小院子现在有：")
    lines.extend(body_lines)
    return "\n".join(lines)


def _garden_list_active_detailed() -> str:
    now = datetime.now(TZ)
    garden.advance(now)
    crop_state = garden.crop_snapshot(
        now=now, acknowledge_conditions=True,
    )
    entries = garden.active_entries()
    away = garden.away_entries()
    coop = garden.coop_snapshot(now=now)
    plots = crop_state["plots"]
    yard_water_line = _garden_yard_water_line(crop_state)
    if not entries and not away and not coop["built"] and not any(plot.get("status") != "empty" for plot in plots) and not yard_water_line:
        return _GARDEN_EMPTY_TEXT
    lines = [
        f"[{entry['id']}] {entry['spot']} · {garden.display_name(entry, include_species=True)}"
        f"（{_garden_kind_label(entry['kind'])}；{entry.get('trait') or '还不太熟悉'}）"
        f"—— {_GARDEN_STAGE_LABEL[garden.compute_stage(entry, now)]}"
        + (
            f" · 亲密 {entry.get('bond_points', 0)}｜{garden.bond_level_name(int(entry.get('bond_level', 0)))}"
            + (" · 常住" if entry.get("residency") == "resident" else "")
            if entry.get("kind") == "animal" else ""
        )
        for entry in entries
    ]
    if away:
        lines.append(
            f"{'、'.join(garden.display_name(entry, include_species=True) for entry in away)}出去转转了，"
            "以后还会沿着熟悉的路回来。"
        )
    crop_lines = [_garden_plot_line(plot, crop_state, now) for plot in plots]
    coop_lines = (
        [_garden_coop_line(coop)]
        if coop["built"] or coop["story_status"] == "awaiting_choice" else []
    )
    yard_lines = [yard_water_line] if yard_water_line else []
    return "小院子现在有：\n" + "\n".join(yard_lines + lines + crop_lines + coop_lines)


def _garden_coop_manure_suffix(coop: dict) -> str:
    """鸡粪份数/堆肥角/肥料库存追加行，只在鸡舍真的建好时有意义。鸡粪份数
    固定展示，堆肥角只在有批次发酵时展示，肥料库存只在大于 0 时展示——
    三段各自独立，缺哪段就不占那一段的版面。
    """
    units = int(coop.get("manure_units") or 0)
    cap = int(coop.get("manure_cap") or 0)
    manure_text = f"鸡粪×{units}"
    if cap > 0:
        manure_text += f"/{cap}"
    if cap > 0 and units >= cap:
        manure_text += "（攒满了，该打扫鸡舍了）"
    parts = [manure_text]
    for batch in coop.get("compost_batches") or []:
        seconds = int(batch.get("remaining_seconds") or 0)
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        parts.append(
            f"堆肥角：鸡粪×{int(batch.get('manure_units', 0))}发酵中，"
            f"约剩{hours}小时{minutes}分，出肥料×{int(batch.get('units', 0))}"
        )
    fertilizer_count = int(coop.get("fertilizer_count") or 0)
    if fertilizer_count > 0:
        parts.append(f"肥料×{fertilizer_count}")
    return "\n" + "；".join(parts)


def _garden_coop_line(coop: dict) -> str:
    status = coop["story_status"]
    if status == "awaiting_choice":
        return f"鸡蛋 · {coop['source_animal_name']}推来了鸡蛋×{coop['egg_count']}，正在等 agent 决定吃掉还是孵化。"
    if status == "incubating":
        seconds = int(coop.get("remaining_seconds") or 0)
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        count = int(coop.get("incubating_egg_count") or 0)
        return (
            f"鸡舍 · agent 正在亲自孵鸡蛋×{count}，约剩{hours}小时{minutes}分；"
            f"成鸡母×{coop.get('hen_count', 0)}、公×{coop.get('rooster_count', 0)}，"
            f"篮子可孵化鸡蛋×{coop.get('hatchable_egg_count', 0)}。"
        ) + _garden_coop_manure_suffix(coop)
    if status == "hatched":
        if coop.get("chick_count"):
            young_hens = sum(
                chick.get("stage") == "chick" and chick.get("sex") == "hen"
                for chick in coop["chicks"]
            )
            young_roosters = sum(
                chick.get("stage") == "chick" and chick.get("sex") == "rooster"
                for chick in coop["chicks"]
            )
            seconds = int(coop.get("next_maturity_seconds") or 0)
            hours, remainder = divmod(seconds, 3600)
            minutes = remainder // 60
            return (
                f"鸡舍 · 小母鸡×{young_hens}、小公鸡×{young_roosters}，"
                f"约剩{hours}小时{minutes}分长大；成鸡母×{coop.get('hen_count', 0)}、"
                f"公×{coop.get('rooster_count', 0)}。"
            ) + _garden_coop_manure_suffix(coop)
        return (
            f"鸡舍 · 母鸡×{coop.get('hen_count', 0)}、公鸡×{coop.get('rooster_count', 0)}；"
            f"篮子鸡蛋×{coop['egg_count']}、可孵化鸡蛋×{coop.get('hatchable_egg_count', 0)}，"
            f"累计下蛋×{coop.get('total_eggs_laid', 0)}。"
        ) + _garden_coop_manure_suffix(coop)
    return "鸡舍 · 已经搭好。" + _garden_coop_manure_suffix(coop)


def _garden_coop_status() -> str:
    coop = garden.coop_snapshot()
    if not coop["built"]:
        if coop["story_status"] == "awaiting_choice":
            return _garden_coop_line(coop) + "\n选择孵化后才会搭起鸡舍。"
        if coop["story_status"] == "eaten":
            return "三枚鸡蛋已经吃掉了，因此没有搭鸡舍。"
        return "院子里还没有鸡舍；要等小动物先送来鸡蛋，决定孵化时才会搭。"
    summary = _garden_coop_line(coop)
    if coop.get("chicks"):
        members = "\n".join(
            (
                f"- {garden.chicken_display_name(chick, include_kind=bool(chick.get('nickname')))}："
                f"{garden.chicken_appearance(chick)}；性格：{chick['personality']}。"
            )
            for chick in coop["chicks"]
        )
        return f"{summary}\n成员：\n{members}"
    return summary


def _garden_view_plot(selector: str) -> str:
    now = datetime.now(TZ)
    garden.advance(now)
    crop_state = garden.crop_snapshot(now=now, acknowledge_conditions=True)
    batch = _garden_plot_batch_selectors(selector)
    if batch is not None and not batch:
        return _garden_list_active()
    selectors = batch if batch is not None else [selector]
    plot_ids = [garden.plot_id_from_selector(s) for s in selectors]
    if None in plot_ids:
        raise HomeError("地块编号不明确，请说『home 院子 查看 1/2/3/4』")
    if len(set(plot_ids)) != len(plot_ids):
        raise HomeError("批量查看的地块有重复，请每块地只写一次")
    by_id = {plot["plot_id"]: plot for plot in crop_state["plots"]}
    selected_ids = set(plot_ids)
    lines = [_garden_plot_line(by_id[plot_id], crop_state, now) for plot_id in plot_ids]
    notes = _merge_plot_attention_notes([
        note for plot in crop_state["plots"]
        if plot["plot_id"] not in selected_ids
        for note in [_garden_plot_attention(plot, crop_state)]
        if note
    ])
    output = "\n".join(lines)
    if notes:
        output += "\n提示：" + "，".join(notes) + "。"
    return output


def _garden_basket() -> str:
    inventory = garden.crop_snapshot()["inventory"]
    def render(section: str, catalog: dict) -> str:
        items = inventory[section]
        if not items:
            return "空"
        return "、".join(f"{catalog[item_id]['name']}×{amount}" for item_id, amount in items.items())
    lines = [
        "门廊小桌上的篮子：",
        f"种子：{render('seeds', garden.garden_crops.CROPS)}",
        f"收获：{render('produce', garden.garden_crops.CROPS)}",
    ]
    # 欠佳堆为空时整行不出现，不占版面（设计稿第六节5）。
    if inventory.get("produce_poor"):
        lines.append(f"收获(欠佳)：{render('produce_poor', garden.garden_crops.CROPS)}")
    lines.append(f"做好的点心：{render('prepared_food', garden.garden_crops.RECIPES)}")
    lines.append(
        f"蛋类：{render('animal_products', {key: {'name': name} for key, name in garden.ANIMAL_PRODUCT_NAMES.items()})}"
    )
    # 肥料只在大于 0 时显示，不占空篮子的版面。
    fertilizer_count = int(inventory.get("fertilizer", {}).get("fertilizer", 0))
    if fertilizer_count > 0:
        lines.append(f"肥料：{fertilizer_count}份")
    return "\n".join(lines)


def _garden_plantable_now() -> str:
    """当季能种的种子；只读，不查仓库内容，只在为空时提示去仓库看看。"""
    inventory = garden.crop_snapshot()["inventory"]
    seeds = inventory.get("seeds", {})
    available = [
        f"{garden.garden_crops.crop_name(crop_id)}×{amount}"
        for crop_id, amount in seeds.items() if int(amount) > 0
    ]
    if not available:
        hint = "，可以说『查看仓库』看看还有什么在等季节" if inventory.get("warehouse_seeds") else ""
        return f"种子盒现在是空的，没有能种下去的种子{hint}。"
    stored = inventory.get("warehouse_seeds", {})
    hint = (
        f"\n另外仓库里还存着{len(stored)}种不是当季的种子，等季节到了会自动搬回来；想看可以说『查看仓库』。"
        if stored else ""
    )
    return "现在能种：" + "、".join(available) + hint


def _garden_warehouse() -> str:
    """仓库：只存放过季种子，只有明确说"仓库"才会展示，不出现在查看/篮子里。"""
    inventory = garden.crop_snapshot()["inventory"]
    stored = inventory.get("warehouse_seeds", {})
    if not stored:
        return "仓库现在是空的，所有种子都在应季，没有需要暂存的。"
    groups: dict[tuple, list[str]] = {}
    for crop_id, amount in stored.items():
        crop = garden.garden_crops.CROPS.get(crop_id)
        if crop is None:
            continue
        groups.setdefault(crop["seasons"], []).append(f"{crop['name']}×{amount}")
    order = garden.garden_crops.SEASON_ORDER
    lines = [
        f"{'/'.join(garden.garden_crops.SEASON_NAMES[s] for s in seasons)}种子："
        + "、".join(names)
        for seasons, names in sorted(groups.items(), key=lambda item: min(order.index(s) for s in item[0]))
    ]
    return "仓库里存放着不是当季的种子：\n" + "\n".join(lines)


def _garden_journal() -> str:
    journal = garden.journal_snapshot()
    def latest(records: list[dict], render, empty: str) -> str:
        return "、".join(render(record) for record in records[-8:]) if records else empty

    species = latest(journal["species_seen"], lambda record: str(record.get("species") or "小访客"), "还没有遇见新的小动物")
    harvests = latest(
        journal["crops_harvested"],
        lambda record: garden.garden_crops.crop_name(str(record.get("crop_id") or ""))
        + ("（品相欠佳）" if record.get("quality") == "poor" else ""),
        "还没有收获记录",
    )
    meals = latest(journal["meals_made"], lambda record: garden.garden_crops.RECIPES.get(str(record.get("recipe_id")), {}).get("name", "一份小点心"), "还没有做过点心")
    gifts = latest(journal["gifts_given"], lambda record: str(record.get("display_name") or "一份小礼物"), "还没有送出院子礼物")
    bonds = latest(journal["bond_milestones"], lambda record: f"{record.get('animal_name') or '小访客'} · {record.get('level_name') or '更熟了一点'}", "还没有亲密里程碑")
    calendar = latest(journal["calendar_moments"], lambda record: str(record.get("name") or "一个季节时刻"), "还没有记下日历时刻")
    incidents = latest(
        journal["crop_incidents"],
        lambda record: (
            f"{garden.plot_label(str(record.get('plot_id')))}地"
            f"{garden.garden_crops.crop_name(str(record.get('crop_id') or ''))}"
            f"的{garden.CONDITION_LABELS.get(str(record.get('type')), '异常')}"
            + (
                f"已由{record.get('resolved_by')}处理"
                if record.get("outcome") == "resolved"
                else "最终枯死"
                if record.get("outcome") == "failed"
                else "仍待处理"
            )
        ),
        "还没有作物事故",
    )
    def _drainage_line(record: dict) -> str:
        at = (record.get("at") or "")[:16].replace("T", " ")
        level = str(record.get("yard_water") or "")
        level_label = {"puddles": "水洼", "flooded": "内涝"}.get(level, level or "未知")
        streak = record.get("rain_streak_days", 0)
        return f"{at} 清了排水沟（{level_label}/连雨{streak}天）"

    drainage = latest(journal.get("drainage", []), _drainage_line, "还没有排过水")
    return (
        "院子手账：\n"
        f"已发现物种：{species}\n"
        f"作物收获：{harvests}\n"
        f"做过的食物：{meals}\n"
        f"送给user的礼物：{gifts}\n"
        f"亲密里程碑：{bonds}\n"
        f"日历时刻：{calendar}\n"
        f"作物事故：{incidents}\n"
        f"排水记录：{drainage}"
    )


def _garden_list_left() -> str:
    garden.advance(datetime.now(TZ))
    entries = sorted(garden.left_entries(), key=lambda e: e.get("left_at") or "", reverse=True)[:5]
    if not entries:
        return "还没有离开的记录。"
    lines = [
        f"{(entry.get('left_at') or '')[:10]} · {entry['spot']}的"
        f"{garden.display_name(entry, include_species=True)}："
        f"{entry.get('departure_note') or '后来顺着自己的生活离开了。'}"
        for entry in entries
    ]
    return "曾经来过、后来离开的：\n" + "\n".join(lines)


def _parse_gift_item_note(text: str) -> tuple[str, str]:
    """从 '凉拌黄瓜 这是刚拌的' 拆出 (物品名, 便签)。"""
    try:
        garden._resolve_gift_item(text)
        return text, ""
    except garden.GardenError:
        pass
    parts = text.split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return text, ""


def _deliver_gift_to_basket(display_name: str, note: str) -> None:
    """写小篮子 + pending 通知文件。"""
    import json as _json
    import uuid as _uuid
    basket_path = Path("gift_basket.md")
    if basket_path.exists():
        content = basket_path.read_text(encoding="utf-8")
        eid = _uuid.uuid4().hex[:8]
        from datetime import datetime as _dt0
        import zoneinfo as _zi0
        now_str = _dt0.now(_zi0.ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
        tail = f" —— {now_str}。{note}" if note else f" —— {now_str}。"
        line = f"- {display_name}{tail} <!-- id:{eid} -->"
        marker = "## 已收到"
        if marker in content:
            content = content.replace(marker, f"{marker}\n\n{line}", 1)
            basket_path.write_text(content, encoding="utf-8")
    pending_path = Path(".pending-gifts.json")
    pending = []
    if pending_path.exists():
        try:
            pending = _json.loads(pending_path.read_text(encoding="utf-8"))
        except Exception:
            pending = []
    from datetime import datetime as _dt
    import zoneinfo as _zi
    pending.append({
        "item": display_name,
        "note": note,
        "from": "agent",
        "at": _dt.now(_zi.ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M"),
    })
    pending_path.write_text(_json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")


def _garden_action_text(key: str, **values) -> str:
    recipe_id = values.pop("recipe_id", None)
    crop_id = values.pop("crop_id", None)
    pool = garden.garden_content.GARDEN_ACTION_TEXT[key]
    if key == "meal" and recipe_id is not None:
        pool = garden.garden_content.RECIPE_MEAL_TEXT.get(recipe_id, pool)
    elif crop_id is not None:
        pool = garden.garden_content.CROP_VARIETY_TEXT.get(crop_id, {}).get(key, pool)
    return random.choice(pool).format(**values)


_OUTING_UNCLAIMED = object()


def _garden_claim_outing() -> dict | None:
    """动作结果已经落定后再领取出门句；附加文案永远不反向阻断动作回执。"""
    try:
        return garden.claim_outing_flavor()
    except (garden.GardenError, TypeError, ValueError):
        return None


def _garden_with_outing(body: str, outing=_OUTING_UNCLAIMED) -> str:
    if outing is _OUTING_UNCLAIMED:
        outing = _garden_claim_outing()
    if not isinstance(outing, dict):
        return body
    text = outing.get("text")
    if not isinstance(text, str) or not text:
        return body
    if outing.get("position") == "prefix":
        return f"{text}\n{body}"
    if outing.get("position") == "aftermath":
        return f"{body}\n{text}"
    return body


_WATER_PROTEST_TEXT = {
    "physical": (
        "{name}的叶尖往旁边一偏，水珠顺着叶脉滚回已经湿透的土面。",
        "{name}的叶片被水压得低了一下，根边的土颜色已经深得发亮。",
    ),
    "slapstick": (
        "{name}猛地甩了一下叶子，把水珠劈头盖脸弹回来，像是在嚷：“今天喝过啦！”",
        "{name}抖着叶片把水往外一掀，差点把水瓢也顶翻，显然不想再喝第二壶。",
    ),
}


def _garden_crop_treatment_text(
    result: dict,
    *,
    use_writer: bool = False,
    outing: dict | None = None,
) -> str:
    plot = garden.plot_label(result["plot_id"])
    name = garden.garden_crops.crop_name(result["crop_id"])
    action = result["action"]
    outcome = result["outcome"]
    if outcome == "resolved":
        condition_name = garden.CONDITION_LABELS[result["condition_type"]]
        penalty = (
            "；已经发生的减产仍保留，本轮会少收1份"
            if result["yield_penalty"] else "；本轮还没有减产"
        )
        fallback = f"{plot}地的{name}已经{action}，{condition_name}处理完了。"
        local_pool = garden.garden_content.crop_care_pool(
            result["crop_id"], f"treat_{result['condition_type']}",
        )
        if local_pool:
            body = random.choice(local_pool)
        else:
            body = (
                garden.crop_treatment_copy(result, fallback=fallback, outing=outing)
                if use_writer else fallback
            )
        return (
            f"{body}\n"
            f"结果：{plot}地的{condition_name}已解决，{name}恢复生长"
            f"{penalty}。"
        )
    if outcome == "wrong_action":
        condition_name = garden.CONDITION_LABELS[result["condition_type"]]
        fallback = f"给{plot}地的{name}做了{action}，眼前的{condition_name}没有消失。"
        body = (
            garden.crop_treatment_copy(result, fallback=fallback, outing=outing)
            if use_writer else fallback
        )
        return (
            f"{body}\n"
            f"结果：{plot}地仍是{condition_name}状态，{name}继续暂停生长；"
            f"需要{result['correct_action']}。"
        )
    if outcome == "withered":
        fallback = f"{plot}地的{name}已经枯死，{action}不能把这一茬救回来。"
        body = (
            garden.crop_treatment_copy(result, fallback=fallback, outing=outing)
            if use_writer else fallback
        )
        return (
            f"{body}\n"
            f"结果：{plot}地状态未改变；需要清理。"
        )
    fallback = f"{plot}地的{name}目前没有需要{action}处理的问题。"
    body = (
        garden.crop_treatment_copy(result, fallback=fallback, outing=outing)
        if use_writer else fallback
    )
    return (
        f"{body}\n"
        f"结果：{plot}地状态未改变；目前不需要{action}。"
    )


def _garden_fertilize_result_text(result: dict) -> str:
    """施肥的正常结果落回文案（第八版品质救援 + 第九版追肥）。

    ``kind == "condition"``：治缺肥成功，跟既有 resolve_crop_condition 的
    resolved 结果形状完全一致，直接复用 `_garden_crop_treatment_text` 与
    既有成功文案池（garden_content 里 nutrient_deficiency 的既有文案，不
    重写）。``quality_rescue``/``quality_unrecoverable`` 是第八版品质救援
    结果，``growth_boost`` 是第九版追肥结果，各用各的文案池；都不是
    错误——硬性拒绝（需要肥料×1/地还泡着/追肥已到上限/用不上肥料）走
    GardenError，不经过这里。
    """
    if result["kind"] == "condition":
        outing = _garden_claim_outing()
        return _garden_with_outing(
            _garden_crop_treatment_text(result, use_writer=True, outing=outing),
            outing,
        )
    plot = garden.plot_label(result["plot_id"])
    name = garden.garden_crops.crop_name(result["crop_id"])
    if result["kind"] == "quality_rescue":
        body = _garden_action_text("fertilize_rescue", plot=plot, name=name)
        return _garden_with_outing(f"{body}\n结果：{plot}地的{name}品质已经救回，不再是欠佳。")
    if result["kind"] == "growth_boost":
        body = _garden_action_text("fertilize_boost", plot=plot, name=name)
        if result["ripened"]:
            tail = f"结果：{plot}地的{name}这一下直接催熟了，可以收获。"
        else:
            estimated_ready_at = result.get("estimated_ready_at")
            ready_text = (
                datetime.fromisoformat(estimated_ready_at).astimezone(TZ).strftime("%m月%d日 %H:%M")
                if estimated_ready_at else "暂时估不出来"
            )
            tail = (
                f"结果：{plot}地的{name}这茬已追肥{result['fertilize_count']}"
                f"/{garden.FERTILIZE_MAX_PER_CYCLE}次，预计{ready_text}熟。"
            )
        return _garden_with_outing(f"{body}\n{tail}")
    body = _garden_action_text("fertilize_unrecoverable", plot=plot, name=name)
    return _garden_with_outing(f"{body}\n结果：{plot}地状态未改变，品相仍是欠佳。")


def _garden_duration_text(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    days, remainder = divmod(total_seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}天{hours}小时{minutes}分{seconds}秒"


def _garden_water_timing_text(result: dict) -> str:
    timing = result.get("timing_after")
    if not isinstance(timing, dict):
        return ""
    remaining = timing.get("current", {}).get("remaining_seconds")
    if not isinstance(remaining, int):
        return ""
    detail = f"计时已刷新：按当前条件预计还有{_garden_duration_text(remaining)}成熟"
    saved = result.get("time_saved_seconds")
    if isinstance(saved, int) and saved > 0:
        detail += f"；本次预计省下{_garden_duration_text(saved)}"
    return f"\n{detail}。"


def _garden_water_crop_text(
    result: dict,
    *,
    use_writer: bool = False,
    outing: dict | None = None,
) -> str:
    plot = garden.plot_label(result["plot"]["plot_id"])
    name = garden.garden_crops.crop_name(result["plot"]["crop_id"])
    outcome = result["outcome"]

    def copy_body(fallback: str) -> str:
        return (
            garden.watering_copy(result, fallback=fallback, outing=outing)
            if use_writer else fallback
        )

    def crop_copy(action: str, fallback: str) -> str:
        local_pool = garden.garden_content.crop_care_pool(
            result["plot"]["crop_id"], action,
        )
        if local_pool:
            return random.choice(local_pool)
        return copy_body(fallback)

    if outcome == "waterlogged":
        body = copy_body(f"第三遍水下去，{name}根边的土面已经浮起一层水光。")
        return (
            f"{body}\n"
            f"结果：{plot}地已进入积水状态，{name}暂停生长；需要松土。"
        )
    if outcome == "blocked_by_condition":
        condition_name = garden.CONDITION_LABELS[result["condition_type"]]
        body = copy_body(
            f"水壶停在{name}根边，院子里已有的{condition_name}先拦住了这次浇水。"
        )
        return (
            f"{body}\n"
            f"结果：本次没有建立第二个异常；院子仍需先处理{condition_name}。"
        )
    if outcome in ("already_waterlogged", "refused"):
        condition_type = result.get("condition_type")
        if condition_type:
            condition_name = garden.CONDITION_LABELS[condition_type]
            detail = f"{plot}地仍是{condition_name}状态"
        else:
            detail = f"{plot}地今天已经浇过三次"
        body = crop_copy(
            "water_repeat", f"水壶停在{plot}地边，没有再往{name}根边倒水。",
        )
        return (
            f"{body}\n"
            f"结果：拒绝继续浇水；{detail}，没有新增生长加成。"
        )
    if outcome == "protest":
        fallback = random.choice(_WATER_PROTEST_TEXT[result["style"]]).format(
            name=name,
        )
        body = crop_copy("water_repeat", fallback)
        return (
            f"{body}\n"
            "结果：今天已经浇过一次，本次没有生长加成；土仍然很湿。"
        )
    if outcome in ("no_need", "too_wet"):
        level = garden.garden_weather.moisture_label(float(result["moisture_before"]))
        detail = "土壤仍合适，先没有倒水" if outcome == "no_need" else "土壤已经湿润，先没有倒水"
        body = crop_copy(
            "water_repeat", f"{name}根边的土还是{level}的，水壶在边上停了停。",
        )
        return f"{body}\n结果：{plot}地{detail}；本次没有改变土壤水分。"
    condition_type = result.get("condition_type")
    if condition_type:
        condition_name = garden.CONDITION_LABELS[condition_type]
        body = crop_copy(
            "water",
            f"给{name}浇了今天第一遍水，湿土在根边慢慢洇开，"
            f"原来的{condition_name}仍留在枝叶上。",
        )
        return (
            f"{body}\n"
            f"结果：本次没有解除{condition_name}，{name}继续暂停生长；"
            f"需要{garden.CONDITION_ACTIONS[condition_type]}。"
        )
    if "moisture_after" in result:
        fallback = f"水慢慢渗进{name}根边发浅的土里。"
        body = crop_copy("water", fallback)
        return (
            f"{body}\n"
            f"结果：{plot}地从{garden.garden_weather.moisture_label(float(result['moisture_before']))}补到湿润；本次浇水有效。"
            f"{_garden_water_timing_text(result)}"
        )
    fallback = _garden_action_text("water" if result["accelerated"] else "water_dormant", name=name)
    body = crop_copy("water", fallback)
    result_line = (
        "结果：今天第一次浇水，已获得一次小幅生长加成。"
        if result["accelerated"]
        else "结果：今天第一次浇水；当前不在生长季，没有增加生长进度。"
    )
    return f"{body}\n{result_line}{_garden_water_timing_text(result)}"


def _garden_water_batch_text(results: list[dict]) -> str:
    """把批量浇水结果按规则归类，避免多块地重复返回同一句。"""
    grouped: dict[tuple[str, str | None], list[str]] = {}
    order: list[tuple[str, str | None]] = []
    for result in results:
        outcome = result["outcome"]
        condition_type = result.get("condition_type")
        if outcome == "watered":
            if condition_type:
                key = ("condition", condition_type)
            elif "moisture_after" in result:
                key = ("watered", None)
            elif result["accelerated"]:
                key = ("accelerated", None)
            else:
                key = ("dormant", None)
        elif outcome in ("blocked_by_condition", "already_waterlogged"):
            key = (outcome, condition_type)
        elif outcome == "refused":
            key = ("refused", condition_type)
        else:
            key = (outcome, None)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(garden.plot_label(result["plot"]["plot_id"]))

    def plot_group(labels: list[str]) -> str:
        return "、".join(label.removesuffix("号") for label in labels) + "号地"

    poured = any(result["outcome"] in ("watered", "waterlogged") for result in results)
    lines = [
        "选中的菜畦已经逐块处理，实际倒水的结果列在下面。"
        if poured else "选中的菜畦这次都没有倒水。",
        "结果：",
    ]
    for category, condition_type in order:
        plots = plot_group(grouped[(category, condition_type)])
        if category == "accelerated":
            detail = "今天第一次浇水，获得一次小幅生长加成。"
        elif category == "watered":
            detail = "土壤已经补到湿润，本次浇水有效。"
        elif category == "dormant":
            detail = "今天第一次浇水，但当前不在生长季，没有增加生长进度。"
        elif category == "protest":
            detail = "今天已经浇过一次，本次没有生长加成，土仍然很湿。"
        elif category == "no_need":
            detail = "土壤仍合适，本次没有倒水。"
        elif category == "too_wet":
            detail = "土壤已经湿润，本次没有倒水。"
        elif category == "condition":
            name = garden.CONDITION_LABELS[condition_type]
            action = garden.CONDITION_ACTIONS[condition_type]
            detail = f"浇水没有解除{name}，作物继续暂停生长；需要{action}。"
        elif category == "waterlogged":
            detail = "第三遍浇水后进入积水状态，暂停生长；需要松土。"
        elif category == "blocked_by_condition":
            name = garden.CONDITION_LABELS[condition_type]
            detail = f"再次浇水的意图已被拦住，没有建立第二个异常；院子仍需先处理{name}。"
        elif category == "already_waterlogged":
            detail = "仍是积水状态，已拒绝继续浇水，没有新增生长加成。"
        elif condition_type:
            name = garden.CONDITION_LABELS[condition_type]
            detail = f"已拒绝继续浇水，仍是{name}状态，没有新增生长加成。"
        else:
            detail = "今天已经浇过三次，已拒绝继续浇水，没有新增生长加成。"
        lines.append(f"- {plots}：{detail}")
    for result in results:
        if result.get("outcome") != "watered":
            continue
        timing_text = _garden_water_timing_text(result).strip()
        if timing_text:
            label = garden.plot_label(result["plot"]["plot_id"])
            lines.append(f"- {label}地{timing_text}")
    return "\n".join(lines)


def _garden_plot_batch_selectors(value: str) -> list[str] | None:
    """返回批量地块；空列表代表全部，None 代表原有单目标语义。

    分隔符要跟 `_GARDEN_BATCH_SEP` 同一套自然连接词——8/4 真实事故：
    "浇水一号地和三号地"里的"和"此前不算分隔符，两块地被当成一个
    不存在的选择器，报错"现在没有能『浇水』的...菜畦"。
    """
    value = value.strip()
    if value in ("全部", "全", "所有"):
        return []
    tokens = [
        token for token in _GARDEN_BATCH_SEP.split(value)
        if token
    ]
    if len(tokens) > 1:
        return tokens
    if len(tokens) == 1 and re.fullmatch(r"[1-4]{2,4}", tokens[0]):
        return list(tokens[0])
    return None


def _garden_catalog_mention(value: str, catalog: dict) -> str | None:
    compact = normalize(value)
    candidates: list[tuple[int, str]] = []
    for item_id, item in catalog.items():
        names = (item_id, str(item.get("name") or ""), *item.get("aliases", ()))
        for name in names:
            normalized = normalize(str(name))
            if normalized and normalized in compact:
                candidates.append((len(normalized), item_id))
    if not candidates:
        return None
    return max(candidates)[1]


def _garden_plot_mentions(value: str) -> list[str]:
    compact = normalize(value)
    if any(word in compact for word in ("全部", "所有", "每块", "全都")):
        return ["全部"]
    mapping = {"一": "1", "二": "2", "三": "3", "四": "4"}
    found: list[tuple[int, str]] = []
    for match in re.finditer(r"p([1-4])", compact):
        found.append((match.start(), match.group(1)))
    for match in re.finditer(r"([一二三四1-4])(?:号)?(?:菜畦|地块|地)", compact):
        found.append((match.start(), mapping.get(match.group(1), match.group(1))))
    for match in re.finditer(r"([1-4]{2,4})(?:号)?(?:菜畦|地块|地)", compact):
        found.extend((match.start() + offset, digit) for offset, digit in enumerate(match.group(1)))
    if not found:
        # CLI风格裸数字地块写法，如"种 黄瓜 3"/"查看 2 4"/"查看2和4"，没有
        # "号/菜畦/地块"后缀时以上规则识别不到，会被误判成"没带编号"。
        # 8/6 真实事故：`home 院子 种 黄瓜 3` 报错"请带上地块编号"。
        tail_match = re.search(r"(?:[1-4]|[，,、/]|和|与|跟)+$", compact)
        if tail_match:
            tail = tail_match.group(0)
            batch = _garden_plot_batch_selectors(tail)
            if batch:
                found.extend((tail_match.start() + offset, digit) for offset, digit in enumerate(batch))
            elif re.fullmatch(r"[1-4]", tail):
                found.append((tail_match.start(), tail))
    result: list[str] = []
    for _position, selector in sorted(found):
        if selector not in result:
            result.append(selector)
    return result


def _garden_animal_mention(value: str) -> str | None:
    compact = normalize(value)
    matches: list[tuple[int, str]] = []
    try:
        animals = [entry for entry in garden.active_entries() if entry.get("kind") == "animal"]
    except garden.GardenError:
        return None
    for animal in animals:
        selectors = (
            str(animal.get("nickname") or ""), str(animal.get("species") or ""),
            garden.display_name(animal, include_species=True), str(animal.get("id") or ""),
        )
        for selector in selectors:
            normalized = normalize(selector)
            if normalized and normalized in compact:
                matches.append((len(normalized), selector))
    return max(matches)[1] if matches else None


def _garden_chicken_mention(value: str) -> str | None:
    compact = normalize(value)
    matches: list[tuple[int, str]] = []
    try:
        chicks = garden.coop_snapshot()["chicks"]
    except garden.GardenError:
        return None
    for chick in chicks:
        selectors = (
            str(chick.get("nickname") or ""), str(chick.get("id") or ""),
            garden.chicken_display_name(chick),
            garden.chicken_display_name(chick, include_kind=True),
        )
        for selector in selectors:
            normalized = normalize(selector)
            if normalized and normalized in compact:
                matches.append((len(normalized), selector))
    return max(matches)[1] if matches else None


def _garden_chicken_rename_pairs(raw: str) -> list[tuple[str, str]]:
    """按空格切块识别『编号叫新名字』，支持一条消息里连续给多只鸡取名。

    不能用 `_garden_chicken_mention` ——那个按昵称/完整 id 做子串匹配，
    刚孵出还没取名的鸡只有短编号前缀，需要单独按前缀实际对上鸡舍成员。
    """
    try:
        chicks = garden.coop_snapshot()["chicks"]
    except garden.GardenError:
        return []
    pairs: list[tuple[str, str]] = []
    for chunk in raw.split():
        match = re.match(r"^(.+?)(?:取名(?:叫|为)?|叫)(.+)$", chunk)
        if not match:
            continue
        selector, nickname = match.group(1), match.group(2)
        if any(garden.matches_chicken_selector(chick, selector) for chick in chicks):
            pairs.append((selector, nickname))
    return pairs


def _garden_egg_count_mention(value: str) -> int | None:
    compact = normalize(value)
    match = re.search(r"([1-9一二两三四五六七八九俩])(?:个|枚)?(?:鸡蛋|蛋)", compact)
    if match is None:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token)
    return {
        "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3,
        "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }[token]


def _garden_meal_request(value: str) -> tuple[str, int | None]:
    egg_count = _garden_egg_count_mention(value)
    if egg_count is None:
        return value.strip(), None
    cleaned = re.sub(
        r"[1-9一二两三四五六七八九俩](?:个|枚)?(?:鸡蛋|蛋)", "", value, count=1,
    )
    cleaned = re.sub(r"[\s,，]*(?:放|用|加|打|要)[\s,，]*$", "", cleaned)
    return cleaned.strip(" ，,"), egg_count


def _garden_semantic_command(raw: str) -> str:
    """把常见自然说法收敛成现有白名单命令；只做本地确定性解析。"""
    raw = raw.strip()
    compact = normalize(raw)
    tokens = raw.split()
    if not compact:
        return raw

    # 鸡舍打扫要抢在下面"已经是合法的标准形式"快速放行、以及"清理"等
    # 作物别名判断之前拦截——"清理鸡舍"这类说法含有"清理"两个字，若不
    # 提前处理会被 prefixed_action 早退分支直接放行成"清理鸡舍"原样传
    # 下去，落进作物「清理」(clear_withered_crop) 而不是鸡舍打扫。命中
    # 规则用确定性关键词收窄，不猜：含"粪"/"粑粑"/"铲屎"任一，或
    # "打扫"/"清扫"与鸡舍类名词同现，才路由到鸡舍打扫；鸡舍类名词单独
    # 配上"清理"这个跟作物清理撞词的动词时，宁可报错也不猜着执行。
    _coop_noun = any(word in compact for word in ("鸡舍", "鸡窝", "鸡圈"))
    # 光出现"粪/粑粑"不代表要打扫——"看看鸡粪"是查询、"施粪肥/撒粪肥"
    # 是施肥的说法，都不能被截胡成真的执行打扫（这是会改状态的动作，
    # 宁可落进后面的别名判断或报错，也不猜）。必须清扫类动词与名词同现
    # 才算数："清理粪便/打扫粑粑/铲屎/清粪"都命中；动词表用"扫/清/铲/
    # 收拾"四个字根，覆盖打扫/清扫/清理/清粪这些组合。
    _manure_noun = any(word in compact for word in ("粪", "粑粑", "屎"))
    _clean_verb = any(word in compact for word in ("扫", "清", "铲", "收拾"))
    if (_manure_noun and _clean_verb) or (
        _coop_noun and any(word in compact for word in ("打扫", "清扫"))
    ):
        return "打扫鸡舍"
    if _coop_noun and any(word in compact for word in ("清理", "拔掉", "清掉")):
        raise HomeError("不确定是要打扫鸡舍还是清理菜畦，可以直接说：home 院子 打扫鸡舍")

    # 已经是合法的标准形式时原样放行，避免兜底重排参数。
    first = normalize(tokens[0]) if tokens else ""
    exact_lengths = {
        "查看": (1, 2), "看看": (1, 2), "状态": (1, 2),
        "播种": (2, 3), "收获": (1, 2), "做点吃的": tuple(range(2, 20)),
        "送给user": tuple(range(2, 20)), "送给另一只agent": tuple(range(2, 20)),
        "取名": tuple(range(3, 20)),
        "喂鸡": (1, 2), "摸摸鸡": (1, 2), "鸡取名": tuple(range(3, 20, 2)),
        "食谱": (1, 2), "菜谱": (1, 2),
    }
    if first in exact_lengths and len(tokens) in exact_lengths[first]:
        return raw
    prefixed_action = next((action for action in (
        "浇水", "除虫", "修剪", "松土", "施肥", "清理", "投喂", "摸摸", "陪玩",
    ) if compact.startswith(action)), None)
    if prefixed_action and not (
        prefixed_action in {"投喂", "摸摸"} and _garden_chicken_mention(raw)
    ):
        return raw

    # 仓库/能种什么都要先于下面的"查看"通用兜底判断，否则"查看仓库"这类说法
    # 会被"查看"关键词截胡，落进查看某地块的逻辑，而不是各自专属的处理。
    if any(word in compact for word in ("仓库", "库房")):
        return "仓库"
    if any(phrase in compact for phrase in (
        "现在能种什么", "还能种什么", "还能种啥", "能种什么", "能种啥",
        "可以种什么", "可以种啥", "查看能种的", "能种的种子",
    )):
        return "能种什么"

    # 第七版新增：院级「排水」要先于下面的「松土」别名判断——"清排水沟"/
    # "排排水"/"疏通排水"都含有「排水」这两个字，若不提前拦截会被"松土"
    # 别名表里的"排水"子串截胡，误判成给单块地治积水异常。
    if any(phrase in compact for phrase in (
        "排水", "清排水沟", "清水沟", "排排水", "疏通排水",
    )):
        return "排水"

    # 鸡舍故事的选择优先级很高，但完整菜名（如番茄炒蛋）必须先排除，
    # 不能因为同时出现“鸡蛋/炒”就误判成吃掉故事里的三枚蛋。
    mentioned_recipe_id = _garden_catalog_mention(raw, garden.garden_crops.RECIPES)
    if "可孵化" in compact and any(verb in compact for verb in ("孵", "孵化", "我来孵")):
        return "可孵化鸡蛋 孵化"
    if any(noun in compact for noun in ("鸡舍", "鸡窝")):
        if any(verb in compact for verb in ("建", "搭", "盖", "造", "弄一个", "做一个")):
            return "建鸡舍"
        if any(verb in compact for verb in ("看", "状态", "怎么样", "进度")):
            return "查看鸡舍"
    choice_open = False
    if any(word in compact for word in ("鸡蛋", "那三枚蛋", "三枚蛋")) or compact in {
        "孵", "孵化", "孵吧", "我来孵", "吃掉", "吃了吧", "煮了吧",
    }:
        try:
            choice_open = garden.coop_snapshot()["story_status"] == "awaiting_choice"
        except garden.GardenError:
            choice_open = False
    if mentioned_recipe_id is None and (choice_open or "鸡蛋" in compact or "三枚蛋" in compact):
        if any(verb in compact for verb in ("孵", "留下", "留着", "不吃")):
            return "鸡蛋 孵化"
        if any(verb in compact for verb in ("吃", "煮", "炒", "做掉")):
            return "鸡蛋 吃掉"

    chicken = _garden_chicken_mention(raw)
    if any(word in compact for word in ("喂鸡", "喂小鸡", "喂母鸡", "喂公鸡")) or (
        chicken and any(word in compact for word in ("喂一下", "喂点", "喂食", "投喂"))
    ):
        return "喂鸡" + (f" {chicken}" if chicken else "")
    if any(word in compact for word in ("摸摸鸡", "摸小鸡", "摸母鸡", "摸公鸡")) or (
        chicken and any(word in compact for word in ("摸摸", "摸一下", "摸一摸"))
    ):
        return "摸摸鸡" + (f" {chicken}" if chicken else "")
    if "取名" in compact or "叫" in compact:
        rename_pairs = _garden_chicken_rename_pairs(raw)
        if rename_pairs:
            args = " ".join(f"{selector} {nickname}" for selector, nickname in rename_pairs)
            return f"鸡取名 {args}"

    if any(word in compact for word in ("篮子", "种子盒", "有什么种子")):
        return "篮子"
    if any(word in compact for word in ("手账", "手帳")):
        return "手账"
    if any(word in compact for word in ("逛", "溜达", "转转", "散步")):
        return "逛逛"
    if any(word in compact for word in ("以前来过", "离开的动物", "院子历史")):
        return "记录"

    crop_id = _garden_catalog_mention(raw, garden.garden_crops.CROPS)
    crop_name = garden.garden_crops.crop_name(crop_id) if crop_id else None
    recipe_id = mentioned_recipe_id
    if recipe_id is None:
        if "黄瓜" in compact and "拍" in compact:
            recipe_id = "smashed_cucumber"
        elif "黄瓜" in compact and "拌" in compact:
            recipe_id = "cucumber_salad"
        elif "番茄" in compact and "拌" in compact:
            recipe_id = "chilled_tomato"
        elif "薄荷" in compact and "水" in compact:
            recipe_id = "mint_water"
        elif "西瓜" in compact and any(word in compact for word in ("切", "果盘")):
            recipe_id = "watermelon_slices"
    recipe_name = garden.garden_crops.RECIPES[recipe_id]["name"] if recipe_id else None
    plots = _garden_plot_mentions(raw)
    plot_arg = " ".join(plots)

    if any(
        phrase in compact
        for phrase in ("能做什么", "可以做什么", "能做啥", "可以做啥", "做什么菜", "做啥菜", "有什么菜谱", "有什么食谱")
    ):
        if "鸡蛋" in compact or compact.startswith("蛋"):
            return "食谱 鸡蛋"
        if crop_name:
            return f"食谱 {crop_name}"
        return "食谱 大全"
    if any(phrase in compact for phrase in ("食谱大全", "菜谱大全", "全部食谱", "全部菜谱", "所有食谱", "所有菜谱")):
        return "食谱 大全"

    if recipe_name and any(word in compact for word in ("做", "制作", "料理", "准备", "弄", "拌", "拍", "切", "泡", "炒")):
        egg_count = _garden_egg_count_mention(raw)
        recipe = garden.garden_crops.RECIPES.get(recipe_id or "", {})
        suffix = f" {egg_count}个蛋" if recipe.get("egg_range") and egg_count is not None else ""
        return f"做点吃的 {recipe_name}{suffix}"
    if any(word in compact for word in ("做饭", "制作", "料理", "准备", "做点吃的", "弄点吃的")) and crop_name:
        return f"做点吃的 {crop_name}"
    if any(word in compact for word in ("送给user", "给user", "送user")):
        item_name = recipe_name or crop_name
        if item_name:
            return f"送给user {item_name}"
    if any(word in compact for word in ("送给另一只agent", "给另一只agent", "送另一只agent")):
        item_name = recipe_name or crop_name
        if item_name:
            return f"送给另一只agent {item_name}"
    is_bare_plant_verb = (
        "种子" not in compact and "品种" not in compact and "物种" not in compact
        and re.search(r"种|栽", compact) is not None
    )
    if crop_name and (
        any(word in compact for word in ("播种", "种下", "种到", "种进", "栽下", "栽到"))
        or is_bare_plant_verb
    ):
        return f"播种 {crop_name}" + (f" {plots[0]}" if plots and plots[0] != "全部" else "")
    if any(word in compact for word in ("浇水", "浇点水", "浇一下", "补水", "浇浇")):
        return "浇水" + (f" {plot_arg}" if plot_arg else "")
    if any(word in compact for word in ("收获", "摘下来", "摘掉", "采下来", "采摘")):
        selector = plots[0] if plots and plots[0] != "全部" else crop_name
        return "收获" + (f" {selector}" if selector else "")

    treatment_aliases = (
        ("除虫", ("除虫", "抓虫", "杀虫")),
        ("修剪", ("修剪", "剪病叶", "剪叶")),
        ("松土", ("松土", "排积水")),
        ("施肥", ("施肥", "加肥", "补肥")),
        ("清理", ("清理", "拔掉", "清掉")),
    )
    for action, aliases in treatment_aliases:
        if any(alias in compact for alias in aliases):
            selector = plots[0] if plots and plots[0] != "全部" else crop_name
            return action + (f" {selector}" if selector else "")

    animal = _garden_animal_mention(raw)
    if any(word in compact for word in ("投喂", "喂点", "喂一点", "给它吃")):
        args = " ".join(item for item in (animal, crop_name) if item)
        return "投喂" + (f" {args}" if args else "")
    if any(word in compact for word in ("摸摸", "摸一下", "摸一摸", "撸一下")):
        return "摸摸" + (f" {animal}" if animal else "")
    if any(word in compact for word in ("陪玩", "玩一会", "玩一下", "陪它玩")):
        return "陪玩" + (f" {animal}" if animal else "")

    view_triggers = ("查看", "看看", "看一下", "瞧瞧", "怎么样", "什么样")
    if any(word in compact for word in view_triggers):
        if any(word in compact for word in ("详细", "详情")):
            return "查看 详细"
        arg = plot_arg if plots and plots[0] != "全部" else ""
        if not arg:
            # "查看 2 4"/"查看2和4"这类不带"号/地"后缀的裸数字批量说法，
            # `_garden_plot_mentions`识别不了；退回用批量选择器解析剩余部分。
            trigger = next(word for word in view_triggers if word in compact)
            tail = raw.replace(trigger, "", 1).strip()
            tail_batch = _garden_plot_batch_selectors(tail)
            if tail_batch:
                arg = " ".join(tail_batch)
            elif tail and garden.plot_id_from_selector(tail) is not None:
                arg = tail
        return "查看" + (f" {arg}" if arg else "")
    return raw


def handle_garden(request: Request) -> str:
    if request.help:
        return garden_help(request.raw_text)
    raw_text = _garden_semantic_command(request.raw_text)
    text = normalize(raw_text)
    try:
        if text in ("", "查看", "看看", "状态"):
            return _garden_list_active()
        if text in ("查看详细", "查看详情", "详细", "详情", "查看全部", "全部"):
            return _garden_list_active_detailed()
        if text in ("鸡舍", "查看鸡舍", "鸡舍状态"):
            return _garden_coop_status()
        if text == "打扫鸡舍":
            if request.dry_run:
                return "将打扫鸡舍，把攒下的鸡粪收进堆肥角"
            result = garden.care_chicken(None, "clean")
            units = result["units"]
            fertilizer_units = result["fertilizer_units"]
            log_store.write_log("info", "activity", f"小院子：打扫鸡舍，收走鸡粪×{units}")
            # 第九版起 care_chicken(clean) 不足一堆（MANURE_PER_FERTILIZER）
            # 已经在 garden.py 里直接 raise，不会再返回 units<=0 的结果，
            # 这里不用再兜底判断（旧版 coop_clean_empty 分支已删，文案池
            # 保留不动，以防以后又有别的路数复用）。
            body = _garden_action_text("coop_clean", units=units)
            return _garden_with_outing(
                f"{body}\n结果：鸡粪×{units}已经收进堆肥角，约三天沤出肥料×{fertilizer_units}。"
            )
        if text == "建鸡舍":
            coop = garden.coop_snapshot()
            if coop["built"]:
                return "鸡舍已经搭好了，不会重复建造。\n" + _garden_coop_status()
            if coop["story_status"] == "awaiting_choice":
                return "三枚鸡蛋正在等 agent 决定；选择孵化时才会搭起鸡舍。"
            return "现在还没有需要孵化的鸡蛋，不会提前搭鸡舍；要等小动物先把鸡蛋送来。"
        if text in ("鸡蛋吃掉", "吃掉鸡蛋"):
            if request.dry_run:
                return "将吃掉鸡舍故事里的三枚鸡蛋；这次选择不可撤销"
            result = garden.resolve_coop_egg_choice("eat")
            log_store.write_log("info", "activity", "小院子：agent 选择吃掉三枚鸡蛋")
            return _garden_with_outing(
                f"三枚鸡蛋已经从篮子里取出并吃掉了（鸡蛋×{result['egg_count']}）。这次选择已经落定。"
            )
        if text in ("鸡蛋孵化", "孵化鸡蛋"):
            if request.dry_run:
                return "将先搭起鸡舍，再把三枚鸡蛋放进软窝，由 agent 亲自孵21小时；这次选择不可撤销"
            result = garden.resolve_coop_egg_choice("incubate")
            hatch_at = datetime.fromisoformat(result["coop"]["hatch_at"]).astimezone(TZ)
            log_store.write_log("info", "activity", "小院子：agent 开始亲自孵三枚鸡蛋")
            return _garden_with_outing(
                "agent 为三枚鸡蛋搭好了鸡舍，把它们放进软窝，开始亲自孵蛋。"
                f"预计约在{hatch_at.strftime('%m月%d日 %H:%M')}出壳，整个过程21小时。"
            )
        if text in ("可孵化鸡蛋孵化", "孵化可孵化鸡蛋"):
            if request.dry_run:
                return "将从篮子取出可孵化鸡蛋×1，由 agent 亲自孵21小时"
            result = garden.start_hatchable_egg_incubation()
            hatch_at = datetime.fromisoformat(result["coop"]["hatch_at"]).astimezone(TZ)
            log_store.write_log("info", "activity", "小院子：agent 开始孵一枚可孵化鸡蛋")
            return _garden_with_outing(
                "可孵化鸡蛋×1已经从篮子放进软窝，agent 开始亲自孵蛋。"
                f"预计约在{hatch_at.strftime('%m月%d日 %H:%M')}出壳，整个过程21小时。"
            )
        raw_tokens = raw_text.strip().split()
        if raw_tokens and raw_tokens[0] in ("喂鸡", "摸摸鸡"):
            if len(raw_tokens) > 2:
                raise HomeError(f"请说『home 院子 {raw_tokens[0]} [鸡的名字或编号]』")
            selector = raw_tokens[1] if len(raw_tokens) == 2 else None
            action = "feed" if raw_tokens[0] == "喂鸡" else "pet"
            if request.dry_run:
                return f"将给 {selector or '唯一一只鸡'} {'喂基础饲料' if action == 'feed' else '轻轻摸摸'}"
            result = garden.care_chicken(selector, action)
            chick = result["chick"]
            name = garden.chicken_display_name(chick)
            kind = garden.chicken_kind(chick)
            pool = (
                garden.garden_content.CHICKEN_FEED_TEXT
                if action == "feed" else garden.garden_content.CHICKEN_PET_TEXT
            )
            body = random.choice(pool).format(name=name, kind=kind)
            weather_flavor = garden._animal_weather_flavor(
                "家禽", str(result.get("weather_mode") or "normal"), random,
            )
            if weather_flavor:
                body = f"{body} {weather_flavor}"
            count = chick["feed_count"] if action == "feed" else chick["pet_count"]
            log_store.write_log("info", "activity", f"小院子：给{name}{'喂食' if action == 'feed' else '摸摸'}")
            return _garden_with_outing(
                f"{body}\n结果：{name}{'喂食' if action == 'feed' else '摸摸'}成功，累计×{count}。"
            )
        if raw_tokens and raw_tokens[0] == "鸡取名":
            rest = raw_tokens[1:]
            if len(rest) < 2 or len(rest) % 2 != 0:
                raise HomeError(
                    "请说『home 院子 鸡取名 <鸡的名字或编号> <新名字>』；"
                    "多只鸡可以连续写：鸡取名 <编号1> <新名字1> <编号2> <新名字2>"
                )
            pairs = [(rest[i], rest[i + 1]) for i in range(0, len(rest), 2)]
            if request.dry_run:
                return "、".join(f"将把 {selector} 取名为『{nickname}』" for selector, nickname in pairs)
            lines = []
            for selector, nickname in pairs:
                chick = garden.name_chicken(selector, nickname)
                name = garden.chicken_display_name(chick)
                body = random.choice(garden.garden_content.CHICKEN_NAME_TEXT).format(
                    name=name, kind=garden.chicken_kind(chick),
                )
                log_store.write_log("info", "activity", f"小院子：给鸡取名为『{name}』")
                lines.append(f"{body}\n结果：取名成功，现在叫{name}。")
            return _garden_with_outing("\n".join(lines))
        plot_view = re.fullmatch(r"(?:查看|看看|状态)(.+)", text)
        if plot_view:
            return _garden_view_plot(plot_view.group(1))
        if text in ("逛逛", "转转"):
            if request.dry_run:
                return "将查看此刻的小院子整体画面"
            return _garden_with_outing(garden.stroll_scene())
        if text in ("记录", "历史", "曾经"):
            return _garden_list_left()
        if text in ("手账", "手帳"):
            return _garden_journal()
        if text in ("篮子", "种子盒"):
            return _garden_basket()
        if text.startswith("食谱") or text.startswith("菜谱"):
            return _garden_recipe_lookup(raw_text.strip()[2:])
        if text == "能种什么":
            return _garden_plantable_now()
        if text == "仓库":
            return _garden_warehouse()
        if text == "排水":
            if request.dry_run:
                return "将疏通排水沟，帮院子加速退水"
            result = garden.drain_yard()
            level_label = {"puddles": "水洼", "flooded": "内涝"}.get(
                result["yard_water"], result["yard_water"],
            )
            log_store.write_log("info", "activity", "小院子：排水")
            body = _garden_action_text("drain")
            body += (
                f"\n结果：排水沟已经清好，院子目前是{level_label}"
                f"（连雨{result['rain_streak_days']}天），接下来会退得快一些。"
            )
            return _garden_with_outing(body)

        if text.startswith("播种"):
            if len(raw_tokens) not in (2, 3):
                raise HomeError("请说『home 院子 播种 <种子名> [地块编号]』")
            if request.dry_run:
                return f"将把 {raw_tokens[1]} 播进{raw_tokens[2] if len(raw_tokens) == 3 else '唯一的空菜畦'}"
            plot = garden.plant_crop(raw_tokens[1], raw_tokens[2] if len(raw_tokens) == 3 else None)
            body = _garden_action_text(
                "plant",
                crop_id=plot["crop_id"],
                plot=garden.plot_label(plot["plot_id"]),
                name=garden.garden_crops.crop_name(plot["crop_id"]),
            )
            notice = plot.get("season_exit_notice")
            return _garden_with_outing(f"{body}{notice}" if notice else body)
        if text.startswith("收获"):
            if len(raw_tokens) > 2:
                raise HomeError("请说『home 院子 收获 [地块编号]』")
            if request.dry_run:
                return "将收获唯一成熟的菜畦" if len(raw_tokens) == 1 else f"将收获{raw_tokens[1]}菜畦"
            result = garden.harvest_crop(raw_tokens[1] if len(raw_tokens) == 2 else None)
            name = garden.garden_crops.crop_name(result["crop_id"])
            log_store.write_log("info", "activity", f"小院子：收获 {name}")
            if result.get("quality") == "poor":
                # 欠佳收获：走通用欠佳池，不查作物专属池（专属池都是夸品相
                # 的），15%彩蛋在此不触发（彩蛋是"品相意外地好"的惊喜，
                # 与欠佳事实冲突）；"结果："行说明入的是欠佳堆（设计稿第六节3）。
                body = (
                    random.choice(garden.garden_content.HARVEST_POOR_TEXT).format(name=name)
                    + f"\n结果：{name}×{result['amount']}（欠佳）已收进篮子的欠佳堆，种子返还×{result['seed_return']}。"
                )
            elif random.random() < 0.15:
                # 收获彩蛋：纯文案惊喜，不追踪品质、不影响篮子和后续做菜。
                body = (
                    random.choice(garden.garden_content.HARVEST_SURPRISE_TEXT).format(name=name)
                    + f"\n结果：{name}×{result['amount']}已进篮子，种子返还×{result['seed_return']}。"
                )
            else:
                body = _garden_action_text(
                    "harvest",
                    crop_id=result["crop_id"],
                    amount=result["amount"],
                    name=name,
                    seed_return=result["seed_return"],
                )
            recipe_ids = garden.garden_crops.recipes_for_crop(result["crop_id"])
            if recipe_ids:
                recipe_names = "、".join(
                    garden.garden_crops.RECIPES[recipe_id]["name"] for recipe_id in recipe_ids
                )
                body += f"\n可以做：{recipe_names}。"
            return _garden_with_outing(body)
        if text.startswith("做点吃的"):
            value = raw_text.strip()[len("做点吃的"):].strip()
            if not value:
                raise HomeError("请说『home 院子 做点吃的 <食谱或作物名>』")
            recipe_value, egg_count = _garden_meal_request(value)
            if request.dry_run:
                recipe_id = garden.garden_crops.resolve_recipe(recipe_value)
                recipe = garden.garden_crops.RECIPES.get(recipe_id or "", {})
                if recipe.get("egg_range") and egg_count is None:
                    raise HomeError(f"请说清楚{recipe.get('name', recipe_value)}想放1～3个蛋")
                egg_note = f"，放鸡蛋×{egg_count}" if egg_count is not None else ""
                return f"将用『{recipe_value}』做一点吃的{egg_note}"
            result = garden.make_meal(recipe_value, egg_count=egg_count)
            name = garden.garden_crops.RECIPES[result["recipe_id"]]["name"]
            log_store.write_log("info", "activity", f"小院子：做了{name}")
            body = _garden_action_text("meal", name=name, recipe_id=result["recipe_id"])
            if result["egg_count"] is not None:
                crops_used = "、".join(
                    f"{garden.garden_crops.crop_name(crop_id)}×{amount}"
                    for crop_id, amount in result["record"]["ingredients"].items()
                )
                return _garden_with_outing(
                    f"{body}\n结果：消耗{crops_used}、鸡蛋×{result['egg_count']}，"
                    "成品×1已放进篮子。"
                )
            return _garden_with_outing(body)
        if text.startswith("送给user"):
            value = raw_text.strip()[len("送给user"):].strip()
            if not value:
                raise HomeError("请说『home 院子 送给user <作物或成品名> [便签内容]』")
            item_name, note = _parse_gift_item_note(value)
            if request.dry_run:
                return f"将把『{item_name}』作为院子里的小礼物送给user"
            result = garden.give_to_baby(item_name, note=note)
            log_store.write_log("info", "activity", f"小院子：送给user {result['display_name']}")
            body = _garden_action_text("gift", name=result["display_name"])
            if note:
                body += f"\n便签：{note}"
            return _garden_with_outing(body)
        if text.startswith("送给另一只agent"):
            remaining = re.sub(r'^送给\s*[Ff][Aa][Tt][Hh][Oo][Mm]\s*', '', raw_text).strip()
            if not remaining:
                raise HomeError("请说『home 院子 送给另一只agent <作物或成品名> [便签内容]』")
            item_name, note = _parse_gift_item_note(remaining)
            if request.dry_run:
                return f"将把『{item_name}』送给另一只agent" + (f"，附便签：{note}" if note else "")
            result = garden.give_to_friend(item_name, note=note)
            log_store.write_log("info", "activity", f"小院子：送给另一只agent {result['display_name']}")
            _deliver_gift_to_basket(result["display_name"], note)
            body = _garden_action_text("gift_other", name=result["display_name"])
            if note:
                body += f"\n便签：{note}"
            return _garden_with_outing(body)

        crop_action = next(
            (
                action for action in (*garden.CONDITION_ACTIONS.values(), "清理")
                if raw_tokens and raw_tokens[0] == action
            ),
            None,
        )
        if crop_action is not None:
            if len(raw_tokens) > 2:
                raise HomeError(
                    f"请说『home 院子 {crop_action} [地块编号]』"
                )
            selector = raw_tokens[1] if len(raw_tokens) == 2 else None
            if request.dry_run:
                return (
                    f"将对{selector or '唯一可处理的菜畦'}执行『{crop_action}』"
                )
            if crop_action == "清理":
                result = garden.clear_withered_crop(selector)
                plot = garden.plot_label(result["plot_id"])
                name = garden.garden_crops.crop_name(result["crop_id"])
                return _garden_with_outing(
                    f"把{plot}地里枯死的{name}残株和根系清了出去。\n"
                    f"结果：{plot}地已清理为空地；没有返还种子或作物。"
                )
            if crop_action == "施肥":
                # 施肥统一改走 garden.fertilize_plot，内部按确定性优先级
                # 分流治缺肥/救品质/救不回/用不上；报错文案（需要肥料×1/
                # 地还泡着/用不上肥料）直接是 GardenError，走下面 except
                # 分支，这里只处理成功与"救不回"两类正常结果。出门天气句
                # 已经在 _garden_fertilize_result_text 内部按各分支的既有
                # 惯例附加过，这里不再重复 _garden_with_outing。
                return _garden_fertilize_result_text(garden.fertilize_plot(selector))
            result = garden.resolve_crop_condition(crop_action, selector)
            outing = _garden_claim_outing()
            return _garden_with_outing(
                _garden_crop_treatment_text(
                    result, use_writer=True, outing=outing,
                ),
                outing,
            )

        if text.startswith("取名"):
            match = re.match(r"^\s*取名\s+(\S+)\s+(.+?)\s*$", raw_text)
            if not match:
                raise HomeError("请说『home 院子 取名 <编号> <昵称>』")
            if request.dry_run:
                return f"将把 {match.group(1)} 取名为『{match.group(2)}』"
            entry = garden.name_entry(match.group(1), match.group(2))
            log_store.write_log("info", "activity", f"小院子：{entry['species']} 改名为『{garden.display_name(entry)}』")
            bond_note = (
                f" · 亲密 {entry['bond_points']}｜{garden.bond_level_name(entry['bond_level'])}"
                if entry.get("kind") == "animal" else ""
            )
            return _garden_with_outing(
                f"取好名字了：{entry['species']}现在叫"
                f"『{garden.display_name(entry)}』。{bond_note}"
            )

        action = next((a for a in _GARDEN_ACTIONS if text.startswith(a)), None)
        if action is None:
            raise HomeError(
                "没听懂要做什么，可以说『home 院子 查看/逛逛』、"
                "『home 院子 浇水/投喂/摸摸/陪玩 <编号>』或『home 院子 取名 <编号> <昵称>』"
            )
        id_prefix = text[len(action):].strip()
        if action in ("投喂", "摸摸", "陪玩") and id_prefix:
            batch_targets = _garden_animal_batch_selectors(id_prefix)
            if batch_targets is not None:
                if request.dry_run:
                    return f"将依次对 {'、'.join(batch_targets)} 执行『{action}』"
                lines = [
                    _garden_care_result_line(action, garden.care(selector, action))
                    for selector in batch_targets
                ]
                return _garden_with_outing("\n".join(lines))
        if action == "投喂" and len(raw_tokens) == 3:
            if request.dry_run:
                return f"将把{raw_tokens[2]}作为小零食给 {raw_tokens[1]}"
            result = garden.feed_animal_treat(raw_tokens[1], raw_tokens[2])
            entry = result["entry"]
            name = garden.garden_crops.crop_name(result["crop_id"])
            log_store.write_log("info", "activity", f"小院子：给 {garden.display_name(entry)} 一小份{name}")
            return _garden_with_outing(
                f"给{garden.display_name(entry)}留了一小份{name}。亲密 {entry['bond_points']}｜{garden.bond_level_name(entry['bond_level'])}"
            )
        if action == "浇水":
            batch_selectors = _garden_plot_batch_selectors(id_prefix)
            if request.dry_run:
                if batch_selectors is not None:
                    target = "所有生长中的菜畦" if not batch_selectors else "、".join(batch_selectors)
                    return f"将一次浇完{target}，并按结果归类"
                return f"将对 {id_prefix or '唯一可浇水的对象'} 执行『浇水』"
            if batch_selectors is not None:
                selectors = batch_selectors or None
                return _garden_with_outing(
                    _garden_water_batch_text(garden.water_crops(selectors))
                )
            garden.advance(datetime.now(TZ))
            legacy = [
                entry for entry in garden.active_entries()
                if entry.get("kind") == "flower"
                and (not id_prefix or garden.matches_entry_selector(entry, id_prefix))
            ]
            crop_state = garden.crop_snapshot()
            plot_id = garden.plot_id_from_selector(id_prefix)
            plots = [plot for plot in crop_state["plots"] if plot.get("status") == "growing" and (not id_prefix or plot.get("plot_id") == plot_id)]
            targets = [("legacy", entry) for entry in legacy] + [("plot", plot) for plot in plots]
            if not targets:
                raise HomeError("现在没有能『浇水』的旧花草或菜畦")
            if len(targets) > 1:
                raise HomeError("现在有不止一个能『浇水』的对象，请带上更明确的编号或 p1/p2/p3/p4")
            target_kind, target = targets[0]
            if target_kind == "legacy":
                id_prefix = target["id"]
            else:
                result = garden.water_crop(target["plot_id"])
                outing = _garden_claim_outing()
                return _garden_with_outing(
                    _garden_water_crop_text(
                        result, use_writer=True, outing=outing,
                    ),
                    outing,
                )
        if not id_prefix:
            garden.advance(datetime.now(TZ))
            candidates = [e for e in garden.active_entries() if action in garden.actions_for(e["kind"])]
            if not candidates:
                raise HomeError(f"现在没有能『{action}』的对象")
            if len(candidates) > 1:
                raise HomeError(f"现在有不止一个能『{action}』的对象，请带上编号")
            id_prefix = candidates[0]["id"]

        if request.dry_run:
            return f"将对 {id_prefix} 执行『{action}』"
        result = garden.care(id_prefix, action)
        return _garden_with_outing(_garden_care_result_line(action, result))
    except HomeError as exc:
        raise HomeError(_garden_failure_hint(raw_text, str(exc))) from exc
    except garden.GardenError as exc:
        raise HomeError(_garden_failure_hint(raw_text, str(exc))) from exc



def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        print("用法：home 院子 <命令>", file=sys.stderr)
        return 2
    # 跳过 "院子" 前缀（兼容带前缀和不带前缀两种调用方式）
    head = normalize(argv[0]) if argv else ""
    if head in ("院子", "小院子", "花园"):
        argv = argv[1:]
    help_requested = any(arg in ("-h", "--help", "帮助", "怎么用") for arg in argv)
    dry_run = "--dry-run" in argv
    raw_tokens = [arg for arg in argv if arg not in ("-h", "--help", "--dry-run")]
    raw = " ".join(raw_tokens).strip()
    remainder = normalize(raw)
    request = Request(
        domain="garden",
        text=remainder,
        raw_text=raw,
        help=help_requested,
        dry_run=dry_run,
    )
    try:
        output = handle_garden(request)
        if not request.help and not request.dry_run:
            intro = garden.claim_session_intro(os.environ.get("SESSION_ID"))
            if intro:
                output = f"[GARDEN] {intro}\n\n{output}"
        print(output)
        return 0
    except HomeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
