# Known limitations

Honest gaps in this agent as built, not aspirations for a future version. See each linked doc for the
full detail behind each item.

## No ratings/reviews source

`reviews` is always `not_available`, reason "no permitted keyless ratings/reviews source
(Google/Trustpilot/Glassdoor require licensed API access)" (`src/signalpost/activity/connector.py`). No
connector attempts this family at all — there is no keyless, ToS-compliant way to collect review/rating
data for a Norwegian company today, and the $0 / no-secrets constraint rules out a licensed provider.

## `group` family: parent-company flag, not full ownership

`group` is available for about 15% of companies: those whose filed annual accounts mark them as a parent
company (`virksomhet.morselskap`, published as `group_role: parent_company`), plus any company registered
as a subunit of another entity (`overordnetEnhet`). The registers publish no keyless list of subsidiaries
or owners (the shareholder register is not an open API), so the names of a parent's subsidiaries and of
a subsidiary's parent are not reported.

## Job postings: NAV feed window and capped search

Jobs come from NAV's public `pam-stilling-feed`: each run walks the feed from about 60 days back
(`If-Modified-Since` jumps straight to that date), keeps currently ACTIVE ads, matches them to the batch by
employer name, and publishes a posting only after `/feedentry/{uuid}` confirms our organisation number (or
one of our subunits). The index and feed position persist in `--state-dir`, so a daily refresh reads only
the new pages (117 pages on a fresh run, 1 on the next). Two limits remain:

- **Import-sourced ads are invisible to the feed window.** Ads imported from other job boards are never
  touched after creation, so their only feed event can be months old; even a 200-day walk did not find the
  three such companies in our gold set.
- **Search is capped.** `arbeidsplassen.nav.no`'s search endpoint rate-limits an IP after roughly 30
  searches, so the agent searches at most 15 staffed companies per run (≥5 employees, no feed match, largest
  first, rotated across runs via `--state-dir`), stopping after two consecutive 429s. Companies not
  searched keep the feed result (`not_available`), never `failed`.

## Website discovery: recall trade-off for precision

Precision over recall is a deliberate design choice (`IDENTITY_RESOLUTION.md`): `exact` requires either
one decisive signal or two independent corroborating signals with zero conflicts. This means real
company websites are sometimes left `not_available` or `ambiguous` rather than published, when the
evidence found doesn't clear that bar — e.g. a registry-declared site on a domain shared by exactly 2
organisations with no org-number-bearing page crawled (see the gold-set QA misses documented in
`docs/web.md`). Company-website company recall against the 131-company gold set:
53.3% (25 Sep 2026; see EVAL.md). No wrong-company website was published against gold at last measurement
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

`financial_history` publishes the list of years the company has filed accounts for
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
