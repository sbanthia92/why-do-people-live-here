"""Report which freely usable photos exist for recently posted places. Read-only.

For each place posted in the last few days, lists the photos in its Wikipedia article and in
its Wikimedia Commons category, applies the same licence rules the pipeline uses for photos
(CC0 or public domain, no attribution, no rights warning, JPEG or PNG), and saves small
copies of the usable ones so they can be looked at. It posts nothing.

Usage:  python scripts/check_images.py --state-dir DIR [--days 7] [--out-dir image-check]
        python scripts/check_images.py --place "Kaktovik, Alaska" --place "Nueva Germania"
"""

import argparse
import asyncio
import json
import re
import sys
import urllib.parse
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.candidate_selector import _HEADERS, _WP_API, _get
from pipeline.image_sourcer import _LICENSE_LABELS, _field, _plain, _unusable_reason

_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_THUMB_WIDTH = 800
# Names that are almost always flags, maps, logos or diagrams rather than photos of a place
_NOT_PHOTOS = re.compile(
    r"flag|coat[ _]of[ _]arms|map|locator|location|icon|logo|symbol|seal|blank|diagram|chart|graph",
    re.IGNORECASE,
)
_IMAGEINFO = {
    "prop": "imageinfo",
    "iiprop": "url|mime|size|extmetadata",
    "iiurlwidth": str(_THUMB_WIDTH),
    "format": "json",
    "formatversion": "2",
}


def photo_record(page: dict, source: str) -> dict | None:
    """One file as a record: whether the pipeline could post it, and why not. None if not a photo."""
    name = page.get("title", "").removeprefix("File:")
    info = (page.get("imageinfo") or [None])[0]
    if not info or _NOT_PHOTOS.search(name):
        return None
    meta = info.get("extmetadata", {})
    reason = _unusable_reason(page, info)
    license_code = _field(meta, "License").lower()
    return {
        "name": name,
        "source": source,
        "width": info.get("width"),
        "height": info.get("height"),
        "usable": reason is None,
        "reason": reason,
        "license": _LICENSE_LABELS.get(license_code, _field(meta, "LicenseShortName") or "unknown"),
        "description": _plain(_field(meta, "ImageDescription"))[:140],
        "page_url": info.get("descriptionurl"),
        "thumb_url": info.get("thumburl") or info.get("url"),
    }


async def _pages(client: httpx.AsyncClient, api: str, params: dict) -> list[dict]:
    resp = await _get(client, api, params={**params, **_IMAGEINFO})
    resp.raise_for_status()
    return resp.json().get("query", {}).get("pages", [])


async def photos_for(client: httpx.AsyncClient, title: str) -> list[dict]:
    """Photos for a place from its Wikipedia article and its Commons category."""
    found: dict[str, dict] = {}
    article = await _pages(
        client,
        _WP_API,
        {
            "action": "query",
            "generator": "images",
            "gimlimit": "100",
            "titles": title,
            "redirects": "1",
        },
    )
    for page in article:
        if record := photo_record(page, "article"):
            found.setdefault(record["name"], record)

    # Commons categories are usually named after the place, sometimes without the region
    for name in dict.fromkeys([title, title.split(",")[0].strip()]):
        members = await _pages(
            client,
            _COMMONS_API,
            {
                "action": "query",
                "generator": "categorymembers",
                "gcmtitle": f"Category:{name}",
                "gcmtype": "file",
                "gcmlimit": "100",
            },
        )
        for page in members:
            # Files listed by Commons itself are hosted there; the pipeline's rule wants "shared"
            record = photo_record({**page, "imagerepository": "shared"}, "category")
            if record:
                found.setdefault(record["name"], record)
        if members:
            break
    return list(found.values())


def render(results: dict[str, list[dict]], max_listed: int = 6) -> str:
    """The report as Markdown: a summary table, then each place's usable photos."""
    lines = ["# Image check", "", "| Place | Photos found | Usable | Rejected |", "|---|---|---|---|"]
    for place, photos in results.items():
        usable = sum(p["usable"] for p in photos)
        lines.append(f"| {place} | {len(photos)} | {usable} | {len(photos) - usable} |")
    for place, photos in results.items():
        usable = [p for p in photos if p["usable"]]
        lines += ["", f"## {place}", ""]
        if not photos:
            lines.append("No photos found in the article or the Commons category.")
            continue
        for p in usable[:max_listed]:
            size = f"{p['width']}×{p['height']}" if p["width"] else "?"
            lines.append(
                f"- **{p['name']}** ({size}, {p['license']}, from the {p['source']})"
                f" — {p['description'] or 'no description'} — {p['page_url']}"
            )
        if len(usable) > max_listed:
            lines.append(f"- … and {len(usable) - max_listed} more usable")
        if not usable:
            lines.append("None usable.")
        rejected = Counter(p["reason"] for p in photos if not p["usable"])
        if rejected:
            why = ", ".join(f"{reason} ×{n}" for reason, n in rejected.most_common())
            lines += ["", f"Rejected {sum(rejected.values())}: {why}."]
    return "\n".join(lines) + "\n"


def recent_places(state_dir: Path, days: int) -> list[str]:
    """Titles of the places posted for real in the last `days` days, oldest first."""
    since = datetime.now(UTC) - timedelta(days=days)

    def rows(table: str) -> list[dict]:
        file = state_dir / f"{table}.jsonl"
        return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []

    urls = {c["id"]: c["wikipedia_url"] for c in rows("candidates")}
    posts = [
        p
        for p in rows("posts")
        if not p["dry_run"] and datetime.fromisoformat(p["posted_at"]) >= since
    ]
    return [
        urllib.parse.unquote(urls[p["candidate_id"]].rsplit("/wiki/", 1)[-1]).replace("_", " ")
        for p in sorted(posts, key=lambda p: p["posted_at"])
        if p["candidate_id"] in urls
    ]


async def save_thumbnails(client: httpx.AsyncClient, place: str, photos: list[dict], out_dir: Path, limit: int) -> None:
    folder = out_dir / re.sub(r"[^a-z0-9]+", "-", place.lower()).strip("-")
    for i, photo in enumerate([p for p in photos if p["usable"]][:limit], start=1):
        try:
            resp = await client.get(photo["thumb_url"], follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"Could not fetch {photo['name']}: {exc!r}", file=sys.stderr)
            continue
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", photo["name"])[-80:]
        (folder / f"{i:02d}-{safe}").write_bytes(resp.content)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", type=Path, help="saved state to take recent places from")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--place", action="append", default=[], help="a Wikipedia title; repeatable")
    parser.add_argument("--out-dir", type=Path, default=Path("image-check"))
    parser.add_argument("--max-images", type=int, default=6, help="usable photos saved per place")
    args = parser.parse_args()

    places = args.place or (recent_places(args.state_dir, args.days) if args.state_dir else [])
    if not places:
        print("No places to check. Give --place, or --state-dir with recent posts.")
        return 1

    results: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
        for place in places:
            results[place] = await photos_for(client, place)
            await save_thumbnails(client, place, results[place], args.out_dir, args.max_images)
    print(render(results, args.max_images), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
