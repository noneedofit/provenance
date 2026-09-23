# Signalpost — full research & plan (v2, 23 Sep 2026)

Status: research only. Nothing built yet. Starter kit downloaded to `signalpost-starter-kit/`
(sha256 3a8bd83e…b571). Probe scripts and results in `research/`.

---

## 1. What actually wins (reading of the rules + starter kit)

| Block | Pts | What earns it | Implication |
|---|---|---|---|
| Coverage | 35 | Per **external field family**: 70% company recall + 30% fact recall vs pooled verified collection | Registry facts are table stakes; points come from websites, profiles, jobs, activity, reviews |
| Accuracy | 30 | Right company, valid source + date; wrong/unsupported facts lose points | Every claim needs URL, retrieved_at, period, hash, span. One material wrong-company match blocks the run from becoming official |
| Refresh | 20 | Rerun: real changes shown, prior evidence kept, no duplicates / false changes | Cheap to get right, many entrants likely weak here |
| Synthesis | 10 | Summary: what it does, what changed, what's unknown, with sources | Deterministic template can get most of it |
| UX | 5 | Find, compare, verify on desktop + mobile | Static viewer is enough; JBOX option (see §7) |

Hard constraints per daily batch: 100 orgs · 45 min · **2,000 outbound requests (incl. redirects/retries)** ·
$10 declared spend · 8 vCPU / 16 GB · secrets via env vars only · one command · pinned deps.
Ranking = mean over every daily batch while our frozen version is active → **submit early**, iterate
(5 versions max, revisions close 18 Oct, entries close 21 Oct).

Scoreboard (16 Sep): Anmol 76.71 · Ajai 70.32 · digikuo / Harsh / Rakesh 66.92 · Sanjai 65.92 ·
Karthik 65.19 · Sudhir 62.92 · Hardik 57.16 · Dhanush 54.92.
Public repos show most entrants are lightly modified starter kits:
- Rakesh: Tavily/Exa discovery + OCR of annual reports.
- Hardik: keyless, 43/100 verified sites, 939 requests.

The bar to beat is roughly **77+**.

## 2. Universe facts (measured)
- 411,160 companies; 92% AS. **85% have no registered employees** (holding, property, housing co-ops).
- 10.9% list a website in the registry.
- Random 100-company batch ≈ 85 near-shells + ~11 small (5–19 staff) + ~4 larger. Coverage is judged
  against what *anyone* verified, so shells mostly cost nothing. Points are won on the ~15 staffed companies
  and on correctly abstaining for the rest.

## 3. Website probe (150 stratified companies, keyless)
- Registry website: usually right, but only ~45% show the org number (after checking contact, about and privacy pages).
  Some are franchise or parent sites (7-Eleven, thon.no), which must be labelled as a relationship, not an exact match.
- No registry website: `.no` name-guessing found a live domain for ~50% of staffed firms, but only ~10% were proven by org number.
  **Verification, not discovery, is the bottleneck** → invest in corroboration signals.
- Search spot-check (5 failures): results were mostly directories (proff, 1881, gulesider) and Facebook, which don't count as evidence.
  The real site surfaced in about 1 of 5.

---

## 4. Source catalogue (all options)

### A. Keyless & officially permitted — core
| # | Source | Gives | Cost in requests | Verified |
|---|---|---|---|---|
| A1 | Brreg enheter/{org} | identity, NACE, address, employees, homepage, name history, MVA | 1 | ✅ |
| A2 | Brreg roller | CEO, board, auditor, accountant + `sistEndret` | 1 | ✅ |
| A3 | Brreg underenheter | workplaces, **subunit names = trade-name/brand aliases** | 1 | ✅ |
| A4 | Brreg konsernstruktur | parent/subsidiary → label groups, avoid parent-site errors | 1 | doc'd in kit |
| A5 | Regnskapsregisteret latest | revenue, results, assets, debt, period | 1 | ✅ |
| A6 | Annual-account copies (years list + PDF) | history 2011–2025, prior-year figures, workforce text | 1+1 (≈30/min limit) | ✅ list |
| A7 | Brreg oppdateringer + bulk CSV | dated registry changes; zero-request identity cache | shared | ✅ |
| A8 | NAV job feed (public token, NLOD) | job ads with **employer.orgnr + homepage** | shared ~50–150/batch | ✅ schema |
| A9 | Wikidata SPARQL (P2333 org no.) | ~10.3k firms, 7.6k with website + social IDs, CC0, exact org-no. link | 1 bulk, pre-cached | ✅ counts |
| A10 | Verified company site | description, contact, locations, careers, news, JSON-LD, **social links** | 3–10 | kit has it |
| A11 | YouTube channel RSS (`feeds/videos.xml`) | dated videos for channels linked from verified site | 1 | to verify |
| A12 | ATS job feeds linked from site (Teamtailor, Webcruiter, ReachMee, Jobylon, HR-manager) | company-owned job lists | 1 | to verify |
| A13 | Company RSS / sitemap lastmod | dated news / public activity | 1–2 | kit has site-news script |
| A14 | Mattilsynet smilefjes (restaurants, org no.) | dated inspections = public activity | 1 | endpoint moved, to verify |
| A15 | TED EU procurement API (keyless) | contract awards naming Norwegian suppliers | shared | to verify |

