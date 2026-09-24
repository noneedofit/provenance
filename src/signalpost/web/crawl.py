"""Fetch a candidate's homepage plus a bounded set of secondary pages.

Uses `ctx.client` (context.HttpClient) exclusively — no direct network access, so unit tests can inject a
fake client. Respects robots.txt via the client (`respect_robots=True`). No JS rendering: JS-only shells are
detected and recorded, never rendered.
"""
from __future__ import annotations

import gzip
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from .blocklist import is_marketplace_or_directory
from .candidates import Candidate, registered_domain

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# Secondary pages to fetch by tier, beyond the homepage.
SECONDARY_PAGE_BUDGET = {"T0": 0, "T1": 2, "T2": 4, "T3": 6}

# Ordered priority terms (path or link-text substrings, casefolded) — first match wins.
PRIORITY_TERMS = (
    "kontakt", "contact",
    "om-oss", "om_oss", "about-us", "about",
    "personvern", "privacy",
    "vilkar", "salgsbetingelser", "kjopsbetingelser", "terms",
    "impressum",
    "karriere", "jobb", "careers", "jobs",
    "nyheter", "aktuelt", "news",
)

PARKED_MARKERS = (
    "domain is for sale", "domain for sale", "hugedomains", "parked at", "miss hosting",
    "her flytter snart en ny gjest", "has been informing visitors",
    "find the best information and most relevant links on all topics related to",
    "buy this domain", "this domain is parked", "future home of something quite cool",
)


@dataclass
class PageFetch:
    url: str
    final_url: str
    status: int
    ok: bool
    html: str
    text: str
    title: str
    links: list[str] = field(default_factory=list)
    redirect_chain: list[str] = field(default_factory=list)
    content_sha256: str | None = None
    snapshot_ref: str | None = None
    retrieved_at: str | None = None
    error: str | None = None
    page_kind: str = ""  # "homepage" or the priority-term label used to pick it


@dataclass
class CrawlResult:
    candidate: Candidate
    pages: list[PageFetch] = field(default_factory=list)
    parked: bool = False
    js_shell: bool = False
    requests_used: int = 0
    fatal_error: str | None = None  # homepage fetch failed entirely


def _normalize_text(html: str) -> str:
    try:
        import trafilatura

        extracted = trafilatura.extract(html, include_links=False, include_tables=False, favor_precision=True)
        return extracted or ""
    except Exception:
        soup = BeautifulSoup(html, "lxml")
        return soup.get_text(" ", strip=True)


def _is_parked(html: str, text: str) -> bool:
    haystack = unicodedata.normalize("NFKD", (html[:5000] + " " + text[:2000])).encode("ascii", "ignore").decode().casefold()
    return any(marker in haystack for marker in PARKED_MARKERS)


_DEAD_PATH_RE = re.compile(r"/(missing|not[-_]?found|404|error|suspended|expired|deactivated)(?:[/.?#]|$)", re.I)


def _is_dead_redirect(start_url: str, final_url: str) -> bool:
    """A redirect off the candidate's own domain onto a hosting/builder platform or a 'missing' page."""
    start, final = registered_domain(start_url), registered_domain(final_url)
    if not final or final == start:
        return False
    parts = urllib.parse.urlsplit(final_url)
    if _DEAD_PATH_RE.search(parts.path or "/"):
        return True
    # Landing on the platform's own front page (not a tenant site like acme.squarespace.com) means the
    # company's site is gone.
    host = (parts.hostname or "").lower().removeprefix("www.")
    return is_marketplace_or_directory(final) and host == final


def _is_js_shell(text: str, soup: BeautifulSoup) -> bool:
    return len(text.strip()) < 100 and len(soup.select("script[src]")) >= 2


def _extract_links(base_url: str, soup: BeautifulSoup, homepage_domain: str) -> list[str]:
    links: list[str] = []
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if registered_domain(parsed.hostname or "") != homepage_domain:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        links.append(clean)
    return links


def _priority_rank(url: str, anchor_text: str) -> int | None:
    haystack = (urllib.parse.urlparse(url).path + " " + anchor_text).casefold()
    for idx, term in enumerate(PRIORITY_TERMS):
        if term in haystack:
            return idx
    return None


def _pick_priority_links(base_url: str, soup: BeautifulSoup, homepage_domain: str, limit: int) -> list[tuple[str, str]]:
    scored: dict[str, tuple[int, str]] = {}
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or registered_domain(parsed.hostname or "") != homepage_domain:
            continue
        rank = _priority_rank(url, anchor.get_text(" ", strip=True))
        if rank is None:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        term = PRIORITY_TERMS[rank]
        if clean not in scored or rank < scored[clean][0]:
            scored[clean] = (rank, term)
    ordered = sorted(scored.items(), key=lambda item: (item[1][0], item[0]))
    return [(url, term) for url, (_, term) in ordered[:limit]]


