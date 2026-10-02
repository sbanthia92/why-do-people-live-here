"""Preview posts locally without Docker and without publishing anything.

Starts an embedded Postgres (the `pgserver` dev dependency) in .dryrun-pg/, applies
the schema, and runs the pipeline with DRY_RUN=true. The database is kept between
runs, so each run previews new places; delete .dryrun-pg/ to start over.

Needs ANTHROPIC_API_KEY in .env (or the environment). X credentials are not needed.

Usage:  python scripts/dry_run.py [--posts N]
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Run as a plain script from anywhere without relying on the package being installed
sys.path.insert(0, str(ROOT))

import asyncpg
import pgserver
from dotenv import load_dotenv

from scheduler.run_pipeline import run_once


async def _apply_schema(database_url: str) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute((ROOT / "db" / "schema.sql").read_text())
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--posts", type=int, default=3, help="number of posts to preview")
    parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        help="candidates to research at once (default: posts + 1, at most 4)",
    )
    args = parser.parse_args()
    # One spare, since some candidates score too low to post
    parallel = args.parallel or min(args.posts + 1, 4)

    load_dotenv(ROOT / ".env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Add it to .env (copy .env.example) and retry.")
        return 2

    server = pgserver.get_server(ROOT / ".dryrun-pg")
    # Set after load_dotenv so the embedded database and dry-run mode always win
    os.environ["DATABASE_URL"] = server.get_uri()
    os.environ["DRY_RUN"] = "true"

    asyncio.run(_apply_schema(os.environ["DATABASE_URL"]))
    produced = asyncio.run(run_once(args.posts, parallel))
    print(f"\nPreviewed {produced} of {args.posts} requested posts.")
    return 0 if produced else 1


if __name__ == "__main__":
    sys.exit(main())
