# CLAUDE.md

Guidance for Claude Code in this repository. The README is the user-facing manual; this file records how the project is built, the decisions behind it, and the conventions to keep.

## What this is

An automated X (Twitter) account that posts one "Why do people live here?" explainer a day about an obscure remote settlement. Pipeline: pick a place from Wikipedia → research it → two yes/no editorial gates → write and fact-check the post → publish to X with a map card. Runs daily on GitHub Actions; state lives on a `data` git branch.

## Commands

```bash
pip install -e ".[dev]"                          # install (Python >=3.11)
pytest                                           # all tests; integration tests start their own embedded Postgres
ruff check .                                     # lint (line length 100, target py311)
python scripts/run_local.py --posts 3            # dry-run preview, embedded Postgres in .dryrun-pg/, needs only ANTHROPIC_API_KEY
python scripts/run_local.py --publish            # post to X for real (asks for confirmation)
python scripts/run_local.py --shared             # use the repo's `data` branch as state (fetch before, push after)
python -m scheduler.run_pipeline --posts 3       # against $DATABASE_URL (apply db/schema.sql first)
python scripts/verify_seed_places.py             # after editing hand-picked places in config/seed_categories.yaml
python scripts/x_check.py                        # verify X credentials without posting
python scripts/x_authorize.py                    # get a token for a different X account (PIN flow)
```

## Layout

- `pipeline/candidate_selector.py` — reads `config/seed_categories.yaml` (Wikipedia categories crawled one subcategory deep, and/or hand-picked `places`), structural filter, variety rotation.
- `pipeline/researcher.py` — Wikipedia article + three concurrent Claude web searches (origin, livelihood, daily life) → research brief with every claim tagged by source; records region.
- `pipeline/assessor.py` — the two yes/no gates.
- `pipeline/content_generator.py` — writing, X-length check, fact check, rewrite loop.
- `pipeline/xtext.py` — X's character counting.
- `pipeline/map_card.py` + `pipeline/data/natural_earth.json.gz` — locator card image.
- `pipeline/image_sourcer.py` — optional Commons photo (off by default).
- `pipeline/publisher.py` — X v2 API via tweepy.
- `pipeline/preview.py` — preview / posted issue bodies.
- `pipeline/llm.py` — model names, token/cost accounting.
- `scheduler/run_pipeline.py` — orchestrates a run, resumption, `--posts`, `--parallel`.
- `db/schema.sql` (idempotent, applied every run, contains migrations), `db/client.py`, `db/state.py` (JSON-lines export/import of tables).
- `assets/` — profile picture and banner, drawn by `assets/make_profile_images.py`.
- `.github/workflows/post.yml` (daily run), `ci.yml` (ruff + tests on 3.11 and 3.12).

## Key decisions (and why)

