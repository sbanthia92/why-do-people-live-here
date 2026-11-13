"""Tests for pipeline.facts_card: which facts reach the card, and drawing it."""

import io
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw

from pipeline import facts_card
from pipeline.content_generator import UnsupportedClaim
from pipeline.facts_card import KeyFact, KeyFacts, facts_card_for_candidate, select_facts

BRIEF = (
    "Land came from 96 villages [20]. The first blast furnace was lit in 1959 [W]. The 2011"
    " Census counted 27,458 vacant houses in Bhilai Nagar [83]. Rails made up to 2007 could"
    " circle the earth 7.5 times [W]. Milk cost $12.69 a gallon [72]."
)


def _facts(*pairs):
    return KeyFacts(facts=[KeyFact(figure=f, caption=c) for f, c in pairs])


async def _select(picked, unsupported=()):
    with (
        patch("pipeline.llm.parse", new=AsyncMock(return_value=picked)),
        patch(
            "pipeline.content_generator.check_facts", new=AsyncMock(return_value=list(unsupported))
        ) as check,
    ):
        return await select_facts("Bhilai", BRIEF), check


async def test_figures_that_are_not_in_the_brief_word_for_word_never_reach_the_card():
    picked = _facts(
        ("96", "villages gave up land"),
        ("1959", "first blast furnace lit"),
        ("27,458", "vacant houses in Bhilai Nagar (2011 census)"),
        ("27,000", "vacant houses"),  # rounded: not what the brief says
        ("many", "people moved in"),  # no number at all
    )

    facts, check = await _select(picked)

    assert [f["figure"] for f in facts] == ["96", "1959", "27,458"]
    # Only the survivors are sent to the fact check, each as a one-line claim
    assert check.call_args.args[2] == [
        "96 villages gave up land",
        "1959 first blast furnace lit",
        "27,458 vacant houses in Bhilai Nagar (2011 census)",
    ]


async def test_facts_the_fact_check_rejects_are_dropped_and_at_most_four_are_kept():
    picked = _facts(
        ("96", "villages"), ("1959", "furnace"), ("27,458", "houses"), ("7.5 times", "rails"),
        ("$12.69", "milk"),
    )
    rejected = [UnsupportedClaim(post=2, claim="1959 furnace", problem="brief says lit, not built")]

    facts, _ = await _select(picked, rejected)

    assert [f["figure"] for f in facts] == ["96", "27,458", "7.5 times", "$12.69"]


async def test_fewer_than_three_good_facts_means_no_card():
    two, check = await _select(_facts(("96", "villages"), ("1959", "furnace")))
    assert two is None
    check.assert_not_called()  # nothing worth paying to check

    rejected = [UnsupportedClaim(post=1, claim="x", problem="y")]
    three, _ = await _select(_facts(("96", "a"), ("1959", "b"), ("27,458", "c")), rejected)
    assert three is None


async def test_overlong_figures_and_captions_are_dropped():
    picked = _facts(
        ("96", "villages"), ("1959", "furnace"), ("27,458", "houses"),
        ("96 whole villages", "too long a figure"),
        ("7.5 times", "x" * 80),
    )
    facts, _ = await _select(picked)
    assert [f["figure"] for f in facts] == ["96", "1959", "27,458"]


def test_card_is_drawn_for_three_or_four_facts_and_saved_with_alt_text(tmp_path):
    four = [{"figure": str(n), "caption": f"caption number {n} " * 2} for n in range(4)]

    for facts in (four, four[:3]):
        card = facts_card_for_candidate("Bhilai", "company town · Asia", facts, tmp_path)
        image = Image.open(io.BytesIO(Path(card["path"]).read_bytes()))
        assert image.size == (1600, 900) and image.format == "PNG"
    assert card["path"].endswith("bhilai-facts.png")
    assert card["alt_text"].startswith("Key facts about Bhilai: 0 caption number 0")


