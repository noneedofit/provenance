from __future__ import annotations

from signalpost.web.candidates import Candidate
from signalpost.web.crawl import PageFetch
from signalpost.web.verify import assess, find_org_numbers, is_valid_orgnr

OUR_ORG = "923456783"
CONFLICT_ORG = "934567897"
SPEC_EXAMPLE_ORG = "979543883"  # from BUILD_SPEC.md example span "Org.nr: 979 543 883"


def page(text: str, *, html: str | None = None, kind: str = "homepage", url: str = "https://example.no/") -> PageFetch:
    return PageFetch(
        url=url, final_url=url, status=200, ok=True, html=html if html is not None else f"<html><body>{text}</body></html>",
        text=text, title="", page_kind=kind, content_sha256="deadbeef", retrieved_at="2026-09-23T00:00:00Z",
    )


REGISTRY_FACTS = {
    "name": "EXAMPLE COMPANY AS",
    "aliases": [],
    "street": "Storgata 1",
    "postcode": "0155",
    "city": "OSLO",
    "phones": ["12345678"],
    "email": "post@example.no",
    "email_domain": "example.no",
    "website": "example.no",
    "role_names": ["Ola Nordmann"],
    "subunits": [],
}


def cand(domain: str = "example.no", source: str = "registry_website") -> Candidate:
    return Candidate(domain=domain, url=f"https://{domain}/", source=source, label="test", decisive=source != "name_guess")


# --- mod-11 / regex -----------------------------------------------------------------------------------


def test_is_valid_orgnr_accepts_known_valid_number():
    assert is_valid_orgnr(SPEC_EXAMPLE_ORG)
    assert is_valid_orgnr(OUR_ORG)


def test_is_valid_orgnr_rejects_random_digits():
    assert not is_valid_orgnr("123456789")
    assert not is_valid_orgnr("00000000")  # wrong length
    assert not is_valid_orgnr("abcdefghi")


def test_find_org_numbers_handles_spacing_dots_no_prefix_and_mva_suffix():
    text = f"Org.nr: {SPEC_EXAMPLE_ORG[:3]} {SPEC_EXAMPLE_ORG[3:6]} {SPEC_EXAMPLE_ORG[6:]}"
    matches = find_org_numbers(text)
    assert any(m.digits == SPEC_EXAMPLE_ORG for m in matches)

    dotted = f"NO {SPEC_EXAMPLE_ORG[:3]}.{SPEC_EXAMPLE_ORG[3:6]}.{SPEC_EXAMPLE_ORG[6:]} MVA"
    matches2 = find_org_numbers(dotted)
    assert any(m.digits == SPEC_EXAMPLE_ORG for m in matches2)


def test_find_org_numbers_ignores_random_9_digit_runs():
    # A random 9-digit phone-like number that fails the mod-11 check must not be misread as an org number.
    matches = find_org_numbers("Call us at 123456789 any time")
    assert matches == []


# --- assess(): decisive org-number match -----------------------------------------------------------


def test_exact_when_org_number_on_site():
    p = page(f"Kontakt oss. Org.nr: {OUR_ORG[:3]} {OUR_ORG[3:6]} {OUR_ORG[6:]}")
    verdict = assess(OUR_ORG, [p], REGISTRY_FACTS, cand())
    assert verdict.status == "exact"
    assert verdict.identity_basis == "org_number_on_source"
    assert verdict.relationship == "exact"


def test_exact_via_jsonld_identifier():
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Organization","name":"Example",'
        '"identifier":"%s"}</script></head><body>Example company</body></html>' % OUR_ORG
    )
    p = page("Example company", html=html)
    verdict = assess(OUR_ORG, [p], REGISTRY_FACTS, cand())
    assert verdict.status == "exact"


# --- assess(): conflicts / known traps from docs/PLAN.md -------------------------------------------


def test_conflicting_org_number_never_exact():
    p = page(f"This site belongs to org {CONFLICT_ORG[:3]} {CONFLICT_ORG[3:6]} {CONFLICT_ORG[6:]}, not us.")
    verdict = assess(OUR_ORG, [p], REGISTRY_FACTS, cand())
    assert verdict.status != "exact"
    assert verdict.status == "related"
    assert verdict.conflicts and verdict.conflicts[0].org_number == CONFLICT_ORG


