# Known limitations

Honest gaps in this agent as built, not aspirations for a future version. See each linked doc for the
full detail behind each item.

## Ratings/reviews: official food-hygiene inspections only

`reviews` carries one source: Mattilsynet's food-hygiene inspection result (smilefjes) for food-service
locations, published only when the inspection page's own "Orgnr." is the company's or one of its
subunits' (`src/signalpost/activity/smilefjes.py`). That covers restaurants, cafés, caterers and similar,
roughly 0–3% of a random batch (2.3% of a 300-company sample). Consumer review platforms (Google, Trustpilot, Glassdoor) need licensed API
access and are not used; every other company reports `not_available` with that reason.

## `group` family: parent-company flag, not full ownership

`group` is available for roughly 12–15% of companies: those whose filed annual accounts mark them as a parent
company (`virksomhet.morselskap`, published as `group_role: parent_company`), plus any company registered
as a subunit of another entity (`overordnetEnhet`). The registers publish no keyless list of subsidiaries
or owners (the shareholder register is not an open API), so the names of a parent's subsidiaries and of
a subsidiary's parent are not reported.

## Job postings: NAV feed snapshot plus live catch-up

Jobs come from NAV's public `pam-stilling-feed`. The agent ships a snapshot of the ads that were active when
it was built (`caches/snapshot/nav_active_ads.jsonl.gz`, with each ad's employer org number and homepage,
built by `scripts/build_nav_snapshot.py`) and, at run time, walks only the feed published since that
snapshot (in parallel time slices) so ads opened or closed since are applied. Ads are matched to the batch
by employer org number (snapshot) and by employer name, and a posting is published only after
`/feedentry/{uuid}` confirms, live, our organisation number or a subunit's. Limits:

- **Active ads last changed before the snapshot window** (about 3% of active ads are older than 60 days)
  are included only if they were active when the snapshot was built.
- **Import-sourced ads** (copied from other job boards) are touched only at creation, so an old one may be
  missing.
- **Search is off by default** (`SIGNALPOST_NAV_SEARCH_MAX`): NAV's search endpoint rate-limits an IP after
  roughly 30 searches.

## Website discovery: recall trade-off for precision

Precision over recall is a deliberate design choice (`IDENTITY_RESOLUTION.md`): `exact` requires either
one decisive signal (org number on the site, or an org-number-keyed record: register, Wikidata,
OpenStreetMap `ref:NO:orgnr`, a NAV ad) or two independent corroborating signals with zero conflicts. The
company's name in the page title and the company's name as the domain count as one signal, never two: a
same-named company (in Norway or abroad) shows both, so a guessed domain always needs an independent
registry fact as well (address, phone, e-mail, a board member, or an open-places match on the registry
phone/e-mail). Parked, "coming soon" and registrar placeholder pages are never accepted. As a result some
real websites stay `not_available` or `ambiguous` when the page shows nothing that ties it to the
register. A registry-declared site shared by a few group companies (or tied to the org number by
Wikidata/OSM but showing a subsidiary's number) is published as the company's website labelled with the
relationship (`parent`/`brand`), never as an exact own-site; profiles and descriptions are not taken from
it. Domains shared by ten or more organisations (property managers, franchise platforms) are not
published. Sites that block automated visitors (HTTP 403 "request blocked") are not worked around.

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
debt) are extracted, from the separate `regnskapsregisteret/regnskap/{org}` endpoint. There is no
multi-year revenue trend: that would need per-year detail fetches this budget doesn't allocate for.

For shell-tier companies (no staff, no website or e-mail domain in the register) the filed-years endpoint,
which is throttled to about one call a second for the whole run, is not called: `financial_history`
carries `latest_filed_year` from the same-day register bulk file (`sisteInnsendteAarsregnskap`) instead of
the full list.

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
