# W4: jobs & public activity (`src/signalpost/activity/`)

`ActivityConnector` (`name="activity"`, `families=("jobs", "activity", "reviews")`) fills the
`hiring_and_activity` section. It runs after the `web` connector (W3) so `ctx.shared` may already carry
`verified_site`, `site_pages`, `social_links`, `feed_urls`, `ats_links`, `news_urls`.

## Modules

- **`nav_feed.py`** (`NavFeedIndex`, `walk_feed`) - a shared, once-per-run "active ads" index built by
  walking NAV's feed API (`pam-stilling-feed.nav.no/api/v1/feed`) forward from `now - window_days`
  (default 60) instead of NAV's search API. History: an earlier version of `nav_live.py` queried
  `arbeidsplassen.nav.no/stillinger/api/search` once per company; that endpoint rate-limits per IP hard
  enough that a live 131-company gold-set run measured 93 companies ending `jobs=failed`
  ("nav_search_rate_limited") and zero verified postings - per-company search cannot work at batch
  scale. The fix (measured live 2026-09-24): `GET /api/v1/feed` with header
  `If-Modified-Since: <HTTP-date>` jumps straight to the page containing that time instead of the feed's
  2023 start; each page has ~1,000 items (measured: 9 pages covered 3 days, 3,349 unique ads, 1,920
  ACTIVE), so walking from `now - 60d` reaches the live tip in roughly 180 requests / ~10 minutes - a
  one-time run-level cost, not a per-company one. `NavFeedIndex` is a small sqlite table
  (`<state-dir>/nav_feed_index.sqlite`) of currently-ACTIVE ads keyed by uuid, with a normalized
  business-name column for matching and a `nav_feed_cursor` table holding the last page id reached -
  mirrors `caches/nav.py`'s `build_full`/cursor pattern exactly, just seeded by `If-Modified-Since`
  instead of page 1. A later run with the *same* `--state-dir` resumes from that cursor (re-fetches the
  last page, since NAV may have appended a new one after it, then continues via `next_id`) instead of
  re-walking the whole window - a few pages per day once caught up. A later, non-ACTIVE event for a uuid
  (the feed is an append-only event log) removes it from the index - supersession, not overwrite-in-place.
  Bounded by `max_pages` (default 200) and `max_seconds` (default 720s / 12 min); if the walk doesn't
  reach the live tip within those bounds, the index is left `partial` (`NavFeedIndex.is_complete() ==
  False`) and callers must say so rather than reporting a confident zero. Every feed-page request is
  charged to `org=None` (the shared pool), same convention as `caches/nav.py`'s `sync_incremental` and
  this package's own NAV public-token fetch - it never touches a company's own allowance or the registry
  reserve (`http.Budget`'s reserve only shields purposes prefixed `registry_`).

- **`nav_live.py`** (`NavLiveConnector`, `name="nav"`, `families=()`) - run by the pipeline *before*
  `web` (order: registry -> nav -> web -> activity; see `pipeline.py`).
  - **`prepare(client, state_dir)`**: builds/resumes the shared `NavFeedIndex` via `nav_feed.walk_feed`,
    exactly once per connector instance (idempotent, thread-safe - a second/concurrent call is a no-op
    once the first has started). `pipeline.run_batch` calls this once, in a background thread, right
    after constructing the shared `HttpClient` and *before* submitting any company to the worker pool,
    so the walk overlaps with the first companies' registry work instead of purely blocking the run;
    `run()` also calls it lazily (blocking) as a same-thread fallback for standalone use / tests that
    build a `CompanyContext` directly without going through `pipeline.py`.
  - **`run(ctx)`** (tiers T1-T3 only; T0 and budget-exhausted companies get `nav_checked` state
    `not_applicable` / `failed` and touch neither the index nor any HTTP request): waits for the shared
    index to be ready, then matches its currently-ACTIVE ads by normalized business name - no HTTP,
    purely a local sqlite lookup - against the union of the legal name (`ctx.shared["registry_facts"]
    ["name"]`, bulk `navn` fallback, trailing Norwegian legal-form token stripped for readability but not
    for matching), `registry_facts["aliases"]`, `registry_facts["subunits"][*]["name"]`, and
    `ctx.caches.aliases.names(org)` trade names when a caches instance is available - normalization is
    `_common.norm_title` (casefold, strip diacritics/punctuation/legal-form tokens, collapse whitespace),
    the same function `nav_feed.NavFeedIndex` uses to index each ad's business name, so an exact
    normalized match is required (no fuzzy scoring). `nav_total_matching_hits` counts every match
    (verified or not); the newest-changed (`sistEndret`) matches, capped at T1: 3, T2: 5, T3: 8, get a
    `GET pam-stilling-feed.nav.no/api/v1/feedentry/{uuid}` fetch with `Authorization: Bearer <token>` -
    the token is the last non-empty line of `GET .../api/publicToken` (plain 401 without it, measured
    live), fetched once and cached behind a lock, refreshed once on a 401.
    `BudgetedHttpClient.get()` gained an additive `headers: dict[str, str] | None = None` kwarg (default
    `None`, every other call site unaffected) purely to carry this header and `If-Modified-Since` - not
    part of `context.HttpClient`'s declared protocol, but Python's structural typing doesn't enforce
    that, and it keeps both on the budgeted/robots/SSRF/snapshot request path instead of a raw `urllib`
    call. An ad is only "verified" (published as `nav_ads`) if `ad_content.employer.orgnr` equals
    `ctx.org` or a subunit organisation number - a name match with a different confirmed org number is
    recorded in evidence but never published as this company's job claim. The earlier per-company search
    endpoint, its throttle, and its 3-consecutive-429 circuit breaker were removed entirely in this
    rewrite (not kept behind a flag): they cannot work at batch scale (see above), so there was nothing
    worth preserving as an option.

  Publishes to `ctx.shared` (no claims, no FamilyState - `nav_jobs.py` turns this into claims):
  `nav_ads` (verified ad dicts: uuid, title, employer_name, employer_orgnr, employer_homepage, published,
  expires, application_due, work_locations, ad_url, feedentry_url, retrieved_at, content_sha256,
  snapshot_ref, evidence_id), `nav_homepages` (deduped `{homepage, uuid}` from verified ads, for `web` to
  use as candidates), `nav_checked` (`{checked, state: "ok"|"not_applicable"|"failed", reason,
  checked_at, index_complete}` - `reason` names an incomplete/partial feed index explicitly on a
  zero-match result, so a company is never told "no postings" when the index simply hasn't reached that
  part of the feed yet), `nav_total_matching_hits`, `nav_search_evidence_ids` (one shared "feed index
  build" evidence id, from `prepare()`, reused across every company so a zero-ads run still has evidence
  to cite on `jobs/active_postings_count`). Evidence: one per feedentry fetch plus that one shared
  feed-index-build evidence, `source_class "public_job_feed"`, `access_policy "NLOD-2.0 (NAV)"`, span =
  `active_ads_indexed=...; window_days=...; complete=...` (index) or the JSON path/value of
  `ad_content.employer.orgnr` (feedentry).

- **`nav_jobs.py`** - `collect(ctx)` prefers the live path (`ctx.shared["nav_checked"]` present, i.e.
  `NavLiveConnector` ran for this company) and falls back to the cached NAV (arbeidsplassen.no) ad feed
  (`ctx.caches.nav.ads_for([...])`) otherwise:
  - **Live path** (`_collect_live`): one `jobs/job_posting` claim per verified `nav_ads` entry
    (`value_key="nav:{uuid}"`, `identity_basis="job_feed_org_number"`, `effective_date=published`,
    evidence = the feedentry's own evidence id from `nav_live.py`) plus one
    `jobs/active_postings_count` claim (`value_key=None`, `{verified, name_matched_total, checked_at}`,
    evidence = all feedentry evidence ids plus the search evidence ids so it's never claimed with zero
    evidence). No `jobs/recent_hiring` claim on this path (superseded by `active_postings_count`).
  - **Cache path** (`_collect_cache`, the original implementation, unchanged): no HTTP, works at every
    tier including T0. Looks up the company's own org number plus any subunit org numbers from
    `ctx.caches.aliases.subunits(org)`. One `jobs/job_posting` claim per `ACTIVE` ad
    (`value_key="nav:{uuid}"`) and one `jobs/recent_hiring` summary claim (`value_key=None`) counting ads
    of any status published in the last 365 days.
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
  (NAV live and/or cache, and/or ATS). Otherwise: on the live path, `nav_jobs._collect_live` decides
  directly - `not_available` reason "checked NAV arbeidsplassen: no active postings for this org number"
  when searched and nothing verified, `not_applicable` reason "not searched: no registered staff or web
  footprint" for T0, `failed` reason `request_budget` on budget exhaustion; on the cache path (no live
  connector ran) the old reason text applies ("checked NAV feed (cache built …): no active postings"),
  distinct from `not_applicable` (no NAV cache supplied) and `failed` (the cache lookup raised).
  `activity` is `not_applicable` when there's no verified site or the tier is T0 (no HTTP budget);
  `not_available` reason "checked: none found" when feeds/news/YouTube were checked and nothing dated
  turned up; `failed` when every attempted source errored.

## Claim conventions used here

- `jobs/job_posting`: NAV cache `value_key="nav:{uuid}"` `{title, employer_name, location, published,
  expires, application_url, ad_url, source: "NAV"}`; NAV live `value_key="nav:{uuid}"`
  `{title, employer_name, location, published, expires, application_due, ad_url,
  source: "NAV arbeidsplassen"}`; ATS `value_key="ats:{url}"` `{title, url, published, source}`.
- `jobs/recent_hiring` (cache path only): single-valued (`value_key=None`), `{count_last_12_months}`.
- `jobs/active_postings_count` (live path only): single-valued (`value_key=None`),
  `{verified, name_matched_total, checked_at}`.
- `activity/activity_item`: `value_key` = item URL. `{title, url, published, source}` for site
  feeds/news, `{title, url, published, platform: "YouTube"}` for videos.
- `extraction_method` values: `nav_feed_v1` (cache), `nav_live_search_v1` / `nav_live_feedentry_v1`
  (live), `teamtailor_rss_v1`, `lever_json_v1`, `greenhouse_json_v1`, `smartrecruiters_json_v1`,
  `careers_html_v1`, `rss_v1`, `html_news_v1`, `youtube_rss_v1`.

## Budget

`connector.py` gates every HTTP-using stage (ATS, feeds, YouTube) on: a verified site present, tier
`!= "T0"`, and `ctx.client.remaining(ctx.org) >= 1`. NAV's cache path never touches HTTP; NAV's live path
(`nav_live.py`, tiers T1-T3 only) spends nothing per company on matching (a local sqlite lookup against
the shared `NavFeedIndex`) and at most 3/5/8 feedentry detail fetches per company (T1/T2/T3) - a few
requests, well inside any tier's allowance, leaving room for `web`/`registry`. The run-level cost is the
one-time (or, with a persistent `--state-dir`, effectively one-time-ever) feed walk: up to `max_pages`
(200) requests bounded by `max_seconds` (720s), charged to `org=None` (the shared pool, not any one
company's allowance or the registry reserve), matching `caches/nav.py`'s existing `sync_incremental`
convention for the same feed family. YouTube resolves at most 2 linked channels and reads at most 5
videos each; feeds/news collect at most 10 dated items total.

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
6. **~~No live NAV fixture~~ - resolved**: `nav_live.py`/`nav_feed.py` hit the real NAV feed + feedentry
   APIs directly, with a trimmed real captured feedentry response under `tests/fixtures/nav_live/` and
   unit tests in `tests/signalpost/test_activity_nav_live.py` / `test_activity_nav_feed.py`.
   `nav_jobs.py`'s cache path (`_collect_cache`, the original implementation) is still only tested
   against a fake `caches.nav`, since caches remain W2's contract and this doesn't change that.
7. **`BudgetedHttpClient.get()` gained an additive `headers` kwarg** (`http.py`, default `None`) so
   `nav_feed.py`/`nav_live.py` can send `Authorization: Bearer <token>` and `If-Modified-Since` while
   staying on the budgeted/robots/SSRF/snapshot request path. This is not declared on
   `context.HttpClient`'s Protocol (that file is the orchestrator's contract) - Python doesn't enforce
   Protocol signatures at runtime, so it works, but the orchestrator may want to add `headers` to the
   formal protocol if another workstream needs the same capability.
8. **`nav_live.py`'s per-company search implementation was rate-limit-bound at batch scale** (see the
   `nav_feed.py` module docstring and the "History" paragraph under `nav_live.py` above) and has been
   replaced by the shared feed-index approach; this is noted here so anyone reading old run reports that
   mention `nav_search_rate_limited` or `nav_live_search` understands why those don't appear any more.
