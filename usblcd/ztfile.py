""".zt theme container: save/load frame sequences + display settings.

Format (self-describing, v2):
    magic:       b"USBLCDZT2\x00"  (10 bytes)
    name_len:    uint32 LE
    name:        UTF-8 bytes
    fps:         uint32 LE
    width:       uint32 LE
    height:      uint32 LE
    rotate:      uint32 LE (0/90/180/270)
    scale:       uint32 LE (0=fit, 1=fill, 2=stretch)
    brightness:  uint32 LE (0-100)
    frame_count: uint32 LE
    per-frame:   uint32 LE length + JPEG bytes (repeated)

Frames are FINAL display frames (rotation/scale/brightness already baked
into the pixels). The loader also accepts TRCC-style .zt files (offset
table + raw JPEG scan) by falling back to FFD8/FFD9 scanning.
"""

from __future__ import annotations

import io
import struct

from PIL import Image

ZT_MAGIC = b"USBLCDZT2\x00"
SCALE_CODES = {"fit": 0, "fill": 1, "stretch": 2}
SCALE_NAMES = {0: "fit", 1: "fill", 2: "stretch"}


def frames_to_zt(
    frames: list[bytes],
    name: str = "",
    fps: int = 24,
    width: int = 1600,
    height: int = 720,
    rotate: int = 0,
    scale: str = "fit",
    brightness: int = 100,
) -> bytes:
    """Serialize pre-encoded final JPEG frames + settings into a .zt."""
    name_b = name.encode("utf-8")
    out = bytearray()
    out += ZT_MAGIC
    out += struct.pack("<I", len(name_b))
    out += name_b
    out += struct.pack("<I", int(fps))
    out += struct.pack("<I", int(width))
    out += struct.pack("<I", int(height))
    out += struct.pack("<I", int(rotate) % 360)
    out += struct.pack("<I", SCALE_CODES.get(scale, 0))
    out += struct.pack("<I", max(0, min(100, int(brightness))))
    out += struct.pack("<I", len(frames))
    for f in frames:
        out += struct.pack("<I", len(f))
        out += f
    return bytes(out)


def zt_parse(data: bytes) -> dict | None:
    """Parse the settings header of a .zt file; None if not our format."""
    if data[: len(ZT_MAGIC)] != ZT_MAGIC:
        return None
    pos = len(ZT_MAGIC)
    (name_len,) = struct.unpack_from("<I", data, pos)
    pos += 4
    name = data[pos : pos + name_len].decode("utf-8", "replace")
    pos += name_len
    fps, width, height, rotate, scale_code, brightness, count = struct.unpack_from(
        "<IIIIIII", data, pos
    )
    return {
        "name": name,
        "fps": fps,
        "width": width,
        "height": height,
        "rotate": rotate,
        "scale": SCALE_NAMES.get(scale_code, "fit"),
        "brightness": brightness,
        "frame_count": count,
    }


def zt_to_frames(data: bytes) -> list[bytes]:
    """Parse a .zt container (ours or TRCC's) into JPEG frame bytes."""
    meta = zt_parse(data)
    if meta is not None:
        pos = len(ZT_MAGIC) + 4 + len(meta["name"].encode("utf-8")) + 4 * 7
        frames = []
        for _ in range(meta["frame_count"]):
            (flen,) = struct.unpack_from("<I", data, pos)
            pos += 4
            frames.append(data[pos : pos + flen])
            pos += flen
        return frames

    # TRCC-style: scan for JPEG SOI/EOI pairs
    frames = []
    pos = 0
    while True:
        idx = data.find(b"\xFF\xD8", pos)
        if idx < 0:
            break
        eoi = data.find(b"\xFF\xD9", idx)
        if eoi < 0:
            break
        frames.append(data[idx : eoi + 2])
        pos = eoi + 2
    return frames


def load_zt_images(path: str) -> list[Image.Image]:
    """Load a .zt file and decode all frames to PIL images."""
    with open(path, "rb") as f:
        data = f.read()
    return [Image.open(io.BytesIO(j)) for j in zt_to_frames(data)]
