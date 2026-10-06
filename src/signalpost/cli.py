"""Command-line entry points: `run`, `validate`, `prepare`."""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import pipeline
from . import validate as validate_mod

DEFAULT_BULK_PATHS = ("./data/brreg-enheter.csv.gz", "./data/brreg-enheter.csv")
BULK_MAX_AGE_HOURS = 20
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


BULK_DOWNLOAD_ATTEMPTS = 3
# The register serves the 155 MB bulk file without resume support, and on a slow day at a few hundred
# KB/s. All attempts together get at most this long (or a quarter of the run deadline, if smaller); past
# it the run goes on with the live registry API alone rather than spend the evaluator's time waiting.
BULK_DOWNLOAD_MAX_S = 600
BULK_READ_TIMEOUT_S = 60


def _bulk_file_ok(path: Path) -> bool:
    """True when the bulk file is usable: a plain CSV with the registry header, or a gzip that reads to
    the end (a truncated download must never be used). A verified file gets a `.ok` marker so the full
    gzip scan happens once, not on every run."""
    import gzip
    import zlib

    marker = path.with_name(path.name + ".ok")
    try:
        stat = path.stat()
        if marker.exists() and marker.read_text().strip() == str(stat.st_size):
            return True
        with open(path, "rb") as raw:
            head = raw.read(4096)
        if head[:2] != b"\x1f\x8b":
            ok = b"organisasjonsnummer" in head
        else:
            with gzip.open(path, "rb") as f:
                while f.read(1 << 22):
                    pass
            ok = True
        if ok:
            marker.write_text(str(stat.st_size))
        return ok
    except (OSError, EOFError, zlib.error):
        return False


