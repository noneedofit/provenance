#!/usr/bin/env python3
"""Live measurement for W4 (jobs & activity) against real Norwegian company sites.

This is NOT part of the timed pipeline and is not covered by `pyproject.toml` - it uses only the
standard library (urllib) for HTTP so it needs no dependency beyond what's already installed for
`signalpost.activity` (bs4/lxml). It exists because `signalpost.http.BudgetedHttpClient` (W1) and the
web connector (W3, which produces `ctx.shared`) are being built in parallel and aren't necessarily
present in this worktree yet - see BUILD_SPEC.md's package layout. When they land, this script can be
pointed at the real `HttpClient` instead of `_MinimalHttpClient` below.

For each company: fetch the homepage, do a quick local extraction of feed links, ATS links, and a
YouTube profile link (the same job W3's `crawl.py`/`extract.py` will do for real), then run our actual
`signalpost.activity.ats/feeds/youtube` connector code against those links and report what came back.
NAV job claims are cache-based (see nav_jobs.py) and are not exercised here since there is no
`caches.nav` fixture available outside the prepared cache tarball.

Usage:
    uv run --with beautifulsoup4 --with lxml --with pydantic python scripts/eval_activity_live.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bs4 import BeautifulSoup  # noqa: E402

from signalpost.activity import ats, feeds, youtube  # noqa: E402
from signalpost.context import Response  # noqa: E402

USER_AGENT = "SignalpostResearchBot/0.1 (+https://github.com/signalpost; evaluation run)"
TIMEOUT = 10.0

NEWS_PATH_RE = re.compile(r"/(?:news|press|aktuelt|nyheter|artikler|blog|presse)(?:/|$)", re.I)


@dataclass
class _MinimalHttpClient:
    """Just enough of the `HttpClient` protocol (context.py) to drive the real connector code."""
    requests_by_org: dict[str, int] = field(default_factory=dict)
    total_requests: int = 0
    _robots_cache: dict[str, RobotFileParser] = field(default_factory=dict)

    def _robots_ok(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._robots_cache.get(origin)
        if rp is None:
            rp = RobotFileParser()
            rp.set_url(urljoin(origin, "/robots.txt"))
            try:
                rp.read()
            except Exception:
                rp = None  # treat unreadable robots.txt as allow, matching a permissive default
            self._robots_cache[origin] = rp
        if rp is None:
            return True
        try:
            return rp.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    def get(self, url, *, org=None, purpose="", accept="*/*", max_bytes=2_000_000, timeout=TIMEOUT,
             respect_robots=True, max_redirects=4, snapshot=True) -> Response:
        self.total_requests += 1
        if org:
            self.requests_by_org[org] = self.requests_by_org.get(org, 0) + 1
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        if respect_robots and not self._robots_ok(url):
            return Response(url=url, final_url=url, redirect_chain=[url], status=0, headers={}, body=b"",
                             retrieved_at=retrieved_at, content_sha256="", elapsed_ms=0, requests_used=0,
                             error="robots_disallowed")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(max_bytes)
                final_url = resp.geturl()
                status = resp.status
        except urllib.error.HTTPError as exc:
            return Response(url=url, final_url=url, redirect_chain=[url], status=exc.code, headers={}, body=b"",
                             retrieved_at=retrieved_at, content_sha256="", elapsed_ms=int((time.time() - start) * 1000),
                             requests_used=1, error=f"http_{exc.code}")
        except Exception as exc:
            return Response(url=url, final_url=url, redirect_chain=[url], status=0, headers={}, body=b"",
                             retrieved_at=retrieved_at, content_sha256="", elapsed_ms=int((time.time() - start) * 1000),
                             requests_used=1, error=f"{type(exc).__name__}")
        import hashlib
        return Response(url=url, final_url=final_url, redirect_chain=[url, final_url] if final_url != url else [url],
                         status=status, headers={}, body=body, retrieved_at=retrieved_at,
                         content_sha256=hashlib.sha256(body).hexdigest(), elapsed_ms=int((time.time() - start) * 1000),
                         requests_used=1, error=None)

    def post_json(self, *a, **kw):
        raise NotImplementedError

    def remaining(self, org=None) -> int:
        return 50

    def dns_resolves(self, hostname: str) -> bool:
        return True


class _Ctx:
    def __init__(self, org, client, shared, tier="T2"):
        self.org = org
        self.client = client
        self.shared = shared
        self.tier = tier
        self.caches = None


def discover_shared(client: _MinimalHttpClient, org: str, homepage: str) -> dict | None:
    """Minimal stand-in for W3's crawl+extract: fetch the homepage, pull feed/ATS/YouTube/news links."""
    resp = client.get(homepage, org=org, purpose="probe_homepage", accept="text/html", respect_robots=True)
    if not resp.ok:
        return None
    soup = BeautifulSoup(resp.text(), "lxml")
    domain = urlparse(resp.final_url).netloc

    feed_urls = []
    for link in soup.find_all("link", attrs={"type": re.compile("rss|atom", re.I)}):
        href = link.get("href")
        if href:
            feed_urls.append(urljoin(resp.final_url, href))

    ats_links = []
    news_urls = []
    social_links = []
    ats_domains = re.compile(
        r"teamtailor\.com|jobs\.lever\.co|greenhouse\.io|smartrecruiters\.com|webcruiter\.no|"
        r"jobylon\.com|reachmee\.com|hr-manager\.net|recman\.(?:no|io)|easycruit\.com|jobbnorge\.no|"
        r"myworkdayjobs\.com", re.I,
    )
    for a in soup.find_all("a", href=True):
        href = urljoin(resp.final_url, a["href"])
        if ats_domains.search(href):
            ats_links.append(href)
        elif "youtube.com/" in href and ("/channel/" in href or "/@" in href or "/c/" in href or "/user/" in href):
            social_links.append({"platform": "youtube", "url": href})
        elif urlparse(href).netloc == domain and NEWS_PATH_RE.search(urlparse(href).path):
            news_urls.append(href)

    return {
        "verified_site": {"url": resp.final_url, "domain": domain},
        "feed_urls": sorted(set(feed_urls))[:2],
        "ats_links": sorted(set(ats_links))[:2],
        "news_urls": sorted(set(news_urls))[:3],
        "social_links": social_links[:1],
    }


