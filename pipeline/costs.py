"""What each place costs to post: Claude tokens, web search fees and X posting.

Costs are added to an in-memory ledger as they happen, keyed by place, and written to the
`costs` table when a stage finishes. A place's cost can span several days (researched one
day, posted another), so it is kept per place and summed in the weekly report.
"""

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

# The place whose work is running. Set per task, so places researched in parallel stay apart.
current_candidate: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_candidate", default=None
)

# US dollars per post on X's pay-per-use plan, as in the README. Update when X's pricing changes.
X_POST_USD = 0.015
X_LINK_POST_USD = 0.20

# (candidate_id, stage, model) -> running totals not yet saved
_ledger: dict[tuple[int, str, str], dict] = {}


@contextmanager
def tracking(candidate_id: int) -> Iterator[None]:
    """Attribute Claude usage inside the block to this place."""
    token = current_candidate.set(candidate_id)
    try:
        yield
    finally:
        current_candidate.reset(token)


def add(
    candidate_id: int,
    stage: str,
    model: str,
    usd: float,
    requests: int = 1,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Add to the not-yet-saved cost of one stage for one place."""
    entry = _ledger.setdefault(
        (candidate_id, stage, model),
        {"usd": 0.0, "requests": 0, "input_tokens": 0, "output_tokens": 0},
    )
    entry["usd"] += usd
    entry["requests"] += requests
    entry["input_tokens"] += input_tokens
    entry["output_tokens"] += output_tokens


async def save(db_pool, candidate_id: int) -> None:
    """Write this place's pending costs to the database and clear them."""
    keys = [key for key in _ledger if key[0] == candidate_id]
    rows = [(key, _ledger.pop(key)) for key in keys]
    if not rows:
        return
    await db_pool.executemany(
        """
        INSERT INTO costs
            (candidate_id, stage, model, requests, input_tokens, output_tokens, usd)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        [
            (
                candidate_id,
                stage,
                model,
                e["requests"],
                e["input_tokens"],
                e["output_tokens"],
                round(e["usd"], 4),
            )
            for (_, stage, model), e in rows
        ],
    )
