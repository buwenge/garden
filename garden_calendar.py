"""小院子的本地日历上下文。

业务层只依赖 ``CalendarContextProvider``，不直接散落北京时间、节气边界或
季节判断。首版使用随源码审查的本地表，不在 tick 时请求网络；未收录年份安全
降级为没有节气名称，但仍给出稳定的近似季节背景，绝不猜测节日日期。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from garden_festivals import DEFAULT_FESTIVALS, FestivalProvider

TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class CalendarContext:
    now: datetime
    day_period: str
    season: str
    term_id: str | None
    term_name: str | None
    term_started_at: datetime | None
    term_ends_at: datetime | None
    festival_id: str | None = None
    festival_name: str | None = None
    festival_started_at: datetime | None = None
    festival_ends_at: datetime | None = None


# 2026 年二十四节气的北京时间起点，逐项按香港天文台 2026 年权威表核对：
# https://www.hko.gov.hk/tc/gts/astron2026/files/2026SolarTerms24.pdf
# 后续可由可替换的 provider 扩展年份，调用方不需要知道算法或数据来源。
_TERMS_2026 = (
    ("小寒", "minor_cold", 1, 5, 16, 23), ("大寒", "major_cold", 1, 20, 9, 45),
    ("立春", "start_of_spring", 2, 4, 4, 2), ("雨水", "rain_water", 2, 18, 23, 52),
    ("惊蛰", "awakening_of_insects", 3, 5, 21, 59), ("春分", "spring_equinox", 3, 20, 22, 46),
    ("清明", "clear_and_bright", 4, 5, 2, 40), ("谷雨", "grain_rain", 4, 20, 9, 39),
    ("立夏", "start_of_summer", 5, 5, 19, 49), ("小满", "grain_buds", 5, 21, 8, 37),
    ("芒种", "grain_in_ear", 6, 5, 23, 48), ("夏至", "summer_solstice", 6, 21, 16, 25),
    ("小暑", "minor_heat", 7, 7, 9, 57), ("大暑", "major_heat", 7, 23, 3, 13),
    ("立秋", "start_of_autumn", 8, 7, 19, 43), ("处暑", "limit_of_heat", 8, 23, 10, 19),
    ("白露", "white_dew", 9, 7, 22, 41), ("秋分", "autumn_equinox", 9, 23, 8, 5),
    ("寒露", "cold_dew", 10, 8, 14, 29), ("霜降", "frost_descent", 10, 23, 17, 38),
    ("立冬", "start_of_winter", 11, 7, 17, 52), ("小雪", "minor_snow", 11, 22, 15, 23),
    ("大雪", "major_snow", 12, 7, 10, 52), ("冬至", "winter_solstice", 12, 22, 4, 50),
)


class CalendarContextProvider:
    """给任意时刻构建唯一、可测试的北京时间世界上下文。"""

    def __init__(self, terms_by_year: dict[int, tuple] | None = None, festival_provider: FestivalProvider | None = None):
        self._terms_by_year = terms_by_year if terms_by_year is not None else {2026: _TERMS_2026}
        self._festival_provider = festival_provider if festival_provider is not None else DEFAULT_FESTIVALS

    @staticmethod
    def _localize(now: datetime) -> datetime:
        if now.tzinfo is None:
            raise ValueError("小院子日历必须使用带时区的时间")
        return now.astimezone(TZ)

    @staticmethod
    def _day_period(now: datetime) -> str:
        hour = now.hour
        if 5 <= hour < 8:
            return "dawn"
        if 8 <= hour < 17:
            return "day"
        if 17 <= hour < 20:
            return "dusk"
        if 20 <= hour or hour < 2:
            return "night"
        return "late_night"

    @staticmethod
    def _fallback_season(now: datetime) -> str:
        # 仅在本地节气表未覆盖该年份时使用；不伪造 term_name。
        if now.month in (3, 4):
            return "spring"
        if now.month in (5, 6, 7):
            return "summer"
        if now.month in (8, 9, 10):
            return "autumn"
        return "winter"

    def context_at(self, now: datetime) -> CalendarContext:
        now = self._localize(now)
        festival = self._festival_provider.context_at(now)
        festival_fields = (
            festival.festival_id if festival else None,
            festival.festival_name if festival else None,
            festival.starts_at if festival else None,
            festival.ends_at if festival else None,
        )
        terms = self._terms_by_year.get(now.year, ())
        starts = [
            (datetime(now.year, month, day, hour, minute, tzinfo=TZ), term_id, name)
            for name, term_id, month, day, hour, minute in terms
        ]
        current_index = max((i for i, (started, _, _) in enumerate(starts) if started <= now), default=-1)
        if current_index >= 0:
            started, term_id, term_name = starts[current_index]
            ends = starts[current_index + 1][0] if current_index + 1 < len(starts) else None
            season = {
                "start_of_spring": "spring", "start_of_summer": "summer",
                "start_of_autumn": "autumn", "start_of_winter": "winter",
            }.get(term_id)
            if season is None:
                # 当前节气处于上一季；在 2026 年首个立春前自然归冬。
                season_starts = {
                    "start_of_spring": "spring", "start_of_summer": "summer",
                    "start_of_autumn": "autumn", "start_of_winter": "winter",
                }
                season = next(
                    (season_starts[item_id] for _, item_id, _ in reversed(starts[:current_index + 1])
                     if item_id in season_starts),
                    "winter",
                )
            return CalendarContext(now, self._day_period(now), season, term_id, term_name, started, ends, *festival_fields)
        return CalendarContext(now, self._day_period(now), self._fallback_season(now), None, None, None, None, *festival_fields)

    def term_definition(self, year: int, term_id: str) -> CalendarContext | None:
        """按本地表精确取一个节气窗口，供持久化事件做防伪校验。"""
        terms = self._terms_by_year.get(year, ())
        for _, candidate_id, month, day, hour, minute in terms:
            if candidate_id == term_id:
                return self.context_at(datetime(year, month, day, hour, minute, tzinfo=TZ))
        return None

    def festival_definition(self, year: int, festival_id: str):
        """给业务层的受控节日查表入口，不暴露 provider 内部状态。"""
        return self._festival_provider.definition(year, festival_id)


DEFAULT_CALENDAR = CalendarContextProvider()
