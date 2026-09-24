# Signalpost agent

A keyless Norwegian company-research agent for the Builderr "Signalpost" competition. Given a batch of
Norwegian organisation numbers, it produces one evidence-backed JSON envelope per company: legal
identity, financials, leadership, workplaces, group structure, verified website, company-owned social
profiles, a site-derived description, job postings, dated public activity, and (honestly) an absent
reviews family — every `available` claim carries a source URL, retrieval time, content hash and the
exact supporting text or JSON path. Re-running against the same state directory detects real changes,
keeps prior evidence, and never invents a false change.

Built on the Builderr Signalpost starter kit.

## What it actually does

- Anchors identity in the Brønnøysund bulk registry (`data.brreg.no`), then calls the live entity,
  roles, subunits and financial-accounts APIs for the company's registry facts.
- Generates website candidates from registry data, Wikidata, NAV job ads and name guesses, crawls each
  one, and verifies it against the org number before publishing it as `exact` — precision over recall;
  see `IDENTITY_RESOLUTION.md`.
- Extracts a description, social profile links and contact details only from a site that passed
  verification.
- Reads NAV job ads (cache and/or live search), ATS feeds, site RSS/news, and YouTube channel RSS for
  channels linked from the verified site.
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

Optional, but recommended for a full run: pre-built offline **caches** (shared/service-provider email
and website domains, subunit trade-name aliases, Wikidata company profiles, and a NAV job-feed index).
These are built once, outside the timed 45-minute run, and reused across many batches:

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
  [--max-requests 1900] \
  [--deadline-seconds 2400] \
  [--workers 12]
```

`--organisations` accepts a `.jsonl` file (one organisation number, or `{"organisation_number": "..."}`,
per line), a `.json` file (a JSON list, or `{"organisation_numbers": [...]}`), or a plain `.txt` file
(one number per line). Every input must be a 9-digit Norwegian organisation number with no duplicates.

`--bulk` is optional (see "Data prerequisites" above for the default-path/auto-download behaviour).
`--caches` is optional; when omitted, the run still completes, with weaker recall on website/jobs
discovery. `--max-requests` defaults to 1,900 (leaving headroom under the competition's 2,000-request
cap), `--deadline-seconds` to 2,400 (40 minutes, leaving headroom under the 45-minute cap; the run still
emits one envelope per input past that point, with unfinished families marked `failed` reason
`deadline`), `--workers` to 12 (thread pool size).

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
company websites) is keyless and free. The only "spend" possible is bandwidth/compute on the runner
itself.

## Models / APIs / licences

| Component | What | Licence / terms |
|---|---|---|
| Language model | none — every claim and summary sentence is built deterministically from fetched evidence; no LLM is called anywhere in the pipeline | n/a |
| Brønnøysund Enhetsregisteret (bulk + live) | company identity, roles, subunits, group | NLOD 2.0 |
| Brønnøysund Regnskapsregisteret | annual accounts | NLOD 2.0 |
| NAV arbeidsplassen search + `pam-stilling-feed` | job postings | NLOD; keyless, public search token |
| Wikidata (SPARQL) | company websites/social profile cache | CC0 |
| Company websites | verified official site content | robots.txt respected per host; see `SOURCES.md` |
| YouTube `feeds/videos.xml` | channel activity, only for channels linked from a verified site | attempted, `respect_robots=True`; currently blocked by YouTube's own `robots.txt` — see `LIMITATIONS.md` |

Full source-by-source detail, robots handling and licence basis: see `SOURCES.md`.

## Request budgeting

Every outbound HTTP request — including every redirect hop and every retry — is charged against a single
global `Budget` (`src/signalpost/http.py`), hard-capped at `--max-requests` (default 1,900, under the
competition's 2,000 cap). Each company gets a soft per-company allowance from `planner.classify` (5/15/
30/45 requests for tiers T0–T3, based only on bulk-registry signals — staff count, NACE code, legal
form, presence of a registered site/domain), but may borrow from the shared pool up to the hard cap.
Every company's official-registry calls are reserved up front so optional sources (website discovery,
jobs, activity) can never starve them. Robots.txt is honoured per host for every non-official source
(company sites, YouTube); official/keyless APIs pass `respect_robots=False`. All requests go through an
SSRF guard (`assert_public_url`) that resolves DNS and rejects private/loopback/link-local/reserved
addresses before connecting.

## Expected cost per 100-company run

**$0.** No third-party API spend; `run-report.json`'s `third_party_cost_usd` is always `0.0`.
