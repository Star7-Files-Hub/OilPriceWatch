"""DB 层单测：重点是**连接必须被关闭**。

`with sqlite3.connect(...) as conn` 只管理事务，不关连接。曾经每个函数都这么写，
每次调用泄漏一个连接。这里用 Connection 子类做探针把「关没关」变成可断言的事实。
"""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import db

_opened: list = []
_closed: list = []


class _TrackedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _opened.append(self)

    def close(self):
        _closed.append(self)
        super().close()


class TestSessionClosesConnection(unittest.TestCase):
    def setUp(self):
        _opened.clear()
        _closed.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_path = db.DB_PATH
        self._saved_conn = db._conn
        db.DB_PATH = Path(self._tmp.name) / "t.db"

        def tracked_conn():
            db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db.DB_PATH, timeout=30, factory=_TrackedConnection)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            return conn

        db._conn = tracked_conn
        db.init()

    def tearDown(self):
        db._conn = self._saved_conn
        db.DB_PATH = self._saved_path
        self._tmp.cleanup()

    def test_every_call_closes_its_connection(self):
        _opened.clear()
        _closed.clear()
        db.add_subscriber("1", "zhejiang")
        db.list_subscribers()
        db.subscriber_count()
        db.set_setting("anchor", "2026-09-24")
        db.get_setting("anchor")
        db.save_snapshot({"generated_at": "2026-09-19T00:00:00", "provinces": {}})
        db.latest()
        db.recent()
        self.assertGreater(len(_opened), 0)
        self.assertEqual(
            len(_opened), len(_closed), "有连接没被关闭（泄漏）"
        )

    def test_write_lock_released_after_error(self):
        """写操作抛异常时不能把全局锁吃掉，否则后续所有写入永久阻塞。"""
        with self.assertRaises(TypeError):
            # 不可 JSON 序列化 ⇒ json.dumps 在持锁期间抛异常
            db.save_snapshot({"generated_at": "x", "bad": object()})
        # 锁没被吃掉 ⇒ 这次写能正常完成
        db.set_setting("k", "v")
        self.assertEqual(db.get_setting("k"), "v")


