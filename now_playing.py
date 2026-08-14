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
                    # Read timeline (position / duration) for the progress bar
                    tl = session.get_timeline_properties()
                    try:
                        duration_sec = (tl.end_time - tl.start_time).total_seconds()
                        position_sec = (tl.position - tl.start_time).total_seconds()
                    except Exception:
                        duration_sec = position_sec = 0
                    return (session, info, session.source_app_user_model_id,
                            position_sec, duration_sec)
            except Exception:
                continue
    except Exception:
        pass
    return None, None, None, 0, 0


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
    position_sec: float = 0,
    duration_sec: float = 0,
    draw_bar: bool = True,
) -> Image.Image:
    """Build the full 1600x720 now-playing image.

    position_sec / duration_sec drive the progress bar + mm:ss labels.
    Pass draw_bar=False to render the clean base frame (no progress UI) —
    used as the backdrop that _redraw_bar() paints the bar onto, so the
    background stays pristine (blurred art + veil) under the bar.
    """
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
    font_time = _font(28)

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

    # 4. Progress bar + mm:ss time labels (bottom-left of text area)
    if draw_bar and duration_sec > 0:
        bar_y = ty + 170
        bar_h = 10  # visible at this scale
        bar_w = max_text_w
        # Background track (dim white — panel is RGB, no alpha channel)
        draw.rectangle([tx, bar_y, tx + bar_w, bar_y + bar_h],
                       fill=(90, 95, 110))
        # Filled portion — accent color so it stands out from the track
        ratio = max(0.0, min(1.0, position_sec / duration_sec))
        fill_w = max(1, int(bar_w * ratio)) if ratio > 0 else 0
        if fill_w > 0:
            draw.rectangle([tx, bar_y, tx + fill_w, bar_y + bar_h],
                           fill=(200, 205, 215))  # light gray fill
        # mm:ss labels below the bar
        played = _format_mmss(position_sec)
        total = _format_mmss(duration_sec)
        _draw_text(draw, (tx, bar_y + bar_h + 10),
                   played, font_time, fill=(210, 215, 225))
        # right-aligned total time
        right = tx + bar_w
        total_w = draw.textlength(total, font=font_time)
        _draw_text(draw, (right - total_w, bar_y + bar_h + 10),
                   total, font_time, fill=(210, 215, 225))

    # 5. Apply rotation (panel may be physically flipped)
    if rotate:
        panel = panel.rotate(-rotate, expand=True)

    return panel


def _bar_geometry(width, height, tx=None, side=None):
    """Shared geometry for the progress bar (same layout as render_now_playing).
    Returns (bar_x, bar_y, bar_w, bar_h)."""
    side = side or min(width // 3, height - 120)
    margin = 80
    ax = (width - side) // 2 + 220
    ay = (height - side) // 2
    tx = margin
    ty = ay + 30
    bar_w = ax - tx - 40
    return tx, ty + 170, bar_w, 10


def _draw_progress_bar(draw, bar_x, bar_y, bar_w, bar_h, position_sec, duration_sec,
                       font_time, tx):
    """Draw the progress bar + mm:ss labels (shared by full render and redraw)."""
    # Background track (dim gray)
    draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], fill=(90, 95, 110))
    # Filled portion (light gray).
    # Guard: duration_sec can be 0 (live streams, YouTube, or a GSMTC
    # race where the timeline hasn't populated) — never divide by zero.
    if duration_sec > 0:
        ratio = max(0.0, min(1.0, position_sec / duration_sec))
        fill_w = max(1, int(bar_w * ratio)) if ratio > 0 else 0
        if fill_w > 0:
            draw.rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h],
                           fill=(200, 205, 215))
    # mm:ss labels below the bar
    played = _format_mmss(position_sec)
    total = _format_mmss(duration_sec)
    _draw_text(draw, (tx, bar_y + bar_h + 10), played, font_time, fill=(210, 215, 225))
    total_w = draw.textlength(total, font=font_time)
    _draw_text(draw, (bar_x + bar_w - total_w, bar_y + bar_h + 10),
               total, font_time, fill=(210, 215, 225))


