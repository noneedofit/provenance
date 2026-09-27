"""Refresh / change detection (W5).

`apply_refresh` compares a freshly-built Envelope against the previously stored profile for the same
organisation and returns an updated Envelope: claims get `first_observed_at` / `last_observed_at`, genuine
differences become `Change` events, superseded claims move into profile history (evidence retained), and
claims from families whose source could not be re-checked this run are carried forward unchanged.

State layout on disk (owned by this module, not part of the orchestrator contract):

    <state_dir>/profiles/{org}.json
    {
      "organisation_number": "...",
      "last_envelope": {...envelope dict, claims are all status="current"...},
      "history": [...claim dicts, status "superseded" (replaced/ended) or "withdrawn" (aged out quietly)...],
      "history_evidence": [...evidence dicts referenced by history claims, kept so changes stay verifiable...],
      "change_log": [...change dicts, bounded to the most recent 200...],
      "run_ids": ["run-1", "run-2", ...]   # bounded to the most recent 200
    }

Field names referenced below (family/field strings, role codes) are not yet finalised by the registry/web/
activity connectors. They are centralised in FIELD_ALIASES / ROLE_CODE_* below so the orchestrator can align
them later without touching the diff logic.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .models import Change, Claim, Envelope, Evidence, canonical, sha256_text, utc_now

# --- Assumed field/family vocabulary -----------------------------------------------------------------
# Multi-valued families: each item's claim_id is stable across runs (see models.claim_key), so missing /
# new claim_ids signal item lifecycle events rather than a changed value.
MULTI_VALUED_FAMILIES: tuple[str, ...] = ("leadership", "jobs", "locations", "profiles", "activity")

# Families whose disappearing/reappearing items age out of a feed/window rather than being "ended" facts.
NO_ENDED_EVENT_FAMILIES: tuple[str, ...] = ("activity",)

# change_type used when a multi-valued item appears for the first time, by family.
NEW_ITEM_CHANGE_TYPE: dict[str, str] = {
    "leadership": "new_role",
    "jobs": "new_job",
    "locations": "new_location",
    "profiles": "new_profile",
    "activity": "new_activity",
}

# change_type used when a previously-current multi-valued item is confirmed gone, by family.
ENDED_ITEM_CHANGE_TYPE: dict[str, str] = {
    "leadership": "ended_role",
    "jobs": "closed_job",
    "locations": "closed_location",
    "profiles": "removed_profile",
}

# change_type used when a single-valued field's value differs (or appears/disappears), by family.
CHANGED_VALUE_TYPE: dict[str, str] = {
    "website": "changed_website",
    "description": "changed_description",
    "identity": "identity_changed",
}
DEFAULT_CHANGED_VALUE_TYPE = "changed_value"
FINANCIAL_FAMILIES: tuple[str, ...] = ("financials", "financial_history")
NEW_FILING_TYPE = "new_filing"

# FamilyState.availability values that mean "this run actually checked the source" (success or a confirmed
# empty result), as opposed to a run that could not check it at all.
CHECKED_AVAILABILITY: tuple[str, ...] = ("available", "not_available", "not_applicable")
UNCHECKED_AVAILABILITY: tuple[str, ...] = ("failed", "blocked", "ambiguous")
UNCHECKED_REASONS: tuple[str, ...] = ("deadline", "request_budget")

CARRY_FORWARD_NOTE = "carried forward: source not re-checked"

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


# --- Profile persistence ------------------------------------------------------------------------------

def _profile_path(state_dir: Path, org: str) -> Path:
    return Path(state_dir) / "profiles" / f"{org}.json"


def _load_profile(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _save_profile(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


# --- Small helpers --------------------------------------------------------------------------------

def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return canonical(value)
    text = value.strip().lower()
    text = _PUNCT_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _period_key(claim: Claim | None) -> Any:
    if claim is None or claim.reporting_period is None:
        return None
    return (claim.reporting_period.start, claim.reporting_period.end)


def _period_advanced(prev: Claim | None, curr: Claim | None) -> bool:
    """True when curr's reporting period is newer than prev's (or prev had none)."""
    prev_end = prev.reporting_period.end if prev and prev.reporting_period else None
    curr_end = curr.reporting_period.end if curr and curr.reporting_period else None
    if curr_end is None:
        return False
    if prev_end is None:
        return True
    return curr_end > prev_end


def _values_equal(prev: Claim, curr: Claim) -> bool:
    return canonical(prev.value) == canonical(curr.value) and _period_key(prev) == _period_key(curr)


def _is_immaterial_text_change(prev: Claim, curr: Claim, family: str) -> bool:
    """Whitespace/case/punctuation-only edits to free text are not material changes."""
    if family != "description":
        return False
    if not isinstance(prev.value, str) or not isinstance(curr.value, str):
        return False
    return _normalize_text(prev.value) == _normalize_text(curr.value)


def _changed_type(family: str) -> str:
    if family in FINANCIAL_FAMILIES:
        return NEW_FILING_TYPE
    return CHANGED_VALUE_TYPE.get(family, DEFAULT_CHANGED_VALUE_TYPE)


def _family_checked_ok(family_state: Any) -> bool:
    if family_state is None:
        return False
    availability = getattr(family_state, "availability", None)
    reason = getattr(family_state, "reason", None) or ""
    if availability in UNCHECKED_AVAILABILITY:
        return False
    if any(marker in reason for marker in UNCHECKED_REASONS):
        return False
    return availability in CHECKED_AVAILABILITY


def _change_id(claim_id: str, change_type: str, previous_value: Any, current_value: Any) -> str:
    payload = f"{claim_id}|{change_type}|{canonical(previous_value)}|{canonical(current_value)}"
    return "x_" + sha256_text(payload)[:20]


def _make_change(
    *,
    change_type: str,
    family: str,
    field: str,
    claim_id: str,
    previous_value: Any,
    current_value: Any,
    previous_evidence_ids: list[str],
    current_evidence_ids: list[str],
    detected_at: str,
) -> Change:
    return Change(
        change_id=_change_id(claim_id, change_type, previous_value, current_value),
        change_type=change_type,
        family=family,
        field=field,
        claim_id=claim_id,
        previous_value=previous_value,
        current_value=current_value,
        previous_evidence_ids=list(previous_evidence_ids),
        current_evidence_ids=list(current_evidence_ids),
        detected_at=detected_at,
        material=True,
    )


# --- Pure diff -----------------------------------------------------------------------------------------

def diff_envelopes(prev: Envelope, curr: Envelope) -> list[Change]:
    """Compare two envelopes for the same organisation and return the Change events between them.

    Pure and side-effect free: it does not consult or write profile state. `curr` is taken as-is, so a
    caller that wants "carried forward" claims (source unavailable this run) to be excluded from the diff
    must merge them into `curr.claims` before calling this — which is what `apply_refresh` does, since a
    claim present unchanged in both sides simply produces no Change.
    """
    if prev.organisation_number != curr.organisation_number:
        raise ValueError("diff_envelopes requires the same organisation_number on both sides")

    detected_at = curr.run.completed_at or curr.run.started_at or utc_now()
    prev_current = {c.claim_id: c for c in prev.claims if c.status == "current"}
    curr_by_id = {c.claim_id: c for c in curr.claims}

    changes: list[Change] = []

    for claim_id, curr_claim in curr_by_id.items():
        prev_claim = prev_current.get(claim_id)
        family = curr_claim.family
        multi_valued = curr_claim.value_key is not None

        if prev_claim is None:
            # Brand new claim_id.
            if multi_valued:
                change_type = NEW_ITEM_CHANGE_TYPE.get(family)
                if change_type is None:
                    continue  # unknown multi-valued family; nothing sensible to name
            else:
                change_type = _changed_type(family)
            changes.append(_make_change(
                change_type=change_type,
                family=family,
                field=curr_claim.field,
                claim_id=claim_id,
                previous_value=None,
                current_value=curr_claim.value,
                previous_evidence_ids=[],
                current_evidence_ids=curr_claim.evidence_ids,
                detected_at=detected_at,
            ))
            continue

        if _values_equal(prev_claim, curr_claim):
            continue
        if _is_immaterial_text_change(prev_claim, curr_claim, family):
            continue

        if family in FINANCIAL_FAMILIES and not _period_advanced(prev_claim, curr_claim):
            change_type = DEFAULT_CHANGED_VALUE_TYPE
        else:
            change_type = _changed_type(family)
        changes.append(_make_change(
            change_type=change_type,
            family=family,
            field=curr_claim.field,
            claim_id=claim_id,
            previous_value=prev_claim.value,
            current_value=curr_claim.value,
            previous_evidence_ids=prev_claim.evidence_ids,
            current_evidence_ids=curr_claim.evidence_ids,
            detected_at=detected_at,
        ))

    for claim_id, prev_claim in prev_current.items():
        if claim_id in curr_by_id:
            continue
        family = prev_claim.family
        if family in NO_ENDED_EVENT_FAMILIES:
            continue
        multi_valued = prev_claim.value_key is not None
        if multi_valued:
            change_type = ENDED_ITEM_CHANGE_TYPE.get(family)
            if change_type is None:
                continue
        else:
            change_type = _changed_type(family)
        changes.append(_make_change(
            change_type=change_type,
            family=family,
            field=prev_claim.field,
            claim_id=claim_id,
            previous_value=prev_claim.value,
            current_value=None,
            previous_evidence_ids=prev_claim.evidence_ids,
            current_evidence_ids=[],
            detected_at=detected_at,
        ))

    return changes


# --- Refresh orchestration -------------------------------------------------------------------------

def apply_refresh(envelope: Envelope, state_dir: Path, *, now: str | None = None) -> Envelope:
    """Merge `envelope` (this run's fresh result) with the stored profile for its organisation.

    Returns a new Envelope: claims carry first/last observed timestamps, `changes` lists this run's
    Change events, and claims from unreachable-this-run families are carried forward from the previous
    profile. The on-disk profile is updated atomically as a side effect.
    """
    org = envelope.organisation_number
    working = envelope.model_copy(deep=True)
    resolved_now = now or working.run.started_at or utc_now()

    path = _profile_path(Path(state_dir), org)
    profile = _load_profile(path)

    if profile is None:
        evidence_retrieved: dict[str, str] = {ev.evidence_id: ev.retrieved_at for ev in working.evidence}
        for claim in working.claims:
            earliest = min(
                (evidence_retrieved[eid] for eid in claim.evidence_ids if eid in evidence_retrieved),
                default=None,
            )
            claim.first_observed_at = claim.first_observed_at or earliest or resolved_now
            claim.last_observed_at = resolved_now
        working.changes = []
        _save_profile(path, {
            "organisation_number": org,
            "last_envelope": working.model_dump(mode="json"),
            "history": [],
            "history_evidence": [],
            "change_log": [],
            "run_ids": [working.run.run_id],
        })
        return working

    prev = Envelope.model_validate(profile["last_envelope"])
    prev_current: dict[str, Claim] = {c.claim_id: c for c in prev.claims if c.status == "current"}
    prev_evidence_by_id: dict[str, Evidence] = {e.evidence_id: e for e in prev.evidence}
    curr_ids = {c.claim_id for c in working.claims}

    history: list[Claim] = [Claim.model_validate(c) for c in profile.get("history", [])]
    history_evidence: dict[str, Evidence] = {
        e["evidence_id"]: Evidence.model_validate(e) for e in profile.get("history_evidence", [])
    }

    working_evidence_ids = {e.evidence_id for e in working.evidence}

    def _retain_history_evidence(claim: Claim) -> None:
        for eid in claim.evidence_ids:
            ev = prev_evidence_by_id.get(eid)
            if ev is not None:
                history_evidence[eid] = ev

    # 1) Claims present this run: stamp first/last observed, supersede changed values into history.
    for claim in working.claims:
        prev_claim = prev_current.get(claim.claim_id)
        if prev_claim is None:
            claim.first_observed_at = resolved_now
            claim.last_observed_at = resolved_now
            continue
        same_value = _values_equal(prev_claim, claim)
        immaterial = (not same_value) and _is_immaterial_text_change(prev_claim, claim, claim.family)
        if same_value or immaterial:
            claim.first_observed_at = prev_claim.first_observed_at or resolved_now
            claim.last_observed_at = resolved_now
            continue
        claim.first_observed_at = resolved_now
        claim.last_observed_at = resolved_now
        history.append(prev_claim.model_copy(update={"status": "superseded"}))
        _retain_history_evidence(prev_claim)

    # 2) Claims that vanished this run: ended / carried forward / silently aged out.
    for claim_id, prev_claim in prev_current.items():
        if claim_id in curr_ids:
            continue
        family = prev_claim.family
        family_state = working.families.get(family)

        if family in NO_ENDED_EVENT_FAMILIES and _family_checked_ok(family_state):
            # Aged out of a feed/window: quietly drop from "current", keep as withdrawn history (no Change).
            history.append(prev_claim.model_copy(update={"status": "withdrawn"}))
            _retain_history_evidence(prev_claim)
            continue

        if not _family_checked_ok(family_state):
            if prev_claim.note and CARRY_FORWARD_NOTE in prev_claim.note:
                note = prev_claim.note
            elif prev_claim.note:
                note = f"{prev_claim.note}; {CARRY_FORWARD_NOTE}"
            else:
                note = CARRY_FORWARD_NOTE
            carried = prev_claim.model_copy(update={"note": note})
            working.claims.append(carried)
            curr_ids.add(claim_id)
            for eid in carried.evidence_ids:
                ev = prev_evidence_by_id.get(eid)
                if ev is not None and ev.evidence_id not in working_evidence_ids:
                    working.evidence.append(ev)
                    working_evidence_ids.add(ev.evidence_id)
            working.errors.append({
                "stage": "refresh",
                "organisation_number": org,
                "family": family,
                "claim_id": claim_id,
                "error": "source_not_rechecked",
                "detail": CARRY_FORWARD_NOTE,
            })
            continue

        # Successfully checked this run and genuinely gone: ended.
        history.append(prev_claim.model_copy(update={"status": "superseded"}))
        _retain_history_evidence(prev_claim)

    # 3) Compute this run's Change events against the (now carry-forward-merged) working envelope.
    working.changes = diff_envelopes(prev, working)
    earlier_runs = [r for r in profile.get("run_ids", []) if r != working.run.run_id]
    working.run.previous_run_id = earlier_runs[-1] if earlier_runs else prev.run.run_id

    change_log = list(profile.get("change_log", []))
    change_log.extend(change.model_dump(mode="json") for change in working.changes)
    change_log = change_log[-200:]

    run_ids = list(profile.get("run_ids", []))
    run_ids.append(working.run.run_id)
    run_ids = run_ids[-200:]

    _save_profile(path, {
        "organisation_number": org,
        "last_envelope": working.model_dump(mode="json"),
        "history": [c.model_dump(mode="json") for c in history],
        "history_evidence": [e.model_dump(mode="json") for e in history_evidence.values()],
        "change_log": change_log,
        "run_ids": run_ids,
    })
    return working
