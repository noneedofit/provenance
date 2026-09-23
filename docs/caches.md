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
| `nav` | `pam-stilling-feed.nav.no` public feed | NLOD 2.0 | 540 feed pages walked, 1,373 ads detail-fetched (1 with full employer data) | 89.0 MB | ~88 min across 4 invocations |

`email_domains`/`aliases`/`wikidata` build fully in well under a minute total and were run to
completion into `../cache` (outside the repo, per the run's `--cache-dir`
convention). `nav` did not: see below — it's a partial, resumable walk, by design (a full walk is a
multi-hour job; see the NAV section's rate/projection numbers).

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

**Bug found and fixed during this build (2026-09-24):** the real `GET /api/v1/feedentry/{id}` response
nests almost the entire ad under `ad_content` — `{uuid, status, sistEndret, ad_content: {employer,
title, published, expires, ..., workLocations}}` — with only `uuid`/`status`/`sistEndret` at the top
level. This isn't documented anywhere (the endpoint has no public schema); it was only visible by
inspecting a live response body. The original `_entry_to_ad_row` read `employer`/`title`/etc. straight
off the top-level object (matching the test fixture, which was hand-written from a guess at the shape,
not a real response), so **every** detail-fetched ad — 532/532 in the first full run — was stored with
`employer_orgnr = NULL`, silently breaking `ads_for` for the entire cache. Fixed by reading from
`ad_content` (falling back to the top level if absent, for robustness against a future shape change).
Added `test_entry_to_ad_row_reads_wrapped_ad_content` / `..._falls_back_to_flat_shape...` in
`tests/signalpost/test_caches_nav.py` and rewrote `tests/fixtures/caches/nav_feedentry_aaa.json` to the
real wrapped shape so this can't regress silently again.

**A second, structural finding surfaced by chasing that bug down:** `ad_content` is only present while
the ad is *currently* active on NAV's live system — once an ad has since closed, `GET .../feedentry/{id}`
returns just `{uuid, status: "INACTIVE", sistEndret}` with no `ad_content` at all (confirmed directly
against the live API). So detail data is only recoverable at the moment an ad is first seen ACTIVE while
walking the feed — a same-day or later re-fetch of an already-closed ad cannot repair it retroactively.
Concretely: of 532 ads detail-fetched in the first full run (all stored NULL due to the parsing bug
above), a same-day repair pass with the fixed parser recovered only 1 — the other 531 had already closed
in the time between the original walk and the repair, so NAV's API no longer serves their content. A
follow-up walk with the fixed parser (309 more detail fetches, live at fetch time) recovered content for
0 additional ads for the same reason — nearly all of the ads encountered as ACTIVE in this feed region
had already closed again by the time their detail was fetched moments later, which says more about how
short-lived a lot of NAV job-feed churn is at this point in the feed than about the code. **Practical
consequence:** `nav.sqlite`'s `ads` table correctly tracks `status` for every uuid it has seen (via
`seen_status` and the INACTIVE-flip path, unaffected by either bug), but only reliably carries full
`employer`/`title`/`application_url` detail for ads that are *still* active by the time a `prepare` or
`sync_incremental` invocation reaches them — which in practice means recently-posted ads walked promptly,
not historical ones recovered after the fact.

**This build (into `../cache`), full history:**

| Step | Pages walked | Detail fetches | Wall time | Notes |
|---|---|---|---|---|
| Run 1 | 1 → 163 | 0 (all null: parsing bug) | 602.6s | resumable cursor established |
| Run 2 | 163 → 536 | 532 (all null: parsing bug) | 2,904.7s | |
| Parser fix + regression tests | — | — | — | `_entry_to_ad_row` now reads `ad_content` |
| Repair pass | 0 (re-fetch by uuid, no page walk) | 532 re-fetched | 1,152.2s | only 1/532 recovered — rest had closed since |
| Run 3 (fixed parser, live) | 536 → 540 | 309 (1 recovered, rest already closed) | 632.2s | |
| **Total** | **540 pages** | **1,373 detail fetches** | **~88 min across 4 invocations** | cursor at page 540, not caught up to "now" |

Measured combined rate across all page-walking invocations (excludes the pure re-fetch repair pass):
540 pages / (602.6 + 2904.7 + 632.2)s ≈ **0.130 pages/s** (≈7.7s/page average, heavier than the pure
~3.7s/page page-fetch-only rate because most pages in this window trigger many detail fetches). At that
combined rate, reaching even 10,000 pages would take **~21 hours** of `prepare` wall time — confirming
the >60-minute rule applies and this is a multi-session background job, not a single-invocation build.
The cursor (`last_feed_page_id` in `nav.sqlite`'s `cursor` table) is saved after every page, so the next
`prepare --skip-email-domains --skip-aliases --skip-wikidata --nav-max-seconds <N>` invocation resumes
from page 540 automatically.

**Verified end-to-end:** `caches.nav.ads_for(["919858400"])` (Atrium People AS) returns 1 ad —
`"Konsulentoppdrag - åpen database - Maritim og Offshore"`, status `ACTIVE`, with a full
`application_url`/`work_locations` — proving the whole pipeline (feed walk → detail fetch → `ads` table
→ `ads_for` → `Caches.load` wiring) works correctly with real production data now that the parsing bug
is fixed.

**Known gap:** this cache's `ads` table only covers pages 1-540 of the feed (not yet caught up to "now"),
and of the 1,373 ads detail-fetched so far, only 1 carries full employer/title data (the structural
"detail only available while live" limitation above) — the other 1,372 correctly have `status =
'INACTIVE'` (via `seen_status`) but `employer_orgnr = NULL` (their content is gone from NAV's API).
Closing this gap requires either (a) many more hours of `prepare` invocations advancing the cursor toward
the feed's real-time tail, where a much larger fraction of detail-fetched ads will still be active at
fetch time, or (b) relying on `sync_incremental` during the daily run, which — once the cursor is near
"now" — detail-fetches items within moments of them going active, avoiding the "already closed by the
time we looked" problem entirely.

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

- NAV: see above — the shipped cache is a partial, resumable walk (pages 1-540 of an unknown-length feed,
  cursor saved), not yet "currently active ads" coverage; only 1 of 1,373 detail-fetched ads carries full
  employer data today, because `ad_content` is only served by NAV's API while an ad is *currently* live,
  and most ads seen as ACTIVE in this feed region had already closed again by the time of the detail
  fetch. `docs/caches.md`'s NAV section documents the full build history, the measured combined rate
  (~0.13 pages/s), and the projected time to walk further.
- `email_domains`/`aliases`/`wikidata` were built from a **local copy** of the enheter bulk CSV
  (`../data/brreg-enheter.csv`, already on disk) rather than downloading
  it in `prepare` — W1's `registry.load_bulk` also needs this file, so it isn't re-fetched here;
  `--bulk` always takes a local path.
- `Aliases.names()`'s prefix-based parent-name dedup is a simple heuristic (documented as such in the
  spec's example) and can occasionally drop a real short brand name that happens to start with the
  parent's name, or keep a low-information "AVD <city>" variant it doesn't recognize as a suffix — no
  attempt is made at NLP-grade brand extraction.
