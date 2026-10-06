"""Shared envelope-building helpers for W5 tests (refresh + synthesis)."""
from __future__ import annotations

from typing import Any

from signalpost.models import (
    Claim,
    Envelope,
    Evidence,
    FamilyState,
    ReportingPeriod,
    RunInfo,
    claim_key,
    evidence_id,
)

ORG = "923609016"


def make_evidence(url: str, span: str = "span", retrieved_at: str = "2026-01-01T00:00:00Z") -> Evidence:
    return Evidence(
        evidence_id=evidence_id(url, span),
        source_url=url,
        source_class="official_registry",
        retrieved_at=retrieved_at,
        extraction_method="test_v1",
        span=span,
    )


def make_claim(
    *,
    org: str = ORG,
    family: str,
    field: str,
    value: Any,
    value_key: str | None = None,
    availability: str = "available",
    evidence_ids: list[str] | None = None,
    reporting_period: tuple[str | None, str | None] | None = None,
    effective_date: str | None = None,
    status: str = "current",
) -> Claim:
    cid = claim_key(org, family, field, value_key)
    return Claim(
        claim_id=cid,
        organisation_number=org,
        family=family,
        field=field,
        value=value,
        value_key=value_key,
        availability=availability,
        evidence_ids=evidence_ids or [],
        reporting_period=ReportingPeriod(start=reporting_period[0], end=reporting_period[1]) if reporting_period else None,
        effective_date=effective_date,
        status=status,
    )


def make_envelope(
    *,
    org: str = ORG,
    run_id: str,
    claims: list[Claim],
    evidence: list[Evidence],
    families: dict[str, FamilyState] | None = None,
    legal_name: str = "Example AS",
    started_at: str = "2026-01-01T00:00:00Z",
) -> Envelope:
    fam = {f: FamilyState(family=f, availability="available", claim_count=0) for f in (
        "identity", "financials", "financial_history", "leadership", "locations", "group",
        "website", "profiles", "description", "jobs", "activity", "reviews",
    )}
    if families:
        fam.update(families)
    return Envelope(
        organisation_number=org,
        legal_name=legal_name,
        run=RunInfo(run_id=run_id, started_at=started_at, completed_at=started_at),
        families=fam,
        claims=claims,
        evidence=evidence,
    )
