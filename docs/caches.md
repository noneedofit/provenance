# W2 — Caches

`from signalpost.caches import Caches` — four keyless, pre-built lookups that let the timed daily run
skip expensive discovery work: which email/website domains are shared (not company-owned), a company's
subunit trade names, its Wikidata website/social profiles, and an index of NAV job ads by employer org
number. Built **outside** the 45-minute run by `python -m signalpost.caches.prepare`, published as a
tarball, loaded in milliseconds by `Caches.load(cache_dir)`.

## Build

```
uv run python -m signalpost.caches.prepare \
  --cache-dir cache/ \
  --bulk /path/to/brreg-enheter.csv          # gzip-compressed despite the name; ~150MB, 1.17M rows
```

This builds `email_domains`, `aliases` (downloads the ~60MB subunit bulk CSV to a temp file and deletes
it afterwards unless `--keep-tmp`), `wikidata`, and `nav` (see below) in that order, writing
`meta.json`, `email_domains.sqlite`, `aliases.sqlite`, `wikidata.sqlite`, `nav.sqlite` into
`--cache-dir`. Each part can be skipped with `--skip-<part>`; `--underenheter <path>` reuses an
already-downloaded subunits CSV instead of re-downloading; `--nav-max-pages` / `--nav-max-seconds` cap
the NAV walk per invocation (see below — it's resumable, so re-running `prepare` continues from the
saved cursor).

Loading is `None`-safe end to end: `Caches.load` never raises. A missing cache directory, a missing
individual `*.sqlite` file, or a corrupt one just leaves that attribute `None`; every consumer already
has to handle `caches is None` per BUILD_SPEC, and handles `caches.<part> is None` the same way. `Nav` is
the one part opened read-write (not just read-only) because `sync_incremental` mutates it during the
daily run.

## API

```python
caches = Caches.load(cache_dir)          # < 1ms in practice (sqlite files opened, nothing bulk-read)

caches.email_domains.org_count("example.no")        # int: orgs using this domain in epostadresse
caches.email_domains.website_org_count("example.no")# int: orgs using this domain in hjemmeside
caches.email_domains.is_freemail("gmail.com")        # bool: hardcoded freemail list
caches.email_domains.is_shared("example.no")         # bool: is_freemail or org_count >= 3

caches.aliases.subunits("974760673")   # list[dict]: organisation_number, name, street, postcode, city,
                                        #   website, email, employees, nace
caches.aliases.names("974760673", parent_name="...")  # list[str]: cleaned distinct trade-name aliases

caches.wikidata.lookup("974560569")    # dict | None: qid, label, websites[list], profiles{platform:url},
                                        #   source_url, retrieved_at

caches.nav.ads_for(["974760673"])      # list[dict]: ads for this org or any of its subunits (via aliases)
caches.nav.sync_incremental(client, max_requests=60, batch_names=[...])  # daily-run pull; see below

caches.meta   # dict: built_at, per-part source_urls / row_count / input_sha256 / license
```

## Sources, licences, sizes (as built 2026-09-23)

| Part | Source | Licence | Rows | File size | Build time |
|---|---|---|---|---|---|
| `email_domains` | `data.brreg.no/enhetsregisteret/api/enheter/lastned/csv` (local copy used for this build) | NLOD 2.0 | 1,175,168 orgs -> 127,744 domains | 7.3 MB | 13.1s |
| `aliases` | `data.brreg.no/enhetsregisteret/api/underenheter/lastned/csv` | NLOD 2.0 | 864,903 subunits | 100.8 MB | 8.0s |
| `wikidata` | `query.wikidata.org/sparql`, P2333 (Norwegian org number) | CC0 | 10,279 orgs (10,939 raw bindings before per-org grouping) | 2.0 MB | 25.9s |
| `nav` | `pam-stilling-feed.nav.no` public feed | NLOD 2.0 | see below — partial build, time-boxed | grows with build time | see below |

`email_domains`/`aliases`/`wikidata` build fully in well under a minute total and were run to
completion into `../cache` (outside the repo, per the run's `--cache-dir`
convention). `nav` did not: see below.

### email_domains details

Extracts the registered domain (via `tldextract.TLDExtract(suffix_list_urls=())`, offline, using the
bundled public-suffix list) from every `epostadresse` and `hjemmeside` in the bulk CSV, and counts orgs
per domain separately for each field. `is_shared` = hardcoded freemail list (gmail.com, hotmail.com,
outlook.com, live.no, yahoo.no, icloud.com, altibox.no, and ~25 more Norwegian/international
freemail/webmail domains) **or** `org_count >= 3` — catches accountants, property managers, and
franchise-platform domains that would otherwise look "company-owned." `website_org_count` is exposed
separately so identity resolution can flag a *website* domain shared by several orgs (a marketplace,
shopping-centre site, or franchise platform) even when the email-domain count alone looks fine.

