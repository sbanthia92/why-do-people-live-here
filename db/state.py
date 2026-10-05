"""Save the database to plain files and load it back.

The pipeline's memory (which places were posted or rejected, what is queued, the last
post's region) has to survive between runs, and a scheduled run starts on a fresh
machine each time. Instead of a hosted database, the state is kept as files committed
to the repository: one JSON-lines file per table, one row per line, ordered by id so
that a day's run shows up in git as a few added lines.
"""

from pathlib import Path

import asyncpg

# In dependency order: a table comes after the tables its rows refer to
TABLES = ("candidates", "research", "assessments", "posts", "variety_log")
# Left out of the saved state. The article text is only needed while a place is being
# researched, it can be fetched again, and it is most of the database's bulk.
_OMITTED_COLUMNS = {"research": ("wikipedia_extract",)}


async def export_state(database_url: str, directory: Path) -> None:
    """Write every table to `directory` as <table>.jsonl."""
    directory.mkdir(parents=True, exist_ok=True)
    conn = await asyncpg.connect(database_url)
    try:
        for table in TABLES:
            row = "to_jsonb(t)" + "".join(f" - '{c}'" for c in _OMITTED_COLUMNS.get(table, ()))
            lines = await conn.fetch(f"SELECT ({row})::text FROM {table} t ORDER BY id")
            (directory / f"{table}.jsonl").write_text("".join(f"{r[0]}\n" for r in lines))
    finally:
        await conn.close()


async def import_state(database_url: str, directory: Path) -> int:
    """Replace the database's contents with the state in `directory`.

    Missing files are treated as empty tables, so a first run can start from nothing.
    Returns the number of rows loaded.
    """
    conn = await asyncpg.connect(database_url)
    loaded = 0
    try:
        async with conn.transaction():
            await conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
            for table in TABLES:
                file = directory / f"{table}.jsonl"
                rows = file.read_text().splitlines() if file.exists() else []
                if not rows:
                    continue
                # Postgres converts each JSON object back into a typed row
                await conn.execute(
                    f"INSERT INTO {table} SELECT * FROM jsonb_populate_recordset(NULL::{table}, $1::jsonb)",
                    "[" + ",".join(rows) + "]",
                )
                # Rows came in with their ids, so move the id counter past them
                await conn.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), max(id)) FROM {table}"
                )
                loaded += len(rows)
    finally:
        await conn.close()
    return loaded
