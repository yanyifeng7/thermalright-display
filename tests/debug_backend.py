import os
print("PATH at start:", os.environ.get("PATH", "")[:100])
print("root in PATH:", "usblcd-display" in os.environ.get("PATH", ""))

# Import device (runs its PATH bootstrap)
from usblcd.device import USBLCD
print("PATH after device import:", os.environ.get("PATH", "")[:120])
import usb.backend.libusb1
be = usb.backend.libusb1.get_backend()
print("backend:", be)
