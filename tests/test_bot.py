"""Bot 离线单测：省份识别、推送文案、共享连接池、**共用 bot 的两条隔离约定**。"""
import json
import os
import threading
import unittest

import httpx

from app import bot


class _FakeDB:
    """替身 DB：只记订阅状态和 settings，绝不开 sqlite 文件。"""

    def __init__(self, settings=None, snapshot=None):
        self.subs = {}
        self.settings = dict(settings or {})
        self.removed = []
        self.snapshot = snapshot
        self.pending = {}

    def add_subscriber(self, chat_id, province_slug=None):
        # 与真 db 对齐：None 表示「保留原值」，不是清空。
        if province_slug is None:
            self.subs.setdefault(str(chat_id), None)
        else:
            self.subs[str(chat_id)] = province_slug

    def remove_subscriber(self, chat_id):
        self.removed.append(str(chat_id))
        self.subs.pop(str(chat_id), None)

    def set_pending_subscription(self, chat_id, province_slug):
        self.pending[str(chat_id)] = province_slug

    def get_pending_subscription(self, chat_id, max_age_seconds=None):
        # 与真 db 对齐的签名（真实现带 TTL 默认值）；替身不管过期，TTL 由 test_db 覆盖。
        return self.pending.get(str(chat_id))

    def clear_pending_subscription(self, chat_id):
        self.pending.pop(str(chat_id), None)

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def latest(self):
        return self.snapshot

    def subscriber_count(self):
        return len(self.subs)


class TestFindProvince(unittest.TestCase):
    def test_full_name(self):
        self.assertEqual(bot._find_province("浙江").slug, "zhejiang")

    def test_with_suffix(self):
        self.assertEqual(bot._find_province("广东省").slug, "guangdong")
        self.assertEqual(bot._find_province("北京市").slug, "beijing")

    def test_special_slug(self):
        # 陕西是 shanxi-3
        self.assertEqual(bot._find_province("陕西").slug, "shanxi-3")


class TestBuildDigest(unittest.TestCase):
    def test_digest_includes_forecast_and_window(self):
        snap = {
            "forecast": {
                "self": {
                    "direction": "上调",
                    "yuan_per_ton": 607.9,
                    "yuan_per_liter": {"92": 0.45, "95": 0.448, "0": 0.514},
                },
                "site": {"direction": "上调", "yuan_per_ton": 635.0},
                "site_yuan_per_ton": 635.0,
                "site_yuan_per_ton_source": "site",
            },
            "window": {
                "next_date": "2026-09-23",
                "workdays_remaining": 2,
            },
        }
        text = bot.build_digest(snap)
        self.assertIn("上调", text)
        self.assertIn("608", text)  # 607.9 四舍五入到整数
        self.assertIn("635", text)  # 网站预测值
        self.assertIn("2026-09-23", text)
        self.assertIn("2 个工作日", text)
        self.assertNotIn("折算", text)  # 网站原文就是元/吨，不该标成折算

    def test_digest_marks_derived_site_value(self):
        """网站只给元/升时，引擎折出来的元/吨必须标明是折算值，不能冒充原文。"""
        snap = {
            "forecast": {
                "self": {
                    "direction": "上调",
                    "yuan_per_ton": 478.5,
                    "yuan_per_liter": {"92": 0.354},
                },
                "site": {
                    "direction": "上调",
                    "yuan_per_ton": None,
                    "yuan_per_liter_min": 0.30,
                    "yuan_per_liter_max": 0.36,
                },
                "site_yuan_per_ton": 445.8,
                "site_yuan_per_ton_source": "derived",
            },
        }
        text = bot.build_digest(snap)
        self.assertIn("446", text)
        self.assertIn("折算", text)

    def test_digest_tolerates_old_snapshot_without_new_fields(self):
        """兼容旧快照：`site_yuan_per_ton` 是后加字段，缺失时要回退到网站原文。"""
        snap = {
            "forecast": {
                "self": {
                    "direction": "上调",
                    "yuan_per_ton": 607.9,
                    "yuan_per_liter": {"92": 0.45},
                },
                "site": {"direction": "上调", "yuan_per_ton": 635.0},
            },
        }
        text = bot.build_digest(snap)
        self.assertIn("635", text)


