"""Name the outlets a post's facts came from, for the closing reply.

The writer says which numbered web sources it used. Those numbers are turned into short outlet
names ("Question of Cities", "Britannica") here, in code, so a name can never be invented. Names
carry no dots: X turns anything that looks like a domain into a link, and a post with a link
costs far more.
"""

import re
from collections import Counter
from urllib.parse import urlparse

_SEPARATORS = (" - ", " | ", " — ", " – ", " · ")
# Second-level labels that are not the site's name (bbc.co.uk, abs.gov.au)
_GENERIC = {"co", "com", "org", "gov", "ac", "net", "edu"}


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def outlet_name(source: dict) -> str | None:
    """A short name for the site a source came from, or None if its address is unusable."""
    host = (urlparse(source.get("url", "")).hostname or "").removeprefix("www.")
    parts = host.split(".")
    if len(parts) < 2:
        return None
    label = parts[-3] if parts[-2] in _GENERIC and len(parts) >= 3 else parts[-2]
    # Page titles often end with the site's own spelling of its name: prefer that when it
    # plainly is the site ("... - Question of Cities" on questionofcities.org)
    title = source.get("source_name") or ""
    for separator in _SEPARATORS:
        if separator in title:
            tail = title.rsplit(separator, 1)[1].strip()
            squashed = _squash(tail)
            if "." not in tail and len(squashed) >= 3 and squashed in (label, _squash(host)):
                return tail
    return label.capitalize()


def outlets_used(numbers: list[int], web_sources: list[dict]) -> list[str]:
    """Outlet names for the source numbers the writer used, most-used first, no repeats.

    Numbers are the brief's tags ([1] is the first web source). Numbers that match no source
    are ignored, as are Wikipedia and its sister sites: the article is named separately.
    """
    counts: Counter[str] = Counter()
    for number in dict.fromkeys(numbers):
        if 1 <= number <= len(web_sources):
            name = outlet_name(web_sources[number - 1])
            if name and _squash(name) not in {"wikipedia", "wikimedia", "wikidata"}:
                counts[name] += 1
    return [name for name, _ in counts.most_common()]
