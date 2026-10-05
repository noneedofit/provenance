"""Batch orchestration: build a CompanyContext per org, run connectors, assemble one Envelope each.

Public entry point: `run_batch(...)`. Connectors run in order [registry, nav, web, activity]; nav/web/activity
are imported lazily so the pipeline works standalone before those workstreams land. `nav` (live NAV
arbeidsplassen jobs, backed by a shared feed index - see `activity/nav_feed.py`) runs before `web` so the
website module can use NAV-confirmed employer homepages as candidates; `activity` runs last and turns
`nav`'s `ctx.shared["nav_ads"]` into job claims. Any connector defining `.prepare(client, state_dir)` (nav
does) gets it called once, in a background thread, before per-company processing starts - see the
"prepare_threads" block below. Exactly one envelope is emitted per input organisation number, in input
order, even when a company crashes.
"""
from __future__ import annotations

import json
import sys
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from . import registry
from .context import CompanyContext
from .http import Budget, BudgetedHttpClient
from .models import (
    FAMILIES,
    SECTIONS,
    Envelope,
    FamilyState,
    Operations,
    RunInfo,
    Summary,
    utc_now,
)
from .planner import classify
from .snapshots import SnapshotStore

DEFAULT_MAX_REQUESTS = 1900
# Families fed by more than one connector (registry activity + website description).
MULTI_SOURCE_FAMILIES = frozenset({"description"})
DEFAULT_DEADLINE_S = 2400  # 40 minutes
# Requests allowed per input company when --max-requests is not given (100 companies -> 1,900).
DEFAULT_REQUESTS_PER_COMPANY = 19
# Registry-only fallback: when the time left is short for the companies not yet started, each of them
# gets just the official-registry pass (a few seconds) instead of nothing at the deadline.
# The margin also covers full crawls already in flight when the switch happens (up to a few minutes).
REGISTRY_ONLY_SAFETY_S = 300
REGISTRY_ONLY_S_PER_COMPANY = 4.0
DEFAULT_WORKERS = 12


def _default_connectors() -> list[Any]:
    connectors: list[Any] = [registry.RegistryConnector()]
    try:
        from signalpost.activity.nav_live import NavLiveConnector  # type: ignore

        connectors.append(NavLiveConnector())
    except ImportError:
        pass
    try:
        from signalpost.web.connector import WebConnector  # type: ignore

        connectors.append(WebConnector())
    except ImportError:
        pass
    try:
        from signalpost.activity.connector import ActivityConnector  # type: ignore

        connectors.append(ActivityConnector())
    except ImportError:
        pass
    return connectors


def _load_caches(caches_dir: str | None) -> Any:
    if not caches_dir:
        return None
    try:
        from signalpost.caches import Caches  # type: ignore

        return Caches.load(caches_dir)
    except ImportError:
        return None
    except Exception:
        return None


def _load_previous(state_dir: Path, org: str) -> dict[str, Any] | None:
    path = state_dir / "profiles" / f"{org}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


def _legal_name(claims: list) -> str | None:
    for claim in claims:
        if claim.family == "identity" and claim.field == "legal_name" and claim.availability == "available":
            return claim.value
    return None


class _CompanyOutcome:
    __slots__ = ("index", "org", "envelope", "deadline_hit", "budget_exhausted")

    def __init__(self, index: int, org: str, envelope: Envelope, deadline_hit: bool, budget_exhausted: bool):
        self.index = index
        self.org = org
        self.envelope = envelope
        self.deadline_hit = deadline_hit
        self.budget_exhausted = budget_exhausted


