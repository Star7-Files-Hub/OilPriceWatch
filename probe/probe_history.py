"""第六轮：找历史日线数据（自算变化率的前提）+ 调价时间表页面。"""
import os
import re
import ssl
import json
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
        return resp.status, resp.read()


def decode(raw):
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


SINA_REF = {"Referer": "https://finance.sina.com.cn"}

print("### A. 新浪外盘期货 日K 接口")
for sym in ["OIL", "CL"]:
    url = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
        f"var%20_t=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={sym}"
    )
    try:
        _, raw = fetch(url, SINA_REF)
        body = decode(raw)
        print(f"  OK {sym:5s} {len(raw)}B  ->  {body[:230]}")
    except urllib.error.HTTPError as e:
        print(f"  {e.code} {sym}")
    except Exception as e:
        print(f"  ERR {sym}: {type(e).__name__}")

print()
print("### B. 东财 secid 尝试（外盘期货）")
for secid in ["101.CL", "102.CL", "101.OIL", "102.OIL", "100.OIL", "103.OIL"]:
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0&end=20500101&lmt=5"
    )
    try:
        _, raw = fetch(url, {"Referer": "https://quote.eastmoney.com/"})
        body = decode(raw)
        has = '"klines"' in body
        print(f"  {'OK ' if has else '-- '} {secid:10s} {body[:110]}")
    except Exception as e:
        print(f"  ERR {secid}: {type(e).__name__}")

print()
print("### C. 调价时间表页面")
for path in ["tz/2025", "tz/2026", "tz"]:
    url = f"https://www.qiyoujiage.com/{path}"
    try:
        _, raw = fetch(url)
        html = decode(raw)
        txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        txt = re.sub(r"(?s)<[^>]+>", "\n", txt)
        lines = [ln.strip() for ln in txt.split("\n") if ln.strip()]
        dates = [ln for ln in lines if re.search(r"\d{1,2}月\d{1,2}日", ln)]
        print(f"  OK {path:9s} {len(raw)}B  含日期行 {len(dates)}")
        for d in dates[:8]:
            print(f"      | {d[:100]}")
    except urllib.error.HTTPError as e:
        print(f"  {e.code} {path}")
    except Exception as e:
        print(f"  ERR {path}: {type(e).__name__}")
