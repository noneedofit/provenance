# Evaluation harness and independent gold labels

> Development notes, written while this part was built (September 2026). Some details have changed
> since; `README.md`, `AGENT.md`, `CRAWLERS.md`, `SOURCES.md`, `IDENTITY_RESOLUTION.md`, `DATA_SCHEMA.md`,
> `REFRESH.md` and `LIMITATIONS.md` describe current behaviour.

Files: `eval/` (package), `eval/data/` (frozen splits + gold), `tests/signalpost/test_eval*.py`.

## Why this exists

The organiser scores against a hidden pooled verified collection we never see during
development. Without a local, independent yardstick we'd be flying blind between submissions —
worse, we could easily fool ourselves by grading the agent against its own claims. This harness
gives us: (1) frozen splits shaped like the organiser's daily batch so we test realistic
company mixes, not a hand-picked easy set; (2) a gold-label collection researched independently
of the agent, so a wrong-company publication is actually caught, not rubber-stamped; (3) a
proxy scorer mirroring the organiser's category weights, so "did this change help" has a number
attached; (4) a promotion rule so we don't ship changes on vibes.

## Splits (`eval/splits.py`)

Built once from `data/signalpost-universe.jsonl.gz` (411,160 rows) with `python -m eval.splits`.
Every org is ranked by `SHA256(SEED:core:org)`; the ranking is sliced into disjoint windows:

| split | size | selection |
|---|---|---|
| `dev` | 300 | uniform random (organiser daily-batch shape) |
| `val` | 200 | uniform random, disjoint from `dev` |
| `test` | 200 | uniform random, disjoint from `dev`/`val`; held out, don't tune against it |
| `stress` | 150 | enriched edge cases, disjoint from all of the above (separate ranking + pool) |

`stress` buckets (round-robin so no single bucket dominates): `franchise_like_name`,
`generic_name_words`, `asa`, `nuf`, `housing_coop` (BRL/SAM/ESEK), `staffed_with_registry_website`,
`staffed_without_registry_website`. Actual counts from the current universe file: 22 / 22 / 22 /
21 / 21 / 21 / 21 (150 total — a few buckets have fewer eligible rows than requested after
dedup with dev/val/test, made up by round-robin over the rest).

