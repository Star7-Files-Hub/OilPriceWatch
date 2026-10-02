"""``/webhook/tg`` 的鉴权门禁（独立复核 P1 的回归）。

背景（2026-10-03 复核发现并实测确认）：部署机 8010 直接对公网开放（从**外网**
``GET /openapi.json`` 得 200），而 ``/webhook/tg`` 当时**完全无鉴权** ——
``if secret and ...`` 在 secret 为空时短路。请求体里的 ``chat.type`` 又完全由调用方
伪造，于是任何第三方都能：

- 替**任意 chat_id** 订阅（写 ``subscribers``）；
- 替任意 chat_id 种下待办（写 ``pending_subscriptions``）⇒ 之后那个会话里一句「是」
  就替受害者订阅成功；
- 伪造 ``type=private`` 绕过「纯省份名只认私聊」的约定。

这些用例把「路由只在该在的时候存在」和「没 secret 一律 403（fail closed）」
变成可断言的事实 —— 在此之前 ``app/main.py`` **一个测试都没有**，所以这个洞
在 139 个测试全绿的情况下活了下来。
"""
import importlib
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import bot, db

_UPDATE = {
    "update_id": 1,
    "message": {
        "chat": {"id": 987654321, "type": "private"},
        "text": "浙江",
    },
}


