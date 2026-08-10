#!/bin/bash
# 部署 gpu-idle-saver 到本机 /opt/gpu-idle-saver 并重启服务。
# 用法：./deploy.sh
# 前置：先 commit / push 好代码（git remote: 344303947/gpu-idle-saver）
set -euo pipefail

DEPLOY_DIR="/opt/gpu-idle-saver"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -d "$DEPLOY_DIR" ]]; then
    echo "部署目录不存在: $DEPLOY_DIR" >&2
    exit 1
fi

echo ">>> 同步 ${SRC_DIR} -> ${DEPLOY_DIR}"
sudo cp "$SRC_DIR/gpu_idle_saver.py" "$DEPLOY_DIR/gpu_idle_saver.py"
sudo cp "$SRC_DIR/config.ini" "$DEPLOY_DIR/config.ini"
echo ">>> 校验语法"
sudo python3 -m py_compile "$DEPLOY_DIR/gpu_idle_saver.py"
echo ">>> 重启 gpu-idle-saver.service"
sudo systemctl restart gpu-idle-saver.service
sleep 1
systemctl is-active gpu-idle-saver.service
echo ">>> 完成"
