# W4: jobs & public activity (`src/signalpost/activity/`)

`ActivityConnector` (`name="activity"`, `families=("jobs", "activity", "reviews")`) fills the
`hiring_and_activity` section. It runs after the `web` connector (W3) so `ctx.shared` may already carry
`verified_site`, `site_pages`, `social_links`, `feed_urls`, `ats_links`, `news_urls`.

## Modules

- **`nav_live.py`** (`NavLiveConnector`, `name="nav"`, `families=()`) - live, keyless lookup against the
  real NAV arbeidsplassen APIs, run by the pipeline *before* `web` (order: registry -> nav -> web ->
  activity; see `pipeline.py`). Fixes the staleness of the cached feed index (that cache is an
  append-only event log walked forward from mid-2023 - reaching today's ads needs a multi-hour walk, see
  gap 6 below and `caches/nav.py`'s module docstring). Two calls, both verified live:
  1. `GET arbeidsplassen.nav.no/stillinger/api/search?q=<name>&size=100` (keyless, robots.txt allows
     everything) - full-text search returning `hits.hits[]._source` with `uuid`, `title`, `businessName`,
     `employer.name`, `published`, `status`, but no organisation number.
  2. `GET pam-stilling-feed.nav.no/api/v1/feedentry/{uuid}` with header `Authorization: Bearer <token>` -
     confirms `ad_content.employer.orgnr`. The token is the last non-empty line of
     `GET pam-stilling-feed.nav.no/api/publicToken` (plain 401 without it, measured live); fetched once
     per connector instance (i.e. once per run) and cached behind a lock, refreshed once on a 401.
     `BudgetedHttpClient.get()` gained an additive `headers: dict[str, str] | None = None` kwarg
     (default `None`, every other call site unaffected) purely to carry this header - not part of
     `context.HttpClient`'s declared protocol, but Python's structural typing doesn't enforce that, and
     it keeps the request on the budgeted/robots/SSRF/snapshot path instead of a raw `urllib` call.

  Query names: the legal name (from `ctx.shared["registry_facts"]["name"]`, bulk `navn` fallback) with
  its trailing Norwegian legal-form token stripped (AS/ASA/SA/DA/ANS/KS/...), plus up to 2 distinct
  subunit trade names for tier T2/T3 (`registry_facts["subunits"]`). Only tiers T1-T3 run this, and only
  when `ctx.client.remaining(ctx.org) >= 1`; T0 and budget-exhausted companies get `nav_checked` state
  `not_applicable` / `failed` and make zero requests.

  Candidate filter (before any detail fetch): a search hit's normalized `employer.name` (`businessName`
  as a secondary match only when `employer.name` is empty) must equal the normalized legal name or a
  normalized subunit name - normalization is `_common.norm_title` (casefold, strip diacritics/punctuation,
  strip legal-form tokens, collapse whitespace), reused as-is from the ATS/NAV title-matching code already
  in this package. `total_matching_hits` counts every name-matched hit (any status); only `ACTIVE` ones,
  newest `published` first, get detail-fetched, capped at T1: 3, T2: 5, T3: 8. An ad is only "verified"
  (published as `nav_ads`) if `ad_content.employer.orgnr` equals `ctx.org` or a subunit organisation
  number - a name match with a different confirmed org number is recorded in evidence but never
  published as this company's job claim.

  Publishes to `ctx.shared` (no claims, no FamilyState - `nav_jobs.py` turns this into claims):
  `nav_ads` (verified ad dicts: uuid, title, employer_name, employer_orgnr, employer_homepage, published,
  expires, application_due, work_locations, ad_url, feedentry_url, retrieved_at, content_sha256,
  snapshot_ref, evidence_id), `nav_homepages` (deduped `{homepage, uuid}` from verified ads, for `web` to
  use as candidates), `nav_checked` (`{checked, state: "ok"|"not_applicable"|"failed", reason,
  checked_at}`), `nav_total_matching_hits`, `nav_search_evidence_ids` (so a zero-ads run still has
  evidence to cite). Evidence: one per search call and one per feedentry fetch, `source_class
  "public_job_feed"`, `access_policy "NLOD-2.0 (NAV)"`, span = the JSON path/value of
  `hits.total.value`/`q` (search) or `ad_content.employer.orgnr` (feedentry).

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
(`nav_live.py`, tiers T1-T3 only) makes at most 1 search request (T1) or 3 (T2/T3, legal name + ≤2
subunit names) plus at most 3/5/8 feedentry detail fetches (T1/T2/T3) - worst case 11 requests at T3,
well inside that tier's ~45-request allowance, leaving room for `web`/`registry`. The public-token fetch
is charged to `org=None` (the shared pool, not any one company's allowance), matching `caches/nav.py`'s
existing `sync_incremental` convention for the same feed family. YouTube resolves at most 2 linked
channels and reads at most 5 videos each; feeds/news collect at most 10 dated items total.

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
6. **~~No live NAV fixture~~ - resolved**: `nav_live.py` now hits the real NAV search + feedentry APIs
   directly, with trimmed real captured responses under `tests/fixtures/nav_live/` and unit tests in
   `tests/signalpost/test_activity_nav_live.py`. `nav_jobs.py`'s cache path (`_collect_cache`, the
   original implementation) is still only tested against a fake `caches.nav`, since caches remain W2's
   contract and this doesn't change that.
