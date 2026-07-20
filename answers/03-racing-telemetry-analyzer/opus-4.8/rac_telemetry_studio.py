#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==========================================================================================
 RAC Telemetry Studio  --  实时遥测接收 / 记录 / 多圈距离对齐对比分析工具 (单文件版)
==========================================================================================

四大技术支柱
------------
 1. 非阻塞 UDP 接收器 (select + socket, 独立子线程, queue.Queue 线程安全投递)
 2. 基于「行驶距离」的数据对齐与重采样 (numpy.interp, 统一距离网格, 时间差 delta-t)
 3. 实时记录与持久化 (distance 突变归零 -> 自动切圈存 CSV: lap_YYYYMMDD_HHMMSS.csv)
 4. Tkinter + Matplotlib 交互式对比 UI (Speed/Pedals/Delta 三通道 + blitting 十字光标)

内置虚拟 UDP 发送器 (20ms 一帧)，无需真实游戏即可完整演示 收 -> 录 -> 存 -> 比 全流程。

用法
----
    python rac_telemetry_studio.py                  # 启动 GUI (默认自动开接收器)
    python rac_telemetry_studio.py --autostart-mock # 启动 GUI 并直接开模拟数据源
    python rac_telemetry_studio.py --port 30000
    python rac_telemetry_studio.py --sender-only    # 仅当发送端(接到别的机器上跑的本工具)
    python rac_telemetry_studio.py --selftest       # 无头自检(不依赖 tkinter), CI 友好

依赖: 标准库 + numpy + matplotlib (+ tkinter, 随 Python 发行)
==========================================================================================
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import random
import select
import signal
import socket
import struct
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.stderr.write("[FATAL] 需要 numpy: pip install numpy\n")
    raise

# ==========================================================================================
# 0. 全局配置
# ==========================================================================================

APP_NAME = "RAC Telemetry Studio"
APP_VERSION = "1.0.0"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 30000
DEFAULT_SEND_HOST = "127.0.0.1"

RECV_BUFSIZE = 65535
SOCK_RCVBUF = 1 << 20            # 1MB 内核接收缓冲, 防丢包
SELECT_TIMEOUT = 0.20            # select 超时(秒) -> 决定线程退出的最大延迟
QUEUE_MAXSIZE = 200_000          # 跨线程队列容量(约 66 分钟 @50Hz)

UI_TICK_MS = 33                  # 主线程排空队列的节拍 (~30Hz)
PLOT_REFRESH_MS = 120            # 实时曲线最小重绘间隔
MAX_PLOT_POINTS = 4000           # 单条曲线绘制点数上限(抽稀), 保证交互流畅

LAP_RESET_DROP_M = 5.0           # distance 回退超过该值 -> 判定为重置/新一圈
MIN_LAP_FRAMES = 30              # 少于该帧数的片段视为噪声, 不落盘
MIN_LAP_DISTANCE_M = 50.0        # 少于该距离的片段视为噪声, 不落盘

DEFAULT_RESAMPLE_STEP_M = 1.0    # 距离重采样步长(米)
MOCK_INTERVAL_S = 0.020          # 模拟发送周期 20ms = 50Hz
MOCK_LAP_LENGTH_M = 1400.0       # 模拟赛道单圈长度

CSV_HEADER = ["timestamp", "distance", "speed", "throttle", "brake"]

# 二进制帧: little-endian  double(timestamp) + 4 x float32
PACKET_STRUCT = struct.Struct("<d4f")

COLOR_A = "#2E7FE8"   # Lap A - 蓝
COLOR_B = "#E23B3B"   # Lap B - 红
COLOR_LIVE = "#17A673"  # LIVE - 绿
COLOR_GAIN = "#2E7FE8"
COLOR_LOSS = "#E23B3B"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ==========================================================================================
# 1. 遥测数据帧 + 编解码 (JSON / Struct 双协议)
# ==========================================================================================

