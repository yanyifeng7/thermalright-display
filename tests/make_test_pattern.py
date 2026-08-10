from PIL import Image, ImageDraw

# Generate a simple test pattern: red/green/blue quadrants + text-free grid
img = Image.new("RGB", (1600, 720))
d = ImageDraw.Draw(img)
w, h = 1600, 720
# Quadrants
d.rectangle([0, 0, w//2, h//2], fill=(255, 0, 0))
d.rectangle([w//2, 0, w, h//2], fill=(0, 255, 0))
d.rectangle([0, h//2, w//2, h], fill=(0, 0, 255))
d.rectangle([w//2, h//2, w, h], fill=(255, 255, 255))
# Border
d.rectangle([0, 0, w-1, h-1], outline=(0, 0, 0), width=8)
img.save(r"C:\Users\YF\AppData\Local\Temp\test_pattern.png")
print("test_pattern.png written")
