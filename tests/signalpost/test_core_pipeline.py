from __future__ import annotations

import json
from pathlib import Path

from signalpost import pipeline
from signalpost.context import CompanyContext
from signalpost.models import FAMILIES, ConnectorResult, FamilyState, Claim, Evidence, claim_key, evidence_id


class _StubConnector:
    name = "stub"
    families = ("identity",)

    def run(self, ctx: CompanyContext) -> ConnectorResult:
        org = ctx.org
        eid = evidence_id("https://example.test/entity", "abc", "$.navn")
        claim = Claim(
            claim_id=claim_key(org, "identity", "legal_name", None), organisation_number=org, family="identity",
            field="legal_name", value=f"Company {org}", availability="available", identity_basis="registry_record",
            evidence_ids=[eid],
        )
        ev = Evidence(
            evidence_id=eid, source_url="https://example.test/entity", source_class="official_registry",
            retrieved_at=ctx.now, extraction_method="stub_v1", span="$.navn",
        )
        return ConnectorResult(
            claims=[claim], evidence=[ev],
            families={"identity": FamilyState(family="identity", availability="available", claim_count=1)},
        )


def test_run_batch_emits_exactly_one_envelope_per_input_including_crash(tmp_path: Path):
    orgs = ["111111111", "222222222", "999999999"]
    # A malformed bulk row (a string instead of a dict) blows up the planner before any connector runs,
    # exercising the outer per-company crash handler.
    bulk_rows = {"111111111": {}, "222222222": {}, "999999999": "not-a-dict"}
    report = pipeline.run_batch(
        orgs, output_dir=str(tmp_path / "out"), state_dir=str(tmp_path / "state"), run_id="test-run",
        bulk_rows=bulk_rows, connectors=[_StubConnector()], workers=2,
    )
    envelopes_path = tmp_path / "out" / "envelopes.jsonl"
    lines = [json.loads(line) for line in envelopes_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3
    assert [e["organisation_number"] for e in lines] == orgs

    ok1, ok2, crashed = lines
    assert ok1["families"]["identity"]["availability"] == "available"
    assert set(ok1["families"].keys()) == set(FAMILIES)
    assert crashed["run"]["terminal_status"] == "failed"
    assert crashed["families"]["identity"]["reason"] == "crash"
    assert report["company_count"] == 3


def test_run_batch_writes_run_report_and_requests_log(tmp_path: Path):
    orgs = ["333333333"]
    pipeline.run_batch(
        orgs, output_dir=str(tmp_path / "out"), state_dir=str(tmp_path / "state"), run_id="test-run-2",
        bulk_rows={"333333333": {}}, connectors=[_StubConnector()], workers=1,
    )
    assert (tmp_path / "out" / "run-report.json").exists()
    report = json.loads((tmp_path / "out" / "run-report.json").read_text(encoding="utf-8"))
    assert "family_state_counts" in report
    assert report["third_party_cost_usd"] == 0.0
    assert (tmp_path / "out" / "requests.jsonl").exists()


def test_run_batch_fills_missing_families_as_not_run(tmp_path: Path):
    orgs = ["444444444"]
    pipeline.run_batch(
        orgs, output_dir=str(tmp_path / "out"), state_dir=str(tmp_path / "state"), run_id="test-run-3",
        bulk_rows={"444444444": {}}, connectors=[], workers=1,
    )
    lines = [json.loads(line) for line in (tmp_path / "out" / "envelopes.jsonl").read_text(encoding="utf-8").splitlines()]
    env = lines[0]
    for fam in FAMILIES:
        assert env["families"][fam]["availability"] == "failed"
        assert env["families"][fam]["reason"] == "not_run"


class _EmployeesConnector:
    """Publishes one employee-count claim whose value the test can change between runs."""

    name = "employees"
    families = ("identity",)

    def __init__(self, count: int):
        self.count = count

    def run(self, ctx: CompanyContext) -> ConnectorResult:
        org = ctx.org
        eid = evidence_id("https://example.test/entity", f"sha{self.count}", "$.antallAnsatte")
        claim = Claim(
            claim_id=claim_key(org, "identity", "employees", None), organisation_number=org, family="identity",
            field="employees", value=self.count, availability="available", identity_basis="registry_record",
            evidence_ids=[eid],
        )
        ev = Evidence(
            evidence_id=eid, source_url="https://example.test/entity", source_class="official_registry",
            retrieved_at=ctx.now, extraction_method="stub_v1", span="$.antallAnsatte",
        )
        return ConnectorResult(
            claims=[claim], evidence=[ev],
            families={"identity": FamilyState(family="identity", availability="available", claim_count=1)},
        )


def _envelopes(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_rerun_publishes_refresh_changes_and_links_previous_run(tmp_path: Path):
    # Regression: the pipeline used to discard apply_refresh's merged envelope, so published output
    # never carried changes or first-observed dates even though the stored profile was updated.
    org = "555555555"
    common = dict(state_dir=str(tmp_path / "state"), bulk_rows={org: {}}, workers=1)
    pipeline.run_batch([org], output_dir=str(tmp_path / "r1"), run_id="r1", connectors=[_EmployeesConnector(10)], **common)
    pipeline.run_batch([org], output_dir=str(tmp_path / "r2"), run_id="r2", connectors=[_EmployeesConnector(12)], **common)
    pipeline.run_batch([org], output_dir=str(tmp_path / "r3"), run_id="r3", connectors=[_EmployeesConnector(12)], **common)

    first, second, third = (_envelopes(tmp_path / r / "envelopes.jsonl")[0] for r in ("r1", "r2", "r3"))
    assert first["changes"] == [] and first["run"]["previous_run_id"] is None
    assert second["run"]["previous_run_id"] == "r1"
    assert any(c["field"] == "employees" for c in second["changes"])
    claim = next(c for c in second["claims"] if c["field"] == "employees" and c["status"] == "current")
    assert claim["first_observed_at"] and claim["last_observed_at"]
    # Unchanged rerun: no false changes.
    assert third["changes"] == [] and third["run"]["previous_run_id"] == "r2"


class _DescriptionNotAvailableConnector:
    """Runs after a connector that published a description claim, and reports the family empty."""

    name = "late"
    families = ("description",)

    def run(self, ctx: CompanyContext) -> ConnectorResult:
        return ConnectorResult(families={"description": FamilyState(family="description", availability="not_available", reason="no verified site")})


class _DescriptionConnector:
    name = "early"
    families = ("description",)

    def run(self, ctx: CompanyContext) -> ConnectorResult:
        eid = evidence_id("https://example.test/entity", "abc", "$.aktivitet")
        claim = Claim(
            claim_id=claim_key(ctx.org, "description", "registry_activity", None), organisation_number=ctx.org,
            family="description", field="registry_activity", value="Drift av restaurant", availability="available",
            identity_basis="registry_record", evidence_ids=[eid],
        )
        ev = Evidence(evidence_id=eid, source_url="https://example.test/entity", source_class="official_registry",
                      retrieved_at=ctx.now, extraction_method="stub_v1", span="$.aktivitet")
        return ConnectorResult(claims=[claim], evidence=[ev], families={})


def test_family_with_claims_is_available_even_if_a_later_connector_found_nothing(tmp_path: Path):
    org = "666666666"
    pipeline.run_batch([org], output_dir=str(tmp_path / "o"), state_dir=str(tmp_path / "s"), run_id="r",
                       bulk_rows={org: {}}, connectors=[_DescriptionConnector(), _DescriptionNotAvailableConnector()], workers=1)
    env = _envelopes(tmp_path / "o" / "envelopes.jsonl")[0]
    assert env["families"]["description"]["availability"] == "available"
    assert env["families"]["description"]["claim_count"] == 1
    assert "https://example.test/entity" in env["families"]["description"]["sources_checked"]


class _ZeroJobsConnector:
    name = "jobs0"
    families = ("jobs",)

    def run(self, ctx: CompanyContext) -> ConnectorResult:
        eid = evidence_id("https://example.test/nav", "abc", "$")
        claim = Claim(
            claim_id=claim_key(ctx.org, "jobs", "active_postings_count", None), organisation_number=ctx.org,
            family="jobs", field="active_postings_count", value={"verified": 0}, availability="available",
            identity_basis="registry_record", evidence_ids=[eid],
        )
        ev = Evidence(evidence_id=eid, source_url="https://example.test/nav", source_class="official_registry",
                      retrieved_at=ctx.now, extraction_method="stub_v1", span="$")
        return ConnectorResult(claims=[claim], evidence=[ev], families={
            "jobs": FamilyState(family="jobs", availability="not_available", reason="checked NAV: no active postings")})


def test_jobs_with_only_a_zero_count_claim_stays_not_available(tmp_path: Path):
    org = "777777777"
    pipeline.run_batch([org], output_dir=str(tmp_path / "o"), state_dir=str(tmp_path / "s"), run_id="r",
                       bulk_rows={org: {}}, connectors=[_ZeroJobsConnector()], workers=1)
    env = _envelopes(tmp_path / "o" / "envelopes.jsonl")[0]
    assert env["families"]["jobs"]["availability"] == "not_available"


class _NamedStub(_StubConnector):
    def __init__(self, name: str, family: str):
        self.name = name
        self.families = (family,)
        self.calls = 0
        self._family = family

    def run(self, ctx: CompanyContext) -> ConnectorResult:
        self.calls += 1
        return ConnectorResult(families={self._family: FamilyState(family=self._family, availability="not_available", reason="checked")})


def test_short_time_budget_falls_back_to_registry_only_for_every_company(tmp_path: Path):
    # With too little time for full research, every company still gets the registry pass and an
    # honest "time_budget" reason for what was skipped, instead of nothing at the deadline.
    orgs = ["811111111", "822222222", "833333333"]
    registry_stub, web_stub = _NamedStub("registry", "identity"), _NamedStub("web", "website")
    pipeline.run_batch(orgs, output_dir=str(tmp_path / "o"), state_dir=str(tmp_path / "s"), run_id="r",
                       bulk_rows={o: {} for o in orgs}, connectors=[registry_stub, web_stub], workers=1,
                       deadline_s=60)
    envs = _envelopes(tmp_path / "o" / "envelopes.jsonl")
    assert len(envs) == 3
    assert registry_stub.calls == 3 and web_stub.calls == 0
    for env in envs:
        assert env["families"]["identity"]["availability"] == "not_available"
        assert env["families"]["website"]["availability"] == "failed"
        assert env["families"]["website"]["reason"].startswith("time_budget")
