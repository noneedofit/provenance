"""Identity resolution: is this candidate site really the company's own site?

Pure functions, no network. BUILD_SPEC.md "Identity rules" is the contract this module implements:

- `exact` requires ONE decisive signal (org number on any fetched page, JSON-LD vatID/taxID/identifier,
  Wikidata P2333 match, NAV ad org-number+homepage match, or a live/not-parked/unconflicted registry
  `hjemmeside`) OR >=2 independent corroborating signals with no conflict.
- A different valid org number shown prominently is a conflict -> never `exact`; becomes `related` with a
  relationship (parent/franchise/brand/service_provider).
- A registry-declared domain used as `website` by >=3 organisations is a shared/parent site -> `related`,
  never `exact`.
- A registry-declared domain whose content shows no link to the company (casino/gambling/adult/pharma
  spam, or a generic "domain for sale" page) is treated as hijacked or parked -> `rejected`, never `exact`
  -- registry_declared is not decisive on its own for a topically unrelated page.

Precision over recall: when unsure, this module returns `ambiguous` or `rejected`, never `exact`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from ..text import fold
from .blocklist import is_franchise_chain_domain
from .candidates import Candidate, registered_domain
from .crawl import PageFetch, is_parked_page

Status = Literal["exact", "related", "ambiguous", "rejected"]
Relationship = Literal["parent", "subsidiary", "franchise", "brand", "service_provider"]

# --- Norwegian organisation-number detection -------------------------------------------------------------

# A run of 9 digits, allowing spaces/dots/non-breaking-spaces between groups (e.g. "979 543 883",
# "979.543.883", "979543883"), optionally preceded by "NO" and/or followed by "MVA".
_ORGNR_CANDIDATE_RE = re.compile(
    r"(?:\bNO[\s.]?)?\b(\d[\d\s. ]{7,15}\d)\b(?:\s?MVA\b)?", re.IGNORECASE
)
_ORGNR_LABEL_RE = re.compile(
    r"(org(?:anisasjonsnummer|\.?\s*nr)\.?\s*[:.]?\s*|eies\s+av\s+|belongs\s+to\s+(?:org\s+)?)"
    r"(\d[\d\s. ]{7,15}\d)",
    re.IGNORECASE,
)

_MOD11_WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)


def is_valid_orgnr(digits: str) -> bool:
    """Mod-11 check digit validation for a 9-digit Norwegian organisation number."""
    if len(digits) != 9 or not digits.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(digits[:8], _MOD11_WEIGHTS, strict=True))  # 9th digit is the check digit
    remainder = total % 11
    check = 0 if remainder == 0 else 11 - remainder
    if check == 10:
        return False
    return check == int(digits[8])


@dataclass
class OrgNumberMatch:
    digits: str
    span: str
    labeled: bool  # preceded by "org.nr" / "organisasjonsnummer" etc.


def find_org_numbers(text: str) -> list[OrgNumberMatch]:
    """Find valid (mod-11) 9-digit organisation numbers in free text, with the matched span."""
    if not text:
        return []
    found: dict[str, OrgNumberMatch] = {}
    for match in _ORGNR_LABEL_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(2))
        if is_valid_orgnr(digits):
            found[digits] = OrgNumberMatch(digits, match.group(0).strip()[:200], labeled=True)
    for match in _ORGNR_CANDIDATE_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(1))
        if is_valid_orgnr(digits) and digits not in found:
            found[digits] = OrgNumberMatch(digits, match.group(0).strip()[:200], labeled=False)
    return list(found.values())


def find_org_number_in_jsonld(value: Any) -> list[OrgNumberMatch]:
    """Walk a JSON-LD structure looking for vatID / taxID / identifier / duns-style org-number fields."""
    matches: list[OrgNumberMatch] = []
    keys = {"vatid", "taxid", "identifier", "orgnr", "organizationnumber", "organisationnumber"}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key.lower() in keys and isinstance(val, (str, int)):
                    digits = re.sub(r"\D", "", str(val))
                    if is_valid_orgnr(digits):
                        matches.append(OrgNumberMatch(digits, f"{key}: {val}"[:200], labeled=True))
                walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return matches


# --- Parked / conflict helpers ---------------------------------------------------------------------------


SMALL_SHARED_DOMAIN_MAX = 3

def _normalize(text: str) -> str:
    return fold(text)



# A registered domain can lapse and be re-registered by an unrelated party (casino/gambling, adult,
# pharma-spam affiliate content is the common case for expired Norwegian small-business domains). A
# registry-declared `hjemmeside` pointing at content like this must never be trusted as decisive.
HIJACK_MARKERS = (
    "casino", "jackpot", "slot machine", "free spins", "no deposit bonus", "online betting",
    "sportsbook", "poker room", "bet now", "play now and win", "best online casino",
    "viagra", "cialis", "online pharmacy", "cheap pharmacy", "buy pills online",
    "adult content", "xxx video", "escort service", "webcam girls",
)


# Word-boundary matching, not bare substring: a naive `marker in haystack` check false-positived on
# CALPRO AS's own page (a medical diagnostics company) because "cialis" is a substring of "IBD Nurse
# SpeCIALISt" -- a real gold-set recall bug (correctly registry_declared calpro.no was rejected as
# "hijacked"). \b boundaries apply to the whole matched phrase, so multi-word markers like "no deposit
# bonus" still require a boundary before "no" and after "bonus", not just anywhere inside a longer word.
_HIJACK_MARKER_RE = re.compile(r"\b(?:" + "|".join(re.escape(m) for m in HIJACK_MARKERS) + r")\b")


def is_hijacked_content(text: str) -> bool:
    haystack = _normalize(text or "")
    return bool(_HIJACK_MARKER_RE.search(haystack))


# A name-derived guess can land on an unrelated, unaffiliated business that happens to share a common
# word with our legal name -- the JOKER AS trap: JOKER AS (983043291) is a sea-fishing company on
# Vaeroy (NACE division 03, fishing), while joker.no is the grocery chain (NACE division 47). The chain's
# own corporate domain is separately gated via FRANCHISE_CHAIN_DOMAINS, but the same "common word, wrong
# industry" trap can hit any *unlisted* domain a name-guess happens to resolve to. This maps a NACE
# division to a small set of Norwegian keyword clusters and flags a site whose text strongly signals a
# *different* cluster than our own division -- conservative (>=2 distinct keyword hits) so it only fires
# on pages that clearly describe a different line of business, not on incidental word overlap.
_NACE_INDUSTRY_CLUSTERS: dict[str, str] = {
    "01": "agriculture", "02": "forestry", "03": "fishing",
    "10": "food_production", "11": "beverages",
    "41": "construction", "42": "construction", "43": "construction",
    "45": "vehicle_trade", "46": "wholesale", "47": "grocery_retail",
    "49": "transport", "50": "transport", "51": "transport", "52": "transport",
    "55": "hospitality", "56": "restaurants",
    "62": "software", "63": "software",
    "64": "finance", "65": "finance", "66": "finance",
    "68": "real_estate",
    "84": "public_admin", "85": "education", "86": "healthcare", "87": "healthcare", "88": "healthcare",
    "93": "sports_fitness",
}

_INDUSTRY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fishing": ("fiskebat", "fiskefartoy", "fiskerikvote", "fangst av fisk", "sjomat", "fiskekvote", "garnfiske", "trålfiske"),
    "grocery_retail": ("dagligvare", "matbutikk", "supermarked", "kjedebutikk", "ukens tilbud", "handlekurv"),
    "restaurants": ("bordbestilling restaurant", "restaurantmeny", "takeaway", "bestill mat na"),
    "hospitality": ("hotellrom", "overnatting", "bestill rom", "ledige rom", "resepsjon hotell"),
    "sports_fitness": ("treningssenter", "medlemskap trening", "gruppetimer", "personlig trener"),
    "software": ("programvare", "saas platform", "api dokumentasjon", "skylosning"),
    "finance": ("bankkonto", "laan og kreditt", "forsikringsvilkar", "sparekonto"),
    "healthcare": ("legetime", "pasientjournal", "helsetjeneste", "fastlege"),
}


def industry_mismatch(nace_code: str | None, site_text: str) -> str | None:
    """Return the mismatched industry cluster name if `site_text` strongly signals an industry different
    from `nace_code`'s NACE division, else None. Unknown/missing NACE code -> never flags (no false guard)."""
    our_cluster = _NACE_INDUSTRY_CLUSTERS.get(str(nace_code or "").strip()[:2])
    if not our_cluster:
        return None
    haystack = _normalize(site_text)
    for cluster, keywords in _INDUSTRY_KEYWORDS.items():
        if cluster == our_cluster:
            continue
        hits = sum(1 for kw in keywords if _normalize(kw) in haystack)
        if hits >= 2:
            return cluster
    return None