def _redraw_bar(art, title, artist, width, height, rotate, position_sec, duration_sec,
                base_jpeg_bytes):
    """Cheap path: decode the CLEAN base frame (no bar), draw only the
    progress bar + time labels on it, return the updated image.

    The base is the pristine blurred-art backdrop, so the bar region
    always shows the real background — no accumulation, no strip.
    ~5ms vs ~50ms for a full render. We un-rotate, draw at pre-rotation
    coordinates, then re-rotate."""
    import io as _io
    img = Image.open(_io.BytesIO(base_jpeg_bytes)).convert("RGB")
    font_time = _font(28)

    if rotate % 360 == 0:
        pass  # no rotation to undo
    elif rotate % 360 == 180:
        img = img.rotate(180)  # undo the display flip
    else:
        # 90/270: render_now_playing used rotate(-rotate, expand=True),
        # so undoing needs rotate(rotate, expand=True) to swap dims back.
        img = img.rotate(rotate, expand=True)

    bx, by, bw, bh = _bar_geometry(width, height)
    d = ImageDraw.Draw(img)
    _draw_progress_bar(d, bx, by, bw, bh, position_sec, duration_sec,
                       font_time, 80)

    # Re-apply the display rotation
    if rotate % 360 == 180:
        img = img.rotate(180)
    elif rotate % 360:
        img = img.rotate(-rotate, expand=True)
    return img


