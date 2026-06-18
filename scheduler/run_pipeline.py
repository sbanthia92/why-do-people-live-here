"""Main entry point. Orchestrates one full pipeline run: select → research → score → generate → publish."""

import asyncio


async def run_once() -> None:
    """Execute one pipeline run: pick a candidate, research it, score it, generate content, publish.

    Reads DRY_RUN from env. Creates and tears down the asyncpg pool per run.
    """
    pass


async def seed_candidates_if_empty(db_pool) -> None:
    """If the candidates table is empty, enqueue initial candidates from seed_categories.yaml."""
    pass


if __name__ == "__main__":
    asyncio.run(run_once())
