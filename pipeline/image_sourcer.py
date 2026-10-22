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
# Only files with no conditions attached: CC0 or the public domain (Commons' machine
# licence codes). Attribution licences (CC BY, CC BY-SA) are refused even though a
# credit line could satisfy them: posting to X grants X a licence to sublicense that
# a CC licence does not let us pass on, and an attribution slip under the older CC
# versions ends the licence at once, which some rights holders bill for.
_NO_CONDITIONS_LICENSES = {"cc0", "pd"}
_LICENSE_LABELS = {"cc0": "CC0", "pd": "public domain"}


def _plain(html: str) -> str:
    """Commons metadata fields are HTML fragments; reduce to plain text."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


async def search_wikimedia(place_name: str, wikipedia_url: str) -> dict | None:
    """Find the article's lead image and its Commons licence details.

    Returns {url, credit, license, page_url} or None if no suitable image found.
    `credit` is the author's name, or None when Commons doesn't give one.
    Uses WIKIMEDIA_USER_AGENT from env for the API request header.

    A photo is accepted only if all of these hold, as stated by Commons' own metadata:
    - it is hosted on Wikimedia Commons, not uploaded locally to Wikipedia (local
      files can be public domain in the US only, or non-free);
    - its licence is CC0 or public domain, and no attribution is required;
    - it carries no rights warning (personality rights, trademark, and the like);
    - it is not marked non-free, and it is a JPEG or PNG (SVGs are usually flags and
      locator maps).
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

    page = pages[0] if pages else {}
    info = (page.get("imageinfo") or [None])[0]
    if not info:
        return None
    if reason := _unusable_reason(page, info):
        console.log(f"[image_sourcer] Lead image for {place_name} not used ({file_name}): {reason}")
        return None

    meta = info.get("extmetadata", {})
    license_code = _field(meta, "License").lower()
    # No credit is owed, but naming the photographer is good manners when Commons has a name
    credit = _field(meta, "Artist") or None
    if credit and len(credit) > _MAX_CREDIT_CHARS:
        credit = None  # a long Artist field is usually a description, not a name
    return {
        "url": info.get("thumburl") or info["url"],
        "credit": credit,
        "license": _LICENSE_LABELS[license_code],
        # Kept in the database as the record of where the photo came from
        "page_url": info.get("descriptionurl"),
    }


def _field(meta: dict, name: str) -> str:
    return _plain(str(meta.get(name, {}).get("value", "")))


def _unusable_reason(page: dict, info: dict) -> str | None:
    """Why a file must not be posted, or None if it is free of conditions."""
    meta = info.get("extmetadata", {})
    if page.get("imagerepository") != "shared":
        return "not hosted on Wikimedia Commons"
    if info.get("mime") not in _ALLOWED_MIME:
        return f"type {info.get('mime')}"
    if "NonFree" in meta:
        return "marked non-free"
    if _field(meta, "License").lower() not in _NO_CONDITIONS_LICENSES:
        return f"licence {_field(meta, 'LicenseShortName') or 'unknown'} is not CC0 or public domain"
    if _field(meta, "AttributionRequired").lower() != "false":
        return "attribution required"
    if restrictions := _field(meta, "Restrictions"):
        return f"rights warning: {restrictions}"
    return None


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