def test_no_card_without_enough_facts(tmp_path):
    one = [{"figure": "96", "caption": "villages"}]
    assert facts_card_for_candidate("Bhilai", "", None, tmp_path) is None
    assert facts_card_for_candidate("Bhilai", "", one * 2, tmp_path) is None
    assert facts_card_for_candidate("Bhilai", "", one * 5, tmp_path) is None
    # Facts in a shape that cannot be drawn cost the card, not the post
    assert facts_card_for_candidate("Bhilai", "", [{"oops": 1}] * 3, tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_the_card_is_on_unless_switched_off(monkeypatch):
    monkeypatch.delenv("FACTS_CARD", raising=False)
    assert facts_card.enabled()
    monkeypatch.setenv("FACTS_CARD", "")  # how an unset repository variable arrives
    assert facts_card.enabled()
    monkeypatch.setenv("FACTS_CARD", "false")
    assert not facts_card.enabled()


async def test_a_figure_only_found_inside_a_longer_number_is_dropped():
    """Kivalina: "80%" passed because "80" sat inside "280" and "1980s"; the brief said "Eighty"."""
    brief = (
        "Eighty percent of homes lack toilets [91]. The harvest fell from 179-280 lb in the early"
        " 1980s [47]. Doughnuts cost $10.45 [79]. Some households hold up to 20 people [67]."
        " The 2020 census counted 444 residents [W]. Milk is $8.75, or 1,280 cents [77]."
    )
    picked = _facts(
        ("80%", "of homes lack toilets"),
        ("$10.45", "a box of doughnuts"),
        ("up to 20", "people in some households"),
        ("444", "residents (2020 census)"),
        ("$8.7", "milk"),  # a prefix of $8.75
        ("280", "lb harvest"),  # the end of a range still counts as a whole number
    )
    with (
        patch("pipeline.llm.parse", new=AsyncMock(return_value=picked)) as parse,
        patch("pipeline.content_generator.check_facts", new=AsyncMock(return_value=[])),
    ):
        facts = await select_facts("Kivalina", brief)

    assert [f["figure"] for f in facts] == ["$10.45", "up to 20", "444", "280"]
    # The picker is told today's date, so it can skip predictions that have passed
    assert parse.call_args.kwargs["user"].startswith("Today's date: ")
    assert "date has passed" in parse.call_args.kwargs["system"]


async def test_only_the_first_year_fact_is_kept():
    brief = BRIEF + " The plant was approved in 1955 [W] and expanded in 1967 [W]."
    picked = KeyFacts(
        facts=[
            KeyFact(figure="1959", caption="year the first blast furnace was lit"),
            KeyFact(figure="96", caption="villages gave up land"),
            KeyFact(figure="1955", caption="year the plant was approved"),
            KeyFact(figure="1967", caption="the plant was expanded", is_year=True),
            KeyFact(figure="27,458", caption="vacant houses in Bhilai Nagar (2011 census)"),
            KeyFact(figure="$12.69", caption="a gallon of milk"),
        ]
    )
    with (
        patch("pipeline.llm.parse", new=AsyncMock(return_value=picked)),
        patch("pipeline.content_generator.check_facts", new=AsyncMock(return_value=[])),
    ):
        facts = await select_facts("Bhilai", brief)

    assert [f["figure"] for f in facts] == ["1959", "96", "27,458", "$12.69"]


def test_every_caption_on_a_card_is_drawn_at_one_size():
    facts = [
        {"figure": "298", "caption": "coconut crabs per hectare in the Ramsar area (2010), a record density"},
        {"figure": "1971", "caption": "year the islanders were removed"},
        {"figure": "690", "caption": "nautical miles from Malé"},
        {"figure": "1,200", "caption": "contract workers in 2024"},
    ]
    sizes = set()
    real = ImageDraw.ImageDraw.text

    def spy(self, xy, text, font=None, fill=None, **kwargs):
        if fill == facts_card.CREAM and text != "DIEGO GARCIA":
            sizes.add(font.size)
        return real(self, xy, text, font=font, fill=fill, **kwargs)

    with patch.object(ImageDraw.ImageDraw, "text", new=spy):
        facts_card.render_facts_card("Diego Garcia", "", facts)

    # The first caption needs three lines, so all four are drawn at the smaller size
    assert sizes == {28}
