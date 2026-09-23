# W4: jobs & public activity (`src/signalpost/activity/`)

`ActivityConnector` (`name="activity"`, `families=("jobs", "activity", "reviews")`) fills the
`hiring_and_activity` section. It runs after the `web` connector (W3) so `ctx.shared` may already carry
`verified_site`, `site_pages`, `social_links`, `feed_urls`, `ats_links`, `news_urls`.

## Modules

- **`nav_jobs.py`** - claims from the cached NAV (arbeidsplassen.no) ad feed only
  (`ctx.caches.nav.ads_for([...])`). No HTTP, works at every tier including T0. Looks up the company's
  own org number plus any subunit org numbers from `ctx.caches.aliases.subunits(org)`. Produces one
  `jobs/job_posting` claim per `ACTIVE` ad (`value_key="nav:{uuid}"`) and one `jobs/recent_hiring`
  summary claim (`value_key=None`) counting ads of any status published in the last 365 days.
- **`ats.py`** - detects an ATS from `ctx.shared["ats_links"]` / links on `ctx.shared["site_pages"]`
  and fetches its keyless job list. Implemented natively: Teamtailor (`{sub}.teamtailor.com/jobs.rss`),
  Lever (`api.lever.co/v0/postings/{company}?mode=json`), Greenhouse
  (`boards-api.greenhouse.io/v1/boards/{token}/jobs`), SmartRecruiters
  (`api.smartrecruiters.com/v1/companies/{id}/postings`). Everything else recognized
  (Webcruiter, Jobylon, ReachMee, HR-manager, Recman, Easycruit, Jobbnorge, Workday) falls back to a
  conservative same-domain anchor-tag scrape of the careers page (`careers_html_v1`). Dedupes against
  NAV by normalized title (`_common.norm_title`) before publishing.
- **`feeds.py`** - RSS/Atom parsing (shared with `ats.py` and `youtube.py`) plus dated-item extraction
  from `ctx.shared["news_urls"]` pages (`<time datetime>`, `meta[property=article:published_time]`,
  JSON-LD `datePublished`, or Norwegian date text like "12. mars 2026" / "12.03.2026"). Only consumes
  feed/news URLs W3 already discovered - never guesses `/feed`, `/rss`. Undated items are dropped, never
  given a fabricated date. Keeps the 10 most recent dated items across both sources.
- **`youtube.py`** - resolves a channel id from a linked YouTube URL (`/channel/UC…` needs no fetch;
  `/@handle`, `/c/`, `/user/` need one page fetch to read `"channelId":"UC…"`,
  `meta[itemprop=identifier]`, or `link[rel=canonical]`) then reads
  `youtube.com/feeds/videos.xml?channel_id=UC…` for the 5 latest videos. Only channels linked from an
  exact-verified site are used (`identity_basis="linked_from_verified_site"`); no scraping of video
  pages, no LinkedIn/Facebook/Instagram/X/TikTok (those stay W3's URL-only profiles).
- **`connector.py`** - orchestrates the above into `ConnectorResult`. `reviews` is always
  `not_available` (no permitted keyless source). `jobs` is `available` when ≥1 active posting exists
  (NAV and/or ATS); otherwise `not_available` with a reason naming what was checked
  ("checked NAV feed (cache built …): no active postings"), distinct from `not_applicable` (no NAV cache
  was supplied to the run at all) and `failed` (the cache lookup itself raised). `activity` is
  `not_applicable` when there's no verified site or the tier is T0 (no HTTP budget); `not_available`
  reason "checked: none found" when feeds/news/YouTube were checked and nothing dated turned up;
  `failed` when every attempted source errored.

## Claim conventions used here

- `jobs/job_posting`: NAV `value_key="nav:{uuid}"`, ATS `value_key="ats:{url}"`.
  `{title, employer_name, location, published, expires, application_url, ad_url, source}` for NAV;
  `{title, url, published, source}` for ATS.
- `jobs/recent_hiring`: single-valued (`value_key=None`), `{count_last_12_months}`.
- `activity/activity_item`: `value_key` = item URL. `{title, url, published, source}` for site
  feeds/news, `{title, url, published, platform: "YouTube"}` for videos.
- `extraction_method` values: `nav_feed_v1`, `teamtailor_rss_v1`, `lever_json_v1`,
  `greenhouse_json_v1`, `smartrecruiters_json_v1`, `careers_html_v1`, `rss_v1`, `html_news_v1`,
  `youtube_rss_v1`.

## Budget

`connector.py` gates every HTTP-using stage (ATS, feeds, YouTube) on: a verified site present, tier
`!= "T0"`, and `ctx.client.remaining(ctx.org) >= 1`. NAV never touches HTTP. YouTube resolves at most 2
linked channels and reads at most 5 videos each; feeds/news collect at most 10 dated items total.

## Known gaps / things the orchestrator should be aware of

1. **YouTube's `robots.txt` disallows `/feeds/videos.xml` for the generic user agent** (verified live:
   `Disallow: /feeds/videos.xml` under `User-agent: *`), even though it's YouTube's own documented
   public-subscription endpoint. With `respect_robots=True` (our default, matching BUILD_SPEC's
   "company sites must pass True" / general conservative stance), this means `youtube.py` will get
   `robots_disallowed` on essentially every real channel and no video claims will be published. If the
   team wants YouTube activity to actually populate, this needs an explicit policy decision (e.g. treat
   this one feed endpoint like the "official APIs may pass False" carve-out) - `youtube.py` itself
   already passes `respect_robots=True` everywhere and does not make that call unilaterally.
