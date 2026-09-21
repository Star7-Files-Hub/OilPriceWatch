"""第四轮：修陕西 slug + 寻找第二独立数据源。"""
import os
import re
import ssl
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


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
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


def to_lines(html):
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", "\n", html)
    html = html.replace("&nbsp;", " ")
    return [ln.strip() for ln in html.split("\n") if ln.strip()]


print("### A. 陕西 slug 变体测试")
for slug in ["shaanxi", "sanxi", "shxi", "shan-xi", "sn", "sx", "shaanxi1", "shanxi1"]:
    url = f"https://www.qiyoujiage.com/{slug}.shtml"
    try:
        status, raw = fetch(url, timeout=12)
        lines = to_lines(decode(raw))
        title = lines[0][:50] if lines else ""
        p92 = next((lines[i + 1] for i, ln in enumerate(lines)
                    if "92#汽油" in ln and i + 1 < len(lines)), None)
        print(f"  OK   {slug:12s} -> {title}  | 92# 次行={p92}")
    except urllib.error.HTTPError as e:
        print(f"  {e.code}  {slug:12s}")
    except Exception as e:
        print(f"  ERR  {slug:12s} {type(e).__name__}")

print()
print("### B. 候选第二源可用性")
candidates = [
    ("chemcp 油价", "https://youjia.chemcp.com/"),
    ("usd-cny 油价", "https://oil.usd-cny.com/"),
    ("icauto 油价", "https://www.icauto.com.cn/oil/"),
    ("92hao 油价", "https://www.92hao.com/"),
]
for name, url in candidates:
    try:
        status, raw = fetch(url, timeout=15)
        lines = to_lines(decode(raw))
        title = lines[0][:60] if lines else ""
        has_price = any(re.search(r"\d\.\d{2}", ln) for ln in lines)
        has_prov = any(k in "".join(lines[:200]) for k in ("92", "95", "柴油"))
        print(f"  OK   {name:12s} {status} {len(raw)}B  标题={title}")
        print(f"       含价格={has_price} 含油品关键词={has_prov}")
    except urllib.error.HTTPError as e:
        print(f"  {e.code}  {name:12s} {url}")
    except Exception as e:
        print(f"  ERR  {name:12s} {type(e).__name__}: {e}")
