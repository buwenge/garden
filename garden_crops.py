"""阶段 C 的本地、可审查菜畦目录。

这里没有网络数据，也不根据模型输出决定任何规则。动物零食表刻意保守：
猫和刺猬没有可由菜畦产物提供的零食；狗只收南瓜；兔子只收本表列出的
叶菜、薄荷、小萝卜与南瓜。它们都只是偶尔的一小份点心，不替代主食。
"""

from __future__ import annotations

# 阶段 C 的环境倾向只影响生长倍率的温和方向，不追求农学精确：warm_loving 在
# 暖热且水分合适时可小幅加速，cool_loving 在长时间高温里比其它作物慢一些，
# neutral 只受统一的轻微高温压力影响。
HEAT_TENDENCIES = frozenset({"warm_loving", "cool_loving", "neutral"})

SEASON_NAMES = {"spring": "春季", "summer": "夏季", "autumn": "秋季", "winter": "冬季"}
SEASON_ORDER = ("spring", "summer", "autumn", "winter")

CROPS = {
    "strawberry": {"name": "草莓", "seasons": ("spring",), "growth_days": 4,
                   "term_affinities": ("rain_water", "clear_and_bright"),
                   "stage_thresholds": (("sprout", 1.0), ("growing", 2.0), ("ready", 4.0)),
                   "harvest_amount": 3, "seed_return": 1, "heat_tendency": "cool_loving"},
    "radish": {"name": "小萝卜", "aliases": ("萝卜",),
                "seasons": ("spring",), "growth_days": 3,
                "term_affinities": ("awakening_of_insects", "grain_rain"),
                "stage_thresholds": (("sprout", 1.0), ("growing", 2.0), ("ready", 3.0)),
                "harvest_amount": 2, "seed_return": 1, "heat_tendency": "cool_loving"},
    "tomato": {"name": "小番茄", "aliases": ("番茄",),
                "seasons": ("summer",), "growth_days": 4,
                "term_affinities": ("minor_heat", "major_heat"),
                "stage_thresholds": (("sprout", 1.0), ("growing", 2.0), ("ready", 4.0)),
                "harvest_amount": 3, "seed_return": 1, "heat_tendency": "warm_loving"},
    "mint": {"name": "薄荷", "seasons": ("summer",), "growth_days": 3,
             "term_affinities": ("grain_in_ear", "summer_solstice"),
             "stage_thresholds": (("sprout", 1.0), ("growing", 2.0), ("ready", 3.0)),
             "harvest_amount": 2, "seed_return": 1, "heat_tendency": "neutral"},
    "cucumber": {"name": "黄瓜", "seasons": ("summer",), "growth_days": 4,
                 "term_affinities": ("grain_in_ear", "summer_solstice"),
                 "stage_thresholds": (("sprout", 1.0), ("growing", 2.0), ("ready", 4.0)),
                 "harvest_amount": 3, "seed_return": 1, "heat_tendency": "neutral"},
    "mini_watermelon": {"name": "西瓜", "aliases": ("小西瓜",),
                        "seasons": ("summer",), "growth_days": 5,
                        "term_affinities": ("minor_heat", "major_heat"),
                        "stage_thresholds": (("sprout", 1.0), ("growing", 3.0), ("ready", 5.0)),
                        "harvest_amount": 2, "seed_return": 1, "heat_tendency": "warm_loving"},
    "pepper": {"name": "辣椒", "seasons": ("spring", "summer", "autumn"), "growth_days": 4,
               "term_affinities": ("minor_heat", "major_heat"),
               "stage_thresholds": (("sprout", 1.0), ("growing", 2.0), ("ready", 4.0)),
               "harvest_amount": 3, "seed_return": 1, "heat_tendency": "warm_loving"},
    "pumpkin": {"name": "南瓜", "seasons": ("autumn",), "growth_days": 5,
                "term_affinities": ("limit_of_heat", "white_dew"),
                "stage_thresholds": (("sprout", 1.0), ("growing", 3.0), ("ready", 5.0)),
                "harvest_amount": 2, "seed_return": 1, "heat_tendency": "warm_loving"},
    "chinese_cabbage": {"name": "小白菜", "aliases": ("白菜",),
                        "seasons": ("autumn",), "growth_days": 3,
                        "term_affinities": ("autumn_equinox", "cold_dew"),
                        "stage_thresholds": (("sprout", 1.0), ("growing", 2.0), ("ready", 3.0)),
                        "harvest_amount": 2, "seed_return": 1, "heat_tendency": "cool_loving"},
    "chestnut": {"name": "板栗", "seasons": ("autumn",), "growth_days": 5,
                 "term_affinities": ("white_dew", "autumn_equinox"),
                 "stage_thresholds": (("sprout", 1.0), ("growing", 3.0), ("ready", 5.0)),
                 "harvest_amount": 2, "seed_return": 1, "heat_tendency": "neutral"},
    "osmanthus": {"name": "桂花", "seasons": ("autumn",), "growth_days": 3,
                  "term_affinities": ("start_of_autumn", "white_dew"),
                  "stage_thresholds": (("sprout", 1.0), ("growing", 2.0), ("ready", 3.0)),
                  "harvest_amount": 2, "seed_return": 1, "heat_tendency": "cool_loving"},
    "sweet_potato": {"name": "红薯", "seasons": ("autumn",), "growth_days": 5,
                     "term_affinities": ("cold_dew", "frost_descent"),
                     "stage_thresholds": (("sprout", 1.0), ("growing", 3.0), ("ready", 5.0)),
                     "harvest_amount": 2, "seed_return": 1, "heat_tendency": "warm_loving"},
}

