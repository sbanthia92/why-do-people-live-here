"""Write a dry-run post to a Markdown file, for review before anything is published."""

import re
from pathlib import Path

from pipeline.scorer import DIMENSIONS


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "post"


async def write_preview(db_pool, candidate_id: int, parts: list[str], directory: str) -> Path:
    """Render one previewed post, with its scores and sources, to `directory`.

    `parts` is every post in order, including the closing source reply. The first
    line of the file is a `# ` heading that doubles as a notification title.
    """
    row = await db_pool.fetchrow(
        """
        SELECT c.place_name, c.story_category, c.region, c.wikipedia_url,
               s.named_origin, s.economic_engine, s.contrast_surprise, s.vivid_detail,
               s.corroboration, s.total_weighted, s.post_format, s.justifications,
               (SELECT web_sources FROM research WHERE candidate_id = c.id
                 ORDER BY researched_at DESC LIMIT 1) AS web_sources
        FROM candidates c
        LEFT JOIN LATERAL (
            SELECT * FROM scores WHERE candidate_id = c.id ORDER BY scored_at DESC LIMIT 1
        ) s ON true
        WHERE c.id = $1
        """,
        candidate_id,
    )
    category = (row["story_category"] or "").replace("_", " ")
    lines = [
        f"# {row['place_name']} — {row['post_format']}, {float(row['total_weighted']):g}/10",
        "",
        f"{category} · {row['region'] or 'region unknown'} · [Wikipedia]({row['wikipedia_url']})",
        "",
        "## Post",
        "",
    ]
    for i, part in enumerate(parts, start=1):
        quoted = "\n".join(f"> {line}" for line in part.splitlines())
        lines += [f"**{i}/{len(parts)}** · {len(part)} characters", "", quoted, ""]

    justifications = row["justifications"] or {}
    lines += ["## Scores", "", "| Dimension | Score | Why |", "|---|---|---|"]
    for dim in DIMENSIONS:
        why = str(justifications.get(dim, "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {dim.replace('_', ' ')} | {row[dim]}/2 | {why} |")

    # One line per site: the passages themselves stay in the database
    sources: dict[str, str] = {}
    for source in row["web_sources"] or []:
        sources.setdefault(source["url"], source["source_name"])
    lines += ["", f"## Web sources ({len(sources)})", ""]
    lines += [f"- [{name}]({url})" for url, name in sources.items()] or ["None found."]

    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    file = path / f"{candidate_id:04d}-{_slug(row['place_name'])}.md"
    file.write_text("\n".join(lines) + "\n")
    return file
