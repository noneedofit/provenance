# Identity resolution: candidate and publication gates

Owned by `src/signalpost/web/` (`candidates.py`, `crawl.py`, `verify.py`, `connector.py`) and
`web/blocklist.py`. **Design goal: precision over recall.** One wrong-company website publication
disqualifies a run under the competition rules, so every module defaults to `ambiguous`, `related`, or
`rejected` when evidence is incomplete or conflicting — `exact` is never a default.

## 1. Candidate generation gate (`web/candidates.py`)

Ordered, domain-deduplicated candidates, cheapest/most-decisive sources first:

1. Registry `hjemmeside` (decisive only after the live checks below — see §2).
2. Wikidata website (`caches.wikidata.lookup(org)`) — already tied to this org number in the cache.
3. NAV employer homepage from an ad confirmed to this org number (cache and/or this run's live `nav`
   connector) — already tied to this org number.
4. OpenStreetMap features tagged `ref:NO:orgnr` with our (or a subunit's) org number (bundled snapshot) —
   tied to this org number, treated like Wikidata.
5. Registry email domain, unless shared (`caches.email_domains.is_shared`, count ≥ 3 orgs or a hardcoded
   freemail list) or a known freemail domain.
6. Subunit websites / email domains (same shared-domain filter).
7. Open places dataset (bundled Overture Maps snapshot): a place carrying our registry phone/mobile or
   e-mail (entity or subunits), or our name core at our postcode, nominates its website (strongest
   matches first, at most 4 places). Never decisive; the match is recorded as a hint for §2.
8. Up to 6 DNS-prefiltered name-guess slugs (3 for tier T0) — legal name, historic names, subunit trade
   names; joined and hyphenated, with and without legal-suffix words; `.no` before `.com`. A single
   generic token (≤3 chars, or a known-generic word like "holding"/"bygg"/"transport") is dropped before it
   is even tried.

A homepage that fails to connect over `https://` is retried once over `http://` and/or with/without
`www.` (only for hosts that resolve in DNS).

**Never a candidate at all** (`web/blocklist.py`'s `MARKETPLACE_BLOCKLIST`): directories (`finn.no`,
`gulesider.no`, `1881.no`, `proff.no`, `purehelp.no`), booking/scheduling platforms (`mittanbud.no`,
`fixit.no`, `timma.no`, `ledigtime.no`, `bestille.no`), social platforms (`facebook.com`, `instagram.com`,
`linkedin.com`, `linktr.ee`, `youtube.com`, `tiktok.com`, `x.com`), and generic site-builder hosts
(`wix.com`/`wixsite.com`, `weebly.com`, `squarespace.com`, `wordpress.com`, `odoo.com`, `myshopify.com`,
`webnode.*`, `jimdofree.com`/`jimdosite.com`). Dropped before any HTTP request, and never used to derive
an email-domain candidate either — `registered_domain()` resolves a site-builder default subdomain
(`myshop.wixsite.com`) to its blocked apex, not to the registrant's own subdomain, so this can't be
bypassed by the default hostname pattern.

## 2. Verification gate (`web/verify.assess`)

Pure function, no network. Given the crawled pages and the org's registry facts, returns one of
`exact` / `related` / `ambiguous` / `rejected`:

- **`exact` requires ONE decisive signal**: our org number found on any fetched page (regex, mod-11
  checked) or in JSON-LD (`vatID`/`taxID`/`identifier`); the candidate came from Wikidata or a NAV ad
  already tied to our org number; or a `registry_website` candidate that is live, not parked, shows no
  conflicting org number, whose domain is not shared by ≥2 organisations, is not a known franchise/chain
  domain, does not name a different legal entity as the page's owner (copyright line / JSON-LD
  `legalName`), and — if it reads as a parent/umbrella page — carries at least one independent
  corroborating signal specific to this org.
- **`exact` OR via ≥2 independent corroborating signals, zero conflicts**: registered street+postcode,
  registry phone (8 digits), registry email, a registered CEO/board member name, the legal name in
  `<title>`, the domain spelling the full legal name, the registry e-mail's domain, or an open-places
  place listing this site with our registry phone/e-mail. **The name in the title and the name as the
  domain count as ONE signal** (a same-named company shows both). A name-guessed, open-places or
  e-mail-domain candidate additionally needs one "hard" signal (name, registry e-mail, e-mail-domain match,
  or an open-places match that also bears our name) — or three independent soft ones (address, phone,
  board member). Exactly one corroborating signal → `ambiguous`, never `exact`.
- **A registry-declared domain shared by 2–3 organisations** is `exact` only when the page names this
  organisation (legal name in the title) *and* shows its registered e-mail, phone or address.
- **A different valid org number shown prominently → never `exact`.** Becomes `related` with a heuristic
  `relationship` (`parent`/`subsidiary`/`franchise`/`brand`/`service_provider`).
- **A registry-declared domain used by ≥2 other organisations → `related`** (shared/parent site), never
  `exact` on bare registry-declared trust.
- **Content that reads as hijacked/re-registered (casino, adult, pharma-spam markers, word-boundary
  matched) → `rejected`**, never trusted via registry-declared trust or corroboration, even if a stale
  cached address fragment is still present.
