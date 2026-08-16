# GPU 功率固定分配守护程序 (gpu-power-service)

v1.0.0 · 多卡推理机的**固定功率分配**守护程序：按在线/活跃卡数查表，每 60s 校验一次，
发现功耗上限被外部修改自动改回。

## 与 gpu-idle-saver 的关系

| | gpu-idle-saver | gpu-power-service |
|---|---|---|
| 定位 | 空闲省电 + 忙态按预算动态分配（带状态机去抖） | **固定功率分配**：按卡量查表直接下发，60s 周期校验纠偏 |
| 卡活跃判定 | 有（忙/闲状态机，慢进快出） | 可选（count/active 双模式，默认 count 不判定活跃） |
| 分配逻辑 | 空pu省电 + 忙卡查表 | 全部卡按"在线卡总数"统一查表（count 模式） |
| 自愈纠偏 | 无（只在状态迁移时下发） | **有**：每 check_interval 比对实际 power.limit 与目标值，异常立即改回 |
| 场景 | 需要空闲大省电 + 忙时按预算动态平衡 | 希望功率长期固定、不随负载抖动，又防外部误改 |

> 两台服务二选一，不可同时启用（都有 `nvidia-smi -pl` 写权限，会互相打架）。
> 当前生产机已启用 gpu-power-service 并禁用 gpu-idle-saver。

## 安装部署

```bash
# 1. 同步到部署目录
sudo mkdir -p /opt/gpu-power-service
sudo cp gpu_power_service.py config.ini /opt/gpu-power-service/
sudo python3 -m py_compile /opt/gpu-power-service/gpu_power_service.py

# 2. 安装 systemd 服务（unit 内 ExecStart 路径指向 /opt/gpu-power-service）
sudo cp gpu-power-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gpu-power-service

# 3. 先禁用旧服务（若还在运行）
sudo systemctl disable --now gpu-idle-saver

# 4. 查看运行状态与日志
systemctl status gpu-power-service
journalctl -u gpu-power-service -f
```

## 配置说明 (config.ini)

```
[general]
allocation_mode = count    # count=简单模式(默认)：按在线卡总数统一档位；active=按活跃卡数
check_interval = 60.0      # 检查/自愈纠偏周期（秒）
util_threshold = 5.0       # active 模式下活跃判定：利用率阈值(%)
power_threshold = 80.0     # active 模式下活跃判定：功耗阈值(W)

[power]
power_profile  = 4:185, 3:200, 2:250, 1:250   # 卡数 -> 每卡功耗上限(W)
power_cap     = 250.0       # 单卡授权上限(W)，任何卡不得超过
power_budget  = 750.0       # 整机 GPU 硬预算(W)：所有卡上限之和 ≤ 此值
power_idle    = 100.0       # 仅 active 模式：空闲卡固定档(W)
deadband      = 2.0         # 自愈死区(W)：|实际-目标| < 此值视为正常不干预
```

修改配置后：`sudo systemctl restart gpu-power-service`

## 验证自愈

```bash
# 手动把某卡功率上限改乱，60s 检查周期后应自动改回目标值
sudo nvidia-smi -i 3 -pl 100
sleep 65
nvidia-smi --query-gpu=index,power.limit --format=csv,noheader
# 3 应由 100 自动恢复为 185（4卡场景）
journalctl -u gpu-power-service -n 5   # 可见 "自愈纠偏下发 [GPU3:185W]"
```

## 安全机制

- **单实例锁**：`/var/run/gpu-power-service.lock`，防止重复进程互相覆盖
- **整机预算硬约束**：任何时刻 `sum(power.limit) ≤ power_budget(750W)`，超限从档位最高的卡削减
- **单卡上限**：`power.limit ≤ min(硬件max, power_cap)`（250W 封顶，防电源超载）
- **退出恢复**：收到 SIGTERM 退出时恢复所有卡到硬件默认功耗上限，不留低功耗残局