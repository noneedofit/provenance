# Crawlers / connectors: budgets and fallback rules

Every connector implements `context.Connector` (`name`, `families`, `run(ctx) -> ConnectorResult`) and
is driven by `pipeline.run_batch` in a fixed order: `registry → nav → web → activity`
(`pipeline._default_connectors`). All outbound HTTP goes through the single `BudgetedHttpClient`
(`src/signalpost/http.py`) — no connector makes a raw request of its own. See `README.md` for the
request-budgeting mechanics (global cap, per-org allowance, borrowing, redirects/retries counted) and
`SOURCES.md` for licence/robots detail per source.

## `registry` (`src/signalpost/registry.py`, `RegistryConnector`)

Families: `identity`, `financials`, `financial_history`, `leadership`, `locations`, `group`.

- **Bulk CSV** (`registry.load_bulk`) — one streamed pass over the (gzip) Brønnøysund `enheter` bulk
  download, keeping only the organisation numbers in the batch. Used as the identity fallback when a
  live call fails, and as the source of `planner.classify`'s tier signals.
- **Live calls**, all `purpose="registry_*"`, `respect_robots=False` (official keyless API):
  - `GET enheter/{org}` — identity.
  - `GET enheter/{org}/roller` — leadership.
  - `GET underenheter?overordnetEnhet={org}&size=100` — locations (subunits); skipped at tier T0 (business
    address only).
  - `GET regnskapsregisteret/regnskap/{org}` — latest financials.
  - `GET regnskap/aarsregnskap/kopi/{org}/aar` — filed years, every company (rate-limited to 1/s; skipped
    in registry-only mode).
- **Budget**: every company's registry calls are reserved up front (3 requests at T0, 4 at T1, 5 at
  T2/T3) via `Budget.reserve`, so optional sources (web, jobs) can never starve official data; the
  reserve is released back to the shared pool as soon as the connector finishes for that company.
- **Fallback**: a 404/410 on any live endpoint records `not_available` with the HTTP status as reason,
  never a crash; identity falls back to the bulk row if the live entity call fails entirely.

## `nav` (`src/signalpost/activity/nav_live.py`, `NavLiveConnector`)

Families: none (publishes only to `ctx.shared`). Runs before `web` so a NAV-confirmed employer homepage
can seed website candidates.

- **Feed index (once per run)**: `GET pam-stilling-feed.nav.no/api/v1/feed` with `If-Modified-Since` set
  60 days back, following `next_url` to the live tip (~115 pages on a fresh state, capped at 200 pages /
  12 minutes). Keeps the latest state of every ACTIVE ad in `--state-dir/nav_feed_index.sqlite`; later
  runs with the same state-dir read only the new pages (about 1 per day). Bearer token = last line of
  `GET pam-stilling-feed.nav.no/api/publicToken`.
- **Per company**: match ACTIVE ads by normalised employer name (legal name, trade names, subunit names;
  exact then token-overlap), then `GET /api/v1/feedentry/{uuid}` to confirm the ad's organisation number.
- **Capped search fallback**: `arbeidsplassen.nav.no/stillinger/api/search` rate-limits an IP after ~30
  searches, so at most 15 staffed companies (≥5 employees, no feed match, largest first, rotated across
  runs via the state-dir) are searched per run, ≥4 s apart, stopping after two consecutive 429s. A company
  not searched keeps its feed-based result, never `failed`.
- **Fallback**: an ad is only published if `ad_content.employer.orgnr` matches this org number or one of
  its subunits; a name match with a different confirmed org number is recorded in evidence but discarded.

## `web` (`src/signalpost/web/`, `WebConnector`)

Families: `website`, `profiles`, `description`. See `IDENTITY_RESOLUTION.md` for the full verification
contract; this section covers budgets and fallback order.

- **Candidate order** (`web/candidates.py`, decisive sources first): registry `hjemmeside` → Wikidata
  website (`caches.wikidata`) → NAV-confirmed employer homepage (cache and/or this run's `nav`
  connector) → registry email domain (unless shared/freemail) → subunit websites/email domains → up to 4
  DNS-prefiltered name-guess slugs (skipped entirely at tier T0). Domains on `web/blocklist.py`'s
  `MARKETPLACE_BLOCKLIST` (directories, booking platforms, social platforms, generic site builders) are
  dropped before any HTTP request; franchise-chain domains (`FRANCHISE_CHAIN_DOMAINS`) remain candidates
  but are gated hard in `verify.assess`.
