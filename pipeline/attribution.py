"""Name the outlets a post's facts came from, for the closing reply.

The writer says which numbered web sources it used. Up to three of those sites, the most used
first, are named in the reply ("Anchorage Daily News", not "Adn"). A cheap model reads each
source's page title and address and gives the publisher's usual name; if that fails, a name
is built from the address. Names carry no dots: X turns anything that looks like a domain
into a link, and a post with a link costs far more.
"""

import re
from collections import Counter
from urllib.parse import urlparse

import anthropic
from pydantic import BaseModel, Field
from rich.console import Console

from pipeline import llm

console = Console()

MAX_OUTLETS = 3
_MAX_NAME_CHARS = 40
_SEPARATORS = (" - ", " | ", " — ", " – ", " · ")
# Second-level labels that are not the site's name (bbc.co.uk, abs.gov.au)
_GENERIC = {"co", "com", "org", "gov", "ac", "net", "edu", "mil"}
# Sites that host other people's pages: naming the host says nothing about the source
_HOSTS = {
    "weebly", "wordpress", "blogspot", "wixsite", "wix", "medium", "substack", "tumblr",
    "github", "google", "facebook", "youtube", "reddit", "twitter", "x", "pinterest",
    "blogger", "typepad", "livejournal", "neocities", "geocities", "angelfire", "tripod",
    "fandom", "zianet", "earthlink", "comcast", "aol",
    "wikipedia", "wikimedia", "wikidata",
}  # fmt: skip


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _is_hosted_page(source: dict) -> bool:
    """A personal page on someone else's site: its host is in _HOSTS, or its path is "/~name"."""
    site = _site(source)
    path = urlparse(source.get("url", "")).path
    return site is None or site[1] in _HOSTS or path.startswith("/~")


def _site(source: dict) -> tuple[str, str] | None:
    """(host, the label that names the site), or None if the address is unusable."""
    host = (urlparse(source.get("url", "")).hostname or "").removeprefix("www.")
    parts = host.split(".")
    if len(parts) < 2:
        return None
    label = parts[-3] if parts[-2] in _GENERIC and len(parts) >= 3 else parts[-2]
    return host, label


def outlet_name(source: dict) -> str | None:
    """A name for a source's site built from its address and title alone, or None."""
    site = _site(source)
    if site is None:
        return None
    host, label = site
    # Page titles often end with the site's own spelling of its name: prefer that when it
    # plainly is the site ("... - Question of Cities" on questionofcities.org, or its
    # initials: "... - Anchorage Daily News" on adn.com)
    title = source.get("source_name") or ""
    for separator in _SEPARATORS:
        if separator in title:
            tail = title.rsplit(separator, 1)[1].strip()
            squashed = _squash(tail)
            initials = "".join(word[0] for word in tail.lower().split() if word[0].isalnum())
            matches = squashed in (label, _squash(host)) or (len(label) >= 3 and initials == label)
            if "." not in tail and len(squashed) >= 3 and matches:
                return tail
    return label.capitalize()


def sites_used(numbers: list[int], web_sources: list[dict]) -> list[dict]:
    """One source per site the writer used, most-used site first, at most MAX_OUTLETS.

    Numbers are the brief's tags ([1] is the first web source). Numbers that match no source
    are ignored, as are Wikipedia (the article is named separately) and sites that only host
    other people's pages.
    """
    counts: Counter[str] = Counter()
    first: dict[str, dict] = {}
    for number in dict.fromkeys(numbers):
        if not 1 <= number <= len(web_sources):
            continue
        source = web_sources[number - 1]
        if _is_hosted_page(source):
            continue
        site = _site(source)
        counts[site[0]] += 1
        first.setdefault(site[0], source)
    return [first[host] for host, _ in counts.most_common(MAX_OUTLETS)]


class OutletNames(BaseModel):
    names: list[str] = Field(description="One name per source, in the order given.")


_SYSTEM = f"""\
For each web source you are given its address and page title. Give the usual name of the \
publisher or organisation behind it, as a reader would know it: "Anchorage Daily News" for \
adn.com, "US Army Corps of Engineers" for usace.army.mil, "Britannica" for britannica.com.

Rules: one name per source, in the same order. At most {_MAX_NAME_CHARS} characters each. No \
full stops, no web addresses, no "the". If you do not recognise the site, give the site's \
name as written in its address, capitalised. Never guess a publisher you are not sure of."""


def _clean(name: str) -> str | None:
    """A model-given name made safe for a post, or None if it cannot be used."""
    name = " ".join(name.replace(".", "").split())
    if not name or len(name) > _MAX_NAME_CHARS or re.search(r"[/@:;\n]|https?", name):
        return None
    return name


async def name_outlets(numbers: list[int], web_sources: list[dict]) -> list[str]:
    """Names of up to three outlets the writer used, for the closing reply. Never raises."""
    sources = sites_used(numbers, web_sources)
    if not sources:
        return []
    fallback = [outlet_name(s) for s in sources]
    listing = "\n".join(
        f"{i}. {_site(s)[0]} — {s.get('source_name') or '(no title)'}"
        for i, s in enumerate(sources, start=1)
    )
    try:
        answer = await llm.parse(
            system=_SYSTEM,
            user=listing,
            output_format=OutletNames,
            effort="low",
            model=llm.CHEAP_MODEL,
        )
        given = [_clean(name) for name in answer.names]
    except (llm.LLMRefusalError, anthropic.APIError, RuntimeError, ValueError) as exc:
        console.log(f"[attribution] Using address names for the sources ({type(exc).__name__})")
        given = []
    if len(given) != len(sources):
        given = [None] * len(sources)
    names = [g or f for g, f in zip(given, fallback, strict=True)]
    return list(dict.fromkeys(n for n in names if n))
