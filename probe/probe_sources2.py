"""第二轮探测：补迪拜原油缺口 + 摸清汽油价格网的数据结构。"""
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


def to_text(html):
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", "\n", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&")
    lines = [ln.strip() for ln in html.split("\n")]
    return [ln for ln in lines if ln]


SINA_REF = {"Referer": "https://finance.sina.com.cn"}

print("### A. 新浪外盘/国内原油代码地毯式测试")
codes = [
    "hf_OIL", "hf_CL", "hf_DUBAI", "hf_OMAN", "hf_MINAS",
    "hf_SC", "nf_SC0", "nf_SC", "hf_NG", "hf_DX", "hf_BRENT",
]
url = "https://hq.sinajs.cn/list=" + ",".join(codes)
try:
    _, raw = fetch(url, SINA_REF)
    for line in decode(raw).split(";"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'var hq_str_(\w+)="(.*)"', line)
        if m:
            code, val = m.group(1), m.group(2)
            flag = "OK " if val else "-- "
            print(f"  {flag}{code:12s} {val[:90]}")
except Exception as e:
    print(f"  FAIL {type(e).__name__}: {e}")

print()
print("### B. 汽油价格网首页 — 找调价预测/变化率相关文本")
try:
    _, raw = fetch("https://www.qiyoujiage.com/")
    lines = to_text(decode(raw))
    keys = ("调价", "变化率", "预计", "上调", "下调", "搁浅", "原油", "窗口")
    hits = [ln for ln in lines if any(k in ln for k in keys)]
    for ln in hits[:40]:
        print(f"  | {ln[:110]}")
    if not hits:
        print("  (无匹配，前 30 行原始文本如下)")
        for ln in lines[:30]:
            print(f"  | {ln[:110]}")
except Exception as e:
    print(f"  FAIL {type(e).__name__}: {e}")

print()
print("### C. 浙江省页面 — 看价格数据长什么样")
try:
    _, raw = fetch("https://www.qiyoujiage.com/zhejiang.shtml")
    lines = to_text(decode(raw))
    for ln in lines:
        if re.search(r"\d+\.\d{2}", ln) or "92" in ln or "95" in ln or "柴油" in ln:
            print(f"  | {ln[:110]}")
except Exception as e:
    print(f"  FAIL {type(e).__name__}: {e}")
