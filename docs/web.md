# W3 — web: website discovery, identity verification, crawl, extraction

Owns `src/signalpost/web/` (`candidates.py`, `crawl.py`, `verify.py`, `extract.py`, `connector.py`).
Publishes the `website`, `profiles`, `description` families via `WebConnector` (`name = "web"`).

## Design goal

**Precision over recall.** One wrong-company website publication disqualifies a run (BUILD_SPEC.md).
Every module defaults to `ambiguous` / `rejected` / `not_available` when evidence is incomplete or
conflicting. `exact` is reserved for candidates with either one decisive identity signal or two
independent corroborating signals and zero conflicts.

## Pipeline

1. **`candidates.generate_candidates(ctx)`** — builds an ordered, domain-deduplicated candidate list from
   (in order): registry `hjemmeside`, Wikidata websites (`caches.wikidata.lookup`), NAV employer
   homepages for ads matching this org number (`caches.nav.ads_for`), the registry email domain (unless
   shared/freemail per `caches.email_domains.is_shared`), subunit websites/email domains, and finally up
   to 4 name-guessed slugs (joined/hyphenated, with/without legal-suffix words, `.no` before `.com`) from
   the legal name, historic names and subunit trade names. Guesses are DNS-prefiltered
   (`client.dns_resolves`) before any HTTP request and are skipped entirely for tier `T0`. A single-token
   slug that is very short (<=3 chars) or a known-generic industry/place word (e.g. "bygg", "holding") is
   dropped — a distinctive short brand name (e.g. "adma") is kept.

2. **`crawl.crawl_candidate(ctx, candidate)`** — fetches the homepage via `ctx.client.get(...,
   respect_robots=True)`, then up to N priority secondary pages (T0: 0, T1: 2, T2: 4, T3: 6) picked from
   homepage links first (contact/kontakt, about/om-oss, privacy/personvern, terms/vilkår/
   salgsbetingelser, impressum, careers/karriere, news/aktuelt — in that priority order), falling back to
   `sitemap.xml` only if the homepage yields no priority links. Detects parked/for-sale placeholders and
   JS-only shells (recorded via `CrawlResult.parked` / `.js_shell`, never rendered — no headless browser).

3. **`verify.assess(org_number, pages, registry_facts, candidate, website_org_count=...)`** — pure
   function, the identity-resolution core:
   - **Org-number detection**: `find_org_numbers` matches 9-digit groups (spaces/dots/nbsp-separated,
     optional `NO` prefix, optional `MVA` suffix, also `org.nr:` / `organisasjonsnummer` labels) and
     validates each with the mod-11 check digit (`is_valid_orgnr`), so unrelated 9-digit numbers (phone
     numbers, etc.) are not misread. JSON-LD `vatID`/`taxID`/`identifier` fields are checked the same way.
   - **Decisive signals** (any one → `exact`): our org number on any fetched page or in JSON-LD; the
     candidate came from Wikidata (tied to our org number in the cache) or a NAV ad with our org number;
     or a `registry_website` candidate that is live, not parked, shows no conflicting org number, and
     whose domain is not used by >=3 organisations (`website_org_count`) — otherwise it is a shared/parent
     site.
   - **Conflict**: a *different* valid org number shown on the site → never `exact`. Returns `related`
     with a heuristic `relationship` (`franchise` if franchise/chain wording co-occurs with our name,
     `parent` if group/konsern/housing-manager wording is present, else `brand`).
   - **Corroborating signals** (need >=2, no conflict, → `exact` with `identity_basis="corroborated"`):
     registered street+postcode, registry phone (8 digits, `+47`/spacing tolerant), registry email,
     a registered CEO/board member name (`registry_facts["role_holders"]`, falls back to the legacy key
     `role_names` if present), and an exact legal-name (minus suffix) match in `<title>`. One corroborating
     signal alone → `ambiguous`, never `exact`.
   - A parked homepage, or a page that fetched fine but shows no evidence at all → `rejected`.

4. **`extract.extract_all(pages)`** — from the *exact*-verified pages only: description (JSON-LD
   `description` > meta/og:description > first substantive paragraph via trafilatura, capped at 400
   chars, with the source span kept), brand name (JSON-LD `name` > `og:site_name` > `<title>`), social
   profiles (reusing `norway_company_agent.website.normalize_social_url` — share/intent links excluded),
   contact email/phone, JSON-LD address/foundingDate/numberOfEmployees/sameAs, RSS/Atom feed links, ATS
   links (teamtailor, webcruiter, jobylon, reachmee, hrmanager, recman, easycruit, workday,
   smartrecruiters, lever, greenhouse, jobbnorge, finn.no job links), and the news page URL.

