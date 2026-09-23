from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from signalpost.models import (  # noqa: E402
    Claim, Envelope, Evidence, FAMILIES, FamilyState, RunInfo, Summary, SummarySentence,
)

from eval import score as score_mod  # noqa: E402
from eval.gold import GoldJobs, GoldProfile, GoldRecord, GoldWebsite, write_gold  # noqa: E402


def _evidence(eid: str, url: str = "https://example.no/kontakt") -> Evidence:
    return Evidence(
        evidence_id=eid,
        source_url=url,
        source_class="company_owned",
        retrieved_at="2026-09-23T00:00:00Z",
        content_sha256="a" * 64,
        extraction_method="regex_orgnr_v1",
        span="org number 123 456 789 found in footer",
    )


def _website_claim(org: str, url: str, *, relationship: str = "exact") -> Claim:
    return Claim(
        claim_id=f"c_website_{org}",
        organisation_number=org,
        family="website",
        field="official_website",
        value=url,
        availability="available",
        identity_basis="org_number_on_source",
        relationship=relationship,
        evidence_ids=[f"e_website_{org}"],
    )


def _profile_claim(org: str, url: str) -> Claim:
    return Claim(
        claim_id=f"c_profile_{org}_{abs(hash(url))}",
        organisation_number=org,
        family="profiles",
        field="profile",
        value=url,
        value_key=url,
        availability="available",
        evidence_ids=[f"e_website_{org}"],
    )


def _base_families(overrides: dict[str, FamilyState] | None = None) -> dict[str, FamilyState]:
    families = {fam: FamilyState(family=fam, availability="not_available", reason="checked: none found") for fam in FAMILIES}
    if overrides:
        families.update(overrides)
    return families


def make_envelope(
    org: str,
    *,
    name: str = "TEST COMPANY AS",
    website_url: str | None = None,
    website_relationship: str = "exact",
    profile_urls: list[str] | None = None,
    jobs_available: bool = False,
    duplicate_claim: bool = False,
    with_summary: bool = True,
) -> Envelope:
    claims: list[Claim] = []
    evidence: list[Evidence] = [_evidence(f"e_website_{org}")]
    family_overrides: dict[str, FamilyState] = {}

    if website_url:
        claims.append(_website_claim(org, website_url, relationship=website_relationship))
        family_overrides["website"] = FamilyState(family="website", availability="available", claim_count=1)
    if profile_urls:
        for url in profile_urls:
            claims.append(_profile_claim(org, url))
        family_overrides["profiles"] = FamilyState(family="profiles", availability="available", claim_count=len(profile_urls))
    if jobs_available:
        claims.append(Claim(
            claim_id=f"c_job_{org}",
            organisation_number=org,
            family="jobs",
            field="job_posting",
            value="Butikkmedarbeider",
            value_key=f"nav:{org}-1",
            availability="available",
            evidence_ids=[f"e_website_{org}"],
        ))
        family_overrides["jobs"] = FamilyState(family="jobs", availability="available", claim_count=1)

    if duplicate_claim and claims:
        claims.append(claims[0])

    summary = Summary(sentences=[SummarySentence(text="Test.", claim_ids=[c.claim_id for c in claims[:1]])], unknowns=["reviews: no permitted source"]) if with_summary else Summary()

    return Envelope(
        organisation_number=org,
        legal_name=name,
        run=RunInfo(run_id="r1", started_at="2026-09-23T00:00:00Z"),
        families=_base_families(family_overrides),
        claims=claims,
        evidence=evidence,
        summary=summary,
    )