_DIGITS_8_RE = re.compile(r"(?<!\d)(\d[\d\s]{6,10}\d)(?!\d)")


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("47") and len(digits) == 10:
        digits = digits[2:]
    return digits


def find_phones(text: str) -> set[str]:
    return set(phones_as_written(text))


def phones_as_written(text: str) -> dict[str, str]:
    """8-digit number -> the first way the page writes it ("38 26 61 11"), so evidence quotes the page."""
    found: dict[str, str] = {}
    for match in _DIGITS_8_RE.finditer(text or ""):
        digits = _normalize_phone(match.group(1))
        if len(digits) == 8:
            found.setdefault(digits, match.group(1).strip())
    return found


# --- Signals / conflicts / verdict ------------------------------------------------------------------------


@dataclass
class Signal:
    kind: str            # e.g. "org_number_on_source", "jsonld_org_number", "registered_address",
                          # "registry_phone", "registry_email", "role_name", "legal_name_match", "wikidata",
                          # "nav_employer_homepage", "registry_declared"
    detail: str
    page_url: str
    span: str


@dataclass
class Conflict:
    kind: str             # "conflicting_org_number"
    detail: str
    org_number: str
    page_url: str
    span: str


@dataclass
class Verdict:
    status: Status
    identity_basis: str | None
    relationship: Relationship | None
    signals: list[Signal] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    note: str = ""


