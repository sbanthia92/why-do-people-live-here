"""Gather research for a candidate: Wikipedia extract + supplementary web sources."""

import asyncio
import urllib.parse
from typing import Literal

import anthropic
import httpx
from pydantic import BaseModel, Field
from rich.console import Console

from pipeline import llm
from pipeline.candidate_selector import _HEADERS, _WP_API, _get

console = Console()

Region = Literal[
    "Africa", "Antarctica", "Asia", "Europe", "North America", "Oceania", "South America"
]


class ResearchSummary(BaseModel):
    region: Region = Field(description="Continent the place is on (or nearest to, for islands).")
    summary: str = Field(description="The research summary, every claim tagged with its sources.")


_SEARCH_SYSTEM = """\
You are researching a remote or unusual settlement for a short explainer on why people \
live (or lived) there. Use web search to find reliable, independent sources — news \
features, government or academic pages, museum and local-history sites. Do not rely on \
Wikipedia or sites that mirror it; that article is already in hand.

You will be given one aspect of the place to research. Stay on that aspect; other \
researchers are covering the rest. Report what each source says, citing it. If sources \
disagree, say so. If you find little, say that plainly instead of padding."""

# One search per aspect, run in parallel. Narrow questions return faster than one broad
# one and between them cover more ground, which is what the corroboration gate needs.
_SEARCH_ASPECTS = {
    "origin": "How and why the place was founded: who, when, and for what reason.",
    "livelihood": (
        "What sustains people there economically, now and in the past, and how many"
        " people live there today."
    ),
    "daily life": (
        "Concrete, striking details of daily life: supplies, transport, climate, prices,"
        " routines, and anything a visitor would find surprising."
    ),
}

_SYNTH_SYSTEM = """\
You are preparing a research brief on a settlement. The brief feeds an editor and then a \
writer who may use only what is in it, so accuracy matters more than polish.

Write a brief that answers: Why do people live here? Cover the founding (who, when, \
why — with names and dates where the sources give them), the economic engine that \
keeps people there, what makes the place surprising, and vivid concrete details \
(numbers, objects, routines). Give the current population if stated.

Rules:
- Use only facts present in the provided sources. Add nothing from memory.
- End every sentence with the tags of the sources that support it: [W] for the \
Wikipedia article, [1], [2], ... for the numbered web sources. A claim backed by both \
gets both tags, e.g. [W][2]. These tags are how the editor judges corroboration.
- Where sources conflict, state both versions with their tags.
- Leave out anything the sources do not support, even if it would make a better story."""


async def fetch_wikipedia_extract(wikipedia_url: str) -> str | None:
    """Fetch the full plain-text extract of a Wikipedia article via the action API.

    Returns plain text or None if the article is not found.
    """
    title = urllib.parse.unquote(wikipedia_url.rstrip("/").split("/wiki/")[-1]).replace("_", " ")
    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": "1",
        "titles": title,
        "redirects": "1",
        "format": "json",
        "formatversion": "2",
    }
    async with httpx.AsyncClient(timeout=15.0, headers=_HEADERS) as client:
        resp = await _get(client, _WP_API, params=params)
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None
    return pages[0].get("extract") or None


async def fetch_web_sources(place_name: str, story_category: str) -> list[dict]:
    """Search for supplementary web sources about the place.

    Runs one focused search per aspect in _SEARCH_ASPECTS concurrently and merges the
    results. Returns a list of {url, snippet, source_name} dicts. Returns [] if web
    search is unavailable, so research can still proceed on the Wikipedia article alone
    (the corroboration gate will then fail).
    """

    async def search(aspect: str, question: str) -> list[dict]:
        user = (
            f"Place: {place_name}\n"
            f"Story type: {story_category.replace('_', ' ')}\n\n"
            f"Aspect to research — {aspect}: {question}"
        )
        try:
            return await llm.web_search_citations(system=_SEARCH_SYSTEM, user=user)
        except anthropic.BadRequestError as exc:
            # An empty credit balance also arrives as a 400. That isn't "search is
            # unavailable": every later call would fail too, so stop here.
            if "credit balance" in exc.message:
                raise
            console.log(
                f"[researcher] Web search ({aspect}) unavailable for {place_name}: {exc.message}"
            )
            return []

    results = await asyncio.gather(*(search(a, q) for a, q in _SEARCH_ASPECTS.items()))

    sources: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for source in (s for result in results for s in result):
        key = (source["url"], source["snippet"])
        if key not in seen:
            seen.add(key)
            sources.append(source)
    return sources


async def synthesise_research(
    place_name: str,
    wikipedia_extract: str,
    web_sources: list[dict],
) -> ResearchSummary:
    """Use Claude to synthesise a single research summary from all gathered sources.

    Returns the region plus the research text that will be passed to the assessor.
    """
    web = "\n\n".join(
        f"[{i}] {s['source_name']} ({s['url']})\n{s['snippet']}"
        for i, s in enumerate(web_sources, start=1)
    )
    user = (
        f"Place: {place_name}\n\n"
        f"<wikipedia_article tag=\"W\">\n{wikipedia_extract}\n</wikipedia_article>\n\n"
        f"<web_sources>\n{web or '(none found)'}\n</web_sources>"
    )
    return await llm.parse(system=_SYNTH_SYSTEM, user=user, output_format=ResearchSummary)


async def run_research(db_pool, candidate_id: int) -> None:
    """Orchestrate full research for a candidate and persist results to the research table.

    Also records the candidate's region and moves it to status 'researched'. If the
    Wikipedia article can't be fetched, the candidate is marked 'skipped'.
    """
    candidate = await db_pool.fetchrow(
        "SELECT place_name, wikipedia_url, story_category FROM candidates WHERE id = $1",
        candidate_id,
    )
    place_name = candidate["place_name"]

    extract, web_sources = await asyncio.gather(
        fetch_wikipedia_extract(candidate["wikipedia_url"]),
        fetch_web_sources(place_name, candidate["story_category"]),
    )
    if not extract:
        console.log(f"[researcher] No Wikipedia text for {place_name} — skipping")
        await db_pool.execute(
            "UPDATE candidates SET status = 'skipped', updated_at = now() WHERE id = $1",
            candidate_id,
        )
        return

    console.log(f"[researcher] {place_name}: {len(web_sources)} web source passages")
    research = await synthesise_research(place_name, extract, web_sources)

    await db_pool.execute(
        """
        INSERT INTO research (candidate_id, wikipedia_extract, web_sources, raw_research)
        VALUES ($1, $2, $3, $4)
        """,
        candidate_id,
        extract,
        web_sources,
        research.summary,
    )
    await db_pool.execute(
        """
        UPDATE candidates SET region = $2, status = 'researched', updated_at = now()
        WHERE id = $1
        """,
        candidate_id,
        research.region,
    )
