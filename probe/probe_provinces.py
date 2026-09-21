"""第三轮探测：验证全国 31 省页面覆盖率 + 提取价格的能力。"""
import os
import re
import ssl
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

PROXY = os.environ.get("https_proxy") or os.environ.get("http_proxy")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

PROVINCES = [
    ("beijing", "北京"), ("tianjin", "天津"), ("hebei", "河北"),
    ("shanxi", "山西"), ("neimenggu", "内蒙古"), ("liaoning", "辽宁"),
    ("jilin", "吉林"), ("heilongjiang", "黑龙江"), ("shanghai", "上海"),
    ("jiangsu", "江苏"), ("zhejiang", "浙江"), ("anhui", "安徽"),
    ("fujian", "福建"), ("jiangxi", "江西"), ("shandong", "山东"),
    ("henan", "河南"), ("hubei", "湖北"), ("hunan", "湖南"),
    ("guangdong", "广东"), ("guangxi", "广西"), ("hainan", "海南"),
    ("chongqing", "重庆"), ("sichuan", "四川"), ("guizhou", "贵州"),
    ("yunnan", "云南"), ("xizang", "西藏"), ("shaanxi", "陕西"),
    ("gansu", "甘肃"), ("qinghai", "青海"), ("ningxia", "宁夏"),
    ("xinjiang", "新疆"),
]


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


def parse_price(lines, label):
    """在行列表里找 'XX92#汽油' 这类标签，返回紧随其后的数字。"""
    for i, ln in enumerate(lines):
        if label in ln:
            for j in range(i + 1, min(i + 4, len(lines))):
                m = re.match(r"^(\d+\.\d{1,3})$", lines[j])
                if m:
                    return float(m.group(1))
    return None


def probe_one(slug, name):
    try:
        _, raw = fetch(f"https://www.qiyoujiage.com/{slug}.shtml")
        lines = to_lines(decode(raw))
        p92 = parse_price(lines, "92#汽油")
        p95 = parse_price(lines, "95#汽油")
        p0 = parse_price(lines, "0#柴油")
        forecast = ""
        for ln in lines:
            if "预计" in ln and "元/吨" in ln:
                forecast = ln[:60]
                break
        ok = all(v is not None for v in (p92, p95, p0))
        mark = "OK " if ok else "WARN"
        print(
            f"  {mark} {name:5s} 92#={p92}  95#={p95}  0#={p0}"
            + (f"   [{forecast}]" if forecast else "")
        )
        return ok
    except urllib.error.HTTPError as e:
        print(f"  FAIL {name:5s} HTTP {e.code}")
        return False
    except Exception as e:
        print(f"  FAIL {name:5s} {type(e).__name__}: {e}")
        return False


print("### 全国 31 省页面覆盖率")
with ThreadPoolExecutor(max_workers=8) as ex:
    results = list(ex.map(lambda t: probe_one(*t), PROVINCES))

ok_count = sum(1 for r in results if r)
print()
print(f"### 结果: {ok_count}/{len(PROVINCES)} 省三项价格全部解析成功")
