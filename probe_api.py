import os, sys, time, json, urllib.request, urllib.error

# 本地请求必须绕过沙箱代理，否则会被 MITM 代理返回 502
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

BASE = "http://127.0.0.1:8000"

def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:
        return None, str(e)

# 等启动
for i in range(30):
    st, _ = get("/api/health")
    if st in (200, 503):
        print(f"server up after ~{i*4}s (health={st})")
        break
    time.sleep(4)
else:
    print("server did not come up; last log:")
    sys.exit(1)

# 给 bootstrap 抓取留时间（31 省 + 新浪）
for i in range(20):
    st, body = get("/api/health")
    try:
        d = json.loads(body)
    except Exception:
        d = {}
    if st == 200 and d.get("coverage", 0) > 0:
        print(f"data ready after bootstrap (~{i*5}s): coverage={d['coverage']} generated_at={d.get('generated_at')}")
        break
    time.sleep(5)
else:
    print("bootstrap still pending after wait; health=", st, body[:200])

print("=== /api/health ===")
print(get("/api/health")[1])
print("=== /api/forecast ===")
st, b = get("/api/forecast")
print("status", st)
try:
    print(json.dumps(json.loads(b), ensure_ascii=False)[:600])
except Exception:
    print(b[:600])
print("=== /api/provinces (count) ===")
st, b = get("/api/provinces")
try:
    d = json.loads(b)
    print("provinces:", len(d.get("provinces", [])), "generated_at:", d.get("generated_at"))
except Exception:
    print(b[:300])
print("=== / (html head) ===")
st, b = get("/")
print("status", st, "len", len(b))
