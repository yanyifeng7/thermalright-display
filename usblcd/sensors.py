"""Live sensor monitoring for the overlay: GPU temp/freq (NVML),
CPU freq (psutil). CPU temp deferred (needs kernel driver — ACPI stub
is useless on AMD X870E).

All reads are in-process and measured ~0.001 ms per poll (see
tests/bench_sensors.py). Never spawn subprocesses (PowerShell WMI was
185 ms/poll — the classic trap).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class SensorReadings:
    gpu_temp_c: float | None = None
    gpu_freq_mhz: int | None = None
    cpu_freq_mhz: float | None = None
    cpu_temp_c: float | None = None  # via LibreHardwareMonitor web server
    ok: bool = False


class SensorMonitor:
    """Poll GPU/CPU sensors on demand (thread-safe, lazy NVML init).

    CPU temp comes from LibreHardwareMonitor's web server
    (http://localhost:8085/data.json) when it's running — LHM reads the
    real AMD SMU sensors (Tctl/Tdie) that user-mode WMI can't see.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._nvml = None      # None = not initialized
        self._nvml_handle = None
        self._nvml_failed = False
        self._lhm_url = "http://localhost:8085/data.json"

    # ---------- NVML (GPU) ----------

    def _init_nvml(self) -> bool:
        """Lazy-init NVML; returns True if available."""
        if self._nvml is not None:
            return self._nvml
        if self._nvml_failed:
            return False
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            return True
        except Exception:
            self._nvml_failed = True
            return False

    # ---------- LibreHardwareMonitor (CPU temp + real freq) ----------

    def _read_cpu_lhm(self) -> tuple[float | None, float | None]:
        """Fetch CPU temp (Tctl/Tdie) and REAL current clock from LHM.

        psutil.cpu_freq() on Windows is stuck at the max turbo (4700 MHz)
        — Windows doesn't expose per-core clocks that way. LHM reads the
        SMU directly and reports live clocks (e.g. 5216 MHz boosting,
        713 MHz effective idle). One HTTP GET gets both values.

        Returns (temp_c, freq_mhz); None entries when LHM isn't running.
        """
        import json as _json
        import urllib.request

        try:
            with urllib.request.urlopen(self._lhm_url, timeout=2) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return None, None
        temps = []
        freqs = []

        def walk(node):
            name = node.get("Text", "")
            val = node.get("Value", "")
            if "Tctl" in name or "Tdie" in name:
                try:
                    temps.append(float(val.split()[0]))
                except (ValueError, IndexError):
                    pass
            elif name == "Cores (Average)" and "MHz" in val:
                try:
                    freqs.append(float(val.split()[0]))
                except (ValueError, IndexError):
                    pass
            for c in node.get("Children", []):
                walk(c)

        walk(data)
        temp = max(temps) if temps else None  # Tctl (hottest) if both present
        freq = max(freqs) if freqs else None
        return temp, freq

    def read(self) -> SensorReadings:
        r = SensorReadings()
        with self._lock:
            try:
                if self._init_nvml():
                    nv = self._nvml
                    h = self._nvml_handle
                    r.gpu_temp_c = nv.nvmlDeviceGetTemperature(
                        h, nv.NVML_TEMPERATURE_GPU
                    )
                    r.gpu_freq_mhz = nv.nvmlDeviceGetClockInfo(
                        h, nv.NVML_CLOCK_GRAPHICS
                    )
            except Exception:
                pass

            try:
                import psutil

                f = psutil.cpu_freq()
                if f:
                    r.cpu_freq_mhz = f.current
            except Exception:
                pass

            # LHM gives REAL CPU temp + live clock (psutil's is stuck at
            # max turbo on Windows). Prefer LHM for both.
            lhm_temp, lhm_freq = self._read_cpu_lhm()
            r.cpu_temp_c = lhm_temp
            if lhm_freq is not None:
                r.cpu_freq_mhz = lhm_freq

            r.ok = any(
                v is not None
                for v in (r.gpu_temp_c, r.gpu_freq_mhz, r.cpu_freq_mhz, r.cpu_temp_c)
            )
            return r

    def shutdown(self):
        with self._lock:
            if self._nvml is not None:
                try:
                    self._nvml.nvmlShutdown()
                except Exception:
                    pass
                self._nvml = None
