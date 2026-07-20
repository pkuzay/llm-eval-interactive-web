# -*- coding: utf-8 -*-
"""
RAC 实时遥测数据接收、记录与多圈对比分析工具
================================================
单文件脚本 | 依赖: 标准库 + numpy + matplotlib (tkinter 后端)

功能:
  1. 非阻塞 UDP 接收器 (独立子线程 + 线程安全队列, 默认端口 30000)
  2. 基于距离(Distance)的多圈数据对齐与重采样 (numpy.interp 线性插值)
  3. 实时记录与 CSV 持久化 (检测 distance 突变归零自动存盘)
  4. 交互式对比 UI: Speed-vs-Distance / Pedals-vs-Distance 双图 + 十字线
  5. 内置虚拟 UDP 遥测发送器 (20ms 周期正弦模拟数据)

数据帧格式 (JSON over UDP):
  {"timestamp": float, "distance": float(m), "speed": float(km/h),
   "throttle": float(0-1), "brake": float(0-1)}

运行:  python telemetry_tool.py
"""

import csv
import json
import math
import os
import queue
import random
import socket
import struct
import sys
import threading
import time
from datetime import datetime

import numpy as np

import matplotlib

matplotlib.use("TkAgg")  # 显式指定 tkinter 后端, 保证跨平台开箱即用
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# matplotlib 中文显示 (按常见平台依次回退)
matplotlib.rcParams["font.sans-serif"] = [
    "Microsoft YaHei", "SimHei", "PingFang SC", "Noto Sans CJK SC",
    "WenQuanYi Micro Hei", "Arial Unicode MS", "DejaVu Sans",
]
matplotlib.rcParams["axes.unicode_minus"] = False

# ---------------------------------------------------------------- 常量

UDP_HOST = "0.0.0.0"
UDP_PORT = 30000
MOCK_TARGET = ("127.0.0.1", UDP_PORT)
MOCK_INTERVAL = 0.02          # 模拟发送周期 20ms
MOCK_LAP_LENGTH = 2400.0      # 模拟单圈长度 (米)
UI_POLL_MS = 50               # UI 从队列拉取数据的周期
RESAMPLE_STEP = 1.0           # 距离重采样步长 (米)
LOG_DIR = os.path.abspath("telemetry_logs")

# 遥测帧字段顺序 (struct 备选格式: 5 个 little-endian double)
STRUCT_FMT = "<5d"
STRUCT_SIZE = struct.calcsize(STRUCT_FMT)
FIELDS = ("timestamp", "distance", "speed", "throttle", "brake")


