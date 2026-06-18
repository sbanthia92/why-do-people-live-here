"""Select next candidate from the DB respecting variety constraints (region + story_category rotation)."""

from typing import Optional


async def get_next_candidate(db_pool) -> Optional[dict]:
    """Return the next approved candidate to process, or None if queue is empty.

    Applies variety rotation: avoids repeating the same region or story_category
    as the most recent post recorded in variety_log.
    """
    pass


async def enqueue_from_wikipedia(db_pool, category_slug: str, story_category: str) -> int:
    """Fetch members of a Wikipedia category and insert new candidates into the DB.

    Returns the number of new rows inserted (skips duplicates by wikipedia_url).
    """
    pass


async def apply_layer1_filter(db_pool, candidate_id: int) -> bool:
    """Run a cheap heuristic pass to decide if a candidate is worth researching.

    Sets candidates.layer1_passed and returns the result.
    """
    pass
