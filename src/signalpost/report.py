"""Builds run-report.json: counts by family state, requests by purpose, runtime percentiles, budget use."""
from __future__ import annotations

from typing import Any

from .models import FAMILIES, Envelope


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(pct * (len(ordered) - 1)))))
    return ordered[idx]


def build_report(
    envelopes: list[Envelope],
    request_log: list[Any],
    *,
    started_at: str,
    completed_at: str,
    runtime_s: float,
    deadline_hits: int,
    budget_exhausted_count: int,
    cache_versions: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    family_state_counts: dict[str, dict[str, int]] = {fam: {} for fam in FAMILIES}
    for env in envelopes:
        for fam, state in env.families.items():
            bucket = family_state_counts.setdefault(fam, {})
            bucket[state.availability] = bucket.get(state.availability, 0) + 1

    requests_by_purpose: dict[str, int] = {}
    total_requests = 0
    for entry in request_log:
        requests_by_purpose[entry.purpose] = requests_by_purpose.get(entry.purpose, 0) + entry.requests_used
        total_requests += entry.requests_used

    runtimes_ms = [env.operations.runtime_ms for env in envelopes]
    terminal_counts: dict[str, int] = {}
    for env in envelopes:
        terminal_counts[env.run.terminal_status] = terminal_counts.get(env.run.terminal_status, 0) + 1

    return {
        "started_at": started_at,
        "completed_at": completed_at,
        "runtime_s": round(runtime_s, 2),
        "company_count": len(envelopes),
        "terminal_status_counts": terminal_counts,
        "family_state_counts": family_state_counts,
        "requests_by_purpose": requests_by_purpose,
        "total_requests": total_requests,
        "runtime_ms_p50": _percentile(runtimes_ms, 0.5),
        "runtime_ms_p95": _percentile(runtimes_ms, 0.95),
        "deadline_hits": deadline_hits,
        "budget_exhausted_count": budget_exhausted_count,
        "cache_versions": cache_versions,
        "validation": validation,
        "third_party_cost_usd": 0.0,
    }