_REQUIRED_CROP_FIELDS = {
    "name": str, "seasons": tuple, "growth_days": int, "term_affinities": tuple,
    "stage_thresholds": tuple, "harvest_amount": int, "seed_return": int,
    "heat_tendency": str,
}


def _validate_crop_catalog() -> None:
    """模块导入时的一次性白名单校验；目录是代码字面量，不是外部输入，
    校验目的是防止手改字段时打错名字或漏填，而不是防御恶意数据。"""
    for crop_id, crop in CROPS.items():
        for field, expected_type in _REQUIRED_CROP_FIELDS.items():
            if field not in crop:
                raise ValueError(f"作物目录 {crop_id} 缺少字段 {field}")
            if not isinstance(crop[field], expected_type):
                raise ValueError(f"作物目录 {crop_id} 字段 {field} 类型不符")
        if crop["heat_tendency"] not in HEAT_TENDENCIES:
            raise ValueError(f"作物目录 {crop_id} 的 heat_tendency 不在白名单内")
        aliases = crop.get("aliases", ())
        if not isinstance(aliases, tuple) or not all(isinstance(alias, str) and alias for alias in aliases):
            raise ValueError(f"作物目录 {crop_id} 的 aliases 格式不符")


_validate_crop_catalog()

INITIAL_SEEDS_BY_SEASON = {
    "spring": {"strawberry": 2, "radish": 2},
    "summer": {"tomato": 2, "mint": 2},
    "autumn": {"pumpkin": 2, "chinese_cabbage": 2},
    "winter": {},
}

# 已经领过旧版首发种子的院子也要拿到后来加入的品种。每个补充包由 garden.py
# 在 meta 里独立记账，只发一次；种子即使暂时过季也会留在篮子里等待适种季节。
BONUS_SEED_PACKS = {
    # INITIAL_SEEDS_BY_SEASON 只在院子第一次初始化那一刻发放当季的两种基础
    # 作物，之后永远不会补发——一个在夏天创建的院子，只要没在春/秋天重新
    # 走过那段初始化代码，就永远拿不到草莓/萝卜或南瓜/小白菜的种子。8/7 发现
    # 真实存档就撞上了这个情况，补两个跟其他品种同款的补充包，让所有院子在
    # 经过对应季节时都能拿到，不管院子最初是哪个季节创建的。
    "spring_base_2026": {
        "seasons": ("spring",), "covered_by_initial_season": True,
        "seeds": {"strawberry": 2, "radish": 2},
    },
    "autumn_base_2026": {
        "seasons": ("autumn",), "covered_by_initial_season": True,
        "seeds": {"pumpkin": 2, "chinese_cabbage": 2},
    },
    "summer_variety_2026": {
        "seasons": ("summer",),
        "seeds": {"cucumber": 2, "mini_watermelon": 2, "pepper": 2},
    },
    "autumn_variety_2026": {
        "seasons": ("autumn",),
        "seeds": {"chestnut": 2, "osmanthus": 2, "sweet_potato": 2},
    },
}

