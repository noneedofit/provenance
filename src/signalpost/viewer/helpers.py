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


# Known nested-dict keys from the real registry connector that read awkwardly if machine-labelled
# (mostly Norwegian Regnskapsregisteret field names bleeding through financials/audit_status etc).
FIELD_LABEL_OVERRIDES: dict[str, str] = {
    "ikkeRevidertAarsregnskap": "Unaudited accounts",
    "fravalgRevisjon": "Audit opted out",
    "small_enterprise": "Small enterprise",
    "type": "Accounting type",
    "role_code": "Role code",
    "role_label": "Role",
    "registered_at": "Registered at",
    "organisation_number": "Org. number",
}


def label_from_field(field: str) -> str:
    """Generic label for a claim field we don't have a bespoke renderer for. Handles snake_case and
    camelCase (real registry payloads mix both, e.g. "registered_employees", "ikkeRevidertAarsregnskap")."""
    if field in FIELD_LABEL_OVERRIDES:
        return FIELD_LABEL_OVERRIDES[field]
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", field)  # split camelCase
    words = re.sub(r"[_\-]+", " ", spaced)
    words = re.sub(r"\s+", " ", words).strip()
    return words[:1].upper() + words[1:] if words else field


# Address-shaped dicts we know how to format as "street, postcode city[, country]".
_ADDRESS_KEYS = {"street", "postcode", "city", "municipality", "country"}


def address_text(addr: dict) -> str:
    parts = []
    if addr.get("street"):
        parts.append(str(addr["street"]))
    city_bit = " ".join(str(x) for x in (addr.get("postcode"), addr.get("city")) if x)
    if city_bit:
        parts.append(city_bit)
    text = ", ".join(parts)
    country = addr.get("country")
    if country and str(country).lower() not in ("norge", "norway"):
        text = f"{text}, {country}" if text else str(country)
    return text or "—"


def plain_scalar(value: Any, prefer: tuple[str, ...] = ("code", "label", "name", "count", "url", "title")) -> Any:
    """Best-effort plain (non-HTML) scalar pulled out of a structured claim value, for directory rows,
    CSV columns and JS filter/sort comparisons. Never render this output directly as trusted HTML — use
    `display_value` for that. Falls back to the first simple (str/int/float/bool) value found."""
    if isinstance(value, dict):
        for key in prefer:
            v = value.get(key)
            if v not in (None, ""):
                return v
        for v in value.values():
            if isinstance(v, (str, int, float, bool)) and v not in (None, ""):
                return v
        return None
    if isinstance(value, list):
        return value[0] if value else None
    return value


def display_value(field: str, value: Any, *, _depth: int = 0) -> str:
    """Generic, robust HTML renderer for any claim value shape: str, int, float, bool, dict, list, None.

    Preference order for dicts, per field-name-agnostic heuristics: address-shaped (street/postcode/city)
    -> "street, postcode city"; money-shaped (amount/currency) -> formatted money; else label, then name,
    then count, then url/title; otherwise a readable "Key: value; Key: value" fallback so an unknown
    connector field never crashes the page. Output is already HTML-escaped/safe to embed directly.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        if isinstance(value, int):
            return f"{value:,}".replace(",", " ")
        return esc(value)
    if isinstance(value, str):
        return esc(value)
    if isinstance(value, dict):
        if "amount" in value and "currency" in value:
            return fmt_money(value)
        if _ADDRESS_KEYS & value.keys():
            addr = address_text(value)
            if value.get("name"):
                extra = []
                if value.get("organisation_number"):
                    extra.append(f"org {value['organisation_number']}")
                if value.get("employees") is not None:
                    extra.append(f"{value['employees']} employees")
                if value.get("nace"):
                    extra.append(f"NACE {value['nace']}")
                suffix = f" ({'; '.join(esc(x) for x in extra)})" if extra else ""
                return f"{esc(value['name'])} — {esc(addr)}{suffix}"
            return esc(addr)
        if value.get("label") not in (None, ""):
            code = value.get("code")
            return esc(f"{value['label']} ({code})") if code not in (None, "") else esc(str(value["label"]))
        if value.get("name") not in (None, ""):
            return esc(str(value["name"]))
        if value.get("count") is not None:
            extra = f" (as of {value['registered_at']})" if value.get("registered_at") else ""
            return esc(f"{value['count']}{extra}")
        if value.get("url"):
            title = value.get("title") or value["url"]
            return f'<a href="{esc(value["url"])}" target="_blank" rel="noopener noreferrer">{esc(title)}</a>'
        if value.get("title"):
            return esc(str(value["title"]))
        # last-resort generic fallback: never crash on an unmapped connector field shape
        bits = []
        for k, v in value.items():
            if v in (None, "", [], {}):
                continue
            bits.append(f"{esc(label_from_field(k))}: {display_value(k, v, _depth=_depth + 1)}")
        return "; ".join(bits) if bits else "—"
    if isinstance(value, (list, tuple)):
        if not value:
            return "—"
        if all(isinstance(v, str) and re.fullmatch(r"\d{4}", v) for v in value):
            years = sorted(value)
            return f"{years[0]}–{years[-1]} ({len(years)} years)" if len(years) > 1 else years[0]
        return ", ".join(display_value(field, v, _depth=_depth + 1) for v in value)
    return esc(value)


def fmt_value(field: str, value: Any) -> str:
    """Backwards-compatible alias for display_value (kept short for call sites)."""
    return display_value(field, value)


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


def first_available(claims: list[dict], family: str, *field_names: str) -> dict | None:
    """Try several candidate field names in order (different connector versions use different names for
    the same fact, e.g. "nace" vs "nace_code"/"nace_description") and return the first current claim found."""
    for field in field_names:
        c = field_value(claims, family, field)
        if c is not None:
            return c
    return None
