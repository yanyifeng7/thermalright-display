#!/usr/bin/env python3
"""usblcd-display GUI — pick a GIF/image, push it to the AIO LCD screen.

Zero-dependency desktop app (tkinter ships with Python).
"""

from __future__ import annotations

import io
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageSequence

from usblcd.device import USBLCD, LCDDeviceError
from usblcd.frames import jpeg_to_frame

# Project root: directory containing this file
_PROJECT_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

APP_NAME = "usblcd-display"
DEVICE_LABEL = "USBDISPLAY (0x87AD:0x70DB)"
RESOLUTIONS = [
    "1600 x 720",
    "960 x 720",
    "480 x 720",
    "720 x 1600",
    "720 x 960",
    "960 x 540",
    "320 x 320",
]
ROTATIONS = ["0°", "90°", "180°", "270°"]
QUALITY_LEVELS = ["High (95)", "Good (85)", "Medium (75)", "Low (60)"]
FPS_CHOICES = ["5", "10", "15", "20", "24", "30", "60"]
SCALE_MODES = ["Fit (letterbox)", "Fill (crop)", "Stretch (fill screen)"]
OVERLAY_POSITIONS = ["top-left", "top-right", "bottom-left", "bottom-right"]
# Minimum seconds between overlay re-renders (bounds re-encode CPU cost)
OVERLAY_MIN_UPDATE_S = 2.0


class PlayerThread(threading.Thread):
    """Background thread that streams frames to the LCD.

    With monitor overlay active, frames are re-encoded LAZILY: each frame
    is decoded + overlay + re-encoded only when it's about to be sent and
    its cached overlay is stale (sensor values changed). This avoids
    re-encoding ALL frames on every sensor tick (the naive approach burned
    ~0.4 cores on a 224-frame GIF — see bench_overlay3.py).
    """

    def __init__(self, lcd: USBLCD, frames: list[bytes], delays_ms: list[int],
                 width: int, height: int, on_error, on_loop,
                 overlay_provider=None):
        super().__init__(daemon=True)
        self.lcd = lcd
        self.frames = frames
        self.delays_ms = delays_ms
        self.width = width
        self.height = height
        self.on_error = on_error
        self.on_loop = on_loop
        self.overlay_provider = overlay_provider  # None = no overlay
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        n = len(self.frames)
        loop = 0
        while not self._stop.is_set():
            loop += 1
            for i, (frame, delay) in enumerate(zip(self.frames, self.delays_ms)):
                if self._stop.is_set():
                    return
                # Lazy overlay: re-encode this frame only if stale
                if self.overlay_provider is not None:
                    frame = self.overlay_provider.get_frame(i, frame)
                t0 = time.monotonic()
                try:
                    self.lcd.send_frame(jpeg_to_frame(frame, self.width, self.height))
                except Exception as e:
                    self.on_error(f"send failed: {e}")
                    return
                remain = delay / 1000 - (time.monotonic() - t0)
                if remain > 0:
                    self._stop.wait(remain)
            self.on_loop(loop)