_FRANCHISE_WORDS = ("franchise", "franchisetaker", "kjede", "forhandler", "chain store", "store locator")
_PARENT_WORDS = ("konsern", "morselskap", "group companies", "our brands", "our stores", "housing manager", "forretningsforer")


def _infer_relationship(our_name: str, site_text: str) -> Relationship:
    haystack = _normalize(site_text)
    if any(word in haystack for word in _FRANCHISE_WORDS) and _normalize(our_name) and _normalize(our_name) in haystack:
        return "franchise"
    if any(word in haystack for word in _PARENT_WORDS):
        return "parent"
    return "brand"


# A lone trade word is not a distinctive name (bygg.no, transport.no belong to someone else).
GENERIC_NAME_WORDS = {
    "bygg", "transport", "eiendom", "holding", "invest", "service", "consulting", "elektro", "handel",
    "regnskap", "maskin", "entreprenor", "renhold", "tannlege", "frisor", "byggservice", "eiendomsservice",
}


def _legal_name_core(name: str) -> set[str]:
    text = _normalize(str(name or ""))
    suffixes = {"as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen", "nuf"}
    return {tok for tok in re.findall(r"[a-z0-9]+", text) if tok not in suffixes and len(tok) > 1}


def _page_all_text(page: PageFetch) -> str:
    """Visible page text for identity matching: title + trafilatura's extracted text + every visible
    text node in the raw html (script/style stripped), via BeautifulSoup get_text() -- deliberately NOT
    raw html. Scanning raw html/attributes let a substring match fire inside a URL or filename that
    merely happens to contain matching digits/street-name text: a real false positive found on a
    namesake site (DACON SERVICES AS, a different company than the gold DACON... AS) whose page never
    visibly mentions our registered address at all, but has an image at
    "/uploads/Durudveien_1600_500....jpg" -- our registered street name, coincidentally, only in a
    filename. get_text() gives every VISIBLE text node (broader than trafilatura's narrower "main
    content" extraction, which can also miss a footer address entirely -- confirmed on GULLSMED FJELL AVD.
    VOLLEN AS's page, where trafilatura returned empty text but get_text() found the address fine)
    without attribute/URL noise. No size cap: a modern page-builder site can be several hundred KB and
    real evidence routinely sits past any small fixed prefix; this is pure CPU work, not a network
    request, so there's no request-budget cost. Memoized on the PageFetch instance -- `assess()` calls
    this several times per page within one verdict.
    """
    cached = getattr(page, "_identity_text_cache", None)
    if cached is not None:
        return cached
    html = page.html or ""
    visible = html
    if html:
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style"]):
                tag.decompose()
            visible = soup.get_text(" ", strip=True)
        except Exception:
            pass
    result = f"{page.title}\n{page.text}\n{visible}"
    try:
        page._identity_text_cache = result
    except Exception:
        pass
    return result


# --- Site-owner name detection (footer copyright / JSON-LD legalName) -------------------------------------
# A registry_declared candidate whose page names a DIFFERENT legal entity as the site's owner (a group's
# shared product/marketing site crediting only the parent/a sibling in its footer or JSON-LD, e.g. Xledger
# Labs AS's registry hjemmeside pointing at xledger.com, whose footer/JSON-LD only ever say "Xledger AS")
# must not get the bare "live, not parked, unconflicted" registry_declared trust -- this is a weaker, softer
# signal than a conflicting ORG NUMBER (no digits involved, so it never escalates to `related` on its own
# the way a conflicting org number does), but it disqualifies the free pass.

_COPYRIGHT_OWNER_RE = re.compile(
    r"(?:©|\(c\)|copyright)\s*(?:\d{4}\s*[-–]?\s*(?:\d{4})?\s*)?"
    r"([A-Za-zÆØÅæøå][A-Za-zÆØÅæøå0-9&.,'\-\s]{1,60}?)(?=\s*[.|\n]|\s{2,}|$)"
)


# Legal-form tokens that make a copyright-line match look like an actual registered company rather than
# a web designer / theme-vendor credit line ("(c) 2020 Daniel Eden" -- a real false positive found while
# scanning a full page: a personal name near an unrelated "copyright"-shaped string on a WordPress theme
# credit). Broader than `_legal_name_core`'s Norwegian-only suffix set since a copyright line can name a
# non-Norwegian vendor too; the point here is only "does this look like a company at all", not matching.
_COMPANY_SUFFIX_TOKENS = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen", "nuf",
    "ab", "oy", "aps", "ltd", "llc", "inc", "gmbh", "plc", "spa", "srl", "nv", "bv", "co", "corp",
}


def _looks_like_company_name(name: str) -> bool:
    tokens = {t for t in re.findall(r"[a-z0-9]+", _normalize(name))}
    return bool(tokens & _COMPANY_SUFFIX_TOKENS)