@dataclass(slots=True)
class Frame:
    """单帧遥测数据。"""
    timestamp: float
    distance: float
    speed: float      # km/h
    throttle: float   # 0..1
    brake: float      # 0..1

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            {
                "timestamp": round(self.timestamp, 4),
                "distance": round(self.distance, 3),
                "speed": round(self.speed, 3),
                "throttle": round(self.throttle, 4),
                "brake": round(self.brake, 4),
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def to_struct_bytes(self) -> bytes:
        return PACKET_STRUCT.pack(self.timestamp, self.distance, self.speed,
                                  self.throttle, self.brake)


# 字段别名, 兼容不同游戏/插件的命名习惯
_ALIASES: Dict[str, Tuple[str, ...]] = {
    "timestamp": ("timestamp", "time", "t", "ts", "sessiontime"),
    "distance":  ("distance", "dist", "d", "lapdist", "lapdistance", "odo"),
    "speed":     ("speed", "spd", "v", "kmh", "velocity"),
    "throttle":  ("throttle", "thr", "gas", "accel"),
    "brake":     ("brake", "brk", "brakes"),
}


def _pick(d: Dict[str, Any], key: str) -> Optional[float]:
    for alias in _ALIASES[key]:
        if alias in d:
            try:
                return float(d[alias])
            except (TypeError, ValueError):
                return None
    return None


def decode_packet(payload: bytes) -> Optional[Frame]:
    """把 UDP 载荷解析为 Frame。无法识别时返回 None（绝不抛异常到接收循环）。"""
    if not payload:
        return None
    try:
        head = payload[:1]
        if head in (b"{", b"["):
            obj = json.loads(payload.decode("utf-8", errors="ignore"))
            if isinstance(obj, list):
                obj = obj[0] if obj and isinstance(obj[0], dict) else {}
            if not isinstance(obj, dict):
                return None
            # 支持小写化键
            low = {str(k).lower(): v for k, v in obj.items()}
            dist = _pick(low, "distance")
            spd = _pick(low, "speed")
            if dist is None or spd is None:
                return None
            if "speed_ms" in low:          # m/s -> km/h
                try:
                    spd = float(low["speed_ms"]) * 3.6
                except (TypeError, ValueError):
                    pass
            ts = _pick(low, "timestamp")
            return Frame(
                timestamp=time.time() if ts is None else ts,
                distance=dist,
                speed=spd,
                throttle=_clamp01(_pick(low, "throttle") or 0.0),
                brake=_clamp01(_pick(low, "brake") or 0.0),
            )
        if len(payload) == PACKET_STRUCT.size:
            ts, dist, spd, thr, brk = PACKET_STRUCT.unpack(payload)
            return Frame(ts, dist, spd, _clamp01(thr), _clamp01(brk))
    except Exception:
        return None
    return None


def _clamp01(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else float(x))


# ==========================================================================================
# 2. 非阻塞 UDP 接收线程
# ==========================================================================================

class ReceiverStats:
    """接收器运行统计（简单计数，GIL 下的 int 自增足够，读取端只做展示）。"""

    __slots__ = ("packets", "frames", "bad", "dropped", "bytes", "last_ts", "started_at")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.packets = 0
        self.frames = 0
        self.bad = 0
        self.dropped = 0
        self.bytes = 0
        self.last_ts = 0.0
        self.started_at = time.time()


class UDPReceiver(threading.Thread):
    """
    非阻塞 UDP 监听线程。

    - socket.setblocking(False) + select 轮询, 不会阻塞在 recvfrom 上;
    - 用 Event 控制退出, 最坏等待 SELECT_TIMEOUT 秒即可 join;
    - 解析后的 Frame 通过 queue.Queue 投递给主线程, 队列满则丢最旧的帧(实时优先)。
    """

    def __init__(self, out_queue: "queue.Queue[Frame]",
                 host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        super().__init__(name="UDPReceiver", daemon=True)
        self.host = host
        self.port = port
        self.out_queue = out_queue
        self.stats = ReceiverStats()
        self._stop_event = threading.Event()
        self._sock: Optional[socket.socket] = None
        self.error: Optional[str] = None

    # ---- 生命周期 -------------------------------------------------------------------
    def bind(self) -> None:
        """同步绑定端口, 失败直接抛 OSError, 便于 UI 立即提示（端口占用等）。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_RCVBUF)
            except OSError:
                pass  # 某些平台不允许调整, 忽略
            sock.bind((self.host, self.port))
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self.port = sock.getsockname()[1]   # port=0 时回填真实端口
        self.stats.reset()

    def stop(self) -> None:
        self._stop_event.set()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def shutdown(self, timeout: float = 2.0) -> None:
        """优雅退出: 置位 -> join -> 关 socket 释放端口。"""
        self.stop()
        if self.is_alive():
            self.join(timeout=timeout)
        self.close()

    # ---- 主循环 ---------------------------------------------------------------------
    def run(self) -> None:
        if self._sock is None:
            try:
                self.bind()
            except OSError as exc:
                self.error = f"绑定 {self.host}:{self.port} 失败: {exc}"
                log(f"[ERR] {self.error}")
                return
        log(f"UDP 接收器已启动: {self.host}:{self.port}")
        sock = self._sock
        try:
            while not self._stop_event.is_set():
                try:
                    readable, _, _ = select.select([sock], [], [], SELECT_TIMEOUT)
                except (OSError, ValueError):
                    break                                  # socket 已被关闭
                if not readable:
                    continue
                # 一次性排空内核缓冲, 降低 select 调用开销
                for _ in range(512):
                    try:
                        payload, _addr = sock.recvfrom(RECV_BUFSIZE)
                    except BlockingIOError:
                        break
                    except (ConnectionResetError, OSError):
                        break
                    self.stats.packets += 1
                    self.stats.bytes += len(payload)
                    frame = decode_packet(payload)
                    if frame is None:
                        self.stats.bad += 1
                        continue
                    self.stats.frames += 1
                    self.stats.last_ts = time.time()
                    self._offer(frame)
        except Exception:                                   # 兜底: 线程绝不静默崩溃
            self.error = traceback.format_exc(limit=3)
            log(f"[ERR] 接收线程异常:\n{self.error}")
        finally:
            self.close()
            log("UDP 接收器已停止, 端口已释放")

    def _offer(self, frame: Frame) -> None:
        try:
            self.out_queue.put_nowait(frame)
        except queue.Full:
            try:                                            # 丢最旧, 保最新
                self.out_queue.get_nowait()
                self.out_queue.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass
            self.stats.dropped += 1


# ==========================================================================================
# 3. 内置虚拟遥测发送器 (Mock)
# ==========================================================================================

class MockTelemetrySender(threading.Thread):
    """
    模拟 BeamNG / RAC 类游戏的遥测输出: 20ms 一帧, 沿正弦叠加的"赛道速度剖面"行驶。
    每圈随机一个 pace 系数与相位噪声, 使不同圈之间存在真实可比的差异。
    """

    def __init__(self, host: str = DEFAULT_SEND_HOST, port: int = DEFAULT_PORT,
                 interval: float = MOCK_INTERVAL_S, use_struct: bool = False,
                 seed: Optional[int] = None) -> None:
        super().__init__(name="MockSender", daemon=True)
        self.addr = (host, port)
        self.interval = max(0.001, float(interval))
        self.use_struct = use_struct
        self._stop_event = threading.Event()
        self._rng = random.Random(seed)
        self.sent = 0
        self.lap_index = 0
        self.error: Optional[str] = None

    def stop(self) -> None:
        self._stop_event.set()

    def shutdown(self, timeout: float = 2.0) -> None:
        self.stop()
        if self.is_alive():
            self.join(timeout=timeout)

    # ---- 简易车辆/赛道模型 -----------------------------------------------------------
    @staticmethod
    def _target_speed(dist: float, pace: float, phase: float) -> float:
        u = 2.0 * math.pi * dist / MOCK_LAP_LENGTH_M
        v = (150.0
             + 55.0 * math.sin(u * 3.0 + phase)
             + 22.0 * math.sin(u * 7.0 + phase * 1.7)
             + 10.0 * math.sin(u * 13.0))
        return max(45.0, min(255.0, v * pace))

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dist = 0.0
        speed = 60.0
        pace = 1.0
        phase = 0.0
        next_tick = time.perf_counter()
        log(f"模拟数据源已启动 -> udp://{self.addr[0]}:{self.addr[1]} "
            f"({1.0 / self.interval:.0f} Hz, {'struct' if self.use_struct else 'json'})")
        try:
            self._new_lap_params()
            pace, phase = self._pace, self._phase
            while not self._stop_event.is_set():
                dt = self.interval
                target = self._target_speed(dist, pace, phase)
                # 一阶惯性逼近目标速度 + 轻微噪声, 模拟油门/刹车响应
                speed += (target - speed) * 0.10 + self._rng.gauss(0.0, 0.25)
                speed = max(5.0, speed)
                dist += speed / 3.6 * dt

                err = target - speed
                throttle = _clamp01(err / 18.0) if err > 0 else 0.0
                brake = _clamp01(-err / 14.0) if err < 0 else 0.0

                frame = Frame(time.time(), dist, speed, throttle, brake)
                payload = frame.to_struct_bytes() if self.use_struct else frame.to_json_bytes()
                try:
                    sock.sendto(payload, self.addr)
                    self.sent += 1
                except OSError:
                    pass  # 对端未监听时 ICMP 端口不可达, 忽略继续

                if dist >= MOCK_LAP_LENGTH_M:               # 冲线 -> 距离归零(触发切圈)
                    dist = 0.0
                    self.lap_index += 1
                    self._new_lap_params()
                    pace, phase = self._pace, self._phase

                next_tick += self.interval
                sleep_for = next_tick - time.perf_counter()
                if sleep_for > 0:
                    self._stop_event.wait(sleep_for)
                else:
                    next_tick = time.perf_counter()          # 落后了就重新对齐节拍
        except Exception:
            self.error = traceback.format_exc(limit=3)
            log(f"[ERR] 模拟发送线程异常:\n{self.error}")
        finally:
            sock.close()
            log("模拟数据源已停止")

    def _new_lap_params(self) -> None:
        self._pace = self._rng.uniform(0.94, 1.06)
        self._phase = self._rng.uniform(-0.35, 0.35)


# ==========================================================================================
# 4. 圈数据模型 + CSV 持久化
# ==========================================================================================

def _sanitize_lap(distance: np.ndarray, *channels: np.ndarray
                  ) -> Tuple[np.ndarray, List[np.ndarray]]:
    """
    清洗为「严格单调递增的距离序列」——numpy.interp 的前提条件。
    步骤: 去 NaN -> 稳定排序 -> 去重复距离(保留首次出现)。
    """
    dist = np.asarray(distance, dtype=float).ravel()
    cols = [np.asarray(c, dtype=float).ravel() for c in channels]
    n = min([dist.size] + [c.size for c in cols]) if cols else dist.size
    dist = dist[:n]
    cols = [c[:n] for c in cols]

    mask = np.isfinite(dist)
    for c in cols:
        mask &= np.isfinite(c)
    dist = dist[mask]
    cols = [c[mask] for c in cols]
    if dist.size == 0:
        return dist, cols

    order = np.argsort(dist, kind="stable")
    dist = dist[order]
    cols = [c[order] for c in cols]

    keep = np.ones(dist.size, dtype=bool)
    if dist.size > 1:
        keep[1:] = np.diff(dist) > 1e-9
    dist = dist[keep]
    cols = [c[keep] for c in cols]
    return dist, cols


@dataclass
class LapData:
    """一圈的完整通道数据（距离已清洗为严格单调递增）。"""
    name: str
    distance: np.ndarray
    speed: np.ndarray
    throttle: np.ndarray
    brake: np.ndarray
    t_rel: np.ndarray                 # 相对时间(秒), 用于计算 delta-t
    path: Optional[str] = None

    # ---- 构造 -----------------------------------------------------------------------
    @classmethod
    def from_arrays(cls, name: str, timestamp, distance, speed, throttle, brake,
                    path: Optional[str] = None) -> "LapData":
        ts = np.asarray(timestamp, dtype=float).ravel()
        dist, (spd, thr, brk, tt) = _sanitize_lap(
            distance, speed, throttle, brake, ts)
        if tt.size:
            tt = tt - tt[0]
            if not np.all(np.diff(tt) >= -1e-6):          # 时间戳异常则用序号兜底
                tt = np.arange(tt.size, dtype=float) * 0.02
        return cls(name=name, distance=dist, speed=spd, throttle=thr,
                   brake=brk, t_rel=tt, path=path)

    @classmethod
    def from_frames(cls, name: str, frames: Sequence[Frame],
                    path: Optional[str] = None) -> "LapData":
        return cls.from_arrays(
            name,
            [f.timestamp for f in frames],
            [f.distance for f in frames],
            [f.speed for f in frames],
            [f.throttle for f in frames],
            [f.brake for f in frames],
            path=path,
        )

    @classmethod
    def from_csv(cls, path: str, name: Optional[str] = None) -> "LapData":
        """读取 CSV（表头大小写/别名不敏感）。异常向上抛给 UI 提示。"""
        cols: Dict[str, List[float]] = {k: [] for k in CSV_HEADER}
        with open(path, "r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("CSV 文件为空")
            idx: Dict[str, int] = {}
            for i, raw in enumerate(header):
                key = raw.strip().lower()
                for canon, aliases in _ALIASES.items():
                    if key in aliases and canon not in idx:
                        idx[canon] = i
            if "distance" not in idx or "speed" not in idx:
                raise ValueError("CSV 缺少必需列: distance / speed")
            for row_no, row in enumerate(reader, start=2):
                if not row:
                    continue
                try:
                    for canon in CSV_HEADER:
                        i = idx.get(canon, -1)
                        val = float(row[i]) if 0 <= i < len(row) and row[i] != "" else math.nan
                        cols[canon].append(val)
                except (ValueError, IndexError):
                    continue                                # 跳过坏行, 不中断加载
        if not cols["distance"]:
            raise ValueError("CSV 中没有有效数据行")
        ts = cols["timestamp"]
        if not np.any(np.isfinite(ts)):
            ts = list(np.arange(len(cols["distance"]), dtype=float) * 0.02)
        thr = cols["throttle"] if np.any(np.isfinite(cols["throttle"])) else [0.0] * len(ts)
        brk = cols["brake"] if np.any(np.isfinite(cols["brake"])) else [0.0] * len(ts)
        lap = cls.from_arrays(name or os.path.splitext(os.path.basename(path))[0],
                              ts, cols["distance"], cols["speed"], thr, brk, path=path)
        if lap.n == 0:
            raise ValueError("CSV 数据清洗后为空（距离列可能全为 NaN）")
        return lap

    # ---- 派生指标 -------------------------------------------------------------------
    @property
    def n(self) -> int:
        return int(self.distance.size)

    @property
    def length(self) -> float:
        return float(self.distance[-1] - self.distance[0]) if self.n > 1 else 0.0

    @property
    def lap_time(self) -> float:
        return float(self.t_rel[-1] - self.t_rel[0]) if self.n > 1 else 0.0

    @property
    def top_speed(self) -> float:
        return float(np.nanmax(self.speed)) if self.n else 0.0

    @property
    def avg_speed(self) -> float:
        return float(np.nanmean(self.speed)) if self.n else 0.0

    def summary(self) -> str:
        return (f"{self.name} | {fmt_time(self.lap_time)} | {self.length:7.1f} m | "
                f"Top {self.top_speed:5.1f} | Avg {self.avg_speed:5.1f} km/h | {self.n} pts")


def fmt_time(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds <= 0:
        return "--:--.---"
    m, s = divmod(seconds, 60.0)
    return f"{int(m):d}:{s:06.3f}"


def save_frames_csv(frames: Sequence[Frame], directory: str,
                    prefix: str = "lap") -> str:
    """落盘为 lap_YYYYMMDD_HHMMSS.csv，返回完整路径。"""
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(directory, f"{prefix}_{stamp}.csv")
    n = 1
    while os.path.exists(path):                              # 同秒多圈防覆盖
        path = os.path.join(directory, f"{prefix}_{stamp}_{n}.csv")
        n += 1
    tmp = path + ".part"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        for f in frames:
            writer.writerow([f"{f.timestamp:.4f}", f"{f.distance:.3f}",
                             f"{f.speed:.3f}", f"{f.throttle:.4f}", f"{f.brake:.4f}"])
    os.replace(tmp, path)                                    # 原子落盘
    return path


# ==========================================================================================
# 5. 实时记录器（切圈检测 + 自动存盘）
# ==========================================================================================

class LapRecorder:
    """
    累积当前圈的帧；当 distance 明显回退（车辆重置 / 冲线归零）时自动切圈存盘。
    仅在主线程调用，无需加锁。
    """

    def __init__(self, out_dir: str) -> None:
        self.out_dir = out_dir
        self.frames: List[Frame] = []
        self.saved_laps: List[str] = []
        self.discarded = 0

    # ---- 写入 -----------------------------------------------------------------------
    def add(self, frame: Frame) -> Optional[Tuple[str, LapData]]:
        """返回 (csv路径, LapData) 表示刚刚完成并保存了一圈。"""
        result = None
        if self.frames and frame.distance < self.frames[-1].distance - LAP_RESET_DROP_M:
            result = self._finalize("lap-reset")
        self.frames.append(frame)
        return result

    def flush(self, reason: str = "manual") -> Optional[Tuple[str, LapData]]:
        return self._finalize(reason)

    def clear(self) -> None:
        self.frames.clear()

    # ---- 内部 -----------------------------------------------------------------------
    def _finalize(self, reason: str) -> Optional[Tuple[str, LapData]]:
        frames, self.frames = self.frames, []
        if len(frames) < MIN_LAP_FRAMES:
            self.discarded += 1
            return None
        span = frames[-1].distance - frames[0].distance
        if span < MIN_LAP_DISTANCE_M:
            self.discarded += 1
            return None
        try:
            path = save_frames_csv(frames, self.out_dir)
        except OSError as exc:
            log(f"[ERR] 保存 CSV 失败: {exc}")
            return None
        lap = LapData.from_frames(os.path.splitext(os.path.basename(path))[0],
                                  frames, path=path)
        self.saved_laps.append(path)
        log(f"已保存圈 ({reason}): {path}  [{lap.summary()}]")
        return path, lap

    # ---- 只读视图 -------------------------------------------------------------------
    @property
    def current_distance(self) -> float:
        return self.frames[-1].distance if self.frames else 0.0

    def live_lap(self, name: str = "LIVE") -> Optional[LapData]:
        if len(self.frames) < 2:
            return None
        return LapData.from_frames(name, self.frames)


# ==========================================================================================
# 6. 核心算法: 基于距离的对齐与重采样
# ==========================================================================================

@dataclass
class Comparison:
    """两圈在统一距离网格上的对齐结果。缺失区间以 NaN 填充（绘图自动断开）。"""
    grid: np.ndarray
    step: float
    a: Optional[Dict[str, np.ndarray]] = None
    b: Optional[Dict[str, np.ndarray]] = None
    d_speed: Optional[np.ndarray] = None     # Lap A - Lap B (km/h)
    d_time: Optional[np.ndarray] = None      # Lap A - Lap B (s), 负值=A 更快

    def index_at(self, x: float) -> Optional[int]:
        if self.grid.size == 0:
            return None
        i = int(np.clip(np.searchsorted(self.grid, x), 0, self.grid.size - 1))
        if i > 0 and abs(self.grid[i - 1] - x) <= abs(self.grid[i] - x):
            i -= 1
        return i


class DistanceAligner:
    """
    以「行驶距离」为唯一横坐标做重采样对齐。

    为什么不用时间轴: 快圈用时短、慢圈用时长, 同一时刻两车在赛道上的位置根本不同,
    时间轴对比出的差异毫无物理意义。以距离为轴, 每个 x 都对应赛道上同一个点,
    才能回答"哪个弯刹早了 / 哪里出弯给油慢了"。
    """

    CHANNELS = ("speed", "throttle", "brake", "t_rel")

    @staticmethod
    def build_grid(laps: Sequence[LapData], step: float) -> np.ndarray:
        laps = [lp for lp in laps if lp is not None and lp.n > 1]
        if not laps:
            return np.empty(0, dtype=float)
        step = max(1e-3, float(step))
        lo = min(float(lp.distance[0]) for lp in laps)
        hi = max(float(lp.distance[-1]) for lp in laps)
        if hi <= lo:
            return np.empty(0, dtype=float)
        n = int(math.floor((hi - lo) / step)) + 1
        n = max(2, min(n, 2_000_000))                        # 防御异常步长导致爆内存
        return lo + np.arange(n, dtype=float) * step

    @staticmethod
    def resample(lap: LapData, grid: np.ndarray) -> Dict[str, np.ndarray]:
        """
        核心: numpy.interp 线性插值。区间外填 NaN, 避免用端点值伪造出不存在的数据。
        """
        out: Dict[str, np.ndarray] = {}
        if lap is None or lap.n < 2 or grid.size == 0:
            empty = np.full(grid.size, np.nan)
            return {ch: empty.copy() for ch in DistanceAligner.CHANNELS}
        xp = lap.distance
        inside = (grid >= xp[0]) & (grid <= xp[-1])
        for ch in DistanceAligner.CHANNELS:
            fp = getattr(lap, ch)
            y = np.interp(grid, xp, fp)                      # 端点外为常数, 随即置 NaN
            y[~inside] = np.nan
            out[ch] = y
        # 圈内相对时间从 0 起算, 便于跨圈比较
        t = out["t_rel"]
        if np.any(np.isfinite(t)):
            out["t_rel"] = t - np.nanmin(t)
        return out

    @classmethod
    def compare(cls, lap_a: Optional[LapData], lap_b: Optional[LapData],
                step: float = DEFAULT_RESAMPLE_STEP_M) -> Comparison:
        grid = cls.build_grid([lp for lp in (lap_a, lap_b) if lp], step)
        cmp = Comparison(grid=grid, step=step)
        if grid.size == 0:
            return cmp
        if lap_a is not None and lap_a.n > 1:
            cmp.a = cls.resample(lap_a, grid)
        if lap_b is not None and lap_b.n > 1:
            cmp.b = cls.resample(lap_b, grid)
        if cmp.a is not None and cmp.b is not None:
            cmp.d_speed = cmp.a["speed"] - cmp.b["speed"]
            cmp.d_time = cmp.a["t_rel"] - cmp.b["t_rel"]
        return cmp


def decimate(x: np.ndarray, *ys: np.ndarray,
             max_points: int = MAX_PLOT_POINTS) -> Tuple[np.ndarray, ...]:
    """等间隔抽稀，保证绘图与十字光标交互始终流畅。"""
    n = x.size
    if n <= max_points:
        return (x,) + ys
    stepi = int(math.ceil(n / max_points))
    return (x[::stepi],) + tuple(y[::stepi] for y in ys)


# ==========================================================================================
# 7. GUI
# ==========================================================================================

def _load_gui_modules():
    """延迟导入 GUI 依赖，使 --selftest / --sender-only 在无显示环境也能运行。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import (
        FigureCanvasTkAgg, NavigationToolbar2Tk)
    matplotlib.rcParams["axes.unicode_minus"] = False
    matplotlib.rcParams["font.size"] = 9
    return tk, filedialog, messagebox, ttk, Figure, FigureCanvasTkAgg, NavigationToolbar2Tk


class TelemetryApp:
    """主窗口：工具栏 + 三通道图表 + 十字光标读数面板 + 状态栏。"""

    def __init__(self, args: argparse.Namespace) -> None:
        (self.tk, self.filedialog, self.messagebox, self.ttk,
         Figure, FigureCanvasTkAgg, NavigationToolbar2Tk) = _load_gui_modules()

        self.args = args
        self.queue: "queue.Queue[Frame]" = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self.recorder = LapRecorder(args.outdir)
        self.receiver: Optional[UDPReceiver] = None
        self.sender: Optional[MockTelemetrySender] = None

        self.lap_a: Optional[LapData] = None
        self.lap_b: Optional[LapData] = None
        self.comparison = Comparison(grid=np.empty(0), step=args.step)

        self._closing = False
        self._need_replot = True
        self._last_plot = 0.0
        self._bg = None
        self._last_frame: Optional[Frame] = None
        self._rate_marker = (time.time(), 0)
        self._hz = 0.0

        self._build_window(Figure, FigureCanvasTkAgg, NavigationToolbar2Tk)
        self._wire_events()

        if args.autostart_recv:
            self.start_receiver()
        if args.autostart_mock:
            self.root.after(400, self.start_sender)
        self.root.after(UI_TICK_MS, self._tick)

    # ---- 界面构建 -------------------------------------------------------------------
    def _build_window(self, Figure, FigureCanvasTkAgg, NavigationToolbar2Tk) -> None:
        tk, ttk = self.tk, self.ttk
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("1280x860")
        self.root.minsize(980, 640)

        # ---------- 工具栏 ----------
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(side="top", fill="x")

        ttk.Label(bar, text="UDP 端口").pack(side="left")
        self.var_port = tk.StringVar(value=str(self.args.port))
        ttk.Entry(bar, textvariable=self.var_port, width=7).pack(side="left", padx=(4, 10))

        self.btn_recv = ttk.Button(bar, text="▶ 启动接收", width=12,
                                   command=self.toggle_receiver)
        self.btn_recv.pack(side="left", padx=2)
        self.btn_mock = ttk.Button(bar, text="🎮 模拟数据源", width=13,
                                   command=self.toggle_sender)
        self.btn_mock.pack(side="left", padx=2)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        ttk.Button(bar, text="💾 保存当前圈", command=self.save_current_lap
                   ).pack(side="left", padx=2)
        ttk.Button(bar, text="📂 载入 Lap A", command=lambda: self.load_lap("A")
                   ).pack(side="left", padx=2)
        ttk.Button(bar, text="📂 载入 Lap B", command=lambda: self.load_lap("B")
                   ).pack(side="left", padx=2)
        ttk.Button(bar, text="↔ 交换", width=6, command=self.swap_laps
                   ).pack(side="left", padx=2)
        ttk.Button(bar, text="🗑 清空", width=7, command=self.clear_laps
                   ).pack(side="left", padx=2)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        ttk.Label(bar, text="重采样步长(m)").pack(side="left")
        self.var_step = tk.StringVar(value=f"{self.args.step:g}")
        cb = ttk.Combobox(bar, textvariable=self.var_step, width=5, state="readonly",
                          values=("0.5", "1", "2", "5", "10"))
        cb.pack(side="left", padx=(4, 10))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.rebuild_comparison())

        self.var_live = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="显示实时圈", variable=self.var_live,
                        command=self._mark_dirty).pack(side="left", padx=4)
        self.var_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动对比最近两圈", variable=self.var_auto
                        ).pack(side="left", padx=4)

        # ---------- 图表 ----------
        body = ttk.Frame(self.root)
        body.pack(side="top", fill="both", expand=True)

        self.fig = Figure(figsize=(11, 7), dpi=100, constrained_layout=True)
        gs = self.fig.add_gridspec(3, 1, height_ratios=[3, 2, 2])
        self.ax_speed = self.fig.add_subplot(gs[0])
        self.ax_pedal = self.fig.add_subplot(gs[1], sharex=self.ax_speed)
        self.ax_delta = self.fig.add_subplot(gs[2], sharex=self.ax_speed)
        self.axes = (self.ax_speed, self.ax_pedal, self.ax_delta)
        self.ax_dtime = self.ax_delta.twinx()

        self.canvas = FigureCanvasTkAgg(self.fig, master=body)
        self.canvas.get_tk_widget().pack(side="top", fill="both", expand=True)
        toolbar_frame = ttk.Frame(body)
        toolbar_frame.pack(side="top", fill="x")
        NavigationToolbar2Tk(self.canvas, toolbar_frame).update()

        # 十字光标(animated -> 不参与常规绘制, 走 blitting)
        self.cursor_lines = [
            ax.axvline(0.0, color="#666666", lw=0.9, ls="--",
                       animated=True, visible=False) for ax in self.axes
        ]

        # ---------- 读数面板 ----------
        self.readout = ttk.LabelFrame(self.root, text="光标读数 (Distance-aligned)",
                                      padding=(8, 4))
        self.readout.pack(side="top", fill="x", padx=8, pady=(0, 4))
        self.vars: Dict[str, "tk.StringVar"] = {}
        headers = ("通道", "Lap A", "Lap B", "Δ (A-B)")
        for c, text in enumerate(headers):
            ttk.Label(self.readout, text=text, width=16 if c else 12,
                      anchor="w", font=("TkDefaultFont", 9, "bold")
                      ).grid(row=0, column=c, sticky="w", padx=4)
        rows = ("Distance", "Speed km/h", "Throttle", "Brake", "Time s")
        for r, label in enumerate(rows, start=1):
            ttk.Label(self.readout, text=label, width=12, anchor="w"
                      ).grid(row=r, column=0, sticky="w", padx=4)
            for c in range(1, 4):
                var = tk.StringVar(value="—")
                self.vars[f"{label}|{c}"] = var
                ttk.Label(self.readout, textvariable=var, width=16, anchor="w"
                          ).grid(row=r, column=c, sticky="w", padx=4)

        self.var_summary = tk.StringVar(value="未载入对比数据")
        ttk.Label(self.readout, textvariable=self.var_summary, foreground="#444444"
                  ).grid(row=len(rows) + 1, column=0, columnspan=4, sticky="w",
                         padx=4, pady=(4, 0))

        # ---------- 状态栏 ----------
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.var_status, relief="sunken",
                  anchor="w", padding=(6, 3)).pack(side="bottom", fill="x")

        self._redraw()

    def _wire_events(self) -> None:
        self.canvas.mpl_connect("draw_event", self._on_draw)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("axes_leave_event", self._on_leave)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Control-s>", lambda _e: self.save_current_lap())
        self.root.bind("<Control-q>", lambda _e: self.on_close())
        self.root.bind("<F5>", lambda _e: self.rebuild_comparison())

    # ---- 线程控制 -------------------------------------------------------------------
    def _port(self) -> int:
        try:
            p = int(self.var_port.get())
            if not (0 <= p <= 65535):
                raise ValueError
            return p
        except ValueError:
            self.messagebox.showwarning(APP_NAME, "端口号无效, 已回退为默认 30000")
            self.var_port.set(str(DEFAULT_PORT))
            return DEFAULT_PORT

    def start_receiver(self) -> None:
        if self.receiver and self.receiver.is_alive():
            return
        rec = UDPReceiver(self.queue, self.args.host, self._port())
        try:
            rec.bind()
        except OSError as exc:
            self.messagebox.showerror(APP_NAME, f"无法监听 UDP 端口 {rec.port}\n\n{exc}\n\n"
                                                f"请检查端口是否被占用后重试。")
            return
        rec.start()
        self.receiver = rec
        self.btn_recv.config(text="■ 停止接收")
        self._set_status(f"正在监听 udp://{self.args.host}:{rec.port}")

    def stop_receiver(self) -> None:
        if self.receiver:
            self.receiver.shutdown()
            self.receiver = None
        self.btn_recv.config(text="▶ 启动接收")
        self._set_status("接收器已停止")

    def toggle_receiver(self) -> None:
        if self.receiver and self.receiver.is_alive():
            self.stop_receiver()
        else:
            self.start_receiver()

    def start_sender(self) -> None:
        if self.sender and self.sender.is_alive():
            return
        if not (self.receiver and self.receiver.is_alive()):
            self.start_receiver()
        self.sender = MockTelemetrySender(self.args.send_host, self._port(),
                                          self.args.interval, self.args.struct)
        self.sender.start()
        self.btn_mock.config(text="■ 停止模拟")
        self._set_status("模拟数据源运行中 (50Hz)")

    def stop_sender(self) -> None:
        if self.sender:
            self.sender.shutdown()
            self.sender = None
        self.btn_mock.config(text="🎮 模拟数据源")

    def toggle_sender(self) -> None:
        if self.sender and self.sender.is_alive():
            self.stop_sender()
        else:
            self.start_sender()

    # ---- 主循环节拍 -----------------------------------------------------------------
    def _tick(self) -> None:
        if self._closing:
            return
        try:
            self._drain_queue()
            now = time.time()
            if self._need_replot and (now - self._last_plot) * 1000.0 >= PLOT_REFRESH_MS:
                self._redraw()
            self._update_status()
        except Exception:
            log("[ERR] UI tick 异常:\n" + traceback.format_exc(limit=4))
        finally:
            if not self._closing:
                self.root.after(UI_TICK_MS, self._tick)

    def _drain_queue(self) -> None:
        got = 0
        while got < 4000:                                    # 单帧节拍处理上限, 防饿死 UI
            try:
                frame = self.queue.get_nowait()
            except queue.Empty:
                break
            got += 1
            self._last_frame = frame
            finished = self.recorder.add(frame)
            if finished is not None:
                self._on_lap_finished(*finished)
        if got:
            self._need_replot = True

    def _on_lap_finished(self, path: str, lap: LapData) -> None:
        if self.var_auto.get():
            self.lap_b = self.lap_a
            if self.lap_b is not None:
                self.lap_b.name = "Lap B"
            self.lap_a = lap
            self.lap_a.name = "Lap A"
            self.rebuild_comparison()
        self._set_status(f"已保存: {os.path.basename(path)}  ({lap.summary()})")

    # ---- 圈管理 ---------------------------------------------------------------------
    def save_current_lap(self) -> None:
        result = self.recorder.flush("manual")
        if result is None:
            self.messagebox.showinfo(
                APP_NAME, f"当前数据不足以保存\n(至少 {MIN_LAP_FRAMES} 帧且 "
                          f"{MIN_LAP_DISTANCE_M:.0f} 米)")
            return
        self._on_lap_finished(*result)

    def load_lap(self, slot: str) -> None:
        path = self.filedialog.askopenfilename(
            title=f"选择 Lap {slot} 的 CSV",
            initialdir=self.args.outdir if os.path.isdir(self.args.outdir) else ".",
            filetypes=[("Telemetry CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            lap = LapData.from_csv(path, name=f"Lap {slot}")
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self.messagebox.showerror(APP_NAME, f"加载失败:\n{path}\n\n{exc}")
            return
        if slot == "A":
            self.lap_a = lap
        else:
            self.lap_b = lap
        self.rebuild_comparison()
        self._set_status(f"Lap {slot} 已载入: {lap.summary()}")

    def swap_laps(self) -> None:
        self.lap_a, self.lap_b = self.lap_b, self.lap_a
        for lap, nm in ((self.lap_a, "Lap A"), (self.lap_b, "Lap B")):
            if lap is not None:
                lap.name = nm
        self.rebuild_comparison()

    def clear_laps(self) -> None:
        self.lap_a = self.lap_b = None
        self.comparison = Comparison(grid=np.empty(0), step=self.args.step)
        self.recorder.clear()
        self._clear_readout()
        self._mark_dirty()

    def rebuild_comparison(self) -> None:
        """重新执行距离对齐(重采样)。"""
        try:
            step = float(self.var_step.get())
        except ValueError:
            step = DEFAULT_RESAMPLE_STEP_M
        try:
            self.comparison = DistanceAligner.compare(self.lap_a, self.lap_b, step)
        except Exception:
            log("[ERR] 对齐失败:\n" + traceback.format_exc(limit=3))
            self.comparison = Comparison(grid=np.empty(0), step=step)
        self._update_summary()
        self._mark_dirty()

    def _update_summary(self) -> None:
        parts = []
        for lap in (self.lap_a, self.lap_b):
            if lap is not None:
                parts.append(lap.summary())
        cmp = self.comparison
        if cmp.d_time is not None and np.any(np.isfinite(cmp.d_time)):
            final = float(cmp.d_time[np.isfinite(cmp.d_time)][-1])
            faster = "A 快" if final < 0 else "B 快"
            parts.append(f"终点累计时间差 Δt = {final:+.3f} s ({faster})  |  "
                         f"重采样步长 {cmp.step:g} m, 网格点 {cmp.grid.size}")
        self.var_summary.set("\n".join(parts) if parts else "未载入对比数据")

    # ---- 绘图 -----------------------------------------------------------------------
    def _mark_dirty(self) -> None:
        self._need_replot = True

    def _redraw(self) -> None:
        self._last_plot = time.time()
        self._need_replot = False
        for ax in self.axes:
            ax.clear()
        self.ax_dtime.clear()
        self._bg = None

        cmp = self.comparison
        any_curve = False

        # --- Speed / Pedals: 使用重采样后的统一距离网格 ---
        for data, lap, color in ((cmp.a, self.lap_a, COLOR_A),
                                 (cmp.b, self.lap_b, COLOR_B)):
            if data is None or lap is None:
                continue
            any_curve = True
            x, spd, thr, brk = decimate(cmp.grid, data["speed"],
                                        data["throttle"], data["brake"])
            label = f"{lap.name} ({fmt_time(lap.lap_time)})"
            self.ax_speed.plot(x, spd, color=color, lw=1.4, label=label)
            self.ax_pedal.plot(x, thr * 100.0, color=color, lw=1.2,
                               label=f"{lap.name} throttle")
            self.ax_pedal.plot(x, brk * 100.0, color=color, lw=1.2, ls="--",
                               alpha=0.85, label=f"{lap.name} brake")

        # --- LIVE 圈(原始采样, 不参与对比) ---
        if self.var_live.get():
            live = self.recorder.live_lap()
            if live is not None and live.n > 2:
                any_curve = True
                x, spd, thr, brk = decimate(live.distance, live.speed,
                                            live.throttle, live.brake)
                self.ax_speed.plot(x, spd, color=COLOR_LIVE, lw=1.0, alpha=0.9,
                                   label=f"LIVE ({live.length:.0f} m)")
                self.ax_pedal.plot(x, thr * 100.0, color=COLOR_LIVE, lw=0.9, alpha=0.6)
                self.ax_pedal.plot(x, brk * 100.0, color=COLOR_LIVE, lw=0.9, ls="--",
                                   alpha=0.6)

        # --- Delta 通道 ---
        if cmp.d_speed is not None:
            x, ds = decimate(cmp.grid, cmp.d_speed)
            valid = np.isfinite(ds)
            self.ax_delta.axhline(0.0, color="#999999", lw=0.8)
            self.ax_delta.fill_between(x, 0.0, np.where(valid, ds, 0.0),
                                       where=valid & (ds >= 0), color=COLOR_GAIN,
                                       alpha=0.30, interpolate=True, linewidth=0)
            self.ax_delta.fill_between(x, 0.0, np.where(valid, ds, 0.0),
                                       where=valid & (ds < 0), color=COLOR_LOSS,
                                       alpha=0.30, interpolate=True, linewidth=0)
            self.ax_delta.plot(x, ds, color="#555555", lw=0.8, label="dSpeed (A-B)")
        if cmp.d_time is not None:
            x, dt = decimate(cmp.grid, cmp.d_time)
            self.ax_dtime.plot(x, dt, color="#8E44AD", lw=1.5, label="dTime (A-B)")
            self.ax_dtime.set_ylabel("dTime A-B [s]", color="#8E44AD")
            self.ax_dtime.tick_params(axis="y", labelcolor="#8E44AD")

        # --- 轴装饰(英文标签, 规避 matplotlib 缺失 CJK 字体的方框问题) ---
        self.ax_speed.set_ylabel("Speed [km/h]")
        self.ax_speed.set_title("Speed vs Distance", fontsize=10, loc="left")
        self.ax_pedal.set_ylabel("Pedals [%]")
        self.ax_pedal.set_ylim(-3, 103)
        self.ax_pedal.set_title("Throttle (solid) / Brake (dashed) vs Distance",
                                fontsize=10, loc="left")
        self.ax_delta.set_ylabel("dSpeed A-B [km/h]")
        self.ax_delta.set_xlabel("Distance [m]")
        self.ax_delta.set_title("Delta channel  (blue: A faster, red: A slower)",
                                fontsize=10, loc="left")
        for ax in self.axes:
            ax.grid(True, alpha=0.25, lw=0.6)
            handles, labels = ax.get_legend_handles_labels()
            if labels:
                ax.legend(loc="upper right", fontsize=8, ncol=2, framealpha=0.85)
        if not any_curve:
            self.ax_speed.text(0.5, 0.5,
                               "No data.  Start the mock source, or load two lap CSVs.",
                               ha="center", va="center", transform=self.ax_speed.transAxes,
                               color="#888888")

        # 十字光标重建(clear 会移除旧 artist)
        self.cursor_lines = [
            ax.axvline(0.0, color="#666666", lw=0.9, ls="--",
                       animated=True, visible=False) for ax in self.axes
        ]
        try:
            self.canvas.draw_idle()
        except Exception:
            log("[ERR] 绘图失败:\n" + traceback.format_exc(limit=3))

    # ---- 十字光标 (blitting) ---------------------------------------------------------
    def _on_draw(self, _event) -> None:
        try:
            self._bg = self.canvas.copy_from_bbox(self.fig.bbox)
        except Exception:
            self._bg = None

    def _on_motion(self, event) -> None:
        if event.inaxes not in self.axes or event.xdata is None:
            return
        x = float(event.xdata)
        if self._bg is None:
            self.canvas.draw()
            return
        try:
            self.canvas.restore_region(self._bg)
            for ax, line in zip(self.axes, self.cursor_lines):
                line.set_xdata([x, x])
                line.set_visible(True)
                ax.draw_artist(line)
            self.canvas.blit(self.fig.bbox)
        except Exception:
            self._bg = None
        self._update_readout(x)

    def _on_leave(self, _event) -> None:
        if self._bg is None:
            return
        try:
            for line in self.cursor_lines:
                line.set_visible(False)
            self.canvas.restore_region(self._bg)
            self.canvas.blit(self.fig.bbox)
        except Exception:
            self._bg = None

    def _update_readout(self, x: float) -> None:
        cmp = self.comparison
        idx = cmp.index_at(x)
        if idx is None:
            self._clear_readout(distance=x)
            return

        def cell(label: str, col: int, text: str) -> None:
            self.vars[f"{label}|{col}"].set(text)

        def val(src: Optional[Dict[str, np.ndarray]], key: str) -> float:
            if src is None:
                return math.nan
            v = src[key][idx]
            return float(v)

        def show(v: float, fmt: str = "{:.1f}") -> str:
            return "—" if not math.isfinite(v) else fmt.format(v)

        cell("Distance", 1, f"{cmp.grid[idx]:.1f} m")
        cell("Distance", 2, f"idx {idx}")
        cell("Distance", 3, f"step {cmp.step:g} m")

        pairs = (("Speed km/h", "speed", 1.0, "{:.1f}"),
                 ("Throttle", "throttle", 100.0, "{:.0f}%"),
                 ("Brake", "brake", 100.0, "{:.0f}%"),
                 ("Time s", "t_rel", 1.0, "{:.3f}"))
        for label, key, scale, fmt in pairs:
            a = val(cmp.a, key) * scale
            b = val(cmp.b, key) * scale
            cell(label, 1, show(a, fmt))
            cell(label, 2, show(b, fmt))
            d = a - b
            cell(label, 3, ("—" if not math.isfinite(d)
                            else ("+" if d >= 0 else "") + fmt.format(d)))

    def _clear_readout(self, distance: Optional[float] = None) -> None:
        for key, var in self.vars.items():
            var.set("—")
        if distance is not None and math.isfinite(distance):
            self.vars["Distance|1"].set(f"{distance:.1f} m")

    # ---- 状态栏 ---------------------------------------------------------------------
    def _set_status(self, text: str) -> None:
        self.var_status.set(text)

    def _update_status(self) -> None:
        rec = self.receiver
        now = time.time()
        if rec:
            t0, f0 = self._rate_marker
            if now - t0 >= 1.0:
                self._hz = (rec.stats.frames - f0) / (now - t0)
                self._rate_marker = (now, rec.stats.frames)
            state = f"监听 :{rec.port}" if rec.is_alive() else "接收器已停"
            net = (f"{state} | 包 {rec.stats.packets} | 帧 {rec.stats.frames} "
                   f"| 坏包 {rec.stats.bad} | 丢弃 {rec.stats.dropped} "
                   f"| {self._hz:5.1f} Hz | 队列 {self.queue.qsize()}")
        else:
            net = "接收器未启动"
        f = self._last_frame
        car = (f"车辆: {f.speed:6.1f} km/h  D={f.distance:7.1f} m  "
               f"T={f.throttle * 100:3.0f}%  B={f.brake * 100:3.0f}%") if f else "车辆: 无数据"
        laps = f"本会话已存 {len(self.recorder.saved_laps)} 圈 -> {self.args.outdir}"
        self.var_status.set(f"{net}    ||    {car}    ||    {laps}")

    # ---- 退出 -----------------------------------------------------------------------
    def on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        log("正在关闭: 停止子线程并释放端口 ...")
        try:
            if self.recorder.frames:
                saved = self.recorder.flush("shutdown")
                if saved:
                    log(f"退出前已保存未完成的圈: {saved[0]}")
        except Exception:
            log("[WARN] 退出保存失败:\n" + traceback.format_exc(limit=2))
        self.stop_sender()
        self.stop_receiver()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
        log("已安全退出")

    def run(self) -> None:
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.on_close()


# ==========================================================================================
# 8. 无头自检 (--selftest)
# ==========================================================================================

def selftest() -> int:
    """不依赖 tkinter / 显示环境, 验证网络、记录、CSV、对齐算法全链路。"""
    import tempfile
    ok = True

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' -> ' + extra) if extra else ''}")

    print("=" * 78)
    print("SELFTEST 1/4  UDP 收发链路 (非阻塞 socket + 线程 + 队列)")
    q: "queue.Queue[Frame]" = queue.Queue(maxsize=10000)
    rec = UDPReceiver(q, "127.0.0.1", 0)
    rec.bind()
    rec.start()
    snd = MockTelemetrySender("127.0.0.1", rec.port, 0.005, use_struct=False, seed=7)
    snd.start()
    time.sleep(1.2)
    snd2 = MockTelemetrySender("127.0.0.1", rec.port, 0.005, use_struct=True, seed=9)
    snd2.start()
    time.sleep(0.4)
    snd.shutdown()
    snd2.shutdown()
    time.sleep(0.2)
    rec.shutdown()
    check("接收到遥测帧", rec.stats.frames > 100, f"frames={rec.stats.frames}")
    check("无坏包(JSON+Struct 双协议均解析成功)", rec.stats.bad == 0,
          f"bad={rec.stats.bad}")
    check("端口已释放", rec._sock is None)
    check("接收线程已终止", not rec.is_alive())

    print("SELFTEST 2/4  记录器切圈 + CSV 持久化")
    frames: List[Frame] = []
    while True:
        try:
            frames.append(q.get_nowait())
        except queue.Empty:
            break
    with tempfile.TemporaryDirectory() as tmp:
        recorder = LapRecorder(tmp)
        saved = 0
        for fr in frames:
            if recorder.add(fr) is not None:
                saved += 1
        last = recorder.flush("selftest")
        total = saved + (1 if last else 0)
        check("至少落盘 1 圈 CSV", total >= 1, f"laps={total}")
        files = sorted(f for f in os.listdir(tmp) if f.endswith(".csv"))
        check("文件名符合 lap_YYYYMMDD_HHMMSS.csv",
              bool(files) and files[0].startswith("lap_") and len(files[0]) >= 22,
              files[0] if files else "-")
        lap = LapData.from_csv(os.path.join(tmp, files[0]))
        check("CSV 可回读且距离严格单调", lap.n > 10 and bool(np.all(np.diff(lap.distance) > 0)),
              f"n={lap.n}, len={lap.length:.1f}m")

    print("SELFTEST 3/4  距离对齐与重采样 (不同长度 / 不同采样率)")
    # Lap A: 800 点, 0..1000m;  Lap B: 137 点(不同采样率), 0..940m, 整体慢 5%
    da = np.linspace(0, 1000, 800)
    va = 120 + 40 * np.sin(da / 1000 * 6 * np.pi)
    ta = np.concatenate([[0.0], np.cumsum(np.diff(da) / (va[:-1] / 3.6))])
    db = np.linspace(0, 940, 137)
    vb = (120 + 40 * np.sin(db / 1000 * 6 * np.pi)) * 0.95
    tb = np.concatenate([[0.0], np.cumsum(np.diff(db) / (vb[:-1] / 3.6))])
    lap_a = LapData.from_arrays("Lap A", ta, da, va, np.zeros_like(da), np.zeros_like(da))
    lap_b = LapData.from_arrays("Lap B", tb, db, vb, np.zeros_like(db), np.zeros_like(db))
    cmp = DistanceAligner.compare(lap_a, lap_b, step=1.0)
    check("统一网格已建立", cmp.grid.size == 1001, f"grid={cmp.grid.size}")
    check("两圈重采样到同一形状",
          cmp.a["speed"].shape == cmp.b["speed"].shape == cmp.grid.shape)
    check("区间外正确置 NaN(B 只跑到 940m)",
          bool(np.all(np.isnan(cmp.b["speed"][950:]))) and
          bool(np.all(np.isfinite(cmp.b["speed"][:900]))))
    mid = cmp.index_at(500.0)
    err = abs(cmp.a["speed"][mid] - np.interp(500.0, da, va))
    check("插值精度 (500m 处误差 < 0.05 km/h)", err < 0.05, f"err={err:.4f}")
    dspd = cmp.d_speed[np.isfinite(cmp.d_speed)]
    check("Δ速度符合 5% 慢车设定", bool(np.all(dspd > 0)), f"mean={dspd.mean():.2f} km/h")
    dt = cmp.d_time[np.isfinite(cmp.d_time)]
    check("Δ时间随距离单调扩大(A 更快 -> 负值)", dt[-1] < dt[0] <= 0.0 + 1e-9,
          f"final dt={dt[-1]:+.3f}s")

    print("SELFTEST 4/4  边界与异常鲁棒性")
    check("空数据不崩溃", DistanceAligner.compare(None, None).grid.size == 0)
    check("单圈也可对齐", DistanceAligner.compare(lap_a, None, 2.0).a is not None)
    check("坏 UDP 包返回 None", decode_packet(b"\x00\x01garbage") is None
          and decode_packet(b"{not json") is None and decode_packet(b"") is None)
    noisy = LapData.from_arrays("noisy", [0, 1, 2, 3, 4],
                                [0, 10, 10, np.nan, 30],   # 重复 + NaN
                                [50, 60, 61, 70, 80], [0] * 5, [0] * 5)
    check("距离列去重/去 NaN 后严格单调",
          bool(np.all(np.diff(noisy.distance) > 0)), f"n={noisy.n}")

    print("=" * 78)
    print("SELFTEST RESULT:", "ALL PASS ✅" if ok else "FAILED ❌")
    return 0 if ok else 1


# ==========================================================================================
# 9. 入口
# ==========================================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=f"{APP_NAME} v{APP_VERSION} - 实时遥测接收/记录/多圈距离对齐分析",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--host", default=DEFAULT_HOST, help="UDP 监听地址")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="UDP 监听端口")
    p.add_argument("--send-host", default=DEFAULT_SEND_HOST, help="模拟数据源目标地址")
    p.add_argument("--interval", type=float, default=MOCK_INTERVAL_S,
                   help="模拟数据源发送周期(秒)")
    p.add_argument("--struct", action="store_true", help="模拟数据源使用二进制 struct 帧")
    p.add_argument("--outdir", default=os.path.join(os.getcwd(), "telemetry_logs"),
                   help="CSV 输出目录")
    p.add_argument("--step", type=float, default=DEFAULT_RESAMPLE_STEP_M,
                   help="距离重采样步长(米)")
    p.add_argument("--autostart-mock", action="store_true", help="启动时自动开模拟数据源")
    p.add_argument("--no-autostart-recv", dest="autostart_recv", action="store_false",
                   help="启动时不自动开接收器")
    p.add_argument("--sender-only", action="store_true", help="仅运行模拟发送端(无 GUI)")
    p.add_argument("--selftest", action="store_true", help="运行无头自检后退出")
    p.set_defaults(autostart_recv=True)
    return p


def run_sender_only(args: argparse.Namespace) -> int:
    sender = MockTelemetrySender(args.send_host, args.port, args.interval, args.struct)
    stop = threading.Event()

    def _sig(_s, _f):
        stop.set()

    for s in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(s, _sig)
        except (ValueError, OSError):
            pass
    sender.start()
    log("按 Ctrl+C 停止发送 ...")
    try:
        while not stop.is_set() and sender.is_alive():
            stop.wait(0.3)
    except KeyboardInterrupt:
        pass
    sender.shutdown()
    log(f"共发送 {sender.sent} 帧")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.selftest:
        return selftest()
    if args.sender_only:
        return run_sender_only(args)
    try:
        os.makedirs(args.outdir, exist_ok=True)
    except OSError as exc:
        log(f"[WARN] 无法创建输出目录 {args.outdir}: {exc}")
        args.outdir = os.getcwd()
    try:
        app = TelemetryApp(args)
    except ImportError as exc:
        sys.stderr.write(
            f"[FATAL] GUI 依赖缺失: {exc}\n"
            "  - tkinter: Debian/Ubuntu 执行 `sudo apt install python3-tk`\n"
            "  - matplotlib: `pip install matplotlib`\n"
            "  无图形环境可用 `--selftest` 或 `--sender-only`。\n")
        return 2
    except Exception:
        sys.stderr.write("[FATAL] 初始化失败:\n" + traceback.format_exc())
        return 3
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
