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
_THREAD = ["Hook about Testville.", "Origin and economy."]


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
    assert "Post 1: Hook about Testville.\n\nPost 2: Origin and economy." in check


async def test_unsupported_claim_is_sent_back_with_the_reason() -> None:
    fixed = ["Hook about Testville.", "Founded in 1858."]
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


async def test_run_content_generation_shapes_single_and_thread(monkeypatch) -> None:
    monkeypatch.setenv("FACTS_CARD", "true")
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "place_name": "Testville",
        "raw_research": "Brief [W].",
        "writer_notes": "Lead with the ferry.",
        "web_sources": [
            {"url": "https://www.britannica.com/place/Testville", "source_name": "Testville"},
            {"url": "https://example.org/a", "source_name": "A page - Example"},
        ],
    }
    generate = AsyncMock(
        side_effect=[
            Posts(posts=["One post."]),
            # 2 is used twice over; 9 matches no source and is ignored
            Posts(posts=["Hook.", "More."], sources=[2, 1, 9]),
        ]
    )

    facts = [{"figure": "96", "caption": "villages"}] * 3
    select = AsyncMock(side_effect=[None, facts])
    name = AsyncMock(side_effect=[[], ["Example News", "Britannica"]])
    with (
        patch("pipeline.content_generator.write", new=generate),
        patch("pipeline.content_generator.name_outlets", new=name),
        patch("pipeline.facts_card.select_facts", new=select),
    ):
        assert await run_content_generation(pool, 1) == {
            "post_text": "One post.",
            "thread_parts": None,
            "sources": [],
            "facts": None,
        }
        assert await run_content_generation(pool, 1) == {
            "post_text": "Hook.",
            "thread_parts": ["More."],
            "sources": ["Example News", "Britannica"],
            "facts": facts,
        }
    assert select.call_args.args == ("Testville", "Brief [W].")
    assert name.call_args.args[0] == [2, 1, 9]  # the numbers the writer said it used
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


def test_the_prompts_ask_for_scope_and_the_check_rejects_widened_claims() -> None:
    from pipeline.content_generator import _FACT_CHECK_SYSTEM, _SYSTEM

    assert "area or year" in _SYSTEM and "web sources whose" in _SYSTEM.replace("\n", " ")
    assert "widens the scope" in _FACT_CHECK_SYSTEM
    assert "stripped of the area or year" in _FACT_CHECK_SYSTEM.replace("\n", " ")


async def test_a_failing_facts_step_costs_the_card_not_the_post(monkeypatch) -> None:
    from pipeline.content_generator import _card_facts
    from pipeline.llm import LLMRefusalError

    with patch("pipeline.facts_card.select_facts", new=AsyncMock(side_effect=LLMRefusalError("no"))):
        assert await _card_facts("Testville", "Brief.") is None

    monkeypatch.setenv("FACTS_CARD", "false")
    select = AsyncMock()
    with patch("pipeline.facts_card.select_facts", new=select):
        assert await _card_facts("Testville", "Brief.") is None
    select.assert_not_called()  # switched off: no model call, no cost


_BRIEF = "The 2011 census counted 280 people [W]. Milk cost $12.69 a gallon in 2019 [3]. It is 1,412 km away [W]."


def _first(*lines):
    return ["\n".join(["An island with no harbour.", *lines, "Why do people live here?"]), "The answer."]


def test_numbered_figures_in_the_first_post_must_be_in_the_brief_digit_for_digit():
    from pipeline.content_generator import _format_problem

    good = _first("1. 280 people (2011 census)", "2. $12.69 for a gallon of milk (2019)", "3. 1,412 km away")
    assert _format_problem(good, _BRIEF) is None

    # "80" is only inside "280", and 1,400 is a rounding the brief does not give
    assert '"80% of homes lack water"' in _format_problem(_first("1. 80% of homes lack water"), _BRIEF)
    assert '"1,400 km away"' in _format_problem(_first("1. 1,400 km away"), _BRIEF)
    # A numbered line has to carry a figure
    assert "has no figure" in _format_problem(_first("1. Very far from anywhere"), _BRIEF)


def test_at_most_three_numbered_lines_and_none_is_fine():
    from pipeline.content_generator import _format_problem

    four = _first("1. 280 people", "2. $12.69 milk", "3. 1,412 km away", "4. 2011 census")
    assert "4 numbered lines" in _format_problem(four, _BRIEF)
    assert _format_problem(["A hook with no figures. Why do people live here?", "The answer."], _BRIEF) is None


async def test_a_figure_not_in_the_brief_is_sent_back_before_the_fact_check():
    bad = _first("1. 1,400 km away")
    good = _first("1. 1,412 km away")
    parse = _parse(bad, good, FactCheck(unsupported=[]))
    with patch("pipeline.llm.parse", new=parse):
        assert await generate_posts("Testville", _BRIEF, None) == good

    # Draft, rewrite, then one fact check: the bad draft never reached the fact checker
    assert parse.await_count == 3
    assert "Copy it exactly from the brief" in parse.await_args_list[1].kwargs["user"]
