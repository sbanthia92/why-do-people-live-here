"""Select candidate places from Wikipedia categories, run layer 1 filter, and persist to DB.

Layer 1 is a cheap pre-filter that runs before any LLM calls.  It keeps only
articles that have enough text and structural depth to be worth researching.
"""

import asyncio
import os
import random
import urllib.parse
from pathlib import Path

import httpx
import yaml
from rich.console import Console

console = Console()

_SEED_PATH = Path(__file__).parent.parent / "config" / "seed_categories.yaml"
_WP_API = "https://en.wikipedia.org/w/api.php"
_WP_REST = "https://en.wikipedia.org/api/rest_v1/page/summary"
# Wikimedia rejects requests without a descriptive User-Agent (HTTP 403)
_HEADERS = {
    "User-Agent": os.getenv(
        "WIKIMEDIA_USER_AGENT", "whydopeoplelivehere/0.1 (contact@example.com)"
    )
}

# Minimum full-article plain-text length to pass layer 1
_MIN_EXTRACT_CHARS = 500
# Layer 1 required sections (any one match is enough)
# Matched as case-insensitive substrings, so "Origin" also covers "Origins"
_REQUIRED_SECTIONS = {"Economy", "History", "Background", "Origin"}
# Max candidates to attempt per category before giving up
_MAX_TRIES = 10
# Max subcategories to descend into (one level) per seed category
_MAX_SUBCATEGORIES = 10
_NS_CATEGORY = 14
# HTTP 429 handling: retry up to this many times, sleeping for Retry-After (capped)
_MAX_RETRIES = 3
_MAX_RETRY_WAIT_S = 60.0


async def _get(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """client.get that waits and retries when Wikimedia rate-limits us (HTTP 429)."""
    for attempt in range(_MAX_RETRIES + 1):
        resp = await client.get(url, **kwargs)
        if resp.status_code != 429 or attempt == _MAX_RETRIES:
            return resp
        try:
            wait = float(resp.headers.get("retry-after", 2 ** (attempt + 1)))
        except ValueError:
            wait = 2 ** (attempt + 1)
        wait = min(wait, _MAX_RETRY_WAIT_S)
        console.log(f"[candidate_selector] HTTP 429 — retrying in {wait:.0f}s: {url}")
        await asyncio.sleep(wait)
    return resp  # unreachable; keeps type checkers happy


def _load_seed_categories() -> dict[str, dict[str, list[str]]]:
    """Load story_category → {"categories": [...], "places": [...]} from seed YAML.

    `categories` are Wikipedia category slugs to crawl; `places` are hand-picked
    article titles. Either key may be absent.
    """
    with open(_SEED_PATH) as f:
        return yaml.safe_load(f)


def _article_url(title: str) -> str:
    encoded = urllib.parse.quote(title.replace(" ", "_"))
    return f"https://en.wikipedia.org/wiki/{encoded}"


def _place_members(titles: list[str]) -> list[dict]:
    """Turn hand-picked article titles into the same shape fetch_category_members returns."""
    return [{"title": t, "wikipedia_url": _article_url(t)} for t in titles]


async def _get_category_members(
    client: httpx.AsyncClient, category_slug: str, limit: int, namespaces: str
) -> list[dict]:
    """Raw categorymembers query. Raises httpx errors to the caller."""
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": f"Category:{category_slug}",
        "cmlimit": str(limit),
        "cmnamespace": namespaces,
        "format": "json",
    }
    resp = await _get(client, _WP_API, params=params)
    resp.raise_for_status()
    return resp.json().get("query", {}).get("categorymembers", [])


