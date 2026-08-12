#!/usr/bin/env python3
"""Display the currently-playing track's album artwork on the AIO LCD.

Polls Windows' GlobalSystemMediaTransportControls (GSMTC) API every
few seconds — the same surface that powers the volume-flyout media card.
Renders a clean 1600x720 "now playing" layout: heavily-blurred album
art as background, centered album square, song + artist text.

Works with any app that registers with Windows media controls
(Apple Music, Spotify, foobar2000, MusicBee, etc.).

Usage:
    python now_playing.py
    python now_playing.py --poll 2        # seconds between updates
    python now_playing.py --width 1600 --height 720 --rotate 180
"""
from __future__ import annotations

import argparse
import ctypes
import io
import sys
import threading
import time
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from usblcd.device import USBLCD, LCDDeviceError
from usblcd.frames import jpeg_to_frame

# ---------- winsdk GSMTC (lazy import so --help works without it) ----------
WSDK_AVAILABLE = False
try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SMTCManager,
    )
    WSDK_AVAILABLE = True
except ImportError:
    pass


# ---------- GSMTC helpers (winsdk is async-only; this wraps it) ---------- #

def _sync(op, timeout: float = 5.0):
    """Block until an IAsyncOperation completes; return its result."""
    done = threading.Event()
    op.completed = lambda *a, **k: done.set()
    done.wait(timeout)
    return op.get_results()


def _get_active_session():
    """Return the first GSMTC session that has media info (Apple Music etc.)."""
    if not WSDK_AVAILABLE:
        return None, None, None
    try:
        mgr = _sync(SMTCManager.request_async())
        for session in mgr.get_sessions():
            try:
                info = _sync(session.try_get_media_properties_async(), timeout=2.0)
                if info and (info.title or info.artist):
                    return session, info, session.source_app_user_model_id
            except Exception:
                continue
    except Exception:
        pass
    return None, None, None


def _get_thumbnail_pil(info, max_size: int = 512) -> Optional[Image.Image]:
    """Download the album-art thumbnail (GSMTC) as a PIL Image."""
    try:
        thumb_ref = info.thumbnail
        if thumb_ref is None:
            return None
        stream = _sync(thumb_ref.open_read_async(), timeout=3.0)
        # DataReader (proper async path — read() is sync-only & unsupported)
        from winsdk.windows.storage.streams import DataReader
        reader = DataReader(stream.get_input_stream_at(0))
        _sync(reader.load_async(stream.size), timeout=3.0)
        ibuf = reader.read_buffer(stream.size)
        return Image.open(io.BytesIO(bytes(ibuf)))
    except Exception:
        return None


# ---------- Layout rendering ---------- #

