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
