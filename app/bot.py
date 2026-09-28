"""Telegram Bot：订阅 + 调价推送。

设计取舍（关键）：不引第三方 Bot 框架（aiogram/PTB），直接用 httpx 调 Telegram Bot API。
理由：
- 需求很简单（发消息 + 几个命令 + 接收更新），框架是杀鸡用牛刀，还得多装依赖；
- 接收更新两条路都支持：①公网 HTTPS 时用 **Webhook**（`POST /webhook/tg`，nginx 终止 TLS）；
  ②没有固定公网地址时用 **Polling**（后台线程 getUpdates）。由 `OILWATCH_TG_WEBHOOK_URL`
  是否配置自动二选一。
- token 全部从环境变量读（`OILWATCH_TG_BOT_TOKEN`）。**没配 token 时所有发送静默跳过**，
  所以本机开发、服务器没填 token 都不会崩，只是不推送。

推送策略（克制，避免骚扰）：只在"新一轮调价周期开始"时广播一次（即窗口的 next_date 变了），
同时附带订阅者所在省份的实时油价。其它时间不主动发。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

import httpx

from . import config, db, net

logger = logging.getLogger("oilwatch.bot")

_API = "https://api.telegram.org/bot{token}/{method}"


def get_token() -> str | None:
    return os.environ.get("OILWATCH_TG_BOT_TOKEN")


def get_webhook_url() -> str | None:
    return os.environ.get("OILWATCH_TG_WEBHOOK_URL")


def get_webhook_secret() -> str | None:
    return os.environ.get("OILWATCH_TG_WEBHOOK_SECRET")


def _post(token: str, method: str, payload: dict, timeout: float = 10) -> dict | None:
    url = _API.format(token=token, method=method)
    try:
        with httpx.Client(
            proxy=net.proxy_url(), verify=net.tls_verify(), timeout=timeout
        ) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001 - Telegram 偶尔抖动，失败就记一笔
        logger.warning("TG %s 失败: %s", method, exc)
        return None


def send_message(chat_id, text: str, parse_mode: str = "HTML") -> dict | None:
    token = get_token()
    if not token:
        logger.info("未配置 TG token，跳过发送 chat=%s", chat_id)
        return None
    return _post(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        },
    )


def set_webhook() -> dict | None:
    token = get_token()
    if not token:
        logger.warning("未配置 TG token，无法设置 webhook")
        return None
    url = get_webhook_url()
    if not url:
        logger.warning("未配置 OILWATCH_TG_WEBHOOK_URL，无法设置 webhook")
        return None
    data: dict = {"url": url, "drop_pending_updates": True}
    secret = get_webhook_secret()
    if secret:
        data["secret_token"] = secret
    return _post(token, "setWebhook", data)


def delete_webhook() -> dict | None:
    token = get_token()
    if not token:
        return None
    return _post(token, "deleteWebhook", {"drop_pending_updates": True})


# --- 命令解析 -----------------------------------------------------------


def _find_province(text: str):
    t = text.strip().rstrip("省").rstrip("市").strip()
    p = config.PROVINCE_BY_NAME.get(t)
    if p:
        return p
    return config.PROVINCE_BY_SLUG.get(t)


_PROVINCE_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")


def handle_text(chat_id, text: str) -> str:
    """处理一条消息文本，返回要回复的内容（None 表示不回复）。

    不碰网络；订阅状态写入 db。命令与省份识别都在这。
    """
    cmd = (text or "").strip()
    if cmd.startswith("/start") or cmd.startswith("/help"):
        db.add_subscriber(str(chat_id))
        return (
            "已订阅全国油价提醒 ⛽\n\n"
            "· 直接发「省份名」或 /province 浙江 设置你所在的省份\n"
            "· 新一轮调价窗口开启时会主动推送提醒\n"
            "· /stop 取消订阅"
        )
    if cmd.startswith("/stop"):
        db.remove_subscriber(str(chat_id))
        return "已取消订阅。需要时发 /start 重新订阅。"
    if cmd.startswith("/province"):
        name = cmd[len("/province"):].strip()
        p = _find_province(name)
        if not p:
            return "没认出这个省份，试试「浙江」「广东」这样的全称。"
        db.add_subscriber(str(chat_id), p.slug)
        return f"已把你所在的省份设为 {p.name} ✅\n调价提醒将按 {p.name} 的油价播报。"
    if _PROVINCE_RE.match(cmd):
        p = _find_province(cmd)
        if not p:
            return "没认出这个省份，试试「浙江」「广东」这样的全称。"
        db.add_subscriber(str(chat_id), p.slug)
        return f"已把你所在的省份设为 {p.name} ✅"
    if cmd.startswith("/"):
        return "未知命令。发「省份名」设置所在省份，/stop 退订。"
    return (
        "我是油价监控机器人。发「省份名」设置你所在的省份，"
        "调价前会提醒你。/stop 退订。"
    )


def dispatch_update(update: dict) -> None:
    """Webhook 与 Polling 共用的更新分发。"""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    if chat_id is None or not text:
        return
    reply = handle_text(chat_id, text)
    if reply:
        send_message(chat_id, reply)


# --- 推送 ---------------------------------------------------------------


def build_digest(snapshot: dict) -> str:
    f = snapshot.get("forecast") or {}
    selfd = f.get("self")
    site = f.get("site")
    w = snapshot.get("window")
    lines = ["⛽ 油价播报 · 新一轮调价窗口开启"]
    if selfd:
        arrow = "↑" if selfd["direction"] == "上调" else "↓"
        lines.append(
            f"下轮预计{selfd['direction']} {arrow}{abs(selfd['yuan_per_ton']):.0f} 元/吨"
        )
        lines.append(
            f"折合 92# {selfd['yuan_per_liter']['92']:+.3f} 元/升"
        )
    if site:
        if site.get("yuan_per_ton") is not None:
            lines.append(f"网站预测：{site['direction']} {site['yuan_per_ton']:.0f} 元/吨")
        elif site.get("yuan_per_liter_min") is not None:
            lines.append(
                f"网站预测：{site['direction']} "
                f"{site['yuan_per_liter_min']}-{site['yuan_per_liter_max']} 元/升"
            )
    if w:
        lines.append(
            f"下次调价：{w['next_date']}（剩 {w['workdays_remaining']} 个工作日）"
        )
    lines.append("数据来源：汽油价格网 / 新浪财经，仅供参考")
    return "\n".join(lines)


def broadcast(text: str, snapshot: dict | None = None) -> int:
    """向所有订阅者广播。返回成功发送数。被拉黑/封禁的订阅者会被清理。"""
    subs = db.list_subscribers()
    provinces = (snapshot or db.latest() or {}).get("provinces", {})
    sent = 0
    for s in subs:
        msg = text
        slug = s.get("province_slug")
        if slug and provinces.get(slug):
            p = provinces[slug]
            pr = p.get("prices") or {}
            if pr.get("92") is not None:
                msg += (
                    f"\n你所在的{p['name']}："
                    f"92# {pr['92']:.2f} 95# {pr['95']:.2f} 0# {pr['0']:.2f}"
                )
        resp = send_message(s["chat_id"], msg)
        if resp and resp.get("ok"):
            sent += 1
        elif resp and resp.get("error_code") in (403, 400):
            # 用户已拉黑机器人或聊天不可用 —— 清理掉，别反复打
            logger.info("清理失效订阅 %s", s["chat_id"])
            db.remove_subscriber(s["chat_id"])
    return sent


def maybe_push(snapshot: dict) -> dict:
    """在 schedule 刷新后调用：新一轮周期开启才广播一次。

    用 settings 里的 tg_last_cycle_next 记录上次广播过的 next_date，
    变了就播（说明已进入下一轮调价窗口），没变就不播。
    """
    if not get_token():
        return {"pushed": 0, "reason": "no_token"}
    w = snapshot.get("window")
    if not w:
        return {"pushed": 0, "reason": "no_window"}
    last = db.get_setting("tg_last_cycle_next")
    if w["next_date"] == last:
        return {"pushed": 0, "reason": "no_change"}
    text = build_digest(snapshot)
    sent = broadcast(text, snapshot)
    db.set_setting("tg_last_cycle_next", w["next_date"])
    return {"pushed": sent, "reason": "new_cycle"}


# --- Polling（无公网地址时的兜底）---------------------------------------


def start_polling() -> threading.Event | None:
    token = get_token()
    if not token:
        logger.info("未配置 TG token，不启动轮询")
        return None
    if get_webhook_url():
        logger.info("已配置 webhook，跳过轮询")
        return None
    stop = threading.Event()
    threading.Thread(target=_poll_loop, args=(token, stop), daemon=True).start()
    logger.info("TG 轮询已启动")
    return stop


def _poll_loop(token: str, stop: threading.Event) -> None:
    offset = 0
    while not stop.is_set():
        # ⚠️ 客户端超时必须 > 长轮询 timeout(30)，否则每轮都在服务端还没返回时
        #    先 read timeout，导致更新永远收不到（实测 10s 默认超时必炸）。
        resp = _post(token, "getUpdates", {"offset": offset, "timeout": 30}, timeout=40)
        if not resp or not resp.get("ok"):
            time.sleep(2)
            continue
        for u in resp.get("result", []):
            offset = u["update_id"] + 1
            try:
                dispatch_update(u)
            except Exception as exc:  # noqa: BLE001
                logger.warning("处理更新失败: %s", exc)
        time.sleep(0.3)
