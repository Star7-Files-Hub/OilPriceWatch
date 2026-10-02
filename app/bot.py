"""Telegram Bot：订阅 + 调价推送。

🔴 **这个 bot 的 token 是多个项目共用的**（tg-assistant 的通知机器人用的就是它）。
   共用意味着两件事，改这个文件之前必须先读懂，否则会直接把别人的机器人搞坏：

   1. **命令面必须独占命名空间**。裸命令（``/start`` ``/help`` ``/status``…）是所有项目
      都想抢的公共名字，撞上就是互相覆盖回复。本项目一律用 ``/oil`` 前缀
      （见 :data:`_COMMANDS`），**匹配不到就完全沉默** —— 不回「未知命令」、不回欢迎语、
      不做任何默认话术。别的项目爱用什么命令是它们的事，我们连问都不问。
      （同一条约定在 tg-assistant 侧写成了 ``tg_assistant/bot_commands.py`` 的开头注释。）

   2. **绝不碰全局状态**。``getUpdates`` 的 ``offset``、``setWebhook``/``deleteWebhook``、
      ``setMyCommands`` 全是**按 token 全局唯一**的：推进 offset 会把别的项目该收到的
      更新一并确认掉，``setWebhook`` 会直接掐断别人的 getUpdates 通道。所以：
      · 轮询**永远不传 offset**（见 :func:`_poll_loop`），只做本地去重；
      · ``set_webhook`` / ``delete_webhook`` 默认**拒绝执行**，需显式 opt-in。

   发送类接口（``sendMessage``）不动全局状态，随便用。

设计取舍（关键）：不引第三方 Bot 框架（aiogram/PTB），直接用 httpx 调 Telegram Bot API。
理由：
- 需求很简单（发消息 + 几个命令 + 接收更新），框架是杀鸡用牛刀，还得多装依赖；
- 接收更新两条路都支持：①公网 HTTPS 时用 **Webhook**（`POST /webhook/tg`，nginx 终止 TLS）；
  ②没有固定公网地址时用 **Polling**（后台线程 getUpdates，**不推进 offset**）。
  由 `OILWATCH_TG_WEBHOOK_URL` 是否配置自动二选一（共用 bot 上 webhook 是全局状态，
  只能由某一个项目设置，本项目不会主动去设）。
- token 全部从环境变量读（`OILWATCH_TG_BOT_TOKEN`）。**没配 token 时所有发送静默跳过**，
  所以本机开发、服务器没填 token 都不会崩，只是不推送。

推送策略（克制，避免骚扰）：只在"新一轮调价周期开始"时广播一次（即窗口的 next_date 变了），
同时附带订阅者所在省份的实时油价。其它时间不主动发。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from . import config, db, net

logger = logging.getLogger("oilwatch.bot")

_API = "https://api.telegram.org/bot{token}/{method}"

# --- 共享 HTTP 连接池（关键性能点）----------------------------------------
# ⚠️ 之前每次调用都 `with httpx.Client(...)` 新建客户端，代价是**每次都要重做一遍
#    TCP + TLS 握手**（实测服务器→api.telegram.org 新建连接 ~0.5s，复用 ~0.15s）。
#    后果有两个：
#    1. 每条回复的 sendMessage 白等 ~0.4s；
#    2. 更糟的是 getUpdates 每轮都换新连接，握手那 ~0.4s 里**根本没在收消息**，
#       用户恰好在那个窗口发消息就得多等一轮（再叠加 0.3s sleep）。
#    httpx.Client 官方保证线程安全，可以全局复用一个。按请求传 timeout 即可。
_client_lock = threading.Lock()
_client: httpx.Client | None = None


def _client_get() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(
                proxy=net.proxy_url(),
                verify=net.tls_verify(),
                timeout=40,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                headers={"Connection": "keep-alive"},
            )
        return _client


def get_token() -> str | None:
    return os.environ.get("OILWATCH_TG_BOT_TOKEN")


def get_webhook_url() -> str | None:
    return os.environ.get("OILWATCH_TG_WEBHOOK_URL")


def get_webhook_secret() -> str | None:
    return os.environ.get("OILWATCH_TG_WEBHOOK_SECRET")


def _post(
    token: str,
    method: str,
    payload: dict,
    timeout: float = 10,
    *,
    retry_transport: bool = False,
) -> dict | None:
    """调一次 Bot API。

    retry_transport：复用的 keep-alive 连接可能已被对端半关（长轮询连接尤其常见），
    这种 TransportError 立刻换连接重试一次即可。**只对幂等的 getUpdates 开**——
    sendMessage 若在响应阶段超时，重试会重复发一条，宁可丢一次也不重复。
    """
    url = _API.format(token=token, method=method)
    started = time.monotonic()
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            resp = _client_get().post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - Telegram 偶尔抖动，失败就记一笔
            last_exc = exc
            if retry_transport and attempt == 0 and isinstance(exc, httpx.TransportError):
                continue
            break
    logger.warning(
        "TG %s 失败(耗时 %.2fs): %s", method, time.monotonic() - started, last_exc
    )
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


def _webhook_allowed() -> bool:
    """共用 bot 上是否允许改动 webhook —— 默认**不允许**，需显式 opt-in。

    为什么默认拒绝：``setWebhook`` 改的是**按 token 全局唯一**的投递方式，一旦设置，
    共用这个 token 的其它项目立刻收不到任何 getUpdates（Telegram 会返回 409）。
    本项目自己的服务器上根本没有 HTTPS 域名，也不需要 webhook，
    所以最安全的默认就是「不碰」。哪天真要单独用这个 bot 了，
    再设 ``OILWATCH_TG_ALLOW_WEBHOOK=1`` 并确认它已不再被别的项目共用。
    """
    return os.environ.get("OILWATCH_TG_ALLOW_WEBHOOK") == "1"


def set_webhook() -> dict | None:
    """设置 webhook —— 🔴 共用 bot 上的**全局破坏性**操作，默认拒绝。"""
    if not _webhook_allowed():
        logger.error(
            "拒绝 setWebhook：此 bot 与其它项目共用 webhook 是全局状态，"
            "会把别的项目的 getUpdates 掐断。确需设置请显式设 "
            "OILWATCH_TG_ALLOW_WEBHOOK=1 并确认该 bot 已不再共用。"
        )
        return None
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
    """删除 webhook —— 同样是全局状态（还会 ``drop_pending_updates`` 清队列），默认拒绝。"""
    if not _webhook_allowed():
        logger.error(
            "拒绝 deleteWebhook：此 bot 与其它项目共用，"
            "删别人的 webhook / 丢别人的待处理更新都可能直接搞坏对方。"
        )
        return None
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


def _find_province_by_slug(slug: str):
    """按 slug 反查省份（待确认订阅里存的是 slug，确认时要还原成省份）。"""
    return config.PROVINCE_BY_SLUG.get(slug)


_PROVINCE_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")

#: 本项目在共用 bot 上**独占**的命名空间前缀。所有命令都挂在这下面。
_NAMESPACE = "/oil"

#: 命名空间内认得的命令 → 动作。**只有这张表里的才回应。**
_COMMANDS: dict[str, str] = {
    "/oil": "help",
    "/oil_help": "help",
    "/oil_today": "today",
    "/oil_start": "start",
    "/oil_stop": "stop",
    "/oil_province": "province",
}

def _help_text() -> str:
    """帮助文案。**用函数而不是常量**：每天几点推是可以配的（``daily_hour()``），
    写成模块级常量会在导入期就把时间点钉死（甚至 NameError）。"""
    return (
        "⛽ 油价监控 · 可用命令（都带 /oil 前缀，避免和别的机器人撞车）\n\n"
        "· /oil_today 现在往哪走（今日走向）\n"
        "· /oil_start 订阅每日油价走向\n"
        "· /oil_start 浙江 订阅并指定省份\n"
        "· /oil_province 浙江 改省份\n"
        "· /oil_stop 取消订阅\n"
        f"· 私聊里直接发「省份名」（如「浙江」）也行：会先问你一句确认再订阅"
        "（群里不认，免得误伤别人的对话）\n\n"
        f"每天 {daily_hour()}:00 推一次当日走向"
        "（新一轮调价窗口开启时另有一次提醒），其余时间不打扰。"
    )

_UNKNOWN_OIL_TEXT = (
    "这条命令我不认识。可用：\n"
    "· /oil_today 今日走向\n"
    "· /oil_start [省份] 订阅\n"
    "· /oil_province 省份 改省份\n"
    "· /oil_stop 取消订阅\n"
    "· /oil_help 看说明"
)

#: 「是」/「否」的口语说法。**只在有本项目待确认订阅时**才会被响应（见 handle_text），
#: 而且只认孤立的短词 —— 别把「是的我在北京」这种闲聊也算进来。
_CONFIRM_YES = frozenset(
    {"是", "是的", "好", "好的", "要", "确认", "订阅", "同意", "y", "yes", "ok"}
)
_CONFIRM_NO = frozenset(
    {"否", "不", "不用", "不要", "别", "取消", "算了", "n", "no"}
)


def is_confirmation(text: str) -> bool:
    """这句话是不是「是/否」这类确认词（大小写、首尾空白不敏感）。"""
    return (text or "").strip().lower() in (_CONFIRM_YES | _CONFIRM_NO)


def _subscribed_reply(p) -> str:
    return (
        f"已订阅 ⛽\n省份：{p.name}\n"
        f"每天 {daily_hour()}:00 推当日油价走向，"
        "新一轮调价窗口开启时另有一次提醒。/oil_stop 退订。"
    )


def parse_command(text: str) -> tuple[str, str] | None:
    """把一条消息解析成「**本项目自己的**命令」。

    返回 ``(动作, 参数)``；**不是本项目的消息一律返回 None**，调用方必须保持完全沉默。
    动作取值：``help`` / ``start`` / ``stop`` / ``province`` / ``province_plain`` / ``unknown``。

    只认两类输入，别的连看都不看：

    1. ``/oil`` 命名空间下的命令（``/oil_start``、``/oil_province 浙江``…）。
       顺带剥掉群里客户端自动补的 ``@botusername`` 后缀。
       **非 ``/oil`` 开头的命令（``/status``、``/start``、``/help``…）一律返回 None** ——
       那是别的项目的命令，我们既不回复也不记日志刷屏。
    2. **恰好等于某个省份名**的纯文本（``浙江`` / ``广东省``），这是本项目的省份设置入口。
       其它任何文本（闲聊、别的项目的关键词）都返回 None。

    ⚠️ 纯省份名与 ``/oil_province`` 必须**分成两个动作**（``province_plain`` / ``province``）：
       前者的处理更保守（只在私聊认、且要先问一句确认），后者是用户明确敲的命令，
       直接生效。以前两者都返回 ``province``，逻辑上根本分不开。

    ⚠️ 这就是「只回自己的命令」的落点：命名空间内的未知命令才给提示（撞不到别人），
       命名空间外的世界一律沉默 —— **绝不回「未知命令」这种公共话术**。
    """
    cmd = (text or "").strip()
    if not cmd:
        return None

    if cmd.startswith("/"):
        head, _, rest = cmd.partition(" ")
        # 群里发的命令会带 ``@botusername`` 后缀，剥掉再匹配。
        head = head.split("@", 1)[0].lower()
        if not head.startswith(_NAMESPACE):
            return None  # 别的项目的命令 ⇒ 沉默
        action = _COMMANDS.get(head)
        if action is None:
            return ("unknown", "")  # 自己的命名空间，可以给个提示
        return (action, rest.strip())

    if _PROVINCE_RE.match(cmd) and _find_province(cmd):
        return ("province_plain", cmd)
    return None


def handle_text(chat_id, text: str, chat_type: str = "private") -> str | None:
    """处理一条消息文本，返回要回复的内容（**None 表示不回复，必须沉默**）。

    不碰网络；订阅状态写入 db。命令识别全部委托给 :func:`parse_command`，
    这里只负责「认得的命令该回什么」。

    ``chat_type`` 是 Telegram 的 ``chat.type``（``private`` / ``group`` /
    ``supergroup`` / ``channel``）。**纯省份名只在私聊里认**：在群里，任何人随手发一句
    「北京」都不该被这个共用 bot 接过去订阅、更不该往群里回一句油价话术。
    显式命令（``/oil_*``）不区分场合，照常工作。
    """
    # 「是/否」这类确认词先处理：它们不是命令（parse_command 会返回 None），
    # 但只有**本项目先问过一句**（有 pending 记录）时才会被响应，否则沉默。
    if is_confirmation(text):
        return _handle_confirmation(chat_id, text)

    parsed = parse_command(text)
    if parsed is None:
        return None
    action, arg = parsed

    if action == "help":
        return _help_text()

    if action == "unknown":
        return _UNKNOWN_OIL_TEXT

    if action == "today":
        # 手动查「现在往哪走」—— 不依赖推送，随时可看（也是推送出问题时的自检入口）。
        snap = db.latest()
        if not snap or not snap.get("window"):
            return "数据还没准备好（上游刚抓取失败或服务刚启动），过几分钟再试。"
        return build_digest(snap, title=f"⛽ 今日油价走向 · {_now_cn().date().isoformat()}")

    if action == "stop":
        db.remove_subscriber(str(chat_id))
        db.clear_pending_subscription(str(chat_id))
        return "已取消订阅。需要时发 /oil_start 重新订阅。"

    if action == "province_plain":
        # 🔴 群里一律不认（也不订阅、也不回复）：这是共用 bot 上最容易误伤别人的入口。
        if chat_type != "private":
            logger.debug(
                "群里收到纯省份名，按约定静默忽略 chat=%s type=%s", chat_id, chat_type
            )
            return None
        p = _find_province(arg)
        if not p:
            return None
        # 只记「意图」，等用户明确回一句「是」才真订阅。
        db.set_pending_subscription(str(chat_id), p.slug)
        return (
            f"📍 要订阅【{p.name}】的每日油价走向吗？\n"
            "回复「是」确认，回复「否」取消。\n"
            f"（也可以直接用 /oil_start {p.name} 订阅）"
        )

    if action == "start":
        if arg:
            p = _find_province(arg)
            if not p:
                return "没认出这个省份，试试「浙江」「广东」这样的全称。"
            db.add_subscriber(str(chat_id), p.slug)
            db.clear_pending_subscription(str(chat_id))
            return _subscribed_reply(p)
        db.add_subscriber(str(chat_id))
        return (
            f"已订阅 ⛽\n\n"
            f"· 每天 {daily_hour()}:00 推当日油价走向\n"
            "· 新一轮调价窗口开启时另有一次提醒\n"
            "· 发「省份名」或 /oil_province 浙江 设置你所在的省份（群里请用命令，纯省份名只在私聊生效）\n"
            "· /oil_today 随时查看今日走向\n"
            "· /oil_stop 取消订阅"
        )

    # action == "province"（显式 /oil_province 浙江：用户敲了明确命令 ⇒ 直接生效）
    p = _find_province(arg)
    if not p:
        return "没认出这个省份，试试「浙江」「广东」这样的全称。"
    db.add_subscriber(str(chat_id), p.slug)
    db.clear_pending_subscription(str(chat_id))
    return f"已把你所在的省份设为 {p.name} ✅\n调价提醒将按 {p.name} 的油价播报。"


def _handle_confirmation(chat_id, text: str) -> str | None:
    """处理「是/否」：**只有本项目先问过一句**（存在 pending 记录）时才回应。

    没有 pending 就返回 None（沉默）—— 共用 bot 上别人的对话里出现「是」太常见了，
    我们没有任何资格去接话。pending 只可能由 :func:`handle_text` 的私聊省份分支写下。
    """
    slug = db.get_pending_subscription(str(chat_id))
    if not slug:
        return None

    if (text or "").strip().lower() in _CONFIRM_NO:
        db.clear_pending_subscription(str(chat_id))
        return "好的，已取消，不会给你推送。"

    p = _find_province_by_slug(slug)
    if not p:
        # 省份表变了导致认不出来：清掉待办，别把用户卡在一个永远确认不了的状态里。
        db.clear_pending_subscription(str(chat_id))
        return "这个省份暂时认不出来了，请用 /oil_start 重新订阅。"
    db.add_subscriber(str(chat_id), p.slug)
    db.clear_pending_subscription(str(chat_id))
    return _subscribed_reply(p)


def dispatch_update(update: dict) -> None:
    """Webhook 与 Polling 共用的更新分发。

    🔴 **不是本项目的更新一律静默丢弃**（只留一行 debug）：这个 bot 被多个项目共用，
    踢回来的任何一条更新都可能是别人的命令，我们没有任何资格去回一句
    「未知命令」—— 那正是之前 `/status` 被抢答成油价欢迎语的原因。

    「是/否」这类确认词也放行进 :func:`handle_text`，但**只有本项目先问过一句**
    （该 chat 有 pending 订阅记录）时才会真回复，否则那一层直接返回 None ——
    共用 bot 上别人的对话里出现「是」太常见，不能接话。
    """
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    # Telegram 一定会给 chat.type；缺字段时按**最保守**的群聊处理（纯省份名不认）。
    chat_type = chat.get("type") or "group"
    text = (message.get("text") or "").strip()
    if chat_id is None or not text:
        return
    if parse_command(text) is None and not is_confirmation(text):
        logger.debug("非本项目命令，静默忽略 chat=%s text=%r", chat_id, text[:30])
        return
    t0 = time.monotonic()
    logger.info(
        "收到本项目消息 chat=%s type=%s text=%r", chat_id, chat_type, text[:30]
    )
    reply = handle_text(chat_id, text, chat_type)
    if reply:
        send_message(chat_id, reply)
    logger.info(
        "已回复 chat=%s 端到端 %.3fs（含发送）", chat_id, time.monotonic() - t0
    )


# --- 推送 ---------------------------------------------------------------


def build_digest(snapshot: dict, title: str = "⛽ 油价播报 · 新一轮调价窗口开启") -> str:
    f = snapshot.get("forecast") or {}
    selfd = f.get("self")
    site = f.get("site")
    w = snapshot.get("window")
    lines = [title]
    if selfd:
        arrow = "↑" if selfd["direction"] == "上调" else "↓"
        lines.append(
            f"下轮预计{selfd['direction']} {arrow}{abs(selfd['yuan_per_ton']):.0f} 元/吨"
        )
        lines.append(
            f"折合 92# {selfd['yuan_per_liter']['92']:+.3f} 元/升"
        )
    if site:
        # 网站只给元/升时由引擎折算成元/吨（见 forecast.site_yuan_per_ton）。
        # ⚠️ `site_yuan_per_ton` 是后加的字段，数据库里的旧快照没有它
        #    ⇒ 回退到网站原文的元/吨，别让老快照的推送少一行。
        ton = f.get("site_yuan_per_ton")
        if ton is None:
            ton = site.get("yuan_per_ton")
        if ton is not None:
            mark = "（折算）" if f.get("site_yuan_per_ton_source") == "derived" else ""
            lines.append(f"网站预测：{site['direction']} {ton:.0f} 元/吨{mark}")
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
                p98 = pr.get("98")
                p98s = f" 98# {p98:.2f}" if p98 is not None else ""
                msg += (
                    f"\n你所在的{p['name']}："
                    f"92# {pr['92']:.2f} 95# {pr['95']:.2f}{p98s} 0# {pr['0']:.2f}"
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
    """新一轮调价窗口开启时广播一次。

    🔴 **幂等键只在「确实送出去」之后才落**（`sent > 0`）。旧写法反过来：先写键、
    再广播，于是只要「该推的那一刻还没有订阅者」或「发送全军覆没」，整轮就被
    永久静默掉了 —— 线上 2026-09-28 正是如此：21:14 首次刷新得到
    next_date=2026-10-15，21:43 的巡检在没有**任何**订阅者时把键写了，
    用户 22:16 才订阅，结果接下来 17 天一条推送都收不到。
    现在改成：没送出去就不落键 ⇒ 下一个 30 分钟的巡检会继续重试，不会静默度过这一轮。
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
    if sent <= 0:
        logger.info(
            "新一轮窗口已开启但无人可送（订阅者 %d 个）⇒ 不落幂等键，下次巡检重试",
            db.subscriber_count(),
        )
        return {"pushed": 0, "reason": "no_recipient"}
    total = db.subscriber_count()
    if sent < total:
        logger.warning(
            "新一轮窗口推送部分失败：%d/%d 名订阅者收到", sent, total
        )
    db.set_setting("tg_last_cycle_next", w["next_date"])
    return {"pushed": sent, "reason": "new_cycle"}


