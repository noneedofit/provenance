"""Deterministic English summary synthesis (W5).

`build_summary` turns an Envelope's claims into short, sourced English sentences. Every sentence names at
least one claim_id (the `unknowns` list is the only exception, since "we don't know X" has no claim to
cite — it cites the family's FamilyState reason instead). Nothing here calls out to the network; the
summary is a pure function of the envelope.

Field/family names are not yet finalised by the registry/web/activity connectors, so every lookup goes
through FIELD_ALIASES / ROLE_CODE_ALIASES below — update these two maps, not the sentence logic, once the
real connectors land.
"""
from __future__ import annotations

from typing import Any

from .models import Claim, Envelope, Summary, SummarySentence

# --- Assumed field vocabulary, centralised for easy realignment ---------------------------------------

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "legal_name": ("legal_name", "name"),
    "legal_form": ("legal_form", "organisation_form"),
    "founded_date": ("founded_date", "registration_date", "stiftelsesdato"),
    "status": ("status", "registration_status"),
    "nace_label": ("nace_label", "nace", "nace_description", "industry_label"),
    "statutory_purpose": ("statutory_purpose", "vedtektsfestet_formaal", "purpose"),
    "municipality": ("municipality", "business_municipality", "business_address"),
    "employees": ("employees", "registered_employees"),
    "revenue": ("revenue",),
    "operating_result": ("operating_result",),
    "annual_result": ("annual_result", "net_result"),
    "role": ("role",),
    "official_website": ("official_website", "website_url", "website"),
    "profile": ("profile", "profile_url"),
    "company_description": ("company_description", "description"),
    "location": ("location", "workplace"),
    "job_posting": ("job_posting", "job"),
    "activity_item": ("activity_item", "activity"),
}

# Brønnøysund role codes (data.brreg.no /roller): DAGL = daglig leder (CEO), LEDE = styreleder (chair).
CEO_ROLE_CODES: tuple[str, ...] = ("DAGL", "CEO")
CHAIR_ROLE_CODES: tuple[str, ...] = ("LEDE", "CHAIR")

MONEY_FIELDS: tuple[str, ...] = ("revenue", "operating_result", "annual_result")

FAMILY_UNKNOWN_LABEL: dict[str, str] = {
    "identity": "its legal identity",
    "financials": "its latest financial figures",
    "financial_history": "its financial history",
    "leadership": "its leadership",
    "locations": "its registered workplaces",
    "group": "its group / ownership structure",
    "website": "an official website",
    "profiles": "public social or video profiles",
    "description": "a description of what it does",
    "jobs": "open job postings",
    "activity": "public activity (news, posts, videos)",
    "reviews": "public reviews or ratings",
}


# --- Helpers -------------------------------------------------------------------------------------------

def _current_claims(envelope: Envelope, family: str, field_key: str | None = None) -> list[Claim]:
    aliases = FIELD_ALIASES.get(field_key, (field_key,)) if field_key else None
    out = []
    for claim in envelope.claims:
        if claim.family != family or claim.status != "current" or claim.availability != "available":
            continue
        if aliases is not None and claim.field not in aliases:
            continue
        out.append(claim)
    return out


def _single(envelope: Envelope, family: str, field_key: str) -> Claim | None:
    claims = _current_claims(envelope, family, field_key)
    return claims[0] if claims else None


def _display(value: Any) -> str:
    """Human-readable text for registry values that arrive as structured dicts."""
    if isinstance(value, dict):
        for key in ("label", "count", "name", "municipality", "city", "url", "title"):
            if value.get(key) not in (None, ""):
                return str(value[key])
        return ", ".join(str(v) for v in value.values() if v not in (None, ""))
    if isinstance(value, list):
        return ", ".join(_display(v) for v in value)
    return str(value)


LEGAL_FORMS_EN = {
    "AS": "private limited company", "ASA": "public limited company", "ENK": "sole proprietorship",
    "NUF": "Norwegian branch of a foreign company", "ANS": "general partnership", "DA": "partnership with shared liability",
    "SA": "cooperative", "BRL": "housing cooperative", "STI": "foundation", "FLI": "association",
    "KS": "limited partnership", "IKS": "inter-municipal company", "KF": "municipal enterprise",
    "SF": "state enterprise", "BA": "limited-liability company", "ESEK": "owner-section association",
    "SAM": "jointly owned property", "PK": "pension fund", "KIRK": "church body", "ORGL": "organisational unit",
}


