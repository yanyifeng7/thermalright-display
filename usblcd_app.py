#!/usr/bin/env python3
"""usblcd-display GUI — pick a GIF/image, push it to the AIO LCD screen.

Zero-dependency desktop app (tkinter ships with Python).
"""

from __future__ import annotations

import io
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageSequence

from usblcd.device import USBLCD, LCDDeviceError
from usblcd.frames import jpeg_to_frame

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


class PlayerThread(threading.Thread):
    """Background thread that streams frames to the LCD."""

    def __init__(self, lcd: USBLCD, frames: list[bytes], delays_ms: list[int],
                 width: int, height: int, on_error, on_loop):
        super().__init__(daemon=True)
        self.lcd = lcd
        self.frames = frames
        self.delays_ms = delays_ms
        self.width = width
        self.height = height
        self.on_error = on_error
        self.on_loop = on_loop
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        n = len(self.frames)
        loop = 0
        while not self._stop.is_set():
            loop += 1
            for frame, delay in zip(self.frames, self.delays_ms):
                if self._stop.is_set():
                    return
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


class LCDApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} — AIO LCD player")
        self.geometry("640x760")
        self.resizable(False, False)
        self.configure(bg="#f0f0f0")

        self.file_path: str | None = None
        self.frames: list[bytes] = []
        self.delays_ms: list[int] = []
        self.player: PlayerThread | None = None
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

        ttk.Label(settings, text="Loop:").grid(row=row, column=2, sticky="w", padx=10, pady=4)
        self.loop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(settings, text="Repeat forever", variable=self.loop_var).grid(
            row=row, column=3, sticky="w", pady=4)

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
        self._preview_total = 1
        self._draw_preview_placeholder("No preview")

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

    def _draw_preview_placeholder(self, text: str):
        self.preview_canvas.delete("all")
        self.preview_canvas.create_text(
            160, 72, text=text, fill="#666", font=("Segoe UI", 10)
        )

    def _load_preview(self, path: str):
        """Show the first frame; animate the FULL GIF/theme in preview."""
        if self._preview_job:
            self.after_cancel(self._preview_job)
            self._preview_job = None
        self._preview_frames = []
        self._preview_idx = 0
        self._preview_src = None      # lazy source handle
        self._preview_slices = []     # .zt: list of (start, end) byte ranges
        self._preview_total = 1

        low = path.lower()
        try:
            if low.endswith(".gif"):
                self._load_gif_preview(path)
            elif low.endswith(".zt"):
                self._load_zt_preview(path)
            else:
                self._load_static_preview(path)
        except Exception as e:
            self._draw_preview_placeholder(f"Preview failed: {e}")

    def _photo_from_image(self, img: Image.Image) -> tk.PhotoImage:
        """Scale an image to the preview canvas and convert to PhotoImage."""
        img = img.convert("RGB")
        cw, ch = 320, 144
        iw, ih = img.size
        scale = min(cw / iw, ch / ih)
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        img = img.resize((nw, nh), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return tk.PhotoImage(data=buf.getvalue())

    def _show_preview_frame(self, photo: tk.PhotoImage):
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(160, 72, image=photo)
        self._preview_photo = photo  # keep reference

    def _load_static_preview(self, path: str):
        img = Image.open(path)
        self._show_preview_frame(self._photo_from_image(img))

    def _load_gif_preview(self, path: str):
        src = Image.open(path)
        self._preview_src = src
        self._preview_total = getattr(src, "n_frames", 1) or 1
        self._show_preview_frame(self._photo_from_image(src))
        if self._preview_total > 1:
            self._animate_preview()

    def _load_zt_preview(self, path: str):
        with open(path, "rb") as f:
            data = f.read()
        # Lazy: store byte ranges, decode one per tick
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
        first = Image.open(io.BytesIO(data[slices[0][0] : slices[0][1]]))
        self._show_preview_frame(self._photo_from_image(first))
        if len(slices) > 1:
            self._animate_preview()

    def _animate_preview(self):
        total = self._preview_total
        if total < 2:
            return
        self._preview_idx = (self._preview_idx + 1) % total
        try:
            if self._preview_src is not None:
                self._preview_src.seek(self._preview_idx)
                frame = self._preview_src.copy()
                self._show_preview_frame(self._photo_from_image(frame))
            elif self._preview_slices:
                s, e = self._preview_slices[self._preview_idx]
                frame = Image.open(io.BytesIO(self._preview_zt_data[s:e]))
                self._show_preview_frame(self._photo_from_image(frame))
        except Exception:
            pass  # skip bad frame, keep animating
        self._preview_job = self.after(80, self._animate_preview)

    def _stop_preview(self):
        if self._preview_job:
            self.after_cancel(self._preview_job)
            self._preview_job = None
        self._preview_frames = []
        self._preview_src = None
        self._preview_slices = []

    def _parse_settings(self):
        res = self.res_var.get().replace(" ", "").split("x")
        width, height = int(res[0]), int(res[1])
        rotate = int(self.rot_var.get().replace("°", ""))
        quality = int(self.qual_var.get().split("(")[1].rstrip(")"))
        fps = int(self.fps_var.get())
        scale = self.scale_var.get().split(" ")[0].lower()
        return width, height, rotate, quality, fps, scale

    def _load_clip(self):
        """Load + pre-encode frames from the selected file."""
        if not self.file_path:
            return False
        width, height, rotate, quality, fps, scale = self._parse_settings()
        target = (width, height)
        path = self.file_path
        low = path.lower()

        frames, delays = [], []
        if low.endswith(".zt"):
            with open(path, "rb") as f:
                data = f.read()
            pos = 0
            while True:
                idx = data.find(b"\xFF\xD8", pos)
                if idx < 0:
                    break
                eoi = data.find(b"\xFF\xD9", idx)
                if eoi < 0:
                    break
                img = Image.open(io.BytesIO(data[idx : eoi + 2])).convert("RGB")
                frames.append(self._encode(img, target, rotate, quality, scale))
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
                frames.append(self._encode(canvas, target, 0, quality, scale))
                d = frame.info.get("duration", 0)
                delays.append(max(10, int(d)) if d else 100)
        else:  # static image
            img = Image.open(path).convert("RGB")
            img = self._scale(img, target, scale)
            if rotate:
                img = img.rotate(-rotate, expand=True)
                img = self._scale(img, target, scale)
            frames.append(self._encode(img, target, 0, quality, scale))
            delays.append(1000)

        if not frames:
            return False
        self.frames, self.delays_ms = frames, delays
        return True

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
    def _encode(img, target, rotate, quality, scale="fit"):
        img = img.convert("RGB")
        if rotate:
            img = img.rotate(-rotate, expand=True)
            img = LCDApp._scale(img, target, scale)
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
        width, height, _, _, _, _ = self._parse_settings()
        total = sum(len(f) for f in self.frames)
        self.player = PlayerThread(
            self.lcd, self.frames, self.delays_ms, width, height,
            on_error=self._on_player_error, on_loop=self._on_loop,
        )
        self.player.start()
        self.play_btn.config(text="Pause")
        self.stop_btn.config(state="normal")
        self._set_status(f"Playing: {n} frames, ~{total // n} KB/frame", "#1a8a3a")

    def _stop_play(self):
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
