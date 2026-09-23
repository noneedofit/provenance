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
    "role_holders": ["Ola Nordmann"],
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


# --- assess(): parent/umbrella registry-declared site with no org-specific corroboration -----------


def test_registry_declared_parent_umbrella_site_without_corroboration_is_related_not_exact():
    # samfundet.no for Samfundets Stotter AS / hav.no for HAV Chartering AS pattern: the registry
    # hjemmeside is live and shows no conflicting org number, but the page reads as a shared group site
    # ("Vart konsern eier flere selskaper...") and carries zero signal tying it to THIS specific entity
    # (no matching address/phone/email/role holder/exact legal name). Must never be exact.
    facts = {**REGISTRY_FACTS, "name": "SAMFUNDETS STOTTER AS", "street": "", "postcode": "", "phones": [], "email": None, "role_holders": []}
    html = "<html><body>Velkommen til Samfundet. Vart konsern eier flere selskaper og driver flere av vare merkevarer.</body></html>"
    p = page("Samfundet konsern", html=html, url="https://samfundet.no/")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="samfundet.no", source="registry_website"), website_org_count=None)
    assert verdict.status == "related"
    assert verdict.relationship is not None


def test_registry_declared_parent_umbrella_site_with_corroboration_can_still_be_exact():
    # The same umbrella wording, but this time the page also carries our registered address -- one
    # corroborating signal is enough to let the registry_declared decisive path through (the umbrella
    # gate only blocks a *bare, unsupported* registry_declared claim).
    facts = {**REGISTRY_FACTS, "name": "HAV CHARTERING AS", "street": "Kaigata 5", "postcode": "5003"}
    html = "<html><body>HAV Chartering AS. Vart konsern eier flere selskaper. Kaigata 5, 5003 Bergen.</body></html>"
    p = page("HAV Chartering konsern", html=html, url="https://hav.no/")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="hav.no", source="registry_website"), website_org_count=None)
    assert verdict.status == "exact"


def test_registry_declared_subpage_with_our_org_number_on_shared_domain_is_exact():
    # The valid case: a specific subpage of a shared/umbrella domain (DNT Nord-Trondelag on dnt.no)
    # that shows OUR org number is exact, with that subpage URL preserved as the website.
    facts = {**REGISTRY_FACTS, "name": "DNT NORD-TRONDELAG"}
    html = f"<html><body>DNT Nord-Trondelag, en del av DNT-konsernet. Org.nr {OUR_ORG[:3]} {OUR_ORG[3:6]} {OUR_ORG[6:]}.</body></html>"
    p = page("DNT Nord-Trondelag", html=html, url="https://dnt.no/nord-trondelag")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="dnt.no", source="registry_website"), website_org_count=None)
    assert verdict.status == "exact"
    assert verdict.identity_basis == "org_number_on_source"


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


# --- assess(): hijacked registry-declared domain (GAASA AS trap) ------------------------------------


def test_registry_declared_domain_hijacked_by_casino_content_is_rejected():
    facts = {**REGISTRY_FACTS, "name": "GAASA AS", "street": "Gaasaveien 4", "postcode": "1234"}
    html = "<html><body>Best online casino! Free spins, no deposit bonus, jackpot slots - play now and win big!</body></html>"
    p = page("Best online casino free spins jackpot slots", html=html, url="https://gaasa.no/")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="gaasa.no", source="registry_website"), website_org_count=1)
    assert verdict.status == "rejected"
    assert verdict.note == "registry website appears hijacked or unrelated"


def test_registry_declared_hijacked_domain_with_real_corroboration_is_not_blindly_exact():
    # Even if a hijacked-looking page happens to contain some matching text, a single corroborating
    # signal must stay ambiguous, never jump straight to exact via the (now-disabled) registry_declared path.
    facts = {**REGISTRY_FACTS, "name": "GAASA AS", "street": "Gaasaveien 4", "postcode": "1234"}
    html = "<html><body>Online casino jackpot! Gaasaveien 4 1234 (old cached address). Free spins bonus.</body></html>"
    p = page("casino jackpot", html=html, url="https://gaasa.no/")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="gaasa.no", source="registry_website"), website_org_count=1)
    assert verdict.status != "exact"


# --- assess(): group sites sharing one corporate domain (Bilfinger/Sonat/Xledger/Avarn pattern) -----


def test_shared_group_domain_without_our_org_number_is_related_not_exact():
    facts = {**REGISTRY_FACTS, "name": "AVARN SECURITY AS"}
    html = "<html><body>Avarn Security operates across the Nordics. Part of the Avarn Group.</body></html>"
    p = page("Avarn Security Nordics group", html=html, url="https://www.avarn.no/")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="avarn.no", source="registry_website"), website_org_count=6)
    assert verdict.status == "related"
    assert verdict.relationship is not None


