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


_PATH_STEP = re.compile(r"\.([^.\[]+)|\[([^\]]+)\]")
_DECODER = json.JSONDecoder()
_WS = re.compile(r"\s*")
RAW_QUOTE_MAX = 1500


def _object_members(text: str, i: int):
    """(key, key_start, value_start, value_end) for each member of the JSON object starting at text[i]."""
    i = _WS.match(text, i + 1).end()
    if text[i] == "}":
        return
    while True:
        key, key_end = _DECODER.raw_decode(text, i)
        value_start = _WS.match(text, _WS.match(text, key_end).end() + 1).end()
        _, value_end = _DECODER.raw_decode(text, value_start)
        yield key, i, value_start, value_end
        i = _WS.match(text, value_end).end()
        if text[i] != ",":
            return
        i = _WS.match(text, i + 1).end()


def _array_items(text: str, i: int):
    """(value_start, value_end) for each item of the JSON array starting at text[i]."""
    i = _WS.match(text, i + 1).end()
    if text[i] == "]":
        return
    while True:
        _, end = _DECODER.raw_decode(text, i)
        yield i, end
        i = _WS.match(text, end).end()
        if text[i] != ",":
            return
        i = _WS.match(text, i + 1).end()


def _matches(item: Any, condition: str) -> bool:
    path, _, wanted = condition.partition("=")
    for key in path.split("."):
        item = item.get(key) if isinstance(item, dict) else None
    return item is not None and str(item) == wanted


def _excerpt(text: str, path: str) -> str | None:
    start = _WS.match(text).end()
    value_start, value_end = start, _DECODER.raw_decode(text, start)[1]
    quote_start = value_start
    rest = path.strip()[1:]
    for m in _PATH_STEP.finditer(rest):
        key, condition = m.group(1), m.group(2)
        if key is not None:
            if text[value_start] != "{":
                return None
            member = next((x for x in _object_members(text, value_start) if x[0] == key), None)
            if member is None:
                return None
            _, quote_start, value_start, value_end = member
        else:
            if text[value_start] != "[":
                return None
            items = list(_array_items(text, value_start))
            if condition.isdigit():
                found = items[int(condition)] if int(condition) < len(items) else None
            else:
                found = next((x for x in items if _matches(json.loads(text[x[0]:x[1]]), condition)), None)
            if found is None:
                return None
            quote_start = value_start = found[0]
            value_end = found[1]
    return text[quote_start:value_end]


def raw_json_quote(body: bytes | None, path: str | None) -> str | None:
    """The exact text a JSON path points at in the raw response, e.g. '"sumDriftsinntekter":35247070'.

    A member is quoted with its key; an array item as the whole item. Copied byte for byte from what the
    source returned (not re-serialised), so a reader or checker finds the quote verbatim in the source.
    A person's birth date is never quoted: a role holder's quote narrows to their name. Over-long quotes are
    cut to a prefix, which is still verbatim. Returns None when the path cannot be resolved.
    """
    if not body or not path or not path.startswith("$") or "/" in path:
        return None
    try:
        text = body.decode("utf-8")
        quote = _excerpt(text, path)
        if quote is not None and '"fodselsdato"' in quote:
            quote = _excerpt(text, path + ".person.navn")
    except (ValueError, IndexError, UnicodeDecodeError):
        return None
    if quote is None or '"fodselsdato"' in quote:
        return None
    return quote[:RAW_QUOTE_MAX]


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
