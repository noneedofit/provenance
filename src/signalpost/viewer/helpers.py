"""Formatting and derived-data helpers shared by the directory and company renderers.

Pure functions only — no I/O. Operates on plain dicts (the `model_dump()` of an Envelope), not on the
pydantic classes themselves, so the viewer stays decoupled from import-time model churn.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

# Availability -> (short label shown on badges, css state class). Never render "missing" as zero.
AVAILABILITY_LABELS: dict[str, str] = {
    "available": "Available",
    "not_available": "Not available",
    "ambiguous": "Ambiguous",
    "blocked": "Blocked",
    "not_applicable": "N/A",
    "failed": "Failed",
}
AVAILABILITY_CLASS: dict[str, str] = {
    "available": "avail-available",
    "not_available": "avail-not-available",
    "ambiguous": "avail-ambiguous",
    "blocked": "avail-blocked",
    "not_applicable": "avail-na",
    "failed": "avail-failed",
}

FAMILY_LABELS: dict[str, str] = {
    "identity": "Identity",
    "financials": "Financials",
    "financial_history": "Financial history",
    "leadership": "Leadership",
    "locations": "Locations",
    "group": "Group",
    "website": "Website",
    "profiles": "Profiles",
    "description": "Description",
    "jobs": "Hiring",
    "activity": "Activity",
    "reviews": "Reviews",
}

SECTION_LABELS: dict[str, str] = {
    "legal_identity_and_brand": "Legal identity & brand",
    "annual_accounts": "Annual accounts",
    "leadership_and_workplaces": "Leadership & workplaces",
    "website_and_profiles": "Website & profiles",
    "hiring_and_activity": "Hiring & activity",
    "evidence_and_availability": "Evidence & availability",
    "refresh_and_changes": "Refresh & changes",
}

# Families counted toward "coverage" (how much of the profile is filled in).
COVERAGE_FAMILIES = (
    "identity", "financials", "financial_history", "leadership", "locations", "group",
    "website", "profiles", "description", "jobs", "activity", "reviews",
)


def esc(value: Any) -> str:
    """HTML-escape any value, coercing to str first."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def short_hash(value: str | None, n: int = 10) -> str:
    if not value:
        return ""
    return value[:n]


def fmt_date(iso: str | None) -> str:
    """Human-readable date with the ISO value kept in a title attribute for hover."""
    if not iso:
        return "<span class=\"muted\">unknown</span>"
    try:
        cleaned = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        human = dt.strftime("%-d %b %Y")
    except ValueError:
        return f"<time datetime=\"{esc(iso)}\">{esc(iso)}</time>"
    return f"<time datetime=\"{esc(iso)}\" title=\"{esc(iso)}\">{esc(human)}</time>"


def fmt_money(value: Any) -> str:
    if not isinstance(value, dict) or value.get("amount") is None:
        return esc(value)
    amount = value["amount"]
    currency = value.get("currency", "")
    try:
        amount_str = f"{int(amount):,}".replace(",", " ")
    except (TypeError, ValueError):
        amount_str = str(amount)
    return f"{esc(currency)} {amount_str}"


def fmt_value(field: str, value: Any) -> str:
    """Render a claim value generically. Special-cases money-shaped dicts; falls back to field-name label."""
    if isinstance(value, dict) and "amount" in value and "currency" in value:
        return fmt_money(value)
    if isinstance(value, (list, tuple)):
        return esc(", ".join(str(v) for v in value))
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return esc(value)


def label_from_field(field: str) -> str:
    """Generic label for a claim field we don't have a bespoke renderer for."""
    words = re.sub(r"[_\-]+", " ", field).strip()
    return words[:1].upper() + words[1:] if words else field


def org_registry_api_url(org: str) -> str:
    return f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}"


def org_registry_search_url(org: str) -> str:
    return f"https://www.brreg.no/?query={org}"


def coverage_counts(families: dict[str, dict]) -> dict[str, int]:
    """Count families by availability state, restricted to COVERAGE_FAMILIES."""
    counts = {k: 0 for k in AVAILABILITY_LABELS}
    for fam in COVERAGE_FAMILIES:
        state = families.get(fam)
        avail = state.get("availability") if state else "not_available"
        counts[avail] = counts.get(avail, 0) + 1
    return counts


def coverage_score(families: dict[str, dict]) -> int:
    """Number of families with data actually available — the "most data found" sort key."""
    return coverage_counts(families)["available"]


def employee_band(count: int | None) -> str:
    if count is None:
        return "unknown"
    if count == 0:
        return "0"
    if count <= 4:
        return "1-4"
    if count <= 19:
        return "5-19"
    if count <= 49:
        return "20-49"
    if count <= 249:
        return "50-249"
    return "250+"


def claims_by_family(claims: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in claims:
        out.setdefault(c["family"], []).append(c)
    return out


def evidence_by_id(evidence: list[dict]) -> dict[str, dict]:
    return {e["evidence_id"]: e for e in evidence}


def field_value(claims: list[dict], family: str, field: str) -> dict | None:
    """First current claim matching family+field (single-valued fields)."""
    for c in claims:
        if c["family"] == family and c["field"] == field and c.get("status", "current") == "current":
            return c
    return None


def fields_values(claims: list[dict], family: str, field: str) -> list[dict]:
    return [c for c in claims if c["family"] == family and c["field"] == field and c.get("status", "current") == "current"]
