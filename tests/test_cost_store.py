"""Tests for db.cost_store: copying costs to a separate private database."""

from pathlib import Path

import asyncpg
import pytest

pgserver = pytest.importorskip("pgserver")

from db.cost_store import _clean_url, _tls_kwargs, fetch, push

_SCHEMA = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()


@pytest.fixture(scope="module")
def local_url(tmp_path_factory) -> str:
    server = pgserver.get_server(tmp_path_factory.mktemp("local-pg"))
    yield server.get_uri()
    server.cleanup()


@pytest.fixture(scope="module")
def remote_url(tmp_path_factory) -> str:
    """A second, separate server standing in for the private cost database."""
    server = pgserver.get_server(tmp_path_factory.mktemp("remote-pg"))
    yield server.get_uri()
    server.cleanup()


@pytest.fixture
async def local(local_url):
    conn = await asyncpg.connect(local_url)
    await conn.execute(_SCHEMA)
    await conn.execute("TRUNCATE costs, candidates RESTART IDENTITY CASCADE")
    await conn.execute(
        "INSERT INTO candidates (place_name, wikipedia_url) VALUES"
        " ('Alpha Island', 'https://en.wikipedia.org/wiki/Alpha_Island')"
    )
    yield conn
    await conn.close()


@pytest.fixture
async def remote(remote_url):
    conn = await asyncpg.connect(remote_url)
    await conn.execute("DROP TABLE IF EXISTS costs")
    yield conn
    await conn.close()


async def _add(local, stage: str, usd: str, model: str = "m") -> None:
    await local.execute(
        "INSERT INTO costs (candidate_id, stage, model, requests, input_tokens, output_tokens, usd)"
        " VALUES (1, $1, $2, 1, 100, 10, $3)",
        stage,
        model,
        usd,
    )


async def test_costs_are_copied_with_the_place_and_read_back(local, remote, local_url, remote_url):
    await _add(local, "writing", "0.1234")

    assert await push(local_url, remote_url, "run-1") == 1

    (row,) = await fetch(remote_url)
    assert (row["run_name"], row["place_name"], row["stage"]) == ("run-1", "Alpha Island", "writing")
    assert row["wikipedia_url"] == "https://en.wikipedia.org/wiki/Alpha_Island"
    assert float(row["usd"]) == pytest.approx(0.1234)
    assert isinstance(row["recorded_at"], str)  # an ISO timestamp, as the report expects


async def test_rows_for_the_same_stage_are_added_up(local, remote, local_url, remote_url):
    # A place's stage can be saved more than once in a run (prepared, then published)
    await _add(local, "web search", "0.2")
    await _add(local, "web search", "0.3")

    assert await push(local_url, remote_url, "run-1") == 1

    (row,) = await fetch(remote_url)
    assert (float(row["usd"]), row["requests"], row["input_tokens"]) == (0.5, 2, 200)


async def test_repeating_a_run_does_not_duplicate_but_a_new_run_adds(local, remote, local_url, remote_url):
    await _add(local, "writing", "0.1")

    await push(local_url, remote_url, "run-1")
    await push(local_url, remote_url, "run-1")  # e.g. the step was retried
    assert len(await fetch(remote_url)) == 1

    await push(local_url, remote_url, "run-2")
    assert [r["run_name"] for r in await fetch(remote_url)] == ["run-1", "run-2"]


async def test_nothing_to_save_never_touches_the_cost_database(local, local_url):
    # An unreachable address would raise if it were used
    assert await push(local_url, "postgresql://u:p@127.0.0.1:1/none", "run-1") == 0


async def test_a_fresh_cost_database_reads_as_empty(remote, remote_url):
    assert await fetch(remote_url) == []


def test_a_remote_database_must_use_tls():
    assert _tls_kwargs("postgresql://u:p@db.example.com/costs") == {"ssl": "require"}
    # The URL can ask for TLS itself, including certificate checks
    assert _tls_kwargs("postgresql://u:p@db.example.com/costs?sslmode=require") == {}
    assert _tls_kwargs("postgresql://u:p@db.example.com/costs?sslmode=verify-full") == {}
    for weak in ("disable", "allow", "prefer"):
        with pytest.raises(ValueError):
            _tls_kwargs(f"postgresql://u:p@db.example.com/costs?sslmode={weak}")


def test_a_database_on_this_machine_needs_no_tls():
    assert _tls_kwargs("postgresql://u:p@localhost/costs") == {}
    assert _tls_kwargs("postgresql://u:p@127.0.0.1:5432/costs") == {}
    assert _tls_kwargs("postgresql://postgres:@/postgres?host=/tmp/pg-socket") == {}


def test_channel_binding_is_dropped_but_tls_and_the_rest_are_kept():
    url = "postgresql://u:p%40ss@ep-x.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

    cleaned = _clean_url(url)

    assert cleaned == "postgresql://u:p%40ss@ep-x.aws.neon.tech/neondb?sslmode=require"
    assert _tls_kwargs(cleaned) == {}  # still asks for TLS


def test_a_url_without_channel_binding_is_unchanged():
    url = "postgresql://u:p@db.example.com/costs?sslmode=require"
    assert _clean_url(url) == url
    assert _clean_url("postgresql://u:p@db.example.com/costs") == "postgresql://u:p@db.example.com/costs"
