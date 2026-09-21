"""IP 地理定位：把客户端 IP 映射到省份（省级足够，无需精确坐标）。

为什么这么做（回顾需求）：
- 用户当初想要"获取地理位置推送当前位置的油价"。微信小程序的 wx.getLocation 已被
  排除；H5 上浏览器 Geolocation 需要 HTTPS + 用户授权，且拿到经纬度后还得反向地理编码，
  还得带一份省级边界数据——太重。
- 油价按省定价，定位到省即可。IP 库能直接给到省级，零授权、零额外数据，最适合公开服务。

数据源：ip-api.com 免费版（仅 HTTP、无需 key、返回 regionName）。
⚠️ 免费版限 45 次/分钟，且是按"调用方出口 IP"解析的——必须显式传用户真实 IP，
   不能省略（否则解析到的是我们服务器自己的 IP）。
⚠️ 调用放在请求链路里，必须短超时 + 失败静默降级：定位不到就返回 None，
   前端退回手动选择，绝不能因为定位失败把页面搞挂。
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

from . import config, net

logger = logging.getLogger("oilwatch.geolocate")

API_TEMPLATE = "http://ip-api.com/json/{ip}?fields=status,message,countryCode,regionName"

# ip-api 的英文 regionName → 我们的 slug。注意它区分 Shaanxi(陕西) / Shanxi(山西)。
ENGLISH_TO_SLUG: dict[str, str] = {
    "beijing": "beijing",
    "tianjin": "tianjin",
    "hebei": "hebei",
    "shanxi": "shanxi",            # 山西
    "inner mongolia": "neimenggu",
    "liaoning": "liaoning",
    "jilin": "jilin",
    "heilongjiang": "heilongjiang",
    "shanghai": "shanghai",
    "jiangsu": "jiangsu",
    "zhejiang": "zhejiang",
    "anhui": "anhui",
    "fujian": "fujian",
    "jiangxi": "jiangxi",
    "shandong": "shandong",
    "henan": "henan",
    "hubei": "hubei",
    "hunan": "hunan",
    "guangdong": "guangdong",
    "guangxi": "guangxi",          # 广西（含壮族，但 regionName 多为 Guangxi）
    "hainan": "hainan",
    "chongqing": "chongqing",
    "sichuan": "sichuan",
    "guizhou": "guizhou",
    "yunnan": "yunnan",
    "tibet": "xizang",
    "shaanxi": "shanxi-3",         # 陕西
    "gansu": "gansu",
    "qinghai": "qinghai",
    "ningxia": "ningxia",
    "xinjiang": "xinjiang",
}

# 结果缓存：同一 IP 一小时查一次即可，避免打爆免费接口。
_CACHE: dict[str, tuple[float, str | None]] = {}
_CACHE_TTL = 3600.0


def _normalize_cn(name: str) -> str:
    return re.sub(r"(省|市|自治区|壮族|回族|维吾尔|族|特别行政区)$", "", name).strip()


def _match(region: str) -> str | None:
    if not region:
        return None
    en = region.lower().strip()
    if en in ENGLISH_TO_SLUG:
        return ENGLISH_TO_SLUG[en]
    # 中文：去后缀后精确匹配，再退到子串匹配
    cn = _normalize_cn(region)
    prov = config.PROVINCE_BY_NAME.get(cn)
    if prov:
        return prov.slug
    for p in config.PROVINCES:
        if p.name in region or region in p.name:
            return p.slug
    return None


def ip_to_province(ip: str) -> str | None:
    """返回省份 slug；定位不到（本地 IP / 调用失败 / 海外）返回 None。"""
    if not ip or ip in ("127.0.0.1", "::1", "unknown", ""):
        return None

    now = time.time()
    cached = _CACHE.get(ip)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    slug: str | None = None
    try:
        client = httpx.Client(
            proxy=net.proxy_url(),
            verify=net.tls_verify(),
            timeout=5.0,
            follow_redirects=True,
            headers={"User-Agent": net.DEFAULT_UA},
        )
        with client:
            resp = client.get(API_TEMPLATE.format(ip=ip))
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") == "success" and data.get("countryCode") == "CN":
            slug = _match(data.get("regionName", ""))
    except Exception as exc:  # noqa: BLE001 - 免费源不稳定，失败就降级
        logger.info("IP 定位失败 %s: %s", ip, exc)
        slug = None

    _CACHE[ip] = (now, slug)
    return slug
