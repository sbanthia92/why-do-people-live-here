"""Tests for scripts/run_local.py with state kept in a directory."""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import anthropic
import asyncpg
import pytest

pytest.importorskip("pgserver")

from pipeline.review import marker

_SCRIPT = Path(__file__).parent.parent / "scripts" / "run_local.py"
_spec = importlib.util.spec_from_file_location("run_local", _SCRIPT)
run_local = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_local)


def _run(monkeypatch, state_dir: Path, fake_run_once) -> int:
    monkeypatch.setattr(run_local, "run_once", fake_run_once)
    monkeypatch.setattr(sys, "argv", ["run_local.py", "--state-dir", str(state_dir), "--posts", "1"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    return run_local.main()


async def _add_place(name: str, status: str = "approved") -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute(
            "INSERT INTO candidates (place_name, wikipedia_url, status) VALUES ($1, $2, $3)",
            name,
            f"https://example.test/{name}",
            status,
        )
    finally:
        await conn.close()


def _places(state_dir: Path) -> list[str]:
    lines = (state_dir / "candidates.jsonl").read_text().splitlines()
    return [json.loads(line)["place_name"] for line in lines]


def test_state_accumulates_across_runs(monkeypatch, tmp_path):
    """Each run starts from what the previous one saved, on a database of its own."""

    async def first(posts, parallel):
        await _add_place("Alpha")
        return 1

    async def second(posts, parallel):
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            # The first run's place was loaded into this run's fresh database
            assert await conn.fetchval("SELECT count(*) FROM candidates") == 1
        finally:
            await conn.close()
        await _add_place("Beta")
        return 1

    assert _run(monkeypatch, tmp_path, first) == 0
    assert _places(tmp_path) == ["Alpha"]
    assert _run(monkeypatch, tmp_path, second) == 0
    assert _places(tmp_path) == ["Alpha", "Beta"]


def test_out_of_credits_is_reported_plainly_and_state_is_still_saved(monkeypatch, tmp_path, capsys):
    async def out_of_credits(posts, parallel):
        await _add_place("Alpha", status="posted")  # something happened before the failure
        raise anthropic.BadRequestError(
            "Your credit balance is too low to access the Anthropic API.",
            response=MagicMock(status_code=400),
            body=None,
        )

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert _run(monkeypatch, tmp_path, out_of_credits) == 3

    output = capsys.readouterr().out
    assert "Anthropic credit balance is too low" in output
    assert "Traceback" not in output
    assert _places(tmp_path) == ["Alpha"]


def test_failure_is_annotated_for_github_actions(monkeypatch, tmp_path, capsys):
    async def bad_key(posts, parallel):
        raise anthropic.AuthenticationError(
            "invalid x-api-key", response=MagicMock(status_code=401), body=None
        )

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert _run(monkeypatch, tmp_path, bad_key) == 3
    assert "::error::Anthropic rejected the API key" in capsys.readouterr().out


def test_rejected_previews_are_skipped_but_posted_places_are_not(monkeypatch, tmp_path):
    """A place rejected on its preview issue is not posted; one that already went out stays posted."""

    async def seed(posts, parallel):
        await _add_place("Alpha", status="approved")
        await _add_place("Beta", status="approved")
        await _add_place("Gamma", status="posted")
        return 1

    async def statuses_after_rejection(posts, parallel):
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            rows = await conn.fetch("SELECT place_name, status FROM candidates ORDER BY id")
        finally:
            await conn.close()
        assert {r["place_name"]: r["status"] for r in rows} == {
            "Alpha": "skipped",
            "Beta": "approved",
            "Gamma": "posted",
        }
        return 1

    assert _run(monkeypatch, tmp_path, seed) == 0

    issues = tmp_path.parent / f"{tmp_path.name}-issues.json"
    issues.write_text(
        json.dumps(
            [
                {"body": f"preview\n{marker('https://example.test/Alpha')}"},
                {"body": f"preview\n{marker('https://example.test/Gamma')}"},
            ]
        )
    )
    monkeypatch.setattr(run_local, "run_once", statuses_after_rejection)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_local.py", "--state-dir", str(tmp_path), "--posts", "1", "--rejected-issues", str(issues)],
    )
    assert run_local.main() == 0
    # The rejection is saved with the rest of the state
    lines = (tmp_path / "candidates.jsonl").read_text().splitlines()
    saved = {json.loads(line)["place_name"]: json.loads(line)["status"] for line in lines}
    assert saved["Alpha"] == "skipped"


async def _add_cost_row() -> None:
    await _add_place("Alpha", status="posted")
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute(
            "INSERT INTO costs (candidate_id, stage, model, requests, usd)"
            " VALUES (1, 'writing', 'm', 1, 0.07)"
        )
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def cost_db_url(tmp_path_factory) -> str:
    """A separate server standing in for the private cost database."""
    import pgserver as _pgserver

    server = _pgserver.get_server(tmp_path_factory.mktemp("cost-pg"))
    yield server.get_uri()
    server.cleanup()


def test_costs_go_to_the_cost_database_and_never_into_the_saved_state(
    monkeypatch, tmp_path, capsys, cost_db_url
):
    import asyncio

    from db.cost_store import fetch

    monkeypatch.setenv("COST_DATABASE_URL", cost_db_url)

    async def run(posts, parallel):
        await _add_cost_row()
        return 1

    assert _run(monkeypatch, tmp_path, run) == 0

    # Nothing about costs is in the state that is committed to the repository
    assert not (tmp_path / "costs").exists() and not (tmp_path / "costs.jsonl").exists()
    assert "0.07" not in "".join(p.read_text() for p in tmp_path.glob("*.jsonl"))
    (row,) = asyncio.run(fetch(cost_db_url))
    assert (row["place_name"], row["stage"], float(row["usd"])) == ("Alpha", "writing", 0.07)
    assert "Saved 1 cost row(s) to the cost database." in capsys.readouterr().out


def test_without_a_cost_database_costs_are_not_saved_anywhere(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("COST_DATABASE_URL", raising=False)

    async def run(posts, parallel):
        await _add_cost_row()
        return 1

    assert _run(monkeypatch, tmp_path, run) == 0

    assert not (tmp_path / "costs").exists() and not (tmp_path / "costs.jsonl").exists()
    assert "COST_DATABASE_URL is not set" in capsys.readouterr().out


def test_an_unreachable_cost_database_does_not_stop_the_state_being_saved_or_leak_its_address(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("COST_DATABASE_URL", "postgresql://someuser:s3cret@127.0.0.1:1/costs")

    async def run(posts, parallel):
        await _add_cost_row()
        return 1

    assert _run(monkeypatch, tmp_path, run) == 0

    output = capsys.readouterr().out
    assert "Could not save this run's costs to the cost database" in output
    assert "s3cret" not in output and "someuser" not in output
    assert _places(tmp_path) == ["Alpha"]  # the rest of the state was still saved
