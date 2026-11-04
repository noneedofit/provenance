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
    assert "rose 50.0% from 2024 to 2025" in joined


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
    assert "went from Old Name AS to New Name AS" in change_sentences[0].text


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


def test_parent_company_summary_uses_group_accounts() -> None:
    ev = make_evidence("https://data.brreg.no/regnskapsregisteret/regnskap/" + ORG)
    own = make_claim(family="financials", field="revenue", value={"amount": 914_000_000, "currency": "NOK"},
                     evidence_ids=[ev.evidence_id], reporting_period=("2025-01-01", "2025-12-31"))
    group = make_claim(family="financials", field="group_revenue", value={"amount": 31_992_000_000, "currency": "NOK"},
                       evidence_ids=[ev.evidence_id], reporting_period=("2025-01-01", "2025-12-31"))
    group_prev = make_claim(family="financial_history", field="group_revenue", value={"amount": 30_638_000_000, "currency": "NOK"},
                            evidence_ids=[ev.evidence_id], reporting_period=("2024-01-01", "2024-12-31"))
    envelope = make_envelope(run_id="run-1", claims=[own, group, group_prev], evidence=[ev])
    joined = " ".join(s.text for s in build_summary(envelope).sentences)
    assert "own (parent-company) accounts" in joined
    assert "the group reported revenue of NOK 31.99B" in joined
    assert "Group revenue rose 4.4% from 2024 to 2025" in joined


def test_news_without_a_title_still_makes_a_whole_sentence() -> None:
    ev = make_evidence("https://example.no/feed/")
    untitled = make_claim(family="activity", field="activity_item", value={"title": None, "url": "https://example.no/a"},
                          value_key="https://example.no/a", evidence_ids=[ev.evidence_id], effective_date="2026-03-01")
    older = make_claim(family="activity", field="activity_item", value={"title": "Ny avdeling", "url": "https://example.no/b"},
                       value_key="https://example.no/b", evidence_ids=[ev.evidence_id], effective_date="2026-01-01")
    for claims, expected in (([untitled, older], '"Ny avdeling" (2026-01-01)'), ([untitled], "most recently on 2026-03-01")):
        texts = [s.text for s in build_summary(make_envelope(run_id="r", claims=claims, evidence=[ev])).sentences]
        assert not [t for t in texts if t.endswith("has .")]
        assert any(expected in t for t in texts), texts


def test_an_operating_loss_alone_reads_as_a_sentence() -> None:
    ev = make_evidence("https://data.brreg.no/regnskapsregisteret/regnskap/" + ORG)
    loss = make_claim(family="financials", field="operating_result", value={"amount": -2333, "currency": "NOK"},
                      evidence_ids=[ev.evidence_id], reporting_period=("2025-01-01", "2025-12-31"))
    texts = [s.text for s in build_summary(make_envelope(run_id="r", claims=[loss], evidence=[ev])).sentences]
    assert any(t.startswith("It had an operating loss of NOK 2,333") for t in texts), texts


def test_a_quoted_register_activity_ends_with_one_full_stop() -> None:
    ev = make_evidence("https://data.brreg.no/enhetsregisteret/api/enheter/" + ORG)
    act = make_claim(family="description", field="registry_activity", value="Utleie av fast eiendom.", evidence_ids=[ev.evidence_id])
    texts = [s.text for s in build_summary(make_envelope(run_id="r", claims=[act], evidence=[ev])).sentences]
    assert any(t.endswith('"Utleie av fast eiendom".') for t in texts), texts
    assert not [t for t in texts if '."' in t]


def test_the_no_changes_sentence_cites_the_rechecked_facts():
    ev = make_evidence("https://data.brreg.no/enhetsregisteret/api/enheter/" + ORG)
    claim = make_claim(family="identity", field="legal_name", value="Example AS", evidence_ids=[ev.evidence_id])
    env = make_envelope(run_id="run-2", claims=[claim], evidence=[ev])
    env.run.previous_run_id = "run-1"
    sentences = build_summary(env).sentences
    no_change = [s for s in sentences if s.text.startswith("No material changes")]
    assert no_change and no_change[0].claim_ids == [claim.claim_id]
    assert all(s.claim_ids for s in sentences)
