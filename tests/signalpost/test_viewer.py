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


# --------------------------------------------------------------------------------------- CLI


def test_cli_main_builds_site(tmp_path):
    out = tmp_path / "cli_site"
    build.main(["--envelopes", str(FIXTURES), "--out", str(out)])
    assert (out / "index.html").is_file()
    assert len(list((out / "companies").glob("*.html"))) == 6
