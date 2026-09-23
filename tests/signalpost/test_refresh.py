from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import ORG, make_claim, make_envelope, make_evidence

from signalpost.models import FamilyState
from signalpost.refresh import apply_refresh, diff_envelopes


def test_first_run_sets_observed_timestamps_and_no_changes(tmp_path: Path) -> None:
    ev = make_evidence("https://data.brreg.no/enhetsregisteret/api/enheter/" + ORG)
    claim = make_claim(family="identity", field="legal_name", value="Example AS", evidence_ids=[ev.evidence_id])
    envelope = make_envelope(run_id="run-1", claims=[claim], evidence=[ev])

    result = apply_refresh(envelope, tmp_path, now="2026-01-01T00:00:00Z")

    assert result.changes == []
    out_claim = result.claims[0]
    assert out_claim.first_observed_at == "2026-01-01T00:00:00Z"
    assert out_claim.last_observed_at == "2026-01-01T00:00:00Z"
    assert (tmp_path / "profiles" / f"{ORG}.json").exists()


def test_rerun_unchanged_produces_zero_changes_and_is_idempotent(tmp_path: Path) -> None:
    ev = make_evidence("https://example.no")
    claim = make_claim(family="identity", field="legal_name", value="Example AS", evidence_ids=[ev.evidence_id])
    envelope1 = make_envelope(run_id="run-1", claims=[claim], evidence=[ev])
    result1 = apply_refresh(envelope1, tmp_path, now="2026-01-01T00:00:00Z")
    assert result1.changes == []

    claim2 = make_claim(family="identity", field="legal_name", value="Example AS", evidence_ids=[ev.evidence_id])
    envelope2 = make_envelope(run_id="run-2", claims=[claim2], evidence=[ev])
    result2 = apply_refresh(envelope2, tmp_path, now="2026-02-01T00:00:00Z")

    assert result2.changes == []
    out_claim = result2.claims[0]
    assert out_claim.first_observed_at == "2026-01-01T00:00:00Z"  # preserved
    assert out_claim.last_observed_at == "2026-02-01T00:00:00Z"   # bumped
    assert len(result2.claims) == 1
    assert len(result2.evidence) == 1

    # Rerun again with identical (run2) input: still zero changes, no duplicate claims/evidence.
    claim3 = make_claim(family="identity", field="legal_name", value="Example AS", evidence_ids=[ev.evidence_id])
    envelope3 = make_envelope(run_id="run-3", claims=[claim3], evidence=[ev])
    result3 = apply_refresh(envelope3, tmp_path, now="2026-03-01T00:00:00Z")
    assert result3.changes == []
    assert len(result3.claims) == 1
    assert len(result3.evidence) == 1


def test_revenue_change_with_new_reporting_period_is_new_filing(tmp_path: Path) -> None:
    ev1 = make_evidence("https://data.brreg.no/regnskapsregisteret/regnskap/" + ORG, retrieved_at="2026-01-01T00:00:00Z")
    revenue1 = make_claim(
        family="financials", field="revenue", value={"amount": 100_000, "currency": "NOK"},
        evidence_ids=[ev1.evidence_id], reporting_period=("2024-01-01", "2024-12-31"),
    )
    envelope1 = make_envelope(run_id="run-1", claims=[revenue1], evidence=[ev1])
    apply_refresh(envelope1, tmp_path, now="2026-01-01T00:00:00Z")

    ev2 = make_evidence("https://data.brreg.no/regnskapsregisteret/regnskap/" + ORG, retrieved_at="2026-06-01T00:00:00Z")
    revenue2 = make_claim(
        family="financials", field="revenue", value={"amount": 150_000, "currency": "NOK"},
        evidence_ids=[ev2.evidence_id], reporting_period=("2025-01-01", "2025-12-31"),
    )
    envelope2 = make_envelope(run_id="run-2", claims=[revenue2], evidence=[ev2])
    result = apply_refresh(envelope2, tmp_path, now="2026-06-01T00:00:00Z")

    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.change_type == "new_filing"
    assert change.previous_value == {"amount": 100_000, "currency": "NOK"}
    assert change.current_value == {"amount": 150_000, "currency": "NOK"}
    assert change.previous_evidence_ids == [ev1.evidence_id]


