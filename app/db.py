"""本地缓存层：SQLite 落盘。

为什么要有它（公开服务的硬要求）：
- 31 省 + 新浪行情每次抓都要打上游免费源，且上游会限流（567）。
- 对陌生用户公开后，绝不能让每个用户请求都穿透到上游，
  必须服务端缓存一份，用户读缓存，定时任务才去打上游。

设计：
- snapshots 表：每次刷新存一整行全量 JSON（含 provinces/forecast/realtime/window）。
  保留历史用于趋势，latest() 取最新一行。
- settings 表：存 anchor（上次真实调价日）等运维配置，避免硬编码。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "oilwatch.db"
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def _session(write: bool = False) -> Iterator[sqlite3.Connection]:
    """一次完整的 DB 会话：拿连接 → 提交/回滚 → **关连接**。

    ⚠️ 这里必须显式 close。`with sqlite3.connect(...) as conn` 只管理事务
    （退出时 commit/rollback），**不会关连接**，之前每个函数都这么写，
    于是每次调用都泄漏一个连接，全靠 GC 兜底。
    写操作串行化（`_lock`），读操作走 WAL 并发读，不需要抢锁。
    """
    if write:
        _lock.acquire()
    try:
        conn = _conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()
    finally:
        if write:
            _lock.release()


def init() -> None:
    with _session(write=True) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS snapshots (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   generated_at TEXT,
                   payload TEXT
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                   key TEXT PRIMARY KEY,
                   value TEXT
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS subscribers (
                   chat_id TEXT PRIMARY KEY,
                   province_slug TEXT,
                   created_at TEXT DEFAULT (datetime('now'))
               )"""
        )


def save_snapshot(payload: dict) -> None:
    with _session(write=True) as conn:
        conn.execute(
            "INSERT INTO snapshots(generated_at, payload) VALUES (?, ?)",
            (payload.get("generated_at"), json.dumps(payload, ensure_ascii=False)),
        )


def latest() -> dict | None:
    with _session() as conn:
        row = conn.execute(
            "SELECT payload FROM snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return json.loads(row[0]) if row else None


def recent(limit: int = 30) -> list[dict]:
    with _session() as conn:
        rows = conn.execute(
            "SELECT generated_at, payload FROM snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [json.loads(r[1]) for r in rows]


def get_setting(key: str, default: str | None = None) -> str | None:
    with _session() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with _session(write=True) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --- 订阅者（TG Bot 推送用）-----------------------------------------------


def add_subscriber(chat_id: str, province_slug: str | None = None) -> None:
    """订阅（已订阅则更新省份）。

    ⚠️ 省份传 ``None`` 时**保留原值**，不是清空：``/oil_start`` 不带省份只是
    「确认订阅」，不该把用户先前设好的省份抹掉（``COALESCE`` 就是为了这个）。
    真要清空省份请用 :func:`set_subscriber_province`。
    """
    with _session(write=True) as conn:
        conn.execute(
            "INSERT INTO subscribers(chat_id, province_slug) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "province_slug = COALESCE(excluded.province_slug, subscribers.province_slug)",
            (str(chat_id), province_slug),
        )


def set_subscriber_province(chat_id: str, province_slug: str | None) -> None:
    with _session(write=True) as conn:
        conn.execute(
            "UPDATE subscribers SET province_slug = ? WHERE chat_id = ?",
            (province_slug, str(chat_id)),
        )


def remove_subscriber(chat_id: str) -> None:
    with _session(write=True) as conn:
        conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (str(chat_id),))


def list_subscribers() -> list[dict]:
    with _session() as conn:
        rows = conn.execute(
            "SELECT chat_id, province_slug FROM subscribers"
        ).fetchall()
    return [{"chat_id": r[0], "province_slug": r[1]} for r in rows]


def subscriber_count() -> int:
    with _session() as conn:
        return conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
