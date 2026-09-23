"""Identity resolution: is this candidate site really the company's own site?

Pure functions, no network. BUILD_SPEC.md "Identity rules" is the contract this module implements:

- `exact` requires ONE decisive signal (org number on any fetched page, JSON-LD vatID/taxID/identifier,
  Wikidata P2333 match, NAV ad org-number+homepage match, or a live/not-parked/unconflicted registry
  `hjemmeside`) OR >=2 independent corroborating signals with no conflict.
- A different valid org number shown prominently is a conflict -> never `exact`; becomes `related` with a
  relationship (parent/franchise/brand/service_provider).
- A registry-declared domain used as `website` by >=3 organisations is a shared/parent site -> `related`,
  never `exact`.

Precision over recall: when unsure, this module returns `ambiguous` or `rejected`, never `exact`.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from .candidates import Candidate
from .crawl import PARKED_MARKERS, PageFetch

Status = Literal["exact", "related", "ambiguous", "rejected"]
Relationship = Literal["parent", "subsidiary", "franchise", "brand", "service_provider"]

# --- Norwegian organisation-number detection -------------------------------------------------------------

# A run of 9 digits, allowing spaces/dots/non-breaking-spaces between groups (e.g. "979 543 883",
# "979.543.883", "979543883"), optionally preceded by "NO" and/or followed by "MVA".
_ORGNR_CANDIDATE_RE = re.compile(
    r"(?:\bNO[\s.]?)?\b(\d[\d\s. ]{7,15}\d)\b(?:\s?MVA\b)?", re.IGNORECASE
)
_ORGNR_LABEL_RE = re.compile(
    r"(org(?:anisasjonsnummer|\.?\s*nr)\.?\s*[:.]?\s*)(\d[\d\s. ]{7,15}\d)", re.IGNORECASE
)

_MOD11_WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)


def is_valid_orgnr(digits: str) -> bool:
    """Mod-11 check digit validation for a 9-digit Norwegian organisation number."""
    if len(digits) != 9 or not digits.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(digits, _MOD11_WEIGHTS))
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


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().casefold()


def is_parked_page(html: str, text: str) -> bool:
    haystack = _normalize((html or "")[:5000] + " " + (text or "")[:2000])
    return any(marker in haystack for marker in PARKED_MARKERS)


_DIGITS_8_RE = re.compile(r"(?<!\d)(\d[\d\s]{6,10}\d)(?!\d)")


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("47") and len(digits) == 10:
        digits = digits[2:]
    return digits


def find_phones(text: str) -> set[str]:
    found = set()
    for match in _DIGITS_8_RE.finditer(text or ""):
        digits = _normalize_phone(match.group(1))
        if len(digits) == 8:
            found.add(digits)
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


def _legal_name_core(name: str) -> set[str]:
    text = str(name or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    suffixes = {"as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen", "nuf"}
    return {tok for tok in re.findall(r"[a-z0-9]+", text) if tok not in suffixes and len(tok) > 1}


def _page_all_text(page: PageFetch) -> str:
    return f"{page.title}\n{page.text}\n{page.html[:20000]}"


def assess(
    org_number: str,
    pages: list[PageFetch],
    registry_facts: dict[str, Any],
    candidate: Candidate,
    *,
    website_org_count: int | None = None,
) -> Verdict:
    """Assess whether `pages` (from `candidate`) belong to the company identified by `org_number`."""
    live_pages = [p for p in pages if p.ok]
    if not live_pages:
        return Verdict("rejected", None, None, note="no page could be fetched")

    homepage = next((p for p in pages if p.page_kind == "homepage"), pages[0])
    if homepage.ok and is_parked_page(homepage.html, homepage.text):
        return Verdict("rejected", None, None, note="homepage is a parked/for-sale placeholder")

    signals: list[Signal] = []
    conflicts: list[Conflict] = []
    our_digits = re.sub(r"\D", "", str(org_number or ""))

    # --- org-number scan across all fetched pages (regex text + JSON-LD) ---
    for page in live_pages:
        for m in find_org_numbers(_page_all_text(page)):
            if m.digits == our_digits:
                signals.append(Signal("org_number_on_source", "organisation number found on site", page.final_url, m.span))
            else:
                conflicts.append(Conflict("conflicting_org_number", "different valid organisation number on site", m.digits, page.final_url, m.span))
        try:
            import extruct

            data = extruct.extract(page.html, base_url=page.final_url, syntaxes=["json-ld"])
            for m in find_org_number_in_jsonld(data.get("json-ld", [])):
                if m.digits == our_digits:
                    signals.append(Signal("jsonld_org_number", "JSON-LD identifier matches organisation number", page.final_url, m.span))
                else:
                    conflicts.append(Conflict("conflicting_org_number", "JSON-LD identifier is a different organisation number", m.digits, page.final_url, m.span))
        except Exception:
            pass

    # --- decisive non-textual sources ---
    if candidate.source == "wikidata_website" and not conflicts:
        signals.append(Signal("wikidata", "candidate sourced from Wikidata entry for this organisation number", homepage.final_url, candidate.label))
    if candidate.source == "nav_employer_homepage" and not conflicts:
        signals.append(Signal("nav_employer_homepage", "NAV job ad for this org number links this homepage", homepage.final_url, candidate.label))

    decisive_kinds = {"org_number_on_source", "jsonld_org_number", "wikidata", "nav_employer_homepage"}
    has_decisive = any(s.kind in decisive_kinds for s in signals)

    # --- registry_declared: only decisive if live, not parked (already checked), no conflict, not shared ---
    registry_declared_ok = False
    if candidate.source == "registry_website" and not conflicts:
        if website_org_count is not None and website_org_count >= 3:
            registry_declared_ok = False
        else:
            registry_declared_ok = True
            signals.append(Signal("registry_declared", "registry-listed hjemmeside is live and unconflicted", homepage.final_url, candidate.url))

    # --- corroborating signals (only meaningful when no conflict) ---
    if not conflicts:
        joined_text = "\n".join(_page_all_text(p) for p in live_pages)
        norm_joined = _normalize(joined_text)

        street = registry_facts.get("street")
        postcode = registry_facts.get("postcode")
        if street and postcode and _normalize(street) in norm_joined and _normalize(postcode) in norm_joined:
            signals.append(Signal("registered_address", "registered street and postcode found on site", homepage.final_url, f"{street} {postcode}"))

        site_phones = find_phones(joined_text)
        for phone in registry_facts.get("phones") or []:
            normalized = _normalize_phone(str(phone))
            if len(normalized) == 8 and normalized in site_phones:
                signals.append(Signal("registry_phone", "registry phone number found on site", homepage.final_url, normalized))
                break

        email = registry_facts.get("email")
        if email and _normalize(email) in norm_joined:
            signals.append(Signal("registry_email", "registry email address found on site", homepage.final_url, email))

        for person in registry_facts.get("role_names") or []:
            if person and _normalize(person) in norm_joined:
                signals.append(Signal("role_name", "registered CEO/board member name found on site", homepage.final_url, person))
                break

        legal_core = _legal_name_core(registry_facts.get("name") or "")
        title_tokens = set(re.findall(r"[a-z0-9]+", _normalize(homepage.title)))
        if legal_core and legal_core.issubset(title_tokens):
            signals.append(Signal("legal_name_match", "exact legal name (minus suffix) found in <title>", homepage.final_url, homepage.title))

    corroborating_kinds = {"registered_address", "registry_phone", "registry_email", "role_name", "legal_name_match"}
    corroborating = [s for s in signals if s.kind in corroborating_kinds]
    distinct_corroborating = {s.kind for s in corroborating}

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
        }
        return Verdict("exact", identity_basis_map[basis], "exact", signals=signals, conflicts=[], note="decisive identity signal")

    if registry_declared_ok:
        return Verdict("exact", "registry_declared", "exact", signals=signals, conflicts=[], note="registry-declared site, live and unconflicted")

    if candidate.source == "registry_website" and not registry_declared_ok and website_org_count and website_org_count >= 3:
        return Verdict(
            "related", None, "parent",
            signals=signals, conflicts=[],
            note=f"registry hjemmeside is shared by {website_org_count} organisations; treated as a shared/parent site",
        )

    if len(distinct_corroborating) >= 2:
        return Verdict("exact", "corroborated", "exact", signals=signals, conflicts=[], note=">=2 independent corroborating signals, no conflict")

    if len(distinct_corroborating) == 1:
        return Verdict("ambiguous", None, None, signals=signals, conflicts=[], note="only one corroborating signal found")

    return Verdict("rejected", None, None, signals=signals, conflicts=[], note="no identity evidence found on site")
