# Agent overview: research strategy and abstention policy

## What runs, in order

`pipeline.run_batch` (`src/signalpost/pipeline.py`) builds one `CompanyContext` per organisation number
and runs a fixed connector sequence, each connector contributing claims/evidence/`FamilyState`s for its
own families and publishing facts other connectors can read from `ctx.shared`:

1. **`registry`** (`registry.RegistryConnector`, `src/signalpost/registry.py`) — families `identity`,
   `financials`, `financial_history`, `leadership`, `locations`, `group`. Calls the live Brønnøysund
   entity/roles/subunits/accounts APIs, falling back to the bulk CSV row if a live call fails. Publishes
   `shared["registry_facts"]` (name, aliases, address, phones, email, website, role holders, subunits) —
   the normalized fact base every later connector corroborates candidates against.
2. **`nav`** (`activity.nav_live.NavLiveConnector`, tiers T1–T3 only) — NAV's public job feed
   (`pam-stilling-feed`): a bundled active-ads index plus a live catch-up walk, matched by the employer's
   org number, each match confirmed on its own feed entry. Publishes zero claims itself; fills `ctx.shared["nav_ads"]` / `["nav_homepages"]` so
   `web` can use a NAV-confirmed employer homepage as a website candidate and `activity` can turn
   verified ads into `jobs` claims.
3. **`web`** (`web.connector.WebConnector`) — families `website`, `profiles`, `description`. Generates
   candidates, crawls each, and verifies identity (see `IDENTITY_RESOLUTION.md`); stops at the first
   `exact` verdict. Publishes `shared["verified_site"]`, `shared["site_pages"]`, `shared["social_links"]`,
   `shared["feed_urls"]`, `shared["ats_links"]`.
4. **`activity`** (`activity.connector.ActivityConnector`) — families `jobs`, `activity`, `reviews`. Reads
   NAV ads (live or cached), an ATS feed/careers page on the verified site, site RSS/Atom/news, and
   YouTube channel RSS for channels linked from the verified site, and Mattilsynet food-hygiene inspection
   results for `reviews` (published only when the inspection page shows our or a subunit's org number).

After all connectors run: `refresh.apply_refresh` merges the result with the previous stored profile
for the same organisation (`REFRESH.md`), `evidence_text.complete` fills each evidence record's exact
supporting text and fixes the order of claims and evidence, and `synthesis.build_summary` turns the
claims into deterministic English sentences (`docs/synthesis.md`). After the batch is written,
`validate.validate_envelopes` checks every envelope and the result goes into `run-report.json`. A connector crash, a missed deadline, or an exhausted request budget
never drops a company: `pipeline.run_batch` always emits exactly one envelope per input, with every
un-run family marked `failed` and a reason (`deadline`, `request_budget`, `time_budget: registry-only pass`,
`connector_error: …`, `crash`, `not_run`).

## Research strategy

- **Tiered budget, not uniform effort.** `planner.classify` (`src/signalpost/planner.py`) scores each
  company from bulk-registry signals alone (employee count, NACE code, legal form, presence of a
  registered site/domain, bankrupt/liquidating flags) into tiers T0 (shell, ~14 requests) through T3
  (50+ staff, ~55 requests) before any network call is made, so budget is spent where a footprint is
  actually likely to exist.
- **Cheapest, most decisive sources first.** Website candidates are tried in the order registry
  `hjemmeside` → Wikidata → NAV employer homepage → OpenStreetMap org-number tag → registry email
  domain → subunit websites/emails → open places data (Overture) → name-guessed slugs (DNS-prefiltered;
  at most 6, or 3 for T0, the last one being the name's first distinctive word on `.no`). A site that
  matches only on the company name gets up to two more pages (privacy, terms, contact) read before its
  verdict is final. The first candidate that verifies
  `exact` wins; the pipeline stops trying further candidates for that company.
- **No JavaScript rendering.** All crawling is server-rendered HTML only (`web.crawl`, stdlib `urllib` +
  BeautifulSoup/trafilatura); a JS-only shell is detected and recorded (`js_shell`), never rendered with
  a headless browser. This is a deliberate recall/cost trade-off — see `LIMITATIONS.md`.
- **Evidence is mandatory, not optional.** Every claim builder in every connector attaches a
  `source_url`, `retrieved_at`, `content_sha256` (where a live fetch occurred), `extraction_method`, and
  a `span` — the literal text or JSON path the value was read from — before the claim is added. The
  validator (`validate.py`) rejects an `available` claim with no evidence.

## Abstention policy

The rule, enforced throughout the code, is: **never turn absence into zero, and
never guess when unsure.** Every family on every envelope gets a `FamilyState.availability` from a fixed
vocabulary, each meaning something specific:

| Availability | Meaning | Example |
|---|---|---|
| `available` | The source was checked and produced at least one claim. | A verified website found. |
| `not_available` | The source was checked and genuinely found nothing — reason states what was checked (e.g. `"checked: none found"`, `"checked NAV arbeidsplassen: no active postings"`). Distinct from never having checked at all. | Jobs family, NAV searched, zero postings. |
| `not_applicable` | Checking this source doesn't make sense for this company. | `activity` when there is no verified site to derive it from; `financial_history` for a legal form that is not obliged to file accounts; `jobs` for a T0 company with no registered staff. |
| `blocked` | A source refused the request (robots.txt disallow, e.g. YouTube's `/feeds/videos.xml`). | — |
| `ambiguous` | Evidence exists but is not decisive enough to publish as fact — e.g. a website candidate with exactly one corroborating signal, or a related/franchise/parent site found instead of an exact match. | `website` claim published at `confidence=0.4`, never treated as the company's own site downstream. |
| `failed` | The check could not run this pass: a crash, the request budget ran out, or the wall-clock deadline hit. Refresh treats this as "not actually rechecked" and carries the previous value forward rather than dropping it. | — |

Concretely, the rule that governs every identity decision (`web/verify.py`): **precision over recall.**
`exact` is reserved for a candidate with one decisive signal (the org number found on the page or in
JSON-LD, a Wikidata/NAV link already tied to this org number, or a live/unconflicted/unshared
registry-declared site) or two independent corroborating signals with zero conflicts. A single
corroborating signal, a conflicting org number on the page, a shared domain, a franchise/chain domain, or
content that reads as hijacked/parked never reaches `exact` — it is published as `related`
(with a `relationship`: `parent`/`subsidiary`/`franchise`/`brand`/`service_provider`) or `rejected`
instead. One wrong-company publication disqualifies a run under the competition rules, so every ambiguous
case resolves toward *not* publishing rather than toward guessing.

## What "done" looks like for one company

An envelope with all 12 `FAMILIES` present (`identity`, `financials`, `financial_history`, `leadership`,
`locations`, `group`, `website`, `profiles`, `description`, `jobs`, `activity`, `reviews`), each with an
honest `availability` and (when checked) a list of sources checked; every `available` claim traceable to
evidence; a deterministic summary citing claim ids; and, on a rerun with the same `--state-dir`, a
`changes` list containing only genuine differences from the previous run.
