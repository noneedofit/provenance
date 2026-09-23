from __future__ import annotations

from signalpost import validate
from signalpost.models import FAMILIES, Claim, Envelope, Evidence, FamilyState, Operations, RunInfo, claim_key, evidence_id


def _base_envelope(org: str = "123456789") -> Envelope:
    families = {fam: FamilyState(family=fam, availability="not_available", reason="checked: none found") for fam in FAMILIES}
    return Envelope(
        organisation_number=org,
        run=RunInfo(run_id="r1", started_at="2026-09-23T00:00:00Z", completed_at="2026-09-23T00:00:05Z"),
        families=families,
        operations=Operations(requests=1),
    )


def test_valid_envelope_passes():
    env = _base_envelope()
    problems = validate.validate_envelope(env)
    assert problems == []


def test_missing_family_state_is_a_violation():
    env = _base_envelope()
    del env.families["jobs"]
    problems = validate.validate_envelope(env)
    assert any("jobs" in p for p in problems)


def test_available_claim_without_evidence_is_a_violation():
    env = _base_envelope()
    env.claims.append(Claim(
        claim_id=claim_key(env.organisation_number, "identity", "legal_name", None),
        organisation_number=env.organisation_number, family="identity", field="legal_name",
        value="Example AS", availability="available", evidence_ids=[],
    ))
    problems = validate.validate_envelope(env)
    assert any("no evidence" in p for p in problems)


def test_claim_referencing_missing_evidence_is_a_violation():
    env = _base_envelope()
    env.claims.append(Claim(
        claim_id=claim_key(env.organisation_number, "identity", "legal_name", None),
        organisation_number=env.organisation_number, family="identity", field="legal_name",
        value="Example AS", availability="available", evidence_ids=["e_doesnotexist"],
    ))
    problems = validate.validate_envelope(env)
    assert any("missing evidence" in p for p in problems)


def test_available_claim_with_none_value_is_a_violation():
    env = _base_envelope()
    env.claims.append(Claim(
        claim_id=claim_key(env.organisation_number, "identity", "legal_name", None),
        organisation_number=env.organisation_number, family="identity", field="legal_name",
        value=None, availability="available", evidence_ids=["e_1"],
    ))
    env.evidence.append(Evidence(
        evidence_id="e_1", source_url="https://example.test", source_class="official_registry",
        retrieved_at="2026-09-23T00:00:00Z", extraction_method="test_v1",
    ))
    problems = validate.validate_envelope(env)
    assert any("value is None" in p for p in problems)


def test_validate_envelopes_checks_uniqueness_and_order():
    env1 = _base_envelope("111111111")
    env2 = _base_envelope("222222222")
    result = validate.validate_envelopes([env1, env2], expected_organisations=["111111111", "222222222"])
    assert result["passed"] is True

    dup_result = validate.validate_envelopes([env1, env1], expected_organisations=["111111111", "111111111"])
    assert dup_result["checks"]["unique_organisation_numbers"] is False
    assert dup_result["passed"] is False
