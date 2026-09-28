"""Bot 离线单测：省份识别、推送文案、共享连接池，不碰网络/数据库。"""
import unittest

import httpx

from app import bot


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


if __name__ == "__main__":
    unittest.main()
