"""Ordered, deduplicated website candidate generation.

Cheapest / most decisive sources first (see docs/PLAN.md §5.2 and BUILD_SPEC.md "Identity rules"):
registry hjemmeside -> Wikidata websites -> NAV employer homepage -> registry email domain ->
subunit websites/emails -> name-guessed slugs. DNS-prefilter guesses before any HTTP request.
"""
from __future__ import annotations

import dataclasses

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import tldextract

from .blocklist import is_marketplace_or_directory

# Words that should not, on their own, drive a name-guess slug (too generic to be a reliable identity guess).
# Includes bare industry-sector words that show up as a HISTORIC/trade name on their own (e.g. Bergen
# Bulktransport AS's subunit historic name "AS Transport AS" -> stripped token "transport"): a single
# generic industry word like this resolves to some unrelated, unaffiliated, already-registered domain far
# more often than it resolves to us.
GENERIC_WORDS = {
    "norge", "norway", "holding", "gruppen", "group", "invest", "investering", "eiendom",
    "eiendommer", "drift", "handel", "service", "tjenester", "consulting", "konsulent",
    "bygg", "shop", "butikk", "hus", "senter", "norsk", "nordic", "as", "asa",
    "transport", "logistikk", "logistics", "elektro", "rens", "vaktmester", "bygg", "montasje",
}

# Legal-form tokens: never part of a real domain, always stripped.
LEGAL_FORM_WORDS = {"as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen", "nuf"}

# Legal-form + generic descriptor tokens, stripped when building the "fully stripped" slug variant. A
# descriptor word (norge/norway/group/...) is usually noise, but not always -- see LEGAL_FORM_WORDS-only
# variant in _name_guess_slugs.
SUFFIX_WORDS = LEGAL_FORM_WORDS | {
    "holding", "eiendom", "eiendommer", "norge", "norway", "group", "gruppen", "invest",
}

# Was 4: too tight in practice. _name_guess_slugs() produces slugs in a fixed order (suffix-stripped
# variants first, then full-token variants, then first-two-tokens), and once the stripped variants alone
# (joined + hyphenated, x2 TLDs = 4 attempts) fill the cap, the full-token variant never gets tried at
# all -- even when it's the one that's actually right (FRESH WATER NORWAY AS -> freshwaternorway.com:
# "norway" is stripped as a generic suffix word, which is usually correct, but here it's part of the
# real brand, and the full-token variant that would have caught it never got a turn). Daily100-batch
# measurement: this raises the request budget by roughly 20% of web_homepage requests (well within the
# 1700-request global cap -- see docs/web.md).
MAX_NAME_GUESSES = 6

# T0 (shell-classified) companies get a much smaller name-guess cap rather than none at all -- see the
# comment at the T0 branch in generate_candidates(). Small enough that it's a negligible fraction of T0's
# ~5-request tier allowance even before the DNS prefilter thins it further.
# Kept small (not 0): a gold-set A/B measurement (with the corrected, non-double-counted request
# total) showed raising this cap from 6->9 / 1->2 added ZERO further gold exact matches
# (correct_exact held at 43) while costing +53 requests (1665->1718) -- reverted; not worth the
# budget against the incoming NAV-jobs-feed headroom requirement (target <=1750/gold131).
T0_MAX_NAME_GUESSES = 3
OPEN_PLACES_MAX_MATCHES = 4


def _slug_tokens(value: str) -> list[str]:
    text = str(value or "").translate(
        str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"})
    )
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [tok for tok in re.findall(r"[a-z0-9]+", text) if tok]


# Alternate, also-common Norwegian domain transliteration: "å" -> "aa" (the traditional double-vowel
# spelling still widely used in registered domains -- e.g. "Zåbra" -> zaabra.no, "Ålesund" -> aalesund.no
# historically) and "ø" -> "oe" (the Danish/German-style romanization, e.g. "Søren" -> soeren). Tried
# ALONGSIDE (never instead of) the primary single-letter transliteration in `_slug_tokens`, since either
# convention is genuinely common and neither reliably predicts the other.
def _slug_tokens_alt(value: str) -> list[str]:
    text = str(value or "").translate(
        str.maketrans({"ø": "oe", "Ø": "OE", "å": "aa", "Å": "AA", "æ": "ae", "Æ": "AE"})
    )
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [tok for tok in re.findall(r"[a-z0-9]+", text) if tok]


