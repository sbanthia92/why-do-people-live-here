"""Main entry point. Orchestrates one full pipeline run: select → research → score → generate → publish."""

import argparse
import asyncio
import os

from dotenv import load_dotenv
from rich.console import Console

from db.client import create_pool
from pipeline.candidate_selector import run_selector
from pipeline.content_generator import run_content_generation
from pipeline.llm import LLMRefusalError
from pipeline.publisher import run_publish
from pipeline.researcher import run_research
from pipeline.scorer import score_candidate

console = Console()

# Candidates prepared (researched and scored) per post before giving up
MAX_ATTEMPTS = 5
# Statuses a previous run may have left a candidate in, before it was scored
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


async def _next_unprepared(db_pool, tried: list[int], last_category: str | None) -> dict | None:
    """Pick up a candidate an earlier run left unscored, otherwise select a new one.

    Candidates of a different story type than the last post are preferred.
    """
    row = await db_pool.fetchrow(
        """
        SELECT id, place_name, story_category, status
        FROM candidates
        WHERE layer1_passed AND status = ANY($1::text[]) AND id <> ALL($2::int[])
        ORDER BY (story_category IS NOT DISTINCT FROM $3), created_at
        LIMIT 1
        """,
        _UNPREPARED,
        tried,
        last_category,
    )
    if row:
        return dict(row)
    return await run_selector(db_pool, avoid_category=last_category)


async def _skip(db_pool, candidate: dict, reason: object) -> None:
    """Mark a candidate that can't produce usable content, so later runs don't retry it."""
    console.log(f"[pipeline] Skipping {candidate['place_name']}: {reason}")
    await db_pool.execute(
        "UPDATE candidates SET status = 'skipped', updated_at = now() WHERE id = $1",
        candidate["id"],
    )


async def _prepare(db_pool, candidate: dict) -> None:
    """Research and score one candidate, leaving it 'scored' or 'skipped'.

    Safe to run for several candidates concurrently: each touches only its own rows.
    """
    try:
        if candidate["status"] == "pending":
            await run_research(db_pool, candidate["id"])
        status = await db_pool.fetchval(
            "SELECT status FROM candidates WHERE id = $1", candidate["id"]
        )
        if status == "researched":
            await score_candidate(db_pool, candidate["id"])
    except (LLMRefusalError, ValueError) as exc:
        await _skip(db_pool, candidate, exc)


async def _pick_ready(
    db_pool, dry_run: bool, avoid_region: str | None, last_category: str | None
) -> dict | None:
    """Choose a scored candidate to post, applying the variety rule.

    Candidates in `avoid_region` are passed over (they stay 'scored' for a later run),
    and a different story type than the last post is preferred. A real run also
    considers 'approved' candidates: previewed by a dry run but never posted.
    """
    row = await db_pool.fetchrow(
        """
        SELECT c.id, c.place_name, c.story_category,
               (SELECT post_format FROM scores WHERE candidate_id = c.id
                 ORDER BY scored_at DESC LIMIT 1) AS post_format
        FROM candidates c
        WHERE c.layer1_passed AND c.status = ANY($1::text[])
          AND ($2::text IS NULL OR c.region IS DISTINCT FROM $2)
        ORDER BY (c.story_category IS NOT DISTINCT FROM $3), c.created_at
        LIMIT 1
        """,
        ["scored"] if dry_run else ["scored", "approved"],
        avoid_region,
        last_category,
    )
    return dict(row) if row else None


async def _publish(db_pool, candidate: dict, dry_run: bool) -> bool:
    """Write and publish one ready candidate. Returns False if its content was unusable."""
    try:
        content = await run_content_generation(db_pool, candidate["id"], candidate["post_format"])
    except (LLMRefusalError, ValueError) as exc:
        await _skip(db_pool, candidate, exc)
        return False
    await run_publish(db_pool, candidate["id"], content, dry_run=dry_run)
    console.log(
        f"[pipeline] {'Previewed' if dry_run else 'Posted'}:"
        f" {candidate['place_name']} ({candidate['post_format']})"
    )
    return True


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
            candidate = await _next_unprepared(db_pool, tried, last_category)
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
    """Execute a pipeline run: pick a candidate, research it, score it, generate content, publish.

    Reads DRY_RUN and DATABASE_URL from env. Creates and tears down the asyncpg pool
    per run. `posts` > 1 repeats the cycle, which is useful for previewing several
    posts in a dry run; `parallel` is how many candidates are researched and scored at
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
    return produced


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--posts", type=int, default=1, help="number of posts to produce")
    parser.add_argument(
        "--parallel", type=int, default=1, help="candidates to research and score at once"
    )
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run_once(args.posts, args.parallel)) else 1)
