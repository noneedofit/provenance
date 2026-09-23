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
