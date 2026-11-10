"""Print a Markdown cost report for the last week. Run it on your own computer.

Costs come from the private cost database (COST_DATABASE_URL, in `.env` or the environment);
the posts and places come from the `data` branch. Costs are never saved in the repository.
A post's cost is everything spent on its place, including research done on earlier days.

Usage:  python scripts/cost_report.py --pull [--days 7]
        python scripts/cost_report.py --state-dir DIR [--days 7]
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.cost_store import fetch


def _rows(state_dir: Path, table: str) -> list[dict]:
    file = state_dir / f"{table}.jsonl"
    return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _usd(amount: float) -> str:
    return f"${amount:,.2f}"


def build_report(state_dir: Path, now: datetime, days: int, costs: list[dict]) -> str:
    """The report for the `days` days before `now`, as Markdown whose first line is a title.

    `costs` are the decrypted cost rows; posts and places come from the state files.
    """
    start = now - timedelta(days=days)
    candidates = {c["id"]: c for c in _rows(state_dir, "candidates")}
    posts = [
        p
        for p in _rows(state_dir, "posts")
        if not p["dry_run"] and start <= _time(p["posted_at"]) <= now
    ]

    per_place: dict[int, float] = defaultdict(float)
    for row in costs:
        per_place[row["candidate_id"]] += float(row["usd"])
    this_week = [r for r in costs if start <= _time(r["recorded_at"]) <= now]
    by_stage: dict[str, float] = defaultdict(float)
    for row in this_week:
        by_stage[row["stage"]] += float(row["usd"])
    spent = sum(by_stage.values())

    title_range = f"{start:%-d %b} – {now:%-d %b %Y}"
    lines = [f"# Cost report: {title_range}", ""]
    if not costs:
        note = "No costs have been recorded yet. They are saved from the first run after cost tracking was added."
        return "\n".join([*lines, note, ""])

    priced = [p for p in posts if p["candidate_id"] in per_place]
    post_costs = [per_place[p["candidate_id"]] for p in priced]
    lines += [
        f"- **Spent this week:** {_usd(spent)}",
        f"- **Posts this week:** {len(posts)}",
    ]
    if post_costs:
        lines.append(
            f"- **Cost per post:** {_usd(sum(post_costs) / len(post_costs))}"
            " (everything spent on that place, including research done on earlier days)"
        )
    if len(priced) < len(posts):
        lines.append(f"- {len(posts) - len(priced)} post(s) went out before costs were tracked.")
    lines += ["", "## Where the money went this week", "", "| Stage | Cost | Share |", "|---|---|---|"]
    for stage, amount in sorted(by_stage.items(), key=lambda item: -item[1]):
        share = f"{amount / spent:.0%}" if spent else "–"
        lines.append(f"| {stage} | {_usd(amount)} | {share} |")

    lines += ["", "## Posts", "", "| Date | Place | Cost | Post |", "|---|---|---|---|"]
    for post in sorted(posts, key=lambda p: p["posted_at"]):
        place = candidates.get(post["candidate_id"], {}).get("place_name", "?")
        cost = _usd(per_place[post["candidate_id"]]) if post["candidate_id"] in per_place else "–"
        link = (
            f"[view](https://x.com/i/status/{post['x_post_id']})" if post.get("x_post_id") else "–"
        )
        lines.append(f"| {_time(post['posted_at']):%-d %b} | {place} | {cost} | {link} |")

    posted_ids = {p["candidate_id"] for p in posts}
    unposted: dict[int, float] = defaultdict(float)
    for row in this_week:
        if row["candidate_id"] not in posted_ids:
            unposted[row["candidate_id"]] += float(row["usd"])
    if unposted:
        summary = (
            f"{_usd(sum(unposted.values()))} across {len(unposted)} place(s): research done ahead"
            " of a later post, or on places the editor or fact check turned down."
        )
        lines += [
            "",
            "## Spent on places not posted this week",
            "",
            summary,
            "",
            "| Place | Cost | Status |",
            "|---|---|---|",
        ]
        for cid, amount in sorted(unposted.items(), key=lambda item: -item[1]):
            place = candidates.get(cid, {})
            lines.append(f"| {place.get('place_name', '?')} | {_usd(amount)} | {place.get('status', '?')} |")
    return "\n".join(lines) + "\n"


def _pull(state_dir: Path) -> None:
    """Make `state_dir` a checkout of the latest `data` branch."""
    subprocess.run(["git", "fetch", "origin", "data"], cwd=ROOT, check=True)
    if state_dir.exists():
        subprocess.run(["git", "checkout", "--detach", "origin/data"], cwd=state_dir, check=True)
    else:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(state_dir), "origin/data"],
            cwd=ROOT,
            check=True,
        )


def attach_places(costs: list[dict], candidates: list[dict]) -> list[dict]:
    """Give each cost row the id its place has in this state, matched by Wikipedia URL.

    A place the state does not know (it was reset since) gets id 0, so its spend still counts.
    """
    ids = {c["wikipedia_url"]: c["id"] for c in candidates}
    return [{**row, "candidate_id": ids.get(row["wikipedia_url"], 0)} for row in costs]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".state")
    parser.add_argument("--pull", action="store_true", help="fetch the latest data branch first")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    url = os.getenv("COST_DATABASE_URL", "").strip()
    if not url:
        print("COST_DATABASE_URL is not set. Put it in .env or the environment.")
        return 2
    if args.pull:
        _pull(args.state_dir)
    try:
        rows = asyncio.run(fetch(url))
    except Exception as exc:  # noqa: BLE001
        # Only the kind of error: in Actions the log is public, and a connection error can quote the host
        print(f"Could not read the cost database ({type(exc).__name__}).")
        return 3
    costs = attach_places(rows, _rows(args.state_dir, "candidates"))
    print(build_report(args.state_dir, datetime.now(UTC), args.days, costs), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
