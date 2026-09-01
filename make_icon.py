"""Generate tray icon assets/icon.ico and icon.png."""
import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
OUT = Path(__file__).parent / "assets"
OUT.mkdir(exist_ok=True)


def rounded_ring_icon(size: int = SIZE) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = size * 0.02
    radius = size * 0.22
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=radius,
                        fill=(17, 24, 39, 255))  # #111827

    def arc(bbox_scale, width_scale, color, start=-90, sweep=270):
        m = size * bbox_scale
        box = [m, m, size - m, size - m]
        w = int(size * width_scale)
        d.arc(box, start=0, end=360, fill=(55, 65, 81, 255), width=w)
        d.arc(box, start=start, end=start + sweep, fill=color, width=w)
        ang = math.radians(start + sweep)
        cx, cy = size / 2, size / 2
        r = (size - 2 * m) / 2
        x, y = cx + r * math.cos(ang), cy + r * math.sin(ang)
        rr = w / 2
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=color)

    arc(0.16, 0.075, (34, 211, 238, 255))    # cyan-400
    arc(0.30, 0.075, (167, 139, 250, 255), sweep=200)  # violet-400

    return img


def main():
    img = rounded_ring_icon()
    img.save(OUT / "icon.png")
    img.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    print("written:", OUT / "icon.ico")


if __name__ == "__main__":
    main()
