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
    the same function `nav_feed.NavFeedIndex` uses to index each ad's business name. Two passes: an
    exact normalized match first, then `NavFeedIndex.match_fuzzy()` (token overlap >= 60% of the
    smaller token set - catches a trade name, an "avd." department suffix, or a truncated
    businessName) widens the candidate pool for the same names. Fuzzy matching never lowers the
    publishing bar - every candidate, exact or fuzzy, still needs its organisation number confirmed via
    feedentry below. `nav_total_matching_hits` counts every match (verified or not, exact or fuzzy);
    the newest-changed (`sistEndret`) matches, capped at T1: 3, T2: 5, T3: 8, get a
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

Run via the actual CLI (`uv run python -m signalpost run ...`), 2026-09-24/25, against two sets: the
131-company gold set and a 100-company daily rehearsal set, each run fresh (empty `--state-dir`) and
then rerun with the *same* `--state-dir` to demonstrate the incremental resume.

| Run | Requests (of which `nav_feed_page`) | Runtime | `jobs` states | Terminal status |
|---|---|---|---|---|
| gold131, fresh state-dir | 1788 (117) | 539s | 1 available / 107 not_available / 23 not_applicable / **0 failed** | 125 completed, 6 partial |
| daily100, fresh state-dir | 785 (117) | 336s | 0 available / 19 not_available / 81 not_applicable / **0 failed** | 100 completed, 0 partial |
| daily100, same state-dir (resume) | 669 (**1**) | 166s | identical to the fresh run above | 100 completed, 0 partial |

(These are the final numbers, after merging `main`'s run-report fix - robots.txt fetches were
previously double-counted in `total_requests` - and after adding `match_fuzzy()`, below. Fuzzy matching
raised `nav_live_feedentry` from 2 to 40 on gold131 - it widened the candidate pool considerably - while
the verified-postings count stayed at exactly 1, confirming every extra candidate it found was
correctly rejected by the mandatory orgnr check, not a source of false positives. The remaining
`partial` envelopes in gold131 are YouTube `robots.txt` blocks (gap 1), unrelated to `jobs`; the
`caches/store.py` concurrency bug from an earlier measurement here was fixed upstream on `main` and
picked up by the merge, dropping gold131's `partial` count from 7-8 to 6.)

Zero `jobs=failed` across both sets - the search-based v1 measured 93/131 failed on this same gold set
(see the superseded section above). The resume run's `nav_feed_page` count dropping from 117 to 1 (and
total requests 552 vs 668, runtime 151s vs 351s) is the incremental-walk behavior working as designed:
the second run's feed walk re-fetched only the last page from the first run's saved cursor, found it
was still the tip of the feed (`next_id` still null), and stopped - `jobs` results are byte-identical
between the fresh and resumed daily100 runs, confirming the resume doesn't lose or duplicate any ads.

