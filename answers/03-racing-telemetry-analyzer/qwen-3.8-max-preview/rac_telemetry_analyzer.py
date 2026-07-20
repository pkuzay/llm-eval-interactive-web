#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rac_telemetry_analyzer.py

Real-time RAC telemetry receiver, logger, and distance-based lap comparison tool.

Features:
- Non-blocking UDP receiver in a separate thread.
- Built-in mock UDP telemetry sender for standalone testing.
- Distance-based lap alignment and resampling using numpy.interp.
- Automatic/manual CSV lap logging.
- Interactive Tkinter + Matplotlib UI with crosshair cursor and A/B comparison.

Default UDP port: 30000
Frame format: JSON
{
    "timestamp": float,
    "distance": float,
    "speed": float,
    "throttle": float,
    "brake": float
}
"""

import argparse
import csv
import json
import math
import queue
import select
import socket
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

try:
    import numpy as np
except ImportError:
    sys.stderr.write("This tool requires numpy. Install with: python -m pip install numpy\n")
    sys.exit(1)

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    sys.stderr.write("Tkinter is required for the GUI. On Debian/Ubuntu install python3-tk.\n")
    sys.exit(1)

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
except Exception as exc:
    sys.stderr.write(f"Matplotlib TkAgg backend is required: {exc}\n")
    sys.exit(1)


FIELDS = ("timestamp", "distance", "speed", "throttle", "brake")


def clamp(value, low=0.0, high=1.0):
    """Clamp a value to [low, high], with NaN/invalid fallback to low."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return low
    if not math.isfinite(value):
        return low
    if value < low:
        return low
    if value > high:
        return high
    return value


def set_line(line, x, y, max_points=20000):
    """Safely set matplotlib line data, downsampling if necessary."""
    if x is None or y is None:
        line.set_visible(False)
        line.set_data([], [])
        return

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)

    if x.size < 2 or y.size < 2:
        line.set_visible(False)
        line.set_data([], [])
        return

    n = min(x.size, y.size)
    x = x[:n]
    y = y[:n]

    if n > max_points:
        idx = np.linspace(0, n - 1, max_points, dtype=int)
        x = x[idx]
        y = y[idx]

    line.set_data(x, y)
    line.set_visible(True)


