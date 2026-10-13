"""Draw a locator card for a place: where on Earth it is, and how far from anywhere.

Everything on the card is drawn here from Natural Earth's public-domain coastlines and
city list (see pipeline/data/README.md), so there is no photograph and no licence to
get wrong.
"""

import gzip
import io
import itertools
import json
import math
import urllib.parse
from functools import lru_cache
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont
from rich.console import Console

from pipeline.candidate_selector import _HEADERS, _WP_REST, _get

console = Console()

_DATA = Path(__file__).parent / "data" / "natural_earth.json.gz"
EARTH_RADIUS_KM = 6371.0

WIDTH, HEIGHT = 1600, 900
# Shapes are drawn at this multiple and scaled down, which smooths their edges
_SCALE = 2
_MAP = HEIGHT  # the regional map is a square filling the card's height

NAVY = (15, 27, 45)
SEA = (21, 36, 58)
LAND = (52, 74, 104)
LAKE = SEA
RING = (60, 82, 112)
ORANGE = (242, 140, 56)
CREAM = (250, 244, 232)
MUTED = (150, 165, 185)

# A city this large counts as "major" for the nearest-city line
_MAJOR_CITY_POPULATION = 300_000
# The regional map shows at least this far in every direction...
_MIN_RADIUS_KM = 1200
# ...and never more than this, however far the nearest city is
_MAX_RADIUS_KM = 4500
_MAX_CITY_LABELS = 5

_BOLD_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)
_REGULAR_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads(gzip.decompress(_DATA.read_bytes()))


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in _BOLD_FONTS if bold else _REGULAR_FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    # Pillow's built-in font: always available, but one weight and fewer accented letters
    return ImageFont.load_default(size)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def nearest_major_city(lat: float, lon: float) -> tuple[str, float]:
    """(name, distance in km) of the closest city of at least _MAJOR_CITY_POPULATION."""
    cities = [c for c in _data()["cities"] if c[3] >= _MAJOR_CITY_POPULATION]
    name, clat, clon, _ = min(cities, key=lambda c: distance_km(lat, lon, c[1], c[2]))
    return name, distance_km(lat, lon, clat, clon)


class _Projection:
    """Views of the globe centred on one point."""

    def __init__(self, lat: float, lon: float):
        self.lat0, self.lon0 = math.radians(lat), math.radians(lon)
        self.sin0, self.cos0 = math.sin(self.lat0), math.cos(self.lat0)

    def _parts(self, lon: float, lat: float) -> tuple[float, float, float]:
        """Unit-sphere x (east), y (north) and cosine of the angle from the centre."""
        phi, dl = math.radians(lat), math.radians(lon) - self.lon0
        sin_p, cos_p = math.sin(phi), math.cos(phi)
        x = cos_p * math.sin(dl)
        y = self.cos0 * sin_p - self.sin0 * cos_p * math.cos(dl)
        cos_c = self.sin0 * sin_p + self.cos0 * cos_p * math.cos(dl)
        return x, y, max(-1.0, min(1.0, cos_c))

    def equidistant(self, lon: float, lat: float) -> tuple[float, float]:
        """Kilometres east and north of the centre, true to distance and direction."""
        x, y, cos_c = self._parts(lon, lat)
        # Near the far side of the globe the projection blows up; hold it at 170°
        c = min(math.acos(cos_c), math.radians(170))
        norm = math.hypot(x, y)
        if norm < 1e-12:
            return 0.0, 0.0
        k = c * EARTH_RADIUS_KM / norm
        return x * k, y * k

    def globe(self, ring: list) -> list[tuple[float, float]] | None:
        """A coastline's positions on a unit disc as seen from above the centre.

        Returns None if the whole shape is on the far side. Otherwise its hidden
        points are pushed to the rim, which keeps a coastline that crosses the
        horizon closed.
        """
        parts = [self._parts(lon, lat) for lon, lat in ring]
        if all(cos_c < 0 for _, _, cos_c in parts):
            return None
        points = []
        for x, y, cos_c in parts:
            if cos_c < 0:
                norm = math.hypot(x, y) or 1.0
                x, y = x / norm, y / norm
            points.append((x, y))
        return points


def _contains(ring: list, lon: float, lat: float) -> bool:
    """Whether a coastline encloses a point, treating longitude and latitude as flat."""
    inside = False
    for (x1, y1), (x2, y2) in itertools.pairwise(ring):
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _far_side_landmass(rings: list, lat: float, lon: float) -> list | None:
    """The landmass holding the point exactly opposite (lat, lon) on the globe, if any.

    Seen from the centre, that landmass lies in every direction at once, so its
    coastline wraps around the whole view and has to be drawn inside out: land
    everywhere, then the coastline's interior filled back in as sea.
    """
    anti_lon = lon - 180 if lon > 0 else lon + 180
    return next((ring for ring in rings if _contains(ring, anti_lon, -lat)), None)


