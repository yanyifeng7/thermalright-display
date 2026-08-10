# Installation Guide

## The easy way (recommended)

**You only need one thing: Python 3.10+.**

1. If you don't have Python: download from [python.org](https://www.python.org/downloads/)
   and install with **"Add python.exe to PATH"** ticked.
2. Download/extract this repo anywhere (e.g. `C:\usblcd-display`).
3. **Double-click `start_gui.bat`.**

That's it. On first run the launcher:
- creates a virtual environment (`.venv`) inside the project folder,
- installs the dependencies automatically (`pyusb`, `Pillow`, `libusb`),
- opens the GUI.

All future launches are instant — just double-click again.

> **No admin rights needed.** The app talks to the display over USB with
> user-level access (the display already uses the standard WinUSB driver).

## What the GUI does

| Control | Purpose |
|---|---|
| **Browse…** | pick a GIF, .zt theme, or image |
| **Resolution** | panel resolution (default 1600×720 for Thermalright Rainbow) |
| **Rotation** | 0/90/180/270 — use **180°** if your panel is mounted upside-down |
| **Scale** | Fit (letterbox) / Fill (crop) / Stretch (fill screen) |
| **Brightness** | click the bar (0-100, click outside the ends for 0/100) |
| **Frame rate** | 5-60 fps — only affects `.zt` themes (GIFs use their own timing) |
| **Connect / Play / Stop** | obvious 🙂 |
| **Save Theme…** | saves current clip + settings into `themes/` |
| **My Themes** | one-click load/delete of saved themes |

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
