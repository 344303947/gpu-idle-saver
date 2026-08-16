#!/bin/bash
# 部署 gpu-power-allocator 到本机 /opt/gpu-power-allocator 并重启服务。
# 用法：./deploy.sh
# 前置：先 commit / push 好代码（git remote: 344303947/gpu-idle-saver）
set -euo pipefail

DEPLOY_DIR="/opt/gpu-power-allocator"
UNIT="gpu-power-allocator.service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -d "$DEPLOY_DIR" ]]; then
    echo ">>> 创建部署目录 $DEPLOY_DIR"
    sudo mkdir -p "$DEPLOY_DIR"
fi

echo ">>> 同步 ${SRC_DIR} -> ${DEPLOY_DIR}"
sudo cp "$SRC_DIR/gpu_power_allocator.py" "$DEPLOY_DIR/gpu_power_allocator.py"
sudo cp "$SRC_DIR/config.ini" "$DEPLOY_DIR/config.ini"

echo ">>> 校验语法"
sudo python3 -m py_compile "$DEPLOY_DIR/gpu_power_allocator.py"

echo ">>> 安装 systemd unit"
sudo cp "$SRC_DIR/$UNIT" /etc/systemd/system/$UNIT
sudo systemctl daemon-reload

echo ">>> 启用并重启 $UNIT"
sudo systemctl enable $UNIT
sudo systemctl restart $UNIT
sleep 1
systemctl is-active $UNIT
echo ">>> 完成 (日志: journalctl -u $UNIT -f)"