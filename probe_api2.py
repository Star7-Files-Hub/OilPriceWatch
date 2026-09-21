import os, json, urllib.request, urllib.error
os.environ.update({"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"})
for k in ("http_proxy", "https_proxy"):
    os.environ.pop(k, None)
BASE = "http://127.0.0.1:8000"

def req(method, path, token=None):
    headers = {}
    if token:
        headers["X-Admin-Token"] = token
    r = urllib.request.Request(BASE + path, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:
        return None, str(e)

print("province shanxi-3 (陕西):", req("GET", "/api/province/shanxi-3")[0])
print("province bad (404):", req("GET", "/api/province/nowhere")[0])

# 手动刷新（本机未设 ADMIN_TOKEN，应放行）
st, b = req("POST", "/api/refresh")
print("refresh:", st, b[:120])

# 设 anchor 触发窗口计算（用一个过去的工作日作为上次调价日）
st, b = req("PUT", "/api/settings/anchor?anchor=2026-09-09")
print("set anchor:", st, b[:200])
st, b = req("GET", "/api/forecast")
try:
    d = json.loads(b)
    print("window after anchor:", json.dumps(d.get("window"), ensure_ascii=False))
except Exception:
    print("forecast parse fail", b[:200])

# 限流测试：连发 70 次 health，应出现 429
codes = []
for _ in range(70):
    codes.append(req("GET", "/api/health")[0])
print("429 count in 70 hits:", sum(1 for c in codes if c == 429), " first codes:", codes[:5], "...", codes[-5:])
