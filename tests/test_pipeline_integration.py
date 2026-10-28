"""End-to-end pipeline tests against a real Postgres.

Claude, Wikipedia and X are faked; the selector, scheduler and every SQL statement run
for real against an embedded Postgres (the `pgserver` dev dependency).
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

pgserver = pytest.importorskip("pgserver")

from db.client import _init_connection
from pipeline.assessor import Assessment
from pipeline.content_generator import FactCheck, Posts
from pipeline.researcher import ResearchSummary
from scheduler.run_pipeline import run_once

_SCHEMA = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()
_TABLES = "variety_log, posts, assessments, research, candidates"


@pytest.fixture(scope="module")
def database_url(tmp_path_factory) -> str:
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"))
    yield server.get_uri()
    server.cleanup()


@pytest.fixture
async def db(database_url, monkeypatch):
    """A clean schema, DATABASE_URL pointed at it, and a connection for assertions."""
    conn = await asyncpg.connect(database_url)
    await _init_connection(conn)  # same JSONB decoding as the pipeline's pool
    await conn.execute(_SCHEMA)
    await conn.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DRY_RUN", "false")
    yield conn
    await conn.close()


class FakeWorld:
    """Stand-ins for everything outside the database."""

    def __init__(self, seed: dict, regions: dict[str, str], no_angle: tuple[str, ...] = ()):
        self.seed = seed
        self.regions = regions
        self.no_angle = no_angle
        self.written: list[str] = []
        self.card_contexts: list[str] = []
        self.researched: list[str] = []
        self.posted: list[dict] = []
        self._in_flight = 0
        self.max_in_flight = 0

    async def parse(self, *, system, user, output_format, effort="medium"):
        place = user.split("\n", 1)[0].removeprefix("Place: ")
        if output_format is ResearchSummary:
            self.researched.append(place)
            # Hold the "request" open briefly so overlapping research is observable
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            await asyncio.sleep(0.05)
            self._in_flight -= 1
            return ResearchSummary(region=self.regions[place], summary=f"{place} brief [W][1].")
        if output_format is Assessment:
            return Assessment(
                corroborated_reason="backed by [W][1]",
                corroborated=True,
                has_angle_reason="an ordinary town" if place in self.no_angle else "no road in",
                has_angle=place not in self.no_angle,
                writer_notes="Lead with the ferry.",
            )
        if output_format is FactCheck:
            return FactCheck(unsupported=[])
        assert output_format is Posts
        self.written.append(place)
        return Posts(posts=[f"Why do people live in {place}?", "2/ Origin.", "3/ Economy."])

    async def post_to_x(self, post_text, thread_parts, image_url, dry_run=False, alt_text=None):
        self.posted.append(
            {"text": post_text, "replies": thread_parts, "dry_run": dry_run, "image": image_url}
        )
        return None if dry_run else f"x-{len(self.posted)}"

    async def map_card(self, place_name, wikipedia_url, context, directory):
        self.card_contexts.append(context)
        return {"path": f"/tmp/{place_name}.png", "alt_text": f"Map showing {place_name}."}

    def patches(self):
        source = [{"url": "https://example.org/a", "snippet": "s", "source_name": "Example"}]
        return [
            patch("pipeline.candidate_selector._load_seed_categories", return_value=self.seed),
            patch("pipeline.candidate_selector.layer1_filter", new=AsyncMock(return_value=(True, {}))),
            # Deterministic order: sources and members are tried as listed
            patch("pipeline.candidate_selector.random.shuffle", new=lambda seq: None),
            patch("pipeline.researcher.fetch_wikipedia_extract", new=AsyncMock(return_value="Text.")),
            patch("pipeline.researcher.fetch_web_sources", new=AsyncMock(return_value=source)),
            patch("pipeline.llm.parse", new=self.parse),
            patch("pipeline.publisher.get_image_for_candidate", new=AsyncMock(return_value=None)),
            patch("pipeline.publisher.map_card_for_candidate", new=self.map_card),
            patch("pipeline.publisher.post_to_x", new=self.post_to_x),
        ]

    def __enter__(self):
        self._active = self.patches()
        for p in self._active:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._active:
            p.stop()


async def _statuses(db) -> dict[str, str]:
    return {r["place_name"]: r["status"] for r in await db.fetch("SELECT * FROM candidates")}


async def test_dry_run_previews_then_real_run_posts_same_candidate(db, monkeypatch, tmp_path):
    world = FakeWorld({"island_community": {"places": ["Alpha Island"]}}, {"Alpha Island": "Oceania"})

    with world:
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("PREVIEW_DIR", str(tmp_path))
        assert await run_once() == 1

        preview = (tmp_path / "0001-alpha-island.md").read_text()
        assert preview.startswith("# Alpha Island — thread of 3\n")
        assert "island community · Oceania · [Wikipedia](https://en.wikipedia.org/wiki/Alpha_Island)" in preview
        assert "**1/4** · 35 characters\n\n> Why do people live in Alpha Island?" in preview
        assert "**4/4**" in preview and "> Source: Wikipedia, “Alpha Island”" in preview
        assert "> A new remote, harsh or strange place every day. Follow for more." in preview
        assert "**Image on the first post:** Map showing Alpha Island." in preview
        assert "- **Independently backed:** yes. backed by [W][1]" in preview
        assert "story:** yes. no road in" in preview
        assert "**Editor's notes to the writer:** Lead with the ferry." in preview
        assert "## Web sources (1)\n\n- [Example](https://example.org/a)" in preview
        assert "add the `rejected` label to this issue" in preview
        assert "<!-- candidate:https://en.wikipedia.org/wiki/Alpha_Island -->" in preview

        post = await db.fetchrow("SELECT * FROM posts")
        assert post["dry_run"] is True and post["x_post_id"] is None
        assert post["post_text"] == "Why do people live in Alpha Island?"
        assert post["thread_parts"] == ["2/ Origin.", "3/ Economy."]  # JSONB round-trip
        assert await _statuses(db) == {"Alpha Island": "approved"}
        assert await db.fetchval("SELECT count(*) FROM variety_log") == 0
        candidate = await db.fetchrow("SELECT region, layer1_passed FROM candidates")
        assert candidate["region"] == "Oceania" and candidate["layer1_passed"] is True
        research = await db.fetchrow("SELECT * FROM research")
        assert research["web_sources"][0]["source_name"] == "Example"
        assessment = await db.fetchrow("SELECT * FROM assessments")
        assert assessment["passed"] and assessment["writer_notes"] == "Lead with the ferry."

        # A second dry run has nothing new to preview
        assert await run_once() == 0

        monkeypatch.setenv("DRY_RUN", "false")
        assert await run_once() == 1

    # The real post overwrote the preview with a record that links to it on X
    assert len(list(tmp_path.iterdir())) == 1
    record = (tmp_path / "0001-alpha-island.md").read_text()
    assert "**Posted to X:** https://x.com/i/status/x-2" in record
    assert "Posted to X" not in preview
    assert "`rejected` label" not in record  # a published post can no longer be rejected
    assert await _statuses(db) == {"Alpha Island": "posted"}
    assert world.researched == ["Alpha Island"]  # the real run reused the stored research
    assert world.written == ["Alpha Island"]  # ...and posted the previewed text, not a new draft
    real = await db.fetchrow("SELECT * FROM posts WHERE NOT dry_run")
    assert real["x_post_id"] == "x-2"
    log = await db.fetchrow("SELECT region, story_category FROM variety_log")
    assert tuple(log) == ("Oceania", "island_community")
    # The closing reply carries the source link
    assert world.posted[-1]["replies"][-1] == (
        "Source: Wikipedia, “Alpha Island”\n\n"
        "A new remote, harsh or strange place every day. Follow for more."
    )
    # The first post carries the map card, labelled with the story type and region
    assert world.posted[-1]["image"] == "/tmp/Alpha Island.png"
    assert world.card_contexts[-1] == "island community · Oceania"
    assert await db.fetchval("SELECT image_url FROM posts WHERE NOT dry_run") == "Alpha Island.png"


async def test_variety_rotates_story_category(db):
    seed = {
        "island_community": {"places": ["Alpha Island", "Beta Island"]},
        "exclave": {"places": ["Gamma Town"]},
    }
    regions = {"Alpha Island": "Oceania", "Beta Island": "Asia", "Gamma Town": "Europe"}

    with FakeWorld(seed, regions) as world:
        assert await run_once(posts=3) == 3

    order = [p["text"].removeprefix("Why do people live in ").rstrip("?") for p in world.posted]
    # Never the same story type twice in a row while another is available
    assert order == ["Alpha Island", "Gamma Town", "Beta Island"]


async def test_parallel_prepares_candidates_together_and_keeps_variety(db):
    seed = {
        "island_community": {"places": ["Alpha Island", "Beta Island"]},
        "exclave": {"places": ["Gamma Town"]},
    }
    regions = {"Alpha Island": "Oceania", "Beta Island": "Asia", "Gamma Town": "Europe"}

    with FakeWorld(seed, regions) as world:
        assert await run_once(posts=3, parallel=3) == 3

    assert world.max_in_flight == 3  # all three were researched at the same time
    assert sorted(world.researched) == ["Alpha Island", "Beta Island", "Gamma Town"]
    order = [p["text"].removeprefix("Why do people live in ").rstrip("?") for p in world.posted]
    # Prepared together, but still posted one at a time with story types alternating
    assert order == ["Alpha Island", "Gamma Town", "Beta Island"]


async def test_parallel_batch_spreads_across_story_types(db):
    seed = {
        "island_community": {"places": ["Alpha Island", "Beta Island", "Delta Island"]},
        "exclave": {"places": ["Gamma Town"]},
    }
    regions = dict.fromkeys(["Alpha Island", "Beta Island", "Delta Island"], "Oceania")
    regions["Gamma Town"] = "Europe"

    with FakeWorld(seed, regions) as world:
        assert await run_once(posts=1, parallel=2) == 1

    # With islands listed first, a batch of two would once have been two islands
    assert sorted(world.researched) == ["Alpha Island", "Gamma Town"]


async def test_sequential_run_prepares_one_candidate_at_a_time(db):
    seed = {"island_community": {"places": ["Alpha Island", "Beta Island"]}}
    regions = {"Alpha Island": "Oceania", "Beta Island": "Asia"}

    with FakeWorld(seed, regions) as world:
        assert await run_once() == 1

    # The daily run does no speculative work: one post, one candidate researched
    assert world.researched == ["Alpha Island"] and world.max_in_flight == 1
    assert await _statuses(db) == {"Alpha Island": "posted"}


async def test_region_rule_yields_when_nothing_else_is_left(db):
    seed = {"island_community": {"places": ["Alpha Island", "Beta Island"]}}
    regions = {"Alpha Island": "Oceania", "Beta Island": "Oceania"}

    with FakeWorld(seed, regions):
        assert await run_once(posts=2) == 2  # a repeat region beats no post

    assert await _statuses(db) == {"Alpha Island": "posted", "Beta Island": "posted"}


async def test_same_region_candidate_is_deferred_not_wasted(db):
    seed = {"island_community": {"places": ["Alpha Island", "Beta Island", "Gamma Island"]}}
    regions = {"Alpha Island": "Oceania", "Beta Island": "Oceania", "Gamma Island": "Europe"}

    with FakeWorld(seed, regions) as world:
        assert await run_once() == 1
        assert await run_once() == 1
        # Beta shares Alpha's region, so it was prepared, parked, and Gamma went out
        assert await _statuses(db) == {
            "Alpha Island": "posted",
            "Beta Island": "assessed",
            "Gamma Island": "posted",
        }
        assert await run_once() == 1

    assert (await _statuses(db))["Beta Island"] == "posted"
    assert world.researched == ["Alpha Island", "Beta Island", "Gamma Island"]  # Beta only once
    regions_logged = [r["region"] for r in await db.fetch("SELECT region FROM variety_log ORDER BY id")]
    assert regions_logged == ["Oceania", "Europe", "Oceania"]


async def test_candidate_failing_a_gate_is_skipped_and_next_one_posts(db):
    seed = {"island_community": {"places": ["Dull Island", "Alpha Island"]}}
    regions = {"Dull Island": "Asia", "Alpha Island": "Oceania"}

    with FakeWorld(seed, regions, no_angle=("Dull Island",)) as world:
        assert await run_once() == 1
        # Nothing left: the skipped candidate is never retried
        assert await run_once() == 0

    assert await _statuses(db) == {"Dull Island": "skipped", "Alpha Island": "posted"}
    assert len(world.posted) == 1
    assert await db.fetchval("SELECT passed FROM assessments ORDER BY id LIMIT 1") is False
    assert world.written == ["Alpha Island"]  # nothing was written for the skipped place
