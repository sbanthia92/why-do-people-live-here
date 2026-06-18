"""Gather research for a candidate: Wikipedia extract + supplementary web sources."""

from typing import Optional


async def fetch_wikipedia_extract(wikipedia_url: str) -> Optional[str]:
    """Fetch the lead-section extract for a Wikipedia article via the REST API.

    Returns plain text or None if the article is not found.
    """
    pass


async def fetch_web_sources(place_name: str, story_category: str) -> list[dict]:
    """Search for supplementary web sources about the place.

    Returns a list of {url, snippet, source_name} dicts.
    """
    pass


async def synthesise_research(
    place_name: str,
    wikipedia_extract: str,
    web_sources: list[dict],
) -> str:
    """Use Claude to synthesise a single research summary from all gathered sources.

    Returns the raw research text that will be passed to the scorer.
    """
    pass


async def run_research(db_pool, candidate_id: int) -> None:
    """Orchestrate full research for a candidate and persist results to the research table."""
    pass
