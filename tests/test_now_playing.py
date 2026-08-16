"""Unit tests for now_playing: render layout, rotation, CJK, progress bar."""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageChops

from now_playing import (
    _bar_geometry,
    _draw_progress_bar,
    _format_mmss,
    _redraw_bar,
    render_now_playing,
)


def _art() -> Image.Image:
    return Image.new("RGB", (600, 600), (80, 80, 180))


# ---------- _format_mmss ----------

def test_format_mmss():
    assert _format_mmss(0) == "0:00"
    assert _format_mmss(59) == "0:59"
    assert _format_mmss(188) == "3:08"
    assert _format_mmss(3725) == "1:02:05"
    assert _format_mmss(-5) == "0:00"


# ---------- render_now_playing ----------

def test_render_size():
    img = render_now_playing(_art(), "T", "A", 1600, 720, 180, 0, 240)
    assert img.size == (1600, 720)
    assert img.mode == "RGB"


def test_render_all_rotations_same_size():
    for rot in (0, 90, 180, 270):
        img = render_now_playing(_art(), "T", "A", 1600, 720, rot, 100, 240)
        # rotate(expand=True) swaps dims for 90/270
        expected = (1600, 720) if rot in (0, 180) else (720, 1600)
        assert img.size == expected, rot


def test_render_cjk_title_renders():
    """CJK titles must render (msgothic/Noto fallback) — no exception,
    and the produced image differs from a blank one."""
    img = render_now_playing(_art(), "水樹奈々 — Synchrogazer",
                             "Nana Mizuki", 1600, 720, 180, 100, 240)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    assert len(buf.getvalue()) > 1000  # has content


def test_render_progress_bar_visible():
    """The bar fill must be present at 50% and absent at 0%."""
    img0 = render_now_playing(_art(), "T", "A", 1600, 720, 180, 0, 240)
    img50 = render_now_playing(_art(), "T", "A", 1600, 720, 180, 120, 240)

    def _fill_px(im):
        count = 0
        for y in range(im.height):
            for x in range(im.width):
                p = im.getpixel((x, y))
                if abs(p[0] - 200) < 10 and abs(p[1] - 205) < 10:
                    count += 1
        return count

    assert _fill_px(img0) < _fill_px(img50)
    assert _fill_px(img50) > 100


def test_render_no_bar_when_draw_bar_false():
    """draw_bar=False must produce no light-gray fill pixels in the bar
    strip region (the album square's white border is excluded — it sits
    elsewhere on the frame)."""
    img = render_now_playing(_art(), "T", "A", 1600, 720, 180, 120, 240,
                             draw_bar=False)
    # Bar strip geometry (pre-rotation), plus a margin
    bx, by, bw, bh = _bar_geometry(1600, 720)
    for y in range(max(0, by - 5), min(720, by + bh + 40)):
        for x in range(max(0, bx - 5), min(1600, bx + bw + 5)):
            p = img.getpixel((x, y))
            assert not (abs(p[0] - 200) < 10 and abs(p[1] - 205) < 10), (x, y)


# ---------- _redraw_bar (the CPU-optimized tick path) ----------

def _base_jpeg(rot=180):
    img = render_now_playing(_art(), "T", "A", 1600, 720, rot, 0, 0,
                             draw_bar=False)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_redraw_bar_matches_full_render_fill(rot):
    """The redraw path must produce the same bar geometry as the full
    render (this catches rotation-mapping regressions like the one that
    produced the black strip / wrong fill position)."""
    base = _base_jpeg(rot)  # base must share the same rotation
    full = render_now_playing(_art(), "T", "A", 1600, 720, rot, 120, 240)
    redrawn = _redraw_bar(_art(), "T", "A", 1600, 720, rot, 120, 240, base)

    def _fill_bbox(im):
        pts = [(x, y) for y in range(im.height) for x in range(im.width)
               if abs(im.getpixel((x, y))[0] - 200) < 10
               and abs(im.getpixel((x, y))[1] - 205) < 10]
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    b1 = _fill_bbox(full)
    b2 = _fill_bbox(redrawn)
    assert b1 is not None and b2 is not None, rot
    # Same fill width and same vertical band (allow 10px JPEG drift)
    w1, w2 = b1[2] - b1[0], b2[2] - b2[0]
    assert abs(w1 - w2) < 20, f"rot {rot}: fill width {w1} vs {w2}"
    assert abs(b1[1] - b2[1]) < 10, f"rot {rot}: y {b1[1]} vs {b2[1]}"


def test_redraw_bar_no_black_strip():
    """Regression: the old redraw path painted a solid dark rectangle
    behind the bar that accumulated into a visible strip. The new path
    decodes a CLEAN base, so the background under the bar must be the
    blurred art, not a near-black rectangle."""
    base = _base_jpeg()
    img = _redraw_bar(_art(), "T", "A", 1600, 720, 180, 120, 240, base)
    # Sample a strip of pixels just above the bar region (pre-rotation).
    # The blurred art is (80,80,180)-ish; a black strip would be ~(15,16,20).
    bx, by, bw, bh = _bar_geometry(1600, 720)
    # Just above the bar, away from the text
    y = by - 20
    xs = range(bx + 50, bx + 300, 25)
    blacks = 0
    for x in xs:
        r, g, b = img.getpixel((x, y))
        if r < 40 and g < 40 and b < 40:
            blacks += 1
    assert blacks <= 1, f"black strip detected: {blacks}/{len(list(xs))} pixels"


# ---------- _draw_progress_bar ----------

def test_draw_progress_bar_geometry(readings):
    d = Image.new("RGB", (1600, 720), (10, 10, 14))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(d)
    bx, by, bw, bh = _bar_geometry(1600, 720)
    from now_playing import _font
    _draw_progress_bar(draw, bx, by, bw, bh, 60, 240, _font(28), 80)
    # Track (unfilled) region is dim gray; the first pixels at 60/240=25%
    # fill start should be light gray. Sample the track just past the fill.
    track_x = bx + int(bw * 0.75)  # past 25% fill -> dim track
    assert d.getpixel((track_x, by + bh // 2)) == (90, 95, 110)
    # Early pixel (before 25%) should be the light fill
    fill_x = bx + int(bw * 0.10)
    p = d.getpixel((fill_x, by + bh // 2))
    assert (abs(p[0] - 200) < 10 and abs(p[1] - 205) < 10), p


def test_draw_progress_bar_zero_duration():
    """Regression: duration_sec=0 (live streams, YouTube, GSMTC race)
    caused ZeroDivisionError in the render tick — a silent exception loop
    that raised CPU to 50C. Must draw an empty bar, never divide by zero."""
    from PIL import ImageDraw
    img = Image.new("RGB", (1600, 720), (10, 10, 14))
    draw = ImageDraw.Draw(img)
    from now_playing import _bar_geometry, _draw_progress_bar, _font
    bx, by, bw, bh = _bar_geometry(1600, 720)
    # Must not raise:
    _draw_progress_bar(draw, bx, by, bw, bh, 100, 0, _font(28), 80)
    _draw_progress_bar(draw, bx, by, bw, bh, 100, -5, _font(28), 80)
    # Zero-duration bar has no light-gray fill (empty bar)
    for x in range(bx, bx + bw):
        p = img.getpixel((x, by + bh // 2))
        assert not (abs(p[0] - 200) < 10 and abs(p[1] - 205) < 10), x


def test_session_selection_prefers_playing_over_paused(monkeypatch):
    """Regression: when two music apps are open, a paused QQ Music used to
    win over a playing Apple Music (whichever iterated first in GSMTC).
    The AIO showed the paused app's artwork. Fix: only return sessions
    where playback_status == Playing; if nothing is playing, fall back
    to the most-recently-updated session."""
    import datetime
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
    )
    import now_playing as np_mod

    EPOCH = datetime.datetime(1601, 1, 1)

    class _PB:
        def __init__(self, s): self.playback_status = s
    class _TL:
        def __init__(self, pos, dur, lu):
            self.start_time = EPOCH
            self.position = EPOCH + datetime.timedelta(seconds=pos)
            self.end_time = EPOCH + datetime.timedelta(seconds=dur)
            self.last_updated_time = lu
    class _Info:
        def __init__(self, t, a):
            self.title = t; self.artist = a
            class X: pass
            self.thumbnail = X()
    class _Op:
        def __init__(self, i): self._i = i
        def get(self): return self._i
    class _Sess:
        def __init__(self, app, t, a, status, pos, dur, lu):
            self.source_app_user_model_id = app
            self._i = _Info(t, a); self._status = status; self._tl = _TL(pos, dur, lu)
        def try_get_media_properties_async(self): return _Op(self._i)
        def get_playback_info(self): return _PB(self._status)
        def get_timeline_properties(self): return self._tl
    class _Mgr:
        def __init__(self, ss): self._ss = ss
        def get_sessions(self): return self._ss
    class _Req:
        def __init__(self, m): self._m = m
        def get(self): return self._m

    now = datetime.datetime(2026, 8, 16, 12, 0)
    # QQ paused (older), Apple playing (newer)
    qq = _Sess("QQ!App", "QQ song", "QQ artist", Status.PAUSED, 50, 200, now)
    apple = _Sess("Apple!App", "Apple song", "Apple artist", Status.PLAYING, 100, 300, now)

    monkeypatch.setattr(np_mod, "_sync", lambda op, timeout=None: op.get())
    monkeypatch.setattr(np_mod.SMTCManager, "request_async", lambda: _Req(_Mgr([qq, apple])))

    _, info, app_id, _, _ = np_mod._get_active_session()
    assert info.title == "Apple song", f"playing app not preferred: got {info.title!r}"
    assert app_id == "Apple!App"

    # Now both paused -> most recently updated wins
    apple_p = _Sess("Apple!App", "Apple paused", "Apple artist", Status.PAUSED, 0, 200, now)
    qq_p = _Sess("QQ!App", "QQ paused", "QQ artist", Status.PAUSED, 0, 200, now - datetime.timedelta(hours=2))
    monkeypatch.setattr(np_mod.SMTCManager, "request_async", lambda: _Req(_Mgr([qq_p, apple_p])))
    _, info, _, _, _ = np_mod._get_active_session()
    assert info.title == "Apple paused", f"recent-paused not preferred: got {info.title!r}"
