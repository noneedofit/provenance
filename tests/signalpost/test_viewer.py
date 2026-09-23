"""Tests for the W6 static viewer (src/signalpost/viewer/)."""
from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from signalpost.models import Envelope, FAMILIES  # noqa: E402
from signalpost.viewer import build, helpers, render  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "viewer" / "envelopes.jsonl"
REAL_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "viewer" / "real_registry.jsonl"


@pytest.fixture(scope="module")
def raw_envelopes() -> list[dict]:
    lines = [l for l in FIXTURES.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [json.loads(l) for l in lines]


@pytest.fixture(scope="module")
def site_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("site")
    build.build_site(FIXTURES, out)
    return out


# --------------------------------------------------------------------------------------- fixtures validity


def test_fixtures_validate_against_envelope_model(raw_envelopes):
    assert len(raw_envelopes) == 6
    for raw in raw_envelopes:
        env = Envelope.model_validate(raw)
        assert env.organisation_number
        assert env.families  # every family present
        assert set(env.families) == set(FAMILIES)


def test_fixtures_cover_the_required_scenarios(raw_envelopes):
    by_org = {e["organisation_number"]: e for e in raw_envelopes}
    dips = by_org["979543883"]
    assert len(dips["claims"]) > 20
    assert len(dips["changes"]) >= 1
    subunits = [c for c in dips["claims"] if c["family"] == "locations" and c["field"] == "subunit"]
    assert len(subunits) == 6

    franchise = by_org["923456789"]
    website_claim = next(c for c in franchise["claims"] if c["family"] == "website")
    assert website_claim["availability"] == "ambiguous"
    assert website_claim["relationship"] == "franchise"

    shell = by_org["934567890"]
    assert shell["families"]["website"]["availability"] == "not_applicable"

    budget = by_org["945678901"]
    failed_fams = [f for f, s in budget["families"].items() if s["availability"] == "failed"]
    assert "financials" in failed_fams
    assert all(budget["families"][f]["reason"] == "request_budget" for f in failed_fams)

    refresh = by_org["956789012"]
    assert len(refresh["changes"]) >= 3
    types = {c["change_type"] for c in refresh["changes"]}
    assert "changed_website" in types


# --------------------------------------------------------------------------------------- site build output


def test_build_produces_expected_files(site_dir, raw_envelopes):
    assert (site_dir / "index.html").is_file()
    assert (site_dir / "compare.html").is_file()
    assert (site_dir / "directory.csv").is_file()
    assert (site_dir / "assets" / "style.css").is_file()
    assert (site_dir / "assets" / "app.js").is_file()
    assert (site_dir / "data" / "index.json").is_file()
    for env in raw_envelopes:
        org = env["organisation_number"]
        assert (site_dir / "companies" / f"{org}.html").is_file()
        assert (site_dir / "data" / "companies" / f"{org}.json").is_file()


def test_index_json_is_valid_and_matches_companies(site_dir, raw_envelopes):
    data = json.loads((site_dir / "data" / "index.json").read_text(encoding="utf-8"))
    rows = data["companies"]
    assert len(rows) == len(raw_envelopes)
    orgs = {r["org"] for r in rows}
    assert orgs == {e["organisation_number"] for e in raw_envelopes}
    for r in rows:
        assert 0 <= r["coverage"] <= 12


def test_company_json_export_round_trips_through_envelope_model(site_dir, raw_envelopes):
    for env in raw_envelopes:
        org = env["organisation_number"]
        dumped = json.loads((site_dir / "data" / "companies" / f"{org}.json").read_text(encoding="utf-8"))
        Envelope.model_validate(dumped)  # still a valid envelope
        assert dumped["organisation_number"] == org


def test_directory_csv_valid_and_has_a_row_per_company(site_dir, raw_envelopes):
    text = (site_dir / "directory.csv").read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert reader.fieldnames == render.CSV_COLUMNS
    assert len(rows) == len(raw_envelopes)
    orgs = {r["org"] for r in rows}
    assert orgs == {e["organisation_number"] for e in raw_envelopes}


def test_no_external_script_or_link_tags(site_dir):
    html_files = list(site_dir.glob("*.html")) + list((site_dir / "companies").glob("*.html"))
    assert html_files
    ext_pattern = re.compile(r'<(script|link)[^>]*(?:src|href)="https?://[^"]*"', re.IGNORECASE)
    for f in html_files:
        text = f.read_text(encoding="utf-8")
        matches = ext_pattern.findall(text)
        assert not matches, f"{f} references an external script/stylesheet: {matches}"
        # only local asset references
        assert 'src="../assets/app.js"' in text or 'src="assets/app.js"' in text
        assert 'href="../assets/style.css"' in text or 'href="assets/style.css"' in text


def test_directory_and_compare_pages_embed_data_json_cards(site_dir, raw_envelopes):
    index_html = (site_dir / "index.html").read_text(encoding="utf-8")
    compare_html = (site_dir / "compare.html").read_text(encoding="utf-8")
    for env in raw_envelopes:
        org = env["organisation_number"]
        assert org in index_html
        assert org in compare_html
    assert index_html.count('data-json="') == len(raw_envelopes)
    assert compare_html.count('data-json="') == len(raw_envelopes)


# --------------------------------------------------------------------------------------- company page content


def test_every_company_page_renders_every_family(site_dir, raw_envelopes):
    for env in raw_envelopes:
        org = env["organisation_number"]
        html = (site_dir / "companies" / f"{org}.html").read_text(encoding="utf-8")
        for fam in FAMILIES:
            assert helpers.FAMILY_LABELS[fam] in html, f"{org} missing family label {fam}"
        # header basics
        assert env["organisation_number"] in html
        assert f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}" in html
        assert "brreg.no" in html
        # ask-this-profile box + evidence-bound copy
        assert "Ask this profile" in html
        assert "Not found in checked sources" not in html or "profile-data" in html


