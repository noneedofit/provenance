"""Open-data sources: the bundled places index (Overture + OSM), Mattilsynet inspection ratings, and the
web-agency credit-link filter for profiles."""
from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

from bs4 import BeautifulSoup

from signalpost.activity import smilefjes
from signalpost.caches import places
from signalpost.context import Response
from signalpost.web.extract import _is_credit_link


def _write_gz(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_places_index_matches_on_phone_email_and_name_postcode(tmp_path, monkeypatch):
    snap = tmp_path / "snap"
    snap.mkdir()
    key = places.contact_key
    _write_gz(snap / "places.jsonl.gz", [
        ["p1", "Trøndelag Betong", "7608", [key("tel", "99264550")], [], ["https://trondelag-betong.no/"], []],
        ["p2", "Vestbo", "5000", [key("tel", "55000000")], [key("mail", "post@vestbo.no")], ["https://vestbo.no/"], ["https://facebook.com/vestbo"]],
    ])
    _write_gz(snap / "osm.jsonl.gz", [["923456783", "node/1", "Kafe", "https://kafe.no/", {}]])
    monkeypatch.setattr(places, "PLACES_SNAPSHOT", snap / "places.jsonl.gz")
    monkeypatch.setattr(places, "OSM_SNAPSHOT", snap / "osm.jsonl.gz")
    info = places.build(tmp_path)
    assert info["places"] == 2 and info["osm"] == 1
    idx = places.Places.load(tmp_path)

    by_phone = idx.match(name="TRØNDELAG BETONG AS", postcode="7608", phones=["+47 992 64 550"], emails=[])
    assert [m["id"] for m in by_phone] == ["p1"]
    assert set(by_phone[0]["how"]) == {"phone", "name", "name_postcode"}

    by_email = idx.match(name="BORETTSLAGET X", postcode="9999", phones=[], emails=["POST@vestbo.no"])
    assert [m["id"] for m in by_email] == ["p2"] and by_email[0]["how"] == ["email"]

    assert idx.match(name="NOBODY AS", postcode="0001", phones=["11111111"], emails=[]) == []
    # The snapshot holds no raw contact data.
    raw = gzip.open(snap / "places.jsonl.gz", "rt").read()
    assert "99264550" not in raw and "post@vestbo.no" not in raw
    osm = idx.osm_for(["923456783"])
    assert osm[0]["website"] == "https://kafe.no/" and osm[0]["url"] == "https://www.openstreetmap.org/node/1"


class _PageClient:
    def __init__(self, pages: dict[str, str], index: dict):
        self.pages = pages
        self.index = index
        self.calls: list[str] = []

    def get(self, url, *, org=None, purpose="", accept="*/*", respect_robots=True, **_):
        self.calls.append(url)
        body = json.dumps(self.index) if url == smilefjes.INDEX_URL else self.pages.get(url)
        ok = body is not None
        return Response(url=url, final_url=url, redirect_chain=[url], status=200 if ok else 404, headers={},
                        body=(body or "").encode(), retrieved_at="2026-10-06T00:00:00Z", content_sha256="x" if ok else "",
                        elapsed_ms=1, requests_used=1, error=None if ok else "http_404")

    def remaining(self, org=None):
        return 100


def _place_page(name: str, orgnr: str) -> str:
    return (f"<html><body><h1>{name}</h1><p>Orgnr. {orgnr}</p><div><h2 class='x'>Siste tilsynsresultat:</h2>"
            f"<div class='w' title='Spisestedet har fått blidt smilefjes.'><svg></svg></div></div>"
            f"<p>Siste tilsynsresultat: 04.04.2024</p></body></html>")


def test_smilefjes_publishes_only_when_page_org_number_is_ours():
    smilefjes.reset_for_tests()
    index = {"lookup": [
        ["/spisested/bergen/brod_og_vin/", "Brød og Vin Restaurant", "Fjøsangerveien 39", "", "5054", "Bergen", [["1", "2024-04-04"]]],
        ["/spisested/bergen/brod_og_vin_2/", "Brød og Vin Kafe", "Fjøsangerveien 41", "", "5054", "Bergen", [["0", "2025-01-01"]]],
    ]}
    client = _PageClient({
        smilefjes.BASE_URL + "/spisested/bergen/brod_og_vin/": _place_page("Brød og Vin Restaurant", "884225442"),
        smilefjes.BASE_URL + "/spisested/bergen/brod_og_vin_2/": _place_page("Brød og Vin Kafe", "999999999"),
    }, index)
    ctx = SimpleNamespace(org="884225442", client=client, bulk={}, registry={"subunits": []},
                          shared={"registry_facts": {"name": "BRØD OG VIN AS", "postcode": "5054", "aliases": []}})
    out = smilefjes.collect(ctx)
    assert out["checked"] and len(out["claims"]) == 1
    claim = out["claims"][0]
    assert claim.value["place_org_number"] == "884225442" and claim.value["grade_code"] == 1
    assert claim.value["inspected_on"] == "2024-04-04" and claim.identity_basis == "org_number_on_source"
    assert "Orgnr. 884225442" in out["evidence"][0].span and "blidt smilefjes" in out["evidence"][0].span
    smilefjes.reset_for_tests()


def test_credit_link_is_dropped_but_company_links_are_kept():
    html = ('<footer><a href="https://facebook.com/acme">FB</a>'
            '<p>Nettside levert av <a href="https://facebook.com/byraa">Byrå</a></p>'
            '<span><a href="https://instagram.com/acme">IG</a></span></footer>')
    soup = BeautifulSoup(html, "lxml")
    flags = {a["href"]: _is_credit_link(a) for a in soup.select("a")}
    assert flags == {"https://facebook.com/acme": False, "https://facebook.com/byraa": True, "https://instagram.com/acme": False}


def test_smilefjes_page_is_authoritative_when_index_is_stale():
    smilefjes.reset_for_tests()
    index = {"lookup": [["/spisested/a/x/", "Kafe Fjord", "Gate 1", "", "5054", "Bergen", [["2", "2023-01-01"]]]]}
    client = _PageClient({smilefjes.BASE_URL + "/spisested/a/x/": _place_page("Kafe Fjord", "884225442")}, index)
    ctx = SimpleNamespace(org="884225442", client=client, bulk={}, registry={"subunits": []},
                          shared={"registry_facts": {"name": "KAFE FJORD AS", "postcode": "5054", "aliases": []}})
    claim = smilefjes.collect(ctx)["claims"][0]
    # The page shows a newer inspection (04.04.2024, smiling face) than the index (2023, straight mouth).
    assert claim.value["inspected_on"] == "2024-04-04" and claim.value["grade"] == "smiling face"
    assert claim.value["grade_code"] is None
    smilefjes.reset_for_tests()
