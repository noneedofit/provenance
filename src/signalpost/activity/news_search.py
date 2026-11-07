"""Dated news about the company from Bing News RSS (keyless; `/news/search` is allowed by bing.com/robots.txt).

One search per company for its exact legal name, in quotes, on the Norwegian market. An item is published only
when all of these hold:
  - the headline or snippet names the company (its legal name without the form suffix, folded);
  - the article page, fetched live (robots.txt respected), shows the company's full legal name including its
    legal form ("Fruktkongen AS") or its organisation number. Norwegian AS/ASA names are unique in the
    register, so a namesake (a product, a foreign company, a common word) does not pass;
  - the page itself states its publication date (meta, JSON-LD or <time>): the feed's date is never used.
At most MAX_ARTICLES pages are fetched per company. Bing is called at most once every BING_INTERVAL_S seconds
across the run. SIGNALPOST_NEWS_SEARCH=0 turns the step off.
"""
from __future__ import annotations

import html as html_mod
import os
import re
import threading
import time
import urllib.parse
from typing import Any
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from ..text import fold
from ._common import make_claim, make_evidence
from .feeds import extract_date_from_html, raw_news_page_quote

BING_NEWS_RSS = "https://www.bing.com/news/search?{query}"
MAX_ARTICLES = 3
BING_INTERVAL_S = 1.0
MIN_CORE_LEN = 4
EXTRACTION_METHOD = "bing_news_rss_v1"
_FORMS = r"(?:as|asa|sa|da|ans|ba|nuf|ks|enk|stiftelse|forening)"
_FORM_SUFFIX = re.compile(r"\s+" + _FORMS + r"(?:\s+i\s+likvidasjon|\s+under\s+avvikling)?$", re.I)
_FORM_PREFIX = re.compile(r"^(?:as|asa|stiftelsen)\s+", re.I)
_NOT_NEWS_HOSTS = ("bing.com", "msn.com/en-us/money/stockdetails", "proff.no", "purehelp.no", "gulesider.no",
                   "1881.no", "brreg.no", "forvalt.no", "regnskapstall.no", "allabolag", "finn.no")

# Price / bond / share-quote pages of finance portals: market data, not news about the company.
_MARKET_DATA = re.compile(r"//(?:markets|finance|quotes?)\.|/(?:bonds?|stocks?|quote|aksjer|kurs)/", re.I)

_bing_lock = threading.Lock()
_bing_last = [0.0]


def _space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def core_name(legal_name: str) -> str:
    """'FRUKTKONGEN AS' -> 'FRUKTKONGEN'; 'AS SLETTEBAKKVEIEN 104' -> 'SLETTEBAKKVEIEN 104'."""
    name = _space(legal_name or "")
    return _space(_FORM_PREFIX.sub("", _FORM_SUFFIX.sub("", name)))


def _contains(haystack_folded: str, needle: str) -> bool:
    """Whole-word containment of `needle` (folded) in an already folded, space-normalised text."""
    n = re.escape(_space(fold(needle)))
    return bool(n) and re.search(r"(?<![0-9a-z])" + n + r"(?![0-9a-z])", haystack_folded) is not None


def _article_url(link: str) -> str | None:
    """Bing wraps the article URL in an apiclick redirect; the real URL is its `url` parameter."""
    link = (link or "").strip()
    parsed = urllib.parse.urlsplit(link)
    if parsed.netloc.endswith("bing.com"):
        target = urllib.parse.parse_qs(parsed.query).get("url", [None])[0]
        link = target or ""
    if not link.startswith(("https://", "http://")):
        return None
    return link


def parse_rss(body: bytes) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    items = []
    for item in root.iter("item"):
        url = _article_url(item.findtext("link") or "")
        if not url:
            continue
        items.append({
            "title": _space(html_mod.unescape(item.findtext("title") or "")),
            "description": _space(html_mod.unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or ""))),
            "url": url,
        })
    return items


def _org_on_page(text: str, org: str) -> bool:
    digits = re.sub(r"\D", "", org)
    pattern = r"(?<!\d)" + r"[\s. ]?".join(digits[i:i + 3] for i in range(0, 9, 3)) + r"(?!\d)"
    return re.search(pattern, text) is not None


