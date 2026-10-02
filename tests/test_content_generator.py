"""Tests for pipeline.content_generator."""

from unittest.mock import AsyncMock, patch

import pytest

from pipeline.content_generator import (
    MAX_TWEET_CHARS,
    SinglePost,
    Thread,
    generate_single_post,
    generate_thread,
    run_content_generation,
)

_ARGS = ("Testville", "Research brief [W].", {"named_origin": "founded 1900"})


def _parse(*results) -> AsyncMock:
    return AsyncMock(side_effect=list(results))


@pytest.mark.asyncio
async def test_generate_single_post_within_char_limit() -> None:
    """generate_single_post output must be ≤280 characters."""
    parse = _parse(SinglePost(text="x" * 300), SinglePost(text="y" * 280))
    with patch("pipeline.content_generator.llm.parse", new=parse):
        text = await generate_single_post(*_ARGS)

    assert len(text) <= MAX_TWEET_CHARS
    assert parse.await_count == 2  # the over-long first draft triggered one rewrite
    assert "300 characters" in parse.call_args_list[1].kwargs["user"]


@pytest.mark.asyncio
async def test_generate_single_post_raises_if_still_too_long() -> None:
    """Two over-long drafts in a row is an error, not a truncated post."""
    parse = _parse(SinglePost(text="x" * 300), SinglePost(text="x" * 290))
    with (
        patch("pipeline.content_generator.llm.parse", new=parse),
        pytest.raises(ValueError, match="290 chars"),
    ):
        await generate_single_post(*_ARGS)


@pytest.mark.asyncio
async def test_generate_thread_has_at_least_two_parts() -> None:
    """generate_thread must return at least 2 tweet strings."""
    tweets = ["Hook about Testville.", "2/ Origin.", "3/ Economy."]
    with patch("pipeline.content_generator.llm.parse", new=_parse(Thread(tweets=tweets))):
        result = await generate_thread(*_ARGS)

    assert result == tweets
    assert len(result) >= 2


@pytest.mark.asyncio
async def test_generate_thread_retries_on_bad_format() -> None:
    """A thread with an over-long part or too few parts is sent back once for fixing."""
    good = ["Hook.", "2/ Origin.", "3/ Economy."]
    parse = _parse(Thread(tweets=["Hook.", "2/ " + "x" * 300, "3/ ok"]), Thread(tweets=good))
    with patch("pipeline.content_generator.llm.parse", new=parse):
        assert await generate_thread(*_ARGS) == good
    assert "posts [2] were over" in parse.call_args_list[1].kwargs["user"]

    parse = _parse(Thread(tweets=["Only one."]), Thread(tweets=["Still one."]))
    with (
        patch("pipeline.content_generator.llm.parse", new=parse),
        pytest.raises(ValueError, match="it had 1 posts"),
    ):
        await generate_thread(*_ARGS)


@pytest.mark.asyncio
async def test_run_content_generation_dispatches_on_format() -> None:
    """run_content_generation calls the correct generator for 'single' vs 'thread' format."""
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "place_name": "Testville",
        "raw_research": "Brief [W].",
        "justifications": {"named_origin": "founded 1900"},
    }
    single = AsyncMock(return_value="One post.")
    thread = AsyncMock(return_value=["Hook.", "2/ More.", "3/ End."])

    with (
        patch("pipeline.content_generator.generate_single_post", new=single),
        patch("pipeline.content_generator.generate_thread", new=thread),
    ):
        assert await run_content_generation(pool, 1, "single") == {
            "post_text": "One post.",
            "thread_parts": None,
        }
        thread.assert_not_awaited()
        assert await run_content_generation(pool, 1, "thread") == {
            "post_text": "Hook.",
            "thread_parts": ["2/ More.", "3/ End."],
        }
        with pytest.raises(ValueError):
            await run_content_generation(pool, 1, "skip")
