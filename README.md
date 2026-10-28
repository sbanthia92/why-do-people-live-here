# Why Do People Live Here?

An automated X (Twitter) pipeline that researches obscure remote settlements and posts a daily "Why do people live here?" explainer. Each post is grounded in source-verified facts, checked by two yes/no editorial gates, and rotated across regions and settlement categories to keep the feed varied.

## Getting started

1. Create a virtualenv and install: `pip install -e ".[dev]"`
2. Copy `.env.example` to `.env` and fill in your API keys. `DRY_RUN=true` is the default there.
3. Start the database: `docker compose up postgres -d`
4. Apply the schema: `psql $DATABASE_URL -f db/schema.sql`
5. Preview posts without publishing: `python -m scheduler.run_pipeline --posts 3`

Add `--parallel N` to research and assess N places at the same time; the daily run uses 1.

No Docker? `python scripts/run_local.py --posts 3` does steps 3–5 in one go: it starts an embedded Postgres in `.dryrun-pg/`, applies the schema and previews posts. It only needs `ANTHROPIC_API_KEY` in `.env`. Add `--publish` to post to X for real (see "Posting to X").

6. Run the tests: `pytest` (the integration tests start their own embedded Postgres)

A dry run prints each post and stores it with the candidate marked `approved`. The next real run (`DRY_RUN=false`) publishes approved candidates first, so a preview is not wasted.

## How it works

1. **Candidate selection** — `config/seed_categories.yaml` gives each story category a source of place articles: Wikipedia categories to crawl (one subcategory level deep) and/or a hand-picked list of article titles, for story types Wikipedia's categories don't cover well. A cheap structural filter drops thin articles before any LLM call; crawled articles must also have coordinates, which keeps out films, lists and concept pages. After editing the hand-picked lists, run `python scripts/verify_seed_places.py` to check every title exists, isn't a redirect or duplicate, and passes the filter.
2. **Research** — The full Wikipedia article plus passages Claude finds through three concurrent web searches (origin, livelihood, daily life; run on Claude Sonnet, the cheaper model, with Wikipedia and its mirrors blocked) are synthesised into a research brief in which every claim is tagged with its sources. The place's region (continent) is recorded here.
3. **Assessment** — Claude, acting as editor, answers two yes/no questions about the brief: is the central answer backed by a source independent of Wikipedia, and is there a real "why would anyone live there?" story with an answer. A place is posted only if both are yes. The reasons are recorded, along with notes for the writer.
4. **Content generation** — Claude writes from the brief alone and decides the length: a single post, or a thread of up to four. Every draft then has to pass two checks. Length is checked in code the way X counts it (links count as 23 characters; some symbols count double). A separate fact check compares each claim in the draft with the brief and reports any number, name, date or quotation the brief does not support. A draft that fails is sent back with the reasons, up to twice; if it still fails, the place is not posted.
5. **Publishing** — Content is posted to X via the v2 API with a map card on the first post, followed by a reply naming the source article and inviting a follow. Every step is recorded in Postgres.

For the full component breakdown, data model and the trade-offs behind each decision, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Images

