"""FastAPI 主程序：对外 HTTP 接口 + 静态 H5 托管。

这是 H5 与 TG Bot 的共同底座：
- H5 拉 /api/* 拿缓存数据；
- TG Bot 后续也读同一份缓存（或调 /api/refresh 触发更新）。

公开服务三件事都在这里做：
1. 用户只读缓存（db.latest），上游只在定时任务里被打；
2. 简单按 IP 限流，挡滥用；
3. 写操作（手动刷新 / 改锚点）用 admin token 保护，避免被外部随便触发抓取。
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import collector, config, db, geolocate, bot, scheduler as _sched
from .limit import RateLimitMiddleware

logger = logging.getLogger("oilwatch.api")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
ADMIN_TOKEN = os.environ.get("OILWATCH_ADMIN_TOKEN")  # 不设则关闭保护（仅本地开发）

# 冷启动：无缓存、或缓存整体不可用（覆盖 0）时，先抓一次把数据垫上。
# 覆盖 0 也要重抓，是因为上一次抓取可能因上游问题（如证书/限流）整批失败，
# 若只判 `is None` 会把这份坏快照一直服务到下次定时刷新。
def _bootstrap() -> None:
    db.init()
    snap = db.latest()
    if snap is None or _coverage(snap) == 0:
        try:
            collector.refresh()
            logger.info("冷启动抓取成功")
        except Exception as exc:  # noqa: BLE001
            logger.warning("冷启动首次抓取失败（先返回 503，等定时任务补）：%s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap()
    _sched.start()
    _bot_poller = bot.start_polling()  # 无 webhook 时后台轮询；有 webhook 则跳过
    yield
    if _bot_poller:
        _bot_poller.set()
    _sched.shutdown()


app = FastAPI(title="OilPriceWatch", version="0.2.0", lifespan=lifespan)
app.add_middleware(RateLimitMiddleware)


def _require_admin(x_admin_token: str | None) -> None:
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")


def _coverage(snap: dict) -> int:
    return sum(1 for p in snap.get("provinces", {}).values() if p.get("complete"))


@app.get("/api/health")
def health() -> dict:
    snap = db.latest()
    return {
        "ok": True,
        "generated_at": snap.get("generated_at") if snap else None,
        "coverage": _coverage(snap) if snap else 0,
        "scheduler_running": _sched._scheduler.running,
    }


@app.get("/api/snapshot")
def snapshot() -> dict:
    snap = db.latest()
    if not snap:
        raise HTTPException(status_code=503, detail="数据尚未就绪")
    return snap


@app.get("/api/forecast")
def forecast() -> dict:
    snap = db.latest()
    if not snap:
        raise HTTPException(status_code=503, detail="数据尚未就绪")
    return {
        "generated_at": snap.get("generated_at"),
        "forecast": snap.get("forecast"),
        "window": snap.get("window"),
        "realtime": snap.get("realtime"),
    }


@app.get("/api/provinces")
def provinces() -> dict:
    snap = db.latest()
    if not snap:
        raise HTTPException(status_code=503, detail="数据尚未就绪")
    return {
        "generated_at": snap.get("generated_at"),
        "provinces": list(snap.get("provinces", {}).values()),
    }


@app.get("/api/province/{slug}")
def province(slug: str) -> dict:
    snap = db.latest()
    if not snap:
        raise HTTPException(status_code=503, detail="数据尚未就绪")
    item = snap.get("provinces", {}).get(slug)
    if not item:
        raise HTTPException(status_code=404, detail="省份不存在")
    return item


@app.get("/api/history")
def history(limit: int = 30) -> list[dict]:
    return db.recent(limit=min(max(limit, 1), 200))


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.get("/api/locate")
def locate(request: Request) -> dict:
    """按客户端 IP 粗定位到省份（省级足够，无需精确坐标）。

    经反向代理时取 X-Forwarded-For 第一个；直连取 client.host。
    定位不到（本地/海外/失败）返回 province=null，前端退回手动选择。
    """
    ip = _client_ip(request)
    slug = geolocate.ip_to_province(ip)
    prov = config.PROVINCE_BY_SLUG.get(slug) if slug else None
    return {"ip": ip, "province": slug, "name": prov.name if prov else None}


@app.post("/webhook/tg")
async def tg_webhook(request: Request) -> dict:
    """Telegram Webhook 入口（生产环境用，需公网 HTTPS）。

    校验 secret_token（配了 OILWATCH_TG_WEBHOOK_SECRET 才校验），解析更新后即时返回 200，
    发送动作同步做（量小，几百 ms 无所谓）。
    """
    secret = bot.get_webhook_secret()
    if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        data = await request.json()
    except Exception:
        return {"ok": False}
    try:
        bot.dispatch_update(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook 处理失败: %s", exc)
    return {"ok": True}


@app.get("/api/bot/status")
def bot_status() -> dict:
    return {
        "token_configured": bool(bot.get_token()),
        "mode": "webhook" if bot.get_webhook_url() else "polling",
        "subscribers": db.subscriber_count(),
    }


@app.post("/api/refresh")
def refresh(x_admin_token: str | None = Header(default=None)) -> dict:
    _require_admin(x_admin_token)
    try:
        data = collector.refresh()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"抓取失败: {exc}")
    return {"ok": True, "generated_at": data.get("generated_at")}


@app.get("/api/settings/anchor")
def get_anchor() -> dict:
    return {"anchor": db.get_setting("anchor")}


@app.put("/api/settings/anchor")
def put_anchor(anchor: str, x_admin_token: str | None = Header(default=None)) -> dict:
    _require_admin(x_admin_token)
    try:
        date.fromisoformat(anchor)
    except ValueError:
        raise HTTPException(status_code=400, detail="anchor 必须是 YYYY-MM-DD")
    db.set_setting("anchor", anchor)
    try:
        collector.refresh()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"刷新失败: {exc}")
    return {"ok": True, "anchor": anchor}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/privacy")
def privacy() -> FileResponse:
    return FileResponse(STATIC_DIR / "privacy.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
