# Why Do People Live Here?

An automated X (Twitter) pipeline that researches obscure remote settlements and posts a daily "Why do people live here?" explainer. Each post is grounded in source-verified facts, filtered through a five-dimension story-worthiness scorer, and rotated across regions and settlement categories to keep the feed varied.

## Getting started

1. Copy `.env.example` to `.env` and fill in your API keys.
2. Start the database: `docker compose up postgres -d`
3. Apply the schema: `psql $DATABASE_URL -f db/schema.sql`
4. Run a dry-run: `DRY_RUN=true python -m scheduler.run_pipeline`

## How it works

1. **Candidate selection** — Wikipedia categories defined in `config/seed_categories.yaml` are crawled for place articles. A variety constraint prevents back-to-back posts from the same region or story category.
2. **Research** — Wikipedia extract and supplementary web sources are fetched and synthesised into a research summary via Claude.
3. **Scoring** — Claude scores each candidate on five dimensions (named origin, economic engine, contrast/surprise, vivid detail, corroboration). Low scorers are skipped; high scorers become threads.
4. **Content generation** — Claude writes the caption or thread, staying strictly within the research findings.
5. **Image sourcing** — A relevant Wikimedia Commons image is fetched and credited.
6. **Publishing** — Content is posted to X via the v2 API. All runs are recorded in Postgres for deduplication and variety tracking.