class TestSharedClient(unittest.TestCase):
    """回归：_post 必须复用同一个 httpx.Client。

    曾经每次调用都新建 client ⇒ 每次都要重做 TCP+TLS 握手（实测 ~0.5s），
    长轮询每轮换连接导致「握手期间收不到消息」，用户感知就是回复延迟高。
    """

    def setUp(self):
        self._saved = bot._client
        bot._client = None
        self.created = []
        self._real_client = httpx.Client
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"ok": True, "result": []})
        )

        def factory(*args, **kwargs):
            self.created.append(kwargs)
            kwargs.pop("proxy", None)
            kwargs.pop("verify", None)
            kwargs.pop("follow_redirects", None)
            return self._real_client(transport=transport, **kwargs)

        httpx.Client = factory

    def tearDown(self):
        httpx.Client = self._real_client
        if bot._client is not None and not bot._client.is_closed:
            bot._client.close()
        bot._client = self._saved

    def test_reuses_one_client_across_calls(self):
        bot._post("tok", "getMe", {})
        bot._post("tok", "getMe", {})
        bot._post("tok", "getUpdates", {"offset": 0, "timeout": 30}, timeout=40)
        self.assertEqual(len(self.created), 1, "每调用一次就新建了 client")

    def test_keepalive_limits_configured(self):
        bot._post("tok", "getMe", {})
        limits = self.created[0]["limits"]
        self.assertGreaterEqual(limits.max_keepalive_connections, 1)

    def test_transport_retry_only_when_opted_in(self):
        """幂等接口（getUpdates）才重试；sendMessage 超时重试会重复发消息。"""
        calls = {"n": 0}

        def boom(req):
            calls["n"] += 1
            raise httpx.ConnectError("stale keep-alive", request=req)

        httpx.Client = lambda **kw: self._real_client(
            transport=httpx.MockTransport(boom), **{
                k: v for k, v in kw.items() if k in ("timeout", "limits", "headers")
            }
        )
        bot._client = None

        self.assertIsNone(bot._post("tok", "sendMessage", {}))
        self.assertEqual(calls["n"], 1, "sendMessage 不该重试")

        calls["n"] = 0
        self.assertIsNone(bot._post("tok", "getUpdates", {}, retry_transport=True))
        self.assertEqual(calls["n"], 2, "getUpdates 应重试一次")


class TestCommandWhitelist(unittest.TestCase):
    """🔴 共用 bot 约定一：只认自己的 ``/oil`` 命名空间，别的一律沉默。"""

    def test_owns_only_oil_namespace(self):
        self.assertEqual(bot.parse_command("/oil_start"), ("start", ""))
        self.assertEqual(bot.parse_command("/oil_stop"), ("stop", ""))
        self.assertEqual(bot.parse_command("/oil_province 浙江"), ("province", "浙江"))
        self.assertEqual(bot.parse_command("/oil_help"), ("help", ""))
        self.assertEqual(bot.parse_command("/oil"), ("help", ""))

    def test_strips_bot_username_suffix(self):
        """群里客户端会自动补 ``@bot``，必须认。"""
        self.assertEqual(
            bot.parse_command("/oil_start@start6668notify_bot"), ("start", "")
        )

    def test_unknown_oil_command_gets_hint(self):
        # 自己的命名空间里可以给提示，撞不到别人
        self.assertEqual(bot.parse_command("/oil_whatever"), ("unknown", ""))

    def test_other_projects_commands_are_silent(self):
        for text in (
            "/status",
            "/status@start6668notify_bot",
            "/start",
            "/help",
            "/stop",
            "/province 浙江",
        ):
            with self.subTest(text=text):
                self.assertIsNone(
                    bot.parse_command(text), "抢了别的项目的命令"
                )

    def test_arbitrary_text_is_silent(self):
        for text in ("你好", "在吗", "hello", "", "   ", "帮我查下油价", "12345", "/"):
            with self.subTest(text=text):
                self.assertIsNone(bot.parse_command(text))

    def test_province_name_is_recognized_as_plain_form(self):
        """纯省份名要与 ``/oil_province`` 区分开：前者保守处理（私聊+确认）。"""
        self.assertEqual(bot.parse_command("浙江"), ("province_plain", "浙江"))
        self.assertEqual(bot.parse_command("广东省"), ("province_plain", "广东省"))

    def test_unknown_province_word_is_silent(self):
        self.assertIsNone(bot.parse_command("没这个省"))


class TestHandleText(unittest.TestCase):
    def setUp(self):
        self._saved = bot.db
        self.db = _FakeDB()
        bot.db = self.db

    def tearDown(self):
        bot.db = self._saved

    def test_silent_on_foreign_command(self):
        self.assertIsNone(bot.handle_text(1, "/status"))
        self.assertEqual(self.db.subs, {}, "别人的命令不该产生任何副作用")

    def test_start_subscribes(self):
        reply = bot.handle_text(1, "/oil_start")
        self.assertIn("已订阅", reply)
        self.assertIn("1", self.db.subs)

    def test_start_with_province_sets_it(self):
        bot.handle_text(1, "/oil_start 浙江")
        self.assertEqual(self.db.subs["1"], "zhejiang")

    def test_plain_start_keeps_existing_province(self):
        """回归：不带省份的订阅**不能**把已设省份清掉（旧 SQL 会写成 NULL）。"""
        self.db.subs["1"] = "zhejiang"
        bot.handle_text(1, "/oil_start")
        self.assertEqual(self.db.subs["1"], "zhejiang")

    def test_bad_province_does_not_subscribe(self):
        reply = bot.handle_text(1, "/oil_start 火星")
        self.assertIn("没认出", reply)
        self.assertEqual(self.db.subs, {})

    def test_stop_unsubscribes(self):
        bot.handle_text(1, "/oil_stop")
        self.assertEqual(self.db.removed, ["1"])

    def test_help_lists_oil_commands(self):
        reply = bot.handle_text(1, "/oil_help")
        self.assertIn("/oil_start", reply)
        self.assertIn("/oil_stop", reply)

    def test_unknown_oil_command_hints_own_commands(self):
        self.assertIn("/oil_start", bot.handle_text(1, "/oil_xyz"))

    def test_province_plain_text_asks_before_subscribing(self):
        """私聊里发纯省份名 ⇒ **先问一句**，不许直接订阅。"""
        reply = bot.handle_text(1, "浙江", "private")
        self.assertIn("要订阅", reply)
        self.assertIn("浙江", reply)
        self.assertEqual(self.db.subs, {}, "还没确认就订阅了")
        self.assertEqual(self.db.pending["1"], "zhejiang")

    def test_province_plain_text_in_group_is_silent(self):
        """🔴 群里**完全不认**纯省份名：不回复、不订阅、不记待办。

        共用 bot 上群里那句「北京」很可能是别人的对话，接过去就是抢答。
        """
        for chat_type in ("group", "supergroup", "channel"):
            with self.subTest(chat_type=chat_type):
                self.assertIsNone(bot.handle_text(1, "浙江", chat_type))
                self.assertEqual(self.db.subs, {})
                self.assertEqual(self.db.pending, {})

    def test_group_can_still_use_explicit_command(self):
        """群里只是不认「纯省份名」，显式命令照常。"""
        bot.handle_text(1, "/oil_start 浙江", "group")
        self.assertEqual(self.db.subs["1"], "zhejiang")