# --- 每日「油价走向」推送 -------------------------------------------------
#
# 用户要的是「每天告诉我往哪走」，而 :func:`maybe_push` 只在**调价周期切换**时响一次
# （每 10 个工作日）—— 两者是互补的，都保留。

#: 每日推送的幂等键：存**已成功推送**的日期（YYYY-MM-DD，Asia/Shanghai）。
_DAILY_KEY = "tg_last_daily_date"

#: 每日推送的默认时间（小时，Asia/Shanghai）。
DEFAULT_DAILY_HOUR = 8


def daily_hour() -> int:
    """每日推送的时间点（小时）。可用 ``OILWATCH_TG_DAILY_HOUR`` 覆盖。"""
    raw = os.environ.get("OILWATCH_TG_DAILY_HOUR")
    if raw is None or not raw.strip():
        return DEFAULT_DAILY_HOUR
    try:
        hour = int(raw)
    except ValueError:
        logger.warning("OILWATCH_TG_DAILY_HOUR=%r 不是整数，回退到 %d", raw, DEFAULT_DAILY_HOUR)
        return DEFAULT_DAILY_HOUR
    if not 0 <= hour <= 23:
        logger.warning("OILWATCH_TG_DAILY_HOUR=%r 超出 0-23，回退到 %d", raw, DEFAULT_DAILY_HOUR)
        return DEFAULT_DAILY_HOUR
    return hour


