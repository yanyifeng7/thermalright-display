# usblcd-display

Lightweight replacement for TRCC's USB LCD pipeline — push images/GIFs directly to Somore/USBDISPLAY AIO LCD screens without the heavy TRCC stack.

## Status: WORKING ✅

Verified end-to-end on a GIGABYTE AIO (USBDISPLAY 0x87AD:0x70DB):
- Device open + handshake ✅
- JPEG frame display ✅ (1600x720, 64-byte header + JPEG payload)
- Animation playback ✅ (GIF and .zt themes at 24 fps)
- Orientation control ✅ (panel mounts upside-down in some AIOs — use `--rotate 180`)

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

## Usage

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
