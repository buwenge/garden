"""小院子的本地传统节日表。

节日日期与节气计算刻意分开：这里仅保存已审核的年度表，既不猜农历、
不访问网络，也不从聊天或任何私人资料推断日期。缺少年份时返回 ``None``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class FestivalContext:
    festival_id: str
    festival_name: str
    starts_at: datetime
    ends_at: datetime


# 2026 年中国传统节日（北京时间）。农历节日逐年显式审阅后再加入；清明、
# 冬至也放在这里，业务层不依赖节气表去“顺便猜”节日。
FESTIVALS_BY_YEAR: dict[int, tuple[tuple[str, str, int, int], ...]] = {
    2026: (
        ("spring_festival", "春节", 2, 17),
        ("lantern_festival", "元宵", 3, 3),
        ("clear_and_bright", "清明", 4, 5),
        ("dragon_boat", "端午", 6, 19),
        ("qixi", "七夕", 8, 19),
        ("mid_autumn", "中秋", 9, 25),
        ("double_ninth", "重阳", 10, 18),
        ("winter_solstice", "冬至", 12, 22),
    ),
}


class FestivalProvider:
    """只读节日上下文；空年度表就是安全的“无节日”。"""

    def __init__(self, festivals_by_year: dict[int, tuple[tuple[str, str, int, int], ...]] | None = None):
        self._festivals_by_year = festivals_by_year if festivals_by_year is not None else FESTIVALS_BY_YEAR

    @staticmethod
    def _localize(now: datetime) -> datetime:
        if now.tzinfo is None:
            raise ValueError("小院子节日必须使用带时区的时间")
        return now.astimezone(TZ)

    def context_at(self, now: datetime) -> FestivalContext | None:
        now = self._localize(now)
        for festival_id, festival_name, month, day in self._festivals_by_year.get(now.year, ()):
            starts_at = datetime.combine(date(now.year, month, day), time.min, tzinfo=TZ)
            ends_at = starts_at + timedelta(days=1)
            if starts_at <= now < ends_at:
                return FestivalContext(festival_id, festival_name, starts_at, ends_at)
        return None

    def definition(self, year: int, festival_id: str) -> FestivalContext | None:
        for candidate_id, festival_name, month, day in self._festivals_by_year.get(year, ()):
            if candidate_id == festival_id:
                starts_at = datetime.combine(date(year, month, day), time.min, tzinfo=TZ)
                return FestivalContext(festival_id, festival_name, starts_at, starts_at + timedelta(days=1))
        return None


class PersonalDateProvider:
    """私人日期的空接口；第三版没有配置、没有展示、也不会推断任何日期。"""

    def context_at(self, now: datetime) -> None:
        del now
        return None


DEFAULT_FESTIVALS = FestivalProvider()
