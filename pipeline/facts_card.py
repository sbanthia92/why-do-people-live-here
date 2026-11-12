"""Draw a "key facts" card for a place: its three or four most striking figures.

The card sits next to the map card on the first post. Every figure on it is public, so each
one passes three checks before it is drawn: the model picks it from the research brief only,
its digits must appear in the brief word for word, and the same fact check the posts go
through must find it supported. A place with fewer than three facts left gets no card.
"""

import io
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw
from pydantic import BaseModel, Field
from rich.console import Console

from pipeline import llm
from pipeline.map_card import (
    CREAM,
    MUTED,
    NAVY,
    ORANGE,
    RED,
    RING,
    SEA,
    YELLOW,
    _fit,
    _font,
    _wrap,
)

console = Console()

WIDTH, HEIGHT = 1600, 900
MIN_FACTS, MAX_FACTS = 3, 4
# Asked for more than fit, since some will not survive the checks
_ASKED = 6
_MAX_FIGURE_CHARS = 12
_MAX_CAPTION_CHARS = 70
_ACCENTS = (YELLOW, ORANGE, RED, YELLOW)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# A card of dates reads like a timeline: one year at most, the rest counts, prices, distances
MAX_YEAR_FACTS = 1
_BARE_YEAR = re.compile(r"(1\d{3}|20\d{2})")


def enabled() -> bool:
    """Whether posts carry a key-facts card (on unless FACTS_CARD=false)."""
    # An unset repository variable arrives as an empty string, which also means "on"
    return (os.getenv("FACTS_CARD") or "true").strip().lower() in {"1", "true", "yes"}


class KeyFact(BaseModel):
    figure: str = Field(
        description=(
            "The number, as it will be printed large: '96', '$12.69', 'up to 80', '7.5 times',"
            f" '-61 °F'. At most {_MAX_FIGURE_CHARS} characters. Keep the brief's qualifier"
            " ('up to', 'about')."
        )
    )
    caption: str = Field(
        description=(
            "What the figure counts, read straight after it, with the area and year when the"
            f" brief gives them: 'vacant houses in Bhilai Nagar (2011 census)'. At most"
            f" {_MAX_CAPTION_CHARS} characters, no full stop."
        )
    )
    is_year: bool = Field(
        default=False,
        description="True when the figure is a calendar year or a date, not a quantity.",
    )


class KeyFacts(BaseModel):
    facts: list[KeyFact] = Field(description="The most striking facts first.")


_SYSTEM = f"""\
You choose the figures for a "key facts" card about a remote or unusual settlement. The card is \
an image posted publicly beside a map, so a wrong figure is a public error.

From the research brief, pick up to {_ASKED} facts that each have a number and would make a \
stranger stop: a price, a count, a distance, a temperature, a year something began. Put the \
most striking first.

Rules:
- Use only figures the brief states, copied exactly. Do not calculate, convert or round.
- Skip any figure the brief marks as disputed, or for which it gives conflicting values.
- Keep the brief's qualifiers ("up to", "about", "one source says") and say what area and year \
a figure covers when the brief gives them.
- Skip predictions, forecasts, plans and deadlines whose date has passed or is about to: "to be \
flooded by 2025" is not a fact once 2025 is over. Prefer what is true now, or settled history.
- At most {MAX_YEAR_FACTS} fact may be a year or a date. Prefer quantities: counts, prices, \
distances, temperatures, populations.
- Each fact must make sense on its own, without the others.
- If the brief has fewer good figures, return fewer. Never pad."""


def _is_year(fact: KeyFact) -> bool:
    """The model's own flag, or a bare year whose caption says so ("1971", "year the ...")."""
    bare = _BARE_YEAR.fullmatch(fact.figure.strip()) is not None
    return fact.is_year or (bare and fact.caption.strip().lower().startswith("year"))


def _digits_in_brief(figure: str, brief: str) -> bool:
    """Every number in the figure appears in the brief as a whole number, exactly as written.

    "80" is not found inside "280" or "1980s". A figure the brief spells out in words
    ("eighty percent") is not found either, and is dropped: the safe side.
    """
    numbers = _NUMBER.findall(figure)
    return bool(numbers) and all(
        re.search(rf"(?<![\d.,]){re.escape(number)}(?!\d|[.,]\d)", brief) for number in numbers
    )


