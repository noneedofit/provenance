# Sources: URLs, licences, robots handling, usage

Every source this agent contacts, keyless throughout. No secrets are required to run it: `README.md`'s
evaluator command needs no environment variables or API keys.

## Secrets

**None required.** No `.env`, no API key, no OAuth token. The one bearer token used
(`nav_live.py`'s NAV `pam-stilling-feed` token) is itself a **public** token fetched at runtime from
NAV's own public `GET /api/publicToken` endpoint — not a credential issued to this project, not stored
anywhere, refreshed automatically if it expires mid-run.

## SSRF guard

Every outbound request, to every source below, passes through `assert_public_url` before connecting
(`src/signalpost/http.py`, reused from the starter kit's `norway_company_agent.website`): resolves DNS
and rejects the request if scheme isn't `http`/`https`, the host is `localhost`/`*.local`, or any
resolved address is not a global public IP (private, loopback, link-local, multicast, reserved). This
runs on every hop of a redirect chain, not just the initial URL, so a redirect cannot be used to reach an
internal address.

## Redirect and retry counting

Every source's requests are made through the single `BudgetedHttpClient`, which disables automatic
redirect-following so each hop is individually SSRF-checked and charged against the request budget; a
retryable failure (timeout, 429, 5xx) gets exactly one bounded retry, also charged. See `README.md`'s
"Request budgeting" section and `CRAWLERS.md` for the full accounting.

---

## Official registry sources

### Brønnøysund Enhetsregisteret (bulk + live)

- **Bulk**: `GET https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv` — gzip CSV, ~1.17M
  rows, downloaded once outside the timed run (or auto-downloaded by `signalpost run` if no local copy
  is found, counted as one request).
- **Live**: `GET .../enheter/{org}`, `.../enheter/{org}/roller`, `.../underenheter?overordnetEnhet={org}`.
- **Licence**: NLOD 2.0 (Norwegian Licence for Open Government Data) — free reuse with attribution.
- **Robots**: `respect_robots=False` — an official, keyless, public government API, not a scraping
  target.
- **Used for**: `identity`, `leadership`, `locations`, `group` families, and `description` (the registered
  activity `aktivitet`, else the statutory purpose, as `registry_activity`) (`src/signalpost/registry.py`).

### Brønnøysund Regnskapsregisteret

- `GET https://data.brreg.no/regnskapsregisteret/regnskap/{org}` (latest filed accounts),
  `GET .../regnskap/aarsregnskap/kopi/{org}/aar` (list of filed years, every company; 1 request/s).
- **Licence**: NLOD 2.0.
- **Robots**: `respect_robots=False`.
- **Used for**: `financials`, `financial_history` families, and `group` (the accounts' parent-company
  flag `virksomhet.morselskap` → `group_role: parent_company`).

---

## NAV (Norwegian Labour and Welfare Administration)

### `arbeidsplassen.nav.no` job search

- `GET https://arbeidsplassen.nav.no/stillinger/api/search?q=<name>&size=100`.
- **Licence basis**: NLOD-equivalent public government data (NAV job ads are public postings).
- **Robots**: keyless; robots.txt allows this endpoint. `respect_robots=True` in practice, but never
  observed to be disallowed here.
- **Access**: no key required; subject to CDN/WAF-level rate limiting (see `LIMITATIONS.md`) — not an
  access-control restriction, an anti-burst throttle.
- **Used for**: candidate NAV job ads (name-matched, then confirmed by org number via the feed entry
  endpoint below) — `nav_live.py`, tiers T1–T3.

### `pam-stilling-feed.nav.no` public feed

- `GET .../api/v1/feedentry/{uuid}` (Authorization: Bearer `<public token>`),
  `GET .../api/publicToken` (the token itself, refetched once per run and on a 401),
  `GET .../api/v1/feed` (the full event-log walk used to build the offline `caches/nav.py` index).
- **Licence**: NLOD 2.0.
- **Robots**: `respect_robots=False` for the token/feedentry calls (official public feed, keyless,
  documented as intended for public subscription/indexing use), gated by a `Bearer` token that is itself
  publicly served — this is an access mechanism, not a paywall or restriction.
- **Used for**: confirming a job ad's organisation number before publishing it as this company's job
  (`jobs/job_posting` claims); building the offline NAV cache consumed by `nav_jobs.py`'s cache path.

---

## Wikidata

- `POST https://query.wikidata.org/sparql` — one query joining P2333 (Norwegian org number) with P856
  (website), P2013 (Facebook), P2003 (Instagram), P2002 (X), P4264 (LinkedIn), P2397 (YouTube channel),
  P1581 (blog).
- **Licence**: CC0 (public domain dedication) — Wikidata's standard licence for all its data.
- **Robots**: `respect_robots=False` (a documented, public query API, not a scraped website).
- **Used for**: building the offline `caches/wikidata.py` cache — decisive website candidates, and social
  profiles published directly (the item carries our org number, so attribution is exact;
  `identity_basis="wikidata_org_number"`). Built at run start when absent; a bundled CC0 snapshot is the
  fallback.

---

## Company websites

- Whatever domain a candidate resolves to (registry `hjemmeside`, Wikidata website, NAV employer
  homepage, subunit site, or a name-guessed slug) — see `IDENTITY_RESOLUTION.md` for how a candidate is
  chosen and verified.
- **Licence / terms basis**: each site's own terms apply; this agent does not claim any special right
  beyond what `robots.txt` permits. No login, no paywall bypass, no CAPTCHA solving.
- **Robots**: `respect_robots=True` on every request (`web/crawl.py`, `activity/ats.py`,
  `activity/feeds.py`) — robots.txt is fetched and cached per host (itself charged as one request the
  first time a host is seen), and a disallowed path is never fetched.
- **User-Agent**: `SignalpostResearchAgent/0.1 (+https://github.com/noneedofit/provenance)`
  (`src/signalpost/http.py:USER_AGENT`) — identifies the agent and points back to this repository, sent
  on every request to every source, not just company sites.
- **Used for**: `website`, `profiles`, `description` families (only from a candidate that passed the
  `exact` verification gate); `jobs` (ATS feed/careers page on the verified site); `activity` (RSS/Atom,
  dated news pages already linked from the verified site).

---

## YouTube RSS (`feeds/videos.xml`)

- `GET https://www.youtube.com/feeds/videos.xml?channel_id=<UC…>` — YouTube's own documented public
  channel-subscription feed, keyless, no API key.
- **Licence / terms basis**: public RSS feed, but **YouTube's own `robots.txt` disallows
  `/feeds/videos.xml` for a generic user agent** (verified live).
- **Robots**: `respect_robots=True`, matching every other third-party source — this connector never
  carves out an exception for itself. In practice this means the request is almost always blocked and no
  video claims are published. See `LIMITATIONS.md`.
- **Used for**: `activity/activity_item` claims (`platform: "YouTube"`), only for channels already linked
  from an exact-verified company site — no scraping of video pages, channel search, or any other YouTube
  surface.

---

## Restricted platforms — not scraped

**LinkedIn, Meta (Facebook/Instagram), Glassdoor, Indeed, and Google** are **never** fetched or scraped
by this agent. Where a link to one of these appears on a verified company's own website (e.g. a
"Follow us on LinkedIn" link in the footer), `web/extract.py`'s social-link extraction records that
**URL only** (reusing the starter kit's `norway_company_agent.website.normalize_social_url`) as a
`profiles/profile` claim (`source_class="company_owned_platform"`, `identity_basis=
"linked_from_verified_site"`) — the link itself, never the platform's content. No page on any of these
domains is ever requested. `web/blocklist.py`'s `MARKETPLACE_BLOCKLIST` additionally ensures none of
these domains can ever be *generated* as a website candidate in the first place (a registry/subunit email
address on `facebook.com` or `linkedin.com`, for instance, is never turned into a candidate).

`reviews` (Google, Trustpilot, Glassdoor, etc.) is always `not_available` — see `LIMITATIONS.md` — no
licensed provider is used, consistent with the $0 / no-secrets constraint.

---

## Summary table

| Source | URL pattern | Licence / terms | Robots | Used for |
|---|---|---|---|---|
| Brreg Enhetsregisteret | `data.brreg.no/enhetsregisteret/api/...` | NLOD 2.0 | `respect_robots=False` (official API) | identity, leadership, locations, group |
| Brreg Regnskapsregisteret | `data.brreg.no/regnskapsregisteret/...` | NLOD 2.0 | `respect_robots=False` | financials, financial_history |
| NAV arbeidsplassen search | `arbeidsplassen.nav.no/stillinger/api/search` | NLOD-equivalent public data | allowed, keyless | jobs candidate search |
| NAV pam-stilling-feed | `pam-stilling-feed.nav.no/api/...` | NLOD 2.0 | `respect_robots=False` (official feed) | jobs confirmation, offline NAV cache |
| Wikidata SPARQL | `query.wikidata.org/sparql` | CC0 | `respect_robots=False` (query API) | website/profile candidate cache |
| Company websites | verified candidate domain | site's own terms | `respect_robots=True` | website, profiles, description, jobs (ATS), activity (feeds/news) |
| YouTube RSS | `youtube.com/feeds/videos.xml` | public feed, but robots-disallowed | `respect_robots=True` → blocked in practice | activity (video items) |
| LinkedIn / Meta / Glassdoor / Indeed / Google | — | not accessed | n/a | never scraped; only linked URLs recorded from a verified site |