class MonitorThread(threading.Thread):
    """Polls sensors at 1 Hz; updates the overlay sprite + marks frames
    stale. Actual per-frame re-encode happens lazily in the player."""

    def __init__(self, app, frames: list[bytes], delays_ms: list[int],
                 width: int, height: int, on_status):
        super().__init__(daemon=True)
        self.app = app
        self.frames = frames          # shared list (base frames)
        self.delays_ms = delays_ms
        self.width = width
        self.height = height
        self.on_status = on_status
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Overlay state: readings + a cache of overlaid frames (by index)
        self._readings = None
        self._cache: dict[int, bytes] = {}
        self._sensor = None

    def stop(self):
        self._stop.set()

    def _sensor_monitor(self):
        if self._sensor is None:
            from usblcd.sensors import SensorMonitor

            self._sensor = SensorMonitor()
        return self._sensor

    def invalidate(self):
        """Drop the overlay cache (brightness/settings changed)."""
        with self._lock:
            self._cache.clear()

    def get_frame(self, i: int, base_frame: bytes) -> bytes:
        """Return the overlaid frame for index i, re-encoding lazily if
        the cached copy is stale (readings changed)."""
        from usblcd.frames import draw_monitor_overlay, apply_brightness
        from PIL import Image
        import io as _io

        with self._lock:
            cached = self._cache.get(i)
            if cached is not None:
                return cached
            readings = self._readings
            brightness = int(self.app.bright_var.get())
            rotate = int(self.app.rot_var.get().replace("°", ""))
            position = self.app.overlay_pos_var.get()
            quality = int(self.app.qual_var.get().split("(")[1].rstrip(")"))
        if readings is None:
            return base_frame
        # Re-encode this one frame with overlay + current brightness
        img = Image.open(_io.BytesIO(base_frame)).convert("RGB")
        img = apply_brightness(img, brightness)
        img = draw_monitor_overlay(
            img,
            gpu_temp_c=readings.gpu_temp_c,
            gpu_freq_mhz=readings.gpu_freq_mhz,
            cpu_freq_mhz=readings.cpu_freq_mhz,
            rotate=rotate,
            position=position,
        )
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        out = buf.getvalue()
        with self._lock:
            self._cache[i] = out
        return out

    def run(self):
        sensor = self._sensor_monitor()
        last_text = None
        last_update = 0.0
        while not self._stop.is_set():
            r = sensor.read()
            # Displayed text decides staleness (what the user SEES):
            # CPU 2 decimals, GPU MHz rounded to 50, GPU temp rounded.
            text = (
                f"{r.cpu_freq_mhz/1000:.2f}" if r.cpu_freq_mhz else "-",
                f"{round(r.gpu_freq_mhz/50)*50}" if r.gpu_freq_mhz else "-",
                f"{r.gpu_temp_c:.0f}" if r.gpu_temp_c else "-",
            )
            now = time.monotonic()
            # Re-render only when the visible text changed AND at least
            # OVERLAY_MIN_UPDATE_S since the last render (bounds the
            # per-frame re-encode cost on long/fast GIFs).
            if text != last_text and (now - last_update) >= OVERLAY_MIN_UPDATE_S:
                last_text = text
                last_update = now
                with self._lock:
                    self._readings = r
                    self._cache.clear()  # all frames stale -> lazy re-encode
                if self.on_status:
                    self.on_status(
                        f"Monitor: CPU {r.cpu_freq_mhz/1000:.2f} GHz"
                        f" | GPU {r.gpu_freq_mhz} MHz {r.gpu_temp_c}C"
                    )
            self._stop.wait(0.5)

        if self._sensor is not None:
            try:
                self._sensor.shutdown()
            except Exception:
                pass


class LCDApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} — AIO LCD player")
        self.geometry("640x880")
        self.resizable(False, False)
        self.configure(bg="#f0f0f0")

        self.file_path: str | None = None
        self.frames: list[bytes] = []
        self.delays_ms: list[int] = []
        self._source_frames: list[bytes] = []
        self.player: PlayerThread | None = None
        self.monitor: MonitorThread | None = None
        self.lcd: USBLCD | None = None

        self._build_ui()
        self._set_status("Not connected", "#888")

    # ---------- UI ----------

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        pad = {"padx": 14, "pady": 6}

        # Header
        header = tk.Label(self, text=APP_NAME, font=("Segoe UI", 16, "bold"),
                          bg="#f0f0f0", fg="#1a1a1a")
        header.pack(anchor="w", **pad)

        # File picker
        file_frame = ttk.Frame(self)
        file_frame.pack(fill="x", **pad)
        self.file_label = ttk.Label(file_frame, text="No file selected",
                                    foreground="#666")
        self.file_label.pack(side="left", fill="x", expand=True)
        ttk.Button(file_frame, text="Browse…", command=self._pick_file).pack(side="right")

        # Settings grid
        settings = ttk.LabelFrame(self, text=" Display settings ")
        settings.pack(fill="x", **pad)

        row = 0
        ttk.Label(settings, text="Resolution:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.res_var = tk.StringVar(value=RESOLUTIONS[0])
        ttk.Combobox(settings, textvariable=self.res_var, values=RESOLUTIONS,
                     state="readonly", width=14).grid(row=row, column=1, sticky="w", pady=4)

        ttk.Label(settings, text="Rotation:").grid(row=row, column=2, sticky="w", padx=10, pady=4)
        self.rot_var = tk.StringVar(value=ROTATIONS[2])  # 180° = upside-down panels
        ttk.Combobox(settings, textvariable=self.rot_var, values=ROTATIONS,
                     state="readonly", width=6).grid(row=row, column=3, sticky="w", pady=4)

        row += 1
        ttk.Label(settings, text="Quality:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.qual_var = tk.StringVar(value=QUALITY_LEVELS[0])
        ttk.Combobox(settings, textvariable=self.qual_var, values=QUALITY_LEVELS,
                     state="readonly", width=14).grid(row=row, column=1, sticky="w", pady=4)

        ttk.Label(settings, text="Frame rate:").grid(row=row, column=2, sticky="w", padx=10, pady=4)
        self.fps_var = tk.StringVar(value="24")
        ttk.Combobox(settings, textvariable=self.fps_var, values=FPS_CHOICES,
                     state="readonly", width=6).grid(row=row, column=3, sticky="w", pady=4)

        row += 1
        ttk.Label(settings, text="Scale:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.scale_var = tk.StringVar(value=SCALE_MODES[0])
        ttk.Combobox(settings, textvariable=self.scale_var, values=SCALE_MODES,
                     state="readonly", width=18).grid(row=row, column=1, sticky="w", pady=4)

        ttk.Label(settings, text="Brightness:").grid(row=row, column=2, sticky="w", padx=10, pady=4)
        self.bright_var = tk.IntVar(value=100)
        bright_frame = ttk.Frame(settings)
        bright_frame.grid(row=row, column=3, sticky="w", pady=4)
        # Click-to-set bar: one click = one brightness change.
        # Snap zones: clicking just outside the left/right end sets 0/100.
        self.bright_canvas = tk.Canvas(bright_frame, width=134, height=22,
                                       bg="#e8e8e8", highlightthickness=1,
                                       highlightbackground="#aaa", cursor="hand2")
        self.bright_canvas.pack(side="left")
        self.bright_canvas.bind("<Button-1>", self._brightness_click)
        self.bright_label = ttk.Label(bright_frame, text="100", width=4)
        self.bright_label.pack(side="left", padx=4)
        self._draw_brightness_bar()

        row += 1
        ttk.Label(settings, text="Loop:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.loop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(settings, text="Repeat forever", variable=self.loop_var).grid(
            row=row, column=1, sticky="w", pady=4)
        self.monitor_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(settings, text="Monitor overlay (CPU/GPU)",
                        variable=self.monitor_var).grid(
            row=row, column=2, columnspan=2, sticky="w", pady=4)

        row += 1
        ttk.Label(settings, text="Overlay pos:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.overlay_pos_var = tk.StringVar(value=OVERLAY_POSITIONS[0])
        ttk.Combobox(settings, textvariable=self.overlay_pos_var, values=OVERLAY_POSITIONS,
                     state="readonly", width=16).grid(row=row, column=1, sticky="w", pady=4)

        # Live preview: refresh when display-affecting settings change
        for var in (self.res_var, self.rot_var, self.scale_var):
            var.trace_add("write", lambda *a: self._refresh_preview())
        self.bright_var.trace_add("write", lambda *a: self._on_brightness())

        # Preview
        preview_frame = ttk.LabelFrame(self, text=" Preview ")
        preview_frame.pack(fill="x", **pad)
        self.preview_canvas = tk.Canvas(preview_frame, width=320, height=144,
                                        bg="#1a1a1a", highlightthickness=1,
                                        highlightbackground="#ccc")
        self.preview_canvas.pack(padx=8, pady=8)
        self._preview_photo = None
        self._preview_frames: list[tk.PhotoImage] = []
        self._preview_idx = 0
        self._preview_job: str | None = None
        self._preview_src = None
        self._preview_slices: list[tuple[int, int]] = []
        self._preview_zt_data: bytes | None = None
        self._preview_static = None
        self._preview_total = 1
        self._preview_delay = 80
        self._draw_preview_placeholder("No preview")

        # My Themes panel
        themes_frame = ttk.LabelFrame(self, text=" My Themes ")
        themes_frame.pack(fill="x", **pad)
        themes_row = ttk.Frame(themes_frame)
        themes_row.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Label(themes_row, text="Saved in themes/:").pack(side="left")
        ttk.Button(themes_row, text="Refresh", command=self._refresh_themes,
                   width=9).pack(side="right")
        ttk.Button(themes_row, text="Delete", command=self._delete_theme,
                   width=9).pack(side="right", padx=4)
        ttk.Button(themes_row, text="Load", command=self._load_theme,
                   width=9).pack(side="right", padx=4)
        self.themes_list = tk.Listbox(themes_frame, height=4,
                                      selectmode=tk.SINGLE,
                                      font=("Segoe UI", 9))
        self.themes_list.pack(fill="x", padx=8, pady=6)
        self.themes_list.bind("<Double-Button-1>", lambda e: self._load_theme())
        self._themes_dir = os.path.join(_PROJECT_ROOT_DIR, "themes")
        os.makedirs(self._themes_dir, exist_ok=True)
        self._refresh_themes()

        # Status
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", **pad)
        ttk.Label(status_frame, text="Status:").pack(side="left")
        self.status_label = tk.Label(status_frame, text="", font=("Segoe UI", 10),
                                     bg="#f0f0f0")
        self.status_label.pack(side="left", padx=6)

        # Controls
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", **pad)
        self.connect_btn = ttk.Button(ctrl, text="Connect", command=self._toggle_connect)
        self.connect_btn.pack(side="left", ipadx=12, ipady=2)
        self.play_btn = ttk.Button(ctrl, text="Play", command=self._toggle_play,
                                   state="disabled")
        self.play_btn.pack(side="left", padx=8, ipadx=12, ipady=2)
        self.stop_btn = ttk.Button(ctrl, text="Stop", command=self._stop_play,
                                   state="disabled")
        self.stop_btn.pack(side="left", ipadx=12, ipady=2)
        self.save_btn = ttk.Button(ctrl, text="Save Theme…", command=self._save_theme,
                                   state="disabled")
        self.save_btn.pack(side="left", padx=8, ipadx=12, ipady=2)

        # Progress
        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill="x", padx=14, pady=2)

        # Info box
        info = tk.Label(self, text=(
            "Supports: animated GIF, .zt theme files, static images\n"
            "Tip: if the image is upside-down, set Rotation to 180°"),
            font=("Segoe UI", 9), bg="#f0f0f0", fg="#888", justify="left")
        info.pack(anchor="w", **pad)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- Actions ----------

    def _pick_file(self):
        path = filedialog.askopenfilename(
            title="Choose a GIF, theme, or image",
            filetypes=[
                ("All supported", "*.gif *.zt *.jpg *.jpeg *.png *.bmp *.webp"),
                ("Animated GIF", "*.gif"),
                ("TRCC theme", "*.zt"),
                ("Images", "*.jpg *.jpeg *.png *.bmp *.webp"),
            ],
        )
        if not path:
            return
        self.file_path = path
        name = path.split("/")[-1].split("\\")[-1]
        self.file_label.config(text=name, foreground="#1a1a1a")
        self._set_status(f"Loaded: {name} (click Play)", "#1a6fb0")
        self._load_preview(path)

    # ---------- Preview ----------

    def _refresh_preview(self):
        """Re-render the current frame with new settings (no file re-open)."""
        if self._preview_src is not None:
            self._render_preview_frame(self._preview_idx)
        elif self._preview_slices:
            self._render_preview_frame(self._preview_idx)
        elif self._preview_static is not None:
            self._show_preview_image(self._preview_transform(self._preview_static))
        else:
            self._draw_preview_placeholder("No preview")

    def _preview_transform(self, img: Image.Image) -> Image.Image:
        """Apply panel settings at preview scale (fast, aspect-accurate).

        Mirrors the encode path (_scale -> rotate -> _scale) but renders into
        a ~640px-wide box instead of the full 1600x720 panel — ~6x less work.
        """
        width, height, rotate, _, _, scale, brightness = self._parse_settings()
        pw = 640
        ph = max(1, round(pw * height / width))
        target = (pw, ph)
        img = self._scale(img, target, scale)
        if rotate:
            img = img.rotate(-rotate, expand=True)
            img = self._scale(img, target, scale)
        from usblcd.frames import apply_brightness

        img = apply_brightness(img, brightness)
        return img

    def _draw_preview_placeholder(self, text: str):
        self.preview_canvas.delete("all")
        self.preview_canvas.create_text(
            160, 72, text=text, fill="#666", font=("Segoe UI", 10)
        )

    def _load_preview(self, path: str):
        """Open source once; render frames at preview scale."""
        if self._preview_job:
            self.after_cancel(self._preview_job)
            self._preview_job = None
        self._preview_frames = []
        self._preview_idx = 0
        self._preview_src = None      # lazy GIF source handle
        self._preview_slices = []     # .zt: list of (start, end) byte ranges
        self._preview_static = None   # static image (PIL, unrendered)
        self._preview_total = 1

        low = path.lower()
        try:
            if low.endswith(".gif"):
                src = Image.open(path)
                self._preview_src = src
                self._preview_total = getattr(src, "n_frames", 1) or 1
                self._render_preview_frame(0)
                if self._preview_total > 1:
                    self._animate_preview()
            elif low.endswith(".zt"):
                with open(path, "rb") as f:
                    data = f.read()
                slices = []
                pos = 0
                while True:
                    idx = data.find(b"\xFF\xD8", pos)
                    if idx < 0:
                        break
                    eoi = data.find(b"\xFF\xD9", idx)
                    if eoi < 0:
                        break
                    slices.append((idx, eoi + 2))
                    pos = eoi + 2
                if not slices:
                    self._draw_preview_placeholder("No frames")
                    return
                self._preview_slices = slices
                self._preview_zt_data = data
                self._preview_total = len(slices)
                self._render_preview_frame(0)
                if len(slices) > 1:
                    self._animate_preview()
            else:
                self._preview_static = Image.open(path).convert("RGB")
                self._show_preview_image(self._preview_transform(self._preview_static))
        except Exception as e:
            self._draw_preview_placeholder(f"Preview failed: {e}")

    def _render_preview_frame(self, idx: int):
        """Transform + show frame idx at preview scale."""
        try:
            if self._preview_src is not None:
                self._preview_src.seek(idx)
                frame = self._preview_src.copy()
                # Respect the GIF's per-frame duration for preview pacing
                d = self._preview_src.info.get("duration", 0)
                self._preview_delay = max(10, int(d)) if d else 100
            elif self._preview_slices:
                s, e = self._preview_slices[idx]
                frame = Image.open(io.BytesIO(self._preview_zt_data[s:e]))
                # .zt has no timing; match the selected frame rate
                fps = int(self.fps_var.get())
                self._preview_delay = max(10, int(1000 / fps))
            else:
                return
            self._show_preview_image(self._preview_transform(frame))
        except Exception:
            pass  # skip bad frame

    def _photo_from_image(self, img: Image.Image) -> tk.PhotoImage:
        """Convert a PIL image to a PhotoImage sized for the canvas."""
        img = img.convert("RGB")
        cw, ch = 320, 144
        iw, ih = img.size
        scale = min(cw / iw, ch / ih)
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        img = img.resize((nw, nh), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return tk.PhotoImage(data=buf.getvalue())

    def _show_preview_image(self, img: Image.Image):
        """Show a PIL image (already panel-transformed) in the canvas."""
        self._show_preview_frame(self._photo_from_image(img))

    def _show_preview_frame(self, photo: tk.PhotoImage):
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(160, 72, image=photo)
        self._preview_photo = photo  # keep reference

    def _animate_preview(self):
        total = self._preview_total
        if total < 2:
            return
        self._preview_idx = (self._preview_idx + 1) % total
        self._render_preview_frame(self._preview_idx)
        delay = getattr(self, "_preview_delay", 80)
        self._preview_job = self.after(delay, self._animate_preview)

    def _stop_preview(self):
        if self._preview_job:
            self.after_cancel(self._preview_job)
            self._preview_job = None
        self._preview_frames = []
        self._preview_src = None
        self._preview_slices = []
        self._preview_static = None

    def _parse_settings(self):
        res = self.res_var.get().replace(" ", "").split("x")
        width, height = int(res[0]), int(res[1])
        rotate = int(self.rot_var.get().replace("°", ""))
        quality = int(self.qual_var.get().split("(")[1].rstrip(")"))
        fps = int(self.fps_var.get())
        scale = self.scale_var.get().split(" ")[0].lower()
        brightness = int(self.bright_var.get())
        return width, height, rotate, quality, fps, scale, brightness

    def _on_brightness(self, *args):
        """Update brightness label + live preview when the value changes."""
        val = int(self.bright_var.get())
        self.bright_label.config(text=str(val))
        self._draw_brightness_bar()
        if self.file_path:
            self._refresh_preview()
        # Live-adjust the playing frames: re-encode with new brightness
        if self.player is not None and self.frames:
            self._reencode_playing(val)

    def _brightness_click(self, event):
        """Single click on the bar sets brightness once (0-100).

        Snap zones: clicks in the left/right 12px margins set 0/100.
        """
        w = self.bright_canvas.winfo_width()
        if w <= 0:
            w = 134
        edge = 12  # snap zone width at each end
        if event.x <= edge:
            val = 0
        elif event.x >= w - edge:
            val = 100
        else:
            frac = (event.x - edge) / (w - 2 * edge)
            val = int(round(max(0.0, min(1.0, frac)) * 100))
        # Only act if the value actually changed (avoid redundant re-encodes)
        if val != int(self.bright_var.get()):
            self.bright_var.set(val)  # triggers _on_brightness once

    def _draw_brightness_bar(self):
        """Paint the brightness bar inset with snap-zone margins + ticks."""
        c = self.bright_canvas
        c.delete("all")
        w = 134
        h = 22
        edge = 12
        bar_w = w - 2 * edge  # 110px visible bar
        val = int(self.bright_var.get())
        fill_w = max(2, int(bar_w * val / 100))
        # Visible bar background (darker, distinct from snap zones)
        c.create_rectangle(edge, 1, w - edge, h - 1, fill="#d0d0d0", outline="")
        # Fill portion
        c.create_rectangle(edge, 1, edge + fill_w, h - 1, fill="#4a90d9", outline="")
        # Tick marks at 0/25/50/75/100
        for frac in (0, 0.25, 0.5, 0.75, 1.0):
            x = int(edge + bar_w * frac)
            c.create_line(x, 1, x, h - 1, fill="#555", width=1)
        # Current value label
        text_x = max(edge + 4, edge + fill_w - 14)
        c.create_text(text_x, h // 2, text=str(val),
                      fill="#fff" if val > 30 else "#333",
                      font=("Segoe UI", 8, "bold"))

    def _reencode_playing(self, brightness: int):
        """Re-encode the current clip's frames with a new brightness.

        Re-encodes from the TRUE pre-brightness frames (avoiding
        double-compression), mutating the shared list in place so the
        running player picks it up. Runs in a background thread.
        """
        from usblcd.frames import apply_brightness

        quality = int(self.qual_var.get().split("(")[1].rstrip(")"))
        source = list(self._source_frames if self._source_frames else self.frames)

        def worker():
            new_frames = []
            for f in source:
                img = Image.open(io.BytesIO(f)).convert("RGB")
                img = apply_brightness(img, brightness)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality)
                new_frames.append(buf.getvalue())
            # Mutate the shared list in place so the running player picks it up
            self.frames[:] = new_frames
            # Overlay cache is now stale (brightness baked into new frames)
            if self.monitor is not None:
                self.monitor.invalidate()

        threading.Thread(target=worker, daemon=True).start()

    def _load_clip(self):
        """Load + pre-encode frames from the selected file."""
        if not self.file_path:
            return False
        width, height, rotate, quality, fps, scale, brightness = self._parse_settings()
        target = (width, height)
        path = self.file_path
        low = path.lower()

        frames, delays = [], []
        own_zt = False
        if low.endswith(".zt"):
            with open(path, "rb") as f:
                data = f.read()
            from usblcd.ztfile import zt_parse, zt_to_frames

            meta = zt_parse(data)
            if meta is not None:
                # Our own theme: frames are FINAL (settings baked into pixels).
                # Use them directly; restore any settings not already set.
                own_zt = True
                self.fps_var.set(str(meta["fps"]))
                self.rot_var.set(f"{meta['rotate']}°")
                self.scale_var.set(SCALE_MODES[["fit", "fill", "stretch"].index(meta["scale"])])
                self.bright_var.set(meta["brightness"])
                res = f"{meta['width']} x {meta['height']}"
                if res in RESOLUTIONS:
                    self.res_var.set(res)
                frames = zt_to_frames(data)
                delays = [int(1000 / meta["fps"])] * len(frames)
            else:
                # TRCC-style .zt: raw JPEGs need re-encoding + current settings.
                # Encode at brightness=100 (pre-brightness source).
                pos = 0
                while True:
                    idx = data.find(b"\xFF\xD8", pos)
                    if idx < 0:
                        break
                    eoi = data.find(b"\xFF\xD9", idx)
                    if eoi < 0:
                        break
                    img = Image.open(io.BytesIO(data[idx : eoi + 2])).convert("RGB")
                    frames.append(self._encode(img, target, rotate, quality, scale, 100))
                    delays.append(int(1000 / fps))
                    pos = eoi + 2
        elif low.endswith(".gif"):
            src = Image.open(path)
            for frame in ImageSequence.Iterator(src):
                rgb = frame.convert("RGB")
                canvas = self._scale(rgb, target, scale)
                if rotate:
                    canvas = canvas.rotate(-rotate, expand=True)
                    canvas = self._scale(canvas, target, scale)
                # brightness=100: _source_frames stay pre-brightness
                frames.append(self._encode(canvas, target, 0, quality, scale, 100))
                d = frame.info.get("duration", 0)
                delays.append(max(10, int(d)) if d else 100)
        else:  # static image
            img = Image.open(path).convert("RGB")
            img = self._scale(img, target, scale)
            if rotate:
                img = img.rotate(-rotate, expand=True)
                img = self._scale(img, target, scale)
            frames.append(self._encode(img, target, 0, quality, scale, 100))
            delays.append(1000)

        if not frames:
            return False
        # _source_frames = TRUE pre-brightness (encoded at 100). The playing
        # frames derive from it by applying the current brightness, so the
        # monitor overlay re-encode always respects the brightness setting.
        self._source_frames = list(frames)
        if own_zt:
            # Final frames already have their settings baked in
            self.frames = list(frames)
        else:
            self.frames = [self._apply_brightness_bytes(f, brightness, quality)
                           for f in frames]
        self.delays_ms = delays
        self.save_btn.config(state="normal")
        return True

    @staticmethod
    def _apply_brightness_bytes(jpeg: bytes, brightness: int, quality: int) -> bytes:
        """Re-encode a JPEG byte frame with a brightness (0-100)."""
        from usblcd.frames import apply_brightness

        if brightness >= 100:
            return jpeg
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        img = apply_brightness(img, brightness)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    @staticmethod
    def _scale(img, target, mode: str = "fit"):
        """Scale image to the target panel.

        mode:
          fit     - letterbox (preserve aspect, black bars)
          fill    - crop to cover (preserve aspect, fill screen)
          stretch - distort to exactly fill the panel
        """
        tw, th = target
        iw, ih = img.size

        if mode == "stretch":
            return img.resize(target, Image.LANCZOS)

        if mode == "fill":
            scale = max(tw / iw, th / ih)
            nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
            img = img.resize((nw, nh), Image.LANCZOS)
            # center-crop to target
            x = (nw - tw) // 2
            y = (nh - th) // 2
            return img.crop((x, y, x + tw, y + th))

        # fit (default): letterbox on black
        scale = min(tw / iw, th / ih)
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        img = img.resize((nw, nh), Image.LANCZOS)
        canvas = Image.new("RGB", target, (0, 0, 0))
        canvas.paste(img, ((tw - nw) // 2, (th - nh) // 2))
        return canvas

    @staticmethod
    def _encode(img, target, rotate, quality, scale="fit", brightness=100):
        from usblcd.frames import apply_brightness

        img = img.convert("RGB")
        if rotate:
            img = img.rotate(-rotate, expand=True)
            img = LCDApp._scale(img, target, scale)
        img = apply_brightness(img, brightness)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    def _toggle_connect(self):
        if self.lcd is not None:
            self._disconnect()
            return
        try:
            lcd = USBLCD("usbdisplay")
            if not lcd.find():
                self._set_status("Device not found — is it plugged in?", "#c0392b")
                return
            lcd.open()
            lcd.handshake()
            self.lcd = lcd
            self.connect_btn.config(text="Disconnect")
            self.play_btn.config(state="normal")
            self._set_status(f"Connected: {DEVICE_LABEL}", "#1a8a3a")
        except Exception as e:
            self._set_status(f"Connect failed: {e}", "#c0392b")

    def _disconnect(self):
        self._stop_play()
        if self.lcd is not None:
            try:
                self.lcd.close()
            except Exception:
                pass
            self.lcd = None
        self.connect_btn.config(text="Connect")
        self.play_btn.config(state="disabled")
        self._set_status("Disconnected", "#888")

    def _toggle_play(self):
        if self.player is not None:
            self._stop_play()
            return
        if self.lcd is None:
            self._set_status("Connect first", "#c0392b")
            return

        self._set_status("Loading frames…", "#1a6fb0")
        self.progress.start()
        self.update_idletasks()
        try:
            ok = self._load_clip()
        except Exception as e:
            ok = False
            self._set_status(f"Load failed: {e}", "#c0392b")
        self.progress.stop()
        if not ok:
            return

        n = len(self.frames)
        width, height, _, _, _, _, _ = self._parse_settings()
        total = sum(len(f) for f in self.frames)
        # Monitor overlay: the monitor provides the lazy overlay cache;
        # the player re-encodes each frame on demand as it cycles.
        self.monitor = None
        if self.monitor_var.get():
            self.monitor = MonitorThread(
                self, self.frames, self.delays_ms, width, height,
                on_status=self._set_status,
            )
            self.monitor.start()
        self.player = PlayerThread(
            self.lcd, self.frames, self.delays_ms, width, height,
            on_error=self._on_player_error, on_loop=self._on_loop,
            overlay_provider=self.monitor,
        )
        self.player.start()
        self.play_btn.config(text="Pause")
        self.stop_btn.config(state="normal")
        self._set_status(f"Playing: {n} frames, ~{total // n} KB/frame", "#1a8a3a")

    def _stop_play(self):
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor.join(timeout=2)
            self.monitor = None
        if self.player is not None:
            self.player.stop()
            self.player.join(timeout=2)
            self.player = None
        self.play_btn.config(text="Play")
        self.stop_btn.config(state="disabled")
        self.progress.stop()

    def _save_theme(self):
        """Save the current clip as a .zt theme in the project themes/ dir."""
        if not self.frames:
            self._set_status("Load a file first", "#c0392b")
            return

        # themes/ dir next to the project root (parent of this file)
        themes_dir = os.path.join(_PROJECT_ROOT_DIR, "themes")
        os.makedirs(themes_dir, exist_ok=True)

        # Suggest a name from the source file
        base = ""
        if self.file_path:
            base = os.path.splitext(os.path.basename(self.file_path))[0]
        name = simpledialog.askstring(
            "Save theme", "Theme name:",
            initialvalue=base or "theme", parent=self,
        )
        if not name:
            return
        # Sanitize filename
        name = re.sub(r'[^A-Za-z0-9_\- ]+', '', name).strip() or "theme"

        fps = int(self.fps_var.get())
        dest = os.path.join(themes_dir, f"{name}.zt")
        try:
            from usblcd.ztfile import frames_to_zt

            width, height, rotate, _, _, scale, brightness = self._parse_settings()
            data = frames_to_zt(
                self.frames,
                name=name,
                fps=fps,
                width=width,
                height=height,
                rotate=rotate,
                scale=scale,
                brightness=brightness,
            )
            with open(dest, "wb") as f:
                f.write(data)
        except Exception as e:
            self._set_status(f"Save failed: {e}", "#c0392b")
            return

        self._set_status(f"Saved: {name}.zt ({len(self.frames)} frames)", "#1a8a3a")
        self._refresh_themes()

    def _refresh_themes(self):
        """List .zt files in the themes/ dir."""
        self.themes_list.delete(0, tk.END)
        try:
            entries = sorted(
                f for f in os.listdir(self._themes_dir) if f.lower().endswith(".zt")
            )
        except OSError:
            entries = []
        for e in entries:
            self.themes_list.insert(tk.END, e)
        if not entries:
            self.themes_list.insert(tk.END, "(no themes saved yet)")

    def _selected_theme(self) -> str | None:
        sel = self.themes_list.curselection()
        if not sel:
            self._set_status("Select a theme first", "#c0392b")
            return None
        name = self.themes_list.get(sel[0])
        if name.startswith("("):
            return None
        return name

    def _load_theme(self):
        """Load a saved theme from themes/ and play it (settings restored)."""
        name = self._selected_theme()
        if not name:
            return
        path = os.path.join(self._themes_dir, name)
        self.file_path = path
        self.file_label.config(text=name, foreground="#1a1a1a")

        # Parse the .zt header for stored settings, if present
        meta = None
        try:
            from usblcd.ztfile import zt_parse

            with open(path, "rb") as f:
                data = f.read()
            meta = zt_parse(data)
        except Exception:
            pass

        if meta is not None:
            # Restore GUI controls to the saved theme's settings
            self.fps_var.set(str(meta["fps"]))
            self.rot_var.set(f"{meta['rotate']}°")
            self.scale_var.set(SCALE_MODES[["fit", "fill", "stretch"].index(meta["scale"])])
            self.bright_var.set(meta["brightness"])
            res = f"{meta['width']} x {meta['height']}"
            if res in RESOLUTIONS:
                self.res_var.set(res)

        self._load_preview(path)
        self._set_status(f"Loaded theme: {name} (click Play)", "#1a6fb0")
        # Auto-play if connected
        if self.lcd is not None and self.player is None:
            self._toggle_play()

    def _delete_theme(self):
        """Delete the selected theme file."""
        name = self._selected_theme()
        if not name:
            return
        if not messagebox.askyesno("Delete theme", f"Delete '{name}'?", parent=self):
            return
        try:
            os.remove(os.path.join(self._themes_dir, name))
            self._set_status(f"Deleted: {name}", "#888")
        except OSError as e:
            self._set_status(f"Delete failed: {e}", "#c0392b")
        self._refresh_themes()

    def _on_player_error(self, msg):
        self.after(0, lambda: (self._stop_play(),
                               self._set_status(msg, "#c0392b")))

    def _on_loop(self, loop):
        self.after(0, lambda: self._set_status(f"Playing… loop {loop}", "#1a8a3a"))

    def _set_status(self, text, color):
        self.status_label.config(text=text, fg=color)

    def _on_close(self):
        self._stop_preview()
        self._stop_play()
        if self.lcd is not None:
            try:
                self.lcd.close()
            except Exception:
                pass
        self.destroy()


def main():
    app = LCDApp()
    app.mainloop()


if __name__ == "__main__":
    sys.exit(main())
