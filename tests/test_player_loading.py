"""Offline tests for gif_player loading (no USB hardware needed)."""

import io
import sys

sys.path.insert(0, r"D:\AI\usblcd-display")

from gif_player import load_gif, load_zt, load_static

TARGET = (1600, 720)

# Test 1: .zt theme file (ff7_1) — we know it's 44 frames at 1600x720
clip = load_zt(r"C:\TRCCCAP\Data\USBLCD\Theme1600720u\ff7_1\Theme.zt", TARGET, 95, 24)
print(f"[zt] ff7_1: {len(clip.frames)} frames")
assert len(clip.frames) == 44, f"expected 44 frames, got {len(clip.frames)}"
assert all(len(f) > 0 and f[:2] == b"\xFF\xD8" for f in clip.frames)
print(f"[zt] all frames valid JPEG, delays={clip.delays_ms[:3]}ms")
print()

# Test 2: animated GIF (use the source FF7 gif from Downloads)
import os
gif_candidates = [
    r"C:\Users\YF\Downloads\Ff Playstation GIF by Square Enix.gif",
    r"C:\Users\YF\Downloads\final fantasy lightning GIF.gif",
]
gif_path = next((p for p in gif_candidates if os.path.exists(p)), None)
if gif_path:
    clip = load_gif(gif_path, TARGET, 180, 95)
    print(f"[gif] {os.path.basename(gif_path)}: {len(clip.frames)} frames")
    assert len(clip.frames) > 1
    assert all(len(f) > 0 and f[:2] == b"\xFF\xD8" for f in clip.frames)
    print(f"[gif] delays={clip.delays_ms[:5]}ms")
    total = sum(len(f) for f in clip.frames)
    print(f"[gif] avg {total // len(clip.frames)} bytes/frame")
else:
    print("[gif] no GIF found in Downloads, skipping")

# Test 3: static image
clip = load_static(r"C:\Users\YF\AppData\Local\Temp\test_pattern.png", TARGET, 0, 95)
print(f"\n[static] {len(clip.frames)} frame, {len(clip.frames[0])} bytes")
assert len(clip.frames) == 1

print("\nALL LOADING TESTS PASSED")
