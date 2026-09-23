"""Request-budget tiering: score each company's footprint likelihood from the bulk row alone.

Tiers (BUILD_SPEC.md):
- T0 shell: no staff, holding/property/housing NACE (64.2/68.x/70.1), legal form BRL/ESEK/SAM, no
  registered site/domain. ~5 requests.
- T1 small: no staff but a registered site/domain, or 1-4 staff. ~15 requests.
- T2 staffed: 5-49 staff, or a site plus any staff. ~30 requests.
- T3 large: 50+ staff. ~45 requests.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SHELL_NACE_PREFIXES = ("64.2", "68.", "70.1")
SHELL_LEGAL_FORMS = {"BRL", "ESEK", "SAM"}

DEFAULT_ALLOWANCES = {"T0": 5, "T1": 15, "T2": 30, "T3": 45}


@dataclass
class TierResult:
    tier: str
    allowance: int
    signals: dict[str, Any]


def _bulk_int(row: dict[str, Any], key: str) -> int | None:
    raw = str(row.get(key) or "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _bulk_bool(row: dict[str, Any], key: str) -> bool:
    return str(row.get(key) or "").strip().lower() == "true"


def classify(
    bulk_row: dict[str, Any], *, caches: Any = None, allowances: dict[str, int] | None = None
) -> TierResult:
    """Classify a company into a budget tier using only its bulk registry row (+ optional caches)."""
    allowances = allowances or DEFAULT_ALLOWANCES
    employees = _bulk_int(bulk_row, "antallAnsatte")
    has_staff = bool(employees and employees > 0)
    legal_form = (bulk_row.get("organisasjonsform.kode") or "").strip().upper()
    nace = (bulk_row.get("naeringskode1.kode") or "").strip()
    is_shell_nace = any(nace.startswith(prefix) for prefix in SHELL_NACE_PREFIXES)
    website = (bulk_row.get("hjemmeside") or "").strip()
    email = (bulk_row.get("epostadresse") or "").strip()
    email_domain = email.rsplit("@", 1)[-1].lower() if "@" in email else None
    email_shared = False
    if caches is not None and email_domain is not None:
        try:
            email_shared = bool(caches.email_domains.is_shared(email_domain))
        except Exception:
            email_shared = False
    has_domain_signal = bool(website) or (bool(email_domain) and not email_shared)
    bankrupt = _bulk_bool(bulk_row, "konkurs")
    liquidating = _bulk_bool(bulk_row, "underAvvikling")

    signals = {
        "employees": employees, "legal_form": legal_form, "nace": nace, "has_website": bool(website),
        "email_domain": email_domain, "email_shared": email_shared, "bankrupt": bankrupt, "liquidating": liquidating,
    }

    if employees is not None and employees >= 50:
        tier = "T3"
    elif has_staff and employees is not None and employees >= 5:
        tier = "T2"
    elif has_staff and has_domain_signal:
        tier = "T2"
    elif has_staff:
        tier = "T1"  # 1-4 staff
    elif has_domain_signal:
        tier = "T1"  # no staff but a site/domain
    elif is_shell_nace or legal_form in SHELL_LEGAL_FORMS or (not has_staff and not has_domain_signal):
        tier = "T0"
    else:
        tier = "T0"

    if bankrupt or liquidating:
        # A defunct company rarely has live external footprint worth chasing; cap spend but still verify identity.
        tier = "T0" if tier in ("T0", "T1") else "T1"

    return TierResult(tier=tier, allowance=allowances.get(tier, DEFAULT_ALLOWANCES["T0"]), signals=signals)
