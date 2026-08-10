"""USBLCD device discovery and transport.

Reverse-engineered from TRCC 2.1.6 (Somore Tech / USBDISPLAY protocol).
"""

from __future__ import annotations

import os
import sys

# Ensure libusb-1.0.dll is findable by pyusb's libloader, which searches
# PATH on Windows. The `libusb` PyPI package bundles platform DLLs under
# libusb/_platform/windows/<arch>/; add that directory to PATH. Fall back
# to a DLL in the project root.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_candidate_dirs = [_PROJECT_ROOT]
try:
    import platform as _pyplatform
    import libusb as _libusb_pkg  # type: ignore

    _arch = "x86_64" if _pyplatform.machine().lower() in ("amd64", "x86_64") else "x86"
    _dll_dir = os.path.join(
        os.path.dirname(_libusb_pkg.__file__), "_platform", "windows", _arch
    )
    if os.path.isdir(_dll_dir):
        _candidate_dirs.insert(0, _dll_dir)
except Exception:
    pass

_path_parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
for _d in _candidate_dirs:
    if _d and not any(os.path.normcase(p) == os.path.normcase(_d) for p in _path_parts):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")

import usb.core
import usb.util
import usb.backend.libusb1

# Device table (from decompiled USBLCDNEW.dll / TRCC.exe)
DEVICES = {
    "usbdisplay": {  # Standard variant — primary target
        "vid": 0x87AD,
        "pid": 0x70DB,
        "write_ep": 0x01,   # EP1 OUT
        "read_ep": 0x01,    # EP1 IN
        "handshake": bytes([0x12, 0x34, 0x56, 0x78] + [0] * 52 + [1, 0, 0, 0] + [0] * 4),
        "handshake_rd_len": 1024,
        "handshake_resp_off": 24,  # array[24] must be non-zero
    },
    "h": {
        "vid": 0x0416,
        "pid": 0x5302,
        "write_ep": 0x02,
        "read_ep": 0x01,
        "handshake": bytes([218, 219, 220, 221] + [0] * 8 + [1] + [0] * 3 + [16] + [0] * 6),
        "handshake_rd_len": 512,
        "handshake_resp_off": 0,
    },
    "ali": {
        "vid": 0x0416,
        "pid": 0x5406,
        "write_ep": 0x02,
        "read_ep": 0x01,
        "handshake": bytes([245, 0, 1, 0, 188, 255, 182, 200, 0, 0, 0, 0, 0, 4, 0, 0] + [0] * 1024),
        "handshake_rd_len": 1024,
        "handshake_resp_off": 0,
    },
    "ly": {
        "vid": 0x0416,
        "pid": 0x5408,
        "write_ep": 0x09,
        "read_ep": 0x01,
        "handshake": bytes([2, 255] + [0] * 14 + [0] * 2032),
        "handshake_rd_len": 512,
        "handshake_resp_off": 0,
    },
    "ly1": {
        "vid": 0x0416,
        "pid": 0x5409,
        "write_ep": 0x02,
        "read_ep": 0x01,
        "handshake": bytes([2, 255] + [0] * 14 + [0] * 496),
        "handshake_rd_len": 511,
        "handshake_resp_off": 0,
    },
}


class LCDDeviceError(Exception):
    pass


class USBLCD:
    """A Somore/USBDISPLAY USB LCD device."""

    def __init__(self, variant: str = "usbdisplay"):
        if variant not in DEVICES:
            raise LCDDeviceError(f"Unknown variant: {variant}")
        self.variant = variant
        self.spec = DEVICES[variant]
        self.dev = None

    def find(self) -> bool:
        """Find and open the device."""
        dev = usb.core.find(idVendor=self.spec["vid"], idProduct=self.spec["pid"])
        if dev is None:
            return False
        self.dev = dev
        return True

    def open(self) -> None:
        if self.dev is None:
            raise LCDDeviceError("Device not found")
        # WinUSB devices (winusb.sys) have no kernel driver to detach — the
        # libusb_kernel_driver_active check is unsupported for them and raises
        # NotImplementedError. Skip it and try set_configuration directly.
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except NotImplementedError:
            pass  # WinUSB path: nothing to detach
        except usb.core.USBError:
            pass

        # set_configuration can fail with "Entity not found" if the device
        # was left in a stale state (e.g. previous process killed while
        # holding it). WinUSB auto-configures on open, so a failed explicit
        # set_configuration is not fatal — retry with a device reset, then
        # continue to claim_interface regardless.
        import time

        last_err = None
        for attempt in range(3):
            try:
                self.dev.set_configuration()
                last_err = None
                break
            except usb.core.USBError as e:
                last_err = e
                if attempt < 2:
                    time.sleep(0.5)
                    try:
                        self.dev.reset()
                    except Exception:
                        pass
                    time.sleep(0.5)
        if last_err is not None:
            # Non-fatal: WinUSB already configured the device at open.
            pass

        try:
            usb.util.claim_interface(self.dev, 0)
        except usb.core.USBError as e:
            raise LCDDeviceError(f"Claim interface failed: {e}") from e

    def handshake(self) -> bytes:
        """Send handshake magic, read response, validate."""
        spec = self.spec
        ep_out = 0x01 | 0x00  # OUT endpoint (libusb wants address 0x01)
        ep_in = 0x01 | 0x80   # IN endpoint

        # Send handshake bytes
        self.dev.write(ep_out, spec["handshake"], timeout=100)

        # Read response
        try:
            resp = self.dev.read(ep_in, spec["handshake_rd_len"], timeout=200)
        except usb.core.USBTimeoutError:
            raise LCDDeviceError("Handshake read timed out")

        resp = bytes(resp)
        off = spec["handshake_resp_off"]
        if off >= len(resp) or resp[off] == 0:
            raise LCDDeviceError("Handshake validation failed: response marker zero")
        return resp

    def send_frame(self, data: bytes) -> None:
        """Send a framed payload to the write endpoint.

        Data must be a complete frame (e.g. 64-byte header + JPEG). Mirrors
        USBLCDNEW: one bulk transfer, plus a zero-length terminator when the
        payload length is a multiple of 512.
        """
        spec = self.spec
        ep_out = spec["write_ep"] | 0x00
        self.dev.write(ep_out, data, timeout=1000)
        if len(data) % 512 == 0:
            self.dev.write(ep_out, b"", timeout=100)

    def close(self) -> None:
        if self.dev is not None:
            try:
                usb.util.release_interface(self.dev, 0)
            except Exception:
                pass
            try:
                self.dev.reset()
            except Exception:
                pass
            self.dev = None

    def __enter__(self):
        if not self.find():
            raise LCDDeviceError(f"Device {self.spec['vid']:04X}:{self.spec['pid']:04X} not found")
        self.open()
        return self

    def __exit__(self, *args):
        self.close()
