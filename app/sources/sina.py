"""新浪财经行情源：国际原油（布伦特 / WTI）、上海原油、人民币汇率。

两个坑（实测踩过，别改）：
1. 必须带 `Referer: https://finance.sina.com.cn`，否则返回空。
2. 返回 GB18030 编码，交给 httpx 自动探测会得到乱码。
"""
from __future__ import annotations

import json
import re

import httpx

from .. import config
from ..net import get_text

_QUOTE_RE = re.compile(r'var hq_str_(\w+)="([^"]*)"')


def fetch_realtime(client: httpx.Client) -> dict[str, dict]:
    """抓实时行情。返回 {brent: {...}, wti: {...}, sc: {...}, usdcny: {...}}。"""
    codes = ",".join(config.SINA_CODES.values())
    text = get_text(
        client,
        config.SINA_QUOTE_URL.format(codes=codes),
        headers={"Referer": config.SINA_REFERER},
        encoding="gb18030",
    )
    payloads: dict[str, list[str]] = {}
    for code, payload in _QUOTE_RE.findall(text):
        if payload:
            payloads[code] = payload.split(",")

    out: dict[str, dict] = {}
    for key, code in config.SINA_CODES.items():
        parts = payloads.get(code)
        if not parts:
            continue
        if key == "usdcny":
            # ⚠️ 新浪外汇字段含义未经权威确认，取第 2 位作为汇率（数值合理），
            #    同时保留 raw 供后续校准。
            out[key] = {"price": _f(parts[1]), "raw": parts}
        elif code.startswith("hf_"):
            # 外盘：0=最新价 4=最高 5=最低 12=日期 13=名称
            out[key] = {
                "price": _f(parts[0]),
                "high": _f(parts[4]),
                "low": _f(parts[5]),
                "date": parts[12],
                "name": parts[13],
                "raw": parts,
            }
        elif code.startswith("nf_"):
            # 国内期货：0=名称 2=开 3=高 4=低 8=最新价
            out[key] = {
                "price": _f(parts[8]),
                "open": _f(parts[2]),
                "high": _f(parts[3]),
                "low": _f(parts[4]),
                "name": parts[0],
                "raw": parts,
            }
    return out


def fetch_daily_kline(
    client: httpx.Client, symbol: str, limit: int = 0
) -> list[dict]:
    """抓外盘日K。symbol 取 OIL（布伦特）或 CL（WTI）。

    limit>0 时只返回最近 limit 条。返回项形如
    {"date": "2026-09-18", "close": 99.75, ...}。
    """
    text = get_text(
        client,
        config.SINA_KLINE_URL.format(symbol=symbol),
        headers={"Referer": config.SINA_REFERER},
    )
    match = re.search(r"\((\[.*\])\)", text, re.S)
    if not match:
        return []
    rows = json.loads(match.group(1))
    out = [
        {
            "date": r["date"],
            "open": _f(r.get("open")),
            "high": _f(r.get("high")),
            "low": _f(r.get("low")),
            "close": _f(r.get("close")),
        }
        for r in rows
        if r.get("date") and r.get("close")
    ]
    return out[-limit:] if limit else out


def fetch_all_klines(client: httpx.Client, limit: int = 0) -> dict[str, list[dict]]:
    """抓全部已配置油种的日K。单个油种失败不影响其它。"""
    out: dict[str, list[dict]] = {}
    for key, symbol in config.SINA_KLINE_SYMBOLS.items():
        try:
            out[key] = fetch_daily_kline(client, symbol, limit=limit)
        except Exception:  # noqa: BLE001 - 免费源，单点失败降级即可
            out[key] = []
    return out


def _f(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None
