"""Source a suitable Wikimedia Commons image for the candidate place."""

import re
import urllib.parse

import httpx
from rich.console import Console

from pipeline.candidate_selector import _HEADERS, _WP_API, _get

console = Console()

_THUMB_WIDTH = 1600
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # X's limit for photos
_ALLOWED_MIME = {"image/jpeg", "image/png"}
_MAX_CREDIT_CHARS = 100
# Licences whose terms a post can meet with a credit line: name the author, name and
# link the licence, link the source. Others on Commons (GFDL, for one) require
# reproducing the full licence text, which a post can't do.
_REUSABLE_LICENSE_PREFIXES = ("cc by", "cc0", "public domain", "pd")


def _plain(html: str) -> str:
    """Commons metadata fields are HTML fragments; reduce to plain text."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


async def search_wikimedia(place_name: str, wikipedia_url: str) -> dict | None:
    """Find the article's lead image and its Commons licence details.

    Returns {url, credit, license, license_url, page_url} or None if no suitable image
    found. Uses WIKIMEDIA_USER_AGENT from env for the API request header.
    Only photos under a Creative Commons attribution licence, CC0 or the public domain
    are accepted: non-free (fair-use) files, other licences, and SVGs (usually flags
    and locator maps) are rejected.
    """
    title = urllib.parse.unquote(wikipedia_url.rstrip("/").split("/wiki/")[-1]).replace("_", " ")
    async with httpx.AsyncClient(timeout=15.0, headers=_HEADERS) as client:
        resp = await _get(
            client,
            _WP_API,
            params={
                "action": "query",
                "prop": "pageimages",
                "piprop": "name",
                "titles": title,
                "redirects": "1",
                "format": "json",
                "formatversion": "2",
            },
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", [])
        file_name = pages[0].get("pageimage") if pages else None
        if not file_name:
            console.log(f"[image_sourcer] No lead image for {place_name}")
            return None

        resp = await _get(
            client,
            _WP_API,
            params={
                "action": "query",
                "prop": "imageinfo",
                "iiprop": "url|mime|extmetadata",
                "iiurlwidth": str(_THUMB_WIDTH),
                "titles": f"File:{file_name}",
                "format": "json",
                "formatversion": "2",
            },
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", [])

    info = (pages[0].get("imageinfo") or [None])[0] if pages else None
    if not info:
        return None
    meta = info.get("extmetadata", {})
    license_name = _plain(meta.get("LicenseShortName", {}).get("value", ""))
    reusable = license_name.lower().startswith(_REUSABLE_LICENSE_PREFIXES)
    if info.get("mime") not in _ALLOWED_MIME or "NonFree" in meta or not reusable:
        console.log(f"[image_sourcer] Lead image for {place_name} not usable ({file_name})")
        return None

    credit = _plain(meta.get("Artist", {}).get("value", "")) or "Unknown author"
    if len(credit) > _MAX_CREDIT_CHARS:
        credit = credit[: _MAX_CREDIT_CHARS - 1].rstrip() + "…"
    return {
        "url": info.get("thumburl") or info["url"],
        "credit": credit,
        "license": license_name,
        "license_url": _plain(meta.get("LicenseUrl", {}).get("value", "")) or None,
        # The file's Commons page: the "source" a CC attribution has to point to
        "page_url": info.get("descriptionurl"),
    }


async def validate_image(image_url: str) -> bool:
    """Check that an image URL is reachable and within acceptable file-size limits."""
    try:
        async with (
            httpx.AsyncClient(timeout=15.0, headers=_HEADERS) as client,
            client.stream("GET", image_url, follow_redirects=True) as resp,
        ):
            if resp.status_code != 200:
                return False
            if resp.headers.get("content-type", "").split(";")[0] not in _ALLOWED_MIME:
                return False
            size = int(resp.headers.get("content-length", "0"))
            return 0 < size <= _MAX_IMAGE_BYTES
    except httpx.HTTPError:
        return False


async def get_image_for_candidate(place_name: str, wikipedia_url: str) -> dict | None:
    """Top-level helper: search, validate, and return image metadata or None.

    Never raises — a post without an image is better than no post.
    """
    try:
        image = await search_wikimedia(place_name, wikipedia_url)
    except httpx.HTTPError as exc:
        console.log(f"[image_sourcer] Image lookup failed for {place_name}: {exc!r}")
        return None
    if image and await validate_image(image["url"]):
        return image
    return None
