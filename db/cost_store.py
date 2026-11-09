"""Keep what each place costs in a private database, completely outside the repository.

The daily run records costs in its own throwaway database, as before. At the end of the run
they are copied to a separate hosted Postgres whose address is the COST_DATABASE_URL secret.
Nothing about costs is saved in the repository, in the clear or encrypted, and the workflow
holds no key that could be used to read it. The connection must use TLS unless the database
is on the same machine.
"""

from urllib.parse import parse_qs, urlparse

import asyncpg

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_TLS_MODES = {"require", "verify-ca", "verify-full"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS costs (
    run_name      TEXT NOT NULL,        -- which run saved this row (the Actions run id)
    wikipedia_url TEXT NOT NULL,        -- identifies the place, whatever its id in the state
    place_name    TEXT,
    stage         TEXT NOT NULL,
    model         TEXT NOT NULL,
    requests      INTEGER NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    usd           NUMERIC(10, 4) NOT NULL,
    recorded_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (run_name, wikipedia_url, stage, model)
)
"""


def _tls_kwargs(url: str) -> dict:
    """Connection settings that make a remote database use TLS; refuses a URL that opts out."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    host = (query.get("host") or [parsed.hostname or ""])[0]
    if host in _LOCAL_HOSTS or host.startswith("/"):
        return {}
    mode = (query.get("sslmode") or [None])[0]
    if mode is None:
        return {"ssl": "require"}
    if mode in _TLS_MODES:
        return {}  # the URL asks for TLS itself, possibly with certificate checks
    raise ValueError("The cost database URL must use sslmode=require (or stricter).")


async def push(local_url: str, remote_url: str, run_name: str) -> int:
    """Copy this run's costs to the private database. Returns the number of rows.

    Safe to repeat for the same `run_name`: rows already there are left alone.
    """
    local = await asyncpg.connect(local_url)
    try:
        rows = await local.fetch(
            """
            SELECT c.wikipedia_url, c.place_name, k.stage, COALESCE(k.model, '') AS model,
                   sum(k.requests)::int AS requests,
                   sum(k.input_tokens)::int AS input_tokens,
                   sum(k.output_tokens)::int AS output_tokens,
                   sum(k.usd) AS usd, min(k.recorded_at) AS recorded_at
            FROM costs k JOIN candidates c ON c.id = k.candidate_id
            GROUP BY c.wikipedia_url, c.place_name, k.stage, k.model
            """
        )
    finally:
        await local.close()
    if not rows:
        return 0
    remote = await asyncpg.connect(remote_url, **_tls_kwargs(remote_url))
    try:
        await remote.execute(_SCHEMA)
        await remote.executemany(
            """
            INSERT INTO costs (run_name, wikipedia_url, place_name, stage, model, requests,
                               input_tokens, output_tokens, usd, recorded_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT DO NOTHING
            """,
            [
                (
                    run_name,
                    r["wikipedia_url"],
                    r["place_name"],
                    r["stage"],
                    r["model"],
                    r["requests"],
                    r["input_tokens"],
                    r["output_tokens"],
                    r["usd"],
                    r["recorded_at"],
                )
                for r in rows
            ],
        )
    finally:
        await remote.close()
    return len(rows)


async def fetch(remote_url: str) -> list[dict]:
    """Every saved cost row, oldest first. `recorded_at` is an ISO timestamp string."""
    remote = await asyncpg.connect(remote_url, **_tls_kwargs(remote_url))
    try:
        await remote.execute(_SCHEMA)  # a fresh database reads as empty, not as an error
        rows = await remote.fetch("SELECT * FROM costs ORDER BY recorded_at, run_name")
    finally:
        await remote.close()
    return [{**dict(r), "recorded_at": r["recorded_at"].isoformat()} for r in rows]
