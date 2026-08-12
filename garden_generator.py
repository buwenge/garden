#!/usr/bin/env python3
"""极简小院子写手：生成初见、照顾反应、逛逛画面和作物短文案。

跟 dream_generator.py 是同一套模式（独立 0600 key、urllib 直连、不经过
任何工具/记忆库），但预算小得多——这里只是氛围闪现，不需要长文，失败了
上层 garden.py 会自动落回本地小词库，从不阻塞 agent 的自动 tick。
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

import garden_scene


ROOT = Path(os.environ.get("MINI_YARD_HOME", Path(__file__).parent))
API_KEY_FILE = Path(os.environ.get("GARDEN_API_KEY_FILE", ROOT / ".garden" / "api_key"))
API_BASE = os.environ.get("GARDEN_API_BASE", "https://api.deepseek.com/v1").rstrip("/")
API_MODEL = os.environ.get("GARDEN_MODEL") or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
API_TIMEOUT = float(os.environ.get("GARDEN_API_TIMEOUT", "8"))
API_MAX_TOKENS = max(60, min(400, int(os.environ.get("GARDEN_MAX_TOKENS", "200"))))
VISIBLE_MAX_CHARS = max(20, min(200, int(os.environ.get("GARDEN_MAX_CHARS", "90"))))
STROLL_MAX_CHARS = max(80, min(180, int(os.environ.get("GARDEN_STROLL_MAX_CHARS", "180"))))
CROP_COPY_MAX_CHARS = max(40, min(140, int(os.environ.get("GARDEN_CROP_COPY_MAX_CHARS", "120"))))
SPECIES_MAX_CHARS = 20


class GardenGeneratorError(Exception):
    pass


def safe_failure_category(exc: Exception) -> str:
    """把写手失败收敛成安全类别，不把正文或注入异常原样写进日志。"""
    message = str(exc)
    if "API 密钥" in message:
        return "configuration"
    if "HTTP " in message:
        return "http_error"
    if "连不上" in message or "超时" in message:
        return "transport_error"
    if "没有返回正文" in message or "没有返回可用" in message:
        return "empty_response"
    if "越界" in message or "截断" in message:
        return "length_error"
    if "格式" in message or "无法识别" in message:
        return "format_error"
    if "漏掉" in message:
        return "fact_omission"
    if "冲突" in message or "编造" in message or "改写" in message or "发明" in message:
        return "fact_conflict"
    if "禁用表达" in message or "不像场景描写" in message:
        return "blocked_content"
    if "写成了动作" in message:
        return "wrote_action_as_observation"
    if "不存在的作物" in message:
        return "invented_crop"
    if "写得过干" in message:
        return "soil_mismatch"
    if "预报写进了当前实况" in message:
        return "forecast_as_current"
    if "动物安全或关系" in message:
        return "animal_relationship_drift"
    if "让动物离开" in message:
        return "animal_left"
    if "第二个作物异常" in message:
        return "extra_condition"
    return "validation_error"


def _api_key() -> str:
    configured = os.environ.get("GARDEN_API_KEY", "").strip()
    if configured:
        return configured
    try:
        mode = stat.S_IMODE(API_KEY_FILE.stat().st_mode)
        if mode & 0o077:
            raise GardenGeneratorError("小院子写手的 API 密钥文件权限过宽，拒绝读取")
        return API_KEY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise GardenGeneratorError("小院子写手的 API 密钥文件无法读取") from exc


_ENCOUNTER_SYSTEM = """你是一个中文自然生态小写手，负责给一款文字游戏里随机出没在现实小院（院门与信箱旁、屋檐下与墙根、菜畦边）的城市小动物，现编一个具体物种和一句初见描述。
要求：
- 物种要具体（比如"三色堇""橘猫"），不要写"一朵花""一只猫"这种笼统说法。
- 小动物以常见、温和的城市猫狗兔为主，不生成危险、受保护或不适合接触的野生动物。
- 描述只写一句话，20到40个汉字，带一点画面感和生活气，不要说教、不要提AI/模型/程序/游戏。
- 用户消息会给出当前北京时间时段、季节和已确认天气标签。可以使用这些事实，
  但不得写出标签之外的雨雪、晴天、风、温度或冲突时段；没有天气标签就不要写天气。
