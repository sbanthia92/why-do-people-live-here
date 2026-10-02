"""Shared Claude client and request helpers for the pipeline's LLM steps."""

import asyncio
import weakref
from typing import TypeVar

import anthropic
from pydantic import BaseModel
from rich.console import Console

console = Console()

MODEL = "claude-opus-5-5"
# Lets the API re-run a safety-declined request on Anthropic's recommended fallback
# model inside the same call, instead of handing us a refusal.
_BETAS = ["server-side-fallback-2026-07-01"]
_MAX_TOKENS = 16000
# A server-tool turn can pause after ~10 internal iterations; resume at most this often
_MAX_PAUSE_RESUMES = 3

# Candidates are researched in parallel and each fans out into several searches;
# this caps how many Claude requests are in flight at once.
_MAX_CONCURRENT_REQUESTS = 6
# The basic search tool, deliberately. The newer web_search_20260209 runs searches from
# inside a code sandbox: failed sandbox attempts used up the search budget, and the text
# came back with empty citation lists, so no sources could be extracted from it.
_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    # The Wikipedia article is already in hand; these sources must be independent of it
    "blocked_domains": ["wikipedia.org"],
}

T = TypeVar("T", bound=BaseModel)

_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _request_slot() -> asyncio.Semaphore:
    """One semaphore per event loop (a semaphore can't be shared across loops)."""
    loop = asyncio.get_running_loop()
    if loop not in _semaphores:
        _semaphores[loop] = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)
    return _semaphores[loop]

_client: anthropic.AsyncAnthropic | None = None


class LLMRefusalError(RuntimeError):
    """Claude (and its fallback) declined the request."""


def get_client() -> anthropic.AsyncAnthropic:
    """Lazily build the client so importing the pipeline needs no credentials."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


async def parse(*, system: str, user: str, output_format: type[T], effort: str = "medium") -> T:
    """Ask Claude for a response validated against the `output_format` Pydantic model."""
    async with _request_slot():
        response = await get_client().beta.messages.parse(
            model=MODEL,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=output_format,
            output_config={"effort": effort},
            betas=_BETAS,
            fallbacks="default",
        )
    if response.stop_reason == "refusal":
        raise LLMRefusalError(f"Claude declined a {output_format.__name__} request")
    if response.parsed_output is None:
        raise RuntimeError(
            f"No parseable {output_format.__name__} in response"
            f" (stop_reason={response.stop_reason})"
        )
    return response.parsed_output


async def web_search_citations(*, system: str, user: str, max_searches: int = 3) -> list[dict]:
    """Have Claude search the web and return the passages it cited.

    Returns a list of {url, snippet, source_name} — one per distinct cited passage.
    """
    messages: list[dict] = [{"role": "user", "content": user}]
    sources: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for _ in range(_MAX_PAUSE_RESUMES + 1):
        async with _request_slot():
            response = await get_client().beta.messages.create(
                model=MODEL,
                max_tokens=_MAX_TOKENS,
                system=system,
                messages=messages,
                tools=[{**_WEB_SEARCH_TOOL, "max_uses": max_searches}],
                output_config={"effort": "medium"},
                betas=_BETAS,
                fallbacks="default",
            )
        if response.stop_reason == "refusal":
            raise LLMRefusalError("Claude declined a web research request")

        for block in response.content:
            if block.type != "text":
                continue
            for citation in block.citations or []:
                url = getattr(citation, "url", None)
                snippet = getattr(citation, "cited_text", "") or ""
                if not url or (url, snippet) in seen:
                    continue
                seen.add((url, snippet))
                sources.append(
                    {
                        "url": url,
                        "snippet": snippet,
                        "source_name": getattr(citation, "title", None) or url,
                    }
                )

        if response.stop_reason != "pause_turn":
            break
        # Resume the paused server-side loop: re-send with the assistant turn appended
        messages = [messages[0], {"role": "assistant", "content": response.content}]

    return sources
