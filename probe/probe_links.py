"""第五轮：从首页导航扒出准确的省份 URL，并测试 icauto 作为第二源。"""
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


print("### A. qiyoujiage 首页 — 省份链接")
_, raw = fetch("https://www.qiyoujiage.com/")
html = decode(raw)
links = re.findall(r'href="([^"]+\.shtml)"[^>]*>([^<]{1,12})<', html)
seen = {}
for href, text in links:
    slug = href.strip("/").replace(".shtml", "")
    if slug and slug not in seen:
        seen[slug] = text.strip()
print(f"  共 {len(seen)} 个 .shtml 链接")
for slug, text in sorted(seen.items()):
    print(f"    {slug:16s} {text}")
if "shaanxi" not in seen:
    shan = [s for s in seen if s.startswith("sha")]
    print(f"  含 'sha' 的 slug: {shan}")

print()
print("### B. icauto 油价页 — 是否含陕西及数据形态")
try:
    _, raw2 = fetch("https://www.icauto.com.cn/oil/")
    h2 = decode(raw2)
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h2)
    txt = re.sub(r"(?s)<[^>]+>", "\n", txt)
    lines = [ln.strip() for ln in txt.split("\n") if ln.strip()]
    idx = [i for i, ln in enumerate(lines) if "陕西" in ln]
    print(f"  含'陕西'的行数: {len(idx)}")
    for i in idx[:6]:
        print(f"    | {lines[i][:100]}")
    print("  --- 前 25 行文本 ---")
    for ln in lines[:25]:
        print(f"    | {ln[:100]}")
except Exception as e:
    print(f"  FAIL {type(e).__name__}: {e}")
