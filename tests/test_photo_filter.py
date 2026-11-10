"""Tests for pipeline.photo_filter: shape, 'not a photo' names and the AI check."""

import base64
from unittest.mock import AsyncMock, patch

import pytest

from pipeline import llm
from pipeline.photo_filter import (
    PhotoVerdict,
    judge_photo,
    looks_like_not_a_photo,
    shape_problem,
)


def test_normal_photos_pass_the_shape_check():
    assert shape_problem(1600, 900) is None
    assert shape_problem(800, 534) is None  # the smallest sensible size
    assert shape_problem(900, 1200) is None  # a portrait
    assert shape_problem(2000, 1000) is None  # 2:1


def test_small_files_and_thin_strips_fail_with_a_reason():
    assert "too small" in shape_problem(111, 83)
    assert "too small" in shape_problem(1600, 400)
    assert "wrong shape" in shape_problem(2166, 700)  # a panorama, cropped badly by X
    assert "wrong shape" in shape_problem(800, 1600)  # a tall strip
    assert shape_problem(None, 900) == "size unknown"


@pytest.mark.parametrize(
    "text",
    [
        "Flag of Alaska.png",
        "Coat_of_arms_of_Peru.png",
        "Yuzhnoukrayinsk prapor.png",
        "Yuzhnoukrainsk gerb.PNG",
        "Прапор міста.png",
        "Locator map of Nevada.jpg",
        "Map of Ontong Java Atoll",
        "Town logo.png",
    ],
)
def test_names_that_say_flag_map_or_logo_are_not_photos(text):
    assert looks_like_not_a_photo(text)


@pytest.mark.parametrize(
    "text",
    ["Maple Street shops.jpg", "Seal colony on the beach.jpg", "Open arms church.jpg", "Main street.jpg", ""],
)
def test_ordinary_names_are_not_mistaken_for_non_photos(text):
    assert not looks_like_not_a_photo(text)


def test_only_a_real_photo_of_the_place_is_accepted():
    def verdict(is_photo, place):
        return PhotoVerdict(reason="r", is_photo=is_photo, place_match=place, what_it_shows="x")

    assert verdict(True, "yes").accepted
    assert not verdict(True, "no").accepted
    assert not verdict(True, "unsure").accepted  # nothing says where it is
    assert not verdict(False, "yes").accepted  # a map of the right place is still no photo


async def test_the_photo_is_sent_to_the_cheaper_model_with_the_place_and_where_it_came_from():
    answer = PhotoVerdict(reason="r", is_photo=True, place_match="yes", what_it_shows="a street")

    with patch("pipeline.llm.parse", new=AsyncMock(return_value=answer)) as parse:
        result = await judge_photo(
            "Afton, Nevada", b"\x89PNGdata", "image/png", "Main street.png", "A street", "article"
        )

    assert result is answer
    kwargs = parse.call_args.kwargs
    assert kwargs["model"] == llm.PHOTO_MODEL and kwargs["effort"] == "low"
    assert kwargs["output_format"] is PhotoVerdict
    image, question = kwargs["user"]
    assert image["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.standard_b64encode(b"\x89PNGdata").decode(),
    }
    assert "Place: Afton, Nevada" in question["text"]
    assert "File name: Main street.png" in question["text"]
    assert "Wikipedia article" in question["text"]
