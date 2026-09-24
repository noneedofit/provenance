# Known limitations

Honest gaps in this agent as built, not aspirations for a future version. See each linked doc for the
full detail behind each item.

## No ratings/reviews source

`reviews` is always `not_available`, reason "no permitted keyless ratings/reviews source
(Google/Trustpilot/Glassdoor require licensed API access)" (`src/signalpost/activity/connector.py`). No
connector attempts this family at all — there is no keyless, ToS-compliant way to collect review/rating
data for a Norwegian company today, and the $0 / no-secrets constraint rules out a licensed provider.

## `group` family mostly `not_available`

`registry.RegistryConnector` only publishes a `group/parent` claim when the live entity response itself
carries `overordnetEnhet`/`hovedenhet` — i.e. when this organisation *is itself registered as a subunit*
of another entity. For a normal parent company (the common case), there is no live/bulk field that lists
its subsidiaries directly, so `group` reports `not_available`, reason "no parent relationship in registry
data," for the large majority of companies. This is a genuine coverage gap, not a bug: resolving the
reverse direction (which orgs list *this* one as their `overordnetEnhet`) would need a reverse index over
the full subunit bulk file, which the current caches don't build.

## NAV search rate-limiting and circuit breaker

`arbeidsplassen.nav.no`'s search endpoint (CDN/WAF-fronted) trips a 429 ban after a burst of roughly 6–8
requests from one IP, observed live to take several minutes to clear. In a full 15-company batch run
during development, this made **~85–90% of search attempts fail with 429** despite serialized throttling
(`nav_live.py`'s `_throttle_search`, minimum 3s spacing) and a bounded retry-with-backoff — largely
because the same sandbox IP had already been driving repeated manual probes against the endpoint earlier
in the session. A clean IP with no prior probing measured a 100% search success rate in the same testing.
When every search attempt for a company errors, that company's `jobs` family correctly reports `failed`
(reason `nav_live_search: http_4xx`), never a false `not_available` — but this means a run from a "hot"
IP can under-report jobs coverage through no fault of the identity/candidate logic. See
`docs/activity.md`'s live-measurement section for the full numbers.

## Website discovery: recall trade-off for precision

Precision over recall is a deliberate design choice (`IDENTITY_RESOLUTION.md`): `exact` requires either
one decisive signal or two independent corroborating signals with zero conflicts. This means real
company websites are sometimes left `not_available` or `ambiguous` rather than published, when the
evidence found doesn't clear that bar — e.g. a registry-declared site on a domain shared by exactly 2
organisations with no org-number-bearing page crawled (see the gold-set QA misses documented in
`docs/web.md`). Company-website company recall against the 131-company gold set:
`<GOLD_RESULTS>`. No wrong-company website was published against gold at last measurement
(`wrong_company_publications: 0`).

## JS-only sites are not rendered

`web/crawl.py` fetches server-rendered HTML only (stdlib `urllib` + BeautifulSoup/trafilatura) — no
headless browser, by design and budget. A site that is a JS-only shell with no server-rendered fallback
content is detected and recorded (`CrawlResult.js_shell`), and typically ends up `rejected` for lack of
extractable evidence, even when it genuinely is the right company. The same applies to
`activity/feeds.py`'s news-date extraction: a confirmed live case (`dips.com/aktuelt`, client-rendered,
no dates or article links in the static HTML) correctly yields zero `activity` claims rather than a
fabricated date, but this means JS-rendered news sections systematically under-report.

## YouTube activity is effectively never populated

`activity/youtube.py` is fully implemented (resolves a channel id, reads
`youtube.com/feeds/videos.xml`) and always passes `respect_robots=True`. **YouTube's own `robots.txt`
disallows `/feeds/videos.xml` for a generic user agent** (verified live), so in practice every real
channel link gets `robots_disallowed` and no video claims are ever published — confirmed in a live
measurement run (one linked YouTube channel found, correctly blocked). This is treated as a source
restriction to respect, not a bug to route around; see `SOURCES.md`.

## Financial history limited to the list of filed years

`financial_history` (T2/T3 only) publishes the list of years the company has filed accounts for
(`regnskap/aarsregnskap/kopi/{org}/aar`), not the actual figures for each of those years — only the
**latest** filed year's metrics (revenue, operating result, annual result, total assets, equity, total
debt) are extracted, from the separate `regnskapsregisteret/regnskap/{org}` endpoint. A full multi-year
financial trend beyond the single revenue-trend synthesis sentence (which compares the latest two periods
only, when both are available) would need per-year detail fetches this budget doesn't allocate for.

## Rate-limited/slow-to-build caches

The NAV job-feed cache (`caches/nav.py`) is a full-history **event log**, not a snapshot — it can only be
walked forward, page by page, at a measured ~3.7s/page server-side latency that no client-side throttling
changes. A full walk to "now" is a multi-hour, multi-session `prepare`-time job (see `docs/caches.md`);
the cache shipped with this submission covers only pages 1–540 of an unknown-length feed, and of the
1,373 ads detail-fetched so far, only 1 carries full employer/title data (NAV's API only serves an ad's
full detail while it is *currently* active — most ads seen ACTIVE in this feed region had already closed
by the time their detail was fetched). `nav_live.py`'s live search+feedentry path exists specifically to
work around this staleness for the timed run itself, subject to the rate-limiting caveat above.

## Other gaps carried from module-level docs

- `SmartRecruiters` public apply URLs are approximated (`ats.py` constructs a common-pattern URL, not
  guaranteed for every tenant).
- Generic ATS HTML scraping (Webcruiter, Jobylon, ReachMee, HR-manager, Recman, Easycruit, Jobbnorge,
  Workday) is a conservative same-domain anchor scrape, not provider-specific parsing — can both miss
  real postings (JS-rendered lists) and pick up unrelated same-domain links on unusual layouts.
- `Aliases.names()`'s prefix-based parent-name dedup (caches) is a simple heuristic and can occasionally
  drop a real short brand name or keep a low-information suffix variant it doesn't recognize.
- The client-side "most data found" sort in the viewer ranks by count of `available` families only, not
  weighted by how many claims each family contributed.
- The viewer's "Ask this profile" Q&A box is keyword/substring matching, not a language model — it will
  occasionally miss an oddly phrased question and fall back to "not found in checked sources" even when a
  relevant claim exists under a different family than expected.