def write_envelopes(envelopes: list[Envelope], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for env in envelopes:
            handle.write(json.dumps(env.model_dump(mode="json"), ensure_ascii=False) + "\n")


def _gold(org: str, *, status="exact", url="https://example.no", profiles=None, jobs_active=None) -> GoldRecord:
    return GoldRecord(
        organisation_number=org,
        name="TEST COMPANY AS",
        website=GoldWebsite(status=status, url=url, evidence_url=url, evidence_note="org number in footer"),
        profiles=[GoldProfile(**p) for p in (profiles or [])],
        jobs=GoldJobs(checked=jobs_active is not None, active=jobs_active, note="checked nav.no"),
        confidence="high",
        labelled_at="2026-09-23",
    )


# --------------------------------------------------------------------------------------

def test_contract_passes_for_well_formed_envelopes(tmp_path: Path) -> None:
    envs = [make_envelope("1"), make_envelope("2", website_url="https://example.no")]
    path = tmp_path / "envelopes.jsonl"
    write_envelopes(envs, path)

    result = score_mod.check_contract(score_mod.read_jsonl(path))
    assert result["passed"] is True
    assert result["envelopes"] == 2
    assert result["claims_missing_evidence"] == 0


def test_contract_flags_duplicate_org_and_duplicate_claims(tmp_path: Path) -> None:
    envs = [make_envelope("1", website_url="https://example.no", duplicate_claim=True), make_envelope("1")]
    path = tmp_path / "envelopes.jsonl"
    write_envelopes(envs, path)

    result = score_mod.check_contract(score_mod.read_jsonl(path))
    assert result["passed"] is False
    assert result["duplicate_organisations"] == ["1"]


def test_website_precision_recall_and_wrong_company_detection(tmp_path: Path) -> None:
    # org 1: correctly identifies its own site -> correct_exact
    # org 2: publishes org 3's site as its own exact site -> wrong company
    # org 3: gold says none_found, but we publish a site anyway -> false positive
    envs = [
        make_envelope("1", website_url="https://example.no"),
        make_envelope("2", website_url="https://another-company.no"),
        make_envelope("3", website_url="https://random-guess.no"),
    ]
    path = tmp_path / "envelopes.jsonl"
    write_envelopes(envs, path)

    gold_path = tmp_path / "gold.jsonl"
    write_gold([
        _gold("1", status="exact", url="https://example.no"),
        _gold("2", status="exact", url="https://another-company-correct.no"),
        _gold("3", status="none_found", url=None),
    ], gold_path)

    from eval.gold import load_gold
    gold = load_gold([gold_path])
    result = score_mod.score_website(score_mod.read_jsonl(path), gold)

    assert result["correct_exact"] == 1
    assert result["wrong_company_publications"] == 1
    assert "2" in result["wrong_company_organisations"]
    assert result["false_positive_publications_where_gold_says_none"] == 1
    assert result["precision"] == pytest.approx(1 / 3, rel=1e-3)
    assert result["company_recall"] == pytest.approx(1 / 2, rel=1e-3)  # 1 of 2 gold-with-site orgs recalled


def test_profiles_precision_recall(tmp_path: Path) -> None:
    envs = [
        make_envelope("1", profile_urls=["https://www.linkedin.com/company/example"]),
        make_envelope("2", profile_urls=["https://www.linkedin.com/company/wrong-one"]),
    ]
    path = tmp_path / "envelopes.jsonl"
    write_envelopes(envs, path)
    gold_path = tmp_path / "gold.jsonl"
    write_gold([
        _gold("1", profiles=[{"platform": "linkedin", "url": "https://www.linkedin.com/company/example"}]),
        _gold("2", profiles=[{"platform": "linkedin", "url": "https://www.linkedin.com/company/correct"}]),
    ], gold_path)

    from eval.gold import load_gold
    gold = load_gold([gold_path])
    result = score_mod.score_profiles(score_mod.read_jsonl(path), gold)

    assert result["correct"] == 1
    assert result["wrong"] == 1
    assert result["precision"] == pytest.approx(0.5)
    assert result["company_recall"] == pytest.approx(0.5)


def test_jobs_accuracy(tmp_path: Path) -> None:
    envs = [make_envelope("1", jobs_available=True), make_envelope("2", jobs_available=False)]
    path = tmp_path / "envelopes.jsonl"
    write_envelopes(envs, path)
    gold_path = tmp_path / "gold.jsonl"
    write_gold([_gold("1", jobs_active=True), _gold("2", jobs_active=False)], gold_path)

    from eval.gold import load_gold
    gold = load_gold([gold_path])
    result = score_mod.score_jobs(score_mod.read_jsonl(path), gold)
    assert result["accuracy"] == 1.0


def test_refresh_detects_duplicate_claims_and_false_changes(tmp_path: Path) -> None:
    prev = [make_envelope("1", website_url="https://example.no")]
    prev_path = tmp_path / "prev.jsonl"
    write_envelopes(prev, prev_path)

    cur_env = make_envelope("1", website_url="https://example.no")
    cur_env.changes.append(__import__("signalpost.models", fromlist=["Change"]).Change(
        change_id="x1", change_type="changed_website", family="website", field="official_website",
        claim_id=cur_env.claims[0].claim_id, detected_at="2026-09-23T00:00:00Z",
    ))
    cur_path = tmp_path / "cur.jsonl"
    write_envelopes([cur_env], cur_path)

    result = score_mod.score_refresh(score_mod.read_jsonl(cur_path), score_mod.read_jsonl(prev_path))
    assert result["checked"] is True
    assert result["false_changes_on_unchanged_sources"] == 1
    assert result["passed"] is False


def test_refresh_clean_when_no_previous(tmp_path: Path) -> None:
    envs = [make_envelope("1")]
    path = tmp_path / "envelopes.jsonl"
    write_envelopes(envs, path)
    result = score_mod.score_refresh(score_mod.read_jsonl(path), None)
    assert result["checked"] is False


def test_budget_checks() -> None:
    ok = score_mod.score_budget({"total_requests": 1500, "runtime_ms": 20 * 60 * 1000})
    assert ok["passed"] is True
    bad = score_mod.score_budget({"total_requests": 2500, "runtime_ms": 20 * 60 * 1000})
    assert bad["passed"] is False
    unmeasured = score_mod.score_budget(None)
    assert unmeasured["checked"] is False


def test_end_to_end_score_and_wrong_company_caps_total(tmp_path: Path) -> None:
    envs = [
        make_envelope("1", website_url="https://example.no"),
        make_envelope("2", website_url="https://wrong-site-for-2.no"),
    ]
    path = tmp_path / "envelopes.jsonl"
    write_envelopes(envs, path)
    gold_path = tmp_path / "gold.jsonl"
    write_gold([
        _gold("1", status="exact", url="https://example.no"),
        _gold("2", status="exact", url="https://correct-site-for-2.no"),
    ], gold_path)

    result = score_mod.score(path, [gold_path])
    assert result["proxy_score"]["wrong_company_total"] == 1
    assert result["proxy_score"]["qualifies"] is False
    assert result["proxy_score"]["total"] <= 40.0


def test_end_to_end_score_clean_run_qualifies(tmp_path: Path) -> None:
    envs = [make_envelope("1", website_url="https://example.no")]
    path = tmp_path / "envelopes.jsonl"
    write_envelopes(envs, path)
    gold_path = tmp_path / "gold.jsonl"
    write_gold([_gold("1", status="exact", url="https://example.no")], gold_path)

    result = score_mod.score(path, [gold_path])
    assert result["proxy_score"]["wrong_company_total"] == 0
    assert result["contract"]["passed"] is True
