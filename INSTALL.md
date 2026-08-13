# Installation Guide

## The easy way (recommended)

**You only need one thing: Python 3.10+.**

1. If you don't have Python: download from [python.org](https://www.python.org/downloads/)
   and install with **"Add python.exe to PATH"** ticked.
2. Download/extract this repo anywhere (e.g. `C:\usblcd-display`).
3. **Double-click `start_gui.bat`.**

That's it. On first run the launcher:
- creates a virtual environment (`.venv`) inside the project folder,
- installs the dependencies automatically (`pyusb`, `Pillow`, `libusb`,
  `nvidia-ml-py`, `psutil`, `winsdk`),
- opens the GUI.

All future launches are instant — just double-click again.

> **No admin rights needed.** The app talks to the display over USB with
> user-level access (the display already uses the standard WinUSB driver).

## What the GUI does

| Control | Purpose |
|---|---|
| **Files → Add File…** | add GIFs/.zt themes/images to the list (playlist) |
| **Files list** | click to preview, double-click to play that one, Play runs the sequence |
| **Resolution** | panel resolution (default 1600×720 for Thermalright Rainbow) |
| **Rotation** | 0/90/180/270 — use **180°** if your panel is mounted upside-down |
| **Scale** | Fit (letterbox) / Fill (crop) / Stretch (fill screen) |
| **Brightness** | click the bar (0-100, click outside the ends for 0/100) |
| **Overlay font** | Normal / Large / X-Large text size for the monitor overlay |
| **Monitor overlay** | draw live CPU/GPU temp + freq on the animation (see below) |
| **Auto-display artwork** | when checked (default), the AIO shows the current song's album art whenever music is playing — it takes priority over the playlist |
| **Repeat** | loop the playlist forever (off = stop after the last item) |
| **Tabs: Playlist / Now Playing** | Playlist = GIFs/themes; Now Playing = live album art + progress from any music app (Apple Music, Spotify, etc.) |
| **Connect / Play / Stop** | obvious 🙂 |

> The playlist + all settings are saved automatically to `config.json` in
> the project folder and restored on next launch — no manual saving.

## Optional: live CPU/GPU monitoring (LibreHardwareMonitor)

The **Monitor overlay** checkbox shows live readings on the display. GPU
temp/freq work out of the box (NVML); **CPU temp + real CPU freq need
LibreHardwareMonitor** running in the background:

1. Download [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases) (v0.9.6+)
2. Run `LibreHardwareMonitor.exe` — approve the UAC prompt (it installs a
   kernel driver, same as HWiNFO/Gigabyte Control Centre do)
3. **Options → Remote Web Server → Run**
4. **Options → Show Hidden Sensors** (required — the CPU temperature group
   is hidden by default and won't appear in the web JSON otherwise)

Why: Windows exposes only a frozen ACPI thermal stub on AMD X870E boards;
LHM's driver reads the real SMU sensor. Without LHM the overlay simply omits
the CPU temp line (GPU readings still work). LHM costs ~123 MB RAM / ~0.02
cores.

## Optional: Now Playing (album art on the AIO)

The **Now Playing** tab shows the current track from any app that registers
with Windows media controls — **Apple Music, Spotify, foobar2000, MusicBee,
web browsers**, etc. — with live album art, title/artist, and a progress bar.
It mirrors to the AIO automatically.

**Requirement:** the `winsdk` package (installed automatically by
`start_gui.bat` via `requirements.txt`). This is the only extra dependency
for this feature — everything else is shared.

**Cost (measured 2026-08-13):** the Now Playing stream (album art +
progress bar + optional sensor overlay) runs at **~0.034 cores / ~205 MB**
with everything enabled — about 12× less than TRCC's full stack. The
GSMTC metadata fetch happens on a background thread every 3s, so the UI
never blocks on the media app, and the progress bar interpolates smoothly
between polls.

**Usage:**
1. Open the GUI, connect the display.
2. Play something in Apple Music (or any music app).
3. The **Now Playing tab** shows the track; with **Auto-display artwork**
   checked (default in Display settings), the AIO shows the album art
   automatically — no Play button needed.

**Details:**
- Works with *any* app that appears in the Windows volume-flyout media card.
- The AIO stream includes a progress bar with mm:ss times (updated every
  second, interpolated for smooth motion).
- CJK/Japanese/Chinese/Korean titles render correctly — **Noto Serif SC VF**
  is bundled in `fonts/` (SIL Open Font License, see `fonts/OFL.txt`).
  Source: [github.com/notofonts/noto-fonts](https://github.com/notofonts/noto-fonts)
  / [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Serif+SC).
  If the bundled file is missing it falls back to the Windows-shipped copy
  (`C:\Windows\Fonts\NotoSerifSC-VF.ttf`, Win 11 22H2+) then MS Gothic.
- No music playing? The display falls back to whatever the playlist would
  show.

There's also a standalone CLI:
```bash
.venv\Scripts\python now_playing.py
```

## Manual setup (if you prefer the command line)

```bash
cd usblcd-display
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python usblcd_app.py        # GUI
.venv\Scripts\python send_image.py --image pic.jpg --width 1600 --height 720 --rotate 180
.venv\Scripts\python gif_player.py --gif anim.gif --width 1600 --height 720 --rotate 180
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Device not found" in the GUI | display unplugged? Replug the AIO's USB cable |
| Screen shows boot logo instead of your image | make sure you clicked **Play**; if it's a `.zt` file try a GIF |
| Image is upside-down | set Rotation to **180°** |
| Screen stays black | brightness is at 0 — click the right end of the brightness bar |
| Error after closing the app abruptly (Access denied / Entity not found) | unplug the AIO USB for 5s and replug; the app should be closed via the window X, not Task Manager |

## Supported panels

Verified: **Thermalright Rainbow** AIO LCD (Somore "USBDISPLAY",
`VID 0x87AD PID 0x70DB`, 1600×720).

Other Somore/USBDISPLAY variants (H/ALi/LY/LY1) are defined in the code but
not yet tested — see [README](README.md#supported-devices).
