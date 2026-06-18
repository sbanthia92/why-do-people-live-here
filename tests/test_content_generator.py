"""Tests for pipeline.content_generator."""

import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_generate_single_post_within_char_limit() -> None:
    """generate_single_post output must be ≤280 characters."""
    pass


@pytest.mark.asyncio
async def test_generate_thread_has_at_least_two_parts() -> None:
    """generate_thread must return at least 2 tweet strings."""
    pass


@pytest.mark.asyncio
async def test_run_content_generation_dispatches_on_format() -> None:
    """run_content_generation calls the correct generator for 'single' vs 'thread' format."""
    pass
