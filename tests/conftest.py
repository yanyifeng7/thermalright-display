"""Shared fixtures: deterministic, hardware-free, no network.

Mocks the two flaky external dependencies:
- winsdk / GSMTC (media session) — replaced by a FakeSession
- LibreHardwareMonitor — replaced by a FakeSensor

This keeps every test headless, fast, and reproducible on CI.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the project root is importable regardless of cwd
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_addoption(parser):
    parser.addoption("--hardware", action="store_true",
                     help="run hardware-dependent e2e tests")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "hardware: requires a real USB display (--hardware)"
    )


class FakeMediaInfo:
    """Mimics winsdk's GlobalSystemMediaTransportControlsSession media props."""

    def __init__(self, title: str = "Sample Title", artist: str = "Sample Artist",
                 thumbnail_bytes: bytes | None = None):
        self.title = title
        self.artist = artist
        self._thumbnail_bytes = thumbnail_bytes
        self.thumbnail = FakeThumbnailRef(thumbnail_bytes) if thumbnail_bytes else None


class FakeThumbnailRef:
    """Mimics IRandomAccessStreamReference with a fixed JPEG payload."""

    def __init__(self, data: bytes):
        self._data = data

    def open_read_async(self):
        from winsdk_utils import CompletedAsync
        return CompletedAsync(FakeStream(self._data))


class FakeStream:
    """Mimics IRandomAccessStream: size + get_input_stream_at()."""

    def __init__(self, data: bytes):
        self.size = len(data)
        self._data = data

    def get_input_stream_at(self, _pos=0):
        return FakeInputStream(self._data)


class FakeInputStream:
    def __init__(self, data: bytes):
        self._data = data


class FakeSession:
    """Mimics a GSMTC session: media props + timeline."""

    def __init__(self, title="Sample Title", artist="Sample Artist",
                 pos_sec=60.0, dur_sec=240.0, thumbnail_bytes: bytes | None = None,
                 app_id="test.App"):
        self.source_app_user_model_id = app_id
        self._info = FakeMediaInfo(title, artist, thumbnail_bytes)
        self.pos_sec = pos_sec
        self.dur_sec = dur_sec

    def try_get_media_properties_async(self):
        from winsdk_utils import CompletedAsync
        return CompletedAsync(self._info)

    def get_timeline_properties(self):
        return FakeTimeline(self.pos_sec, self.dur_sec)


class FakeTimeline:
    """Mimics the winsdk timeline object (start/end/position as timedelta-like)."""

    def __init__(self, pos_sec: float, dur_sec: float):
        self.start_time = SecondsLike(0)
        self.end_time = SecondsLike(dur_sec)
        self.position = SecondsLike(pos_sec)


class SecondsLike:
    """Minimal timedelta stand-in with .total_seconds()."""

    def __init__(self, sec: float):
        self._sec = sec

    def total_seconds(self) -> float:
        return self._sec

    def __sub__(self, other: "SecondsLike") -> "SecondsLike":
        return SecondsLike(self._sec - other._sec)


class FakeReadings:
    """Mimics usblcd.sensors.SensorReadings."""

    def __init__(self, cpu_freq_mhz=5200.0, cpu_temp_c=42.0,
                 gpu_freq_mhz=1980, gpu_temp_c=46.0):
        self.cpu_freq_mhz = cpu_freq_mhz
        self.cpu_temp_c = cpu_temp_c
        self.gpu_freq_mhz = gpu_freq_mhz
        self.gpu_temp_c = gpu_temp_c


@pytest.fixture
def jpeg_bytes() -> bytes:
    """A tiny valid JPEG (1x1 red) for thumbnail-stream tests."""
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 40)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture
def readings() -> FakeReadings:
    return FakeReadings()
