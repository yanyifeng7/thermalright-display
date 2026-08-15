#!/usr/bin/env python3
"""usblcd-display GUI — pick a GIF/image, push it to the AIO LCD screen.

Zero-dependency desktop app (tkinter ships with Python).
"""

from __future__ import annotations

import io
import os
import logging
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

from PIL import Image, ImageSequence, ImageTk

from usblcd.device import USBLCD, LCDDeviceError

# Runtime exception logging: hot loops used to swallow exceptions with
# `except Exception: pass`, which hid real failures (e.g. the corrupt-base
# bug that burned 66% CPU silently). These helpers log once per exception
# type so a persistent failure is visible in the log without spamming it.
_logger = logging.getLogger("usblcd-app")
_logger.setLevel(logging.DEBUG)
if not _logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(_h)

_logged_excs: set = set()


def _log_exc(context: str):
    """Log a swallowed exception once per (context, type)."""
    import traceback

    et = sys.exc_info()[1]
    key = (context, type(et).__name__)
    if key in _logged_excs:
        return
    _logged_excs.add(key)
    _logger.error("%s: %s: %s", context, type(et).__name__, et)
    _logger.debug("".join(traceback.format_exc()))
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
# .zt themes have no timing; play them at this fixed rate
ZT_PLAY_FPS = 24
SCALE_MODES = ["Fit (letterbox)", "Fill (crop)", "Stretch (fill screen)"]
OVERLAY_POSITIONS = ["top-left", "top-right", "bottom-left", "bottom-right"]
# Overlay font sizes: base text size scales by these factors
OVERLAY_FONT_SIZES = ["Normal", "Large", "X-Large"]
OVERLAY_FONT_SCALE = {"Normal": 1.0, "Large": 1.35, "X-Large": 1.75}
# Now-playing metadata poll rate (display labels -> seconds). Lower = fresher
# track changes but more OS (GSMTC) wakeups; the progress bar stays smooth
# regardless (it interpolates between polls via the render tick).
POLL_RATES = ["2s", "3s", "5s", "10s"]
POLL_RATE_SECONDS = {"2s": 2, "3s": 3, "5s": 5, "10s": 10}


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
                 overlay_provider=None, cycles=None, on_complete=None):
        super().__init__(daemon=True)
        self.lcd = lcd
        self.frames = frames
        self.delays_ms = delays_ms
        self.width = width
        self.height = height
        self.on_error = on_error
        self.on_loop = on_loop
        self.overlay_provider = overlay_provider  # None = no overlay
        self.cycles = cycles          # None = loop forever, N = N cycles then done
        self.on_complete = on_complete
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        n = len(self.frames)
        loop = 0
        while not self._stop.is_set():
            if self.cycles is not None and loop >= self.cycles:
                break
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
        if self.on_complete is not None and not self._stop.is_set():
            self.on_complete()


class MonitorThread(threading.Thread):
    """Polls sensors; updates a pre-rendered overlay SPRITE when the
    visible readings change. Per displayed frame the player pastes the
    sprite onto the decoded frame — no per-frame text/rotation work
    (measured: 0.126 cores on a 224-frame GIF, always-live stats)."""

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
        # Overlay state: readings + sprite (RGBA block, paste pos) + frame cache
        self._readings = None
        self._sprite: tuple | None = None
        self._cache: dict[int, bytes] = {}
        self._sensor = None

    def stop(self):
        self._stop.set()

    def _sensor_monitor(self):
        if self._sensor is None:
            from usblcd.sensors import SensorMonitor

            self._sensor = SensorMonitor(
                auto_launch_lhm=self.app.lhm_auto_var.get()
            )
        return self._sensor

    def invalidate(self):
        """Drop the overlay cache (brightness/settings changed)."""
        with self._lock:
            self._cache.clear()

    def _build_sprite(self, r):
        """Pre-render the overlay text block ONCE (rotated + positioned)."""
        from usblcd.frames import build_overlay_sprite

        rotate = int(self.app.rot_var.get().replace("°", ""))
        position = self.app.overlay_pos_var.get()
        font_scale = OVERLAY_FONT_SCALE.get(self.app.overlay_font_var.get(), 1.0)
        self._sprite = build_overlay_sprite(
            r, self.width, self.height, rotate, position, font_scale
        )

    def get_frame(self, i: int, base_frame: bytes) -> bytes:
        """Return the overlaid frame: paste the cached sprite, encode once."""
        from usblcd.frames import apply_brightness
        from PIL import Image
        import io as _io

        with self._lock:
            cached = self._cache.get(i)
            if cached is not None:
                return cached
            sprite = self._sprite
            brightness = int(self.app.bright_var.get())
            quality = int(self.app.qual_var.get().split("(")[1].rstrip(")"))
        if sprite is None:
            return base_frame
        img = Image.open(_io.BytesIO(base_frame)).convert("RGB")
        img = apply_brightness(img, brightness)
        block, paste = sprite
        img.paste(block, paste, block)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        out = buf.getvalue()
        with self._lock:
            self._cache[i] = out
        return out

    def run(self):
        sensor = self._sensor_monitor()
        last_text = None
        while not self._stop.is_set():
            r = sensor.read()
            # Displayed text decides staleness (what the user SEES):
            # CPU 2 decimals, CPU temp, GPU MHz rounded to 50, GPU temp.
            text = (
                f"{r.cpu_freq_mhz/1000:.2f}" if r.cpu_freq_mhz else "-",
                f"{r.cpu_temp_c:.0f}" if r.cpu_temp_c else "-",
                f"{round(r.gpu_freq_mhz/50)*50}" if r.gpu_freq_mhz else "-",
                f"{r.gpu_temp_c:.0f}" if r.gpu_temp_c else "-",
                self.app.overlay_pos_var.get(),
                self.app.overlay_font_var.get(),
            )
            # Sprite is cheap to rebuild — update whenever the visible text
            # changes (no min-interval cap needed; always-live stats).
            if text != last_text:
                last_text = text
                with self._lock:
                    self._readings = r
                    self._build_sprite(r)
                    self._cache.clear()  # all frames stale -> lazy re-paste
                if self.on_status:
                    parts = [f"CPU {r.cpu_freq_mhz/1000:.2f} GHz"]
                    if r.cpu_temp_c is not None:
                        parts.append(f"{r.cpu_temp_c:.0f}C")
                    parts.append(f"GPU {r.gpu_freq_mhz} MHz {r.gpu_temp_c:.0f}C")
                    # Marshal to the tk main thread (widgets aren't thread-safe)
                    msg = "Monitor: " + " | ".join(parts)
                    if r.cpu_temp_c is None:
                        # LHM missing and not auto-launching: hint instead of
                        # silently hiding the CPU temp line.
                        msg += ("  (LHM not running — enable 'Auto-start LHM' "
                                "in settings for CPU temp)")
                    try:
                        self.app.after(0, lambda m=msg: self.on_status(m, "#1a8a3a"))
                    except Exception:
                        _log_exc("monitor_status_marshal")
            self._stop.wait(0.5)

        if self._sensor is not None:
            try:
                self._sensor.shutdown()
            except Exception:
                pass


