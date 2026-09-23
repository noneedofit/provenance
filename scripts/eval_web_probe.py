#!/usr/bin/env python3
"""Live measurement of web/candidates+crawl+verify against tests/fixtures/website-probe-150.json.

Uses a minimal local HttpClient (not signalpost.http.BudgetedHttpClient, which W1 owns and may not exist
yet in this worktree) implementing the same context.HttpClient protocol, so results transfer directly once
the real client lands. Makes REAL network requests — run manually, not part of `pytest`.

Usage: uv run python scripts/eval_web_probe.py [--limit N] [--stratum small|large|none]
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.context import CompanyContext, Response  # noqa: E402
from signalpost.models import sha256_text, utc_now  # noqa: E402
from signalpost.web.candidates import generate_candidates  # noqa: E402
from signalpost.web.connector import WebConnector  # noqa: E402

USER_AGENT = "builderr-signalpost-eval/0.1 (+https://builderr.ai)"


def _is_public(host: str, timeout: float = 3.0) -> bool:
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    finally:
        socket.setdefaulttimeout(previous)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


class LiveHttpClient:
    """Minimal real HttpClient for this eval script only. Not used by unit tests or production code."""

    def __init__(self, per_company_budget: int = 15, global_budget: int = 1800, timeout: float = 8.0):
        self.per_company_budget = per_company_budget
        self.global_budget = global_budget
        self.used_total = 0
        self.used_by_org: dict[str, int] = defaultdict(int)
        self.timeout = timeout
        self._dns_cache: dict[str, bool] = {}
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}

    def remaining(self, org: str | None = None) -> int:
        org_used = self.used_by_org.get(org or "", 0)
        return max(0, min(self.per_company_budget - org_used, self.global_budget - self.used_total))

    def dns_resolves(self, hostname: str) -> bool:
        if hostname not in self._dns_cache:
            self._dns_cache[hostname] = _is_public(hostname)
        return self._dns_cache[hostname]

    def _robots_allowed(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
        parser = self._robots_cache.get(robots_url)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            try:
                req = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    parser.parse(resp.read().decode("utf-8", "replace").splitlines())
            except Exception:
                parser.parse([])  # empty robots.txt => allow
            self._robots_cache[robots_url] = parser
        return parser.can_fetch(USER_AGENT, url)

    def get(self, url, *, org=None, purpose="", accept="*/*", max_bytes=2_000_000, timeout=10.0,
            respect_robots=True, max_redirects=4, snapshot=True) -> Response:
        self.used_total += 1
        if org:
            self.used_by_org[org] += 1
        started = time.monotonic()
        host = urllib.parse.urlparse(url).hostname or ""
        if not self.dns_resolves(host):
            return Response(url=url, final_url=url, redirect_chain=[url], status=0, headers={}, body=b"",
                             retrieved_at=utc_now(), content_sha256="", elapsed_ms=0, requests_used=1, error="dns")
        if respect_robots and not self._robots_allowed(url):
            return Response(url=url, final_url=url, redirect_chain=[url], status=0, headers={}, body=b"",
                             retrieved_at=utc_now(), content_sha256="", elapsed_ms=0, requests_used=1, error="robots_disallowed")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read(max_bytes + 1)
                final_url = resp.geturl()
                status = resp.status
                headers = dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            return Response(url=url, final_url=exc.geturl() or url, redirect_chain=[url], status=exc.code,
                             headers={}, body=b"", retrieved_at=utc_now(), content_sha256="", elapsed_ms=elapsed,
                             requests_used=1, error=f"http_{exc.code}")
        except Exception as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            return Response(url=url, final_url=url, redirect_chain=[url], status=0, headers={}, body=b"",
                             retrieved_at=utc_now(), content_sha256="", elapsed_ms=elapsed, requests_used=1,
                             error=f"{type(exc).__name__}"[:60])
        elapsed = int((time.monotonic() - started) * 1000)
        return Response(url=url, final_url=final_url, redirect_chain=[url, final_url] if final_url != url else [url],
                         status=status, headers=headers, body=body[:max_bytes], retrieved_at=utc_now(),
                         content_sha256=sha256_text(body[:max_bytes]), elapsed_ms=elapsed, requests_used=1, error=None)

    def post_json(self, url, payload, *, org=None, purpose="", timeout=20.0):
        raise NotImplementedError


TIER_MAP = {"none": "T1", "small": "T2", "large": "T3"}  # probe fixture tiers -> planner-ish tiers


def run(entries: list[dict], *, per_company_budget: int, save_json: str | None = None) -> dict:
    stats = {
        "by_stratum": defaultdict(lambda: Counter()),
        "requests_per_company": [],
        "manual_review": [],
        "per_company": [],
    }
    for entry in entries:
        org = entry["org"]
        name = entry["name"]
        stratum = entry.get("tier", "unknown")
        tier = TIER_MAP.get(stratum, "T2")
        registry_facts = {
            "name": name, "aliases": [], "street": "", "postcode": "", "city": "",
            "phones": [], "email": None, "email_domain": None,
            "website": entry.get("reg_site") or None, "role_holders": [], "subunits": [],
        }
        client = LiveHttpClient(per_company_budget=per_company_budget)
        ctx = CompanyContext(org=org, run_id="eval", now=utc_now(), tier=tier, bulk={}, registry={},
                              caches=None, client=client, snapshots=None, shared={"registry_facts": registry_facts}, previous=None)
        try:
            cands = generate_candidates(ctx)
        except Exception as exc:
            cands = []
            print(f"  ! candidate generation failed for {org} {name}: {exc}", file=sys.stderr)
        try:
            result = WebConnector().run(ctx)
        except Exception as exc:
            print(f"  ! connector failed for {org} {name}: {exc}", file=sys.stderr)
            continue

        stats["requests_per_company"].append(client.used_total)
        bucket = stats["by_stratum"][stratum]
        bucket["companies"] += 1
        bucket["candidates_tried"] += len(cands)
        status = result.families.get("website")
        avail = status.availability if status else "missing"
        bucket[f"website_{avail}"] += 1
        website_url = None
        website_basis = None
        website_relationship = None
        for claim in result.claims:
            if claim.family == "website" and claim.field == "official_website":
                basis = claim.identity_basis or "none"
                website_url = claim.value.get("url") if isinstance(claim.value, dict) else None
                website_basis = basis
                website_relationship = claim.relationship
                if avail == "available" and basis not in {"org_number_on_source", "wikidata_org_number", "job_feed_org_number"}:
                    stats["manual_review"].append({
                        "org": org, "name": name, "status": "exact", "basis": basis, "url": website_url, "note": claim.note,
                    })
                if avail == "ambiguous":
                    stats["manual_review"].append({
                        "org": org, "name": name, "status": "related", "relationship": claim.relationship, "url": website_url, "note": claim.note,
                    })
        stats["per_company"].append({
            "org": org, "name": name, "stratum": stratum, "website_availability": avail,
            "website_url": website_url, "identity_basis": website_basis, "relationship": website_relationship,
            "requests": client.used_total, "candidates_tried": len(cands),
            "web_attempts": result.shared.get("web_attempts", []),
        })
        print(f"[{stratum}] {org} {name}: website={avail} requests={client.used_total} candidates={len(cands)}", flush=True)
        if save_json:
            Path(save_json).write_text(json.dumps(stats["per_company"], indent=2, ensure_ascii=False))
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--stratum", choices=["small", "large", "none"], default=None)
    parser.add_argument("--per-company-budget", type=int, default=15)
    parser.add_argument("--fixture", default=str(ROOT / "tests/fixtures/website-probe-150.json"))
    parser.add_argument("--save-json", default=None, help="Write per-company results to this JSON file as they complete.")
    args = parser.parse_args()

    entries = json.loads(Path(args.fixture).read_text())
    if args.stratum:
        entries = [e for e in entries if e.get("tier") == args.stratum]
    if args.limit:
        entries = entries[: args.limit]

    stats = run(entries, per_company_budget=args.per_company_budget, save_json=args.save_json)

    print("\n=== Summary by stratum ===")
    for stratum, bucket in stats["by_stratum"].items():
        n = bucket["companies"]
        print(f"\n{stratum} (n={n}):")
        for key in sorted(bucket):
            if key == "companies":
                continue
            print(f"  {key}: {bucket[key]} ({100 * bucket[key] / n:.0f}%)" if n else f"  {key}: {bucket[key]}")

    reqs = stats["requests_per_company"]
    if reqs:
        reqs_sorted = sorted(reqs)
        p50 = reqs_sorted[len(reqs_sorted) // 2]
        print(f"\nRequests per company: mean={sum(reqs) / len(reqs):.1f} p50={p50} max={max(reqs)}")

    print(f"\n=== Manual review needed ({len(stats['manual_review'])} verdicts: exact-without-orgnr/wikidata/nav, or related) ===")
    for item in stats["manual_review"]:
        print(f"  {item}")


if __name__ == "__main__":
    main()