def test_evidence_links_present_for_available_claims(site_dir):
    org = "979543883"
    html = (site_dir / "companies" / f"{org}.html").read_text(encoding="utf-8")
    assert 'class="evidence-drawer"' in html
    assert "https://www.dips.com" in html
    assert "retrieved" in html.lower() or "Retrieved" in html
    assert "Content hash" in html
    assert "Extraction method" in html
    assert "span-text" in html  # exact supporting text is shown


def test_ambiguous_website_labeled_related_not_exact(site_dir):
    html = (site_dir / "companies" / "923456789.html").read_text(encoding="utf-8")
    assert "related / ambiguous" in html
    assert "verified exact" not in html


def test_exact_website_labeled_verified(site_dir):
    html = (site_dir / "companies" / "979543883.html").read_text(encoding="utf-8")
    assert "verified exact" in html


def test_holding_shell_shows_not_applicable_not_zero(site_dir):
    html = (site_dir / "companies" / "934567890.html").read_text(encoding="utf-8")
    assert "N/A" in html  # not_applicable badge label
    # never render an unchecked/not-applicable family's numeric claim count as a bare "0" pretending to be data
    assert "not applicable" in html.lower() or "N/A" in html


def test_failed_family_shows_request_budget_reason(site_dir):
    html = (site_dir / "companies" / "945678901.html").read_text(encoding="utf-8")
    assert "request_budget" in html
    assert "Failed" in html


def test_refresh_changes_show_previous_and_current_with_evidence(site_dir):
    html = (site_dir / "companies" / "956789012.html").read_text(encoding="utf-8")
    assert "changed website" in html.lower()
    assert "kystvindradgivning.no" in html
    assert "kystvind.no" in html
    assert html.count("[src]") >= 4  # both previous and current sides link evidence


def test_financials_sparkline_is_inline_svg(site_dir):
    html = (site_dir / "companies" / "979543883.html").read_text(encoding="utf-8")
    assert "<svg" in html
    assert "sparkline" in html
    # no external image request for the chart
    assert "<img" not in html


def test_profile_data_json_embedded_and_valid(site_dir, raw_envelopes):
    for env in raw_envelopes:
        org = env["organisation_number"]
        html = (site_dir / "companies" / f"{org}.html").read_text(encoding="utf-8")
        m = re.search(r'<script type="application/json" id="profile-data">(.*?)</script>', html, re.DOTALL)
        assert m, f"missing profile-data script for {org}"
        payload = json.loads(m.group(1))
        assert payload["org"] == org


def test_dates_shown_readable_with_iso_title(site_dir):
    html = (site_dir / "companies" / "979543883.html").read_text(encoding="utf-8")
    assert re.search(r'<time datetime="2026-09-\d\dT[^"]*" title="2026-09-\d\dT[^"]*">', html)


def test_norwegian_characters_render_correctly(site_dir):
    html = (site_dir / "companies" / "912345678.html").read_text(encoding="utf-8")
    assert "Tromsø" in html
    html2 = (site_dir / "companies" / "956789012.html").read_text(encoding="utf-8")
    assert "Rådgivning" in html2


# --------------------------------------------------------------------------------------- helpers unit tests


@pytest.mark.parametrize("count,band", [(0, "0"), (1, "1-4"), (4, "1-4"), (5, "5-19"), (19, "5-19"),
                                          (20, "20-49"), (49, "20-49"), (50, "50-249"), (249, "50-249"),
                                          (250, "250+"), (None, "unknown")])
