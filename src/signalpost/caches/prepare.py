"""Build every W2 cache into a cache directory, outside the timed daily run.

    uv run python -m signalpost.caches.prepare --cache-dir cache/ --bulk /path/to/brreg-enheter.csv

The bulk `enheter` CSV (gzip-compressed despite the `.csv` name) must already be downloaded — it's
~150MB and shared with W1's registry loader, so we don't fetch it here. The subunits bulk CSV
(underenheter, ~60MB) and the Wikidata/NAV sources ARE fetched over the network by this command.

Flags let each part be skipped (useful for CI / partial rebuilds) and let the NAV full build be capped,
since a full sequential walk of NAV's history-event feed can take hours (see caches/nav.py and
docs/caches.md for the measured rate).
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from . import aliases as aliases_mod
from . import email_domains as email_domains_mod
from . import nav as nav_mod
from . import store
from . import wikidata as wikidata_mod

UNDERENHETER_URL = "https://data.brreg.no/enhetsregisteret/api/underenheter/lastned/csv"


def _download(url: str, dest: Path, *, user_agent: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as out:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)


def build_email_domains(args: argparse.Namespace) -> None:
    print(f"[email_domains] reading {args.bulk} ...", file=sys.stderr)
    t0 = time.monotonic()
    info = email_domains_mod.build(args.bulk, args.cache_dir)
    info["build_seconds"] = round(time.monotonic() - t0, 1)
    store.update_meta_part(args.cache_dir, "email_domains", info)
    print(f"[email_domains] {info['row_count']} rows, {info['domain_count']} domains, "
          f"{info['build_seconds']}s", file=sys.stderr)


def build_aliases(args: argparse.Namespace) -> None:
    if args.underenheter:
        underenheter_path = Path(args.underenheter)
    else:
        tmp_dir = Path(args.tmp_dir) if args.tmp_dir else Path(tempfile.mkdtemp(prefix="signalpost_nav_"))
        underenheter_path = tmp_dir / "underenheter.csv.gz"
        print(f"[aliases] downloading {UNDERENHETER_URL} -> {underenheter_path} ...", file=sys.stderr)
        _download(UNDERENHETER_URL, underenheter_path,
                   user_agent="SignalpostResearchAgent/0.1 (+https://github.com/noneedofit/provenance)")

    print(f"[aliases] building from {underenheter_path} ...", file=sys.stderr)
    t0 = time.monotonic()
    info = aliases_mod.build(underenheter_path, args.cache_dir)
    info["build_seconds"] = round(time.monotonic() - t0, 1)
    info["source_url_downloaded"] = UNDERENHETER_URL if not args.underenheter else None
    store.update_meta_part(args.cache_dir, "aliases", info)
    print(f"[aliases] {info['row_count']} subunit rows, {info['build_seconds']}s", file=sys.stderr)

    if not args.underenheter and not args.keep_tmp:
        try:
            underenheter_path.unlink()
        except OSError:
            pass


def build_wikidata(args: argparse.Namespace) -> None:
    print("[wikidata] querying https://query.wikidata.org/sparql ...", file=sys.stderr)
    t0 = time.monotonic()
    info = wikidata_mod.build(args.cache_dir)
    info["build_seconds"] = round(time.monotonic() - t0, 1)
    store.update_meta_part(args.cache_dir, "wikidata", info)
    print(f"[wikidata] {info['row_count']} orgs with a Wikidata item, {info['build_seconds']}s",
          file=sys.stderr)


def build_nav(args: argparse.Namespace) -> None:
    print(f"[nav] walking feed (max_pages={args.nav_max_pages}, max_seconds={args.nav_max_seconds}) ...",
          file=sys.stderr)
    t0 = time.monotonic()
    report = nav_mod.build_full(
        args.cache_dir,
        max_pages=args.nav_max_pages,
        max_seconds=args.nav_max_seconds,
        fetch_details_for_active=not args.nav_no_details,
        resume=not args.nav_no_resume,
    )
    report["wall_seconds"] = round(time.monotonic() - t0, 1)
    rate = report["pages_fetched"] / report["elapsed_s"] if report["elapsed_s"] else 0.0
    projected_pages_hint = (
        "Full-feed page count is unknown without a complete walk (early pages are a one-time backfill "
        "burst, not representative of steady-state density); see docs/caches.md for the measured rate "
        "and methodology."
    )
    info = {
        **report,
        "measured_rate_pages_per_s": round(rate, 4),
        "note": projected_pages_hint,
        "source_urls": [nav_mod.FEED_URL, nav_mod.TOKEN_URL],
        "license": "NLOD 2.0",
        "built_at": store.utc_now(),
    }
    store.update_meta_part(args.cache_dir, "nav", info)
    print(f"[nav] pages={report['pages_fetched']} active_seen={report['active_seen']} "
          f"detail_fetches={report['detail_fetches']} requests={report['requests_used']} "
          f"elapsed={report['elapsed_s']:.1f}s reached_end={report['reached_end']}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m signalpost.caches.prepare",
                                      description=__doc__)
    parser.add_argument("--cache-dir", required=True, type=Path, help="output directory for cache files")
    parser.add_argument("--bulk", type=Path, help="path to the Brønnøysund enheter bulk CSV (gzip)")
    parser.add_argument("--underenheter", type=Path, default=None,
                         help="path to an already-downloaded underenheter bulk CSV (skips the download)")
    parser.add_argument("--tmp-dir", type=Path, default=None,
                         help="directory for the temporary underenheter download (default: mkdtemp)")
    parser.add_argument("--keep-tmp", action="store_true", help="keep the downloaded underenheter CSV")

    parser.add_argument("--skip-email-domains", action="store_true")
    parser.add_argument("--skip-aliases", action="store_true")
    parser.add_argument("--skip-wikidata", action="store_true")
    parser.add_argument("--skip-nav", action="store_true")

    parser.add_argument("--nav-max-pages", type=int, default=None,
                         help="cap on feed pages walked this invocation (resumable across invocations)")
    parser.add_argument("--nav-max-seconds", type=float, default=None,
                         help="wall-clock cap on the NAV walk this invocation")
    parser.add_argument("--nav-no-details", action="store_true",
                         help="skip feedentry detail fetches (status-only walk, much cheaper)")
    parser.add_argument("--nav-no-resume", action="store_true",
                         help="ignore any saved cursor and restart the NAV walk from page 1")

    args = parser.parse_args(argv)
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_email_domains:
        if not args.bulk:
            parser.error("--bulk is required unless --skip-email-domains is given")
        build_email_domains(args)
    if not args.skip_aliases:
        build_aliases(args)
    if not args.skip_wikidata:
        build_wikidata(args)
    if not args.skip_nav:
        build_nav(args)

    print(f"done. meta.json: {args.cache_dir / 'meta.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
