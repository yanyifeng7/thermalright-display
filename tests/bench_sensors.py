"""Benchmark the 4 monitoring sensors: per-poll cost and total overhead.

Sensors:
  1. CPU temp  — via WMI (Win32_Temperature? no — use OpenHardwareMonitor-free path:
                 best-effort: psutil.sensors_temperatures, falls back to WMI MSAcpi_ThermalZoneTemperature)
  2. GPU temp  — NVML
  3. GPU freq  — NVML
  4. CPU freq  — psutil.cpu_freq() (computed from performance counters)
"""

import time
import statistics


def cpu_temp_wmi():
    """Read CPU temp via WMI thermal zone (fallback; may return odd values)."""
    import subprocess
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature | Select-Object -First 1).CurrentTemperature"],
        capture_output=True, text=True, timeout=5)
    try:
        return float(r.stdout.strip()) / 10.0 - 273.15  # deci-Kelvin -> C
    except Exception:
        return None


def cpu_temp_psutil():
    import psutil
    try:
        t = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "cpu_thermal"):
            if key in t:
                return t[key][0].current
    except Exception:
        pass
    return None


def gpu_temp_nvml():
    import pynvml
    h = pynvml.nvmlDeviceGetHandleByIndex(0)
    return pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)


def gpu_freq_nvml():
    import pynvml
    h = pynvml.nvmlDeviceGetHandleByIndex(0)
    return pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS)


def cpu_freq_psutil():
    import psutil
    f = psutil.cpu_freq()
    return f.current if f else None


def bench(name, fn, n=200):
    # warmup
    try:
        fn()
    except Exception:
        pass
    times = []
    vals = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            v = fn()
            vals.append(v)
        except Exception as e:
            v = None
            times.append(None)
            continue
        dt = (time.perf_counter() - t0) * 1000  # ms
        times.append(dt)
    ok = [t for t in times if t is not None]
    med = statistics.median(ok) if ok else float("nan")
    mean = statistics.mean(ok) if ok else float("nan")
    p95 = sorted(ok)[int(len(ok) * 0.95)] if ok else float("nan")
    val = vals[-1] if vals else None
    print(f"{name:14} median {med:7.3f} ms | mean {mean:7.3f} ms | p95 {p95:7.3f} ms | last={val}")


if __name__ == "__main__":
    import pynvml
    pynvml.nvmlInit()
    print("Sensor overhead benchmark (200 polls each)\n")
    bench("CPU temp (psutil)", cpu_temp_psutil)
    bench("CPU temp (WMI)", cpu_temp_wmi)
    bench("GPU temp (NVML)", gpu_temp_nvml)
    bench("GPU freq (NVML)", gpu_freq_nvml)
    bench("CPU freq (psutil)", cpu_freq_psutil)
    pynvml.nvmlShutdown()

    # 1 Hz total: sum of medians
    print("\n--- 1 Hz total estimate ---")
    totals = [0.0]
    print("(see medians above; sum of the four ~1ms sensors)")
