# Sources: URLs, licences, robots handling, usage

Every source this agent contacts, keyless throughout. No secrets are required to run it: `README.md`'s
evaluator command needs no environment variables or API keys.

## Secrets

**None required.** No `.env`, no API key, no OAuth token. The one bearer token used
(`nav_live.py`'s NAV `pam-stilling-feed` token) is itself a **public** token fetched at runtime from
NAV's own public `GET /api/publicToken` endpoint — not a credential issued to this project, not stored
anywhere, refreshed automatically if it expires mid-run.

## SSRF guard

Every request made during the batch, to every source below, passes through `assert_public_url` before connecting
(`src/signalpost/http.py`, guard in `src/signalpost/urls.py`, adapted from the starter kit): resolves DNS
and rejects the request if scheme isn't `http`/`https`, the host is `localhost`/`*.local`, or any
resolved address is not a global public IP (private, loopback, link-local, multicast, reserved). This
runs on every hop of a redirect chain, not just the initial URL, so a redirect cannot be used to reach an
internal address. The one-time setup downloads (the registry bulk file, and Wikidata only when queried
live) go to fixed official URLs outside this client and are counted as `setup_requests`.

## Redirect and retry counting

Every source's requests are made through the single `BudgetedHttpClient`, which disables automatic
redirect-following so each hop is individually SSRF-checked and charged against the request budget; a
retryable failure (timeout, connection error, 429, 5xx) gets one bounded retry, also charged; an
official API's 5xx gets one more, later attempt, and a company homepage that fails TLS gets one
plain-http attempt. Calls to the registry API (`data.brreg.no`) reuse one open connection per worker
thread (HTTP keep-alive) unless a proxy is configured; a connection the server has already closed is
replaced once, which is the same request, not a retry. See `README.md`'s
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
  `GET .../regnskap/aarsregnskap/kopi/{org}/aar` (list of filed years; 1 request/s; a T0 company whose
  bulk row states its latest filed year uses that instead).
- **Licence**: NLOD 2.0.
- **Robots**: `respect_robots=False`.
- **Used for**: `financials`, `financial_history` families, and `group` (the accounts' parent-company
  flag `virksomhet.morselskap` → `group_role: parent_company`).

---

## NAV (Norwegian Labour and Welfare Administration)

### `arbeidsplassen.nav.no` job search (off by default)

- `GET https://arbeidsplassen.nav.no/stillinger/api/search?q=<name>&size=100`.
- **Licence basis**: NLOD-equivalent public government data (NAV job ads are public postings).
- **Robots**: keyless; robots.txt allows this endpoint. `respect_robots=True` in practice, but never
  observed to be disallowed here.
- **Access**: no key required; subject to CDN/WAF-level rate limiting (see `LIMITATIONS.md`) — not an
  access-control restriction, an anti-burst throttle.
- **Used for**: nothing in a default run. With `SIGNALPOST_NAV_SEARCH_MAX=<n>` set, up to n staffed
  companies with no feed match are searched (results confirmed by org number via the feed entry endpoint
  below). Off by default because its results vary between runs.

### `pam-stilling-feed.nav.no` public feed

- `GET .../api/v1/feedentry/{uuid}` (Authorization: Bearer `<public token>`),
  `GET .../api/publicToken` (the token itself, refetched once per run and on a 401),
  `GET .../api/v1/feed` (the event-log walk that keeps the run's active-ads index current; a bundled
  snapshot of active ads seeds a fresh state-dir).
- **Licence**: NLOD 2.0.
- **Robots**: `respect_robots=True` (robots.txt is checked; the feed is an official public feed, keyless,
  documented as intended for public subscription/indexing use), gated by a `Bearer` token that is itself
  publicly served — this is an access mechanism, not a paywall or restriction.
- **Used for**: finding active ads whose employer org number is this company or one of its subunits, and
  confirming each on its own feed entry before publishing it as this company's job (`jobs` claims).

---

## Wikidata

- `GET https://query.wikidata.org/sparql?query=…` — one query joining P2333 (Norwegian org number) with P856
  (website), P2013 (Facebook), P2003 (Instagram), P2002 (X), P4264 (LinkedIn), P2397 (YouTube channel),
  P1581 (blog).
- **Licence**: CC0 (public domain dedication) — Wikidata's standard licence for all its data.
- **Robots**: `respect_robots=False` (a documented, public query API, not a scraped website).
- **Used for**: building the offline `caches/wikidata.py` cache — decisive website candidates, and social
  profiles published directly (the item carries our org number, so attribution is exact;
  `identity_basis="wikidata_org_number"`). A run copies the bundled CC0 snapshot (0 requests, identical
  between runs); `SIGNALPOST_WIKIDATA_LIVE=1` queries the live endpoint instead.

---

## Open places datasets (bundled, pinned snapshots)

- **Overture Maps Places**, release `2026-09-23.1`
  (`s3://overturemaps-us-west-2/release/2026-09-23.1/theme=places/type=place/`): the Norwegian places
  that carry a website or social link (228,308 places: name, postcode, phones, e-mails, websites,
  socials). Built by `scripts/build_places_snapshot.py` with DuckDB, shipped as
  `src/signalpost/caches/snapshot/places_no.jsonl.gz`.
- **OpenStreetMap** features tagged `ref:NO:orgnr` (11,177 features, Overpass API, OSM base
  2026-06-01), shipped as `src/signalpost/caches/snapshot/osm_orgnr_no.jsonl.gz`.
- **Licence**: Overture places are CDLA-Permissive-2.0 / Apache-2.0 / CC0 (per source; no attribution
  required). OpenStreetMap data is ODbL 1.0: © OpenStreetMap contributors.
- **Requests**: none at run time. The snapshots are indexed into `cache/places.sqlite` at setup (a few
  seconds) and pinned, so two runs of the same input read the same candidates.
- **Used for**: website *candidates* only. A place that carries the company's registry phone or e-mail
  (or its name core at its postcode) nominates the place's website; an OSM feature tagged with the
  company's (or a subunit's) org number nominates its website. Every candidate is still fetched live and
  must pass the identity gate (`IDENTITY_RESOLUTION.md`); the dataset is never claim evidence on its own.

