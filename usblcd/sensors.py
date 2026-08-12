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
        self._lhm_launch_tried = False  # only attempt auto-launch once
        self._lhm_paths = (
            r"D:\AI\LibreHardwareMonitor\LibreHardwareMonitor.exe",
            # Common install locations
            r"C:\Program Files\LibreHardwareMonitor\LibreHardwareMonitor.exe",
            r"C:\Program Files (x86)\LibreHardwareMonitor\LibreHardwareMonitor.exe",
        )

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

    # ---------- LibreHardwareMonitor (single source for CPU+GPU) ----------

    def _try_launch_lhm(self):
        """Best-effort auto-launch of LibreHardwareMonitor.

        Writes a tiny self-elevating .bat (the kernel driver needs admin)
        and fires it once. If elevation is declined or the exe isn't
        found, we simply fall back to NVML/psutil — never crash.
        """
        import os
        import subprocess
        import tempfile

        exe = next((p for p in self._lhm_paths if os.path.isfile(p)), None)
        if exe is None:
            return
        try:
            bat = os.path.join(tempfile.gettempdir(), "lhm_autostart.bat")
            with open(bat, "w", encoding="utf-8") as f:
                # Self-elevate: restart via PowerShell Start-Process -Verb RunAs
                f.write(
                    "@echo off\r\n"
                    'powershell.exe -NoProfile -Command "\r\n'
                    "  $p = Start-Process -FilePath '%s' -Verb RunAs -PassThru\r\n"
                    '"\r\n' % exe.replace("'", "''")
                )
            # Fire and forget; the UAC prompt appears for the user once.
            subprocess.Popen(
                ["cmd.exe", "/c", bat],
                creationflags=subprocess.CREATE_NO_WINDOW,
                close_fds=True,
            )
        except Exception:
            pass

    def _read_lhm(self) -> SensorReadings:
        """Fetch CPU temp/freq + GPU temp/freq from LHM's web server.

        LHM's kernel driver reads the SMU (CPU) and NVIDIA NVML (GPU)
        directly — one HTTP GET replaces three separate sensor APIs and
        gives REAL live clocks (psutil.cpu_freq() is stuck at max turbo
        on Windows; NVML works but adds a dependency).

        Returns a partial SensorReadings (None entries when LHM isn't
        running — caller falls back to NVML/psutil).
        """
        import json as _json
        import urllib.request

        r = SensorReadings()
        try:
            with urllib.request.urlopen(self._lhm_url, timeout=2) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            # LHM isn't running — try to launch it once (it's needed for
            # CPU temp/freq). Uses a self-elevating .bat so the kernel
            # driver loads with one familiar UAC prompt.
            if not self._lhm_launch_tried:
                self._lhm_launch_tried = True
                self._try_launch_lhm()
            return r
        temps = []
        freqs = []
        gpu_temps = []
        gpu_freqs = []
        # Which subtree we're under: CPU vs GPU hardware node
        in_gpu = False
        gpu_nodes = ("RTX 5070", "NVIDIA GeForce", "NVIDIA")

        def walk(node, gpu: bool):
            name = node.get("Text", "")
            val = node.get("Value", "")
            hw = node.get("HardwareType", "")
            if gpu or any(g in name for g in gpu_nodes):
                gpu = True
            if gpu:
                if name == "GPU Core" and "°C" in val:
                    try:
                        gpu_temps.append(float(val.split()[0]))
                    except (ValueError, IndexError):
                        pass
                elif name == "GPU Core" and "MHz" in val:
                    try:
                        gpu_freqs.append(float(val.split()[0]))
                    except (ValueError, IndexError):
                        pass
            else:
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
                walk(c, gpu)

        walk(data, False)
        if temps:
            r.cpu_temp_c = max(temps)  # Tctl (hottest) if both present
        if freqs:
            r.cpu_freq_mhz = max(freqs)
        if gpu_temps:
            r.gpu_temp_c = max(gpu_temps)
        if gpu_freqs:
            r.gpu_freq_mhz = int(max(gpu_freqs))
        return r

    def read(self) -> SensorReadings:
        r = SensorReadings()

        # LHM is the single source when running: real CPU temp/freq + GPU
        # temp/freq in one HTTP GET (SMU + NVML behind LHM's driver).
        lhm = self._read_lhm()
        r.cpu_temp_c = lhm.cpu_temp_c
        r.cpu_freq_mhz = lhm.cpu_freq_mhz
        r.gpu_temp_c = lhm.gpu_temp_c
        r.gpu_freq_mhz = lhm.gpu_freq_mhz

        # Fallbacks when LHM isn't running (or misses a sensor)
        with self._lock:
            try:
                if r.gpu_temp_c is None or r.gpu_freq_mhz is None:
                    if self._init_nvml():
                        nv = self._nvml
                        h = self._nvml_handle
                        if r.gpu_temp_c is None:
                            r.gpu_temp_c = nv.nvmlDeviceGetTemperature(
                                h, nv.NVML_TEMPERATURE_GPU
                            )
                        if r.gpu_freq_mhz is None:
                            r.gpu_freq_mhz = nv.nvmlDeviceGetClockInfo(
                                h, nv.NVML_CLOCK_GRAPHICS
                            )
            except Exception:
                pass

            try:
                if r.cpu_freq_mhz is None:
                    import psutil

                    f = psutil.cpu_freq()
                    if f:
                        r.cpu_freq_mhz = f.current
            except Exception:
                pass

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
