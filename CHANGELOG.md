# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- Research, scoring, content generation, publishing and the scheduler: the pipeline now runs end to end, with a dry-run mode and `--posts N` for previews.
- Web research runs three focused searches per place concurrently, and `--parallel N` researches and scores several places at once.
- `scripts/dry_run.py` previews posts against an embedded Postgres, with no Docker needed.
- Optional photo attachment (`POST_IMAGES=true`, off by default) limited to CC BY, CC BY-SA, CC0 and public-domain images, with a full credit line.
- Hand-picked place lists in `config/seed_categories.yaml` for story types Wikipedia's categories don't cover, and `scripts/verify_seed_places.py` to check them.
- Candidates resume from the step they stopped at; dry-run previews are published by the next real run.
- Variety rule: story category rotation, and deferral of candidates from the same region as the last post.
- Integration tests that run the whole pipeline against an embedded Postgres.

### Changed
- Seed config format: each story category now has `categories` and/or `places`. Removed ten categories that don't exist on Wikipedia or are empty.
- Category crawl descends one subcategory level and requires coordinates, so non-place articles are rejected.
- The 500-character minimum applies to the full article text instead of the lead paragraph.
- The daily workflow uses a persistent `DATABASE_URL` secret instead of a throwaway Postgres container, and takes the Wikimedia contact from a repository variable.

### Fixed
- Wikipedia requests were rejected with HTTP 403 because no User-Agent was sent.
- Article titles containing commas or parentheses were double-encoded and returned 404.
- Rate-limited requests (HTTP 429) are retried after the delay Wikimedia asks for.
- A section named "Origin" now satisfies the section check.

## [0.1.0] - 2026-06-18

### Added
- Initial repo scaffold: pipeline modules, scheduler, DB schema, seed categories, Docker setup, and GitHub Actions workflow.