- 只输出JSON，格式为 {"species": "...", "text": "..."}，不要输出其他内容。"""

_REACTION_SYSTEM = """你是同一款文字游戏里的小写手，负责写"照顾了一株植物/一只流浪动物之后"它的简短反应。
要求：
- 只写一句话，15到30个汉字，从它的视角写反应，不要说教、不要提AI/模型/程序/游戏。
- 如果被告知它此前有些疲惫，写出恢复精神后的可见变化。
- 只描述这次动作后的可见反应，不催促下一次照顾，不虚构其他动作，不使用"主人"。
- 不补写天气、季节、晨昏或其他没有提供的环境事实。
- 只输出纯文本反应本身，不要输出JSON、引号或其他说明。"""

_STROLL_SYSTEM = """你是同一座现实小院的中文实时场景写手。用户只会给你当前可见的最小结构化快照。
要求：
- 根据这一次快照现场写一段新的自然语言画面，不要复述字段名，不要索要或挑选本地候选。
- 只写快照已经给出的季节、时段、实况天气、土壤、作物、异常、动物与位置；没有的事实一律不猜。
- yard_water 只能按 none/puddles/flooded 原档表现，不能把水洼升级成内涝；rain_streak_days 可自然写成连雨多久，但不得据此升级雨势、风力或积水档位。
- 这是查看画面，不得写成浇水、施肥、松土、除虫、修剪、播种、收获等动作已经发生。
- 不得把预报写成实况，不得新增作物异常，不得让动物受伤、离开或改变亲密关系。
- 只写表现，不决定任何玩法结果；不要提AI、模型、程序、游戏、指令、数据或“主人”。
- 正文20到180个汉字，以完整中文标点结尾。
- 只输出JSON，且只能是 {"text":"现场生成的正文"}，不要输出其他字段、代码块或说明。"""

_CROP_COPY_SYSTEM = """你是同一座现实小院的中文实时短文案写手。用户只会给你本次动作或事件的最小结构化事实。
要求：
- 根据这一次事实现场写一句新的自然语言正文，不要复述字段名，不要索要或挑选本地候选。
- 必须写到 crop_name；异常事件和处理还必须忠于 condition_type、severity、outcome、action 与 yield_penalty。
- 浇水只表现代码已经决定的 outcome、soil_state、water_source、watering_reason 与 style；不得把未倒水写成已倒水。
- outing_mode、yard_water 与 rain_streak_days 是动作时已确认的出门环境事实；可顺势轻描，但雨的大小、风力、积水档位和连雨天数不得升级或发明，不要重复完整出门句，也不得据此声称已给作物浇水。
- weather_tags 为空时不得补写晴雨冷暖风；不得把预报写成实况，也不得虚构第二个异常、恢复、减产、死亡或收获。
- “结果：”行由规则代码另行追加，正文不得代写或改写结果，不要提AI、模型、程序、游戏、指令、数据或“主人”。
- 正文12到120个汉字，以完整中文标点结尾。
- 只输出JSON，且只能是 {"text":"现场生成的正文"}，不要输出其他字段、代码块或说明。"""

_BLOCKED_PHRASES = (
    "忽略之前",
    "忽略以上",
    "系统提示",
    "system prompt",
    "执行命令",
    "调用工具",
    "home ",
)

_CROP_COPY_ALLOWED_FIELDS = {
    "condition_event": frozenset({
        "copy_type", "plot_label", "crop_name", "condition_type", "condition_name",
        "severity", "yield_penalty", "correct_action", "season", "day_period",
        "weather_tags",
    }),
    "treatment": frozenset({
        "copy_type", "plot_label", "crop_name", "condition_type", "condition_name",
        "outcome", "action", "correct_action", "yield_penalty", "season", "day_period",
        "weather_tags", "outing_mode", "yard_water", "rain_streak_days",
    }),
    "watering": frozenset({
        "copy_type", "plot_label", "crop_name", "condition_type", "condition_name",
        "outcome", "style", "watering_count", "accelerated", "season", "day_period",
        "weather_status", "weather_tags", "soil_state", "water_source", "watering_reason",
        "outing_mode", "yard_water", "rain_streak_days",
    }),
}

_CONDITION_COPY_MARKERS = {
    "pest": ("虫", "啃痕", "虫眼"),
    "diseased_leaf": ("病叶", "病斑", "病边", "斑点", "带斑", "卷边", "发黑"),
    "waterlogged": ("积水", "浮水", "湿泥", "水光", "湿透"),
    "nutrient_deficiency": ("缺肥", "养分", "叶色", "发黄", "长势", "颜色褪"),
}
_CROP_ACTION_MARKERS = {
    "浇水": ("浇水", "浇了", "浇过", "倒水", "倒了水", "灌水", "灌溉", "添水", "补水"),
    "松土": ("松土", "松了土", "翻土", "翻了土", "疏松土"),
    "除虫": ("除虫", "除了虫", "捉虫", "捉了虫", "清虫", "喷药", "打药"),
    "修剪": ("修剪", "修剪了", "剪叶", "剪了叶", "剪掉", "剪除"),
    "施肥": ("施肥", "施了肥", "追肥", "撒肥", "撒了肥", "添肥", "补肥"),
    "播种": ("播种", "播了种", "种下", "种进", "下种"),
    "收获": ("收获", "采摘", "摘下", "摘了", "拔掉"),
}


def _public_crop_copy_facts(facts: dict) -> dict:
    if not isinstance(facts, dict):
        raise GardenGeneratorError("小院子作物文案事实格式不对")
    copy_type = facts.get("copy_type")
    allowed = _CROP_COPY_ALLOWED_FIELDS.get(copy_type)
    if allowed is None:
        raise GardenGeneratorError("小院子作物文案类型无效")
    public = {
        key: facts[key]
        for key in allowed
        if key in facts and facts[key] is not None
    }
    if not isinstance(public.get("crop_name"), str) or not public["crop_name"]:
        raise GardenGeneratorError("小院子作物文案缺少作物事实")
    if not isinstance(public.get("plot_label"), str) or not public["plot_label"]:
        raise GardenGeneratorError("小院子作物文案缺少地块事实")
    weather = public.get("weather_tags", [])
    if not isinstance(weather, list) or any(not isinstance(tag, str) for tag in weather):
        raise GardenGeneratorError("小院子作物文案天气事实格式不对")
    weather_status = public.get("weather_status", "fresh" if weather else "missing")
    if weather_status not in ("fresh", "missing") or (weather_status != "fresh" and weather):
        raise GardenGeneratorError("小院子作物文案天气新鲜度事实不一致")
    outing_mode = public.get("outing_mode")
    if outing_mode is not None:
        if outing_mode not in (
            "typhoon", "storm_rain", "windy_rain", "rain",
            "light_rain", "snow", "gale",
        ):
            raise GardenGeneratorError("小院子作物文案出门天气档位无效")
        if public.get("yard_water") not in ("none", "puddles", "flooded"):
            raise GardenGeneratorError("小院子作物文案院内积水档位无效")
        streak = public.get("rain_streak_days")
        if isinstance(streak, bool) or not isinstance(streak, int) or streak < 0:
            raise GardenGeneratorError("小院子作物文案连雨天数无效")
    if copy_type == "condition_event":
        if public.get("severity") not in ("warning", "damaged", "withered"):
            raise GardenGeneratorError("小院子作物异常文案阶段无效")
        expected_penalty = 0 if public["severity"] == "warning" else 1
        if public.get("yield_penalty") != expected_penalty:
            raise GardenGeneratorError("小院子作物异常文案减产事实不一致")
    elif copy_type == "treatment":
        if public.get("outcome") not in ("resolved", "wrong_action", "healthy", "withered"):
            raise GardenGeneratorError("小院子处理文案结果无效")
        if not isinstance(public.get("action"), str) or not public["action"]:
            raise GardenGeneratorError("小院子处理文案缺少动作事实")
    elif copy_type == "watering":
        if public.get("outcome") not in (
            "watered", "no_need", "too_wet", "protest", "waterlogged", "blocked_by_condition",
            "already_waterlogged", "refused",
        ):
            raise GardenGeneratorError("小院子浇水文案结果无效")
        if public["outcome"] == "protest" and public.get("style") not in ("physical", "slapstick"):
            raise GardenGeneratorError("小院子浇水抗议风格无效")
        if public.get("soil_state") not in ("干燥", "偏干", "合适", "湿润", "湿透", None):
            raise GardenGeneratorError("小院子浇水文案土壤档位无效")
        if public.get("water_source") not in ("manual", "rain", "none", None):
            raise GardenGeneratorError("小院子浇水文案水源事实无效")
        if public.get("watering_reason") not in (
            "normal_dry", "hot_clear_dry", "hot_dry", "wind_dry",
            "low_humidity_dry", "unneeded", "active_waterlogged", None,
        ):
            raise GardenGeneratorError("小院子浇水文案原因事实无效")
    return public


def build_crop_copy_payload(facts: dict) -> dict:
    public = _public_crop_copy_facts(facts)
    return {
        "model": API_MODEL,
        "messages": [
            {"role": "system", "content": _CROP_COPY_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {"facts": public},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ),
            },
        ],
        # V4 默认开启思考；这些短文案预算小，思考会把 max_tokens 吃空导致空
        # 响应（同 morning_paper.py 的坑），机械套模板不需要推理，关掉。
        "thinking": {"type": "disabled"},
        "temperature": 1.0,
        "max_tokens": max(API_MAX_TOKENS, 220),
        "stream": False,
    }


def _kind_label(kind: str) -> str:
    return "植物" if kind == "flower" else "城市小动物"


def build_encounter_payload(
    kind: str,
    *,
    category: str = "",
    personality: str = "",
    season: str = "",
    day_period: str = "",
    weather_tags: frozenset[str] = frozenset(),
) -> dict:
    if kind == "animal" and category:
        user = (
            f"这次类型是：城市小动物，类别限定为『{category}』（务必只写这个类别里"
            f"常见的具体品种，不要写成其他类别的动物）。"
        )
        if personality:
            user += f"它的性格是『{personality}』，初见描述要能体现这种性格。"
    else:
        user = f"这次类型是：{_kind_label(kind)}。"
    user += (
        f"当前季节标签：{season or '未知'}；北京时间时段标签：{day_period or '未知'}；"
        f"已确认天气标签：{','.join(sorted(weather_tags)) if weather_tags else '无'}。"
    )
    return {
        "model": API_MODEL,
        "messages": [
            {"role": "system", "content": _ENCOUNTER_SYSTEM},
            {"role": "user", "content": user},
        ],
        "thinking": {"type": "disabled"},
        "temperature": 1.1,
        "max_tokens": API_MAX_TOKENS,
        "stream": False,
    }


def build_reaction_payload(
    kind: str,
    species: str,
    action: str,
    revived: bool,
    *,
    trait: str = "",
    category: str = "",
    personality: str = "",
) -> dict:
    revived_note = (
        "它刚从有些疲惫的状态恢复。"
        if revived
        else "它状态还算正常，这次只是一次日常照顾。"
    )
    trait_note = f"；它一贯的小特点：{trait}" if trait else ""
    personality_note = ""
    if category and personality:
        personality_note = (
            f"；动物类别：{category}，性格：{personality}"
            "（反应要体现这种性格，比如活泼的更主动/夸张，怕生的更含蓄/慢热，"
            "但不要直接说“它很活泼/它很怕生”这种评价词）"
        )
    user = (
        f"类型：{_kind_label(kind)}；具体物种：{species}{trait_note}{personality_note}；"
        f"这次的照顾动作：{action}。{revived_note}"
    )
    return {
        "model": API_MODEL,
        "messages": [
            {"role": "system", "content": _REACTION_SYSTEM},
            {"role": "user", "content": user},
        ],
        "thinking": {"type": "disabled"},
        "temperature": 1.0,
        "max_tokens": API_MAX_TOKENS,
        "stream": False,
    }


def build_stroll_payload(snapshot: dict) -> dict:
    visible_snapshot = garden_scene.writer_snapshot(snapshot)
    return {
        "model": API_MODEL,
        "messages": [
            {"role": "system", "content": _STROLL_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {"snapshot": visible_snapshot},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ),
            },
        ],
        "thinking": {"type": "disabled"},
        "temperature": 0.9,
        "max_tokens": max(API_MAX_TOKENS, 260),
        "stream": False,
    }


def request_chat(payload: dict, key: str) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        exc.close()
        raise GardenGeneratorError(f"小院子写手暂时拒绝服务（HTTP {status_code}）") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GardenGeneratorError("小院子写手暂时连不上") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GardenGeneratorError("小院子写手返回了无法识别的结果") from exc


def _extract_text(result: dict) -> str:
    try:
        text = result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise GardenGeneratorError("小院子写手没有返回正文") from exc
    if not text:
        raise GardenGeneratorError("小院子写手没有返回正文")
    return text


def _clean_scene_text(value: object, *, max_chars: int, label: str) -> str:
    if not isinstance(value, str):
        raise GardenGeneratorError(f"小院子写手返回的{label}格式不对")
    text = " ".join(value.split()).strip()
    if not text:
        raise GardenGeneratorError(f"小院子写手没有返回{label}")
    lowered = text.lower()
    if any(phrase in lowered for phrase in _BLOCKED_PHRASES):
        raise GardenGeneratorError(f"小院子写手返回的{label}不像场景描写")
    return text[:max_chars]


def _generated_json_text(
    raw: str,
    *,
    label: str,
    min_chars: int,
    max_chars: int,
) -> str:
    """解析实时写手正文；超长直接拒绝，不能截断后冒充完整句。"""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GardenGeneratorError(f"小院子写手没有返回可用的{label}") from exc
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"text"}
        or not isinstance(parsed["text"], str)
    ):
        raise GardenGeneratorError(f"小院子写手返回的{label}格式不对")
    text = " ".join(parsed["text"].split()).strip()
    if not text:
        raise GardenGeneratorError("小院子写手没有返回正文")
    if len(text) < min_chars or len(text) > max_chars:
        raise GardenGeneratorError(f"小院子写手返回的{label}越界")
    if text[-1] not in "。！？…」』”":
        raise GardenGeneratorError(f"小院子写手返回的{label}像是被截断了")
    lowered = text.lower()
    if any(phrase in lowered for phrase in _BLOCKED_PHRASES):
        raise GardenGeneratorError(f"小院子写手返回的{label}不像场景描写")
    return text


def generate_encounter(
    kind: str,
    *,
    category: str = "",
    personality: str = "",
    season: str = "",
    day_period: str = "",
    weather_tags: frozenset[str] = frozenset(),
) -> dict:
    key = _api_key()
    if not key:
        raise GardenGeneratorError("小院子写手的 API 密钥尚未配置")
    result = request_chat(
        build_encounter_payload(
            kind,
            category=category,
            personality=personality,
            season=season,
            day_period=day_period,
            weather_tags=weather_tags,
        ),
        key,
    )
    raw = _extract_text(result)
    try:
        parsed = json.loads(raw)
        species = _clean_scene_text(
            parsed["species"],
            max_chars=SPECIES_MAX_CHARS,
            label="物种",
        )
        text = _clean_scene_text(
            parsed["text"],
            max_chars=VISIBLE_MAX_CHARS,
            label="初见描述",
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GardenGeneratorError("小院子写手没有返回可用的物种描述") from exc
    return {"species": species, "text": text}


def generate_reaction(
    kind: str,
    species: str,
    action: str,
    revived: bool,
    *,
    trait: str = "",
    category: str = "",
    personality: str = "",
) -> str:
    key = _api_key()
    if not key:
        raise GardenGeneratorError("小院子写手的 API 密钥尚未配置")
    result = request_chat(
        build_reaction_payload(
            kind, species, action, revived,
            trait=trait, category=category, personality=personality,
        ),
        key,
    )
    return _clean_scene_text(
        _extract_text(result),
        max_chars=VISIBLE_MAX_CHARS,
        label="照顾反应",
    )


def generate_stroll(snapshot: dict) -> str:
    key = _api_key()
    if not key:
        raise GardenGeneratorError("小院子写手的 API 密钥尚未配置")
    text = _generated_json_text(
        _extract_text(request_chat(build_stroll_payload(snapshot), key)),
        label="逛逛画面",
        min_chars=20,
        max_chars=STROLL_MAX_CHARS,
    )
    try:
        garden_scene.validate_writer_text(text, snapshot)
    except ValueError as exc:
        raise GardenGeneratorError(str(exc)) from exc
    return text


def _validate_crop_copy_text(text: str, facts: dict) -> str:
    if len(text) < 12 or len(text) > CROP_COPY_MAX_CHARS:
        raise GardenGeneratorError("小院子写手返回的作物正文越界")
    if text[-1] not in "。！？…」』”":
        raise GardenGeneratorError("小院子写手返回的作物正文像是被截断了")
    lowered = text.lower()
    blocked = _BLOCKED_PHRASES + (
        "主人", "人工智能", "ai", "模型", "程序", "游戏", "指令", "数据",
        "没关系", "一切都会好起来", "这也是成长的一部分", "只要用心照顾",
        "结果：",
    )
    if any(phrase in lowered for phrase in blocked):
        raise GardenGeneratorError("小院子写手返回的作物正文含有禁用表达")

    crop_name = facts["crop_name"]
    if crop_name not in text:
        raise GardenGeneratorError("小院子写手漏掉了当前作物")
    condition_type = facts.get("condition_type")
    markers = _CONDITION_COPY_MARKERS.get(str(condition_type or ""), ())
    copy_type = facts["copy_type"]
    outcome = str(facts.get("outcome") or "")
    mentioned_actions = {
        action
        for action, action_markers in _CROP_ACTION_MARKERS.items()
        if any(marker in text for marker in action_markers)
    }
    if copy_type == "condition_event" and mentioned_actions:
        raise GardenGeneratorError("小院子写手把异常出现写成了处理动作")
    if copy_type == "treatment":
        allowed_action = str(facts.get("action") or "")
        if any(action != allowed_action for action in mentioned_actions):
            raise GardenGeneratorError("小院子写手编造了本次没有发生的处理动作")
    if copy_type == "watering" and mentioned_actions - {"浇水"}:
        raise GardenGeneratorError("小院子写手给浇水结果编造了其他动作")
    if (
        condition_type
        and copy_type != "watering"
        and not (copy_type == "treatment" and outcome == "withered")
        and not any(marker in text for marker in markers)
    ):
        raise GardenGeneratorError("小院子写手漏掉了当前异常")

    if copy_type == "condition_event":
        severity = facts.get("severity")
        if severity == "warning" and any(
            marker in text for marker in ("已解决", "恢复生长", "枯死", "少收", "减产")
        ):
            raise GardenGeneratorError("小院子写手改写了异常初始结果")
        if severity == "damaged":
            if not any(marker in text for marker in ("少收", "减产", "少了一份", "损失")):
                raise GardenGeneratorError("小院子写手漏掉了减产事实")
            if any(marker in text for marker in ("已解决", "恢复生长", "枯死")):
                raise GardenGeneratorError("小院子写手改写了异常恶化结果")
        if severity == "withered":
            if not any(marker in text for marker in ("枯死", "干枯", "枯枝", "这一茬已经结束")):
                raise GardenGeneratorError("小院子写手漏掉了枯死事实")
            if any(marker in text for marker in ("恢复生长", "救活", "活过来")):
                raise GardenGeneratorError("小院子写手改写了枯死结果")
    elif copy_type == "treatment":
        if outcome in ("wrong_action", "withered") and any(
            marker in text for marker in ("已解决", "恢复生长", "救活", "活过来")
        ):
            raise GardenGeneratorError("小院子写手改写了处理结果")
        if outcome == "withered" and not any(
            marker in text for marker in ("枯死", "干枯", "枯枝", "这一茬已经结束")
        ):
            raise GardenGeneratorError("小院子写手漏掉了枯死事实")
        if outcome == "healthy" and any(
            marker in text for marker in ("虫害", "病叶", "积水", "缺肥", "暂停生长")
        ):
            raise GardenGeneratorError("小院子写手给健康作物发明了异常")
        if facts.get("yield_penalty") == 0 and any(
            marker in text for marker in ("少收", "减产")
        ):
            raise GardenGeneratorError("小院子写手发明了不存在的减产")
    elif copy_type == "watering":
        if outcome in ("no_need", "too_wet", "already_waterlogged", "refused", "blocked_by_condition") and any(
            marker in text
            for marker in (
                "浇了", "浇上", "倒下", "倒进", "添了水", "补了水",
                "水落下", "挨了一遍水", "又喝了水",
            )
        ):
            raise GardenGeneratorError("小院子写手把未浇水结果写成了已经浇水")
        if outcome == "protest":
            if any(marker in text for marker in ("积水", "暂停生长", "生长加成")):
                raise GardenGeneratorError("小院子写手改写了第二次浇水结果")
            if facts.get("style") == "physical" and any(
                marker in text for marker in ("“", "”", "嚷", "骂", "开口", "说：")
            ):
                raise GardenGeneratorError("小院子写手把现实抗议写成了拟人对白")
        if outcome == "waterlogged" and not any(
            marker in text for marker in _CONDITION_COPY_MARKERS["waterlogged"]
        ):
            raise GardenGeneratorError("小院子写手漏掉了积水事实")
        if outcome == "blocked_by_condition" and condition_type and not any(
            marker in text for marker in markers
        ):
            raise GardenGeneratorError("小院子写手漏掉了院内已有异常")
        if outcome == "watered" and condition_type and not any(marker in text for marker in markers):
            raise GardenGeneratorError("小院子写手漏掉了浇水未解除的异常")
        soil_state = facts.get("soil_state")
        if any(marker in text for marker in ("土干了", "土壤干燥", "干裂的土", "偏干")) and soil_state not in ("干燥", "偏干"):
            raise GardenGeneratorError("小院子写手把土壤写得过干")
        if "雨浇透" in text and not (
            facts.get("water_source") == "rain" and soil_state in ("湿润", "湿透")
        ):
            raise GardenGeneratorError("小院子写手编造了降雨浇透")
    return text


def validate_crop_copy_text(text: str, facts: dict) -> str:
    """供调用点对注入写手与本地候选使用同一套结果边界。"""
    return _validate_crop_copy_text(text, _public_crop_copy_facts(facts))


def generate_crop_copy(facts: dict) -> str:
    public = _public_crop_copy_facts(facts)
    key = _api_key()
    if not key:
        raise GardenGeneratorError("小院子写手的 API 密钥尚未配置")
    text = _generated_json_text(
        _extract_text(request_chat(build_crop_copy_payload(public), key)),
        label="作物正文",
        min_chars=12,
        max_chars=CROP_COPY_MAX_CHARS,
    )
    return _validate_crop_copy_text(text, public)
