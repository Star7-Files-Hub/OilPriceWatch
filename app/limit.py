"""极简内存限流中间件（按 IP 固定窗口计数）。

不引第三方库，公开服务够用：每个 IP 在窗口内超过阈值直接 429。
阈值偏宽松（默认 60/分钟），主要挡爬虫和滥用，不挡正常用户。

⚠️ 多进程/多实例部署时本内存计数不共享——那时应换 Redis 或反向代理层限流。
   现阶段单进程 uvicorn 跑，没问题。
"""
from __future__ import annotations

import json
import time
from collections import defaultdict

from starlette.types import ASGIApp, Receive, Scope, Send


class RateLimitMiddleware:
    def __init__(
        self, app: ASGIApp, max_requests: int = 60, window_seconds: int = 60
    ) -> None:
        self.app = app
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._last_sweep = time.time()

    def _client_ip(self, scope: Scope) -> str:
        client = scope.get("client")
        if client:
            return client[0]
        # 经过反向代理时取 X-Forwarded-For 第一个
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                return value.decode("utf-8", "ignore").split(",")[0].strip()
        return "unknown"

    def _allow(self, ip: str) -> bool:
        now = time.time()
        # 周期性清旧，避免 dict 无限增长
        if now - self._last_sweep > self.window_seconds:
            self._hits = defaultdict(list)
            self._last_sweep = now

        window = [t for t in self._hits[ip] if now - t < self.window_seconds]
        window.append(now)
        self._hits[ip] = window
        return len(window) <= self.max_requests

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        ip = self._client_ip(scope)
        if not self._allow(ip):
            body = json.dumps({"error": "too_many_requests"}).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", b"60"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)
