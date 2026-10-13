"""Publish content to X (Twitter) and record the post in the DB."""

import asyncio
import io
import os
import tempfile
import urllib.parse
from pathlib import Path

import httpx
import tweepy
from rich.console import Console
from rich.panel import Panel

from pipeline.candidate_selector import _HEADERS
from pipeline.image_sourcer import get_image_for_candidate
from pipeline.map_card import map_card_for_candidate
from pipeline.preview import write_preview
from pipeline.xtext import weighted_length

console = Console()


def _images_enabled() -> bool:
    """Photos are off unless POST_IMAGES=true.

    Reusing Commons photos on X carries licensing questions (see README), so posts are
    text-only by default.
    """
    return os.getenv("POST_IMAGES", "false").strip().lower() in {"1", "true", "yes"}


def _source_link_enabled() -> bool:
    """Whether the closing reply links to the Wikipedia article (SOURCE_LINK=true).

    Off by default: X charges many times more for a post containing a link than for
    a plain one, so the source is named in text instead.
    """
    return os.getenv("SOURCE_LINK", "false").strip().lower() in {"1", "true", "yes"}


def _map_cards_enabled() -> bool:
    """Whether the first post carries a locator map we draw ourselves (on unless MAP_CARDS=false)."""
    return os.getenv("MAP_CARDS", "true").strip().lower() in {"1", "true", "yes"}


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
    alt_text: str | None = None,
) -> str | None:
    """Post to X and return the post ID.

    If dry_run=True, logs the content but does not call the X API.
    Handles single post and thread (reply-chaining) cases. The image, if any, is
    attached to the first post.

    If the first post fails, the error propagates and nothing was published. If a
    later reply fails, the thread is left as far as it got and the first post's ID is
    still returned: the hook is already public, and retrying the whole thread on the
    next run would post it twice.
    """
    parts = [post_text, *(thread_parts or [])]
    if dry_run:
        for i, part in enumerate(parts, start=1):
            title = f"DRY RUN — post {i}/{len(parts)} ({weighted_length(part)} chars)"
            if i == 1 and image_url:
                title += " + image"
            console.print(Panel(part, title=title, title_align="left"))
        if image_url:
            console.print(f"Image: {image_url}")
        return None

    media_id = await upload_image(image_url, alt_text) if image_url else None
    client = _x_client()
    first_id: str | None = None
    reply_to: str | None = None
    for i, part in enumerate(parts):
        try:
            response = await asyncio.to_thread(
                client.create_tweet,
                text=part,
                media_ids=[media_id] if i == 0 and media_id else None,
                in_reply_to_tweet_id=reply_to,
            )
        except tweepy.TweepyException as exc:
            if first_id is None:
                raise
            console.log(
                f"[publisher] Thread stopped at post {i + 1} of {len(parts)}: {exc!r}."
                f" The first {i} are published."
            )
            break
        reply_to = str(response.data["id"])
        first_id = first_id or reply_to
    return first_id


async def upload_image(source: str, alt_text: str | None = None) -> str | None:
    """Upload an image to X, from a local file or a URL, and describe it for screen readers.

    Returns the X media_id string, or None on failure.
    """
    try:
        if source.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
                resp = await client.get(source, follow_redirects=True)
                resp.raise_for_status()
            data = resp.content
            # Path only: Wikimedia thumbnail URLs carry a query string after the extension
            filename = urllib.parse.urlparse(source).path.rsplit("/", 1)[-1] or "image.jpg"
        else:
            data = Path(source).read_bytes()
            filename = Path(source).name
        api = _x_media_api()
        media = await asyncio.to_thread(api.media_upload, filename=filename, file=io.BytesIO(data))
    except (httpx.HTTPError, OSError, tweepy.TweepyException) as exc:
        console.log(f"[publisher] Image upload failed, posting without it: {exc!r}")
        return None

    if alt_text:
        try:
            await asyncio.to_thread(api.create_media_metadata, media.media_id, alt_text[:1000])
        except tweepy.TweepyException as exc:
            # The image is still usable without its description
            console.log(f"[publisher] Could not set image description: {exc!r}")
    return str(media.media_id)


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


# Closes every thread. It rides on the source reply instead of being a post of its own.
FOLLOW_LINE = "A new remote, harsh or strange place every day. Follow for more."


def _footer(wikipedia_url: str, photo: dict | None) -> str:
    """Closing reply: where the facts (and any photo) came from, and an invitation to follow.

    A photo credit carries what a Creative Commons licence asks for: the author,
    the licence and a link to it, and a link to the source file.
    """
    if _source_link_enabled():
        lines = [f"Source: {wikipedia_url}"]
    else:
        title = urllib.parse.unquote(wikipedia_url.rstrip("/").split("/wiki/")[-1])
        lines = [f"Source: Wikipedia, “{title.replace('_', ' ')}”"]
    if photo:
        license_part = " ".join(filter(None, [photo["license"], photo.get("license_url")]))
        lines.append(f"Photo: {photo['credit']}, {license_part}, via Wikimedia Commons")
        if photo.get("page_url"):
            lines.append(photo["page_url"])
    lines += ["", FOLLOW_LINE]
    return "\n".join(lines)


async def run_publish(db_pool, candidate_id: int, content: dict, dry_run: bool = False) -> None:
    """Orchestrate the image, X posting, and DB persistence for a single candidate."""
    candidate = await db_pool.fetchrow(
        "SELECT place_name, wikipedia_url, story_category, region FROM candidates WHERE id = $1",
        candidate_id,
    )
    name, url = candidate["place_name"], candidate["wikipedia_url"]

    # One image on the first post: the map card we draw, or failing that a licensed photo
    card = photo = None
    if _map_cards_enabled():
        context = " · ".join(
            filter(None, [(candidate["story_category"] or "").replace("_", " "), candidate["region"]])
        )
        directory = Path(os.getenv("PREVIEW_DIR") or tempfile.gettempdir())
        card = await map_card_for_candidate(name, url, context, directory)
    if card is None and _images_enabled():
        photo = await get_image_for_candidate(name, url)
    image_source = card["path"] if card else photo["url"] if photo else None
    alt_text = card["alt_text"] if card else None

    replies = [*(content["thread_parts"] or []), _footer(url, photo)]

    x_post_id = await post_to_x(
        content["post_text"], replies, image_source, dry_run=dry_run, alt_text=alt_text
    )
    # Leave a Markdown copy of what was previewed or posted (the daily workflow turns
    # these into notifications).
    if preview_dir := os.getenv("PREVIEW_DIR"):
        file = await write_preview(
            db_pool,
            candidate_id,
            [content["post_text"], *replies],
            preview_dir,
            x_post_id=x_post_id,
            image_note=alt_text,
        )
        console.log(f"[publisher] {'Preview' if dry_run else 'Record'} written to {file}")

    await persist_post(
        db_pool,
        candidate_id,
        {
            "post_text": content["post_text"],
            "thread_parts": content["thread_parts"],
            "image_url": Path(card["path"]).name if card else photo["url"] if photo else None,
            "image_credit": f"{photo['credit']} ({photo['license']})" if photo else None,
            "x_post_id": x_post_id,
            "dry_run": dry_run,
        },
    )
