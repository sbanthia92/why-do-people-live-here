"""Draw the account's profile picture and banner.

    python assets/make_profile_images.py

Needs Pillow (pip install pillow), which is not a dependency of the pipeline. The
images use only shapes drawn here and a system font, so there is nothing to license.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
NAVY = (15, 27, 45)
ORANGE = (242, 140, 56)
CREAM = (250, 244, 232)
MUTED = (150, 165, 185)
# Shapes are drawn large and scaled down, which smooths their edges
SCALE = 4

_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise SystemExit("No usable font found; add one to _FONTS.")


def _pin(draw: ImageDraw.ImageDraw, cx: float, top: float, size: float) -> None:
    """A map pin with a question mark, `size` tall, its head centred on cx."""
    r = size * 0.36
    cy = top + r
    tip = (cx, top + size)
    # The tail: a triangle from the sides of the head down to the tip
    draw.polygon([(cx - r * 0.86, cy + r * 0.52), (cx + r * 0.86, cy + r * 0.52), tip], fill=ORANGE)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ORANGE)
    font = _font(int(r * 1.45))
    draw.text((cx, cy - r * 0.04), "?", font=font, fill=NAVY, anchor="mm")


def profile_picture(size: int = 400) -> Image.Image:
    big = size * SCALE
    image = Image.new("RGB", (big, big), NAVY)
    draw = ImageDraw.Draw(image)
    # X crops the picture to a circle, so the pin stays well inside it
    pin_height = big * 0.62
    _pin(draw, big / 2, (big - pin_height) / 2 - big * 0.01, pin_height)
    return image.resize((size, size), Image.LANCZOS)


def banner(width: int = 1500, height: int = 500) -> Image.Image:
    w, h = width * SCALE, height * SCALE
    image = Image.new("RGB", (w, h), NAVY)
    draw = ImageDraw.Draw(image)
    # A scatter of faint dots, like settlements on an empty map
    for i in range(140):
        x = (i * 7919) % w
        y = (i * 104729) % h
        radius = SCALE * (2 + (i % 3))
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=(34, 52, 78))
    # The profile picture covers the bottom-left corner, so the text sits right of centre
    left = w * 0.415
    _pin(draw, w * 0.335, h * 0.24, h * 0.5)
    draw.text((left, h * 0.42), "Why do people live here?", font=_font(int(h * 0.128)), fill=CREAM, anchor="lm")
    draw.text(
        (left, h * 0.60),
        "One remote, harsh or strange place a day.",
        font=_font(int(h * 0.068)),
        fill=MUTED,
        anchor="lm",
    )
    return image.resize((width, height), Image.LANCZOS)


if __name__ == "__main__":
    profile_picture().save(HERE / "profile-picture.png")
    banner().save(HERE / "banner.png")
    print(f"Wrote {HERE / 'profile-picture.png'} and {HERE / 'banner.png'}")