Rejected / restricted:
- Brreg kunngjøringer: `w2.brreg.no/kunngjoring/` is disallowed by robots.txt.
- Norid WHOIS: no holder data; its terms forbid this use.
- proff / 1881 / gulesider / purehelp: third-party directories. Their terms restrict scraping and they aren't evidence.
- LinkedIn, Facebook, Instagram, Glassdoor, Indeed scraping: not permitted. We can only publish the profile URL when the verified site links to it.
- Oslo Børs newsweb: no public API. Use the company's investor-relations page instead.
- Doffin: API needs a subscription key.

### B. Free-tier keys (no money, but a key + terms)
| Source | Value | Catch |
|---|---|---|
| YouTube Data API v3 | subscribers/views for verified channels (1 unit/channel; 10k units/day free) | adds key dependency; RSS already gives dated activity |
| Google Places API (New) | ratings, review count, place website, address match (strong identity corroboration) | free caps 5k Pro / 1k Enterprise per month; ratings = Enterprise ($35/1k after cap); **terms forbid storing content beyond place_id** → conflicts with evidence preservation. High risk |
| Doffin API | procurement notices | registration; low hit rate for a random batch |

### C. Paid keys
| Source | Cost / 100 companies | Lift | Note |
|---|---|---|---|
| Brave Search ($5/1k) | ~$0.25–0.75 | discovery of sites not name-derivable | kit already has `run_brave_discovery.py` (transient, storage-safe) |
| Tavily / Exa / Serper | similar | same | Rakesh uses Tavily/Exa |
| Claude Haiku 4.5 ($1/$5 per MTok) | ~$0.5 | summary / description polish | never decides identity; also costs requests |
| Claude web_search tool (~$10/1k + tokens) | ~$1+ | same as search | less control → skip |

**Decision:** build keyless (A-list) first. It carries every required section, has $0 cost (third tie-breaker) and no key-failure mode
(a failed batch scores 0). Later A/B-test Brave (with its $5 free monthly credit) and YouTube API on a held-out set.
Keep them only if they add ≥2 proxy points with zero new wrong-company publications.

---

## 5. Methods

### 5.1 Request-budget planner (key idea)
Score each company's "footprint likelihood" from registry data alone:
- inputs: employees, NACE (holding/property/BRL → low), legal form, registry website, subunits, MVA, revenue;
- tiers: **T0 shell** registry only (~5 req) · **T1 small** (~15 req) · **T2 staffed** (~30–40 req) · **T3 large** (~50 req);
- a global scheduler reallocates unused budget; hard stop at ~1,850 to leave retry headroom.

### 5.2 Identity resolution (the accuracy engine)
Candidate generators, cheapest first:
1. registry homepage;
2. Wikidata website;
3. NAV employer homepage;
4. domains of emails and URLs in NAV ads;
5. name → `.no`/`.com` slugs from legal name, **subunit names**, historic names;
6. (optional) search API.

Pre-filter candidates with DNS before any HTTP request.

Proof signals, scored:
- **Decisive:** org number on the site (footer, /kontakt, /personvern, /vilkår, /salgsbetingelser; Norwegian e-commerce law makes web shops show it); JSON-LD `vatID` / `taxID` / `identifier`; a NAV ad with the same org number linking the domain; Wikidata.
- **Corroborating:** registered street + postcode on the site; CEO or board names; legal-name match; municipality.

Publication rule:
- `exact` = one decisive signal, or ≥2 independent corroborating signals with no conflict.
- A conflicting org number, or a konsernstruktur parent/sibling → `related` (labelled), never `exact`.
- Everything else → `ambiguous` / `not_available`.

