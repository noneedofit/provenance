"""Search-provider candidate discovery (Brave Search API; Serper or Tavily as declared alternatives), used
only when a key is configured.

Builderr's source policy allows a search API to *find* candidates, never to support a claim: every domain a
search result nominates is a plain, non-decisive website candidate that must pass the same live
verification (org number on the site, or registry address/phone/e-mail/role names) as a name guess before
anything is published. Search output is transient: titles, snippets and ranks are discarded once the
candidate domains are taken; nothing from the provider becomes evidence.

Configuration (all optional; without a key this module does nothing and makes no request):
- `BRAVE_SEARCH_API_KEY` (or `BRAVE_API_KEY`): the subscription token, sent as a header, never logged.
- `SERPER_API_KEY` or `TAVILY_API_KEY`: used only when no Brave key is set (first one found wins).
- `SIGNALPOST_SEARCH_RPS`: request rate limit across all workers (default 1 per second).
- `SIGNALPOST_SEARCH_MAX`: maximum searches in one run (default: unlimited; at most one per company).
- `SIGNALPOST_SEARCH_COST_USD`: price per search for the cost report (default 0.005, Brave's list price).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
from typing import Any

from .blocklist import is_marketplace_or_directory

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
MAX_RESULTS = 10
MAX_CANDIDATES = 3

# Directories, registers, news and platform hosts a search for a company name returns: never its own site.
SKIP_DOMAINS: frozenset[str] = frozenset({
    "brreg.no", "proff.no", "purehelp.no", "gulesider.no", "1881.no", "finn.no", "firmasjekk.no",
    "regnskapstall.no", "forvalt.no", "bedriftsdatabasen.no", "enirotreff.no", "kompass.com", "dnb.com",
    "infobel.com", "opencorporates.com", "northdata.com", "northdata.de", "bizapedia.com", "lei-lookup.com",
    "allabolag.se", "hitta.se", "krak.dk", "degulesider.dk", "yelp.com", "tripadvisor.com", "tripadvisor.no",
    "booking.com", "hotels.com", "expedia.com", "trustpilot.com", "glassdoor.com", "glassdoor.no", "indeed.com",
    "wikipedia.org", "wikidata.org", "linkedin.com", "facebook.com", "instagram.com", "youtube.com", "x.com",
    "twitter.com", "tiktok.com", "pinterest.com", "nav.no", "arbeidsplassen.no", "regjeringen.no",
    "lovdata.no", "doffin.no", "nrk.no", "vg.no", "dagbladet.no", "aftenposten.no", "dn.no", "e24.no",
    "tu.no", "finansavisen.no", "kapital.no", "nettavisen.no", "mittanbud.no", "anbudstorget.no",
    "google.com", "apple.com", "bing.com", "amazon.com", "ebay.com", "github.com", "medium.com",
    "kunngjoringer.no", "eniro.no", "bransjen.no", "norskeselskaper.no", "selskapsregisteret.no",
    "rolleregisteret.no", "naeringslivet.no", "bedrift.no", "firmaer.no", "baaam.se", "list.co",
})

_LOCK = threading.Lock()
_STATE: dict = {"last": 0.0, "used": 0, "per_org": {}}


SERPER_ENDPOINT = "https://google.serper.dev/search"
TAVILY_ENDPOINT = "https://api.tavily.com/search"


def provider() -> tuple[str, str] | None:
    """(provider name, key) for the first configured search provider, else None."""
    for name, variables in (("brave", ("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY")), ("serper", ("SERPER_API_KEY",)), ("tavily", ("TAVILY_API_KEY",))):
        for variable in variables:
            key = (os.environ.get(variable) or "").strip()
            if key:
                return name, key
    return None


def api_key() -> str | None:
    found = provider()
    return found[1] if found else None


def enabled() -> bool:
    return provider() is not None


def _rate_wait() -> bool:
    """Space searches across worker threads; False once the run's search cap is used up."""
    try:
        rps = max(0.05, float(os.environ.get("SIGNALPOST_SEARCH_RPS") or 1))
    except ValueError:
        rps = 1.0
    cap_text = os.environ.get("SIGNALPOST_SEARCH_MAX")
    cap = int(cap_text) if cap_text and cap_text.isdigit() else None
    with _LOCK:
        if cap is not None and _STATE["used"] >= cap:
            return False
        wait = _STATE["last"] + 1.0 / rps - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _STATE["last"] = time.monotonic()
        _STATE["used"] += 1
    return True


