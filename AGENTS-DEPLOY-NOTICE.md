# ⚠️ 本目录是「部署副本」，不是代码源！

此处是 `gpu-idle-saver.service` 实际运行的工作目录（root 所有）。
**该服务已被 gpu-power-service 替换并删除，勿再启用。**

**请勿直接在 /opt/gpu-idle-saver/ 下修改代码或初始化 git。**

## 代码事实源（真正的 git 仓库）
    /home/sean/works/170hx4/gpu-idle-saver/
    git remote: git@github.com:344303947/gpu-idle-saver.git (main)

## 旧服务 gpu-idle-saver（已下线删除）
- 已被 `gpu-power-service` 替换，本机 systemd unit 已删除（不会误操作）。
- 仓库保留 gpu_idle_saver.py / config.ini / deploy.sh 仅作历史参考，勿再部署使用。

## 新服务：gpu-power-service（固定功率分配，已在生产机替换 gpu-idle-saver）
- 代码源：仓库子目录 `gpu-power-service/`（gpu_power_service.py + config.ini + systemd unit + deploy.sh）
- 部署目录：/opt/gpu-power-service/
- 见 gpu-power-service/AGENTS-DEPLOY-NOTICE.md 的安装/配置/自愈验证说明
- 生产机现状：gpu-idle-saver 已 disabled 且 unit 已删除，gpu-power-service 已 enabled + running
- 重要：两台服务都有 `nvidia-smi -pl` 写权限，**不可同时启用**，二选一
