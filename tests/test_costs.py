"""Tests for per-place cost tracking: the ledger, the pricing in llm, and saving."""

import asyncio
from types import SimpleNamespace

import pytest

from pipeline import costs, llm


@pytest.fixture(autouse=True)
def clean_ledger():
    costs._ledger.clear()
    yield
    costs._ledger.clear()


def _response(model, tokens_in, tokens_out, searches=0):
    usage = SimpleNamespace(
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=searches),
    )
    return SimpleNamespace(model=model, usage=usage)


class FakePool:
    def __init__(self):
        self.rows = []

    async def executemany(self, sql, rows):
        self.rows += rows


def _by_stage(pool):
    return {(r[0], r[1], r[2]): r for r in ((p[0], p[1], p[2], *p[3:]) for p in pool.rows)}


async def test_usage_is_priced_and_attributed_to_the_place_being_worked_on(monkeypatch):
    monkeypatch.setattr(llm, "_usage", {})
    with costs.tracking(7):
        # 1M tokens in at $4 plus 0.5M out at $20 is $14 on the main model
        llm._record_usage(_response(llm.MODEL, 1_000_000, 500_000), "writing")
        # Three searches on the cheaper model: its tokens, plus the per-search fee
        llm._record_usage(_response(llm.SEARCH_MODEL, 1_000_000, 0, searches=3), "web search")

    pool = FakePool()
    await costs.save(pool, 7)

    rows = {(r[1], r[2]): r for r in pool.rows}
    assert rows[("writing", llm.MODEL)][6] == pytest.approx(14.0)
    assert rows[("web search", llm.SEARCH_MODEL)][6] == pytest.approx(2.0)
    fee = rows[("search fee", "web_search")]
    assert (fee[3], fee[6]) == (3, pytest.approx(0.03))
    assert all(r[0] == 7 for r in pool.rows)


async def test_nothing_is_recorded_outside_a_place(monkeypatch):
    monkeypatch.setattr(llm, "_usage", {})
    llm._record_usage(_response(llm.MODEL, 1000, 1000), "writing")
    assert costs._ledger == {}


async def test_save_clears_what_it_saved_and_leaves_other_places_alone():
    costs.add(1, "writing", "m", 0.5)
    costs.add(2, "writing", "m", 0.25)
    pool = FakePool()

    await costs.save(pool, 1)
    await costs.save(pool, 1)  # nothing left to save for place 1

    assert [r[0] for r in pool.rows] == [1]
    assert list(costs._ledger) == [(2, "writing", "m")]


async def test_places_worked_on_at_the_same_time_stay_apart(monkeypatch):
    monkeypatch.setattr(llm, "_usage", {})

    async def work(candidate_id, tokens):
        with costs.tracking(candidate_id):
            await asyncio.sleep(0.01)  # let the other task run in between
            llm._record_usage(_response(llm.MODEL, tokens, 0), "brief")

    await asyncio.gather(work(1, 1_000_000), work(2, 2_000_000))

    pool = FakePool()
    await costs.save(pool, 1)
    await costs.save(pool, 2)
    assert {r[0]: r[6] for r in pool.rows} == {1: pytest.approx(4.0), 2: pytest.approx(8.0)}