def _resolve_bulk_path(explicit: str | None, time_budget_s: float = BULK_DOWNLOAD_MAX_S) -> tuple[str | None, int]:
    """Return (bulk file path or None, requests used). Never raises: the run can proceed without the
    bulk file (the live registry API covers identity), so a failed download must not crash the batch.
    Every download attempt together stays within `time_budget_s`."""
    if explicit:
        return explicit, 0
    stale: str | None = None
    for candidate in DEFAULT_BULK_PATHS:
        if Path(candidate).exists():
            if _bulk_file_ok(Path(candidate)):
                age_h = (time.time() - Path(candidate).stat().st_mtime) / 3600
                if age_h <= BULK_MAX_AGE_HOURS:
                    return candidate, 0
                # The register publishes the bulk file nightly: a day-old copy is refreshed so facts read
                # from it (and their retrieval time) stay current. The old copy is kept as a fallback.
                print(f"bulk file {candidate} is {age_h:.0f} h old; downloading today's copy", file=sys.stderr)
                stale = candidate
                break
            print(f"bulk file {candidate} is incomplete or corrupt; downloading a fresh copy", file=sys.stderr)
            Path(candidate).unlink(missing_ok=True)
            Path(candidate + ".ok").unlink(missing_ok=True)
    Path("./data").mkdir(parents=True, exist_ok=True)
    dest = Path("./data/brreg-enheter.csv")
    tmp = dest.with_suffix(".csv.part")
    requests_used = 0
    started = time.monotonic()
    for attempt in range(1, BULK_DOWNLOAD_ATTEMPTS + 1):
        left = time_budget_s - (time.monotonic() - started)
        if left < 10:
            print(f"bulk download time budget ({time_budget_s:.0f} s) used up", file=sys.stderr)
            break
        requests_used += 1
        print(f"downloading {BULK_DOWNLOAD_URL} -> {dest} (attempt {attempt}/{BULK_DOWNLOAD_ATTEMPTS})", file=sys.stderr)
        try:
            request = urllib.request.Request(BULK_DOWNLOAD_URL, headers={"User-Agent": "SignalpostResearchAgent/0.1 (+https://github.com/noneedofit/provenance)"})
            with urllib.request.urlopen(request, timeout=min(BULK_READ_TIMEOUT_S, left)) as resp, open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    if time.monotonic() - started > time_budget_s:
                        raise TimeoutError(f"download time budget ({time_budget_s:.0f} s) used up")
            if _bulk_file_ok(tmp):
                tmp.replace(dest)
                tmp.with_name(tmp.name + ".ok").replace(dest.with_name(dest.name + ".ok"))
                return str(dest), requests_used
            print("downloaded bulk file is incomplete", file=sys.stderr)
        except Exception as exc:  # network drop, IncompleteRead, timeout
            print(f"bulk download failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        if attempt < BULK_DOWNLOAD_ATTEMPTS and time.monotonic() - started + 5 * attempt < time_budget_s:
            time.sleep(5 * attempt)
    if stale is not None:
        print(f"bulk download failed; using the older copy {stale}", file=sys.stderr)
        return stale, requests_used
    print("continuing without the bulk file (live registry API only; bundled shared-domain table)", file=sys.stderr)
    return None, requests_used


DEFAULT_CACHE_DIR = "./cache"


def _env_int(name: str) -> int | None:
    import os

    value = os.environ.get(name, "").strip()
    return int(value) if value.isdigit() else None


def _ensure_caches(explicit: str | None, bulk_path: str | None) -> tuple[str | None, int]:
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
        from signalpost.caches import email_domains as email_domains_mod
        from signalpost.caches import store
        from signalpost.caches import wikidata as wikidata_mod
    except Exception as exc:  # caches package missing in a stripped build
        print(f"caches unavailable ({exc}); running without them", file=sys.stderr)
        return explicit, 0
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not (cache_dir / "email_domains.sqlite").exists():
        try:
            if not bulk_path:
                raise FileNotFoundError("no bulk file")
            print(f"building {cache_dir}/email_domains.sqlite from {bulk_path} (0 requests)", file=sys.stderr)
            info = email_domains_mod.build(bulk_path, cache_dir)
            store.update_meta_part(cache_dir, "email_domains", info)
        except Exception as exc:
            # The shared-domain table guards against wrong-company websites; never run without it.
            snapshot = Path(__file__).parent / "caches" / "snapshot" / "email_domains.sqlite"
            (cache_dir / "email_domains.sqlite").unlink(missing_ok=True)
            import shutil
            shutil.copyfile(snapshot, cache_dir / "email_domains.sqlite")
            print(f"email_domains build failed ({exc}); using the bundled snapshot", file=sys.stderr)
    if not (cache_dir / "wikidata.sqlite").exists():
        # Deterministic by default: the bundled CC0 snapshot, so two runs of the same input read the same
        # Wikidata facts (and spend no request). SIGNALPOST_WIKIDATA_LIVE=1 queries the live endpoint.
        import os
        import shutil

        snapshot = Path(__file__).parent / "caches" / "snapshot" / "wikidata.sqlite"
        live = os.environ.get("SIGNALPOST_WIKIDATA_LIVE", "").strip() == "1" or not snapshot.exists()
        if live:
            print(f"building {cache_dir}/wikidata.sqlite from Wikidata SPARQL", file=sys.stderr)
            try:
                info = wikidata_mod.build(cache_dir)
                store.update_meta_part(cache_dir, "wikidata", info)
                requests_used += 1 + int(info.get("raw_binding_count") or 0) // 20_000
            except Exception as exc:
                requests_used += 1
                print(f"wikidata cache build failed ({exc})", file=sys.stderr)
        if not (cache_dir / "wikidata.sqlite").exists() and snapshot.exists():
            shutil.copyfile(snapshot, cache_dir / "wikidata.sqlite")
            print("using the bundled Wikidata snapshot (CC0)", file=sys.stderr)
    try:
        from signalpost.caches import places as places_mod

        marker = cache_dir / "places.release"
        wanted = "|".join(places_mod.SNAPSHOT_META[k] for k in ("format", "overture_release", "osm_base"))
        if not (cache_dir / places_mod.DB_NAME).exists() or not marker.exists() or marker.read_text().strip() != wanted:
            print(f"indexing the bundled open places snapshots (Overture {places_mod.SNAPSHOT_META['overture_release']}, OSM) (0 requests)", file=sys.stderr)
            info = places_mod.build(cache_dir)
            store.update_meta_part(cache_dir, "places", info)
            marker.write_text(wanted)
    except Exception as exc:  # candidates only; the run is complete without them
        print(f"open places index unavailable ({exc})", file=sys.stderr)
    return str(cache_dir), requests_used


def cmd_run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    orgs = _read_organisation_inputs(args.organisations)
    bulk_path, bulk_requests = _resolve_bulk_path(args.bulk, min(BULK_DOWNLOAD_MAX_S, args.deadline_seconds / 4))
    caches_dir, setup_requests = _ensure_caches(args.caches, bulk_path)
    setup_requests += bulk_requests
    max_requests = args.max_requests or pipeline.DEFAULT_REQUESTS_PER_COMPANY * max(1, len(orgs))
    if setup_requests and max_requests:
        max_requests = max(0, max_requests - setup_requests)
    # The deadline covers the whole command, setup included, so a slow setup shortens the batch instead of
    # making the run overrun.
    setup_s = time.monotonic() - started
    report = pipeline.run_batch(
        orgs, output_dir=args.output_dir, state_dir=args.state_dir, run_id=args.run_id,
        bulk_path=bulk_path, caches_dir=caches_dir, max_requests=max_requests,
        deadline_s=max(60, int(args.deadline_seconds - setup_s)), workers=args.workers,
    )
    # Setup requests (bulk download, Wikidata query) happen before the batch budget starts; record them
    # so the report's grand total covers every outbound request this command made.
    report["setup_requests"] = setup_requests
    report["setup_s"] = round(setup_s, 1)
    report["total_requests_including_setup"] = int(report.get("total_requests", 0)) + setup_requests
    report_path = Path(args.output_dir) / "run-report.json"
    if report_path.exists():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
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
        print("signalpost.caches.prepare is not available (caches package missing from this build)", file=sys.stderr)
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
    p_run.add_argument("--max-requests", type=int, default=_env_int("SIGNALPOST_MAX_REQUESTS"),
                       help=f"Total outbound request cap for the run (default: {pipeline.DEFAULT_REQUESTS_PER_COMPANY} per input company; env SIGNALPOST_MAX_REQUESTS)")
    p_run.add_argument("--workers", type=int, default=_env_int("SIGNALPOST_WORKERS") or pipeline.DEFAULT_WORKERS)
    p_run.add_argument("--deadline-seconds", type=int, default=_env_int("SIGNALPOST_DEADLINE_SECONDS") or pipeline.DEFAULT_DEADLINE_S,
                       help="Stop starting new work this many seconds after the command starts, setup included (default 2400; env SIGNALPOST_DEADLINE_SECONDS)")
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
