"""Fetch a candidate's homepage plus a bounded set of secondary pages.

Uses `ctx.client` (context.HttpClient) exclusively — no direct network access, so unit tests can inject a
fake client. Respects robots.txt via the client (`respect_robots=True`). No JS rendering: JS-only shells are
detected and recorded, never rendered.
"""
from __future__ import annotations

import gzip
import re
import urllib.parse
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from ..text import fold
from .blocklist import is_marketplace_or_directory
from .candidates import Candidate, registered_domain

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# Secondary pages to fetch by tier, beyond the homepage.
SECONDARY_PAGE_BUDGET = {"T0": 0, "T1": 2, "T2": 4, "T3": 6}

# Ordered priority terms (path or link-text substrings, casefolded) — first match wins.
PRIORITY_TERMS = (
    "kontakt", "contact",
    "om-oss", "om_oss", "about-us", "about",
    # "kontoret" ("the office") is a common Norwegian about-us-equivalent page slug, especially for
    # architecture/law/consulting firms -- e.g. rakark.no's homepage has no body text at all (an
    # image-portfolio nav list only), but /kontoret/ carries the real substantive company description.
    # Substring-matches both "kontor" and "kontoret" paths/link text without colliding with "kontakt"
    # (checked: "kontor" is not a substring of "kontakt" or vice versa).
    "kontor",
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
    # Registrar parking pages (Domeneshop and similar), seen on name-guessed .no domains
    "does not have an active website here", "currently does not have an active website",
)
# "Coming soon" wording only marks a placeholder when it is (nearly) all the page says: real sites also
# write "nye produkter kommer snart".
PLACEHOLDER_MARKERS = (
    "lanseres snart", "kommer snart", "her kommer:", "under construction", "under oppbygging",
    "coming soon", "nettsiden er under arbeid", "siden er under utvikling", "site under construction",
    # Hosting-provider and web-server default pages: the domain is registered but holds no site.
    "this one is taken, but you can find another available domain", "hosted by one.com",
    "index of /", "proudly served by litespeed web server", "welcome to nginx!", "apache2 ubuntu default page",
    "apache2 debian default page", "test page for the apache http server", "default web site page",
)
PLACEHOLDER_MAX_TEXT = 900


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


def is_parked_page(html: str, text: str) -> bool:
    """A parked/for-sale page, or a page that is (nearly) only a placeholder or a hosting/server default
    page. Placeholder wording is checked on the page's own text as well as the extracted text: text
    extraction keeps only the "main" paragraph and can drop the "under construction" line."""
    haystack = fold((html or "")[:5000] + " " + (text or "")[:2000])
    if any(marker in haystack for marker in PARKED_MARKERS):
        return True
    visible = [fold(text or "")]
    if html and len(html) < 200_000:
        visible.append(fold(BeautifulSoup(html, "lxml").get_text(" ", strip=True)))
    return any(len(v) <= PLACEHOLDER_MAX_TEXT and any(marker in v for marker in PLACEHOLDER_MARKERS) for v in visible)


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


_CONNECT_ERRORS = {"network_error", "ssl_error"}
_NAME_STOP = {"as", "asa", "ans", "da", "enk", "sa", "nuf", "iks", "holding", "invest", "eiendom", "norge", "norway", "group", "gruppen"}


def _homepage_names_company(ctx: Any, homepage: PageFetch) -> bool:
    name = str((getattr(ctx, "bulk", None) or {}).get("navn") or "")
    tokens = {t for t in re.findall(r"[a-z0-9]+", fold(name)) if len(t) >= 4 and t not in _NAME_STOP}
    if not tokens:
        return False
    page_tokens = set(re.findall(r"[a-z0-9]+", fold(homepage.title + " " + homepage.text[:20000])))
    return bool(tokens & page_tokens)


def _alternate_homepages(ctx: Any, url: str, *, skip_same_host_http: bool = False) -> list[str]:
    """Other scheme/host spellings of a homepage, in the order browsers effectively try them, limited to
    hosts that resolve in DNS (a local lookup, not an HTTP request)."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return []
    other = host[4:] if host.startswith("www.") else f"www.{host}"
    resolves = getattr(ctx.client, "dns_resolves", None)
    hosts = [h for h in (host, other) if resolves is None or resolves(h)]
    path = parts.path or "/"
    out = []
    for h in hosts:
        for scheme in ("https", "http"):
            alt = f"{scheme}://{h}{path}"
            if skip_same_host_http and h == host and scheme == "http":
                continue
            if alt != url and alt not in out:
                out.append(alt)
    return out[:2]


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
    if homepage is not None and not homepage.ok and homepage.error in _CONNECT_ERRORS:
        # Many small Norwegian sites answer only on plain http, or only on (or without) "www.". A connection
        # failure on the one URL we tried is not evidence that the site does not exist.
        tried_http = homepage.error == "ssl_error"  # the HTTP client already retried plain http on this host
        for alt in _alternate_homepages(ctx, candidate.url, skip_same_host_http=tried_http):
            if ctx.client.remaining(ctx.org) < 1:
                break
            retry, used = _fetch_page(ctx, alt, "web_homepage_fallback")
            result.requests_used += used
            if retry is not None and (retry.ok or retry.error not in _CONNECT_ERRORS):
                homepage = retry
                break
    if homepage is None or not homepage.ok:
        result.fatal_error = homepage.error if homepage else "fetch_failed"
        if homepage is not None:
            result.pages.append(homepage)
        return result
    homepage.page_kind = "homepage"
    result.pages.append(homepage)

    if is_parked_page(homepage.html, homepage.text) or _is_dead_redirect(candidate.url, homepage.final_url):
        result.parked = True
        return result

    soup = BeautifulSoup(homepage.html, "lxml")
    homepage.links = _extract_links(homepage.final_url, soup, candidate.domain)
    if _is_js_shell(homepage.text, soup):
        result.js_shell = True

    if max_secondary <= 0 and _homepage_names_company(ctx, homepage):
        # A shell-tier company still gets one contact/about page when its homepage already carries its name:
        # that page is where the address, phone and org number usually are.
        max_secondary = 1
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
