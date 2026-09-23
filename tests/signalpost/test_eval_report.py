from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eval import report as report_mod  # noqa: E402


def _report(total: float, wrong: int, precision: float | None, budget_passed: bool | None = True, qualifies: bool = True) -> dict:
    return {
        "proxy_score": {"total": total, "wrong_company_total": wrong, "qualifies": qualifies},
        "website": {"precision": precision},
        "budget": {"checked": budget_passed is not None, "passed": budget_passed},
    }


def test_promote_on_clean_gain() -> None:
    baseline = _report(50.0, 0, 0.9)
    challenger = _report(53.0, 0, 0.9)
    result = report_mod.compare(baseline, challenger)
    assert result["decision"] == "PROMOTE"


def test_reject_on_new_wrong_company() -> None:
    baseline = _report(50.0, 0, 0.9)
    challenger = _report(60.0, 1, 0.9)
    result = report_mod.compare(baseline, challenger)
    assert result["decision"] == "REJECT"
    assert result["new_wrong_company_publications"] == 1


def test_reject_on_precision_drop() -> None:
    baseline = _report(50.0, 0, 0.9)
    challenger = _report(60.0, 0, 0.8)
    result = report_mod.compare(baseline, challenger)
    assert result["decision"] == "REJECT"


def test_hold_when_gain_too_small() -> None:
    baseline = _report(50.0, 0, 0.9)
    challenger = _report(51.0, 0, 0.9)
    result = report_mod.compare(baseline, challenger)
    assert result["decision"] == "HOLD"


def test_reject_when_over_budget() -> None:
    baseline = _report(50.0, 0, 0.9)
    challenger = _report(60.0, 0, 0.9, budget_passed=False)
    result = report_mod.compare(baseline, challenger)
    assert result["decision"] == "REJECT"
