"""Let the owner reject a previewed place from its GitHub issue.

Each preview file ends with a hidden marker naming the place's Wikipedia URL. The owner adds
the `rejected` label to the preview issue; the workflow passes the labelled issues to the
runner, which marks those places `skipped` before posting anything.
"""

import json
import re

_MARKER = re.compile(r"<!-- candidate:(\S+) -->")


def marker(wikipedia_url: str) -> str:
    """The hidden line that ties a preview issue back to its place."""
    return f"<!-- candidate:{wikipedia_url} -->"


def rejected_urls(issues_json: str) -> list[str]:
    """Wikipedia URLs named by the markers in `gh issue list --json body` output."""
    issues = json.loads(issues_json or "[]")
    urls = (url for issue in issues for url in _MARKER.findall(issue.get("body") or ""))
    return sorted(set(urls))


async def reject_candidates(conn, urls: list[str]) -> list[str]:
    """Mark rejected places `skipped`; returns their names.

    Only places that have not gone out are touched: a place already `posted` stays posted,
    and one already `skipped` is left alone, so applying the same rejection twice is safe.
    """
    rows = await conn.fetch(
        """
        UPDATE candidates SET status = 'skipped', updated_at = now()
        WHERE wikipedia_url = ANY($1::text[]) AND status IN ('assessed', 'approved')
        RETURNING place_name
        """,
        urls,
    )
    return [row["place_name"] for row in rows]