# =================================================================
# 支柱 1: 高并发 UDP 接收器 (独立线程 + 线程安全队列)
# =================================================================
class TelemetryReceiver(threading.Thread):
    """非阻塞 UDP 监听器。解析 JSON / struct 字节流, 推入 queue.Queue。"""

    def __init__(self, out_queue, host=UDP_HOST, port=UDP_PORT):
        super().__init__(daemon=True, name="TelemetryReceiver")
        self.out_queue = out_queue
        self.host = host
        self.port = port
        self._stop_event = threading.Event()
        self._sock = None
        self.packets_received = 0
        self.packets_dropped = 0
        self.last_error = None

    def run(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            # 超时实现"伪非阻塞": 保证 stop() 能及时响应, 且绝不永久阻塞
            self._sock.settimeout(0.2)
        except OSError as exc:
            self.last_error = f"端口 {self.port} 绑定失败: {exc}"
            return

        while not self._stop_event.is_set():
            try:
                data, _addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break  # socket 被关闭, 退出线程
            sample = self._parse_packet(data)
            if sample is None:
                self.packets_dropped += 1
                continue
            self.packets_received += 1
            try:
                self.out_queue.put_nowait(sample)
            except queue.Full:
                self.packets_dropped += 1
        self._close_socket()

    def _parse_packet(self, data):
        """兼容 JSON 文本帧与 struct('<5d') 二进制帧; 非法帧返回 None。"""
        sample = None
        # 优先尝试 JSON
        try:
            obj = json.loads(data.decode("utf-8"))
            sample = {k: float(obj[k]) for k in FIELDS}
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            # 回退: 固定长度二进制帧
            if len(data) == STRUCT_SIZE:
                try:
                    values = struct.unpack(STRUCT_FMT, data)
                    sample = dict(zip(FIELDS, values))
                except struct.error:
                    sample = None
        if sample is None:
            return None
        # 数据合理性校验 (丢弃明显损坏的帧)
        if not (math.isfinite(sample["distance"]) and math.isfinite(sample["speed"])):
            return None
        if sample["distance"] < 0 or sample["speed"] < 0 or sample["speed"] > 600:
            return None
        sample["throttle"] = min(1.0, max(0.0, sample["throttle"]))
        sample["brake"] = min(1.0, max(0.0, sample["brake"]))
        return sample

    def stop(self):
        self._stop_event.set()
        self._close_socket()

    def _close_socket(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# =================================================================
# 内置模拟数据源: 虚拟 UDP 遥测发送器
# =================================================================
class MockTelemetrySender(threading.Thread):
    """模拟一台车反复跑圈: 正弦速度 + 噪声, 油门/刹车与速度相位联动。
    每 20ms 向本机 30000 端口发送一帧 JSON。跑满一圈后 distance 归零,
    模拟真实游戏的"过线重置"。可通过 speed_factor 模拟快/慢圈。"""

    def __init__(self, target=MOCK_TARGET, interval=MOCK_INTERVAL,
                 lap_length=MOCK_LAP_LENGTH):
        super().__init__(daemon=True, name="MockTelemetrySender")
        self.target = target
        self.interval = interval
        self.lap_length = lap_length
        self._stop_event = threading.Event()
        self.speed_factor = 1.0  # 圈速扰动: 每圈略有快慢, 模拟真实差异

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        distance = 0.0
        lap_no = 1
        t0 = time.monotonic()
        while not self._stop_event.is_set():
            elapsed = time.monotonic() - t0
            # 速度曲线: 基础 120 + 大幅正弦起伏(弯道) + 圈速因子 + 噪声
            phase = distance / self.lap_length * 4 * math.pi
            base = 120.0 + 95.0 * math.sin(phase)
            speed = max(25.0, base * self.speed_factor
                        + random.uniform(-2.5, 2.5))
            # 油门/刹车与速度导数联动: 加速段给油, 减速段刹车
            accel_intent = math.cos(phase)
            throttle = min(1.0, max(0.0, 0.55 + 0.5 * accel_intent
                                    + random.uniform(-0.05, 0.05)))
            brake = min(1.0, max(0.0, -0.35 + -0.65 * accel_intent
                                 + random.uniform(-0.04, 0.04)))
            if throttle > 0.15 and brake > 0.15:
                brake = 0.0 if throttle > brake else brake

            frame = {
                "timestamp": time.time(),
                "distance": round(distance, 3),
                "speed": round(speed, 3),
                "throttle": round(throttle, 4),
                "brake": round(brake, 4),
            }
            try:
                sock.sendto(json.dumps(frame).encode("utf-8"), self.target)
            except OSError:
                break

            distance += speed / 3.6 * self.interval
            if distance >= self.lap_length:
                distance = 0.0
                lap_no += 1
                # 每圈 ±3% 的快慢浮动, 让两圈曲线产生真实差异
                self.speed_factor = 1.0 + random.uniform(-0.03, 0.03)

            # 精确休眠: 补偿处理耗时, 维持 20ms 节拍
            sleep_t = self.interval - (time.monotonic() - t0 - elapsed)
            if sleep_t > 0:
                self._stop_event.wait(sleep_t)
        sock.close()

    def stop(self):
        self._stop_event.set()


# =================================================================
# 支柱 2: 基于距离的对齐与重采样算法
# =================================================================
class LapData:
    """单圈数据容器: 内部按距离升序维护 numpy 数组。"""

    def __init__(self, name="Lap", samples=None):
        self.name = name
        self.distance = np.array([], dtype=float)
        self.speed = np.array([], dtype=float)
        self.throttle = np.array([], dtype=float)
        self.brake = np.array([], dtype=float)
        if samples:
            self.load_samples(samples)

    def load_samples(self, samples):
        """samples: dict 列表。自动剔除距离重复/倒退点并排序。"""
        rows = sorted(samples, key=lambda s: s["distance"])
        dist, spd, thr, brk = [], [], [], []
        last_d = -1.0
        for s in rows:
            d = float(s["distance"])
            if d <= last_d:          # 插值要求 X 严格递增
                continue
            last_d = d
            dist.append(d)
            spd.append(float(s["speed"]))
            thr.append(float(s["throttle"]))
            brk.append(float(s["brake"]))
        self.distance = np.asarray(dist)
        self.speed = np.asarray(spd)
        self.throttle = np.asarray(thr)
        self.brake = np.asarray(brk)

    @property
    def is_empty(self):
        return self.distance.size < 2

    @property
    def lap_length(self):
        return float(self.distance[-1]) if not self.is_empty else 0.0

    def resample_to(self, grid):
        """将本圈各通道线性插值到指定距离网格, 返回 dict。"""
        return {
            "speed": np.interp(grid, self.distance, self.speed),
            "throttle": np.interp(grid, self.distance, self.throttle),
            "brake": np.interp(grid, self.distance, self.brake),
        }


def build_common_grid(lap_a, lap_b, step=RESAMPLE_STEP):
    """构造两圈距离交集上的等间距公共网格(空间对齐的核心)。"""
    lo = max(lap_a.distance[0], lap_b.distance[0])
    hi = min(lap_a.distance[-1], lap_b.distance[-1])
    if hi <= lo:
        return None
    return np.arange(lo, hi, step)


def load_lap_from_csv(path, name=None):
    """从 CSV 读取单圈数据为 LapData。"""
    samples = []
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in FIELDS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV 缺少列: {', '.join(missing)}")
        for row in reader:
            try:
                samples.append({k: float(row[k]) for k in FIELDS})
            except (ValueError, TypeError):
                continue  # 跳过坏行
    if len(samples) < 2:
        raise ValueError("CSV 有效数据不足 2 行")
    lap = LapData(name=name or os.path.basename(path), samples=samples)
    if lap.is_empty:
        raise ValueError("CSV 距离字段无有效递增序列")
    return lap


# =================================================================
# 支柱 4: 交互式 UI (tkinter + matplotlib)
# =================================================================
class TelemetryApp:
    COLOR_A = "#1f77ff"   # Lap A 蓝
    COLOR_B = "#e33e3e"   # Lap B 红
    COLOR_LIVE = "#1fae54"

    def __init__(self, root):
        self.root = root
        root.title("RAC 实时遥测 · 多圈对比分析工具")
        root.geometry("1180x760")
        root.minsize(900, 600)

        self.data_queue = queue.Queue(maxsize=20000)
        self.receiver = None
        self.sender = None

        self.recording = False
        self.current_samples = []     # 正在记录的当前圈
        self.live_lap = LapData("Live")
        self.lap_a = None             # 对比圈 A
        self.lap_b = None             # 对比圈 B
        self._last_distance = None    # 用于检测 distance 突变归零
        self._status_var = tk.StringVar(value="就绪 — 点击「启动模拟源」+「启动接收」开始演示")
        self._rec_var = tk.StringVar(value="● 未记录")

        self._build_toolbar()
        self._build_charts()
        self._bind_events()

        os.makedirs(LOG_DIR, exist_ok=True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_queue()

    # ---------------- UI 构建 ----------------
    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(bar, text="数据源:").pack(side=tk.LEFT)
        ttk.Button(bar, text="启动模拟源", command=self.start_mock).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="停止模拟源", command=self.stop_mock).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="启动接收", command=self.start_receiver).pack(side=tk.LEFT, padx=(10, 2))
        ttk.Button(bar, text="停止接收", command=self.stop_receiver).pack(side=tk.LEFT, padx=2)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Button(bar, text="● 开始记录", command=self.toggle_recording).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="手动存圈", command=self.save_current_lap).pack(side=tk.LEFT, padx=2)
        ttk.Label(bar, textvariable=self._rec_var, foreground="#c0392b").pack(side=tk.LEFT, padx=6)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Button(bar, text="载入 Lap A…", command=lambda: self.load_csv_dialog("A")).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="载入 Lap B…", command=lambda: self.load_csv_dialog("B")).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="清空对比", command=self.clear_comparison).pack(side=tk.LEFT, padx=2)

        status = ttk.Label(self.root, textvariable=self._status_var,
                           anchor=tk.W, padding=(8, 2))
        status.pack(side=tk.BOTTOM, fill=tk.X)

    def _build_charts(self):
        self.fig = Figure(figsize=(9, 6), dpi=100, constrained_layout=True)
        self.ax_speed = self.fig.add_subplot(211)
        self.ax_pedal = self.fig.add_subplot(212, sharex=self.ax_speed)

        self.ax_speed.set_ylabel("速度 (km/h)")
        self.ax_speed.set_title("Speed vs Distance  (蓝 = Lap A, 红 = Lap B, 绿 = 实时圈)")
        self.ax_speed.grid(True, alpha=0.3)
        self.ax_pedal.set_xlabel("行驶距离 Distance (m)")
        self.ax_pedal.set_ylabel("踏板开度 (0~1)")
        self.ax_pedal.set_title("Pedals vs Distance  (实线=油门, 虚线=刹车)")
        self.ax_pedal.set_ylim(-0.05, 1.05)
        self.ax_pedal.grid(True, alpha=0.3)

        # 十字线 (初始隐藏)
        self.vline_speed = self.ax_speed.axvline(x=0, color="#555555",
                                                 lw=0.8, ls="--", visible=False)
        self.vline_pedal = self.ax_pedal.axvline(x=0, color="#555555",
                                                 lw=0.8, ls="--", visible=False)
        # 悬停数值标签
        self.hover_text = self.ax_speed.text(
            0.01, 0.02, "", transform=self.ax_speed.transAxes, fontsize=9,
            va="bottom", ha="left",
            bbox=dict(boxstyle="round,pad=0.35", fc="#fffbe6", ec="#999999", alpha=0.95))

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    def _bind_events(self):
        self.canvas.mpl_connect("motion_notify_event", self.on_mouse_move)
        self.canvas.mpl_connect("axes_leave_event", self.on_mouse_leave)

    # ---------------- 线程控制 ----------------
    def start_mock(self):
        if self.sender and self.sender.is_alive():
            self._set_status("模拟源已在运行")
            return
        self.sender = MockTelemetrySender()
        self.sender.start()
        self._set_status(f"模拟源已启动 → 每 {int(MOCK_INTERVAL*1000)}ms 发往 {MOCK_TARGET[0]}:{MOCK_TARGET[1]}")

    def stop_mock(self):
        if self.sender:
            self.sender.stop()
            self.sender = None
        self._set_status("模拟源已停止")

    def start_receiver(self):
        if self.receiver and self.receiver.is_alive():
            self._set_status("接收器已在运行")
            return
        self.receiver = TelemetryReceiver(self.data_queue)
        self.receiver.start()
        # 等待线程尝试绑定, 检查端口是否被占用
        self.root.after(300, self._check_receiver_started)

    def _check_receiver_started(self):
        if self.receiver and self.receiver.last_error:
            messagebox.showerror("接收器错误", self.receiver.last_error)
            self.receiver = None
            self._set_status("接收器启动失败")
        elif self.receiver:
            self._set_status(f"UDP 接收器监听中: {UDP_HOST}:{UDP_PORT}")

    def stop_receiver(self):
        if self.receiver:
            self.receiver.stop()
            self.receiver = None
        self._set_status("接收器已停止")

    # ---------------- 记录 / 存盘 (支柱 3) ----------------
    def toggle_recording(self):
        self.recording = not self.recording
        if self.recording:
            self.current_samples = []
            self._last_distance = None
            self._rec_var.set("● 记录中…")
            self._set_status("记录已开始 (检测到 distance 归零将自动存圈)")
        else:
            self._rec_var.set("● 未记录")
            self._set_status("记录已停止 (未保存的当前圈缓存保留, 可手动存圈)")

    def save_current_lap(self):
        """手动触发存圈。"""
        if len(self.current_samples) < 2:
            self._set_status("当前圈数据不足, 无法保存")
            return
        self._flush_lap_to_csv(self.current_samples)
        self.current_samples = []
        self._last_distance = None

    def _flush_lap_to_csv(self, samples):
        """CSV 持久化: lap_YYYYMMDD_HHMMSS.csv, 表头即 FIELDS。"""
        fname = "lap_{}.csv".format(datetime.now().strftime("%Y%m%d_%H%M%S"))
        path = os.path.join(LOG_DIR, fname)
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(samples)
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法写入 CSV:\n{exc}")
            return None
        self._set_status(f"已存圈: {fname}  ({len(samples)} 帧)")
        return path

    # ---------------- CSV 载入与对比 ----------------
    def load_csv_dialog(self, slot):
        path = filedialog.askopenfilename(
            title=f"选择 Lap {slot} 的 CSV 文件",
            initialdir=LOG_DIR,
            filetypes=[("CSV 遥测文件", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            lap = load_lap_from_csv(path, name=f"Lap {slot}")
        except (OSError, ValueError) as exc:
            messagebox.showerror("载入失败", f"{os.path.basename(path)}:\n{exc}")
            return
        if slot == "A":
            self.lap_a = lap
        else:
            self.lap_b = lap
        self.redraw_comparison()
        self._set_status(f"Lap {slot} 载入成功: {os.path.basename(path)}  "
                         f"({lap.distance.size} 点, {lap.lap_length:.0f} m)")

    def clear_comparison(self):
        self.lap_a = None
        self.lap_b = None
        self.redraw_comparison()
        self._set_status("对比圈已清空")

    # ---------------- 主循环: 从队列拉数据 ----------------
    def _poll_queue(self):
        """每 UI_POLL_MS 毫秒排空一次队列: 更新实时曲线与记录缓存。"""
        batch = []
        try:
            while True:
                batch.append(self.data_queue.get_nowait())
        except queue.Empty:
            pass

        if batch:
            for s in batch:
                d = s["distance"]
                # 车辆重置检测: distance 从较大值突变为 0 → 自动存圈开新圈
                if (self.recording and self._last_distance is not None
                        and d < self._last_distance - 50.0
                        and len(self.current_samples) > 10):
                    self._flush_lap_to_csv(self.current_samples)
                    self.current_samples = []
                self._last_distance = d
                if self.recording:
                    self.current_samples.append(s)
            self._update_live_plot(batch[-1])

        self.root.after(UI_POLL_MS, self._poll_queue)

    # ---------------- 绘图 ----------------
    def _update_live_plot(self, latest):
        """轻量刷新: 只画最近窗口内的实时轨迹, 避免大数据量卡顿。"""
        self.live_lap = LapData("Live", self.current_samples[-3000:]
                                if self.current_samples else None)
        self.redraw_comparison(live_only=True, latest=latest)

    def redraw_comparison(self, live_only=False, latest=None):
        """核心渲染: 距离对齐重采样后绘制 Lap A / Lap B / 实时圈。"""
        for ax in (self.ax_speed, self.ax_pedal):
            for line in list(ax.lines):
                if line not in (self.vline_speed, self.vline_pedal):
                    line.remove()

        legend_handles = []

        # --- 对比圈: 重采样到公共距离网格 (支柱 2) ---
        resampled = {}
        grid = None
        if self.lap_a and self.lap_b and not (self.lap_a.is_empty or self.lap_b.is_empty):
            grid = build_common_grid(self.lap_a, self.lap_b)
            if grid is not None:
                resampled["A"] = self.lap_a.resample_to(grid)
                resampled["B"] = self.lap_b.resample_to(grid)
        # 只有单圈时退化为原始距离轴
        for key, lap, color in (("A", self.lap_a, self.COLOR_A),
                                ("B", self.lap_b, self.COLOR_B)):
            if lap is None or lap.is_empty:
                continue
            if grid is not None and key in resampled:
                x, data = grid, resampled[key]
            else:
                x, data = lap.distance, {"speed": lap.speed,
                                         "throttle": lap.throttle,
                                         "brake": lap.brake}
            h1, = self.ax_speed.plot(x, data["speed"], color=color, lw=1.6,
                                     label=f"Lap {key} 速度")
            h2, = self.ax_pedal.plot(x, data["throttle"], color=color, lw=1.3,
                                     ls="-", label=f"Lap {key} 油门")
            self.ax_pedal.plot(x, data["brake"], color=color, lw=1.3,
                               ls="--", label=f"Lap {key} 刹车")
            legend_handles.append(h1)

        # --- 实时圈 (绿) ---
        if not self.live_lap.is_empty:
            h, = self.ax_speed.plot(self.live_lap.distance, self.live_lap.speed,
                                    color=self.COLOR_LIVE, lw=1.2, alpha=0.85,
                                    label="实时圈")
            self.ax_pedal.plot(self.live_lap.distance, self.live_lap.throttle,
                               color=self.COLOR_LIVE, lw=1.0, alpha=0.7)
            self.ax_pedal.plot(self.live_lap.distance, self.live_lap.brake,
                               color=self.COLOR_LIVE, lw=1.0, ls="--", alpha=0.7)
            legend_handles.append(h)

        if legend_handles:
            self.ax_speed.legend(loc="upper right", fontsize=8)
        self.ax_pedal.legend(loc="upper right", fontsize=8, ncol=4)

        if live_only and latest is not None and self.live_lap.is_empty is False:
            # 实时模式: 视野跟随最新距离 (滑动窗口 600m)
            d = latest["distance"]
            self.ax_speed.set_xlim(max(0, d - 600), max(600, d + 100))
        else:
            self.ax_speed.relim()
            self.ax_speed.autoscale_view()
        self.ax_pedal.relim()
        self.ax_pedal.autoscale_view(scaley=False)

        self._hover_grid = grid
        self._hover_data = resampled
        self.canvas.draw_idle()

    # ---------------- 十字线悬停 ----------------
    def on_mouse_move(self, event):
        if event.inaxes not in (self.ax_speed, self.ax_pedal) or event.xdata is None:
            return
        x = event.xdata
        self.vline_speed.set_xdata([x, x])
        self.vline_pedal.set_xdata([x, x])
        self.vline_speed.set_visible(True)
        self.vline_pedal.set_visible(True)

        lines = [f"d = {x:7.1f} m"]
        # 在公共网格上取最近点, 显示两圈数值与差值
        if self._hover_grid is not None and self._hover_data:
            idx = int(np.searchsorted(self._hover_grid, x))
            idx = min(max(idx, 0), len(self._hover_grid) - 1)
            a, b = self._hover_data.get("A"), self._hover_data.get("B")
            if a and b:
                ds = a["speed"][idx] - b["speed"][idx]
                lines.append(f"A 速度 {a['speed'][idx]:6.1f} | B {b['speed'][idx]:6.1f}"
                             f" | Δ {ds:+.1f} km/h")
                dt = a["throttle"][idx] - b["throttle"][idx]
                db = a["brake"][idx] - b["brake"][idx]
                lines.append(f"油门 A {a['throttle'][idx]:.2f} B {b['throttle'][idx]:.2f}"
                             f" Δ {dt:+.2f}")
                lines.append(f"刹车 A {a['brake'][idx]:.2f} B {b['brake'][idx]:.2f}"
                             f" Δ {db:+.2f}")
        else:
            # 无对比圈时悬停显示实时/单圈数值
            for lap, tag in ((self.lap_a, "A"), (self.lap_b, "B"),
                             (self.live_lap, "Live")):
                if lap and not lap.is_empty and \
                        lap.distance[0] <= x <= lap.distance[-1]:
                    spd = float(np.interp(x, lap.distance, lap.speed))
                    lines.append(f"{tag} 速度 {spd:6.1f} km/h")
        self.hover_text.set_text("\n".join(lines))
        self.canvas.draw_idle()

    def on_mouse_leave(self, _event):
        self.vline_speed.set_visible(False)
        self.vline_pedal.set_visible(False)
        self.hover_text.set_text("")
        self.canvas.draw_idle()

    # ---------------- 状态与退出 ----------------
    def _set_status(self, msg):
        self._status_var.set(msg)

    def on_close(self):
        """优雅退出: 停线程 → 释放端口 → 销毁窗口。"""
        if self.recording and len(self.current_samples) > 10:
            if messagebox.askyesno("退出", "当前圈尚未保存, 是否保存后退出?"):
                self._flush_lap_to_csv(self.current_samples)
        if self.sender:
            self.sender.stop()
        if self.receiver:
            self.receiver.stop()
        # 给线程少量时间收尾
        for t in (self.sender, self.receiver):
            if t and t.is_alive():
                t.join(timeout=0.6)
        self.root.destroy()


def main():
    root = tk.Tk()
    TelemetryApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