async def fetch_category_members(category_slug: str, limit: int = 100) -> list[dict]:
    """Fetch article titles from a Wikipedia category and one level of its subcategories.

    Uses the MediaWiki action API:
      action=query & list=categorymembers & cmtitle=Category:{slug} & cmnamespace=0|14
    Namespace 0 is articles, 14 is subcategories. Many seed categories (e.g.
    Ghost_towns_in_the_United_States) hold few articles directly and split the rest
    into subcategories, so up to _MAX_SUBCATEGORIES randomly chosen subcategories are
    also fetched (articles only — no deeper recursion). `limit` applies per request.
    Returns a list of dicts with keys: title, pageid, wikipedia_url.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
            members = await _get_category_members(client, category_slug, limit, "0|14")

            subcats = [m["title"] for m in members if m.get("ns") == _NS_CATEGORY]
            members = [m for m in members if m.get("ns", 0) == 0]

            random.shuffle(subcats)
            for subcat in subcats[:_MAX_SUBCATEGORIES]:
                sub_slug = subcat.removeprefix("Category:").replace(" ", "_")
                try:
                    members += await _get_category_members(client, sub_slug, limit, "0")
                except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                    # A bad subcategory shouldn't sink the whole parent category
                    console.log(
                        f"[candidate_selector] Skipping subcategory {sub_slug}: {exc!r}"
                    )
    except httpx.TimeoutException:
        console.log(f"[candidate_selector] TIMEOUT fetching category: {category_slug}")
        return []
    except httpx.HTTPStatusError as exc:
        console.log(
            f"[candidate_selector] HTTP {exc.response.status_code} fetching category:"
            f" {category_slug}"
        )
        return []

    results = []
    seen: set[int] = set()
    for m in members:
        # An article can sit in several subcategories at once
        if m["pageid"] in seen:
            continue
        seen.add(m["pageid"])
        results.append(
            {
                "title": m["title"],
                "pageid": m["pageid"],
                "wikipedia_url": _article_url(m["title"]),
            }
        )
    return results


async def layer1_filter(
    wikipedia_url: str, require_coordinates: bool = False
) -> tuple[bool, dict]:
    """Run cheap structural checks on a Wikipedia article before any LLM call.

    Checks:
      1. A 'description' field is present in the REST summary — disambiguation pages
         lack this.
      2. Full-article plain text >= 500 chars — ensures the article is substantive.
         (The REST summary extract is only the lead paragraph, usually well under 500
         chars even for long articles, so it can't be used for this check.)
      3. The article has at least one section matching {Economy, History, Background, Origin}
         — ensures the place has documented depth.
      4. With require_coordinates=True, the summary must carry geographic coordinates.
         Category crawls pull in films, lists and concept articles ("Company town");
         only real places have coordinates. Hand-picked places skip this check.

    Returns (passed, metadata) where metadata contains extract (full plain text),
    section_names, char_count.
    On any unexpected error returns (False, {}).
    """
    title = wikipedia_url.rstrip("/").split("/wiki/")[-1]
    title_decoded = urllib.parse.unquote(title).replace("_", " ")

    metadata: dict = {"extract": "", "section_names": [], "char_count": 0}

    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
            # --- summary check ---
            # title may already be percent-encoded (see fetch_category_members); normalise
            # before re-encoding so "%2C" doesn't become "%252C"
            summary_url = f"{_WP_REST}/{urllib.parse.quote(urllib.parse.unquote(title))}"
            try:
                sr = await _get(client, summary_url)
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

            if "description" not in summary:
                console.log(
                    f"[candidate_selector] Layer 1 FAIL: {title_decoded}"
                    " (no description — likely disambiguation)"
                )
                return False, metadata

            if require_coordinates and "coordinates" not in summary:
                console.log(
                    f"[candidate_selector] Layer 1 FAIL: {title_decoded}"
                    " (no coordinates — not a place)"
                )
                return False, metadata

            # --- full-article length check ---
            extract_params = {
                "action": "query",
                "prop": "extracts",
                "explaintext": "1",
                "titles": title_decoded,
                "redirects": "1",
                "format": "json",
                "formatversion": "2",
            }
            try:
                er = await _get(client, _WP_API, params=extract_params)
                er.raise_for_status()
                extract_data = er.json()
            except httpx.TimeoutException:
                console.log(
                    f"[candidate_selector] TIMEOUT fetching extract: {title_decoded}"
                )
                return False, metadata
            except httpx.HTTPStatusError as exc:
                console.log(
                    f"[candidate_selector] HTTP {exc.response.status_code} extract:"
                    f" {title_decoded}"
                )
                return False, metadata

            pages = extract_data.get("query", {}).get("pages", [])
            extract = pages[0].get("extract", "") if pages else ""
            char_count = len(extract)
            metadata["extract"] = extract
            metadata["char_count"] = char_count

            if char_count < _MIN_EXTRACT_CHARS:
                console.log(
                    f"[candidate_selector] Layer 1 FAIL: {title_decoded}"
                    f" ({char_count} chars, below minimum)"
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
                pr = await _get(client, _WP_API, params=sections_params)
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


async def select_candidate(story_category: str, db_conn) -> dict | None:
    """Pick a new candidate from a story_category, run layer 1, and persist if it passes.

    Sources are each crawled category plus (as one more source) the hand-picked
    places list, tried in random order. Tries up to _MAX_TRIES members per source
    before moving on; returns None when all sources are exhausted.
    """
    seed = _load_seed_categories().get(story_category) or {}
    places = seed.get("places") or []
    sources: list[str | None] = list(seed.get("categories") or [])
    if places:
        sources.append(None)  # None = the hand-picked places list
    if not sources:
        return None

    random.shuffle(sources)
    for slug in sources:
        if slug is None:
            console.log(f"[candidate_selector] Using hand-picked places: {story_category}")
            members = _place_members(places)
        else:
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

            # Crawled categories contain non-places; hand-picked lists are already vetted
            passed, _ = await layer1_filter(url, require_coordinates=slug is not None)

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


async def run_selector(db_conn, avoid_category: str | None = None) -> dict | None:
    """Iterate story_categories in random order, return first passing candidate.

    Loads seed_categories.yaml, shuffles story_category keys, calls select_candidate
    for each until one returns a result.  Returns None if all categories are exhausted.

    `avoid_category` (the story_category of the most recent post) is tried last, so
    back-to-back posts only share a story type when nothing else is available.
    """
    seed = _load_seed_categories()
    categories = list(seed.keys())
    random.shuffle(categories)
    if avoid_category in categories:
        categories.remove(avoid_category)
        categories.append(avoid_category)

    for story_category in categories:
        console.log(f"[candidate_selector] Trying story_category: {story_category}")
        candidate = await select_candidate(story_category, db_conn)
        if candidate:
            return candidate

    console.log("[candidate_selector] All categories exhausted — no candidate found.")
    return None
