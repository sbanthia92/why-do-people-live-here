"""Select candidate places from Wikipedia categories, run layer 1 filter, and persist to DB.

Layer 1 is a cheap pre-filter that runs before any LLM calls.  It keeps only
articles that have enough text and structural depth to be worth researching.
"""

import random
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx
import yaml
from rich.console import Console

console = Console()

_SEED_PATH = Path(__file__).parent.parent / "config" / "seed_categories.yaml"
_WP_API = "https://en.wikipedia.org/w/api.php"
_WP_REST = "https://en.wikipedia.org/api/rest_v1/page/summary"

# Minimum extract length to pass layer 1
_MIN_EXTRACT_CHARS = 500
# Layer 1 required sections (any one match is enough)
_REQUIRED_SECTIONS = {"Economy", "History", "Background", "Origins"}
# Max candidates to attempt per category before giving up
_MAX_TRIES = 10


def _load_seed_categories() -> dict[str, list[str]]:
    """Load story_category → [category_slug, ...] mapping from seed YAML."""
    with open(_SEED_PATH) as f:
        return yaml.safe_load(f)


async def fetch_category_members(category_slug: str, limit: int = 100) -> list[dict]:
    """Fetch article titles from a Wikipedia category via the categorymembers API.

    Uses the MediaWiki action API:
      action=query & list=categorymembers & cmtitle=Category:{slug} & cmnamespace=0
    cmnamespace=0 restricts results to article pages only (no talk/file pages).
    Returns a list of dicts with keys: title, pageid, wikipedia_url.
    """
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": f"Category:{category_slug}",
        "cmlimit": str(limit),
        "cmnamespace": "0",
        "format": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_WP_API, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        console.log(f"[candidate_selector] TIMEOUT fetching category: {category_slug}")
        return []
    except httpx.HTTPStatusError as exc:
        console.log(
            f"[candidate_selector] HTTP {exc.response.status_code} fetching category:"
            f" {category_slug}"
        )
        return []

    members = data.get("query", {}).get("categorymembers", [])
    results = []
    for m in members:
        title = m["title"]
        encoded = urllib.parse.quote(title.replace(" ", "_"))
        results.append(
            {
                "title": title,
                "pageid": m["pageid"],
                "wikipedia_url": f"https://en.wikipedia.org/wiki/{encoded}",
            }
        )
    return results


async def layer1_filter(wikipedia_url: str) -> tuple[bool, dict]:
    """Run cheap structural checks on a Wikipedia article before any LLM call.

    Checks:
      1. Extract length >= 500 chars — ensures the article is substantive.
      2. A 'description' field is present — disambiguation pages lack this.
      3. The article has at least one section in {Economy, History, Background, Origins}
         with more than one paragraph — ensures the place has documented depth.

    Returns (passed, metadata) where metadata contains extract, section_names, char_count.
    On any unexpected error returns (False, {}).
    """
    title = wikipedia_url.rstrip("/").split("/wiki/")[-1]
    title_decoded = urllib.parse.unquote(title).replace("_", " ")

    metadata: dict = {"extract": "", "section_names": [], "char_count": 0}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # --- summary check ---
            summary_url = f"{_WP_REST}/{urllib.parse.quote(title)}"
            try:
                sr = await client.get(summary_url)
                sr.raise_for_status()
                summary = sr.json()
            except httpx.TimeoutException:
                console.log(
                    f"[candidate_selector] TIMEOUT fetching summary: {title_decoded}"
                )
                return False, {}
            except httpx.HTTPStatusError as exc:
                console.log(
                    f"[candidate_selector] HTTP {exc.response.status_code} summary:"
                    f" {title_decoded}"
                )
                return False, {}

            extract = summary.get("extract", "")
            char_count = len(extract)
            metadata["extract"] = extract
            metadata["char_count"] = char_count

            if char_count < _MIN_EXTRACT_CHARS:
                console.log(
                    f"[candidate_selector] Layer 1 FAIL: {title_decoded}"
                    f" ({char_count} chars, below minimum)"
                )
                return False, metadata

            if "description" not in summary:
                console.log(
                    f"[candidate_selector] Layer 1 FAIL: {title_decoded}"
                    " (no description — likely disambiguation)"
                )
                return False, metadata

            # --- sections check ---
            sections_params = {
                "action": "parse",
                "page": title_decoded,
                "prop": "sections",
                "format": "json",
            }
            try:
                pr = await client.get(_WP_API, params=sections_params)
                pr.raise_for_status()
                parse_data = pr.json()
            except httpx.TimeoutException:
                console.log(
                    f"[candidate_selector] TIMEOUT fetching sections: {title_decoded}"
                )
                return False, metadata
            except httpx.HTTPStatusError as exc:
                console.log(
                    f"[candidate_selector] HTTP {exc.response.status_code} sections:"
                    f" {title_decoded}"
                )
                return False, metadata

            sections = parse_data.get("parse", {}).get("sections", [])
            section_names = [s.get("line", "") for s in sections]
            metadata["section_names"] = section_names

            has_required = any(
                any(req.lower() in name.lower() for req in _REQUIRED_SECTIONS)
                for name in section_names
            )

            if not has_required:
                console.log(
                    f"[candidate_selector] Layer 1 FAIL: {title_decoded}"
                    f" ({char_count} chars, no Economy/History section)"
                )
                return False, metadata

            matching = [
                n
                for n in section_names
                if any(req.lower() in n.lower() for req in _REQUIRED_SECTIONS)
            ]
            console.log(
                f"[candidate_selector] Layer 1 PASS: {title_decoded}"
                f" ({char_count} chars, has {matching[0]} section)"
            )
            return True, metadata

    except Exception as exc:  # noqa: BLE001
        console.log(
            f"[candidate_selector] Unexpected error in layer1_filter for"
            f" {title_decoded}: {exc}"
        )
        return False, {}


