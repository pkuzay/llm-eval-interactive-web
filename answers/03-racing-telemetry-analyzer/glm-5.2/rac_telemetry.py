#!/usr/bin/env python3
"""
RAC Telemetry Tool
==================
Real-time UDP telemetry receiver, logger, and multi-lap comparison analyzer
for racing games (BeamNG-style output).

Single-file script.
Dependencies: numpy, matplotlib  (tkinter ships with CPython)

Quick start:
    python rac_telemetry.py
    → Click "Start Receiver"  (listens UDP :30000)
    → Click "Start Mock"      (injects simulated telemetry at 50 Hz)
    → Click "Record"          (begins logging)
    → When the mock car completes a lap (distance wraps to 0) the lap
      is auto-saved to saved_laps/lap_YYYYMMDD_HHMMSS.csv
    → Click "Load Lap A" / "Load Lap B" to compare two laps side-by-side
    → Hover the chart for a crosshair with per-lap values and deltas
"""

from __future__ import annotations

import os
import csv
import json
import math
import time
import socket
import struct
import queue
import threading
from datetime import datetime

import numpy as np

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import matplotlib
matplotlib.use('TkAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

# ═══════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════

UDP_PORT            = 30000
MOCK_SEND_INTERVAL  = 0.02          # 20 ms → 50 Hz
RESAMPLE_STEP       = 1.0           # metres per resampled point
TRACK_LENGTH        = 2000.0        # metres (mock track length)
AUTO_SAVE_DIR       = "saved_laps"
LAP_RESET_THRESHOLD = 0.3           # new_dist < last_dist * threshold → reset

# Binary struct: little-endian 5 × float32
#   timestamp  distance  speed  throttle  brake
TELEMETRY_FMT  = '<5f'
TELEMETRY_SIZE = struct.calcsize(TELEMETRY_FMT)

# Visual palette
C_LAP_A   = '#2266dd'
C_LAP_B   = '#dd3333'
C_CURRENT = '#22aa55'


# ═══════════════════════════════════════════════════════════════════
#  Telemetry Packet
# ═══════════════════════════════════════════════════════════════════

class TelemetryPacket:
    """Single telemetry frame — lightweight slot-based container."""
    __slots__ = ('timestamp', 'distance', 'speed', 'throttle', 'brake')

    def __init__(self, timestamp: float, distance: float, speed: float,
                 throttle: float, brake: float):
        self.timestamp = timestamp
        self.distance  = distance
        self.speed     = speed
        self.throttle  = throttle
        self.brake     = brake

    @classmethod
    def unpack(cls, raw: bytes) -> 'TelemetryPacket | None':
        """Parse raw UDP payload (struct or JSON) into a packet, or None."""
        if len(raw) >= TELEMETRY_SIZE:
            ts, dist, spd, thr, brk = struct.unpack(TELEMETRY_FMT, raw[:TELEMETRY_SIZE])
            return cls(ts, dist, spd, thr, brk)
        # Fallback: JSON
        try:
            d = json.loads(raw.decode('utf-8'))
            return cls(float(d['timestamp']), float(d['distance']),
                       float(d['speed']),     float(d['throttle']),
                       float(d['brake']))
        except Exception:
            return None


# ═══════════════════════════════════════════════════════════════════
#  UDP Receiver Thread  (non-blocking, daemon)
# ═══════════════════════════════════════════════════════════════════

class UDPTelemetryReceiver(threading.Thread):
    """Non-blocking UDP receiver running as a daemon thread."""

    def __init__(self, port: int, data_queue: queue.Queue):
        super().__init__(daemon=True, name="UDP-Receiver")
        self.port = port
        self.data_queue = data_queue
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self.error_msg: str | None = None

    def run(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(('0.0.0.0', self.port))
            self._sock.settimeout(0.2)                     # non-blocking with periodic wakeup
        except OSError as e:
            self.error_msg = str(e)
            return

        while not self._stop_event.is_set():
            try:
                raw, _ = self._sock.recvfrom(2048)
                pkt = TelemetryPacket.unpack(raw)
                if pkt is not None:
                    try:
                        self.data_queue.put_nowait(pkt)
                    except queue.Full:
                        pass                              # drop frame if backpressure
            except socket.timeout:
                continue                                  # check _stop_event and loop
            except OSError:
                break                                     # socket closed

        self._close_sock()

    def stop(self) -> None:
        self._stop_event.set()
        self._close_sock()

    def _close_sock(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# ═══════════════════════════════════════════════════════════════════
#  Mock Telemetry Sender Thread
# ═══════════════════════════════════════════════════════════════════

class MockTelemetrySender(threading.Thread):
    """Simulates a racing game emitting telemetry via UDP at ~50 Hz."""

    def __init__(self, port: int, interval: float = MOCK_SEND_INTERVAL):
        super().__init__(daemon=True, name="Mock-Sender")
        self.port = port
        self.interval = interval
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None

    def run(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            return

        distance = 0.0
        target = ('127.0.0.1', self.port)

        while not self._stop_event.is_set():
            # ── Speed profile: multi-harmonic sine to mimic a track ──
            speed = (130.0
                     + 70.0 * math.sin(2 * math.pi * distance / 700.0)
                     + 30.0 * math.sin(2 * math.pi * distance / 280.0 + 1.3)
                     + 15.0 * math.sin(2 * math.pi * distance / 120.0 + 0.7))
            speed = float(max(15.0, min(220.0, speed + np.random.normal(0, 1.5))))

            # ── Derive pedal inputs from speed slope (ds/dDistance) ──
            ds_dd = (70.0 * (2 * math.pi / 700.0) * math.cos(2 * math.pi * distance / 700.0)
                     + 30.0 * (2 * math.pi / 280.0) * math.cos(2 * math.pi * distance / 280.0 + 1.3)
                     + 15.0 * (2 * math.pi / 120.0) * math.cos(2 * math.pi * distance / 120.0 + 0.7))

            if ds_dd > 2.0:                                # accelerating
                throttle = min(1.0, ds_dd / 40.0 + 0.3 + np.random.normal(0, 0.02))
                brake    = max(0.0, np.random.normal(0.02, 0.01))
            elif ds_dd < -2.0:                             # braking
                throttle = max(0.0, np.random.normal(0.1, 0.03))
                brake    = min(1.0, abs(ds_dd) / 40.0 + 0.2 + np.random.normal(0, 0.02))
            else:                                          # steady-state / coasting
                throttle = min(1.0, 0.45 + np.random.normal(0, 0.03))
                brake    = max(0.0, np.random.normal(0.01, 0.01))

            throttle = float(max(0.0, min(1.0, throttle)))
            brake    = float(max(0.0, min(1.0, brake)))

            # ── Advance distance ──
            speed_ms = speed / 3.6
            distance += speed_ms * self.interval
            if distance >= TRACK_LENGTH:
                distance = 0.0                             # new lap

            # ── Pack & send ──
            ts = time.time()
            payload = struct.pack(TELEMETRY_FMT, ts, distance, speed, throttle, brake)
            try:
                self._sock.sendto(payload, target)
            except OSError:
                break

            self._stop_event.wait(self.interval)           # interruptible sleep

        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def stop(self) -> None:
        self._stop_event.set()


# ═══════════════════════════════════════════════════════════════════
#  Lap Data Container
# ═══════════════════════════════════════════════════════════════════

class LapData:
    """Accumulates raw telemetry for one lap; provides CSV I/O."""

    def __init__(self, name: str = "Lap"):
        self.name = name
        self.timestamps: list[float] = []
        self.distances:  list[float] = []
        self.speeds:     list[float] = []
        self.throttles:  list[float] = []
        self.brakes:     list[float] = []

    def add(self, pkt: TelemetryPacket) -> None:
        self.timestamps.append(pkt.timestamp)
        self.distances.append(pkt.distance)
        self.speeds.append(pkt.speed)
        self.throttles.append(pkt.throttle)
        self.brakes.append(pkt.brake)

    def is_valid(self) -> bool:
        return len(self.distances) >= 2

    def max_distance(self) -> float:
        return max(self.distances) if self.distances else 0.0

    def arrays(self):
        return (np.asarray(self.distances,  dtype=np.float64),
                np.asarray(self.speeds,     dtype=np.float64),
                np.asarray(self.throttles,  dtype=np.float64),
                np.asarray(self.brakes,     dtype=np.float64))

    # ── CSV persistence ──

    def save_csv(self, filepath: str) -> None:
        with open(filepath, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['timestamp', 'distance_m', 'speed_kmh', 'throttle', 'brake'])
            for i in range(len(self.timestamps)):
                w.writerow([
                    f"{self.timestamps[i]:.6f}",
                    f"{self.distances[i]:.4f}",
                    f"{self.speeds[i]:.4f}",
                    f"{self.throttles[i]:.6f}",
                    f"{self.brakes[i]:.6f}",
                ])

    @classmethod
    def load_csv(cls, filepath: str, name: str | None = None) -> 'LapData':
        lap = cls(name=name or os.path.basename(filepath))
        with open(filepath, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                lap.timestamps.append(float(row['timestamp']))
                lap.distances.append(float(row['distance_m']))
                lap.speeds.append(float(row['speed_kmh']))
                lap.throttles.append(float(row['throttle']))
                lap.brakes.append(float(row['brake']))
        return lap


# ═══════════════════════════════════════════════════════════════════
#  Distance-Based Resampling  (core algorithm)
# ═══════════════════════════════════════════════════════════════════

def resample_by_distance(lap: LapData, step: float = RESAMPLE_STEP):
    """
    Resample lap telemetry onto a uniform distance grid via linear interpolation.

    Traditional time-axis comparison is useless in racing because a fast lap
    and a slow lap have different durations.  By resampling onto a common
    distance grid we achieve spatial alignment — every sample at distance *d*
    represents the same physical point on the track regardless of lap time.

    Returns (dist_grid, speed, throttle, brake) as numpy arrays, or None.
    """
    if not lap.is_valid():
        return None

    dist, spd, thr, brk = lap.arrays()

    # ── Ensure monotonically increasing distance ──
    # (handles any backward jumps from data glitches or resets)
    clean_idx = [0]
    for i in range(1, len(dist)):
        if dist[i] > dist[clean_idx[-1]]:
            clean_idx.append(i)
    if len(clean_idx) < 2:
        return None

    dist_c = dist[clean_idx]
    spd_c  = spd[clean_idx]
    thr_c  = thr[clean_idx]
    brk_c  = brk[clean_idx]

    max_d = dist_c[-1]
    grid  = np.arange(0.0, max_d, step)

    # ── Linear interpolation onto the uniform grid ──
    r_spd = np.interp(grid, dist_c, spd_c)
    r_thr = np.interp(grid, dist_c, thr_c)
    r_brk = np.interp(grid, dist_c, brk_c)

    return grid, r_spd, r_thr, r_brk


# ═══════════════════════════════════════════════════════════════════
#  Main Application  (Tkinter + Matplotlib)
# ═══════════════════════════════════════════════════════════════════

class TelemetryApp:
    """Main GUI application: receiver control, recording, comparison, plotting."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("RAC Telemetry — Multi-Lap Comparison Tool")
        self.root.geometry("1400x850")
        self.root.minsize(1000, 600)

        # ── State ──
        self.data_queue: queue.Queue = queue.Queue(maxsize=10000)
        self.receiver:    UDPTelemetryReceiver | None = None
        self.mock_sender: MockTelemetrySender   | None = None

        self.recording   = False
        self.current_lap = LapData("Current")

        self.lap_a:      LapData | None = None
        self.lap_b:      LapData | None = None
        self.lap_a_path: str | None = None
        self.lap_b_path: str | None = None

        # Resampled data caches: (grid, spd, thr, brk) or None
        self._rsv_a:       tuple | None = None
        self._rsv_b:       tuple | None = None
        self._rsv_current: tuple | None = None

        # Line artist references (for efficient live updates without full redraw)
        self._ln_spd_a = self._ln_spd_b = self._ln_spd_cur = None
        self._ln_thr_a = self._ln_brk_a = self._ln_thr_b = self._ln_brk_b = None
        self._ln_thr_cur = self._ln_brk_cur = None

        # Crosshair artists
        self._vlines:   list = []
        self._mk_a_spd = self._mk_b_spd = None
        self._mk_a_thr = self._mk_a_brk = self._mk_b_thr = self._mk_b_brk = None

        # FPS tracking
        self._frame_count = 0
        self._fps_time    = time.time()

        os.makedirs(AUTO_SAVE_DIR, exist_ok=True)

        self._build_ui()
        self.update_plot()           # initial empty draw + crosshair init
        self._poll_queue()           # start polling loop (50 ms)
        self._live_update()          # start live plot refresh (250 ms)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─────────────────────────── UI ───────────────────────────

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass

        # ── Control bar ──
        ctrl = ttk.Frame(self.root, padding=(8, 6))
        ctrl.pack(side=tk.TOP, fill=tk.X)

        # Network group
        g1 = ttk.LabelFrame(ctrl, text="Network", padding=6)
        g1.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6))
        self.btn_recv_start = ttk.Button(g1, text="▶ Start Receiver", command=self.start_receiver)
        self.btn_recv_start.pack(side=tk.LEFT, padx=2)
        self.btn_recv_stop = ttk.Button(g1, text="■ Stop", command=self.stop_receiver, state=tk.DISABLED)
        self.btn_recv_stop.pack(side=tk.LEFT, padx=2)
        self.btn_mock_start = ttk.Button(g1, text="▶ Start Mock", command=self.start_mock)
        self.btn_mock_start.pack(side=tk.LEFT, padx=2)
        self.btn_mock_stop = ttk.Button(g1, text="■ Stop", command=self.stop_mock, state=tk.DISABLED)
        self.btn_mock_stop.pack(side=tk.LEFT, padx=2)

        # Recording group
        g2 = ttk.LabelFrame(ctrl, text="Recording", padding=6)
        g2.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6))
        self.btn_record = ttk.Button(g2, text="● Record", command=self.toggle_recording)
        self.btn_record.pack(side=tk.LEFT, padx=2)
        self.btn_finish = ttk.Button(g2, text="Finish & Save", command=self.finish_current_lap)
        self.btn_finish.pack(side=tk.LEFT, padx=2)
        self.rec_indicator = tk.Label(g2, text="●", fg='gray', font=('Arial', 14))
        self.rec_indicator.pack(side=tk.LEFT, padx=4)

        # Comparison group
        g3 = ttk.LabelFrame(ctrl, text="Lap Comparison", padding=6)
        g3.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6))
        ttk.Button(g3, text="Load Lap A (Blue)", command=lambda: self.load_lap('A')).pack(side=tk.LEFT, padx=2)
        ttk.Button(g3, text="Load Lap B (Red)",  command=lambda: self.load_lap('B')).pack(side=tk.LEFT, padx=2)
        ttk.Button(g3, text="Clear",              command=self.clear_laps).pack(side=tk.LEFT, padx=2)
        ttk.Button(g3, text="Update Plot",        command=self.update_plot).pack(side=tk.LEFT, padx=2)

        # ── Bottom labels (packed before plot frame so they sit below) ──
        self.status_var = tk.StringVar(value="Ready. Click 'Start Receiver' and 'Start Mock' to begin.")
        ttk.Label(self.root, textvariable=self.status_var,
                  relief=tk.SUNKEN, anchor=tk.W).pack(side=tk.BOTTOM, fill=tk.X)

        self.crosshair_var = tk.StringVar(
            value="Move cursor over the chart to compare lap values at each distance point.")
        tk.Label(self.root, textvariable=self.crosshair_var,
                 font=('Courier', 10), relief=tk.GROOVE, anchor=tk.W,
                 padx=8, pady=3).pack(side=tk.BOTTOM, fill=tk.X)

        # ── Plot frame (canvas + toolbar) ──
        plot_frame = ttk.Frame(self.root)
        plot_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.fig = Figure(figsize=(14, 7), dpi=100, facecolor='#fafafa')
        self.fig.subplots_adjust(hspace=0.32, left=0.07, right=0.97, top=0.94, bottom=0.07)

        self.ax_speed  = self.fig.add_subplot(2, 1, 1)
        self.ax_pedals = self.fig.add_subplot(2, 1, 2)
        self._style_axes()

        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        toolbar = NavigationToolbar2Tk(self.canvas, plot_frame)
        toolbar.update()

        self.canvas.mpl_connect('motion_notify_event', self._on_motion)

    def _style_axes(self) -> None:
        for ax, title, ylabel in [
            (self.ax_speed,  'Speed vs Distance',  'Speed (km/h)'),
            (self.ax_pedals, 'Pedals vs Distance', 'Pedal (0–1)'),
        ]:
            ax.set_title(title, fontsize=11, fontweight='bold')
            ax.set_xlabel('Distance (m)', fontsize=9)
            ax.set_ylabel(ylabel, fontsize=9)
            ax.grid(True, alpha=0.25, linestyle='--')
            ax.set_facecolor('#f5f5f5')
            ax.tick_params(labelsize=8)

    # ─────────────────────── Network Controls ───────────────────────

    def start_receiver(self) -> None:
        if self.receiver and self.receiver.is_alive():
            return
        self.receiver = UDPTelemetryReceiver(UDP_PORT, self.data_queue)
        self.receiver.start()
        self.root.after(300, self._check_receiver)

    def _check_receiver(self) -> None:
        if self.receiver is None:
            return
        if self.receiver.error_msg:
            messagebox.showerror("Socket Error",
                                 f"Failed to bind UDP port {UDP_PORT}:\n{self.receiver.error_msg}")
            self.receiver = None
        elif self.receiver.is_alive():
            self.btn_recv_start.config(state=tk.DISABLED)
            self.btn_recv_stop.config(state=tk.NORMAL)
            self._set_status(f"Receiver listening on UDP :{UDP_PORT}")

    def stop_receiver(self) -> None:
        if self.receiver:
            self.receiver.stop()
            self.receiver.join(timeout=2.0)
            self.receiver = None
        self.btn_recv_start.config(state=tk.NORMAL)
        self.btn_recv_stop.config(state=tk.DISABLED)
        self._set_status("Receiver stopped.")

    def start_mock(self) -> None:
        if self.mock_sender and self.mock_sender.is_alive():
            return
        self.mock_sender = MockTelemetrySender(UDP_PORT)
        self.mock_sender.start()
        self.btn_mock_start.config(state=tk.DISABLED)
        self.btn_mock_stop.config(state=tk.NORMAL)
        self._set_status("Mock telemetry sender started (50 Hz).")

    def stop_mock(self) -> None:
        if self.mock_sender:
            self.mock_sender.stop()
            self.mock_sender.join(timeout=2.0)
            self.mock_sender = None
        self.btn_mock_start.config(state=tk.NORMAL)
        self.btn_mock_stop.config(state=tk.DISABLED)
        self._set_status("Mock sender stopped.")

    # ─────────────────────── Recording Controls ───────────────────────

    def toggle_recording(self) -> None:
        if self.recording:
            self.recording = False
            self.rec_indicator.config(fg='gray')
            self.btn_record.config(text="● Record")
            self._set_status("Recording paused.")
        else:
            self.recording = True
            self.rec_indicator.config(fg='red')
            self.btn_record.config(text="⏸ Pause")
            self._set_status("Recording started...")
            self.update_plot()   # ensure current-lap line exists

    def finish_current_lap(self) -> None:
        """Manually finish and save the current lap to CSV."""
        if not self.current_lap.is_valid():
            messagebox.showwarning("No Data", "Current lap has insufficient data to save.")
            return
        self.recording = False
        self.rec_indicator.config(fg='gray')
        self.btn_record.config(text="● Record")
        self._save_current_lap()
        self.current_lap = LapData("Current")
        self._rsv_current = None
        self.update_plot()

    def _save_current_lap(self) -> None:
        if not self.current_lap.is_valid():
            return
        fname = f"lap_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        fpath = os.path.join(AUTO_SAVE_DIR, fname)
        try:
            self.current_lap.save_csv(fpath)
            self._set_status(f"Lap saved → {fpath}")
        except OSError as e:
            messagebox.showerror("Save Error", f"Failed to save CSV:\n{e}")

    # ─────────────────────── Lap Loading ───────────────────────

    def load_lap(self, which: str) -> None:
        path = filedialog.askopenfilename(
            title=f"Load Lap {which}",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir=AUTO_SAVE_DIR if os.path.isdir(AUTO_SAVE_DIR) else os.getcwd(),
        )
        if not path:
            return
        try:
            lap = LapData.load_csv(path, name=f"Lap {which}")
        except Exception as e:
            messagebox.showerror("Load Error", f"Failed to load CSV:\n{e}")
            return
        if not lap.is_valid():
            messagebox.showwarning("Invalid Data", "The selected file has insufficient data points.")
            return

        if which == 'A':
            self.lap_a = lap
            self.lap_a_path = path
            self._rsv_a = resample_by_distance(lap)
        else:
            self.lap_b = lap
            self.lap_b_path = path
            self._rsv_b = resample_by_distance(lap)

        self._set_status(
            f"Loaded Lap {which}: {os.path.basename(path)}  "
            f"({lap.max_distance():.0f} m, {len(lap.distances)} pts)")
        self.update_plot()

    def clear_laps(self) -> None:
        self.lap_a = self.lap_b = None
        self.lap_a_path = self.lap_b_path = None
        self._rsv_a = self._rsv_b = None
        self._set_status("Laps cleared.")
        self.update_plot()

    # ─────────────────────── Plotting ───────────────────────

    def update_plot(self) -> None:
        """Full redraw of both subplots + crosshair artists."""
        self.ax_speed.clear()
        self.ax_pedals.clear()
        self._style_axes()

        # Reset line references
        self._ln_spd_a = self._ln_spd_b = self._ln_spd_cur = None
        self._ln_thr_a = self._ln_brk_a = self._ln_thr_b = self._ln_brk_b = None
        self._ln_thr_cur = self._ln_brk_cur = None

        # ── Speed subplot ──
        if self._rsv_a is not None:
            g, s, _, _ = self._rsv_a
            self._ln_spd_a, = self.ax_speed.plot(g, s, color=C_LAP_A, lw=1.5,
                                                  label='Lap A', alpha=0.9)
        if self._rsv_b is not None:
            g, s, _, _ = self._rsv_b
            self._ln_spd_b, = self.ax_speed.plot(g, s, color=C_LAP_B, lw=1.5,
                                                  label='Lap B', alpha=0.9)
        # Current lap line (always created so live updates can use it)
        if self._rsv_current is not None:
            g, s, _, _ = self._rsv_current
            self._ln_spd_cur, = self.ax_speed.plot(g, s, color=C_CURRENT, lw=1.0,
                                                     label='Current', alpha=0.5, ls='--')
        else:
            self._ln_spd_cur, = self.ax_speed.plot([], [], color=C_CURRENT, lw=1.0,
                                                     label='Current', alpha=0.5, ls='--')

        self.ax_speed.legend(loc='upper right', fontsize=8, framealpha=0.8)
        self.ax_speed.set_ylim(bottom=0)

        # ── Pedals subplot ──
        if self._rsv_a is not None:
            g, _, t, b = self._rsv_a
            self._ln_thr_a, = self.ax_pedals.plot(g, t, color=C_LAP_A, lw=1.2, label='A Thr', alpha=0.8)
            self._ln_brk_a, = self.ax_pedals.plot(g, b, color=C_LAP_A, lw=1.2, label='A Brk', alpha=0.8, ls='--')
        if self._rsv_b is not None:
            g, _, t, b = self._rsv_b
            self._ln_thr_b, = self.ax_pedals.plot(g, t, color=C_LAP_B, lw=1.2, label='B Thr', alpha=0.8)
            self._ln_brk_b, = self.ax_pedals.plot(g, b, color=C_LAP_B, lw=1.2, label='B Brk', alpha=0.8, ls='--')
        if self._rsv_current is not None:
            g, _, t, b = self._rsv_current
            self._ln_thr_cur, = self.ax_pedals.plot(g, t, color=C_CURRENT, lw=0.8, label='Cur Thr', alpha=0.4)
            self._ln_brk_cur, = self.ax_pedals.plot(g, b, color=C_CURRENT, lw=0.8, label='Cur Brk', alpha=0.4, ls='--')
        else:
            self._ln_thr_cur, = self.ax_pedals.plot([], [], color=C_CURRENT, lw=0.8, label='Cur Thr', alpha=0.4)
            self._ln_brk_cur, = self.ax_pedals.plot([], [], color=C_CURRENT, lw=0.8, label='Cur Brk', alpha=0.4, ls='--')

        self.ax_pedals.legend(loc='upper right', fontsize=7, ncol=3, framealpha=0.8)
        self.ax_pedals.set_ylim(-0.05, 1.1)

        # ── X-axis limits (span all available data) ──
        max_d = 0.0
        for rsv in (self._rsv_a, self._rsv_b, self._rsv_current):
            if rsv is not None:
                max_d = max(max_d, rsv[0][-1])
        if max_d > 0:
            self.ax_speed.set_xlim(0, max_d * 1.02)
            self.ax_pedals.set_xlim(0, max_d * 1.02)

        # ── Recreate crosshair artists (axes were cleared) ──
        self._vlines = []
        for ax in (self.ax_speed, self.ax_pedals):
            vl = ax.axvline(x=0, color='gray', lw=0.8, ls='--', alpha=0.7, zorder=10)
            vl.set_visible(False)
            self._vlines.append(vl)

        self._mk_a_spd, = self.ax_speed.plot([], [], 'o', color=C_LAP_A, ms=7, zorder=11, visible=False)
        self._mk_b_spd, = self.ax_speed.plot([], [], 's', color=C_LAP_B, ms=7, zorder=11, visible=False)
        self._mk_a_thr, = self.ax_pedals.plot([], [], 'o', color=C_LAP_A, ms=6, zorder=11, visible=False)
        self._mk_a_brk, = self.ax_pedals.plot([], [], 'v', color=C_LAP_A, ms=6, zorder=11, visible=False)
        self._mk_b_thr, = self.ax_pedals.plot([], [], 'o', color=C_LAP_B, ms=6, zorder=11, visible=False)
        self._mk_b_brk, = self.ax_pedals.plot([], [], 'v', color=C_LAP_B, ms=6, zorder=11, visible=False)

        self.canvas.draw_idle()

    def _update_current_only(self) -> None:
        """Lightweight update: refresh only the 'Current' lap lines (no full redraw)."""
        if self._ln_spd_cur is None:
            return
        if self._rsv_current is not None:
            g, s, t, b = self._rsv_current
            self._ln_spd_cur.set_data(g, s)
            self._ln_thr_cur.set_data(g, t)
            self._ln_brk_cur.set_data(g, b)
            # Extend x-axis if current lap exceeds current limits
            cur_xlim = self.ax_speed.get_xlim()
            if len(g) > 0 and g[-1] > cur_xlim[1]:
                new_max = float(g[-1]) * 1.02
                self.ax_speed.set_xlim(cur_xlim[0], new_max)
                self.ax_pedals.set_xlim(cur_xlim[0], new_max)
        else:
            self._ln_spd_cur.set_data([], [])
            self._ln_thr_cur.set_data([], [])
            self._ln_brk_cur.set_data([], [])
        self.canvas.draw_idle()

    # ─────────────────────── Crosshair ───────────────────────

    def _on_motion(self, event) -> None:
        """Handle mouse movement: update crosshair and info text."""
        if event.xdata is None:
            self._hide_crosshair()
            return

        x = event.xdata
        for vl in self._vlines:
            vl.set_xdata([x, x])
            vl.set_visible(True)

        parts: list[str] = [f"Dist: {x:7.1f} m"]
        sa = sb = None

        # ── Lap A values at cursor distance ──
        if self._rsv_a is not None:
            ga, spa, tha, bra = self._rsv_a
            sa = float(np.interp(x, ga, spa))
            ta = float(np.interp(x, ga, tha))
            ba = float(np.interp(x, ga, bra))
            self._mk_a_spd.set_data([x], [sa]); self._mk_a_spd.set_visible(True)
            self._mk_a_thr.set_data([x], [ta]); self._mk_a_thr.set_visible(True)
            self._mk_a_brk.set_data([x], [ba]); self._mk_a_brk.set_visible(True)
            parts.append(f"│ A: spd={sa:6.1f}  thr={ta:.2f}  brk={ba:.2f}")
        else:
            self._mk_a_spd.set_visible(False)
            self._mk_a_thr.set_visible(False)
            self._mk_a_brk.set_visible(False)

        # ── Lap B values at cursor distance ──
        if self._rsv_b is not None:
            gb, spb, thb, brb = self._rsv_b
            sb = float(np.interp(x, gb, spb))
            tb = float(np.interp(x, gb, thb))
            bb = float(np.interp(x, gb, brb))
            self._mk_b_spd.set_data([x], [sb]); self._mk_b_spd.set_visible(True)
            self._mk_b_thr.set_data([x], [tb]); self._mk_b_thr.set_visible(True)
            self._mk_b_brk.set_data([x], [bb]); self._mk_b_brk.set_visible(True)
            parts.append(f"│ B: spd={sb:6.1f}  thr={tb:.2f}  brk={bb:.2f}")
        else:
            self._mk_b_spd.set_visible(False)
            self._mk_b_thr.set_visible(False)
            self._mk_b_brk.set_visible(False)

        # ── Speed delta ──
        if sa is not None and sb is not None:
            d = sb - sa
            parts.append(f"│ Δspd={'+' if d >= 0 else ''}{d:6.1f} km/h")

        self.crosshair_var.set("  ".join(parts))
        self.canvas.draw_idle()

    def _hide_crosshair(self) -> None:
        for vl in self._vlines:
            vl.set_visible(False)
        for mk in (self._mk_a_spd, self._mk_b_spd,
                   self._mk_a_thr, self._mk_a_brk,
                   self._mk_b_thr, self._mk_b_brk):
            if mk is not None:
                mk.set_visible(False)
        self.canvas.draw_idle()

    # ─────────────────────── Queue Polling ───────────────────────

    def _poll_queue(self) -> None:
        """Drain the data queue into the current lap buffer (every 50 ms)."""
        if self.recording:
            count = 0
            while count < 500:   # cap per cycle to avoid UI freeze
                try:
                    pkt = self.data_queue.get_nowait()
                except queue.Empty:
                    break
                self._process_packet(pkt)
                self._frame_count += 1
                count += 1

            # FPS + live status update
            now = time.time()
            if now - self._fps_time >= 1.0:
                fps = self._frame_count / (now - self._fps_time)
                self._frame_count = 0
                self._fps_time = now
                last_spd = self.current_lap.speeds[-1]    if self.current_lap.speeds     else 0
                last_dst = self.current_lap.distances[-1] if self.current_lap.distances  else 0
                self._set_status(
                    f"REC │ {fps:.0f} fps │ {len(self.current_lap.distances)} pts │ "
                    f"dist={last_dst:.0f}m │ spd={last_spd:.1f} km/h")

        self.root.after(50, self._poll_queue)

    def _process_packet(self, pkt: TelemetryPacket) -> None:
        """Add packet to current lap; auto-detect lap resets."""
        last_d = self.current_lap.distances[-1] if self.current_lap.distances else 0.0

        # Lap reset detection: distance drops sharply (e.g., car crosses start/finish)
        if last_d > 50.0 and pkt.distance < last_d * LAP_RESET_THRESHOLD:
            self._save_current_lap()
            self.current_lap = LapData("Current")
            self._set_status("Auto lap reset detected — previous lap saved. New lap started.")

        self.current_lap.add(pkt)

    # ─────────────────────── Live Plot Update ───────────────────────

    def _live_update(self) -> None:
        """Periodically resample current lap and refresh its lines (every 250 ms)."""
        if self.recording and self.current_lap.is_valid():
            self._rsv_current = resample_by_distance(self.current_lap)
            self._update_current_only()

        self.root.after(250, self._live_update)

    # ─────────────────────── Utilities ───────────────────────

    def _set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    def _on_close(self) -> None:
        """Graceful shutdown — stop threads, close sockets, destroy window."""
        self.recording = False
        if self.receiver:
            self.receiver.stop()
            self.receiver.join(timeout=2.0)
        if self.mock_sender:
            self.mock_sender.stop()
            self.mock_sender.join(timeout=2.0)
        self.root.destroy()


# ═══════════════════════════════════════════════════════════════════
#  Entry Point
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    root = tk.Tk()
    TelemetryApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()