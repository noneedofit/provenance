"""Compare two `eval.score` outputs (baseline vs challenger) and apply the promotion rule.

Promotion rule (from docs/PLAN.md section 6):
    zero new wrong-company publications, no precision drop, >=2 proxy points gain, still within
    budget -> PROMOTE. Otherwise HOLD (neutral / insufficient evidence) or REJECT (regression).

Usage:
    python -m eval.report --baseline eval-report-baseline.json --challenger eval-report-new.json \
        --out promotion.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

MIN_PROXY_GAIN = 2.0


def _get(report: dict[str, Any], *path: str, default: Any = None) -> Any:
    node: Any = report
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def compare(baseline: dict[str, Any], challenger: dict[str, Any]) -> dict[str, Any]:
    base_total = _get(baseline, "proxy_score", "total", default=0.0)
    chal_total = _get(challenger, "proxy_score", "total", default=0.0)
    proxy_gain = round(chal_total - base_total, 3)

    base_wrong = _get(baseline, "proxy_score", "wrong_company_total", default=0)
    chal_wrong = _get(challenger, "proxy_score", "wrong_company_total", default=0)
    new_wrong_company = max(0, chal_wrong - base_wrong)

    base_precision = _get(baseline, "website", "precision")
    chal_precision = _get(challenger, "website", "precision")
    precision_drop = (
        base_precision is not None and chal_precision is not None and chal_precision < base_precision
    )

    base_budget_ok = _get(baseline, "budget", "passed", default=True)  # unmeasured -> treat as ok
    chal_budget_ok = _get(challenger, "budget", "passed", default=True)
    within_budget = chal_budget_ok is not False

    base_qualifies = _get(baseline, "proxy_score", "qualifies", default=False)
    chal_qualifies = _get(challenger, "proxy_score", "qualifies", default=False)

    checks = {
        "zero_new_wrong_company_publications": new_wrong_company == 0,
        "no_precision_drop": not precision_drop,
        "proxy_gain_at_least_2": proxy_gain >= MIN_PROXY_GAIN,
        "within_budget": within_budget,
        "challenger_qualifies": chal_qualifies,
    }

    if chal_wrong > base_wrong or precision_drop or not within_budget or (chal_qualifies is False and base_qualifies is True):
        decision = "REJECT"
    elif all(checks.values()):
        decision = "PROMOTE"
    else:
        decision = "HOLD"

    return {
        "decision": decision,
        "checks": checks,
        "baseline_total": base_total,
        "challenger_total": chal_total,
        "proxy_gain": proxy_gain,
        "baseline_wrong_company_total": base_wrong,
        "challenger_wrong_company_total": chal_wrong,
        "new_wrong_company_publications": new_wrong_company,
        "baseline_precision": base_precision,
        "challenger_precision": chal_precision,
        "reasoning": _reasoning(decision, checks, proxy_gain, new_wrong_company, precision_drop),
    }


def _reasoning(decision: str, checks: dict[str, bool], proxy_gain: float, new_wrong: int, precision_drop: bool) -> str:
    if decision == "REJECT":
        reasons = []
        if new_wrong:
            reasons.append(f"{new_wrong} new wrong-company publication(s)")
        if precision_drop:
            reasons.append("website precision dropped")
        if not checks["within_budget"]:
            reasons.append("challenger exceeds budget")
        if not checks["challenger_qualifies"]:
            reasons.append("challenger fails qualification gates")
        return "Reject: " + "; ".join(reasons) if reasons else "Reject: regression detected"
    if decision == "PROMOTE":
        return f"Promote: +{proxy_gain} proxy points, no new wrong-company publications, no precision drop, within budget."
    failing = [k for k, v in checks.items() if not v]
    return "Hold: " + ", ".join(failing) if failing else "Hold: insufficient evidence"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Compare two eval.score reports and apply the promotion rule.")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--challenger", required=True)
    parser.add_argument("--out")
    args = parser.parse_args()

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    challenger = json.loads(Path(args.challenger).read_text(encoding="utf-8"))
    result = compare(baseline, challenger)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
