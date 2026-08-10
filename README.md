# GPU 空闲自动省电守护程序

vLLM 多卡推理机的 GPU 电源管理守护程序：**空闲时自动降低每张卡的功耗上限省电，忙时按整机电源预算恢复满性能**。绝不锁定频率，推理延迟零影响。

---

## 目录

1. [背景与设计原理](#背景与设计原理)
2. [工作原理](#工作原理)
3. [安装部署](#安装部署)
4. [配置说明](#配置说明)
5. [日常使用](#日常使用)
6. [安全机制](#安全机制)
7. [故障排查](#故障排查)
8. [常见问题 FAQ](#常见问题-faq)

---

## 背景与设计原理

### 为什么不做"手动切换 P0/P8"？
显卡的 P0/P8 性能状态由**驱动按负载自动管理**，任何用户态程序都无法强制切换。真正可控制的是 `nvidia-smi -pl`（功耗上限）。因此本程序通过**控制功耗上限**间接影响 P-State：

- **空闲**时压低功耗上限（默认 100W）→ 驱动自动进入更深 P-State → 省电
- **忙**时恢复功耗上限 → 驱动自动拉满频率回到 P0 → 满性能

### 为什么采用"整机预算 + 动态智能分配"？

整机**电源功率有限（本机 1200W）**。显卡出厂固件允许每卡最高 100~300W，但多张卡**绝不能同时各自拉满**——4×300=1200W 已逼近电源极限，若再有 CPU/内存等负载会瞬时超载、导致宕机/断电/电源损坏。因此为 CPU/其他部件**预留 450W**，GPU 合计只占 750W。

因此本程序用一条**硬性安全红线**兜底：

> **任意时刻，所有 GPU 的 `power.limit` 之和 ≤ `power_budget`（本机默认 750W）。**

在这个总预算内做**动态智能分配**：
- 空闲卡固定压到 `power_low`，只占很小一部分预算；
- 忙卡按各自**实测功耗（power.draw）加权**瓜分"预算 − 空闲占用"——用得多的卡拿到更高上限；
- 数学上保证总和恒 ≤ `power_budget`，无论几张卡、卡多卡少、`power_low` 设多大。

不再依赖手工写死的档位表，天然防超载。

---

## 工作原理

### 每卡独立状态机 + 动态电源预算

```
            ┌──────────────────────────────────────────────┐
            │          每张卡各自独立判定 忙/闲             │
            └──────────────────────────────────────────────┘
   BUSY(满血) ◀───────────────  IDLE(省电)
        ▲                           │
        │ 连续 idle_samples 次空闲    │ 连续 busy_samples 次忙
        │ （默认 30×2s=60s，慢进）    │ （默认 3×2s=6s，唤醒）
        └───────────────────────────┘
```

- **进入省电要慢**（60s 无负载）→ 防止瞬时尖峰误伤，避免频繁切换
- **唤醒要相对快**（6s 持续有负载）→ 请求来了尽快满血
- **每张卡独立判定**：某张卡忙就恢复那张，不影响其他卡

### 功率动态智能分配

每个采样周期：
- **空闲卡** → 固定 `power_low`；
- **忙卡** → 各卡按实测功耗 `power.draw` 作为权重，瓜分"`power_budget` − 空闲卡占用"，权重低（用得少）的卡先被满足、权重高（用得多）的卡拿到更多上限；
- 任何情况下各卡上限取整后若仍超预算，从上限最大的忙卡开始削减，**最终总和 ≤ `power_budget`**。

```
忙的卡  → power = min( 权重化分配值, 单卡物理上限 )
空闲的卡 → power = power_low（默认 100W）
硬约束  → sum(all power.limit) ≤ power_budget（本机默认 750W）
```

### 采样与执行周期

```
主循环（每 2 秒）:
  1. sample()      → nvidia-smi 抓取每卡 pstate/利用率/功耗
  2. evaluate()    → 每卡独立判定忙闲，双向去抖
  3. apply_limits() → 动态分配并仅对"目标值有变化"的卡执行 nvidia-smi -pl
```

---

## 安装部署

已通过 systemd 服务部署并启用（开机自启）。

### 手动安装（重装/新环境）

```bash
# 1. 拷贝程序到 /opt（root 专属）
sudo mkdir -p /opt/gpu-idle-saver
sudo cp gpu_idle_saver.py config.ini /opt/gpu-idle-saver/
sudo chown root:root /opt/gpu-idle-saver/*

# 2. 安装 systemd 服务
sudo cp gpu-idle-saver.service /etc/systemd/system/
sudo systemctl daemon-reload

# 3. 启用 + 启动
sudo systemctl enable --now gpu-idle-saver
```

> 要求：root 权限（改功耗需要）、NVIDIA 驱动、`nvidia-smi` 在 PATH 中、Python3（任意版本，无第三方依赖，用 `/usr/bin/python3` 即可）。

---

## 配置说明

配置文件：`/opt/gpu-idle-saver/config.ini`

```ini
[general]
# 采样间隔（秒），即状态机判定周期
interval = 2.0

# 连续多少采样空闲才进入省电（慢进去抖）：30×2s = 60s
idle_samples = 30

# 连续多少采样忙才唤醒（快去抖）：3×2s = 6s
busy_samples = 3

# 单卡判定"忙"的阈值：利用率>5% 或 功耗>80W 即视为忙
busy_util_threshold = 5.0
busy_power_threshold = 80.0

# 管理的 GPU：all=全部；也可 0,1,2 或 0-3
gpu_ids = all

[power]
# 省电态功耗上限（W），0 = 自动取每卡 min_limit
power_low = 100.0

# 整机 GPU 功耗硬预算（W）——安全关键！所有卡功耗上限之和绝不允许超过它。
# 电源额定 1200W - 预留 450W 给 CPU/内存等 -> 本机 GPU 预算 750W。
power_budget = 750.0

# 动态分配下发死区（W）：与当前生效值差 < 该值视为微小调整，不立即下发；重大变化(差≥死区)才会执行
deploy_deadband = 10.0

# 微小调整的最短下发间隔（秒）：抑制 ±几瓦抖动反复 -pl；重大变化不受此限制
deploy_min_interval = 10.0

# 恢复/退出兜底的"每卡满血档位"参考（仅 _restore_all 用，仍会被 power_budget 封顶）
power_budget_by_active = 4:180, 3:200, 2:250, 1:300
```

### 调参建议

| 场景 | 改动 |
|------|------|
| 想更省电（不怕唤醒慢） | 调大 `idle_samples`、调小 `busy_samples`、调低 `power_low` |
| 怕频繁切换（负载贴边抖动） | 调大 `busy_samples`，或提高 `busy_power_threshold`/`busy_util_threshold` |
| 电源升级/降级 | 只改 `power_budget` 一行（整机GPU预算） |
| 想给某类负载更大功率 | 卡内任务自然按实际功耗加权，无需改表 |
| 只管理部分卡（如留 1 卡常驻） | `gpu_ids = 0` |

**安全红线**：`power_low` 不能低于单卡物理 `min_limit`（程序自动 clamp）；`power_budget` 是 GPU 总和上限，
应为**电源额定减去预留**（本机 1200W − 450W = 750W）。程序内部会再强制 `sum(power.limit) ≤ power_budget`，
即使配置/卡数异常也超不出去。

---

## 使用

### 常用命令

```bash
# 查看服务状态
sudo systemctl status gpu-idle-saver

# 实时跟踪切换日志
sudo journalctl -u gpu-idle-saver -f

# 查看最近日志
sudo journalctl -u gpu-idle-saver -n 50

# 重启（改配置后必做）
sudo systemctl restart gpu-idle-saver

# 停止
sudo systemctl stop gpu-idle-saver

# 开机/取消开机自启
sudo systemctl enable gpu-idle-saver
sudo systemctl disable gpu-idle-saver
```

### 直接观测

```bash
# 各卡功耗/上限实时
watch -n2 nvidia-smi
```

### dry-run 预演（不改任何功耗，只观察状态机判定）

```bash
python3 /home/sean/works/170hx4/gpu-idle-saver/gpu_idle_saver.py --dry-run
```

### 源码开发版位置

本仓库源码副本：`/home/sean/works/170hx4/gpu-idle-saver/`
改代码后需同步到 `/opt` 并重启服务：

```bash
sudo cp gpu_idle_saver.py config.ini /opt/gpu-idle-saver/
sudo systemctl restart gpu-idle-saver
```

---

## 安全机制

| 机制 | 说明 |
|------|------|
| **单实例锁** | flock 锁（`/var/run/gpu-idle-saver.lock`），重复启动第二实例会被拒绝 |
| **整机硬预算** | 所有卡上限之和恒 ≤ `power_budget`（本机默认750W=1200W电源−预留450W），动态分配数学保证 + 下发前再强制削减，任何卡数/配置都超不出去 |
| **启动预算自检** | 开机校验 `N×power_low ≤ 预算`，否则自动下调 idle 档，避免"全空闲"就超限 |
| **慢进快出** | 进省电需 60s 空闲去抖，唤醒只需 6s，避免抖动 |
| **N/A 视为忙** | 驱动查询异常（N/A/失联）时保守判定为忙，绝不当空闲降功耗 |
| **失败不切状态** | `nvidia-smi -pl` 失败（rc≠0）时不生效并自动重试，日志报错 |
| **退出兜底** | 任何退出路径（信号/崩溃/异常）都会在 finally 按全部卡数预算恢复功耗 |
| **信号处理** | 信号处理器只置停止标志，不做阻塞调用，恢复动作统一走 finally |
| **配置容错** | 非法 `gpu_ids` / 预算自动回退默认档 |
| **无第三方依赖** | 纯标准库，Python2/3 通用（当前用系统 python3） |

---

## 故障排查

| 现象 | 原因与解法 |
|------|-----------|
| 服务启动后 4 卡保持在较高功耗一项不变久 | 当前 4 卡都在忙（有负载/利用率>5%），属正常；空闲 60s 后自动降 |
| 日志报 `nvidia-smi ... 失败 (rc≠0)` | 权限不足：需 root 运行；或驱动重置，稍后自动重试 |
| 日志没有"进入省电态" | 卡一直判定忙，检查 `busy_util_threshold` / `busy_power_threshold` 是否过低 |
| 日志报 "已有实例在运行" | 有重复实例（如手动跑 + 服务同时开），用 `systemctl status` 或 `pgrep` 排查 |
| 修改 config 不生效 | 改在 `/opt/gpu-idle-saver/config.ini` 后必须 `systemctl restart` |
| 幂等下发 | 同目标值只下发一次；`deployed` 记录已生效值，避免每 2s 重复 `-pl` |

---

## 卸载

```bash
# 恢复满血功耗 + 停止服务
sudo systemctl stop gpu-idle-saver
sudo systemctl disable gpu-idle-saver
sudo rm -f /etc/systemd/system/gpu-idle-saver.service
sudo rm -rf /opt/gpu-idle-saver
sudo systemctl daemon-reload
```

> 注：程序正常退出时已自动恢复功耗上限。系统重启后驱动会恢复出厂默认功耗上限，无需手动处理。

---

## 版本记录

| 版本 | 变化 |
|------|------|
| v0.1.0 | 初版：整机两级状态机（全局忙/闲）、固定满电值 |
| v0.2.0 | **重构**：每卡独立状态机；满电功耗改为**动态电源预算**（按活跃卡数分配）；全面对抗性修复（单实例锁、N/A 防御、失败重试、双向去抖） |
| v0.3.0 | **安全加固 + 动态智能分配**：引入整机硬预算 `power_budget`(本机默认750W=1200W电源−预留450W)，忙卡按实测功耗加权分配，硬保证 `sum(power.limit)≤预算`；修复"卡数>4 会超载"与退出兜底超限的问题 |
| v0.3.1 | **下发死区**：新增 `deploy_deadband`(默认10W) + `deploy_min_interval`(默认10s)，微小调整(差<死区)受间隔抑制，重大变化立即下发；忙卡 ±几瓦抖动不再每 2s 刷 `nvidia-smi -pl` |

---

*文档对应版本：v0.3.1 · 整机 GPU 硬预算默认 750W（电源1200W 预留450W给CPU等），动态智能分配 + 下发死区*