# Evaluation harness: corpus, scorer, promotion rule

Owned by `eval/` (`splits.py`, `gold.py`, `score.py`, `report.py`) plus `eval/data/`. This is a local,
independent yardstick, since the organiser scores against a hidden pooled verified collection we never
see during development — without it, "did this change help" has no number attached, and a wrong-company
publication would go uncaught until a real submission scored it.

## Splits (`eval/splits.py`)

Built once from `data/signalpost-universe.jsonl.gz` (411,160 rows) with `python -m eval.splits`. Every
organisation is ranked by `SHA256(SEED:core:org)`; the ranking is sliced into disjoint windows:

| split | size | selection |
|---|---|---|
| `dev` | 300 | uniform random (shaped like the organiser's daily batch) |
| `val` | 200 | uniform random, disjoint from `dev` |
| `test` | 200 | uniform random, disjoint from `dev`/`val`; held out, not tuned against |
| `stress` | 150 | enriched edge cases, disjoint from all of the above |

`stress` buckets (round-robin so no single bucket dominates): `franchise_like_name`,
`generic_name_words`, `asa`, `nuf`, `housing_coop` (BRL/SAM/ESEK), `staffed_with_registry_website`,
`staffed_without_registry_website`.

`eval.splits.daily_like(universe_path, seed, size=100)` is **not** a frozen split — it draws an
independent uniform 100-row batch per seed, for rehearsing the organiser's daily-batch shape (100
companies) during development without tuning against a fixed set. Re-running `python -m eval.splits` is
deterministic (same seed, same input → byte-identical output); the script asserts zero organisation-
number overlap across `dev`/`val`/`test`/`stress` before writing anything.

## Gold labels (`eval/gold.py`, `eval/data/gold_web.jsonl`)

**131 independently researched companies** (target was ≥120) — the `dev` split's staffed companies
(employees ≥ 1) plus a prioritized slice of `stress` (staffed-with/without-website, franchise-like names,
ASA companies — the buckets most likely to produce a wrong-company error). Labelled by fetching the
Brønnøysund registry API (`data.brreg.no/enhetsregisteret/api/enheter/{org}`) directly for authoritative
name/address/phone/email, then researching the live web independently for a candidate site and verifying
it against that registry record (org number on the page, matching address/phone, JSON-LD identifiers)
before accepting it as `exact`. Related/franchise/parent sites are labelled as such, never as `exact` for
the subsidiary/franchisee; genuinely unresolved cases are labelled `uncertain`, not guessed.

**Labelling rule: never read the agent's own output while labelling**, and re-verify from scratch rather
than "confirming" against the agent's claims if a correction is ever needed — the whole point is an
independent check, not a rubber stamp.

Current label distribution (`eval/data/gold_web.jsonl`, 131 rows, `website.status`):

```
exact:        76
none_found:   29
related:      16
uncertain:    10
```

Schema (`eval.gold.GoldRecord`): `organisation_number`, `name`, `website` (`status` ∈
`exact`/`related`/`none_found`/`uncertain`, plus `url`/`relationship`/`evidence_url`/`evidence_note`),
`profiles`, `jobs` (`checked`/`active`/`note`), `confidence`, `notes`, `labeller: "independent"`,
`labelled_at`.

## Scorer (`eval/score.py`)

```
uv run python -m eval.score --envelopes <path> --gold eval/data/gold_web.jsonl [--report run-report.json] --out eval-report.json
```

Produces `eval-report.json` plus a companion `eval-report.md` readable table. `score(...)` returns:

- **`contract`** — exactly-one-envelope-per-org, every `FAMILIES` entry present, every `available` claim
  has evidence, no dangling evidence references, no value/availability invariant violations. (Mirrors
  `validate.py`'s checks, run against the same output.)
- **`family_coverage`** — per family: companies with `available` state and rate, total claim counts.
  Independent of gold; works on any envelope set.
- **`abstention_rate`** — fraction of (envelope, external family) pairs that correctly abstained
  (`not_available`/`ambiguous`) rather than guessed.
- **`website` / `profiles` / `jobs`** — precision and company recall against `gold_web.jsonl`. `website`
  reports `wrong_company_publications` prominently: an `exact` claim whose normalized URL doesn't match
  gold's site for that org (or matches a *different* org's related/franchise site) counts here.
  `gold.status == "uncertain"` rows are excluded from precision/recall (no known truth to grade against).
- **`pooled_recall_proxy`** — `0.7 * company_recall + 0.3 * fact_recall` per family, approximating the
  organiser's stated coverage method (family-weight configurable via `score(..., family_weights=...)`,
  default equal weight across website/profiles/description/jobs/activity). `description` and `activity`
  have no independent gold yet, so their contribution is coverage-only (unverified for precision),
  flagged separately in `unverified_families_coverage_only`.
- **`refresh`** — only when `--previous` envelopes are supplied: duplicate claim ids, and "false changes"
  (a `Change` reported even though the current-status values are byte-identical to the previous run — the
  idempotency violation the organiser explicitly checks for).
- **`budget`** — only when `--report run-report.json` is supplied: total requests ≤ 2,000, runtime ≤ 45
  minutes.
- **`synthesis`** — every summary sentence cites ≥1 claim id; unknowns are listed for at least half the
  envelopes.
- **`proxy_score`** — coverage (35, from `pooled_recall_proxy.overall`), accuracy (30, from website
  precision minus a heavy per-wrong-company-publication penalty), refresh (20), synthesis (10), UX (5,
  **placeholder** — no automated UX proxy exists in this harness). A run with any wrong-company
  publication, a failing contract, or a budget breach fails `qualification_gates` and its total is
  hard-capped at 40/100, mirroring "any material wrong-company match blocks qualification."

### Latest scored run

Gold set (131 companies), code of 25 Sep 2026 (historical; before revision 1):

| Metric | Value |
|---|---|
| Wrong-company website publications | **0** |
| Website precision vs gold | 100% (two gold labels corrected after independent re-checks: HONG KONG PALACE AS → sushime.no, VIEJEGA AS → viejega.no) |
| Website company recall | 53.3% |
| Description on companies with a verified site | 44 / 44 |
| Jobs `failed` | 0 (was 93 / 131 with per-company search) |
| Requests / runtime | ~1,800 / ~9 min (131 companies) |

Revision-1 measurements (6 Oct 2026):

| Check | Result |
|---|---|
| 1,500 random companies in one run (defaults), run twice | 27.6 and 25.5 min, 14,517 and 14,476 requests, peak memory 738 MB, no deadline or budget hits, 1,500 of 1,500 completed in both. Identical facts for 1,497 of 1,500; the 3 differences were sites down in one run (2) or a site that changed its own text (1) |
| 300 random companies, before → after | website 13.0% → 18.0% (0 lost), profiles 7.3% → 10.0%, activity 3.7% → 6.3%, reviews 0 → 2.3% |
| Wrong-company check of every newly accepted website (independent hand-check) | round 1: 6 wrong of 28 — all from counting the name in domain and title twice; rule fixed (name signals count once, parked pages rejected). Round 2: 1 wrong of 17 (a housing manager's site listed on the company's Wikidata item, published as a group site); group sites now need a word of the company's name in the domain, and a re-run no longer publishes it |
| 1,500 companies drawn like the scored population (account filers, 60% with registered staff), one run (defaults), 7 Oct | 27.9 min, 19,688 requests (13 per company), no deadline or budget hits, 1,500 of 1,500 completed, validation passed; website 33.5%, profiles 21.3%, reviews 3.1% |
| 300 account filers, before → after the 7 Oct changes | website 100 → 104 companies (0 lost), profiles 61 → 63; the 4 new sites checked by hand: all correct (3 show the org number) |
| Builderr's 100-company sample, before → after the 7 Oct changes | websites matching Builderr's 72 → 73 of 84 (+P.E. Gaarud, +H. Lundbeck), social platforms 76 → 81 of 86 |
| Determinism, two fresh runs of the 100 smoke-test companies from a clean clone | 0 fact differences, 0 evidence differences (ids, sources, quotes) |
| Clean clone of the submitted commit | see `smoke-test/README.md` (audit 0 problems, rerun 0 changes) |

