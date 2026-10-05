# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- In publishing mode each post is filed as an issue labelled `posted`, linking to it on X.
- Profile picture and banner for the X account in `assets/`, with the script that draws them.
- Every run prints its Claude token use and an estimated cost.
- State kept in the repository: the daily workflow loads the pipeline's memory from files on the `data` branch and commits it back after each run. No hosted database is needed, including for posting to X.
- `scripts/run_local.py --shared` uses the same `data` branch from a local machine, and `--state-dir` uses any directory.
- `scripts/x_check.py` verifies X credentials without posting.
- `SOURCE_LINK` setting: the closing reply names the source in text by default, because X charges far more for a post containing a link.
- Post length is measured the way X counts it (links as 23 characters, some symbols double).
- Daily workflow preview mode: without X credentials, each post is filed as a GitHub issue (assigned to the owner, so it is emailed) with its scores and sources. Posting to X now requires the `PUBLISH_TO_X` variable.
- CI workflow: ruff and the test suite on Python 3.11 and 3.12 for every pull request.
- A candidate that scores 0 on contrast/surprise gets a single post at most.
- Research, scoring, content generation, publishing and the scheduler: the pipeline now runs end to end, with a dry-run mode and `--posts N` for previews.
- Web research runs three focused searches per place concurrently, and `--parallel N` researches and scores several places at once.
- `scripts/run_local.py` previews posts against an embedded Postgres, with no Docker needed; `--publish` posts to X for real.
- Optional photo attachment (`POST_IMAGES=true`, off by default) limited to CC BY, CC BY-SA, CC0 and public-domain images, with a full credit line.
- Hand-picked place lists in `config/seed_categories.yaml` for story types Wikipedia's categories don't cover, and `scripts/verify_seed_places.py` to check them.
- Candidates resume from the step they stopped at; dry-run previews are published by the next real run.
- Variety rule: story category rotation, and deferral of candidates from the same region as the last post.
- Integration tests that run the whole pipeline against an embedded Postgres.

### Changed
- The daily workflow runs at 14:17 UTC (was 09:17), which is daytime for audiences in the Americas, Europe and India.
- Web search runs on Claude Sonnet 5.5 instead of Opus 5.5, roughly halving its price per token; the brief, gates and writing stay on Opus.
- Web search blocks Wikipedia mirrors and derivatives (Kiddle, Grokipedia, Wikiwand and others), which would otherwise count as independent corroboration.
- The daily workflow no longer uses the Actions cache or a `DATABASE_URL` secret, and runs at 09:17 UTC instead of on the hour.
- The 0–10 score is replaced by two yes/no gates: independently corroborated, and a real "why would anyone live there?" story. The writer decides the length; threads are capped at four posts.
- A place previewed by a dry run is posted with exactly the previewed text, not a fresh draft.
- If a thread fails after its first post, the place is still recorded as posted so the hook is not published twice.
- `scripts/dry_run.py` is now `scripts/run_local.py`.
- Scoring rubric is stricter: 1 is the normal score and each 2 has an explicit test, so totals spread out instead of clustering at 9–10.
- Thread hooks open with the surprising fact and end on the question; threads default to three posts and grow only when the material warrants.
- A batch of candidates prepared in parallel is spread across story types.
- Seed config format: each story category now has `categories` and/or `places`. Removed ten categories that don't exist on Wikipedia or are empty.
- Category crawl descends one subcategory level and requires coordinates, so non-place articles are rejected.
- The 500-character minimum applies to the full article text instead of the lead paragraph.
- The daily workflow uses a persistent `DATABASE_URL` secret instead of a throwaway Postgres container, and takes the Wikimedia contact from a repository variable.

### Fixed
- An empty Anthropic credit balance was logged as "web search unavailable" and then crashed with a traceback. It now stops the run with one clear line, and the state is still saved.
- Wikipedia requests were rejected with HTTP 403 because no User-Agent was sent.
- Article titles containing commas or parentheses were double-encoded and returned 404.
- Rate-limited requests (HTTP 429) are retried after the delay Wikimedia asks for.
- A section named "Origin" now satisfies the section check.

## [0.1.0] - 2026-06-18

### Added
- Initial repo scaffold: pipeline modules, scheduler, DB schema, seed categories, Docker setup, and GitHub Actions workflow.
