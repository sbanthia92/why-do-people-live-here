-- Database schema for why-do-people-live-here

CREATE TABLE IF NOT EXISTS candidates (
    id               SERIAL PRIMARY KEY,
    place_name       TEXT NOT NULL,
    wikipedia_url    TEXT NOT NULL UNIQUE,
    region           TEXT,           -- continent or broad geographic area
    story_category   TEXT,           -- e.g. 'ghost_town', 'company_town', 'colonial_resettlement'
    status           TEXT NOT NULL DEFAULT 'pending',
                                     -- pending | researched | scored | approved | posted | skipped
    layer1_passed    BOOLEAN,
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS research (
    id                SERIAL PRIMARY KEY,
    candidate_id      INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    wikipedia_extract TEXT,
    web_sources       JSONB,          -- array of {url, snippet, source_name}
    raw_research      TEXT,           -- full synthesised research text passed to scorer
    researched_at     TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scores (
    id                SERIAL PRIMARY KEY,
    candidate_id      INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    named_origin      INTEGER CHECK (named_origin BETWEEN 0 AND 2),
    economic_engine   INTEGER CHECK (economic_engine BETWEEN 0 AND 2),
    contrast_surprise INTEGER CHECK (contrast_surprise BETWEEN 0 AND 2),
    vivid_detail      INTEGER CHECK (vivid_detail BETWEEN 0 AND 2),
    corroboration     INTEGER CHECK (corroboration BETWEEN 0 AND 2),
    total_weighted    NUMERIC(5,2),
    justifications    JSONB,          -- {dimension: "reason string"}
    post_format       TEXT,           -- 'single' | 'thread' | 'skip'
    scored_at         TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS posts (
    id            SERIAL PRIMARY KEY,
    candidate_id  INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    post_text     TEXT NOT NULL,      -- caption / first tweet
    thread_parts  JSONB,              -- null if single post; array of strings if thread
    image_url     TEXT,               -- Wikimedia Commons URL used
    image_credit  TEXT,
    x_post_id     TEXT,               -- returned by X API after posting
    posted_at     TIMESTAMPTZ DEFAULT now(),
    dry_run       BOOLEAN DEFAULT false
);

CREATE TABLE IF NOT EXISTS variety_log (
    id             SERIAL PRIMARY KEY,
    posted_at      TIMESTAMPTZ DEFAULT now(),
    region         TEXT NOT NULL,
    story_category TEXT NOT NULL
);
