from __future__ import annotations

import time

from signalpost import workforce
from signalpost.context import CompanyContext

from activity_testkit import FakeHttpClient

ORG = "912345678"
URL = workforce.PDF_URL.format(org=ORG, year=2024)


def _ctx(client, **shared):
    return CompanyContext(org=ORG, run_id="t", now="2026-11-07T00:00:00Z", tier="T1", bulk={}, client=client, shared=shared)


def test_extract_fte_with_decimal_comma():
    r = workforce.extract("Note 3 Lønnskostnader\nAntall årsverk i regnskapsåret: 8,40\n")
    assert r == {"status": "ok", "count": 8.4, "measure": "fte", "match": r["match"]}


def test_extract_ocr_spacing_and_split_decimal():
    assert workforce.extract("Antall ars verk i regnskapsaret 1 00")["count"] == 1


def test_extract_no_employees():
    r = workforce.extract("Selskapet har ikke hatt ansatte i 2024.")
    assert (r["status"], r["count"], r["measure"]) == ("ok", 0, "employees")


def test_fte_preferred_over_headcount():
    r = workforce.extract("Gjennomsnittlig antall ansatte: 12\nAntall årsverk i regnskapsåret: 9,5")
    assert (r["count"], r["measure"]) == (9.5, "fte")


def test_group_figures_are_skipped():
    assert workforce.extract("Konsernet: antall årsverk i regnskapsåret: 120") is None


def test_conflicting_figures_are_not_published():
    assert workforce.extract("Antall årsverk: 3\nAntall årsverk: 4")["status"] == "conflict"


def test_quote_is_the_source_line_as_read():
    text = "Lønn 1 000\nAntall årsverk i\nregnskapsåret: 2\n"
    match = workforce.extract(text)["match"]
    assert workforce.quote_for(text, match) == "Antall årsverk i regnskapsåret: 2"


def test_connector_publishes_claim(monkeypatch):
    client = FakeHttpClient()
    client.route(URL, b"%PDF-1.4 fake")
    monkeypatch.setattr(workforce, "tools", lambda: {"ocr": True, "pdftotext": None})
    monkeypatch.setattr(workforce, "read_pdf", lambda pdf: {
        "result": workforce.extract("Antall årsverk i regnskapsåret: 8,40"), "page": 5,
        "text": "Antall årsverk i regnskapsåret: 8,40", "method": "ocr"})
    result = workforce.WorkforceConnector().run(_ctx(client, latest_filed_year=2024, deadline_at=time.monotonic() + 3600))
    assert len(result.claims) == 1
    claim = result.claims[0]
    assert (claim.family, claim.field, claim.value["count"], claim.value["year"]) == ("financials", "workforce", 8.4, 2024)
    ev = result.evidence[0]
    assert ev.source_class == "official_filing" and ev.claim_span == "Antall årsverk i regnskapsåret: 8,40"
    assert claim.evidence_ids == [ev.evidence_id]


def test_connector_skips_without_year_or_near_deadline(monkeypatch):
    client = FakeHttpClient()
    monkeypatch.setattr(workforce, "tools", lambda: {"ocr": True, "pdftotext": None})
    assert workforce.WorkforceConnector().run(_ctx(client)).claims == []
    assert workforce.WorkforceConnector().run(_ctx(client, latest_filed_year=2024, deadline_at=time.monotonic() + 60)).claims == []
    assert workforce.WorkforceConnector().run(_ctx(client, latest_filed_year=2024, registry_only=True)).claims == []
    assert client.calls == []


def test_connector_skips_without_ocr_tools(monkeypatch):
    client = FakeHttpClient()
    monkeypatch.setattr(workforce, "tools", lambda: {"ocr": False, "pdftotext": None})
    assert workforce.WorkforceConnector().run(_ctx(client, latest_filed_year=2024)).claims == []
    assert client.calls == []


def test_number_first_column_thousands_and_years():
    assert workforce.extract("Antall årsverk 3   4", ocr=False)["count"] == 3
    assert workforce.extract("Gjennomsnittlig antall årsverk: 1 200")["count"] == 1200
    assert workforce.extract("Antall årsverk 2024 2023") is None
    assert workforce.extract("Antall årsverk i regnskapsåret 2024: 12,5")["count"] == 12.5
    assert workforce.extract("Antall årsverk i regnskapsaret 8 40")["count"] == 8.4
    assert workforce.extract("Antall årsverk 8 40", ocr=False)["count"] == 8


def test_history_slot_within_skips_long_queue(monkeypatch):
    from signalpost import registry
    monkeypatch.setattr(registry, "_history_last", [time.monotonic() + 100])
    assert registry.history_slot_within(5) is False
    monkeypatch.setattr(registry, "_history_last", [0.0])
    assert registry.history_slot_within(5) is True


def test_published_site_must_show_our_number_or_name(monkeypatch):
    from types import SimpleNamespace
    from signalpost.web import connector
    v = lambda *kinds: SimpleNamespace(signals=[SimpleNamespace(kind=k) for k in kinds])
    monkeypatch.setattr(connector, "_registry_facts", lambda ctx: {"name": "QUBBS HOLDING AS"})
    assert connector._site_lacks_our_name(None, v("registry_declared", "registry_email"))
    assert connector._site_lacks_our_name(None, v("legal_name_match", "registry_email"))  # brand in title: not enough
    assert connector._site_lacks_our_name(None, v("org_number_on_source")) is None
    assert connector._site_lacks_our_name(None, v("legal_name_in_text", "registry_email")) is None
    monkeypatch.setattr(connector, "_registry_facts", lambda ctx: {"name": "VIGGO VENN IN VEST AS"})
    assert connector._site_lacks_our_name(None, v("registry_email_domain", "registry_declared"))
    assert connector._site_lacks_our_name(None, v("domain_name_match", "role_name")) is None
    assert connector._site_lacks_our_name(None, v("registry_declared", "registered_address")) is None


def test_a_page_that_only_says_parked_is_parked():
    from signalpost.web.crawl import is_parked_page
    assert is_parked_page("<html><head><title>Parked</title></head><body><h1>Parked</h1></body></html>", "Parked", "http://arcticsubsea.no/")
    assert not is_parked_page("<html><body><h1>Parkering</h1><p>Vi har parked biler i sentrum</p></body></html>", "Parkering Vi har parked biler i sentrum", "http://x.no/")


def test_dns_temporary_failure_is_not_cached(monkeypatch):
    import socket
    from signalpost import http as sp_http
    client = sp_http.BudgetedHttpClient.__new__(sp_http.BudgetedHttpClient)
    import threading, concurrent.futures
    client._dns_lock = threading.Lock(); client._dns_cache = {}; client.dns_timeout = 1.0
    client._dns_executor = concurrent.futures.ThreadPoolExecutor(2)
    calls = []
    def flaky(host, port):
        calls.append(host)
        raise socket.gaierror(socket.EAI_AGAIN, "temporary failure")
    monkeypatch.setattr(socket, "getaddrinfo", flaky)
    assert client.dns_resolves("example.no") is False and "example.no" not in client._dns_cache
    def nxdomain(host, port):
        raise socket.gaierror(socket.EAI_NONAME, "no such name")
    monkeypatch.setattr(socket, "getaddrinfo", nxdomain)
    assert client.dns_resolves("nosuch.no") is False and client._dns_cache["nosuch.no"] is False
