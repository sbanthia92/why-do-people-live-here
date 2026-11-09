"""Tests for db.private_costs: cost rows encrypted so a public repository can't read them."""

from pathlib import Path

import asyncpg
import pytest

pgserver = pytest.importorskip("pgserver")

from nacl.exceptions import CryptoError

from db.private_costs import export_encrypted, generate_keys, read_encrypted

_SCHEMA = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()


@pytest.fixture(scope="module")
def database_url(tmp_path_factory) -> str:
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"))
    yield server.get_uri()
    server.cleanup()


@pytest.fixture
async def db(database_url):
    conn = await asyncpg.connect(database_url)
    await conn.execute(_SCHEMA)
    await conn.execute("TRUNCATE costs, candidates RESTART IDENTITY CASCADE")
    await conn.execute(
        "INSERT INTO candidates (place_name, wikipedia_url) VALUES ('Alpha', 'https://x.test/A')"
    )
    yield conn
    await conn.close()


async def _add_cost(db, stage: str, usd: str) -> None:
    await db.execute(
        "INSERT INTO costs (candidate_id, stage, model, requests, usd) VALUES (1, $1, 'm', 1, $2)",
        stage,
        usd,
    )


async def test_costs_round_trip_and_are_not_readable_in_the_file(db, database_url, tmp_path):
    public, private = generate_keys()
    await _add_cost(db, "writing", "0.1234")

    assert await export_encrypted(database_url, tmp_path, public, "run-1") == 1

    saved = (tmp_path / "costs" / "run-1.enc").read_bytes()
    assert b"writing" not in saved and b"0.1234" not in saved
    rows = read_encrypted(tmp_path, private)
    assert [(r["stage"], float(r["usd"])) for r in rows] == [("writing", 0.1234)]


async def test_the_wrong_key_cannot_read_the_costs(db, database_url, tmp_path):
    public, _ = generate_keys()
    _, someone_elses_private = generate_keys()
    await _add_cost(db, "writing", "0.1")
    await export_encrypted(database_url, tmp_path, public, "run-1")

    with pytest.raises(CryptoError):
        read_encrypted(tmp_path, someone_elses_private)


async def test_each_run_adds_a_file_and_all_are_read_in_order(db, database_url, tmp_path):
    public, private = generate_keys()
    await _add_cost(db, "web search", "0.2")
    await export_encrypted(database_url, tmp_path, public, "20261101-a")
    await db.execute("TRUNCATE costs")
    await _add_cost(db, "writing", "0.1")
    await export_encrypted(database_url, tmp_path, public, "20261102-b")

    assert [r["stage"] for r in read_encrypted(tmp_path, private)] == ["web search", "writing"]


async def test_no_rows_means_no_file(db, database_url, tmp_path):
    public, _ = generate_keys()
    assert await export_encrypted(database_url, tmp_path, public, "run-1") == 0
    assert not (tmp_path / "costs").exists()