class TestSubscribeConfirmation(unittest.TestCase):
    """纯省份名 → 问一句 → 回「是」才订阅（共用 bot 上的防误伤设计）。"""

    def setUp(self):
        self._saved = bot.db
        self.db = _FakeDB()
        bot.db = self.db

    def tearDown(self):
        bot.db = self._saved

    def test_yes_confirms_and_subscribes(self):
        bot.handle_text(1, "浙江", "private")
        reply = bot.handle_text(1, "是", "private")
        self.assertIn("已订阅", reply)
        self.assertEqual(self.db.subs["1"], "zhejiang")
        self.assertEqual(self.db.pending, {}, "确认后待办没清掉")

    def test_no_cancels_without_subscribing(self):
        bot.handle_text(1, "浙江", "private")
        reply = bot.handle_text(1, "否", "private")
        self.assertIn("取消", reply)
        self.assertEqual(self.db.subs, {})
        self.assertEqual(self.db.pending, {})

    def test_confirmation_words_without_pending_are_silent(self):
        """🔴 核心防误伤：没有本项目待办时，「是」必须沉默。"""
        for word in ("是", "好的", "要", "否", "取消", "yes", "OK"):
            with self.subTest(word=word):
                self.assertIsNone(bot.handle_text(999, word, "private"))

    def test_confirmation_is_per_chat(self):
        bot.handle_text(1, "浙江", "private")
        # 另一个人说「是」，不该替 1 号确认
        self.assertIsNone(bot.handle_text(2, "是", "private"))
        self.assertEqual(self.db.subs, {})
        self.assertEqual(self.db.pending, {"1": "zhejiang"})

    def test_later_province_overrides_pending(self):
        bot.handle_text(1, "浙江", "private")
        bot.handle_text(1, "广东", "private")
        self.assertEqual(self.db.pending["1"], "guangdong")
        bot.handle_text(1, "是", "private")
        self.assertEqual(self.db.subs["1"], "guangdong")

    def test_explicit_command_clears_pending(self):
        """用显式命令订阅后，不该再留一条待办等着被下一句「是」误触发。"""
        bot.handle_text(1, "浙江", "private")
        bot.handle_text(1, "/oil_start 广东", "private")
        self.assertEqual(self.db.subs["1"], "guangdong")
        self.assertEqual(self.db.pending, {})

    def test_stop_clears_pending(self):
        bot.handle_text(1, "浙江", "private")
        bot.handle_text(1, "/oil_stop", "private")
        self.assertEqual(self.db.pending, {})

    def test_long_sentence_with_yes_is_not_confirmation(self):
        """不能把闲聊里的「是的我在北京」当成确认。"""
        bot.handle_text(1, "浙江", "private")
        self.assertIsNone(bot.handle_text(1, "是的我在北京", "private"))
        self.assertEqual(self.db.subs, {})

    def test_group_is_not_confirmed_even_with_pending(self):
        """🔴 纵深防御（复核 P3）：确认路径也必须挡群聊。

        写入端只给私聊留待办，但「群聊完全沉默」不能**只**依赖写入端 ——
        只要某群有了 pending 行（改库 / 伪造 type=private 的未鉴权端点），
        确认路径就会回复整个群并把它订阅了。这里直接手工种下 pending 来验第二道闸。
        """
        self.db.pending["-1001234567890"] = "zhejiang"
        for ctype in ("group", "supergroup", "channel"):
            with self.subTest(chat_type=ctype):
                self.assertIsNone(bot.handle_text(-1001234567890, "是", ctype))
        self.assertEqual(self.db.subs, {})

    def test_start_without_province_clears_pending(self):
        """复核 P4：无参 /oil_start 也清了待办，否则日后一句「是」会偷偷改掉刚设的省份。"""
        bot.handle_text(1, "浙江", "private")
        bot.handle_text(1, "/oil_start", "private")
        self.assertEqual(self.db.pending, {})
        self.assertIsNone(bot.handle_text(1, "是", "private"))
        self.assertIsNone(self.db.subs["1"], "残留待办被兑现，悄悄改了省份")

    def test_confirmation_variants_are_recognized(self):
        """复核 P7：'好' 认而 '好呀' 不认是不对称的，用户以为回了其实被静默。"""
        for w in (
            "是", "是的", "好", "好的", "好呀", "好的呢", "嗯", "行", "可以", "对",
            "要", "要的", "确认", "订阅", "订阅吧", "OK", "ok!", "YES", "y", "是。", " 是 ",
        ):
            with self.subTest(word=w):
                self.assertTrue(bot.is_confirmation(w), f"{w!r} 应该算确认")
        for w in ("否", "不", "不是", "不用", "不要", "不要了", "不行", "取消", "算了", "no"):
            with self.subTest(word=w):
                self.assertTrue(bot.is_confirmation(w), f"{w!r} 应该算否认")

    def test_chitchat_is_never_a_confirmation(self):
        for w in ("是的我在北京", "你好", "是不是", "好的好的", "没事", "", "   ", "行不行"):
            with self.subTest(word=w):
                self.assertFalse(bot.is_confirmation(w), f"{w!r} 不该算确认")

    def test_variant_actually_confirms_via_handle_text(self):
        bot.handle_text(1, "浙江", "private")
        reply = bot.handle_text(1, "好呀", "private")
        self.assertIn("已订阅", reply)
        self.assertEqual(self.db.subs["1"], "zhejiang")


