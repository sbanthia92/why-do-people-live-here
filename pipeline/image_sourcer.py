"""Source a suitable Wikimedia Commons image for the candidate place."""

from typing import Optional


async def search_wikimedia(place_name: str, wikipedia_url: str) -> Optional[dict]:
    """Query Wikimedia Commons for an image related to the place.

    Returns {url, credit, license} or None if no suitable image found.
    Uses WIKIMEDIA_USER_AGENT from env for the API request header.
    """
    pass


async def validate_image(image_url: str) -> bool:
    """Check that an image URL is reachable and within acceptable file-size limits."""
    pass


async def get_image_for_candidate(place_name: str, wikipedia_url: str) -> Optional[dict]:
    """Top-level helper: search, validate, and return image metadata or None."""
    pass
