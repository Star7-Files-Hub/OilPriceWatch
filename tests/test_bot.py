"""Bot 离线单测：省份识别、推送文案、共享连接池、**共用 bot 的两条隔离约定**。"""
import json
import os
import threading
import unittest

import httpx

from app import bot


class _FakeDB:
    """替身 DB：只记订阅状态和 settings，绝不开 sqlite 文件。"""

    def __init__(self, settings=None):
        self.subs = {}
        self.settings = dict(settings or {})
        self.removed = []

    def add_subscriber(self, chat_id, province_slug=None):
        # 与真 db 对齐：None 表示「保留原值」，不是清空。
        if province_slug is None:
            self.subs.setdefault(str(chat_id), None)
        else:
            self.subs[str(chat_id)] = province_slug

    def remove_subscriber(self, chat_id):
        self.removed.append(str(chat_id))
        self.subs.pop(str(chat_id), None)

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


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

    def test_province_name_still_recognized(self):
        self.assertEqual(bot.parse_command("浙江"), ("province", "浙江"))
        self.assertEqual(bot.parse_command("广东省"), ("province", "广东省"))

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

    def test_province_plain_text_subscribes(self):
        bot.handle_text(1, "浙江")
        self.assertEqual(self.db.subs["1"], "zhejiang")


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
    def _update(text, chat=5608153118):
        return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}

    def test_status_command_is_ignored(self):
        """线上事故回归：tg-assistant 的 ``/status`` 曾被本项目的欢迎语抢答。"""
        bot.dispatch_update(self._update("/status"))
        self.assertEqual(self.sent, [], "共用 bot 上抢答了别的项目的命令")

    def test_chitchat_is_ignored(self):
        bot.dispatch_update(self._update("你好"))
        self.assertEqual(self.sent, [])

    def test_own_command_is_answered(self):
        bot.dispatch_update(self._update("/oil_start 浙江"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("浙江", self.sent[0][1])


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


if __name__ == "__main__":
    unittest.main()