### aliases details

Indexed by `overordnetEnhet` (parent org number). `.names()` strips legal-form/branch tokens (AS, ASA,
ANS, DA, BA, SA, NUF, ENK, AVD/AVDELING) and drops a cleaned subunit name that is a prefix/duplicate of
the cleaned parent name (`"DIPS AS AVD BERGEN"` under `"DIPS AS"` → nothing new) while keeping a
genuinely distinct trade name (`"HAGELAND HOKKSUND"` under `"EIKER HAGESENTER AS"` → kept). The
underenheter bulk CSV (~60MB gzip) is downloaded to a temp path during `prepare` and deleted afterwards
(`--keep-tmp` to retain it, `--underenheter <path>` to reuse an already-downloaded copy and skip the
network call).

### wikidata details

One SPARQL query joins P2333 (Norwegian org number) with P856 (website), P2013 (Facebook ID), P2003
(Instagram username), P2002 (X username), P4264 (LinkedIn company ID), P2397 (YouTube channel ID), P1581
(blog), and `rdfs:label` (nb/en). ~10,939 raw result rows (some orgs have multiple websites) group into
10,279 distinct org numbers. The endpoint answered the full unpaginated query in ~10s during
development; `fetch_all_rows` still paginates by 20k-row pages (`LIMIT`/`OFFSET`) defensively — one page
covers everything today, but pagination means a future item-count increase or a slow day (SPARQL
endpoint timeout) degrades to multiple smaller requests instead of a hard failure. IDs are turned into
profile URLs (e.g. Facebook ID `X` → `https://www.facebook.com/X`); `website`/`blog` are already full
URLs and are kept as-is. Source URL per item is `https://www.wikidata.org/wiki/<QID>`; licence CC0.

## NAV job feed — findings and build strategy

