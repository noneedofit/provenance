"""ATS (applicant tracking system) job claims, detected from links on the verified company site.

Only runs against `ctx.shared["ats_links"]` / links found on `ctx.shared["site_pages"]` published by the
web connector (W3) - i.e. only after the company's website was verified `exact`. Every claim therefore
gets `identity_basis="linked_from_verified_site"`.

Keyless public job-board endpoints are used where they exist (Teamtailor RSS, Lever JSON, Greenhouse
JSON, SmartRecruiters JSON). Everything else (Webcruiter, Jobylon, ReachMee, HR-manager, Recman,
Easycruit, Jobbnorge, Workday) falls back to a conservative same-domain link scrape of the careers page.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ._common import make_claim, make_evidence, norm_title
from .feeds import parse_feed

TEAMTAILOR_RE = re.compile(r"([a-z0-9-]+)\.teamtailor\.com", re.I)
LEVER_RE = re.compile(r"jobs\.lever\.co/([a-z0-9-]+)", re.I)
GREENHOUSE_RE = re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([a-z0-9-]+)", re.I)
SMARTRECRUITERS_RE = re.compile(r"smartrecruiters\.com/([a-zA-Z0-9\-]+)", re.I)

# provider -> (detection regex, api-handled)
_PROVIDERS: list[tuple[str, re.Pattern]] = [
    ("teamtailor", TEAMTAILOR_RE),
    ("lever", LEVER_RE),
    ("greenhouse", GREENHOUSE_RE),
    ("smartrecruiters", SMARTRECRUITERS_RE),
    ("webcruiter", re.compile(r"webcruiter\.no", re.I)),
    ("jobylon", re.compile(r"jobylon\.com", re.I)),
    ("reachmee", re.compile(r"reachmee\.com", re.I)),
    ("hr-manager", re.compile(r"hr-manager\.net", re.I)),
    ("recman", re.compile(r"recman\.(?:no|io)", re.I)),
    ("easycruit", re.compile(r"easycruit\.com", re.I)),
    ("jobbnorge", re.compile(r"jobbnorge\.no", re.I)),
    ("workday", re.compile(r"myworkdayjobs\.com", re.I)),
]

_HTML_STOPWORDS = {
    "home", "log in", "logg inn", "contact", "kontakt", "cookies", "personvern",
    "about", "om oss", "search", "søk", "sign in", "privacy", "vilkår", "terms",
}


def candidate_urls(ctx) -> list[str]:
    shared = ctx.shared or {}
    urls = [u for u in (shared.get("ats_links") or []) if isinstance(u, str)]
    for page in shared.get("site_pages") or []:
        urls += [u for u in (page.get("links") or []) if isinstance(u, str)]
    seen: set[str] = set()
    out = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def detect(urls: list[str]) -> tuple[str, str] | None:
    for url in urls:
        for name, pattern in _PROVIDERS:
            if pattern.search(url):
                return name, url
    return None


def _fetch_teamtailor(ctx, matched_url: str):
    m = TEAMTAILOR_RE.search(matched_url)
    sub = m.group(1) if m else None
    feed_url = f"https://{sub}.teamtailor.com/jobs.rss" if sub else matched_url
    resp = ctx.client.get(
        feed_url, org=ctx.org, purpose="ats_teamtailor_rss",
        accept="application/rss+xml, application/xml, text/xml", respect_robots=True,
    )
    if not resp.ok:
        return [], resp, feed_url, "teamtailor_rss_v1"
    items = [{"title": i.get("title"), "url": i.get("url"), "published": i.get("published")} for i in parse_feed(resp.text())]
    return items, resp, feed_url, "teamtailor_rss_v1"


def _fetch_lever(ctx, matched_url: str):
    m = LEVER_RE.search(matched_url)
    company = m.group(1) if m else None
    api_url = f"https://api.lever.co/v0/postings/{company}?mode=json" if company else matched_url
    resp = ctx.client.get(api_url, org=ctx.org, purpose="ats_lever_json", accept="application/json", respect_robots=True)
    items: list[dict] = []
    if resp.ok:
        try:
            data = json.loads(resp.text())
        except Exception:
            data = []
        for p in data if isinstance(data, list) else []:
            title = p.get("text")
            url = p.get("hostedUrl") or p.get("applyUrl")
            published = None
            created = p.get("createdAt")
            if isinstance(created, (int, float)):
                try:
                    published = datetime.fromtimestamp(created / 1000, tz=timezone.utc).date().isoformat()
                except Exception:
                    published = None
            if title and url:
                items.append({"title": title, "url": url, "published": published})
    return items, resp, api_url, "lever_json_v1"


def _fetch_greenhouse(ctx, matched_url: str):
    m = GREENHOUSE_RE.search(matched_url)
    token = m.group(1) if m else None
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs" if token else matched_url
    resp = ctx.client.get(api_url, org=ctx.org, purpose="ats_greenhouse_json", accept="application/json", respect_robots=True)
    items: list[dict] = []
    if resp.ok:
        try:
            data = json.loads(resp.text())
        except Exception:
            data = {}
        for j in data.get("jobs") or []:
            title = j.get("title")
            url = j.get("absolute_url")
            published = None
            raw = j.get("updated_at") or j.get("first_published")
            if raw:
                published = str(raw)[:10]
            if title and url:
                items.append({"title": title, "url": url, "published": published})
    return items, resp, api_url, "greenhouse_json_v1"


def _fetch_smartrecruiters(ctx, matched_url: str):
    m = SMARTRECRUITERS_RE.search(matched_url)
    company = m.group(1) if m else None
    api_url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings" if company else matched_url
    resp = ctx.client.get(api_url, org=ctx.org, purpose="ats_smartrecruiters_json", accept="application/json", respect_robots=True)
    items: list[dict] = []
    if resp.ok:
        try:
            data = json.loads(resp.text())
        except Exception:
            data = {}
        for p in data.get("content") or []:
            title = p.get("name")
            pid = p.get("id")
            published = p.get("releasedDate")
            if published:
                published = str(published)[:10]
            # Best-effort public URL pattern; SmartRecruiters does not return the public apply URL
            # in the postings-list payload.
            url = f"https://jobs.smartrecruiters.com/{company}/{pid}" if company and pid else None
            if title and url:
                items.append({"title": title, "url": url, "published": published})
    return items, resp, api_url, "smartrecruiters_json_v1"


def _generic_html_jobs(ctx, provider: str, matched_url: str):
    resp = ctx.client.get(matched_url, org=ctx.org, purpose=f"ats_{provider}_html", accept="text/html", respect_robots=True)
    items: list[dict] = []
    if resp.ok:
        soup = BeautifulSoup(resp.text(), "lxml")
        domain = urlparse(matched_url).netloc
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if not text or len(text) < 3 or len(text) > 140 or text.casefold() in _HTML_STOPWORDS:
                continue
            full = urljoin(matched_url, a["href"])
            if urlparse(full).netloc != domain or full in seen:
                continue
            seen.add(full)
            items.append({"title": text, "url": full, "published": None})
            if len(items) >= 20:
                break
    return items, resp, matched_url, "careers_html_v1"


_HANDLERS = {
    "teamtailor": _fetch_teamtailor,
    "lever": _fetch_lever,
    "greenhouse": _fetch_greenhouse,
    "smartrecruiters": _fetch_smartrecruiters,
}


def collect(ctx, exclude_titles: set[str] | None = None) -> dict:
    exclude_titles = exclude_titles or set()
    result: dict = {"claims": [], "evidence": [], "checked": False, "errors": [], "provider": None}
    urls = candidate_urls(ctx)
    hit = detect(urls)
    if hit is None or ctx.client is None:
        return result
    provider, matched_url = hit
    result["provider"] = provider

    handler = _HANDLERS.get(provider, lambda c, u: _generic_html_jobs(c, provider, u))
    items, resp, fetched_url, extraction_method = handler(ctx, matched_url)
    result["checked"] = True
    if not resp.ok:
        result["errors"].append({"stage": f"ats_{provider}", "url": fetched_url, "error": resp.error or f"http_{resp.status}"})
        return result

    ev = make_evidence(
        source_url=fetched_url, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
        http_status=resp.status, source_class="company_owned_platform", retrieved_at=resp.retrieved_at,
        content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
        extraction_method=extraction_method, span=f"provider={provider}; items={len(items)}",
        access_policy="robots-allowed",
    )
    claims = []
    for item in items:
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url or norm_title(title) in exclude_titles:
            continue
        value = {"title": title, "url": url, "published": item.get("published"), "source": provider}
        claims.append(make_claim(
            org=ctx.org, family="jobs", field="job_posting", value=value,
            value_key=f"ats:{url}", availability="available", identity_basis="linked_from_verified_site",
            effective_date=item.get("published"), evidence_ids=[ev.evidence_id],
        ))
    result["claims"] = claims
    result["evidence"] = [ev]
    return result
