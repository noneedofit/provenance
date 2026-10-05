"""Envelope validator: enforces the invariants listed in BUILD_SPEC.md's "Envelope invariants" section."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import FAMILIES, SECTIONS, Envelope


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _read_organisation_numbers(path: str) -> list[str]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif p.suffix == ".json":
        body = json.loads(text)
        values = body if isinstance(body, list) else body.get("organisation_numbers", [])
    else:
        values = [line.strip() for line in text.splitlines() if line.strip()]
    orgs = []
    for v in values:
        org = v.get("organisation_number") if isinstance(v, dict) else v
        orgs.append(str(org))
    return orgs


def validate_envelope(env: Envelope) -> list[str]:
    """Returns a list of violation strings for one envelope (empty = valid)."""
    problems: list[str] = []
    for fam in FAMILIES:
        if fam not in env.families:
            problems.append(f"missing FamilyState for family '{fam}'")

    evidence_ids = {e.evidence_id for e in env.evidence}
    evidence_by_id = {e.evidence_id: e for e in env.evidence}
    claim_ids = {c.claim_id for c in env.claims}
    for claim in env.claims:
        if claim.availability == "available":
            if claim.value is None:
                problems.append(f"claim {claim.claim_id} is 'available' but value is None")
            if not claim.evidence_ids:
                problems.append(f"claim {claim.claim_id} is 'available' but has no evidence")
            if claim.status == "current":
                # Verifiable from the saved result alone: public source URL, retrieval time, supporting text.
                for eid in claim.evidence_ids:
                    ev = evidence_by_id.get(eid)
                    if ev is None:
                        continue
                    if not str(ev.source_url or "").startswith(("http://", "https://")):
                        problems.append(f"evidence {eid} for claim {claim.claim_id} has no public source URL")
                    if not ev.retrieved_at:
                        problems.append(f"evidence {eid} for claim {claim.claim_id} has no retrieval time")
                    if not (ev.claim_span or "").strip():
                        problems.append(f"evidence {eid} for claim {claim.claim_id} has no supporting text (claim_span)")
        for eid in claim.evidence_ids:
            if eid not in evidence_ids:
                problems.append(f"claim {claim.claim_id} references missing evidence {eid}")

    for ev in env.evidence:
        if not (ev.claim_span or "").strip():
            problems.append(f"evidence {ev.evidence_id} has no supporting text (claim_span)")

    for section, claim_id_list in env.sections.items():
        if section not in SECTIONS:
            problems.append(f"unknown section '{section}' in envelope.sections")
        for cid in claim_id_list:
            if cid not in claim_ids:
                problems.append(f"section '{section}' references missing claim {cid}")

    if env.operations is None:
        problems.append("operations missing")

    return problems


def validate_envelopes(
    envelopes: list[Envelope], expected_organisations: list[str] | None = None
) -> dict[str, Any]:
    orgs = [e.organisation_number for e in envelopes]
    checks: dict[str, Any] = {
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
    }
    if expected_organisations is not None:
        checks["exact_expected_count"] = len(envelopes) == len(expected_organisations)
        checks["same_order_as_input"] = orgs == list(expected_organisations)
        checks["zero_dropped_inputs"] = set(expected_organisations) <= set(orgs)

    per_envelope_problems: dict[str, list[str]] = {}
    for env in envelopes:
        problems = validate_envelope(env)
        if problems:
            per_envelope_problems[env.organisation_number] = problems

    checks["all_envelopes_valid"] = not per_envelope_problems
    passed = all(checks.values())
    return {"passed": passed, "checks": checks, "problems": per_envelope_problems}


def validate_files(envelopes_path: str, organisations_path: str | None = None) -> dict[str, Any]:
    raw = _read_jsonl(envelopes_path)
    envelopes = [Envelope.model_validate(item) for item in raw]
    expected = _read_organisation_numbers(organisations_path) if organisations_path else None
    return validate_envelopes(envelopes, expected)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="signalpost validate")
    parser.add_argument("--envelopes", required=True)
    parser.add_argument("--organisations", required=False)
    args = parser.parse_args(argv)
    result = validate_files(args.envelopes, args.organisations)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