class TestHandleTextDefaultIsConservative(unittest.TestCase):
    """复核 P6：默认值不能是最宽松的那个 ``private``。

    唯一区分场合的分支就是「纯省份名」，默认宽松意味着将来任何新调用方
    （新入口 / 脚本 / 测试）忘了传 ``chat_type`` 就会静默走宽松分支。
    """

    def test_signature_default_is_group(self):
        import inspect

        default = inspect.signature(bot.handle_text).parameters["chat_type"].default
        self.assertEqual(default, "group", "handle_text 的默认 chat_type 又变宽松了")

    def test_omitting_chat_type_does_not_ask_or_subscribe(self):
        self._saved = bot.db
        bot.db = _FakeDB()
        try:
            self.assertIsNone(bot.handle_text(1, "浙江"))
            self.assertEqual(bot.db.subs, {})
            self.assertEqual(bot.db.pending, {})
        finally:
            bot.db = self._saved


class TestDispatchLoggingIsTruthful(unittest.TestCase):
    """复核 P5：日志必须如实。

    以前「已回复」打在 ``if reply`` **外面**，什么都没发也打印「已回复」——
    拿日志当发送证据会得出错误结论；同时别人的每条「是」都会刷两行 INFO
    （和 63896 行那次同属「别人的流量刷我们的日志」）。
    """

    def setUp(self):
        self._saved_db, self._saved_send = bot.db, bot.send_message
        bot.db = _FakeDB()
        self.sent = []
        bot.send_message = lambda chat_id, text, **kw: self.sent.append((chat_id, text))

    def tearDown(self):
        bot.db, bot.send_message = self._saved_db, self._saved_send

    @staticmethod
    def _update(text, chat=5608153118, chat_type="private"):
        return {"update_id": 1, "message": {"chat": {"id": chat, "type": chat_type}, "text": text}}

    def test_no_reply_log_when_nothing_sent(self):
        with self.assertNoLogs("oilwatch.bot", level="INFO"):
            bot.dispatch_update(self._update("是"))  # 没有待办 ⇒ 沉默
        self.assertEqual(self.sent, [])

    def test_group_province_name_logs_nothing_at_info(self):
        with self.assertNoLogs("oilwatch.bot", level="INFO"):
            bot.dispatch_update(
                self._update("北京", chat=-100999, chat_type="supergroup")
            )
        self.assertEqual(self.sent, [])

    def test_own_command_logs_reply_with_text(self):
        with self.assertLogs("oilwatch.bot", level="INFO") as cm:
            bot.dispatch_update(self._update("/oil_start 浙江"))
        out = "\n".join(cm.output)
        self.assertIn("已回复", out)
        self.assertIn("/oil_start", out, "日志里没有原始文本，无法作为收到的证据")
        self.assertEqual(len(self.sent), 1)

    def test_confirmation_with_pending_logs_reply(self):
        bot.db.pending["5608153118"] = "zhejiang"
        with self.assertLogs("oilwatch.bot", level="INFO") as cm:
            bot.dispatch_update(self._update("是"))
        self.assertIn("已回复", "\n".join(cm.output))

    def test_malformed_updates_do_not_raise(self):
        """复核 P8：畸形 update 不该抛 AttributeError（公网入口尤其如此）。"""
        for bad in (
            None,
            [],
            "x",
            {},
            {"message": None},
            {"message": "not-a-dict"},
            {"message": {"chat": None, "text": "浙江"}},
            {"message": {"chat": "not-a-dict", "text": "浙江"}},
            {"message": {"chat": {"id": None}, "text": "浙江"}},
            {"message": {"chat": {"id": 1, "type": "private"}, "text": None}},
        ):
            with self.subTest(bad=bad):
                bot.dispatch_update(bad)
        self.assertEqual(self.sent, [])