class LCDApp(tk.Tk):
    def __init__(self):
        from usblcd import theme

        super().__init__()
        self.title(f"{APP_NAME} — AIO LCD player")
        self.geometry("640x1030")
        self.resizable(False, False)
        self.configure(bg=theme.BG)

        self.file_path: str | None = None
        self.frames: list[bytes] = []
        self.delays_ms: list[int] = []
        self._source_frames: list[bytes] = []
        self.player: PlayerThread | None = None
        self.monitor: MonitorThread | None = None
        self.lcd: USBLCD | None = None
        self._stop_play_requested = False

        self._build_ui()
        self._load_config()
        self._set_status("Not connected", "#888")
        # Watch the Now Playing Session Manager service: it has a known
        # runaway (~100% CPU sustained) when a media session is active,
        # which slowly heats the CPU. Restart it if it spins.
        self._watchdog_start()

    # ---------- UI ----------

    def _build_ui(self):
        from usblcd import theme

        style = ttk.Style()
        theme.setup_style(style)

        # Combobox dropdown popup (tk-level option, applies to all)
        self.option_add("*TCombobox*Listbox.background", theme.INPUT)
        self.option_add("*TCombobox*Listbox.foreground", theme.TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", theme.ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", theme.ACCENT_TEXT)
        self.option_add("*TCombobox*Listbox.borderWidth", "0")

        pad = {"padx": 14, "pady": 6}

        # Header
        header = tk.Label(self, text=APP_NAME, font=(theme.FONT_BOLD, 16, "bold"),
                          bg=theme.BG, fg=theme.TEXT)
        header.pack(anchor="w", **pad)
        sub = tk.Label(self, text="AIO LCD player — GIFs, themes & live monitoring",
                       font=(theme.FONT, 9), bg=theme.BG, fg=theme.TEXT_DIM)
        sub.pack(anchor="w", padx=14, pady=(0, 8))

        # Settings grid
        settings = ttk.LabelFrame(self, text=" Display settings ",
                                  style="Dark.TLabelframe")
        settings.pack(fill="x", **pad)

        row = 0
        ttk.Label(settings, text="Resolution:", style="Dark.TLabel").grid(
            row=row, column=0, sticky="w", padx=10, pady=5)
        self.res_var = tk.StringVar(value=RESOLUTIONS[0])
        ttk.Combobox(settings, textvariable=self.res_var, values=RESOLUTIONS,
                     state="readonly", width=14, style="Dark.TCombobox").grid(
            row=row, column=1, sticky="w", pady=5)

        ttk.Label(settings, text="Rotation:", style="Dark.TLabel").grid(
            row=row, column=2, sticky="w", padx=10, pady=5)
        self.rot_var = tk.StringVar(value=ROTATIONS[2])  # 180° = upside-down panels
        ttk.Combobox(settings, textvariable=self.rot_var, values=ROTATIONS,
                     state="readonly", width=6, style="Dark.TCombobox").grid(
            row=row, column=3, sticky="w", pady=5)

        row += 1
        ttk.Label(settings, text="Quality:", style="Dark.TLabel").grid(
            row=row, column=0, sticky="w", padx=10, pady=5)
        self.qual_var = tk.StringVar(value=QUALITY_LEVELS[0])
        ttk.Combobox(settings, textvariable=self.qual_var, values=QUALITY_LEVELS,
                     state="readonly", width=14, style="Dark.TCombobox").grid(
            row=row, column=1, sticky="w", pady=5)

        ttk.Label(settings, text="Overlay font:", style="Dark.TLabel").grid(
            row=row, column=2, sticky="w", padx=10, pady=5)
        self.overlay_font_var = tk.StringVar(value=OVERLAY_FONT_SIZES[0])
        ttk.Combobox(settings, textvariable=self.overlay_font_var,
                     values=OVERLAY_FONT_SIZES, state="readonly", width=10,
                     style="Dark.TCombobox").grid(
            row=row, column=3, sticky="w", pady=5)

        row += 1
        ttk.Label(settings, text="Scale:", style="Dark.TLabel").grid(
            row=row, column=0, sticky="w", padx=10, pady=5)
        self.scale_var = tk.StringVar(value=SCALE_MODES[0])
        ttk.Combobox(settings, textvariable=self.scale_var, values=SCALE_MODES,
                     state="readonly", width=18, style="Dark.TCombobox").grid(
            row=row, column=1, sticky="w", pady=5)

        ttk.Label(settings, text="Brightness:", style="Dark.TLabel").grid(
            row=row, column=2, sticky="w", padx=10, pady=5)
        self.bright_var = tk.IntVar(value=100)
        bright_frame = ttk.Frame(settings, style="Dark.TFrame")
        bright_frame.grid(row=row, column=3, sticky="w", pady=5)
        # Click-to-set bar: one click = one brightness change.
        # Snap zones: clicking just outside the left/right end sets 0/100.
        self.bright_canvas = tk.Canvas(bright_frame, width=134, height=22,
                                       bg=theme.INPUT, highlightthickness=1,
                                       highlightbackground=theme.BORDER,
                                       cursor="hand2")
        self.bright_canvas.pack(side="left")
        self.bright_canvas.bind("<Button-1>", self._brightness_click)
        self.bright_label = ttk.Label(bright_frame, text="100", width=4,
                                      style="Dark.TLabel")
        self.bright_label.pack(side="left", padx=4)
        self._draw_brightness_bar()

        row += 1
        ttk.Label(settings, text="Loop:", style="Dark.TLabel").grid(
            row=row, column=0, sticky="w", padx=10, pady=5)
        self.loop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(settings, text="Repeat forever", variable=self.loop_var,
                        style="Dark.TCheckbutton").grid(
            row=row, column=1, sticky="w", pady=5)
        self.monitor_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(settings, text="Monitor overlay (CPU/GPU)",
                        variable=self.monitor_var,
                        style="Dark.TCheckbutton").grid(
            row=row, column=2, columnspan=2, sticky="w", pady=5)

        row += 1
        self.np_auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(settings, text="Auto-display artwork (priority)",
                        variable=self.np_auto_var,
                        style="Dark.TCheckbutton").grid(
            row=row, column=1, columnspan=3, sticky="w", pady=5)
        # Auto-display drives the poller even when the tab isn't open
        self.np_auto_var.trace_add("write", lambda *a: self._np_auto_changed())

        row += 1
        self.lhm_auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(settings,
                        text="Auto-start LHM if missing (admin prompt)",
                        variable=self.lhm_auto_var,
                        style="Dark.TCheckbutton").grid(
            row=row, column=1, columnspan=3, sticky="w", pady=5)
        # Auto-launching LHM installs a kernel driver — strictly opt-in.
        # Enabling it only affects the next SensorMonitor creation.

        row += 1
        ttk.Label(settings, text="Overlay pos:", style="Dark.TLabel").grid(
            row=row, column=0, sticky="w", padx=10, pady=5)
        self.overlay_pos_var = tk.StringVar(value=OVERLAY_POSITIONS[0])
        ttk.Combobox(settings, textvariable=self.overlay_pos_var,
                     values=OVERLAY_POSITIONS, state="readonly", width=16,
                     style="Dark.TCombobox").grid(
            row=row, column=1, sticky="w", pady=5)

        ttk.Label(settings, text="Poll rate:", style="Dark.TLabel").grid(
            row=row, column=2, sticky="w", padx=10, pady=5)
        # How often to re-fetch now-playing metadata from the media app.
        # Lower = fresher track changes but more OS (GSMTC) wakeups; the
        # progress bar stays smooth regardless (interpolated between polls).
        self.np_poll_var = tk.StringVar(value=POLL_RATES[1])
        ttk.Combobox(settings, textvariable=self.np_poll_var,
                     values=POLL_RATES, state="readonly", width=8,
                     style="Dark.TCombobox").grid(
            row=row, column=3, sticky="w", pady=5)

        # Live preview: refresh when display-affecting settings change
        for var in (self.res_var, self.rot_var, self.scale_var):
            var.trace_add("write", lambda *a: self._refresh_preview())
        self.bright_var.trace_add("write", lambda *a: self._on_brightness())
        # Brightness change -> rebuild clip cache at new brightness (debounced
        # in _on_brightness_cache so slider drags don't fire a rebuild/tick)
        self.bright_var.trace_add("write", self._on_brightness_cache)
        # Persist settings changes (debounced) so they survive restarts
        for var in (self.res_var, self.rot_var, self.qual_var,
                    self.scale_var, self.bright_var, self.loop_var,
                    self.monitor_var, self.overlay_pos_var,
                    self.overlay_font_var, self.np_auto_var,
                    self.lhm_auto_var, self.np_poll_var):
            var.trace_add("write", self._schedule_config_save)

        # Tabbed content: [Playlist] [Now Playing]
        nb = ttk.Notebook(self, style="Dark.TNotebook")
        nb.pack(fill="both", expand=True, **pad)

        # ---------- Tab 1: Playlist ----------
        tab_playlist = ttk.Frame(nb, style="Dark.TFrame")
        nb.add(tab_playlist, text="  Playlist  ")

        # Preview
        preview_frame = ttk.LabelFrame(tab_playlist, text=" Preview ",
                                       style="Dark.TLabelframe")
        preview_frame.pack(fill="x", **pad)
        self.preview_canvas = tk.Canvas(preview_frame, width=320, height=144,
                                        bg=theme.PREVIEW_BG, highlightthickness=1,
                                        highlightbackground=theme.BORDER)
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

        # Playlist panel — the single file selector (add, preview, play)
        pl_frame = ttk.LabelFrame(tab_playlist, text=" Files ",
                                  style="Dark.TLabelframe")
        pl_frame.pack(fill="x", **pad)
        pl_row = ttk.Frame(pl_frame, style="Dark.TFrame")
        pl_row.pack(fill="x", padx=8, pady=(6, 0))
        self.file_label = ttk.Label(pl_row, text="No file selected",
                                    style="Dark.Dim.TLabel")
        self.file_label.pack(side="left", fill="x", expand=True)
        ttk.Button(pl_row, text="Clear", command=self._playlist_clear,
                   width=8, style="Dark.TButton").pack(side="right", padx=2)
        ttk.Button(pl_row, text="Remove", command=self._playlist_remove,
                   width=8, style="Dark.TButton").pack(side="right", padx=2)
        ttk.Button(pl_row, text="▲", command=lambda: self._playlist_move(-1),
                   width=3, style="Dark.TButton").pack(side="right", padx=1)
        ttk.Button(pl_row, text="▼", command=lambda: self._playlist_move(1),
                   width=3, style="Dark.TButton").pack(side="right", padx=1)
        ttk.Button(pl_row, text="Add File…", command=self._playlist_add,
                   width=10, style="Accent.TButton").pack(side="right", padx=2)
        self.playlist_list = tk.Listbox(pl_frame, height=4,
                                        selectmode=tk.EXTENDED,
                                        font=(theme.FONT, 9),
                                        bg=theme.INPUT, fg=theme.TEXT,
                                        selectbackground=theme.ACCENT,
                                        selectforeground=theme.ACCENT_TEXT,
                                        highlightthickness=1,
                                        highlightbackground=theme.BORDER,
                                        borderwidth=0)
        self.playlist_list.pack(fill="x", padx=8, pady=6)
        self.playlist_list.bind("<Double-Button-1>", lambda e: self._playlist_play_selected())
        self.playlist_list.bind("<<ListboxSelect>>", lambda e: self._playlist_on_select())
        self.playlist: list[str] = []      # file paths
        self.playlist_idx: int = 0         # current item when playing
        self._clip_cache: dict[str, tuple] = {}  # path -> (frames, delays, own_zt, meta)
        self._cache_brightness: int | None = None  # brightness of cached frames
        self._preload: tuple | None = None  # (legacy)
        self._preload_lock = threading.Lock()

        # ---------- Tab 2: Now Playing ----------
        tab_nowplaying = ttk.Frame(nb, style="Dark.TFrame")
        nb.add(tab_nowplaying, text="  Now Playing  ")
        self._tab_nowplaying = tab_nowplaying

        self.np_art_canvas = tk.Canvas(tab_nowplaying, width=320, height=180,
                                       bg=theme.PREVIEW_BG, highlightthickness=1,
                                       highlightbackground=theme.BORDER)
        self.np_art_canvas.pack(padx=8, pady=(8, 4))
        self._np_art_photo = None
        self._draw_np_placeholder("No media playing")

        np_info = ttk.Frame(tab_nowplaying, style="Dark.TFrame")
        np_info.pack(fill="x", padx=14)
        self.np_title_label = tk.Label(np_info, text="—", font=(theme.FONT_BOLD, 13),
                                       bg=theme.BG, fg=theme.TEXT, anchor="w")
        self.np_title_label.pack(fill="x")
        self.np_artist_label = tk.Label(np_info, text="", font=(theme.FONT, 10),
                                        bg=theme.BG, fg=theme.TEXT_DIM, anchor="w")
        self.np_artist_label.pack(fill="x")

        np_progress = ttk.Frame(tab_nowplaying, style="Dark.TFrame")
        np_progress.pack(fill="x", padx=14, pady=(10, 2))
        self.np_progress = ttk.Progressbar(np_progress, mode="determinate",
                                           style="Dark.Horizontal.TProgressbar")
        self.np_progress.pack(fill="x")
        np_time_row = ttk.Frame(tab_nowplaying, style="Dark.TFrame")
        np_time_row.pack(fill="x", padx=14)
        self.np_time_label = tk.Label(np_time_row, text="0:00 / 0:00",
                                      font=(theme.FONT, 9),
                                      bg=theme.BG, fg=theme.TEXT_DIM, anchor="w")
        self.np_time_label.pack(fill="x")

        np_hint = tk.Label(tab_nowplaying,
                           text="Shows the track currently playing in Apple Music,\n"
                                "Spotify or any app that registers with Windows\n"
                                "media controls. Album art + progress mirror the AIO.\n\n"
                                "Auto-display artwork lives in Display settings: when\n"
                                "checked, artwork takes priority over the playlist.",
                           font=(theme.FONT, 9), bg=theme.BG, fg=theme.TEXT_DIM,
                           justify="left")
        np_hint.pack(anchor="w", padx=14, pady=(10, 6))

        # Now-playing poller (runs only while this tab is active)
        self._np_active = False
        self._np_poll_job: str | None = None
        self._np_keepalive_job: str | None = None
        self._np_last_key = None
        self._np_last_art: Image.Image | None = None
        self._np_last_frame: bytes | None = None  # last JPEG frame sent to AIO
        self._np_base_jpeg: bytes | None = None  # clean bar-less base frame
        self._np_sprite: tuple | None = None     # overlay sprite (block, pos)
        self._np_sprite_text = None              # last sprite staleness key
        self._np_sensor = None                   # lazy SensorMonitor

        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        # Guard: the notebook fires this event during construction (when the
        # second tab is added it becomes selected). Ignore that first event
        # so the poller only starts when the user actually clicks the tab.
        self._nb_ready = False
        self.after(50, self._nb_ready_set)

        # Status
        status_frame = ttk.Frame(self, style="Dark.TFrame")
        status_frame.pack(fill="x", **pad)
        ttk.Label(status_frame, text="Status:",
                  style="Dark.Dim.TLabel").pack(side="left")
        self.status_label = tk.Label(status_frame, text="", font=(theme.FONT, 10),
                                     bg=theme.BG, fg=theme.TEXT_DIM)
        self.status_label.pack(side="left", padx=6)

        # Controls
        ctrl = ttk.Frame(self, style="Dark.TFrame")
        ctrl.pack(fill="x", **pad)
        self.connect_btn = ttk.Button(ctrl, text="Connect",
                                      command=self._toggle_connect,
                                      style="Dark.TButton")
        self.connect_btn.pack(side="left", ipadx=12, ipady=3)
        self.play_btn = ttk.Button(ctrl, text="Play", command=self._toggle_play,
                                   state="disabled", style="Accent.TButton")
        self.play_btn.pack(side="left", padx=8, ipadx=12, ipady=3)
        self.stop_btn = ttk.Button(ctrl, text="Stop", command=self._stop_play,
                                   state="disabled", style="Dark.TButton")
        self.stop_btn.pack(side="left", ipadx=12, ipady=3)

        # Progress
        self.progress = ttk.Progressbar(self, mode="indeterminate",
                                        style="Dark.Horizontal.TProgressbar")
        self.progress.pack(fill="x", padx=14, pady=2)

        # Info box
        info = tk.Label(self, text=(
            "Supports: animated GIF, .zt theme files, static images\n"
            "Tip: if the image is upside-down, set Rotation to 180°\n"
            "Monitoring: run LibreHardwareMonitor (web server) for CPU temp"),
            font=(theme.FONT, 9), bg=theme.BG, fg=theme.TEXT_DIM,
            justify="left")
        info.pack(anchor="w", **pad)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- Actions ----------

    # ---------- Playlist (single file selector) ----------

    def _playlist_add(self):
        paths = filedialog.askopenfilenames(
            title="Add to playlist",
            filetypes=[
                ("All supported", "*.gif *.zt *.jpg *.jpeg *.png *.bmp *.webp"),
                ("Animated GIF", "*.gif"),
                ("TRCC theme", "*.zt"),
                ("Images", "*.jpg *.jpeg *.png *.bmp *.webp"),
            ],
        )
        for p in paths:
            if p not in self.playlist:
                self.playlist.append(p)
                self.playlist_list.insert(tk.END, os.path.basename(p))
        if paths:
            # Select the first newly-added file so the preview shows it
            self.playlist_list.selection_clear(0, tk.END)
            first_new = len(self.playlist) - len(paths)
            self.playlist_list.selection_set(first_new)
            self.playlist_list.see(first_new)
            self._playlist_on_select()
            self._set_status(f"Files: {len(self.playlist)}", "#1a6fb0")
            self._save_config()
            self._load_all_clips()

    def _playlist_on_select(self):
        """Listbox selection -> load that file as 'current' + preview."""
        sel = self.playlist_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self.playlist):
            return
        path = self.playlist[idx]
        self.file_path = path
        self.file_label.config(text=self._short_name(path), foreground="#1a1a1a")
        # Show the preview (also loads source frames for Save Theme)
        self._load_preview(path)

    @staticmethod
    def _short_name(path: str, limit: int = 30) -> str:
        """Basename truncated with ellipsis so long filenames don't push
        the playlist buttons off-screen."""
        name = os.path.basename(path)
        return name if len(name) <= limit else name[: limit - 1] + "…"

    def _playlist_remove(self):
        sel = list(self.playlist_list.curselection())
        for idx in reversed(sel):
            removed = self.playlist.pop(idx)
            self._clip_cache.pop(removed, None)
            self.playlist_list.delete(idx)
        self._set_status(f"Files: {len(self.playlist)}", "#1a6fb0")
        self._save_config()

    def _playlist_clear(self):
        self.playlist.clear()
        self._clip_cache.clear()
        self.playlist_list.delete(0, tk.END)
        self._set_status("Playlist cleared", "#888")
        self._save_config()

    def _playlist_move(self, delta: int):
        sel = self.playlist_list.curselection()
        if not sel:
            return
        idx = sel[0]
        new = idx + delta
        if new < 0 or new >= len(self.playlist):
            return
        self.playlist[idx], self.playlist[new] = self.playlist[new], self.playlist[idx]
        self.playlist_list.delete(0, tk.END)
        for p in self.playlist:
            self.playlist_list.insert(tk.END, os.path.basename(p))
        self.playlist_list.selection_set(new)
        self._save_config()

    def _playlist_play_selected(self):
        """Double-click an item: load + play just that one."""
        sel = self.playlist_list.curselection()
        if not sel:
            return
        self._playlist_on_select()  # sets file_path + label + preview
        if self.lcd is not None and self.player is None:
            self._toggle_play()

    # ---------- Playlist playback ----------

    def _playlist_advance(self):
        """Called when the current playlist item finishes its single cycle."""
        if self.playlist and not self._stop_play_requested:
            self.playlist_idx += 1
            if self.playlist_idx >= len(self.playlist):
                if self.loop_var.get():
                    self.playlist_idx = 0
                else:
                    self._set_status("Playlist finished", "#888")
                    self._stop_play()
                    return
            self._playlist_start_item(self.playlist_idx)
        else:
            self._stop_play()

    def _playlist_advance(self):
        """Called when the current playlist item finishes its single cycle."""
        if self.playlist and not self._stop_play_requested:
            self.playlist_idx += 1
            if self.playlist_idx >= len(self.playlist):
                if self.loop_var.get():
                    self.playlist_idx = 0
                else:
                    self._set_status("Playlist finished", "#888")
                    self._stop_play()
                    return
            # Single-item loop (or the next item is the same file we just
            # played): reuse the loaded frames — reloading + re-brightness
            # on every cycle would re-encode the clip forever.
            nxt_path = self.playlist[self.playlist_idx]
            if nxt_path == self.file_path and self.frames:
                self._restart_player()
            else:
                self._playlist_start_item(self.playlist_idx)
        else:
            self._stop_play()

    def _restart_player(self):
        """Restart playback of the current frames (no reload/re-encode)."""
        width, height, _, _, _, _, _ = self._parse_settings()
        self.player = PlayerThread(
            self.lcd, self.frames, self.delays_ms, width, height,
            on_error=self._on_player_error, on_loop=self._on_loop,
            overlay_provider=self.monitor,
            cycles=1, on_complete=self._playlist_advance,
        )
        self.player.start()
        self.play_btn.config(text="Pause")
        self.stop_btn.config(state="normal")
        self._set_status(
            f"Playing {self.playlist_idx + 1}/{len(self.playlist)}: "
            f"{self._short_name(self.file_path)}",
            "#1a8a3a",
        )

    # ---------- Clip cache (load-all-at-once) ----------

    def _load_all_clips(self, brightness=None):
        """Background-load every playlist item into _clip_cache.

        All clips are encoded once at the CURRENT brightness and held in
        RAM (~15MB/clip typical — cheap on 32GB). Switching between items
        is an instant cache lookup: no preload thread, no per-switch
        encode, zero-pause playback with zero background CPU.

        If `brightness` is given (or differs from the cached brightness),
        the cache is rebuilt at that brightness — used when the user
        changes the brightness slider (debounced).
        """
        if brightness is None:
            brightness = int(self.bright_var.get())
        with self._preload_lock:
            if self._cache_brightness == brightness and self._clip_cache:
                return  # cache already matches
            self._cache_brightness = brightness
            self._clip_cache.clear()
            paths = list(self.playlist)

        width, height, rotate, quality, fps, scale, _ = self._parse_settings()
        target = (width, height)

        def worker():
            for i, path in enumerate(paths):
                if path in self._clip_cache:
                    continue
                try:
                    frames, delays, own_zt, meta = self._load_clip_frames(
                        path, target, rotate, quality, scale, fps
                    )
                    # Cache BRIGHTNESS-APPLIED frames so a playlist switch
                    # is a pure lookup (no per-switch re-encode).
                    if not own_zt:
                        frames = [
                            self._apply_brightness_bytes(f, brightness, quality)
                            for f in frames
                        ]
                    self._clip_cache[path] = (frames, delays, own_zt, meta)
                    if i == 0 and not self.file_path:
                        # First clip becomes the selected file for preview
                        def _select_first():
                            if self.playlist_list.size() > 0:
                                self.playlist_list.selection_clear(0, tk.END)
                                self.playlist_list.selection_set(0)
                                self._playlist_on_select()
                        self.after(0, _select_first)
                except Exception:
                    _log_exc("playlist_load")

        threading.Thread(target=worker, daemon=True).start()

    def _clip_from_cache(self, path: str):
        """Return cached frames for path (cached at current brightness,
        already brightness-applied), or load + cache on demand."""
        with self._preload_lock:
            if path in self._clip_cache:
                return self._clip_cache[path]
        width, height, rotate, quality, fps, scale, brightness = self._parse_settings()
        frames, delays, own_zt, meta = self._load_clip_frames(
            path, (width, height), rotate, quality, scale, fps
        )
        if not own_zt:
            frames = [
                self._apply_brightness_bytes(f, brightness, quality)
                for f in frames
            ]
        with self._preload_lock:
            self._clip_cache[path] = (frames, delays, own_zt, meta)
        return frames, delays, own_zt, meta

    def _on_brightness_cache(self, *args):
        """Rebuild the clip cache at the new brightness (debounced so
        slider drags don't fire a rebuild per tick)."""
        job = getattr(self, "_brightness_cache_job", None)
        if job:
            self.after_cancel(job)
        self._brightness_cache_job = self.after(800, self._load_all_clips)
        # Now-playing artwork: invalidate the base so the next poll re-bakes
        # the new brightness (the poller rebuilds it on track-change logic)
        self._np_base_jpeg = None

    def _playlist_start_item(self, idx: int):
        """Load + play one playlist item (single cycle, then advance)."""
        if not self.playlist or idx >= len(self.playlist):
            return
        path = self.playlist[idx]
        self.file_path = path
        self.file_label.config(text=self._short_name(path), foreground="#1a1a1a")
        self.playlist_list.selection_clear(0, tk.END)
        self.playlist_list.selection_set(idx)
        self.playlist_list.see(idx)

        # Use the all-clips cache for instant switching; fall back to a
        # synchronous load if the cache is empty (first play before load).
        frames, delays, own_zt, meta = self._clip_from_cache(path)
        width, height, rotate, quality, fps, scale, brightness = self._parse_settings()
        if not frames:
            self._set_status(f"Load failed: {self._short_name(path)}", "#c0392b")
            self._advance_after_error()
            return

        if meta is not None:
            self._apply_zt_meta(self, meta)
        # Overlay cache is keyed by frame index — stale across items
        if self.monitor is not None:
            self.monitor.invalidate()
        # Cached frames are ALREADY brightness-applied (see _load_all_clips) —
        # a playlist switch is a pure lookup, zero re-encode.
        self._source_frames = list(frames)
        self.frames = list(frames)
        self.delays_ms = delays

        self.player = PlayerThread(
            self.lcd, self.frames, self.delays_ms, width, height,
            on_error=self._on_player_error, on_loop=self._on_loop,
            overlay_provider=self.monitor,
            cycles=1, on_complete=self._playlist_advance,
        )
        self.player.start()
        self.play_btn.config(text="Pause")
        self.stop_btn.config(state="normal")
        self._set_status(
            f"Playing {idx + 1}/{len(self.playlist)}: {self._short_name(path)}",
            "#1a8a3a",
        )
        # Next item is served from the all-clips cache — nothing to preload

    def _advance_after_error(self):
        """Skip a broken playlist item after a short pause."""
        self._set_status("Skipping item…", "#c0392b")
        self.after(800, self._playlist_advance)

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

    # ---------- Now Playing tab ----------

    def _draw_np_placeholder(self, text: str):
        self.np_art_canvas.delete("all")
        self.np_art_canvas.create_text(
            160, 90, text=text, fill="#666", font=("Segoe UI", 10)
        )

    def _nb_ready_set(self):
        self._nb_ready = True

    def _on_tab_changed(self, event=None):
        if not getattr(self, "_nb_ready", False):
            return  # construction-time event; ignore
        nb = event.widget
        # tab identity via index: 0 = Playlist, 1 = Now Playing
        try:
            idx = nb.index(nb.select())
        except Exception:
            idx = 0
        self._np_active = (idx == 1)
        if self._np_poll_job:
            self.after_cancel(self._np_poll_job)
            self._np_poll_job = None
        if self._np_active:
            self._np_poll_job = self.after(500, self._np_poll_once)
        else:
            self._set_np_idle()
            self._np_poll_job = None
            # Artwork no longer has priority (unless auto is checked —
            # then keepalive continues; _np_keepalive checks that)
            if not self.np_auto_var.get() and self._np_keepalive_job:
                self.after_cancel(self._np_keepalive_job)
                self._np_keepalive_job = None

    def _np_auto_changed(self):
        """Auto-display toggled. When checked, start the poller so artwork
        streams to the AIO regardless of the active tab."""
        if self.np_auto_var.get() and self.lcd is not None:
            if self._np_poll_job is None:
                self._np_poll_job = self.after(200, self._np_poll_once)
        else:
            # Auto off: if the tab isn't active, stop the artwork stream
            if not self._np_active and self._np_keepalive_job:
                self.after_cancel(self._np_keepalive_job)
                self._np_keepalive_job = None

    def _np_poll_interval(self) -> int:
        """Seconds between now-playing metadata polls (user-selectable)."""
        label = self.np_poll_var.get() if hasattr(self, "np_poll_var") else "3s"
        return POLL_RATE_SECONDS.get(label, 3)

    # ---------- NPSMSvc watchdog ----------
    # Windows' Now Playing Session Manager Service (NPSMSvc_*) has a known
    # runaway: when a media session is active it can spin at ~100%+ of a
    # core indefinitely (observed 98% avg over 2.4 days -> CPU temp creep
    # 39->48C). Restarting it is safe (Windows respawns it on demand) and
    # works non-elevated. This watchdog checks its CPU periodically and
    # kills it when it sustains high usage.

    def _watchdog_start(self):
        self._watchdog_stop = threading.Event()
        threading.Thread(target=self._watchdog_loop, daemon=True,
                         name="npsmsvc-watchdog").start()

    def _watchdog_stop_now(self):
        getattr(self, "_watchdog_stop", threading.Event()).set()

    def _watchdog_loop(self):
        import subprocess
        import psutil as _psutil

        stop = getattr(self, "_watchdog_stop", threading.Event())
        high_strikes = 0
        handle_strikes = 0
        while not stop.is_set():
            try:
                # Find the NPSMSvc process (name has a per-machine suffix)
                out = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command",
                     "Get-CimInstance Win32_Service | Where-Object { $_.Name -like 'NPSMSvc_*' } | "
                     "ForEach-Object { $_.ProcessId }"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                ).stdout.strip()
                pid = int(out.splitlines()[0]) if out.strip() else None
                if pid is None:
                    high_strikes = 0
                    stop.wait(15)
                    continue
                # Measure its CPU over a 3s window (cpu_percent is since
                # last call; call it once after the wait for the delta).
                p = _psutil.Process(pid)
                p.cpu_percent(interval=None)  # prime the sample
                stop.wait(3)
                avg = p.cpu_percent(interval=None)
                # cpu_percent returns % of ONE core; ~1 core = 100%
                if avg > 40:
                    high_strikes += 1
                    if high_strikes >= 3:
                        _logger.warning(
                            "NPSMSvc runaway: %.0f%% CPU sustained — restarting service", avg)
                        subprocess.run(
                            ["powershell.exe", "-NoProfile", "-Command",
                             "Stop-Process -Id %d -Force" % pid],
                            capture_output=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        high_strikes = 0
                else:
                    high_strikes = 0
                # Handle leak: npsm.dll leaks COM/session handles (~0.4/s
                # baseline; the runaway is a spin loop on leaked state).
                # A big handle count with rising CPU = early spin signal.
                h = p.num_handles()
                if h > 1500:
                    handle_strikes += 1
                    if handle_strikes >= 2:
                        _logger.warning(
                            "NPSMSvc handle leak: %d handles — restarting service", h)
                        subprocess.run(
                            ["powershell.exe", "-NoProfile", "-Command",
                             "Stop-Process -Id %d -Force" % pid],
                            capture_output=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        handle_strikes = 0
                else:
                    handle_strikes = 0
            except Exception:
                _log_exc("watchdog")
            stop.wait(15)

    def _np_poll_once(self):
        """Poll GSMTC on a BACKGROUND thread; never block the tkinter
        mainloop. The winsdk async calls can take ~2s (cross-process COM
        to the media app) — running them on the UI thread made polls
        overlap and stack waits (the 132% CPU / thread-leak bug).

        The UI thread only schedules the fetch + a keepalive render; the
        background thread delivers metadata back via after().
        """
        self._np_poll_job = None
        # Run while the tab is open OR auto-display wants artwork priority
        if not self._np_active and not self.np_auto_var.get():
            return
        if getattr(self, "_np_session_fetching", False):
            # A fetch is in flight; don't stack another (the old bug).
            self._np_poll_job = self.after(
            int(self._np_poll_interval() * 1000), self._np_poll_once)
            return
        # Mark a fetch as in-flight IMMEDIATELY (before the thread starts)
        # so concurrent _np_auto_changed / tab-change callers can't spawn
        # a second poll in the same event-loop iteration (they check
        # _np_poll_job is None, which is true during the fetch window —
        # that race accumulated hundreds of fetch threads, ~23s CPU each).
        self._np_session_fetching = True

        def fetch():
            try:
                from now_playing import _get_active_session
                result = _get_active_session()
            except Exception:
                result = (None, None, None, 0, 0)
            # Store in a thread-safe slot; the main-thread render tick
            # picks it up (tkinter after() can't be called from here).
            self._np_session_result = result
            # Reset the in-flight flag HERE (in the thread that knows it
            # finished), not in _np_session_ready — a race there let a
            # pile-up of fetch threads accumulate (hundreds of threads,
            # each ~23s CPU, temp -> 53C).
            self._np_session_fetching = False

        threading.Thread(target=fetch, daemon=True).start()
        # Ensure the render tick is running (it drains the fetch result
        # and keeps the progress bar smooth between metadata polls)
        if getattr(self, "_np_render_job", None) is None:
            self._np_render_job = self.after(500, self._np_render_tick)

    def _np_session_ready(self):
        """Called on the UI thread (from the render tick) when a background
        GSMTC fetch has landed; renders the artwork frame."""
        self._np_session_fetching = False
        result = self._np_session_result
        self._np_session_result = None
        try:
            from now_playing import (
                _get_thumbnail_pil,
                _format_mmss,
                render_now_playing,
                _redraw_bar,
            )
            session, info, app_id, pos, dur = result
            if info is None or not info.title:
                self._set_np_idle()
                # Auto-fallback: AIO shows the playlist
                self._np_active = False
                # Stop the render tick (no session = nothing to draw)
                if getattr(self, "_np_render_job", None):
                    try:
                        self.after_cancel(self._np_render_job)
                    except Exception:
                        pass
                    self._np_render_job = None
                if self.player is None and self.frames:
                    # resume playlist? just stop the now-playing stream
                    pass
                self._np_poll_job = self.after(
            int(self._np_poll_interval() * 1000), self._np_poll_once)
                return

            title = info.title or ""
            artist = info.artist or ""
            key = f"{app_id}|{title}|{artist}"
            # Store the freshest metadata + timestamps so the cheap render
            # tick can interpolate position between GSMTC polls.
            self._np_meta = (title, artist, key)
            self._np_duration = dur
            self._np_pos_base = pos
            self._np_pos_time = time.monotonic()

            # Art refresh (only when the track changed)
            if key != self._np_last_key:
                self._np_last_key = key
                # winsdk's async thumbnail open fails under tkinter's
                # single-threaded mainloop (WinError -2147483634). Fetch
                # the art on a background thread and deliver the PIL image
                # back to the UI thread via after().
                # Guard: never spawn a second fetch while one is in flight
                # (a hung winsdk fetch used to leak one thread + GDI handle
                # per poll — measured 9,780 threads / 63K handles / 1.6GB).
                if getattr(self, "_np_fetch_inflight", False):
                    # Keep the current art; just don't spawn another thread
                    pass
                else:
                    self._np_fetch_inflight = True

                    def fetch_art():
                        art = None
                        try:
                            art = _get_thumbnail_pil(info)
                        except Exception:
                            art = None
                        finally:
                            # ALWAYS reset the flag — a hung winsdk fetch
                            # must not block art loading for every future
                            # track (that froze the artwork on the old
                            # song). Reset before marshaling so the next
                            # poll can start even if after() fails.
                            self._np_fetch_inflight = False
                        # Deliver via the thread-safe result slot; the
                        # render tick drains it (after() from a worker
                        # thread is not reliable under tkinter).
                        self._np_art_result = (key, art)

                    threading.Thread(target=fetch_art, daemon=True).start()

            # Text + progress (every poll)
            self.np_title_label.config(text=title)
            self.np_artist_label.config(text=artist)
            if dur > 0:
                self.np_progress.config(maximum=dur, value=min(pos, dur))
                self.np_time_label.config(
                    text=f"{_format_mmss(pos)} / {_format_mmss(dur)}"
                )
            else:
                self.np_progress.config(maximum=100, value=0)
                self.np_time_label.config(text="")

            # Mirror to the AIO (now-playing render) if connected.
            # Priority: auto-display checked OR now-playing tab active.
            want_art = self.np_auto_var.get() or self._np_active
            if self.lcd is not None and want_art and self._np_last_art is not None:
                w, h = 1600, 720
                # Monitor overlay: poll sensors + rebuild the sprite when the
                # visible text changes. Sensors are polled every 2s (they
                # change ~0.7x/sec anyway) to avoid bursty CPU spikes.
                if self.monitor_var.get():
                    now = time.monotonic()
                    if now - getattr(self, "_np_sensor_last", 0) >= 2.0:
                        self._np_sensor_last = now
                        try:
                            if self._np_sensor is None:
                                from usblcd.sensors import SensorMonitor
                                self._np_sensor = SensorMonitor(
                                    auto_launch_lhm=self.lhm_auto_var.get()
                                )
                            r = self._np_sensor.read()
                            text = (
                                f"{r.cpu_freq_mhz/1000:.2f}" if r.cpu_freq_mhz else "-",
                                f"{r.cpu_temp_c:.0f}" if r.cpu_temp_c else "-",
                                f"{round(r.gpu_freq_mhz/50)*50}" if r.gpu_freq_mhz else "-",
                                f"{r.gpu_temp_c:.0f}" if r.gpu_temp_c else "-",
                                self.overlay_pos_var.get(),
                                self.overlay_font_var.get(),
                            )
                            if text != self._np_sprite_text:
                                self._np_sprite_text = text
                                from usblcd.frames import build_overlay_sprite
                                rot_s = str(self.rot_var.get()).replace("°", "").strip()
                                rotate = int(rot_s) if rot_s.isdigit() else 0
                                self._np_sprite = build_overlay_sprite(
                                    r, w, h, rotate,
                                    self.overlay_pos_var.get(),
                                    OVERLAY_FONT_SCALE.get(self.overlay_font_var.get(), 1.0),
                                )
                        except Exception:
                            _log_exc("sensor_poll")
                try:
                    # Rot values come as "180°" — strip the degree symbol
                    rot_s = str(self.rot_var.get()).replace("°", "").strip()
                    rotate = int(rot_s) if rot_s.isdigit() else 0
                    # Render the full frame only on track change; position
                    # ticks reuse the cheap _redraw_bar path (decode base
                    # + bar strip, ~5ms vs ~50ms full render). The base is
                    # rebuilt when the track changes OR it was invalidated
                    # (e.g. brightness changed mid-track).
                    if (self._np_last_key == key
                            and self._np_base_jpeg is not None):
                        img = _redraw_bar(
                            self._np_last_art, title, artist, w, h, rotate,
                            pos, dur, self._np_base_jpeg,
                        )
                    else:
                        img = render_now_playing(
                            self._np_last_art, title, artist, w, h, rotate,
                            pos, dur,
                        )
                        # Cache the clean base (no bar) for the cheap path.
                        # Brightness is baked into the base (track-change
                        # only, +2.9ms) so position ticks stay cheap —
                        # _redraw_bar decodes the already-dimmed base.
                        buf = io.BytesIO()
                        base_img = render_now_playing(
                            self._np_last_art, title, artist, w, h, rotate,
                            0, 0, draw_bar=False,
                        )
                        brightness = self.bright_var.get()
                        if brightness < 100:
                            from usblcd.frames import apply_brightness
                            base_img = apply_brightness(base_img, brightness)
                        base_img.save(buf, format="JPEG", quality=92)
                        self._np_base_jpeg = buf.getvalue()
                    # Overlay: paste the monitor sprite onto the artwork frame
                    # when Monitor overlay is enabled (same as GIF playback).
                    if self.monitor_var.get() and self._np_sprite is not None:
                        block, paste = self._np_sprite
                        img.paste(block, paste, block)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=92)
                    self._np_last_frame = jpeg_to_frame(buf.getvalue(), w, h)
                    self._np_send(self._np_last_frame)
                    # Keepalive: re-send every 100ms so the AIO stays lit.
                    self._np_start_keepalive()
                except Exception:
                    _log_exc("session_ready_render")
        except Exception:
            _log_exc("session_ready")
        # Poll every 3s, not 1s: _get_active_session's winsdk async can take
        # ~2s to resolve (the _sync wait). A 1s schedule overlaps polls,
        # stacking _sync waits on the tkinter thread and leaking a thread
        # per poll (measured: 9,780 threads / 1.6GB after ~1h). Position
        # smoothness is preserved by the 500ms _np_render_tick between polls.
        self._np_poll_job = self.after(
            int(self._np_poll_interval() * 1000), self._np_poll_once)

    def _np_render_tick(self):
        """Cheap per-tick render (every 500ms): interpolate the position
        from the last GSMTC poll + monotonic clock, then redraw only the
        bar strip via _redraw_bar (~5ms). No GSMTC, no threads — this is
        what keeps the progress bar smooth between 3s metadata polls.

        Also picks up a background session fetch result when one landed
        (the fetch thread can't call tkinter after(), so the result is
        stored in a slot this tick drains)."""
        self._np_render_job = None
        if not (self.np_auto_var.get() or self._np_active):
            return
        # Health check: the fetch-thread pile-up (hundreds of threads,
        # each ~23s CPU, temp -> 53C) raised NO exception — it was silent.
        # Log an early warning when the thread count grows abnormally so
        # a leak is visible minutes after it starts, not hours later.
        try:
            import threading as _th
            n = _th.active_count()
            last = getattr(self, "_np_threads_last", None)
            if last is not None and n > 50 and n > last:
                _logger.warning("thread count growing: %d (was %d) — possible leak", n, last)
            self._np_threads_last = n
        except Exception:
            _log_exc("thread_telemetry")
        # Drain a completed background session fetch, if any
        if getattr(self, "_np_session_result", None) is not None:
            self._np_session_ready()
        # Drain a completed art fetch, if any (thread-safe slot)
        ar = getattr(self, "_np_art_result", None)
        if ar is not None:
            self._np_art_result = None
            akey, aart = ar
            try:
                self._np_art_ready(akey, aart)
            except Exception:
                _log_exc("art_ready")
        meta = getattr(self, "_np_meta", None)
        if meta is None or self.lcd is None or self._np_last_art is None:
            # Nothing to render yet; keep ticking
            self._np_render_job = self.after(500, self._np_render_tick)
            return
        try:
            title, artist, _key = meta
            dur = getattr(self, "_np_duration", 0) or 0
            base = getattr(self, "_np_pos_base", 0) or 0
            t0 = getattr(self, "_np_pos_time", time.monotonic())
            pos = base + (time.monotonic() - t0)
            if dur > 0 and pos > dur:
                pos = dur  # clamp; next poll will confirm track end
            if dur > 0 and self._np_base_jpeg is not None:
                from now_playing import _redraw_bar
                # Guard: a corrupt base (failed encode) makes every tick
                # throw UnidentifiedImageError, which the except below
                # swallows -> a hot raise/catch loop at 2/sec (measured
                # 66% CPU from PyErr_PrintEx churn). Validate once and
                # drop the bad base so the next session poll rebuilds it.
                base_jpeg = self._np_base_jpeg
                if not base_jpeg.startswith(b"\xff\xd8"):
                    self._np_base_jpeg = None  # rebuild on next poll
                    self._np_render_job = self.after(500, self._np_render_tick)
                    return
                w, h = 1600, 720
                rot_s = str(self.rot_var.get()).replace("°", "").strip()
                rotate = int(rot_s) if rot_s.isdigit() else 0
                img = _redraw_bar(
                    self._np_last_art, title, artist, w, h, rotate,
                    pos, dur, base_jpeg,
                )
                # Overlay sprite (same as GIF playback)
                if self.monitor_var.get() and self._np_sprite is not None:
                    block, paste = self._np_sprite
                    img.paste(block, paste, block)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=92)
                self._np_last_frame = jpeg_to_frame(buf.getvalue(), w, h)
                self._np_send(self._np_last_frame)
                # Keepalive follows (the 100ms re-send uses this frame)
                self._np_start_keepalive()
        except Exception:
            _log_exc("render_tick")
        # Always reschedule (the tick is the smooth-bar heartbeat; an
        # exception must not kill it — the corrupt-base guard handles
        # the persistent-failure case, logging makes it visible).
        self._np_render_job = self.after(500, self._np_render_tick)

    def _np_send_ensure_writer(self):
        """Start the background USB writer thread if not running.
        The writer drains the latest-frame slot so the main thread never
        blocks on a USB write (a slow write used to stall the tkinter
        mainloop: 258% CPU)."""
        if getattr(self, "_np_writer", None) is not None:
            return
        import queue as _queue
        self._np_frame_q = _queue.Queue(maxsize=1)  # latest frame only
        self._np_writer_stop = threading.Event()

        def writer():
            q = self._np_frame_q
            stop = self._np_writer_stop
            lcd = None
            while not stop.is_set():
                try:
                    frame = q.get(timeout=0.5)
                except Exception:
                    continue  # empty; loop (also lets stop event be seen)
                if frame is None:
                    continue
                # Drain to the latest frame (keepalive only needs freshness)
                latest = frame
                while True:
                    try:
                        latest = q.get_nowait()
                    except Exception:
                        break
                # Read the LCD fresh EVERY loop: the writer must not cache
                # the device object across disconnect/reconnect. A stale
                # reference (closed LCD A) made send_frame throw on the
                # new connection — silently swallowed, so frames vanished
                # with no log and no artwork (reconnect bug).
                lcd = self.lcd
                if lcd is not None:
                    try:
                        lcd.send_frame(latest)
                    except Exception:
                        _log_exc("np_writer_send")

        t = threading.Thread(target=writer, daemon=True, name="np-usb-writer")
        t.start()
        self._np_writer = t

    def _np_send(self, frame: bytes):
        """Publish a frame for the background writer (never blocks)."""
        self._np_send_ensure_writer()
        q = getattr(self, "_np_frame_q", None)
        if q is None:
            return
        try:
            if q.full():
                # Keep only the newest frame
                try:
                    q.get_nowait()
                except Exception:
                    pass
            q.put_nowait(frame)
        except Exception:
            _log_exc("np_send_queue")

    def _np_start_keepalive(self):
        """Start the keepalive loop, but only if no other path already did.
        Uses a generation counter: each caller bumps the generation; the
        loop only continues if it's still the latest generation. This is
        race-free where a bare None-check wasn't (a None-check let two
        keepalive loops start, doubling USB traffic + CPU)."""
        gen = getattr(self, "_np_keepalive_gen", 0) + 1
        self._np_keepalive_gen = gen
        if self._np_keepalive_job is not None:
            return  # someone already runs it
        self._np_keepalive_job = self.after(100, self._np_keepalive)

    def _np_keepalive(self):
        """Re-send the last now-playing frame every 100ms so the AIO panel
        doesn't power down between polls. The actual USB write happens on
        the background writer thread — never on the mainloop."""
        gen = getattr(self, "_np_keepalive_gen", 0)
        self._np_keepalive_job = None
        if not (self.np_auto_var.get() or self._np_active):
            return  # artwork no longer has priority
        if self.lcd is not None and self._np_last_frame is not None:
            self._np_send(self._np_last_frame)
            if getattr(self, "_np_keepalive_gen", 0) == gen:
                self._np_keepalive_job = self.after(100, self._np_keepalive)

    def _np_art_ready(self, key, art):
        """Called on the UI thread when the background art fetch completes."""
        if key != self._np_last_key:
            return  # track changed since; stale
        self._np_last_art = art
        self._np_base_jpeg = None  # new track -> rebuild the clean base
        if art is not None:
            # Skip the tab thumbnail render when the same art is already
            # streaming to the AIO — redundant (and costs CPU/GPU).
            if self._np_streaming_to_aio():
                self._draw_np_placeholder("Streaming to AIO…")
                return
            thumb = art.copy()
            thumb.thumbnail((300, 170))
            self._np_art_photo = ImageTk.PhotoImage(thumb)
            self.np_art_canvas.delete("all")
            self.np_art_canvas.create_image(160, 90, image=self._np_art_photo)
        else:
            self._draw_np_placeholder("No artwork")

    def _set_np_idle(self):
        self.np_title_label.config(text="—")
        self.np_artist_label.config(text="")
        self.np_progress.config(maximum=100, value=0)
        self.np_time_label.config(text="0:00 / 0:00")
        self._draw_np_placeholder("No media playing")
        self._np_last_key = None
        self._np_last_art = None
        if self._np_keepalive_job:
            self.after_cancel(self._np_keepalive_job)
            self._np_keepalive_job = None

    def _on_close_np(self):
        if self._np_poll_job:
            self.after_cancel(self._np_poll_job)
            self._np_poll_job = None
        if self._np_keepalive_job:
            self.after_cancel(self._np_keepalive_job)
            self._np_keepalive_job = None
        if getattr(self, "_np_render_job", None):
            try:
                self.after_cancel(self._np_render_job)
            except Exception:
                pass
            self._np_render_job = None
        # Stop the background USB writer
        if getattr(self, "_np_writer", None) is not None:
            try:
                self._np_writer_stop.set()
            except Exception:
                pass
            self._np_writer = None
            self._np_frame_q = None

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
                # .zt has no timing; play at the fixed theme rate
                self._preview_delay = max(10, int(1000 / ZT_PLAY_FPS))
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
        # Don't animate the preview while the same content plays on the
        # AIO — it's redundant and costs ~0.09 cores of CPU. Covers both
        # GIF playback (player active) and artwork streaming (auto-display).
        # Also pause when NOT connected and nothing is playing: at startup
        # the persisted playlist auto-loads and the preview would otherwise
        # loop GIF frame decodes forever (~10% CPU) for no viewer benefit.
        if (self.player is not None or self._np_streaming_to_aio()
                or (self.lcd is None and self.player is None)):
            self._preview_job = self.after(200, self._animate_preview)
            return
        self._preview_idx = (self._preview_idx + 1) % total
        self._render_preview_frame(self._preview_idx)
        delay = getattr(self, "_preview_delay", 80)
        self._preview_job = self.after(delay, self._animate_preview)

    def _np_streaming_to_aio(self) -> bool:
        """True when the now-playing artwork is currently being streamed
        to the AIO (auto-display on with art available + connected)."""
        return (
            self.lcd is not None
            and self.np_auto_var.get()
            and self._np_last_art is not None
        )

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
        fps = ZT_PLAY_FPS  # .zt themes play at a fixed rate (GIFs self-time)
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
        from usblcd import theme

        c = self.bright_canvas
        c.delete("all")
        w = 134
        h = 22
        edge = 12
        bar_w = w - 2 * edge  # 110px visible bar
        val = int(self.bright_var.get())
        fill_w = max(2, int(bar_w * val / 100))
        # Visible bar background (darker, distinct from snap zones)
        c.create_rectangle(edge, 1, w - edge, h - 1, fill="#2a2a34", outline="")
        # Fill portion
        c.create_rectangle(edge, 1, edge + fill_w, h - 1, fill=theme.ACCENT, outline="")
        # Tick marks at 0/25/50/75/100
        for frac in (0, 0.25, 0.5, 0.75, 1.0):
            x = int(edge + bar_w * frac)
            c.create_line(x, 1, x, h - 1, fill=theme.BORDER, width=1)
        # Current value label
        text_x = max(edge + 4, edge + fill_w - 14)
        c.create_text(text_x, h // 2, text=str(val),
                      fill="#ffffff" if val > 30 else "#9a9aa5",
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

    @staticmethod
    def _load_clip_frames(path, target, rotate, quality, scale, fps):
        """Pure frame loader — NO UI access, safe for background threads.

        Returns (frames, delays, own_zt, meta) where meta is the settings
        dict from an own-format .zt (None otherwise) that the caller must
        apply to the UI on the main thread.
        """
        from usblcd.ztfile import zt_parse, zt_to_frames

        low = path.lower()
        frames, delays = [], []
        own_zt = False
        meta = None
        if low.endswith(".zt"):
            with open(path, "rb") as f:
                data = f.read()
            meta = zt_parse(data)
            if meta is not None:
                own_zt = True
                frames = zt_to_frames(data)
                delays = [int(1000 / meta["fps"])] * len(frames)
            else:
                pos = 0
                while True:
                    idx = data.find(b"\xFF\xD8", pos)
                    if idx < 0:
                        break
                    eoi = data.find(b"\xFF\xD9", idx)
                    if eoi < 0:
                        break
                    img = Image.open(io.BytesIO(data[idx : eoi + 2])).convert("RGB")
                    frames.append(LCDApp._encode(img, target, rotate, quality, scale, 100))
                    delays.append(int(1000 / fps))
                    pos = eoi + 2
        elif low.endswith(".gif"):
            src = Image.open(path)
            for frame in ImageSequence.Iterator(src):
                rgb = frame.convert("RGB")
                canvas = LCDApp._scale(rgb, target, scale)
                if rotate:
                    canvas = canvas.rotate(-rotate, expand=True)
                    canvas = LCDApp._scale(canvas, target, scale)
                frames.append(LCDApp._encode(canvas, target, 0, quality, scale, 100))
                d = frame.info.get("duration", 0)
                delays.append(max(10, int(d)) if d else 100)
        else:  # static image
            img = Image.open(path).convert("RGB")
            img = LCDApp._scale(img, target, scale)
            if rotate:
                img = img.rotate(-rotate, expand=True)
                img = LCDApp._scale(img, target, scale)
            frames.append(LCDApp._encode(img, target, 0, quality, scale, 100))
            delays.append(1000)
        return frames, delays, own_zt, meta

    @staticmethod
    def _apply_zt_meta(app, meta):
        """Restore GUI controls from an own-format .zt header (main thread)."""
        if meta is None:
            return
        app.rot_var.set(f"{meta['rotate']}°")
        app.scale_var.set(SCALE_MODES[["fit", "fill", "stretch"].index(meta["scale"])])
        app.bright_var.set(meta["brightness"])
        res = f"{meta['width']} x {meta['height']}"
        if res in RESOLUTIONS:
            app.res_var.set(res)

    def _load_clip(self):
        """Load + pre-encode frames from the selected file (main thread)."""
        if not self.file_path:
            return False
        width, height, rotate, quality, fps, scale, brightness = self._parse_settings()
        target = (width, height)
        frames, delays, own_zt, meta = self._load_clip_frames(
            self.file_path, target, rotate, quality, scale, fps
        )
        if meta is not None:
            self._apply_zt_meta(self, meta)

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
            # Auto-display artwork: start the now-playing poller so the AIO
            # shows album art immediately (no tab click needed).
            if self.np_auto_var.get() and self._np_poll_job is None:
                self._np_poll_job = self.after(200, self._np_poll_once)
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
        # Stop the now-playing stream (poller + keepalive)
        if self._np_poll_job:
            self.after_cancel(self._np_poll_job)
            self._np_poll_job = None
        if self._np_keepalive_job:
            self.after_cancel(self._np_keepalive_job)
            self._np_keepalive_job = None
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

        # Auto-display artwork priority: if checked and we have a track +
        # artwork, Play shows the now-playing stream instead of the playlist.
        if self.np_auto_var.get() and self._np_last_art is not None:
            self._set_status("Showing now-playing artwork", "#1a6fb0")
            # Ensure the np poller is streaming to the AIO
            if self._np_poll_job is None:
                self._np_poll_job = self.after(200, self._np_poll_once)
            return

        # Playlist mode: play items in order, one cycle each
        if self.playlist:
            self._stop_play_requested = False
            self.playlist_idx = 0
            # Recreate the monitor (bound to current frame list)
            self.monitor = None
            if self.monitor_var.get():
                width, height, _, _, _, _, _ = self._parse_settings()
                self.monitor = MonitorThread(
                    self, self.frames, self.delays_ms, width, height,
                    on_status=self._set_status,
                )
                self.monitor.start()
            self._playlist_start_item(0)
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
        self._stop_play_requested = True
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

    def _on_player_error(self, msg):
        self.after(0, lambda: (self._stop_play(),
                               self._set_status(msg, "#c0392b")))

    def _on_loop(self, loop):
        self.after(0, lambda: self._set_status(f"Playing… loop {loop}", "#1a8a3a"))

    def _set_status(self, text, color):
        self.status_label.config(text=text, fg=color)

    def _on_close(self):
        self._save_config()
        self._stop_preview()
        self._stop_play()
        self._on_close_np()
        self._watchdog_stop_now()
        if self.lcd is not None:
            try:
                self.lcd.close()
            except Exception:
                pass
        self.destroy()

    # ---------- Config persistence ----------

    CONFIG_PATH = os.path.join(_PROJECT_ROOT_DIR, "config.json")

    def _schedule_config_save(self, *args):
        """Debounced config save (avoids a write per combobox keystroke)."""
        if getattr(self, "_cfg_job", None):
            self.after_cancel(self._cfg_job)
        self._cfg_job = self.after(500, self._save_config)

    def _save_config(self):
        """Persist the playlist + display settings (called on change/close)."""
        import json

        cfg = {
            "playlist": list(self.playlist),
            "settings": {
                "resolution": self.res_var.get(),
                "rotation": self.rot_var.get(),
                "quality": self.qual_var.get(),
                "scale": self.scale_var.get(),
                "brightness": self.bright_var.get(),
                "loop": self.loop_var.get(),
                "monitor": self.monitor_var.get(),
                "auto_art": self.np_auto_var.get(),
                "auto_lhm": self.lhm_auto_var.get(),
                "poll_rate": self.np_poll_var.get(),
                "overlay_pos": self.overlay_pos_var.get(),
                "overlay_font": self.overlay_font_var.get(),
            },
        }
        try:
            with open(self.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except OSError:
            pass

    def _load_config(self):
        """Restore the playlist + settings saved by a previous run."""
        import json

        try:
            with open(self.CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            return
        s = cfg.get("settings", {})
        self.res_var.set(s.get("resolution", self.res_var.get()))
        self.rot_var.set(s.get("rotation", self.rot_var.get()))
        self.qual_var.set(s.get("quality", self.qual_var.get()))
        self.scale_var.set(s.get("scale", self.scale_var.get()))
        self.bright_var.set(s.get("brightness", self.bright_var.get()))
        self.loop_var.set(s.get("loop", self.loop_var.get()))
        self.monitor_var.set(s.get("monitor", self.monitor_var.get()))
        self.np_auto_var.set(s.get("auto_art", self.np_auto_var.get()))
        self.lhm_auto_var.set(s.get("auto_lhm", self.lhm_auto_var.get()))
        self.np_poll_var.set(s.get("poll_rate", self.np_poll_var.get()))
        self.overlay_pos_var.set(s.get("overlay_pos", self.overlay_pos_var.get()))
        self.overlay_font_var.set(
            s.get("overlay_font", self.overlay_font_var.get())
        )
        # Playlist: restore files that still exist
        for p in cfg.get("playlist", []):
            if os.path.isfile(p) and p not in self.playlist:
                self.playlist.append(p)
                self.playlist_list.insert(tk.END, os.path.basename(p))
        if self.playlist:
            self._load_all_clips()


def main():
    app = LCDApp()
    app.mainloop()


if __name__ == "__main__":
    sys.exit(main())
