"""Tests for pipeline.candidate_selector."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_summary_response(
    extract: str, with_description: bool = True, with_coordinates: bool = True
) -> dict:
    data = {"extract": extract, "title": "Test Place"}
    if with_description:
        data["description"] = "A place in some country"
    if with_coordinates:
        data["coordinates"] = {"lat": 38.2, "lon": -119.0}
    return data


def _make_extract_response(full_text: str) -> dict:
    """Full-article plain text from action=query&prop=extracts (formatversion=2)."""
    return {"query": {"pages": [{"title": "Test Place", "extract": full_text}]}}


def _make_sections_response(section_names: list[str]) -> dict:
    return {
        "parse": {
            "sections": [{"line": name, "number": str(i)} for i, name in enumerate(section_names)]
        }
    }


# ---------------------------------------------------------------------------
# fetch_category_members
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_category_members_returns_correct_structure():
    """Returns list of dicts with title, pageid, wikipedia_url."""
    from pipeline.candidate_selector import fetch_category_members

    api_response = {
        "query": {
            "categorymembers": [
                {"title": "Bodie, California", "pageid": 123},
                {"title": "Centralia, Pennsylvania", "pageid": 456},
            ]
        }
    }

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = api_response

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await fetch_category_members("Ghost_towns_in_the_United_States")

    assert len(result) == 2
    assert result[0]["title"] == "Bodie, California"
    assert result[0]["pageid"] == 123
    assert "wikipedia_url" in result[0]
    assert "en.wikipedia.org/wiki/" in result[0]["wikipedia_url"]


@pytest.mark.asyncio
async def test_fetch_category_members_timeout_returns_empty():
    """Returns empty list on httpx.TimeoutException."""
    from pipeline.candidate_selector import fetch_category_members

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await fetch_category_members("Ghost_towns_in_the_United_States")

    assert result == []


@pytest.mark.asyncio
async def test_fetch_category_members_descends_one_subcategory_level():
    """Articles in subcategories are included; subcategory entries themselves are not."""
    from pipeline.candidate_selector import fetch_category_members

    parent = {"query": {"categorymembers": [
        {"title": "Bodie, California", "pageid": 1, "ns": 0},
        {"title": "Category:Ghost towns in Nevada", "pageid": 900, "ns": 14},
    ]}}
    child = {"query": {"categorymembers": [
        {"title": "Rhyolite, Nevada", "pageid": 2, "ns": 0},
        {"title": "Bodie, California", "pageid": 1, "ns": 0},  # duplicate across levels
    ]}}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = _mock_client_sequential(
            [_mock_http_response(parent), _mock_http_response(child)]
        )
        mock_client_cls.return_value = mock_client
        result = await fetch_category_members("Ghost_towns_in_the_United_States")

    assert [r["title"] for r in result] == ["Bodie, California", "Rhyolite, Nevada"]
    parent_params = mock_client.get.call_args_list[0].kwargs["params"]
    child_params = mock_client.get.call_args_list[1].kwargs["params"]
    assert parent_params["cmnamespace"] == "0|14"
    assert child_params["cmtitle"] == "Category:Ghost_towns_in_Nevada"
    assert child_params["cmnamespace"] == "0"  # no deeper recursion


@pytest.mark.asyncio
async def test_fetch_category_members_skips_failing_subcategory():
    """A subcategory error doesn't discard the parent's articles."""
    from pipeline.candidate_selector import fetch_category_members

    parent = {"query": {"categorymembers": [
        {"title": "Bodie, California", "pageid": 1, "ns": 0},
        {"title": "Category:Broken", "pageid": 900, "ns": 14},
    ]}}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_client_sequential(
            [_mock_http_response(parent), httpx.TimeoutException("timeout")]
        )
        result = await fetch_category_members("Ghost_towns_in_the_United_States")

    assert [r["title"] for r in result] == ["Bodie, California"]


@pytest.mark.asyncio
async def test_get_retries_on_429_using_retry_after():
    """Rate-limited responses are retried after the server's Retry-After delay."""
    from pipeline.candidate_selector import _get

    limited = MagicMock(status_code=429, headers={"retry-after": "7"})
    ok = MagicMock(status_code=200, headers={})
    client = AsyncMock()
    client.get = AsyncMock(side_effect=[limited, ok])

    with patch("pipeline.candidate_selector.asyncio.sleep", new=AsyncMock()) as sleep:
        resp = await _get(client, "https://example.org")

    assert resp is ok
    sleep.assert_awaited_once_with(7.0)