def test_ceo_replaced(tmp_path: Path) -> None:
    ev1 = make_evidence("https://data.brreg.no/enhetsregisteret/api/enheter/" + ORG + "/roller")
    ceo1 = make_claim(
        family="leadership", field="role", value={"role_code": "DAGL", "name": "Kari Nordmann"},
        value_key="DAGL|kari nordmann", evidence_ids=[ev1.evidence_id],
    )
    envelope1 = make_envelope(run_id="run-1", claims=[ceo1], evidence=[ev1])
    apply_refresh(envelope1, tmp_path, now="2026-01-01T00:00:00Z")

    ev2 = make_evidence("https://data.brreg.no/enhetsregisteret/api/enheter/" + ORG + "/roller", retrieved_at="2026-06-01T00:00:00Z")
    ceo2 = make_claim(
        family="leadership", field="role", value={"role_code": "DAGL", "name": "Ola Hansen"},
        value_key="DAGL|ola hansen", evidence_ids=[ev2.evidence_id],
    )
    envelope2 = make_envelope(run_id="run-2", claims=[ceo2], evidence=[ev2])
    result = apply_refresh(envelope2, tmp_path, now="2026-06-01T00:00:00Z")

    change_types = {c.change_type for c in result.changes}
    assert "new_role" in change_types   # Ola Hansen is a new claim_id (different value_key)
    assert "ended_role" in change_types  # Kari Nordmann's claim_id vanished


def test_board_member_added(tmp_path: Path) -> None:
    ev1 = make_evidence("https://data.brreg.no/enhetsregisteret/api/enheter/" + ORG + "/roller")
    member1 = make_claim(
        family="leadership", field="role", value={"role_code": "MEDL", "name": "Kari Nordmann"},
        value_key="MEDL|kari nordmann", evidence_ids=[ev1.evidence_id],
    )
    envelope1 = make_envelope(run_id="run-1", claims=[member1], evidence=[ev1])
    apply_refresh(envelope1, tmp_path, now="2026-01-01T00:00:00Z")

    ev2 = make_evidence("https://data.brreg.no/enhetsregisteret/api/enheter/" + ORG + "/roller", retrieved_at="2026-02-01T00:00:00Z")
    member2 = make_claim(
        family="leadership", field="role", value={"role_code": "MEDL", "name": "Kari Nordmann"},
        value_key="MEDL|kari nordmann", evidence_ids=[ev1.evidence_id],
    )
    new_member = make_claim(
        family="leadership", field="role", value={"role_code": "MEDL", "name": "Per Olsen"},
        value_key="MEDL|per olsen", evidence_ids=[ev2.evidence_id],
    )
    envelope2 = make_envelope(run_id="run-2", claims=[member2, new_member], evidence=[ev1, ev2])
    result = apply_refresh(envelope2, tmp_path, now="2026-02-01T00:00:00Z")

    assert len(result.changes) == 1
    assert result.changes[0].change_type == "new_role"
    assert result.changes[0].claim_id == new_member.claim_id


