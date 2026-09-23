#!/usr/bin/env python3
"""Replay harness for signalpost.refresh, in the spirit of scripts/run_refresh_replay.py.

Takes a fixture of (prev_envelope, curr_envelope, expected_change_types) scenarios
(default: tests/fixtures/refresh/scenarios.json), and for each scenario:

  1. Seeds a fresh state dir by running `apply_refresh(prev_envelope, ...)`.
  2. Runs `apply_refresh(curr_envelope, ...)` and collects the observed change_types.
  3. Reruns `apply_refresh(curr_envelope, ...)` a second time (same inputs) and asserts zero changes
     and no growth in claim/evidence counts — the idempotency check.

Reports precision/recall of change_type detection against `expected_change_types`, a false-change flag,
and an idempotent flag, aggregated the way the starter kit's replay report does.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.models import Envelope  # noqa: E402
from signalpost.refresh import apply_refresh  # noqa: E402


def run_scenario(scenario: dict) -> dict:
    prev_envelope = Envelope.model_validate(scenario["prev_envelope"])
    curr_envelope = Envelope.model_validate(scenario["curr_envelope"])
    expected = sorted(scenario["expected_change_types"])

    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        apply_refresh(prev_envelope, state_dir, now=scenario.get("prev_now"))
        result1 = apply_refresh(curr_envelope.model_copy(deep=True), state_dir, now=scenario.get("curr_now"))
        observed = sorted(c.change_type for c in result1.changes)

        claims_after_run1 = len(result1.claims)
        evidence_after_run1 = len(result1.evidence)

        result2 = apply_refresh(curr_envelope.model_copy(deep=True), state_dir, now=scenario.get("curr_now"))
        idempotent = (
            result2.changes == []
            and len(result2.claims) == claims_after_run1
            and len(result2.evidence) == evidence_after_run1
        )

    exp_set, obs_set = set(expected), set(observed)
    true_positive = len(exp_set & obs_set)
    false_positive = sorted(obs_set - exp_set)
    false_negative = sorted(exp_set - obs_set)

    return {
        "name": scenario["name"],
        "expected_change_types": expected,
        "observed_change_types": observed,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "idempotent": idempotent,
        "passed": not false_positive and not false_negative and idempotent,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay signalpost.refresh over saved envelope scenarios")
    parser.add_argument("--fixtures", default=str(ROOT / "tests" / "fixtures" / "refresh" / "scenarios.json"))
    parser.add_argument("--output", default=str(ROOT / "tests" / "fixtures" / "refresh" / "replay-report.json"))
    args = parser.parse_args()

    manifest = json.loads(Path(args.fixtures).read_text(encoding="utf-8"))
    results = [run_scenario(s) for s in manifest["scenarios"]]

    total_tp = sum(r["true_positive"] for r in results)
    total_fp = sum(len(r["false_positive"]) for r in results)
    total_fn = sum(len(r["false_negative"]) for r in results)
    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 1.0
    recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 1.0
    false_change_scenarios = [r["name"] for r in results if r["false_positive"]]
    missed_change_scenarios = [r["name"] for r in results if r["false_negative"]]
    non_idempotent_scenarios = [r["name"] for r in results if not r["idempotent"]]

    report = {
        "corpus": manifest.get("corpus", "signalpost W5 refresh replay"),
        "scenarios": len(results),
        "precision": precision,
        "recall": recall,
        "false_change_scenarios": false_change_scenarios,
        "missed_change_scenarios": missed_change_scenarios,
        "non_idempotent_scenarios": non_idempotent_scenarios,
        "idempotent": not non_idempotent_scenarios,
        "qualification_passed": precision >= 0.95 and recall >= 0.95 and not non_idempotent_scenarios,
        "results": results,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["qualification_passed"] else 1)


if __name__ == "__main__":
    main()
