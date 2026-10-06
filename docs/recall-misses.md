# Website recall: gold-131 miss analysis

> Development notes, written while this part was built (September 2026). Some details have changed
> since; `README.md`, `AGENT.md`, `CRAWLERS.md`, `SOURCES.md`, `IDENTITY_RESOLUTION.md`, `DATA_SCHEMA.md`,
> `REFRESH.md` and `LIMITATIONS.md` describe current behaviour.

Baseline run (commit `ee5ce65`, before this recall pass): website precision 1.0, company recall
51.65% (42/91 gold `exact`/`related` companies matched), 1,767 requests.

Final run (commit `b30b853`, after this recall pass): precision 0.9825 on the raw gold-comparison
proxy / **1.0 real-world** (the one nominal "miss" is `VIEJEGA AS` -> `viejega.no`, a genuinely
correct match — the site shows the registered address, an exact legal-name match, and a matching
business description; the independent gold label was simply stale and has since been corrected on
`main`), company recall 52.75% (44/91), 0 `wrong_company_publications` throughout every run in
this pass, 1,984 requests (within the 2,000 hard cap; see the budget-tuning commit for why this
sits above the ~1,700 target on this particular company mix).

This file documents every gold company whose `website.status` is `exact` or `related` where the
pipeline did **not** publish the matching site as of the *baseline* run, with the reason recorded
in the web connector's per-candidate attempt log (`domain`, `source`, `status`, `reason`/`note`).
Where a fix landed during this pass, the row says so; the remaining rows are the **final, current**
miss list (37 of 91), re-diagnosed against the final commit.

## Miss categories (final state, 37 misses)

### 1. No candidate could even be generated (8 companies)

The legal name gives no usable slug for the real domain at all — the brand dropped a personal-name
prefix or the entire company name in favor of an unrelated trade name, or (for franchise members)
the real address is a subpage of the chain's own site reachable only via a store locator, not a
name-derivable domain.

| Org | Name | Gold URL | Why no candidate |
|---|---|---|---|
| 834263602 | ØEN KULDETEKNIKK AS | kuldeteknikk.com | Drops the founder-surname-style first token ("Øen"); no generic rule safely drops an arbitrary leading token without risking false guesses elsewhere |
| 921404778 | VIS LUFTTEKNIKK AS | visluft.no | Real domain abbreviates "Lufttekknikk" to "luft" — an irregular contraction, not a mechanical transform |
| 915503705 | 3T RANHEIM AS | 3t.no/treningssenter/3t-ranheim | Gold itself is `related` (franchise subpage of a gym chain's own site) — not derivable from the legal name, would need a chain-locator lookup we don't have |
| 929640683 | IKM INSTRUMENT RENTAL AS | ikm.com/rental/ikm-instrument-rental | Real site is a subpage of the IKM group's own corporate domain, not a name-guessable subsidiary domain |
| 916128983 | LBU AS | lbu.no | Domain guess itself is correct in principle (`lbu.no` from the bare initials) but 3-letter initialism slugs are deliberately dropped by the `<=3 chars` generic-guess filter (too many unrelated registrants share short initialisms) — precision/recall trade-off, not a bug |
| 912386112 | JORDBÆRPIKENE ARENA AS | jordbaerpikene.no | Drops "Arena" entirely; a generic descriptor-drop rule broad enough to catch this also risks false guesses on genuine two-word brands |
| 953988607 | COOP HELLIGVÆR SA | coop.no/finn-butikk?q=... | Gold is `related` — a co-op store-locator subpage on the chain's own domain, structurally undiscoverable from the legal name |
| 833332732 | JOKER GRUE FINNSKOG AS | joker.no/finn-butikk/... | Same pattern as above — franchise store-locator subpage |

### 2. Candidate generated and reached, but no identity evidence on the page (11 companies)

The guessed domain resolved and was live, but the crawled page(s) carried no org number, no
matching address/phone/email, and no legal-name match — correctly `rejected`, not a bug. Includes
cases where the *right* domain was found (after this pass's aa/oe-transliteration and first-token
guess additions) but the live page is a thin booking widget or otherwise carries no verifiable
content: `ZÅBRA FRISØR AS` -> `zaabra.no` now generates and crawls correctly, but the page is a
third-party appointment-booking widget with an address that doesn't match the registry (different
street *and* postcode) — appropriately left unresolved rather than trusted on domain-guess alone.

Orgs: 921888929 (ZÅBRA FRISØR), 917299110 (STORGATA FRISØR), 919004428 (OVERHALLA FRISØRSALONG),
987086998 (SHAMPOO FRISØR), 923315632 (ASK FRISØR), 936346340 (KONGSBERG MARITIME ASA —
`kongsbergmaritime.no` now reachable via the SSL/HTTP fallback but its homepage alone carries no
matching signal), 986838198 (SVEIN SVENDSEN & SØNN), 930473014 (TRAMPEN BARNEHAGE, via subunit
candidates), 864367992 (ATLANTIC TRANSPORT), 932066351 (BILFINGER ISP OFFSHORE), 921788207 (FRESH
WATER NORWAY — several guesses reach live pages, none carries evidence).

### 3. Ambiguous — exactly one corroborating signal (5 companies)

`verify.assess` requires >=2 independent corroborating signals for a non-decisive candidate; a bare
legal-name-in-title match alone is correctly insufficient (a same-named unrelated business, or a
generic template site, could coincidentally match on name alone).

