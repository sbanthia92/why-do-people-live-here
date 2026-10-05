"""Tests for db.state: the database saved to files and loaded back."""

import json
from pathlib import Path

import asyncpg
import pytest

pgserver = pytest.importorskip("pgserver")

from db.state import TABLES, export_state, import_state

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
    await conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    yield conn
    await conn.close()


async def _populate(db) -> None:
    await db.execute(
        """
        INSERT INTO candidates (place_name, wikipedia_url, region, story_category, status,
                                layer1_passed)
        VALUES ('Karaganda', 'https://en.wikipedia.org/wiki/Karaganda', 'Asia',
                'colonial_resettlement', 'posted', true),
               ('Dull Town', 'https://en.wikipedia.org/wiki/Dull_Town', NULL, 'ghost_town',
                'skipped', false)
        """
    )
    await db.execute(
        """
        INSERT INTO research (candidate_id, wikipedia_extract, web_sources, raw_research)
        VALUES (1, 'A very long article…', '[{"url": "https://example.org", "snippet": "s",
                "source_name": "Example"}]', 'Brief [W][1].')
        """
    )
    await db.execute(
        """
        INSERT INTO assessments (candidate_id, corroborated, corroborated_reason, has_angle,
                                 has_angle_reason, writer_notes, passed)
        VALUES (1, true, 'backed', true, 'steppe', 'Lead with the joke.', true)
        """
    )
    await db.execute(
        """
        INSERT INTO posts (candidate_id, post_text, thread_parts, x_post_id, dry_run)
        VALUES (1, 'Hook “quoted” — −13 °C', '["2/ More.", "3/ End."]', '2106168528156184956',
                false)
        """
    )
    await db.execute(
        "INSERT INTO variety_log (region, story_category) VALUES ('Asia', 'colonial_resettlement')"
    )


async def _snapshot(db) -> dict:
    return {
        table: [dict(r) for r in await db.fetch(f"SELECT * FROM {table} ORDER BY id")]
        for table in TABLES
    }


async def test_state_round_trips_through_files(db, database_url, tmp_path):
    await _populate(db)
    before = await _snapshot(db)

    await export_state(database_url, tmp_path)
    await db.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    loaded = await import_state(database_url, tmp_path)

    after = await _snapshot(db)
    assert loaded == 6
    # The article text is deliberately not saved; everything else comes back exactly
    assert after["research"][0].pop("wikipedia_extract") is None
    before["research"][0].pop("wikipedia_extract")
    assert after == before


async def test_new_rows_after_import_get_fresh_ids(db, database_url, tmp_path):
    """Loaded rows keep their ids, and the next insert must not collide with them."""
    await _populate(db)
    await export_state(database_url, tmp_path)
    await import_state(database_url, tmp_path)

    new_id = await db.fetchval(
        """
        INSERT INTO candidates (place_name, wikipedia_url) VALUES ('New', 'https://x.test/New')
        RETURNING id
        """
    )
    assert new_id == 3


async def test_files_are_one_row_per_line_in_id_order(db, database_url, tmp_path):
    """So that a day's run shows up in git as a few added lines."""
    await _populate(db)
    await export_state(database_url, tmp_path)

    lines = (tmp_path / "candidates.jsonl").read_text().splitlines()
    assert [json.loads(line)["place_name"] for line in lines] == ["Karaganda", "Dull Town"]
    assert "wikipedia_extract" not in (tmp_path / "research.jsonl").read_text()
    assert {p.name for p in tmp_path.iterdir()} == {f"{t}.jsonl" for t in TABLES}


async def test_import_from_empty_directory_starts_clean(db, database_url, tmp_path):
    """A first run has no state yet; that is an empty database, not an error."""
    await _populate(db)

    assert await import_state(database_url, tmp_path) == 0
    assert all(rows == [] for rows in (await _snapshot(db)).values())
