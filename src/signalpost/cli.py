"""Command-line entry points: `run`, `validate`, `prepare`."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

from . import pipeline, validate as validate_mod

DEFAULT_BULK_PATHS = ("./data/brreg-enheter.csv.gz", "./data/brreg-enheter.csv")
BULK_DOWNLOAD_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"


def _read_organisation_inputs(path: str) -> list[str]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".jsonl":
        values: list[Any] = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif p.suffix == ".json":
        body = json.loads(text)
        values = body if isinstance(body, list) else body.get("organisation_numbers", [])
    else:
        values = [line.strip() for line in text.splitlines() if line.strip()]
    orgs = []
    for v in values:
        org = v.get("organisation_number") if isinstance(v, dict) else v
        org = "".join(ch for ch in str(org or "") if ch.isdigit())
        if len(org) != 9:
            raise ValueError(f"Invalid Norwegian organisation number: {v!r}")
        orgs.append(org)
    if len(orgs) != len(set(orgs)):
        raise ValueError("Organisation-number input contains duplicates")
    return orgs


def _resolve_bulk_path(explicit: str | None) -> str:
    if explicit:
        return explicit
    for candidate in DEFAULT_BULK_PATHS:
        if Path(candidate).exists():
            return candidate
    Path("./data").mkdir(parents=True, exist_ok=True)
    dest = Path("./data/brreg-enheter.csv")
    print(f"No bulk file found; downloading {BULK_DOWNLOAD_URL} -> {dest} (counts as 1 request)", file=sys.stderr)
    request = urllib.request.Request(BULK_DOWNLOAD_URL, headers={"User-Agent": "SignalpostResearchAgent/0.1 (+contact in repo README)"})
    with urllib.request.urlopen(request, timeout=120) as resp, open(dest, "wb") as f:
        f.write(resp.read())
    return str(dest)


def cmd_run(args: argparse.Namespace) -> int:
    orgs = _read_organisation_inputs(args.organisations)
    bulk_path = _resolve_bulk_path(args.bulk)
    report = pipeline.run_batch(
        orgs, output_dir=args.output_dir, state_dir=args.state_dir, run_id=args.run_id,
        bulk_path=bulk_path, caches_dir=args.caches, max_requests=args.max_requests,
        deadline_s=args.deadline_seconds, workers=args.workers,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    result = validate_mod.validate_files(args.envelopes, args.organisations)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def cmd_prepare(args: argparse.Namespace) -> int:
    try:
        from signalpost.caches.prepare import main as prepare_main  # type: ignore
    except ImportError:
        print("signalpost.caches.prepare is not available (workstream W2 not present in this build)", file=sys.stderr)
        return 2
    return prepare_main(args.rest)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signalpost")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the research pipeline for a batch of organisations")
    p_run.add_argument("--organisations", required=True, help="JSONL / JSON list / txt of organisation numbers")
    p_run.add_argument("--output-dir", required=True)
    p_run.add_argument("--state-dir", required=True)
    p_run.add_argument("--run-id", required=True)
    p_run.add_argument("--bulk", default=None, help="Path to the (gzip) Brreg bulk enheter CSV")
    p_run.add_argument("--caches", default=None, help="Path to a prepared caches directory")
    p_run.add_argument("--max-requests", type=int, default=pipeline.DEFAULT_MAX_REQUESTS)
    p_run.add_argument("--workers", type=int, default=pipeline.DEFAULT_WORKERS)
    p_run.add_argument("--deadline-seconds", type=int, default=pipeline.DEFAULT_DEADLINE_S)
    p_run.set_defaults(func=cmd_run)

    p_validate = sub.add_parser("validate", help="Validate an envelopes.jsonl file")
    p_validate.add_argument("--envelopes", required=True)
    p_validate.add_argument("--organisations", required=False)
    p_validate.set_defaults(func=cmd_validate)

    p_prepare = sub.add_parser("prepare", help="Build the offline caches (delegates to signalpost.caches.prepare)")
    p_prepare.add_argument("rest", nargs=argparse.REMAINDER)
    p_prepare.set_defaults(func=cmd_prepare)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
