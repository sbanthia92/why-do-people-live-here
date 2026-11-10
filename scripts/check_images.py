"""Report which freely usable photos exist for recently posted places. Read-only.

For each place posted in the last few days, lists the photos in its Wikipedia article and in its
exact Wikimedia Commons category, and keeps only those that pass:
  1. the licence rules the pipeline uses for photos (CC0 or public domain, no attribution, no
     rights warning, JPEG or PNG);
  2. a size and shape check, and a check that the name does not say flag, map or logo;
  3. an AI check that it is a real photograph of this place (needs ANTHROPIC_API_KEY).
It saves small copies of the accepted photos so they can be looked at. It posts nothing.

Usage:  python scripts/check_images.py --state-dir DIR [--days 7] [--out-dir image-check]
        python scripts/check_images.py --place "Kaktovik, Alaska" --place "Nueva Germania"
        add --no-ai to skip the AI check
"""

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.parse
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anthropic
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import llm
from pipeline.candidate_selector import _HEADERS, _WP_API, _get
from pipeline.image_sourcer import _LICENSE_LABELS, _field, _plain, _unusable_reason
from pipeline.photo_filter import judge_photo, looks_like_not_a_photo, shape_problem

_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_THUMB_WIDTH = 800
# Most photos a place sends to the AI check, so one place with hundreds cannot run up the bill
_MAX_JUDGED = 12
_IMAGEINFO = {
    "prop": "imageinfo",
    "iiprop": "url|mime|size|extmetadata",
    "iiurlwidth": str(_THUMB_WIDTH),
    "format": "json",
    "formatversion": "2",
}


def photo_record(page: dict, source: str) -> dict | None:
    """One file as a record: whether it could be posted, and why not. None if it is no photo."""
    name = page.get("title", "").removeprefix("File:")
    info = (page.get("imageinfo") or [None])[0]
    if not info:
        return None
    meta = info.get("extmetadata", {})
    description = _plain(_field(meta, "ImageDescription"))
    if looks_like_not_a_photo(name, description[:100]):
        return None
    reason = _unusable_reason(page, info) or shape_problem(info.get("width"), info.get("height"))
    license_code = _field(meta, "License").lower()
    return {
        "name": name,
        "source": source,
        "width": info.get("width"),
        "height": info.get("height"),
        "usable": reason is None,
        "reason": reason,
        "license": _LICENSE_LABELS.get(license_code, _field(meta, "LicenseShortName") or "unknown"),
        "description": description[:140],
        "page_url": info.get("descriptionurl"),
        "thumb_url": info.get("thumburl") or info.get("url"),
        "mime": info.get("mime"),
        "image": None,
        "shows": None,
    }


async def _pages(client: httpx.AsyncClient, api: str, params: dict) -> list[dict]:
    resp = await _get(client, api, params={**params, **_IMAGEINFO})
    resp.raise_for_status()
    return resp.json().get("query", {}).get("pages", [])


async def photos_for(client: httpx.AsyncClient, title: str) -> list[dict]:
    """Photos for a place from its Wikipedia article and its exact Commons category.

    Only the category named exactly like the article is used. A shorter name ("Livingston")
    can be a different place entirely.
    """
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

    members = await _pages(
        client,
        _COMMONS_API,
        {
            "action": "query",
            "generator": "categorymembers",
            "gcmtitle": f"Category:{title}",
            "gcmtype": "file",
            "gcmlimit": "100",
        },
    )
    for page in members:
        # Files listed by Commons itself are hosted there; the pipeline's rule wants "shared"
        record = photo_record({**page, "imagerepository": "shared"}, "category")
        if record:
            found.setdefault(record["name"], record)
    return list(found.values())