The `partial` terminal statuses in both fresh runs are **not** a `nav_live.py`/`nav_feed.py` issue: they
trace to a pre-existing, unrelated bug in `caches/store.py`'s shared sqlite connections (`stage:
"candidates"`, `InterfaceError: bad parameter or other API misuse` - the same class of concurrency bug
`NavFeedIndex` had and was fixed for in this same work, just in a different module this workstream
doesn't own) and, for several gold131 companies, YouTube's `robots.txt` blocking their channel feed (a
known, already-documented gap, see gap 1 above) - both flagged separately, neither affects `jobs`.

One real posting was found and verified end to end in the gold131 run: **HALLAGERSTUA BARNEHAGE SA**
(971474351) - "Bli ringevikar i en liten barnehage med natur, bevegelse og trygghet i sentrum",
matched by name against the feed index, confirmed by org number via feedentry, published as an
`available` `jobs/job_posting` claim.

### Recall gap: import-sourced ads are absent from the feed API entirely

`eval/data/gold_web.jsonl` independently labels 4 companies with an active NAV posting as of
2026-09-24: HALLAGERSTUA BARNEHAGE SA (found, above), OSLO AKUTTEN AS, FRESH WATER NORWAY AS, and
Olsen Nauen Klokkestøperi AS (all 3 missed). Diagnosis (live, 2026-09-24/25): a fresh, from-scratch
200-day feed walk (427 pages, confirmed `reached_end=True` - it caught up to the live tip) found zero
exact or fuzzy match for any of the 3 missed companies anywhere in that much larger index (10,523 ads,
barely more than the 60-day window's 10,081 - see below). Re-checking `arbeidsplassen.nav.no`'s own
search API confirms `NavLiveConnector`'s earlier, since-verified Atrium People AS ad (orgnr 919858400,
`tests/fixtures/nav_live/feedentry_atrium_people.json`) is *still* `ACTIVE` right now - but a direct
feedentry re-fetch shows its `sistEndret` is unchanged since `2025-12-29`, ~9 months ago, and that same
200-day-deep feed walk does not contain it either. NAV's feed event log only fires on a status
**change** (see `caches/nav.py`'s and `nav_feed.py`'s module docstrings); an ad created once through
NAV's `IMPORTAPI`/third-party-recruiter import path and never subsequently touched sits at its
original, potentially very old, feed position forever - not "outside a 60-day window" so much as
"outside any window this connector can afford to walk". This is a genuine structural gap in the feed
API, not a matching bug: **no window length and no name-normalization fix can close it**, since the
affected ads simply never generate a feed event for the walk to see.

What this rules in and out as fixes:
- **Window extension: measured and rejected as the general fix.** 60 -> 200 days (117 -> 427 pages)
  grew the indexed active-ad count by only ~4.4% (10,081 -> 10,523, against NAV's own reported ~14k
  total active ads platform-wide - see below), and doesn't even recover these 3 companies (their sole
  feed event is older than 200 days). Meanwhile gold131's fresh run already sits close to the 1,900
  hard cap (1788-1856 across measured runs) - lengthening the default window would trade a real budget
  risk for a small, diminishing-returns recall gain that doesn't fix the actual problem.
  `DEFAULT_WINDOW_DAYS` stays at 60.
- **Fuzzy matching: added, doesn't recover these 3 either, but is still worth keeping.**
  `NavFeedIndex.match_fuzzy()` (token overlap >= 60% of the smaller set, same shape as `caches/nav.py`'s
  existing `fuzzy_name_match`) runs alongside the exact match for every company; a live gold131 rerun
  with it enabled still finds exactly the same 1 verified posting (`nav_live_feedentry` requests rose
  2 -> 40 as fuzzy widened the candidate pool, all but the 1 real match correctly rejected by the
  mandatory orgnr check) - confirming it adds real candidates for genuine trade-name/"avd."-suffix
  cases without ever publishing a false positive, but that the 3 gold misses are an index-coverage gap,
  not a name-matching one.
- **Reintroducing search was considered and deliberately not done.** It would close this specific gap
  (search *does* index import-sourced ads, since it's how the independent labeller found them), but
  it's exactly the mechanism this rewrite replaced for failing at batch scale (see "History" above) -
  re-adding it, even scoped to zero-match companies only and carefully throttled, needs an explicit
  product decision given the observed WAF behavior, not a unilateral reintroduction here.

**Index coverage vs NAV's total.** The 60-day index holds ~10,100 currently-ACTIVE ads; NAV's own
platform-wide total is on the order of ~14,000 active ads (per the gold-labelling session's own
estimate, not independently re-measured here). That gap is consistent with, and roughly the right
order of magnitude for, exactly the import-sourced/never-updated-since-creation category described
above - most of NAV's day-to-day posting activity is captured by the 60-day window; the remainder is
disproportionately old, one-off-imported listings the feed event log was never going to show without
walking back to their original (unpredictable, sometimes very old) creation date.
