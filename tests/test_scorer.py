"""Tests for pipeline.scorer."""

from unittest.mock import AsyncMock, patch

import pytest

from pipeline.scorer import (
    DIMENSIONS,
    SINGLE_MIN_TOTAL,
    THREAD_MIN_TOTAL,
    WEIGHTS,
    Scores,
    compute_weighted_total,
    decide_post_format,
    score_candidate,
)


def _scores(value: int = 2, **overrides: int) -> dict[str, int]:
    return {**dict.fromkeys(DIMENSIONS, value), **overrides}


def test_compute_weighted_total_max_score() -> None:
    """All dimensions at 2 should yield the expected maximum total."""
    assert compute_weighted_total(_scores(2)) == 2 * sum(WEIGHTS.values())


def test_compute_weighted_total_zero() -> None:
    """All dimensions at 0 should yield 0.0."""
    assert compute_weighted_total(_scores(0)) == 0.0


def test_compute_weighted_total_applies_weights() -> None:
    """Each dimension contributes its score times its weight."""
    with patch.dict(WEIGHTS, {"corroboration": 3.0}):
        assert compute_weighted_total(_scores(0, corroboration=2)) == 6.0


def test_decide_post_format_high_score_returns_thread() -> None:
    """A high total_weighted score should map to 'thread'."""
    assert decide_post_format(THREAD_MIN_TOTAL) == "thread"
    assert decide_post_format(10.0) == "thread"


def test_decide_post_format_low_score_returns_skip() -> None:
    """A score below the minimum threshold should map to 'skip'."""
    assert decide_post_format(SINGLE_MIN_TOTAL - 0.5) == "skip"
    assert decide_post_format(0.0) == "skip"


def test_decide_post_format_mid_score_returns_single() -> None:
    """A mid-range score should map to 'single'."""
    assert decide_post_format(SINGLE_MIN_TOTAL) == "single"
    assert decide_post_format(THREAD_MIN_TOTAL - 0.5) == "single"


def _db_pool() -> AsyncMock:
    pool = AsyncMock()
    pool.fetchrow.return_value = {"place_name": "Testville", "raw_research": "Brief [W]."}
    return pool


@pytest.mark.asyncio
async def test_score_candidate_persists_total_and_format() -> None:
    """Scores are totalled, mapped to a format, and written with status 'scored'."""
    pool = _db_pool()
    parsed = Scores(**_scores(2, vivid_detail=1), justifications={"named_origin": "why"})

    with patch("pipeline.scorer.llm.parse", new=AsyncMock(return_value=parsed)):
        result = await score_candidate(pool, 7)

    assert result["total_weighted"] == 9.0
    assert result["post_format"] == "thread"
    insert_args, status_args = (c.args for c in pool.execute.call_args_list)
    assert insert_args[1] == 7 and insert_args[-1] == "thread"
    assert status_args[1:] == (7, "scored")


@pytest.mark.asyncio
async def test_score_candidate_skips_uncorroborated() -> None:
    """Corroboration 0 forces 'skip' even when the total would otherwise qualify."""
    pool = _db_pool()
    parsed = Scores(**_scores(2, corroboration=0), justifications={})

    with patch("pipeline.scorer.llm.parse", new=AsyncMock(return_value=parsed)):
        result = await score_candidate(pool, 7)

    assert result["total_weighted"] == 8.0
    assert result["post_format"] == "skip"
    assert pool.execute.call_args_list[-1].args[1:] == (7, "skipped")