def _wait_for_bing() -> None:
    with _bing_lock:
        wait = _bing_last[0] + BING_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _bing_last[0] = time.monotonic()


def collect(ctx: Any) -> dict:
    """{"claims", "evidence", "checked", "errors"}"""
    result: dict = {"claims": [], "evidence": [], "checked": False, "errors": []}
    shared = ctx.shared or {}
    client = ctx.client
    if os.environ.get("SIGNALPOST_NEWS_SEARCH", "1") == "0" or client is None or shared.get("registry_only"):
        return result
    on_pace = shared.get("on_pace")
    if callable(on_pace) and not on_pace():
        return result
    legal_name = _space((shared.get("registry_facts") or {}).get("name") or "")
    core = core_name(legal_name)
    if len(core) < MIN_CORE_LEN or core.casefold() == legal_name.casefold() and not re.search(r"\s", core):
        return result  # no legal form to anchor on and a one-word name: too ambiguous to search
    if client.remaining(ctx.org) < 2:
        return result
    query = urllib.parse.urlencode({"q": f'"{core}"', "format": "rss", "setmkt": "nb-NO", "setlang": "nb"})
    url = BING_NEWS_RSS.format(query=query)
    _wait_for_bing()
    resp = client.get(url, org=ctx.org, purpose="news_search_bing", accept="application/rss+xml, application/xml, text/xml",
                      timeout=15.0, respect_robots=True, snapshot=False)
    if not resp.ok:
        result["errors"].append({"stage": "news_search", "url": url, "error": resp.error or f"http_{resp.status}"})
        return result
    result["checked"] = True
    own_site = str(shared.get("verified_site") or "")
    own_host = urllib.parse.urlsplit(own_site).netloc.lower().removeprefix("www.") if own_site else ""
    seen: set[str] = set()
    fetched = 0
    for item in parse_rss(resp.body):
        if fetched >= MAX_ARTICLES or client.remaining(ctx.org) < 1:
            break
        host = urllib.parse.urlsplit(item["url"]).netloc.lower().removeprefix("www.")
        if item["url"] in seen or any(bad in item["url"] for bad in _NOT_NEWS_HOSTS) or (own_host and host == own_host) \
                or _MARKET_DATA.search(item["url"]):
            continue
        if not _contains(fold(item["title"] + " " + item["description"]), core):
            continue  # the result does not name the company
        seen.add(item["url"])
        fetched += 1
        page = client.get(item["url"], org=ctx.org, purpose="news_article", accept="text/html", respect_robots=True)
        if not page.ok:
            result["errors"].append({"stage": "news_article", "url": item["url"], "error": page.error or f"http_{page.status}"})
            continue
        page_html = page.text()
        text = _space(BeautifulSoup(page_html, "lxml").get_text(" ", strip=True))
        by_org = _org_on_page(text, ctx.org)
        if not by_org and not _contains(fold(text), legal_name):
            continue  # the page never names the company with its legal form or org number: possibly a namesake
        iso, span = extract_date_from_html(page_html)
        if not iso:
            continue  # no publication date stated by the page itself
        article_url = page.final_url or item["url"]
        ev = make_evidence(
            source_url=item["url"], final_url=page.final_url, redirect_chain=page.redirect_chain, http_status=page.status,
            source_class="news_media", retrieved_at=page.retrieved_at, content_sha256=page.content_sha256,
            snapshot_ref=page.snapshot_ref, extraction_method=EXTRACTION_METHOD, span=span or "",
            access_policy="robots-allowed", quote=raw_news_page_quote(page_html, span),
        )
        result["evidence"].append(ev)
        result["claims"].append(make_claim(
            org=ctx.org, family="activity", field="activity_item",
            value={"title": item["title"], "url": article_url, "published": iso, "source": host},
            value_key=article_url, availability="available",
            identity_basis="org_number_on_source" if by_org else "legal_name_on_source",
            effective_date=iso, evidence_ids=[ev.evidence_id],
            note=None if by_org else "the article names the company by its full registered name",
        ))
    return result