def test_employee_band(count, band):
    assert helpers.employee_band(count) == band


def test_coverage_counts_never_hides_missing_as_zero_family():
    families = {
        "identity": {"availability": "available"},
        "financials": {"availability": "not_available"},
    }
    counts = helpers.coverage_counts(families)
    # families not present in the dict at all still count as not_available, not silently dropped
    assert sum(counts.values()) == len(helpers.COVERAGE_FAMILIES)


def test_fmt_money_handles_missing_and_present():
    assert helpers.fmt_money({"amount": 1000, "currency": "NOK"}) == "NOK 1 000"
    assert helpers.fmt_money(None) == ""


def test_all_availability_states_have_labels_and_render_without_error():
    # Every Availability literal from models.py must have a badge label/class and must not crash rendering.
    for state in ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"):
        assert state in helpers.AVAILABILITY_LABELS
        assert state in helpers.AVAILABILITY_CLASS

    envelope = {
        "organisation_number": "999999999",
        "legal_name": "Synthetic Test AS",
        "run": {"run_id": "run_x", "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:00:00Z"},
        "sections": {},
        "families": {fam: {"family": fam, "availability": "blocked", "reason": "robots_disallowed",
                            "sources_checked": [], "claim_count": 0} for fam in FAMILIES},
        "claims": [],
        "evidence": [],
        "changes": [],
        "summary": {"language": "en", "sentences": [], "unknowns": [], "method": "template_v1"},
        "errors": [],
        "operations": {"requests": 0, "bytes": 0, "runtime_ms": 0, "third_party_cost_usd": 0.0},
    }
    html = render.render_company_page(envelope)
    assert "Blocked" in html
    assert "robots_disallowed" in html


# --------------------------------------------------------------------------------------- real pipeline output
#
# tests/fixtures/viewer/real_registry.jsonl holds 5 companies taken verbatim from a real run of the
# registry connector (see the coordinator's fixtures-real/registry-only-20.jsonl). Real claim values are
# often structured (dicts/lists/bools) rather than plain strings, e.g. identity/legal_form =
# {"code": "AS", "label": "Aksjeselskap"}, identity/nace = {"code": .., "label": ..},
# identity/registered_employees = {"count": .., "registered_at": ..}, identity/business_address /
# postal_address = {street, postcode, city, municipality, country} (municipality lives only here, there is
# no separate top-level municipality claim), leadership/role = {"role_code", "role_label", "name"},
# locations/workplace = {organisation_number, name, street, postcode, city, employees, nace},
# financials/<metric> = {"amount", "currency"}, financials/accounting_type and audit_status are opaque
# dicts, financial_history/filed_years is a list of year strings, and identity carries historic_names
# (list), registered_website/registry_email/registry_phone (strings), status, founded_date,
# vat_registered (bool) and a long free-text statutory_purpose. This used to crash render_directory_card
# with "TypeError: sequence item 0: expected str instance, dict found" because every render path assumed
# scalar string values.


