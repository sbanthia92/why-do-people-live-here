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
        add --no-ai to skip the AI check, or --compare to also run the stronger model and see
        where the cheap one differs
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

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import llm
from pipeline.candidate_selector import _HEADERS
from pipeline.photos import _MAX_JUDGED, check_with_ai, photo_record, photos_for  # noqa: F401


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
    lines += _comparison(results)
    return "\n".join(lines) + "\n"


def _comparison(results: dict[str, list[dict]]) -> list[str]:
    """How the cheap model's decisions compare with the stronger model's, if both ran."""
    both = [(place, p) for place, photos in results.items() for p in photos if p["reference"]]
    if not both:
        return []

    def cheap(photo: dict) -> bool:
        """Whether the cheap model accepted it (only photos it judged are in `both`)."""
        return photo["reason"] is None or not photo["reason"].startswith("AI check")

    agree = sum(cheap(p) == p["reference"][0] for _, p in both)
    lines = ["", "## Cheap model against the stronger one", ""]
    lines.append(f"Both judged {len(both)} photos and agreed on {agree}.")
    risky = [(pl, p) for pl, p in both if cheap(p) and not p["reference"][0]]
    lost = [(pl, p) for pl, p in both if not cheap(p) and p["reference"][0]]
    lines.append(
        f"Accepted by the cheap model but rejected by the stronger one: {len(risky)}"
        " (a wrong photo could get through)."
    )
    lines += [f"- {pl}: **{p['name']}** — {p['reference'][1]}" for pl, p in risky]
    lines.append(
        f"Rejected by the cheap model but accepted by the stronger one: {len(lost)}"
        " (a good photo would be lost)."
    )
    lines += [f"- {pl}: **{p['name']}** — {p['reason']}" for pl, p in lost]
    return lines


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
    parser.add_argument(
        "--compare",
        action="store_true",
        help="also run the stronger model on the same photos and show where the two differ",
    )
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
                await check_with_ai(client, place, results[place], args.compare)
            await save_thumbnails(client, place, results[place], args.out_dir, args.max_images)
    print(render(results, args.max_images, ai_note), end="")
    if use_ai:
        # Tokens only: this log can be public, and dollars are private
        print(llm.usage_summary(show_cost=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
