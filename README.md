# Provenance

Every fact about a Norwegian company, with its source.


A keyless Norwegian company-research agent for the Builderr "Signalpost" competition. Given a batch of
Norwegian organisation numbers, it produces one evidence-backed JSON envelope per company: legal
identity, financials, leadership, workplaces, group structure, verified website, company-owned social
profiles, a site-derived description, job postings, dated public activity, and official food-hygiene
inspection ratings (Mattilsynet) where they exist — every `available` claim carries a source URL, retrieval time, content hash and the
exact supporting text or JSON path. Re-running against the same state directory detects real changes,
keeps prior evidence, and never invents a false change.

Built on the Builderr Signalpost starter kit (its SSRF guard and social-URL normaliser live on in
`src/signalpost/urls.py`; the kit's prototype scripts are not part of this agent).

## What it actually does

- Anchors identity in the Brønnøysund bulk registry (`data.brreg.no`), then calls the live entity,
  roles, subunits and financial-accounts APIs for the company's registry facts.
- Generates website candidates from registry data, Wikidata, NAV job ads, two bundled open places
  datasets (Overture Maps places matched on the registry phone/e-mail or name and postcode, and
  OpenStreetMap features tagged with the org number) and name guesses, crawls each one, and verifies it
  live before publishing it as `exact` — precision over recall; see `IDENTITY_RESOLUTION.md`.
- Extracts a description, social profile links and contact details only from a site that passed
  verification.
- Reads NAV job ads (a bundled snapshot of active ads with each employer's org number, plus a live walk of
  `pam-stilling-feed` since that snapshot, kept incrementally in `--state-dir`; every posting is confirmed
  live against the org number), ATS feeds, site RSS/news, and YouTube channel RSS for channels linked from
  the verified site.
- Reads Mattilsynet's public food-hygiene inspection results (smilefjes) for the company's food-service
  locations, published only when the inspection page shows the company's or a subunit's org number.
- Diffs every run against the previous stored profile for the same organisation, emitting `Change`
  events only for genuine differences and carrying forward claims whose source could not be re-checked
  this run.
- Builds deterministic English summary sentences from claims only (no LLM), and a static, offline-capable
  HTML viewer.
- Enforces a global outbound request budget (every redirect hop and retry counts), a wall-clock deadline,
  and an SSRF guard on every fetched URL.

## Quickstart

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
```

### Data prerequisites

The pipeline needs the Brønnøysund bulk registry CSV (gzip-compressed despite the `.csv` name). Download
it once:

```bash
mkdir -p data
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o data/brreg-enheter.csv
```

`signalpost run` looks for `./data/brreg-enheter.csv.gz` or `./data/brreg-enheter.csv` by default (or
pass `--bulk <path>` explicitly); if neither is found it downloads the file itself (counted as one
request). No API key is required — the registry API is keyless and public.

**Caches are built automatically.** When `--caches` is omitted, `signalpost run` uses `./cache` and, if
it is empty, builds the two identity-critical parts itself before the batch starts: the shared-domain
table from the bulk file (0 requests, a few seconds) and the Wikidata table (1 SPARQL request, with a
bundled CC0 snapshot as fallback). A clean clone therefore needs only `uv sync` and the run command below;
the first run also downloads the bulk file, indexes the bundled open-places and NAV snapshots, and reads
the NAV job feed published since the NAV snapshot (a few requests per day of snapshot age, reused from
`--state-dir` by later runs). Setup requests are reported separately in `run-report.json`
(`setup_requests`, `total_requests_including_setup`).

Optional: the fuller pre-built offline **caches** (shared/service-provider email
and website domains, subunit trade-name aliases, Wikidata company profiles, and a NAV job-feed index).
These are built once, outside the timed run, and reused across many batches:

```bash
uv run python -m signalpost prepare \
  --cache-dir cache/ \
  --bulk data/brreg-enheter.csv
```

(Equivalently: `uv run python -m signalpost.caches.prepare --cache-dir cache/ --bulk data/brreg-enheter.csv`
— the `signalpost prepare` subcommand simply delegates to this module.) The NAV part of `prepare` is a
slow, resumable walk of a full event-log feed (measured ~3.7s/page; see `docs/caches.md`) — it can be
capped with `--nav-max-seconds <N>` and re-invoked later to continue from its saved cursor, or skipped
entirely with `--skip-nav` for a first run. `--skip-email-domains` / `--skip-aliases` / `--skip-wikidata`
skip the other parts individually. Caches are optional end to end: every consumer in the pipeline handles
`--caches` being omitted.

### The evaluator command

```bash
uv run python -m signalpost run \
  --organisations <path/to/organisation-numbers.jsonl|.json|.txt> \
  --output-dir out/ \
  --state-dir state/ \
  --run-id <run-id> \
  [--bulk data/brreg-enheter.csv] \
  [--caches cache/] \
  [--max-requests <N>] \
  [--deadline-seconds 2400] \
  [--workers 24]
```

`--organisations` accepts a `.jsonl` file (one organisation number, or `{"organisation_number": "..."}`,
per line), a `.json` file (a JSON list, or `{"organisation_numbers": [...]}`), or a plain `.txt` file
(one number per line). Every input must be a 9-digit Norwegian organisation number with no duplicates.

`--bulk` is optional (see "Data prerequisites" above for the default-path/auto-download behaviour).
`--caches` is optional; when omitted, `./cache` is used and built automatically (see above).

Run budget (the official batch is 1,000 companies in one run, and may grow to 1,100; the scored reference
set is 1,500):
- `--max-requests` (env `SIGNALPOST_MAX_REQUESTS`) — total outbound request cap; default 26 per input
  company (2,600 per 100, 39,000 per 1,500). Measured use: about 10 per company.
- `--deadline-seconds` (env `SIGNALPOST_DEADLINE_SECONDS`) — default 2,400 (40 minutes). When the time
  left is short for the companies not yet started, each remaining company gets the official-registry pass
  only (identity, leadership, locations, financials), with skipped families reported `failed` reason
  `time_budget` — never an empty result.
- `--workers` (env `SIGNALPOST_WORKERS`) — thread pool size, default 24.
- Deterministic by default: identical input gives identical factual output. Two timing-dependent
  sources are opt-in: `SIGNALPOST_WIKIDATA_LIVE=1` (query Wikidata live instead of the bundled CC0
  snapshot) and `SIGNALPOST_NAV_SEARCH_MAX=<n>` (rate-limited NAV search fallback, off by default).
  The open places and NAV active-ads snapshots are pinned files in `src/signalpost/caches/snapshot/`
  (rebuilt with `scripts/build_places_snapshot.py` and `scripts/build_nav_snapshot.py`);
  `SIGNALPOST_NAV_SNAPSHOT=0` walks the full NAV window live instead. `SIGNALPOST_WEB_DEBUG=<file>`
  appends every website candidate verdict to a file for diagnostics (not part of the output).

Measured: 1,500 random companies in one run with the defaults took 26 minutes and 14,504 requests
(peak memory 660 MB), with no deadline or budget hits.

Output, written to `--output-dir`:
- `envelopes.jsonl` — exactly one JSON `Envelope` per input organisation number, in input order.
- `run-report.json` — request counts by purpose, runtime p50/p95, family-state counts, deadline/budget
  hits, validation result, `third_party_cost_usd: 0.0`.
- `requests.jsonl` — every outbound request attempted (purpose, org, URL, status, requests used, error).

State, written to `--state-dir` (reused across runs — see "Refresh" below):
- `profiles/{organisation_number}.json` — the last envelope plus claim history, for refresh diffing.
- `snapshots/` — gzip, content-addressed raw response bodies referenced by evidence `snapshot_ref`.

### Validate

```bash
uv run python -m signalpost validate --envelopes out/envelopes.jsonl [--organisations <path>]
```

Checks the envelope-invariants contract: exactly one envelope per organisation number (when
`--organisations` is given), every family present, every `available` claim has a value and evidence, no
dangling evidence references. Exits non-zero on failure.

### Build the viewer

```bash
uv run python -m signalpost.viewer.build --envelopes out/envelopes.jsonl --out out/site
```

Produces a fully static, offline-capable site (`out/site/index.html`) — a directory with search/filter/
sort/CSV export, a pre-rendered profile page per company with an evidence drawer on every fact, and a
2–4-company compare view. No CDN, no external scripts or fonts; works from a plain `file://` URL or any
static host. See `docs/viewer.md`.

### Refresh

Refresh is not a separate command: rerun `signalpost run` with the **same `--state-dir`** (a new
`--run-id`, and optionally a changed `--organisations` list). Each company's new envelope is merged
against its stored profile; `envelope.changes` lists what genuinely differs since the previous run,
prior evidence for superseded claims is retained in `--state-dir`, and a source that could not be
re-checked this run carries its previous claim forward unchanged rather than dropping it or inventing a
change. See `REFRESH.md`.

## Tests

```bash
uv run --with pytest pytest -q tests/signalpost
```

## Cost

**$0 per run.** No LLM, no paid API, no required secrets. Every source used (Brønnøysund, NAV, Wikidata,
Overture Maps and OpenStreetMap snapshots, Mattilsynet, company websites) is keyless and free. The only "spend" possible is bandwidth/compute on the runner
itself.

## Models / APIs / licences

| Component | What | Licence / terms |
|---|---|---|
| Language model | none — every claim and summary sentence is built deterministically from fetched evidence; no LLM is called anywhere in the pipeline | n/a |
| Brønnøysund Enhetsregisteret (bulk + live) | company identity, roles, subunits, group | NLOD 2.0 |
| Brønnøysund Regnskapsregisteret | annual accounts | NLOD 2.0 |
| NAV arbeidsplassen search + `pam-stilling-feed` | job postings | NLOD; keyless, public search token |
| Wikidata (SPARQL) | company websites/social profile cache | CC0 |
| Overture Maps Places (bundled snapshot) | website candidates | CDLA-Permissive-2.0 / Apache-2.0 / CC0 |
| OpenStreetMap `ref:NO:orgnr` (bundled snapshot) | website candidates | ODbL 1.0, © OpenStreetMap contributors |
| Mattilsynet smilefjes | food-hygiene inspection ratings | public authority data |
| Company websites | verified official site content | robots.txt respected per host; see `SOURCES.md` |
| YouTube `feeds/videos.xml` | channel activity, only for channels linked from a verified site | attempted, `respect_robots=True`; currently blocked by YouTube's own `robots.txt` — see `LIMITATIONS.md` |

Full source-by-source detail, robots handling and licence basis: see `SOURCES.md`.

## Request budgeting

Every outbound HTTP request — including every redirect hop and every retry — is charged against a single
global `Budget` (`src/signalpost/http.py`), hard-capped at `--max-requests` (default 26 per input company, overridable
by the evaluator). Each company gets a soft per-company allowance from `planner.classify` (14/24/
36/55 requests for tiers T0–T3, based only on bulk-registry signals — staff count, NACE code, legal
form, presence of a registered site/domain), but may borrow from the shared pool up to the hard cap.
Every company's official-registry calls are reserved up front so optional sources (website discovery,
jobs, activity) can never starve them. Robots.txt is honoured per host for every non-official source
(company sites, YouTube); official/keyless APIs pass `respect_robots=False`. All requests go through an
SSRF guard (`assert_public_url`) that resolves DNS and rejects private/loopback/link-local/reserved
addresses before connecting.

## Expected cost per run

**$0** for a 1,000-company batch or any other size. No third-party API spend; `run-report.json`'s `third_party_cost_usd` is always `0.0`.
