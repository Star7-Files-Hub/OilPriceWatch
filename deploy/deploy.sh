#!/usr/bin/env bash
# 部署 OilPriceWatch 到远程服务器（systemd + nginx）。
#
# 前提（在目标服务器上一次性准备）：
#   1. 装好 python3 / nginx / certbot（certbot 用 --nginx 申请证书）
#   2. useradd -m oilwatch && mkdir -p /opt/oilwatch && chown oilwatch:oilwatch /opt/oilwatch
#   3. 把 oilwatch.service 放到 /etc/systemd/system/，nginx 配置放到 sites-enabled/
#   4. 在 /opt/oilwatch/.env 填好 token/密钥（见 .env.example）
#   5. systemctl enable --now oilwatch
#
# 用法： ./deploy.sh user@host [remote_dir]
#   例如：./deploy.sh root@<你的服务器> /opt/oilwatch
#
# ⚠️ 本脚本用 rsync 同步（Linux 一般自带）；若目标机没有 rsync，可改用 scp -r。
set -euo pipefail

REMOTE="${1:?用法: deploy.sh user@host [remote_dir]}"
REMOTE_DIR="${2:-/opt/oilwatch}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> 同步代码（排除 data/、.git、本地缓存、探测脚本）"
rsync -az --delete \
  --exclude data --exclude '.git' --exclude '__pycache__' \
  --exclude 'outputs' --exclude 'probe' --exclude '*.db' --exclude '*.pyc' \
  --exclude 'data' \
  "$HERE/" "$REMOTE:$REMOTE_DIR/"

echo "==> 远程：建 venv + 装依赖 + 重启服务"
ssh "$REMOTE" bash -s <<EOF
set -e
cd "$REMOTE_DIR"
if [ ! -d venv ]; then python3 -m venv venv; fi
./venv/bin/pip install -q -r requirements.txt
sudo systemctl restart oilwatch
EOF

echo "==> 完成。查看状态：ssh $REMOTE 'journalctl -u oilwatch -f'"
