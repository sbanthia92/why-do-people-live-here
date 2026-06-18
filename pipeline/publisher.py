"""Publish content to X (Twitter) and record the post in the DB."""

from typing import Optional


async def post_to_x(
    post_text: str,
    thread_parts: Optional[list[str]],
    image_url: Optional[str],
    dry_run: bool = False,
) -> Optional[str]:
    """Post to X and return the post ID.

    If dry_run=True, logs the content but does not call the X API.
    Handles single post and thread (reply-chaining) cases.
    """
    pass


async def upload_image(image_url: str) -> Optional[str]:
    """Download an image from Wikimedia and upload it to X media upload endpoint.

    Returns the X media_id string, or None on failure.
    """
    pass


async def persist_post(db_pool, candidate_id: int, post_data: dict) -> None:
    """Write a row to the posts table, update candidates.status to 'posted',
    and append a row to variety_log."""
    pass


async def run_publish(db_pool, candidate_id: int, content: dict, dry_run: bool = False) -> None:
    """Orchestrate image upload, X posting, and DB persistence for a single candidate."""
    pass