def pick_companies() -> list[dict]:
    probe_path = ROOT / "tests" / "fixtures" / "website-probe-150.json"
    rows = json.loads(probe_path.read_text())
    picked = []
    for row in rows:
        site = row.get("reg_site") or row.get("deep_hit") or (row.get("guess_live") or [None])[0]
        if isinstance(site, list):
            site = site[0] if site else None
        if not site:
            continue
        if not site.startswith("http"):
            site = "https://" + site
        picked.append({"org": row["org"], "name": row["name"], "url": site})
        if len(picked) >= 14:
            break
    picked.append({"org": "979543883", "name": "DIPS AS", "url": "https://www.dips.com"})
    return picked


def main() -> None:
    companies = pick_companies()
    client = _MinimalHttpClient()
    report = []

    for company in companies:
        org, name, url = company["org"], company["name"], company["url"]
        before = client.total_requests
        shared = discover_shared(client, org, url)
        if shared is None:
            report.append({"org": org, "name": name, "url": url, "error": "homepage_fetch_failed", "requests": client.total_requests - before})
            continue

        ctx = _Ctx(org, client, shared)
        ats_result = ats.collect(ctx)
        feeds_result = feeds.collect(ctx)
        yt_result = youtube.collect(ctx)

        report.append({
            "org": org, "name": name, "url": shared["verified_site"]["url"],
            "feed_urls_found": shared["feed_urls"], "ats_links_found": shared["ats_links"],
            "youtube_links_found": [s["url"] for s in shared["social_links"]],
            "ats_provider": ats_result["provider"], "ats_jobs": len(ats_result["claims"]),
            "feed_activity_items": len(feeds_result["claims"]),
            "youtube_items": len(yt_result["claims"]),
            "errors": ats_result.get("errors", []) + feeds_result.get("errors", []) + yt_result.get("errors", []),
            "requests_used": client.total_requests - before,
        })

    summary = {
        "companies": len(companies),
        "total_requests": client.total_requests,
        "requests_by_org": client.requests_by_org,
        "companies_with_any_activity_or_jobs": sum(
            1 for r in report if r.get("ats_jobs") or r.get("feed_activity_items") or r.get("youtube_items")
        ),
        "companies_with_ats_detected": sum(1 for r in report if r.get("ats_provider")),
        "results": report,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
