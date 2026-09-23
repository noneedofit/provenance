from __future__ import annotations

from helpers import ORG, make_claim, make_envelope, make_evidence

from signalpost.models import Change
from signalpost.synthesis import build_summary, format_money, summary_markdown


def test_format_money_abbreviations() -> None:
    assert format_money({"amount": 1_500_000_000, "currency": "NOK"}) == "NOK 1.50B"
    assert format_money({"amount": 2_300_000, "currency": "NOK"}) == "NOK 2.30M"
    assert format_money({"amount": 4_200, "currency": "NOK"}) == "NOK 4,200"
    assert format_money({"amount": -500_000, "currency": "NOK"}) == "-NOK 500,000"


def test_every_sentence_has_at_least_one_claim_id() -> None:
    ev = make_evidence("https://example.no")
    desc = make_claim(family="description", field="company_description", value="We build boats.", evidence_ids=[ev.evidence_id])
    form = make_claim(family="identity", field="legal_form", value="AS", evidence_ids=[ev.evidence_id])
    revenue = make_claim(
        family="financials", field="revenue", value={"amount": 1_000_000, "currency": "NOK"},
        evidence_ids=[ev.evidence_id], reporting_period=("2025-01-01", "2025-12-31"),
    )
    ceo = make_claim(
        family="leadership", field="role", value={"role_code": "DAGL", "name": "Kari Nordmann"},
        value_key="DAGL|kari nordmann", evidence_ids=[ev.evidence_id],
    )
    website = make_claim(family="website", field="official_website", value="https://example.no", evidence_ids=[ev.evidence_id])
    envelope = make_envelope(run_id="run-1", claims=[desc, form, revenue, ceo, website], evidence=[ev])

    summary = build_summary(envelope)
    assert summary.sentences
    for sentence in summary.sentences:
        assert sentence.claim_ids, f"sentence without claim_ids: {sentence.text}"


def test_revenue_trend_percentage() -> None:
    ev = make_evidence("https://data.brreg.no/regnskapsregisteret/regnskap/" + ORG)
    prev = make_claim(
        family="financial_history", field="revenue", value={"amount": 100_000, "currency": "NOK"},
        evidence_ids=[ev.evidence_id], reporting_period=("2024-01-01", "2024-12-31"),
    )
    latest = make_claim(
        family="financials", field="revenue", value={"amount": 150_000, "currency": "NOK"},
        evidence_ids=[ev.evidence_id], reporting_period=("2025-01-01", "2025-12-31"),
    )
    envelope = make_envelope(run_id="run-1", claims=[prev, latest], evidence=[ev])
    summary = build_summary(envelope)
    joined = " ".join(s.text for s in summary.sentences)
    assert "up 50.0%" in joined


def test_unknowns_list_not_available_families() -> None:
    from signalpost.models import FamilyState

    ev = make_evidence("https://example.no")
    claim = make_claim(family="identity", field="legal_form", value="AS", evidence_ids=[ev.evidence_id])
    families = {
        "jobs": FamilyState(family="jobs", availability="not_available", reason="checked: none found"),
        "website": FamilyState(family="website", availability="failed", reason="no candidate site passed verification"),
    }
    envelope = make_envelope(run_id="run-1", claims=[claim], evidence=[ev], families=families)
    summary = build_summary(envelope)
    assert any("job" in u.lower() for u in summary.unknowns)
    assert any("website" in u.lower() for u in summary.unknowns)


def test_what_changed_sentence_cites_change_claim() -> None:
    ev = make_evidence("https://example.no")
    claim = make_claim(family="identity", field="legal_form", value="AS", evidence_ids=[ev.evidence_id])
    envelope = make_envelope(run_id="run-2", claims=[claim], evidence=[ev])
    envelope.changes = [
        Change(
            change_id="x_test",
            change_type="identity_changed",
            family="identity",
            field="legal_name",
            claim_id=claim.claim_id,
            previous_value="Old Name AS",
            current_value="New Name AS",
            detected_at="2026-02-01T00:00:00Z",
        )
    ]
    summary = build_summary(envelope)
    change_sentences = [s for s in summary.sentences if "previous check" in s.text]
    assert len(change_sentences) == 1
    assert change_sentences[0].claim_ids == [claim.claim_id]
    assert "run-2" in change_sentences[0].text


def test_sparse_shell_summary() -> None:
    ev = make_evidence("https://data.brreg.no/enhetsregisteret/api/enheter/" + ORG)
    form = make_claim(family="identity", field="legal_form", value="AS", evidence_ids=[ev.evidence_id])
    municipality = make_claim(family="identity", field="municipality", value="Oslo", evidence_ids=[ev.evidence_id])
    envelope = make_envelope(run_id="run-1", claims=[form, municipality], evidence=[ev], legal_name="Holding AS")

    summary = build_summary(envelope)
    joined = " ".join(s.text for s in summary.sentences)
    assert "Holding AS" in joined


def test_summary_markdown_renders() -> None:
    ev = make_evidence("https://example.no")
    claim = make_claim(family="identity", field="legal_form", value="AS", evidence_ids=[ev.evidence_id])
    envelope = make_envelope(run_id="run-1", claims=[claim], evidence=[ev])
    envelope.summary = build_summary(envelope)
    md = summary_markdown(envelope)
    assert md.startswith("#")
    assert "AS" in md