def _now_cn() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def maybe_daily_push(snapshot: dict, now: datetime | None = None) -> dict:
    """每日「油价走向」：每个自然日最多成功推一次。

    什么时候推：**当天还没成功推过**，且**已经过了配置的时间点**。
    为什么不是「只在整点那一分钟推」：巡检是每 30 分钟一次、服务还可能重启，
    卡死某个分钟极易漏推。用「>= 时间点 + 当天未推」表达，天然带**补推**能力
    （服务在 08:00 挂了、10:00 才起来，当天仍然会补上）。

    幂等键与 :func:`maybe_push` 同一条纪律：**送出去了才落**，没订阅者/全失败就保持
    待发，下一次巡检接着试。
    """
    if not get_token():
        return {"pushed": 0, "reason": "no_token"}
    if not snapshot.get("window"):
        return {"pushed": 0, "reason": "no_window"}

    now = now or _now_cn()
    today = now.date().isoformat()
    if now.hour < daily_hour():
        return {"pushed": 0, "reason": "before_hour"}
    if db.get_setting(_DAILY_KEY) == today:
        return {"pushed": 0, "reason": "already_sent_today"}

    text = build_digest(snapshot, title=f"⛽ 今日油价走向 · {today}")
    sent = broadcast(text, snapshot)
    if sent <= 0:
        logger.info(
            "每日油价走向无人可送（订阅者 %d 个）⇒ 不落幂等键，下次巡检重试",
            db.subscriber_count(),
        )
        return {"pushed": 0, "reason": "no_recipient"}
    # ⚠️ `sent > 0` 的准确含义是「**至少**成功送出一个」。2 个订阅者只成功 1 个时，
    #    另一个当天就静默丢了 —— 落键前必须把这个部分失败**喊出来**，
    #    否则它和「全部成功」在日志上完全一样（这正是 9/28 那次事故难以发现的同款盲区）。
    total = db.subscriber_count()
    if sent < total:
        logger.warning(
            "每日油价走向部分失败：%d/%d 名订阅者收到（其余今天不会再补发）",
            sent,
            total,
        )
    db.set_setting(_DAILY_KEY, today)
    return {"pushed": sent, "reason": "daily"}


