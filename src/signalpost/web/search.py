"""Search-provider candidate discovery (Brave Search API), used only when a key is configured.

Builderr's source policy allows a search API to *find* candidates, never to support a claim: every domain a
search result nominates is a plain, non-decisive website candidate that must pass the same live
verification (org number on the site, or registry address/phone/e-mail/role names) as a name guess before
anything is published. Search output is transient: titles, snippets and ranks are discarded once the
candidate domains are taken; nothing from the provider becomes evidence.

Configuration (all optional; without a key this module does nothing and makes no request):
- `BRAVE_SEARCH_API_KEY` (or `BRAVE_API_KEY`): the subscription token, sent as a header, never logged.
- `SIGNALPOST_SEARCH_RPS`: request rate limit across all workers (default 1 per second).
- `SIGNALPOST_SEARCH_MAX`: maximum searches in one run (default: one per company in the batch).
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
_STATE = {"last": 0.0, "used": 0}


def api_key() -> str | None:
    key = (os.environ.get("BRAVE_SEARCH_API_KEY") or os.environ.get("BRAVE_API_KEY") or "").strip()
    return key or None


def enabled() -> bool:
    return api_key() is not None


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


def build_query(name: str, municipality: str | None) -> str:
    """Legal name without the legal-form suffix, quoted, plus the registered municipality."""
    core = re.sub(r"\s+(AS|ASA|SA|DA|ANS|ENK|NUF|BA|SF|KS|IKS|STI|FLI|BRL|AB|A/S)$", "", name.strip(), flags=re.I)
    query = f'"{core}"'
    if municipality:
        query += f" {municipality.title()}"
    return query


def result_domains(payload: Any, registered_domain) -> list[tuple[str, str]]:
    """(domain, homepage url) for each distinct result host that could be the company's own site."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    results = ((payload or {}).get("web") or {}).get("results") or [] if isinstance(payload, dict) else []
    for item in results[:MAX_RESULTS]:
        url = str(item.get("url") or "") if isinstance(item, dict) else ""
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
    key = api_key()
    if not key or client is None or not name or not _rate_wait():
        return []
    params = {"q": build_query(name, municipality), "country": "NO", "search_lang": "nb", "count": str(MAX_RESULTS)}
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    try:
        resp = client.get(
            url, org=org, purpose="search_candidates", accept="application/json", respect_robots=False,
            max_redirects=0, snapshot=False, timeout=10.0,
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
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
