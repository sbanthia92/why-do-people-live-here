"""Tests for pipeline.attribution: naming the outlets a post's facts came from."""

from unittest.mock import AsyncMock, patch

from pipeline import llm
from pipeline.attribution import OutletNames, name_outlets, outlet_name, sites_used


def _source(url, name=""):
    return {"url": url, "source_name": name, "snippet": "s"}


def test_the_sites_own_name_is_taken_from_the_page_title_when_it_matches_the_address():
    source = _source(
        "https://questionofcities.org/remember-bhilai/",
        "Remember Bhilai? The workers\u2019 township now lies abandoned - Question of Cities",
    )
    assert outlet_name(source) == "Question of Cities"


def test_otherwise_the_name_comes_from_the_address():
    assert outlet_name(_source("https://www.britannica.com/place/Bhilai", "Bhilai")) == "Britannica"
    assert outlet_name(_source("https://www.bbc.co.uk/news/1", "Steel town - a history")) == "Bbc"
    # A title tail that is not the site's name is not trusted
    assert outlet_name(_source("https://example.org/a", "Bhilai - India's steel city")) == "Example"


def test_names_never_contain_a_dot_so_x_cannot_turn_them_into_links():
    source = _source("https://findeasy.in/population", "Population of Bhilai | FindEasy.in")
    assert outlet_name(source) == "Findeasy"
    assert outlet_name(_source("not a url")) is None


KIVALINA = [
    _source("https://www.commerce.alaska.gov/a", "Community: Kivalina"),
    _source("https://www.adn.com/1", "Kivalina's long wait - Anchorage Daily News"),
    _source("https://www.adn.com/2", "Seawall - Anchorage Daily News"),
    _source("https://kivalina.weebly.com/", "Kivalina"),
    _source("https://www.poa.usace.army.mil/x", "Kivalina erosion"),
    _source("https://www.huffpost.com/y", "A village drowning"),
    _source("https://en.wikipedia.org/wiki/Kivalina", "Kivalina - Wikipedia"),
]


def test_at_most_three_sites_most_used_first_without_hosts_or_wikipedia():
    used = sites_used([1, 2, 3, 4, 5, 6, 7, 0, 99], KIVALINA)

    # adn.com was cited twice, so it leads; weebly (a host) and Wikipedia are left out
    assert [s["url"] for s in used] == [
        "https://www.adn.com/1",
        "https://www.commerce.alaska.gov/a",
        "https://www.poa.usace.army.mil/x",
    ]
    assert sites_used([], KIVALINA) == []


async def _named(answer, numbers=(1, 2, 3, 5)):
    parse = AsyncMock(side_effect=answer if isinstance(answer, Exception) else None, return_value=answer)
    with patch("pipeline.llm.parse", new=parse):
        return await name_outlets(list(numbers), KIVALINA), parse


async def test_outlets_get_proper_names_from_the_cheap_model():
    answer = OutletNames(
        names=["Anchorage Daily News", "Alaska Dept. of Commerce", "U.S. Army Corps of Engineers"]
    )

    names, parse = await _named(answer)

    # Full stops are removed, so nothing can be read as a web address
    assert names == ["Anchorage Daily News", "Alaska Dept of Commerce", "US Army Corps of Engineers"]
    kwargs = parse.call_args.kwargs
    assert kwargs["model"] == llm.CHEAP_MODEL and kwargs["output_format"] is OutletNames
    assert kwargs["user"].startswith("1. adn.com — Kivalina's long wait - Anchorage Daily News\n2. ")


async def test_unusable_names_fall_back_to_the_address_one_by_one():
    answer = OutletNames(names=["see https://adn.com", "", "x" * 60])
    names, _ = await _named(answer)
    assert names == ["Anchorage Daily News", "Alaska", "Army"]


async def test_a_wrong_count_or_a_failing_model_falls_back_entirely_and_never_raises():
    names, _ = await _named(OutletNames(names=["Only one"]))
    assert names == ["Anchorage Daily News", "Alaska", "Army"]

    names, _ = await _named(llm.LLMRefusalError("declined"))
    assert names == ["Anchorage Daily News", "Alaska", "Army"]


async def test_no_sources_used_means_no_model_call():
    names, parse = await _named(OutletNames(names=[]), numbers=())
    assert names == []
    parse.assert_not_called()
