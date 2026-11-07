"""Print a Markdown cost report for the last week, from the saved state files.

Reads the JSON-lines files the daily run keeps on the `data` branch. Needs no database.
A post's cost is everything spent on its place, including research done on earlier days.

Usage:  python scripts/cost_report.py --state-dir DIR [--days 7]
"""

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _rows(state_dir: Path, table: str) -> list[dict]:
    file = state_dir / f"{table}.jsonl"
    return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _usd(amount: float) -> str:
    return f"${amount:,.2f}"


def build_report(state_dir: Path, now: datetime, days: int = 7) -> str:
    """The report for the `days` days before `now`, as Markdown whose first line is a title."""
    start = now - timedelta(days=days)
    candidates = {c["id"]: c for c in _rows(state_dir, "candidates")}
    costs = _rows(state_dir, "costs")
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
    lines = [f"# Weekly cost report: {title_range}", ""]
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    print(build_report(args.state_dir, datetime.now(UTC), args.days), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
