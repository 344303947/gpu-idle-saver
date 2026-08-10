# ⚠️ 本目录是「部署副本」，不是代码源！

此处是 `gpu-idle-saver.service` 实际运行的工作目录（root 所有），
由 systemd 直接 ExecStart 引用。

**请勿直接在 /opt/gpu-idle-saver/ 下修改代码或初始化 git。**

## 代码事实源（真正的 git 仓库）
    /home/sean/works/170hx4/gpu-idle-saver/
    git remote: git@github.com:344303947/gpu-idle-saver.git (main)

## 正确的修改流程
1. 改 /home/sean/works/170hx4/gpu-idle-saver/ 下的源文件
2. git commit + git push
3. 把改好的 gpu_idle_saver.py / config.ini 同步到本目录：
       sudo cp /home/sean/works/170hx4/gpu-idle-saver/{gpu_idle_saver.py,config.ini} /opt/gpu-idle-saver/
4. sudo systemctl restart gpu-idle-saver

## 相关配置文件
- /etc/systemd/system/gpu-idle-saver.service   （服务 unit，也请保持与仓库内一致）
