"""Tests for pipeline.publisher and pipeline.image_sourcer."""

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


_CANDIDATE = {
    "place_name": "Testville",
    "wikipedia_url": _URL,
    "story_category": "ghost_town",
    "region": "Asia",
}
_CARD = {"path": "/tmp/testville.png", "alt_text": "Map showing Testville."}


async def _publish(monkeypatch, *, env: dict, card, photo=None) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    """Run run_publish in a dry run with the given settings; returns the mocks it used."""
    from pipeline.publisher import run_publish

    for key in ("MAP_CARDS", "POST_IMAGES", "PREVIEW_DIR", "SOURCE_LINK"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    pool = AsyncMock()
    pool.fetchrow.return_value = _CANDIDATE
    draw, lookup, post = AsyncMock(return_value=card), AsyncMock(return_value=photo), AsyncMock(return_value=None)

    with (
        patch("pipeline.publisher.map_card_for_candidate", new=draw),
        patch("pipeline.publisher.get_image_for_candidate", new=lookup),
        patch("pipeline.publisher.post_to_x", new=post),
    ):
        await run_publish(pool, 1, {"post_text": "Hook.", "thread_parts": None}, dry_run=True)
    return draw, lookup, post


async def test_map_card_goes_on_the_first_post_by_default(monkeypatch):
    draw, lookup, post = await _publish(monkeypatch, env={}, card=_CARD)

    assert draw.await_args.args[:3] == ("Testville", _URL, "ghost town · Asia")
    lookup.assert_not_awaited()  # no photo lookup: photos stay off
    assert post.await_args.args[2] == "/tmp/testville.png"
    assert post.await_args.kwargs["alt_text"] == "Map showing Testville."


async def test_no_image_when_maps_are_off_or_the_place_has_no_coordinates(monkeypatch):
    draw, lookup, post = await _publish(monkeypatch, env={"MAP_CARDS": "false"}, card=_CARD)
    draw.assert_not_awaited()
    lookup.assert_not_awaited()
    assert post.await_args.args[2] is None

    # Coordinates missing: the card can't be drawn, and the post goes out without one
    _, lookup, post = await _publish(monkeypatch, env={}, card=None)
    lookup.assert_not_awaited()
    assert post.await_args.args[2] is None


async def test_photo_is_only_a_fallback_and_only_when_enabled(monkeypatch):
    photo = {"url": "https://upload.example/p.jpg", "credit": "Jo", "license": "CC0"}
    _, lookup, post = await _publish(
        monkeypatch, env={"POST_IMAGES": "true"}, card=None, photo=photo
    )
    lookup.assert_awaited_once()
    assert post.await_args.args[2] == "https://upload.example/p.jpg"

    _, lookup, post = await _publish(
        monkeypatch, env={"POST_IMAGES": "true"}, card=_CARD, photo=photo
    )
    lookup.assert_not_awaited()  # the map card wins
    assert post.await_args.args[2] == "/tmp/testville.png"


def test_footer_names_the_source_without_a_link_by_default(monkeypatch):
    """A post containing a link costs far more on X, so the link is opt-in."""
    monkeypatch.delenv("SOURCE_LINK", raising=False)
    encoded = "https://en.wikipedia.org/wiki/Shell%2C_Ecuador"
    assert _footer(encoded, None) == (
        "Source: Wikipedia, “Shell, Ecuador”\n\n"
        "A new remote, harsh or strange place every day. Follow for more."
    )
    assert "http" not in _footer(_URL, None)


def test_footer_credits_author_licence_and_source(monkeypatch):
    monkeypatch.setenv("SOURCE_LINK", "true")
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
        "https://commons.wikimedia.org/wiki/File:Testville.jpg\n\n"
        "A new remote, harsh or strange place every day. Follow for more."
    )
    assert _footer(_URL, None).startswith("Source: https://en.wikipedia.org/wiki/Testville\n")


async def test_thread_failure_after_first_post_keeps_what_was_published():
    """A reply that fails must not make the next run post the whole thread again."""
    import tweepy

    from pipeline.publisher import post_to_x

    client = MagicMock()
    client.create_tweet.side_effect = [
        MagicMock(data={"id": 111}),
        MagicMock(data={"id": 222}),
        tweepy.TweepyException("rate limited"),
    ]
    with patch("pipeline.publisher._x_client", return_value=client):
        first_id = await post_to_x("Hook.", ["2/ More.", "3/ End.", "Source"], None)

    assert first_id == "111"
    assert client.create_tweet.call_count == 3  # stopped at the failure
    replies_to = [c.kwargs["in_reply_to_tweet_id"] for c in client.create_tweet.call_args_list]
    assert replies_to == [None, "111", "222"]  # each post replies to the one before


async def test_failure_on_first_post_raises():
    """Nothing was published, so the run should fail and the candidate stay ready."""
    import tweepy

    from pipeline.publisher import post_to_x

    client = MagicMock()
    client.create_tweet.side_effect = tweepy.TweepyException("unauthorized")
    with (
        patch("pipeline.publisher._x_client", return_value=client),
        pytest.raises(tweepy.TweepyException),
    ):
        await post_to_x("Hook.", None, None)
