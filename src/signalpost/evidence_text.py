"""Make every claim verifiable from the saved result alone.

The output contract's evidence record carries `id`, `source_url`, `retrieved_at`, `content_sha256` and
`claim_span` -- the exact supporting text or value from the source. Connectors record `span` as either
quoted page text (an org number line, a description) or a locator (a JSON path, a feed note). This pass
fills `claim_span` with real text: the quoted span when it is text, otherwise the exact value read at that
locator, rendered as plain text. It also orders claims and evidence deterministically, so identical
inputs produce byte-identical factual output.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import FAMILIES, Claim, Envelope, Evidence

# Spans that locate a value rather than quote it.
_LOCATOR = re.compile(
    r"^(\$|feed item count|wp-json|regex |active_ads_indexed|Q\d+ )", re.I,
)
_PATH_VALUE = re.compile(r"^\$\.?([^=]+)=(.*)$")
_FAMILY_ORDER = {name: i for i, name in enumerate(FAMILIES)}


def _amount(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number == int(number) else f"{number:.2f}"


def value_text(claim: Claim) -> str:
    """The claim's value as the plain text a reader would find at the source."""
    v = claim.value
    field = claim.field
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _amount(v)
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if not isinstance(v, dict):
        return str(v)
    if "amount" in v:
        return f"{_amount(v['amount'])} {v.get('currency') or 'NOK'}".strip()
    if field == "official_website" or ("url" in v and "title" not in v and "platform" not in v):
        return str(v.get("url") or "")
    if "platform" in v and "url" in v:
        return str(v["url"])
    if "title" in v:
        when = v.get("published") or v.get("application_due")
        who = v.get("employer_name")
        parts = [str(v["title"])]
        if who:
            parts.append(str(who))
        if when:
            parts.append(str(when)[:10])
        return " — ".join(parts)
    if "role_label" in v or "role_code" in v:
        return f"{v.get('role_label') or v.get('role_code')}: {v.get('name') or ''}".strip()
    if {"street", "postcode", "city"} & set(v):
        place = " ".join(str(x) for x in (v.get("postcode"), v.get("city")) if x)
        lead = str(v.get("name") or "")
        return ", ".join(x for x in (lead, str(v.get("street") or ""), place) if x)
    if "code" in v and "label" in v:
        return f"{v['label']} ({v['code']})"
    if "count" in v:
        when = v.get("registered_at")
        return f"{v['count']}" + (f" (registered {when})" if when else "")
    if "verified" in v:
        return f"{v['verified']} verified active job posting(s)"
    if v.get("role") == "parent_company":
        return "virksomhet.morselskap: true"
    return "; ".join(f"{k}: {x}" for k, x in v.items() if x not in (None, "", [], {}))


def json_quote(obj: Any, keys: tuple[str, ...]) -> str | None:
    """The source JSON's own key/value pairs, verbatim: '"konkurs": false, "underAvvikling": false'."""
    if not isinstance(obj, dict):
        return None
    parts = [f'"{k}": {json.dumps(obj[k], ensure_ascii=False)}' for k in keys if k in obj]
    return ", ".join(parts) or None


def _located_value(span: str) -> str:
    """`$.a.b=value` -> `a.b: value`; any other span is already text."""
    m = _PATH_VALUE.match(span.strip())
    return f"{m.group(1)}: {m.group(2)}" if m else span.strip()


def _supporting_text(claim: Claim, ev: Evidence) -> str:
    span = (ev.span or "").strip()
    method = ev.extraction_method or ""
    if method == "web_wikidata_v1":
        return f"Wikidata item with this organisation number lists official website {value_text(claim)}"
    if method == "brreg_subunit_parent_v1" and span:
        return _located_value(span)  # the register shows the workplace's parent organisation
    if method == "web_email_domain_match_v1" and span:
        return f"registry e-mail domain {span} is the site's domain"
    if span and not _LOCATOR.match(span):
        return span
    text = value_text(claim)
    return text or span


def complete(envelope: Envelope) -> Envelope:
    """Fill `id` and `claim_span` on every evidence record and sort claims/evidence deterministically."""
    by_id = {ev.evidence_id: ev for ev in envelope.evidence}
    claims = sorted(
        envelope.claims,
        key=lambda c: (_FAMILY_ORDER.get(c.family, 99), c.field, c.value_key or "", c.claim_id),
    )
    texts: dict[str, list[str]] = {}
    for claim in claims:
        for eid in claim.evidence_ids:
            ev = by_id.get(eid)
            if ev is None or ev.claim_span:
                continue
            text = _supporting_text(claim, ev)
            if text and text not in texts.setdefault(eid, []):
                texts[eid].append(text)
    for ev in envelope.evidence:
        ev.id = ev.evidence_id
        if not ev.claim_span and texts.get(ev.evidence_id):
            # One record can back several claims (e.g. a bulk-file row): quote every supported value.
            ev.claim_span = " | ".join(texts[ev.evidence_id])[:500]
        if not ev.claim_span and ev.span:
            # A record no claim cites (e.g. a job ad checked and rejected because its employer org number
            # is another company's) still quotes the exact value it read.
            ev.claim_span = _located_value(ev.span)
    envelope.claims = claims
    envelope.evidence = sorted(envelope.evidence, key=lambda ev: ev.evidence_id)
    return envelope