---

## Mattilsynet food-hygiene inspections (smilefjes)

- `GET https://smilefjes.mattilsynet.no/search/index/nb.json` (the site's own place index, one request per
  run) and `GET https://smilefjes.mattilsynet.no/spisested/...` (one page per candidate place, at most 3
  per company).
- **Licence / terms basis**: public results published by the Norwegian Food Safety Authority; the site
  has no robots.txt (404), and every request still goes through the robots check.
- **Used for**: `reviews/inspection_rating`. A place is a candidate when its postcode equals the
  company's or a subunit's and its name shares a distinctive name word; it is published only when the
  page's own "Orgnr." is the company's or a subunit's organisation number. The quoted evidence is the page
  text (place name, org number, latest inspection date and the smiley result).

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
  on every request to every source (setup downloads included), not just company sites.
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
  from an exact-verified company site. A linked channel given by handle rather than id needs one fetch of
  that channel page (robots-checked) to read its channel id; no video pages, channel search, or any other
  YouTube surface.

---

## Restricted platforms — not scraped

**LinkedIn, Meta (Facebook/Instagram), Glassdoor, Indeed, and Google** are **never** fetched or scraped
by this agent. Where a link to one of these appears on a verified company's own website (e.g. a
"Follow us on LinkedIn" link in the footer), `web/extract.py`'s social-link extraction records that
**URL only** (`signalpost.urls.normalize_social_url`, adapted from the starter kit) as a
`profiles/profile` claim (`source_class="company_owned_platform"`, `identity_basis=
"linked_from_verified_site"`) — the link itself, never the platform's content. No page on any of these
domains is ever requested. `web/blocklist.py`'s `MARKETPLACE_BLOCKLIST` additionally ensures none of
these domains can ever be *generated* as a website candidate in the first place (a registry/subunit email
address on `facebook.com` or `linkedin.com`, for instance, is never turned into a candidate).

Consumer review platforms (Google, Trustpilot, Glassdoor, etc.) are not used — they need licensed API
access, and the agent runs with $0 and no secrets. `reviews` carries only Mattilsynet's public
food-hygiene inspection results (see above).

---

## Summary table

| Source | URL pattern | Licence / terms | Robots | Used for |
|---|---|---|---|---|
| Brreg Enhetsregisteret | `data.brreg.no/enhetsregisteret/api/...` | NLOD 2.0 | `respect_robots=False` (official API) | identity, leadership, locations, group |
| Brreg Regnskapsregisteret | `data.brreg.no/regnskapsregisteret/...` | NLOD 2.0 | `respect_robots=False` | financials, financial_history |
| NAV pam-stilling-feed | `pam-stilling-feed.nav.no/api/...` | NLOD 2.0 | `respect_robots=True` (official feed) | jobs (active-ads index + feed-entry confirmation) |
| NAV arbeidsplassen search (off by default) | `arbeidsplassen.nav.no/stillinger/api/search` | NLOD-equivalent public data | `respect_robots=True`, keyless | opt-in jobs fallback |
| Wikidata (bundled snapshot; SPARQL opt-in) | `query.wikidata.org/sparql` | CC0 | n/a by default; query API when live | website/profile candidate cache |
| Company websites | verified candidate domain | site's own terms | `respect_robots=True` | website, profiles, description, jobs (ATS), activity (feeds/news) |
| Overture Maps Places (snapshot) | bundled `places_no.jsonl.gz` (release 2026-09-23.1) | CDLA-Permissive-2.0 / Apache-2.0 / CC0 | n/a (no run-time requests) | website candidates |
| OpenStreetMap `ref:NO:orgnr` (snapshot) | bundled `osm_orgnr_no.jsonl.gz` | ODbL 1.0, © OpenStreetMap contributors | n/a (no run-time requests) | website candidates (org-number keyed) |
| Mattilsynet smilefjes | `smilefjes.mattilsynet.no/search/index/nb.json`, `/spisested/...` | public authority data | `respect_robots=True` (no robots.txt) | reviews (official inspection rating) |
| YouTube RSS | `youtube.com/feeds/videos.xml` | public feed, but robots-disallowed | `respect_robots=True` → blocked in practice | activity (video items) |
| LinkedIn / Meta / Glassdoor / Indeed / Google | — | not accessed | n/a | never scraped; only linked URLs recorded from a verified site |
