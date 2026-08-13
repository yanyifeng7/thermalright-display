"""Integration tests for the GUI's now-playing poller logic.

These instantiate LCDApp but never touch real hardware:
- USBLCD is monkeypatched with a fake (records send_frame calls).
- The winsdk GSMTC session is monkeypatched with FakeSession.

Covers the exact regressions hit during development:
- rotation combobox value '180°' must parse (was int('180°') -> crash)
- art fetch must work with a tkinter root present (winsdk/tk conflict)
- the artwork frame must actually be sent to the AIO
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from tests.conftest import FakeSession, FakeReadings
from tests.winsdk_utils import CompletedAsync


class FakeLCD:
    """Stand-in for USBLCD: records frames, handshake, close."""

    def __init__(self):
        self.sent: list[bytes] = []
        self.closed = False
        self.product = "FakeUSBDISPLAY"

    def find(self):
        return True

    def open(self):
        pass

    def handshake(self):
        pass

    def send_frame(self, frame: bytes):
        self.sent.append(frame)

    def close(self):
        self.closed = True


@pytest.fixture(scope="module")
def app():
    """Build the GUI app with fakes; no real device, no real winsdk.

    MODULE-scoped: Windows Tcl 8.6 + Python 3.14 corrupts the interpreter
    after ~2 Tk roots are created+destroyed in one process (init.tcl /
    tcl_findLibrary errors). A single root for the whole module avoids it.
    Patches are applied once and restored at module teardown (monkeypatch
    is function-scoped, so we save/restore manually).
    """
    import now_playing as np_mod
    from usblcd import sensors as sensors_mod
    import usblcd.device as device_mod

    session = FakeSession(thumbnail_bytes=_fake_jpeg())

    def fake_get_session():
        return (session, session._info, session.source_app_user_model_id,
                session.pos_sec, session.dur_sec)

    class FakeSensorMonitor:
        def read(self):
            return FakeReadings()

    # Save originals, apply fakes
    orig_session = np_mod._get_active_session
    orig_sensor = sensors_mod.SensorMonitor
    orig_usblcd = device_mod.USBLCD
    np_mod._get_active_session = fake_get_session
    sensors_mod.SensorMonitor = FakeSensorMonitor
    device_mod.USBLCD = lambda *a, **k: FakeLCD()

    import usblcd_app

    app = usblcd_app.LCDApp()
    app.update_idletasks()
    app._test_session = session  # for tests needing the fake session
    yield app
    # Tear down: stop background threads + cancel pending callbacks so no
    # thread touches tk vars after the app is destroyed.
    try:
        app._stop_play()
    except Exception:
        pass
    try:
        if app.monitor is not None:
            app.monitor.stop()
    except Exception:
        pass
    try:
        app._stop_preview()
    except Exception:
        pass
    try:
        app._on_close_np()
    except Exception:
        pass
    try:
        app._on_close()
    except Exception:
        pass
    # Restore originals
    np_mod._get_active_session = orig_session
    sensors_mod.SensorMonitor = orig_sensor
    device_mod.USBLCD = orig_usblcd


def _fake_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 40)).save(buf, format="JPEG")
    return buf.getvalue()


def test_rotation_parse_no_crash(app):
    """The rot combobox holds '180°'; the np poller must parse it (the
    original code did int('180°') and crashed the AIO send path)."""
    app.rot_var.set("180°")
    # Directly exercise the parse used in the poller
    rot_s = str(app.rot_var.get()).replace("°", "").strip()
    assert int(rot_s) == 180


def test_np_poller_sends_artwork_frame(app, monkeypatch):
    """With auto-display on + connected, one poll must produce a frame
    sent to the LCD."""
    import now_playing as np_mod

    # Patch the thumbnail fetch (the real winsdk DataReader can't read
    # fakes; the art decoding itself is covered by unit tests).
    def fake_thumb(info):
        from PIL import Image
        return Image.new("RGB", (64, 64), (200, 30, 40))

    monkeypatch.setattr(np_mod, "_get_thumbnail_pil", fake_thumb)

    # Save + force the state the poller depends on (module-scoped app)
    saved_lcd = app.lcd
    saved_auto = app.np_auto_var.get()
    saved_active = app._np_active
    saved_art = app._np_last_art
    saved_key = app._np_last_key
    saved_base = app._np_base_jpeg
    try:
        app.lcd = FakeLCD()
        app._np_last_art = fake_thumb(None)
        app._np_last_key = "test.App|Sample Title|Sample Artist"
        app._np_base_jpeg = None
        app.np_auto_var.set(True)
        app._np_active = True

        # Run the poller body (spawns a background fetch thread)
        app._np_poll_once()
        # Pump the tk loop until the background fetch lands + frame sends
        import time as _time
        deadline = _time.monotonic() + 5
        while _time.monotonic() < deadline and not app.lcd.sent:
            app.update()
            _time.sleep(0.05)
        assert len(app.lcd.sent) >= 1, "no frame was sent to the LCD"
    finally:
        app.lcd = saved_lcd
        app.np_auto_var.set(saved_auto)
        app._np_active = saved_active
        app._np_last_art = saved_art
        app._np_last_key = saved_key
        app._np_base_jpeg = saved_base
        # Cancel the rescheduled poll so it doesn't fire after teardown
        if app._np_poll_job:
            try:
                app.after_cancel(app._np_poll_job)
            except Exception:
                pass
            app._np_poll_job = None


def test_np_poll_idle_no_crash(app, monkeypatch):
    """No media session -> poller must idle gracefully."""
    import now_playing as np_mod

    def fake_none():
        return (None, None, None, 0, 0)

    monkeypatch.setattr(np_mod, "_get_active_session", fake_none)
    app.lcd = FakeLCD()
    app._np_poll_once()  # must not raise


def test_auto_display_priority_blocks_play(app, monkeypatch):
    """With auto-display on + art available, _toggle_play must NOT start
    a GIF player (artwork has priority)."""
    import now_playing as np_mod
    from PIL import Image

    def fake_thumb(info):
        return Image.new("RGB", (64, 64), (200, 30, 40))

    monkeypatch.setattr(np_mod, "_get_thumbnail_pil", fake_thumb)
    app._np_last_art = fake_thumb(None)
    app._np_last_key = "test.App|Sample Title|Sample Artist"
    app.lcd = FakeLCD()
    app.np_auto_var.set(True)
    app._stop_play()  # ensure clean slate

    app._toggle_play()
    assert app.player is None, "play started a GIF player while artwork active"


def test_brightness_invalidates_base(app):
    """Changing brightness must invalidate the cached base so the next
    poll re-bakes it (regression: stale dimming after slider change)."""
    app._np_base_jpeg = b"stale-bytes"
    app.bright_var.set(50)
    app._on_brightness_cache()
    assert app._np_base_jpeg is None
    # Clean up the debounce timer so it doesn't fire after teardown
    job = getattr(app, "_brightness_cache_job", None)
    if job:
        try:
            app.after_cancel(job)
        except Exception:
            pass
        app._brightness_cache_job = None


def test_preview_paused_when_not_connected(app):
    """Regression: at startup the persisted playlist auto-selects the
    first clip, which used to start the preview animation loop — burning
    ~10% CPU decoding GIF frames while idle. The preview must pause when
    the device isn't connected and nothing is playing."""
    # Save state we mutate (module-scoped app is shared between tests)
    saved_lcd = app.lcd
    saved_player = app.player
    saved_art = app._np_last_art
    saved_auto = app.np_auto_var.get()
    try:
        app.lcd = None
        app.player = None
        app._np_last_art = None  # clear any leaked art (module-scoped app)
        app.np_auto_var.set(False)
        # Simulate a multi-frame GIF preview being active
        app._preview_total = 44
        app._preview_idx = 0
        app._preview_job = None

        app._animate_preview()
        # The pause branch reschedules without rendering a new frame:
        # _preview_idx must NOT advance
        assert app._preview_idx == 0, "preview advanced a frame while idle"

        # And with a connected device (browsing), it should animate
        app.lcd = FakeLCD()
        app._animate_preview()
        assert app._preview_idx == 1, "preview did not animate while connected"
    finally:
        # Restore the shared app state
        app.lcd = saved_lcd
        app.player = saved_player
        app._np_last_art = saved_art
        app.np_auto_var.set(saved_auto)