class TestDispatchSilence(unittest.TestCase):
    """dispatch_update 层面必须真的**不调** send_message。"""

    def setUp(self):
        self._saved_db, self._saved_send = bot.db, bot.send_message
        bot.db = _FakeDB()
        self.sent = []
        bot.send_message = lambda chat_id, text, **kw: self.sent.append((chat_id, text))

    def tearDown(self):
        bot.db, bot.send_message = self._saved_db, self._saved_send

    @staticmethod
    def _update(text, chat=5608153118, chat_type="private"):
        return {
            "update_id": 1,
            "message": {"chat": {"id": chat, "type": chat_type}, "text": text},
        }

    def test_status_command_is_ignored(self):
        """线上事故回归：tg-assistant 的 ``/status`` 曾被本项目的欢迎语抢答。"""
        bot.dispatch_update(self._update("/status"))
        self.assertEqual(self.sent, [], "共用 bot 上抢答了别的项目的命令")

    def test_chitchat_is_ignored(self):
        bot.dispatch_update(self._update("你好"))
        self.assertEqual(self.sent, [])

    def test_bare_yes_from_stranger_is_ignored(self):
        """比 /status 更危险的一种抢答：别人的对话里一句「是」。"""
        bot.dispatch_update(self._update("是"))
        self.assertEqual(self.sent, [])

    def test_bare_province_in_group_is_ignored(self):
        bot.dispatch_update(self._update("北京", chat=-1001234567890, chat_type="supergroup"))
        self.assertEqual(self.sent, [], "群里被纯省份名抢答了")
        self.assertEqual(bot.db.subs, {})

    def test_bare_province_in_private_asks_then_confirms(self):
        bot.dispatch_update(self._update("浙江"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("要订阅", self.sent[0][1])
        self.assertEqual(bot.db.subs, {}, "还没确认就订阅了")

        bot.dispatch_update(self._update("是"))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("已订阅", self.sent[1][1])
        self.assertEqual(bot.db.subs["5608153118"], "zhejiang")

    def test_own_command_is_answered(self):
        bot.dispatch_update(self._update("/oil_start 浙江"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("浙江", self.sent[0][1])

    def test_missing_chat_type_is_treated_as_group(self):
        """字段缺失时按最保守处理：纯省份名不认。"""
        bot.dispatch_update(
            {"update_id": 1, "message": {"chat": {"id": 5608153118}, "text": "浙江"}}
        )
        self.assertEqual(self.sent, [])
        self.assertEqual(bot.db.subs, {})


class TestNonInvasivePolling(unittest.TestCase):
    """🔴 共用 bot 约定二：轮询**绝不推进全局 offset**，靠本地去重防重复回复。"""

    def setUp(self):
        self._saved = (bot.db, bot._post, bot.dispatch_update, bot.time.sleep)
        self.handled = []
        self.payloads = []

    def tearDown(self):
        bot.db, bot._post, bot.dispatch_update, bot.time.sleep = self._saved

    def _run(self, batches, db=None):
        bot.db = db or _FakeDB()
        bot.dispatch_update = lambda u: self.handled.append(u)
        it = iter(batches)
        rounds = {"n": 0}

        def fake_post(token, method, payload, *a, **kw):
            self.payloads.append((method, dict(payload)))
            return {"ok": True, "result": next(it, [])}

        stop = threading.Event()

        def fake_sleep(_seconds):
            rounds["n"] += 1
            if rounds["n"] >= len(batches):
                stop.set()

        bot._post = fake_post
        bot.time.sleep = fake_sleep
        bot._poll_loop("tok", stop)

    @staticmethod
    def _msg(text, update_id):
        return {"update_id": update_id, "message": {"chat": {"id": 1}, "text": text}}

    def test_getupdates_never_carries_offset(self):
        self._run([[]] * 2)
        methods = {m for m, _ in self.payloads}
        self.assertEqual(methods, {"getUpdates"})
        for _, payload in self.payloads:
            self.assertNotIn(
                "offset", payload, "推进 offset 会把别的项目的更新一并确认掉"
            )

    def test_repeated_unconfirmed_batch_is_handled_once(self):
        """不推进 offset ⇒ Telegram 反复返回同一批，必须只处理一次。"""
        upd = self._msg("/oil_start", 9)
        self._run([[upd], [upd], [upd]])
        self.assertEqual([u["update_id"] for u in self.handled], [9])

    def test_seen_ids_survive_restart(self):
        """去重表落在 settings 里 ⇒ 进程重启也不会把老消息再回一遍。"""
        upd = self._msg("/oil_start", 42)
        db = _FakeDB()
        self._run([[upd]], db=db)
        self.assertEqual(len(self.handled), 1)

        # 模拟重启：新建一个 _poll_loop 的调用，但沿用同一份 settings
        self.handled.clear()
        self._run([[upd], [upd]], db=db)
        self.assertEqual(self.handled, [], "重启后重复回复了老消息")
        self.assertIn(42, json.loads(db.settings[bot._SEEN_KEY]))

    def test_batch_is_acked_locally_not_by_offset(self):
        """处理过的 id 要落进本地去重表。"""
        db = _FakeDB()
        self._run([[self._msg("/oil_start", 5)]], db=db)
        self.assertEqual(json.loads(db.settings[bot._SEEN_KEY]), [5])


class TestWebhookGuard(unittest.TestCase):
    """共用 bot 上 setWebhook / deleteWebhook 是全局破坏性操作 ⇒ 默认拒绝。"""

    def setUp(self):
        self._saved_post = bot._post
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("OILWATCH_TG_ALLOW_WEBHOOK", "OILWATCH_TG_BOT_TOKEN",
                      "OILWATCH_TG_WEBHOOK_URL")
        }
        self.calls = []
        bot._post = lambda *a, **kw: (self.calls.append(a), {"ok": True})[1]
        os.environ["OILWATCH_TG_BOT_TOKEN"] = "8914978774:fake"
        os.environ["OILWATCH_TG_WEBHOOK_URL"] = "https://example.com/webhook/tg"
        os.environ.pop("OILWATCH_TG_ALLOW_WEBHOOK", None)

    def tearDown(self):
        bot._post = self._saved_post
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_set_webhook_refused_by_default(self):
        self.assertIsNone(bot.set_webhook())
        self.assertEqual(self.calls, [], "默认就发了 setWebhook，会掐断别的项目")

    def test_delete_webhook_refused_by_default(self):
        self.assertIsNone(bot.delete_webhook())
        self.assertEqual(self.calls, [])

    def test_opt_in_allows_set_webhook(self):
        os.environ["OILWATCH_TG_ALLOW_WEBHOOK"] = "1"
        self.assertIsNotNone(bot.set_webhook())
        self.assertEqual(self.calls[0][1], "setWebhook")


class _PushHarness(unittest.TestCase):
    """推送类用例的公共脚手架：打桩 db / broadcast / token，记录发出去的文案。"""

    SNAP = {
        "generated_at": "2026-10-02T21:39:28",
        "window": {"next_date": "2026-10-15", "workdays_remaining": 7},
        "forecast": {
            "self": {
                "direction": "下调",
                "yuan_per_ton": -168.9,
                "yuan_per_liter": {"92": -0.125},
            },
            "site": {"direction": "下调", "yuan_per_ton": 150.0},
        },
    }

    def setUp(self):
        self._saved = (bot.db, bot.broadcast, bot.get_token)
        self.db = _FakeDB()
        bot.db = self.db
        bot.get_token = lambda: "8914978774:fake"
        self.sent = []
        self.recipients = 1  # 让用例能模拟「一个订阅者都没有」

        def fake_broadcast(text, snapshot=None):
            self.sent.append(text)
            return self.recipients

        bot.broadcast = fake_broadcast

    def tearDown(self):
        bot.db, bot.broadcast, bot.get_token = self._saved

    @staticmethod
    def at(hour, day=3):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime(2026, 10, day, hour, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class TestDailyPush(_PushHarness):
    """每日油价走向：每天最多成功一次，且**送出去才落幂等键**。"""

    def test_before_hour_is_silent(self):
        r = bot.maybe_daily_push(self.SNAP, now=self.at(7))
        self.assertEqual(r["reason"], "before_hour")
        self.assertEqual(self.sent, [])

    def test_sends_once_per_day(self):
        r = bot.maybe_daily_push(self.SNAP, now=self.at(8))
        self.assertEqual(r["reason"], "daily")
        self.assertEqual(len(self.sent), 1)
        r2 = bot.maybe_daily_push(self.SNAP, now=self.at(9))
        self.assertEqual(r2["reason"], "already_sent_today")
        self.assertEqual(len(self.sent), 1, "同一天重复推送")

    def test_sends_again_next_day(self):
        bot.maybe_daily_push(self.SNAP, now=self.at(8, day=3))
        r = bot.maybe_daily_push(self.SNAP, now=self.at(8, day=4))
        self.assertEqual(r["reason"], "daily")
        self.assertEqual(len(self.sent), 2)

    def test_catch_up_late_in_day(self):
        """服务 8 点没起来、22 点才起 ⇒ 当天照样补推，不静默度过一天。"""
        r = bot.maybe_daily_push(self.SNAP, now=self.at(22))
        self.assertEqual(r["reason"], "daily")

    def test_no_recipient_keeps_key_unset_and_retries(self):
        """回归线上 9/28：推送那一刻没有订阅者时，不能把这一天的机会用掉。"""
        self.recipients = 0
        r = bot.maybe_daily_push(self.SNAP, now=self.at(8))
        self.assertEqual(r["reason"], "no_recipient")
        self.assertNotIn(bot._DAILY_KEY, self.db.settings, "没人收到却落了幂等键")

        # 用户后来订阅了 ⇒ 同一天仍能补推
        self.recipients = 1
        r2 = bot.maybe_daily_push(self.SNAP, now=self.at(9))
        self.assertEqual(r2["reason"], "daily")

    def test_title_is_daily_wording(self):
        bot.maybe_daily_push(self.SNAP, now=self.at(8))
        self.assertIn("今日油价走向", self.sent[0])

    def test_no_window_is_silent(self):
        r = bot.maybe_daily_push({"forecast": {}}, now=self.at(8))
        self.assertEqual(r["reason"], "no_window")
        self.assertEqual(self.sent, [])

    def test_partial_delivery_is_logged_not_silent(self):
        """``sent>0`` 只是「至少送出一个」。2 个订阅者只成功 1 个时必须告警。

        否则「部分失败」和「全部成功」在日志上完全一样 —— 与 9/28 那次
        「0 送达却若无其事地落键」是同一类盲区（验证者发现的 P4）。
        """
        self.db.subs = {"1": None, "2": None}  # subscriber_count() == 2
        self.recipients = 1
        with self.assertLogs("oilwatch.bot", level="WARNING") as cm:
            r = bot.maybe_daily_push(self.SNAP, now=self.at(8))
        self.assertEqual(r["pushed"], 1)
        self.assertTrue(
            any("部分失败" in m for m in cm.output), f"部分失败没告警: {cm.output}"
        )

    def test_all_delivered_does_not_warn(self):
        self.db.subs = {"1": None, "2": None}
        self.recipients = 2
        # assertNoLogs 而不是 assertLogs：后者要求「至少有一条」，全成功时反而会报错。
        with self.assertNoLogs("oilwatch.bot", level="WARNING"):
            r = bot.maybe_daily_push(self.SNAP, now=self.at(8))
        self.assertEqual(r["pushed"], 2)


class TestDailyTimeZone(unittest.TestCase):
    """回归（验证者发现的盲区）：所有测试都显式传 ``now=`` 或自建 CST 时间，

    于是把 ``Asia/Shanghai`` 写成 ``UTC`` 也全绿 —— 而线上会变成 16:00 CST 才推。
    """

    def test_now_cn_is_shanghai(self):
        now = bot._now_cn()
        self.assertEqual(str(now.tzinfo), "Asia/Shanghai")
        self.assertEqual(now.utcoffset().total_seconds(), 8 * 3600)

    def test_scheduler_timezone_is_shanghai(self):
        from app import scheduler

        self.assertEqual(str(scheduler._scheduler.timezone), "Asia/Shanghai")


class TestOffsetOptIn(unittest.TestCase):
    """默认**不推进 offset**；只有显式 ``OILWATCH_TG_ALLOW_OFFSET=1`` 才确认水位。

    这是给「确认过该 token 上没有别的项目用 Bot API getUpdates」之后留的闸门：
    开闸后积压会被清空、长轮询才真正生效（命令秒级可达），代价是会影响别的消费者。
    """

    def setUp(self):
        self._saved = (
            bot.db,
            bot._post,
            bot.dispatch_update,
            bot.time.sleep,
            os.environ.get("OILWATCH_TG_ALLOW_OFFSET"),
        )
        os.environ.pop("OILWATCH_TG_ALLOW_OFFSET", None)
        self.payloads = []

    def tearDown(self):
        bot.db, bot._post, bot.dispatch_update, bot.time.sleep = self._saved[:4]
        if self._saved[4] is None:
            os.environ.pop("OILWATCH_TG_ALLOW_OFFSET", None)
        else:
            os.environ["OILWATCH_TG_ALLOW_OFFSET"] = self._saved[4]

    def _run(self, batches, seen=None):
        db = _FakeDB()
        if seen:
            db.settings[bot._SEEN_KEY] = json.dumps(seen)
        bot.db = db
        bot.dispatch_update = lambda u: None
        it = iter(batches)
        rounds = {"n": 0}

        def fake_post(token, method, payload, *a, **kw):
            self.payloads.append(dict(payload))
            return {"ok": True, "result": next(it, [])}

        stop = threading.Event()

        def fake_sleep(_s):
            rounds["n"] += 1
            if rounds["n"] >= len(batches):
                stop.set()

        bot._post = fake_post
        bot.time.sleep = fake_sleep
        bot._poll_loop("tok", stop)

    def test_default_is_off(self):
        self._run([[{"update_id": 7, "message": {}}], []], seen=[5])
        self.assertTrue(self.payloads)
        for p in self.payloads:
            self.assertNotIn("offset", p, "默认档居然传了 offset")

    def test_enabled_advances_watermark(self):
        os.environ["OILWATCH_TG_ALLOW_OFFSET"] = "1"
        self._run(
            [[{"update_id": 7, "message": {}}], [{"update_id": 9, "message": {}}]],
            seen=[5],
        )
        self.assertEqual(self.payloads[0]["offset"], 6, "没从本地去重表推出起始水位")
        self.assertEqual(self.payloads[1]["offset"], 8, "处理完 7 之后水位没推进")

    def test_never_carries_allowed_updates_even_when_enabled(self):
        os.environ["OILWATCH_TG_ALLOW_OFFSET"] = "1"
        self._run([[{"update_id": 7, "message": {}}], []], seen=[5])
        for p in self.payloads:
            self.assertNotIn("allowed_updates", p, "开闸也不许碰 allowed_updates")


class TestCyclePushKey(_PushHarness):
    """周期推送的幂等键同样必须「送达后才落」。"""

    def test_no_change_after_delivery(self):
        self.assertEqual(bot.maybe_push(self.SNAP)["reason"], "new_cycle")
        self.assertEqual(self.db.settings["tg_last_cycle_next"], "2026-10-15")
        self.assertEqual(bot.maybe_push(self.SNAP)["reason"], "no_change")

    def test_regression_2026_09_28_nobody_subscribed_yet(self):
        """线上事故回归：21:43 推送时**还没有订阅者**，不能就此把整轮静默掉。

        旧写法先落键再发送 ⇒ 用户 22:16 订阅后要干等到下一轮（17 天）。
        """
        self.recipients = 0
        r = bot.maybe_push(self.SNAP)
        self.assertEqual(r["reason"], "no_recipient")
        self.assertIsNone(
            self.db.settings.get("tg_last_cycle_next"), "没人收到却落了幂等键"
        )

        # 用户订阅了 ⇒ 下一轮巡检必须把这一轮的播报补上
        self.recipients = 1
        r2 = bot.maybe_push(self.SNAP)
        self.assertEqual(r2["reason"], "new_cycle")
        self.assertEqual(len(self.sent), 2)  # 第一次也调了 broadcast（只是没人收）


class TestDailyHourConfig(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("OILWATCH_TG_DAILY_HOUR")
        os.environ.pop("OILWATCH_TG_DAILY_HOUR", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("OILWATCH_TG_DAILY_HOUR", None)
        else:
            os.environ["OILWATCH_TG_DAILY_HOUR"] = self._saved

    def test_default(self):
        self.assertEqual(bot.daily_hour(), 8)

    def test_override(self):
        os.environ["OILWATCH_TG_DAILY_HOUR"] = "7"
        self.assertEqual(bot.daily_hour(), 7)

    def test_invalid_falls_back(self):
        for bad in ("abc", "99", "-1", ""):
            os.environ["OILWATCH_TG_DAILY_HOUR"] = bad
            with self.subTest(bad=bad):
                self.assertEqual(bot.daily_hour(), 8)


class TestTodayCommand(unittest.TestCase):
    """``/oil_today`` 手动查今日走向（也是推送出问题时的自检入口）。"""

    def setUp(self):
        self._saved = bot.db
        self.db = _FakeDB(snapshot=_PushHarness.SNAP)
        bot.db = self.db

    def tearDown(self):
        bot.db = self._saved

    def test_returns_digest(self):
        reply = bot.handle_text(1, "/oil_today")
        self.assertIn("今日油价走向", reply)
        self.assertIn("下调", reply)
        self.assertIn("2026-10-15", reply)

    def test_no_data_is_graceful(self):
        self.db.snapshot = None
        self.assertIn("还没准备好", bot.handle_text(1, "/oil_today"))

    def test_still_silent_for_foreign_commands(self):
        self.assertIsNone(bot.handle_text(1, "/status"))


class TestPollingGlobalState(unittest.TestCase):
    """共用 bot 的全局状态黑名单：**offset 和 allowed_updates 都不能碰**。

    回归 2026-10-02：曾用 ``allowed_updates`` 过滤频道贴，线上实测发现它**不是**
    按次生效，而是被 Telegram 写成**按 token 全局且持久**的订阅设置 ——
    ``getWebhookInfo`` 的 ``allowed_updates`` 字段会跟着变，而且之后**不传该参数并不会恢复**
    （传 ["callback_query"] ⇒ 全局变 ["callback_query"]；再不带参数调用，全局仍是它）。
    那会静默掐掉其它项目/将来新增场景的更新类型，与「绝不碰全局状态」的红线冲突。
    """

    def setUp(self):
        self._saved = (bot.db, bot._post, bot.dispatch_update, bot.time.sleep)
        self.payloads = []

    def tearDown(self):
        bot.db, bot._post, bot.dispatch_update, bot.time.sleep = self._saved

    def _run(self, batches):
        bot.db = _FakeDB(snapshot=_PushHarness.SNAP)
        self.handled = []
        bot.dispatch_update = lambda u: self.handled.append(u)
        it = iter(batches)
        rounds = {"n": 0}

        def fake_post(token, method, payload, *a, **kw):
            self.payloads.append(dict(payload))
            return {"ok": True, "result": next(it, [])}

        stop = threading.Event()

        def fake_sleep(_s):
            rounds["n"] += 1
            if rounds["n"] >= len(batches):
                stop.set()

        bot._post = fake_post
        bot.time.sleep = fake_sleep
        bot._poll_loop("tok", stop)

    def test_never_sends_offset(self):
        self._run([[]] * 2)
        self.assertTrue(self.payloads)
        for p in self.payloads:
            self.assertNotIn("offset", p, "offset 是全局确认水位，会把别人的更新确认掉")

    def test_never_sends_allowed_updates(self):
        self._run([[]] * 2)
        self.assertTrue(self.payloads)
        for p in self.payloads:
            self.assertNotIn(
                "allowed_updates",
                p,
                "allowed_updates 是全局持久设置，会静默掐掉别的项目的更新类型",
            )

    def test_foreign_page_full_of_channel_posts_still_finds_our_message(self):
        """一整页 100 条别人的频道贴 + 末尾我们的一条命令 ⇒ 必须照样交到分发层。

        （过滤别人的更新是 :func:`dispatch_update` 的职责，见 TestDispatchSilence；
        轮询这层的职责是**别把页面里的东西漏掉**。）
        """
        page = [{"update_id": i, "channel_post": {"message_id": i}} for i in range(1, 100)]
        page.append(
            {
                "update_id": 100,
                "message": {
                    "chat": {"id": 5608153118},
                    "text": "/oil_start 浙江",
                },
            }
        )
        self._run([page, []])
        got = [u["update_id"] for u in self.handled]
        self.assertIn(100, got, "自己的命令被满页频道贴挡掉了")
        self.assertEqual(got, list(range(1, 101)), "有更新没被交给分发层（去重误杀）")

    def test_backlog_warning_is_throttled_not_spammed(self):
        """回归：这行告警曾经每轮都打，4 天刷了 63896 行日志。"""
        page = [{"update_id": i, "channel_post": {"message_id": i}} for i in range(100)]
        with self.assertLogs("oilwatch.bot", level="WARNING") as cm:
            self._run([page] * 5)
        warned = [r for r in cm.records if "压到一页上限" in r.getMessage()]
        self.assertEqual(len(warned), 1, f"积压告警没有限流，打了 {len(warned)} 次")


if __name__ == "__main__":
    unittest.main()