class TestAddSubscriberKeepsProvince(unittest.TestCase):
    """回归：``/oil_start`` 不带省份时**不能**把已设省份清成 NULL。

    旧 SQL 是 ``DO UPDATE SET province_slug = excluded.province_slug``：
    excluded 为 NULL 就直接覆盖 ⇒ 用户随手发一次 ``/oil_start`` 省份就没了，
    调价播报悄悄退化成全国版（不报错、只是少了那一行）。
    ⚠️ 这条必须跑**真 sqlite** 才抓得到 —— 替身 DB 不实现 SQL 语义。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_path = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "t.db"
        db.init()

    def tearDown(self):
        db.DB_PATH = self._saved_path
        self._tmp.cleanup()

    @staticmethod
    def _subs() -> dict:
        return {s["chat_id"]: s["province_slug"] for s in db.list_subscribers()}

    def test_none_keeps_existing_province(self):
        db.add_subscriber("1", "zhejiang")
        db.add_subscriber("1")  # 不带省份的 /oil_start
        self.assertEqual(self._subs()["1"], "zhejiang")

    def test_explicit_province_still_updates(self):
        db.add_subscriber("1", "zhejiang")
        db.add_subscriber("1", "guangdong")
        self.assertEqual(self._subs()["1"], "guangdong")

    def test_new_subscriber_without_province_is_none(self):
        db.add_subscriber("2")
        self.assertIsNone(self._subs()["2"])

    def test_set_subscriber_province_can_still_clear(self):
        """真要清空省份，得走 set_subscriber_province，语义没被堵死。"""
        db.add_subscriber("1", "zhejiang")
        db.set_subscriber_province("1", None)
        self.assertIsNone(self._subs()["1"])

    def test_is_subscriber(self):
        """⚠️ 必须用**真 sqlite** 测：bot 的测试用的是 _FakeDB，它有自己的 is_subscriber，

        所以把真实现改成恒 True 时 bot 那套测试全绿（注入实测存活）。这个函数是
        「共用 bot 上只对我们的订阅者回退订提示」那道闸，不能没有真实覆盖。
        """
        self.assertFalse(db.is_subscriber("1"))
        db.add_subscriber("1", "zhejiang")
        self.assertTrue(db.is_subscriber("1"))
        self.assertTrue(db.is_subscriber(1), "int 形式的 chat_id 也要认")
        self.assertFalse(db.is_subscriber("2"))
        db.remove_subscriber("1")
        self.assertFalse(db.is_subscriber("1"))


class TestPendingSubscription(unittest.TestCase):
    """待确认订阅（私聊纯省份名 → 问一句 → 回「是」才订阅）落库语义。

    ⚠️ 同样必须跑**真 sqlite**：这里断言的是 ``ON CONFLICT DO UPDATE`` 的覆盖行为。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_path = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "t.db"
        db.init()

    def tearDown(self):
        db.DB_PATH = self._saved_path
        self._tmp.cleanup()

    def test_none_when_absent(self):
        self.assertIsNone(db.get_pending_subscription("1"))

    def test_set_then_get(self):
        db.set_pending_subscription("1", "zhejiang")
        self.assertEqual(db.get_pending_subscription("1"), "zhejiang")

    def test_later_set_overrides(self):
        """每个 chat 只留一条：后发的省份覆盖前一条，不会堆积成状态机。"""
        db.set_pending_subscription("1", "zhejiang")
        db.set_pending_subscription("1", "guangdong")
        self.assertEqual(db.get_pending_subscription("1"), "guangdong")

    def test_clear(self):
        db.set_pending_subscription("1", "zhejiang")
        db.clear_pending_subscription("1")
        self.assertIsNone(db.get_pending_subscription("1"))

    def test_pending_is_per_chat(self):
        db.set_pending_subscription("1", "zhejiang")
        self.assertIsNone(db.get_pending_subscription("2"))

    def test_pending_does_not_subscribe(self):
        """写待办**不能**顺手把人订阅了 —— 那正是这次要修掉的误伤。"""
        db.set_pending_subscription("1", "zhejiang")
        self.assertEqual(db.list_subscribers(), [])
        self.assertEqual(db.subscriber_count(), 0)

    def _age_pending(self, chat_id: str, stamp: str) -> None:
        with db._session(write=True) as conn:
            conn.execute(
                "UPDATE pending_subscriptions SET created_at = ? WHERE chat_id = ?",
                (stamp, chat_id),
            )

    def test_stale_pending_expires_and_is_deleted(self):
        """🔴 复核 P2：``created_at`` 曾经只写不读 ⇒ 待办永不过期。

        后果：用户几个月后随口一句「好」都会被兑现成订阅。现在超过 TTL 即
        视为不存在，并顺手删掉过期行。
        """
        db.set_pending_subscription("1", "zhejiang")
        self._age_pending("1", "2020-01-01 00:00:00")
        self.assertIsNone(db.get_pending_subscription("1"), "过期待办仍被当成有效")
        with db._session() as conn:
            left = conn.execute(
                "SELECT COUNT(*) FROM pending_subscriptions WHERE chat_id = ?", ("1",)
            ).fetchone()[0]
        self.assertEqual(left, 0, "过期行没有被清掉，表会越积越多")

    def test_fresh_pending_survives(self):
        db.set_pending_subscription("1", "zhejiang")
        self.assertEqual(db.get_pending_subscription("1"), "zhejiang")

    def test_custom_max_age_is_honored(self):
        db.set_pending_subscription("1", "zhejiang")
        self.assertIsNone(db.get_pending_subscription("1", max_age_seconds=0))
        self.assertTrue(db.PENDING_TTL_SECONDS >= 60, "TTL 短得不合理")

    def test_unparsable_timestamp_is_treated_as_expired(self):
        """时间戳坏了宁可让用户重新确认一次，也不要在几个月后突然兑现。"""
        db.set_pending_subscription("1", "zhejiang")
        self._age_pending("1", "not-a-timestamp")
        self.assertIsNone(db.get_pending_subscription("1"))

    def test_future_timestamp_is_treated_as_expired(self):
        """🔴 复核 F2：``(now - made) > ttl`` 对**未来**时间戳恒为假 ⇒ 永不判过期。

        时钟回拨（NTP 校正 / 虚机快照回滚）或有人写库都能造出这种「长生待办」。
        方向必须和「缺失/畸形」一致：都按不可信处理。
        """
        db.set_pending_subscription("1", "zhejiang")
        self._age_pending("1", "2099-01-01 00:00:00")
        self.assertIsNone(db.get_pending_subscription("1"), "未来时间戳的待办长生不老")
        with db._session() as conn:
            left = conn.execute(
                "SELECT COUNT(*) FROM pending_subscriptions WHERE chat_id = ?", ("1",)
            ).fetchone()[0]
        self.assertEqual(left, 0)

    def test_slightly_future_timestamp_is_tolerated(self):
        """只容忍小幅超前（几秒的时钟漂移），不能把正常写入误判成过期。"""
        db.set_pending_subscription("1", "zhejiang")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._age_pending("1", stamp)
        self.assertEqual(db.get_pending_subscription("1"), "zhejiang")


