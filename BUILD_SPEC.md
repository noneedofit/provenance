# Signalpost agent — build spec (orchestrator-owned)

> **Historical document.** This is the specification the agent was first built from (September 2026).
> Budgets, tiers and sources have changed since; the current behaviour is documented in `README.md`,
> `SOURCES.md`, `IDENTITY_RESOLUTION.md`, `DATA_SCHEMA.md` and `LIMITATIONS.md`.

This file was the what; the research notes behind it are summarised in `SOURCES.md` and `LIMITATIONS.md`.
Contract files owned by the orchestrator (do NOT change without asking): `src/signalpost/models.py`,
`src/signalpost/context.py`, this file.

## Goal
One command: `uv run python -m signalpost run --organisations <jsonl|txt> --output-dir <dir> --state-dir <dir> --run-id <id>`
→ exactly one `Envelope` (models.py) per input in `<output-dir>/envelopes.jsonl`, plus `run-report.json`,
plus stored profile history in `<state-dir>` for refresh. Keyless. Python 3.12+, deps pinned in uv.lock.

## Hard limits (enforced in code, not by convention)
- 100 companies ≤ 45 min wall clock → target ≤ 30 min; hard deadline 40 min then emit remaining envelopes with
  `failed` families (reason `deadline`), never drop an input.
- ≤ 2,000 outbound HTTP requests per batch, **every redirect hop and retry counts**. Target ≤ 1,700.
  DNS lookups are not HTTP requests (declared in docs).
- $0 third-party spend. No required secrets.
- Never publish a fact under the wrong company. When unsure: `ambiguous` or `not_available`.
- Never turn absence into zero. A checked source with zero items → `not_available` with reason
  "checked: none found"; an unchecked source → state explains why (`not_applicable`, `blocked`, `failed`).

## Package layout and ownership
```
src/signalpost/
  models.py, context.py          ORCHESTRATOR (contract)
  http.py                        W1 core   BudgetedHttpClient (implements context.HttpClient)
  snapshots.py                   W1 core   gzip content-addressed store under <state-dir>/snapshots/
  registry.py                    W1 core   bulk loader + live Brreg fetchers + claim builders
  planner.py                     W1 core   tiering + per-company request allowance
  pipeline.py                    W1 core   orchestration, connector registry, envelope assembly, deadline
  report.py                      W1 core   run-report.json (requests by purpose, p50/p95, states)
  validate.py                    W1 core   envelope validator (schema + invariants below)
  __main__.py / cli.py           W1 core   CLI: run, validate, prepare
  caches/                        W2 caches nav.py, wikidata.py, email_domains.py, aliases.py, prepare.py, store.py
  web/                           W3 web    candidates.py, verify.py, crawl.py, extract.py, connector.py
  activity/                      W4 activity  nav_jobs.py, ats.py, feeds.py, youtube.py, connector.py
  refresh.py, synthesis.py       W5 refresh/summary
  viewer/                        W6 UI     static site builder
eval/                            W7 eval   splits, gold, proxy scorer
tests/signalpost/                each workstream adds tests for its own modules
```
Never edit files owned by another workstream. If you need an interface change, write it in your final report.
Reuse the starter kit freely by copying functions (its SSRF guard and social-URL normaliser now live in
`src/signalpost/urls.py`); all code lives in `src/signalpost/`.

## Pipeline order per company (pipeline.py)
1. `registry` connector (W1): bulk row → identity claims; live: `enheter/{org}`, `enheter/{org}/roller`,
   `underenheter?overordnetEnhet={org}&size=100` (skip if bulk says no subunits is not knowable → always call for
   T1+; T0 may skip and mark locations from business address only), `regnskapsregisteret/regnskap/{org}`,
   `regnskap/aarsregnskap/kopi/{org}/aar` (T2+ only; ~30 req/min limit, use a shared rate limiter).
   Families: identity, financials, financial_history, leadership, locations, group (parent via `overordnetEnhet` /
   `hovedenhet`; group otherwise `not_available`).
