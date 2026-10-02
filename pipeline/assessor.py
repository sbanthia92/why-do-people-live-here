"""Decide whether a researched candidate is worth posting: two yes/no gates."""

from pydantic import BaseModel, Field
from rich.console import Console

from pipeline import llm

console = Console()


class Assessment(BaseModel):
    # Each reason is declared before its verdict so it is written first
    corroborated_reason: str = Field(
        description="The passage, with its source tags, that decides the corroboration gate."
    )
    corroborated: bool
    has_angle_reason: str = Field(description="What the puzzle and its answer are, or why not.")
    has_angle: bool
    writer_notes: str = Field(
        description=(
            "For the writer: the two or three strongest things in the brief, and anything"
            " disputed that must be flagged or left out."
        )
    )

    @property
    def passed(self) -> bool:
        return self.corroborated and self.has_angle


_ASSESS_SYSTEM = """\
You are the editor of "Why Do People Live Here?", an account that explains why anyone \
lives somewhere remote, harsh or strange. You are deciding whether a research brief on \
one place is good enough to post. Judge only the brief. Each sentence in it carries \
source tags: [W] is Wikipedia, [1], [2], ... are independent web sources.

Answer two yes/no questions. A place is posted only if both are yes.

1. corroborated — Is the central answer independently backed?
The central answer is what a post would rest on: why the place exists, or what keeps \
people there. Say yes if at least one of those is supported by a web source as well as \
[W]. Sources disagreeing on a detail is fine as long as the brief reports the \
disagreement. Say no if everything that matters rests on [W] alone, or if the sources \
flatly contradict each other on the central answer.

2. has_angle — Is there a real "why would anyone live there?" story, with an answer?
Say yes if a general reader would find it surprising that people live (or lived) there \
— the place is hostile, cut off, or exists for a remarkable reason such as a decree, a \
relocation, a single company or a border quirk — and the brief actually explains why \
they do. Say no for an ordinary town with an ordinary history, and for a brief too thin \
to answer the question.

For each, write the reason first, pointing to the passage that decides it, then the \
verdict. Judge from what is written in the brief, not from what you know about the place.

Then write short notes for the writer: the two or three strongest things in the brief, \
and anything disputed that must be flagged or left out."""


async def assess_candidate(db_pool, candidate_id: int) -> Assessment:
    """Ask Claude the two gate questions about a researched candidate and persist the result.

    The candidate moves to status 'assessed' if both gates pass, otherwise 'skipped'.
    """
    row = await db_pool.fetchrow(
        """
        SELECT c.place_name, r.raw_research
        FROM candidates c
        JOIN research r ON r.candidate_id = c.id
        WHERE c.id = $1
        ORDER BY r.researched_at DESC
        LIMIT 1
        """,
        candidate_id,
    )
    assessment = await llm.parse(
        system=_ASSESS_SYSTEM,
        user=f"Place: {row['place_name']}\n\n<research_brief>\n{row['raw_research']}\n</research_brief>",
        output_format=Assessment,
    )
    await persist_assessment(db_pool, candidate_id, assessment)
    verdict = "pass" if assessment.passed else "skip"
    console.log(
        f"[assessor] {row['place_name']}: corroborated={assessment.corroborated},"
        f" angle={assessment.has_angle} → {verdict}"
    )
    return assessment


async def persist_assessment(db_pool, candidate_id: int, assessment: Assessment) -> None:
    """Write a row to the assessments table and update candidates.status."""
    await db_pool.execute(
        """
        INSERT INTO assessments
            (candidate_id, corroborated, corroborated_reason, has_angle, has_angle_reason,
             writer_notes, passed)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        candidate_id,
        assessment.corroborated,
        assessment.corroborated_reason,
        assessment.has_angle,
        assessment.has_angle_reason,
        assessment.writer_notes,
        assessment.passed,
    )
    await db_pool.execute(
        "UPDATE candidates SET status = $2, updated_at = now() WHERE id = $1",
        candidate_id,
        "assessed" if assessment.passed else "skipped",
    )