`daily_like(universe_path, seed, size=100)` is **not** a frozen split — it draws an independent
uniform 100-row batch per `seed`, for rehearsing the organiser's daily-batch shape during
development (e.g. spot-checking runtime/budget on a batch you haven't tuned against).

Re-running `python -m eval.splits` is deterministic (same SEED, same input file -> byte-identical
output), and the script itself asserts zero organisation-number overlap across dev/val/test/stress
before writing anything.

## Gold labels (`eval/gold.py`, `eval/data/gold_web.jsonl`)

**Independent** ground truth for the external families (website, profiles, a jobs spot-check),
covering the `dev` split's staffed companies (employees >= 1, 51 companies) and a prioritized
slice of the `stress` set (staffed-with-website, staffed-without-website, franchise-like names,
and ASA companies — the buckets most likely to produce wrong-company errors). Labelled by
fetching the Brønnøysund registry API (`data.brreg.no/enhetsregisteret/api/enheter/{org}`) for
authoritative name/address/phone/email, then researching the live web for a candidate site and
verifying it against that registry record (org number on the page, matching address/phone,
JSON-LD identifiers) before accepting it as "exact". Related/franchise/parent sites are labelled
as such, never as "exact" for the subsidiary/franchisee. Uncertain cases are labelled
"uncertain", not guessed. See `eval/gold.py` for the exact schema (`GoldRecord`).

**Labelling was never done by reading our own agent's output** — the agent didn't exist yet
when these labels were produced, and even after it exists this collection should stay
independent (re-verify from scratch if a correction is ever needed, don't "confirm" against our
own claims).

Label counts: see `EVAL.md` (131 companies: exact 76, none_found 29, related 16, uncertain 10).

## Scorer (`eval/score.py`)

`score(envelopes_path, gold_paths, *, previous_envelopes=None, report=None)` returns a dict with:

- **`contract`** — exactly-one-envelope-per-org, every `FAMILIES` entry present, every
  `available` claim has evidence, no dangling evidence refs, no value/availability invariant
  violations.
- **`family_coverage`** — per family: companies with `available` state and rate, fact
  (`claim_count`) totals. Independent of gold (works on any envelope set).
- **`abstention_rate`** — fraction of (envelope, external family) pairs that correctly abstained
  (`not_available`/`ambiguous`) rather than guessed.
- **`website`** / **`profiles`** / **`jobs`** — precision and company recall against
  `gold_web.jsonl`. `website` also reports `wrong_company_publications` prominently: an `exact`
  claim whose normalized URL doesn't match gold's site for that org (or matches a *different*
  org's related/franchise site) counts here. `gold.status == "uncertain"` rows are excluded from
  precision/recall (we don't know the truth, so we don't grade against a guess).
- **`pooled_recall_proxy`** — `0.7 * company_recall + 0.3 * fact_recall` per family (mirroring
  the organiser's stated method), family-weight configurable via `score(..., family_weights=...)`
  (default: equal weight across website/profiles/description/jobs/activity). `description` and
  `activity` have no independent gold in this harness yet, so their contribution is a
  **coverage-only** proxy (companies where we published *something*, unverified for precision) —
  flagged separately in `unverified_families_coverage_only` so it never masquerades as a
  precision-checked number.
- **`refresh`** — only computed when `--previous` envelopes are supplied: duplicate claim ids,
  and "false changes" (a `Change` reported even though the claim's current-status values are
  byte-identical to the previous run — the idempotency violation the organiser explicitly
  checks for).
- **`budget`** — only computed when `--report run-report.json` is supplied: total requests <=
  2,000, runtime <= 45 minutes.
- **`synthesis`** — every summary sentence cites at least one claim id; unknowns are listed for
  at least half the envelopes (families genuinely not available should say so).
- **`proxy_score`** — coverage (35, from `pooled_recall_proxy.overall`), accuracy (30, from
  website precision minus a heavy per-wrong-company-publication penalty), refresh (20), synthesis
  (10), UX (5, **placeholder — no automated UX proxy exists in this harness**; see "Known gaps"
  below). A run with any wrong-company publication, a failing contract, or a budget breach fails
  `qualification_gates` and its total is hard-capped at 40/100, mirroring "any material
  wrong-company match blocks qualification" from the brief.

Run `uv run python -m eval.score --envelopes <path> --gold eval/data/gold_web.jsonl --out
eval-report.json` for a JSON report plus a companion `eval-report.md` (or `--markdown <path>`)
with a readable table.

## Promotion rule (`eval/report.py`)

`compare(baseline_report, challenger_report)`:

- **REJECT** if the challenger has any *new* wrong-company publication, a website-precision drop,
  exceeds budget, or newly fails qualification while the baseline qualified.
- **PROMOTE** if zero new wrong-company publications, no precision drop, proxy total gain >= 2.0
  points, within budget, and the challenger itself qualifies.
- **HOLD** otherwise (e.g. gain < 2 points but nothing regressed — inconclusive, keep iterating).

CLI: `python -m eval.report --baseline eval-report-baseline.json --challenger
eval-report-new.json [--out promotion.json]`.

## Known gaps / honest limitations

- **UX (5 pts)** has no automated proxy here — the organiser's rubric item is about a human
  finding/comparing/verifying facts on desktop+mobile, which this harness can't observe. It's a
  placeholder `0.0` in `proxy_score`; W6's static viewer should be checked manually.
- **`description` and `activity` families** have no independent gold yet (only a coverage-only,
  unverified proxy). Extending `gold_web.jsonl` with a labelled "what does the site actually
  say / what activity items are real" sample would close this gap; flagged in
  `unverified_families_coverage_only` on every report so it's never silently trusted as
  precision-checked.
- **`reviews`** has no gold and no coverage proxy either — the brief itself says keyless review
  sources aren't available (`not_applicable`), so this is expected to stay empty.
- The pooled-recall formula here approximates the organiser's "pooled verified collection" with
  our own gold as the denominator; the real pooled collection additionally includes what *other*
  entrants verified, which this harness has no visibility into. Treat `pooled_recall_proxy` as a
  lower-bound-ish local estimate, not the true organiser number.
- Gold labels cover a sample (dev staffed + a slice of stress), not the full `dev`/`val`/`test`
  splits — extending coverage over time (especially into `val`/`test` once we stop actively
  tuning against `dev`) would tighten the proxy further.
