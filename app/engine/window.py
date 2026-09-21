"""调价窗口推算。

国内成品油每 **10 个工作日** 调一次，调价日当天 24 时生效。
节假日会顺延，所以严格推算需要节假日表——见 :mod:`app.engine.holidays`。

⚠️ 本模块不猜测"上一次调价是哪天"，那必须由外部传入锚点（真实调价日），
   否则整条时间轴会漂移。没有人工锚点时退而用 ``last_anchor`` 按种子递推。
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .. import config
from .holidays import EMPTY, HolidayCalendar

# 已知最近一次真实调价日，作为"没有人工锚点"时的推算种子。
# 来源：汽油价格网调价日历归档页最后一条记录（已核对为真实调价，非"搁浅"）。
# ⚠️ 每年初顺手核对一次——只要它仍是真实调价日，向前递推就始终正确。
_AUTO_SEED = date(2024, 12, 18)

_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


def add_workdays(
    start: date, count: int, cal: HolidayCalendar = EMPTY
) -> date:
    """从 start 往后数 count 个工作日（不含 start 本身）。"""
    if count <= 0:
        return start
    day = start
    remaining = count
    while remaining > 0:
        day += timedelta(days=1)
        if cal.is_workday(day):
            remaining -= 1
    return day


def count_workdays(
    start: date, end: date, cal: HolidayCalendar = EMPTY
) -> int:
    """统计 (start, end] 区间内的工作日数量。"""
    if end <= start:
        return 0
    day = start
    total = 0
    while day < end:
        day += timedelta(days=1)
        if cal.is_workday(day):
            total += 1
    return total


def next_window(
    anchor: date,
    today: date | None = None,
    cal: HolidayCalendar = EMPTY,
) -> dict:
    """从上次真实调价日 anchor 推算出下一次调价窗口。

    返回 {next_date, workdays_elapsed, workdays_remaining, progress}。
    """
    today = today or date.today()
    next_date = add_workdays(anchor, config.CYCLE_WORKDAYS, cal)
    elapsed = count_workdays(anchor, min(today, next_date), cal)
    remaining = max(0, config.CYCLE_WORKDAYS - elapsed)
    return {
        "anchor": anchor.isoformat(),
        "next_date": next_date.isoformat(),
        "cycle_workdays": config.CYCLE_WORKDAYS,
        "workdays_elapsed": elapsed,
        "workdays_remaining": remaining,
        "progress": round(elapsed / config.CYCLE_WORKDAYS, 3),
    }


def parse_adjustment_dates(html: str) -> list[date]:
    """从调价日历归档页 HTML 里抠出所有 YYYY年M月D日。仅用于人工刷新种子。"""
    out: list[date] = []
    for y, m, d in _DATE_RE.findall(html):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    return out


def last_anchor(
    today: date | None = None, cal: HolidayCalendar = EMPTY
) -> date:
    """没有人工锚点时，从种子按 10 工作日向前递推，取今天之前最近一次调价日。

    为什么不直接用抓到的最大日期当锚点：归档页可能停更（实测只到 2024），
    直接拿会严重漂移。以真实调价日为种子向后 +10 工作日递推到今天附近，
    即使归档停更也能给出接近的锚点。

    默认 ``cal=EMPTY``（只按周末，无节假日）——这是"无日历"基线，测试用它
    钉住确定性结果。生产路径（cli.collect）会传入 ``CN_2025_2026`` 以计入
    真实调休，给出更准的锚点。
    """
    today = today or date.today()
    cur = _AUTO_SEED
    while add_workdays(cur, config.CYCLE_WORKDAYS, cal) <= today:
        cur = add_workdays(cur, config.CYCLE_WORKDAYS, cal)
    return cur
