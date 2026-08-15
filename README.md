# usblcd-display

Lightweight replacement for TRCC (Thermalright Control Centre)'s USB LCD pipeline — push images/GIFs directly to Thermalright Rainbow / Somore-USBDISPLAY AIO LCD screens without the heavy TRCC stack.

## Status: WORKING ✅

Verified end-to-end on a **Thermalright Rainbow** AIO (panel is a Somore "USBDISPLAY" 0x87AD:0x70DB):
- Device open + handshake ✅
- JPEG frame display ✅ (1600x720, 64-byte header + JPEG payload)
- Animation playback ✅ (GIF and .zt themes at 24-60 fps)
- Orientation control ✅ (panel mounts upside-down in some AIOs — use `--rotate 180`)
- Brightness ✅ (0-100, software overlay — same method TRCC uses)
- **Playlist mode** ✅ (play multiple GIFs/themes in sequence, zero-pause switching, loop on/off)
- **Auto-persist** ✅ (playlist + all display settings saved to `config.json`, restored on launch)
- **Live monitor overlay** ✅ (CPU temp/freq + GPU temp/freq drawn on the animation, 4 corner positions, 3 font sizes, rotates with the frame)
- **Now Playing tab** ✅ (live album art + title/artist/progress from any music app — Apple Music, Spotify, foobar2000 — mirrored to the AIO)
- **Auto-display artwork** ✅ (checkbox: artwork takes priority over the playlist whenever music is playing)
- **Dark UI** ✅ (modern dark theme, no more win95-era gray)

## Live monitoring overlay

The GUI can draw live sensor readings on top of the animation:

```
CPU 4.98 GHz | CPU 43C | GPU 920 MHz | GPU 41C
```

| Sensor | Source | Requires |
|---|---|---|
| CPU temp (Tctl) | **LibreHardwareMonitor** web server (`localhost:8085`) | LHM running |
| CPU freq (real, live) | **LibreHardwareMonitor** (psutil falls back to max-turbo only) | LHM running |
| GPU temp | LibreHardwareMonitor / NVML | — |
| GPU freq | LibreHardwareMonitor / NVML | — |

**Why LibreHardwareMonitor for CPU temp:** Windows user-mode WMI only exposes
the ACPI thermal zone, which on AMD X870E boards is a **frozen stub** (~17°C,
never moves). LHM's kernel driver reads the real **AMD SMU** sensor (Tctl/Tdie
~40°C at idle, matching BIOS/HWiNFO). It also gives **real live CPU clocks**
(5.0-5.2 GHz boosting) — `psutil.cpu_freq()` is stuck at the max turbo
(4.70 GHz) on Windows.

