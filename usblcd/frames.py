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


def apply_brightness(img: Image.Image, brightness: int) -> Image.Image:
    """Apply TRCC-style brightness (0-100) via black overlay.

    TRCC never sends a BKL_SET USB command — it dims in software by drawing
    a black overlay with alpha = (100 - brightness) * 255 / 100 over each
    frame before JPEG encoding (UCScreenImage.cs:1089-1093).
    brightness 100 = no overlay; 0 = fully black (screen off).
    """
    if brightness >= 100:
        return img
    alpha = (100 - brightness) * 255 // 100
    if alpha <= 0:
        return img
    black = Image.new("RGB", img.size, (0, 0, 0))
    return Image.blend(img, black, alpha / 255.0)


def draw_monitor_overlay(
    img: Image.Image,
    gpu_temp_c=None,
    gpu_freq_mhz=None,
    cpu_freq_mhz=None,
    cpu_temp_c=None,
    rotate: int = 0,
    position: str = "top-left",
    font_scale: float = 1.0,
) -> Image.Image:
    """Draw a small sensor text block in a chosen corner.

    The panel is 1600x720 (or 960x720 etc.); the overlay is drawn at a
    size proportional to the image width so it scales across resolutions.
    `rotate` (0/90/180/270) rotates the text block to match the frame
    rotation, so the readings stay upright when the panel flips the image.
    `position` is the DISPLAYED corner: top-left / top-right / bottom-left /
    bottom-right (the block is repositioned so it lands there after the
    panel's physical flip). `font_scale` (1.0/1.35/1.75) adjusts the text
    size. Returns a copy — input not modified.
    """
    from PIL import ImageDraw, ImageFont

    img = img.convert("RGB").copy()
    w, h = img.size
    font_px = max(18, int(w // 48 * font_scale))  # ~33px at 1600 wide
    pad = font_px // 2

    lines = []
    if cpu_freq_mhz is not None:
        lines.append(f"CPU {cpu_freq_mhz / 1000:.2f} GHz")
    if cpu_temp_c is not None:
        lines.append(f"CPU {cpu_temp_c:.0f} C")
    if gpu_freq_mhz is not None:
        lines.append(f"GPU {gpu_freq_mhz} MHz")
    if gpu_temp_c is not None:
        lines.append(f"GPU {gpu_temp_c:.0f} C")

    if not lines:
        return img

    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype("arial.ttf", font_px)
    # Measure the widest line
    line_w = max(draw.textlength(t, font=font) for t in lines)
    box_w = int(line_w) + pad * 2
    box_h = len(lines) * (font_px + 6) + pad * 2

    # Build the text block on its own layer so it can be rotated
    block = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(block)
    bd.rectangle([0, 0, box_w - 1, box_h - 1], fill=(0, 0, 0, 140))
    y = pad + 2
    for t in lines:
        bd.text((pad + 2, y), t, fill=(255, 255, 255), font=font)
        y += font_px + 6

    # Rotate the block the SAME way the GIF/image is rotated so the text
    # ends up upright after the panel's physical flip
    if rotate:
        block = block.rotate(-rotate, expand=True)

    def _rot_point(px, py, angle_deg):
        """Rotate a point around the frame center by angle (deg)."""
        import math

        rad = math.radians(angle_deg)
        cx, cy = w / 2.0, h / 2.0
        dx, dy = px - cx, py - cy
        return (
            cx + dx * math.cos(rad) - dy * math.sin(rad),
            cy + dx * math.sin(rad) + dy * math.cos(rad),
        )

    # Desired DISPLAYED corner -> anchor the block top-left in SENT coords
    if position == "top-right":
        ax, ay = _rot_point(w - pad, pad, -rotate)
        pos = (int(ax - block.width), int(ay))
    elif position == "bottom-left":
        ax, ay = _rot_point(pad, h - pad, -rotate)
        pos = (int(ax), int(ay - block.height))
    elif position == "bottom-right":
        ax, ay = _rot_point(w - pad, h - pad, -rotate)
        pos = (int(ax - block.width), int(ay - block.height))
    else:  # top-left (default)
        ax, ay = _rot_point(pad, pad, -rotate)
        pos = (int(ax), int(ay))

    # Clamp so the block stays fully inside the frame
    pos = (
        max(0, min(pos[0], w - block.width)),
        max(0, min(pos[1], h - block.height)),
    )

    img = img.convert("RGBA")
    img.paste(block, pos, block)  # block used as its own alpha mask
    return img.convert("RGB")


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


def build_overlay_sprite(readings, width: int, height: int, rotate: int = 180,
                         position: str = "top-left", font_scale: float = 1.0):
    """Pre-render the monitor-overlay text block once (rotated + positioned).

    Shared by the GIF overlay (MonitorThread) and the now-playing artwork
    stream. Returns (RGBA block image, paste position) or None when there
    are no readable sensors.

    readings: object with cpu_freq_mhz, cpu_temp_c, gpu_freq_mhz, gpu_temp_c
              (None when unavailable).
    """
    import math
    from PIL import Image, ImageDraw, ImageFont

    lines = []
    if readings.cpu_freq_mhz is not None:
        lines.append(f"CPU {readings.cpu_freq_mhz/1000:.2f} GHz")
    if readings.cpu_temp_c is not None:
        lines.append(f"CPU {readings.cpu_temp_c:.0f} C")
    if readings.gpu_freq_mhz is not None:
        lines.append(f"GPU {readings.gpu_freq_mhz} MHz")
    if readings.gpu_temp_c is not None:
        lines.append(f"GPU {readings.gpu_temp_c:.0f} C")
    if not lines:
        return None

    try:
        font = ImageFont.truetype("segoeui.ttf", font_px := max(18, int(width // 48 * font_scale)))
    except Exception:
        font = ImageFont.load_default()
    pad = font_px // 2
    d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    line_h = int(font_px * 1.35)
    max_w = max(d.textlength(t, font=font) for t in lines)
    bw, bh = int(max_w) + pad * 2, line_h * len(lines) + pad

    block = Image.new("RGBA", (bw, bh), (0, 0, 0, 150))
    bd = ImageDraw.Draw(block)
    y = pad
    for t in lines:
        bd.text((pad, y), t, font=font, fill=(255, 255, 255, 255))
        y += line_h

    if rotate:
        block = block.rotate(-rotate, expand=True)

    def _rot_point(px, py, angle_deg):
        rad = math.radians(angle_deg)
        cx, cy = width / 2.0, height / 2.0
        dx, dy = px - cx, py - cy
        return (
            cx + dx * math.cos(rad) - dy * math.sin(rad),
            cy + dx * math.sin(rad) + dy * math.cos(rad),
        )

    if position == "top-right":
        ax, ay = _rot_point(width - pad, pad, -rotate)
        pos = (int(ax - block.width), int(ay))
    elif position == "bottom-left":
        ax, ay = _rot_point(pad, height - pad, -rotate)
        pos = (int(ax), int(ay - block.height))
    elif position == "bottom-right":
        ax, ay = _rot_point(width - pad, height - pad, -rotate)
        pos = (int(ax - block.width), int(ay - block.height))
    else:  # top-left (default)
        ax, ay = _rot_point(pad, pad, -rotate)
        pos = (int(ax), int(ay))

    pos = (
        max(0, min(pos[0], width - block.width)),
        max(0, min(pos[1], height - block.height)),
    )
    return (block, pos)
