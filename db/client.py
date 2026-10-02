"""asyncpg connection pool factory and shared query helpers."""

import json
from typing import Any

import asyncpg


async def _init_connection(conn: asyncpg.Connection) -> None:
    # Let callers pass/receive plain dicts and lists for JSONB columns
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def create_pool(database_url: str) -> asyncpg.Pool:
    """Create and return an asyncpg connection pool from DATABASE_URL."""
    return await asyncpg.create_pool(database_url, min_size=1, max_size=5, init=_init_connection)


async def execute(pool: asyncpg.Pool, query: str, *args: Any) -> str:
    """Execute a write query and return the status string."""
    return await pool.execute(query, *args)


async def fetch(pool: asyncpg.Pool, query: str, *args: Any) -> list[asyncpg.Record]:
    """Execute a read query and return all rows."""
    return await pool.fetch(query, *args)


async def fetchrow(pool: asyncpg.Pool, query: str, *args: Any) -> asyncpg.Record | None:
    """Execute a read query and return a single row or None."""
    return await pool.fetchrow(query, *args)