@pytest.mark.asyncio
async def test_get_gives_up_after_max_retries():
    """Returns the last 429 response (for raise_for_status) instead of looping forever."""
    from pipeline.candidate_selector import _MAX_RETRIES, _get

    limited = MagicMock(status_code=429, headers={"retry-after": "1"})
    client = AsyncMock()
    client.get = AsyncMock(return_value=limited)

    with patch("pipeline.candidate_selector.asyncio.sleep", new=AsyncMock()):
        resp = await _get(client, "https://example.org")

    assert resp is limited
    assert client.get.await_count == _MAX_RETRIES + 1


# ---------------------------------------------------------------------------
# layer1_filter — pass cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_layer1_filter_pass_long_extract_with_economy_section():
    """Passes when extract >= 500 chars and Economy section present."""
    from pipeline.candidate_selector import layer1_filter

    # Lead paragraph is short; the full article is what gets measured
    summary_data = _make_summary_response("A" * 200)
    extract_data = _make_extract_response("A" * 600)
    sections_data = _make_sections_response(["History", "Economy", "Demographics"])

    responses = [
        _mock_http_response(summary_data),
        _mock_http_response(extract_data),
        _mock_http_response(sections_data),
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = _mock_client_sequential(responses)
        mock_client_cls.return_value = mock_client

        passed, meta = await layer1_filter(
            "https://en.wikipedia.org/wiki/Bodie,_California"
        )

    assert passed is True
    assert meta["char_count"] == 600
    assert "Economy" in meta["section_names"]


@pytest.mark.asyncio
async def test_layer1_filter_pass_history_section():
    """Passes when History section is present (not just Economy)."""
    from pipeline.candidate_selector import layer1_filter

    summary_data = _make_summary_response("B" * 200)
    extract_data = _make_extract_response("B" * 800)
    sections_data = _make_sections_response(["History", "Demographics"])

    responses = [
        _mock_http_response(summary_data),
        _mock_http_response(extract_data),
        _mock_http_response(sections_data),
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_client_sequential(responses)
        passed, _ = await layer1_filter(
            "https://en.wikipedia.org/wiki/Some_Place"
        )

    assert passed is True


# ---------------------------------------------------------------------------
# layer1_filter — fail cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_layer1_filter_does_not_double_encode_title():
    """URLs from fetch_category_members are already percent-encoded; don't re-encode them."""
    from pipeline.candidate_selector import layer1_filter

    responses = [
        _mock_http_response(_make_summary_response("C" * 200)),
        _mock_http_response(_make_extract_response("C" * 600)),
        _mock_http_response(_make_sections_response(["History"])),
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = _mock_client_sequential(responses)
        mock_client_cls.return_value = mock_client
        await layer1_filter("https://en.wikipedia.org/wiki/Australia%2C_Cuba")

    summary_url = mock_client.get.call_args_list[0].args[0]
    assert summary_url.endswith("/Australia%2C_Cuba")

@pytest.mark.asyncio
async def test_layer1_filter_fail_short_extract():
    """Fails when the full article text is below 500 chars."""
    from pipeline.candidate_selector import layer1_filter

    summary_data = _make_summary_response("Short extract.")
    extract_data = _make_extract_response("Short extract." * 10)  # ~140 chars
    sections_data = _make_sections_response(["Economy"])

    responses = [
        _mock_http_response(summary_data),
        _mock_http_response(extract_data),
        _mock_http_response(sections_data),
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_client_sequential(responses)
        passed, meta = await layer1_filter(
            "https://en.wikipedia.org/wiki/Short_Place"
        )

    assert passed is False
    assert meta["char_count"] < 500


@pytest.mark.asyncio
async def test_layer1_filter_fail_no_description():
    """Fails when summary has no 'description' field (disambiguation page)."""
    from pipeline.candidate_selector import layer1_filter

    summary_data = _make_summary_response("A" * 600, with_description=False)
    sections_data = _make_sections_response(["Economy"])

    responses = [_mock_http_response(summary_data), _mock_http_response(sections_data)]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_client_sequential(responses)
        passed, _ = await layer1_filter(
            "https://en.wikipedia.org/wiki/Disambiguation_Place"
        )

    assert passed is False


@pytest.mark.asyncio
async def test_layer1_filter_require_coordinates_rejects_non_places():
    """Films, lists and concept articles have no coordinates; crawled sources reject them."""
    from pipeline.candidate_selector import layer1_filter

    def responses():
        return [
            _mock_http_response(_make_summary_response("A" * 200, with_coordinates=False)),
            _mock_http_response(_make_extract_response("A" * 600)),
            _mock_http_response(_make_sections_response(["History"])),
        ]

    url = "https://en.wikipedia.org/wiki/Billy_Elliot"
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_client_sequential(responses())
        strict, _ = await layer1_filter(url, require_coordinates=True)
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_client_sequential(responses())
        lenient, _ = await layer1_filter(url)

    assert strict is False
    assert lenient is True  # hand-picked places aren't required to have coordinates


@pytest.mark.asyncio
async def test_layer1_filter_fail_no_required_section():
    """Fails when no Economy/History/Background/Origins section found."""
    from pipeline.candidate_selector import layer1_filter

    summary_data = _make_summary_response("A" * 200)
    extract_data = _make_extract_response("A" * 600)
    sections_data = _make_sections_response(["Geography", "Demographics", "Culture"])

    responses = [
        _mock_http_response(summary_data),
        _mock_http_response(extract_data),
        _mock_http_response(sections_data),
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_client_sequential(responses)
        passed, _ = await layer1_filter(
            "https://en.wikipedia.org/wiki/Thin_Place"
        )

    assert passed is False


@pytest.mark.asyncio
async def test_layer1_filter_returns_false_on_unexpected_error():
    """Never raises; returns (False, {}) on unexpected error."""
    from pipeline.candidate_selector import layer1_filter

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=RuntimeError("boom"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        passed, meta = await layer1_filter("https://en.wikipedia.org/wiki/Error_Place")

    assert passed is False
    assert meta == {}


# ---------------------------------------------------------------------------
# select_candidate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_candidate_skips_already_known():
    """Candidates already in the DB are skipped."""
    from pipeline.candidate_selector import select_candidate

    members = [
        {"title": "Known Place", "pageid": 1,
         "wikipedia_url": "https://en.wikipedia.org/wiki/Known_Place"},
        {"title": "New Place", "pageid": 2,
         "wikipedia_url": "https://en.wikipedia.org/wiki/New_Place"},
    ]

    db_conn = _make_db_conn(
        fetch_results={"variety_log": []},
        fetchrow_map={
            "https://en.wikipedia.org/wiki/Known_Place": {"id": 99, "layer1_passed": True},
            "https://en.wikipedia.org/wiki/New_Place": None,  # not in DB
        },
    )

    long_extract = "A" * 600
    summary_data = _make_summary_response("A" * 200)
    extract_data = _make_extract_response(long_extract)
    sections_data = _make_sections_response(["Economy", "History"])
    http_responses = [
        _mock_http_response(summary_data),
        _mock_http_response(extract_data),
        _mock_http_response(sections_data),
    ]

    inserted_row = {
        "id": 1, "place_name": "New Place",
        "wikipedia_url": "https://en.wikipedia.org/wiki/New_Place",
        "story_category": "ghost_town", "status": "pending",
        "layer1_passed": True, "created_at": None,
    }
    db_conn.fetchrow.side_effect = _fetchrow_side_effect(
        {
            "https://en.wikipedia.org/wiki/Known_Place": {"id": 99, "layer1_passed": True},
            "https://en.wikipedia.org/wiki/New_Place": None,
        },
        insert_return=inserted_row,
    )

    with (
        patch("pipeline.candidate_selector.fetch_category_members", return_value=members),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client_cls.return_value = _mock_client_sequential(http_responses)
        result = await select_candidate("ghost_town", db_conn)

    assert result is not None
    assert result["place_name"] == "New Place"


@pytest.mark.asyncio
async def test_select_candidate_returns_none_when_all_members_known():
    """Returns None when every member of the category is already in the DB."""
    from pipeline.candidate_selector import select_candidate

    db_conn = _make_db_conn(fetch_results={"variety_log": []}, fetchrow_map={})

    members = [
        {"title": "Ghost Town A", "pageid": 1,
         "wikipedia_url": "https://en.wikipedia.org/wiki/Ghost_Town_A"},
    ]
    db_conn.fetchrow.return_value = {"id": 10, "layer1_passed": True}

    with patch("pipeline.candidate_selector.fetch_category_members", return_value=members):
        result = await select_candidate("ghost_town", db_conn)

    assert result is None



@pytest.mark.asyncio
async def test_select_candidate_uses_hand_picked_places():
    """A story_category with only `places` selects from them without crawling categories."""
    from pipeline.candidate_selector import select_candidate

    seed = {"island_community": {"places": ["Tristan da Cunha"]}}
    url = "https://en.wikipedia.org/wiki/Tristan_da_Cunha"
    db_conn = _make_db_conn(fetch_results={"variety_log": []}, fetchrow_map={})
    db_conn.fetchrow.side_effect = _fetchrow_side_effect(
        {url: None},
        insert_return={"id": 1, "place_name": "Tristan da Cunha", "wikipedia_url": url},
    )
    http_responses = [
        _mock_http_response(_make_summary_response("A" * 200)),
        _mock_http_response(_make_extract_response("A" * 600)),
        _mock_http_response(_make_sections_response(["History"])),
    ]

    with (
        patch("pipeline.candidate_selector._load_seed_categories", return_value=seed),
        patch("pipeline.candidate_selector.fetch_category_members") as fetch_members,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client_cls.return_value = _mock_client_sequential(http_responses)
        result = await select_candidate("island_community", db_conn)

    fetch_members.assert_not_called()
    assert result["wikipedia_url"] == url
    # place_name/url are what get persisted
    insert_args = db_conn.fetchrow.call_args_list[-1].args
    assert insert_args[1:3] == ("Tristan da Cunha", url)


@pytest.mark.asyncio
async def test_run_selector_tries_avoided_category_last():
    """The most recently posted story_category is only used when all others come up empty."""
    from pipeline.candidate_selector import run_selector

    seed = {"ghost_town": {}, "exclave": {}, "isolated": {}}
    tried: list[str] = []

    async def fake_select(story_category, db_conn):
        tried.append(story_category)

    with (
        patch("pipeline.candidate_selector._load_seed_categories", return_value=seed),
        patch("pipeline.candidate_selector.select_candidate", side_effect=fake_select),
    ):
        for _ in range(10):  # order is otherwise random
            tried.clear()
            await run_selector(AsyncMock(), avoid_category="exclave")
            assert tried[-1] == "exclave"
            assert sorted(tried) == ["exclave", "ghost_town", "isolated"]


def test_seed_config_shape():
    """Every story_category has at least one source and only known keys."""
    from pipeline.candidate_selector import _load_seed_categories

    seed = _load_seed_categories()
    assert seed
    for story_category, cfg in seed.items():
        assert set(cfg) <= {"categories", "places"}, story_category
        assert cfg.get("categories") or cfg.get("places"), story_category

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_http_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = data
    return resp


def _mock_client_sequential(responses: list) -> AsyncMock:
    """Return an AsyncClient mock that yields responses in order."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=responses)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _make_db_conn(fetch_results: dict, fetchrow_map: dict) -> AsyncMock:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_results.get("variety_log", []))
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    # fetchrow is configured per-test
    return conn


def _fetchrow_side_effect(url_map: dict, insert_return: dict):
    """Build a side_effect for fetchrow that handles both SELECT and INSERT calls."""
    call_count = 0

    async def _side_effect(query, *args):
        nonlocal call_count
        # SELECT calls pass the URL as the first positional arg
        if args and args[0] in url_map:
            return url_map[args[0]]
        # INSERT/RETURNING call — return the inserted row
        return insert_return

    return _side_effect
