from PIL import Image
from usblcd.frames import image_to_rgb565, rgb565_to_image, bgr_to_rgb565

# TRCC layout check: hi = G[7:5]|B[7:3], lo = R[7:3]|G[7:5]
# red   (r=255,g=0,b=0): hi = 0 + 0 = 0x00, lo = 0xF8 + 0 = 0xF8
# blue  (r=0,g=0,b=255): hi = 0 + 31 = 0x1F, lo = 0 + 0 = 0x00
# green (r=0,g=255,b=0): hi = 0xE0 + 0 = 0xE0, lo = 0 + 7 = 0x07
hi, lo = bgr_to_rgb565(0, 0, 255)
assert (hi, lo) == (0x00, 0xF8), f"red expected (0x00,0xF8) got ({hi:02X},{lo:02X})"
print("red  -> bytes 00 F8  OK")

hi, lo = bgr_to_rgb565(255, 0, 0)
assert (hi, lo) == (0x1F, 0x00), f"blue expected (0x1F,0x00) got ({hi:02X},{lo:02X})"
print("blue -> bytes 1F 00  OK")

hi, lo = bgr_to_rgb565(0, 255, 0)
assert (hi, lo) == (0xE0, 0x07), f"green expected (0xE0,0x07) got ({hi:02X},{lo:02X})"
print("green-> bytes E0 07  OK")

# Round trip: decode(encode(x)) should reconstruct channels within RGB565 tolerance
colors = {
    "red":   (255, 0, 0),
    "green": (0, 255, 0),
    "blue":  (0, 0, 255),
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "gray":  (128, 128, 128),
}
max_err = 0
for name, (r, g, b) in colors.items():
    img = Image.new("RGB", (4, 4), (r, g, b))
    data = image_to_rgb565(img)
    back = rgb565_to_image(data, 4, 4)
    got = back.getpixel((0, 0))
    err = max(abs(got[i] - (r, g, b)[i]) for i in range(3))
    max_err = max(max_err, err)
    print(f"{name:6} in=({r:3},{g:3},{b:3}) out=({got[0]:3},{got[1]:3},{got[2]:3}) err={err}")

assert max_err <= 40, f"round-trip error too large: {max_err}"
print(f"\nAll checks passed. Max round-trip error: {max_err}")