class LapData:
    """Container for one lap of telemetry data."""

    def __init__(self, name="Lap"):
        self.name = name
        self.timestamp = []
        self.distance = []
        self.speed = []
        self.throttle = []
        self.brake = []
        self._cache = None

    def clear(self):
        self.timestamp.clear()
        self.distance.clear()
        self.speed.clear()
        self.throttle.clear()
        self.brake.clear()
        self._cache = None

    def __len__(self):
        return len(self.distance)

    @property
    def last_distance(self):
        return self.distance[-1] if self.distance else None

    def append(self, frame):
        """Append one telemetry frame. Invalid frames are ignored."""
        try:
            ts = float(frame.get("timestamp", time.time()))
            dist = float(frame.get("distance"))
            speed = float(frame.get("speed"))
            throttle = clamp(frame.get("throttle", 0.0), 0.0, 1.0)
            brake = clamp(frame.get("brake", 0.0), 0.0, 1.0)
        except (TypeError, ValueError, KeyError):
            return False

        if not all(math.isfinite(v) for v in (ts, dist, speed, throttle, brake)):
            return False

        if dist < 0.0:
            dist = 0.0
        if speed < 0.0:
            speed = 0.0

        self.timestamp.append(ts)
        self.distance.append(dist)
        self.speed.append(speed)
        self.throttle.append(throttle)
        self.brake.append(brake)
        self._cache = None
        return True

    def is_valid(self, min_points=10, min_distance=20.0):
        if len(self.distance) < min_points:
            return False
        try:
            d = np.asarray(self.distance, dtype=float)
            d = d[np.isfinite(d)]
            if d.size < min_points:
                return False
            return float(np.max(d) - np.min(d)) >= float(min_distance)
        except Exception:
            return False

    def sanitized(self):
        """
        Return cleaned, distance-sorted, duplicate-reduced arrays:
        distance, timestamp, speed, throttle, brake
        """
        if self._cache is not None:
            return self._cache

        empty = (
            np.array([], dtype=float),
            np.array([], dtype=float),
            np.array([], dtype=float),
            np.array([], dtype=float),
            np.array([], dtype=float),
        )

        if len(self.distance) < 2:
            self._cache = empty
            return empty

        try:
            d = np.asarray(self.distance, dtype=float)
            ts = np.asarray(self.timestamp, dtype=float)
            sp = np.asarray(self.speed, dtype=float)
            th = np.asarray(self.throttle, dtype=float)
            br = np.asarray(self.brake, dtype=float)

            mask = (
                np.isfinite(d)
                & np.isfinite(ts)
                & np.isfinite(sp)
                & np.isfinite(th)
                & np.isfinite(br)
            )

            if int(mask.sum()) < 2:
                self._cache = empty
                return empty

            d = d[mask]
            ts = ts[mask]
            sp = sp[mask]
            th = th[mask]
            br = br[mask]

            order = np.argsort(d, kind="mergesort")
            d = d[order]
            ts = ts[order]
            sp = sp[order]
            th = th[order]
            br = br[order]

            uniq, inv, counts = np.unique(d, return_inverse=True, return_counts=True)
            if uniq.size < 2:
                self._cache = empty
                return empty

            counts = counts.astype(float)

            def average(values):
                return np.bincount(inv, weights=values) / counts

            ts2 = average(ts)
            sp2 = np.maximum(average(sp), 0.0)
            th2 = np.clip(average(th), 0.0, 1.0)
            br2 = np.clip(average(br), 0.0, 1.0)

            self._cache = (uniq, ts2, sp2, th2, br2)
            return self._cache

        except Exception:
            self._cache = empty
            return empty

    def value_at(self, x):
        """Interpolated value at distance x."""
        d, ts, sp, th, br = self.sanitized()
        if d.size < 2:
            return None

        try:
            x = float(x)
        except (TypeError, ValueError):
            return None

        if not math.isfinite(x) or x < d[0] or x > d[-1]:
            return None

        return {
            "distance": x,
            "timestamp": float(np.interp(x, d, ts)),
            "speed": float(np.interp(x, d, sp)),
            "throttle": float(np.interp(x, d, th)),
            "brake": float(np.interp(x, d, br)),
        }

    def save_csv(self, directory=".", basename=None, min_points=2, min_distance=1.0):
        if not self.is_valid(min_points=min_points, min_distance=min_distance):
            return None

        try:
            Path(directory).mkdir(parents=True, exist_ok=True)

            if basename is None:
                basename = datetime.now().strftime("lap_%Y%m%d_%H%M%S")

            path = Path(directory) / f"{basename}.csv"
            counter = 1
            while path.exists():
                path = Path(directory) / f"{basename}_{counter:03d}.csv"
                counter += 1

            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(FIELDS)
                for i in range(len(self.distance)):
                    writer.writerow(
                        [
                            f"{self.timestamp[i]:.6f}",
                            f"{self.distance[i]:.3f}",
                            f"{self.speed[i]:.3f}",
                            f"{self.throttle[i]:.4f}",
                            f"{self.brake[i]:.4f}",
                        ]
                    )

            return str(path)

        except Exception:
            traceback.print_exc()
            return None

    @classmethod
    def load_csv(cls, path):
        p = Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(str(p))

        lap = cls(name=p.stem)

        with open(p, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise ValueError("Empty CSV file.")

            normalized = {name.strip().lower(): name for name in reader.fieldnames}
            missing = [field for field in FIELDS if field not in normalized]
            if missing:
                raise ValueError("Missing required columns: " + ", ".join(missing))

            for row in reader:
                try:
                    frame = {field: float(row[normalized[field]]) for field in FIELDS}
                except (TypeError, ValueError, KeyError):
                    continue
                lap.append(frame)

        if len(lap) == 0:
            raise ValueError("No valid telemetry rows found in CSV.")

        return lap


def positive_median_step(distance):
    if distance.size < 2:
        return 1.0

    diffs = np.diff(distance)
    diffs = diffs[np.isfinite(diffs) & (diffs > 1e-9)]

    if diffs.size == 0:
        return 1.0

    step = float(np.median(diffs))
    if not math.isfinite(step) or step <= 0.0:
        return 1.0

    return step


def align_laps(lap_a, lap_b, max_points=20000):
    """
    Distance-based alignment and resampling.

    Both laps are interpolated onto the same distance grid covering their
    overlapping distance range.
    """
    da, _, sa, ta, ba = lap_a.sanitized()
    db, _, sb, tb, bb = lap_b.sanitized()

    if da.size < 2 or db.size < 2:
        return None

    d_start = max(float(da[0]), float(db[0]))
    d_end = min(float(da[-1]), float(db[-1]))

    if d_end - d_start <= 1.0:
        return None

    step_a = positive_median_step(da)
    step_b = positive_median_step(db)
    step = min(step_a, step_b)
    step = max(0.2, min(10.0, step))

    span = d_end - d_start
    if span / step + 1 > max_points:
        step = span / float(max_points - 1)

    grid = np.arange(d_start, d_end + step * 0.5, step, dtype=float)

    if grid.size < 2:
        grid = np.linspace(d_start, d_end, num=min(max_points, 1000), dtype=float)

    grid = grid[(grid >= d_start - 1e-9) & (grid <= d_end + 1e-9)]

    if grid.size < 2:
        return None

    return {
        "distance": grid,
        "A_speed": np.interp(grid, da, sa),
        "B_speed": np.interp(grid, db, sb),
        "A_throttle": np.interp(grid, da, ta),
        "B_throttle": np.interp(grid, db, tb),
        "A_brake": np.interp(grid, da, ba),
        "B_brake": np.interp(grid, db, bb),
    }


class UdpReceiver(threading.Thread):
    """
    Non-blocking UDP telemetry receiver.

    Runs in a dedicated thread and pushes parsed JSON frames into a queue.
    """

    def __init__(self, port, data_queue, host="0.0.0.0"):
        super().__init__(daemon=True)
        self.port = port
        self.host = host
        self.data_queue = data_queue
        self._stop_event = threading.Event()
        self.sock = None
        self.error = None
        self.ready = threading.Event()
        self.bound = False
        self.received = 0

    def stop(self):
        self._stop_event.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def run(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.host, self.port))
            self.sock.setblocking(False)

            self.bound = True
            self.ready.set()

            while not self._stop_event.is_set():
                try:
                    readable, _, _ = select.select([self.sock], [], [], 0.1)
                except (OSError, ValueError):
                    break

                if not readable:
                    continue

                try:
                    data, _ = self.sock.recvfrom(65535)
                except BlockingIOError:
                    continue
                except OSError:
                    break

                if not data:
                    continue

                try:
                    obj = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                except Exception:
                    continue

                if not isinstance(obj, dict):
                    continue

                try:
                    if "timestamp" not in obj:
                        obj["timestamp"] = time.time()

                    if "distance" not in obj or "speed" not in obj:
                        continue

                    obj.setdefault("throttle", 0.0)
                    obj.setdefault("brake", 0.0)

                    self.data_queue.put_nowait(obj)
                    self.received += 1

                except queue.Full:
                    # Drop oldest frame if the UI cannot consume fast enough.
                    try:
                        self.data_queue.get_nowait()
                        self.data_queue.put_nowait(obj)
                    except Exception:
                        pass
                except Exception:
                    continue

        except Exception as exc:
            self.error = f"{exc.__class__.__name__}: {exc}"
        finally:
            self.ready.set()
            self.bound = False
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass


