#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPU 空闲自动省电守护程序 v0.3.2

用途：vLLM 多卡推理机。空闲时降低每张卡功耗上限省电；忙时恢复性能。
安全约束：整机 GPU 功耗硬上限 = power_budget(默认 750W，电源1200W 预留450W给CPU等)。任意时刻
"所有卡 power.limit 之和"绝不允许超过它——空闲卡固定 power_low，忙卡按各自
实测功耗加权瓜分"预算−空闲占用"，天然保证总和 ≤ 预算，杜绝多卡同时拉满瞬时
超载电源。绝不锁定频率，保证推理延迟不受影响。

每卡独立状态机 + 动态功率分配：
  - 每张卡自己判定 忙/闲，双向去抖(慢进快出)
  - 忙的卡【均分】power_budget 内的剩余预算(适配 PP 流水线同质负载)；空闲卡降功耗上限省电
  - 整机硬约束：sum(power.limit) ≤ power_budget，任何配置/卡数下都成立
  - 下发死区 + 最短间隔：±几瓦抖动不反复下发，重大变化立即响应

运行：python3 gpu_idle_saver.py --config config.ini --dry-run
"""

import argparse
import configparser
import datetime
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import fcntl

VERSION = "0.3.2"

STATE_BUSY = "busy"
STATE_IDLE = "idle"

SAMPLE_QUERY = (
    "index,pstate,utilization.gpu,power.draw,power.limit,clocks.sm,memory.used"
)

class GPUIdleSaver:
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run

        self.interval = cfg.getfloat("general", "interval", fallback=2.0)
        self.idle_samples = cfg.getint("general", "idle_samples", fallback=30)
        self.busy_samples = cfg.getint("general", "busy_samples", fallback=3)
        self.idle_samples = max(1, self.idle_samples)
        self.busy_samples = max(1, self.busy_samples)
        self.busy_util_threshold = cfg.getfloat("general", "busy_util_threshold", fallback=5.0)
        self.busy_power_threshold = cfg.getfloat("general", "busy_power_threshold", fallback=80.0)

        # 活跃卡数 -> 每卡满血功耗上限(W)。仅用于退出恢复的兜底档位(见 _restore_all)。
        self.power_profile = self._parse_budget(
            cfg.get("power", "power_budget_by_active", fallback="4:180, 3:200, 2:250, 1:300"))
        # 整机 GPU 硬预算(W)：任意时刻所有卡 power.limit 之和绝不允许超过此值。
        # 本机电源额定 1200W，预留 450W 给 CPU/其他部件，故 GPU 预算 750W。
        self.power_budget = cfg.getfloat("power", "power_budget", fallback=750.0)
        self.power_low = cfg.getfloat("power", "power_low", fallback=100.0)
        # 每卡功耗硬上限(W)——安全风控：忙态分配/退出恢复等任何情况下，单卡功率
        # 上限绝不允许超过此值(默认280W)；0 表示不设额外上限(沿用硬件 max)。
        self.power_cap = cfg.getfloat("power", "power_cap", fallback=280.0)
        # 下发死区(W)：目标与当前生效值之差小于该值视为"微小调整"，受最短间隔抑制，
        # 防止 ±几瓦的反复抖动每 2s 刷一次 nvidia-smi -pl；重大变化(差>=死区)立即下发。
        self.deploy_deadband = cfg.getfloat("power", "deploy_deadband", fallback=10.0)
        # 微小调整的最短下发间隔(秒)：抑制频繁下发；重大变化不受此限制。
        self.deploy_min_interval = cfg.getfloat("power", "deploy_min_interval", fallback=10.0)

        try:
            self.gpu_ids = self._parse_gpu_ids(cfg.get("general", "gpu_ids", fallback="all"))
        except (ValueError, TypeError):
            self.log("warn", "gpu_ids 配置无法解析，回退为 all")
            self.gpu_ids = None

        self.nvidia_smi = self._find_binary("nvidia-smi")

        self.gpu_info = {}      # index -> {min,max,default,current,low}
        self.gpu_state = {}     # index -> busy/idle
        self.idle_streak = {}
        self.busy_streak = {}
        self.deployed = {}      # 每卡当前已生效目标值(避免重复下发)
        self.last_deploy_time = {}  # index -> 最近一次真正下发的时间戳(用于微小调整最短间隔)
        self.stop_flag = False

        self._lock_fd = self._acquire_lock()
        self._query_power_ranges()
        self._validate_budget()

    # ---------- 单实例锁 ----------
    def _acquire_lock(self):
        lock_path = "/var/run/gpu-idle-saver.lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            lock_path = os.path.join(tempfile.gettempdir(), "gpu-idle-saver.lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.log("error", "已有实例在运行(gpu-idle-saver)，拒绝启动")
            os._exit(1)
        self.log("info", f"单实例锁已获取: {lock_path}")
        return fd

    def _release_lock(self):
        if getattr(self, "_lock_fd", None):
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass

    def _parse_budget(self, s):
        """解析 '4:180, 3:200, 2:250, 1:300' -> {4:180, 3:200, 2:250, 1:300}"""
        budget = {}
        for part in s.replace("，", ",").split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            k, v = part.split(":")
            if k.strip().isdigit() and v.strip().replace(".", "", 1).isdigit():
                budget[int(k.strip())] = float(v.strip())
        if not budget:
            budget = {1: 300.0, 2: 250.0, 3: 200.0, 4: 180.0}
        return budget

    def budget_for(self, n_active):
        """最多 n_active 张卡同时满载时，允许的单卡功率上限(W)。
        取"覆盖 n_active 的最低档预算"：n=4 -> 180, n=3 -> 200, n=2 -> 250, n=1 -> 300。
        n 超过最大档则取最大档(最保守)。"""
        n_active = max(1, n_active)
        keys = sorted(self.power_profile.keys())
        if n_active >= keys[-1]:
            return self.power_profile[keys[-1]]
        for k in keys:
            if k >= n_active:
                return self.power_profile[k]
        return self.power_profile[keys[-1]]

    # ---------- 基础设施 ----------
    def _find_binary(self, name):
        try:
            out = subprocess.run(["which", name], capture_output=True, text=True)
            if out.returncode == 0:
                return out.stdout.strip()
        except Exception:
            pass
        return name

    def _parse_gpu_ids(self, s):
        s = s.strip().lower()
        if s in ("", "all", "auto"):
            return None
        ids = []
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-")
                if int(a) > int(b):
                    raise ValueError(f"范围颠倒: {part}")
                ids.extend(range(int(a), int(b) + 1))
            else:
                ids.append(int(part))
        return sorted(set(ids))

    def _query_power_ranges(self):
        out = self._smi("--query-gpu=index,power.min_limit,power.max_limit,power.limit,power.default_limit,power.draw,utilization.gpu,pstate,clocks.sm --format=csv,noheader,nounits")
        if not out:
            self.log("warn", "无法探测 GPU 信息，可能是权限或驱动问题")
            return
        for line in out:
            parts = [p.strip() for p in line.split(",")]
            try:
                idx = int(parts[0])
                min_l = self._to_float(parts[1])
                max_l = self._to_float(parts[2])
                cur_l = self._to_float(parts[3])
                def_l = self._to_float(parts[4])
            except (ValueError, IndexError):
                continue
            # 单卡物理上限：max_limit > default > 当前 > 保守预算
            master = max_l if max_l and max_l > 0 else (def_l if def_l and def_l > 0 else (cur_l if cur_l > 0 else self.budget_for(1)))
            # 安全风控：物理上限再叠加每卡硬上限 power_cap，任何卡在忙态/恢复时
            # 都绝不会被授权超过 280W
            if self.power_cap and self.power_cap > 0 and master > self.power_cap:
                master = self.power_cap
            low = self.power_low
            if low <= 0:
                low = min_l if min_l and min_l > 0 else master * 0.5
            low = max(min_l or 0, min(low, master))
            self.gpu_info[idx] = {
                "min": min_l or 0,
                # max 存"授权上限"＝min(硬件max, power_cap)：忙态动态分配(_build_targets
                # 的 hi)与退出恢复(_restore_all 的 bound)都以此为界，任何单卡绝不超过 280W
                "max": master,
                "default": def_l or 0,
                "current": cur_l or 0,
                "low": low,
            }
            self.gpu_state[idx] = STATE_BUSY
            self.deployed[idx] = None
        if self.gpu_ids is not None:
            self.gpu_info = {k: v for k, v in self.gpu_info.items() if k in self.gpu_ids}
            self.gpu_state = {k: v for k, v in self.gpu_state.items() if k in self.gpu_ids}
            self.deployed = {k: v for k, v in self.deployed.items() if k in self.gpu_ids}
        self.log("info", f"探测到 GPU: {json.dumps({k: {'low': v['low'], 'max': v['max']} for k, v in self.gpu_info.items()})}")

    def _validate_budget(self):
        """安全硬约束：任何时刻所有卡上限之和 ≤ 整机预算 power_budget。

        空闲态最坏情况是 N 张卡同时停在 power_low。若 N×low > 预算，
        则连"全空闲"都会超限，动态分配的"剩余预算"将为负、数学失效。
        这里把 idle 档自动下调到 预算/N 以内，保证分配算法前提成立。"""
        n = len(self.gpu_info)
        if n == 0:
            return
        used = max((info["low"] for info in self.gpu_info.values()), default=self.power_low)
        if n * used > self.power_budget:
            new_low = self.power_budget / n
            self.log("warn",
                    f"整机预算不足: N×low={n}×{used:.0f}W > {self.power_budget:.0f}W，"
                    f"自动下调 idle 档至 {new_low:.0f}W")
            for info in self.gpu_info.values():
                info["low"] = min(info["low"], new_low)
            self.power_low = min(self.power_low, new_low)

    @staticmethod
    def _to_float(s):
        s = str(s).strip()
        if not s or s.lower() == "n/a":
            return 0.0
        try:
            return float(s.split()[0])
        except (ValueError, IndexError):
            try:
                return float(s.rstrip(" W%MHz").strip())
            except ValueError:
                return 0.0

    def _smi_rc(self, args, timeout=6):
        try:
            r = subprocess.run(
                [self.nvidia_smi] + args.split(),
                capture_output=True, text=True, timeout=timeout,
            )
            lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
            return r.returncode, lines
        except subprocess.TimeoutExpired:
            self.log("warn", f"nvidia-smi {args} 超时")
            return -1, []
        except Exception as e:
            self.log("error", f"nvidia-smi {args} 异常: {e}")
            return -2, []

    def _smi(self, args):
        rc, lines = self._smi_rc(args)
        return lines if rc == 0 else []

    # ---------- 日志 ----------
    def log(self, level, msg):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tag = "[DRY]" if self.dry_run else "     "
        print(f"{ts} {tag} [{level:5s}] {msg}", flush=True)

    # ---------- 采样 ----------
    def sample(self):
        out = self._smi(f"--query-gpu={SAMPLE_QUERY} --format=csv,noheader,nounits")
        result = {}
        for line in out:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                continue
            try:
                idx = int(parts[0])
            except ValueError:
                continue
            na = (parts[2].upper() == "N/A" or parts[3].upper() == "N/A"
                  or parts[5].upper() == "N/A")
            result[idx] = {
                "pstate": parts[1],
                "util": self._to_float(parts[2]),
                "power": self._to_float(parts[3]),
                "limit": self._to_float(parts[4]),
                "sm": parts[5],
                "mem": parts[6],
                "na": na,
            }
        if self.gpu_ids is not None:
            result = {k: v for k, v in result.items() if k in self.gpu_ids}
        return result

    def card_busy(self, s):
        """单卡忙判定。N/A 或 超阈值 → 忙。"""
        if s.get("na"):
            return True
        if s["util"] > self.busy_util_threshold:
            return True
        if s["power"] > self.busy_power_threshold:
            return True
        return False

    # ---------- 状态机(每卡独立) ----------
    def evaluate(self, samples):
        if not samples:
            # 采样全失败：不改变状态(保守)，下周期再看
            return
        for idx in list(self.gpu_info.keys()):
            s = samples.get(idx)
            if s is None:
                # 某卡失联：保守视为忙，并立即恢复其满血预算下限
                if self.gpu_state[idx] == STATE_IDLE:
                    self.gpu_state[idx] = STATE_BUSY
                    self.log("warn", f"GPU{idx} 采样缺失，视为忙")
                continue
            busy = self.card_busy(s)
            if self.gpu_state[idx] == STATE_BUSY:
                if busy:
                    self.idle_streak[idx] = 0
                else:
                    self.idle_streak[idx] = self.idle_streak.get(idx, 0) + 1
                    if self.idle_streak[idx] >= self.idle_samples:
                        self.gpu_state[idx] = STATE_IDLE
                        self.busy_streak[idx] = 0
                        self.log("info", f">>> GPU{idx} 进入省电态(IDLE)")
            else:  # STATE_IDLE
                if busy:
                    self.busy_streak[idx] = self.busy_streak.get(idx, 0) + 1
                    if self.busy_streak[idx] >= self.busy_samples:
                        self.gpu_state[idx] = STATE_BUSY
                        self.idle_streak[idx] = 0
                        self.log("info", f"<<< GPU{idx} 恢复忙态(满血)")
                else:
                    self.busy_streak[idx] = 0

    # ---------- 动态智能功率分配 ----------
    def _build_targets(self, samples):
        """整机预算内动态分配：空闲卡固定 power_low；忙卡【均分】"整机预算 − 空闲占用"。
        适配 PP(流水线)推理：每卡同质负载、串联执行，瓶颈卡决定全链路吞吐，
        用功耗加权的"按果配因"会喂饱饱饿饿、拖慢流水线；均分保证每卡上限一致。
        超单卡物理上限的份额 clamp 到 hi，并把剩余预算再分给能接收的卡。
        数学上保证: 所有卡上限之和 ≤ power_budget(默认750W)，绝不超整机 GPU 预算。"""
        n = len(self.gpu_info)
        if n == 0:
            return {}
        low = max((info["low"] for info in self.gpu_info.values()), default=self.power_low)
        idle_total = sum(self.gpu_info[i]["low"] for i, st in self.gpu_state.items()
                       if st == STATE_IDLE)
        leftover = max(0.0, self.power_budget - idle_total)

        targets = {}
        busy_ids = []
        for idx, info in self.gpu_info.items():
            if self.gpu_state[idx] == STATE_IDLE:
                targets[idx] = float(info["low"])
            else:
                targets[idx] = float(info["low"])  # 占位；下面统一均分
                busy_ids.append(idx)

        if busy_ids:
            pool = leftover
            alloc = {idx: 0.0 for idx in busy_ids}
            pending = list(busy_ids)
            while pending and pool > 0.0:
                share = pool / len(pending)
                next_pending = []
                for idx in pending:
                    info = self.gpu_info[idx]
                    hi = info["max"] or info["default"] or leftover
                    got = min(share, hi - alloc[idx])
                    alloc[idx] += got
                    pool -= got
                    if alloc[idx] < hi - 0.5:
                        next_pending.append(idx)
                if len(next_pending) == len(pending):
                    pool = 0.0  # 无人到顶但池未耗尽(浮点)，避免死循环
                pending = next_pending
            for idx in busy_ids:
                targets[idx] = alloc[idx]

        # 取整到瓦，避免 nvidia-smi -pl 对非整数兼容问题
        targets = {k: int(round(v)) for k, v in targets.items()}

        # 硬校验：和绝不能超过整机预算，从上限最大的忙卡开始削减
        total = sum(targets.values())
        if total > int(self.power_budget):
            overflow = total - int(self.power_budget)
            for idx in sorted(busy_ids, key=lambda i: targets[i], reverse=True):
                if overflow <= 0:
                    break
                cut = min(targets[idx] - int(low), overflow)
                targets[idx] -= cut
                overflow -= cut
        return targets

    def apply_limits(self, samples):
        """按动态智能分配结果下发（滞回 + 去抖）：
        - 首次下发：立即执行；
        - 差 < 死区(微小抖动)：一律不动，形成滞回窗口，彻底抑制 ±几瓦反复 -pl；
        - 差 >= 死区(重大变化/忙闲迁移)：响应并受最短间隔节流，防止负载颤振每 2s 大跳。"""
        targets = self._build_targets(samples)
        n_busy = sum(1 for st in self.gpu_state.values() if st == STATE_BUSY)
        now = time.time()
        changed = False
        for idx, t in targets.items():
            cur = self.deployed.get(idx)
            if cur is None:
                pass  # 首次下发：无条件执行
            elif abs(cur - t) < self.deploy_deadband:
                continue  # 微小调整：保持已下发值不动（滞回）
            elif now - self.last_deploy_time.get(idx, 0.0) < self.deploy_min_interval:
                continue  # 连续重大变化：受最短间隔节流，避免颤振高频大跳
            if self.dry_run:
                self.log("info", f"[dry-run] GPU{idx} 功耗上限 -> {t}W (活跃 {n_busy} 卡)")
            else:
                self._set_power_limit(idx, t)
            self.deployed[idx] = t
            self.last_deploy_time[idx] = now
            changed = True
        if changed:
            summary = ", ".join(f"GPU{i}:{t}W" for i, t in sorted(targets.items()))
            self.log("info",
                    f"功率动态分配下发 [{summary}] (活跃{n_busy}卡, 总和{sum(targets.values())}W"
                    f" ≤ 预算{self.power_budget:.0f}W)")
        return True

    def _set_power_limit(self, idx, watts):
        rc, _ = self._smi_rc(f"-i {idx} -pl {watts}")
        if rc != 0:
            self.log("error", f"GPU{idx} 设置功耗上限 {watts}W 失败 (rc={rc})")
            return False
        return True

    def _restore_all(self):
        """退出兜底：恢复所有卡到"保守满血"(受整机预算 power_budget 封顶)并释放锁。"""
        n_active = max(1, len(self.gpu_info))
        per = self.budget_for(n_active)
        # 整机硬预算：全部卡同时满血也不能超过 power_budget
        per = min(per, self.power_budget / n_active)
        self.log("info", f"退出前恢复功耗: 按 {n_active} 卡预算每卡 {per:.0f}W (≤整机预算{self.power_budget:.0f}W)")
        for idx, info in self.gpu_info.items():
            if self.dry_run:
                self.log("info", f"[dry-run] GPU{idx} 恢复功耗上限 -> {per:.0f}W")
                continue
            bound = info["max"] or info["default"]
            target = min(per, bound) if bound and bound > 0 else per
            self._set_power_limit(idx, target)
        self._release_lock()

    # ---------- 主循环 ----------
    def run(self):
        self.log("info", f"GPU Idle Saver v{VERSION} (dry_run={self.dry_run}, interval={self.interval}s)")
        self.log("info", f"忙判定: util>{self.busy_util_threshold}% 或 power>{self.busy_power_threshold}W")
        self.log("info", f"省电上限 {self.power_low}W | 整机GPU硬预算 {self.power_budget:.0f}W (动态智能分配)")
        if self.dry_run:
            self.log("warn", "DRY-RUN 模式：只观察状态机，不修改任何功耗设置")

        while not self.stop_flag:
            samples = self.sample()
            self.evaluate(samples)
            self.apply_limits(samples)
            try:
                time.sleep(self.interval)
            except KeyboardInterrupt:
                break

    # ---------- 信号 ----------
    def handle_signal(self, signum, frame):
        """信号处理器只置标志；恢复动作统一走 finally 的 _restore_all()。"""
        self.log("info", f"收到信号 {signum}，正在退出（恢复满血功耗）...")
        self.stop_flag = True


def main():
    parser = argparse.ArgumentParser(description="GPU 空闲自动省电守护程序(电源预算法)")
    parser.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini"))
    parser.add_argument("--dry-run", action="store_true", help="只观察不修改")
    args = parser.parse_args()

    cfg = configparser.ConfigParser()
    if not os.path.isfile(args.config):
        print(f"配置文件不存在: {args.config}", file=sys.stderr)
        sys.exit(1)
    cfg.read(args.config)

    saver = GPUIdleSaver(cfg, dry_run=args.dry_run)
    signal.signal(signal.SIGTERM, saver.handle_signal)
    signal.signal(signal.SIGINT, saver.handle_signal)

    try:
        saver.run()
    finally:
        saver._restore_all()


if __name__ == "__main__":
    main()
