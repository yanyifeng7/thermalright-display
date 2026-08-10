""".zt theme container: save/load frame sequences.

Format (our own, self-describing):
    magic:      b"USBLCDZT1\x00"  (10 bytes)
    name_len:   uint32 LE
    name:       UTF-8 bytes
    fps:        uint32 LE (nominal; per-frame delays stored too)
    frame_count: uint32 LE
    per-frame:  uint32 LE length + JPEG bytes (repeated)

The loader also accepts TRCC-style .zt files (offset table + raw JPEG
scan) by falling back to FFD8/FFD9 scanning.
"""

from __future__ import annotations

import io
import struct

from PIL import Image

ZT_MAGIC = b"USBLCDZT1\x00"


def frames_to_zt(frames: list[bytes], name: str = "", fps: int = 24) -> bytes:
    """Serialize pre-encoded JPEG frames into a .zt container."""
    name_b = name.encode("utf-8")
    out = bytearray()
    out += ZT_MAGIC
    out += struct.pack("<I", len(name_b))
    out += name_b
    out += struct.pack("<I", fps)
    out += struct.pack("<I", len(frames))
    for f in frames:
        out += struct.pack("<I", len(f))
        out += f
    return bytes(out)


def zt_to_frames(data: bytes) -> list[bytes]:
    """Parse a .zt container (ours or TRCC's) into JPEG frame bytes."""
    if data[: len(ZT_MAGIC)] == ZT_MAGIC:
        pos = len(ZT_MAGIC)
        (name_len,) = struct.unpack_from("<I", data, pos)
        pos += 4 + name_len
        pos += 4  # fps
        (count,) = struct.unpack_from("<I", data, pos)
        pos += 4
        frames = []
        for _ in range(count):
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