def find_copyright_owners(text: str) -> list[str]:
    """All copyright-line owner names on a page that look like an actual company (carry a legal-form
    suffix token), not just the first -- a page can legitimately carry more than one (e.g. a shared
    booking-widget/template vendor's own footer copyright line ABOVE the site's own "Copyright (c) ...
    Four Season Spa AS" line further down). Returning only the first match would flag a mismatch off the
    vendor's name and miss our own, correct one lower on the page. Filtering to names that look like a
    company (rather than every "(c) <text>"-shaped string) avoids false-flagging a web designer / theme
    credit line ("(c) 2020 Daniel Eden") as a conflicting site owner."""
    return [
        m.group(1).strip()
        for m in _COPYRIGHT_OWNER_RE.finditer(text or "")
        if m.group(1).strip() and _looks_like_company_name(m.group(1))
    ]


def _find_jsonld_legal_names(nodes: Any) -> list[str]:
    names: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            kind = node.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & {"Organization", "Corporation", "LocalBusiness"}:
                for key in ("legalName", "name"):
                    value = node.get(key)
                    if isinstance(value, str) and value.strip():
                        names.append(value.strip())
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(nodes)
    return names


_OWNER_BOILERPLATE = {
    "all", "rights", "reserved", "copyright", "alle", "rettigheter", "forbeholdt", "c", "by", "av",
}


def site_owner_mismatch(our_name: str, owner_names: list[str]) -> str | None:
    """Return an owner name if NONE of `owner_names` matches `our_name`'s legal-name core, or None if at
    least one does (or no owner name was found at all -- absence is not a mismatch). A page can name
    more than one entity (a template/booking-widget vendor's own footer line, e.g. "Destino AS", ABOVE
    the site's own "Four Season Spa AS" copyright line) -- only flag a mismatch when our own name is
    named NOWHERE on the page, not merely when it isn't the first name found."""
    our_core = _legal_name_core(our_name)
    if not our_core:
        return None
    named_others: list[str] = []
    for owner in owner_names:
        owner_core = _legal_name_core(owner)
        if not owner_core:
            continue
        if our_core <= owner_core and not (owner_core - our_core - _OWNER_BOILERPLATE):
            # The owner text is our name plus copyright boilerplate ("All Rights Reserved Rælingen El
            # Installasjon AS" for RÆLINGEN EL-INSTALLASJON AS). A different entity either lacks one of our
            # words ("Xledger AS" for XLEDGER LABS AS) or adds its own ("Acme Group AS" for ACME AS).
            return None
        named_others.append(owner)
    return named_others[0] if named_others else None


