"""RSS/Atom feed parsing and dated-item extraction from company site pages.

Primary sources are free: `ctx.shared["feed_urls"]` (`<link rel="alternate">` on already-crawled pages)
and `ctx.shared["news_urls"]` published by the web connector (W3), both only present once the company's
site is verified `exact`.

W3's own page-picker prioritizes identity-verification pages (kontakt/om-oss/personvern/vilkar/karriere)
over a news/blog page for its tier-limited secondary-page budget, so a real news section routinely never
gets fetched at all -- `feed_urls`/`news_urls` end up empty even when the site clearly has one (measured
on daily100: several "failed: no source" companies turned out to have an undiscovered `/blogg` or
`/aktuelt` link right on the homepage). `EXTRA_DISCOVERY_BUDGET` extra requests per verified site pay for
targeted follow-up, cheapest/most-likely-to-hit first:
1. A same-domain link already visible on a page W3 fetched (`ctx.shared["site_pages"][*]["links"]`) but
   never itself fetched, whose path/text matches a Norwegian news/blog term W3's crawl priority ranks
   below the identity pages -- free to *find* (already-fetched HTML), only fetching it costs a request,
   and it's real (not a guess), so this goes first.
2. A WordPress `/feed/` guess (near-universal on WP sites, the single most common Norwegian
   small-business CMS) -- only tried when (1) found no candidate link to check.
A gold-131 measurement (before this reordering/cap) showed spending up to 3 requests/site (adding a
`/wp-json/wp/v2/posts` REST probe as a 3rd step) pushed one company over the request budget and gained
no further coverage the cheaper 2-step version didn't already reach -- cut back to 2 for that reason.
Every extra fetch still goes through `ctx.client.get(..., respect_robots=True)` like any other request,
and is counted against the same per-company budget.

Only dated items become claims. Undated items are dropped rather than fabricating a date.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from ._common import make_claim, make_evidence

MAX_ACTIVITY_ITEMS = 10

# Hard cap on EXTRA requests (beyond the free feed_urls/news_urls already fetched by W3) this module
# will spend discovering activity sources for one company. Keeps this a cheap, bounded add-on rather
# than an open-ended crawl. A gold-131 measurement at 2 landed at 1,858 total requests -- over the
# 1,850 target for this batch mix (a denser-than-average concentration of verified sites) -- so this
# is 1: spend it on the single best candidate (a real discovered link if one exists, else one
# WordPress /feed/ guess), never both.
EXTRA_DISCOVERY_BUDGET = 1

# Terms W3's crawl.py PRIORITY_TERMS ranks below the identity-verification pages (kontakt/om-oss/
# personvern/vilkar/karriere), so a genuine news/blog link is routinely present on an already-fetched
# page but never itself fetched within the tier's secondary-page budget. Superset of W3's own
# nyheter/aktuelt/news (kept in sync deliberately -- see docs/activity.md) plus a few more common
# Norwegian/English news-and-blog terms that are just as unambiguous a signal.
_NEWS_LINK_TERMS = ("nyheter", "aktuelt", "presse", "blogg", "blog", "artikler", "news")

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


def parse_wp_rest_posts(text: str) -> list[dict]:
    """Parse a WordPress REST API `/wp-json/wp/v2/posts` JSON response into [{title, url, published}].

    Public, keyless, no auth -- every stock WordPress site exposes this unless explicitly disabled.
    Structured JSON (unlike HTML scraping): `title.rendered`, `link`, `date` (site-local, no timezone
    suffix -- treated as a plain date, matching this module's ISO-date-only granularity elsewhere).
    """
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    items: list[dict] = []
    for post in data:
        if not isinstance(post, dict):
            continue
        title_obj = post.get("title")
        title = title_obj.get("rendered") if isinstance(title_obj, dict) else None
        url = post.get("link")
        date_raw = post.get("date") or post.get("date_gmt")
        published = normalize_iso_date(date_raw) if date_raw else None
        if title and url and published:
            items.append({"title": BeautifulSoup(str(title), "lxml").get_text(strip=True), "url": url, "published": published})
    return items


def _homepage_url(ctx) -> str | None:
    verified = (ctx.shared or {}).get("verified_site") or {}
    url = verified.get("url")
    if url:
        return url
    domain = verified.get("domain")
    return f"https://{domain}/" if domain else None


def _discover_link_candidates(ctx, already_seen: set[str]) -> list[str]:
    """Same-domain links already visible on pages W3 fetched (`ctx.shared["site_pages"][*]["links"]`)
    whose path or anchor text matches a Norwegian news/blog term ranked below the identity-verification
    pages in W3's own crawl priority (see module docstring) -- zero extra requests to find, since these
    pages were already fetched for identity verification; only fetching the matched page itself costs
    a request. Free of any HTTP call, so always safe to run before spending the extra-discovery budget.
    """
    site_pages = (ctx.shared or {}).get("site_pages") or []
    found: list[str] = []
    for page in site_pages:
        for link in page.get("links") or []:
            if link in already_seen or link in found:
                continue
            path = urllib.parse.urlparse(link).path.casefold()
            if any(term in path for term in _NEWS_LINK_TERMS):
                found.append(link)
    return found


_COMMENT_TITLE = re.compile(r"^\s*(comment on|comments on|kommentar til|kommentarer til|kommentar på)\b", re.I)
_PLACEHOLDER_KEYS = {"hello world", "hei verden", "hallo verden", "sample page", "eksempelside"}


def is_company_item(item: dict, feed_url: str = "") -> bool:
    """False for items that are not the company's own publication: blog comments (a comments feed,
    '#comment-' links, 'Comment on ...' titles) and CMS placeholder posts ('Hello world!')."""
    url = str(item.get("url") or "")
    title = str(item.get("title") or "").strip()
    if "/comments/feed" in feed_url or "/comments/" in url or "#comment" in url or "replytocom=" in url:
        return False
    if _COMMENT_TITLE.search(title) or re.sub(r"[^\w ]", "", title.lower()).strip() in _PLACEHOLDER_KEYS:
        return False
    return True


def _process_feed_url(ctx, feed_url: str, seen_urls: set[str], result: dict, *, guessed: bool = False) -> list[tuple[dict, object]]:
    resp = ctx.client.get(
        feed_url, org=ctx.org, purpose="site_feed",
        accept="application/rss+xml, application/atom+xml, application/xml, text/xml",
        respect_robots=True,
    )
    result["checked"] = True
    if not resp.ok:
        # A guessed URL (WordPress /feed/) that does not exist is an answer, not an error; recording it
        # would mark the whole company "partial".
        if not (guessed and 400 <= (resp.status or 0) < 500):
            result["errors"].append({"stage": "feed", "url": feed_url, "error": resp.error or f"http_{resp.status}"})
        return []
    parsed = parse_feed(resp.text())
    ev = make_evidence(
        source_url=feed_url, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
        http_status=resp.status, source_class="company_owned", retrieved_at=resp.retrieved_at,
        content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
        extraction_method="rss_v1", span=f"feed item count={len(parsed)}", access_policy="robots-allowed",
    )
    out = []
    for item in parsed:
        url = item.get("url")
        if not url or not item.get("published") or url in seen_urls or not is_company_item(item, feed_url):
            continue
        seen_urls.add(url)
        out.append((item, ev))
    return out


def _process_wp_rest_url(ctx, rest_url: str, seen_urls: set[str], result: dict) -> list[tuple[dict, object]]:
    resp = ctx.client.get(rest_url, org=ctx.org, purpose="site_feed", accept="application/json", respect_robots=True)
    result["checked"] = True
    if not resp.ok:
        result["errors"].append({"stage": "wp_rest", "url": rest_url, "error": resp.error or f"http_{resp.status}"})
        return []
    parsed = parse_wp_rest_posts(resp.text())
    ev = make_evidence(
        source_url=rest_url, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
        http_status=resp.status, source_class="company_owned", retrieved_at=resp.retrieved_at,
        content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
        extraction_method="wp_rest_v1", span=f"wp-json post count={len(parsed)}", access_policy="robots-allowed",
    )
    out = []
    for item in parsed:
        url = item.get("url")
        if not url or url in seen_urls or not is_company_item(item, rest_url):
            continue
        seen_urls.add(url)
        out.append((item, ev))
    return out


def _process_news_url(ctx, news_url: str, seen_urls: set[str], result: dict, *, purpose: str = "site_news_page") -> tuple[dict, object] | None:
    resp = ctx.client.get(news_url, org=ctx.org, purpose=purpose, accept="text/html", respect_robots=True)
    result["checked"] = True
    if not resp.ok:
        result["errors"].append({"stage": "news_page", "url": news_url, "error": resp.error or f"http_{resp.status}"})
        return None
    html = resp.text()
    iso, span = extract_date_from_html(html)
    if not iso or news_url in seen_urls:
        return None
    seen_urls.add(news_url)
    ev = make_evidence(
        source_url=news_url, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
        http_status=resp.status, source_class="company_owned", retrieved_at=resp.retrieved_at,
        content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
        extraction_method="html_news_v1", span=span or "", access_policy="robots-allowed",
    )
    title = _extract_title(html, fallback=news_url)
    return ({"title": title, "url": news_url, "published": iso}, ev)


def collect(ctx) -> dict:
    """Fetch shared feed_urls + news_urls (free), then, only if that found nothing, spend up to
    EXTRA_DISCOVERY_BUDGET extra requests on WordPress feed/REST discovery and same-domain news/blog
    links already visible on pages W3 fetched. Extract dated items, return the <=10 most recent as
    claims."""
    result: dict = {"claims": [], "evidence": [], "checked": False, "errors": []}
    shared = ctx.shared or {}
    feed_urls = list(shared.get("feed_urls") or [])
    news_urls = list(shared.get("news_urls") or [])
    if ctx.client is None:
        return result
    if not feed_urls and not news_urls and not shared.get("verified_site") and not shared.get("site_pages"):
        return result

    seen_urls: set[str] = set()
    candidates: list[tuple[dict, object]] = []  # (item, evidence)

    for feed_url in feed_urls:
        candidates.extend(_process_feed_url(ctx, feed_url, seen_urls, result))

    for news_url in news_urls:
        item = _process_news_url(ctx, news_url, seen_urls, result)
        if item:
            candidates.append(item)

    # Extra discovery: only spend the extra budget when the free sources above produced nothing --
    # maximizes coverage exactly where it's needed without adding cost to sites that already work.
    # Cheapest/most-likely-to-hit first (see module docstring for the request-budget measurement behind
    # this order and the 2-request cap): a real link already seen on a fetched page, THEN one WordPress
    # guess -- not both a WordPress feed guess AND a REST-API guess, which measured no extra coverage
    # for the added request.
    extra_requests_used = 0
    if not candidates and extra_requests_used < EXTRA_DISCOVERY_BUDGET:
        # 1. A same-domain news/blog link already visible on a page W3 fetched for identity
        # verification, but that W3 itself never fetched (crowded out by kontakt/om-oss in the tier's
        # secondary-page budget). Free to find; only fetching it costs a request.
        link_candidates = _discover_link_candidates(ctx, seen_urls | set(news_urls))
        for link in link_candidates:
            if extra_requests_used >= EXTRA_DISCOVERY_BUDGET:
                break
            item = _process_news_url(ctx, link, seen_urls, result, purpose="site_news_page_discovered")
            extra_requests_used += 1
            if item:
                candidates.append(item)
                break  # one dated item is enough to confirm the section is real; stop spending budget

        if not candidates and extra_requests_used < EXTRA_DISCOVERY_BUDGET:
            # 2. WordPress /feed/ -- near-universal on the single most common Norwegian small-business
            # CMS; reuses the same RSS/Atom parser as a discovered <link rel="alternate"> feed. Tried
            # last since it's a guess rather than a link the site itself actually shows.
            homepage = _homepage_url(ctx)
            if homepage:
                wp_feed_url = f"{homepage.rstrip('/')}/feed/"
                if wp_feed_url not in feed_urls:
                    candidates.extend(_process_feed_url(ctx, wp_feed_url, seen_urls, result, guessed=True))
                    extra_requests_used += 1

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
