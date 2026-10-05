"""Main entry point. Orchestrates one full pipeline run: select → research → assess → generate → publish."""

import argparse
import asyncio
import os

from dotenv import load_dotenv
from rich.console import Console

from db.client import create_pool
from pipeline.assessor import assess_candidate
from pipeline.candidate_selector import run_selector
from pipeline.content_generator import run_content_generation
from pipeline.llm import LLMRefusalError, usage_summary
from pipeline.publisher import run_publish
from pipeline.researcher import run_research

console = Console()

# Candidates prepared (researched and assessed) per post before giving up
MAX_ATTEMPTS = 5
# Statuses a previous run may have left a candidate in, before it was assessed
_UNPREPARED = ["pending", "researched"]


def _is_dry_run() -> bool:
    return os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes"}


async def _last_post(db_pool, dry_run: bool) -> tuple[str | None, str | None]:
    """(region, story_category) of the most recent post, for the variety rule.

    Real runs ignore dry-run posts; dry runs count them, so a batch of previews
    shows the same variety a sequence of real posts would.
    """
    row = await db_pool.fetchrow(
        """
        SELECT c.region, c.story_category
        FROM posts p
        JOIN candidates c ON c.id = p.candidate_id
        WHERE $1 OR NOT p.dry_run
        ORDER BY p.posted_at DESC, p.id DESC
        LIMIT 1
        """,
        dry_run,
    )
    return (row["region"], row["story_category"]) if row else (None, None)


async def _next_unprepared(db_pool, tried: list[int], avoid_categories: list[str]) -> dict | None:
    """Pick up a candidate an earlier run left unassessed, otherwise select a new one.

    Candidates whose story type is not in `avoid_categories` are preferred.
    """
    row = await db_pool.fetchrow(
        """
        SELECT id, place_name, story_category, status
        FROM candidates
        WHERE layer1_passed AND status = ANY($1::text[]) AND id <> ALL($2::int[])
        ORDER BY (story_category = ANY($3::text[])), created_at
        LIMIT 1
        """,
        _UNPREPARED,
        tried,
        avoid_categories,
    )
    if row and row["story_category"] not in avoid_categories:
        return dict(row)
    # Only avoided story types are left unfinished: a new one from elsewhere is better
    return await run_selector(db_pool, avoid_categories=avoid_categories) or (
        dict(row) if row else None
    )


async def _skip(db_pool, candidate: dict, reason: object) -> None:
    """Mark a candidate that can't produce usable content, so later runs don't retry it."""
    console.log(f"[pipeline] Skipping {candidate['place_name']}: {reason}")
    await db_pool.execute(
        "UPDATE candidates SET status = 'skipped', updated_at = now() WHERE id = $1",
        candidate["id"],
    )


async def _prepare(db_pool, candidate: dict) -> None:
    """Research and assess one candidate, leaving it 'assessed' or 'skipped'.

    Safe to run for several candidates concurrently: each touches only its own rows.
    """
    try:
        if candidate["status"] == "pending":
            await run_research(db_pool, candidate["id"])
        status = await db_pool.fetchval(
            "SELECT status FROM candidates WHERE id = $1", candidate["id"]
        )
        if status == "researched":
            await assess_candidate(db_pool, candidate["id"])
    except (LLMRefusalError, ValueError) as exc:
        await _skip(db_pool, candidate, exc)


async def _pick_ready(
    db_pool, dry_run: bool, avoid_region: str | None, last_category: str | None
) -> dict | None:
    """Choose an assessed candidate to post, applying the variety rule.

    Candidates in `avoid_region` are passed over (they stay 'assessed' for a later run),
    and a different story type than the last post is preferred. A real run also
    considers 'approved' candidates: previewed by a dry run but never posted.
    """
    row = await db_pool.fetchrow(
        """
        SELECT c.id, c.place_name, c.story_category, c.status
        FROM candidates c
        WHERE c.layer1_passed AND c.status = ANY($1::text[])
          AND ($2::text IS NULL OR c.region IS DISTINCT FROM $2)
        ORDER BY (c.story_category IS NOT DISTINCT FROM $3), c.created_at
        LIMIT 1
        """,
        ["assessed"] if dry_run else ["assessed", "approved"],
        avoid_region,
        last_category,
    )
    return dict(row) if row else None


