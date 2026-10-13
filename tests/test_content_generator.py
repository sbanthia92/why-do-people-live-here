"""Tests for pipeline.content_generator and X post length."""

from unittest.mock import AsyncMock, patch

import pytest

from pipeline.content_generator import (
    MAX_POSTS,
    MAX_REWRITES,
    FactCheck,
    Posts,
    UnsupportedClaim,
    generate_posts,
    run_content_generation,
)
from pipeline.xtext import MAX_POST_LENGTH, weighted_length

_ARGS = ("Testville", "Research brief [W].", "Lead with the ferry.")
_CLEAN = FactCheck(unsupported=[])
_THREAD = ["Hook about Testville.", "2/ Origin.", "3/ Economy."]


def _flagged(post: int = 2, claim: str = "founded in 1850") -> FactCheck:
    return FactCheck(
        unsupported=[UnsupportedClaim(post=post, claim=claim, problem="the brief says 1858")]
    )


def _parse(*replies) -> AsyncMock:
    """llm.parse returning the given replies in order: lists become drafts."""
    return AsyncMock(side_effect=[Posts(posts=r) if isinstance(r, list) else r for r in replies])


def _calls(parse: AsyncMock) -> list[str]:
    return [c.kwargs["output_format"].__name__ for c in parse.call_args_list]


async def test_writer_chooses_single_post_or_thread() -> None:
    """Whatever length the writer returns, within limits, is used once it passes the check."""
    parse = _parse(["One post."], _CLEAN, _THREAD, _CLEAN)
    with patch("pipeline.content_generator.llm.parse", new=parse):
        assert await generate_posts(*_ARGS) == ["One post."]
        assert await generate_posts(*_ARGS) == _THREAD


async def test_every_draft_is_fact_checked_against_the_brief() -> None:
    parse = _parse(_THREAD, _CLEAN)
    with patch("pipeline.content_generator.llm.parse", new=parse):
        await generate_posts(*_ARGS)

    assert _calls(parse) == ["Posts", "FactCheck"]
    check = parse.call_args_list[1].kwargs["user"]
    assert "<research_brief>\nResearch brief [W].\n</research_brief>" in check
    assert "Post 1: Hook about Testville.\n\nPost 2: 2/ Origin." in check


async def test_unsupported_claim_is_sent_back_with_the_reason() -> None:
    fixed = ["Hook about Testville.", "2/ Founded in 1858.", "3/ Economy."]
    parse = _parse(_THREAD, _flagged(), fixed, _CLEAN)
    with patch("pipeline.content_generator.llm.parse", new=parse):
        assert await generate_posts(*_ARGS) == fixed

    assert _calls(parse) == ["Posts", "FactCheck", "Posts", "FactCheck"]
    rewrite = parse.call_args_list[2].kwargs["user"]
    assert '- Post 2: "founded in 1850" — the brief says 1858' in rewrite
    assert "<previous_draft>\nHook about Testville." in rewrite


async def test_draft_that_keeps_failing_the_fact_check_is_not_posted() -> None:
    replies = [r for _ in range(MAX_REWRITES + 1) for r in (_THREAD, _flagged())]
    parse = _parse(*replies)
    with (
        patch("pipeline.content_generator.llm.parse", new=parse),
        pytest.raises(ValueError, match="still failing after 2 rewrites.*founded in 1850"),
    ):
        await generate_posts(*_ARGS)
    assert parse.await_count == 2 * (MAX_REWRITES + 1)


async def test_over_long_post_is_sent_back_without_a_fact_check() -> None:
    """Format is checked first, in code: no point fact-checking a draft that can't be posted."""
    parse = _parse(["x" * 300], ["y" * 280], _CLEAN)
    with patch("pipeline.content_generator.llm.parse", new=parse):
        posts = await generate_posts(*_ARGS)

    assert weighted_length(posts[0]) <= MAX_POST_LENGTH
    assert _calls(parse) == ["Posts", "Posts", "FactCheck"]
    assert "posts [1] were over 280 characters" in parse.call_args_list[1].kwargs["user"]


async def test_too_many_posts_every_time_raises() -> None:
    too_many = ["post"] * (MAX_POSTS + 1)
    parse = _parse(*[too_many] * (MAX_REWRITES + 1))
    with (
        patch("pipeline.content_generator.llm.parse", new=parse),
        pytest.raises(ValueError, match=f"it had {MAX_POSTS + 1} posts"),
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
