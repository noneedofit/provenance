"""Reviews: Mattilsynet's official food-hygiene inspection result ("smilefjes") per food-service location.

Source: https://smilefjes.mattilsynet.no (Norwegian Food Safety Authority; public, no robots.txt). The
site's own search index (`/search/index/nb.json`, one download per run) lists every inspected place with
name, address, postcode, page path and its inspection history. A place is a candidate for a company when
its postcode equals the company's or one of its subunits' postcode and its name shares a distinctive name
token. The candidate's page is then fetched live and published only when the page's "Orgnr." equals the
company's organisation number or one of its subunits' -- the page itself is the evidence.

Published as `reviews/inspection_rating`: a public rating by the authority, dated, with the page quote.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import threading
import unicodedata
from typing import Any

from ._common import make_claim, make_evidence

BASE_URL = "https://smilefjes.mattilsynet.no"
INDEX_URL = BASE_URL + "/search/index/nb.json"
MAX_PAGES_PER_COMPANY = 3
GRADE_TEXT = {
    "0": "smiling face (no breaches found)",
    "1": "smiling face (minor breaches, no follow-up needed)",
    "2": "straight mouth (breaches that require follow-up)",
    "3": "sad mouth (serious breaches)",
}
_STOP = {"as", "asa", "da", "ans", "sa", "enk", "nuf", "avd", "og", "the", "i", "pa", "cafe", "kafe", "restaurant",
         "pizza", "sushi", "bar", "kro", "grill", "bakeri", "kafeteria", "kantine"}
_FOLD = str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"})

_lock = threading.Lock()
_state: dict[str, Any] = {"loaded": False, "by_postcode": {}, "error": None, "evidence": None}


def _tokens(name: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", (name or "").translate(_FOLD)).encode("ascii", "ignore").decode().casefold()
    return {t for t in re.findall(r"[a-z0-9]+", folded) if len(t) >= 3 and t not in _STOP}


def _load_index(client: Any) -> None:
    """Download the site's place index once per run (thread-safe). Failure leaves an error state."""
    with _lock:
        if _state["loaded"]:
            return
        _state["loaded"] = True
        resp = client.get(INDEX_URL, org=None, purpose="smilefjes_index", accept="application/json", respect_robots=True)
        if not resp.ok:
            _state["error"] = resp.error or f"http_{resp.status}"
            return
        try:
            lookup = json.loads(resp.text()).get("lookup") or []
        except Exception as exc:  # defensive: external payload
            _state["error"] = f"parse_error:{type(exc).__name__}"
            return
        by_postcode: dict[str, list[list]] = {}
        for row in lookup:
            if isinstance(row, list) and len(row) >= 7 and row[4]:
                by_postcode.setdefault(str(row[4]), []).append(row)
        _state["by_postcode"] = by_postcode


def reset_for_tests() -> None:
    with _lock:
        _state.update({"loaded": False, "by_postcode": {}, "error": None, "evidence": None})


def _places_for(ctx: Any) -> list[tuple[str, list[str]]]:
    """(postcode, name) pairs for the company and its subunits."""
    out: list[tuple[str, list[str]]] = []
    facts = (ctx.shared or {}).get("registry_facts") or {}
    bulk = ctx.bulk or {}
    names = [facts.get("name") or bulk.get("navn") or ""] + list(facts.get("aliases") or [])
    postcode = facts.get("postcode") or bulk.get("forretningsadresse.postnummer")
    if postcode:
        out.append((str(postcode), names))
    for sub in (ctx.registry or {}).get("subunits") or []:
        if sub.get("postcode"):
            out.append((str(sub["postcode"]), [sub.get("name") or ""] + names[:1]))
    return out


def candidates(ctx: Any) -> list[list]:
    found: dict[str, list] = {}
    for postcode, names in _places_for(ctx):
        wanted = set().union(*(_tokens(n) for n in names if n)) if names else set()
        if not wanted:
            continue
        for row in _state["by_postcode"].get(postcode, []):
            if _tokens(row[1]) & wanted:
                found.setdefault(row[0], row)
    return [found[k] for k in sorted(found)]


_ORGNR_RE = re.compile(r"Orgnr\.?\s*(\d{3}\s?\d{3}\s?\d{3})")
_LATEST_RE = re.compile(r"Siste tilsynsresultat:\s*</h2>\s*<div[^>]*title=\"([^\"]+)\"", re.S)
_LATEST_DATE_RE = re.compile(r"Siste tilsynsresultat:\s*(\d{2}\.\d{2}\.\d{4})")


def _visible_text(page_html: str) -> str:
    text = re.sub(r"(?is)<(script|style|svg)[^>]*>.*?</\1>", " ", page_html)
    return html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)))


def collect(ctx: Any) -> dict:
    """Returns {claims, evidence, checked, error}."""
    result: dict = {"claims": [], "evidence": [], "checked": False, "error": None}
    client = ctx.client
    if client is None:
        return result
    _load_index(client)
    if _state["error"]:
        result["error"] = _state["error"]
        return result
    result["checked"] = True
    our_orgs = {str(ctx.org)} | {str(s.get("organisation_number")) for s in (ctx.registry or {}).get("subunits") or [] if s.get("organisation_number")}
    for row in candidates(ctx)[:MAX_PAGES_PER_COMPANY]:
        if client.remaining(ctx.org) < 1:
            break
        url = BASE_URL + row[0]
        resp = client.get(url, org=ctx.org, purpose="smilefjes_place", accept="text/html", respect_robots=True)
        if not resp.ok:
            continue
        page = resp.text()
        text = _visible_text(page)
        m = _ORGNR_RE.search(text)
        if not m:
            continue
        page_org = re.sub(r"\D", "", m.group(1))
        if page_org not in our_orgs:
            continue  # same name and postcode, different organisation: never published
        latest_title = _LATEST_RE.search(page)
        latest_date = _LATEST_DATE_RE.search(text)
        history = row[6] if isinstance(row[6], list) else []
        grade = str(history[0][0]) if history and history[0] else None
        inspected = str(history[0][1]) if history and len(history[0]) > 1 else None
        if grade not in GRADE_TEXT or not inspected:
            continue
        quote_parts = [f"{row[1]} Orgnr. {page_org}"]
        if latest_date:
            quote_parts.append(f"Siste tilsynsresultat: {latest_date.group(1)}")
        if latest_title:
            quote_parts.append(html_mod.unescape(latest_title.group(1)))
        ev = make_evidence(
            source_url=resp.final_url or url, source_class="official_inspection", retrieved_at=resp.retrieved_at,
            extraction_method="mattilsynet_smilefjes_v1", final_url=resp.final_url, redirect_chain=resp.redirect_chain,
            http_status=resp.status, content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
            span=" — ".join(quote_parts), access_policy="Public authority data (Mattilsynet)",
        )
        result["evidence"].append(ev)
        result["claims"].append(make_claim(
            org=ctx.org, family="reviews", field="inspection_rating",
            value={
                "rater": "Mattilsynet (Norwegian Food Safety Authority)",
                "scheme": "smilefjes food-hygiene inspection",
                "place": row[1], "place_org_number": page_org, "grade_code": int(grade),
                "grade": GRADE_TEXT[grade], "inspected_on": inspected, "url": resp.final_url or url,
            },
            value_key=f"smilefjes:{row[0]}", availability="available",
            identity_basis="org_number_on_source", effective_date=inspected, evidence_ids=[ev.evidence_id],
        ))
    return result
