"""定时调度：后台周期性刷新缓存 + 每日油价走向推送。

用 APScheduler 的 BackgroundScheduler，不阻塞主进程（FastAPI 在跑）。
默认每 30 分钟刷新一次——公开服务下，用户只读缓存，上游只在这两个时间点被打。

⚠️ 调价窗口临近时（剩 1-2 个工作日）可以更频繁，但第一阶段先固定 30 分钟，
   避免把上游免费源打挂（它限流很凶）。后续可按 window.workdays_remaining 动态提速。

📣 两条推送都挂在同一次巡检（:func:`_job`）上，各自判幂等，**不是**各起一套逻辑：
   · ``bot.maybe_push``：新一轮调价窗口开启时一次；
   · ``bot.maybe_daily_push``：每天 ``OILWATCH_TG_DAILY_HOUR``（默认 8 点）之后一次。
   幂等键由 bot 侧维护，且**只在真的送出去之后才落** ⇒ 服务重启、错过时间点都能
   自动补推，既不会重复发，也不会因为「那一刻还没有订阅者」把一整轮静默掉
   （线上 2026-09-28 就是这么丢掉一轮的，见 bot.maybe_push 的注释）。
"""
from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from . import collector, bot

logger = logging.getLogger("oilwatch.scheduler")

_scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
_DEFAULT_INTERVAL = 30  # 分钟

#: 巡检重入锁：interval 任务与每日整点任务可能撞在一起（都调 :func:`_job`），
#: 没有它就会并发打两次上游，并让两条推送路径互相抢幂等键。
_job_lock = threading.Lock()


def _push(data: dict) -> None:
    """两条推送路径的收尾：各自判幂等，谁有得推谁推。"""
    for name, fn in (
        ("新一轮窗口推送", bot.maybe_push),
        ("每日油价走向", bot.maybe_daily_push),
    ):
        try:
            result = fn(data)
        except Exception as exc:  # noqa: BLE001 - 推送失败绝不能影响下一次巡检
            logger.warning("%s失败: %s", name, exc)
            continue
        if result.get("pushed"):
            logger.info("%s已推送给 %d 名订阅者", name, result["pushed"])
        else:
            logger.debug("%s跳过: %s", name, result.get("reason"))


def _job() -> None:
    """刷新缓存 → 按需推送（每 30 分钟一次；每日整点那次也走这里）。"""
    if not _job_lock.acquire(blocking=False):
        logger.debug("上一次巡检还没结束，跳过本次")
        return
    try:
        try:
            data = collector.refresh()
        except Exception as exc:  # noqa: BLE001
            logger.warning("定时刷新失败: %s", exc)
            return
        _push(data)
    finally:
        _job_lock.release()


def start(interval_minutes: int = _DEFAULT_INTERVAL) -> None:
    if _scheduler.running:
        return
    _scheduler.add_job(
        _job,
        "interval",
        minutes=interval_minutes,
        id="refresh",
        max_instances=1,
        coalesce=True,  # 错过就合并成一次，别堆job
        misfire_grace_time=600,
    )
    # 每日整点再来一次：保证「8 点推」是准点的（30 分钟巡检负责兜底补推）。
    hour = bot.daily_hour()
    _scheduler.add_job(
        _job,
        "cron",
        hour=hour,
        minute=0,
        id="daily_push",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    _scheduler.start()
    logger.info(
        "调度器已启动：每 %d 分钟刷新一次，每日 %d:00 推送油价走向",
        interval_minutes,
        hour,
    )


def shutdown() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("调度器已停止")
