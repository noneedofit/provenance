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


DEFAULT_CACHE_DIR = "./cache"


def _ensure_caches(explicit: str | None, bulk_path: str) -> tuple[str | None, int]:
    """Return a caches directory, building the identity-critical parts when none was supplied.

    The shared-domain table (email_domains) is what stops a registry website shared by many
    organisations (a housing co-op manager, an accountant, a group site) from being published as one
    company's own site, so a run must never go without it. It is built locally from the bulk file
    (0 requests, a few seconds). Wikidata is one SPARQL query (a few pages); it is counted and reported.
    Returns (cache_dir, requests_used_building).
    """
    cache_dir = Path(explicit or DEFAULT_CACHE_DIR)
    requests_used = 0
    try:
        from signalpost.caches import email_domains as email_domains_mod, store, wikidata as wikidata_mod
    except Exception as exc:  # caches package missing in a stripped build
        print(f"caches unavailable ({exc}); running without them", file=sys.stderr)
        return explicit, 0
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not (cache_dir / "email_domains.sqlite").exists():
        print(f"building {cache_dir}/email_domains.sqlite from {bulk_path} (0 requests)", file=sys.stderr)
        info = email_domains_mod.build(bulk_path, cache_dir)
        store.update_meta_part(cache_dir, "email_domains", info)
    if not (cache_dir / "wikidata.sqlite").exists():
        print(f"building {cache_dir}/wikidata.sqlite from Wikidata SPARQL", file=sys.stderr)
        try:
            info = wikidata_mod.build(cache_dir)
            store.update_meta_part(cache_dir, "wikidata", info)
            requests_used += 1 + int(info.get("raw_binding_count") or 0) // 20_000
        except Exception as exc:
            requests_used += 1
            snapshot = Path(__file__).parent / "caches" / "snapshot" / "wikidata.sqlite"
            if snapshot.exists():
                import shutil
                shutil.copyfile(snapshot, cache_dir / "wikidata.sqlite")
                print(f"wikidata query failed ({exc}); using the bundled snapshot (CC0)", file=sys.stderr)
            else:
                print(f"wikidata cache build failed ({exc}); continuing without it", file=sys.stderr)
    return str(cache_dir), requests_used


def cmd_run(args: argparse.Namespace) -> int:
    orgs = _read_organisation_inputs(args.organisations)
    bulk_path = _resolve_bulk_path(args.bulk)
    caches_dir, setup_requests = _ensure_caches(args.caches, bulk_path)
    max_requests = args.max_requests
    if setup_requests and max_requests:
        max_requests = max(0, max_requests - setup_requests)
    report = pipeline.run_batch(
        orgs, output_dir=args.output_dir, state_dir=args.state_dir, run_id=args.run_id,
        bulk_path=bulk_path, caches_dir=caches_dir, max_requests=max_requests,
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
