"""Generate post copy (caption + optional thread parts) from research and scores."""

from typing import Optional


async def generate_single_post(
    place_name: str,
    raw_research: str,
    justifications: dict,
) -> str:
    """Generate a single-tweet caption (≤280 chars) grounded in the research.

    Returns the tweet text.
    """
    pass


async def generate_thread(
    place_name: str,
    raw_research: str,
    justifications: dict,
) -> list[str]:
    """Generate a multi-part thread. First part is the hook tweet; rest are numbered follow-ups.

    Returns an ordered list of tweet strings.
    """
    pass


async def run_content_generation(
    db_pool,
    candidate_id: int,
    post_format: str,
) -> dict:
    """Dispatch to single or thread generator based on post_format, return content dict."""
    pass