5. **`connector.WebConnector`** — tries candidates decisive-sources-first, stops at the first `exact`
   verdict. Publishes:
   - `website/official_website` (`{url, domain, brand_name}`, `relationship="exact"`) on success, or an
     `ambiguous` claim carrying the best `related` candidate's URL/relationship if no candidate verified
     exactly, or `not_available` with reason `"checked N candidates: none verified"` / `"no candidates"`.
   - `website/site_contact_email`, `website/site_phone` when found on the verified site.
   - `profiles/profile` — one claim per normalized social URL, `identity_basis="linked_from_verified_site"`.
   - `description/company_description`.
   - `ConnectorResult.shared`: `verified_site`, `site_pages` (url/final_url/snapshot_ref/text_excerpt
     <=3000 chars/links), `social_links`, `feed_urls`, `ats_links`, `brand_name`, `news_urls`, and
     `web_attempts` — every candidate tried with its verdict, for eval/debugging (not just the winner).
   - Honest `FamilyState` for `website`, `profiles`, `description` in every branch (never silently empty).
   - Respects `ctx.client.remaining(org)`: once a company's allowance is exhausted, remaining candidates
     are recorded as `skipped` / `request_budget` rather than guessed at.

## Interfaces consumed (not owned)

`context.CompanyContext` / `context.HttpClient` / `context.Response` (W1), `models.Claim` /
`Evidence` / `FamilyState` / `ConnectorResult` (orchestrator), `ctx.shared["registry_facts"]` (W1
registry connector: `organisation_number, name, aliases, street, postcode, city, phones, email,
email_domain, website, role_holders, subunits`; falls back to `ctx.bulk` when absent), `ctx.caches`
(W2: `email_domains.is_shared/.website_org_count`, `wikidata.lookup`, `nav.ads_for` — all optional,
`None`-safe).

## Tests

`tests/signalpost/test_web_verify.py`, `test_web_candidates.py`, `test_web_crawl.py`,
`test_web_extract.py`, `test_web_connector.py` — 41 tests, no network (fake `HttpClient` in
`tests/signalpost/web_fakes.py`). Regression coverage for the known identity traps from
`docs/PLAN.md` §3: `BUTIKKDRIFT KALIYUGARASAN AS -> 7-eleven.no` (franchise), `MECCA AS -> thon.no`
(parent/brand), `BESTSELLER AS -> bestseller.com` (group site, correctly not `exact`), and a shared
housing-manager domain (`obos.no`-style, `website_org_count >= 3` forces `related` even without a
conflicting org number).

Run: `uv run --with pytest pytest -q tests/signalpost`.

## Live eval script

`scripts/eval_web_probe.py` runs candidates+crawl+verify against
`tests/fixtures/website-probe-150.json` with **real network requests** (not part of the pytest suite).
It ships its own minimal `LiveHttpClient` implementing `context.HttpClient` (the real
`signalpost.http.BudgetedHttpClient` from W1 was not yet merged into this worktree when this was
written; swapping it in is a drop-in change — see "Known gaps"). Usage:

```
uv run python scripts/eval_web_probe.py [--limit N] [--stratum small|large|none] [--per-company-budget N]
```

It prints per-company request counts and a per-stratum summary (candidates tried, website
availability breakdown, requests/company), plus a "manual review" list of every `exact` verdict whose
`identity_basis` isn't `org_number_on_source` / `wikidata_org_number` / `job_feed_org_number`, and every
`related` verdict — these are the ones a human should read before trusting the rule set.

### Results (150-company probe, live run, `--per-company-budget 15`)

See the run log for the exact per-company breakdown. Headline numbers and the manual-review outcome are
in the final report of the commit that added this script — re-run the script to reproduce, since it
depends on live sites' current content.

## Known gaps / follow-ups

- The eval script's `LiveHttpClient` is a standalone stdlib client for measurement only; it does not
  share W1's snapshot store, retry/backoff policy, or thread pool. Swapping in
  `signalpost.http.BudgetedHttpClient` (now on `main`) once this branch is rebased is a one-line change
  in `scripts/eval_web_probe.py`.
- `verify._infer_relationship` is a small keyword heuristic (franchise/parent/brand wording). It is
  intentionally conservative — it never returns `exact` on conflict — but the specific relationship label
  (franchise vs. parent vs. brand) can be wrong; only the "never `exact` on conflict" property is
  load-bearing for correctness.
- Name-guess slug generation does not yet try subunit trade-name variants beyond the first 4 total
  guesses across all names; for companies with many subunits some plausible guesses are never tried
  (recall loss only — does not affect precision).
- No JS rendering (by design/budget). Sites that are JS-only shells with no server-rendered fallback
  content are recorded as `js_shell` and typically end up `rejected` for lack of extractable evidence,
  even when they are, in fact, the right company.
- `extract.extract_contact` phone regex may occasionally match a non-phone 8-digit run (e.g. an org
  number written without spacing) on pages with no clearer contact page; it is not used for identity
  (verify.py has its own stricter phone corroboration path), only for the `site_phone` claim.
