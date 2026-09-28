"""统一日志配置。

⚠️ 之前**完全没有配置 logging**：根 logger 没有 handler，Python 的 lastResort 只放行
WARNING 及以上，于是 `logger.info(...)` 全部被丢弃 —— 排查 Bot 延迟时
「日志里看不到消息到达时刻」就是这个原因，白绕了一圈。

这里只给 `oilwatch.*` 这棵子树挂 handler（并 propagate=False），
不碰 root、不碰 uvicorn 自己的 logger，避免和 uvicorn 访问日志重复刷屏。
"""
from __future__ import annotations

import logging
import os

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_configured = False


def setup() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    level = os.environ.get("OILWATCH_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger("oilwatch")
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(handler)
    root.propagate = False