class TestPendingTtlWiring(unittest.TestCase):
    """🔴 复核 F7：db 层的 TTL 有覆盖，但**bot 层有没有真的用上**当时零覆盖。

    注入 ``_handle_confirmation`` 传 ``max_age_seconds=10**9``（等于关掉 TTL）163 个测试
    全绿 ⇒ 说明没人验过这条接线。这里用**真 sqlite**跑一遍完整确认链路。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_path, self._saved_db = db.DB_PATH, None
        db.DB_PATH = Path(self._tmp.name) / "t.db"
        db.init()
        from app import bot

        self._bot = bot
        self._saved_bot_db = bot.db
        bot.db = db
        self._saved_send = bot.send_message
        bot.send_message = lambda *a, **k: None

    def tearDown(self):
        self._bot.db = self._saved_bot_db
        self._bot.send_message = self._saved_send
        db.DB_PATH = self._saved_path
        self._tmp.cleanup()

    def _age(self, chat_id: str, stamp: str) -> None:
        with db._session(write=True) as conn:
            conn.execute(
                "UPDATE pending_subscriptions SET created_at = ? WHERE chat_id = ?",
                (stamp, chat_id),
            )

    def _say(self, text: str, chat_id: int = 555, ctype: str = "private") -> list:
        sent = []
        self._bot.send_message = lambda cid, t, **k: sent.append((cid, t))
        self._bot.dispatch_update(
            {"update_id": 1, "message": {"chat": {"id": chat_id, "type": ctype}, "text": text}}
        )
        return sent

    def test_expired_pending_cannot_subscribe_via_bot(self):
        self._say("浙江")  # 走完整链路落一条待办
        self.assertEqual(db.get_pending_subscription("555"), "zhejiang")
        self._age("555", "2020-01-01 00:00:00")
        self.assertEqual(self._say("是"), [], "过期待办被兑现成了订阅")
        self.assertEqual(db.list_subscribers(), [])

    def test_fresh_pending_still_subscribes_via_bot(self):
        self._say("浙江")
        sent = self._say("是")
        self.assertEqual(len(sent), 1)
        self.assertIn("已订阅", sent[0][1])
        self.assertEqual(db.list_subscribers(), [{"chat_id": "555", "province_slug": "zhejiang"}])


if __name__ == "__main__":
    unittest.main()