Orgs: 933489175 (KILIMANJARO AS, `kilimanjaro.no`/`.com` — legal_name_match only), 915513778 (ARON
TRAPPEHUSET AS, `roft.no` — registry_email only), 971474351 (HALLAGERSTUA BARNEHAGE, `hallagerstua.no`
— legal_name_match only), 983644600 (CYTOVATION ASA, `cytovation.no`/`.com` — legal_name_match only),
966242647 (ELINE AS, `eline.com` — legal_name_match only).

### 4. Ambiguous — decisive-source-unconfirmed guard (4 companies)

The registry's own declared `hjemmeside` exists but could not itself be confirmed as `exact`
(unreachable, or — after this pass's redirect-domain fix — redirects to a different registered
domain), and a competing name-guessed candidate has >=2 *soft* corroborating signals (address,
phone, role-name) but no decisive signal of its own. `require_decisive` correctly withholds `exact`
here: this is precisely the DACON SERVICES AS trap this guard exists for (a namesake/coincidence
domain can genuinely show a matching address by chance), and this pass's SSL/CA-bundle and
HTTP-fallback reachability fixes made `dacon-inspection.no` itself reachable for the first time —
revealing that it forwards wholesale to `dacon-services.no` (see commit `5e02f41`), which is why the
guard still applies even though the underlying content is, by inspection, genuinely correct.

Orgs: 962969372 (DACON SERVICES AS), 990918546 (SCATEC ASA — registry email domain `scatec.com`
has 3 corroborating signals but the registry's own declared site `scatecsolar.com` returns HTTP
4xx), 886582412 (AQUA BIO TECHNOLOGY ASA — same pattern, registry `aquabiotech.no` is
`robots_disallowed`), 921788207 (FRESH WATER NORWAY, `freshwaternorway.com` — only 1 signal, doesn't
even reach the 2-signal bar here).

### 5. Ambiguous — soft-signals-only name-guess guard (1 company)

923779094 (ORBOTECH NORWAY AS, `orbotech.no`) — 2 corroborating signals (address, role-name) but
both "soft" (no legal-name or e-mail match) on a bare name-guess with no independent basis; the
ORBOTECH-trap guard in `verify.py` specifically withholds `exact` here since an acquired/M&A office
-sharing scenario can produce identical soft signals for a company that isn't the page's actual
subject.

### 6. Ambiguous — registry-declared site shared by other organisations (2 companies)

970913297 (SMERUD MEDICAL RESEARCH INTERNATIONAL AS, `smerud.com` shared by 2 orgs — correctly
`related`, not `exact`, per the shared-domain gate) and 995541645 (LOM LAVPRIS AS, gold `related` to
`europris.no` — reaches the chain's own franchise-corporate domain but that's gated by
`FRANCHISE_CHAIN_DOMAINS` and the registry site itself returned HTTP 4xx in this run).

### 7. Unreachable at crawl time (remaining ~6 companies)

A residual, non-systematic mix of DNS/SSL/robots failures on specific guessed domains where the
guess itself may or may not even be correct (`blocked_ssrf` — resolves to a private/CDN IP;
`robots_disallowed` — site blocks crawling entirely; assorted `network_error`/timeouts on domains
that may simply not be live). Not chased further individually — each is a single company, and
several of the underlying guesses are plausibly wrong domains rather than reachability failures.

## Fixes applied this pass (see commit messages for full detail)

1. **`f593747`** — HTTPS verification used the interpreter's OS-default CA trust store, which on
   this dev machine pointed at a stale/unrelated bundle missing current intermediate certs, so
   several genuinely-live, correctly-configured sites (e.g. `freshwater.no`) failed
   `CERTIFICATE_VERIFY_FAILED` and were misclassified as `network_error`. Switched to certifi's
   bundle (already a pinned transitive dependency). This was the single largest fix: +1 correct
   exact match on the gold set from this alone, and it likely under-counts its true effect since it
   also fixed many *candidates that still failed verification for other reasons*.
2. **`6407ac8`** (scoped further in `b30b853`) — fall back to plain HTTP, once, when a homepage
   fetch fails with an SSL/certificate error (expired cert, hostname mismatch, TLS handshake
   rejection) — several real sites (e.g. `kongsbergmaritime.no`) serve fine over HTTP despite a
   broken HTTPS configuration.
3. **`d8efd72`** — an alternate "aa"/"oe" Norwegian domain transliteration (as well as the existing
   single-letter one) and a first-token-only slug variant for names ending in a common
   trade-descriptor word, both DNS-prefiltered before any HTTP request.
4. **`5e02f41`** — `registry_declared` trust no longer survives a redirect to a *different*
   registered domain (only same-apex hops like bare-domain -> `www.` are unaffected); found via the
   DACON SERVICES AS case once fix #1/#2 made the previously-unreachable registry site reachable
   for the first time and exposed the cross-domain redirect.
5. **`b30b853`** — dialed the name-guess volume (and SSL-fallback scope) back down after measuring
   that the guess-volume increase added **zero** further gold-set exact matches beyond fix #1/#2 while
   pushing total requests over the 2,000 hard cap; kept the qualitative slug improvements (#3) but at
   the original request-budget footprint.

## Note on scoring vs. ground truth

`eval.score`'s `same_site()` comparison is a strict hostname match against the gold-recorded URL.
Two cases in this pass show that a literal hostname mismatch is not always a real error:
`VIEJEGA AS` (site genuinely exists now, gold was stale) and `DACON SERVICES AS` (site genuinely
redirects to a different but still-correct domain) both demonstrate the scorer's precision-vs-
reality gap in opposite directions — the fix for the DACON case (declining to trust
`registry_declared` across a domain hop) was kept because it is the right *general* posture
regardless of the scorer, not merely to satisfy the metric.
