"""汽油价格网数据源：31 省零售价 + 该站现成的调价预测值。

⚠️ 陕西的 slug 是 "shanxi-3"，"shanxi" 是山西。详见 config.PROVINCES。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

import httpx

from .. import config
from ..net import get_text

_SCRIPT_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_PRICE_RE = re.compile(r"^(\d+\.\d{1,3})$")

# ⚠️ 该站预测文案改过：早期是「预计上调油价XXX元/吨(0.48-0.57元/升)」，
#    现在常见「目前预计上涨0.30元/升-0.36元/升」（用词「上涨/下跌」、且只给元/升）。
#    两种都要吃，方向统一归一化成 上调/下调。
_DIR_MAP = {"上调": "上调", "上涨": "上调", "下调": "下调", "下跌": "下调"}
_FORECAST_RE = re.compile(
    r"预计\s*(上调|下调|上涨|下跌)\s*(?:油价)?\s*"
    r"(?:([\d.]+)\s*元/吨)?"
    r"(?:\s*[（(]?\s*([\d.]+)\s*元/升\s*-\s*([\d.]+)\s*元/升\s*[)）]?)?"
)


def _to_lines(html: str) -> list[str]:
    text = _SCRIPT_RE.sub(" ", html)
    text = _TAG_RE.sub("\n", text)
    text = text.replace("&nbsp;", " ")
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def _find_price(lines: list[str], label: str) -> float | None:
    """标签后 1-3 行内出现的第一个纯数字即价格。"""
    for i, ln in enumerate(lines):
        if label in ln:
            for j in range(i + 1, min(i + 4, len(lines))):
                match = _PRICE_RE.match(lines[j])
                if match:
                    return float(match.group(1))
    return None


def _find_forecast(lines: list[str]) -> dict | None:
    for ln in lines:
        match = _FORECAST_RE.search(ln)
        if match and (match.group(2) or match.group(3)):
            return {
                "direction": _DIR_MAP[match.group(1)],
                "yuan_per_ton": float(match.group(2)) if match.group(2) else None,
                "yuan_per_liter_min": float(match.group(3)) if match.group(3) else None,
                "yuan_per_liter_max": float(match.group(4)) if match.group(4) else None,
                "text": ln[:140],
            }
    return None


def parse_province(html: str, province: config.Province) -> dict:
    """把省级页面 HTML 解析成结构化数据。可离线测试。"""
    lines = _to_lines(html)
    prices = {key: _find_price(lines, label) for label, key in config.FUEL_LABELS.items()}
    return {
        "slug": province.slug,
        "name": province.name,
        "region": province.region,
        "prices": prices,
        "forecast": _find_forecast(lines),
        # 只看必需油品；98# 缺失不算不完整（部分省份本就不供 98#）
        "complete": all(prices.get(f) is not None for f in config.REQUIRED_FUELS),
    }


def fetch_province(client: httpx.Client, province: config.Province) -> dict:
    url = config.QIYOUJIAGE_PROVINCE_URL.format(slug=province.slug)
    html = get_text(client, url, encoding="utf-8")
    return parse_province(html, province)


def fetch_all_provinces(
    client: httpx.Client, max_workers: int = 3
) -> dict[str, dict]:
    """并发抓 31 省。单省失败降级为空价格，不影响整体。

    ⚠️ max_workers 不要调高：实测并发 6 时上游会对末尾请求返回 567（限流），
    导致宁夏、新疆稳定掉队。3 是实测可用的值。
    ⚠️ 对陌生用户公开服务时还要在此基础上加本地缓存，不要每次请求都打上游。
    """

    def work(province: config.Province) -> dict:
        try:
            return fetch_province(client, province)
        except Exception as exc:  # noqa: BLE001 - 免费源，失败降级
            return {
                "slug": province.slug,
                "name": province.name,
                "region": province.region,
                "prices": {},
                "forecast": None,
                "complete": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(work, config.PROVINCES))
    return {r["slug"]: r for r in results}
