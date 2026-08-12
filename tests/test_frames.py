"""Unit tests for usblcd.frames: brightness, sprite, framing."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from usblcd.frames import apply_brightness, build_overlay_sprite, jpeg_to_frame


# ---------- apply_brightness ----------

def test_brightness_100_noop():
    img = Image.new("RGB", (100, 50), (120, 100, 80))
    out = apply_brightness(img, 100)
    assert out is img  # fast path returns the same object


def test_brightness_0_black():
    img = Image.new("RGB", (100, 50), (120, 100, 80))
    out = apply_brightness(img, 0)
    assert out.getpixel((50, 25)) == (0, 0, 0)


def test_brightness_50_halfway():
    img = Image.new("RGB", (100, 50), (200, 100, 0))
    out = apply_brightness(img, 50)
    px = out.getpixel((50, 25))
    assert abs(px[0] - 100) <= 1  # 200 -> ~100
    assert abs(px[1] - 50) <= 1   # 100 -> ~50


def test_brightness_preserves_size_and_mode():
    img = Image.new("RGB", (1600, 720), (120, 100, 80))
    out = apply_brightness(img, 30)
    assert out.size == (1600, 720)
    assert out.mode == "RGB"


# ---------- build_overlay_sprite ----------

def test_sprite_all_sensors(readings):
    sprite = build_overlay_sprite(readings, 1600, 720, rotate=180,
                                  position="bottom-right", font_scale=1.0)
    assert sprite is not None
    block, pos = sprite
    assert block.mode == "RGBA"
    # 4 lines of text -> block taller than wide-ish
    assert block.width > 100
    assert block.height > 60
    # Position must be within the frame
    assert 0 <= pos[0] < 1600
    assert 0 <= pos[1] < 720


def test_sprite_no_sensors():
    class Empty:
        cpu_freq_mhz = None
        cpu_temp_c = None
        gpu_freq_mhz = None
        gpu_temp_c = None

    assert build_overlay_sprite(Empty(), 1600, 720) is None


def test_sprite_all_positions_in_bounds(readings):
    for pos_name in ("top-left", "top-right", "bottom-left", "bottom-right"):
        sprite = build_overlay_sprite(readings, 1600, 720, rotate=180,
                                      position=pos_name, font_scale=1.0)
        assert sprite is not None, pos_name
        block, pos = sprite
        assert 0 <= pos[0] <= 1600 - block.width, pos_name
        assert 0 <= pos[1] <= 720 - block.height, pos_name


def test_sprite_rotations_in_bounds(readings):
    for rot in (0, 90, 180, 270):
        sprite = build_overlay_sprite(readings, 1600, 720, rotate=rot,
                                      position="bottom-right", font_scale=1.0)
        assert sprite is not None, rot
        block, pos = sprite
        assert 0 <= pos[0] <= 1600 - block.width, rot
        assert 0 <= pos[1] <= 720 - block.height, rot


def test_sprite_pastes_with_alpha(readings):
    """Pasting the sprite onto a frame must not raise and must cover."""
    from now_playing import render_now_playing

    art = Image.new("RGB", (600, 600), (80, 80, 180))
    img = render_now_playing(art, "Sample", "Artist", 1600, 720, 180, 100, 240)
    sprite = build_overlay_sprite(readings, 1600, 720, rotate=180,
                                  position="top-left", font_scale=1.0)
    block, pos = sprite
    img.paste(block, pos, block)
    # A pixel inside the block region should be darker than the pure art
    # (the semi-transparent black backing).
    px = img.getpixel((pos[0] + 5, pos[1] + 5))
    assert sum(px) < 3 * 200


# ---------- SensorMonitor auto-launch opt-in ----------

def test_sensor_auto_launch_defaults_off():
    """Auto-launching LHM installs a kernel driver — it must be strictly
    opt-in. New users must never get a surprise UAC prompt."""
    from usblcd.sensors import SensorMonitor

    m = SensorMonitor()
    assert m._auto_launch is False
    m2 = SensorMonitor(auto_launch_lhm=True)
    assert m2._auto_launch is True


def test_sensor_auto_launch_not_fired_when_off(monkeypatch):
    """With auto_launch off, a failed LHM read must NOT try to launch."""
    from usblcd.sensors import SensorMonitor

    m = SensorMonitor(auto_launch_lhm=False)
    launched = []

    def fake_launch():
        launched.append(True)

    monkeypatch.setattr(m, "_try_launch_lhm", fake_launch)
    monkeypatch.setattr(m, "_lhm_url", "http://localhost:9/data.json")  # unreachable
    m._read_lhm()
    assert launched == [], "auto-launch fired even though opt-in is off"


# ---------- jpeg_to_frame ----------

def test_jpeg_to_frame_header(jpeg_bytes):
    frame = jpeg_to_frame(jpeg_bytes, 1600, 720)
    # 64-byte header + payload
    assert len(frame) == 64 + len(jpeg_bytes)
    assert frame[:4] == b"\x12\x34\x56\x78"  # magic
    assert frame[4:8] == b"\x02\x00\x00\x00"  # PICTURE command
    # width/height LE
    assert frame[8:12] == (1600).to_bytes(4, "little")
    assert frame[12:16] == (720).to_bytes(4, "little")
    # JPEG length at bytes 60-63
    assert frame[60:64] == len(jpeg_bytes).to_bytes(4, "little")


def test_jpeg_to_frame_payload_roundtrip(jpeg_bytes):
    frame = jpeg_to_frame(jpeg_bytes, 1600, 720)
    assert frame[64:] == jpeg_bytes