**Setup (one-time):**
1. Download [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases) (v0.9.6+)
2. Run `LibreHardwareMonitor.exe` (approve the UAC prompt — it installs a kernel driver)
3. **Options → Remote Web Server → Run**
4. **Options → Show Hidden Sensors** (the CPU temperature group is hidden by default and won't export to the JSON otherwise)

That's it — the overlay picks up CPU temp/freq automatically when LHM is
running, and degrades gracefully (CPU temp line disappears) when it isn't.
LHM costs ~123 MB RAM and ~0.02 cores idle.

### Overlay architecture (2026-08-11: sprite-based)

The overlay is **sprite-based**: the text block (font, rotation, position
baked) is rendered **once** per sensor change into a small RGBA image, and
each displayed frame is `decode → paste sprite → encode`. No per-frame text
drawing or rotation math. Stats are **always live** (rebuild on every visible
text change — no staleness cap), because rebuilding the sprite is cheap.
Overlay position + font size (Normal/Large/X-Large) are selectable and
persisted.

## Now Playing (album art on the AIO)

The **Now Playing tab** shows what's currently playing in any app that
registers with Windows media controls — Apple Music, Spotify, foobar2000,
MusicBee, browsers, etc. — and can mirror it to the AIO:

```
┌───────────────────────────────────────┐
│  [blurred album art as background]    │
│                                       │
│  Title — Artist           [album]     │
│  ▓▓▓▓▓▓▓▓░░░░░░░░  (progress bar)    │
│  1:23 / 4:05                          │
└───────────────────────────────────────┘
```

- **Data source:** Windows **GSMTC** API (`GlobalSystemMediaTransportControls`)
  — the same one that powers the volume-flyout media card. Polled once per
  second: title, artist, album art (512px JPEG thumbnail), position, duration.
- **Auto-display artwork** checkbox (Display settings): when checked (default),
  artwork takes priority — the AIO shows the now-playing render whenever music
  is playing, regardless of the active tab or Play button. Unchecked: the
  active tab at Play time decides.
- **Keepalive:** the frame is re-sent every 100 ms so the panel stays lit
  between 1-second polls (the AIO blanks after ~1-2s without data).
- **Smooth progress bar:** position is interpolated between GSMTC polls
  (which have 1-second granularity) using a local monotonic timer; the bar
  rebuilds 4×/sec via a cheap strip redraw (~5 ms — decode base, patch bar,
  re-encode) instead of a full 50 ms render.
- **CJK titles render correctly** — the repo bundles **Noto Serif SC VF**
  (Google Noto, [SIL Open Font License](https://scripts.sil.org/OFL)) in
  `fonts/`, covering Latin + Japanese + Chinese + Korean. If the bundled
  file is missing, the app falls back to the Windows-shipped copy
  (`C:\Windows\Fonts\NotoSerifSC-VF.ttf`, Win 11 22H2+), then MS Gothic.
  License text: [`fonts/OFL.txt`](fonts/OFL.txt).
  Source/upstream: [github.com/notofonts/noto-fonts](https://github.com/notofonts/noto-fonts)
  (Noto Serif SC variable font, download under
  [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Serif+SC)).

**Cost (measured 2026-08-13, overlay ON + artwork streaming):**

| Component | CPU | RAM |
|---|---|---|
| **usblcd GUI** (artwork + progress + overlay + tabs) | **0.034 cores (3.4%)** | ~205 MB |
| LibreHardwareMonitor (background sensor source) | 0.017 cores (1.7%) | ~123 MB |
| **Full stack total** | **~0.05 cores (5%)** | ~330 MB |

The GSMTC session fetch runs on a **background thread** (never blocking the
UI — the winsdk COM calls to the media app can take ~2s). The progress bar
stays smooth via a 500 ms local render tick that interpolates position from
the last poll; metadata is re-fetched every 3 s. This keeps the GUI at
~1-3% CPU even when Apple Music is busy decoding lossless audio.

**Reliability by construction** (the stream survives hostile inputs):

- **USB writes never block the UI** — frames go to a single background
  writer thread with latest-frame semantics; a slow device can't stall the
  mainloop.
- **One fetch at a time** — session fetches and art fetches are guarded
  (in-flight flags set *before* the thread spawns, reset in `finally`), so
  a hung winsdk COM call can't wedge art loading or pile up threads.
- **Graceful degradation** — a corrupt base JPEG or a 0-duration track
  (live streams, YouTube) draws an empty bar instead of crashing the loop.
- **Self-diagnostics** — runtime exceptions in hot paths are logged once
  per type with full tracebacks, and a thread-count health check warns if
  the process starts leaking threads. Silent failures don't stay silent.

> **`winsdk` is required for the Now Playing feature** — see [INSTALL.md](INSTALL.md).

## Performance: ~190× lighter than TRCC

Measured live on the same hardware, same ff7 theme, same display (2026-08-10):

| Metric | **TRCC** (4 processes) | **usblcd-display** (1 process) |
|---|---|---|
| CPU (per second of wall time) | **0.405 cores** (40.5% of one core) | **0.0021 cores** (0.2% of one core) |
| Fraction of an 8-core system | 5.06% | 0.03% |
| Processes | TRCC.exe + HWINFO.exe + USBLCDNEW.exe + USBLCD.exe | 1 python process |
| Breakdown | TRCC 29.6s / HWINFO 4.7s / USBLCDNEW 1.0s / USBLCD 0.1s per 30s | single-threaded |

**≈193× less CPU** for the identical animation. TRCC burns ~40% of a core
permanently (HWiNFO sensor polling + continuous re-render + 4-process
overhead); this tool pre-encodes frames once and only does USB writes.

### Monitor overlay CPU (live sensors on top of the animation)

Measured with the sprite-based overlay on real GIFs (2026-08-11):

| Scenario | CPU | vs TRCC |
|---|---|---|
| GIF playback only (44-frame theme @ 24fps) | **0.0008 cores (0.1%)** | ~500× less |
| GIF + overlay (224-frame GIF @ 33fps, worst case) | **0.126 cores (12.6%)** | ~3.2× less |
| GIF + overlay (44-frame GIF, live hardware) | **0.090 cores (9.0%)** | ~4.5× less |
| TRCC full stack | 0.405 cores (40.5%) | — |

Overlay cost scales with **sensor-change rate × frame count** (each text
change re-composites the animation cycle once — JPEG can't be partially
re-encoded). The sprite design makes the per-frame work minimal, and frames
are cached between changes (stable readings → near-zero CPU).

### Playlist CPU (load-all cache)

Playlist mode encodes **every item once** into an in-memory cache at load
time (~15 MB/clip; an 8-GIF playlist ≈ 123 MB — trivial on 32 GB). Switching
between items is a **pure dict lookup**: zero-pause, zero re-encode, no
background preload thread. Measured live (2026-08-11):

| Config | CPU | vs TRCC |
|---|---|---|
| **6-GIF playlist, loop on, playing** | **0.0039 cores (0.4%)** | ~100× less |
| Single-GIF loop, brightness 45 | 0.0008 cores | ~500× less |
| **Now Playing (album art + progress + overlay)** | **0.034 cores (3.4%)** | ~12× less |
| **Full stack (GUI + LHM)** | **~0.05 cores (5%)** | ~8× less |
| TRCC full stack (idle) | 0.405 cores | — |

Brightness changes rebuild the cache in the background (debounced 800 ms so
slider drags don't spam); the currently-playing clip dims live instantly.

### Gotchas discovered (hardware-verified)
1. **Every frame MUST be wrapped in the 64-byte PICTURE header** — raw JPEG gets silently ignored (display shows boot logo)
2. **Raw .zt JPEG bytes are rejected by the display decoder** — always re-encode through PIL first (TRCC does the same internally)
3. **The device holds the last frame indefinitely** on USB idle — no blanking worry for static images
4. **Killing a process that holds the device wedges the USB handle** — Windows needs a device replug (or disable/enable) to recover; always exit cleanly

## Supported devices

| Variant | VID | PID | Endpoint | Status |
|---|---|---|---|---|
| **USBDISPLAY (Standard)** | 0x87AD | 0x70DB | Write EP1 | ✅ verified |
| H variant | 0x0416 | 0x5302 | Write EP2 | ⏳ planned |
| ALi | 0x0416 | 0x5406 | Write EP2 | ⏳ planned |
| LY | 0x0416 | 0x5408 | Write EP9 | ⏳ planned |
| LY1 | 0x0416 | 0x5409 | Write EP2 | ⏳ planned |

## Protocol (reverse-engineered from TRCC 2.1.6)

### Frame format (verified)
The display accepts **JPEG frames wrapped in a 64-byte header**:

```
[64-byte header][JPEG data]
header:
  bytes 0-3:   0x12 0x34 0x56 0x78  (magic)
  bytes 4-7:   2                     (SSCRM_CMD_TYPE_PICTURE)
  bytes 8-11:  width  (LE uint32)   e.g. 1600
  bytes 12-15: height (LE uint32)   e.g. 720
  bytes 16-55: zeros
  bytes 56-59: 2
  bytes 60-63: JPEG length (LE uint32)
```

### Handshake
- Write 64-byte magic: `0x12 0x34 0x56 0x78` + zeros + `01 00 00 00` at bytes 56-59
- Read 1024-byte response; byte 24 must be non-zero

### Transport
- USB bulk write to EP1 OUT (4096-byte chunks, 2048-byte tail)
- If payload length % 512 == 0, send a zero-length terminator transfer
- ≥15 ms between frames (TRCC paces at ~66 fps)

### Commands (SSCRM_CMD_TYPE_*)
| Value | Command |
|---|---|
| 1 | DEV_INFO |
| 2 | PICTURE |
| 3 | LOGO |
| 4 | OTA |
| 5 | UPG_STATE |
| 6 | ROTATE |
| 7 | SCR_SET |
| 8 | BKL_SET |
| 9 | LOGO_STATE |

### Theme format (.zt)
MJPEG container: N × JPEG frames at device resolution (e.g. 1600x720).
Frame count = GIF animation length. See `docs/zt-format.md`.

## Installation

**Easiest path: install Python 3.10+ (tick "Add to PATH"), then double-click
[`start_gui.bat`](INSTALL.md)** — it creates the venv, installs dependencies,
and opens the GUI. No admin needed. Full guide: [INSTALL.md](INSTALL.md).

Manual / CLI setup:

```
pip install -r requirements.txt
pip install winsdk          # only needed for the Now Playing / album-art feature
python send_image.py --image frame.jpg --width 1600 --height 720
python send_image.py --image frame.jpg --width 1600 --height 720 --rotate 180 --stay
python gif_player.py --gif anim.gif --width 1600 --height 720 --rotate 180
python gif_player.py --gif theme.zt --width 1600 --height 720 --rotate 180 --fps 24
python now_playing.py       # CLI: show current track's album art on the AIO
```

`--stay` re-sends the frame every second (panels blank on USB idle in some cases) with auto-reconnect.

## Project layout

```
usblcd-display/
├── usblcd/              # core library
│   ├── device.py        # device discovery + USB transport
│   ├── frames.py        # JPEG framing, brightness, overlay sprite helpers
│   ├── sensors.py       # sensor reads (LHM web server + NVML/psutil fallback)
│   ├── ztfile.py        # .zt theme read (TRCC compatibility)
│   └── theme.py         # dark UI palette + ttk styles
├── usblcd_app.py        # GUI (tabs: Playlist + Now Playing, monitor overlay)
├── now_playing.py       # CLI: album art + progress from any GSMTC music app
├── send_image.py        # CLI: send a single image
├── gif_player.py        # CLI: play a GIF/theme
├── fonts/               # Noto Serif SC VF (OFL-licensed, Git LFS) + OFL.txt
├── tests/               # unit tests
└── docs/                # protocol notes
```

## Testing

Unit + integration tests run headless (no USB display needed) — they mock
GSMTC (media session) and LibreHardwareMonitor:

```bash
.venv\Scripts\python -m pytest            # 28 tests, ~6s
```

Coverage:
- `frames.py`: brightness math, sprite geometry (all 4 corners × 4 rotations
  in-bounds), 64-byte header framing
- `now_playing.py`: mm:ss formatting, CJK rendering, progress-bar visibility,
  rotation handling, **bar-strip redraw matches full render** (regression:
  black-strip bug), no-black-strip guarantee
- GUI poller (with fake session + fake LCD): rotation `180°` parse (regression:
  `int("180°")` crash), artwork frame actually sent, auto-display priority
  blocks GIF play, brightness invalidates the base cache

Hardware e2e (opt-in, real display plugged in):

```bash
.venv\Scripts\python -m pytest tests/test_e2e.py --hardware
```

## License

MIT
