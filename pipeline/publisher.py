"""Publish content to X (Twitter) and record the post in the DB."""

import asyncio
import io
import os
import urllib.parse

import httpx
import tweepy
from rich.console import Console
from rich.panel import Panel

from pipeline.candidate_selector import _HEADERS
from pipeline.image_sourcer import get_image_for_candidate
from pipeline.preview import write_preview

console = Console()


def _images_enabled() -> bool:
    """Photos are off unless POST_IMAGES=true.

    Reusing Commons photos on X carries licensing questions (see README), so posts are
    text-only by default.
    """
    return os.getenv("POST_IMAGES", "false").strip().lower() in {"1", "true", "yes"}


def _x_client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )


def _x_media_api() -> tweepy.API:
    # Media upload is only available on the v1.1 API
    auth = tweepy.OAuth1UserHandler(
        os.environ["X_API_KEY"],
        os.environ["X_API_SECRET"],
        os.environ["X_ACCESS_TOKEN"],
        os.environ["X_ACCESS_SECRET"],
    )
    return tweepy.API(auth)


async def post_to_x(
    post_text: str,
    thread_parts: list[str] | None,
    image_url: str | None,
    dry_run: bool = False,
) -> str | None:
    """Post to X and return the post ID.

    If dry_run=True, logs the content but does not call the X API.
    Handles single post and thread (reply-chaining) cases. The image, if any, is
    attached to the first post.
    """
    parts = [post_text, *(thread_parts or [])]
    if dry_run:
        for i, part in enumerate(parts, start=1):
            title = f"DRY RUN — post {i}/{len(parts)} ({len(part)} chars)"
            if i == 1 and image_url:
                title += " + image"
            console.print(Panel(part, title=title, title_align="left"))
        if image_url:
            console.print(f"Image: {image_url}")
        return None

    media_id = await upload_image(image_url) if image_url else None
    client = _x_client()
    first_id: str | None = None
    reply_to: str | None = None
    for i, part in enumerate(parts):
        response = await asyncio.to_thread(
            client.create_tweet,
            text=part,
            media_ids=[media_id] if i == 0 and media_id else None,
            in_reply_to_tweet_id=reply_to,
        )
        reply_to = str(response.data["id"])
        first_id = first_id or reply_to
    return first_id


async def upload_image(image_url: str) -> str | None:
    """Download an image from Wikimedia and upload it to X media upload endpoint.

    Returns the X media_id string, or None on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
            resp = await client.get(image_url, follow_redirects=True)
            resp.raise_for_status()
        # Path only: Wikimedia thumbnail URLs carry a query string after the extension
        filename = urllib.parse.urlparse(image_url).path.rsplit("/", 1)[-1] or "image.jpg"
        media = await asyncio.to_thread(
            _x_media_api().media_upload, filename=filename, file=io.BytesIO(resp.content)
        )
        return str(media.media_id)
    except (httpx.HTTPError, tweepy.TweepyException) as exc:
        console.log(f"[publisher] Image upload failed, posting without it: {exc!r}")
        return None


async def persist_post(db_pool, candidate_id: int, post_data: dict) -> None:
    """Write a row to the posts table and update the candidate's status.

    A real post sets status 'posted' and appends a row to variety_log. A dry run sets
    status 'approved' instead — content is ready but nothing went out — and leaves
    variety_log alone, so a later real run can still publish the candidate.
    """
    dry_run = post_data["dry_run"]
    await db_pool.execute(
        """
        INSERT INTO posts
            (candidate_id, post_text, thread_parts, image_url, image_credit, x_post_id, dry_run)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        candidate_id,
        post_data["post_text"],
        post_data["thread_parts"],
        post_data.get("image_url"),
        post_data.get("image_credit"),
        post_data.get("x_post_id"),
        dry_run,
    )
    await db_pool.execute(
        "UPDATE candidates SET status = $2, updated_at = now() WHERE id = $1",
        candidate_id,
        "approved" if dry_run else "posted",
    )
    if not dry_run:
        await db_pool.execute(
            """
            INSERT INTO variety_log (region, story_category)
            SELECT COALESCE(region, 'Unknown'), story_category FROM candidates WHERE id = $1
            """,
            candidate_id,
        )


def _footer(wikipedia_url: str, image: dict | None) -> str:
    """Closing reply: where the facts and the photo came from.

    The photo credit carries what a Creative Commons licence asks for: the author,
    the licence and a link to it, and a link to the source file.
    """
    lines = [f"Source: {wikipedia_url}"]
    if image:
        license_part = " ".join(filter(None, [image["license"], image.get("license_url")]))
        lines.append(f"Photo: {image['credit']}, {license_part}, via Wikimedia Commons")
        if image.get("page_url"):
            lines.append(image["page_url"])
    return "\n".join(lines)


async def run_publish(db_pool, candidate_id: int, content: dict, dry_run: bool = False) -> None:
    """Orchestrate image upload, X posting, and DB persistence for a single candidate."""
    candidate = await db_pool.fetchrow(
        "SELECT place_name, wikipedia_url FROM candidates WHERE id = $1", candidate_id
    )
    image = (
        await get_image_for_candidate(candidate["place_name"], candidate["wikipedia_url"])
        if _images_enabled()
        else None
    )
    replies = [*(content["thread_parts"] or []), _footer(candidate["wikipedia_url"], image)]

    x_post_id = await post_to_x(
        content["post_text"], replies, image["url"] if image else None, dry_run=dry_run
    )
    # A dry run can also leave a Markdown copy to review (the daily workflow turns
    # these into notifications).
    if dry_run and (preview_dir := os.getenv("PREVIEW_DIR")):
        file = await write_preview(
            db_pool, candidate_id, [content["post_text"], *replies], preview_dir
        )
        console.log(f"[publisher] Preview written to {file}")

    await persist_post(
        db_pool,
        candidate_id,
        {
            "post_text": content["post_text"],
            "thread_parts": content["thread_parts"],
            "image_url": image["url"] if image else None,
            "image_credit": f"{image['credit']} ({image['license']})" if image else None,
            "x_post_id": x_post_id,
            "dry_run": dry_run,
        },
    )
