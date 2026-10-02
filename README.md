# Why Do People Live Here?

An automated X (Twitter) pipeline that researches obscure remote settlements and posts a daily "Why do people live here?" explainer. Each post is grounded in source-verified facts, filtered through a five-dimension story-worthiness scorer, and rotated across regions and settlement categories to keep the feed varied.

## Getting started

1. Create a virtualenv and install: `pip install -e ".[dev]"`
2. Copy `.env.example` to `.env` and fill in your API keys. `DRY_RUN=true` is the default there.
3. Start the database: `docker compose up postgres -d`
4. Apply the schema: `psql $DATABASE_URL -f db/schema.sql`
5. Preview posts without publishing: `python -m scheduler.run_pipeline --posts 3`

Add `--parallel N` to research and score N places at the same time; the daily run uses 1.

No Docker? `python scripts/dry_run.py --posts 3` does steps 3–5 in one go: it starts an embedded Postgres in `.dryrun-pg/`, applies the schema and previews posts. It only needs `ANTHROPIC_API_KEY` in `.env`.

6. Run the tests: `pytest` (the integration tests start their own embedded Postgres)

A dry run prints each post and stores it with the candidate marked `approved`. The next real run (`DRY_RUN=false`) publishes approved candidates first, so a preview is not wasted.

## How it works

1. **Candidate selection** — `config/seed_categories.yaml` gives each story category a source of place articles: Wikipedia categories to crawl (one subcategory level deep) and/or a hand-picked list of article titles, for story types Wikipedia's categories don't cover well. A cheap structural filter drops thin articles before any LLM call; crawled articles must also have coordinates, which keeps out films, lists and concept pages. After editing the hand-picked lists, run `python scripts/verify_seed_places.py` to check every title exists, isn't a redirect or duplicate, and passes the filter.
2. **Research** — The full Wikipedia article plus passages Claude finds through three concurrent web searches (origin, livelihood, daily life) are synthesised into a research brief in which every claim is tagged with its sources. The place's region (continent) is recorded here.
3. **Scoring** — Claude scores the brief 0–2 on five dimensions (named origin, economic engine, contrast/surprise, vivid detail, corroboration). Weights and thresholds live at the top of `pipeline/scorer.py`: a total of 8+ out of 10 becomes a thread, 5+ a single post, anything lower is skipped. Two rules override the total: a candidate with no independent corroboration is skipped, and one that scores 0 on contrast/surprise gets a single post at most.
4. **Content generation** — Claude writes the post or thread from the brief alone. Length limits are checked in code, with one rewrite if a draft runs over.
5. **Publishing** — Content is posted to X via the v2 API, followed by a reply linking the source article. Every step is recorded in Postgres.

## Images

Posts are text-only by default. Setting `POST_IMAGES=true` attaches the article's lead image when it is a photo under CC BY, CC BY-SA, CC0 or the public domain, and adds a credit to the closing reply: author, licence with a link, and a link to the file on Wikimedia Commons.

This is off by default because reuse on X is not risk-free: posting grants X a broad licence that a CC licence does not let you pass on, Commons licence labels are occasionally wrong, and a copyright licence does not cover people's image rights, trademarks or, in some countries, recent buildings and artworks.

## Variety

- **Story category** — the most recently posted category is tried last, so two posts in a row only share a story type when nothing else is available.
- **Region** — a candidate on the same continent as the last post is parked (its research is kept) and picked up by a later run.

## Resuming

Each candidate carries a status (`pending → researched → scored → approved/posted`, or `skipped`). A run that fails part-way leaves the candidate where it stopped, and the next run continues from there instead of redoing the work. Each run tries up to five candidates before giving up.

## Deployment

`.github/workflows/post.yml` runs the pipeline daily at 09:00 UTC, and can be triggered by hand. It has two modes.

**Preview mode (the default).** Nothing is sent to X. Each post is filed as a GitHub issue labelled `preview` and assigned to the repository owner, so GitHub emails it to them; it also appears on the run's summary page. The issue shows every post with its character count, the scores with the reason for each, and the web sources used. This mode needs only:

- Secret: `ANTHROPIC_API_KEY`
- Variable: `WIKIMEDIA_CONTACT` (an email or URL; Wikimedia asks for a contact in the User-Agent)

Without a `DATABASE_URL`, preview mode uses an embedded Postgres whose data is carried between runs by the Actions cache. That is best-effort: if the cache is evicted, previews start over.

**Publishing mode.** Set the repository variable `PUBLISH_TO_X` to `true`. This also needs:

- Secret: `DATABASE_URL` (a hosted Postgres that persists between runs, so posted places are remembered)
- Secrets: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`

If `ANTHROPIC_API_KEY` is missing the run skips with a warning instead of failing.

`.github/workflows/ci.yml` runs ruff and the tests (Python 3.11 and 3.12) on every pull request and on pushes to `main`. It needs no secrets.
