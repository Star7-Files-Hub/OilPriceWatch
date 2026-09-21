"""探测油价/原油数据源可用性。只读，不改动任何东西。

用法: python probe_sources.py
"""
import os
import ssl
import sys
import urllib.request
import urllib.error

PROXY = os.environ.get("https_proxy") or os.environ.get("http_proxy")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def fetch(url, headers=None, timeout=20):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    handlers = [urllib.request.HTTPSHandler(context=CTX)]
    if PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, resp.headers.get("Content-Type", ""), raw


def decode(raw):
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def probe(name, url, headers=None):
    print("=" * 70)
    print(f"[{name}]")
    print(f"  url: {url}")
    try:
        status, ctype, raw = fetch(url, headers)
        body = decode(raw)
        print(f"  status: {status}   bytes: {len(raw)}   ctype: {ctype}")
        snippet = body.replace("\r", " ").replace("\n", " ").strip()[:260]
        print(f"  body: {snippet}")
    except urllib.error.HTTPError as e:
        print(f"  HTTPError: {e.code} {e.reason}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")


SINA_REF = {"Referer": "https://finance.sina.com.cn"}

if __name__ == "__main__":
    print(f"proxy = {PROXY}")
    print()

    probe(
        "sina-futures-overseas (WTI + Brent)",
        "https://hq.sinajs.cn/list=hf_CL,hf_OIL,hf_DINIW",
        SINA_REF,
    )

    probe(
        "eastmoney-quote (futures)",
        "https://push2.eastmoney.com/api/qt/stock/get"
        "?secid=101.CL&fields=f43,f44,f45,f57,f58,f60,f169,f170",
        {"Referer": "https://quote.eastmoney.com/"},
    )

    probe(
        "eastmoney-klines (Brent daily)",
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        "?secid=101.BZ&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0&end=20500101&lmt=30",
        {"Referer": "https://quote.eastmoney.com/"},
    )

    probe(
        "sina-fx (USDCNY)",
        "https://hq.sinajs.cn/list=fx_susdcny",
        SINA_REF,
    )

    probe("qiyoujiage homepage", "https://www.qiyoujiage.com/")

    probe("qiyoujiage zhejiang", "https://www.qiyoujiage.com/zhejiang.shtml")
