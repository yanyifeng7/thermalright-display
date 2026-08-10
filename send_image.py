#!/usr/bin/env python3
"""Send a single image to the USB LCD display.

Usage:
    python send_image.py --image frame.jpg [--variant usbdisplay] [--rotate 0|90|180|270]
"""

from __future__ import annotations

import argparse
import sys
import time

from PIL import Image

from usblcd.device import USBLCD, LCDDeviceError
from usblcd.frames import jpeg_to_frame


def main() -> int:
    ap = argparse.ArgumentParser(description="Send an image to the USB LCD")
    ap.add_argument("--image", required=True, help="Image file (JPEG/PNG/GIF first frame)")
    ap.add_argument("--variant", default="usbdisplay", choices=["usbdisplay", "h", "ali", "ly", "ly1"])
    ap.add_argument("--width", type=int, default=1600, help="Target panel width")
    ap.add_argument("--height", type=int, default=720, help="Target panel height")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                    help="Rotate image before sending")
    ap.add_argument("--quality", type=int, default=95, help="JPEG quality (TRCC uses 95 default)")
    ap.add_argument("--stay", action="store_true", help="Keep sending the frame (some panels blank on idle)")
    args = ap.parse_args()

    # Load and prepare image
    img = Image.open(args.image)
    img = img.convert("RGB")

    # Fit to panel with letterboxing (matches TRCC behavior)
    target = (args.width, args.height)
    img = fit_image(img, target)
    if args.rotate:
        img = img.rotate(-args.rotate, expand=True)

    # JPEG-encode (this is what the mode-2/1600x720 device expects)
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=args.quality)
    jpeg = buf.getvalue()
    frame = jpeg_to_frame(jpeg, args.width, args.height)
    print(f"Frame: {img.size[0]}x{img.size[1]} -> JPEG {len(jpeg)} bytes "
          f"+ 64B header = {len(frame)} bytes")

    try:
        with USBLCD(args.variant) as lcd:
            print(f"Device opened: {args.variant} ({lcd.spec['vid']:04X}:{lcd.spec['pid']:04X})")
            resp = lcd.handshake()
            print(f"Handshake OK ({len(resp)} bytes response)")

            # Send the framed JPEG (mode-2 device: 64B header + JPEG payload)
            lcd.send_frame(frame)
            print("Frame sent. Check the display!")

            if args.stay:
                print("Keeping display alive (Ctrl+C to stop)...")
                try:
                    while True:
                        time.sleep(1)
                        try:
                            lcd.send_frame(frame)
                        except Exception as e:
                            print(f"  send failed ({e}), reconnecting...")
                            lcd.close()
                            # Re-open and re-handshake
                            lcd.dev = None
                            if lcd.find():
                                lcd.open()
                                lcd.handshake()
                            time.sleep(2)
                except KeyboardInterrupt:
                    pass
    except LCDDeviceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # usb.core errors etc.
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


def fit_image(img: Image.Image, target: tuple[int, int]) -> Image.Image:
    """Scale image to fit target, letterbox with black."""
    tw, th = target
    iw, ih = img.size
    scale = min(tw / iw, th / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", target, (0, 0, 0))
    canvas.paste(img, ((tw - nw) // 2, (th - nh) // 2))
    return canvas


if __name__ == "__main__":
    sys.exit(main())