async def select_facts(place_name: str, raw_research: str) -> list[dict] | None:
    """The facts for a place's card as [{"figure", "caption"}], or None if too few hold up."""
    # Imported here: content_generator imports this module
    from pipeline.content_generator import check_facts

    picked = await llm.parse(
        system=_SYSTEM,
        user=(
            f"Today's date: {datetime.now(UTC):%-d %B %Y}\nPlace: {place_name}\n\n"
            f"<research_brief>\n{raw_research}\n</research_brief>"
        ),
        output_format=KeyFacts,
    )
    facts, years = [], 0
    for f in picked.facts:
        if (
            len(f.figure.strip()) > _MAX_FIGURE_CHARS
            or len(f.caption.strip()) > _MAX_CAPTION_CHARS
            or not _digits_in_brief(f.figure, raw_research)
        ):
            continue
        if _is_year(f):
            # The first year the model listed is the one it found most striking
            years += 1
            if years > MAX_YEAR_FACTS:
                continue
        facts.append({"figure": f.figure.strip(), "caption": f.caption.strip().rstrip(".")})
    if len(facts) < MIN_FACTS:
        return None
    # The same check the posts go through: each fact is read as a one-line post
    unsupported = await check_facts(
        place_name, raw_research, [f"{f['figure']} {f['caption']}" for f in facts]
    )
    rejected = {claim.post for claim in unsupported}
    facts = [f for i, f in enumerate(facts, start=1) if i not in rejected][:MAX_FACTS]
    if len(facts) < MIN_FACTS:
        console.log(f"[facts_card] {place_name}: too few facts passed the check; no card")
        return None
    return facts


def alt_text(place_name: str, facts: list[dict]) -> str:
    listed = "; ".join(f"{f['figure']} {f['caption']}" for f in facts)
    return f"Key facts about {place_name}: {listed}."


def render_facts_card(place_name: str, context: str, facts: list[dict]) -> bytes:
    """Draw the card as PNG bytes: four facts in a 2x2 grid, or three in rows."""
    image = Image.new("RGB", (WIDTH, HEIGHT), NAVY)
    draw = ImageDraw.Draw(image)
    left, right = 80, WIDTH - 80

    title = _fit(draw, place_name.upper(), right - left, 64)
    draw.text((left, 108), place_name.upper(), font=title, fill=CREAM, anchor="ls")
    draw.rectangle([left, 128, left + 96, 134], fill=ORANGE)
    if context:
        draw.text((left, 178), context, font=_font(30), fill=MUTED, anchor="ls")

    top, bottom, gap = 215, 815, 32
    columns = 2 if len(facts) == 4 else 1
    rows = len(facts) // columns
    tile_w = (right - left - gap * (columns - 1)) // columns
    tile_h = (bottom - top - gap * (rows - 1)) // rows
    # One caption size for the whole card: the smaller one if any caption needs it
    caption_width = tile_w - 96 if columns == 2 else tile_w - 520 - 96
    size = 34
    if any(len(_wrap(draw, f["caption"], _font(size), caption_width)) > 2 for f in facts):
        size = 28
    line_height = int(size * 1.3)
    for i, fact in enumerate(facts):
        x = left + (i % columns) * (tile_w + gap)
        y = top + (i // columns) * (tile_h + gap)
        accent = _ACCENTS[i % len(_ACCENTS)]
        draw.rounded_rectangle([x, y, x + tile_w, y + tile_h], radius=22, fill=SEA, outline=RING, width=2)
        draw.rounded_rectangle([x, y, x + 12, y + tile_h], radius=6, fill=accent)
        inner = x + 48
        if columns == 2:
            # The figure above its caption
            figure = _fit(draw, fact["figure"], tile_w - 96, 88)
            draw.text((inner, y + 40), fact["figure"], font=figure, fill=accent, anchor="lt")
            caption_left, caption_top = inner, y + 150
        else:
            # The figure on the left, its caption beside it
            figure_width = 520
            figure = _fit(draw, fact["figure"], figure_width - 40, 84)
            draw.text((inner, y + tile_h / 2), fact["figure"], font=figure, fill=accent, anchor="lm")
            caption_left, caption_top = inner + figure_width, None
        lines = _wrap(draw, fact["caption"], _font(size), caption_width)[:3]
        if caption_top is None:
            caption_top = y + tile_h / 2 - line_height * len(lines) / 2
        for n, line in enumerate(lines):
            draw.text(
                (caption_left, caption_top + n * line_height), line, font=_font(size), fill=CREAM, anchor="lt"
            )

    draw.text(
        (right, HEIGHT - 34), "Why do people live here?", font=_font(28, bold=True), fill=MUTED, anchor="rs"
    )
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def facts_card_for_candidate(
    place_name: str, context: str, facts: list[dict] | None, directory: Path
) -> dict | None:
    """Draw and save the card. Returns {"path", "alt_text"}, or None without enough facts.

    Never raises: a post without the card is better than no post.
    """
    if not facts or not MIN_FACTS <= len(facts) <= MAX_FACTS:
        return None
    try:
        png = render_facts_card(place_name, context, facts)
        directory.mkdir(parents=True, exist_ok=True)
        slug = "".join(c if c.isalnum() else "-" for c in place_name.lower()).strip("-")
        path = directory / f"{slug}-facts.png"
        path.write_bytes(png)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        console.log(f"[facts_card] Could not draw the facts card for {place_name}: {exc!r}")
        return None
    return {"path": str(path), "alt_text": alt_text(place_name, facts)}
