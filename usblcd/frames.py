"""RGB565 frame encoding/decoding.

The USBDISPLAY LCD panels take raw RGB565 pixels (2 bytes/pixel).
Endianness: the TRCC code emits [G3:R5][R5:G3] per the decompiled converter.
"""

from __future__ import annotations

from PIL import Image


def bgr_to_rgb565(b: int, g: int, r: int) -> tuple[int, int]:
    """Convert a BGR byte triple to the two TRCC-format bytes.

    From FormCZTV.ImageTo565 (main branch, myDeviceMode != 10):
        array3[i]   = (G << 3 & 0xE0) + (B >> 3)   # hi: G[7:5] | B[7:3]
        array3[i+1] = (R & 0xF8) + (G >> 5)        # lo: R[7:3] | G[7:5]
    NOTE: this is TRCC's packed layout, NOT standard RGB565 (green is
    packed into 3 bits twice). We mirror it exactly for device compat.
    """
    hi = ((g << 3) & 0xE0) + (b >> 3)
    lo = (r & 0xF8) + (g >> 5)
    return hi, lo


def make_frame_header(width: int, height: int, payload_len: int) -> bytes:
    """Build the 64-byte frame header for the PICTURE command.

    Matches TRCC FormCZTV.ImageToJpg is1600x720 branch:
        bytes 0-3:   0x12 0x34 0x56 0x78 (magic)
        bytes 4-7:   2 (SSCRM_CMD_TYPE_PICTURE)
        bytes 8-11:  width (LE uint32)
        bytes 12-15: height (LE uint32)
        bytes 16-55: zeros
        bytes 56-59: 2
        bytes 60-63: payload length (LE uint32)
    """
    import struct

    hdr = bytearray(64)
    hdr[0:4] = bytes([0x12, 0x34, 0x56, 0x78])
    hdr[4:8] = struct.pack("<I", 2)          # PICTURE
    hdr[8:12] = struct.pack("<I", width)
    hdr[12:16] = struct.pack("<I", height)
    hdr[56:60] = struct.pack("<I", 2)
    hdr[60:64] = struct.pack("<I", payload_len)
    return bytes(hdr)


def jpeg_to_frame(jpeg: bytes, width: int, height: int) -> bytes:
    """Wrap JPEG data in the 64-byte PICTURE header for USB transfer."""
    return make_frame_header(width, height, len(jpeg)) + jpeg


def image_to_rgb565(img: Image.Image) -> bytes:
    """Convert a PIL image (RGB) to the raw RGB565 byte stream."""
    img = img.convert("RGB")
    w, h = img.size
    px = img.load()
    out = bytearray(w * h * 2)
    i = 0
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            hi, lo = bgr_to_rgb565(b, g, r)
            out[i] = hi
            out[i + 1] = lo
            i += 2
    return bytes(out)


def rgb565_to_image(data: bytes, w: int, h: int) -> Image.Image:
    """Decode raw RGB565 bytes back to a PIL image (for verification).

    Inverse of bgr_to_rgb565:
        hi = (g << 3 & 0xE0) + (r >> 3)
        lo = (b & 0xF8) + (g >> 5)
    """
    if len(data) < w * h * 2:
        raise ValueError(f"data too small: {len(data)} < {w * h * 2}")
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * 2
            hi = data[i]
            lo = data[i + 1]
            # Inverse of TRCC packing:
            #   hi = G[4:2] | B[7:3]   (G<<3 & 0xE0 keeps G bits 4:2 at 7:5)
            #   lo = R[7:3] | G[7:5]   (G>>5 keeps G bits 7:5 at 2:0)
            r = lo & 0xF8
            b = (hi & 0x1F) << 3
            g_mid3 = (hi >> 5) & 0x07   # G[4:2]
            g_hi3 = lo & 0x07           # G[7:5]
            g = ((g_hi3 << 3) | g_mid3) << 2  # 6-bit green G[7:2]
            px[x, y] = (r, g, b)
    return img
