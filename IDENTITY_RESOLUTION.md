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
   names; joined and hyphenated, with and without legal-suffix words; `.no` before `.com`. Initials are
   joined (P.E. GAARUD AS → `pe-gaarud`). The first distinctive word alone (5+ characters, not generic) is
   the last guess for a longer name (CIMPLE TECHNOLOGY AS → `cimple.no`). A single generic token (≤3 chars,
   or a known-generic word like "holding"/"bygg"/"transport") is dropped before it is even tried.

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
- **A site that matches only on our name** (in the title or as the domain) gets one more look before the
  verdict stands: up to two unread pages that usually name the business behind a site (privacy statement,
  sales terms, impressum, then contact/about) are fetched and the same rules are applied again. Our org
  number there makes it `exact`; nothing new leaves it `ambiguous`.
- **A registry `hjemmeside` that forwards to the domain of the registry's own e-mail address for us**
  (`lundbeck.no` → `lundbeck.com/no`, e-mail `norway@lundbeck.com`) is not treated as forwarding to
  someone else's site: the register ties both domains to this organisation.
- **A topic/industry mismatch** between the company's NACE division and the site's content (≥2 distinct
  keyword-cluster hits for a *different* industry) blocks a non-decisive, non-registry-declared candidate
  from reaching `exact` via corroboration alone.

## 3. Publication gate (`web/connector.WebConnector`)

Tries candidates decisive-sources-first, stops at the first `exact` verdict. Only an `exact` verdict is
published as `website.official_website` with `relationship="exact"` and used to derive `profiles` and
`description`. A `related` verdict on a site the register (shared by at most 9 organisations, domain
carrying a distinctive word of our name), Wikidata or OpenStreetMap declares for this org number is
published as the company's website labelled with its relationship (`parent`/`brand`/`subsidiary`,
`confidence=0.8`) — the company's group site — but never used for description, and used for profiles only
when the domain is exactly our name (HAIKJEFTEN AS → `haikjeften.no`, which shows a sister company's org
number): those profiles are published with the same relationship, never as `exact`. When the register
lists a page on a shared chain/group site for us (HUSFLIDEN HOLMESTRAND SA → `norskflid.no/holmestrand`),
that page is published the same way if it still loads under that path and every distinctive word of our
name is on it (no profiles: the links on a chain page are mostly the chain's). Any other
`related` verdict is published as an `ambiguous` claim (`confidence=0.4`) that downstream connectors
(jobs/activity) never treat as a verified site. No verdict at all → `not_available`
with a reason naming how many candidates were tried.

## Regression-tested traps

`tests/signalpost/test_web_verify.py`, `test_web_candidates.py`, `test_web_blocklist.py` (130 web-module
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
| **Registered site forwarding to a shared e-mail domain** | A company's registered site forwarding to its accountant's site, where the company's registered e-mail also lives — the forward is trusted only when no other organisation uses that e-mail domain (H. LUNDBECK AS: lundbeck.no → lundbeck.com, own e-mail domain) | `test_a_registry_site_forwarding_to_our_own_email_domain_is_our_site`, `test_a_registry_site_forwarding_to_a_shared_email_domain_is_not_trusted_on_the_register_alone` |
| **Own-name domain forwarding to the parent** | FRONT SYSTEMS AS: frontsystems.no forwards to the parent's egsoftware.com, whose links are the parent's profiles — found by the 7 Oct 2026 hand-check list | `test_a_name_domain_forwarding_to_the_parents_site_does_not_give_the_parents_profiles` |
| **Generic path on a shared site** | A property manager's `/index.php` listed as a company's homepage | `test_a_generic_registry_listed_path_on_a_shared_site_is_not_our_page` |
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