**Map cards (on by default).** The first post of each thread carries a locator card drawn by `pipeline/map_card.py`: a regional map centred on the place with distance rings and the nearest cities, a small globe, the coordinates, and the nearest major city with its distance. It is drawn from [Natural Earth](https://www.naturalearthdata.com/) coastlines and city data, which are in the public domain, so there is no photograph and no licence to get wrong. The card also gets a text description for screen readers. A place with no coordinates on Wikipedia is posted without one. Set `MAP_CARDS=false` to turn the cards off.

**Photos (off by default).** Setting `POST_IMAGES=true` lets a place with no map card fall back to the article's lead image, but only a file that, by Commons' own metadata:

- is hosted on Wikimedia Commons (local Wikipedia uploads can be public domain in the US only, or non-free);
- is CC0 or public domain, with no attribution required;
- has no rights warning (personality rights, trademarks and the like);
- is a JPEG or PNG photo, not marked non-free.

Attribution licences (CC BY, CC BY-SA) are refused. Posting to X grants X a licence to pass the image on, which a CC licence does not let you give, and under the older CC versions any slip in the credit ends the licence at once, which some rights holders charge for. With CC0 and public-domain files neither problem arises. The closing reply still names the photographer when Commons gives a name, as a courtesy and without links, and the database records the file's Commons page.

What this cannot rule out: a file whose Commons licence is wrong (someone uploaded a photo that was not theirs), or a rights issue Commons has not flagged. Both are rare, and the second is mostly about photos of people, but they are why photos stay off by default.

## Variety

- **Story category** — the most recently posted category is tried last, so two posts in a row only share a story type when nothing else is available.
- **Region** — a candidate on the same continent as the last post is parked (its research is kept) and picked up by a later run.

## Resuming

Each candidate carries a status (`pending → researched → assessed → approved/posted`, or `skipped`). A run that fails part-way leaves the candidate where it stopped, and the next run continues from there instead of redoing the work. A place previewed by a dry run is `approved`; the next real run posts exactly the text that was previewed. Each run tries up to five candidates before giving up.

## Posting to X

X's API is pay-per-use: you buy credits and each request draws on them. At the time of writing a plain post costs $0.015 and a post containing a link costs $0.20, so a four-post thread plus the source reply is about $0.08 a day. Check [X's pricing page](https://docs.x.com/x-api/getting-started/pricing) for current rates.

1. In the X developer console, open your app, set **Read and write** in its user authentication settings (type: Web App, Automated App or Bot; any callback URL), and buy credits.
2. Generate the API key and secret, and put them in `.env` as `X_API_KEY` and `X_API_SECRET`.
3. Get an access token for the account that should post:
   - **Posting from the developer account itself:** generate the access token and secret on the same page (after step 1, or they stay read-only) and put them in `.env` as `X_ACCESS_TOKEN` and `X_ACCESS_SECRET`.
   - **Posting from a different account:** run `python scripts/x_authorize.py`, open the link it prints while logged in as that account, authorize, and type the PIN back in. It saves that account's token to `.env` without showing it.
4. Check them without posting anything: `python scripts/x_check.py`. It prints the account the posts would go out as.
5. Post one for real: `python scripts/run_local.py --publish`. It asks for confirmation first, and publishes places you have already previewed before preparing new ones.

Two settings affect cost and appearance, both off by default:

- `SOURCE_LINK=true` makes the closing reply link to the Wikipedia article instead of naming it. That one post is then charged at the link rate.
- `POST_IMAGES=true` attaches a CC0 or public-domain photo when there is no map card (see "Images").

If a thread fails part-way (a rate limit, say), the posts already published stay up and the place is recorded as posted, so the next run does not post the hook a second time.

## What a run costs

Every run ends by printing its Claude token use and an estimated cost. The web searches are the bulk of it, which is why they run on Claude Sonnet while the brief, the editor's gates and the writing stay on Claude Opus. The estimate leaves out Anthropic's per-search fee. X charges separately per post (see "Posting to X").

## Where the pipeline keeps its memory

The pipeline has to remember which places it has posted or rejected, what is queued, and the last post's region. That state lives in Postgres while a run is in progress; what differs is where it rests between runs.

- **In the repository (the daily workflow).** The state is saved as JSON-lines files, one per table, committed to a branch called `data`. Each run loads them into a throwaway embedded Postgres, does its work, and commits the result back. Nothing to sign up for, nothing that expires, and `git log data` shows what happened each day.
- **On your machine (the default for `scripts/run_local.py`).** An embedded Postgres in `.dryrun-pg/`. Delete the folder to start over.
- **Shared between the two.** `python scripts/run_local.py --shared` fetches the `data` branch before the run and commits and pushes it afterwards, so a post published from your machine is known to the daily workflow and the other way round.
- **A hosted Postgres**, if you prefer one: set `DATABASE_URL`, apply `db/schema.sql`, and run `python -m scheduler.run_pipeline`.

The saved state leaves out the full Wikipedia article text, which is only needed while a place is being researched.

## Deployment

`.github/workflows/post.yml` runs the pipeline daily, and can be triggered by hand. It is scheduled for 09:17 UTC and in practice starts at about 14:30 UTC, because GitHub has been starting it roughly five hours late. It has two modes.

**Preview mode (the default).** Nothing is sent to X. Each post is filed as a GitHub issue labelled `preview` and assigned to the repository owner, so GitHub emails it to them; it also appears on the run's summary page. The issue shows every post with its character count, the editor's two yes/no verdicts with their reasons, and the web sources used. This mode needs only:

- Secret: `ANTHROPIC_API_KEY`
- Variable: `WIKIMEDIA_CONTACT` (an email or URL; Wikimedia asks for a contact in the User-Agent)

**Publishing mode.** Set the repository variable `PUBLISH_TO_X` to `true`. This also needs:

- Secrets: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`

In publishing mode, places that were previewed and not yet posted go out first, with the previewed text. Each published post is filed as an issue labelled `posted`, with a link to it on X, so the owner is still emailed about what went out.

If `ANTHROPIC_API_KEY` is missing the run skips with a warning instead of failing. If the Anthropic account is out of credits, the run fails with a one-line explanation.

`.github/workflows/ci.yml` runs ruff and the tests (Python 3.11 and 3.12) on every pull request and on pushes to `main`. It needs no secrets.