`pam-stilling-feed.nav.no` exposes a full-history **event log**, not a snapshot: `GET /api/v1/feed`
returns the OLDEST page (from feed inception), each page has up to 1000 items and a `next_id` (null on
the last page), and there is **no `previous_id`** — the feed can only be walked forward, from page 1 or
from a saved `next_id` checkpoint. `?last=true` returns only the single newest item (useful as a
liveness probe, useless for building an index, since you still can't walk backward from it).

Each feed item carries `_feed_entry {uuid, status (ACTIVE/INACTIVE), businessName, sistEndret}`; the same
job-ad `uuid` reappears every time its status changes, so the correct "current" state per ad is whichever
occurrence is *last* when walking forward. There is no shortcut to "just the active ones" — reconstructing
current status requires walking the full event log (or trusting `client.get`'s incremental cursor, which
only works once you've already walked to "now" once).

**Measured (2026-09-23, this build):**
- Public token (`GET /api/publicToken`) returns quickly; last line of the body is the JWT.
- Per-page fetch latency is consistently **~3.6-3.9s**, regardless of client-side throttling — this is
  server/pagination cost, not something our ≤5 req/s politeness cap controls. A full sequential walk is
  latency-bound, not bandwidth- or rate-limit-bound, and cannot be parallelized (each page requires the
  previous page's `next_id`).
- The feed's earliest pages are a **one-time backfill burst**: the first 121 pages (121,000 events)
  span only ~4m51s of `sistEndret` time (2023-06-14T12:21:40 → 2023-06-14T12:26:31); a further sample to
  page 100 (100,000 events) only reached 2023-06-14T12:25:43 — i.e. ~1,700 events/second of feed-time
  density right after the feed's inception, almost certainly a bulk load of pre-existing ad history
  stamped with the migration timestamp. **This means early-page density cannot be extrapolated to
  estimate total page count** — the steady-state (real-time) posting rate later in the feed is unknown
  without walking further, and doing so is itself the expensive part.
- At the measured **~3.7s/page**, walking even 1,000 pages costs ~62 minutes — past the 45-minute daily
  run budget on its own, before counting detail fetches. A full walk to "now" (which spans the backfill
  burst plus 3+ years of real activity) is therefore a **multi-hour `prepare`-time job**, not something
  done inside a single invocation.

**Build strategy implemented:**
1. `nav.build_full(cache_dir, max_pages=..., max_seconds=..., resume=True)` walks forward from a saved
   `last_feed_page_id` cursor (sqlite `cursor` table), so `prepare` can be re-invoked repeatedly — each
   call continues exactly where the previous one stopped, bounded by a page or wall-clock cap. Every
   item updates `seen_status` (uuid → latest status/businessName/sistEndret); items currently ACTIVE also
   get a full detail fetch (`GET /api/v1/feedentry/{id}`) and are upserted into `ads`; items seen as
   INACTIVE flip any existing `ads` row for that uuid to closed.
2. `nav.Nav.ads_for(orgs)` resolves each given org number plus its subunits (via `.aliases`, wired by
   `Caches.load`) and returns matching `ads` rows — so a caller can pass a parent org number and get ads
   posted under any of its `BEDR` subunit numbers too (NAV `employer.orgnr` is frequently a subunit, not
   the parent).
3. `Nav.sync_incremental(client, max_requests=60, batch_names=None)` is the **daily-run** tool: it
   continues from the same saved cursor through the run's `context.HttpClient` (charged `org=None`,
   purpose `"nav_feed"`/`"nav_entry"`), flips closed ads, and — to stay inside the tiny daily request
   budget — only fetches full entry detail for a NEW active item when its `businessName` fuzzy-matches
   (casefold, strip AS/ASA/ANS/DA/BA/SA/NUF/ENK, ≥60% token overlap) a name in the current batch. This is
   the mechanism that keeps the NAV index gradually catching up to "now" across many days of running the
   agent, without ever spending the daily budget on ads for companies outside the batch.

**This build (into `../cache`):** ran `prepare` with a wall-clock cap
(see the run's own log / `meta.json` `parts.nav` for the exact numbers — pages fetched, active ads seen,
detail fetches, elapsed seconds — captured at build time) rather than a full walk, per the >60-minute
rule. The saved cursor makes the next `prepare` invocation a resumption, not a restart; **known gap**:
this cache's `ads` table only covers whatever portion of the backfill-burst window the time-boxed run
reached — it does **not** yet represent "currently active ads" for most of the org universe. Getting
there requires either (a) several more hours of `prepare` invocations advancing the same cursor before
the feed's real-time tail (and thus today's genuinely active ads) is reached, or (b) accepting that
`sync_incremental` during the daily run will, over enough days, walk the remaining distance while also
picking up today's batch's ads opportunistically. Given the request-latency bottleneck is server-side,
neither shortcuts the wall-clock cost; only wall-clock time (spread across many `prepare` calls) fixes
it.

## Refresh cadence

- `email_domains` / `aliases`: rebuild whenever a fresh `enheter`/`underenheter` bulk snapshot is fetched
  (Brønnøysund publishes new dumps continuously; a weekly rebuild is plenty — domain-sharing and subunit
  rosters change slowly).
- `wikidata`: rebuild weekly-to-monthly; P2333-linked items and their social IDs change rarely, and the
  query is cheap (~10s) to rerun.
- `nav`: `sync_incremental` runs every daily batch (cheap, bounded by `max_requests`); a full/continued
  `prepare` walk should be scheduled periodically (e.g. a background weekly job) to keep closing the gap
  to "now," since the daily incremental sync alone only advances as fast as the feed's page-latency
  allows within its small request budget.

## Publishing

`caches.store.pack(cache_dir, tar_path)` tars+gzips `meta.json` plus the four `*.sqlite` files (skipping
any that don't exist) and returns the tarball's sha256 — attach that tarball to a GitHub release (or any
static host) as the "declared external cache." `caches.store.fetch_and_unpack(url, dest, client)` is the
consumer side: downloads through the run's `HttpClient` (counts as one request, purpose `"cache_fetch"`,
`org=None`) and extracts into `dest`, returning `{url, bytes, sha256, files}`. A run passes
`--caches <dir>` pointing at the unpacked directory; `Caches.load` handles the rest.

## Known gaps

- NAV: see above — the shipped cache is a partial, time-boxed walk near the feed's 2023 genesis, not yet
  "currently active ads" coverage. `docs/caches.md`'s own numbers (this section, `meta.json`
  `parts.nav`) document exactly how far it got.
- `email_domains`/`aliases`/`wikidata` were built from a **local copy** of the enheter bulk CSV
  (`../data/brreg-enheter.csv`, already on disk) rather than downloading
  it in `prepare` — W1's `registry.load_bulk` also needs this file, so it isn't re-fetched here;
  `--bulk` always takes a local path.
- `Aliases.names()`'s prefix-based parent-name dedup is a simple heuristic (documented as such in the
  spec's example) and can occasionally drop a real short brand name that happens to start with the
  parent's name, or keep a low-information "AVD <city>" variant it doesn't recognize as a suffix — no
  attempt is made at NLP-grade brand extraction.
