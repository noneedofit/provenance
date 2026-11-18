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


# External families: the ones a run has to leave the registry for (Builderr's recall pool).
EXTERNAL_FAMILIES = ("website", "profiles", "activity", "jobs")
_CONNECTION_ERRORS = ("network_error", "ssl_error", "timeout", "dns")
# A run is flagged "degraded" when the environment, not the companies, limited what it could find.
DEGRADED_IDENTITY_FAILED_SHARE = 0.02
DEGRADED_CONNECTION_FAILURE_SHARE = 0.15


def run_health(envelopes: list[Envelope], request_log: list[Any], *, deadline_hits: int, budget_exhausted_count: int) -> dict[str, Any]:
    """How healthy the run's environment was, so a crippled run (no network, failing DNS, a starved request
    budget) can never pass for "these companies have nothing online"."""
    n = len(envelopes) or 1
    external = {fam: sum(1 for env in envelopes if env.families.get(fam) is not None and env.families[fam].availability == "available")
                for fam in EXTERNAL_FAMILIES}
    identity_failed = sum(1 for env in envelopes if env.families.get("identity") is not None and env.families["identity"].availability == "failed")
    registry_only = sum(1 for env in envelopes if any("registry-only" in (state.reason or "") for state in env.families.values()))
    errors_by_kind: dict[str, int] = {}
    for entry in request_log:
        if entry.error:
            errors_by_kind[entry.error] = errors_by_kind.get(entry.error, 0) + 1
    attempts = len(request_log) or 1
    connection_failures = sum(errors_by_kind.get(kind, 0) for kind in _CONNECTION_ERRORS)
    reasons = []
    if deadline_hits:
        reasons.append(f"{deadline_hits} companies hit the run deadline")
    if budget_exhausted_count:
        reasons.append(f"{budget_exhausted_count} companies ran out of request budget")
    if registry_only:
        reasons.append(f"{registry_only} companies got only the registry pass (time budget)")
    if identity_failed / n > DEGRADED_IDENTITY_FAILED_SHARE:
        reasons.append(f"registry lookup failed for {identity_failed} of {len(envelopes)} companies")
    if connection_failures / attempts > DEGRADED_CONNECTION_FAILURE_SHARE:
        reasons.append(f"{connection_failures} of {len(request_log)} requests failed at the connection level")
    return {
        "degraded": bool(reasons),
        "reasons": reasons,
        "external_family_companies": external,
        "identity_failed_companies": identity_failed,
        "registry_only_companies": registry_only,
        "connection_failure_share": round(connection_failures / attempts, 3),
        "errors_by_kind": dict(sorted(errors_by_kind.items(), key=lambda kv: -kv[1])),
    }


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
        "third_party_cost_usd": round(sum(env.operations.third_party_cost_usd for env in envelopes), 4),
        "health": run_health(envelopes, request_log, deadline_hits=deadline_hits, budget_exhausted_count=budget_exhausted_count),
    }
