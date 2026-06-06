"""Generate apple-touch-icon.png from the same geometry as favicon.svg.

iOS Safari ignores SVG favicons for the home-screen icon and requires a PNG
declared via <link rel="apple-touch-icon">. This script reproduces the exact
shapes in favicon.svg (clapperboard + play triangle) at 180x180, the standard
apple-touch-icon size, using supersampling for smooth anti-aliased edges.

This is a build-time tool, not a runtime dependency. Run it whenever the SVG
changes:

    pip install pillow
    python static/generate_apple_touch_icon.py
"""

from PIL import Image, ImageDraw

# Source SVG viewBox is 0 0 32 32. Target output is 180x180.
VIEWBOX = 32
TARGET = 180
SS = 8  # supersampling factor for anti-aliasing
S = (TARGET / VIEWBOX) * SS  # units -> supersampled pixels

BG = (14, 14, 16, 255)        # #0e0e10
ACCENT = (167, 139, 250, 255)  # #a78bfa
STROKE_W = 1.6

full = TARGET * SS
img = Image.new("RGBA", (full, full), (0, 0, 0, 0))
d = ImageDraw.Draw(img)


def rrect(x, y, w, h, r, **kw):
    d.rounded_rectangle([x * S, y * S, (x + w) * S, (y + h) * S], radius=r * S, **kw)


# Background rounded square (rx=7). Corners stay transparent, matching the SVG.
rrect(0, 0, VIEWBOX, VIEWBOX, 7, fill=BG)

# Outlined rectangle (the clapperboard body). SVG stroke is centred on the path,
# so outset the box by half the stroke width and inflate the radius to match.
half = STROKE_W / 2
d.rounded_rectangle(
    [(4 - half) * S, (9 - half) * S, (28 + half) * S, (23 + half) * S],
    radius=(2 + half) * S,
    outline=ACCENT,
    width=round(STROKE_W * S),
)

# Clapper "teeth": three on top, three on bottom (rx=1).
for x, w in ((7, 4), (13, 3), (18, 3)):
    rrect(x, 6, w, 5, 1, fill=ACCENT)   # top row
    rrect(x, 21, w, 5, 1, fill=ACCENT)  # bottom row

# Play triangle.
d.polygon([(13 * S, 13 * S), (21 * S, 16 * S), (13 * S, 19 * S)], fill=ACCENT)

img = img.resize((TARGET, TARGET), Image.LANCZOS)
out = __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0] + "/apple-touch-icon.png"
img.save(out, "PNG")
print("Wrote", out)
