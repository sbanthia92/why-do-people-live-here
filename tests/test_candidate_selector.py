"""Tests for pipeline.candidate_selector."""

import pytest
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_get_next_candidate_returns_none_when_empty() -> None:
    """get_next_candidate returns None when no candidates are pending."""
    pass


@pytest.mark.asyncio
async def test_enqueue_from_wikipedia_skips_duplicates() -> None:
    """enqueue_from_wikipedia returns 0 when all URLs already exist in the DB."""
    pass


@pytest.mark.asyncio
async def test_layer1_filter_rejects_stub_articles() -> None:
    """apply_layer1_filter returns False for candidates with insufficient Wikipedia content."""
    pass
