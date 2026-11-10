"""Tests for pipeline.attribution: naming the outlets a post's facts came from."""

from pipeline.attribution import outlet_name, outlets_used


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


def test_outlets_are_most_used_first_without_repeats_wikipedia_or_unknown_numbers():
    sources = [
        _source("https://www.britannica.com/place/Bhilai", "Bhilai"),
        _source("https://questionofcities.org/a", "A - Question of Cities"),
        _source("https://questionofcities.org/b", "B - Question of Cities"),
        _source("https://en.wikipedia.org/wiki/Bhilai", "Bhilai - Wikipedia"),
    ]

    assert outlets_used([1, 2, 3, 3, 4, 0, 99], sources) == ["Question of Cities", "Britannica"]
    assert outlets_used([], sources) == []
