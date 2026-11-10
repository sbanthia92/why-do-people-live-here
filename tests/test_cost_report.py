"""Tests for scripts/cost_report.py."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "scripts" / "cost_report.py"
_spec = importlib.util.spec_from_file_location("cost_report", _SCRIPT)
cost_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cost_report)

NOW = datetime(2026, 11, 13, 18, 47, tzinfo=UTC)


def _write(directory: Path, table: str, rows: list[dict]) -> None:
    (directory / f"{table}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def _cost(candidate_id, stage, usd, when):
    return {"candidate_id": candidate_id, "stage": stage, "usd": usd, "recorded_at": when}


def _state(tmp_path: Path) -> list[dict]:
    """Write the places and posts to `tmp_path`; returns the cost rows (kept apart: they live in the cost database)."""
    _write(
        tmp_path,
        "candidates",
        [
            {"id": 1, "place_name": "Alpha Island", "status": "posted"},
            {"id": 2, "place_name": "Beta Bay", "status": "assessed"},
            {"id": 3, "place_name": "Dull Town", "status": "skipped"},
            {"id": 4, "place_name": "Old Place", "status": "posted"},
        ],
    )
    _write(
        tmp_path,
        "posts",
        [
            # Researched two days before it was posted: that cost still counts toward the post
            {"candidate_id": 1, "posted_at": "2026-11-12T14:30:00+00:00", "dry_run": False,
             "x_post_id": "111"},
            # A preview is not a post
            {"candidate_id": 2, "posted_at": "2026-11-12T10:00:00+00:00", "dry_run": True,
             "x_post_id": None},
            # Posted before the week, so not in this report
            {"candidate_id": 4, "posted_at": "2026-10-20T14:30:00+00:00", "dry_run": False,
             "x_post_id": "444"},
        ],
    )
    return [
            _cost(1, "web search", "0.2000", "2026-11-10T14:30:00+00:00"),
            _cost(1, "writing", "0.1000", "2026-11-12T14:30:00+00:00"),
            _cost(1, "x posting", "0.0600", "2026-11-12T14:31:00+00:00"),
            _cost(2, "web search", "0.3000", "2026-11-12T10:00:00+00:00"),
            _cost(3, "web search", "0.1000", "2026-11-11T14:30:00+00:00"),
            _cost(4, "writing", "9.9900", "2026-10-20T14:30:00+00:00"),  # last month
        ]


def test_report_totals_cost_per_post_and_links(tmp_path):
    report = cost_report.build_report(tmp_path, NOW, 7, _state(tmp_path))

    assert report.startswith("# Cost report: 6 Nov – 13 Nov 2026\n")
    assert "**Spent this week:** $0.76" in report  # 0.20 + 0.10 + 0.06 + 0.30 + 0.10
    assert "**Posts this week:** 1" in report
    # The post's cost is everything spent on its place, not just this week's rows
    assert "**Cost per post:** $0.36" in report
    assert "| 12 Nov | Alpha Island | $0.36 | [view](https://x.com/i/status/111) |" in report
    assert "Old Place" not in report


def test_report_breaks_spend_down_by_stage_and_shows_unposted_places(tmp_path):
    report = cost_report.build_report(tmp_path, NOW, 7, _state(tmp_path))

    assert "| web search | $0.60 | 79% |" in report
    assert "| writing | $0.10 | 13% |" in report
    assert "| x posting | $0.06 | 8% |" in report
    assert "$0.40 across 2 place(s)" in report
    assert "| Beta Bay | $0.30 | assessed |" in report
    assert "| Dull Town | $0.10 | skipped |" in report


def test_report_without_any_recorded_costs_says_so(tmp_path):
    _write(tmp_path, "candidates", [])
    _write(tmp_path, "posts", [])
    report = cost_report.build_report(tmp_path, NOW, 7, [])
    assert "No costs have been recorded yet" in report


def test_posts_from_before_tracking_are_counted_but_not_priced(tmp_path):
    _write(tmp_path, "candidates", [{"id": 1, "place_name": "Alpha", "status": "posted"},
                                    {"id": 2, "place_name": "Beta", "status": "posted"}])
    _write(tmp_path, "posts", [
        {"candidate_id": 1, "posted_at": "2026-11-12T14:30:00+00:00", "dry_run": False, "x_post_id": "1"},
        {"candidate_id": 2, "posted_at": "2026-11-11T14:30:00+00:00", "dry_run": False, "x_post_id": "2"},
    ])
    costs = [_cost(1, "writing", "0.5000", "2026-11-12T14:30:00+00:00")]

    report = cost_report.build_report(tmp_path, NOW, 7, costs)

    assert "**Posts this week:** 2" in report
    assert "**Cost per post:** $0.50" in report
    assert "1 post(s) went out before costs were tracked." in report
    assert "| 11 Nov | Beta | – |" in report


def test_cost_rows_are_matched_to_places_by_wikipedia_url_not_by_id():
    candidates = [{"id": 7, "wikipedia_url": "https://en.wikipedia.org/wiki/Alpha"}]
    costs = [
        {"wikipedia_url": "https://en.wikipedia.org/wiki/Alpha", "stage": "writing", "usd": "0.1"},
        {"wikipedia_url": "https://en.wikipedia.org/wiki/Gone", "stage": "writing", "usd": "0.2"},
    ]

    matched = cost_report.attach_places(costs, candidates)

    # A place the state no longer knows still counts toward spend, under id 0
    assert [row["candidate_id"] for row in matched] == [7, 0]


def test_a_database_failure_prints_only_the_kind_of_error(monkeypatch, tmp_path, capsys):
    """In Actions the log is public, and a connection error can quote the host."""
    import sys

    async def failing_fetch(url):
        raise OSError("could not reach secret-host.example.com")

    monkeypatch.setattr(cost_report, "fetch", failing_fetch)
    monkeypatch.setenv("COST_DATABASE_URL", "postgresql://u:p@secret-host.example.com/db")
    monkeypatch.setattr(sys, "argv", ["cost_report.py", "--state-dir", str(tmp_path)])

    assert cost_report.main() == 3

    output = capsys.readouterr().out
    assert "Could not read the cost database (OSError)." in output
    assert "secret-host" not in output