2. **SmartRecruiters public apply URLs are approximated.** The `/postings` list endpoint doesn't return
   the public `jobs.smartrecruiters.com` URL, only an internal id; `ats.py` constructs
   `https://jobs.smartrecruiters.com/{company}/{postingId}`, which matches SmartRecruiters' common
   pattern but isn't guaranteed for every tenant.
3. **Generic ATS HTML scraping (Webcruiter, Jobylon, ReachMee, HR-manager, Recman, Easycruit,
   Jobbnorge, Workday) is a conservative same-domain anchor scrape**, not provider-specific parsing. It
   filters short/nav-like link text but can still both miss real postings (JS-rendered lists) and pick
   up unrelated same-domain links on unusual page layouts. Tested against a hand-built fixture matching
   the Jobbnorge page shape, not a live-verified page.
4. **`feeds.py`'s HTML date extraction misses JS-rendered news pages** - confirmed live against
   `dips.com/aktuelt`, a client-rendered page with no dates or article links in the static HTML;
   `extract_date_from_html` correctly returns `(None, None)` rather than fabricating a date, so the
   page is simply dropped (no `activity` claim), which is the intended safe behavior but does mean
   JS-only news sections under-report.
5. **`tests/fixtures/activity/lever_jobs.json` is hand-built**, not live-fetched: `api.lever.co` was
   unreachable from this sandbox (timed out on every attempt). Its shape matches Lever's public
   `/v0/postings/{company}?mode=json` schema (documented and stable) but wasn't confirmed against a live
   response in this environment. `tests/fixtures/activity/news_article.html` and `careers_generic.html`
   are also hand-built (a JS-rendered real Norwegian news page and no easily-found live Jobbnorge-style
   page were available in the time budget) - both are representative, not captured-live, fixtures.
6. **No live NAV fixture**: `nav_jobs.py` is only tested against a fake `caches.nav` (per BUILD_SPEC,
   caches are W2's contract). `scripts/eval_activity_live.py` therefore cannot exercise real NAV data.

## Live measurement

`scripts/eval_activity_live.py` fetches ~14 companies from `tests/fixtures/website-probe-150.json` that
have a known site, plus DIPS AS (979543883, dips.com), with a small stdlib-only HTTP client (robots.txt
respected via `urllib.robotparser`), does a quick local extraction of feed/ATS/YouTube/news links from
each homepage, and runs the real `ats`/`feeds`/`youtube` connector code against them. It does not
exercise `nav_jobs.py` (needs `caches.nav`, W2's contract, not available standalone) and stands in for
W3's real crawl/extract (only looks at the homepage, not the fuller site).

Result from a live run (2026-09-23, 15 companies, 24 total requests): mostly small companies with no
RSS/ATS footprint at all (expected - RSS feeds and dedicated ATS systems are uncommon among small
Norwegian businesses), one YouTube channel link found and correctly blocked by YouTube's robots.txt
(see gap 1), and one JS-rendered news page correctly yielding zero activity claims rather than a
fabricated date (see gap 4). Run it yourself with:
`uv run --with beautifulsoup4 --with lxml --with pydantic python scripts/eval_activity_live.py`