- **Crawl** (`web/crawl.py`): homepage first, then up to N secondary pages by tier (T0: 0, T1: 2, T2: 4,
  T3: 6), picked by priority terms (contact, about, privacy, terms, impressum, careers, news) from
  homepage links, falling back to `sitemap.xml` only if no priority links were found on the homepage.
  Parked/for-sale placeholders and JS-only shells are detected and recorded, never rendered.
- **Fallback order**: the connector tries candidates until the first `exact` verdict, or — if none verify
  exactly — publishes the best `related` candidate as an `ambiguous` claim (`confidence=0.4`, never
  treated as the company's own site by downstream connectors), or `not_available` with a reason stating
  how many candidates were tried.
- **Budget**: `client.remaining(org) <= 0` stops trying further candidates mid-loop; skipped candidates
  are recorded with reason `request_budget`, never silently dropped from the attempt log
  (`shared["web_attempts"]`).

## `activity` (`src/signalpost/activity/`, `ActivityConnector`)

Families: `jobs`, `activity`, `reviews`.

1. **NAV job ads** — no HTTP at all if the live `nav` connector already ran this company
   (`nav_jobs._collect_live` reads `ctx.shared`); otherwise falls back to the cached NAV feed index
   (`ctx.caches.nav.ads_for`, works at every tier including T0, no HTTP).
2. **ATS feed/page** (`ats.py`) — only if a site was verified `exact` this run, tier `!= T0`, and budget
   remains. Native parsers for Teamtailor, Lever, Greenhouse, SmartRecruiters; everything else recognized
   (Webcruiter, Jobylon, ReachMee, HR-manager, Recman, Easycruit, Jobbnorge, Workday) falls back to a
   conservative same-domain anchor-tag scrape of the careers page. Deduped against NAV by normalized
   title.
3. **Site RSS/Atom + dated news** (`feeds.py`) — same gate as ATS; only consumes feed/news URLs already
   discovered by `web` (`ctx.shared["feed_urls"]` / `["news_urls"]`) — never guesses `/feed` or `/rss`
   paths. Undated items are dropped, never given a fabricated date.
4. **YouTube channel RSS** (`youtube.py`) — same gate; only for channels already linked from the
   exact-verified site. Currently returns nothing in practice: YouTube's own `robots.txt` disallows
   `/feeds/videos.xml` for a generic user agent, and this connector respects robots.txt on every request
   (see `LIMITATIONS.md` / `SOURCES.md`).
5. **`reviews`** — always `not_available`, reason "no permitted keyless ratings/reviews source
   (Google/Trustpilot/Glassdoor require licensed API access)". No connector attempts this family.

**Budget**: every HTTP-using stage (ATS, feeds, YouTube) is gated on a verified site present, tier
`!= "T0"`, and `ctx.client.remaining(org) >= 1`. YouTube resolves at most 2 linked channels and reads at
most 5 videos each; feeds/news collect at most 10 dated items total.

## Global request budget (`src/signalpost/http.py`)

`Budget` (thread-safe) enforces a single hard cap (`--max-requests`, default 19 per input company) across the whole
batch. Every redirect hop and every retry (timeout, 429, or 5xx — one bounded retry) counts as a request.
`BudgetedHttpClient` resolves DNS and blocks non-public addresses before every connection
(`assert_public_url`, reused from the starter kit), disables automatic redirect-following so each hop can
be counted and SSRF-checked individually, and honours `robots.txt` per host (cached, itself charged as
one request) for every call made with `respect_robots=True` — the default for company sites and third-
party platforms; official/keyless registry and NAV-feedentry calls pass `respect_robots=False`.

## Fallback summary

| Situation | Behaviour |
|---|---|
| Registry live call 404/410 | `not_available`, reason states the HTTP status; identity falls back to the bulk CSV row. |
| No candidate website verifies `exact` | Best `related` candidate published `ambiguous`, or `not_available` with a "checked N candidates" reason. |
| Request budget exhausted mid-company | Remaining un-run families marked `failed` reason `request_budget`; already-collected claims are kept. |
| Wall-clock deadline hit mid-batch | Remaining un-run families for that company marked `failed` reason `deadline`; the company still gets exactly one envelope. |
| A connector raises | Caught per company; other connectors still run; the crash is recorded in `envelope.errors`, never drops the company. |
| No `--caches` supplied | Every cache-consuming lookup (Wikidata, shared-domain detection, NAV cache path, alias names) degrades to `None`/skip, not a crash. |
