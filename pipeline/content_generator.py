"""Generate post copy (caption + optional thread parts) from research and scores."""

from pydantic import BaseModel, Field

from pipeline import llm

MAX_TWEET_CHARS = 280
MIN_THREAD_PARTS = 3
MAX_THREAD_PARTS = 5


class SinglePost(BaseModel):
    text: str = Field(description="The complete post.")


class Thread(BaseModel):
    tweets: list[str] = Field(description="The posts in order; the first is the hook.")


_VOICE = """\
You write for "Why Do People Live Here?", an X account that explains, one place at a \
time, why anyone lives somewhere remote, harsh or strange. Readers are curious \
generalists scrolling a feed: the post has to make them stop, then leave them knowing \
the real reason the place exists.

Voice: plain, specific and a little dry. Lead with the most surprising true fact. \
Prefer a concrete number, name or object to an adjective. Do not use hashtags, emojis, \
rhetorical questions other than the account's own framing, or tourism-brochure language.

Accuracy: use only facts stated in the research brief. The brief's source tags ([W], \
[1], ...) are for your reference — never reproduce them in the post. If the brief marks \
a claim as disputed, either leave it out or say it is disputed. The scorer's notes tell \
you which parts of the story were judged strongest; build on those."""

_SINGLE_SYSTEM = (
    _VOICE
    + f"""

Write one post of at most {MAX_TWEET_CHARS} characters that stands entirely on its own. \
Open with the fact that makes living there seem unlikely, name the place and where it \
is, and answer the question: why do people live here?"""
)

_THREAD_SYSTEM = (
    _VOICE
    + f"""

Write a thread of {MIN_THREAD_PARTS} to {MAX_THREAD_PARTS} posts, each at most \
{MAX_TWEET_CHARS} characters.

The first post is the hook, and most readers see only this one. Open with the fact \
that makes living there seem unlikely — not with coordinates, distances or a census \
figure. Name the place and say where it is in a few words. End the post with the \
question itself: "Why do people live here?", or "Why did people live here?" for an \
abandoned place. It has to make sense with nothing after it.

The posts that follow answer the question: how the place began, what keeps people \
there, and the details that make it real — one idea per post. Start each with its \
number and a slash, like "2/". End on the strongest fact, not on a summary.

Length follows the material. {MIN_THREAD_PARTS} posts is a complete thread and the \
usual length. Add a fourth or fifth only when it carries something a reader would be \
sorry to miss; a post that repeats, pads or lists minor facts weakens the thread."""
)


def _user_prompt(place_name: str, raw_research: str, justifications: dict) -> str:
    notes = "\n".join(f"- {k}: {v}" for k, v in justifications.items())
    return (
        f"Place: {place_name}\n\n"
        f"<research_brief>\n{raw_research}\n</research_brief>\n\n"
        f"<scorer_notes>\n{notes}\n</scorer_notes>"
    )


def _too_long(parts: list[str]) -> list[int]:
    return [i for i, p in enumerate(parts, start=1) if len(p) > MAX_TWEET_CHARS]


async def generate_single_post(
    place_name: str,
    raw_research: str,
    justifications: dict,
) -> str:
    """Generate a single-tweet caption (≤280 chars) grounded in the research.

    Returns the tweet text. Retries once if the model overshoots the length limit.
    """
    user = _user_prompt(place_name, raw_research, justifications)
    post = await llm.parse(system=_SINGLE_SYSTEM, user=user, output_format=SinglePost)
    if len(post.text) > MAX_TWEET_CHARS:
        retry = (
            f"{user}\n\nYour previous draft was {len(post.text)} characters, over the"
            f" {MAX_TWEET_CHARS} limit. Rewrite it shorter:\n{post.text}"
        )
        post = await llm.parse(system=_SINGLE_SYSTEM, user=retry, output_format=SinglePost)
        if len(post.text) > MAX_TWEET_CHARS:
            raise ValueError(f"Single post for {place_name} is still {len(post.text)} chars")
    return post.text


async def generate_thread(
    place_name: str,
    raw_research: str,
    justifications: dict,
) -> list[str]:
    """Generate a multi-part thread. First part is the hook tweet; rest are numbered follow-ups.

    Returns an ordered list of tweet strings. Retries once if any part is too long or
    the part count is out of range.
    """
    user = _user_prompt(place_name, raw_research, justifications)
    thread = await llm.parse(system=_THREAD_SYSTEM, user=user, output_format=Thread)

    def problem(tweets: list[str]) -> str | None:
        if not MIN_THREAD_PARTS <= len(tweets) <= MAX_THREAD_PARTS:
            return f"it had {len(tweets)} posts"
        if long := _too_long(tweets):
            return f"posts {long} were over {MAX_TWEET_CHARS} characters"
        return None

    if issue := problem(thread.tweets):
        drafts = "\n".join(thread.tweets)
        retry = f"{user}\n\nYour previous draft broke the format: {issue}. Fix it:\n{drafts}"
        thread = await llm.parse(system=_THREAD_SYSTEM, user=retry, output_format=Thread)
        if issue := problem(thread.tweets):
            raise ValueError(f"Thread for {place_name} still invalid: {issue}")
    return thread.tweets


async def run_content_generation(
    db_pool,
    candidate_id: int,
    post_format: str,
) -> dict:
    """Dispatch to single or thread generator based on post_format, return content dict.

    Returns {"post_text": first tweet, "thread_parts": follow-up tweets or None}.
    """
    row = await db_pool.fetchrow(
        """
        SELECT c.place_name,
               (SELECT raw_research FROM research WHERE candidate_id = c.id
                 ORDER BY researched_at DESC LIMIT 1) AS raw_research,
               (SELECT justifications FROM scores WHERE candidate_id = c.id
                 ORDER BY scored_at DESC LIMIT 1) AS justifications
        FROM candidates c
        WHERE c.id = $1
        """,
        candidate_id,
    )
    args = (row["place_name"], row["raw_research"], row["justifications"] or {})
    if post_format == "thread":
        tweets = await generate_thread(*args)
        return {"post_text": tweets[0], "thread_parts": tweets[1:]}
    if post_format == "single":
        return {"post_text": await generate_single_post(*args), "thread_parts": None}
    raise ValueError(f"Cannot generate content for post_format={post_format!r}")