def _article(phrase: str) -> str:
    return ("an " if phrase[:1].lower() in "aeiou" else "a ") + phrase


def _count(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _join(parts: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _clip(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _scalar(value: Any) -> Any:
    if isinstance(value, dict) and "amount" in value:
        return value["amount"]
    return value


def format_money(value: Any, currency: str = "NOK") -> str:
    amount = _scalar(value)
    if isinstance(value, dict):
        currency = value.get("currency", currency)
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if amount < 0 else ""
    magnitude = abs(amount)
    if magnitude >= 1_000_000_000:
        body = f"{magnitude / 1_000_000_000:,.2f}B"
    elif magnitude >= 1_000_000:
        body = f"{magnitude / 1_000_000:,.2f}M"
    else:
        body = f"{magnitude:,.0f}"
    return f"{sign}{currency} {body}"


def _role_person(claim: Claim) -> str | None:
    value = claim.value
    if isinstance(value, dict):
        name = value.get("name") or value.get("person_name")
        if name:
            return str(name)
    if isinstance(value, str):
        return value
    if claim.value_key and "|" in claim.value_key:
        return claim.value_key.split("|", 1)[1]
    return None


def _role_code(claim: Claim) -> str | None:
    value = claim.value
    if isinstance(value, dict) and value.get("role_code"):
        return str(value["role_code"])
    if claim.value_key and "|" in claim.value_key:
        return claim.value_key.split("|", 1)[0]
    return None


def _find_role(envelope: Envelope, codes: tuple[str, ...]) -> Claim | None:
    for claim in _current_claims(envelope, "leadership", "role"):
        if _role_code(claim) in codes:
            return claim
    return None


def _period_label(claim: Claim) -> str | None:
    if claim.reporting_period and claim.reporting_period.end:
        return claim.reporting_period.end[:4]
    return claim.effective_date[:4] if claim.effective_date else None


def _pct_change(previous: float, current: float) -> float | None:
    if previous == 0:
        return None
    return (current - previous) / abs(previous) * 100.0


def _financial_history_claims(envelope: Envelope, field_key: str) -> list[Claim]:
    return sorted(
        _current_claims(envelope, "financial_history", field_key),
        key=lambda c: (c.reporting_period.end if c.reporting_period else "") or "",
    )


# --- Sentence builders -----------------------------------------------------------------------------

def _sentence_what_it_does(envelope: Envelope) -> SummarySentence | None:
    desc = _single(envelope, "description", "company_description")
    if desc is not None and isinstance(desc.value, str) and desc.value.strip():
        text = desc.value.strip()
        quote = text if len(text) <= 200 else text[:197].rstrip() + "..."
        return SummarySentence(
            text=f'{envelope.legal_name or "The company"} describes itself on its website as: "{quote.rstrip(".")}".',
            claim_ids=[desc.claim_id],
        )
    nace = _single(envelope, "identity", "nace_label")
    purpose = _single(envelope, "identity", "statutory_purpose")
    if nace is not None or purpose is not None:
        parts = []
        claim_ids = []
        name = envelope.legal_name or "The company"
        if nace is not None:
            parts.append(f"is registered in the industry \"{_display(nace.value)}\"")
            claim_ids.append(nace.claim_id)
        if purpose is not None:
            parts.append(f"its statutory purpose reads \"{_clip(purpose.value)}\"")
            claim_ids.append(purpose.claim_id)
        if nace is not None:
            text = f"{name} " + "; ".join(parts) + "."
        else:
            text = f"According to the register, {parts[0].replace('its statutory purpose reads', 'the statutory purpose of ' + name + ' reads')}."
        return SummarySentence(text=text, claim_ids=claim_ids)
    return None


def _sentence_legal_form(envelope: Envelope) -> SummarySentence | None:
    form = _single(envelope, "identity", "legal_form")
    founded = _single(envelope, "identity", "founded_date")
    municipality = _single(envelope, "identity", "municipality")
    if form is None and founded is None and municipality is None:
        return None
    name = envelope.legal_name or "The company"
    parts = []
    claim_ids = []
    if form is not None:
        code = form.value.get("code") if isinstance(form.value, dict) else None
        label = LEGAL_FORMS_EN.get(str(code or "").upper()) or _display(form.value)
        parts.append(f"is {_article(label)} ({code})" if code else f"is {_article(label)}")
        claim_ids.append(form.claim_id)
    if founded is not None:
        parts.append(f"founded on {_display(founded.value)}")
        claim_ids.append(founded.claim_id)
    if municipality is not None:
        parts.append(f"registered in {_display(municipality.value).title()}")
        claim_ids.append(municipality.claim_id)
    return SummarySentence(text=f"{name} " + ", ".join(parts) + ".", claim_ids=claim_ids)


def _sentence_size(envelope: Envelope) -> SummarySentence | None:
    employees = _single(envelope, "identity", "employees")
    revenue = _single(envelope, "financials", "revenue")
    op_result = _single(envelope, "financials", "operating_result")
    if employees is None and revenue is None and op_result is None:
        return None
    parts = []
    claim_ids = []
    if employees is not None:
        count = _display(employees.value)
        parts.append(f"has {count} registered employee{'' if count == '1' else 's'}")
        claim_ids.append(employees.claim_id)
    if revenue is not None:
        period = _period_label(revenue) or "its latest filed year"
        parts.append(f"reported revenue of {format_money(revenue.value)} for {period}")
        claim_ids.append(revenue.claim_id)
    if op_result is not None:
        period = _period_label(op_result) or "the same year"
        try:
            loss = float(_scalar(op_result.value)) < 0
        except (TypeError, ValueError):
            loss = False
        if loss:
            parts.append(f"an operating loss of {format_money(abs(float(_scalar(op_result.value))), op_result.value.get('currency', 'NOK') if isinstance(op_result.value, dict) else 'NOK')} ({period})")
        else:
            parts.append(f"an operating result of {format_money(op_result.value)} ({period})")
        claim_ids.append(op_result.claim_id)
    text = "It " + _join(parts) + "."

    trend = _sentence_revenue_trend(envelope, revenue)
    if trend is not None:
        text = text + " " + trend.text
        claim_ids = claim_ids + trend.claim_ids
    return SummarySentence(text=text, claim_ids=claim_ids)


def _sentence_revenue_trend(envelope: Envelope, latest_revenue: Claim | None) -> SummarySentence | None:
    if latest_revenue is None:
        return None
    history = _financial_history_claims(envelope, "revenue")
    if not history:
        return None
    previous = history[-1]
    try:
        prev_amount = float(_scalar(previous.value))
        curr_amount = float(_scalar(latest_revenue.value))
    except (TypeError, ValueError):
        return None
    pct = _pct_change(prev_amount, curr_amount)
    if pct is None:
        return None
    direction = "up" if pct >= 0 else "down"
    prev_period = _period_label(previous) or "the previous filed year"
    return SummarySentence(
        text=f"Revenue is {direction} {abs(pct):.1f}% versus {prev_period}.",
        claim_ids=[latest_revenue.claim_id, previous.claim_id],
    )


def _sentence_leadership(envelope: Envelope) -> SummarySentence | None:
    ceo = _find_role(envelope, CEO_ROLE_CODES)
    chair = _find_role(envelope, CHAIR_ROLE_CODES)
    if ceo is None and chair is None:
        return None
    parts = []
    claim_ids = []
    name = envelope.legal_name or "The company"
    if ceo is not None:
        person = _role_person(ceo)
        if person:
            parts.append(f"CEO {person}")
            claim_ids.append(ceo.claim_id)
    if chair is not None:
        person = _role_person(chair)
        if person:
            parts.append(f"board chair {person}")
            claim_ids.append(chair.claim_id)
    if not parts:
        return None
    return SummarySentence(text=f"{name} is led by " + " and ".join(parts) + ".", claim_ids=claim_ids)


def _sentence_footprint(envelope: Envelope) -> SummarySentence | None:
    website = _single(envelope, "website", "official_website")
    profiles = _current_claims(envelope, "profiles", "profile")
    locations = _current_claims(envelope, "locations", "location")
    if website is None and not profiles and not locations:
        return None
    parts = []
    claim_ids = []
    if website is not None:
        url = website.value.get("url") if isinstance(website.value, dict) else website.value
        parts.append(f"a verified website ({url})")
        claim_ids.append(website.claim_id)
    if profiles:
        parts.append(_count(len(profiles), "linked public profile"))
        claim_ids.extend(c.claim_id for c in profiles)
    if locations:
        parts.append(_count(len(locations), "registered workplace"))
        claim_ids.extend(c.claim_id for c in locations)
    name = envelope.legal_name or "The company"
    return SummarySentence(text=f"{name} has " + _join(parts) + ".", claim_ids=claim_ids)


def _sentence_hiring(envelope: Envelope) -> SummarySentence | None:
    jobs = _current_claims(envelope, "jobs", "job_posting")
    activity = sorted(
        _current_claims(envelope, "activity", "activity_item"),
        key=lambda c: (c.effective_date or ""),
        reverse=True,
    )
    if not jobs and not activity:
        return None
    parts = []
    claim_ids = []
    if jobs:
        titles = [c.value.get("title") for c in jobs if isinstance(c.value, dict) and c.value.get("title")]
        if titles:
            parts.append(f"{_count(len(jobs), 'active job posting')} on NAV, most recently \"{titles[0]}\"")
        else:
            parts.append(f"{_count(len(jobs), 'active job posting')} on NAV")
        claim_ids.extend(c.claim_id for c in jobs)
    if activity:
        latest = activity[0]
        title = latest.value.get("title") if isinstance(latest.value, dict) else latest.value
        when = latest.effective_date or "an unspecified date"
        if title:
            parts.append(f"recent public activity, most recently \"{title}\" ({when})")
        claim_ids.append(latest.claim_id)
    name = envelope.legal_name or "The company"
    return SummarySentence(text=f"{name} has " + _join(parts) + ".", claim_ids=claim_ids)


def _sentence_change(envelope: Envelope, change) -> SummarySentence:
    run_id = envelope.run.run_id
    label = change.change_type.replace("_", " ")
    if change.previous_value is not None and change.current_value is not None:
        text = (
            f"Since the previous check (run {run_id}), the {change.field} changed from "
            f"{change.previous_value!r} to {change.current_value!r}."
        )
    elif change.current_value is not None:
        text = f"Since the previous check (run {run_id}), a new {label} was found: {change.current_value!r}."
    else:
        text = f"Since the previous check (run {run_id}), {label.replace('changed ', '')} for {change.field}."
    return SummarySentence(text=text, claim_ids=[change.claim_id])


# --- Public API -----------------------------------------------------------------------------------

def build_summary(envelope: Envelope) -> Summary:
    sentences: list[SummarySentence] = []

    what_it_does = _sentence_what_it_does(envelope)
    legal = _sentence_legal_form(envelope)
    size = _sentence_size(envelope)
    leadership = _sentence_leadership(envelope)
    footprint = _sentence_footprint(envelope)
    hiring = _sentence_hiring(envelope)

    for sentence in (what_it_does, legal, size, leadership, footprint, hiring):
        if sentence is not None:
            sentences.append(sentence)

    for change in envelope.changes:
        sentences.append(_sentence_change(envelope, change))

    if not sentences:
        # Sparse shell: fall back to whatever identity facts exist.
        name = envelope.legal_name or f"Organisation {envelope.organisation_number}"
        identity_claims = _current_claims(envelope, "identity")
        if identity_claims:
            form = _single(envelope, "identity", "legal_form")
            municipality = _single(envelope, "identity", "municipality")
            bits = []
            ids = []
            if form is not None:
                bits.append(f"a {form.value}")
                ids.append(form.claim_id)
            location_bit = f" registered in {municipality.value}" if municipality is not None else ""
            if municipality is not None:
                ids.append(municipality.claim_id)
            sentences.append(SummarySentence(
                text=(
                    f"{name} is {' '.join(bits) or 'a registered organisation'}{location_bit}; "
                    "no website, jobs, or public activity were found in permitted sources."
                ),
                claim_ids=ids or [c.claim_id for c in identity_claims[:1]],
            ))

    unknowns = []
    for family, state in sorted(envelope.families.items()):
        if state.availability in ("available",):
            continue
        label = FAMILY_UNKNOWN_LABEL.get(family, family)
        reason = state.reason or state.availability
        unknowns.append(f"{label.capitalize()}: {reason}")

    return Summary(language="en", sentences=sentences, unknowns=unknowns, method="template_v1")


def summary_markdown(envelope: Envelope) -> str:
    summary = envelope.summary if envelope.summary.sentences or envelope.summary.unknowns else build_summary(envelope)
    lines = [f"# {envelope.legal_name or envelope.organisation_number}", ""]
    for sentence in summary.sentences:
        cites = " ".join(f"[{cid}]" for cid in sentence.claim_ids)
        lines.append(f"- {sentence.text} {cites}".rstrip())
    if summary.unknowns:
        lines.append("")
        lines.append("## Unknown / not available")
        for item in summary.unknowns:
            lines.append(f"- {item}")
    return "\n".join(lines) + "\n"
