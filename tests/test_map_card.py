"""Tests for pipeline.map_card: the locator card drawn for each post."""

import io
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from PIL import Image

from pipeline import map_card
from pipeline.map_card import (
    HEIGHT,
    LAND,
    SEA,
    WIDTH,
    distance_km,
    map_card_for_candidate,
    nearest_major_city,
    render_map_card,
)


def _close(colour: tuple, expected: tuple, tolerance: int = 12) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(colour[:3], expected, strict=True))


def _card(name: str, lat: float, lon: float) -> Image.Image:
    return Image.open(io.BytesIO(render_map_card(name, lat, lon, "test · Somewhere"))).convert("RGB")


def test_distance_and_nearest_city():
    assert distance_km(51.5, -0.13, 48.86, 2.35) == pytest.approx(344, abs=5)  # London–Paris
    name, km = nearest_major_city(76.42, -82.90)  # Grise Fiord
    assert name == "Ottawa" and km == pytest.approx(3460, abs=30)


def test_card_is_a_16_by_9_png_small_enough_for_x():
    png = render_map_card("Heimaey", 63.43, -20.27, "island community · Europe")
    assert Image.open(io.BytesIO(png)).size == (WIDTH, HEIGHT) == (1600, 900)
    assert len(png) < 5 * 1024 * 1024  # X's limit for an image


@pytest.mark.parametrize(
    ("name", "lat", "lon", "at_the_place"),
    [
        ("Vostok Station", -78.46, 106.84, LAND),  # deep inside Antarctica
        ("Funafuti", -8.52, 179.2, SEA),  # mid-Pacific, with Africa exactly opposite
        ("Point Nemo", -48.88, -123.39, SEA),  # as far from land as it gets
        ("South Pole", -90.0, 0.0, LAND),
        ("North Pole", 90.0, 0.0, SEA),
    ],
)
def test_land_and_sea_are_right_at_awkward_places(name, lat, lon, at_the_place):
    """A landmass on the far side of the globe must not flood the map."""
    image = _card(name, lat, lon)
    # Just outside the dot and its halo, on the regional map and on the small globe
    assert _close(image.getpixel((450 + 40, 450)), at_the_place), "regional map"
    assert _close(image.getpixel((1434 + 16, 178)), at_the_place), "globe"


def test_long_and_accented_names_fit():
    for name in ("Amundsen–Scott South Pole Station", "Ittoqqortoormiit", "Llívia", "Træna"):
        assert _card(name, 60.0, 10.0).size == (WIDTH, HEIGHT)


async def test_map_card_for_candidate_writes_a_file_with_alt_text(tmp_path):
    summary = MagicMock()
    summary.json.return_value = {"coordinates": {"lat": 63.43, "lon": -20.27}}
    with patch("pipeline.map_card._get", new=AsyncMock(return_value=summary)):
        card = await map_card_for_candidate(
            "Heimaey", "https://en.wikipedia.org/wiki/Heimaey", "island community · Europe", tmp_path
        )

    assert card["path"] == str(tmp_path / "heimaey.png")
    assert Image.open(card["path"]).size == (WIDTH, HEIGHT)
    assert card["alt_text"] == (
        "Map showing the location of Heimaey at 63.43°N  20.27°W."
        " The nearest major city, Dublin, is about 1,380 km away."
    )


async def test_map_card_for_candidate_gives_up_quietly(tmp_path):
    """No coordinates, or Wikipedia unreachable: no card, and no exception."""
    no_coordinates = MagicMock()
    no_coordinates.json.return_value = {"title": "Neutral Moresnet"}
    with patch("pipeline.map_card._get", new=AsyncMock(return_value=no_coordinates)):
        assert await map_card_for_candidate("X", "https://en.wikipedia.org/wiki/X", "", tmp_path) is None

    with patch("pipeline.map_card._get", new=AsyncMock(side_effect=httpx.ConnectError("down"))):
        assert await map_card_for_candidate("X", "https://en.wikipedia.org/wiki/X", "", tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_map_data_is_bundled():
    data = map_card._data()
    assert len(data["land"]) > 1000 and len(data["cities"]) > 200
