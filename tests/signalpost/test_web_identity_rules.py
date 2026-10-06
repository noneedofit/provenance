"""Identity rules added for recall without wrong-company risk: Norwegian letter folding, domain = full legal
name, registry e-mail domain, three independent soft signals, open places hints, OSM org-number tags."""
from __future__ import annotations

import dataclasses

from signalpost.web.candidates import Candidate
from signalpost.web.crawl import PageFetch, _alternate_homepages
from signalpost.web.verify import assess

ORG = "923456783"


def page(text: str, *, title: str = "", url: str = "https://example.no/", kind: str = "homepage") -> PageFetch:
    return PageFetch(
        url=url, final_url=url, status=200, ok=True, html=f"<html><head><title>{title}</title></head><body>{text}</body></html>",
        text=text, title=title, page_kind=kind, content_sha256="deadbeef", retrieved_at="2026-10-06T00:00:00Z",
    )


def facts(name: str, **extra) -> dict:
    base = {"name": name, "aliases": [], "street": "Tingstadvegen 133", "postcode": "7608", "city": "LEVANGER",
            "phones": [], "email": None, "email_domain": None, "website": None, "role_holders": [], "subunits": []}
    base.update(extra)
    return base


def cand(domain: str, source: str = "name_guess", hints: tuple[str, ...] = ()) -> Candidate:
    return Candidate(domain=domain, url=f"https://{domain}/", source=source, label="test", decisive=False, hints=hints)


def test_norwegian_letters_fold_the_same_on_both_sides():
    p = page("Kontakt oss: Tingstadvegen 133, 7608 Levanger", title="Trøndelag Betong AS - Betong i Trøndelag",
             url="https://trondelag-betong.no/")
    v = assess(ORG, [p], facts("TRØNDELAG BETONG AS"), cand("trondelag-betong.no"))
    assert "legal_name_match" in {s.kind for s in v.signals}
    assert v.status == "exact"


def test_name_in_domain_and_title_is_one_fact_not_two():
    # Namesakes show both (ottokoch.com is a German chef; activepartner.no a same-named company): the name
    # needs an independent registry fact next to it.
    p = page("Creative conferences and events.", title="The MICE Guru", url="https://themiceguru.com/")
    v = assess(ORG, [p], facts("THE MICE GURU AS", street=None, postcode=None), cand("themiceguru.com"))
    kinds = {s.kind for s in v.signals}
    assert {"legal_name_match", "domain_name_match"} <= kinds
    assert v.status != "exact"
    with_address = page("The MICE Guru, Tingstadvegen 133, 7608 Levanger", title="The MICE Guru", url="https://themiceguru.com/")
    assert assess(ORG, [with_address], facts("THE MICE GURU AS"), cand("themiceguru.com")).status == "exact"


def test_parked_and_coming_soon_pages_are_rejected():
    from signalpost.web.verify import is_parked_page
    parked = "senja-taxi.no is parked senja-taxi.no is registered, but the owner currently does not have an active website here."
    assert is_parked_page("", parked)
    assert is_parked_page("", "hestekrefter.no Lanseres snart")
    assert not is_parked_page("", "Velkommen til Hestekrefter. " + "Vi selger traktorer og utstyr. " * 40 + "Nye modeller kommer snart.")


def test_generic_single_word_domain_is_not_a_name_match():
    p = page("Vi bygger hus.", title="Bygg", url="https://bygg.no/")
    v = assess(ORG, [p], facts("BYGG AS", street=None, postcode=None), cand("bygg.no"))
    assert "domain_name_match" not in {s.kind for s in v.signals}
    assert v.status != "exact"


def test_registry_email_domain_with_address_only_stays_ambiguous():
    # An accountant's domain can carry the client's registered (= accountant's) office address.
    p = page("Regnskapskontoret, Tingstadvegen 133, 7608 Levanger", title="Regnskapskontoret", url="https://regnskapskontoret.no/")
    v = assess(ORG, [p], facts("NORDIC FISH AS", email_domain="regnskapskontoret.no"), cand("regnskapskontoret.no", "registry_email_domain"))
    assert v.status != "exact"