### Editorial quality
- **Two yes/no gates replaced the 0–10 score** (PR #6). A place posts only if (1) the central answer is backed by a source independent of Wikipedia, and (2) there is a real "why would anyone live there?" story that the brief answers. The weighted score was a soft judgement with placeholder weights, and totals clustered at 9–10 even after a stricter rubric (PR #4).
- **Wikipedia and its mirrors are blocked from web search** (Kiddle, Grokipedia, Wikiwand, ...). A mirror counted as "independent" would let Wikipedia corroborate itself and pass gate 1.
- **The writer decides length; threads are capped at four posts.** With a cap of five, every thread came back as five.
- **Fact check on every draft** (PR #19): each claim is compared with the brief; any unsupported number, name, date or quotation sends it back, up to twice. Still failing → place not posted. The writer works from the brief only.
- **Hooks** open with the surprising fact and end on the question.

### Cost
- **Web search runs on Sonnet (`claude-sonnet-5-5`); brief, gates, writing and fact check on Opus (`claude-opus-5-5`)** (PR #9). Search reads tens of thousands of tokens per call and is most of the cost (~$0.24 for one place's research on Sonnet).
- **Every run prints token use and estimated cost** (excludes Anthropic's per-search fee).
- **X is pay-per-use**: ~$0.015 per plain post, ~$0.20 per post with a link (Oct 2026). So the closing reply **names** the source article instead of linking (`SOURCE_LINK=false` default), and the photo credit has no links.

### Publishing behaviour
- **What was previewed is what gets posted.** A dry-run preview marks the candidate `approved`; the next real run publishes that exact text first. The owner can veto a preview by adding the `rejected` label to its issue (`pipeline/review.py`); the next run marks that place `skipped`. Rejection is a veto, not a required approval.
- **A thread that fails part-way is still recorded as posted**, so the hook is never published twice.
- **Candidates resume**: `pending → researched → assessed → approved/posted`, or `skipped`. A failed run continues from where it stopped. Up to five candidates tried per run.
- **Closing reply** names the source and ends with a follow line ("A new remote, harsh or strange place every day. Follow for more.").

### Images and licensing
- **Map cards on by default** (`MAP_CARDS`), drawn from public-domain Natural Earth data: no licence risk, has alt text, skipped when Wikipedia has no coordinates. The nearest major city is always labelled (PR #21).
- **Photos off by default** (`POST_IMAGES`) and only a fallback when there is no card. Since PR #31, only Wikimedia Commons files that are CC0 or public domain, `AttributionRequired: false`, no `Restrictions`, JPEG/PNG, not non-free. **CC BY / CC BY-SA are refused**: posting to X grants X a sublicensable licence a CC licence can't pass on, and attribution slips under older CC versions terminate the licence. Remaining risk: mislabelled Commons files.
- **Profile art** is drawn from the same Natural Earth data, so nothing to license.

### Variety
- Most recently posted story category is tried last.
- A candidate on the same continent as the last post is parked (research kept) for a later run.
- Parallel batches are spread across story types.
- The `exclave` category was renamed `border_oddity` (it also holds divided towns, leased territories); `db/schema.sql` migrates old rows.

### Candidate sourcing
- Hand-picked place lists exist for story types Wikipedia categories cover badly (~217 places). Ten nonexistent/empty categories were removed.
- Crawled articles must have coordinates (keeps out films, lists, concept pages); 500-char minimum on the full article text.
- Wikipedia requests need a User-Agent with a contact (`WIKIMEDIA_USER_AGENT` / `WIKIMEDIA_CONTACT`) or they get 403; 429s are retried after `Retry-After`; titles with commas/parentheses must not be double-encoded.

### State and deployment
- **No hosted database** (PR #7). State is JSON-lines files (one per table, ordered by id) on the `data` branch. Each workflow run loads them into a throwaway embedded Postgres (`pgserver`), runs, and commits back — even on failure, since a post may already have gone out. Full article text is left out of saved state. Earlier approaches (throwaway container, Actions cache, `DATABASE_URL` secret) were dropped because they lost history or needed signup.
- **Schema changes go in `db/schema.sql` as idempotent migrations**, because it is re-applied on every run after state is loaded.
- **Daily workflow is preview mode by default**: posts are filed as GitHub issues labelled `preview`, assigned to the owner (so GitHub emails them). `PUBLISH_TO_X=true` + the four X secrets switches to publishing; each published post is filed as a `posted` issue with its X link.
- **Schedule is `17 9 * * *` (09:17 UTC) to land at ~14:30 UTC**: GitHub has started this workflow ~5 hours late consistently. 14:30 UTC is daytime for the Americas, Europe and India. If the delay disappears, posts go out 5 hours early.
- Missing `ANTHROPIC_API_KEY` → run skips with a warning. Out of Anthropic credits → one clear error line, state still saved.
- Workflow and Dockerfile run Python 3.11; CI tests 3.11 and 3.12.

## Environment gotchas

- **Hidden `.pth` on macOS**: the repo is under an iCloud-synced Desktop, and macOS can mark `.venv/lib/python3.12/site-packages/__editable__.*.pth` hidden; Python then skips it and `import pipeline` fails outside the repo root. Fix: `chflags nohidden` on that file. Scripts in `scripts/` add the repo root to `sys.path` to avoid depending on the install.
- Map cards render with DejaVu fonts on the GitHub runner (installed by the workflow) and Arial on macOS.
- `.env` holds real keys and is gitignored; `.env.example` defaults to `DRY_RUN=true`.

## Conventions

- **Branches**: `phase/N-short-name` for feature phases, `fix/...` for fixes, `docs/...` for docs. Never commit to `main` directly; open a PR.
- **Commits / PR titles**: Conventional Commits (`feat:`, `fix:`, `docs:`), plain-English subject.
- **PR bodies**: "What this does", details, "Testing" (test count, ruff, any real API runs with numbers), and **"Not yet exercised"** listing what has not actually been run. Be explicit about anything only tested with fakes.
- **Update `CHANGELOG.md`** (Keep a Changelog, `[Unreleased]`) and the README for user-visible changes.
- **Keep `docs/ARCHITECTURE.md` current.** A PR that changes the pipeline stages, the status flow, `db/schema.sql`, the state/deployment setup, or a decision recorded there must update that document in the same PR: the affected section, and the trade-offs entry if a decision changed. List it under "Testing" in the PR body.
- **Writing style** for docs, prompts and PRs: plain, concrete language; say what something does and why; no hype.
- **Tests**: Claude, Wikipedia and X are faked; integration tests use a real embedded Postgres. Run `pytest` and `ruff check .` before every PR.
- **Never post to X from a test or by accident**: anything that publishes goes through `--publish` (with confirmation) or `PUBLISH_TO_X=true`.

## Potential next features

Ideas not built yet, roughly in order of value. Each says why and where it would go. Confirm with the owner before starting one; keep the conventions above (phase branch, PR with "Not yet exercised", CHANGELOG, tests with fakes).

1. **Require approval before posting.** Rejecting a preview with a label is built; this would flip the default so a preview is only eligible once the owner adds an `approved` label. Costs a missed post on days the owner doesn't look, so decide whether that is wanted. Touches `pipeline/review.py`, `.github/workflows/post.yml`, `scheduler/run_pipeline.py`.
2. **Engagement feedback.** Fetch impressions, likes and reposts for posted threads a few days later and store them in a new table. Use them to report which story categories and hook styles work, and later to weight category rotation. Check X's per-read pricing first. Touches `pipeline/publisher.py`, `db/schema.sql` (new idempotent table), `db/state.py`.
3. **Duplicate detection by Wikidata ID.** The same place can come in under different titles (redirects, alternate names) or from two seed lists. Resolve each candidate to its Wikidata QID and skip any QID already posted or queued. Touches `pipeline/candidate_selector.py`, `db/schema.sql`.
4. **Wikidata facts in the brief.** Population, elevation, founding date and climate figures from Wikidata as structured, sourced claims, so the fact check has more to compare numbers against. Touches `pipeline/researcher.py`.
5. **Candidate pool report.** Count unused candidates per story category each run and warn (in the run log and the preview issue) when one is running low, so seed lists can be topped up before rotation degrades. Touches `scheduler/run_pipeline.py`, `pipeline/candidate_selector.py`.
6. **Second platform (Bluesky or Mastodon).** Both have free posting APIs and allow links, so the source could be linked there. Needs a small publisher interface so X stays the default and each platform has its own length rules. Touches `pipeline/publisher.py`, `pipeline/xtext.py`, workflow secrets.
7. **Archive site on GitHub Pages.** A static page built from `posts.jsonl` on the `data` branch: a map of every place posted, with the thread text and sources. Free hosting, gives the X bio one link. New `site/` build step in the workflow.
8. **Fact-check regression set.** A handful of saved briefs with known-good and known-bad drafts, run against the real model on demand (not in CI), to check the gates and fact check still behave after a model or prompt change. New `evals/` directory and script.
9. **Fuller cost accounting.** Include Anthropic's per-search fee and X's per-post charge in the run's cost line, and post a weekly total as an issue. Touches `pipeline/llm.py`, `scheduler/run_pipeline.py`.
10. **Schedule drift guard.** The cron assumes GitHub starts the workflow ~5 hours late. Log the actual start time, and if the delay disappears, wait or trigger from an external scheduler (`repository_dispatch`) so posts still land near 14:30 UTC. Touches `.github/workflows/post.yml`.
11. **Corrections.** A script to record a reader-reported error against a posted place and post a correction reply under the thread (through `--publish`-style confirmation). New `scripts/correct.py`, a `corrections` table.
