"""Write a dry-run post to a Markdown file, for review before anything is published."""

import re
from pathlib import Path

from pipeline import review
from pipeline.xtext import weighted_length


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "post"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


async def write_preview(
    db_pool,
    candidate_id: int,
    parts: list[str],
    directory: str,
    x_post_id: str | None = None,
    image_note: str | None = None,
) -> Path:
    """Render one post, with the editor's verdict and sources, to `directory`.

    `parts` is every post in order, including the closing source reply. The first
    line of the file is a `# ` heading that doubles as a notification title. Pass
    `x_post_id` for a post that has gone out, and the file links to it on X.
    `image_note` describes the image attached to the first post, if there is one.
    """
    row = await db_pool.fetchrow(
        """
        SELECT c.place_name, c.story_category, c.region, c.wikipedia_url,
               a.corroborated, a.corroborated_reason, a.has_angle, a.has_angle_reason,
               a.writer_notes,
               (SELECT web_sources FROM research WHERE candidate_id = c.id
                 ORDER BY researched_at DESC LIMIT 1) AS web_sources
        FROM candidates c
        LEFT JOIN LATERAL (
            SELECT * FROM assessments WHERE candidate_id = c.id
            ORDER BY assessed_at DESC LIMIT 1
        ) a ON true
        WHERE c.id = $1
        """,
        candidate_id,
    )
    # The closing source reply isn't part of the written post
    written = len(parts) - 1
    shape = "single post" if written == 1 else f"thread of {written}"
    category = (row["story_category"] or "").replace("_", " ")
    lines = [
        f"# {row['place_name']} — {shape}",
        "",
        f"{category} · {row['region'] or 'region unknown'} · [Wikipedia]({row['wikipedia_url']})",
        "",
        *([f"**Posted to X:** https://x.com/i/status/{x_post_id}", ""] if x_post_id else []),
        *([f"**Image on the first post:** {image_note}", ""] if image_note else []),
        "## Post",
        "",
    ]
    for i, part in enumerate(parts, start=1):
        quoted = "\n".join(f"> {line}" for line in part.splitlines())
        lines += [f"**{i}/{len(parts)}** · {weighted_length(part)} characters", "", quoted, ""]

    if row["corroborated"] is not None:
        lines += [
            "## Why it was chosen",
            "",
            f"- **Independently backed:** {_yes_no(row['corroborated'])}."
            f" {row['corroborated_reason'] or ''}".rstrip(),
            f"- **A real \"why would anyone live there?\" story:** {_yes_no(row['has_angle'])}."
            f" {row['has_angle_reason'] or ''}".rstrip(),
            "",
        ]
        if row["writer_notes"]:
            lines += ["**Editor's notes to the writer:** " + row["writer_notes"], ""]

    # One line per site: the passages themselves stay in the database
    sources: dict[str, str] = {}
    for source in row["web_sources"] or []:
        sources.setdefault(source["url"], source["source_name"])
    lines += [f"## Web sources ({len(sources)})", ""]
    lines += [f"- [{name}]({url})" for url, name in sources.items()] or ["None found."]

    if not x_post_id:
        lines += ["", "To stop this place being posted, add the `rejected` label to this issue."]
    lines += ["", review.marker(row["wikipedia_url"])]

    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    file = path / f"{candidate_id:04d}-{_slug(row['place_name'])}.md"
    file.write_text("\n".join(lines) + "\n")
    return file