2. `web` connector (W3): candidates → verify → crawl → extract. Publishes `shared["verified_site"]`,
   `shared["site_pages"]` (list of {url, final_url, snapshot_ref, text excerpt, links}), `shared["social_links"]`,
   `shared["feed_urls"]`, `shared["ats_links"]`. Families: website, profiles, description.
3. `activity` connector (W4): NAV jobs by org number (incl. subunit org numbers), ATS feeds + careers pages from the
   verified site, RSS/Atom/news pages from the verified site, YouTube channel RSS for linked channels.
   Families: jobs, activity. `reviews` → `not_available` reason "no permitted keyless review source".
4. `synthesis` (W5) builds Summary from claims only. 5. `refresh` (W5) merges with previous profile → changes,
   first/last observed, superseded claims; writes new profile to state. 6. validate + emit envelope.

## Budget tiers (planner.py)
Signals from the bulk row only: employees, legal form, NACE, registry website, email domain (non-shared),
subunit count (aliases cache), MVA registration, bankrupt/liquidating.
- T0 shell (no staff, holding/property/housing NACE 64.2/68.x/70.1, BRL/ESEK/SAM, no site/domain): ~5 requests.
- T1 small (no staff but site/domain, or 1–4 staff): ~15. T2 (5–49 staff or site+staff): ~30. T3 (50+): ~45.
Global budget object: per-company allowance, borrow from a shared pool, hard global cap 1,900 (leaves 100 for
shared/cache sync). When a company's allowance is used up, its remaining connectors return `failed` reason
`request_budget` for unchecked families — never guess.

## Identity rules for external facts (W3 owns, everyone obeys)
- `exact` website requires ONE decisive signal: org number on any fetched page of that site (digits, allow spaces/dots,
  also "NO 123 456 789 MVA"), JSON-LD `vatID/taxID/identifier` = org number, Wikidata P2333 match, NAV ad with this
  org number whose employer homepage is this domain, or registry `hjemmeside` of this org number
  (identity_basis `registry_declared`) that is live, not parked, and not flagged as a franchise/parent site —
  OR two independent corroborating signals with no conflict: registered street+postcode on site, registry phone on
  site, registry email address on site, CEO/board member full name on site, exact legal-name match in title/JSON-LD.
- Conflict → never exact: a different org number shown prominently (footer/contact/JSON-LD) → `relationship`
  parent/franchise/brand/service_provider with `availability: ambiguous`, not published as the company's website.
- Email domains used by ≥3 organisations in the bulk file are service-provider domains (property managers,
  accountants) — never a website candidate for exact publication.
- Social profiles: only if linked from the exact-verified site (or Wikidata by org number). Store URL only;
  never scrape the platform.

## Claim conventions
- `claim_key(org, family, field, value_key)` from models.py. Single-valued fields: value_key None.
  Multi-valued: roles `f"{role_code}|{normalized person name}"`, jobs `nav:{uuid}` / `ats:{url}`, locations subunit
  org number, profiles normalized URL, activity item URL (or feed guid).
- Financial claims: one claim per metric (revenue, operating_result, annual_result, total_assets, equity, total_debt,
  employees_in_accounts if present) with `reporting_period`, currency NOK in value `{amount, currency}`.
- Every `available` claim has ≥1 evidence id; every evidence has source_url, retrieved_at, content_sha256,
  extraction_method and a `span` (the exact text or JSON path it came from).
- Dates: ISO 8601. Persons: name + role only; never store birth dates.

## Envelope invariants (validate.py enforces)
1. Exactly one envelope per input org number, same order as input, even on crash (catch per company).
2. Every family in `FAMILIES` has a FamilyState. 3. `available` claims have value ≠ None and evidence.
4. No claim value 0 unless the source literally states 0 (financial amounts may legitimately be 0 — keep span).
5. `sections` lists claim ids per models.SECTIONS. 6. operations filled. 7. evidence ids referenced exist.