async def check_with_ai(client: httpx.AsyncClient, place: str, photos: list[dict]) -> None:
    """Ask Claude whether each photo that passed the cheap checks is a real photo of the place.

    Updates the records in place. A photo that cannot be checked is not accepted.
    """
    candidates = [p for p in photos if p["usable"]]
    for photo in candidates[_MAX_JUDGED:]:
        photo.update(usable=False, reason="not checked (limit per place)")

    async def check(photo: dict) -> None:
        try:
            resp = await client.get(photo["thumb_url"], follow_redirects=True)
            resp.raise_for_status()
            photo["image"] = resp.content
            verdict = await judge_photo(
                place,
                resp.content,
                resp.headers.get("content-type", photo["mime"] or "image/jpeg").split(";")[0],
                photo["name"],
                photo["description"],
                photo["source"],
            )
        except (httpx.HTTPError, anthropic.APIError, llm.LLMRefusalError) as exc:
            photo.update(usable=False, reason=f"not checked ({type(exc).__name__})")
            return
        photo["shows"] = verdict.what_it_shows
        if not verdict.accepted:
            what = "not a photo" if not verdict.is_photo else f"place: {verdict.place_match}"
            photo.update(usable=False, reason=f"AI check ({what}): {verdict.reason}")

    await asyncio.gather(*(check(p) for p in candidates[:_MAX_JUDGED]))


def render(results: dict[str, list[dict]], max_listed: int = 6, ai_note: str = "") -> str:
    """The report as Markdown: a summary table, then each place's accepted photos."""
    lines = ["# Image check", ""]
    if ai_note:
        lines += [ai_note, ""]
    lines += ["| Place | Photos found | Accepted | Rejected |", "|---|---|---|---|"]
    for place, photos in results.items():
        usable = sum(p["usable"] for p in photos)
        lines.append(f"| {place} | {len(photos)} | {usable} | {len(photos) - usable} |")
    for place, photos in results.items():
        usable = [p for p in photos if p["usable"]]
        lines += ["", f"## {place}", ""]
        if not photos:
            lines.append("No photos found in the article or the exact Commons category.")
            continue
        for p in usable[:max_listed]:
            size = f"{p['width']}×{p['height']}" if p["width"] else "?"
            shows = p["shows"] or p["description"] or "no description"
            lines.append(
                f"- **{p['name']}** ({size}, {p['license']}, from the {p['source']})"
                f" — {shows} — {p['page_url']}"
            )
        if len(usable) > max_listed:
            lines.append(f"- … and {len(usable) - max_listed} more accepted")
        if not usable:
            lines.append("None accepted.")
        rejected = Counter(p["reason"].split(":")[0] for p in photos if not p["usable"])
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


async def save_thumbnails(
    client: httpx.AsyncClient, place: str, photos: list[dict], out_dir: Path, limit: int
) -> None:
    folder = out_dir / re.sub(r"[^a-z0-9]+", "-", place.lower()).strip("-")
    for i, photo in enumerate([p for p in photos if p["usable"]][:limit], start=1):
        data = photo["image"]
        if data is None:
            try:
                resp = await client.get(photo["thumb_url"], follow_redirects=True)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"Could not fetch {photo['name']}: {exc!r}", file=sys.stderr)
                continue
            data = resp.content
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", photo["name"])[-80:]
        (folder / f"{i:02d}-{safe}").write_bytes(data)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", type=Path, help="saved state to take recent places from")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--place", action="append", default=[], help="a Wikipedia title; repeatable")
    parser.add_argument("--out-dir", type=Path, default=Path("image-check"))
    parser.add_argument("--max-images", type=int, default=6, help="accepted photos saved per place")
    parser.add_argument("--no-ai", action="store_true", help="skip the AI check")
    args = parser.parse_args()

    places = args.place or (recent_places(args.state_dir, args.days) if args.state_dir else [])
    if not places:
        print("No places to check. Give --place, or --state-dir with recent posts.")
        return 1
    use_ai = not args.no_ai and bool(os.getenv("ANTHROPIC_API_KEY"))
    ai_note = "" if use_ai else "The AI check was skipped: photos are not confirmed to show the place."

    results: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
        for place in places:
            results[place] = await photos_for(client, place)
            if use_ai:
                await check_with_ai(client, place, results[place])
            await save_thumbnails(client, place, results[place], args.out_dir, args.max_images)
    print(render(results, args.max_images, ai_note), end="")
    if use_ai:
        # Tokens only: this log can be public, and dollars are private
        print(llm.usage_summary(show_cost=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
