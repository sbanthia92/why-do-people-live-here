"""Tests for pipeline.candidate_selector."""

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_summary_response(extract: str, with_description: bool = True) -> dict:
    data = {"extract": extract, "title": "Test Place"}
    if with_description:
        data["description"] = "A place in some country"
    return data


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


# ---------------------------------------------------------------------------
# layer1_filter — pass cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_layer1_filter_pass_long_extract_with_economy_section():
    """Passes when extract >= 500 chars and Economy section present."""
    from pipeline.candidate_selector import layer1_filter

    long_extract = "A" * 600
    summary_data = _make_summary_response(long_extract)
    sections_data = _make_sections_response(["History", "Economy", "Demographics"])

    responses = [
        _mock_http_response(summary_data),
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

    summary_data = _make_summary_response("B" * 800)
    sections_data = _make_sections_response(["History", "Demographics"])

    responses = [_mock_http_response(summary_data), _mock_http_response(sections_data)]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_client_sequential(responses)
        passed, meta = await layer1_filter(
            "https://en.wikipedia.org/wiki/Some_Place"
        )

    assert passed is True


# ---------------------------------------------------------------------------
# layer1_filter — fail cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_layer1_filter_fail_short_extract():
    """Fails when extract is below 500 chars."""
    from pipeline.candidate_selector import layer1_filter

    summary_data = _make_summary_response("Short extract." * 10)  # ~140 chars
    sections_data = _make_sections_response(["Economy"])

    responses = [_mock_http_response(summary_data), _mock_http_response(sections_data)]

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
async def test_layer1_filter_fail_no_required_section():
    """Fails when no Economy/History/Background/Origins section found."""
    from pipeline.candidate_selector import layer1_filter

    summary_data = _make_summary_response("A" * 600)
    sections_data = _make_sections_response(["Geography", "Demographics", "Culture"])

    responses = [_mock_http_response(summary_data), _mock_http_response(sections_data)]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_client_sequential(responses)
        passed, meta = await layer1_filter(
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
    summary_data = _make_summary_response(long_extract)
    sections_data = _make_sections_response(["Economy", "History"])
    http_responses = [_mock_http_response(summary_data), _mock_http_response(sections_data)]

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
async def test_select_candidate_variety_rotation_returns_none_on_blocked_region():
    """Returns None early when last 3 variety_log entries share same region."""
    from pipeline.candidate_selector import select_candidate

    # 3 consecutive posts from same region — variety guard should trigger
    db_conn = _make_db_conn(
        fetch_results={"variety_log": [
            {"region": "North America"},
            {"region": "North America"},
            {"region": "North America"},
            {"region": "Europe"},
            {"region": "Europe"},
        ]},
        fetchrow_map={},
    )

    # All members are already in DB so select_candidate gives up → None
    members = [
        {"title": "Ghost Town A", "pageid": 1,
         "wikipedia_url": "https://en.wikipedia.org/wiki/Ghost_Town_A"},
    ]
    db_conn.fetchrow.return_value = {"id": 10, "layer1_passed": True}

    with patch("pipeline.candidate_selector.fetch_category_members", return_value=members):
        result = await select_candidate("ghost_town", db_conn)

    assert result is None


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