def assess(
    org_number: str,
    pages: list[PageFetch],
    registry_facts: dict[str, Any],
    candidate: Candidate,
    *,
    website_org_count: int | None = None,
    require_decisive: bool = False,
) -> Verdict:
    """Assess whether `pages` (from `candidate`) belong to the company identified by `org_number`.

    `require_decisive`: the registry itself declared a `hjemmeside` for this org, but connector.py
    couldn't confirm THAT site (unreachable/rejected/ambiguous) before trying this candidate. A
    namesake/coincidence trap found on the gold set: DACON SERVICES AS's registry-declared
    dacon-inspection.no had a persistent SSL error, and a name-guessed dacon-services.no turned out to
    show a genuinely matching address+phone on its contact page too (multi-domain business, a
    predecessor/successor entity, or pure coincidence -- not distinguishable from page content alone).
    With the authoritative registry-declared site itself unconfirmed, a competing candidate needs a
    decisive signal (org number, Wikidata, NAV) to reach `exact` here -- 2-signal corroboration alone
    is downgraded to `ambiguous`.
    """
    live_pages = [p for p in pages if p.ok]
    if not live_pages:
        return Verdict("rejected", None, None, note="no page could be fetched")

    homepage = next((p for p in pages if p.page_kind == "homepage"), pages[0])
    if homepage.ok and is_parked_page(homepage.html, homepage.text, homepage.final_url):
        return Verdict("rejected", None, None, note="homepage is a parked/for-sale placeholder")

    signals: list[Signal] = []
    our_digits = re.sub(r"\D", "", str(org_number or ""))

    # --- org-number scan across all fetched pages (regex text + JSON-LD) ---
    # A group/holding/investor-relations page routinely lists SEVERAL org numbers (subsidiaries, board
    # members' other directorships, a parent entity) alongside our own -- e.g. Kitron/Mowi/AF Gruppen's
    # corporate sites, or DNT Nord-Trondelag's page on the shared dnt.no domain. Our own org number
    # appearing anywhere on the page is decisive ON ITS OWN, regardless of what else is also on the page.
    # A DIFFERENT org number is only a genuine conflict (this site belongs to someone else, not merely
    # "also mentions" someone else) when OUR number is absent from the page AND the other number is
    # presented as THIS site's owner: a labelled match ("Org.nr:", "organisasjonsnummer", "eies av",
    # "belongs to org" -- see the broadened _ORGNR_LABEL_RE) or a JSON-LD organisation identifier. A bare
    # unlabelled 9-digit run elsewhere on the page (a subsidiary in a list, an unrelated reference number
    # that happens to pass the mod-11 check) is not strong enough evidence of ownership and is discarded.
    other_org_matches: list[Conflict] = []
    site_owner_names: list[str] = []
    for page in live_pages:
        for m in find_org_numbers(_page_all_text(page)):
            if m.digits == our_digits:
                signals.append(Signal("org_number_on_source", "organisation number found on site", page.final_url, m.span))
            elif m.labeled:
                other_org_matches.append(Conflict("conflicting_org_number", "different valid organisation number presented as site owner", m.digits, page.final_url, m.span))
        site_owner_names.extend(find_copyright_owners(_page_all_text(page)))
        try:
            import extruct

            data = extruct.extract(page.html, base_url=page.final_url, syntaxes=["json-ld"])
            jsonld_nodes = data.get("json-ld", []) or []
            for m in find_org_number_in_jsonld(jsonld_nodes):
                if m.digits == our_digits:
                    signals.append(Signal("jsonld_org_number", "JSON-LD identifier matches organisation number", page.final_url, m.span))
                else:
                    other_org_matches.append(Conflict("conflicting_org_number", "JSON-LD identifier is a different organisation number", m.digits, page.final_url, m.span))
            site_owner_names.extend(_find_jsonld_legal_names(jsonld_nodes))
        except Exception:
            pass

    has_our_org_number = any(s.kind in {"org_number_on_source", "jsonld_org_number"} for s in signals)
    conflicts: list[Conflict] = [] if has_our_org_number else other_org_matches

    # --- decisive non-textual sources ---
    if candidate.source == "wikidata_website" and not conflicts:
        signals.append(Signal("wikidata", "candidate sourced from Wikidata entry for this organisation number", homepage.final_url, candidate.label))
    if candidate.source == "nav_employer_homepage" and not conflicts:
        signals.append(Signal("nav_employer_homepage", "NAV job ad for this org number links this homepage", homepage.final_url, candidate.label))
    if candidate.source == "osm_orgnr_website" and not conflicts:
        signals.append(Signal("osm_orgnr", "OpenStreetMap feature tagged with this organisation number links this site", homepage.final_url, candidate.label))

    decisive_kinds = {"org_number_on_source", "jsonld_org_number", "wikidata", "nav_employer_homepage", "osm_orgnr"}
    has_decisive = any(s.kind in decisive_kinds for s in signals)

    # A domain can be re-registered after the company let it lapse and now serves unrelated spam
    # (casino/adult/pharma affiliate content is the common pattern). Content topically unrelated to the
    # company overrides a bare registry_declared / parked-style trust — never decisive on its own.
    hijacked = any(is_hijacked_content(_page_all_text(p)) for p in live_pages)

    # A national chain/franchisor's own corporate domain (joker.no, kiwi.no, thon.no, ...). A franchisee
    # "find your store" entry on that domain routinely carries the exact franchisee address/phone/name,
    # which would otherwise satisfy the >=2-corroborating-signal exact rule. Corroboration and bare
    # registry_declared trust are disabled here; only a literal org-number match can still produce exact
    # (e.g. when assessing the chain's own headquarters entity against its own domain).
    is_chain_domain = is_franchise_chain_domain(candidate.domain)

    # --- corroborating signals (only meaningful when no conflict) ---
    if not conflicts:
        joined_text = "\n".join(_page_all_text(p) for p in live_pages)
        norm_joined = _normalize(joined_text)
        page_texts = [(p, _page_all_text(p)) for p in live_pages]

        def found_on(test: Callable[[str], bool]) -> str:
            """URL of the first fetched page whose own text passes `test` (evidence must cite that page)."""
            for page, text in page_texts:
                if test(text):
                    return page.final_url
            return homepage.final_url

        street = registry_facts.get("street")
        postcode = registry_facts.get("postcode")
        if street and postcode and _normalize(street) in norm_joined and _normalize(postcode) in norm_joined:
            where = found_on(lambda t: _normalize(street) in _normalize(t) and _normalize(postcode) in _normalize(t))
            signals.append(Signal("registered_address", "registered street and postcode found on site", where, f"{street} {postcode}"))

        site_phones = find_phones(joined_text)
        for phone in registry_facts.get("phones") or []:
            normalized = _normalize_phone(str(phone))
            if len(normalized) == 8 and normalized in site_phones:
                where = found_on(lambda t, n=normalized: n in find_phones(t))
                written = phones_as_written(next((t for p, t in page_texts if p.final_url == where), joined_text))
                signals.append(Signal("registry_phone", "registry phone number found on site", where, written.get(normalized, normalized)))
                break

        email = registry_facts.get("email")
        if email and _normalize(email) in norm_joined:
            where = found_on(lambda t: _normalize(email) in _normalize(t))
            signals.append(Signal("registry_email", "registry email address found on site", where, email))

        role_persons = registry_facts.get("role_holders") or registry_facts.get("role_names") or []
        for person in role_persons:
            if person and _normalize(person) in norm_joined:
                where = found_on(lambda t, n=person: _normalize(n) in _normalize(t))
                signals.append(Signal("role_name", "registered CEO/board member name found on site", where, person))
                break

        legal_core = _legal_name_core(registry_facts.get("name") or "")
        title_tokens = set(re.findall(r"[a-z0-9]+", _normalize(homepage.title)))
        if legal_core and legal_core.issubset(title_tokens):
            signals.append(Signal("legal_name_match", "exact legal name (minus suffix) found in <title>", homepage.final_url, homepage.title))

        # An open places dataset lists this site for a place that carries our registry phone or e-mail. Strong
        # only when that place also bears our name core (phone/e-mail alone can be a property manager's or an
        # accountant's switchboard shared by many companies).
        hints = set(getattr(candidate, "hints", ()) or ())
        if hints & {"phone", "email"}:
            kind = "open_places_contact_named" if "name" in hints else "open_places_contact"
            signals.append(Signal(kind, f"open places dataset lists this site for a place matching our {'/'.join(sorted(hints))}", homepage.final_url, candidate.label))

        # The domain is the company's full legal name (minus legal-form words), e.g. "THE MICE GURU AS" ->
        # themiceguru.com. Only a distinctive name counts: long enough, not a lone generic trade word.
        legal_core_tokens = _legal_name_core(registry_facts.get("name") or "")
        ordered_core = [t for t in re.findall(r"[a-z0-9]+", _normalize(registry_facts.get("name") or "")) if t in legal_core_tokens]
        slug = "".join(ordered_core)
        label = (candidate.domain or "").rsplit(".", 1)[0].replace("-", "")
        if slug and label == slug and len(slug) >= 8 and slug not in GENERIC_NAME_WORDS:
            signals.append(Signal("domain_name_match", "site domain is the company's full legal name", homepage.final_url, candidate.domain))

        # The registry's own e-mail address for this company is at this domain (domain not shared by other
        # organisations: shared/freemail domains never become candidates). A registered fact, independent of
        # anything the page says.
        if candidate.source == "registry_email_domain":
            signals.append(Signal("registry_email_domain", "registry e-mail address for this organisation uses this domain", homepage.final_url, candidate.domain))

        # The registered e-mail's domain being the SAME domain we're assessing is a structural signal
        # independent of page content (a company almost never uses someone else's domain for its own
        # e-mail address) -- helps a name-guess candidate that a registry_email_domain candidate would
        # already carry decisively, but also a candidate reached some other way (e.g. Wikidata, a
        # subunit website) that happens to share the registered e-mail's domain.
        # Excludes candidate.source == "registry_email_domain": for that source the candidate's domain
        # IS the registered e-mail's domain by construction (that's how the candidate was generated), so
        # the match is tautological, not independent confirmation. For every other source (a name guess,
        # the registry hjemmeside, Wikidata, a subunit site, ...) landing on the same domain the
        # registered e-mail uses is a genuine, independent structural signal.
        email_domain = (registry_facts.get("email_domain") or "").strip().lower()
        if email_domain and email_domain == candidate.domain and candidate.source != "registry_email_domain":
            signals.append(Signal("email_domain_match", "registered e-mail domain matches this site's domain", homepage.final_url, email_domain))

    corroborating_kinds = {
        "registered_address", "registry_phone", "registry_email", "role_name", "legal_name_match",
        "email_domain_match", "open_places_contact", "open_places_contact_named", "domain_name_match",
        "registry_email_domain",
    }
    corroborating = [s for s in signals if s.kind in corroborating_kinds]
    distinct_corroborating = {s.kind for s in corroborating}
    # "Hard" signals require matching a specific, hard-to-coincidentally-satisfy piece of registry data
    # (our exact legal name in <title>, the registry e-mail text itself, or the e-mail's domain). "Soft"
    # signals (address, phone, a board member's name) are page CONTENT that can genuinely, coincidentally
    # match on a page that isn't entity-specific -- a shared office after an acquisition, a former
    # tenant's address, a person who moved employer. See the name_guess guard below.
    STRONG_CORROBORATING_KINDS = {
        "legal_name_match", "registry_email", "email_domain_match", "open_places_contact_named", "domain_name_match",
    }
    # The name in the <title> and the name as the domain are one fact (the company's name), not two: a
    # namesake (ottokoch.com, a German chef; activepartner.no, a same-named company) shows both. Count them
    # as one dimension, so a name always needs an independent registry fact (address, phone, e-mail, role
    # holder, open places contact) next to it.
    NAME_KINDS = {"legal_name_match", "domain_name_match"}
    distinct_corroborating = {("name" if k in NAME_KINDS else k) for k in distinct_corroborating}
    distinct_corroborating_raw = {s.kind for s in corroborating}

    # A parent/umbrella or franchise/chain page (group site, "our brands", "our stores", housing
    # manager, franchise/chain wording) is exactly the "flagged as a franchise/parent site" carve-out in
    # BUILD_SPEC.md's registry_declared rule (samfundet.no for Samfundets Stotter AS, hav.no for HAV
    # Chartering AS, assemblin.com, bilfinger.com, ...): a registry hjemmeside pointing at one of these
    # is decisive on its own ONLY when it also carries at least one independent corroborating signal
    # specific to this org (address/phone/email/role holder/exact legal name) -- otherwise it is a
    # generic shared site and the bare "live, not parked, unconflicted" fact is not enough.
    umbrella_wording = any(
        word in _normalize("\n".join(_page_all_text(p) for p in live_pages)) for word in (_FRANCHISE_WORDS + _PARENT_WORDS)
    )

    # A different legal entity named as the site's owner (footer "(c) Xledger AS" / JSON-LD legalName)
    # while our own registered name is a different entity (e.g. "Xledger Labs AS") is the XLEDGER LABS AS
    # trap: xledger.com is Xledger's shared group/product site, and no page on it ever names Xledger Labs
    # AS specifically. This is a softer signal than a conflicting ORG NUMBER (no digits, so it never
    # escalates the verdict to `related` the way `conflicts` does) -- it only disqualifies the bare
    # registry_declared free pass, the same way umbrella wording does.
    owner_name_mismatch = site_owner_mismatch(registry_facts.get("name") or "", site_owner_names)

    # A registry-declared domain that forwards WHOLESALE to a different registered domain (dacon-
    # inspection.no -> dacon-services.no after a rebrand/consolidation) is a genuinely weaker signal than
    # one that stays on its own domain: the registry only vouches for the domain it named, not for
    # wherever that domain's current owner happens to point it today (a lapsed/resold domain forwarding
    # to an unrelated party would look identical from here). Same-domain hops (bare apex -> www, a path
    # redirect) are unaffected -- `registered_domain()` normalizes both to the same apex.
    redirected_off_domain = registered_domain(homepage.final_url or "") != candidate.domain

    # --- registry_declared: only decisive if live, not parked (already checked), no conflict, not shared
    # (>=2 organisations also registered on this domain -- a sibling/parent entity, not just "any old
    # coincidence"), not a known franchise/chain domain, not naming a different legal entity as the site's
    # owner, not reading as a parent/umbrella site with zero org-specific corroboration, not showing
    # content topically unrelated to the company (hijacked/re-registered domain), and not redirecting
    # wholesale to a different registered domain ---
    registry_declared_ok = False
    shared_domain = website_org_count is not None and website_org_count >= 2
    if candidate.source == "registry_website" and not conflicts and not hijacked and not is_chain_domain:
        unsupported_umbrella = umbrella_wording and not distinct_corroborating
        if not shared_domain and not unsupported_umbrella and not owner_name_mismatch and not redirected_off_domain:
            registry_declared_ok = True
            signals.append(Signal("registry_declared", "registry-listed hjemmeside is live and unconflicted", homepage.final_url, candidate.url))

    # A page that reads as hijacked/re-registered spam must never reach `exact` off corroboration alone,
    # even if a stale cached fragment of the old site's address happens to still be present.
    mismatched_industry = None
    if not has_decisive and not registry_declared_ok and not hijacked:
        joined_text = "\n".join(_page_all_text(p) for p in live_pages)
        mismatched_industry = industry_mismatch(registry_facts.get("nace"), joined_text)

    # --- verdict ---
    if conflicts:
        relationship = _infer_relationship(registry_facts.get("name") or "", "\n".join(_page_all_text(p) for p in live_pages))
        return Verdict(
            "related", None, relationship, signals=signals, conflicts=conflicts,
            note=f"site shows a different organisation number ({conflicts[0].org_number}); treated as {relationship}",
        )

    if has_decisive:
        basis = next(s.kind for s in signals if s.kind in decisive_kinds)
        identity_basis_map = {
            "org_number_on_source": "org_number_on_source",
            "jsonld_org_number": "org_number_on_source",
            "wikidata": "wikidata_org_number",
            "nav_employer_homepage": "job_feed_org_number",
            "osm_orgnr": "open_map_org_number",
        }
        return Verdict("exact", identity_basis_map[basis], "exact", signals=signals, conflicts=[], note="decisive identity signal")

    if registry_declared_ok:
        return Verdict("exact", "registry_declared", "exact", signals=signals, conflicts=[], note="registry-declared site, live and unconflicted")

    if (
        candidate.source == "registry_website" and not registry_declared_ok and shared_domain
        and website_org_count is not None and website_org_count <= SMALL_SHARED_DOMAIN_MAX
        and not hijacked and not is_chain_domain and not owner_name_mismatch
        and "legal_name_match" in distinct_corroborating_raw
        and distinct_corroborating_raw & {"registry_email", "registry_phone", "registered_address"}
    ):
        # The register lists this site for us, it is shared with only a few organisations (a sister
        # company), and the page itself carries our exact legal name plus our registered e-mail, phone or
        # address: specific to this organisation, not a generic shared site.
        return Verdict("exact", "registry_declared", "exact", signals=signals, conflicts=[],
                       note=f"registry-declared site shared by {website_org_count} organisations, page names this organisation and its registered contact details")

    if candidate.source == "registry_website" and not registry_declared_ok and shared_domain:
        return Verdict(
            "related", None, "parent",
            signals=signals, conflicts=[],
            note=f"registry hjemmeside is shared by {website_org_count} organisations; treated as a shared/parent site",
        )

    if candidate.source == "registry_website" and not registry_declared_ok and owner_name_mismatch:
        # Live, unconflicted (no org NUMBER conflict), not a shared domain by our count -- but the page
        # names a different legal entity as its owner (XLEDGER LABS AS -> xledger.com, footer/JSON-LD
        # only ever say "Xledger"/"Xledger AS"). Never published as our own website; related at best.
        return Verdict(
            "related", None, "brand", signals=signals, conflicts=[],
            note=f"registry hjemmeside names a different legal entity as site owner ({owner_name_mismatch!r}); treated as brand",
        )

    if candidate.source == "registry_website" and not registry_declared_ok and umbrella_wording and not distinct_corroborating:
        # Registry-declared, live, unconflicted, but the page reads as a parent/umbrella or franchise
        # site (group/konsern/"our brands"/"our stores"/chain wording) and carries zero corroborating
        # signal tying it to THIS org specifically -- e.g. samfundet.no, hav.no, assemblin.com,
        # bilfinger.com. Never published as our own website; related at best.
        relationship = _infer_relationship(registry_facts.get("name") or "", "\n".join(_page_all_text(p) for p in live_pages))
        return Verdict(
            "related", None, relationship, signals=signals, conflicts=[],
            note=f"registry hjemmeside reads as a parent/umbrella site with no org-specific corroboration; treated as {relationship}",
        )

    if len(distinct_corroborating) >= 2:
        if hijacked:
            return Verdict(
                "ambiguous", None, None, signals=signals, conflicts=[],
                note="corroborating signals found but page reads as hijacked/re-registered; not trusted",
            )
        if mismatched_industry:
            return Verdict(
                "ambiguous", None, None, signals=signals, conflicts=[],
                note=(
                    f"corroborating signals found but site content matches '{mismatched_industry}', not our "
                    "registered industry; topic/industry mismatch guard for a name-derived guess"
                ),
            )
        if require_decisive:
            return Verdict(
                "ambiguous", None, None, signals=signals, conflicts=[],
                note=(
                    ">=2 corroborating signals, but the registry's own declared hjemmeside could not be "
                    "confirmed for this org -- a name-derived guess needs a decisive signal (not "
                    "corroboration alone) while the authoritative site remains unconfirmed"
                ),
            )
        three_independent_soft = len(distinct_corroborating_raw & {"registered_address", "registry_phone", "role_name"}) >= 3
        if (
            candidate.source in ("name_guess", "open_places", "registry_email_domain")
            and not (distinct_corroborating_raw & STRONG_CORROBORATING_KINDS)
            and not three_independent_soft
        ):
            # ORBOTECH NORWAY AS trap: a name-guess with NO independent basis of its own (unlike
            # registry_website/wikidata/nav) landed on a corporate group's brand site after an
            # acquisition (formerly "MK Salg AS"), and the group's Norway landing page genuinely carries
            # the same office address and a board member's name (the business/personnel were absorbed
            # into the group, not a coincidence, but also not evidence this page is entity-specific
            # rather than the group's shared page -- gold: "no Orbotech Norway-specific domain or
            # org-number page was found"). "Soft" content signals (address/phone/role-name) alone can
            # coincidentally satisfy 2-of-3 for a parent/group page after an M&A/office-sharing event;
            # require at least one "hard" signal (exact legal name in <title>, registry e-mail text, or
            # e-mail-domain match) before trusting a bare name-guess.
            return Verdict(
                "ambiguous", None, None, signals=signals, conflicts=[],
                note=(
                    ">=2 corroborating signals but all are address/phone/role-name (no legal-name or "
                    "e-mail match) on a name-derived guess with no independent basis of its own -- too "
                    "weak on their own for a company that could be sharing an office/personnel with an "
                    "unrelated or parent entity"
                ),
            )
        return Verdict("exact", "corroborated", "exact", signals=signals, conflicts=[], note=">=2 independent corroborating signals, no conflict")

    if len(distinct_corroborating) == 1:
        return Verdict("ambiguous", None, None, signals=signals, conflicts=[], note="only one corroborating signal found")

    if candidate.source == "registry_website" and hijacked:
        return Verdict(
            "rejected", None, None, signals=signals, conflicts=[],
            note="registry website appears hijacked or unrelated",
        )

    return Verdict("rejected", None, None, signals=signals, conflicts=[], note="no identity evidence found on site")