class MockTelemetrySender(threading.Thread):
    """
    Built-in mock UDP telemetry sender.

    Sends one JSON frame every interval seconds to the receiver port.
    Simulates a repeating lap with distance-based speed/pedal patterns.
    """

    def __init__(self, port, host="127.0.0.1", interval=0.02, lap_length=1200.0):
        super().__init__(daemon=True)
        self.port = port
        self.host = host
        self.interval = max(0.001, float(interval))
        self.lap_length = max(50.0, float(lap_length))
        self._stop_event = threading.Event()
        self.sock = None
        self.error = None
        self.sent = 0

    def stop(self):
        self._stop_event.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def run(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            distance = 0.0
            last = time.perf_counter()

            two_pi = 2.0 * math.pi
            k1 = two_pi / 350.0
            k2 = two_pi / 97.0

            while not self._stop_event.is_set():
                now = time.perf_counter()
                dt = now - last
                last = now

                if dt <= 0.0:
                    dt = self.interval
                dt = min(dt, 0.2)

                t = time.time()

                phase1 = k1 * distance
                phase2 = k2 * distance

                speed = (
                    100.0
                    + 50.0 * math.sin(phase1)
                    + 20.0 * math.sin(phase2)
                    + 1.5 * math.sin(t * 3.1)
                )
                speed = max(0.0, speed)

                # Approximate longitudinal acceleration from distance-based speed gradient.
                ds_dd = (
                    50.0 * k1 * math.cos(phase1)
                    + 20.0 * k2 * math.cos(phase2)
                )
                accel = ds_dd * speed / 3.6

                throttle = clamp(0.55 + accel / 18.0, 0.0, 1.0)
                brake = clamp(-accel / 25.0, 0.0, 1.0)

                if brake > 0.05:
                    throttle = min(throttle, 0.05)

                frame = {
                    "timestamp": t,
                    "distance": distance,
                    "speed": speed,
                    "throttle": throttle,
                    "brake": brake,
                }

                payload = json.dumps(frame, separators=(",", ":")).encode("utf-8")
                self.sock.sendto(payload, (self.host, self.port))
                self.sent += 1

                distance += (speed / 3.6) * dt

                if distance >= self.lap_length:
                    distance = 0.0

                self._stop_event.wait(self.interval)

        except Exception as exc:
            self.error = f"{exc.__class__.__name__}: {exc}"
        finally:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass


class Crosshair:
    """Blitted vertical crosshair for Matplotlib canvases."""

    def __init__(self, fig, canvas, axes):
        self.fig = fig
        self.canvas = canvas
        self.axes = axes
        self.background = None
        self.lines = []

        for ax in axes:
            line = ax.axvline(
                0.0,
                color="#444444",
                linestyle="--",
                linewidth=1.0,
                visible=False,
                zorder=10,
            )
            self.lines.append(line)

        self.canvas.mpl_connect("draw_event", self.on_draw)

    def on_draw(self, event):
        try:
            self.background = self.canvas.copy_from_bbox(self.canvas.figure.bbox)
        except Exception:
            self.background = None

    def invalidate(self):
        """Hide crosshair before a full redraw so background stays clean."""
        for line in self.lines:
            try:
                line.set_visible(False)
            except Exception:
                pass

    def show(self, x):
        if x is None or not math.isfinite(x):
            return

        if self.background is None:
            try:
                self.canvas.draw()
            except Exception:
                return
            if self.background is None:
                return

        try:
            self.canvas.restore_region(self.background)

            for ax, line in zip(self.axes, self.lines):
                ymin, ymax = ax.get_ylim()
                line.set_data([x, x], [ymin, ymax])
                line.set_visible(True)
                self.canvas.draw_artist(line)

            self.canvas.blit(self.canvas.figure.bbox)

        except Exception:
            pass

    def hide(self):
        if self.background is not None:
            try:
                self.canvas.restore_region(self.background)
                self.canvas.blit(self.canvas.figure.bbox)
            except Exception:
                pass

        for line in self.lines:
            try:
                line.set_visible(False)
            except Exception:
                pass


class TelemetryApp:
    POLL_MS = 20
    REFRESH_MS = 100

    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.log_dir = args.log_dir
        self._closed = False

        self.queue = queue.Queue(maxsize=10000)

        self.receiver = None
        self.sender = None

        self.live_lap = LapData("Live")
        self._live_cache = None

        self.lap_a = None
        self.lap_b = None
        self.aligned = None

        self.recording = True
        self.reset_drop = max(0.5, float(args.reset_drop))

        self.last_mouse_x = None
        self.mouse_inside = False

        self._build_ui()
        self._build_plots()

        self._start_receiver()

        if (
            not args.no_mock
            and self.receiver is not None
            and self.receiver.bound
            and not self.receiver.error
        ):
            self._start_sender()

        self._poll_queue()
        self._refresh_tick()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        self.root.title("RAC Telemetry Receiver / Lap Comparison Tool")
        self.root.geometry("1320x880")

        main = ttk.Frame(self.root, padding=(6, 6, 6, 6))
        main.pack(fill=tk.BOTH, expand=True)

        toolbar = ttk.Frame(main)
        toolbar.pack(side=tk.TOP, fill=tk.X, pady=(0, 4))

        self.btn_mock = ttk.Button(toolbar, text="Start Mock", command=self._toggle_mock)
        self.btn_mock.pack(side=tk.LEFT, padx=2)

        self.btn_record = ttk.Button(toolbar, text="Stop Recording", command=self._toggle_recording)
        self.btn_record.pack(side=tk.LEFT, padx=2)

        self.btn_save = ttk.Button(toolbar, text="Save Current Lap", command=self._save_current_lap)
        self.btn_save.pack(side=tk.LEFT, padx=2)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        self.btn_load_a = ttk.Button(toolbar, text="Load Lap A", command=lambda: self._load_lap("A"))
        self.btn_load_a.pack(side=tk.LEFT, padx=2)

        self.btn_load_b = ttk.Button(toolbar, text="Load Lap B", command=lambda: self._load_lap("B"))
        self.btn_load_b.pack(side=tk.LEFT, padx=2)

        self.btn_clear_compare = ttk.Button(toolbar, text="Clear Compare", command=self._clear_compare)
        self.btn_clear_compare.pack(side=tk.LEFT, padx=2)

        self.btn_clear_live = ttk.Button(toolbar, text="Clear Live", command=self._clear_live)
        self.btn_clear_live.pack(side=tk.LEFT, padx=2)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        self.autoscale_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="Auto Scale", variable=self.autoscale_var).pack(side=tk.LEFT, padx=4)

        self.autosave_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="Auto Save on Reset", variable=self.autosave_var).pack(side=tk.LEFT, padx=4)

        self.status_var = tk.StringVar(value="Initializing...")
        self.info_var = tk.StringVar(value="Move mouse over plots to inspect values.")

        status_frame = ttk.Frame(main)
        status_frame.pack(side=tk.TOP, fill=tk.X, pady=(0, 2))

        ttk.Label(status_frame, textvariable=self.status_var, anchor=tk.W).pack(side=tk.TOP, fill=tk.X)
        ttk.Label(
            status_frame,
            textvariable=self.info_var,
            anchor=tk.W,
            foreground="#003366",
        ).pack(side=tk.TOP, fill=tk.X)

        self.plot_frame = ttk.Frame(main)
        self.plot_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    def _build_plots(self):
        self.fig = Figure(figsize=(11, 7), dpi=100)

        self.ax_speed = self.fig.add_subplot(2, 1, 1)
        self.ax_pedals = self.fig.add_subplot(2, 1, 2, sharex=self.ax_speed)

        self.ax_speed.set_title("Speed vs Distance")
        self.ax_speed.set_ylabel("Speed (km/h)")
        self.ax_speed.grid(True, linestyle=":", alpha=0.6)

        self.ax_pedals.set_title("Throttle / Brake vs Distance")
        self.ax_pedals.set_xlabel("Distance (m)")
        self.ax_pedals.set_ylabel("Pedal (0-1)")
        self.ax_pedals.grid(True, linestyle=":", alpha=0.6)
        self.ax_pedals.set_ylim(-0.05, 1.05)

        self.speed_lines = {
            "A": self.ax_speed.plot([], [], color="tab:blue", linewidth=1.5, label="Lap A", visible=False)[0],
            "B": self.ax_speed.plot([], [], color="tab:red", linewidth=1.5, label="Lap B", visible=False)[0],
            "Live": self.ax_speed.plot([], [], color="tab:green", linewidth=1.2, label="Live", visible=False)[0],
        }

        self.pedal_lines = {
            "A_th": self.ax_pedals.plot([], [], color="tab:blue", linewidth=1.3, label="A throttle", visible=False)[0],
            "A_br": self.ax_pedals.plot([], [], color="tab:blue", linewidth=1.3, linestyle="--", label="A brake", visible=False)[0],
            "B_th": self.ax_pedals.plot([], [], color="tab:red", linewidth=1.3, label="B throttle", visible=False)[0],
            "B_br": self.ax_pedals.plot([], [], color="tab:red", linewidth=1.3, linestyle="--", label="B brake", visible=False)[0],
            "Live_th": self.ax_pedals.plot([], [], color="tab:green", linewidth=1.1, label="Live throttle", visible=False)[0],
            "Live_br": self.ax_pedals.plot([], [], color="tab:green", linewidth=1.1, linestyle="--", label="Live brake", visible=False)[0],
        }

        self.ax_speed.legend(loc="upper right", fontsize=8)
        self.ax_pedals.legend(loc="upper right", fontsize=7, ncol=3)

        self.fig.subplots_adjust(left=0.07, right=0.98, top=0.94, bottom=0.08, hspace=0.35)

        self.toolbar_frame = ttk.Frame(self.plot_frame)
        self.toolbar_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_frame)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        try:
            self.toolbar = NavigationToolbar2Tk(self.canvas, self.toolbar_frame)
            self.toolbar.update()
        except Exception:
            self.toolbar = None

        self.crosshair = Crosshair(self.fig, self.canvas, [self.ax_speed, self.ax_pedals])

        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("axes_leave_event", self._on_leave)

    def _start_receiver(self):
        if self.receiver is not None and self.receiver.is_alive():
            return

        self.receiver = UdpReceiver(self.args.port, self.queue, host=self.args.bind)
        self.receiver.start()

        if not self.receiver.ready.wait(1.5):
            self.status_var.set("UDP receiver start timeout.")
            return

        if self.receiver.error:
            msg = f"Cannot bind UDP port {self.args.port}: {self.receiver.error}"
            self.status_var.set(msg)
            self.root.after(
                250,
                lambda m=msg: messagebox.showerror(
                    "UDP Receiver Error",
                    m + "\n\nThe tool will continue without UDP input.",
                ),
            )

    def _start_sender(self):
        if self.sender is not None and self.sender.is_alive():
            return

        self.sender = MockTelemetrySender(
            port=self.args.port,
            host=self.args.mock_target,
            interval=self.args.mock_interval,
            lap_length=self.args.lap_length,
        )
        self.sender.start()
        self._update_button_states()

    def _stop_sender(self):
        if self.sender is not None:
            self.sender.stop()
            self.sender.join(timeout=0.5)
            self.sender = None
        self._update_button_states()

    def _toggle_mock(self):
        if self.sender is not None and self.sender.is_alive():
            self._stop_sender()
        else:
            self._start_sender()

    def _toggle_recording(self):
        self.recording = not self.recording
        self._update_button_states()

    def _save_current_lap(self):
        path = self.live_lap.save_csv(
            self.log_dir,
            min_points=5,
            min_distance=5.0,
        )

        if path:
            self.status_var.set(f"Saved current lap: {path}")
            self.live_lap = LapData("Live")
            self._live_cache = None
            self._redraw()
        else:
            messagebox.showwarning(
                "Save Current Lap",
                "Current live lap has too few points (<5) or distance range (<5 m).",
            )

    def _load_lap(self, target):
        initial_dir = self.log_dir if Path(self.log_dir).is_dir() else Path.cwd()

        path = filedialog.askopenfilename(
            title=f"Load Lap {target}",
            initialdir=str(initial_dir),
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )

        if not path:
            return

        try:
            lap = LapData.load_csv(path)
        except Exception as exc:
            messagebox.showerror("Load Error", str(exc))
            return

        if target == "A":
            self.lap_a = lap
        elif target == "B":
            self.lap_b = lap

        self._update_alignment()
        self._redraw()
        self.status_var.set(f"Loaded Lap {target}: {path} ({len(lap)} points)")

    def _clear_compare(self):
        self.lap_a = None
        self.lap_b = None
        self.aligned = None
        self._redraw()

    def _clear_live(self):
        self.live_lap = LapData("Live")
        self._live_cache = None

        try:
            while True:
                self.queue.get_nowait()
        except queue.Empty:
            pass

        self._redraw()

    def _update_alignment(self):
        if self.lap_a is not None and self.lap_b is not None:
            self.aligned = align_laps(self.lap_a, self.lap_b, max_points=20000)
        else:
            self.aligned = None

    def _finalize_live_lap(self, auto=True):
        if len(self.live_lap) == 0:
            return

        if self.autosave_var.get():
            path = self.live_lap.save_csv(
                self.log_dir,
                min_points=10,
                min_distance=20.0,
            )
            if path:
                label = "Auto saved" if auto else "Saved"
                self.status_var.set(f"{label} lap: {path}")

        self.live_lap = LapData("Live")
        self._live_cache = None

    def _handle_frame(self, frame):
        if not self.recording:
            return

        try:
            d = float(frame.get("distance"))
        except (TypeError, ValueError):
            return

        if not math.isfinite(d):
            return

        last = self.live_lap.last_distance

        if last is not None and last > 20.0 and d < last - self.reset_drop:
            self._finalize_live_lap(auto=True)

        self.live_lap.append(frame)
        self._live_cache = None

    def _poll_queue(self):
        if self._closed:
            return

        try:
            processed = 0
            while processed < 1000:
                frame = self.queue.get_nowait()
                processed += 1
                self._handle_frame(frame)

        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            if not self._closed:
                self.root.after(self.POLL_MS, self._poll_queue)

    def _refresh_tick(self):
        if self._closed:
            return

        try:
            self._redraw()
        except Exception:
            traceback.print_exc()
        finally:
            if not self._closed:
                self.root.after(self.REFRESH_MS, self._refresh_tick)

    def _redraw(self):
        if self._closed:
            return

        self.crosshair.invalidate()
        self._update_lines()

        if self.autoscale_var.get():
            self._autoscale()

        self.canvas.draw()

        if self.mouse_inside and self.last_mouse_x is not None:
            self.crosshair.show(self.last_mouse_x)
            self.info_var.set(self.format_info(self.last_mouse_x))

        self._update_status()
        self._update_button_states()

    def _update_lines(self):
        if self.aligned is not None:
            grid = self.aligned["distance"]

            set_line(self.speed_lines["A"], grid, self.aligned["A_speed"])
            set_line(self.speed_lines["B"], grid, self.aligned["B_speed"])

            set_line(self.pedal_lines["A_th"], grid, self.aligned["A_throttle"])
            set_line(self.pedal_lines["A_br"], grid, self.aligned["A_brake"])
            set_line(self.pedal_lines["B_th"], grid, self.aligned["B_throttle"])
            set_line(self.pedal_lines["B_br"], grid, self.aligned["B_brake"])

        else:
            if self.lap_a is not None:
                d, _, sp, th, br = self.lap_a.sanitized()
                set_line(self.speed_lines["A"], d, sp)
                set_line(self.pedal_lines["A_th"], d, th)
                set_line(self.pedal_lines["A_br"], d, br)
            else:
                set_line(self.speed_lines["A"], None, None)
                set_line(self.pedal_lines["A_th"], None, None)
                set_line(self.pedal_lines["A_br"], None, None)

            if self.lap_b is not None:
                d, _, sp, th, br = self.lap_b.sanitized()
                set_line(self.speed_lines["B"], d, sp)
                set_line(self.pedal_lines["B_th"], d, th)
                set_line(self.pedal_lines["B_br"], d, br)
            else:
                set_line(self.speed_lines["B"], None, None)
                set_line(self.pedal_lines["B_th"], None, None)
                set_line(self.pedal_lines["B_br"], None, None)

        live = self._live_arrays()
        if live is not None:
            d, sp, th, br = live
            set_line(self.speed_lines["Live"], d, sp)
            set_line(self.pedal_lines["Live_th"], d, th)
            set_line(self.pedal_lines["Live_br"], d, br)
        else:
            set_line(self.speed_lines["Live"], None, None)
            set_line(self.pedal_lines["Live_th"], None, None)
            set_line(self.pedal_lines["Live_br"], None, None)

    @staticmethod
    def _collect_bounds(line, xmins, xmaxs, ymins, ymaxs):
        if not line.get_visible():
            return

        xd = line.get_xdata()
        if len(xd) == 0:
            return

        xd = np.asarray(xd, dtype=float)
        maskx = np.isfinite(xd)

        if not np.any(maskx):
            return

        xmins.append(float(np.min(xd[maskx])))
        xmaxs.append(float(np.max(xd[maskx])))

        if ymins is not None and ymaxs is not None:
            yd = np.asarray(line.get_ydata(), dtype=float)
            masky = np.isfinite(yd)
            if np.any(masky):
                ymins.append(float(np.min(yd[masky])))
                ymaxs.append(float(np.max(yd[masky])))

    def _autoscale(self):
        xmins = []
        xmaxs = []
        ymins = []
        ymaxs = []

        for line in self.speed_lines.values():
            self._collect_bounds(line, xmins, xmaxs, ymins, ymaxs)

        for line in self.pedal_lines.values():
            self._collect_bounds(line, xmins, xmaxs, None, None)

        if xmins:
            xmin = min(xmins)
            xmax = max(xmaxs)
            if xmax - xmin < 1e-6:
                xmax = xmin + 10.0
            pad = max(1.0, (xmax - xmin) * 0.02)
            self.ax_speed.set_xlim(xmin - pad, xmax + pad)
            self.ax_pedals.set_xlim(xmin - pad, xmax + pad)
        else:
            self.ax_speed.set_xlim(0.0, 100.0)
            self.ax_pedals.set_xlim(0.0, 100.0)

        if ymins:
            ymin = min(ymins)
            ymax = max(ymaxs)
            if ymax - ymin < 1e-6:
                ymax = ymin + 10.0
            pad = max(1.0, (ymax - ymin) * 0.08)
            self.ax_speed.set_ylim(ymin - pad, ymax + pad)
        else:
            self.ax_speed.set_ylim(0.0, 150.0)

        self.ax_pedals.set_ylim(-0.05, 1.05)

    def _update_status(self):
        if self.receiver is not None and self.receiver.is_alive():
            if self.receiver.error:
                udp_state = "error"
            elif self.receiver.bound:
                udp_state = f"listening on {self.args.port}"
            else:
                udp_state = "starting"
        else:
            udp_state = "stopped"

        if self.sender is not None and self.sender.is_alive():
            if self.sender.error:
                mock_state = "error"
            else:
                mock_state = f"running -> {self.args.mock_target}:{self.args.port}"
        else:
            mock_state = "stopped"

        record_state = "ON" if self.recording else "OFF"
        live_points = len(self.live_lap)
        last_dist = self.live_lap.last_distance

        parts = [
            f"UDP: {udp_state}",
            f"Mock: {mock_state}",
            f"Record: {record_state}",
            f"Live pts: {live_points}",
        ]

        if last_dist is not None:
            parts.append(f"Live dist: {last_dist:.1f} m")

        parts.append(f"Queue: {self.queue.qsize()}")

        if self.receiver is not None:
            parts.append(f"RX: {self.receiver.received}")

        if self.sender is not None:
            parts.append(f"TX: {self.sender.sent}")

        self.status_var.set(" | ".join(parts))

    def _update_button_states(self):
        try:
            mock_running = self.sender is not None and self.sender.is_alive()
            self.btn_mock.configure(text="Stop Mock" if mock_running else "Start Mock")
            self.btn_record.configure(text="Start Recording" if not self.recording else "Stop Recording")
        except Exception:
            pass

    def _compute_live_arrays(self):
        lap = self.live_lap

        if len(lap) < 2:
            return None

        try:
            d = np.asarray(lap.distance, dtype=float)
            sp = np.asarray(lap.speed, dtype=float)
            th = np.asarray(lap.throttle, dtype=float)
            br = np.asarray(lap.brake, dtype=float)

            mask = np.isfinite(d) & np.isfinite(sp) & np.isfinite(th) & np.isfinite(br)

            if int(mask.sum()) < 2:
                return None

            d = d[mask]
            sp = sp[mask]
            th = th[mask]
            br = br[mask]

            if not np.all(d[1:] >= d[:-1]):
                order = np.argsort(d, kind="mergesort")
                d = d[order]
                sp = sp[order]
                th = th[order]
                br = br[order]

            if d.size >= 2 and np.any(np.diff(d) == 0.0):
                uniq, idx = np.unique(d, return_index=True)
                d = uniq
                sp = sp[idx]
                th = th[idx]
                br = br[idx]

            if d.size < 2:
                return None

            return d, sp, th, br

        except Exception:
            return None

    def _live_arrays(self):
        version = len(self.live_lap)

        if self._live_cache is not None and self._live_cache[0] == version:
            return self._live_cache[1]

        arrays = self._compute_live_arrays()
        self._live_cache = (version, arrays)
        return arrays

    def _live_value_at(self, x):
        arrays = self._live_arrays()
        if arrays is None:
            return None

        d, sp, th, br = arrays

        try:
            x = float(x)
        except (TypeError, ValueError):
            return None

        if not math.isfinite(x) or x < d[0] or x > d[-1]:
            return None

        return {
            "distance": x,
            "speed": float(np.interp(x, d, sp)),
            "throttle": float(np.interp(x, d, th)),
            "brake": float(np.interp(x, d, br)),
        }

    def _on_motion(self, event):
        if event.inaxes not in (self.ax_speed, self.ax_pedals):
            return

        if event.xdata is None:
            return

        try:
            self.last_mouse_x = float(event.xdata)
        except (TypeError, ValueError):
            return

        self.mouse_inside = True
        self.crosshair.show(self.last_mouse_x)
        self.info_var.set(self.format_info(self.last_mouse_x))

    def _on_leave(self, event):
        self.mouse_inside = False
        self.last_mouse_x = None
        self.crosshair.hide()
        self.info_var.set("Move mouse over plots to inspect values.")

    @staticmethod
    def _f(value, precision=1):
        try:
            value = float(value)
            if math.isfinite(value):
                return f"{value:.{precision}f}"
        except (TypeError, ValueError):
            pass
        return "--"

    def format_info(self, x):
        try:
            x = float(x)
        except (TypeError, ValueError):
            return ""

        if not math.isfinite(x):
            return ""

        # Preferred mode: aligned Lap A vs Lap B.
        if self.aligned is not None:
            grid = self.aligned["distance"]

            if grid.size >= 2 and grid[0] <= x <= grid[-1]:
                sa = float(np.interp(x, grid, self.aligned["A_speed"]))
                sb = float(np.interp(x, grid, self.aligned["B_speed"]))

                ta = float(np.interp(x, grid, self.aligned["A_throttle"]))
                tb = float(np.interp(x, grid, self.aligned["B_throttle"]))

                ba = float(np.interp(x, grid, self.aligned["A_brake"]))
                bb = float(np.interp(x, grid, self.aligned["B_brake"]))

                return (
                    f"Distance: {x:.1f} m | "
                    f"Speed A: {self._f(sa, 1)}, B: {self._f(sb, 1)}, Δ(B-A): {self._f(sb - sa, 1)} km/h | "
                    f"Throttle A: {self._f(ta, 2)}, B: {self._f(tb, 2)}, Δ: {self._f(tb - ta, 2)} | "
                    f"Brake A: {self._f(ba, 2)}, B: {self._f(bb, 2)}, Δ: {self._f(bb - ba, 2)}"
                )

        # Live vs Lap A.
        if self.lap_a is not None and len(self.live_lap) >= 2:
            va = self.lap_a.value_at(x)
            vl = self._live_value_at(x)

            if va is not None and vl is not None:
                return (
                    f"Distance: {x:.1f} m | "
                    f"Speed Live: {self._f(vl['speed'], 1)}, A: {self._f(va['speed'], 1)}, Δ(Live-A): {self._f(vl['speed'] - va['speed'], 1)} km/h | "
                    f"Throttle Live: {self._f(vl['throttle'], 2)}, A: {self._f(va['throttle'], 2)}, Δ: {self._f(vl['throttle'] - va['throttle'], 2)} | "
                    f"Brake Live: {self._f(vl['brake'], 2)}, A: {self._f(va['brake'], 2)}, Δ: {self._f(vl['brake'] - va['brake'], 2)}"
                )

        # Live vs Lap B.
        if self.lap_b is not None and len(self.live_lap) >= 2:
            vb = self.lap_b.value_at(x)
            vl = self._live_value_at(x)

            if vb is not None and vl is not None:
                return (
                    f"Distance: {x:.1f} m | "
                    f"Speed Live: {self._f(vl['speed'], 1)}, B: {self._f(vb['speed'], 1)}, Δ(Live-B): {self._f(vl['speed'] - vb['speed'], 1)} km/h | "
                    f"Throttle Live: {self._f(vl['throttle'], 2)}, B: {self._f(vb['throttle'], 2)}, Δ: {self._f(vl['throttle'] - vb['throttle'], 2)} | "
                    f"Brake Live: {self._f(vl['brake'], 2)}, B: {self._f(vb['brake'], 2)}, Δ: {self._f(vl['brake'] - vb['brake'], 2)}"
                )

        # Single source inspection.
        for lap, label in ((self.lap_a, "Lap A"), (self.lap_b, "Lap B")):
            if lap is not None:
                v = lap.value_at(x)
                if v is not None:
                    return (
                        f"Distance: {x:.1f} m | {label} | "
                        f"Speed: {self._f(v['speed'], 1)} km/h | "
                        f"Throttle: {self._f(v['throttle'], 2)} | "
                        f"Brake: {self._f(v['brake'], 2)}"
                    )

        live = self._live_value_at(x)
        if live is not None:
            return (
                f"Distance: {x:.1f} m | Live | "
                f"Speed: {self._f(live['speed'], 1)} km/h | "
                f"Throttle: {self._f(live['throttle'], 2)} | "
                f"Brake: {self._f(live['brake'], 2)}"
            )

        return f"Distance: {x:.1f} m | no data at cursor"

    def _on_close(self):
        self._closed = True

        try:
            if self.sender is not None:
                self.sender.stop()
                self.sender.join(timeout=0.5)
        except Exception:
            pass

        try:
            if self.receiver is not None:
                self.receiver.stop()
                self.receiver.join(timeout=0.5)
        except Exception:
            pass

        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Real-time RAC telemetry receiver, logger, and distance-based lap comparison tool."
    )

    parser.add_argument("--port", type=int, default=30000, help="UDP listen port, default 30000")
    parser.add_argument("--bind", default="0.0.0.0", help="UDP bind address, default 0.0.0.0")
    parser.add_argument("--log-dir", default="telemetry_logs", help="CSV output directory")
    parser.add_argument("--reset-drop", type=float, default=5.0, help="Distance drop in meters that triggers lap reset detection")

    parser.add_argument("--no-mock", action="store_true", help="Do not start the built-in mock UDP sender")
    parser.add_argument("--mock-target", default="127.0.0.1", help="Mock sender destination address")
    parser.add_argument("--mock-interval", type=float, default=0.02, help="Mock sender interval in seconds, default 0.02")
    parser.add_argument("--lap-length", type=float, default=1200.0, help="Mock lap length in meters, default 1200")

    args = parser.parse_args()

    root = tk.Tk()
    TelemetryApp(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()