"""RSS/Atom feed parsing and dated-item extraction from company site pages.

Consumes `ctx.shared["feed_urls"]` and `ctx.shared["news_urls"]` published by the web connector (W3) -
only present once the company's website is verified `exact`. We never guess feed paths (no blind
`/feed`, `/rss` probing): those URLs must already have been discovered by W3 via links on the verified
site.

Only dated items become claims. Undated items are dropped rather than fabricating a date.
"""
from __future__ import annotations

import json
import re
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from ._common import make_claim, make_evidence

MAX_ACTIVITY_ITEMS = 10

_NORWEGIAN_MONTHS = {
    "januar": 1, "februar": 2, "mars": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12,
}
_NO_MONTH_RE = re.compile(
    r"\b(\d{1,2})\.\s*(" + "|".join(_NORWEGIAN_MONTHS) + r")\s*(\d{4})\b", re.I
)
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(elem: ET.Element, *names: str) -> str | None:
    wanted = {n.lower() for n in names}
    for child in list(elem):
        if _local(child.tag) in wanted and child.text and child.text.strip():
            return child.text.strip()
    return None


def _atom_link(elem: ET.Element) -> str | None:
    links = [c for c in list(elem) if _local(c.tag) == "link"]
    if not links:
        return None
    for link in links:
        if link.get("rel") in (None, "alternate"):
            href = link.get("href")
            if href:
                return href
    href = links[0].get("href")
    return href


def normalize_iso_date(raw: str | None) -> str | None:
    """Best-effort normalization of a date string to an ISO 8601 date (YYYY-MM-DD or full timestamp)."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        return dt.date().isoformat()
    except Exception:
        pass
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    return None


def parse_norwegian_date(text: str) -> str | None:
    m = _NO_MONTH_RE.search(text)
    if m:
        day = int(m.group(1))
        month = _NORWEGIAN_MONTHS[m.group(2).lower()]
        year = int(m.group(3))
        if 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    m = _NUMERIC_DATE_RE.search(text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def parse_feed(text: str) -> list[dict]:
    """Parse an RSS 2.0 or Atom feed into [{title, url, published}], published as ISO date or None."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    items: list[dict] = []
    for elem in root.iter():
        local = _local(elem.tag)
        if local == "item":  # RSS
            title = _child_text(elem, "title")
            link = _child_text(elem, "link")
            pub = _child_text(elem, "pubdate", "pubDate", "date")
            items.append({"title": title, "url": link, "published": normalize_iso_date(pub)})
        elif local == "entry":  # Atom
            title = _child_text(elem, "title")
            link = _atom_link(elem)
            pub = _child_text(elem, "published") or _child_text(elem, "updated")
            items.append({"title": title, "url": link, "published": normalize_iso_date(pub)})
    return items


def extract_date_from_html(html: str) -> tuple[str | None, str | None]:
    """Best-effort (iso_date, span) from <time datetime>, meta article:published_time, JSON-LD
    datePublished, or Norwegian-language date text. Returns (None, None) if nothing parseable found."""
    soup = BeautifulSoup(html, "lxml")

    time_el = soup.find("time", attrs={"datetime": True})
    if time_el and time_el.get("datetime"):
        iso = normalize_iso_date(time_el["datetime"])
        if iso:
            return iso, f'<time datetime="{time_el["datetime"]}">'

    meta = soup.find("meta", attrs={"property": "article:published_time"})
    if meta and meta.get("content"):
        iso = normalize_iso_date(meta["content"])
        if iso:
            return iso, "meta[property=article:published_time]"

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if isinstance(obj, dict) and obj.get("datePublished"):
                iso = normalize_iso_date(str(obj["datePublished"]))
                if iso:
                    return iso, "JSON-LD datePublished"

    text = soup.get_text(" ", strip=True)[:4000]
    iso = parse_norwegian_date(text)
    if iso:
        return iso, "regex Norwegian date pattern"
    return None, None


def _extract_title(html: str, fallback: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return fallback


def collect(ctx) -> dict:
    """Fetch shared feed_urls + news_urls, extract dated items, return the ≤10 most recent as claims."""
    result: dict = {"claims": [], "evidence": [], "checked": False, "errors": []}
    shared = ctx.shared or {}
    feed_urls = list(shared.get("feed_urls") or [])
    news_urls = list(shared.get("news_urls") or [])
    if not feed_urls and not news_urls:
        return result
    if ctx.client is None:
        return result

    seen_urls: set[str] = set()
    candidates: list[tuple[dict, object]] = []  # (item, evidence)

    for feed_url in feed_urls:
        resp = ctx.client.get(
            feed_url, org=ctx.org, purpose="site_feed",
            accept="application/rss+xml, application/atom+xml, application/xml, text/xml",
            respect_robots=True,
        )
        result["checked"] = True
        if not resp.ok:
            result["errors"].append({"stage": "feed", "url": feed_url, "error": resp.error or f"http_{resp.status}"})
            continue
        parsed = parse_feed(resp.text())
        ev = make_evidence(
            source_url=feed_url, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
            http_status=resp.status, source_class="company_owned", retrieved_at=resp.retrieved_at,
            content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
            extraction_method="rss_v1", span=f"feed item count={len(parsed)}", access_policy="robots-allowed",
        )
        for item in parsed:
            url = item.get("url")
            if not url or not item.get("published") or url in seen_urls:
                continue
            seen_urls.add(url)
            candidates.append((item, ev))

    for news_url in news_urls:
        resp = ctx.client.get(news_url, org=ctx.org, purpose="site_news_page", accept="text/html", respect_robots=True)
        result["checked"] = True
        if not resp.ok:
            result["errors"].append({"stage": "news_page", "url": news_url, "error": resp.error or f"http_{resp.status}"})
            continue
        html = resp.text()
        iso, span = extract_date_from_html(html)
        if not iso or news_url in seen_urls:
            continue
        seen_urls.add(news_url)
        ev = make_evidence(
            source_url=news_url, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
            http_status=resp.status, source_class="company_owned", retrieved_at=resp.retrieved_at,
            content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
            extraction_method="html_news_v1", span=span or "", access_policy="robots-allowed",
        )
        title = _extract_title(html, fallback=news_url)
        candidates.append(({"title": title, "url": news_url, "published": iso}, ev))

    candidates.sort(key=lambda pair: pair[0]["published"], reverse=True)
    claims = []
    evidence = []
    for item, ev in candidates[:MAX_ACTIVITY_ITEMS]:
        claim = make_claim(
            org=ctx.org, family="activity", field="activity_item",
            value={"title": item["title"], "url": item["url"], "published": item["published"], "source": "company website"},
            value_key=item["url"], availability="available", identity_basis="linked_from_verified_site",
            effective_date=item["published"], evidence_ids=[ev.evidence_id],
        )
        claims.append(claim)
        evidence.append(ev)
    result["claims"] = claims
    result["evidence"] = evidence
    return result
