#!/usr/bin/env python3
"""Play GIF/theme animations on the USB LCD display.

Supports:
  * Animated GIFs (frame timing respected, composited properly)
  * .zt theme files (TRCC MJPEG container — already-JPEG frames)
  * Static images (single frame)

Usage:
    python gif_player.py --gif anim.gif --width 1600 --height 720 --rotate 180
    python gif_player.py --gif theme.zt --width 1600 --height 720
    python gif_player.py --gif still.jpg --width 1600 --height 720
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from dataclasses import dataclass, field

from PIL import Image, ImageSequence

from usblcd.device import USBLCD, LCDDeviceError
from usblcd.frames import jpeg_to_frame


@dataclass
class Clip:
    """A sequence of pre-encoded JPEG frames with per-frame delays (ms)."""

    frames: list[bytes] = field(default_factory=list)
    delays_ms: list[int] = field(default_factory=list)


def load_gif(path: str, target: tuple[int, int], rotate: int, quality: int) -> Clip:
    """Load an animated GIF, letterbox to target, encode frames as JPEG."""
    src = Image.open(path)
    clip = Clip()
    # PIL's duration per frame after seek; some GIFs only set it on frame 0
    for i, frame in enumerate(ImageSequence.Iterator(src)):
        rgb = frame.convert("RGB")
        canvas = fit_image(rgb, target)
        if rotate:
            canvas = canvas.rotate(-rotate, expand=True)
            # Rotation of a letterboxed frame needs re-fit to keep dims exact
            canvas = fit_image(canvas, target)
        clip.frames.append(jpeg_encode(canvas, quality))
        d = frame.info.get("duration", 0)
        clip.delays_ms.append(max(10, int(d)) if d else 100)
    return clip


def load_zt(path: str, target: tuple[int, int], quality: int, fps: int, rotate: int = 0) -> Clip:
    """Load a .zt theme file (MJPEG container: sequential JPEG frames).

    NOTE: raw .zt JPEG bytes are REJECTED by the display decoder (verified).
    TRCC always re-encodes via its JPEG encoder before sending — we do the
    same with PIL to match the device's accepted format.
    """
    with open(path, "rb") as f:
        data = f.read()
    clip = Clip()
    pos = 0
    while True:
        idx = data.find(b"\xFF\xD8", pos)
        if idx < 0:
            break
        eoi = data.find(b"\xFF\xD9", idx)
        if eoi < 0:
            break
        jpeg = data[idx : eoi + 2]
        # Re-encode through PIL — the device rejects raw .zt JPEG bytes
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        img = fit_image(img, target)
        if rotate:
            img = img.rotate(-rotate, expand=True)
            img = fit_image(img, target)
        clip.frames.append(jpeg_encode(img, quality))
        clip.delays_ms.append(int(1000 / fps))
        pos = eoi + 2
    return clip


def load_static(path: str, target: tuple[int, int], rotate: int, quality: int) -> Clip:
    img = Image.open(path).convert("RGB")
    img = fit_image(img, target)
    if rotate:
        img = img.rotate(-rotate, expand=True)
        img = fit_image(img, target)
    return Clip(frames=[jpeg_encode(img, quality)], delays_ms=[1000])


def jpeg_encode(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def fit_image(img: Image.Image, target: tuple[int, int]) -> Image.Image:
    """Scale image to fit target, letterbox with black."""
    tw, th = target
    iw, ih = img.size
    scale = min(tw / iw, th / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", target, (0, 0, 0))
    canvas.paste(img, ((tw - nw) // 2, (th - nh) // 2))
    return canvas


def play(clip: Clip, width: int, height: int, variant: str, repeat: int) -> int:
    """Send frames in a loop. repeat=-1 = infinite."""
    n = len(clip.frames)
    if n == 0:
        print("ERROR: no frames", file=sys.stderr)
        return 1

    total_len = sum(len(f) for f in clip.frames)
    print(f"Loaded: {n} frames, avg {total_len // n} bytes/frame, "
          f"~{n * 1000 / max(clip.delays_ms) / 1000:.1f} fps native")

    loops = 0
    try:
        with USBLCD(variant) as lcd:
            print(f"Device opened: {variant} ({lcd.spec['vid']:04X}:{lcd.spec['pid']:04X})")
            resp = lcd.handshake()
            print(f"Handshake OK ({len(resp)} bytes response)")

            while repeat == -1 or loops < repeat:
                loops += 1
                for i, (frame, delay) in enumerate(zip(clip.frames, clip.delays_ms)):
                    t0 = time.monotonic()
                    try:
                        # Wrap raw JPEG in the 64-byte PICTURE header
                        lcd.send_frame(jpeg_to_frame(frame, width, height))
                    except Exception as e:
                        print(f"  send failed ({e}), reconnecting...", flush=True)
                        if not reconnect(lcd):
                            print("  reconnect failed, giving up", file=sys.stderr, flush=True)
                            return 1
                    # Pace to the frame delay
                    elapsed = time.monotonic() - t0
                    remain = delay / 1000 - elapsed
                    if remain > 0:
                        time.sleep(remain)
                print(f"  loop {loops} done ({n} frames)", flush=True)
    except LCDDeviceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped by user")
    return 0


def reconnect(lcd: USBLCD) -> bool:
    """Re-open the device after a send failure."""
    import time as _t

    for _ in range(5):
        try:
            lcd.close()
        except Exception:
            pass
        lcd.dev = None
        _t.sleep(2)
        try:
            if lcd.find() and lcd.open():
                lcd.handshake()
                return True
        except Exception:
            pass
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Play GIF/theme animations on the USB LCD")
    ap.add_argument("--gif", required=True, help="GIF, .zt theme file, or static image")
    ap.add_argument("--variant", default="usbdisplay", choices=["usbdisplay", "h", "ali", "ly", "ly1"])
    ap.add_argument("--width", type=int, default=1600, help="Panel width")
    ap.add_argument("--height", type=int, default=720, help="Panel height")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                    help="Rotate frames (panels mount upside-down in some AIOs)")
    ap.add_argument("--quality", type=int, default=95, help="JPEG quality")
    ap.add_argument("--fps", type=int, default=24, help="Playback fps for .zt files (5-60)")
    ap.add_argument("--repeat", type=int, default=-1, help="Loop count (-1 = infinite)")
    args = ap.parse_args()

    target = (args.width, args.height)
    low = args.gif.lower()
    if low.endswith(".zt"):
        clip = load_zt(args.gif, target, args.quality, args.fps, args.rotate)
    elif low.endswith(".gif"):
        clip = load_gif(args.gif, target, args.rotate, args.quality)
    else:
        clip = load_static(args.gif, target, args.rotate, args.quality)

    return play(clip, args.width, args.height, args.variant, args.repeat)


if __name__ == "__main__":
    sys.exit(main())
