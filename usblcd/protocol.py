"""Core protocol constants and helpers."""

from __future__ import annotations

# Shared-memory control words (TRCC ↔ USBLCDNEW handshake)
# These are used in the original two-process architecture; a standalone
# tool can skip shared memory and talk to the USB device directly.
CTRL_POWER_OFF = bytes([0xAA, 0xBB, 0xCC, 0xDD])
CTRL_NEW_FRAME = bytes([0x00, 0x01, 0x01, 0x1E])
CTRL_ACK = bytes([0x00, 0x01, 0x00, 0x1E])

# Frame pacing: TRCC sleeps 15 ms between frames (~66 fps ceiling)
MIN_FRAME_INTERVAL_S = 0.015

# Chunk sizes used by USBLCDNEW when writing to the endpoint
USB_WRITE_CHUNK = 4096
USB_WRITE_TAIL = 2048

# Standard frame resolutions per device class
RES_1600x720 = (1600, 720)
RES_960x720 = (960, 720)
RES_480x720 = (480, 720)
