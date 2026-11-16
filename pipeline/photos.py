"""Find photos of a place that can be posted: free of conditions, and really of that place.

Photos come from the place's Wikipedia article and the Wikimedia Commons category named exactly
like it. A photo is used only if Commons labels it CC0 or public domain with no attribution
required and no rights warning (image_sourcer), it is large enough and the right shape, its
name does not say flag, map or logo, and an AI check says it is a real photograph of this place
with no recognisable person in it (photo_filter).
"""

import asyncio
import os
import urllib.parse
from pathlib import Path

import anthropic
import httpx
from rich.console import Console

from pipeline import llm
from pipeline.candidate_selector import _HEADERS, _WP_API, _get
from pipeline.image_sourcer import _LICENSE_LABELS, _field, _plain, _unusable_reason
from pipeline.photo_filter import judge_photo, looks_like_not_a_photo, shape_problem

console = Console()

# Photos added to a post after the map card (X allows four images in all)
MAX_PHOTOS = 3

_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Wide enough to post as it is, and what the AI check looks at
_THUMB_WIDTH = 1200
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
        "reference": None,
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


async def check_with_ai(
    client: httpx.AsyncClient, place: str, photos: list[dict], compare: bool = False
) -> None:
    """Ask Claude whether each photo that passed the cheap checks is a real photo of the place.

    Updates the records in place. A photo that cannot be checked is not accepted. With `compare`,
    the stronger reference model judges the same photos too, to show where the cheap one differs
    (its answer is recorded, but the cheap model's decides).
    """
    candidates = [p for p in photos if p["usable"]]
    for photo in candidates[_MAX_JUDGED:]:
        photo.update(usable=False, reason="not checked (limit per place)")

    async def check(photo: dict) -> None:
        try:
            resp = await client.get(photo["thumb_url"], follow_redirects=True)
            resp.raise_for_status()
            photo["image"] = resp.content
            args = (
                place,
                resp.content,
                resp.headers.get("content-type", photo["mime"] or "image/jpeg").split(";")[0],
                photo["name"],
                photo["description"],
                photo["source"],
            )
            verdict = await judge_photo(*args)
            if compare:
                try:
                    reference = await judge_photo(*args, model=llm.PHOTO_REFERENCE_MODEL)
                    photo["reference"] = (reference.accepted, reference.reason)
                except (anthropic.APIError, llm.LLMRefusalError):
                    photo["reference"] = None  # no second opinion for this one
        except (httpx.HTTPError, anthropic.APIError, llm.LLMRefusalError) as exc:
            photo.update(usable=False, reason=f"not checked ({type(exc).__name__})")
            return
        photo["shows"] = verdict.what_it_shows
        if not verdict.accepted:
            what = (
                "not a photo"
                if not verdict.is_photo
                else f"place: {verdict.place_match}"
                if verdict.place_match != "yes"
                else "a recognisable person"
            )
            photo.update(usable=False, reason=f"AI check ({what}): {verdict.reason}")

    await asyncio.gather(*(check(p) for p in candidates[:_MAX_JUDGED]))


def enabled() -> bool:
    """Whether posts carry photos when a place has usable ones (on unless POST_IMAGES=false)."""
    # An unset repository variable arrives as an empty string, which also means "on"
    return (os.getenv("POST_IMAGES") or "true").strip().lower() in {"1", "true", "yes"}


async def photos_for_candidate(
    place_name: str, wikipedia_url: str, directory: Path, limit: int = MAX_PHOTOS
) -> list[dict]:
    """Up to `limit` checked photos of a place, saved to `directory`.

    Returns [{"path", "alt_text", "page_url", "license"}], Commons category photos first.
    Never raises: a post without photos is better than no post.
    """
    if limit <= 0:
        return []
    title = urllib.parse.unquote(wikipedia_url.rstrip("/").split("/wiki/")[-1]).replace("_", " ")
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
            photos = await photos_for(client, title)
            # The category is the stronger evidence that a photo shows this place
            photos.sort(key=lambda p: p["source"] != "category")
            await check_with_ai(client, place_name, photos)
    except (httpx.HTTPError, anthropic.APIError, ValueError, KeyError) as exc:
        console.log(f"[photos] Photo lookup failed for {place_name}: {exc!r}")
        return []
    accepted = [p for p in photos if p["usable"] and p["image"]][:limit]
    slug = "".join(c if c.isalnum() else "-" for c in place_name.lower()).strip("-")
    saved = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for i, photo in enumerate(accepted, start=1):
            extension = "png" if photo["mime"] == "image/png" else "jpg"
            path = directory / f"{slug}-photo-{i}.{extension}"
            path.write_bytes(photo["image"])
            saved.append(
                {
                    "path": str(path),
                    "alt_text": f"Photo of {place_name}: {photo['shows'] or photo['description']}"[:420],
                    "page_url": photo["page_url"],
                    "license": photo["license"],
                }
            )
    except OSError as exc:
        console.log(f"[photos] Could not save photos for {place_name}: {exc!r}")
    console.log(f"[photos] {place_name}: {len(photos)} found, {len(saved)} used")
    return saved