def reset_for_tests() -> None:
    with _LOCK:
        _STATE["last"] = 0.0
        _STATE["used"] = 0
        _STATE["per_org"] = {}


def _price() -> float:
    try:
        return max(0.0, float(os.environ.get("SIGNALPOST_SEARCH_COST_USD") or 0.005))
    except ValueError:
        return 0.005


def cost_usd(org: str | None = None) -> float:
    """Declared third-party cost of the searches made so far (for one company, or the whole run)."""
    with _LOCK:
        count = _STATE["per_org"].get(org, 0) if org is not None else _STATE["used"]
    return round(count * _price(), 4)


def build_query(name: str, municipality: str | None) -> str:
    """Legal name without the legal-form suffix, quoted, plus the registered municipality."""
    core = re.sub(r"\s+(AS|ASA|SA|DA|ANS|ENK|NUF|BA|SF|KS|IKS|STI|FLI|BRL|AB|A/S)$", "", name.strip(), flags=re.I)
    query = f'"{core}"'
    if municipality:
        query += f" {municipality.title()}"
    return query


def _result_urls(payload: Any) -> list[str]:
    """Result URLs in rank order from a Brave (web.results[].url), Serper (organic[].link) or Tavily
    (results[].url) response."""
    if not isinstance(payload, dict):
        return []
    items = ((payload.get("web") or {}).get("results") if isinstance(payload.get("web"), dict) else None) \
        or payload.get("organic") or payload.get("results") or []
    return [str(i.get("url") or i.get("link") or "") for i in items if isinstance(i, dict)]


def result_domains(payload: Any, registered_domain) -> list[tuple[str, str]]:
    """(domain, homepage url) for each distinct result host that could be the company's own site."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url in _result_urls(payload)[:MAX_RESULTS]:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        domain = registered_domain(parts.hostname)
        if not domain or domain in seen:
            continue
        seen.add(domain)
        if domain in SKIP_DOMAINS or is_marketplace_or_directory(domain) or domain.endswith((".gov", ".kommune.no")):
            continue
        out.append((domain, f"{parts.scheme}://{parts.hostname}/"))
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def search_candidates(client: Any, org: str, name: str, municipality: str | None, registered_domain) -> list[tuple[str, str]]:
    """Run one search for the company and return candidate (domain, url) pairs; [] on any failure."""
    found = provider()
    if not found or client is None or not name or not _rate_wait():
        return []
    which, key = found
    with _LOCK:
        _STATE["per_org"][org] = _STATE["per_org"].get(org, 0) + 1
    query = build_query(name, municipality)
    try:
        if which == "brave":
            params = {"q": query, "country": "NO", "search_lang": "nb", "count": str(MAX_RESULTS)}
            resp = client.get(
                ENDPOINT + "?" + urllib.parse.urlencode(params), org=org, purpose="search_candidates",
                accept="application/json", respect_robots=False, max_redirects=0, snapshot=False, timeout=10.0,
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
            )
        elif which == "serper":
            resp = client.post_json(
                SERPER_ENDPOINT, {"q": query, "gl": "no", "hl": "no", "num": MAX_RESULTS}, org=org,
                purpose="search_candidates", timeout=10.0, headers={"X-API-KEY": key}, snapshot=False,
            )
        else:
            resp = client.post_json(
                TAVILY_ENDPOINT, {"query": query, "max_results": MAX_RESULTS, "country": "norway"}, org=org,
                purpose="search_candidates", timeout=10.0, headers={"Authorization": f"Bearer {key}"}, snapshot=False,
            )
    except Exception:  # noqa: BLE001 - discovery must never break the company's run
        return []
    if not getattr(resp, "ok", False):
        return []
    try:
        payload = json.loads(resp.body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return []
    return result_domains(payload, registered_domain)
