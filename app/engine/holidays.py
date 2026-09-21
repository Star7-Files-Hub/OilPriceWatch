"""中国法定节假日日历。

成品油每 **10 个工作日** 调一次，而「工作日」要扣掉法定假日、加上调休补班日。
没有这张表，auto-anchor 的递推会有 ±1~2 天漂移。

模型：
- ``off``        法定假日（含自然落在周末的那几天，列进来无害）。
- ``make_up_work`` 周末补班日——本质是周六/日但必须上班，单靠「假日集合」
                无法表达（周末默认就不是工作日），必须显式覆盖成工作日。

数据来源：国务院办公厅《关于2025年部分节假日安排的通知》《关于2026年部分
节假日安排的通知》（gov.cn 原文）。

⚠️ 每年初顺手把下一年度的安排补进 ``CN_2025_2026``（改名或加新常量），否则
   跨年后的 auto-anchor 会退化成「只按周末」推算。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


def _d(y: int, m: int, d: int) -> date:
    return date(y, m, d)


def _span(y: int, m1: int, d1: int, m2: int, d2: int) -> set[date]:
    out: set[date] = set()
    cur = date(y, m1, d1)
    end = date(y, m2, d2)
    while cur <= end:
        out.add(cur)
        cur += timedelta(days=1)
    return out


@dataclass(frozen=True)
class HolidayCalendar:
    off: frozenset[date] = frozenset()
    make_up_work: frozenset[date] = frozenset()

    def is_workday(self, day: date) -> bool:
        if day in self.make_up_work:
            return True
        if day.weekday() >= 5:  # 周六=5, 周日=6
            return False
        return day not in self.off


# 空日历：只按「周一到周五」判断，等价于旧逻辑（无节假日）。
EMPTY = HolidayCalendar()

# --- 2025 ---------------------------------------------------------------
_OFF_2025 = frozenset(
    {_d(2025, 1, 1)}                       # 元旦
    | _span(2025, 1, 28, 2, 4)             # 春节 8 天
    | _span(2025, 4, 4, 4, 6)              # 清明 3 天
    | _span(2025, 5, 1, 5, 5)              # 劳动节 5 天
    | _span(2025, 5, 31, 6, 2)             # 端午 3 天
    | _span(2025, 10, 1, 10, 8)            # 国庆+中秋 8 天
)
_MAKEUP_2025 = frozenset(
    {
        _d(2025, 1, 26),   # 春节补班（周日）
        _d(2025, 2, 8),    # 春节补班（周六）
        _d(2025, 4, 27),   # 劳动节补班（周日）
        _d(2025, 9, 28),   # 国庆补班（周日）
        _d(2025, 10, 11),  # 国庆补班（周六）
    }
)

# --- 2026 ---------------------------------------------------------------
_OFF_2026 = frozenset(
    _span(2026, 1, 1, 1, 3)                # 元旦 3 天
    | _span(2026, 2, 15, 2, 23)            # 春节 9 天
    | _span(2026, 4, 4, 4, 6)              # 清明 3 天
    | _span(2026, 5, 1, 5, 5)              # 劳动节 5 天
    | _span(2026, 6, 19, 6, 21)            # 端午 3 天
    | _span(2026, 9, 25, 9, 27)            # 中秋 3 天（不调休）
    | _span(2026, 10, 1, 10, 7)            # 国庆 7 天
)
_MAKEUP_2026 = frozenset(
    {
        _d(2026, 1, 4),    # 元旦补班（周日）
        _d(2026, 2, 14),   # 春节补班（周六）
        _d(2026, 2, 28),   # 春节补班（周六）
        _d(2026, 5, 9),    # 劳动节补班（周六）
        _d(2026, 9, 20),   # 国庆补班（周日）
        _d(2026, 10, 10),  # 国庆补班（周六）
    }
)

# 防御：两个集合不能重叠，否则同一天既是假日又是补班，逻辑自相矛盾。
assert not (_OFF_2025 & _MAKEUP_2025), "2025 off/make_up 重叠"
assert not (_OFF_2026 & _MAKEUP_2026), "2026 off/make_up 重叠"

CN_2025_2026 = HolidayCalendar(_OFF_2025 | _OFF_2026, _MAKEUP_2025 | _MAKEUP_2026)
