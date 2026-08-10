import os, sys, traceback
sys.path.insert(0, r"D:\AI\usblcd-display")

from usblcd.device import USBLCD, LCDDeviceError
from usblcd.frames import image_to_rgb565
from PIL import Image

img = Image.open(r"C:\Users\YF\AppData\Local\Temp\test_pattern.png").convert("RGB")
payload = image_to_rgb565(img)
print(f"payload: {len(payload)} bytes")

lcd = USBLCD("usbdisplay")
try:
    found = lcd.find()
    print("find():", found)
    if found:
        dev = lcd.dev
        print("configs:", dev.bNumConfigurations)
        # Check kernel driver status
        try:
            active = dev.is_kernel_driver_active(0)
            print("kernel driver active:", active)
        except Exception as e:
            print("kernel driver check:", type(e).__name__, e)
        try:
            lcd.open()
            print("open(): OK")
        except Exception as e:
            print("open() FAILED:", type(e).__name__, e)
            traceback.print_exc()
finally:
    lcd.close()
