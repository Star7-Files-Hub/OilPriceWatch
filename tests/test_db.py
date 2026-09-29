"""DB 层单测：重点是**连接必须被关闭**。

`with sqlite3.connect(...) as conn` 只管理事务，不关连接。曾经每个函数都这么写，
每次调用泄漏一个连接。这里用 Connection 子类做探针把「关没关」变成可断言的事实。
"""
import sqlite3
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