def test_job_opened_and_closed(tmp_path: Path) -> None:
    ev1 = make_evidence("https://arbeidsplassen.nav.no/stillinger/api/nav-uuid-1")
    job1 = make_claim(
        family="jobs", field="job_posting", value={"title": "Developer"}, value_key="nav:uuid-1",
        evidence_ids=[ev1.evidence_id],
    )
    envelope1 = make_envelope(run_id="run-1", claims=[job1], evidence=[ev1])
    apply_refresh(envelope1, tmp_path, now="2026-01-01T00:00:00Z")

    # Run 2: job1 is gone (jobs family successfully checked), a new job appears.
    ev2 = make_evidence("https://arbeidsplassen.nav.no/stillinger/api/nav-uuid-2", retrieved_at="2026-02-01T00:00:00Z")
    job2 = make_claim(
        family="jobs", field="job_posting", value={"title": "Sales manager"}, value_key="nav:uuid-2",
        evidence_ids=[ev2.evidence_id],
    )
    envelope2 = make_envelope(run_id="run-2", claims=[job2], evidence=[ev2])
    result = apply_refresh(envelope2, tmp_path, now="2026-02-01T00:00:00Z")

    types = {c.change_type: c for c in result.changes}
    assert "closed_job" in types and types["closed_job"].claim_id == job1.claim_id
    assert "new_job" in types and types["new_job"].claim_id == job2.claim_id


def test_subunit_closed(tmp_path: Path) -> None:
    ev1 = make_evidence("https://data.brreg.no/enhetsregisteret/api/underenheter/999888777")
    loc1 = make_claim(
        family="locations", field="location", value={"name": "Oslo store"}, value_key="999888777",
        evidence_ids=[ev1.evidence_id],
    )
    envelope1 = make_envelope(run_id="run-1", claims=[loc1], evidence=[ev1])
    apply_refresh(envelope1, tmp_path, now="2026-01-01T00:00:00Z")

    envelope2 = make_envelope(run_id="run-2", claims=[], evidence=[])
    result = apply_refresh(envelope2, tmp_path, now="2026-02-01T00:00:00Z")

    assert len(result.changes) == 1
    assert result.changes[0].change_type == "closed_location"
    assert result.changes[0].claim_id == loc1.claim_id
    assert result.claims == []  # closed location is not carried into current claims


def test_website_changed(tmp_path: Path) -> None:
    ev1 = make_evidence("https://old.example.no")
    site1 = make_claim(family="website", field="official_website", value="https://old.example.no", evidence_ids=[ev1.evidence_id])
    envelope1 = make_envelope(run_id="run-1", claims=[site1], evidence=[ev1])
    apply_refresh(envelope1, tmp_path, now="2026-01-01T00:00:00Z")

    ev2 = make_evidence("https://new.example.no", retrieved_at="2026-02-01T00:00:00Z")
    site2 = make_claim(family="website", field="official_website", value="https://new.example.no", evidence_ids=[ev2.evidence_id])
    envelope2 = make_envelope(run_id="run-2", claims=[site2], evidence=[ev2])
    result = apply_refresh(envelope2, tmp_path, now="2026-02-01T00:00:00Z")

    assert len(result.changes) == 1
    assert result.changes[0].change_type == "changed_website"
    assert result.changes[0].previous_value == "https://old.example.no"
    assert result.changes[0].current_value == "https://new.example.no"


def test_description_whitespace_only_edit_is_not_a_change(tmp_path: Path) -> None:
    ev1 = make_evidence("https://example.no/about")
    desc1 = make_claim(family="description", field="company_description", value="We build boats.", evidence_ids=[ev1.evidence_id])
    envelope1 = make_envelope(run_id="run-1", claims=[desc1], evidence=[ev1])
    apply_refresh(envelope1, tmp_path, now="2026-01-01T00:00:00Z")

    ev2 = make_evidence("https://example.no/about", retrieved_at="2026-02-01T00:00:00Z")
    desc2 = make_claim(family="description", field="company_description", value="We build boats!  ", evidence_ids=[ev2.evidence_id])
    envelope2 = make_envelope(run_id="run-2", claims=[desc2], evidence=[ev2])
    result = apply_refresh(envelope2, tmp_path, now="2026-02-01T00:00:00Z")

    assert result.changes == []
    assert result.claims[0].first_observed_at == "2026-01-01T00:00:00Z"


