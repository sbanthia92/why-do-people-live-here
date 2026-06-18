"""asyncpg connection pool factory and shared query helpers."""

from typing import Any, Optional
import asyncpg


async def create_pool(database_url: str) -> asyncpg.Pool:
    """Create and return an asyncpg connection pool from DATABASE_URL."""
    pass


async def execute(pool: asyncpg.Pool, query: str, *args: Any) -> str:
    """Execute a write query and return the status string."""
    pass


async def fetch(pool: asyncpg.Pool, query: str, *args: Any) -> list[asyncpg.Record]:
    """Execute a read query and return all rows."""
    pass


async def fetchrow(pool: asyncpg.Pool, query: str, *args: Any) -> Optional[asyncpg.Record]:
    """Execute a read query and return a single row or None."""
    pass