def registered_domain(url_or_host: str) -> str:
    ext = tldextract.extract(url_or_host or "")
    return ext.top_domain_under_public_suffix or ""


def _homepage_url(domain: str) -> str:
    return f"https://{domain}/"


def _normalize_url(value: str) -> str:
    import urllib.parse

    raw = value if value.startswith(("http://", "https://")) else f"https://{value}"
    parsed = urllib.parse.urlparse(raw)
    path = parsed.path or "/"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


@dataclass(frozen=True)
class Candidate:
    domain: str
    url: str
    source: str          # e.g. "registry_website", "wikidata_website", "nav_employer_homepage",
                          # "registry_email_domain", "subunit_website", "subunit_email_domain", "name_guess"
    label: str            # human readable provenance for logging/eval
    decisive: bool = False  # True if this source alone can establish identity (subject to live-site checks)
    rank: int = 0
    hints: tuple[str, ...] = ()  # open_places: which registry keys tied the place to us ("phone", "email", "name", ...)


# Priority when the SAME domain is produced by more than one source (e.g. Kitron/Mowi/AF Gruppen: the
# registry hjemmeside and the Wikidata P856 website are the same domain). Lower wins. A Wikidata- or
# NAV-sourced candidate is independently tied to OUR org number already, so verify.assess() can treat it
# as decisive even when the bare registry_declared path for that same domain would be blocked (shared
# domain, parent/umbrella wording, hijacked content) -- collapsing to "whichever came first" would throw
# that decisive signal away and silently fall back to the weaker, blockable registry_declared path.
_SOURCE_PRIORITY = {
    "wikidata_website": 0,
    "nav_employer_homepage": 0,
    "osm_orgnr_website": 0,
    "registry_website": 1,
    "registry_email_domain": 2,
    "open_places": 2,
    "subunit_website": 3,
    "subunit_email_domain": 3,
    "name_guess": 4,
}


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Domain-dedupe, keeping the single most decisive source per domain (see _SOURCE_PRIORITY) rather
    than simply the first one generated. Preserves first-seen order for the winning domain."""
    best: dict[str, Candidate] = {}
    order: list[str] = []
    for cand in candidates:
        if not cand.domain:
            continue
        if is_marketplace_or_directory(cand.domain):
            # Directories, booking/scheduling platforms and generic site-builder hosts are never a
            # company's own website, even when the registry or a subunit happens to list one (e.g. a
            # booking-platform URL entered as "hjemmeside" by mistake, or an email address on a free
            # site-builder subdomain). BUILD_SPEC.md: directories aren't evidence.
            continue
        if cand.domain not in best:
            best[cand.domain] = cand
            order.append(cand.domain)
        elif _SOURCE_PRIORITY.get(cand.source, 99) < _SOURCE_PRIORITY.get(best[cand.domain].source, 99):
            best[cand.domain] = dataclasses.replace(cand, hints=cand.hints or best[cand.domain].hints)
        elif cand.hints and not best[cand.domain].hints:
            # Same domain also nominated by an open places match: keep that corroboration on the winner.
            best[cand.domain] = dataclasses.replace(best[cand.domain], hints=cand.hints)
    ordered = [best[d] for d in order]
    return ordered


def registry_facts(ctx: Any) -> dict[str, Any]:
    shared = getattr(ctx, "shared", {}) or {}
    bulk = getattr(ctx, "bulk", {}) or {}
    facts = shared.get("registry_facts")
    if facts:
        # W1's registry_facts() (the live-registry-aware version) doesn't carry a "nace" field; the
        # industry/topic-mismatch guard (verify.industry_mismatch) needs one. Fall back to the bulk row
        # without touching registry.py (owned by W1).
        if "nace" not in facts:
            facts = {**facts, "nace": bulk.get("naeringskode1.kode")}
        return facts
    return {
        "name": bulk.get("navn"),
        "aliases": [],
        "street": bulk.get("forretningsadresse.adresse"),
        "postcode": bulk.get("forretningsadresse.postnummer"),
        "city": bulk.get("forretningsadresse.poststed"),
        "phones": [p for p in [bulk.get("telefon"), bulk.get("mobil")] if p],
        "email": bulk.get("epostadresse"),
        "email_domain": (bulk.get("epostadresse") or "").split("@")[-1] if bulk.get("epostadresse") else None,
        "website": bulk.get("hjemmeside"),
        "nace": bulk.get("naeringskode1.kode"),
        "role_holders": [],
        "subunits": [],
    }


# Conjunctions/branch-qualifier connectors that a company's own domain very often just drops entirely
# rather than spelling out -- "DRANGE KRAN OG TRANSPORT AS" -> drangekran.no (drops "og transport", not
# "drange-og-kran"). Used only to decide which tokens count as "substantive" for the first-two-tokens
# variant below; the full-name variants above are unaffected.
_CONJUNCTIONS = {"og", "and", "&"}


def _name_guess_slugs(name: str) -> list[str]:
    """Up to 2 base slugs (joined, hyphenated), each tried with and without suffix words removed, plus a
    first-two-substantive-tokens variant for longer names, an alternate aa/oe transliteration of the same
    variants, and a first-single-token variant for a short, distinctive brand name."""
    tokens = _slug_tokens(name)
    if not tokens:
        return []
    alt_tokens = _slug_tokens_alt(name)

    def _variants_for(toks_primary: list[str]) -> list[str]:
        if not toks_primary:
            return []
        stripped = [t for t in toks_primary if t not in SUFFIX_WORDS] or toks_primary
        # The legal-form suffix (AS/ASA/...) should ALWAYS be dropped -- it's never part of a real domain
        # -- but a bare descriptor word ("Norway", "Group", ...) sometimes genuinely IS part of the brand
        # (FRESH WATER NORWAY AS -> freshwaternorway.com). `stripped` above removes both; this variant
        # removes only the legal form, keeping any descriptor word, so a brand that keeps it still gets a
        # matching slug instead of being represented only by the (wrong, "AS"-suffixed) full-name variant.
        legal_form_stripped = [t for t in toks_primary if t not in LEGAL_FORM_WORDS] or toks_primary
        out: list[str] = []
        for toks in (stripped, legal_form_stripped, toks_primary):
            if not toks:
                continue
            joined = "".join(toks)
            hyphenated = "-".join(toks)
            for slug in (joined, hyphenated):
                if slug and slug not in out:
                    out.append(slug)

        # First-two-tokens: a trade/brand domain routinely drops everything past the first two
        # distinctive words -- a branch qualifier ("AVD. VOLLEN"), a conjunction clause ("OG TRANSPORT"),
        # a co-founder's surname or a trailing place/product name. E.g. "GULLSMED FJELL AVD. VOLLEN AS" ->
        # gullsmedfjell.no, "SVEIN SVENDSEN & SONN TRAFIKKSKOLE AS" -> svein-svendsen.no. Only tried once
        # there are >=3 substantive (non-suffix, non-conjunction) tokens, so a short name isn't affected
        # (it already gets the same slug from the variants above).
        substantive = [t for t in stripped if t not in _CONJUNCTIONS]
        if len(substantive) >= 3:
            first_two = substantive[:2]
            for slug in ("".join(first_two), "-".join(first_two)):
                if slug and slug not in out:
                    out.append(slug)

        # First-token-only: some brands register just their first distinctive word and drop a generic
        # trailing descriptor entirely rather than joining it (e.g. "ZAABRA FRISOR AS" -> zaabra.no, not
        # zaabrafrisor.no -- "frisor"/"hairdresser" is the industry descriptor, not part of the brand).
        # Only tried when there are exactly 2 substantive tokens AND the second one is a common,
        # non-brand-distinctive trade/descriptor word (reusing GENERIC_WORDS plus a small set of trade
        # nouns) -- narrow enough that it doesn't fire on a genuine two-word brand name.
        if len(substantive) == 2 and substantive[1] in (GENERIC_WORDS | _TRADE_DESCRIPTOR_WORDS):
            first = substantive[0]
            if first and first not in out:
                out.append(first)
        return out

    variants = _variants_for(tokens)
    if alt_tokens != tokens:
        for slug in _variants_for(alt_tokens):
            if slug not in variants:
                variants.append(slug)

    # Drop overly generic results: a single token that is very short (<=3 chars, likely an
    # abbreviation/initialism with many unrelated registrants) or a known-generic industry/place word.
    # A distinctive short brand name (e.g. "adma", "rema", "kiwi") is kept.
    filtered = []
    for slug in variants:
        bare_tokens = slug.split("-")
        if len(bare_tokens) == 1 and (len(bare_tokens[0]) <= 3 or bare_tokens[0] in GENERIC_WORDS):
            continue
        filtered.append(slug)
    return filtered


# Common Norwegian trade/occupation nouns that routinely get dropped from a small business's own domain
# name -- the domain is just the brand/founder name (e.g. "Zaabra" alone), with the trade descriptor
# ("Frisor"/hairdresser) only in the legal name, not the URL. Deliberately narrow (occupation nouns for
# very small, single-location trades) -- NOT industry-sector words in general (those stay in GENERIC_WORDS
# and are never treated as droppable-second-token here to avoid over-triggering on genuine two-word brands).
_TRADE_DESCRIPTOR_WORDS = {
    "frisor", "frisoer", "salong", "frisorsalong", "frisoersalong", "barbershop", "barber",
}


def generate_candidates(ctx: Any) -> list[Candidate]:
    """Return ordered, domain-deduplicated candidates. Guesses are DNS-prefiltered when a client is present."""
    facts = registry_facts(ctx)
    tier = getattr(ctx, "tier", "T2")
    client = getattr(ctx, "client", None)
    caches = getattr(ctx, "caches", None)
    org = getattr(ctx, "org", None)

    candidates: list[Candidate] = []
    rank = 0

    # 1. Registry hjemmeside (registry_declared is decisive only after live/parked/conflict checks in verify.py).
    reg_site = facts.get("website")
    if reg_site:
        domain = registered_domain(reg_site)
        if domain:
            url = _normalize_url(reg_site)
            candidates.append(Candidate(domain, url, "registry_website", "registry hjemmeside", decisive=True, rank=rank))
            rank += 1

    # 2. Wikidata websites (decisive identity: tied to org number in the cache itself).
    wikidata = caches.wikidata.lookup(org) if caches is not None and getattr(caches, "wikidata", None) and org else None
    if wikidata:
        for site in wikidata.get("websites") or []:
            domain = registered_domain(site)
            if domain:
                candidates.append(Candidate(domain, site, "wikidata_website", "wikidata P856", decisive=True, rank=rank))
                rank += 1

    # 3. NAV employer homepage for ads tied to this org (decisive when the homepage domain matches).
    nav_ads = caches.nav.ads_for([org]) if caches is not None and getattr(caches, "nav", None) and org else []
    for ad in nav_ads or []:
        homepage = ad.get("employer_homepage")
        if homepage and str(ad.get("employer_orgnr") or "") == str(org):
            domain = registered_domain(homepage)
            if domain:
                candidates.append(Candidate(domain, homepage, "nav_employer_homepage", "NAV ad employer_homepage", decisive=True, rank=rank))
                rank += 1

    # 3b. Live NAV homepages published earlier THIS run by the activity/NAV connector
    # (ctx.shared["nav_homepages"]: list of {homepage, uuid}), already verified against our org number by
    # that connector before publishing. Same decisive source as the cache-based lookup above (verify.py
    # treats "nav_employer_homepage" as one decisive signal regardless of which one produced it) -- this
    # just covers a run where the live connector found something the offline `caches.nav` snapshot
    # didn't yet have. Falls back gracefully: an older orchestrator, or a run where that connector hasn't
    # run yet/found nothing, simply has no `nav_homepages` key.
    shared = getattr(ctx, "shared", None) or {}
    for entry in shared.get("nav_homepages") or []:
        homepage = entry.get("homepage") if isinstance(entry, dict) else None
        if not homepage:
            continue
        domain = registered_domain(homepage)
        if domain:
            candidates.append(Candidate(
                domain, homepage, "nav_employer_homepage",
                f"live NAV ad {entry.get('uuid', '')}".strip(), decisive=True, rank=rank,
            ))
            rank += 1

    # 3c. OpenStreetMap features tagged with our (or a subunit's) org number: keyed by org number, like Wikidata.
    places = getattr(caches, "places", None) if caches is not None else None
    sub_orgs = [str(s.get("organisation_number") or "") for s in facts.get("subunits") or []]
    if places is not None and org:
        for feat in places.osm_for([str(org)] + sub_orgs):
            domain = registered_domain(feat.get("website") or "")
            if domain:
                candidates.append(Candidate(
                    domain, _normalize_url(feat["website"]), "osm_orgnr_website",
                    f"OpenStreetMap {feat['osm_id']} ref:NO:orgnr={feat['orgnr']}", decisive=True, rank=rank,
                ))
                rank += 1

    # 4. Registry email domain, unless shared/freemail.
    email_domain = facts.get("email_domain")
    if email_domain:
        shared_domain = False
        if caches is not None and getattr(caches, "email_domains", None):
            shared_domain = caches.email_domains.is_shared(email_domain)
        if not shared_domain and email_domain not in _FREEMAIL_FALLBACK:
            domain = registered_domain(email_domain)
            if domain:
                candidates.append(Candidate(domain, _homepage_url(domain), "registry_email_domain", "registry email domain", decisive=False, rank=rank))
                rank += 1

    # 5. Subunit websites / email domains (labelled subunit-derived).
    for subunit in facts.get("subunits") or []:
        sub_site = subunit.get("website")
        if sub_site:
            domain = registered_domain(sub_site)
            if domain:
                url = _normalize_url(sub_site)
                candidates.append(Candidate(domain, url, "subunit_website", f"subunit {subunit.get('name', '')} website", decisive=False, rank=rank))
                rank += 1
        sub_email = subunit.get("email")
        if sub_email and "@" in sub_email:
            sub_domain_raw = sub_email.split("@")[-1]
            shared_domain = False
            if caches is not None and getattr(caches, "email_domains", None):
                shared_domain = caches.email_domains.is_shared(sub_domain_raw)
            if not shared_domain:
                domain = registered_domain(sub_domain_raw)
                if domain:
                    candidates.append(Candidate(domain, _homepage_url(domain), "subunit_email_domain", f"subunit {subunit.get('name', '')} email domain", decisive=False, rank=rank))
                    rank += 1

    # 5b. Open places dataset (Overture): a place carrying our registry phone/e-mail, or our name core at our
    # postcode, nominates its website. Strongest matches first (two keys before one); never decisive.
    if places is not None:
        phones = list(facts.get("phones") or [])
        emails = [facts.get("email")] if facts.get("email") else []
        for subunit in facts.get("subunits") or []:
            phones += list(subunit.get("phones") or [])
            if subunit.get("email"):
                emails.append(subunit["email"])
        matches = places.match(name=facts.get("name") or "", postcode=facts.get("postcode"), phones=phones, emails=emails)
        matches.sort(key=lambda m: (-len(m["how"]), m["id"]))
        for m in matches[:OPEN_PLACES_MAX_MATCHES]:
            for site in m["websites"][:2]:
                domain = registered_domain(site)
                if domain:
                    candidates.append(Candidate(
                        domain, _normalize_url(site), "open_places",
                        f"Overture place {m['id']} ({m['name']}) matched on {'+'.join(m['how'])}",
                        decisive=False, rank=rank, hints=tuple(m["how"]),
                    ))
                    rank += 1

    # 6. Name-based guesses: legal name, historic names (aliases), subunit trade names.
    # T0 gets a much smaller cap (T0_MAX_NAME_GUESSES) rather than being skipped outright: a company
    # lands in T0 whenever the bulk row shows no staff AND no site/e-mail domain signal, which includes
    # plenty of ordinary small staffed businesses whose employee count is simply missing/blank in the
    # bulk file (not actually zero) -- e.g. a one-location hairdresser or a holding-less small AS. Since
    # every guess here is DNS-prefiltered before any HTTP request and verify.assess() is the only thing
    # that can ever turn a guess into a published `exact` (this only affects what gets TRIED, never what
    # gets trusted), a couple of cheap guesses for T0 costs at most ~2 extra requests per T0 company with
    # zero precision risk -- pure recall for the "actually small, just under-described-in-bulk" case.
    guess_cap = MAX_NAME_GUESSES if tier != "T0" else T0_MAX_NAME_GUESSES
    names = [facts.get("name")] + list(facts.get("aliases") or [])
    for subunit in facts.get("subunits") or []:
        if subunit.get("name"):
            names.append(subunit["name"])
    guess_count = 0
    for name in names:
        if not name or guess_count >= guess_cap:
            break
        for slug in _name_guess_slugs(name):
            if guess_count >= guess_cap:
                break
            for tld in (".no", ".com"):
                domain = f"{slug}{tld}"
                if client is not None and hasattr(client, "dns_resolves") and not client.dns_resolves(domain):
                    continue
                candidates.append(Candidate(domain, _homepage_url(domain), "name_guess", f"name guess from '{name}'", decisive=False, rank=rank))
                rank += 1
                guess_count += 1
                if guess_count >= guess_cap:
                    break

    return _dedupe(candidates)


_FREEMAIL_FALLBACK = {
    "gmail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "icloud.com", "me.com",
}
