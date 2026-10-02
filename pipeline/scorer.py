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


class Scores(BaseModel):
    named_origin: DimensionScore
    economic_engine: DimensionScore
    contrast_surprise: DimensionScore
    vivid_detail: DimensionScore
    corroboration: DimensionScore
    justifications: dict[str, str] = Field(
        description="One short reason per dimension, keyed by dimension name."
    )


_SCORE_SYSTEM = """\
You are scoring whether a settlement makes a good "Why do people live here?" post. \
Judge only the research brief you are given. Each sentence in it carries source tags: \
[W] is Wikipedia, [1], [2], ... are independent web sources.

Score each dimension 0, 1 or 2:

named_origin — Is there a specific founding story?
  2: a named person, group or event and a date or period. 1: a general origin with no \
specifics. 0: origin unknown or absent.
economic_engine — Is it clear what sustains (or sustained) people there?
  2: a specific, explained livelihood. 1: mentioned only vaguely. 0: not addressed.
contrast_surprise — Would a reader be surprised that people live here?
  2: a sharp tension between the place's hostility or oddity and the fact of living \
there. 1: mildly unusual. 0: an ordinary town.
vivid_detail — Are there concrete, memorable specifics (numbers, objects, routines)?
  2: several. 1: one or two. 0: only generalities.
corroboration — Are the key claims backed by more than one source?
  2: the main origin and livelihood claims carry a web-source tag as well as [W]. \
1: some independent support, or only minor claims corroborated. 0: everything rests \
on [W] alone, or sources contradict each other on the main claims.

Be strict: a 2 has to be earned by what is written in the brief, not by what you know \
about the place. Give one short justification per dimension, quoting or pointing to \
the part of the brief that decided the score."""


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


async def score_candidate(db_pool, candidate_id: int) -> dict:
    """Ask Claude to score the candidate on all five dimensions and return the result dict.

    Dimensions: named_origin, economic_engine, contrast_surprise, vivid_detail, corroboration.
    Each is 0–2. total_weighted and post_format are computed here before persisting.
    The feed promises source-verified facts, so an uncorroborated candidate (0) is
    skipped whatever its total.
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
    scores["post_format"] = (
        "skip" if scores["corroboration"] == 0 else decide_post_format(scores["total_weighted"])
    )
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
