"""Shared data contract for the Signalpost agent.

Every module reads and writes these types. Change them only through the orchestrator.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "signalpost-envelope/1"

Availability = Literal["available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"]

# Field families. Official families come from Brønnøysund; external families are the coverage differentiators.
Family = Literal[
    "identity",           # legal identity, status, NACE, address, contact, public brand/aliases
    "financials",         # latest filed annual accounts
    "financial_history",  # earlier filed years
    "leadership",         # CEO, chair, board, auditor
    "locations",          # registered workplaces (subunits) and business address
    "group",              # parent / subsidiary / franchise relationships
    "website",            # verified official website
    "profiles",           # company-owned social/video profiles linked from the verified site or Wikidata
    "description",        # what the company does, from the verified site
    "jobs",               # hiring: job postings
    "activity",           # dated public activity: news, posts, videos, press releases
    "reviews",            # ratings/reviews from a permitted source
]
FAMILIES: tuple[str, ...] = (
    "identity", "financials", "financial_history", "leadership", "locations", "group",
    "website", "profiles", "description", "jobs", "activity", "reviews",
)
EXTERNAL_FAMILIES: tuple[str, ...] = ("website", "profiles", "description", "jobs", "activity", "reviews")

SourceClass = Literal[
    "official_registry",       # data.brreg.no live API
    "official_registry_bulk",  # Brønnøysund bulk download snapshot
    "official_filing",         # annual-account copy (PDF) from Regnskapsregisteret
    "public_job_feed",         # NAV arbeidsplassen feed
    "open_knowledge_base",     # Wikidata (CC0)
    "company_owned",           # the verified company website and feeds it publishes
    "company_owned_platform",  # company-owned profile on a third-party platform (URL/feeds only)
    "derived",                 # computed by the agent from other evidence (e.g. summary)
]

IdentityBasis = Literal[
    "registry_record",          # the fact comes from the official register for this org number
    "org_number_on_source",     # the source itself shows the exact organisation number
    "registry_declared",        # the register lists this website/email domain for the org number
    "job_feed_org_number",      # NAV ad carries the exact organisation number
    "wikidata_org_number",      # Wikidata item carries the exact organisation number
    "linked_from_verified_site",  # linked from a site already verified as exact
    "corroborated",             # >=2 independent corroborating signals, no conflict
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical(value: Any) -> str:
    """Deterministic JSON used for stable keys and hashes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def claim_key(org: str, family: str, field: str, value_key: str | None) -> str:
    """Stable claim identity across runs.

    Single-valued fields (value_key=None) keep the same claim_id when the value changes, so refresh can
    report `changed`. Multi-valued fields (jobs, roles, profiles, activity items) pass a stable item key
    (e.g. job uuid, role type + person name, normalized profile URL), so each item has its own lifecycle.
    """
    return "c_" + sha256_text(f"{org}|{family}|{field}|{value_key or ''}")[:20]


class ReportingPeriod(BaseModel):
    start: str | None = None  # ISO date
    end: str | None = None


class Evidence(BaseModel):
    evidence_id: str                      # "e_" + sha256(source_url|content_sha256|span)[:20]
    source_url: str
    final_url: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    http_status: int | None = None
    source_class: SourceClass
    retrieved_at: str
    content_sha256: str | None = None     # hash of the raw response body
    snapshot_ref: str | None = None       # path/key of the stored raw snapshot
    extraction_method: str                # e.g. "brreg_roles_v1", "jsonld_org_v1", "regex_orgnr_v1"
    extractor_version: str = "1"
    span: str | None = None               # exact supporting text / JSON path / selector (<= 500 chars)
    access_policy: str | None = None      # e.g. "NLOD-2.0", "CC0", "robots-allowed"
    # Output-contract fields: `id` mirrors evidence_id; `claim_span` is the exact supporting text or value
    # from the source, inline, so a claim can be verified from the saved result alone.
    id: str | None = None
    claim_span: str | None = None


