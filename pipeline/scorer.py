"""Score a researched candidate across five dimensions and decide post format."""

from typing import Literal


PostFormat = Literal["single", "thread", "skip"]


async def score_candidate(
    db_pool,
    candidate_id: int,
) -> dict:
    """Ask Claude to score the candidate on all five dimensions and return the result dict.

    Dimensions: named_origin, economic_engine, contrast_surprise, vivid_detail, corroboration.
    Each is 0–2. total_weighted is computed here before persisting.
    """
    pass


def compute_weighted_total(scores: dict[str, int]) -> float:
    """Compute the weighted total score from individual dimension scores.

    Weights TBD; placeholder uses equal weighting.
    """
    pass


def decide_post_format(total_weighted: float) -> PostFormat:
    """Decide whether to post a single tweet, a thread, or skip based on total score."""
    pass


async def persist_scores(db_pool, candidate_id: int, scores: dict) -> None:
    """Write a row to the scores table and update candidates.status to 'scored'."""
    pass