# 资料依据：Cornell 说明猫是专性肉食动物，刺猬保护组织建议野生刺猬只补充
# 肉基食物；RWAF 列出兔可少量食用的叶菜、薄荷、南瓜和萝卜；Cornell 列出狗
# 可作低热量零食的蔬菜。不能由这些资料直接推出的组合一律不开放。
ANIMAL_TREAT_COMPATIBILITY = {
    "猫": frozenset(),
    "狗": frozenset({"pumpkin"}),
    "兔子": frozenset({"radish", "mint", "pumpkin", "chinese_cabbage"}),
    "刺猬": frozenset(),
}

RECIPES = {
    "tomato_mint_salad": {"name": "番茄薄荷小沙拉", "ingredients": {"tomato": 1, "mint": 1}},
    "chilled_tomato": {"name": "凉拌番茄", "ingredients": {"tomato": 1}},
    "mint_water": {"name": "薄荷水", "ingredients": {"mint": 1}},
    "watermelon_slices": {"name": "西瓜果切", "ingredients": {"mini_watermelon": 1}},
    "watermelon_juice": {"name": "西瓜汁", "ingredients": {"mini_watermelon": 1}},
    "watermelon_smoothie": {"name": "西瓜冰沙", "ingredients": {"mini_watermelon": 1}},
    "cucumber_salad": {"name": "凉拌黄瓜", "ingredients": {"cucumber": 1}},
    "smashed_cucumber": {"name": "拍黄瓜", "ingredients": {"cucumber": 1}},
    "pepper_eggs": {"name": "辣椒炒蛋", "ingredients": {"pepper": 1}, "egg_range": (1, 3)},
    "tomato_eggs": {"name": "番茄炒蛋", "ingredients": {"tomato": 1}, "egg_range": (1, 3)},
    "strawberry_compote": {"name": "草莓小果酱", "ingredients": {"strawberry": 2}},
    "radish_side": {"name": "清拌小萝卜", "ingredients": {"radish": 1}},
    "pumpkin_porridge": {"name": "南瓜小米粥", "ingredients": {"pumpkin": 1}},
    "cabbage_soup": {"name": "小白菜清汤", "ingredients": {"chinese_cabbage": 1}},
    "tomato_salad": {"name": "番茄沙拉", "ingredients": {"tomato": 1}},
    "sugar_roasted_chestnut": {"name": "糖炒栗子", "ingredients": {"chestnut": 1}},
    "osmanthus_cake": {"name": "桂花糕", "ingredients": {"osmanthus": 1}},
    "pumpkin_cake": {"name": "南瓜饼", "ingredients": {"pumpkin": 1}},
    "stir_fried_cabbage": {"name": "炝炒白菜", "ingredients": {"chinese_cabbage": 1, "pepper": 1}},
    "steamed_sweet_potato": {"name": "蒸红薯", "ingredients": {"sweet_potato": 1}},
    "roasted_sweet_potato": {"name": "烤红薯", "ingredients": {"sweet_potato": 1}},
    "chili_powder": {"name": "辣椒粉", "ingredients": {"pepper": 1}},
    "cabbage_with_dip": {
        "name": "蘸水白菜", "ingredients": {"chinese_cabbage": 1},
        "prepared_ingredients": {"chili_powder": 1},
    },
    "osmanthus_honey": {"name": "桂花蜜", "ingredients": {"osmanthus": 1}},
    "osmanthus_honey_water": {
        "name": "桂花蜜泡水", "ingredients": {},
        "prepared_ingredients": {"osmanthus_honey": 1},
    },
    "chestnut_pumpkin_soup": {"name": "板栗南瓜羹", "ingredients": {"chestnut": 1, "pumpkin": 1}},
}


def crop_name(crop_id: str) -> str:
    return CROPS[crop_id]["name"]


def resolve_crop(value: str) -> str | None:
    cleaned = "".join(value.split()).lower()
    matches = [
        crop_id for crop_id, crop in CROPS.items()
        if cleaned in (
            crop_id,
            crop["name"].lower(),
            *(alias.lower() for alias in crop.get("aliases", ())),
        )
    ]
    return matches[0] if len(matches) == 1 else None


def recipes_for_crop(crop_id: str) -> list[str]:
    return [recipe_id for recipe_id, recipe in RECIPES.items() if crop_id in recipe["ingredients"]]


def resolve_recipe(value: str) -> str | None:
    cleaned = "".join(value.split()).lower()
    direct = [recipe_id for recipe_id, recipe in RECIPES.items() if cleaned in (recipe_id, recipe["name"].lower())]
    if len(direct) == 1:
        return direct[0]
    crop_id = resolve_crop(cleaned)
    matches = recipes_for_crop(crop_id) if crop_id else []
    return matches[0] if len(matches) == 1 else None
