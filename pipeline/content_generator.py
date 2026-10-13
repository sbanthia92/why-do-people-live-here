"""Generate post copy (a single post or a thread) from the research and the editor's notes."""

from pydantic import BaseModel, Field
from rich.console import Console

from pipeline import llm
from pipeline.xtext import MAX_POST_LENGTH, weighted_length

# Given more room the writer fills it: with a cap of five, every thread came back as five.
MAX_POSTS = 4
# How many times a draft that fails the format or fact check is sent back to the writer
MAX_REWRITES = 2

console = Console()


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


class UnsupportedClaim(BaseModel):
    post: int = Field(description="Which post the claim is in, counting from 1.")
    claim: str = Field(description="The claim, quoted from the post.")
    problem: str = Field(description="What the brief says instead, or that it says nothing.")


class FactCheck(BaseModel):
    unsupported: list[UnsupportedClaim] = Field(
        description="Every claim the brief does not support. Empty if the draft is clean."
    )


_FACT_CHECK_SYSTEM = """\
You are fact-checking a draft post against the research brief it was written from. The \
brief is the only permitted source: a claim counts as supported only if the brief \
states it. Your own knowledge of the place does not count, in either direction.

Go through the draft one claim at a time — every number, date, name, place, quotation \
and statement of cause. Report a claim when:
- the brief does not state it;
- it differs from the brief: a different figure, date, name or spelling, or a quotation \
that is not word for word;
- it is stronger than the brief: "about" or "up to" dropped, a range narrowed to one \
number, "is said to" turned into fact;
- the brief marks it as disputed or uncertain and the draft states it flatly.

Do not report: rounding the brief itself would accept (1,412 km as "about 1,400 km"), \
plain rewording, the account's framing question, or the absence of source tags, which \
the writer is told to leave out.

For each problem give the post number, the claim as written, and what the brief says \
instead. If the draft is clean, return an empty list."""


def _format_problem(posts: list[str]) -> str | None:
    """Why this draft can't be posted as written, or None if it can."""
    if not 1 <= len(posts) <= MAX_POSTS:
        return f"it had {len(posts)} posts (the limit is {MAX_POSTS})"
    too_long = [i for i, p in enumerate(posts, start=1) if weighted_length(p) > MAX_POST_LENGTH]
    if too_long:
        return f"posts {too_long} were over {MAX_POST_LENGTH} characters"
    return None


async def check_facts(place_name: str, raw_research: str, posts: list[str]) -> list[UnsupportedClaim]:
    """Compare a draft with its research brief; returns the claims the brief does not support."""
    draft = "\n\n".join(f"Post {i}: {post}" for i, post in enumerate(posts, start=1))
    user = (
        f"Place: {place_name}\n\n"
        f"<research_brief>\n{raw_research}\n</research_brief>\n\n"
        f"<draft>\n{draft}\n</draft>"
    )
    result = await llm.parse(system=_FACT_CHECK_SYSTEM, user=user, output_format=FactCheck)
    return result.unsupported


async def generate_posts(place_name: str, raw_research: str, writer_notes: str | None) -> list[str]:
    """Write the post for a place: a list of one post, or of a thread's posts in order.

    A draft has to pass two checks before it is returned: the format limits, checked
    in code, and a fact check of every claim against the research brief. A draft that
    fails is sent back with the reasons, up to MAX_REWRITES times; if it still fails,
    ValueError is raised and the place is not posted.
    """
    user = (
        f"Place: {place_name}\n\n"
        f"<research_brief>\n{raw_research}\n</research_brief>\n\n"
        f"<editor_notes>\n{writer_notes or '(none)'}\n</editor_notes>"
    )
    prompt = user
    for attempt in range(MAX_REWRITES + 1):
        posts = (await llm.parse(system=_SYSTEM, user=prompt, output_format=Posts)).posts

        if issue := _format_problem(posts):
            feedback = f"Your previous draft broke the format: {issue}. Fix it."
        else:
            unsupported = await check_facts(place_name, raw_research, posts)
            if not unsupported:
                if attempt:
                    console.log(f"[writer] {place_name}: passed after {attempt} rewrite(s)")
                return posts
            issue = "; ".join(f'post {c.post}: "{c.claim}" ({c.problem})' for c in unsupported)
            console.log(f"[writer] {place_name}: fact check found {len(unsupported)} problem(s)")
            feedback = (
                "A fact-checker compared your previous draft with the research brief and found"
                " claims the brief does not support. Correct each one to match the brief, or"
                " cut it. Do not add new claims while fixing these.\n"
                + "\n".join(
                    f'- Post {c.post}: "{c.claim}" — {c.problem}' for c in unsupported
                )
            )
        previous = "\n".join(posts)
        prompt = f"{user}\n\n{feedback}\n\n<previous_draft>\n{previous}\n</previous_draft>"

    raise ValueError(f"Post for {place_name} still failing after {MAX_REWRITES} rewrites: {issue}")


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