class _WebhookEnv(unittest.TestCase):
    """每个用例都在自己的临时库 + 干净环境变量下重新加载 ``app.main``。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_path = db.DB_PATH
        self._saved_send = bot.send_message
        db.DB_PATH = Path(self._tmp.name) / "t.db"
        db.init()
        bot.send_message = lambda chat_id, text, **kw: None  # 不许真的发网
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("OILWATCH_TG_WEBHOOK_URL", "OILWATCH_TG_WEBHOOK_SECRET")
        }
        for k in self._saved_env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        db.DB_PATH = self._saved_path
        bot.send_message = self._saved_send
        self._tmp.cleanup()

    def _app(self):
        from app import main as main_mod

        importlib.reload(main_mod)  # 环境变量决定路由要不要注册
        return main_mod.app

    def _client(self) -> TestClient:
        # 不用 with（不跑 lifespan）：否则会真的起调度器/轮询。
        return TestClient(self._app())


class TestWebhookRoutePresence(_WebhookEnv):
    def test_route_absent_in_polling_mode(self):
        """轮询模式下这个路由**根本不该存在** ⇒ 404，而不是「存在但拒绝」。"""
        app = self._app()
        self.assertNotIn("/webhook/tg", app.openapi()["paths"])
        self.assertEqual(self._client().post("/webhook/tg", json=_UPDATE).status_code, 404)

    def test_route_present_in_webhook_mode(self):
        os.environ["OILWATCH_TG_WEBHOOK_URL"] = "https://example.com/webhook/tg"
        os.environ["OILWATCH_TG_WEBHOOK_SECRET"] = "s3cret"
        self.assertIn("/webhook/tg", self._app().openapi()["paths"])


class TestWebhookAuth(_WebhookEnv):
    def setUp(self):
        super().setUp()
        os.environ["OILWATCH_TG_WEBHOOK_URL"] = "https://example.com/webhook/tg"

    def _pending(self) -> list:
        with db._session() as conn:
            return conn.execute("SELECT chat_id FROM pending_subscriptions").fetchall()

    def test_empty_secret_fails_closed(self):
        """配了 URL 但没配 secret ⇒ 403，且**不许**产生任何写入。"""
        r = self._client().post("/webhook/tg", json=_UPDATE)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._pending(), [], "未鉴权的请求居然写进了待办表")

    def test_missing_or_wrong_token_rejected(self):
        os.environ["OILWATCH_TG_WEBHOOK_SECRET"] = "s3cret"
        client = self._client()
        for headers in ({}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}):
            with self.subTest(headers=headers):
                r = client.post("/webhook/tg", json=_UPDATE, headers=headers)
                self.assertEqual(r.status_code, 403)
        self.assertEqual(self._pending(), [], "鉴权失败的请求居然写进了待办表")

    def test_correct_token_is_accepted_and_dispatches(self):
        os.environ["OILWATCH_TG_WEBHOOK_SECRET"] = "s3cret"
        r = self._client().post(
            "/webhook/tg",
            json=_UPDATE,
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True})
        # 证明真的走到了 dispatch：私聊发「浙江」只落待办、不落订阅。
        self.assertEqual(self._pending(), [("987654321",)])
        self.assertEqual(db.list_subscribers(), [])

    def test_forged_private_type_cannot_subscribe_without_secret(self):
        """没有 secret 时，伪造 ``type=private`` 也订阅不了任何东西。"""
        forged = {
            "update_id": 2,
            "message": {
                "chat": {"id": 987654321, "type": "private"},
                "text": "浙江",
            },
        }
        self.assertEqual(self._client().post("/webhook/tg", json=forged).status_code, 403)
        self.assertEqual(db.list_subscribers(), [])
        self.assertEqual(self._pending(), [])


class TestAdminWriteEndpointsFailClosed(_WebhookEnv):
    """🔴 复核 F1：写接口必须 **fail-closed**。

    同一个坑的另一半：``/webhook/tg`` 修好了，但 ``_require_admin`` 当时是
    ``if ADMIN_TOKEN and ...`` —— 空值短路即放行，而 ``deploy/.env.example`` 出厂就是
    空值 ⇒「忘配 token」的部署 = 公网匿名可改调价锚点、可反复触发上游抓取
    （锚点被改会让**所有省份**的预测文案整体错位；上游限流严，反复抓会把 IP 打进黑名单）。
    原先只有一条启动 WARNING，且**零测试覆盖** —— 把门禁删掉 163 个测试全绿。
    """

    _REFRESH = "/api/refresh"
    _ANCHOR = "/api/settings/anchor"

    def setUp(self):
        super().setUp()
        from app import main as main_mod

        importlib.reload(main_mod)
        self._main = main_mod
        self._saved_token = main_mod.ADMIN_TOKEN
        self._saved_dev = main_mod.DEV_OPEN
        self._calls = []
        self._saved_refresh = main_mod.collector.refresh

        def _fake_refresh(*a, **k):
            self._calls.append("refresh")
            return {"generated_at": "stub"}

        main_mod.collector.refresh = _fake_refresh
        self._saved_anchor = main_mod.db.set_setting
        main_mod.db.set_setting = lambda k, v: self._calls.append(("anchor", v))

    def tearDown(self):
        self._main.ADMIN_TOKEN = self._saved_token
        self._main.DEV_OPEN = self._saved_dev
        self._main.collector.refresh = self._saved_refresh
        self._main.db.set_setting = self._saved_anchor
        super().tearDown()

    def _client(self) -> TestClient:
        return TestClient(self._main.app)

    def test_no_token_configured_rejects_writes(self):
        """未配 token ⇒ 403，且**不许**产生任何副作用。"""
        self._main.ADMIN_TOKEN = None
        self._main.DEV_OPEN = False
        c = self._client()
        self.assertEqual(c.post(self._REFRESH).status_code, 403)
        self.assertEqual(c.put(self._ANCHOR, params={"anchor": "2026-01-01"}).status_code, 403)
        self.assertEqual(self._calls, [], "未鉴权的写请求居然真的执行了")

    def test_wrong_and_missing_token_rejected(self):
        self._main.ADMIN_TOKEN = "s3cret"
        self._main.DEV_OPEN = False
        c = self._client()
        for headers in ({}, {"X-Admin-Token": "wrong"}):
            with self.subTest(headers=headers):
                self.assertEqual(c.post(self._REFRESH, headers=headers).status_code, 403)
        self.assertEqual(self._calls, [])

    def test_correct_token_allows_writes(self):
        self._main.ADMIN_TOKEN = "s3cret"
        self._main.DEV_OPEN = False
        c = self._client()
        r = c.post(self._REFRESH, headers={"X-Admin-Token": "s3cret"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("refresh", self._calls)

    def test_dev_open_is_the_only_escape_hatch(self):
        """本地调试靠**显式**的 OILWATCH_DEV=1，而不是「没配 token 就放行」。"""
        self._main.ADMIN_TOKEN = None
        self._main.DEV_OPEN = True
        self.assertEqual(self._client().post(self._REFRESH).status_code, 200)
        self.assertIn("refresh", self._calls)

    def test_dev_open_defaults_to_off(self):
        """🔴 钉住**模块默认值**：逃生门必须是显式打开的，不能被改成默认 True。

        （上面两个用例都显式设 DEV_OPEN，所以它们盖不住「默认值被改松」这种回归。）
        """
        saved = os.environ.pop("OILWATCH_DEV", None)
        try:
            main_mod = importlib.reload(self._main)
            self.assertFalse(main_mod.DEV_OPEN, "OILWATCH_DEV 的默认值变松了")
        finally:
            if saved is not None:
                os.environ["OILWATCH_DEV"] = saved

    def test_original_fail_open_guard_would_be_caught(self):
        """把门禁换回**当初那版**（``if ADMIN_TOKEN and ...``）必须被测出来。

        ⚠️ 上一轮的注入只删掉「未配 token」那一支，但 ``not x_admin_token`` 仍然拦得住，
        所以注入存活 ≠ 测试无效。真回归是这一版：
        """
        def _old_guard(x_admin_token):
            if self._main.ADMIN_TOKEN and x_admin_token != self._main.ADMIN_TOKEN:
                raise HTTPException(status_code=403, detail="forbidden")

        self._main.ADMIN_TOKEN = None
        self._main.DEV_OPEN = False
        _old_guard(None)  # 旧版：空 token ⇒ 直接放行，不抛异常
        with self.assertRaises(HTTPException):
            self._main._require_admin(None)  # 新版：必须 403


class TestAdminTokenWarning(unittest.TestCase):
    """``ADMIN_TOKEN`` 为空是 fail-open，保留但必须**显眼**（启动即 WARNING）。"""

    def test_warns_when_admin_token_missing(self):
        from app import main as main_mod

        saved_token, saved_dev = main_mod.ADMIN_TOKEN, main_mod.DEV_OPEN
        main_mod.ADMIN_TOKEN, main_mod.DEV_OPEN = None, False
        try:
            with self.assertLogs("oilwatch.api", level="WARNING") as cm:
                main_mod._warn_insecure_config()
            self.assertIn("OILWATCH_ADMIN_TOKEN", "\n".join(cm.output))
        finally:
            main_mod.ADMIN_TOKEN, main_mod.DEV_OPEN = saved_token, saved_dev

    def test_silent_when_admin_token_set(self):
        from app import main as main_mod

        saved = main_mod.ADMIN_TOKEN
        main_mod.ADMIN_TOKEN = "something"
        try:
            with self.assertNoLogs("oilwatch.api", level="WARNING"):
                main_mod._warn_insecure_config()
        finally:
            main_mod.ADMIN_TOKEN = saved

    def test_errors_when_webhook_url_without_secret(self):
        """复核 F8：配了 URL 却没 secret ⇒ 路由一律 403 且不再轮询 = 彻底聋。"""
        from app import main as main_mod

        saved_env = {
            k: os.environ.get(k)
            for k in ("OILWATCH_TG_WEBHOOK_URL", "OILWATCH_TG_WEBHOOK_SECRET")
        }
        saved_token = main_mod.ADMIN_TOKEN
        main_mod.ADMIN_TOKEN = "x"
        os.environ["OILWATCH_TG_WEBHOOK_URL"] = "https://example.com/webhook/tg"
        os.environ.pop("OILWATCH_TG_WEBHOOK_SECRET", None)
        try:
            with self.assertLogs("oilwatch.api", level="ERROR") as cm:
                main_mod._warn_insecure_config()
            self.assertIn("完全收不到更新", "\n".join(cm.output))
        finally:
            main_mod.ADMIN_TOKEN = saved_token
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
