"""Small shared helpers for the activity connector (jobs / activity / reviews).

Not part of the orchestrator contract - internal to this package.
"""
from __future__ import annotations

import re
from typing import Any

from ..models import Claim, Evidence, claim_key, evidence_id
from ..text import fold


def make_evidence(
    *,
    source_url: str,
    source_class: str,
    retrieved_at: str,
    extraction_method: str,
    final_url: str | None = None,
    redirect_chain: list[str] | None = None,
    http_status: int | None = None,
    content_sha256: str | None = None,
    snapshot_ref: str | None = None,
    span: str | None = None,
    access_policy: str | None = None,
    extractor_version: str = "1",
) -> Evidence:
    if span is not None and len(span) > 500:
        span = span[:497] + "..."
    return Evidence(
        evidence_id=evidence_id(source_url, content_sha256, span),
        source_url=source_url,
        final_url=final_url or source_url,
        redirect_chain=redirect_chain or [source_url],
        http_status=http_status,
        source_class=source_class,  # type: ignore[arg-type]
        retrieved_at=retrieved_at,
        content_sha256=content_sha256,
        snapshot_ref=snapshot_ref,
        extraction_method=extraction_method,
        extractor_version=extractor_version,
        span=span,
        access_policy=access_policy,
    )


def make_claim(
    *,
    org: str,
    family: str,
    field: str,
    value: Any,
    value_key: str | None = None,
    availability: str = "available",
    identity_basis: str | None = None,
    relationship: str | None = None,
    reporting_period: Any = None,
    effective_date: str | None = None,
    evidence_ids: list[str] | None = None,
    confidence: float = 1.0,
    note: str | None = None,
) -> Claim:
    return Claim(
        claim_id=claim_key(org, family, field, value_key),
        organisation_number=org,
        family=family,
        field=field,
        value=value if availability == "available" else None,
        value_key=value_key,
        availability=availability,  # type: ignore[arg-type]
        confidence=confidence,
        identity_basis=identity_basis,  # type: ignore[arg-type]
        relationship=relationship,  # type: ignore[arg-type]
        reporting_period=reporting_period,
        effective_date=effective_date,
        evidence_ids=evidence_ids or [],
        note=note,
    )


_LEGAL_SUFFIXES = {"as", "asa", "sa", "da", "ans", "enk", "nuf", "avd"}


def norm_title(value: Any) -> str:
    """Normalize a job title for cross-source dedupe (NAV vs ATS)."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", fold(str(value or ""))) if t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)