def evidence_id(source_url: str, content_sha256: str | None, span: str | None) -> str:
    return "e_" + sha256_text(f"{source_url}|{content_sha256 or ''}|{span or ''}")[:20]


class Claim(BaseModel):
    claim_id: str
    organisation_number: str
    family: str
    field: str                            # e.g. "legal_name", "revenue", "role", "official_website", "job_posting"
    value: Any = None                     # None only when availability != "available"
    value_key: str | None = None
    availability: Availability
    confidence: float = 1.0
    identity_basis: IdentityBasis | None = None
    relationship: Literal["exact", "parent", "subsidiary", "franchise", "brand", "service_provider"] | None = None
    reporting_period: ReportingPeriod | None = None
    effective_date: str | None = None     # date the fact refers to (published date, role change date...)
    evidence_ids: list[str] = Field(default_factory=list)
    first_observed_at: str | None = None  # set by refresh
    last_observed_at: str | None = None   # set by refresh
    status: Literal["current", "superseded", "withdrawn"] = "current"
    note: str | None = None


class FamilyState(BaseModel):
    family: str
    availability: Availability            # overall state for the family on this run
    reason: str | None = None             # human-readable: why not_available / blocked / ambiguous / failed
    sources_checked: list[str] = Field(default_factory=list)
    claim_count: int = 0


class Change(BaseModel):
    change_id: str                        # "x_" + sha256(claim_id|type|previous|current)[:20]
    change_type: str                      # new_role, ended_role, new_filing, changed_value, new_job, closed_job,
                                          # new_location, closed_location, new_profile, changed_website,
                                          # changed_description, new_activity, identity_changed
    family: str
    field: str
    claim_id: str
    previous_value: Any = None
    current_value: Any = None
    previous_evidence_ids: list[str] = Field(default_factory=list)
    current_evidence_ids: list[str] = Field(default_factory=list)
    detected_at: str
    material: bool = True


class SummarySentence(BaseModel):
    text: str
    claim_ids: list[str] = Field(default_factory=list)


class Summary(BaseModel):
    language: str = "en"
    sentences: list[SummarySentence] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)   # families/fields that remain unknown, with reason
    method: str = "template_v1"


class RunInfo(BaseModel):
    run_id: str
    previous_run_id: str | None = None
    started_at: str
    completed_at: str | None = None
    terminal_status: Literal["completed", "partial", "failed"] = "completed"
    agent_version: str = "0.1.0"
    tier: str | None = None               # budget tier from the planner


class Operations(BaseModel):
    requests: int = 0
    bytes: int = 0
    runtime_ms: int = 0
    third_party_cost_usd: float = 0.0


class Envelope(BaseModel):
    schema_version: str = SCHEMA_VERSION
    organisation_number: str
    legal_name: str | None = None
    run: RunInfo
    sections: dict[str, list[str]] = Field(default_factory=dict)  # section name -> claim_ids (see SECTIONS)
    families: dict[str, FamilyState] = Field(default_factory=dict)
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    summary: Summary = Field(default_factory=Summary)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    operations: Operations = Field(default_factory=Operations)


# The seven sections required by the brief, mapped to families.
SECTIONS: dict[str, tuple[str, ...]] = {
    "legal_identity_and_brand": ("identity", "group"),
    "annual_accounts": ("financials", "financial_history"),
    "leadership_and_workplaces": ("leadership", "locations"),
    "website_and_profiles": ("website", "profiles", "description"),
    "hiring_and_activity": ("jobs", "activity", "reviews"),
    "evidence_and_availability": (),   # satisfied by claims/evidence/families
    "refresh_and_changes": (),         # satisfied by run/changes
}


class ConnectorResult(BaseModel):
    """What every connector returns. Connectors never write envelopes directly."""
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    families: dict[str, FamilyState] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    # Facts other connectors may use (e.g. verified site URL, discovered feed URLs, social links).
    shared: dict[str, Any] = Field(default_factory=dict)
