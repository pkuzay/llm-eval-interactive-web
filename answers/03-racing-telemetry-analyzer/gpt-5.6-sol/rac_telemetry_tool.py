#!/usr/bin/env python3
"""
RAC Telemetry Tool - single-file UDP logger and distance-aligned lap comparer.

Dependencies:
    Python 3.9+, numpy, matplotlib (Tkinter is included with most Python builds)

Run:
    python rac_telemetry_tool.py
    python rac_telemetry_tool.py --port 30000 --output-dir telemetry_laps
    python rac_telemetry_tool.py --no-mock
    python rac_telemetry_tool.py --self-test

UDP JSON frame format (UTF-8):
    {"timestamp": 1720000000.0, "distance": 123.4, "speed": 141.2,
     "throttle": 0.82, "brake": 0.0}
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import select
import socket
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np


CSV_FIELDS = ("timestamp", "distance", "speed", "throttle", "brake")


@dataclass(frozen=True)
class TelemetrySample:
    timestamp: float
    distance: float
    speed: float
    throttle: float
    brake: float

    @classmethod
    def from_mapping(cls, obj: Dict[str, object]) -> "TelemetrySample":
        try:
            values = {name: float(obj[name]) for name in CSV_FIELDS}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"missing or invalid telemetry field: {exc}") from exc
        if not all(math.isfinite(v) for v in values.values()):
            raise ValueError("telemetry contains NaN or infinity")
        if values["timestamp"] < 0 or values["distance"] < 0 or values["speed"] < 0:
            raise ValueError("timestamp, distance, and speed must be non-negative")
        # Mildly tolerant bounds make real devices usable while rejecting corrupt frames.
        if values["speed"] > 1500 or values["distance"] > 100_000_000:
            raise ValueError("speed or distance is outside a plausible range")
        values["throttle"] = min(1.0, max(0.0, values["throttle"]))
        values["brake"] = min(1.0, max(0.0, values["brake"]))
        return cls(**values)

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> "TelemetrySample":
        if len(payload) > 64 * 1024:
            raise ValueError("UDP datagram is too large")
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid UTF-8 JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError("telemetry JSON must be an object")
        return cls.from_mapping(obj)

    def as_row(self) -> Dict[str, float]:
        return {name: getattr(self, name) for name in CSV_FIELDS}


@dataclass
class LapData:
    name: str
    timestamp: np.ndarray
    distance: np.ndarray
    speed: np.ndarray
    throttle: np.ndarray
    brake: np.ndarray
    source: Optional[Path] = None

    def __len__(self) -> int:
        return int(self.distance.size)

    @classmethod
    def from_samples(
        cls, name: str, samples: Sequence[TelemetrySample], source: Optional[Path] = None
    ) -> "LapData":
        if not samples:
            raise ValueError("a lap must contain at least one sample")
        matrix = np.asarray(
            [[getattr(s, field) for field in CSV_FIELDS] for s in samples], dtype=float
        )
        return cls(
            name=name,
            timestamp=matrix[:, 0],
            distance=matrix[:, 1],
            speed=matrix[:, 2],
            throttle=matrix[:, 3],
            brake=matrix[:, 4],
            source=source,
        )

    @classmethod
    def from_csv(cls, path: Path) -> "LapData":
        rows: List[TelemetrySample] = []
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError("CSV has no header")
                missing = [field for field in CSV_FIELDS if field not in reader.fieldnames]
                if missing:
                    raise ValueError(f"CSV missing columns: {', '.join(missing)}")
                for line_no, row in enumerate(reader, start=2):
                    try:
                        rows.append(TelemetrySample.from_mapping(row))
                    except ValueError as exc:
                        raise ValueError(f"line {line_no}: {exc}") from exc
        except OSError as exc:
            raise OSError(f"cannot read {path}: {exc}") from exc
        if len(rows) < 2:
            raise ValueError("CSV must contain at least two valid samples")
        return cls.from_samples(path.stem, rows, source=path)

    def elapsed_seconds(self) -> float:
        if len(self) < 2:
            return 0.0
        return max(0.0, float(self.timestamp[-1] - self.timestamp[0]))


@dataclass
class AlignedLaps:
    distance: np.ndarray
    a: Dict[str, np.ndarray]
    b: Dict[str, np.ndarray]
    step_m: float


def _collapse_duplicate_distances(lap: LapData) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
    """Sort by distance and average values at duplicate distance coordinates."""
    arrays = {
        "speed": np.asarray(lap.speed, dtype=float),
        "throttle": np.asarray(lap.throttle, dtype=float),
        "brake": np.asarray(lap.brake, dtype=float),
    }
    distance = np.asarray(lap.distance, dtype=float)
    valid = np.isfinite(distance)
    for arr in arrays.values():
        valid &= np.isfinite(arr)
    distance = distance[valid]
    if distance.size < 2:
        raise ValueError(f"{lap.name} has fewer than two finite samples")
    order = np.argsort(distance, kind="stable")
    distance = distance[order]
    arrays = {name: arr[valid][order] for name, arr in arrays.items()}
    unique, inverse, counts = np.unique(distance, return_inverse=True, return_counts=True)
    if unique.size < 2:
        raise ValueError(f"{lap.name} does not span a measurable distance")
    collapsed: Dict[str, np.ndarray] = {}
    for name, arr in arrays.items():
        sums = np.bincount(inverse, weights=arr, minlength=unique.size)
        collapsed[name] = sums / counts
    return unique, collapsed


def align_laps_by_distance(lap_a: LapData, lap_b: LapData, step_m: float = 1.0) -> AlignedLaps:
    """Interpolate two laps onto one distance grid over their shared track range."""
    if not math.isfinite(step_m) or step_m <= 0:
        raise ValueError("distance step must be a positive finite number")
    da, channels_a = _collapse_duplicate_distances(lap_a)
    db, channels_b = _collapse_duplicate_distances(lap_b)
    start = max(float(da[0]), float(db[0]))
    end = min(float(da[-1]), float(db[-1]))
    if end <= start:
        raise ValueError("laps have no overlapping distance range")
    # Include the shared end point and cap excessive grids to protect the UI.
    count = int(math.floor((end - start) / step_m)) + 1
    if count < 2:
        grid = np.asarray([start, end], dtype=float)
    else:
        if count > 2_000_000:
            raise ValueError("distance grid would exceed 2,000,000 points; increase the step")
        grid = start + np.arange(count, dtype=float) * step_m
        if end - grid[-1] > step_m * 1e-9:
            grid = np.append(grid, end)
    aligned_a = {name: np.interp(grid, da, arr) for name, arr in channels_a.items()}
    aligned_b = {name: np.interp(grid, db, arr) for name, arr in channels_b.items()}
    return AlignedLaps(grid, aligned_a, aligned_b, step_m)


def save_lap_csv(samples: Sequence[TelemetrySample], output_dir: Path) -> Path:
    """Atomically save one lap, preserving the requested timestamp naming convention."""
    if not samples:
        raise ValueError("cannot save an empty lap")
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = output_dir / f"lap_{stamp}.csv"
    sequence = 1
    while candidate.exists():
        candidate = output_dir / f"lap_{stamp}_{sequence:03d}.csv"
        sequence += 1
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", dir=output_dir, delete=False,
            prefix=".lap_", suffix=".tmp"
        ) as handle:
            temp_name = handle.name
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for sample in samples:
                writer.writerow(sample.as_row())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, candidate)
        return candidate
    except Exception:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise


class UDPReceiver(threading.Thread):
    def __init__(
        self,
        host: str,
        port: int,
        output_queue: "queue.Queue[TelemetrySample]",
        stop_event: threading.Event,
        error_callback: Callable[[str], None],
    ) -> None:
        super().__init__(name="UDPReceiver", daemon=False)
        self.host = host
        self.port = port
        self.output_queue = output_queue
        self.stop_event = stop_event
        self.error_callback = error_callback
        self.socket: Optional[socket.socket] = None
        self.received = 0
        self.invalid = 0
        self.dropped = 0

    def run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket = sock
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.setblocking(False)
            while not self.stop_event.is_set():
                try:
                    ready, _, _ = select.select([sock], [], [], 0.1)
                    if not ready:
                        continue
                    # Drain all currently available datagrams per wake-up.
                    while not self.stop_event.is_set():
                        try:
                            payload, _address = sock.recvfrom(65535)
                        except BlockingIOError:
                            break
                        try:
                            sample = TelemetrySample.from_json_bytes(payload)
                        except ValueError:
                            self.invalid += 1
                            continue
                        try:
                            self.output_queue.put_nowait(sample)
                        except queue.Full:
                            # Prefer fresh telemetry to stale UI data under overload.
                            try:
                                self.output_queue.get_nowait()
                                self.output_queue.put_nowait(sample)
                                self.dropped += 1
                            except (queue.Empty, queue.Full):
                                self.dropped += 1
                        self.received += 1
                except (OSError, ValueError) as exc:
                    if not self.stop_event.is_set():
                        self.error_callback(f"UDP receive error: {exc}")
                        time.sleep(0.1)
        except OSError as exc:
            self.error_callback(f"cannot listen on UDP {self.host}:{self.port}: {exc}")
        except Exception as exc:
            self.error_callback(f"unexpected receiver failure: {exc}\n{traceback.format_exc()}")
        finally:
            if self.socket is not None:
                try:
                    self.socket.close()
                except OSError:
                    pass
                self.socket = None

    def close(self) -> None:
        self.stop_event.set()
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass


class MockTelemetrySender(threading.Thread):
    """A deterministic-ish 50 Hz source that resets distance at each mock lap."""

    def __init__(self, host: str, port: int, stop_event: threading.Event) -> None:
        super().__init__(name="MockTelemetrySender", daemon=False)
        self.host = host
        self.port = port
        self.stop_event = stop_event
        self.socket: Optional[socket.socket] = None
        self.lap_length_m = 1200.0

    def run(self) -> None:
        distance = 0.0
        lap_index = 0
        period = 0.020
        next_tick = time.monotonic()
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            while not self.stop_event.is_set():
                phase = 2.0 * math.pi * distance / self.lap_length_m
                # Several harmonics create straights, corners, braking and acceleration zones.
                target_speed = 150.0 + 52.0 * math.sin(phase - 0.25) + 24.0 * math.sin(3 * phase + 0.8)
                target_speed += 4.0 * math.sin(lap_index * 1.7 + phase * 0.5)
                speed = max(45.0, min(245.0, target_speed))
                slope = 52.0 * math.cos(phase - 0.25) + 72.0 * math.cos(3 * phase + 0.8)
                throttle = min(1.0, max(0.0, 0.55 + slope / 115.0))
                brake = min(1.0, max(0.0, -slope / 100.0))
                if brake > 0.05:
                    throttle *= 0.08
                sample = {
                    "timestamp": time.time(),
                    "distance": distance,
                    "speed": speed,
                    "throttle": throttle,
                    "brake": brake,
                }
                payload = json.dumps(sample, separators=(",", ":")).encode("utf-8")
                try:
                    self.socket.sendto(payload, (self.host, self.port))
                except OSError:
                    if not self.stop_event.is_set():
                        time.sleep(0.05)
                distance += speed / 3.6 * period
                if distance >= self.lap_length_m:
                    distance = 0.0
                    lap_index += 1
                next_tick += period
                delay = next_tick - time.monotonic()
                if delay > 0:
                    self.stop_event.wait(delay)
                else:
                    next_tick = time.monotonic()
        finally:
            if self.socket is not None:
                try:
                    self.socket.close()
                except OSError:
                    pass
                self.socket = None


class TelemetryRecorder:
    def __init__(self, output_dir: Path, minimum_lap_samples: int = 20) -> None:
        self.output_dir = output_dir
        self.minimum_lap_samples = minimum_lap_samples
        self.current: List[TelemetrySample] = []
        self.last_saved: Optional[LapData] = None

    @staticmethod
    def is_reset(previous: TelemetrySample, current: TelemetrySample) -> bool:
        # A reset must be a substantial backwards jump, avoiding small sensor jitter.
        return previous.distance >= 30.0 and (
            current.distance <= 5.0 or current.distance < previous.distance - 25.0
        )

    def add(self, sample: TelemetrySample) -> Optional[Tuple[LapData, Path]]:
        completed: Optional[Tuple[LapData, Path]] = None
        if self.current and self.is_reset(self.current[-1], sample):
            completed = self.finish("automatic distance reset")
        self.current.append(sample)
        return completed

    def finish(self, reason: str = "manual") -> Optional[Tuple[LapData, Path]]:
        if len(self.current) < self.minimum_lap_samples:
            self.current.clear()
            return None
        samples = self.current
        self.current = []
        try:
            path = save_lap_csv(samples, self.output_dir)
            lap = LapData.from_samples(path.stem, samples, source=path)
            self.last_saved = lap
            return lap, path
        except Exception:
            # A transient disk failure must not silently discard a completed lap.
            self.current = samples + self.current
            raise


class TelemetryApp:
    UI_POLL_MS = 20
    PLOT_REFRESH_SECONDS = 0.12
    MAX_QUEUE_DRAIN = 5000

    def __init__(self, root: "object", host: str, port: int, output_dir: Path, mock: bool) -> None:
        # Imports are delayed so --self-test works on headless machines.
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.figure import Figure

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.root = root
        self.host = host
        self.port = port
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.queue: "queue.Queue[TelemetrySample]" = queue.Queue(maxsize=20_000)
        self.receiver_stop = threading.Event()
        self.receiver = UDPReceiver(host, port, self.queue, self.receiver_stop, self._thread_error)
        self.mock_stop = threading.Event()
        self.mock_sender: Optional[MockTelemetrySender] = None
        self.recorder = TelemetryRecorder(output_dir)
        self.lap_a: Optional[LapData] = None
        self.lap_b: Optional[LapData] = None
        self.aligned: Optional[AlignedLaps] = None
        self.closing = False
        self.last_plot_time = 0.0
        self.pending_errors: "queue.Queue[str]" = queue.Queue()

        root.title("RAC Telemetry — Real-time Logger & Lap Comparison")
        root.geometry("1260x820")
        root.minsize(900, 650)
        root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        controls = ttk.Frame(root, padding=(10, 8))
        controls.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(controls, text="Load Lap A", command=lambda: self.load_lap("a")).pack(side=tk.LEFT, padx=3)
        ttk.Button(controls, text="Load Lap B", command=lambda: self.load_lap("b")).pack(side=tk.LEFT, padx=3)
        ttk.Button(controls, text="Use Current as A", command=lambda: self.use_current("a")).pack(side=tk.LEFT, padx=3)
        ttk.Button(controls, text="Use Current as B", command=lambda: self.use_current("b")).pack(side=tk.LEFT, padx=3)
        ttk.Button(controls, text="Finish / Save Lap", command=self.finish_lap).pack(side=tk.LEFT, padx=3)
        self.mock_button = ttk.Button(controls, command=self.toggle_mock)
        self.mock_button.pack(side=tk.LEFT, padx=(14, 3))

        ttk.Label(controls, text="Grid step (m):").pack(side=tk.LEFT, padx=(14, 3))
        self.step_var = tk.StringVar(value="1.0")
        step_entry = ttk.Entry(controls, textvariable=self.step_var, width=7)
        step_entry.pack(side=tk.LEFT)
        step_entry.bind("<Return>", lambda _event: self.rebuild_comparison())
        ttk.Button(controls, text="Apply", command=self.rebuild_comparison).pack(side=tk.LEFT, padx=3)

        info = ttk.Frame(root, padding=(10, 0, 10, 5))
        info.pack(side=tk.TOP, fill=tk.X)
        self.status_var = tk.StringVar(value=f"Listening on UDP {host}:{port}")
        ttk.Label(info, textvariable=self.status_var).pack(side=tk.LEFT)
        self.cursor_var = tk.StringVar(value="Move over a chart to inspect distance-aligned values")
        ttk.Label(info, textvariable=self.cursor_var, anchor=tk.E).pack(side=tk.RIGHT, fill=tk.X, expand=True)

        self.figure = Figure(figsize=(11.5, 7.0), dpi=100, constrained_layout=True)
        self.ax_speed = self.figure.add_subplot(211)
        self.ax_pedals = self.figure.add_subplot(212, sharex=self.ax_speed)
        self.canvas = FigureCanvasTkAgg(self.figure, master=root)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, root, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side=tk.BOTTOM, fill=tk.X)

        self.crosshair_speed = self.ax_speed.axvline(0, color="#666666", lw=0.9, ls="--", visible=False)
        self.crosshair_pedals = self.ax_pedals.axvline(0, color="#666666", lw=0.9, ls="--", visible=False)
        self.canvas.mpl_connect("motion_notify_event", self.on_mouse_move)
        self.canvas.mpl_connect("figure_leave_event", self.on_mouse_leave)

        self._draw_empty_axes()
        self.receiver.start()
        if mock:
            root.after(150, self.start_mock)
        self._update_mock_button()
        root.after(self.UI_POLL_MS, self.poll_queue)

    def _thread_error(self, message: str) -> None:
        self.pending_errors.put(message)

    def _draw_empty_axes(self) -> None:
        for ax in (self.ax_speed, self.ax_pedals):
            ax.grid(True, alpha=0.25)
            ax.margins(x=0.01)
        self.ax_speed.set_title("Speed vs Distance")
        self.ax_speed.set_ylabel("Speed (km/h)")
        self.ax_pedals.set_title("Pedals vs Distance")
        self.ax_pedals.set_ylabel("Input")
        self.ax_pedals.set_xlabel("Distance (m)")
        self.ax_pedals.set_ylim(-0.04, 1.04)
        self.canvas.draw_idle()

    def _update_mock_button(self) -> None:
        running = self.mock_sender is not None and self.mock_sender.is_alive()
        self.mock_button.configure(text="Stop Mock" if running else "Start Mock")

    def start_mock(self) -> None:
        if self.closing or (self.mock_sender is not None and self.mock_sender.is_alive()):
            return
        self.mock_stop = threading.Event()
        target_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
        self.mock_sender = MockTelemetrySender(target_host, self.port, self.mock_stop)
        self.mock_sender.start()
        self._update_mock_button()
        self.status_var.set(f"Mock 50 Hz → UDP {self.port}; recording")

    def stop_mock(self) -> None:
        if self.mock_sender is None:
            return
        self.mock_stop.set()
        self.mock_sender.join(timeout=1.0)
        self.mock_sender = None
        self._update_mock_button()
        if not self.closing:
            self.status_var.set(f"Listening on UDP {self.host}:{self.port}; mock stopped")

    def toggle_mock(self) -> None:
        if self.mock_sender is not None and self.mock_sender.is_alive():
            self.stop_mock()
        else:
            self.start_mock()

    def poll_queue(self) -> None:
        if self.closing:
            return
        processed = 0
        last_completed: Optional[Tuple[LapData, Path]] = None
        try:
            while processed < self.MAX_QUEUE_DRAIN:
                try:
                    sample = self.queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    completed = self.recorder.add(sample)
                    if completed is not None:
                        last_completed = completed
                except Exception as exc:
                    self.pending_errors.put(f"logging error: {exc}")
                processed += 1
            if last_completed is not None:
                lap, path = last_completed
                if self.lap_a is None:
                    self.lap_a = lap
                elif self.lap_b is None or self.lap_b is self.recorder.last_saved:
                    self.lap_b = lap
                else:
                    # After A and B exist, each new completed lap becomes B.
                    self.lap_b = lap
                self.rebuild_comparison(silent=True)
                self.status_var.set(f"Saved {path.name} — {len(lap)} samples, {lap.distance[-1]:.0f} m")
            now = time.monotonic()
            if processed and now - self.last_plot_time >= self.PLOT_REFRESH_SECONDS:
                self.refresh_plot()
                self.last_plot_time = now
            try:
                error = self.pending_errors.get_nowait()
            except queue.Empty:
                error = None
            if error:
                self.status_var.set(error.splitlines()[0])
            self.root.after(self.UI_POLL_MS, self.poll_queue)
        except Exception as exc:
            self.status_var.set(f"UI update error: {exc}")
            self.root.after(250, self.poll_queue)

    def load_lap(self, slot: str) -> None:
        path_text = self.filedialog.askopenfilename(
            title=f"Load Lap {slot.upper()}",
            initialdir=str(self.output_dir),
            filetypes=[("Telemetry CSV", "*.csv"), ("All files", "*.*")],
        )
        if not path_text:
            return
        try:
            lap = LapData.from_csv(Path(path_text))
            if slot == "a":
                self.lap_a = lap
            else:
                self.lap_b = lap
            self.rebuild_comparison()
            self.status_var.set(
                f"Loaded Lap {slot.upper()}: {Path(path_text).name} — {len(lap)} samples, "
                f"{lap.distance[-1] - lap.distance[0]:.0f} m"
            )
        except Exception as exc:
            self.messagebox.showerror("Load failed", str(exc))

    def use_current(self, slot: str) -> None:
        if len(self.recorder.current) < 2:
            self.messagebox.showinfo("No data", "The current lap has fewer than two samples.")
            return
        try:
            lap = LapData.from_samples("Current live lap", list(self.recorder.current))
            if slot == "a":
                self.lap_a = lap
            else:
                self.lap_b = lap
            self.rebuild_comparison(silent=True)
            self.refresh_plot()
            self.status_var.set(f"Current live lap assigned to slot {slot.upper()}")
        except Exception as exc:
            self.messagebox.showerror("Cannot use current lap", str(exc))

    def finish_lap(self) -> None:
        try:
            result = self.recorder.finish("manual")
            if result is None:
                self.status_var.set("Lap not saved: fewer than 20 samples")
                return
            lap, path = result
            if self.lap_a is None:
                self.lap_a = lap
            else:
                self.lap_b = lap
            self.rebuild_comparison(silent=True)
            self.refresh_plot()
            self.status_var.set(f"Saved {path.name}")
        except Exception as exc:
            self.messagebox.showerror("Save failed", str(exc))

    def rebuild_comparison(self, silent: bool = False) -> None:
        self.aligned = None
        if self.lap_a is None or self.lap_b is None:
            self.refresh_plot()
            if not silent:
                self.status_var.set("Load or assign both Lap A and Lap B to compare them")
            return
        try:
            step = float(self.step_var.get())
            self.aligned = align_laps_by_distance(self.lap_a, self.lap_b, step)
            self.refresh_plot()
            if not silent:
                self.status_var.set(
                    f"Aligned {self.lap_a.name} and {self.lap_b.name}: "
                    f"{self.aligned.distance[0]:.1f}–{self.aligned.distance[-1]:.1f} m, step {step:g} m"
                )
        except Exception as exc:
            if not silent:
                self.messagebox.showerror("Alignment failed", str(exc))
            else:
                self.status_var.set(f"Alignment failed: {exc}")
            self.refresh_plot()

    @staticmethod
    def _decimate(x: np.ndarray, *ys: np.ndarray, limit: int = 8000) -> Tuple[np.ndarray, ...]:
        if x.size <= limit:
            return (x, *ys)
        stride = int(math.ceil(x.size / limit))
        return (x[::stride], *(y[::stride] for y in ys))

    def refresh_plot(self) -> None:
        # Clear and redraw keeps state predictable; at ~8 Hz and decimated lines it remains responsive.
        self.ax_speed.clear()
        self.ax_pedals.clear()
        self._draw_empty_axes()
        blue, red, green = "#1769E0", "#D9363E", "#159B72"

        if self.aligned is not None:
            d, sa, sb = self._decimate(
                self.aligned.distance, self.aligned.a["speed"], self.aligned.b["speed"]
            )
            self.ax_speed.plot(d, sa, color=blue, lw=1.6, label=f"Lap A — {self.lap_a.name}")
            self.ax_speed.plot(d, sb, color=red, lw=1.6, label=f"Lap B — {self.lap_b.name}")
            d, ta, ba, tb, bb = self._decimate(
                self.aligned.distance,
                self.aligned.a["throttle"], self.aligned.a["brake"],
                self.aligned.b["throttle"], self.aligned.b["brake"],
            )
            self.ax_pedals.plot(d, ta, color=blue, lw=1.45, label="A throttle")
            self.ax_pedals.plot(d, ba, color=blue, lw=1.25, ls="--", label="A brake")
            self.ax_pedals.plot(d, tb, color=red, lw=1.45, label="B throttle")
            self.ax_pedals.plot(d, bb, color=red, lw=1.25, ls="--", label="B brake")
        else:
            for lap, color, label in ((self.lap_a, blue, "Lap A"), (self.lap_b, red, "Lap B")):
                if lap is not None:
                    d, speed, throttle, brake = self._decimate(
                        lap.distance, lap.speed, lap.throttle, lap.brake
                    )
                    self.ax_speed.plot(d, speed, color=color, lw=1.5, label=f"{label} — {lap.name}")
                    self.ax_pedals.plot(d, throttle, color=color, lw=1.4, label=f"{label} throttle")
                    self.ax_pedals.plot(d, brake, color=color, lw=1.2, ls="--", label=f"{label} brake")

        if self.recorder.current:
            live = self.recorder.current
            # Avoid rebuilding a large NumPy matrix more often than needed.
            stride = max(1, int(math.ceil(len(live) / 8000)))
            subset = live[::stride]
            d = np.fromiter((s.distance for s in subset), dtype=float)
            speed = np.fromiter((s.speed for s in subset), dtype=float)
            throttle = np.fromiter((s.throttle for s in subset), dtype=float)
            brake = np.fromiter((s.brake for s in subset), dtype=float)
            self.ax_speed.plot(d, speed, color=green, lw=1.1, alpha=0.8, label="Current live")
            self.ax_pedals.plot(d, throttle, color=green, lw=1.0, alpha=0.75, label="Live throttle")
            self.ax_pedals.plot(d, brake, color=green, lw=0.9, ls=":", alpha=0.75, label="Live brake")

        if self.ax_speed.lines:
            self.ax_speed.legend(loc="upper right", fontsize=8, framealpha=0.9)
        if self.ax_pedals.lines:
            self.ax_pedals.legend(loc="upper right", fontsize=8, ncol=3, framealpha=0.9)
        self.crosshair_speed = self.ax_speed.axvline(0, color="#555555", lw=0.9, ls="--", visible=False)
        self.crosshair_pedals = self.ax_pedals.axvline(0, color="#555555", lw=0.9, ls="--", visible=False)
        self.canvas.draw_idle()

    def on_mouse_move(self, event: "object") -> None:
        if event.inaxes not in (self.ax_speed, self.ax_pedals) or event.xdata is None:
            self.on_mouse_leave(event)
            return
        x = float(event.xdata)
        self.crosshair_speed.set_xdata([x, x])
        self.crosshair_pedals.set_xdata([x, x])
        self.crosshair_speed.set_visible(True)
        self.crosshair_pedals.set_visible(True)
        if self.aligned is not None:
            idx = int(np.clip(np.searchsorted(self.aligned.distance, x), 0, self.aligned.distance.size - 1))
            if idx > 0 and abs(self.aligned.distance[idx - 1] - x) < abs(self.aligned.distance[idx] - x):
                idx -= 1
            d = self.aligned.distance[idx]
            sa, sb = self.aligned.a["speed"][idx], self.aligned.b["speed"][idx]
            ta, tb = self.aligned.a["throttle"][idx], self.aligned.b["throttle"][idx]
            ba, bb = self.aligned.a["brake"][idx], self.aligned.b["brake"][idx]
            self.cursor_var.set(
                f"{d:.1f} m  |  Speed A {sa:.1f}, B {sb:.1f}, Δ(B−A) {sb-sa:+.1f} km/h  |  "
                f"Throttle A {ta:.2f}, B {tb:.2f}, Δ {tb-ta:+.2f}  |  "
                f"Brake A {ba:.2f}, B {bb:.2f}, Δ {bb-ba:+.2f}"
            )
        else:
            self.cursor_var.set(f"Distance {x:.1f} m — load/assign A and B for aligned differences")
        self.canvas.draw_idle()

    def on_mouse_leave(self, _event: "object") -> None:
        if hasattr(self, "crosshair_speed"):
            self.crosshair_speed.set_visible(False)
            self.crosshair_pedals.set_visible(False)
            self.canvas.draw_idle()

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.status_var.set("Stopping receiver and mock sender…")
        try:
            self.stop_mock()
            self.receiver.close()
            self.receiver.join(timeout=1.5)
            if self.recorder.current:
                try:
                    self.recorder.finish("shutdown")
                except Exception as exc:
                    print(f"Warning: could not save final lap: {exc}", file=sys.stderr)
        finally:
            self.root.destroy()


def _make_test_lap(name: str, distances: np.ndarray, offset: float = 0.0) -> LapData:
    speed = 100.0 + distances * 0.1 + offset
    throttle = np.clip(distances / max(float(distances[-1]), 1.0), 0, 1)
    brake = 1.0 - throttle
    timestamps = 1_700_000_000.0 + np.arange(distances.size) * 0.02
    return LapData(name, timestamps, distances, speed, throttle, brake)


def run_self_test() -> int:
    """Headless algorithm, CSV, parser, and loopback UDP checks."""
    try:
        sample = TelemetrySample.from_json_bytes(
            b'{"timestamp":1,"distance":2,"speed":3,"throttle":1.2,"brake":-0.1}'
        )
        assert sample.throttle == 1.0 and sample.brake == 0.0

        lap_a = _make_test_lap("A", np.asarray([0.0, 2.0, 4.0, 6.0]), 0.0)
        # Unsorted and duplicate coordinates deliberately exercise preprocessing.
        lap_b = _make_test_lap("B", np.asarray([0.0, 1.5, 3.0, 3.0, 6.0]), 5.0)
        aligned = align_laps_by_distance(lap_a, lap_b, step_m=1.0)
        assert np.allclose(aligned.distance, np.arange(7.0))
        assert np.allclose(aligned.b["speed"] - aligned.a["speed"], 5.0, atol=0.2)

        with tempfile.TemporaryDirectory() as temp_dir:
            samples = [
                TelemetrySample(float(i), float(i), 80.0 + i, 0.5, 0.0) for i in range(25)
            ]
            path = save_lap_csv(samples, Path(temp_dir))
            loaded = LapData.from_csv(path)
            assert len(loaded) == len(samples)
            assert np.allclose(loaded.distance, np.arange(25.0))

            recorder = TelemetryRecorder(Path(temp_dir), minimum_lap_samples=5)
            completed = None
            for i in range(8):
                completed = recorder.add(
                    TelemetrySample(10.0 + i, float(i * 10), 90.0, 0.7, 0.0)
                ) or completed
            reset_result = recorder.add(TelemetrySample(18.0, 0.0, 70.0, 0.2, 0.4))
            assert reset_result is not None and reset_result[1].exists()
            assert len(reset_result[0]) == 8 and len(recorder.current) == 1

        data_queue: "queue.Queue[TelemetrySample]" = queue.Queue(maxsize=20)
        stop = threading.Event()
        errors: List[str] = []
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
        probe.close()
        receiver = UDPReceiver("127.0.0.1", port, data_queue, stop, errors.append)
        receiver.start()
        time.sleep(0.05)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(
            b'{"timestamp":1,"distance":2,"speed":3,"throttle":0.4,"brake":0.1}',
            ("127.0.0.1", port),
        )
        received = data_queue.get(timeout=1.0)
        sender.close()
        receiver.close()
        receiver.join(timeout=1.0)
        assert received.distance == 2.0 and not errors and not receiver.is_alive()
        print("Self-test passed: parser, distance alignment, CSV round-trip, UDP loopback")
        return 0
    except Exception:
        traceback.print_exc()
        return 1


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real-time RAC UDP telemetry and lap comparison tool")
    parser.add_argument("--host", default="0.0.0.0", help="UDP bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=30000, help="UDP listen port (default: 30000)")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("telemetry_laps"),
        help="directory for automatically recorded lap CSV files",
    )
    parser.add_argument("--no-mock", action="store_true", help="do not auto-start the built-in mock sender")
    parser.add_argument("--self-test", action="store_true", help="run headless checks and exit")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return run_self_test()
    try:
        import tkinter as tk
        import matplotlib  # noqa: F401 - explicit dependency check
    except ImportError as exc:
        print(
            f"Missing GUI dependency: {exc}\nInstall NumPy and Matplotlib with: "
            f"{sys.executable} -m pip install numpy matplotlib\n"
            "Tkinter is normally bundled with Python; on Linux install python3-tk.",
            file=sys.stderr,
        )
        return 2
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Cannot open the graphical window: {exc}", file=sys.stderr)
        return 2
    try:
        TelemetryApp(root, args.host, args.port, args.output_dir.expanduser().resolve(), not args.no_mock)
        root.mainloop()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        traceback.print_exc()
        try:
            root.destroy()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
