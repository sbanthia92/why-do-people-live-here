"""Decide which freely licensed photos are worth posting for a place.

Licence rules live in image_sourcer. These are the other checks: is the file big enough and the
right shape, is it a photo at all (not a flag, map or logo), and does it really show this place.
"""

import base64
import re
from typing import Literal

from pydantic import BaseModel, Field

from pipeline import llm

# X shows a picture best at roughly this size; smaller files look blurry, thin strips get cropped
MIN_WIDTH, MIN_HEIGHT = 800, 500
MIN_ASPECT, MAX_ASPECT = 0.6, 2.2  # width / height: from a tall portrait to a wide panorama

# Words that mark a flag, map, logo or drawing rather than a photograph, in several languages
_NOT_A_PHOTO = re.compile(
    r"(?<![^\W\d_])("
    r"flag|flagge|flaga|bandera|drapeau|bandiera|vlag|prapor|прапор|флаг|"
    r"coat of arms|gerb|герб|wappen|escudo|blason|stemma|"
    r"map|maps|mapa|karte|carte|locator|location map|"
    r"icon|logo|symbol|emblem|blank|diagram|chart|graph|globe"
    r")(?![^\W\d_])",
    re.IGNORECASE,
)


def shape_problem(width: int | None, height: int | None) -> str | None:
    """Why a file is the wrong size or shape to post, or None if it is fine."""
    if not width or not height:
        return "size unknown"
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return f"too small ({width}x{height}, need at least {MIN_WIDTH}x{MIN_HEIGHT})"
    ratio = width / height
    if not MIN_ASPECT <= ratio <= MAX_ASPECT:
        return f"wrong shape ({width}x{height})"
    return None


def looks_like_not_a_photo(*texts: str) -> bool:
    """True if a file name or description names a flag, map, logo or similar."""
    # File names may use underscores for spaces
    return any(_NOT_A_PHOTO.search(text.replace("_", " ")) for text in texts if text)


class PhotoVerdict(BaseModel):
    # The reason is written first, so the verdict follows from it
    reason: str = Field(description="One sentence: the evidence for the verdict.")
    is_photo: bool = Field(
        description="A real photograph, not a map, flag, coat of arms, icon, diagram, chart, "
        "logo, screenshot or drawing."
    )
    place_match: Literal["yes", "no", "unsure"] = Field(
        description="Whether it shows this place: yes only if the evidence says so."
    )
    what_it_shows: str = Field(description="A few words on what is in the picture.")

    @property
    def accepted(self) -> bool:
        return self.is_photo and self.place_match == "yes"


_SYSTEM = """\
You check photos for an account that explains why people live in remote places. For one \
photo you get the place it must show, the file's name and description, and where the file came \
from. Decide two things.

1. is_photo: is it a real photograph? Maps, flags, coats of arms, icons, charts, diagrams, \
logos, screenshots and drawings are not.
2. place_match: does it show THIS place? Answer yes only when the file name, description or the \
picture itself says so. Answer no when the evidence names a different place, including one with a \
similar name (a town in another country), or when the photo is a generic illustration of a type of \
place. Answer unsure when nothing says where it is. A file from the place's own category on \
Wikimedia Commons is good evidence; a picture that merely appears in the place's Wikipedia \
article is weaker, because articles sometimes illustrate a place with a photo of another one."""

_SOURCES = {
    "category": "the place's own Wikimedia Commons category",
    "article": "the place's Wikipedia article",
}


async def judge_photo(
    place: str,
    image: bytes,
    media_type: str,
    file_name: str,
    description: str,
    source: str,
    model: str | None = None,
) -> PhotoVerdict:
    """Ask Claude whether a photo is a real photograph of `place` (on PHOTO_MODEL unless told)."""
    question = (
        f"Place: {place}\n"
        f"File name: {file_name}\n"
        f"Description: {description or '(none)'}\n"
        f"The file came from {_SOURCES.get(source, source)}."
    )
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image).decode(),
            },
        },
        {"type": "text", "text": question},
    ]
    return await llm.parse(
        system=_SYSTEM,
        user=content,
        output_format=PhotoVerdict,
        effort="low",
        model=model or llm.PHOTO_MODEL,
    )