Official batch shape (1,000 companies in ONE run, final code, 28 Sep 2026): every envelope valid; about
25 minutes and 9,000 requests with the budget of that version (19 per company, 40-minute deadline), no deadline or
budget hits. Available per family: identity 1000, leadership 1000, locations 999, financials 997,
financial_history 996, description 984, website 214, group 147, profiles 141, activity 74, jobs 7. An
automated audit of every envelope (evidence source/time/hash on every claim, no dangling or duplicate
claims, no future dates, every summary sentence cited) found 0 problems, and 150 of 150 sampled
revenue, operating-result, CEO, chair and employee facts matched the live register.

Published websites were re-verified on an earlier 1,000-company set: 174 automatically (org number
re-found on the evidence page, or live registry website) and the rest by hand — 0 wrong-company, 0
related, 1 unclear (a thin storefront whose brand name matches the legal name).

Refresh stability under network errors: the same 100 companies run three times with one `--state-dir`
reported 0 changes on runs 2 and 3 despite ~22 website connection errors per run (transient failures are
retried once, and a previously verified source that cannot be reached is carried forward, not removed).

Clean-clone test: `uv sync` plus the evaluator command with no data or caches downloads the bulk file,
builds the shared-domain, Wikidata and open-places tables, and completes; see `smoke-test/README.md` for
the current 100-company clean-clone run.

Refresh on real data: batch 1 of the 24 Sep profiles rerun on 27 Sep against its stored state reported
sensible typed changes (new filings, a moved business address, a newly verified website and profile, a
withdrawn subsidiary site) with `previous_run_id` set on every envelope; an immediate second rerun reported
**0 changes**.

## Promotion rule (`eval/report.py`)

```
python -m eval.report --baseline eval-report-baseline.json --challenger eval-report-new.json [--out promotion.json]
```

`compare(baseline_report, challenger_report)`:

- **REJECT** if the challenger has any *new* wrong-company publication, a website-precision drop, exceeds
  budget, or newly fails qualification while the baseline qualified.
- **PROMOTE** if zero new wrong-company publications, no precision drop, proxy total gain ≥ 2.0 points,
  within budget, and the challenger itself qualifies.
- **HOLD** otherwise (e.g. gain < 2 points but nothing regressed — inconclusive, keep iterating).

## Known gaps / honest limitations

- **UX (5 pts)** has no automated proxy — the organiser's rubric item is about a human finding/comparing/
  verifying facts on desktop and mobile, which this harness cannot observe. Placeholder `0.0` in
  `proxy_score`; the static viewer (`docs/viewer.md`) should be checked manually.
- **`description` and `activity`** have no independent gold yet — only a coverage-only, unverified proxy,
  flagged on every report via `unverified_families_coverage_only` so it is never silently trusted as
  precision-checked.
- **`reviews`** has no gold and no coverage proxy; it carries Mattilsynet food-hygiene inspection results
  for food-service companies only (about 2% of a random batch; see `LIMITATIONS.md`).
- `pooled_recall_proxy` approximates the organiser's "pooled verified collection" with our own gold as
  the denominator; the real pooled collection additionally includes what *other* entrants verified,
  invisible to this harness. Treat it as a lower-bound-ish local estimate, not the organiser's true
  number.
- Gold labels cover a sample (`dev` staffed + a slice of `stress`), not the full `dev`/`val`/`test`
  splits.