def _sitemap_links(ctx: Any, homepage: str, homepage_domain: str, limit: int) -> list[tuple[str, str]]:
    client = ctx.client
    sitemap_url = urllib.parse.urljoin(homepage, "/sitemap.xml")
    resp = client.get(sitemap_url, org=ctx.org, purpose="web_sitemap", respect_robots=True)
    if not resp.ok:
        return []
    body = resp.body
    try:
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        text = body.decode("utf-8", "replace")
    except Exception:
        return []
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
    picked: list[tuple[str, str]] = []
    for url in urls:
        parsed = urllib.parse.urlparse(url)
        if registered_domain(parsed.hostname or "") != homepage_domain:
            continue
        rank = _priority_rank(url, "")
        if rank is None:
            continue
        picked.append((url, PRIORITY_TERMS[rank]))
    picked.sort(key=lambda item: PRIORITY_TERMS.index(item[1]))
    return picked[:limit]


def _fetch_page(ctx: Any, url: str, purpose: str) -> tuple[PageFetch | None, int]:
    client = ctx.client
    resp = client.get(url, org=ctx.org, purpose=purpose, respect_robots=True)
    used = getattr(resp, "requests_used", 1) or 1
    if not resp.ok:
        return PageFetch(
            url=url, final_url=resp.final_url or url, status=resp.status, ok=False, html="", text="",
            title="", redirect_chain=resp.redirect_chain, content_sha256=resp.content_sha256 or None,
            snapshot_ref=resp.snapshot_ref, retrieved_at=resp.retrieved_at, error=resp.error or f"http_{resp.status}",
        ), used
    content_type = (resp.headers or {}).get("content-type", "") if hasattr(resp, "headers") else ""
    html = resp.text()
    if "html" not in content_type.lower() and "<html" not in html[:2000].lower() and content_type:
        return PageFetch(
            url=url, final_url=resp.final_url or url, status=resp.status, ok=False, html="", text="",
            title="", redirect_chain=resp.redirect_chain, content_sha256=resp.content_sha256,
            snapshot_ref=resp.snapshot_ref, retrieved_at=resp.retrieved_at, error="non_html_content",
        ), used
    soup = BeautifulSoup(html, "lxml")
    text = _normalize_text(html)
    title = soup.title.get_text(" ", strip=True)[:500] if soup.title else ""
    page = PageFetch(
        url=url, final_url=resp.final_url or url, status=resp.status, ok=True, html=html, text=text, title=title,
        redirect_chain=resp.redirect_chain, content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
        retrieved_at=resp.retrieved_at,
    )
    return page, used


def crawl_candidate(
    ctx: Any,
    candidate: Candidate,
    *,
    max_secondary: int | None = None,
    stop_check: Callable[[list[PageFetch]], bool] | None = None,
) -> CrawlResult:
    """Fetch homepage then up to `max_secondary` priority pages. Stops early if `stop_check(pages)` is True."""
    tier = getattr(ctx, "tier", "T2")
    if max_secondary is None:
        max_secondary = SECONDARY_PAGE_BUDGET.get(tier, 2)

    result = CrawlResult(candidate=candidate)
    homepage, used = _fetch_page(ctx, candidate.url, "web_homepage")
    result.requests_used += used
    if homepage is None or not homepage.ok:
        result.fatal_error = homepage.error if homepage else "fetch_failed"
        if homepage is not None:
            result.pages.append(homepage)
        return result
    homepage.page_kind = "homepage"
    result.pages.append(homepage)

    if _is_parked(homepage.html, homepage.text) or _is_dead_redirect(candidate.url, homepage.final_url):
        result.parked = True
        return result

    soup = BeautifulSoup(homepage.html, "lxml")
    homepage.links = _extract_links(homepage.final_url, soup, candidate.domain)
    if _is_js_shell(homepage.text, soup):
        result.js_shell = True

    if max_secondary <= 0:
        return result
    if stop_check and stop_check(result.pages):
        return result

    homepage_domain = candidate.domain
    picked = _pick_priority_links(homepage.final_url, soup, homepage_domain, max_secondary)
    if not picked:
        picked = _sitemap_links(ctx, homepage.final_url, homepage_domain, max_secondary)
        result.requests_used += 1  # sitemap fetch itself, counted even if empty

    for url, term in picked:
        if stop_check and stop_check(result.pages):
            break
        page, used = _fetch_page(ctx, url, f"web_secondary_{term}")
        result.requests_used += used
        if page is None:
            continue
        page.page_kind = term
        result.pages.append(page)
        if page.ok:
            page_soup = BeautifulSoup(page.html, "lxml")
            page.links = _extract_links(page.final_url, page_soup, homepage_domain)
            if not result.js_shell and _is_js_shell(page.text, page_soup):
                result.js_shell = True

    return result