def render_now_playing(
    art: Image.Image,
    title: str,
    artist: str,
    width: int = 1600,
    height: int = 720,
    rotate: int = 0,
) -> Image.Image:
    """Build the full 1600x720 now-playing image."""
    panel = Image.new("RGB", (width, height), (10, 10, 14))
    draw = ImageDraw.Draw(panel)

    # 1. Background: album art scaled to fill, heavily blurred
    bg = art.copy().convert("RGB")
    bg = _scale(bg, (width, height), mode="fill")
    bg = bg.filter(ImageFilter.GaussianBlur(radius=32))
    # Darken overlay so text reads
    dark = Image.new("RGB", (width, height), (0, 0, 0))
    panel.paste(bg, (0, 0))
    panel.paste(dark, (0, 0), Image.new("RGBA", (width, height), (0, 0, 0, 110)))

    # 2. Album square on the right side (panel rotated 180 -> visually left)
    side = min(width // 3, height - 120)
    art_sq = art.copy().convert("RGB")
    art_sq = _scale(art_sq, (side, side), mode="fill")
    # Subtle border
    margin = 80
    ax = (width - side) // 2 + 220  # past center, toward right side
    ay = (height - side) // 2
    panel.paste(art_sq, (ax, ay))
    draw.rectangle([ax, ay, ax + side - 1, ay + side - 1],
                   outline=(255, 255, 255, 60), width=2)

    # 3. Text (left of album art)
    tx = margin
    ty = ay + 30
    font_title = _font(72)
    font_artist = _font(40)

    # Truncate long titles to one line (limit to left half, beside the art)
    title = title.strip() if title else "—"
    artist = artist.strip() if artist else ""
    max_text_w = ax - tx - 40  # leave gap before album art
    while draw.textlength(title, font=font_title) > max_text_w and len(title) > 4:
        title = title[:-2]
    while draw.textlength(artist, font=font_artist) > max_text_w and len(artist) > 4:
        artist = artist[:-2]
    artist = artist if draw.textlength(artist, font=font_artist) <= max_text_w \
        else (artist[:60] + "…") if len(artist) > 60 else artist

    _draw_text(draw, (tx, ty), title, font_title, fill=(245, 245, 248))
    _draw_text(draw, (tx, ty + 100), artist, font_artist, fill=(180, 185, 195))
    _draw_text(draw, (tx, ty + 170), "NOW PLAYING", _font(24), fill=(130, 140, 160))

    # 4. Apply rotation (panel may be physically flipped)
    if rotate:
        panel = panel.rotate(-rotate, expand=True)

    return panel


def _scale(img: Image.Image, target: tuple[int, int], mode: str = "fit") -> Image.Image:
    """Fit / fill / stretch — matches the GUI's scale modes."""
    tw, th = target
    iw, ih = img.size
    if mode == "stretch":
        return img.resize((tw, th), Image.LANCZOS)
    if mode == "fill":
        s = max(tw / iw, th / ih)
        return img.resize((int(iw * s), int(ih * s)), Image.LANCZOS).crop((
            (int(iw * s) - tw) // 2, (int(ih * s) - th) // 2,
            (int(iw * s) + tw) // 2, (int(ih * s) + th) // 2,
        ))
    # fit (letterbox)
    s = min(tw / iw, th / ih)
    nw, nh = int(iw * s), int(ih * s)
    bg = Image.new("RGB", (tw, th), (10, 10, 14))
    bg.paste(img.resize((nw, nh), Image.LANCZOS), ((tw - nw) // 2, (th - nh) // 2))
    return bg


# Font chain. NotoSansSC-VF (Google Noto Sans SC, ships on Windows 11
# 22H2+) is our primary — it covers Latin + Japanese + Chinese + Korean
# with modern, well-hinted glyphs designed for screens. msgothic.ttc is
# the fallback (slightly older / less pretty but always present).
_FONT_PRIMARY = "NotoSansSC-VF.ttf"
_FONT_LATIN = "segoeui.ttf"
_CJK_FONTS = (_FONT_PRIMARY, "msgothic.ttc", "msyh.ttc", "malgun.ttf", "simhei.ttf")


def _font(px: int) -> ImageFont.FreeTypeFont:
    """Primary font (Noto Sans SC VF): covers Latin + JP + CN + KR, ships
    on Windows 11 22H2+. Beautiful screen-tuned glyphs."""
    try:
        return ImageFont.truetype(_FONT_PRIMARY, px)
    except Exception:
        return ImageFont.load_default()


def _latin_font(px: int) -> ImageFont.FreeTypeFont:
    """Latin-only font (segoeui): tighter Latin metrics than msgothic."""
    try:
        return ImageFont.truetype(_FONT_LATIN, px)
    except Exception:
        return _font(px)


def _draw_text(draw, pos, text, font, fill, anchor=None):
    """Draw text using the given font.

    We don't do per-character font picking anymore — it doesn't work
    reliably through PIL's Python API (missing glyphs render as .notdef
    boxes that look like valid glyphs to getmask/getbbox). Instead, the
    caller passes the right font: _font() for general use (covers most
    CJK + Latin), _latin_font() for pure-ASCII labels where tighter
    Latin metrics matter.
    """
    if not text:
        return
    if anchor:
        draw.text(pos, text, fill=fill, font=font, anchor=anchor)
    else:
        draw.text(pos, text, fill=fill, font=font)


# ---------- Main loop ---------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--rotate", type=int, default=180,
                    help="panel rotation (0/90/180/270); use 180 for AIOs flipped in case")
    ap.add_argument("--poll", type=float, default=2.0,
                    help="seconds between GSMTC polls (default: 2)")
    ap.add_argument("--quality", type=int, default=92, help="JPEG quality (default: 92)")
    args = ap.parse_args()

    if not WSDK_AVAILABLE:
        print("ERROR: winsdk not installed. Run: pip install winsdk", file=sys.stderr)
        return 1

    try:
        lcd = USBLCD("usbdisplay")
        if not lcd.find():
            print(f"ERROR: USB LCD (0x87AD:0x70DB) not found", file=sys.stderr)
            return 2
        lcd.open()
        lcd.handshake()
    except LCDDeviceError as e:
        print(f"ERROR: LCD open failed: {e}", file=sys.stderr)
        return 2

    print(f"Display: {lcd.dev.product}")
    print(f"Rendering {args.width}x{args.height}, rotate {args.rotate}, "
          f"poll {args.poll}s. Ctrl-C to stop.")

    last_key = None
    last_frame = None
    last_send_time = 0.0
    no_art_counter = 0
    # Panel refresh interval: the AIO powers down the display if it sees
    # no new data for ~1-2s, so we re-send the current frame every 100ms.
    # (Cost: a single ~180KB JPEG every 100ms = ~1.8MB/s USB, trivial.)
    REFRESH_INTERVAL_S = 0.1

    try:
        while True:
            session, info, app_id = _get_active_session()

            if info is None:
                # No active media — show a calm "nothing playing" panel
                if last_key != "__idle__":
                    print(f"[{time.strftime('%H:%M:%S')}] no active media session")
                    last_key = "__idle__"
                    last_frame = None
                # Build a "no track" frame once, re-send it at REFRESH_INTERVAL_S
                if last_frame is None:
                    img = Image.new("RGB", (args.width, args.height), (10, 10, 14))
                    d = ImageDraw.Draw(img)
                    d.text((60, 60), "No media playing", fill=(120, 125, 135), font=_font(56))
                    if args.rotate:
                        img = img.rotate(-args.rotate, expand=True)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=args.quality)
                    last_frame = jpeg_to_frame(buf.getvalue(), *img.size)
                now = time.monotonic()
                if now - last_send_time >= REFRESH_INTERVAL_S:
                    lcd.send_frame(last_frame)
                    last_send_time = now
                time.sleep(0.5)
                continue

            title = info.title or ""
            artist = info.artist or ""
            key = f"{app_id}|{title}|{artist}"

            # Build a new frame only when the track changes
            if key != last_key:
                print(f"[{time.strftime('%H:%M:%S')}] {app_id}: {title} — {artist}")
                last_key = key
                art = _get_thumbnail_pil(info)
                if art is None:
                    no_art_counter += 1
                    print(f"  no artwork ({no_art_counter})")
                    img = Image.new("RGB", (args.width, args.height), (10, 10, 14))
                    d = ImageDraw.Draw(img)
                    d.text((60, 60), title or "—", fill=(245, 245, 248), font=_font(72))
                    d.text((60, 160), artist, fill=(180, 185, 195), font=_font(40))
                    if args.rotate:
                        img = img.rotate(-args.rotate, expand=True)
                else:
                    no_art_counter = 0
                    img = render_now_playing(art, title, artist, args.width, args.height, args.rotate)

                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=args.quality)
                last_frame = jpeg_to_frame(buf.getvalue(), *img.size)

            # Re-send the current frame at the panel refresh rate (keeps
            # the AIO from powering down the display between track changes)
            now = time.monotonic()
            if now - last_send_time >= REFRESH_INTERVAL_S:
                lcd.send_frame(last_frame)
                last_send_time = now
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        try:
            lcd.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())