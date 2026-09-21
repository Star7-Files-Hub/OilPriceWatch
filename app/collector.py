"""采集器：把第一阶段 cli.collect() 的结果落盘到缓存。

第二阶段里所有"打上游"的动作都收口到这里，调度器和手动刷新接口都调它，
保证落库逻辑只有一份。
"""
from __future__ import annotations

import logging

from . import cli, db

logger = logging.getLogger("oilwatch.collector")


def refresh() -> dict:
    """抓一次全量数据并写入缓存。返回写入的 payload。

    锚点（上次真实调价日）从 settings 读；cli.collect 会在给了 anchor 时
    自动把调价窗口算进结果里。没设 anchor 则 window 字段为空，不影响其它数据。
    """
    anchor = db.get_setting("anchor")
    data = cli.collect(anchor=anchor)
    db.save_snapshot(data)
    logger.info(
        "刷新完成 generated_at=%s 覆盖=%d/31",
        data.get("generated_at"),
        sum(1 for p in data["provinces"].values() if p.get("complete")),
    )
    return data