def test_shared_group_domain_promoted_to_exact_only_by_org_number_match():
    facts = {**REGISTRY_FACTS, "name": "AVARN SECURITY AS"}
    html = f"<html><body>Avarn Security AS. Org.nr {OUR_ORG[:3]} {OUR_ORG[3:6]} {OUR_ORG[6:]}.</body></html>"
    p = page("Avarn Security AS org number", html=html, url="https://www.avarn.no/")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="avarn.no", source="registry_website"), website_org_count=6)
    assert verdict.status == "exact"
    assert verdict.identity_basis == "org_number_on_source"


# --- assess(): namesake companies with different org numbers (two "Øen Kuldeteknikk AS") ------------


def test_namesake_company_without_org_number_match_does_not_resolve_by_name_alone():
    # Two distinct legal entities share the exact registered name; only the address differs. Assessing
    # against the WRONG org number's facts must not reach `exact` off a bare name/title match.
    site_text = "Oen Kuldeteknikk AS. Kontakt oss i Bergen."
    p = page(site_text, url="https://oenkuldeteknikk.no/")
    facts_wrong_entity = {**REGISTRY_FACTS, "name": "Øen Kuldeteknikk AS", "street": "Annenveien 9", "postcode": "9999", "role_holders": []}
    verdict = assess(OUR_ORG, [p], facts_wrong_entity, cand(domain="oenkuldeteknikk.no", source="name_guess"))
    assert verdict.status != "exact"


def test_our_org_number_decisive_even_when_other_org_numbers_also_present():
    # Mowi/Kitron/AF Gruppen/DNT-Nord-Trondelag-style trap: a large group/investor-relations page lists
    # SEVERAL org numbers (subsidiaries, a parent entity) alongside our own. Our own number anywhere on
    # the page must still be decisive -- a different number elsewhere is not automatically a conflict.
    other_org = "934567897"
    html = (
        f"<html><body>Kitron ASA. Org.nr {OUR_ORG[:3]} {OUR_ORG[3:6]} {OUR_ORG[6:]}. "
        f"Subsidiaries include Kitron Sweden AB (org {other_org[:3]} {other_org[3:6]} {other_org[6:]}) "
        "and others.</body></html>"
    )
    p = page("Kitron ASA subsidiaries", html=html, url="https://www.kitron.com/")
    verdict = assess(OUR_ORG, [p], REGISTRY_FACTS, cand(domain="kitron.com", source="wikidata_website"))
    assert verdict.status == "exact"
    assert verdict.identity_basis == "org_number_on_source"


def test_unlabeled_other_org_number_is_not_a_conflict_and_does_not_block_wikidata():
    # A bare, unlabelled 9-digit run that happens to pass the mod-11 check (a coincidental reference
    # number, an unrelated ID) elsewhere on a Wikidata-sourced candidate's homepage must not block the
    # decisive wikidata signal -- only a LABELLED match or a JSON-LD identifier counts as a genuine
    # conflict.
    other_valid_orgnr = "934567897"  # passes mod-11, appears with no "org.nr"/"eies av" label nearby
    html = f"<html><body>Ref: {other_valid_orgnr[:3]} {other_valid_orgnr[3:6]} {other_valid_orgnr[6:]}. Welcome to Kitron.</body></html>"
    p = page("Kitron unrelated reference number", html=html, url="https://www.kitron.com/")
    verdict = assess(OUR_ORG, [p], REGISTRY_FACTS, cand(domain="kitron.com", source="wikidata_website"))
    assert verdict.status == "exact"
    assert verdict.identity_basis == "wikidata_org_number"


def test_labeled_other_org_number_without_ours_is_still_a_genuine_conflict():
    # The precision-preserving counterpart: when OUR number is absent and the OTHER number is clearly
    # presented as the site's owner (labelled "Org.nr:"), it is still a real conflict -> never exact.
    other_org = "934567897"
    html = f"<html><body>This site belongs to Org.nr {other_org[:3]} {other_org[3:6]} {other_org[6:]}, not us.</body></html>"
    p = page("belongs to someone else", html=html, url="https://www.kitron.com/")
    verdict = assess(OUR_ORG, [p], REGISTRY_FACTS, cand(domain="kitron.com", source="wikidata_website"))
    assert verdict.status != "exact"
    assert verdict.status == "related"


# --- assess(): XLEDGER LABS AS trap (group/product site crediting a different legal entity) ----------


def test_xledger_trap_jsonld_owner_name_mismatch_blocks_registry_declared_exact():
    # XLEDGER LABS AS's registry hjemmeside is xledger.com, Xledger's shared product/marketing site.
    # No page on it ever names "Xledger Labs AS" specifically -- JSON-LD only ever says "Xledger". Must
    # not resolve to exact off bare registry_declared trust.
    facts = {**REGISTRY_FACTS, "name": "XLEDGER LABS AS", "street": "", "postcode": "", "phones": [], "email": None, "role_holders": []}
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Organization","name":"Xledger",'
        '"url":"https://xledger.com/"}</script></head>'
        "<body>Xledger. Cloud ERP for growing businesses.</body></html>"
    )
    p = page("Xledger cloud ERP", html=html, url="https://xledger.com/")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="xledger.com", source="registry_website"), website_org_count=1)
    assert verdict.status != "exact"
    assert verdict.status == "related"


