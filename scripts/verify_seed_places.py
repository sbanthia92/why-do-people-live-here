"""Check the hand-picked `places` in config/seed_categories.yaml against Wikipedia.

For every title it reports:
  - MISSING   the article doesn't exist
  - REDIRECT  the title redirects; use the canonical title instead (else dedup breaks,
              since the same place would get two different URLs)
  - DISAMBIG  the title is a disambiguation page
  - DUPLICATE the title appears more than once across story categories
  - FAIL      exists but fails the layer 1 filter (too short / no History-type section)
  - OK        passes

The short description is printed alongside so non-place articles are easy to spot.
Requests run sequentially with a short delay to stay inside Wikimedia's rate limits.

Usage:  python scripts/verify_seed_places.py [story_category ...]
Exit code is non-zero if anything other than OK was found.
"""

import asyncio
import sys
from collections import Counter
from pathlib import Path

import httpx

# Run as a plain script from anywhere without relying on the package being installed
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import candidate_selector
from pipeline.candidate_selector import (
    _HEADERS,
    _WP_API,
    _load_seed_categories,
    layer1_filter,
)

# layer1_filter logs every check; this script prints its own summary line instead
candidate_selector.console.quiet = True

_BATCH = 50  # MediaWiki max titles per query
_DELAY_S = 1.0


async def _resolve(client: httpx.AsyncClient, titles: list[str]) -> dict[str, dict]:
    """Map each input title → {canonical, missing, disambig, description}."""
    out: dict[str, dict] = {}
    for i in range(0, len(titles), _BATCH):
        batch = titles[i : i + _BATCH]
        resp = await candidate_selector._get(
            client,
            _WP_API,
            params={
                "action": "query",
                "titles": "|".join(batch),
                "redirects": "1",
                "prop": "pageprops|description",
                "format": "json",
                "formatversion": "2",
            },
        )
        resp.raise_for_status()
        q = resp.json()["query"]
        # input title → title after normalisation and redirects
        hops = {n["from"]: n["to"] for n in q.get("normalized", [])}
        redirects = {r["from"]: r["to"] for r in q.get("redirects", [])}
        pages = {p["title"]: p for p in q["pages"]}
        for t in batch:
            final = redirects.get(hops.get(t, t), hops.get(t, t))
            page = pages.get(final, {"missing": True})
            out[t] = {
                "canonical": final,
                "missing": bool(page.get("missing") or page.get("invalid")),
                "disambig": "disambiguation" in page.get("pageprops", {}),
                "description": page.get("description", ""),
            }
        await asyncio.sleep(_DELAY_S)
    return out


async def main(only: list[str]) -> int:
    seed = _load_seed_categories()
    all_titles = [t for cfg in seed.values() for t in (cfg or {}).get("places") or []]
    dupes = {t for t, n in Counter(all_titles).items() if n > 1}
    problems = 0

    async with httpx.AsyncClient(timeout=15.0, headers=_HEADERS) as client:
        for story_category, cfg in seed.items():
            places = (cfg or {}).get("places") or []
            if not places or (only and story_category not in only):
                continue
            print(f"\n== {story_category} ({len(places)} places)")
            info = await _resolve(client, places)
            counts: Counter = Counter()
            for t in places:
                i = info[t]
                if i["missing"]:
                    status = "MISSING"
                elif i["disambig"]:
                    status = "DISAMBIG"
                elif i["canonical"] != t:
                    status = f"REDIRECT → {i['canonical']}"
                elif t in dupes:
                    status = "DUPLICATE"
                else:
                    passed, meta = await layer1_filter(
                        "https://en.wikipedia.org/wiki/" + t.replace(" ", "_")
                    )
                    await asyncio.sleep(_DELAY_S)
                    status = "OK" if passed else f"FAIL ({meta.get('char_count', '?')} chars)"
                counts[status.split()[0]] += 1
                if not status.startswith("OK"):
                    problems += 1
                print(f"  {status:40} {t:45} {i['description']}")
            print(f"  -- {dict(counts)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