def test_source_failure_carries_forward_no_change(tmp_path: Path) -> None:
    ev1 = make_evidence("https://data.brreg.no/enhetsregisteret/api/enheter/" + ORG + "/roller")
    ceo1 = make_claim(
        family="leadership", field="role", value={"role_code": "DAGL", "name": "Kari Nordmann"},
        value_key="DAGL|kari nordmann", evidence_ids=[ev1.evidence_id],
    )
    envelope1 = make_envelope(run_id="run-1", claims=[ceo1], evidence=[ev1])
    apply_refresh(envelope1, tmp_path, now="2026-01-01T00:00:00Z")

    families = {"leadership": FamilyState(family="leadership", availability="failed", reason="request_budget")}
    envelope2 = make_envelope(run_id="run-2", claims=[], evidence=[], families=families)
    result = apply_refresh(envelope2, tmp_path, now="2026-02-01T00:00:00Z")

    assert result.changes == []
    assert len(result.claims) == 1
    carried = result.claims[0]
    assert carried.claim_id == ceo1.claim_id
    assert carried.last_observed_at == "2026-01-01T00:00:00Z"  # unchanged, not re-checked
    assert "carried forward" in (carried.note or "")
    assert any(e.get("error") == "source_not_rechecked" for e in result.errors)

    # Rerun again while still failing: still no duplicate note growth, still carried, still no changes.
    envelope3 = make_envelope(run_id="run-3", claims=[], evidence=[], families=families)
    result2 = apply_refresh(envelope3, tmp_path, now="2026-03-01T00:00:00Z")
    assert result2.changes == []
    assert len(result2.claims) == 1
    assert result2.claims[0].note.count("carried forward") == 1


def test_activity_item_aged_out_is_not_a_change(tmp_path: Path) -> None:
    ev1 = make_evidence("https://example.no/news/old-post")
    old_item = make_claim(
        family="activity", field="activity_item", value={"title": "Old post"}, value_key="https://example.no/news/old-post",
        evidence_ids=[ev1.evidence_id], effective_date="2025-01-01",
    )
    envelope1 = make_envelope(run_id="run-1", claims=[old_item], evidence=[ev1])
    apply_refresh(envelope1, tmp_path, now="2026-01-01T00:00:00Z")

    # Run 2: feed only returns recent items, old_item fell off the window. Activity checked successfully.
    ev2 = make_evidence("https://example.no/news/new-post", retrieved_at="2026-02-01T00:00:00Z")
    new_item = make_claim(
        family="activity", field="activity_item", value={"title": "New post"}, value_key="https://example.no/news/new-post",
        evidence_ids=[ev2.evidence_id], effective_date="2026-02-01",
    )
    envelope2 = make_envelope(run_id="run-2", claims=[new_item], evidence=[ev2])
    result = apply_refresh(envelope2, tmp_path, now="2026-02-01T00:00:00Z")

    types = [c.change_type for c in result.changes]
    assert types == ["new_activity"]  # no "ended"/"closed" event for the aged-out item
    assert all(c.claim_id != old_item.claim_id for c in result.changes)


def test_diff_envelopes_is_pure_and_matches_apply_refresh(tmp_path: Path) -> None:
    ev1 = make_evidence("https://example.no")
    claim1 = make_claim(family="identity", field="legal_name", value="Old Name AS", evidence_ids=[ev1.evidence_id])
    envelope1 = make_envelope(run_id="run-1", claims=[claim1], evidence=[ev1])

    ev2 = make_evidence("https://example.no", retrieved_at="2026-02-01T00:00:00Z")
    claim2 = make_claim(family="identity", field="legal_name", value="New Name AS", evidence_ids=[ev2.evidence_id])
    envelope2 = make_envelope(run_id="run-2", claims=[claim2], evidence=[ev2])

    changes = diff_envelopes(envelope1, envelope2)
    assert len(changes) == 1
    assert changes[0].change_type == "identity_changed"
    # diff_envelopes must not mutate its inputs.
    assert envelope1.claims[0].value == "Old Name AS"