def test_xledger_style_site_with_our_own_name_in_jsonld_is_still_exact():
    # Sanity check: when the JSON-LD name DOES match our registered name, the mismatch guard must not
    # fire -- registry_declared trust still applies normally.
    facts = {**REGISTRY_FACTS, "name": "XLEDGER LABS AS"}
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Organization","name":"Xledger Labs AS",'
        '"url":"https://xledgerlabs.no/"}</script></head><body>Xledger Labs AS.</body></html>'
    )
    p = page("Xledger Labs AS", html=html, url="https://xledgerlabs.no/")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="xledgerlabs.no", source="registry_website"), website_org_count=1)
    assert verdict.status == "exact"
    assert verdict.identity_basis == "registry_declared"


def test_registry_declared_domain_shared_by_two_orgs_blocks_exact():
    # Coordinator fix: the registry_declared decisive gate now checks website_org_count >= 2 (not 3) --
    # a domain also registered by even one sibling/parent entity is not decisive on bare trust alone.
    p = page("Example Company AS leverer tjenester i Oslo.")
    verdict = assess(OUR_ORG, [p], REGISTRY_FACTS, cand(source="registry_website"), website_org_count=2)
    assert verdict.status != "exact"
    assert verdict.status == "related"


def test_namesake_company_resolved_by_org_number_match():
    site_text = f"Oen Kuldeteknikk AS. Org.nr {OUR_ORG[:3]} {OUR_ORG[3:6]} {OUR_ORG[6:]}. Kontakt oss i Bergen."
    p = page(site_text, url="https://oenkuldeteknikk.no/")
    facts = {**REGISTRY_FACTS, "name": "Øen Kuldeteknikk AS"}
    verdict = assess(OUR_ORG, [p], facts, cand(domain="oenkuldeteknikk.no", source="name_guess"))
    assert verdict.status == "exact"
    assert verdict.identity_basis == "org_number_on_source"


# --- assess(): JOKER AS trap (sea-fishing company on Vaeroy vs joker.no the grocery chain) ----------


def test_joker_franchise_domain_never_exact_without_our_org_number():
    # JOKER AS (983043291) is a sea-fishing company; joker.no is the grocery chain's own corporate
    # domain (blocklist.py FRANCHISE_CHAIN_DOMAINS). Even with the same brand word and a plausible
    # corroborating address/name match, a name-guess landing here must never resolve to exact.
    facts = {**REGISTRY_FACTS, "name": "JOKER AS", "street": "Kaiveien 3", "postcode": "8063", "city": "VAEROY"}
    html = "<html><head><title>Joker</title></head><body>Joker dagligvare. Ukens tilbud i din matbutikk. Kaiveien 3 8063.</body></html>"
    p = page("Joker dagligvare ukens tilbud", html=html, url="https://joker.no/")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="joker.no", source="name_guess"))
    assert verdict.status != "exact"


def test_industry_mismatch_guard_blocks_corroboration_on_unlisted_namesake_domain():
    # Same trap as JOKER AS, but on a domain that is NOT in the franchise/chain blocklist -- the
    # topic/industry-mismatch guard (NACE division vs site content) must independently catch it. Our
    # company is registered under NACE 03.11 (sea fishing); the guessed domain's content reads as a
    # grocery retailer, and even carries two normally-decisive corroborating signals (address + legal
    # name in title) inherited from a stale/incidental match.
    facts = {
        **REGISTRY_FACTS, "name": "BRIS AS", "street": "Kaiveien 3", "postcode": "8063",
        "phones": ["12345678"], "nace": "03.11",
    }
    html = (
        "<html><head><title>Bris AS</title></head><body>Bris AS - din lokale dagligvare og matbutikk. "
        "Ukens tilbud denne uken. Velkommen til handlekurv og kjedebutikk. Kaiveien 3 8063. Ring 12345678."
        "</body></html>"
    )
    p = page("Bris AS dagligvare matbutikk", html=html, url="https://bris.no/")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="bris.no", source="name_guess"))
    assert verdict.status != "exact"
    assert verdict.status == "ambiguous"
    assert "industry" in verdict.note.lower()


def test_industry_mismatch_guard_does_not_block_same_industry_corroboration():
    # Sanity check: the guard must not suppress a genuine match just because NACE is set -- a fishing
    # company's own site, describing fishing, with 2 corroborating signals, is still exact.
    facts = {
        **REGISTRY_FACTS, "name": "BRIS AS", "street": "Kaiveien 3", "postcode": "8063",
        "phones": ["12345678"], "nace": "03.11",
    }
    html = (
        "<html><head><title>Bris AS</title></head><body>Bris AS driver fiskebat og fangst av fisk i "
        "Lofoten. Kaiveien 3 8063. Ring 12345678.</body></html>"
    )
    p = page("Bris AS fiskebat", html=html, url="https://bris.no/")
    verdict = assess(OUR_ORG, [p], facts, cand(domain="bris.no", source="name_guess"))
    assert verdict.status == "exact"
    assert verdict.identity_basis == "corroborated"