9. **Feedentry 404s on old/import-sourced ads**: a live run against the earlier search-based
   implementation observed a company (Bergene Holm AS) whose search hits all 404'd on feedentry -
   NAV's search index apparently retains some ads whose feedentry record has since been removed. The
   feed-index approach can hit the same thing for any uuid it indexes; `nav_live.py` already handles it
   correctly (a 404 is recorded in `errors` and the ad is simply never verified/published), so this is a
   noted behavior, not an open bug.

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

### `nav_live.py` v1 (per-company search) live measurement - superseded, kept for history

The paragraphs below describe the *first* implementation (per-company `arbeidsplassen.nav.no` search),
kept only so old run artifacts that mention `nav_live_search` / `nav_search_rate_limited` have an
explanation. That implementation has been removed; see "History" under `nav_live.py` above and the fresh
feed-index measurement in the next section for the current behavior.

A 2026-09-24 run against 15 staffed (T1-T3) companies confirmed the matching/verification logic end to
end when search succeeded (Møller Bil Bergen AS: 4 matched/3 verified; Norsk Scania AS: 2 matched/2
verified; several others correctly 0-matched with a real `200` response). But a *separate*, larger run
against the 131-company gold set measured the endpoint's real batch-scale behavior: 93 companies ended
`jobs=failed` ("nav_search_rate_limited") and zero companies had any verified posting - the search
endpoint's per-IP rate limit trips almost immediately under the pipeline's normal concurrency, no matter
how much a single connector throttles or retries its own requests. That measurement is what motivated
the feed-index rewrite.

### `nav_live.py` / `nav_feed.py` (feed-index) live measurement
