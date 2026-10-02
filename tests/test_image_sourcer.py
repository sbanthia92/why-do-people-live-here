"""Tests for pipeline.image_sourcer and the photo credit line."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline.image_sourcer import search_wikimedia
from pipeline.publisher import _footer

_URL = "https://en.wikipedia.org/wiki/Testville"


def _response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = data
    return resp


def _imageinfo(license_name: str, mime: str = "image/jpeg", **extra_meta) -> dict:
    meta = {
        "LicenseShortName": {"value": license_name},
        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
        "Artist": {"value": '<a href="//commons.wikimedia.org/wiki/User:Jo">Jo Bloggs</a>'},
        **extra_meta,
    }
    info = {
        "url": "https://upload.wikimedia.org/full.jpg",
        "thumburl": "https://upload.wikimedia.org/thumb.jpg",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Testville.jpg",
        "mime": mime,
        "extmetadata": meta,
    }
    return {"query": {"pages": [{"imageinfo": [info]}]}}


async def _search(imageinfo: dict) -> dict | None:
    pageimage = {"query": {"pages": [{"pageimage": "Testville.jpg"}]}}
    get = AsyncMock(side_effect=[_response(pageimage), _response(imageinfo)])
    with patch("pipeline.image_sourcer._get", new=get):
        return await search_wikimedia("Testville", _URL)


async def test_search_wikimedia_returns_attribution_details():
    image = await _search(_imageinfo("CC BY-SA 4.0"))

    assert image == {
        "url": "https://upload.wikimedia.org/thumb.jpg",
        "credit": "Jo Bloggs",  # HTML stripped
        "license": "CC BY-SA 4.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0",
        "page_url": "https://commons.wikimedia.org/wiki/File:Testville.jpg",
    }


@pytest.mark.parametrize("license_name", ["CC BY 2.0", "CC0", "Public domain"])
async def test_search_wikimedia_accepts_reusable_licenses(license_name):
    assert (await _search(_imageinfo(license_name)))["license"] == license_name


@pytest.mark.parametrize(
    "imageinfo",
    [
        _imageinfo("GFDL"),  # needs the full licence text reproduced
        _imageinfo("Fair use", NonFree={"value": "true"}),
        _imageinfo("CC BY-SA 4.0", NonFree={"value": "true"}),
        _imageinfo("CC BY-SA 4.0", mime="image/svg+xml"),  # flags and locator maps
        _imageinfo(""),
    ],
)
async def test_search_wikimedia_rejects_unusable_images(imageinfo):
    assert await _search(imageinfo) is None


@pytest.mark.parametrize(("setting", "expect_lookup"), [(None, False), ("true", True)])
async def test_images_are_off_unless_enabled(monkeypatch, setting, expect_lookup):
    """run_publish only looks for a photo when POST_IMAGES is switched on."""
    from pipeline.publisher import run_publish

    monkeypatch.delenv("POST_IMAGES", raising=False)
    if setting:
        monkeypatch.setenv("POST_IMAGES", setting)
    pool = AsyncMock()
    pool.fetchrow.return_value = {"place_name": "Testville", "wikipedia_url": _URL}
    lookup = AsyncMock(return_value=None)

    with (
        patch("pipeline.publisher.get_image_for_candidate", new=lookup),
        patch("pipeline.publisher.post_to_x", new=AsyncMock(return_value=None)),
    ):
        await run_publish(pool, 1, {"post_text": "Hook.", "thread_parts": None}, dry_run=True)

    assert lookup.await_count == (1 if expect_lookup else 0)


def test_footer_credits_author_licence_and_source():
    image = {
        "credit": "Jo Bloggs",
        "license": "CC BY-SA 4.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0",
        "page_url": "https://commons.wikimedia.org/wiki/File:Testville.jpg",
    }
    assert _footer(_URL, image) == (
        "Source: https://en.wikipedia.org/wiki/Testville\n"
        "Photo: Jo Bloggs, CC BY-SA 4.0 https://creativecommons.org/licenses/by-sa/4.0,"
        " via Wikimedia Commons\n"
        "https://commons.wikimedia.org/wiki/File:Testville.jpg"
    )
    assert _footer(_URL, None) == "Source: https://en.wikipedia.org/wiki/Testville"
