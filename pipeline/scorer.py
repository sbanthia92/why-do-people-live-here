"""Score a researched candidate across five dimensions and decide post format."""

from typing import Literal

from pydantic import BaseModel, Field
from rich.console import Console

from pipeline import llm

console = Console()

PostFormat = Literal["single", "thread", "skip"]
DimensionScore = Literal[0, 1, 2]

DIMENSIONS = (
    "named_origin",
    "economic_engine",
    "contrast_surprise",
    "vivid_detail",
    "corroboration",
)
# Equal weighting for now; tune here. Max total = 2 * sum(weights) = 10.
WEIGHTS: dict[str, float] = dict.fromkeys(DIMENSIONS, 1.0)
THREAD_MIN_TOTAL = 8.0
SINGLE_MIN_TOTAL = 5.0


class Justifications(BaseModel):
    """One short reason per dimension.

    Named fields, not a free-form dict: structured output can only fill keys the
    schema declares, and an open dict comes back empty.
    """

    named_origin: str
    economic_engine: str
    contrast_surprise: str
    vivid_detail: str
    corroboration: str


class Scores(BaseModel):
    # Declared first so the reasons are written before the numbers they justify
    justifications: Justifications = Field(
        description="For each dimension, the passage in the brief that decides its score."
    )
    named_origin: DimensionScore
    economic_engine: DimensionScore
    contrast_surprise: DimensionScore
    vivid_detail: DimensionScore
    corroboration: DimensionScore


_SCORE_SYSTEM = """\
You are scoring whether a settlement makes a good "Why do people live here?" post. \
Judge only the research brief you are given. Each sentence in it carries source tags: \
[W] is Wikipedia, [1], [2], ... are independent web sources.

The scores decide what gets published: the best places become threads, decent ones a \
single post, and the rest are dropped. That only works if the scores spread out. A \
well-researched brief is not the same as a strong story, and every brief you see will \
be well researched. Score the story.

Score each dimension 0, 1 or 2. A 1 is the normal score for a solid brief. Give a 2 \
only when the brief meets every part of the test for it, and a 0 without hesitation \
when the test for 1 is not met.

named_origin — Is there a specific founding story?
  2: a named person or group, a date, and the stated reason they came — all three.
  1: an origin with one or two of those, or a gradual settlement with no founding moment.
  0: origin unknown, or not in the brief.
economic_engine — Is it clear what people there live on?
  2: the brief explains, with specifics, how residents make a living today (for an \
abandoned place: what they lived on, and why it ended).
  1: the livelihood is named but not explained, or only the historical one is covered.
  0: not addressed.
contrast_surprise — Would a general reader ask "why would anyone live there?"
  2: living there today means enduring something most people would not — extreme \
cold, heat or isolation, no road out, a border that cuts the town off — and the brief \
shows it.
  1: the place is unusual, or its past was harsh, but life there now is broadly normal.
  0: an ordinary town or city. A large, functioning city scores 0 or 1 however dramatic \
its history.
vivid_detail — Are there concrete details a reader would repeat to someone else?
  2: at least three details that are specific and out of the ordinary (a price, an \
object, a rule, a routine). Population figures, dates and distances do not count.
  1: one or two such details.
  0: only generalities and statistics.
corroboration — Are the key claims backed by more than one source?
  2: both the origin claim and the livelihood claim carry a web-source tag as well as \
[W], and the sources do not contradict each other on them.
  1: only one of those two is independently supported, or only minor claims are.
  0: everything rests on [W] alone, or sources contradict each other on the main claims.

Score from what is written in the brief, not from what you know about the place. For \
each dimension give one short justification that quotes or points to the passage that \
decided the score — for a 2, the passage that meets the test."""


def compute_weighted_total(scores: dict[str, int]) -> float:
    """Compute the weighted total score from individual dimension scores."""
    return float(sum(WEIGHTS[d] * scores[d] for d in DIMENSIONS))


def decide_post_format(total_weighted: float) -> PostFormat:
    """Decide whether to post a single tweet, a thread, or skip based on total score."""
    if total_weighted >= THREAD_MIN_TOTAL:
        return "thread"
    if total_weighted >= SINGLE_MIN_TOTAL:
        return "single"
    return "skip"


def post_format_for(scores: dict) -> PostFormat:
    """Post format for a full set of scores: the total, adjusted by two overriding rules.

    - The feed promises source-verified facts, so a candidate with no independent
      corroboration is skipped whatever its total.
    - The account's premise is surprise that anyone lives there, so an unsurprising
      place gets a single post at most, however well documented.
    """
    if scores["corroboration"] == 0:
        return "skip"
    post_format = decide_post_format(scores["total_weighted"])
    if post_format == "thread" and scores["contrast_surprise"] == 0:
        return "single"
    return post_format


async def score_candidate(db_pool, candidate_id: int) -> dict:
    """Ask Claude to score the candidate on all five dimensions and return the result dict.

    Dimensions: named_origin, economic_engine, contrast_surprise, vivid_detail, corroboration.
    Each is 0–2. total_weighted and post_format are computed here before persisting.
    """
    row = await db_pool.fetchrow(
        """
        SELECT c.place_name, r.raw_research
        FROM candidates c
        JOIN research r ON r.candidate_id = c.id
        WHERE c.id = $1
        ORDER BY r.researched_at DESC
        LIMIT 1
        """,
        candidate_id,
    )
    parsed = await llm.parse(
        system=_SCORE_SYSTEM,
        user=f"Place: {row['place_name']}\n\n<research_brief>\n{row['raw_research']}\n</research_brief>",
        output_format=Scores,
    )
    scores = parsed.model_dump()
    scores["total_weighted"] = compute_weighted_total(scores)
    scores["post_format"] = post_format_for(scores)
    await persist_scores(db_pool, candidate_id, scores)
    console.log(
        f"[scorer] {row['place_name']}: total {scores['total_weighted']:.1f}"
        f" → {scores['post_format']}"
    )
    return scores


async def persist_scores(db_pool, candidate_id: int, scores: dict) -> None:
    """Write a row to the scores table and update candidates.status.

    Status becomes 'scored', or 'skipped' when the post format is 'skip'.
    """
    await db_pool.execute(
        """
        INSERT INTO scores
            (candidate_id, named_origin, economic_engine, contrast_surprise, vivid_detail,
             corroboration, total_weighted, justifications, post_format)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        candidate_id,
        *(scores[d] for d in DIMENSIONS),
        scores["total_weighted"],
        scores["justifications"],
        scores["post_format"],
    )
    await db_pool.execute(
        "UPDATE candidates SET status = $2, updated_at = now() WHERE id = $1",
        candidate_id,
        "skipped" if scores["post_format"] == "skip" else "scored",
    )
