"""定时调度：后台周期性刷新缓存。

用 APScheduler 的 BackgroundScheduler，不阻塞主进程（FastAPI 在跑）。
默认每 30 分钟刷新一次——公开服务下，用户只读缓存，上游只在这两个时间点被打。

⚠️ 调价窗口临近时（剩 1-2 个工作日）可以更频繁，但第一阶段先固定 30 分钟，
   避免把上游免费源打挂（它限流很凶）。后续可按 window.workdays_remaining 动态提速。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from . import collector, bot

logger = logging.getLogger("oilwatch.scheduler")

_scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
_DEFAULT_INTERVAL = 30  # 分钟


def _job() -> None:
    """刷新缓存，然后（若已配 TG token）在新一轮调价窗口开启时推送。"""
    try:
        data = collector.refresh()
    except Exception as exc:  # noqa: BLE001
        logger.warning("定时刷新失败: %s", exc)
        return
    try:
        result = bot.maybe_push(data)
        if result.get("pushed"):
            logger.info("已向 %d 名订阅者推送", result["pushed"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("推送失败: %s", exc)


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
    _scheduler.start()
    logger.info("调度器已启动，每 %d 分钟刷新一次", interval_minutes)


def shutdown() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("调度器已停止")
