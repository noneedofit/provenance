from __future__ import annotations

import json
from pathlib import Path

from conftest import FIXTURES
from fake_http import FakeHttpClient

from signalpost import registry
from signalpost.context import CompanyContext
from signalpost.models import utc_now

ORG = "979543883"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{ORG}_{name}.json").read_text(encoding="utf-8"))


def _client() -> FakeHttpClient:
    client = FakeHttpClient()
    for module in ("entity", "roles", "subunits", "accounts", "years"):
        data = _load(module)
        client.add_json(data["url"], data["status"], data["body"])
    return client


def _ctx(client: FakeHttpClient, tier: str = "T3") -> CompanyContext:
    return CompanyContext(org=ORG, run_id="test-run", now=utc_now(), tier=tier, bulk={}, client=client)


def test_registry_connector_identity_from_live():
    client = _client()
    ctx = _ctx(client)
    result = registry.RegistryConnector().run(ctx)

    assert result.families["identity"].availability == "available"
    name_claims = [c for c in result.claims if c.family == "identity" and c.field == "legal_name"]
    assert name_claims and name_claims[0].value == "DIPS AS"
    assert name_claims[0].evidence_ids
    # every available claim must reference real evidence
    evidence_ids = {e.evidence_id for e in result.evidence}
    for claim in result.claims:
        if claim.availability == "available":
            assert claim.value is not None
            for eid in claim.evidence_ids:
                assert eid in evidence_ids


def test_registry_connector_leadership_marks_resigned_and_current():
    client = _client()
    ctx = _ctx(client)
    result = registry.RegistryConnector().run(ctx)

    roles = [c for c in result.claims if c.family == "leadership" and c.field == "role"]
    assert roles, "expected role claims"
    daglig_leder = [c for c in roles if c.value["role_code"] == "DAGL"]
    assert daglig_leder and daglig_leder[0].value["name"] == "Thomas Smedsrud"
    assert all(c.status == "current" for c in roles)  # fixture has no avregistrert=true roles
    last_changed = [c for c in result.claims if c.field == "roles_last_changed"]
    assert last_changed


def test_registry_connector_financials():
    client = _client()
    ctx = _ctx(client)
    result = registry.RegistryConnector().run(ctx)

    revenue = [c for c in result.claims if c.family == "financials" and c.field == "revenue"]
    assert revenue
    assert revenue[0].value["amount"] == 790400865.0
    assert revenue[0].value["currency"] == "NOK"
    assert revenue[0].reporting_period.end == "2025-12-31"

    history = [c for c in result.claims if c.family == "financial_history" and c.field == "filed_years"]
    assert history and "2025" in history[0].value


def test_registry_connector_locations_subunits():
    client = _client()
    ctx = _ctx(client)
    result = registry.RegistryConnector().run(ctx)

    workplaces = [c for c in result.claims if c.family == "locations" and c.field == "workplace"]
    assert len(workplaces) == 6
    bergen = [c for c in workplaces if c.value["city"] == "BERGEN"]
    assert bergen and bergen[0].value["organisation_number"] == "916610181"


def test_registry_connector_t0_skips_subunits():
    client = _client()
    ctx = _ctx(client, tier="T0")
    result = registry.RegistryConnector().run(ctx)

    workplaces = [c for c in result.claims if c.family == "locations" and c.field == "workplace"]
    assert workplaces == []
    business_address = [c for c in result.claims if c.family == "locations" and c.field == "business_address"]
    assert business_address
    assert "underenheter" not in " ".join(client.calls)


def test_registry_connector_publishes_shared_registry_facts():
    client = _client()
    ctx = _ctx(client)
    result = registry.RegistryConnector().run(ctx)

    facts = result.shared["registry_facts"]
    assert facts["name"] == "DIPS AS"
    assert facts["website"] == "https://www.dips.com"
    assert "Thomas Smedsrud" in facts["role_holders"]
    assert facts["email_domain"] == "dips.no"


def test_registry_connector_budget_exhausted_marks_failed():
    client = FakeHttpClient(budget=0)
    ctx = _ctx(client)
    result = registry.RegistryConnector().run(ctx)
    assert result.families["identity"].availability == "failed"
    assert result.families["identity"].reason == "request_budget"


def test_load_bulk_gzip_and_plain(tmp_path: Path):
    import csv
    import gzip

    csv_path = tmp_path / "bulk.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["organisasjonsnummer", "navn"])
        writer.writerow(["111111111", "Alpha AS"])
        writer.writerow(["222222222", "Beta AS"])
    rows = registry.load_bulk(str(csv_path), ["111111111"])
    assert rows["111111111"]["navn"] == "Alpha AS"
    assert "222222222" not in rows

    gz_path = tmp_path / "bulk.csv.gz"
    with open(csv_path, "rb") as src, gzip.open(gz_path, "wb") as dst:
        dst.write(src.read())
    rows_gz = registry.load_bulk(str(gz_path), ["222222222"])
    assert rows_gz["222222222"]["navn"] == "Beta AS"