### 5.3 Crawl & extraction
- static first; sitemap.xml to find contact, about, careers and news pages; robots respected;
- extruct (JSON-LD / OpenGraph) → trafilatura text → rules;
- Playwright only if a JS-shell check fails (costly; measure first);
- social links from the verified site → profiles family;
- careers: ATS detection → feed; plus NAV matches (map a subunit org number to its parent).

### 5.4 Financial history
Years list + latest-minus-one PDF. Parse the prior-year column with pypdf. OCR (tesseract) only as a fallback, only for T2/T3 companies.

### 5.5 Evidence & refresh
- Immutable snapshots: URL, redirect chain, status, retrieved_at, sha256, extractor version, source class.
- Stable claim keys, e.g. `org|family|field|normalized-value`. Idempotent upserts.
- Diff types: `new_role`, `ended_role`, `new_filing`, `new_job`, `closed_job`, `new_location`, `changed_website`, `changed_description`.
- Reporting materiality. Failed refresh keeps the last value and records the failure.
- The kit's refresh replay fixture is the regression test.

### 5.6 Synthesis
- Deterministic template, Norwegian + English, with a citation on every sentence.
- Sections: what it does · size & financial trend · leadership · footprint · hiring/activity · what changed · unknowns.
- Optional LLM rewrite later, grounded in claims only.

### 5.7 UX
- Static site generated from the envelopes: directory + search, company page, side-by-side compare, evidence drawer per fact, mobile layout.
- Same structure as the builderr sample site (5 data areas).

---

## 6. Evaluation loop (how we'll know we're winning)
- Frozen dev / validation / test splits from the universe, stratified like the kit's `sampling.py`, with no org or host overlap.
- Hand-labelled gold for ~150 companies: correct site, profiles, whether jobs exist. The kit's fixtures help.
- Local proxy scorer mirroring v2 weights. Track:
  - wrong-company publications (must stay 0);
  - claim precision;
  - per-family company/fact recall;
  - requests;
  - p50/p95 runtime;
  - refresh false-change rate.
- Promotion rule: zero new wrong-company publications, no precision drop, ≥2 proxy points gain, still within budget.

---

## 7. JBOX bonus ($500: $250/$150/$100 for *qualifying* agents "built with JBOX")
- JBOX (jboxai.com) is an AI **app builder**: plain-language brief → Next.js + Supabase app with hosting.
  50 free credits, then $20 per 100. Founders Jacques Louw and Saurav Chanda; Håvard Liltved Dalen (the challenge sponsor) is a co-founder.
- Natural fit: build the **UX layer** (the 5-point user-facing site) in JBOX, fed by our envelopes.
  The Python crawler stays the agent.
- **Unknown:** whether "built with JBOX" counts a JBOX-built UI, or needs more. Ask Builderr (submit@builderr.ai) before investing.
  The pool is probably thinly contested, so worth pursuing if the UI route is accepted.

---

## 8. Build order & timeline (today 23 Sep; revisions close 18 Oct)
| Phase | Dates | Deliverable |
|---|---|---|
| 0 Setup | 23–24 Sep | run kit replay + 10-company smoke; repo; pinned deps; dev/val/test splits; gold labelling starts |
| 1 Foundation | 24–26 Sep | registry layer (A1–A7) via bulk cache + live checks; evidence store; budget planner; envelope validator |
| 2 Identity + site | 26–30 Sep | candidate generators, DNS prefilter, proof scoring, site crawl, social links |
| 3 Jobs & activity | 30 Sep–2 Oct | NAV index, ATS feeds, site news / RSS, YouTube RSS, Wikidata cache |
| 4 Refresh + synthesis + UI | 2–4 Oct | diffs, summary templates, static viewer |
| **v1 submit** | ~4 Oct | 1,000 profiles + manifest + repo/commit; starts earning daily scores |
| 5 Experiments | 5–15 Oct | A/B Brave, YouTube API, PDF history, Playwright fallback, JBOX UI; ≤4 revisions |
| Freeze | by 18 Oct | final version |

## 9. Risks
- Wrong-company publication → strict gate + related/franchise labelling + regression fixtures from our probe (7-Eleven, Thon).
- Request overrun (redirects count) → cap redirects, per-company budgets, a global counter that fails safe.
- Source rate limits (annual-account copies ~30/min) → schedule early, in parallel with crawling.
- Evaluator environment differences → one-command runner, clean-room test, no required secrets.
- Rubric weights per field family are unpublished → optimise the families visible on the sample site first.
