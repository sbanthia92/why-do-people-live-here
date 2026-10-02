"""Tests for pipeline.content_generator and X post length."""

from unittest.mock import AsyncMock, patch

import pytest

from pipeline.content_generator import MAX_POSTS, Posts, generate_posts, run_content_generation
from pipeline.xtext import MAX_POST_LENGTH, weighted_length

_ARGS = ("Testville", "Research brief [W].", "Lead with the ferry.")


def _parse(*drafts: list[str]) -> AsyncMock:
    return AsyncMock(side_effect=[Posts(posts=d) for d in drafts])


async def test_writer_chooses_single_post_or_thread() -> None:
    """Whatever length the writer returns, within limits, is used as-is."""
    thread = ["Hook about Testville.", "2/ Origin.", "3/ Economy."]
    with patch("pipeline.content_generator.llm.parse", new=_parse(["One post."], thread)):
        assert await generate_posts(*_ARGS) == ["One post."]
        assert await generate_posts(*_ARGS) == thread


async def test_over_long_post_is_sent_back_once() -> None:
    parse = _parse(["x" * 300], ["y" * 280])
    with patch("pipeline.content_generator.llm.parse", new=parse):
        posts = await generate_posts(*_ARGS)

    assert weighted_length(posts[0]) <= MAX_POST_LENGTH
    assert parse.await_count == 2
    assert "posts [1] were over 280 characters" in parse.call_args_list[1].kwargs["user"]


async def test_still_invalid_after_retry_raises() -> None:
    """Two bad drafts in a row is an error, not a truncated post."""
    too_many = ["post"] * (MAX_POSTS + 1)
    with (
        patch("pipeline.content_generator.llm.parse", new=_parse(too_many, too_many)),
        pytest.raises(ValueError, match=f"it had {MAX_POSTS + 1} posts"),
    ):
        await generate_posts(*_ARGS)

    with (
        patch("pipeline.content_generator.llm.parse", new=_parse([], [])),
        pytest.raises(ValueError, match="it had 0 posts"),
    ):
        await generate_posts(*_ARGS)


async def test_run_content_generation_shapes_single_and_thread() -> None:
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "place_name": "Testville",
        "raw_research": "Brief [W].",
        "writer_notes": "Lead with the ferry.",
    }
    generate = AsyncMock(side_effect=[["One post."], ["Hook.", "2/ More.", "3/ End."]])

    with patch("pipeline.content_generator.generate_posts", new=generate):
        assert await run_content_generation(pool, 1) == {
            "post_text": "One post.",
            "thread_parts": None,
        }
        assert await run_content_generation(pool, 1) == {
            "post_text": "Hook.",
            "thread_parts": ["2/ More.", "3/ End."],
        }
    assert generate.call_args.args == ("Testville", "Brief [W].", "Lead with the ferry.")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello", 5),
        ("−13.5 °C in Oymyakon", 21),  # the true minus sign (U+2212) counts double
        ("“quoted” — dash", 15),  # curly quotes and em dash count once each
        ("🙂", 2),  # emoji count double
        ("東京", 4),  # CJK counts double
        ("See https://en.wikipedia.org/wiki/A_very_long_article_title_indeed now", 31),
    ],
)
def test_weighted_length_counts_like_x(text: str, expected: int) -> None:
    assert weighted_length(text) == expected