- **A parked/for-sale placeholder homepage → `rejected`**, including registrar parking pages ("is
  registered, but the owner currently does not have an active website") and short "lanseres snart" /
  "kommer snart" / "under construction" pages.
- **A topic/industry mismatch** between the company's NACE division and the site's content (≥2 distinct
  keyword-cluster hits for a *different* industry) blocks a non-decisive, non-registry-declared candidate
  from reaching `exact` via corroboration alone.

## 3. Publication gate (`web/connector.WebConnector`)

Tries candidates decisive-sources-first, stops at the first `exact` verdict. Only an `exact` verdict is
published as `website.official_website` with `relationship="exact"` and used to derive `profiles` and
`description`. A `related` verdict on a site the register (shared by at most 9 organisations, domain
carrying a distinctive word of our name), Wikidata or OpenStreetMap declares for this org number is
published as the company's website labelled with its relationship (`parent`/`brand`/`subsidiary`,
`confidence=0.8`) — the company's group site — but never used for profiles or description. Any other
`related` verdict is published as an `ambiguous` claim (`confidence=0.4`) that downstream connectors
(jobs/activity) never treat as a verified site. No verdict at all → `not_available`
with a reason naming how many candidates were tried.

## Regression-tested traps

`tests/signalpost/test_web_verify.py`, `test_web_candidates.py`, `test_web_blocklist.py` (114 web-module
tests in `test_web_*.py`, no network — fake `HttpClient` in `tests/signalpost/web_fakes.py`; run with
`uv run --with pytest pytest -q tests/signalpost`):

| Trap | Example | Test(s) |
|---|---|---|
| **Franchise chain** | BUTIKKDRIFT KALIYUGARASAN AS's page on `7-eleven.no` — a franchisee's own address/phone/name on a "find your store" page would otherwise satisfy the ≥2-corroborating-signal rule | `test_franchise_trap_kaliyugarasan_7eleven`, `test_joker_franchise_domain_never_exact_without_our_org_number` |
| **Namesake / generic name** | A company sharing a common word with an unrelated business (JOKER AS, a sea-fishing company, vs. `joker.no`, the grocery chain) | `test_namesake_company_without_org_number_match_does_not_resolve_by_name_alone`, `test_namesake_company_resolved_by_org_number_match`, `test_industry_mismatch_guard_blocks_corroboration_on_unlisted_namesake_domain` |
| **Booking / scheduling platforms** | A registry or subunit email on `timma.no`/`fixit.no`/`ledigtime.no`/`bestille.no`/`mittanbud.no` must never derive a website candidate | `test_web_blocklist.py` blocklist coverage |
| **Shared / umbrella domain** | A parent/group/housing-manager site (`samfundet.no`, `hav.no`, `assemblin.com`, `bilfinger.com`, an OBOS/USBL-style housing manager) with no org-specific corroboration | `test_shared_housing_manager_domain_never_exact_even_without_conflict`, `test_registry_declared_parent_umbrella_site_without_corroboration_is_related_not_exact`, `test_registry_declared_parent_umbrella_site_with_corroboration_can_still_be_exact`, `test_shared_group_domain_without_our_org_number_is_related_not_exact`, `test_shared_group_domain_promoted_to_exact_only_by_org_number_match`, `test_registry_declared_domain_shared_by_two_orgs_blocks_exact` |
| **Sibling-registered domains** | A subpage of a shared domain that nonetheless shows *our* org number (DNT Nord-Trøndelag on `dnt.no/nord-trondelag`) must still resolve `exact` via the org-number path | `test_registry_declared_subpage_with_our_org_number_on_shared_domain_is_exact` |
| **Hijacked / re-registered domain** | GAASA AS's registry `hjemmeside` now serving casino-affiliate spam | `test_registry_declared_domain_hijacked_by_casino_content_is_rejected`, `test_hijack_marker_requires_word_boundary_not_bare_substring`, `test_registry_declared_hijacked_domain_with_real_corroboration_is_not_blindly_exact` |
| **Same-named company, name in domain and title** | `ottokoch.com` (a German chef) for OTTO KOCH AS; `activepartner.no` (another ACTIVE PARTNER AS) — found by a hand-check on 6 Oct 2026 | `test_name_in_domain_and_title_is_one_fact_not_two` |
| **Parked / coming-soon page** | Domeneshop parking pages, "Lanseres snart" | `test_parked_and_coming_soon_pages_are_rejected` |
| **Property manager's switchboard** | a housing co-op's registry phone is its manager's (`vestbo.no`) | `test_open_places_phone_match_alone_is_not_enough` |
| **Accountant's domain** | a registry e-mail on an accountant's domain whose page shows the client's (= accountant's) address | `test_registry_email_domain_with_address_only_stays_ambiguous` |
| **Live, unconflicted but wrong owner** | A registry-declared site whose footer/JSON-LD names a different legal entity (Xledger Labs AS → xledger.com, which only ever names "Xledger"/"Xledger AS") | covered in `verify.py`'s `site_owner_mismatch` path, exercised via the connector test suite |

## Gold-set validation (independent of unit tests)

`eval/data/gold_web.jsonl` (131 independently researched, non-agent-derived labels — see `EVAL.md`)
provides an end-to-end check against the real pipeline, not just the verification function in isolation.
The remaining known `related`-not-`exact` misses (precision-safe, i.e. never a wrong-company
publication) are documented in `docs/web.md`: two sibling-shared domains with no org-number-bearing page
crawled, one `odoo.com`-hosted shared registry domain, and one case (ARBEIDERPARTIET) where the crawled
page shows a different, labelled org number likely belonging to a separate administrative company.

## Known trade-offs

- No JavaScript rendering: a JS-only shell with no server-rendered content is recorded (`js_shell`) and
  typically ends up `rejected` for lack of extractable evidence, even when it is, in fact, the right
  company (recall loss, not a precision risk).
- `verify._infer_relationship`'s specific label (`franchise` vs. `parent` vs. `brand`) is a keyword
  heuristic and can be wrong; only the "never `exact` on conflict" property is load-bearing for
  correctness.
- Name-guess slug generation is capped at 6 guesses (3 for T0) across all name variants; companies with many
  subunit trade names may have plausible guesses never tried (recall loss only).