def test_registry_email_domain_with_legal_name_on_site_is_exact():
    p = page("Velkommen til tannlegekontoret.", title="Tannlege Emmerhoff", url="https://tannlege-emmerhoff.no/")
    v = assess(ORG, [p], facts("TANNLEGE EMMERHOFF AS", email_domain="tannlege-emmerhoff.no"), cand("tannlege-emmerhoff.no", "registry_email_domain"))
    assert v.status == "exact"


def test_three_independent_soft_signals_are_exact_two_are_not():
    text = "Sjø-Sport, Gismerøyveien 1, 4515 Mandal. Tlf 38 26 00 00. Daglig leder Kari Nordmann"
    f = facts("SJØ-SPORT MANDAL AS", street="Gismerøyveien 1", postcode="4515", phones=["38260000"], role_holders=["Kari Nordmann"])
    v3 = assess(ORG, [page(text, url="https://sjo-sport.no/")], f, cand("sjo-sport.no", "open_places", ("phone",)))
    assert v3.status == "exact"
    f2 = dict(f, role_holders=[])
    v2 = assess(ORG, [page(text, url="https://sjo-sport.no/")], f2, cand("sjo-sport.no", "name_guess"))
    assert v2.status != "exact"


def test_open_places_phone_match_alone_is_not_enough():
    # A housing co-op's registry phone is often its property manager's switchboard.
    p = page("Vi forvalter borettslag i hele regionen.", title="Vestbo", url="https://vestbo.no/")
    v = assess(ORG, [p], facts("BORETTSLAGET ERLEVEIEN 53"), cand("vestbo.no", "open_places", ("phone",)))
    assert v.status != "exact"


def test_osm_org_number_tag_is_decisive():
    p = page("Velkommen.", title="Kafe", url="https://kafe.no/")
    c = dataclasses.replace(cand("kafe.no", "osm_orgnr_website"), decisive=True)
    v = assess(ORG, [p], facts("KAFE AS", street=None, postcode=None), c)
    assert v.status == "exact" and v.identity_basis == "open_map_org_number"


class _DNS:
    def __init__(self, live: set[str]):
        self.live = live

    def dns_resolves(self, host: str) -> bool:
        return host in self.live


class _Ctx:
    def __init__(self, live: set[str]):
        self.client = _DNS(live)


def test_alternate_homepages_try_http_and_www_only_for_resolving_hosts():
    ctx = _Ctx({"helbu.no", "www.helbu.no"})
    assert _alternate_homepages(ctx, "https://helbu.no/") == ["http://helbu.no/", "https://www.helbu.no/"]
    assert _alternate_homepages(_Ctx(set()), "https://dead.no/") == []
    assert _alternate_homepages(_Ctx({"www.only.no"}), "https://only.no/") == ["https://www.only.no/", "http://www.only.no/"]


def test_owner_line_with_boilerplate_still_names_us():
    from signalpost.web.verify import site_owner_mismatch
    assert site_owner_mismatch("RÆLINGEN EL-INSTALLASJON AS", ["All Rights Reserved Rælingen El Installasjon AS"]) is None
    assert site_owner_mismatch("XLEDGER LABS AS", ["Xledger AS"]) == "Xledger AS"


def test_registry_site_shared_by_two_with_our_name_and_email_is_exact():
    text = "Rana Utvikling AS. Kontakt: post@ru.no. Daglig leder Kari Nordmann."
    f = facts("RANA UTVIKLING AS", email="post@ru.no", email_domain="ru.no", role_holders=["Kari Nordmann"], website="ru.no")
    p = page(text, title="Rana Utvikling AS", url="https://ru.no/")
    v = assess(ORG, [p], f, Candidate(domain="ru.no", url="https://ru.no/", source="registry_website", label="t", decisive=True), website_org_count=2)
    assert v.status == "exact"
    # The same page shared by many organisations (a property manager) stays a shared site.
    v_many = assess(ORG, [p], f, Candidate(domain="ru.no", url="https://ru.no/", source="registry_website", label="t", decisive=True), website_org_count=40)
    assert v_many.status != "exact"