async def _publish(db_pool, candidate: dict, dry_run: bool) -> bool:
    """Write and publish one ready candidate. Returns False if its content was unusable.

    An 'approved' candidate was previewed by a dry run: the text that was previewed is
    the text that gets posted, not a fresh draft nobody has seen.
    """
    content = await _previewed_content(db_pool, candidate) if not dry_run else None
    if content is None:
        try:
            content = await run_content_generation(db_pool, candidate["id"])
        except (LLMRefusalError, ValueError) as exc:
            await _skip(db_pool, candidate, exc)
            return False
    await run_publish(db_pool, candidate["id"], content, dry_run=dry_run)
    posts = 1 + len(content["thread_parts"] or [])
    console.log(
        f"[pipeline] {'Previewed' if dry_run else 'Posted'}: {candidate['place_name']}"
        f" ({'single post' if posts == 1 else f'thread of {posts}'})"
    )
    return True


async def _previewed_content(db_pool, candidate: dict) -> dict | None:
    """The content a dry run stored for an 'approved' candidate, if any."""
    if candidate["status"] != "approved":
        return None
    row = await db_pool.fetchrow(
        """
        SELECT post_text, thread_parts FROM posts
        WHERE candidate_id = $1 AND dry_run
        ORDER BY posted_at DESC, id DESC
        LIMIT 1
        """,
        candidate["id"],
    )
    return dict(row) if row else None


async def _post_one(db_pool, dry_run: bool, parallel: int) -> bool:
    """Produce one post. Returns True on success.

    Posts a ready candidate if one satisfies the variety rule. Otherwise prepares up to
    `parallel` more candidates at once and looks again, until MAX_ATTEMPTS candidates
    have been prepared. Candidates prepared but not used stay ready for the next post.
    """
    last_region, last_category = await _last_post(db_pool, dry_run)
    tried: list[int] = []

    while True:
        while ready := await _pick_ready(db_pool, dry_run, last_region, last_category):
            if await _publish(db_pool, ready, dry_run):
                return True

        batch: list[dict] = []
        for _ in range(min(parallel, MAX_ATTEMPTS - len(tried))):
            # Spread a batch across story types, so the posts that follow can alternate.
            # The last post's type goes at the end of the list: it is avoided hardest.
            avoid = [c["story_category"] for c in batch]
            if last_category:
                avoid.append(last_category)
            candidate = await _next_unprepared(db_pool, tried, avoid)
            if candidate is None:
                break
            tried.append(candidate["id"])
            batch.append(candidate)
        if not batch:
            break
        await asyncio.gather(*(_prepare(db_pool, c) for c in batch))

    # Nothing new to prepare: the region rule yields, since a repeat region beats no post
    while ready := await _pick_ready(db_pool, dry_run, None, last_category):
        console.log(f"[pipeline] Only same-region candidates left; using {ready['place_name']}")
        if await _publish(db_pool, ready, dry_run):
            return True

    console.log(f"[pipeline] No post ({len(tried)} candidates prepared).")
    return False


async def run_once(posts: int = 1, parallel: int = 1) -> int:
    """Execute a pipeline run: pick a candidate, research it, assess it, generate content, publish.

    Reads DRY_RUN and DATABASE_URL from env. Creates and tears down the asyncpg pool
    per run. `posts` > 1 repeats the cycle, which is useful for previewing several
    posts in a dry run; `parallel` is how many candidates are researched and assessed at
    the same time. Returns the number of posts produced.
    """
    load_dotenv()
    dry_run = _is_dry_run()
    db_pool = await create_pool(os.environ["DATABASE_URL"])
    produced = 0
    try:
        for _ in range(posts):
            if not await _post_one(db_pool, dry_run, max(1, parallel)):
                break
            produced += 1
    finally:
        await db_pool.close()
        console.print(usage_summary())
    return produced


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--posts", type=int, default=1, help="number of posts to produce")
    parser.add_argument(
        "--parallel", type=int, default=1, help="candidates to research and assess at once"
    )
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run_once(args.posts, args.parallel)) else 1)