def _format_mmss(seconds: float) -> str:
    """Format seconds as mm:ss (or h:mm:ss for >1h tracks)."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


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


# Font chain. NotoSerifSC-VF (Google Noto Serif SC) is bundled in
# fonts/ under the SIL Open Font License v1.1 — covers Latin + JP + CN + KR.
# If the bundled file is missing, we fall back to the Windows-shipped
# versions of the same font family, then msgothic.ttc.
import os as _os
_FONT_PRIMARY = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                              "fonts", "NotoSerifSC-VF.ttf")
_FONT_LATIN = "segoeui.ttf"
_CJK_FONTS = (_FONT_PRIMARY,
             # Windows fallbacks (in case the bundled file is missing)
             "NotoSerifSC-VF.ttf",  # Windows 11 22H2+ ships it
             "msgothic.ttc",
             "msyh.ttc",
             "malgun.ttf",
             "simhei.ttf")


def _font(px: int) -> ImageFont.FreeTypeFont:
    """Primary font (Noto Serif SC VF, bundled under OFL): elegant
    classical serif covering Latin + JP + CN + KR. Falls back to
    Windows-shipped variants if the bundled file is missing."""
    for fname in (_FONT_PRIMARY,) + _CJK_FONTS[1:]:
        if _os.path.isfile(fname):
            try:
                return ImageFont.truetype(fname, px)
            except Exception:
                continue
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
    # GSMTC timeline has 1s granularity, so we interpolate the position
    # between polls for smooth bar motion. We rebuild the frame when the
    # interpolated position has visibly moved (every 250ms is plenty).
    GSMTC_POLL_S = 1.0
    POS_REBUILD_S = 0.25
    last_gsmtc_poll = 0.0
    last_gsmtc_pos = 0.0
    last_gsmtc_track = None
    last_rebuild = 0.0
    # Cache the last good session/info so we don't flicker when a single
    # GSMTC call times out (Apple Music sometimes returns blank for one
    # cycle while it re-fetches media properties).
    last_session = None
    last_info = None
    last_app_id = None
    last_duration = 0.0
    _cached_art = None  # album art cached per track
    last_frame_bytes = None  # last JPEG bytes (for the cheap bar-redraw path)
    _base_jpeg = None  # clean bar-less base frame (blurred art + veil)

    try:
        while True:
            now_mono = time.monotonic()
            # Poll GSMTC every GSMTC_POLL_S (1s). Otherwise use the
            # interpolated position (last GSMTC reading + elapsed time).
            if now_mono - last_gsmtc_poll >= GSMTC_POLL_S:
                session, info, app_id, gsmtc_pos, duration_sec = _get_active_session()
                last_gsmtc_poll = now_mono
                # Cache the good result so a single transient miss
                # (Apple Music re-fetching media properties) doesn't
                # flicker the display to "no active media".
                if info is not None and info.title:
                    last_session, last_info, last_app_id = session, info, app_id
                    last_duration = duration_sec
                # Detect track changes (or huge position jumps e.g. user seeked).
                track_now = info.title if info and info.title else None
                if track_now != last_gsmtc_track or abs(gsmtc_pos - last_gsmtc_pos) > 2:
                    last_gsmtc_track = track_now
                last_gsmtc_pos = gsmtc_pos

            # Use the cached session if this iteration's GSMTC call missed
            if info is None and last_info is not None:
                session, info, app_id = last_session, last_info, last_app_id
                duration_sec = last_duration

            # Interpolate: assume the song plays at 1x speed from the
            # last GSMTC reading until the next one arrives. GSMTC
            # occasionally corrects this.
            position_sec = last_gsmtc_pos + (time.monotonic() - last_gsmtc_poll)
            # Clamp to duration
            if duration_sec > 0:
                position_sec = min(position_sec, duration_sec)

            if info is None:
                # No active media (and no cache) — show a calm panel
                if last_key != "__idle__":
                    print(f"[{time.strftime('%H:%M:%S')}] no active media session")
                    last_key = "__idle__"
                    last_frame = None
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
            track_key = f"{app_id}|{title}|{artist}"

            # Rebuild every POS_REBUILD_S for smooth bar motion (or on track change).
            # Frame build is expensive (~30ms), so we throttle: only rebuild
            # when the position has visibly moved or the track changed.
            # The art + text (everything except the bar) is cached per track;
            # position updates only re-render the thin bar strip.
            now_mono = time.monotonic()
            track_changed = last_key is None or last_key[0] != track_key
            should_rebuild = (
                track_changed
                or (now_mono - last_rebuild) >= POS_REBUILD_S
            )
            if should_rebuild:
                # Throttle the "new track" log so we don't spam on every rebuild
                if track_changed:
                    print(f"[{time.strftime('%H:%M:%S')}] {app_id}: {title} — {artist}")
                    # New track: render the CLEAN base (no progress bar) once.
                    # The bar is painted on top by _redraw_bar() on each tick,
                    # keeping the blurred-art background pristine underneath.
                    art = _get_thumbnail_pil(info)
                    _cached_art = art
                    if art is None:
                        no_art_counter += 1
                        if no_art_counter == 1:
                            print(f"  no artwork ({no_art_counter})")
                        img = Image.new("RGB", (args.width, args.height), (10, 10, 14))
                        d = ImageDraw.Draw(img)
                        d.text((60, 60), title or "—", fill=(245, 245, 248), font=_font(72))
                        d.text((60, 160), artist, fill=(180, 185, 195), font=_font(40))
                        if args.rotate:
                            img = img.rotate(-args.rotate, expand=True)
                        # no-art fallback: bar-less base
                        _base_jpeg = None
                    else:
                        no_art_counter = 0
                        img = render_now_playing(art, title, artist, args.width, args.height,
                                                 args.rotate, 0, 0, draw_bar=False)
                    # Encode the clean base once; the bar gets added per tick
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=args.quality)
                    _base_jpeg = buf.getvalue()
                    # Draw the bar at the current position on the base
                    img = _redraw_bar(art, title, artist, args.width, args.height,
                                      args.rotate, position_sec, duration_sec,
                                      _base_jpeg)
                else:
                    art = _cached_art
                    # Position tick: redraw the bar onto the CLEAN base
                    # (pristine background every time — no strip accumulation)
                    img = _redraw_bar(art, title, artist, args.width, args.height,
                                      args.rotate, position_sec, duration_sec,
                                      _base_jpeg)

                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=args.quality)
                last_frame = jpeg_to_frame(buf.getvalue(), *img.size)
                last_frame_bytes = buf.getvalue()
                last_rebuild = now_mono
                last_key = (track_key,)

            # Re-send the current frame at the panel refresh rate (keeps
            # the AIO from powering down the display between track changes)
            now = time.monotonic()
            if now - last_send_time >= REFRESH_INTERVAL_S:
                lcd.send_frame(last_frame)
                last_send_time = now
            # Short sleep to keep CPU low (this loop runs ~10x/sec)
            time.sleep(0.05)
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