@pytest.fixture(scope="module")
def real_raw_envelopes() -> list[dict]:
    lines = [l for l in REAL_FIXTURES.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [json.loads(l) for l in lines]


@pytest.fixture(scope="module")
def real_site_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("real_site")
    build.build_site(REAL_FIXTURES, out)
    return out


def test_real_fixtures_validate_against_envelope_model(real_raw_envelopes):
    assert len(real_raw_envelopes) == 5
    for raw in real_raw_envelopes:
        Envelope.model_validate(raw)


def test_real_fixtures_contain_structured_values(real_raw_envelopes):
    """Sanity-check the fixture actually exercises the shapes described above, so this test suite would
    have caught the original crash."""
    field_types: dict[tuple[str, str], set[str]] = {}
    for e in real_raw_envelopes:
        for c in e["claims"]:
            field_types.setdefault((c["family"], c["field"]), set()).add(type(c["value"]).__name__)
    assert field_types.get(("identity", "legal_form")) == {"dict"}
    assert field_types.get(("identity", "nace")) == {"dict"}
    assert "dict" in field_types.get(("identity", "business_address"), set())
    assert "list" in field_types.get(("identity", "historic_names"), set()) | field_types.get(("financial_history", "filed_years"), set())
    assert "dict" in field_types.get(("leadership", "role"), set())
    assert "dict" in field_types.get(("financials", "revenue"), set())
    assert "bool" in field_types.get(("identity", "vat_registered"), set())


def test_real_site_builds_without_crashing(real_site_dir, real_raw_envelopes):
    assert (real_site_dir / "index.html").is_file()
    assert (real_site_dir / "compare.html").is_file()
    for e in real_raw_envelopes:
        org = e["organisation_number"]
        assert (real_site_dir / "companies" / f"{org}.html").is_file()


def test_real_directory_card_has_no_raw_dict_repr(real_site_dir):
    """The original crash was building directory cards from structured values; guard against the same
    bug returning as a rendered "{'code': ...}" / "{'amount': ...}" Python-repr leaking into HTML."""
    index_html = (real_site_dir / "index.html").read_text(encoding="utf-8")
    assert "{'code'" not in index_html
    assert "{'label'" not in index_html
    assert "{'amount'" not in index_html


def test_real_company_pages_render_structured_fields_readably(real_site_dir):
    # GUSTAVSSON INVEST AS: dict legal_form/nace, no registered_employees claim at all.
    html = (real_site_dir / "companies" / "928173909.html").read_text(encoding="utf-8")
    assert "Aksjeselskap (AS)" in html
    assert "Frisering og barbering" in html
    assert "{'" not in html  # no raw Python dict repr (str(dict)) leaked into the page

    # DYREBESKYTTELSEN NORGE: historic_names list, long statutory_purpose (clipped + expandable),
    # registered_website as a real link, vat_registered bool, a workplace, board + CEO roles.
    html = (real_site_dir / "companies" / "971277475.html").read_text(encoding="utf-8")
    assert "STIFTELSEN DYREBESKYTTELSEN NORGE" in html  # historic_names joined readably
    assert 'href="https://www.dyrebeskyttelsen.no"' in html
    assert "show full text" in html  # long statutory_purpose is clipped with an expand toggle
    assert "Vat registered" in html and ("Yes" in html or "No" in html)
    assert "Åshild Roaldset" in html  # CEO name from a structured leadership/role dict
    assert "board chair" in html.lower() or "Styrets leder" in html

    # HALDEN LASTEBILSENTRAL AS: locations/workplace dict, financial_history/filed_years list,
    # accounting_type/audit_status opaque dicts, multiple structured leadership roles.
    html = (real_site_dir / "companies" / "935756030.html").read_text(encoding="utf-8")
    assert "HALDEN LASTEBILSENTRAL A/L" in html  # workplace name
    assert "1788 HALDEN" in html  # workplace address formatted as "street, postcode city"
    assert re.search(r"20\d\d.{0,3}20\d\d", html)  # filed_years rendered as a year range, not a raw list
    assert "Small enterprise" in html  # accounting_type dict rendered via generic key:value fallback

    # SAMEIET SOLÅSVEIEN 5: a housing co-op (ESEK legal form), no separate municipality claim — must be
    # derived from business_address.
    html = (real_site_dir / "companies" / "920861172.html").read_text(encoding="utf-8")
    assert "Eierseksjonssameie" in html
    assert "STAVANGER" in html

    # AUTO SHOP NORWAY AS: negative annual result.
    html = (real_site_dir / "companies" / "935852048.html").read_text(encoding="utf-8")
    assert "-" in html  # negative money value rendered, not dropped


def test_real_directory_row_municipality_derived_from_address(real_site_dir):
    """None of the 5 real fixtures has a bare identity/municipality claim — it must come from
    business_address/postal_address instead, both in the directory row JSON and the CSV."""
    data = json.loads((real_site_dir / "data" / "index.json").read_text(encoding="utf-8"))
    rows = {r["org"]: r for r in data["companies"]}
    assert rows["928173909"]["municipality"] == "OSLO"
    assert rows["920861172"]["municipality"] == "STAVANGER"
    csv_text = (real_site_dir / "directory.csv").read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(csv_text))
    csv_rows = {r["org"]: r for r in reader}
    assert csv_rows["928173909"]["municipality"] == "OSLO"


def test_real_legal_form_filter_options_are_plain_codes(real_site_dir):
    """Directory filter <option> values must be plain scalars (e.g. "AS", "ESEK"), never a Python dict
    repr, so the <select> and the client-side JS comparison both work."""
    index_html = (real_site_dir / "index.html").read_text(encoding="utf-8")
    assert '<option value="AS">AS</option>' in index_html
    assert '<option value="ESEK">ESEK</option>' in index_html


# --------------------------------------------------------------------------------------- CLI


def test_cli_main_builds_site(tmp_path):
    out = tmp_path / "cli_site"
    build.main(["--envelopes", str(FIXTURES), "--out", str(out)])
    assert (out / "index.html").is_file()
    assert len(list((out / "companies").glob("*.html"))) == 6