# --- Polling（无公网地址时的兜底）---------------------------------------
#
# 🔴 **本模块的轮询不推进 offset**，这是刻意设计而不是忘了传参数，理由见 _poll_loop。

#: 本地去重表在 settings 里的键名。
_SEEN_KEY = "tg_seen_update_ids"

#: 本地去重表最多记多少个 update_id（超出丢最老的）。
#: 为什么不推进 offset 也不会漏自己的命令、也不会重复回复：见 :func:`_poll_loop`。
_SEEN_CAP = 500

#: getUpdates 单次要多少条（Telegram 上限 100）。
_FETCH_LIMIT = 100

#: 积压提示的最小间隔（秒）。没有它时**每轮**都打，4 天刷了 63896 行日志。
_BACKLOG_WARN_INTERVAL = 3600.0


def _offset_enabled() -> bool:
    """是否允许推进 offset。**默认关闭**，这是共用 bot 的安全档。

    为什么要留这个开关：不推进 offset ⇒ Telegram 永不删除更新 ⇒ 频道贴
    （tg-assistant 通知频道的 channel_post）会**长期把一页(100 条)顶满**
    （2026-10-02 线上采样：抽到的历史页面里 68% 正好取满 100 条），
    此时最新的几条（很可能就是用户的命令）要等老更新 24 小时过期后才可见
    —— **不会丢，但会延迟**（典型十几分钟，突发时可达数小时）。
    打开后会确认水位、把积压清掉，长轮询也才真正生效（命令秒级可达、日志干净）。

    ⚠️ 打开的前提：**确认这个 token 上再没有别的项目用 Bot API 的 getUpdates**。
    线上 tg-assistant 走 pyrogram/MTProto 用户账号转发，不受影响；但这是「共用
    token」的约定，必须由人确认后再开（见 README「机器人是共用的」）。
    """
    return os.environ.get("OILWATCH_TG_ALLOW_OFFSET", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _initial_offset(seen: list[int]) -> int | None:
    """开闸时从本地去重表推出起始水位；没开闸（默认）返回 None ⇒ 不传 offset。"""
    if not _offset_enabled() or not seen:
        return None
    return max(seen) + 1

#: 没有新消息时的退避秒数。可调：``OILWATCH_TG_POLL_IDLE_SLEEP``。
_DEFAULT_IDLE_SLEEP = 2.0


def _idle_sleep() -> float:
    raw = os.environ.get("OILWATCH_TG_POLL_IDLE_SLEEP")
    if not raw:
        return _DEFAULT_IDLE_SLEEP
    try:
        return max(0.2, float(raw))
    except ValueError:
        return _DEFAULT_IDLE_SLEEP


def _load_seen() -> list[int]:
    """从 settings 读出已处理过的 update_id。

    为什么要持久化：不推进 offset ⇒ 同一个更新会被 Telegram **反复**返回，
    进程重启后如果去重表丢了，就会把老消息再回一遍（用户看到重复回复）。
    """
    raw = db.get_setting(_SEEN_KEY)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    out: list[int] = []
    for it in items if isinstance(items, list) else []:
        try:
            out.append(int(it))
        except (TypeError, ValueError):
            continue
    return out[-_SEEN_CAP:]


def _save_seen(seen: list[int]) -> None:
    db.set_setting(_SEEN_KEY, json.dumps(seen[-_SEEN_CAP:]))


def start_polling() -> threading.Event | None:
    token = get_token()
    if not token:
        logger.info("未配置 TG token，不启动轮询")
        return None
    if get_webhook_url():
        logger.warning(
            "已配置 OILWATCH_TG_WEBHOOK_URL ⇒ 跳过轮询，改由 /webhook/tg 收更新。"
            "⚠️ 此 bot 与其它项目共用，webhook 是全局状态；本项目不会主动去设它。"
        )
        return None
    stop = threading.Event()
    threading.Thread(target=_poll_loop, args=(token, stop), daemon=True).start()
    logger.info(
        "TG 轮询已启动（%s；只处理 /oil* 命令）",
        "**开闸**：会推进 offset，影响共用该 token 的其它 getUpdates 消费者"
        if _offset_enabled()
        else "非侵入式：不推进 offset、不设 allowed_updates",
    )
    return stop


def _poll_loop(token: str, stop: threading.Event) -> None:
    """后台轮询。**绝不传 offset** —— 这是本文件最重要的一个约定。

    为什么：``offset`` 是**按 token 全局唯一**的确认水位。传了它，Telegram 会把
    所有 ``update_id < offset`` 的更新一并标记为已确认并删除 —— 共用这个 token 的
    其它项目（tg-assistant 等）就永远收不到自己那几条了。这正是 tg-assistant 侧
    ``bot_commands.py`` 开头写死的那条约定。

    不传 offset 的代价与对策：
    · Telegram 会**反复返回同一批未确认更新** ⇒ 本地用 update_id 去重（持久化到
      settings，重启也不重复回复）；取回来全是老消息时主动退避 ``idle_sleep``，
      避免空转打 API。
    · 触发长轮询的前提是「队列里没有未确认更新」；有老消息压着时接口会立刻返回，
      所以那点退避是必须的，不是为了省流量。

    🔴 **也绝不能传 ``allowed_updates``** —— 这是 2026-10-02 实测踩到的第二个坑，
    比 offset 更阴：它**看起来**只是「本次只收某几类更新」，但 Telegram 会把它
    **写成按 token 全局且持久**的订阅设置（``getWebhookInfo`` 的 ``allowed_updates``
    字段会跟着变），而且**之后不传该参数并不会恢复**（官方文档原话：
    "If not specified, the previous setting will be used."）。
    实测：传 ``["callback_query"]`` ⇒ 全局变成 ``["callback_query"]``；再调用一次
    **不带**该参数，全局仍是 ``["callback_query"]``。而且它**功能上真的会压制**其它类型
    （隔离实验：设成「除 channel_post 外全部」后，频道发送方计数 +1 而对应更新始终没进队列）。
    后果是别的项目（或将来新增的 callback/频道场景）会**静默**收不到那类更新，
    且极难排查 —— 与「绝不碰全局状态」的红线直接冲突。

    ⇒ 结论：这个共用 bot 的订阅面**只能保持默认**（官方默认 = 全部类型，**除**
      ``chat_member`` / ``message_reaction`` / ``message_reaction_count``）；
      频道贴(``channel_post``)刷屏只能忍。它的真实代价（2026-10-02 线上测量，
      经独立验证者复核）：
      · 页面**经常被顶满**：抽取的历史日志里 **68% 的页面正好 100 条**（=limit），
        说明队列长期贴着上限（当天 15 次低谷采样 93~97，不代表常态）；
      · 超限时被截掉的是**最新**的更新（getUpdates 从最老未确认开始返回）⇒
        用户的命令**看不见但不会丢**：等更老的更新 24 小时过期腾出位置后仍会被取到
        （典型延迟十几分钟，突发时可达数小时，最坏接近 24h）。
        想彻底消除这个延迟，见 :func:`_offset_enabled`（``OILWATCH_TG_ALLOW_OFFSET=1``）。

    ⚠️ 还原 ``allowed_updates`` 的判据陷阱：官方文档写明该参数
    "doesn't affect updates created before the call"，而本服务**从不确认**任何更新
    ⇒ 老积压永远算「调用之前创建的更新」⇒ **无论过滤是否生效，channel_post 都会照常返回**。
    所以「还原后又能收到 channel_post」**不构成**还原成功的证据；唯一有效判据是
    ``getWebhookInfo.allowed_updates`` 字段本身是否回到缺失/空。
    （还原写法：``getUpdates`` 传 ``allowed_updates=[]``，空列表即官方默认。）

    ⚠️ 另一个实测现象：**同一 token 上并发 getUpdates 会互相抢页** —— 串行调用稳定返回
    整页，并发 3 个请求时出现过 ``(99, 1, 1)``，落单者只拿到最老那一条。
    所以排查时别一边跑服务一边手工 getUpdates，会把服务的页面抢成残页。

    不传 offset 的代价与对策：
    · Telegram 会**反复返回同一批未确认更新** ⇒ 本地用 update_id 去重（持久化到
      settings，重启也不重复回复）；取回来全是老消息时主动退避 ``idle_sleep``，
      避免空转打 API。
    · 触发长轮询的前提是「队列里没有未确认更新」；有老消息压着时接口会立刻返回，
      所以那点退避是必须的，不是为了省流量。
    """
    seen = _load_seen()
    seen_set = set(seen)
    idle = _idle_sleep()
    last_backlog_warn = 0.0
    backlog_warned = False
    # None = 不传 offset（默认档，共用 bot 的安全档）；整数 = 要确认到的水位（显式开闸）。
    offset = _initial_offset(seen)
    logger.info(
        "轮询去重表已载入 %d 条历史 update_id（推进 offset：%s）",
        len(seen),
        "开" if offset is not None else "关",
    )

    while not stop.is_set():
        # ⚠️ 客户端超时必须 > 长轮询 timeout(30)，否则每轮都在服务端还没返回时
        #    先 read timeout，导致更新永远收不到（实测 10s 默认超时必炸）。
        # ⚠️ 默认既不传 offset，也**从不传 allowed_updates**（两者都是全局状态）。
        payload = {"timeout": 30, "limit": _FETCH_LIMIT}
        if offset is not None:
            payload["offset"] = offset
        resp = _post(
            token,
            "getUpdates",
            payload,
            timeout=40,
            retry_transport=True,
        )
        if not resp or not resp.get("ok"):
            # 409 的典型原因：有人给这个共用 token 设了 webhook ⇒ getUpdates 被禁。
            # 这时**绝不能**去 deleteWebhook（那也是全局状态，会搞坏对方），只能报出来让人处理。
            time.sleep(idle)
            continue

        updates = resp.get("result", [])
        if len(updates) >= _FETCH_LIMIT:
            # 这个 bot 同时是 tg-assistant 通知频道 Notify(-1002626018568) 的成员，
            # 频道每发一条就产生一条 channel_post，把这一页占满。属**预期噪音**：
            # 线上抽样：68% 的页面正好 100 条 ⇒ 队列长期贴着上限，最新的更新
            # 要等老更新 24h 过期后才可见（**延迟，不丢**）；想彻底消除见 _offset_enabled。
            # ⚠️ 曾经每轮都打这行 ⇒ 4 天刷了 63896 行日志。现在：进程内第一次 WARNING，
            #    之后每小时最多 INFO 一条。
            now = time.monotonic()
            if not backlog_warned:
                backlog_warned = True
                last_backlog_warn = now
                logger.warning(
                    "未确认更新已压到一页上限 %d 条（共用 bot 收到大量频道贴，属预期）；"
                    "不推进 offset 也不设 allowed_updates ⇒ 页面长期贴着上限，"
                    "此时**最新**的更新要等老更新 24h 过期后才可见（延迟，不丢）。"
                    "想消除延迟见 OILWATCH_TG_ALLOW_OFFSET。",
                    len(updates),
                )
            elif now - last_backlog_warn >= _BACKLOG_WARN_INTERVAL:
                last_backlog_warn = now
                logger.info("未确认更新仍压在一页上限（%d 条）", len(updates))

        fresh = [u for u in updates if u.get("update_id") not in seen_set]
        if not fresh:
            # 全是已处理过的老消息（未确认所以被反复返回）—— 退避，别空转。
            time.sleep(idle)
            continue

        logger.info("取到 %d 条新更新（未确认池共 %d 条）", len(fresh), len(updates))
        for u in fresh:
            uid = u.get("update_id")
            if isinstance(uid, int):
                seen.append(uid)
                seen_set.add(uid)
            # 先记账再处理：处理失败也不重放，免得一条毒更新被无限重试。
            try:
                dispatch_update(u)
            except Exception as exc:  # noqa: BLE001
                logger.warning("处理更新失败: %s", exc)

        # 去重表封顶，防止 settings 无限增长。
        while len(seen) > _SEEN_CAP:
            seen_set.discard(seen.pop(0))
        try:
            _save_seen(seen)
        except Exception as exc:  # noqa: BLE001
            logger.warning("持久化去重表失败（本次仍生效）: %s", exc)

        # 仅当显式开闸（OILWATCH_TG_ALLOW_OFFSET=1）才推进确认水位。
        # 推进后 Telegram 会把这些更新彻底删掉、长轮询也随之生效（队列排空、命令秒级可达）。
        if offset is not None and updates:
            offset = max(u.get("update_id", 0) for u in updates) + 1

        # 复用连接后这个间隔只为让出 GIL / 防止空转，不需要再靠它兜握手时间。
        time.sleep(0.05)
