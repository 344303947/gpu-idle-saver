#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPU 功率固定分配守护程序 v1.0.0

用途：多卡推理机。按"当前活跃卡片数"固定分配每卡功耗上限：
      4活跃卡 -> 每卡 185W
      3活跃卡 -> 每卡 200W
      2活跃卡 -> 每卡 250W
      1活跃卡 -> 每卡 250W
      单卡授权上限绝不超过 power_cap(默认250W)；
      任意时刻所有卡 power.limit 之和 ≤ power_budget(默认750W)。

自愈纠偏：每 check_interval(默认60s) 比对"实际生效的 power.limit"与"目标档位值"，
      若被外部工具(nvidia-smi 手动设置/其他程序)改乱，自动改回目标值。

固定分配语义：不区分空闲/忙碌去抖，直接按当前活跃卡数查表分配。
      活跃判定：利用率 > util_threshold(%) 或 功耗 > power_threshold(W)。

运行：python3 gpu_power_allocator.py --config config.ini [--dry-run]
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

VERSION = "1.0.0"

SAMPLE_QUERY = "index,pstate,utilization.gpu,power.draw,power.limit,clocks.sm,memory.used"


class GPUAllocator:
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run

        # 分配模式：count=按在线卡数量直接固定功率(简单模式,默认)；active=按活跃卡数量分配
        self.allocation_mode = cfg.get("general", "allocation_mode", fallback="count").strip().lower()
        if self.allocation_mode not in ("count", "active"):
            self.log("warn", f"未知 allocation_mode={self.allocation_mode}，回退为 count")
            self.allocation_mode = "count"
        # 检查/分配周期(秒)：固定的自愈与分配评估节奏
        self.check_interval = max(1.0, cfg.getfloat("general", "check_interval", fallback=60.0))
        # 活跃判定阈值
        self.util_threshold = cfg.getfloat("general", "util_threshold", fallback=5.0)
        self.power_threshold = cfg.getfloat("general", "power_threshold", fallback=80.0)

        # 活跃卡数 -> 每卡功耗上限(W) 档位表
        self.power_profile = self._parse_profile(
            cfg.get("power", "power_profile", fallback="4:185, 3:200, 2:250, 1:250"))
        # 空闲卡固定功耗上限(W)：无负载的卡统一保持此档省电
        self.idle_power = max(0.0, cfg.getfloat("power", "power_idle", fallback=100.0))
        # 每卡授权上限(W)：任何单卡 power.limit 不得超过(安全风控)
        self.power_cap = cfg.getfloat("power", "power_cap", fallback=250.0)
        # 整机 GPU 硬预算(W)：任意时刻所有卡 power.limit 之和 ≤ 此值
        self.power_budget = cfg.getfloat("power", "power_budget", fallback=750.0)
        # 自愈死区(W)：实际值与目标值差 < 死区视为正常，不反复下发
        self.deadband = cfg.getfloat("power", "deadband", fallback=2.0)

        self.nvidia_smi = self._find_binary("nvidia-smi")

        self.gpu_info = {}     # index -> {min,max,default,current}
        self.stop_flag = False

        self._lock_fd = self._acquire_lock()
        self._query_power_ranges()
        self._validate_config()

    # ---------- 单实例锁 ----------
    def _acquire_lock(self):
        lock_path = "/var/run/gpu-power-allocator.lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            lock_path = os.path.join(tempfile.gettempdir(), "gpu-power-allocator.lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.log("error", "已有实例在运行(gpu-power-allocator)，拒绝启动")
            os._exit(1)
        self.log("info", f"单实例锁已获取: {lock_path}")
        return fd

    def _release_lock(self):
        if getattr(self, "_lock_fd", None):
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass

    # ---------- 配置解析 ----------
    def _parse_profile(self, s):
        """解析 '4:185, 3:200, 2:250, 1:250' -> {4:185, 3:200, 2:250, 1:250}"""
        profile = {}
        for part in s.replace("，", ",").split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            k, v = part.split(":")
            if k.strip().isdigit() and v.strip().replace(".", "", 1).isdigit():
                profile[int(k.strip())] = float(v.strip())
        if not profile:
            profile = {1: 250.0, 2: 250.0, 3: 200.0, 4: 185.0}
        return profile

    def watts_for(self, n_active):
        """按活跃卡数查固定档位表。
        n=1..2 -> 250W, n=3 -> 200W, n=4 -> 185W；
        超过档位表最大项则取最保守档(多卡满载时压最低)。"""
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

    def _query_power_ranges(self):
        out = self._smi("--query-gpu=index,power.min_limit,power.max_limit,power.limit,power.default_limit --format=csv,noheader,nounits")
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
            # 单卡物理上限：max_limit > default > 当前
            master = max_l if max_l and max_l > 0 else (def_l if def_l and def_l > 0 else (cur_l if cur_l > 0 else self.watts_for(1)))
            # 安全风控：授权上限再叠加 power_cap
            if self.power_cap and self.power_cap > 0 and master > self.power_cap:
                master = self.power_cap
            self.gpu_info[idx] = {
                "min": min_l or 0,
                "max": master,
                "default": def_l or 0,
                "current": cur_l or 0,
            }
        self.log("info", f"探测到 GPU: {json.dumps({k: {'max': v['max']} for k, v in self.gpu_info.items()})}")

    def _validate_config(self):
        """配置自检：档位表内任何档位不允许超过 power_cap，避免下发被 clamp 后档位失真。"""
        for n, w in self.power_profile.items():
            if self.power_cap and self.power_cap > 0 and w > self.power_cap:
                self.log("warn", f"档位 {n}卡={w:.0f}W 超过 power_cap={self.power_cap:.0f}W，将按 {self.power_cap:.0f}W 下发")
        n = len(self.gpu_info)
        if n > 0:
            per = self.watts_for(n)
            if n * per > self.power_budget:
                self.log("warn",
                        f"整机预算不足: {n}卡×{per:.0f}W={n*per:.0f}W > 预算{self.power_budget:.0f}W，"
                        f"繁忙分配会被预算硬校验压限")

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
        out = self._smi("--query-gpu=%s --format=csv,noheader,nounits" % SAMPLE_QUERY)
        result = {}
        for line in out:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                continue
            try:
                idx = int(parts[0])
            except ValueError:
                continue
            na = (parts[2].upper() == "N/A" or parts[3].upper() == "N/A" or parts[5].upper() == "N/A")
            result[idx] = {
                "pstate": parts[1],
                "util": self._to_float(parts[2]),
                "power": self._to_float(parts[3]),
                "limit": self._to_float(parts[4]),
                "sm": parts[5],
                "mem": parts[6],
                "na": na,
            }
        return result

    def card_active(self, s):
        """单卡活跃判定。采样缺失或 N/A → 一律视为活跃(保守)。"""
        if s is None or s.get("na"):
            return True
        if s["util"] > self.util_threshold:
            return True
        if s["power"] > self.power_threshold:
            return True
        return False

    # ---------- 目标档位计算 ----------
    def build_targets(self, samples):
        """固定功率分配，两种模式（配置 allocation_mode 切换）：
        - count(简单模式,默认)：不判断活跃度，按"在线卡总数"查表，所有卡统一同档。
          4卡->185W, 3卡->200W, 2卡->250W, 1卡->250W；
        - active(活跃模式)：活跃卡按"活跃卡数"查表档位，空闲卡固定 idle_power 省电档。
        - 单卡 clamp 到授权上限 max = min(硬件max, power_cap)，且不低于 min；
        - 硬校验：所有卡之和 ≤ power_budget，从档位最高的卡削减。"""
        n = len(self.gpu_info)
        if n == 0:
            return {}, 0

        if self.allocation_mode == "active":
            n_active = sum(1 for idx in self.gpu_info if self.card_active(samples.get(idx)))
        else:  # count 简单模式：按在线卡总数
            n_active = n
        per = self.watts_for(n_active)

        targets = {}
        for idx, info in self.gpu_info.items():
            if self.allocation_mode == "active" and not self.card_active(samples.get(idx)):
                t = float(self.idle_power)
            else:
                t = float(per)
            hi = info["max"]
            if hi and hi > 0:
                t = min(t, float(hi))
            t = max(t, float(info["min"]))
            targets[idx] = int(round(t))

        # 整机硬预算：总和超过预算时从档位最高的卡开始削减
        total = sum(targets.values())
        if total > int(self.power_budget):
            overflow = total - int(self.power_budget)
            if self.allocation_mode == "active":
                cut_ids = [idx for idx in self.gpu_info if self.card_active(samples.get(idx))]
            else:
                cut_ids = list(self.gpu_info.keys())
            if not cut_ids:
                cut_ids = list(self.gpu_info.keys())
            for idx in sorted(cut_ids, key=lambda i: targets[i], reverse=True):
                if overflow <= 0:
                    break
                floor = int(self.gpu_info[idx].get("min") or 0)
                cut = min(targets[idx] - floor, overflow)
                if cut <= 0:
                    continue
                targets[idx] -= cut
                overflow -= cut
        return targets, n_active

    # ---------- 自愈纠偏 ----------
    def check_and_fix(self, samples):
        """比对实际 power.limit 与目标档位，异常(被外部改动)立即改回。"""
        if not self.gpu_info:
            self.log("warn", "无 GPU 可管理")
            return
        targets, n_active = self.build_targets(samples)
        fixed = []
        for idx, t in targets.items():
            s = samples.get(idx)
            actual = s["limit"] if s else 0.0
            if abs(actual - t) < self.deadband:
                continue  # 实际值在目标 ±死区内：正常
            if self.dry_run:
                self.log("info", f"[dry-run] GPU{idx} 功耗上限 {actual:.0f}W -> 目标 {t}W (活跃{n_active}卡)")
                continue
            if self._set_power_limit(idx, t):
                fixed.append(f"GPU{idx}:{t}W")
        if fixed:
            self.log("info", f"自愈纠偏下发 [{', '.join(fixed)}] (活跃{n_active}卡, 目标总和{sum(targets.values())}W ≤ 预算{self.power_budget:.0f}W)")

    def _set_power_limit(self, idx, watts):
        rc, _ = self._smi_rc(f"-i {idx} -pl {watts}")
        if rc != 0:
            self.log("error", f"GPU{idx} 设置功耗上限 {watts}W 失败 (rc={rc})")
            return False
        return True

    def _restore_defaults(self):
        """退出兜底：恢复所有卡到硬件默认功耗上限并释放锁。"""
        if not self.gpu_info:
            self._release_lock()
            return
        self.log("info", "退出前恢复所有卡到硬件默认功耗上限")
        for idx, info in self.gpu_info.items():
            d = info["default"]
            if d and d > 0:
                if not self.dry_run:
                    self._set_power_limit(idx, int(round(d)))
        self._release_lock()

    # ---------- 主循环 ----------
    def run(self):
        self.log("info", f"GPU Power Allocator v{VERSION} (mode={self.allocation_mode}, dry_run={self.dry_run}, check_interval={self.check_interval}s)")
        if self.allocation_mode == "active":
            self.log("info", f"活跃判定: util>{self.util_threshold}% 或 power>{self.power_threshold}W")
        self.log("info", f"固定分配档位表: {json.dumps({k: f'{v:.0f}W' for k, v in sorted(self.power_profile.items())})}")
        self.log("info", f"单卡上限 {self.power_cap:.0f}W | 整机预算 {self.power_budget:.0f}W | 自愈死区 {self.deadband}W")
        if self.dry_run:
            self.log("warn", "DRY-RUN 模式：只观察计算，不修改任何功耗设置")

        while not self.stop_flag:
            samples = self.sample()
            targets, n_active = self.build_targets(samples)
            self.log("info",
                     f"当前活跃 {n_active} 卡 | 目标分配: " +
                     ", ".join(f"GPU{i}:{t}W" for i, t in sorted(targets.items())))
            self.check_and_fix(samples)
            try:
                time.sleep(self.check_interval)
            except KeyboardInterrupt:
                break

    # ---------- 信号 ----------
    def handle_signal(self, signum, frame):
        self.log("info", f"收到信号 {signum}，正在退出（恢复默认功耗）...")
        self.stop_flag = True


def main():
    parser = argparse.ArgumentParser(description="GPU 功率固定分配守护程序")
    parser.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini"))
    parser.add_argument("--dry-run", action="store_true", help="只观察不修改")
    args = parser.parse_args()

    cfg = configparser.ConfigParser()
    if not os.path.isfile(args.config):
        print(f"配置文件不存在: {args.config}", file=sys.stderr)
        sys.exit(1)
    cfg.read(args.config)

    alloc = GPUAllocator(cfg, dry_run=args.dry_run)
    signal.signal(signal.SIGTERM, alloc.handle_signal)
    signal.signal(signal.SIGINT, alloc.handle_signal)

    try:
        alloc.run()
    finally:
        alloc._restore_defaults()


if __name__ == "__main__":
    main()
