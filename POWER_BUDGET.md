# 显卡功率服务：功率预算与安全设计说明

> 对应程序：`/opt/gpu-idle-saver`（源码副本 `/home/sean/works/170hx4/gpu-idle-saver`）
> 版本：**v0.3.0** · 更新：2026-08-09

---

## 1. 目标

本服务是 vLLM 多卡推理机的**显卡电源管理守护进程**，同时满足两件事：

1. **省电**：空闲卡压低功耗上限（`power_low`，默认 100W），降低空闲待机功耗；
2. **安全红线**：任意时刻**所有显卡 power.limit 之和不得超过整机 GPU 预算**，防止多卡同时拉满瞬时超载电源（宕机/断电/烧电源）。

绝不锁定频率，不引入额外延迟 —— 只通过 `nvidia-smi -pl` 控制功耗上限间接影响 P-State。

---

## 2. 安全模型（硬不变量）

**核心不变量（代码层强制，任何配置/卡数下都成立）：**

```
sum(所有卡的 power.limit)  ≤  power_budget
```

`power.limit` 是显卡的硬件功耗上限，驱动保证单卡实际功耗不会超过它，
因此只要各卡上限之和 ≤ 预算，整机 GPU 总功耗就一定 ≤ 预算。
这个不变量是**数学保证**，不是靠手工调参碰运气。

### 本机预算取值

| 项 | 值 | 说明 |
|----|----|------|
| 电源额定 | 1200W | 整机供电能力/危险线 |
| 预留（CPU/内存等） | 450W | 供 CPU、内存及其他部件 |
| **GPU 硬预算 `power_budget`** | **750W** | 1200 − 450 |

> 若电源额定或预留变化，按 `power_budget = 额定 − 预留` 调整 `config.ini` 即可。

---

## 3. 动态分配算法（忙卡均分）

每个采样周期（默认 2s）：

1. **空闲卡** → 固定 `power_low`（占用很小预算）；
2. **忙卡** → **均分**「`power_budget` − 空闲卡占用」，每张忙卡上限一致：
   - 适配 PP(流水线)推理：每卡同质负载、串联执行，**瓶颈卡决定全链路吞吐**，
     按实测功耗加权的"用得多拿得多"是正反馈，会让饱的越饱、饿的越饿、拖慢流水线；
   - 某卡物理上限（`max`）低于均分份额时 clamp 到 `max`，把差额再分给其他忙卡；
3. **兜底**：各卡取整后若总和仍超预算，从上限最大的忙卡开始削减 —— 最终恒有 `sum ≤ power_budget`。

```
忙的卡  → power = 均分份额（clamp 到每卡物理上限）
空闲的卡 → power = power_low
硬约束  → sum(all power.limit) ≤ power_budget
```

### 相比 v0.2.0 的改进

- 旧版用写死的档位表 `{4:180,3:200,2:250,1:300}`，**超过 4 卡时 `N×180` 会线性超载**（如 7 卡 = 1260W），且总和不做任何校验；
- v0.3.0 改为「整机预算」硬约束，**卡数多少、`power_low` 多大都不会超预算**；
- v0.3.2 忙卡统一**均分**剩余预算（替代 v0.3.0 的功耗加权），消除 PP 流水线的"饱的饱饿的饿"。

---

## 4. 典型场景实测（预算 750W，忙卡均分）

| 场景 | 分配结果 | 总和 |
|------|---------|------|
| **4 忙**（PP 同质） | 忙卡 ~188W/卡 | 750W ✓ |
| **3 忙 1 闲** | 忙卡 ~217W/卡，空闲 100W | 750W ✓ |
| **2 忙 2 闲** | 忙卡 300/250W（均分份额、受物理上限 clamp），空闲各 100W | 750W ✓ |
| **1 忙 3 闲** | 忙卡 300W，空闲各 100W | 600W ✓ |

> 实际负载大概率是「3 忙 1 闲」或「4 忙」：3 忙时忙卡合计 650W、4 忙时约 188W/卡，
> 均分保证每张忙卡上限一致，PP 流水线无短板。

---

## 5. 启动自检

`_validate_budget()`：校验「全空闲」最坏情况 `N × power_low ≤ power_budget`。
若不满足（如 `power_low` 顶得过高），自动下调 idle 档，保证分配算法的剩余预算始终为正、空闲态也不超限。

---

## 6. 验证

- `python3 -m py_compile gpu_idle_saver.py` 通过；
- mock 掉 `nvidia-smi` 的单元自测覆盖：4 忙均分、**忙卡数超过物理上限时 clamp + 剩余再分收敛**、`power_low` 顶高时自检下调、退出兜底不超限；
- 实际部署重启后首个下发周期日志确认：
  `[GPU0:186W, GPU1:188W, GPU2:188W, GPU3:188W] (活跃4卡, 总和750W ≤ 预算750W)`。

---

## 7. 部署 / 备份 / 回滚

### 部署（root）
```bash
sudo cp gpu_idle_saver.py config.ini /opt/gpu-idle-saver/
sudo systemctl restart gpu-idle-saver
journalctl -u gpu-idle-saver -f
```

### 备份（改动前务必先备份）
```bash
TS=$(date +%Y%m%d-%H%M%S)
sudo mkdir -p /opt/gpu-idle-saver.bak-$TS
sudo cp -a /opt/gpu-idle-saver/. /opt/gpu-idle-saver.bak-$TS/
```

### 回滚
```bash
sudo systemctl stop gpu-idle-saver
sudo cp /opt/gpu-idle-saver.bak-<时间戳>/* /opt/gpu-idle-saver/
sudo systemctl start gpu-idle-saver
```

> 现存备份：`/opt/gpu-idle-saver.bak-20260809-233233/`（v0.2.0）

---

## 8. 风险与注意

- `power_budget` 是 **GPU 独享**上限，已扣掉 450W 给 CPU 等；**不要**为了单卡更高把它调回 1200W，否则 CPU 一高负载就会顶到电源危险线。
- 忙→闲进省电需 60s（保持满血，有富余），闲→忙唤醒约 6s（期间偏低，是性能、非功率问题）。
- 退出兜底 `_restore_all` 按 `min(档位, 预算/N)` 恢复，N 再多也不会超预算。