## Refresh (W5)
State: `<state-dir>/profiles/{org}.json` = last envelope + full claim history (`history: list[Claim]` of superseded
claims with their evidence). Re-run with unchanged sources → zero changes, no duplicate claims/evidence
(idempotent). Changed single value → `changed_value` (or a typed name: `changed_website`, `changed_description`,
`new_filing` when reporting period advances). New multi-valued item → `new_*`; item missing when its source was
checked successfully → `ended_role` / `closed_job` / `closed_location`, claim `superseded`, evidence kept.
Source failed → keep last value, `last_observed_at` unchanged, add error, NO change event.

## Synthesis (W5)
Deterministic English sentences, each citing claim_ids: what it does (description or NACE), where, size (employees,
revenue trend if history), leadership, footprint (website/profiles), hiring/activity, what changed since last run,
and `unknowns` listing each family not available with its reason. No sentence without claims.

## Run report (run-report.json)
counts by family state, requests by purpose, total requests, runtime p50/p95 per company, deadline hits,
budget exhaustion count, validation result, cache versions used, third_party_cost_usd = 0.

## Definition of done per workstream
Code + tests (`uv run --with pytest pytest -q tests/signalpost`) passing, no network in unit tests (use fixtures /
fake HttpClient), a short `docs/<workstream>.md` describing behavior and limits, final report listing files,
interfaces used/added, known gaps.

## Caches API (W2 implements, W1/W3/W4 consume) — `from signalpost.caches import Caches`
Built outside the timed run by `python -m signalpost prepare --cache-dir cache/ --bulk <brreg-enheter.csv.gz>`,
published by us as a versioned tarball (declared external cache), loaded with `Caches.load(cache_dir)`.
A run may pass `--caches <dir>`; if absent, caches are `None`-safe (every consumer must handle missing caches).
- `caches.email_domains.org_count(domain) -> int`, `.is_shared(domain) -> bool` (count ≥ 3 or freemail),
  `.is_freemail(domain) -> bool`
- `caches.aliases.subunits(org) -> list[dict]` keys: organisation_number, name, street, postcode, city,
  website, email, employees, nace; `.names(org) -> list[str]` (distinct subunit trade names, cleaned)
- `caches.wikidata.lookup(org) -> dict | None` keys: qid, label, websites[list], profiles{platform: url},
  source_url (entity URL), retrieved_at
- `caches.nav.ads_for(orgs: list[str]) -> list[dict]` keys: uuid, title, status (ACTIVE/INACTIVE), employer_name,
  employer_orgnr, employer_homepage, published, expires, updated, application_url, source_url (feedentry URL),
  retrieved_at, content_sha256, work_locations[list[dict]]; `.sync_incremental(client, max_requests=60) -> dict`
  pulls feed pages newer than the cached cursor (charged to org=None) and fetches entry details only for items
  whose businessName matches a name in the current batch.
- `caches.meta -> dict` (built_at, source urls, row counts, sha256 of inputs).
Registry bulk: `registry.load_bulk(path, orgs) -> dict[org, row]` (W1). CSV is gzip-compressed despite the name.
Known bulk columns include: navn, organisasjonsform.kode, naeringskode1.kode, antallAnsatte, hjemmeside,
epostadresse, telefon, mobil, forretningsadresse.adresse/postnummer/poststed/kommune, postadresse.*,
registrertIMvaRegisteret, konkurs, underAvvikling, overordnetEnhet, stiftelsesdato, vedtektsfestetFormaal,
sisteInnsendteAarsregnskap, institusjonellSektorkode.*.

## Dependency rule
Do not edit pyproject.toml/uv.lock. Use stdlib + installed deps (pydantic, beautifulsoup4, lxml, extruct,
trafilatura, tldextract, pypdf). Need something else? Say so in your final report.