7. **`BudgetedHttpClient.get()` gained an additive `headers` kwarg** (`http.py`, default `None`) so
   `nav_live.py` can send `Authorization: Bearer <token>` to NAV's feedentry endpoint while staying on
   the budgeted/robots/SSRF/snapshot request path. This is not declared on `context.HttpClient`'s
   Protocol (that file is the orchestrator's contract) - Python doesn't enforce Protocol signatures at
   runtime, so it works, but the orchestrator may want to add `headers` to the formal protocol if another
   workstream needs the same capability.

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

### `nav_live.py` live measurement (real pipeline runs, 2026-09-24)

Run via the actual CLI (`uv run python -m signalpost run ...`) against 15 staffed (T1-T3) companies
including 919858400 (Atrium People AS) and 979543883 (DIPS AS). Two connector-correctness findings,
confirmed end to end:
- **Møller Bil Bergen AS** (834083922, T3): 4 name-matched hits, 3 verified (org number confirmed) ->
  `jobs` `available`, 3 `jobs/job_posting` claims plus `jobs/active_postings_count`
  `{verified: 3, name_matched_total: 4}`.
- **Norsk Scania AS** (879263662, T3): 2 matched, 2 verified -> `available`, 2 postings.
- Most others (DIPS AS, Vinmonopolet, Norwegian Air Shuttle AOC, Würth Norge, Intrum, Hansa Borg, JM
  Norge, Bertel O. Steen Lastebil og Buss): search succeeded, 0 name-matched hits -> correctly
  `not_available` "checked NAV arbeidsplassen: no active postings for this org number", not a silent
  zero.
- **Bergene Holm AS** (812750062, T3): 4 name-matched ACTIVE hits, but every one of their feedentry
  fetches returned 404 (measured live - the search index apparently retains hits whose feedentry
  record has since been removed/archived, seen only for this company in this run). Correctly published
  as 0 verified ads (never guesses an org number from a 404), not a false positive.
- **Atrium People AS** (919858400): classified tier **T0** by the planner (bulk `antallAnsatte` is
  empty/0 for this org - it staffs projects via contractors/subunits rather than registering direct
  employees), so `NavLiveConnector` correctly makes zero requests and reports `not_applicable`
  ("not searched: no registered staff or web footprint") - this is the planner's tier signal working as
  designed (BUILD_SPEC's tiers are bulk-only signals), not a `nav_live.py` bug; a separate direct
  search+feedentry check (outside the pipeline, same code path) against 919858400 confirms the connector
  itself does find and verify Atrium People's live NAV ads (orgnr 919858400, homepage
  www.atriumpeople.no) when it's allowed to run - see `tests/fixtures/nav_live/`, captured from this
  exact company.

**Rate limiting was the dominant effect across full 15-company batch runs.** The search endpoint
(`arbeidsplassen.nav.no/stillinger/api/search`, CDN/WAF-fronted) 429'd the large majority of requests in
two full-batch runs (429 rate ~90% and ~85% of search attempts respectively) despite the
`_throttle_search` spacing (`MIN_SEARCH_INTERVAL_S=3.0`) and bounded retry-with-backoff - almost
certainly because this same sandbox's egress IP had already been driving repeated manual `curl` probes
against the same endpoint earlier in the same session while verifying the API shape, which appears to
trip a ban that decays over several minutes rather than a simple per-second token bucket (a clean
2-company run issued shortly after a ~15-minute gap succeeded 100%: Würth Norge AS and Hansa Borg AS
both got a real `200` search response with a genuine 0-match result). Companies whose every search
attempt 429'd (OneSubsea Processing AS, Spenncon AS, Framo Fusa AS in the affected runs) correctly
surfaced `jobs` `failed` reason `"nav_live_search: http_4xx"` rather than a false `not_available` -
this exercised the safety net added specifically for this failure mode (see `nav_live.py`: if every
search attempt errors, `nav_checked` state is `failed`, never `ok` with an empty result). Total requests
for the 15-company batch stayed well inside the 2,000 cap (155-303 depending on the run's retry/429
count) and completed in 2-7 minutes; a production run against a cold IP (no prior manual probing) should
see a search success rate close to the clean 2-company run's 100%.
