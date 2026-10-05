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


async def test_fetch_web_sources_does_not_hide_an_empty_credit_balance():
    """That 400 isn't "search unavailable": it has to stop the run, not thin the research."""
    import pytest

    out_of_credits = anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.",
        response=MagicMock(status_code=400),
        body=None,
    )
    search = AsyncMock(side_effect=out_of_credits)
    with (
        patch("pipeline.researcher.llm.web_search_citations", new=search),
        pytest.raises(anthropic.BadRequestError, match="credit balance"),
    ):
        await fetch_web_sources("Testville", "island_community")


def _response(model: str, tokens_in: int, tokens_out: int, citations=()) -> MagicMock:
    usage = MagicMock(
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    text = MagicMock(type="text", citations=list(citations))
    return MagicMock(model=model, usage=usage, stop_reason="end_turn", content=[text])


async def test_web_search_runs_on_the_search_model_and_blocks_wikipedia_mirrors(monkeypatch):
    from pipeline import llm

    citation = MagicMock(url="https://example.org/a", cited_text="founded in 1900", title="Example")
    create = AsyncMock(return_value=_response(llm.SEARCH_MODEL, 30_000, 2_000, [citation]))
    client = MagicMock()
    client.beta.messages.create = create
    monkeypatch.setattr(llm, "get_client", lambda: client)
    monkeypatch.setattr(llm, "_usage", {})

    sources = await llm.web_search_citations(system="s", user="u")

    assert sources == [
        {"url": "https://example.org/a", "snippet": "founded in 1900", "source_name": "Example"}
    ]
    request = create.call_args.kwargs
    assert request["model"] == llm.SEARCH_MODEL != llm.MODEL
    blocked = request["tools"][0]["blocked_domains"]
    assert {"wikipedia.org", "kiddle.co", "grokipedia.com"} <= set(blocked)


def test_usage_summary_totals_by_model(monkeypatch):
    from pipeline import llm

    monkeypatch.setattr(llm, "_usage", {})
    assert llm.usage_summary() == "Claude usage: no requests made."

    llm._record_usage(_response(llm.SEARCH_MODEL, 1_000_000, 100_000))
    llm._record_usage(_response(llm.MODEL, 500_000, 0))
    llm._record_usage(_response(llm.MODEL, 500_000, 50_000))
    llm._record_usage(_response("some-fallback-model", 10, 10))

    summary = llm.usage_summary()
    # Sonnet: $2 in + $1 out. Opus: $4 in + $1 out. The unpriced model adds nothing.
    assert summary.startswith("Claude usage (estimated $8.00, before web search fees):")
    assert f"{llm.SEARCH_MODEL}: 1 requests, 1,000,000 tokens in, 100,000 out, about $3.00" in summary
    assert f"{llm.MODEL}: 2 requests, 1,000,000 tokens in, 50,000 out, about $5.00" in summary
    assert "some-fallback-model: 1 requests, 10 tokens in, 10 out" in summary
