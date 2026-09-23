"""Ordered, deduplicated website candidate generation.

Cheapest / most decisive sources first (see docs/PLAN.md §5.2 and BUILD_SPEC.md "Identity rules"):
registry hjemmeside -> Wikidata websites -> NAV employer homepage -> registry email domain ->
subunit websites/emails -> name-guessed slugs. DNS-prefilter guesses before any HTTP request.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
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

# Legal-form / generic suffix tokens stripped when building a slug variant "without suffix words".
SUFFIX_WORDS = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen", "nuf",
    "holding", "eiendom", "eiendommer", "norge", "norway", "group", "gruppen", "invest",
}

MAX_NAME_GUESSES = 4


def _slug_tokens(value: str) -> list[str]:
    text = str(value or "").translate(
        str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"})
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


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    ordered: list[Candidate] = []
    for cand in candidates:
        if not cand.domain or cand.domain in seen:
            continue
        if is_marketplace_or_directory(cand.domain):
            # Directories, booking/scheduling platforms and generic site-builder hosts are never a
            # company's own website, even when the registry or a subunit happens to list one (e.g. a
            # booking-platform URL entered as "hjemmeside" by mistake, or an email address on a free
            # site-builder subdomain). BUILD_SPEC.md: directories aren't evidence.
            continue
        seen.add(cand.domain)
        ordered.append(cand)
    return ordered


def registry_facts(ctx: Any) -> dict[str, Any]:
    shared = getattr(ctx, "shared", {}) or {}
    facts = shared.get("registry_facts")
    if facts:
        return facts
    bulk = getattr(ctx, "bulk", {}) or {}
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


def _name_guess_slugs(name: str) -> list[str]:
    """Up to 2 base slugs (joined, hyphenated), each tried with and without suffix words removed."""
    tokens = _slug_tokens(name)
    if not tokens:
        return []
    stripped = [t for t in tokens if t not in SUFFIX_WORDS] or tokens
    variants: list[str] = []
    for toks in (stripped, tokens):
        if not toks:
            continue
        joined = "".join(toks)
        hyphenated = "-".join(toks)
        for slug in (joined, hyphenated):
            if slug and slug not in variants:
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

    # 6. Name-based guesses: legal name, historic names (aliases), subunit trade names. Skip for T0.
    if tier != "T0":
        names = [facts.get("name")] + list(facts.get("aliases") or [])
        for subunit in facts.get("subunits") or []:
            if subunit.get("name"):
                names.append(subunit["name"])
        guess_count = 0
        for name in names:
            if not name or guess_count >= MAX_NAME_GUESSES:
                break
            for slug in _name_guess_slugs(name):
                if guess_count >= MAX_NAME_GUESSES:
                    break
                for tld in (".no", ".com"):
                    domain = f"{slug}{tld}"
                    if client is not None and hasattr(client, "dns_resolves") and not client.dns_resolves(domain):
                        continue
                    candidates.append(Candidate(domain, _homepage_url(domain), "name_guess", f"name guess from '{name}'", decisive=False, rank=rank))
                    rank += 1
                    guess_count += 1
                    if guess_count >= MAX_NAME_GUESSES:
                        break

    return _dedupe(candidates)


_FREEMAIL_FALLBACK = {
    "gmail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "icloud.com", "me.com",
}
