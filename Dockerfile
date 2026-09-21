FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY . .

# 非 root 运行；data/ 存 SQLite 缓存与订阅者
RUN useradd -m appuser \
    && mkdir -p data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# ⚠️ 必须单 worker：APScheduler 与 Telegram polling 线程都是进程内单例，
#    多 worker 会导致每实例各刷一次上游、各推一次，重复打扰用户。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
