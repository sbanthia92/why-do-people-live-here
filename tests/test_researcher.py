"""Tests for pipeline.researcher."""

from unittest.mock import AsyncMock, MagicMock, patch

import anthropic

from pipeline.researcher import _SEARCH_ASPECTS, fetch_web_sources


def _source(url: str, snippet: str) -> dict:
    return {"url": url, "snippet": snippet, "source_name": url}


async def test_fetch_web_sources_searches_each_aspect_and_merges():
    """One search per aspect; results are merged with duplicate passages removed."""
    shared = _source("https://example.org/a", "founded in 1900")
    search = AsyncMock(
        side_effect=[
            [shared],
            [shared, _source("https://example.org/b", "a fish plant")],
            [_source("https://example.org/a", "ketchup costs $50")],
        ]
    )
    with patch("pipeline.researcher.llm.web_search_citations", new=search):
        sources = await fetch_web_sources("Testville", "island_community")

    assert search.await_count == len(_SEARCH_ASPECTS) == 3
    prompts = [c.kwargs["user"] for c in search.call_args_list]
    assert all("Place: Testville" in p for p in prompts)
    assert [a for a in _SEARCH_ASPECTS if any(f"— {a}:" in p for p in prompts)] == list(
        _SEARCH_ASPECTS
    )
    # Same URL with a different passage is kept; the exact duplicate is dropped
    assert [(s["url"][-1], s["snippet"]) for s in sources] == [
        ("a", "founded in 1900"),
        ("b", "a fish plant"),
        ("a", "ketchup costs $50"),
    ]


async def test_fetch_web_sources_survives_one_failed_search():
    """A search the API rejects costs only that aspect, not the whole research step."""
    bad_request = anthropic.BadRequestError(
        "web search not enabled", response=MagicMock(status_code=400), body=None
    )
    search = AsyncMock(side_effect=[bad_request, [_source("https://example.org/b", "x")], []])
    with patch("pipeline.researcher.llm.web_search_citations", new=search):
        sources = await fetch_web_sources("Testville", "island_community")

    assert [s["url"] for s in sources] == ["https://example.org/b"]