def run_batch(
    orgs: list[str],
    *,
    output_dir: str,
    state_dir: str,
    run_id: str,
    bulk_path: str | None = None,
    caches_dir: str | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    deadline_s: int = DEFAULT_DEADLINE_S,
    workers: int = DEFAULT_WORKERS,
    connectors: list[Any] | None = None,
    bulk_rows: dict[str, dict[str, Any]] | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Run the full pipeline for `orgs`, writing envelopes.jsonl / run-report.json / requests.jsonl.

    Returns a small summary dict (also used to build run-report.json). `bulk_rows` lets callers (mainly
    tests) inject bulk rows directly instead of a bulk CSV path.
    """
    t_start = time.monotonic()
    started_at = utc_now()

    output_path = Path(output_dir)
    state_path = Path(state_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    state_path.mkdir(parents=True, exist_ok=True)

    org_list = list(orgs)
    if bulk_rows is not None:
        bulk = {org: bulk_rows.get(org, {}) for org in org_list}
    elif bulk_path:
        try:
            bulk = registry.load_bulk(bulk_path, org_list)
        except Exception as exc:  # a corrupt bulk file must not crash the batch; live API covers identity
            print(f"bulk file unreadable ({type(exc).__name__}: {exc}); continuing without it", file=sys.stderr)
            bulk = {}
    else:
        bulk = {}

    caches = _load_caches(caches_dir)
    budget = Budget(hard_cap=max_requests)
    snapshots = SnapshotStore(state_path)
    client = BudgetedHttpClient(budget, snapshots=snapshots)
    conn_list = connectors if connectors is not None else _default_connectors()

    # Reserve every company's official registry calls up front (entity, roles, accounts; subunits and
    # filing years from T1/T2), so optional sources such as job search can never starve official data.
    # The reserve never takes more than half the run cap, so a small evaluator-supplied cap cannot leave
    # a negative budget that refuses every request.
    reserve_cap = max(0, max_requests // 2 // max(1, len(org_list)))
    for org in org_list:
        try:
            tier = classify(bulk.get(org, {}), caches=caches).tier
        except Exception:  # a malformed row is handled (and reported) per company below
            tier = "T2"
        budget.reserve(org, min({"T0": 4, "T1": 5}.get(tier, 5), reserve_cap))

    # Any connector that defines `.start_prepare(client, state_dir)` gets it called exactly once, here,
    # before per-company processing starts (e.g. NavLiveConnector's shared NAV feed index walk - a
    # one-time, run-level cost, not a per-company one). `start_prepare` claims the "prepare" slot
    # synchronously (on this thread) before starting the actual work in the background, which is what
    # guarantees no company thread can race ahead of us and build its own throwaway (state_dir=None)
    # copy - see `nav_live.NavLiveConnector.start_prepare`'s docstring. Running the work itself in the
    # background lets registry calls for the first companies overlap with it; a company whose pipeline
    # reaches a not-yet-ready connector blocks on that connector's own readiness signal, not on this
    # thread. Joined (bounded by `deadline_s`) after the batch finishes so run-report reflects the
    # connector's final state either way.
    prepare_threads: list[threading.Thread] = []
    for connector in conn_list:
        start_prepare = getattr(connector, "start_prepare", None)
        if callable(start_prepare):
            try:
                t = start_prepare(client, str(state_path), bulk)
            except TypeError:
                # A connector whose start_prepare(client, state_dir) doesn't accept bulk_rows (only
                # NavLiveConnector does today, for its employee-ranked search-fallback selection).
                t = start_prepare(client, str(state_path))
            if t is not None:
                prepare_threads.append(t)

    deadline_at = t_start + deadline_s
    results: list[_CompanyOutcome | None] = [None] * len(org_list)
    done_count = 0
    done_lock = threading.Lock()
    started = [0]
    started_lock = threading.Lock()
    worker_count = max(1, workers)

    def registry_only_now() -> bool:
        with started_lock:
            started[0] += 1
            not_started = len(org_list) - started[0]
        time_left = deadline_at - time.monotonic()
        return time_left < REGISTRY_ONLY_SAFETY_S + REGISTRY_ONLY_S_PER_COMPANY * (not_started + 1) / worker_count

    def process(index: int, org: str) -> _CompanyOutcome:
        now = utc_now()
        registry_only = registry_only_now()
        deadline_hit = time.monotonic() > deadline_at
        budget_exhausted = False
        errors: list[dict[str, Any]] = []
        claims_by_id: dict[str, Any] = {}
        evidence_by_id: dict[str, Any] = {}
        families: dict[str, FamilyState] = {}
        changes: list[Any] = []
        summary = Summary()
        try:
            row = bulk.get(org, {})
            tier_result = classify(row, caches=caches)
            budget.allocate(org, tier_result.allowance)
            ctx = CompanyContext(
                org=org, run_id=run_id, now=now, tier=tier_result.tier, bulk=row, caches=caches,
                client=client, snapshots=snapshots, shared={"registry_only": registry_only}, previous=_load_previous(state_path, org),
            )
            if not deadline_hit:
                for connector in conn_list:
                    if registry_only and getattr(connector, "name", "") != "registry":
                        continue
                    if time.monotonic() > deadline_at:
                        deadline_hit = True
                        break
                    try:
                        result = connector.run(ctx)
                    except Exception as exc:  # a connector crash never drops the company
                        errors.append({"connector": getattr(connector, "name", str(connector)), "error": f"{type(exc).__name__}: {exc}"})
                        continue
                    finally:
                        if getattr(connector, "name", "") == "registry":
                            budget.release(org)
                    for claim in result.claims:
                        claims_by_id[claim.claim_id] = claim
                    for ev in result.evidence:
                        evidence_by_id[ev.evidence_id] = ev
                    for fam_name, state in result.families.items():
                        families[fam_name] = state
                    ctx.shared.update(result.shared)
                    errors.extend(result.errors)

            budget.release(org)
            if client.remaining(org) <= 0:
                budget_exhausted = True

            # Several connectors can feed one family (description: registry activity + website text).
            # The last connector's state wins above, so a family that holds current claims but was
            # reported not_available/not_applicable by a later connector is corrected to available.
            # failed/blocked/ambiguous stay as reported so refresh still carries earlier facts forward.
            for fam_name, state in list(families.items()):
                if fam_name not in MULTI_SOURCE_FAMILIES:
                    continue  # e.g. jobs always carries a count claim, even when the count is 0
                current = [c for c in claims_by_id.values() if c.family == fam_name and c.status == "current" and c.availability == "available"]
                if current and state.availability in ("not_available", "not_applicable", "available"):
                    families[fam_name] = FamilyState(
                        family=fam_name, availability="available",
                        reason=None, claim_count=len(current),
                        sources_checked=list(dict.fromkeys(list(state.sources_checked or []) + [
                            evidence_by_id[e].source_url for c in current for e in c.evidence_ids if e in evidence_by_id
                        ])),
                    )

            for fam in FAMILIES:
                if fam not in families:
                    reason = "deadline" if deadline_hit else (
                        "request_budget" if budget_exhausted else ("time_budget: registry-only pass" if registry_only else "not_run"))
                    families[fam] = FamilyState(family=fam, availability="failed", reason=reason)

            envelope = Envelope(
                organisation_number=org,
                legal_name=_legal_name(list(claims_by_id.values())),
                run=RunInfo(
                    run_id=run_id, started_at=started_at, completed_at=None,
                    terminal_status="partial" if (deadline_hit or budget_exhausted or registry_only or errors) else "completed",
                    tier=tier_result.tier,
                ),
                families=families,
                claims=list(claims_by_id.values()),
                evidence=list(evidence_by_id.values()),
                changes=changes,
                summary=summary,
                errors=errors,
            )

            try:
                from . import refresh  # type: ignore

                # apply_refresh returns the merged envelope (changes, observed dates, carry-forward);
                # the input is left untouched, so the result must replace it.
                envelope = refresh.apply_refresh(envelope, str(state_path))
            except ImportError:
                for claim in envelope.claims:
                    claim.first_observed_at = claim.first_observed_at or now
                    claim.last_observed_at = now
            except Exception as exc:
                errors.append({"stage": "refresh", "error": f"{type(exc).__name__}: {exc}"})
                for claim in envelope.claims:
                    claim.first_observed_at = claim.first_observed_at or now
                    claim.last_observed_at = now

            # Every evidence record gets its exact supporting text inline (`claim_span`), and claims and
            # evidence are ordered deterministically, before anything is summarised or written.
            from . import evidence_text

            envelope = evidence_text.complete(envelope)

            # Summarise after the refresh merge so change sentences and carried-forward facts appear.
            try:
                from . import synthesis  # type: ignore

                envelope.summary = synthesis.build_summary(envelope)
            except ImportError:
                pass
            except Exception as exc:
                errors.append({"stage": "synthesis", "error": f"{type(exc).__name__}: {exc}"})

            envelope.errors = errors
            envelope.sections = {
                section: [c.claim_id for c in envelope.claims if c.family in fams]
                for section, fams in SECTIONS.items()
            }
            envelope.run.completed_at = utc_now()
            org_requests = budget.org_used(org)
            envelope.operations = Operations(requests=org_requests, bytes=0, runtime_ms=int((time.monotonic() - t_start) * 1000), third_party_cost_usd=0.0)
            return _CompanyOutcome(index, org, envelope, deadline_hit, budget_exhausted)
        except Exception as exc:  # last-resort: still emit exactly one envelope for this org
            fam_states = {fam: FamilyState(family=fam, availability="failed", reason="crash") for fam in FAMILIES}
            envelope = Envelope(
                organisation_number=org,
                run=RunInfo(run_id=run_id, started_at=started_at, completed_at=utc_now(), terminal_status="failed"),
                families=fam_states,
                errors=[{"stage": "pipeline", "error": f"{type(exc).__name__}: {exc}"}],
                sections={section: [] for section in SECTIONS},
            )
            return _CompanyOutcome(index, org, envelope, deadline_hit, budget_exhausted)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(process, i, org): i for i, org in enumerate(org_list)}
        for future in as_completed(futures):
            outcome = future.result()
            results[outcome.index] = outcome
            with done_lock:
                done_count += 1
                if progress_cb:
                    progress_cb(done_count, len(org_list))

    for t in prepare_threads:
        t.join(timeout=max(1.0, deadline_at - time.monotonic()))

    envelopes = [r.envelope for r in results if r is not None]
    deadline_hits = sum(1 for r in results if r and r.deadline_hit)
    budget_exhausted_count = sum(1 for r in results if r and r.budget_exhausted)

    envelopes_path = output_path / "envelopes.jsonl"
    _atomic_write(envelopes_path, "\n".join(env.model_dump_json() for env in envelopes) + ("\n" if envelopes else ""))

    requests_log_path = output_path / "requests.jsonl"
    log_lines = [
        json.dumps({
            "purpose": e.purpose, "org": e.org, "url": e.url, "status": e.status,
            "requests_used": e.requests_used, "elapsed_ms": e.elapsed_ms, "error": e.error,
        }, ensure_ascii=False)
        for e in client.request_log
    ]
    _atomic_write(requests_log_path, "\n".join(log_lines) + ("\n" if log_lines else ""))

    from . import report as report_mod
    from .validate import validate_envelopes

    validation = validate_envelopes(envelopes, list(orgs))
    completed_at = utc_now()
    run_report = report_mod.build_report(
        envelopes, client.request_log, started_at=started_at, completed_at=completed_at,
        runtime_s=time.monotonic() - t_start, deadline_hits=deadline_hits,
        budget_exhausted_count=budget_exhausted_count, validation=validation, cache_versions=getattr(caches, "meta", None) if caches else None,
    )
    _atomic_write(output_path / "run-report.json", json.dumps(run_report, ensure_ascii=False, indent=2))

    return run_report