def _nice_ring_step(radius_km: float) -> int:
    """A round distance that puts two to four rings inside the map."""
    for step in (250, 500, 1000, 2000):
        if radius_km / step <= 4:
            return step
    return 2000


def _fit(draw: ImageDraw.ImageDraw, text: str, width: int, size: int, bold: bool = True):
    """The largest font, at most `size`, at which `text` fits in `width`."""
    while size > 24:
        font = _font(size, bold)
        if draw.textlength(text, font=font) <= width:
            return font
        size -= 4
    return _font(size, bold)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return [*lines, line]


def _format_coordinates(lat: float, lon: float) -> str:
    return f"{abs(lat):.2f}°{'N' if lat >= 0 else 'S'}  {abs(lon):.2f}°{'E' if lon >= 0 else 'W'}"


def render_map_card(place_name: str, lat: float, lon: float, context: str = "") -> bytes:
    """PNG bytes of the locator card for a place.

    `context` is a short line shown under the name, such as "island community · Europe".
    """
    s = _SCALE
    image = Image.new("RGB", (WIDTH * s, HEIGHT * s), NAVY)
    draw = ImageDraw.Draw(image)
    data, projection = _data(), _Projection(lat, lon)
    city_name, city_km = nearest_major_city(lat, lon)

    # --- Regional map: a square on the left, centred on the place ----------------
    radius_km = max(_MIN_RADIUS_KM, min(_MAX_RADIUS_KM, city_km * 1.18))
    size = _MAP * s
    centre = size / 2
    px_per_km = (size / 2) / radius_km

    def on_map(lon_: float, lat_: float) -> tuple[float, float]:
        east, north = projection.equidistant(lon_, lat_)
        return centre + east * px_per_km, centre - north * px_per_km

    reach_km = radius_km * 3  # shapes entirely beyond this can't touch the map

    def in_reach(ring: list) -> bool:
        sample = ring[:: max(1, len(ring) // 40)]
        return min(distance_km(lat, lon, y, x) for x, y in sample) <= reach_km

    draw.rectangle([0, 0, size, size], fill=SEA)
    far_side = _far_side_landmass(data["land"], lat, lon)
    if far_side is not None and in_reach(far_side):
        draw.rectangle([0, 0, size, size], fill=LAND)
        draw.polygon([on_map(x, y) for x, y in far_side], fill=SEA)
    for shapes, colour in ((data["land"], LAND), (data["lakes"], LAKE)):
        for ring in shapes:
            if ring is not far_side and in_reach(ring):
                draw.polygon([on_map(x, y) for x, y in ring], fill=colour)

    step = _nice_ring_step(radius_km)
    small = _font(22 * s)
    for km in range(step, int(radius_km * 1.5), step):
        r = km * px_per_km
        draw.ellipse([centre - r, centre - r, centre + r, centre + r], outline=RING, width=2 * s)
        if r < size / 2 - 30 * s:
            draw.text((centre, centre - r - 6 * s), f"{km:,} km", font=small, fill=MUTED, anchor="ms")

    # Cities inside the map, largest first, with room kept around the place itself
    shown = 0
    for name, clat, clon, _population in data["cities"]:
        km = distance_km(lat, lon, clat, clon)
        if shown >= _MAX_CITY_LABELS or km > radius_km * 0.96 or km < radius_km * 0.12:
            continue
        x, y = on_map(clon, clat)
        draw.ellipse([x - 5 * s, y - 5 * s, x + 5 * s, y + 5 * s], fill=CREAM)
        draw.text((x + 12 * s, y), name, font=small, fill=CREAM, anchor="lm")
        shown += 1

    # The place: a dot with a halo
    for r, colour in ((26, None), (11, ORANGE)):
        box = [centre - r * s, centre - r * s, centre + r * s, centre + r * s]
        if colour:
            draw.ellipse(box, fill=colour)
        else:
            draw.ellipse(box, outline=ORANGE, width=3 * s)

    # Everything right of the map is the text panel
    draw.rectangle([size, 0, WIDTH * s, HEIGHT * s], fill=NAVY)
    left = size + 56 * s
    panel = WIDTH * s - left - 48 * s

    # --- Globe: where on Earth the regional map is --------------------------------
    globe_r = 118 * s
    gx, gy = WIDTH * s - 48 * s - globe_r, 60 * s + globe_r
    disc = [gx - globe_r, gy - globe_r, gx + globe_r, gy + globe_r]

    def on_globe(points: list) -> list:
        return [(gx + x * globe_r, gy - y * globe_r) for x, y in points]

    draw.ellipse(disc, fill=SEA)
    far_side = _far_side_landmass(data["land_coarse"], lat, lon)
    if far_side is not None and (points := projection.globe(far_side)):
        draw.ellipse(disc, fill=LAND)
        draw.polygon(on_globe(points), fill=SEA)
    for ring in data["land_coarse"]:
        if ring is not far_side and (points := projection.globe(ring)):
            draw.polygon(on_globe(points), fill=LAND)
    draw.ellipse([gx - globe_r, gy - globe_r, gx + globe_r, gy + globe_r], outline=RING, width=2 * s)
    draw.ellipse([gx - 9 * s, gy - 9 * s, gx + 9 * s, gy + 9 * s], fill=ORANGE)

    # --- Text ----------------------------------------------------------------------
    # The name: as large as fits on two lines
    for name_size in range(84, 36, -4):
        name_font = _fit(draw, max(place_name.split(), key=len), panel, name_size * s)
        lines = _wrap(draw, place_name, name_font, panel)
        if len(lines) <= 2:
            break
    y = (410 if len(lines) == 1 else 370) * s
    for line in lines[:2]:
        draw.text((left, y), line, font=name_font, fill=CREAM, anchor="ls")
        y += int(name_font.size * 1.12)
    y -= int(name_font.size * 0.55)

    body = _font(30 * s)
    if context:
        draw.text((left, y), context, font=body, fill=MUTED, anchor="ls")
        y += 46 * s
    draw.text((left, y), _format_coordinates(lat, lon), font=body, fill=MUTED, anchor="ls")

    y += 78 * s
    draw.text((left, y), "NEAREST MAJOR CITY", font=_font(22 * s, bold=True), fill=ORANGE, anchor="ls")
    y += 50 * s
    city_font = _fit(draw, city_name, panel, 44 * s)
    draw.text((left, y), city_name, font=city_font, fill=CREAM, anchor="ls")
    y += 58 * s
    rounded = round(city_km, -1) if city_km >= 100 else round(city_km)
    draw.text((left, y), f"{rounded:,.0f} km away", font=_font(38 * s, bold=True), fill=CREAM, anchor="ls")

    draw.text(
        (left, HEIGHT * s - 52 * s),
        "Why do people live here?",
        font=_font(28 * s, bold=True),
        fill=MUTED,
        anchor="ls",
    )

    out = io.BytesIO()
    image.resize((WIDTH, HEIGHT), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


async def fetch_coordinates(wikipedia_url: str) -> tuple[float, float] | None:
    """(lat, lon) from the article's Wikipedia summary, or None if it has none."""
    title = urllib.parse.unquote(wikipedia_url.rstrip("/").split("/wiki/")[-1])
    async with httpx.AsyncClient(timeout=15.0, headers=_HEADERS) as client:
        resp = await _get(client, f"{_WP_REST}/{urllib.parse.quote(title)}")
        resp.raise_for_status()
        coordinates = resp.json().get("coordinates")
    if not coordinates:
        return None
    return float(coordinates["lat"]), float(coordinates["lon"])


async def map_card_for_candidate(
    place_name: str, wikipedia_url: str, context: str, directory: Path
) -> dict | None:
    """Draw the card for a candidate and save it to `directory`.

    Returns {"path", "alt_text"}, or None if the place has no coordinates. Never
    raises: a post without a map is better than no post.
    """
    try:
        coordinates = await fetch_coordinates(wikipedia_url)
        if coordinates is None:
            console.log(f"[map_card] No coordinates for {place_name}; posting without a map")
            return None
        lat, lon = coordinates
        png = render_map_card(place_name, lat, lon, context)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        console.log(f"[map_card] Could not draw a map for {place_name}: {exc!r}")
        return None

    directory.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() else "-" for c in place_name.lower()).strip("-")
    path = directory / f"{slug}.png"
    path.write_bytes(png)
    city, km = nearest_major_city(lat, lon)
    return {
        "path": str(path),
        "alt_text": (
            f"Map showing the location of {place_name} at {_format_coordinates(lat, lon)}."
            f" The nearest major city, {city}, is about {round(km, -1):,.0f} km away."
        ),
    }
