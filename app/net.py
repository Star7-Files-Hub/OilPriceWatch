"""HTTP 客户端封装。

统一处理三件事，避免每个数据源各写一遍：
1. 代理——从 https_proxy 环境变量读，**绝不硬编码**（端口每次会话都可能变）
2. 编码——新浪行情返回 GB18030，httpx 自动探测会给出乱码，必须显式指定
3. 重试——免费源不稳定，带指数退避
"""
from __future__ import annotations

import os
import time

import httpx

from . import config

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def proxy_url() -> str | None:
    """从环境变量取代理。本机沙箱代理端口是动态的，硬编码必然连接失败。"""
    return os.environ.get("https_proxy") or os.environ.get("http_proxy")


def tls_verify() -> bool:
    """是否校验 TLS 证书。

    ⚠️ 本机 WorkBuddy 沙箱有一层 MITM 代理，证书链不可信，不关掉会全线
    CERTIFICATE_VERIFY_FAILED。本机跑要设 OILWATCH_TLS_INSECURE=1；
    部署到自己的服务器后**不要**设这个变量，保持默认校验。
    """
    return os.environ.get("OILWATCH_TLS_INSECURE") != "1"


def make_client() -> httpx.Client:
    return httpx.Client(
        proxy=proxy_url(),
        verify=tls_verify(),
        timeout=config.REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_UA, "Accept": "*/*"},
    )


def get_text(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    encoding: str | None = None,
    retries: int = config.REQUEST_RETRIES,
) -> str:
    """抓取文本，带重试。encoding 指定时强制覆盖自动探测结果。

    ⚠️ 上游会限流：实测汽油价格网在并发 6 时对末尾请求返回 `567 Unknown Status`。
    因此 5xx 的退避时间要显著加长，否则 31 省抓取必然有一两个掉队。
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            if encoding:
                resp.encoding = encoding
            return resp.text
        except Exception as exc:  # noqa: BLE001 - 免费源异常类型不可枚举，统一重试
            last_exc = exc
            if attempt < retries - 1:
                delay = 1.5**attempt
                if (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code >= 500
                ):
                    delay *= 2.5
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc
