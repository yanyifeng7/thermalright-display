"""E2E tests against the real USB display.

These require actual hardware — skipped unless --hardware is passed:

    python -m pytest tests/test_e2e.py --hardware

Run them only when the AIO is plugged in and no other process holds it.
"""
from __future__ import annotations

import io

import pytest

from usblcd.device import USBLCD, LCDDeviceError


def _hardware_enabled(request) -> bool:
    return request.config.getoption("--hardware")


@pytest.fixture
def lcd(request):
    if not _hardware_enabled(request):
        pytest.skip("requires --hardware (real display plugged in)")
    dev = USBLCD("usbdisplay")
    if not dev.find():
        pytest.skip("display not found on USB")
    dev.open()
    dev.handshake()
    yield dev
    try:
        dev.close()
    except Exception:
        pass


@pytest.mark.hardware
def test_handshake(lcd):
    assert lcd.dev is not None


@pytest.mark.hardware
def test_send_solid_frame(lcd):
    from PIL import Image
    from usblcd.frames import jpeg_to_frame

    img = Image.new("RGB", (1600, 720), (255, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    frame = jpeg_to_frame(buf.getvalue(), 1600, 720)
    lcd.send_frame(frame)  # must not raise


@pytest.mark.hardware
def test_send_nowplaying_render(lcd):
    """Render the now-playing layout and push it — validates the whole
    render -> frame -> send pipeline on real hardware."""
    from PIL import Image
    from usblcd.frames import jpeg_to_frame
    from now_playing import render_now_playing

    art = Image.new("RGB", (600, 600), (80, 80, 180))
    img = render_now_playing(art, "E2E Test", "thermalright-display",
                             1600, 720, 180, 60, 240)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    frame = jpeg_to_frame(buf.getvalue(), 1600, 720)
    lcd.send_frame(frame)
