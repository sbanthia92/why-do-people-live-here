"""Draw the account's profile picture and banner.

    python assets/make_profile_images.py

Both use the same map data and colours as the cards attached to posts (see
pipeline/map_card.py), so there is nothing to license. The banner plots the places in
assets/places.json: the hand-picked places from config/seed_categories.yaml, with
coordinates looked up on Wikipedia.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from PIL import Image, ImageDraw

from pipeline.map_card import CREAM, LAND, MUTED, NAVY, ORANGE, RING, SEA, _data, _font, _Projection

# Shapes are drawn large and scaled down, which smooths their edges
SCALE = 4


def _pin(draw: ImageDraw.ImageDraw, cx: float, top: float, size: float, outline: float = 0) -> None:
    """A map pin with a question mark, `size` tall, its head centred on cx.

    `outline` draws a dark rim that width wide, to lift the pin off a busy background.
    """
    r = size * 0.36
    cy = top + r
    tip = (cx, top + size)

    def shape(grow: float, colour: tuple) -> None:
        rr = r + grow
        draw.polygon(
            [(cx - rr * 0.86, cy + rr * 0.52), (cx + rr * 0.86, cy + rr * 0.52), (tip[0], tip[1] + grow * 1.6)],
            fill=colour,
        )
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=colour)

    if outline:
        shape(outline, NAVY)
    shape(0, ORANGE)
    draw.text((cx, cy - r * 0.04), "?", font=_font(int(r * 1.45), bold=True), fill=NAVY, anchor="mm")


def profile_picture(size: int = 400) -> Image.Image:
    """The pin standing on a globe. X crops this to a circle, so the globe fills it."""
    big = size * SCALE
    image = Image.new("RGB", (big, big), NAVY)
    draw = ImageDraw.Draw(image)

    # A globe a little larger than the frame, seen from above the North Atlantic, so
    # the circle X cuts out is all globe
    centre, radius = big / 2, big * 0.56
    projection = _Projection(38, -30)
    draw.ellipse([centre - radius, centre - radius, centre + radius, centre + radius], fill=SEA)
    for ring in _data()["land_coarse"]:
        if points := projection.globe(ring):
            draw.polygon([(centre + x * radius, centre - y * radius) for x, y in points], fill=LAND)

    pin_height = big * 0.56
    _pin(draw, centre, (big - pin_height) / 2 - big * 0.015, pin_height, outline=big * 0.022)
    return image.resize((size, size), Image.LANCZOS)


def banner(width: int = 1500, height: int = 500) -> Image.Image:
    """A world map with a dot at every place on the account's list."""
    w, h = width * SCALE, height * SCALE
    image = Image.new("RGB", (w, h), SEA)
    draw = ImageDraw.Draw(image)

    # The whole width is 360° of longitude; the height runs from 84°N to 63°S, which
    # keeps Arctic and Antarctic-fringe settlements in frame
    north, south = 84.0, -63.0

    def at(lon: float, lat: float) -> tuple[float, float]:
        return (lon + 180) / 360 * w, (north - lat) / (north - south) * h

    for ring in _data()["land"]:
        draw.polygon([at(lon, lat) for lon, lat in ring], fill=LAND)
    for ring in _data()["lakes"]:
        draw.polygon([at(lon, lat) for lon, lat in ring], fill=SEA)

    places = json.loads((HERE / "places.json").read_text())
    for lat, lon in places.values():
        if not south < lat < north:
            continue
        x, y = at(lon, lat)
        for r, colour in ((7.0, NAVY), (4.6, ORANGE)):
            draw.ellipse([x - r * SCALE, y - r * SCALE, x + r * SCALE, y + r * SCALE], fill=colour)

    # A dark band behind the lettering. The profile picture covers the bottom-left
    # corner on X, so the lettering sits to the right of it.
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shade = ImageDraw.Draw(overlay)
    band_top, band_bottom = h * 0.60, h * 0.93
    shade.rectangle([0, band_top, w, band_bottom], fill=(*NAVY, 218))
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.line([0, band_top, w, band_top], fill=RING, width=SCALE)
    draw.line([0, band_bottom, w, band_bottom], fill=RING, width=SCALE)

    left = w * 0.27
    middle = (band_top + band_bottom) / 2
    draw.text((left, middle - h * 0.045), "Why do people live here?", font=_font(int(h * 0.118), bold=True), fill=CREAM, anchor="lm")
    draw.text(
        (left, middle + h * 0.085),
        "One remote, harsh or strange place a day. Every dot is on the list.",
        font=_font(int(h * 0.052)),
        fill=MUTED,
        anchor="lm",
    )
    return image.resize((width, height), Image.LANCZOS)


if __name__ == "__main__":
    profile_picture().save(HERE / "profile-picture.png")
    banner().save(HERE / "banner.png")
    print(f"Wrote {HERE / 'profile-picture.png'} and {HERE / 'banner.png'}")
