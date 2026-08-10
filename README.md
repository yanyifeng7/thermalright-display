# usblcd-display

Lightweight replacement for TRCC (Thermalright Control Centre)'s USB LCD pipeline — push images/GIFs directly to Thermalright Rainbow / Somore-USBDISPLAY AIO LCD screens without the heavy TRCC stack.

## Status: WORKING ✅

Verified end-to-end on a **Thermalright Rainbow** AIO (panel is a Somore "USBDISPLAY" 0x87AD:0x70DB):
- Device open + handshake ✅
- JPEG frame display ✅ (1600x720, 64-byte header + JPEG payload)
- Animation playback ✅ (GIF and .zt themes at 24-60 fps)
- Orientation control ✅ (panel mounts upside-down in some AIOs — use `--rotate 180`)
- Brightness ✅ (0-100, software overlay — same method TRCC uses)
- Theme save/load ✅ (settings persisted: rotation, scale, brightness, fps)
- Live monitor overlay ✅ (GPU temp/freq + CPU freq drawn on the animation, 4 corner positions, rotates with the frame)

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

The GUI can overlay live **GPU temp / GPU freq / CPU freq** (see Monitor
overlay checkbox). That adds re-encode work — measured on real GIFs
(2026-08-10):

| Scenario | CPU | vs TRCC |
|---|---|---|
| GIF playback only (44-frame theme @ 24fps) | **0.002 cores (0.2%)** | ~193× less |
| GIF + overlay (224-frame GIF @ 33fps) | **0.156 cores (15.6%)** | ~2.6× less |
| GIF + overlay (100-frame GIF @ 17fps) | **0.171 cores (17.1%)** | ~2.4× less |
| TRCC full stack | 0.405 cores (40.5%) | — |

Overlay cost scales with **update rate × frame count** (each sensor-text
change re-encodes the animation cycle once — JPEG can't be partially
re-encoded). The overlay updates at most every 2s and only when the
visible text actually changes (CPU 2 decimals), so cost stays bounded
while readings feel live.

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
python send_image.py --image frame.jpg --width 1600 --height 720
python send_image.py --image frame.jpg --width 1600 --height 720 --rotate 180 --stay
python gif_player.py --gif anim.gif --width 1600 --height 720 --rotate 180
python gif_player.py --gif theme.zt --width 1600 --height 720 --rotate 180 --fps 24
```

`--stay` re-sends the frame every second (panels blank on USB idle in some cases) with auto-reconnect.

## Project layout

```
usblcd-display/
├── usblcd/              # core protocol library
│   ├── device.py        # device discovery + USB transport
│   ├── frames.py        # JPEG framing + RGB565 codecs
│   └── protocol.py      # constants
├── send_image.py        # CLI: send a single image
├── tests/               # unit tests
└── docs/                # protocol notes
```

## License

MIT
