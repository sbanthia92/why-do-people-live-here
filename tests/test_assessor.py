"""Tests for pipeline.assessor."""

from unittest.mock import AsyncMock, patch

import pytest

from pipeline.assessor import Assessment, assess_candidate


def _assessment(corroborated: bool = True, has_angle: bool = True) -> Assessment:
    return Assessment(
        corroborated_reason="origin backed by [W][2]",
        corroborated=corroborated,
        has_angle_reason="no road in; the mine explains it",
        has_angle=has_angle,
        writer_notes="Lead with the ferry.",
    )


def _db_pool() -> AsyncMock:
    pool = AsyncMock()
    pool.fetchrow.return_value = {"place_name": "Testville", "raw_research": "Brief [W]."}
    return pool


@pytest.mark.parametrize(
    ("corroborated", "has_angle", "passed"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_passes_only_when_both_gates_are_yes(corroborated, has_angle, passed) -> None:
    assert _assessment(corroborated, has_angle).passed is passed


async def test_assess_candidate_persists_and_marks_assessed() -> None:
    pool = _db_pool()
    with patch("pipeline.assessor.llm.parse", new=AsyncMock(return_value=_assessment())):
        result = await assess_candidate(pool, 7)

    assert result.passed
    insert_args, status_args = (c.args for c in pool.execute.call_args_list)
    assert insert_args[1:] == (
        7,
        True,
        "origin backed by [W][2]",
        True,
        "no road in; the mine explains it",
        "Lead with the ferry.",
        True,
    )
    assert status_args[1:] == (7, "assessed")


async def test_assess_candidate_skips_when_a_gate_fails() -> None:
    pool = _db_pool()
    failing = _assessment(corroborated=False)
    with patch("pipeline.assessor.llm.parse", new=AsyncMock(return_value=failing)):
        result = await assess_candidate(pool, 7)

    assert not result.passed
    assert pool.execute.call_args_list[-1].args[1:] == (7, "skipped")