def test_franchise_trap_kaliyugarasan_7eleven():
    facts = {**REGISTRY_FACTS, "name": "BUTIKKDRIFT KALIYUGARASAN AS"}
    html = (
        f'<html><body>7-Eleven Norge. Org.nr {CONFLICT_ORG}. Finn din 7-Eleven-butikk hos vaar franchisetaker '
        f"BUTIKKDRIFT KALIYUGARASAN AS i Oslo.</body></html>"
    )
    p = page("7-Eleven Norge franchisetaker", html=html)
    verdict = assess(OUR_ORG, [p], facts, cand(domain="7-eleven.no"))
    assert verdict.status == "related"
    assert verdict.relationship == "franchise"


def test_parent_brand_trap_mecca_thon():
    facts = {**REGISTRY_FACTS, "name": "MECCA AS"}
    html = f'<html><body>Thon Hotels. Konsern-morselskap. Org.nr {CONFLICT_ORG}. Var kjede eier Mecca.</body></html>'
    p = page("Thon Hotels konsern", html=html)
    verdict = assess(OUR_ORG, [p], facts, cand(domain="thon.no"))
    assert verdict.status == "related"
    assert verdict.relationship in {"parent", "franchise"}


def test_group_site_bestseller_no_false_exact():
    # A name-guessed group/corporate site (not registry-declared) with no Norwegian org-number or
    # address evidence must never be published as exact — only >=1 corroborating signal here.
    facts = {**REGISTRY_FACTS, "name": "BESTSELLER AS"}
    p = page("BESTSELLER is a global fashion group operating many brands worldwide.")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="bestseller.com", source="name_guess"))
    assert verdict.status != "exact"


def test_shared_housing_manager_domain_never_exact_even_without_conflict():
    facts = {**REGISTRY_FACTS, "name": "SAMEIET EKSEMPEL", "street": "Eksempelveien 2", "postcode": "0170"}
    p = page("Sameiet Eksempel forvaltes av OBOS. Eksempelveien 2, 0170 Oslo.")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="obos.no", source="registry_website"), website_org_count=500)
    assert verdict.status != "exact"


# --- assess(): registry_declared happy path ----------------------------------------------------------


def test_registry_declared_exact_when_live_unconflicted_and_not_shared():
    p = page("Example Company AS leverer tjenester i Oslo.")
    verdict = assess(OUR_ORG, [p], REGISTRY_FACTS, cand(source="registry_website"), website_org_count=1)
    assert verdict.status == "exact"
    assert verdict.identity_basis == "registry_declared"


# --- assess(): corroborating signals -----------------------------------------------------------------


def test_two_corroborating_signals_no_conflict_is_exact():
    text = "Example Company AS. Storgata 1, 0155 Oslo. Kontakt: Ola Nordmann, daglig leder."
    p = page(text)
    verdict = assess(OUR_ORG, [p], REGISTRY_FACTS, cand(source="name_guess"))
    assert verdict.status == "exact"
    assert verdict.identity_basis == "corroborated"


def test_single_corroborating_signal_is_ambiguous_not_exact():
    text = "Storgata 1, 0155 Oslo."
    p = page(text)
    verdict = assess(OUR_ORG, [p], REGISTRY_FACTS, cand(source="name_guess"))
    assert verdict.status == "ambiguous"


def test_no_evidence_is_rejected():
    p = page("Welcome to our generic template website with no identifying information.")
    verdict = assess(OUR_ORG, [p], REGISTRY_FACTS, cand(source="name_guess"))
    assert verdict.status == "rejected"


def test_parked_page_is_rejected():
    p = page("This domain is for sale. Buy this domain today via HugeDomains.")
    verdict = assess(OUR_ORG, [p], REGISTRY_FACTS, cand(source="name_guess"))
    assert verdict.status == "rejected"
    assert "parked" in verdict.note
