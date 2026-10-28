"""Run the pipeline without Docker or a hosted database: preview posts, or publish them.

Starts an embedded Postgres (the `pgserver` dev dependency), applies the schema and runs
the pipeline. By default nothing is published: posts are printed (a dry run). With
--publish, posts go to X for real. Places previewed earlier are published first, with
exactly the text that was previewed.

Where the pipeline's memory lives:
  (default)         .dryrun-pg/ on this machine. Delete it to start over.
  --state-dir DIR   plain files in DIR, loaded before the run and saved after it. The
                    daily workflow uses this with a checkout of the `data` branch.
  --shared          the repository's `data` branch: fetched before the run, committed
                    and pushed after it, so this machine and the daily workflow share
                    one history of what has been posted.

Needs ANTHROPIC_API_KEY in .env; --publish also needs the four X_* keys.

Usage:  python scripts/run_local.py [--posts N] [--publish] [--shared]
"""

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Run as a plain script from anywhere without relying on the package being installed
sys.path.insert(0, str(ROOT))

import anthropic
import asyncpg
import pgserver
from dotenv import load_dotenv

from db.state import export_state, import_state
from pipeline.review import reject_candidates, rejected_urls
from scheduler.run_pipeline import run_once

X_KEYS = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")
DATA_BRANCH = "data"
SHARED_DIR = ROOT / ".state"


async def _apply_schema(database_url: str) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute((ROOT / "db" / "schema.sql").read_text())
    finally:
        await conn.close()


async def _apply_rejections(database_url: str, issues_file: Path) -> None:
    """Skip the places the owner rejected on their preview issues."""
    urls = rejected_urls(issues_file.read_text())
    conn = await asyncpg.connect(database_url)
    try:
        names = await reject_candidates(conn, urls)
    finally:
        await conn.close()
    if names:
        print(f"Skipping {len(names)} place(s) rejected on their preview issue: {', '.join(names)}")


def _git(*args: str, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def _checkout_shared_state() -> None:
    """Make SHARED_DIR a checkout of the latest `data` branch (created if it doesn't exist)."""
    exists = _git("ls-remote", "--exit-code", "--heads", "origin", DATA_BRANCH, check=False)
    if SHARED_DIR.exists():
        _git("worktree", "remove", "--force", str(SHARED_DIR), check=False)
    if exists.returncode == 0:
        _git("fetch", "origin", DATA_BRANCH)
        _git("worktree", "add", "--force", "-B", DATA_BRANCH, str(SHARED_DIR), f"origin/{DATA_BRANCH}")
    else:
        # First use: an empty branch with no history in common with the code
        _git("worktree", "add", "--force", "--detach", str(SHARED_DIR))
        _git("checkout", "--orphan", DATA_BRANCH, cwd=SHARED_DIR)
        _git("rm", "-rf", "--quiet", ".", cwd=SHARED_DIR, check=False)


def _push_shared_state(message: str) -> None:
    _git("add", "-A", cwd=SHARED_DIR)
    if _git("diff", "--cached", "--quiet", cwd=SHARED_DIR, check=False).returncode == 0:
        print("State unchanged; nothing to push.")
        return
    _git("commit", "-m", message, cwd=SHARED_DIR)
    _git("push", "origin", f"HEAD:{DATA_BRANCH}", cwd=SHARED_DIR)
    print(f"Saved state to the '{DATA_BRANCH}' branch.")


def _explain(exc: anthropic.APIStatusError) -> str:
    """One line a person can act on, instead of a traceback."""
    message = getattr(exc, "message", str(exc))
    if "credit balance" in message:
        return "Anthropic credit balance is too low. Add credits under Plans & Billing in the Anthropic console."
    if isinstance(exc, anthropic.AuthenticationError):
        return "Anthropic rejected the API key. Check ANTHROPIC_API_KEY."
    return f"Anthropic API error ({exc.status_code}): {message}"


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
    parser.add_argument(
        "--rejected-issues",
        type=Path,
        help="JSON from `gh issue list --json body`: places named there are not posted",
    )
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--state-dir", type=Path, help="load state from, and save it to, this directory")
    where.add_argument("--shared", action="store_true", help=f"use the repository's '{DATA_BRANCH}' branch")
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
            print(f"Cannot publish: {', '.join(missing)} not set.")
            print("Check the keys first with: python scripts/x_check.py")
            return 2
        if not args.yes:
            answer = input(f"This will publish {posts} post(s) to X for real. Type 'publish' to continue: ")
            if answer.strip().lower() != "publish":
                print("Cancelled; nothing was posted.")
                return 1

    if args.shared:
        _checkout_shared_state()
    state_dir = SHARED_DIR if args.shared else args.state_dir

    # With saved state the database is rebuilt from it every run, so it can be thrown
    # away afterwards; otherwise it is kept on this machine.
    scratch = tempfile.TemporaryDirectory() if state_dir else None
    server = pgserver.get_server(scratch.name if scratch else ROOT / ".dryrun-pg")
    # Set after load_dotenv so the embedded database and the chosen mode always win
    database_url = os.environ["DATABASE_URL"] = server.get_uri()
    os.environ["DRY_RUN"] = "false" if args.publish else "true"

    asyncio.run(_apply_schema(database_url))
    if state_dir:
        loaded = asyncio.run(import_state(database_url, state_dir))
        # Again, so the schema's migrations also apply to the rows just loaded
        asyncio.run(_apply_schema(database_url))
        print(f"Loaded {loaded} rows of state from {state_dir}.")
    if args.rejected_issues:
        asyncio.run(_apply_rejections(database_url, args.rejected_issues))

    produced, failure = 0, None
    try:
        produced = asyncio.run(run_once(posts, parallel))
    except anthropic.APIStatusError as exc:
        failure = _explain(exc)
    finally:
        # Save even after a failure: a post may have gone out before the error, and
        # forgetting it would mean posting the same place again.
        if state_dir:
            asyncio.run(export_state(database_url, state_dir))
            if args.shared:
                _push_shared_state(f"State after {'publishing' if args.publish else 'previewing'} {produced} post(s)")
        server.cleanup()
        if scratch:
            scratch.cleanup()

    if failure:
        # The ::error:: prefix makes GitHub Actions show this line on the run's page
        print(f"::error::{failure}" if os.getenv("GITHUB_ACTIONS") else failure)
        return 3
    print(f"\n{'Published' if args.publish else 'Previewed'} {produced} of {posts} requested posts.")
    return 0 if produced else 1


if __name__ == "__main__":
    sys.exit(main())