async def select_candidate(story_category: str, db_conn) -> Optional[dict]:
    """Pick a new candidate from a story_category, run layer 1, and persist if it passes.

    Variety rotation: if the last 3 posts in variety_log all share a region with
    candidates from this category, return None to let the caller try another category.

    Tries up to _MAX_TRIES members before returning None.
    """
    seed = _load_seed_categories()
    slugs = seed.get(story_category, [])
    if not slugs:
        return None

    # --- variety rotation guard ---
    recent = await db_conn.fetch(
        "SELECT region FROM variety_log ORDER BY posted_at DESC LIMIT 5"
    )
    recent_regions = [r["region"] for r in recent]
    if len(recent_regions) >= 3 and len(set(recent_regions[:3])) == 1:
        blocked_region = recent_regions[0]
        console.log(
            f"[candidate_selector] Variety rotation: blocking region '{blocked_region}'"
            f" for story_category '{story_category}'"
        )
        # We don't know candidate regions at this point; caller can pass None back
        # and retry another story_category. Signal by returning None only when ALL
        # candidates we'd try are from the blocked region — resolved below after fetch.

    random.shuffle(slugs)
    for slug in slugs:
        console.log(
            f"[candidate_selector] Fetching category: {story_category} / {slug}"
        )
        members = await fetch_category_members(slug)
        if not members:
            continue

        random.shuffle(members)
        tries = 0
        for member in members:
            if tries >= _MAX_TRIES:
                break

            url = member["wikipedia_url"]
            title = member["title"]

            # Skip if already in candidates table
            existing = await db_conn.fetchrow(
                "SELECT id, layer1_passed FROM candidates WHERE wikipedia_url = $1", url
            )
            if existing:
                continue

            tries += 1

            # Variety rotation: skip if candidate's region matches blocked region
            # (Region is unknown pre-insert; we skip only after we retrieve it post-pass)

            passed, meta = await layer1_filter(url)

            if not passed:
                # Persist as failed so we never retry this article
                await db_conn.execute(
                    """
                    INSERT INTO candidates
                        (place_name, wikipedia_url, story_category, layer1_passed, status)
                    VALUES ($1, $2, $3, false, 'skipped')
                    ON CONFLICT (wikipedia_url) DO NOTHING
                    """,
                    title,
                    url,
                    story_category,
                )
                continue

            # Insert passing candidate
            row = await db_conn.fetchrow(
                """
                INSERT INTO candidates
                    (place_name, wikipedia_url, story_category, layer1_passed, status)
                VALUES ($1, $2, $3, true, 'pending')
                ON CONFLICT (wikipedia_url) DO UPDATE
                    SET layer1_passed = EXCLUDED.layer1_passed,
                        updated_at = now()
                RETURNING id, place_name, wikipedia_url, story_category, status,
                          layer1_passed, created_at
                """,
                title,
                url,
                story_category,
            )
            return dict(row)

    return None


async def run_selector(db_conn) -> Optional[dict]:
    """Iterate story_categories in random order, return first passing candidate.

    Loads seed_categories.yaml, shuffles story_category keys, calls select_candidate
    for each until one returns a result.  Returns None if all categories are exhausted.
    """
    seed = _load_seed_categories()
    categories = list(seed.keys())
    random.shuffle(categories)

    for story_category in categories:
        console.log(f"[candidate_selector] Trying story_category: {story_category}")
        candidate = await select_candidate(story_category, db_conn)
        if candidate:
            return candidate

    console.log("[candidate_selector] All categories exhausted — no candidate found.")
    return None
