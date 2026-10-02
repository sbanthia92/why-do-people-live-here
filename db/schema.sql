-- Database schema for why-do-people-live-here

CREATE TABLE IF NOT EXISTS candidates (
    id               SERIAL PRIMARY KEY,
    place_name       TEXT NOT NULL,
    wikipedia_url    TEXT NOT NULL UNIQUE,
    region           TEXT,           -- continent or broad geographic area
    story_category   TEXT,           -- e.g. 'ghost_town', 'company_town', 'colonial_resettlement'
    status           TEXT NOT NULL DEFAULT 'pending',
                                     -- pending | researched | assessed | approved | posted | skipped
    layer1_passed    BOOLEAN,
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS research (
    id                SERIAL PRIMARY KEY,
    candidate_id      INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    wikipedia_extract TEXT,
    web_sources       JSONB,          -- array of {url, snippet, source_name}
    raw_research      TEXT,           -- full synthesised research text passed to the assessor
    researched_at     TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assessments (
    id                  SERIAL PRIMARY KEY,
    candidate_id        INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    corroborated        BOOLEAN NOT NULL,   -- central answer backed by an independent source
    corroborated_reason TEXT,
    has_angle           BOOLEAN NOT NULL,   -- a real "why would anyone live there?" story
    has_angle_reason    TEXT,
    writer_notes        TEXT,               -- what the writer should build on or avoid
    passed              BOOLEAN NOT NULL,   -- both gates yes
    assessed_at         TIMESTAMPTZ DEFAULT now()
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

-- Migration from the earlier 0-10 scoring, which the two yes/no gates replaced.
-- Safe to re-run: candidates that passed the old scoring count as assessed.
DROP TABLE IF EXISTS scores;
UPDATE candidates SET status = 'assessed' WHERE status = 'scored';
