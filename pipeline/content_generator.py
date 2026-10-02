"""Generate post copy (a single post or a thread) from the research and the editor's notes."""

from pydantic import BaseModel, Field

from pipeline import llm
from pipeline.xtext import MAX_POST_LENGTH, weighted_length

# Given more room the writer fills it: with a cap of five, every thread came back as five.
MAX_POSTS = 4


class Posts(BaseModel):
    posts: list[str] = Field(
        description="The post, or the posts of a thread in order with the hook first."
    )


_SYSTEM = f"""\
You write for "Why Do People Live Here?", an X account that explains, one place at a \
time, why anyone lives somewhere remote, harsh or strange. Readers are curious \
generalists scrolling a feed: the post has to make them stop, then leave them knowing \
the real reason the place exists.

Voice: plain, specific and a little dry. Prefer a concrete number, name or object to an \
adjective. Do not use hashtags, emojis, or tourism-brochure language.

Accuracy: use only facts stated in the research brief. The brief's source tags ([W], \
[1], ...) are for your reference — never reproduce them. If the brief marks a claim as \
disputed, either leave it out or say it is disputed. The editor's notes tell you what \
is strongest in the brief; build on those.

Length: you decide it from the material. Every post is at most {MAX_POST_LENGTH} characters.
- One post, when the whole answer fits: open with the fact that makes living there \
seem unlikely, name the place and where it is, and say why people live there. No \
numbering.
- A thread, for a place with a real story: three posts, or {MAX_POSTS} at most. The \
brief will hold far more good material than fits. Choosing what to leave out is the \
job: keep the few facts a reader would repeat to someone else and drop the rest.

In a thread, the first post is the hook, and most readers see only that one. Open with \
the fact that makes living there seem unlikely — not with coordinates, distances or a \
census figure. Name the place and say where it is in a few words. End it with the \
question itself: "Why do people live here?", or "Why did people live here?" for an \
abandoned place. It has to make sense with nothing after it. The posts that follow \
answer the question — how the place began, what keeps people there, the details that \
make it real — one idea per post, each starting with its number and a slash, like \
"2/". End on the strongest fact, not on a summary."""


def _problem(posts: list[str]) -> str | None:
    """Why this draft can't be posted as written, or None if it can."""
    if not 1 <= len(posts) <= MAX_POSTS:
        return f"it had {len(posts)} posts"
    too_long = [i for i, p in enumerate(posts, start=1) if weighted_length(p) > MAX_POST_LENGTH]
    if too_long:
        return f"posts {too_long} were over {MAX_POST_LENGTH} characters"
    return None


async def generate_posts(place_name: str, raw_research: str, writer_notes: str | None) -> list[str]:
    """Write the post for a place: a list of one post, or of a thread's posts in order.

    Limits are checked in code; a draft that breaks them is sent back once for fixing.
    """
    user = (
        f"Place: {place_name}\n\n"
        f"<research_brief>\n{raw_research}\n</research_brief>\n\n"
        f"<editor_notes>\n{writer_notes or '(none)'}\n</editor_notes>"
    )
    draft = await llm.parse(system=_SYSTEM, user=user, output_format=Posts)
    if issue := _problem(draft.posts):
        previous = "\n".join(draft.posts)
        retry = f"{user}\n\nYour previous draft broke the format: {issue}. Fix it:\n{previous}"
        draft = await llm.parse(system=_SYSTEM, user=retry, output_format=Posts)
        if issue := _problem(draft.posts):
            raise ValueError(f"Post for {place_name} still invalid: {issue}")
    return draft.posts


async def run_content_generation(db_pool, candidate_id: int) -> dict:
    """Write the content for a candidate.

    Returns {"post_text": first post, "thread_parts": follow-up posts, or None for a
    single post}.
    """
    row = await db_pool.fetchrow(
        """
        SELECT c.place_name,
               (SELECT raw_research FROM research WHERE candidate_id = c.id
                 ORDER BY researched_at DESC LIMIT 1) AS raw_research,
               (SELECT writer_notes FROM assessments WHERE candidate_id = c.id
                 ORDER BY assessed_at DESC LIMIT 1) AS writer_notes
        FROM candidates c
        WHERE c.id = $1
        """,
        candidate_id,
    )
    posts = await generate_posts(row["place_name"], row["raw_research"], row["writer_notes"])
    return {"post_text": posts[0], "thread_parts": posts[1:] or None}
