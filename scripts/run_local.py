"""Run the pipeline locally without Docker: preview posts, or publish them to X.

Starts an embedded Postgres (the `pgserver` dev dependency) in .dryrun-pg/ and applies
the schema. The database is kept between runs, so each run works on new places; delete
.dryrun-pg/ to start over.

By default nothing is published: posts are printed (a dry run). With --publish, posts go
to X for real. Places previewed earlier are published first, with exactly the text that
was previewed.

Needs ANTHROPIC_API_KEY in .env; --publish also needs the four X_* keys.

Usage:  python scripts/run_local.py [--posts N] [--publish]
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

X_KEYS = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")


async def _apply_schema(database_url: str) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute((ROOT / "db" / "schema.sql").read_text())
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--posts", type=int, default=None, help="number of posts (default 3, or 1 with --publish)")
    parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        help="candidates to research at once (default: posts + 1, at most 4)",
    )
    parser.add_argument("--publish", action="store_true", help="post to X for real")
    parser.add_argument("--yes", action="store_true", help="with --publish, skip the confirmation")
    args = parser.parse_args()
    posts = args.posts or (1 if args.publish else 3)
    # One spare, since some candidates don't pass the editor's gates
    parallel = args.parallel or min(posts + 1, 4)

    load_dotenv(ROOT / ".env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Add it to .env (copy .env.example) and retry.")
        return 2
    if args.publish:
        missing = [key for key in X_KEYS if not os.getenv(key)]
        if missing:
            print(f"Cannot publish: {', '.join(missing)} not set in .env.")
            print("Check the keys first with: python scripts/x_check.py")
            return 2
        if not args.yes:
            answer = input(f"This will publish {posts} post(s) to X for real. Type 'publish' to continue: ")
            if answer.strip().lower() != "publish":
                print("Cancelled; nothing was posted.")
                return 1

    server = pgserver.get_server(ROOT / ".dryrun-pg")
    # Set after load_dotenv so the embedded database and the chosen mode always win
    os.environ["DATABASE_URL"] = server.get_uri()
    os.environ["DRY_RUN"] = "false" if args.publish else "true"

    asyncio.run(_apply_schema(os.environ["DATABASE_URL"]))
    produced = asyncio.run(run_once(posts, parallel))
    print(f"\n{'Published' if args.publish else 'Previewed'} {produced} of {posts} requested posts.")
    return 0 if produced else 1


if __name__ == "__main__":
    sys.exit(main())
