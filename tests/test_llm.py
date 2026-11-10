"""Tests for how pipeline.llm talks to different models."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from pipeline import costs, llm


class Answer(BaseModel):
    ok: bool


def _client(parse):
    client = MagicMock()
    client.beta.messages.parse = parse
    return client


def _response(model="m"):
    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=0, server_tool_use=None)
    return SimpleNamespace(
        model=model, usage=usage, stop_reason="end_turn", parsed_output=Answer(ok=True)
    )


async def test_the_photo_model_is_haiku_and_is_not_sent_the_fallback_setting(monkeypatch):
    parse = AsyncMock(return_value=_response(llm.PHOTO_MODEL))
    monkeypatch.setattr(llm, "get_client", lambda: _client(parse))
    monkeypatch.setattr(llm, "_usage", {})

    await llm.parse(system="s", user="u", output_format=Answer, model=llm.PHOTO_MODEL)

    kwargs = parse.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-5-5"
    # Haiku has no server-side fallback: the setting would change nothing or be rejected
    assert "fallbacks" not in kwargs and "betas" not in kwargs


async def test_the_main_model_still_gets_the_fallback_setting(monkeypatch):
    parse = AsyncMock(return_value=_response(llm.MODEL))
    monkeypatch.setattr(llm, "get_client", lambda: _client(parse))
    monkeypatch.setattr(llm, "_usage", {})

    await llm.parse(system="s", user="u", output_format=Answer)

    kwargs = parse.call_args.kwargs
    assert kwargs["model"] == llm.MODEL
    assert kwargs["fallbacks"] == "default" and kwargs["betas"] == llm._BETAS


async def test_content_blocks_are_passed_through_unchanged(monkeypatch):
    parse = AsyncMock(return_value=_response(llm.PHOTO_MODEL))
    monkeypatch.setattr(llm, "get_client", lambda: _client(parse))
    monkeypatch.setattr(llm, "_usage", {})
    blocks = [{"type": "image", "source": {}}, {"type": "text", "text": "q"}]

    await llm.parse(system="s", user=blocks, output_format=Answer, model=llm.PHOTO_MODEL)

    assert parse.call_args.kwargs["messages"] == [{"role": "user", "content": blocks}]


def test_haiku_usage_is_priced_at_haiku_rates_not_the_main_models(monkeypatch):
    monkeypatch.setattr(llm, "_usage", {})
    costs._ledger.clear()
    with costs.tracking(1):
        llm._record_usage(_response(llm.PHOTO_MODEL), "photo check")

    (entry,) = costs._ledger.values()
    assert entry["usd"] == pytest.approx(0.10)  # a million tokens at $0.10, not $4.00
    costs._ledger.clear()
