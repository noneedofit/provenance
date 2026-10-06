"""HTML rendering for the Signalpost static viewer.

Every function here returns a plain `str` of HTML. No file I/O — that lives in build.py. Works purely off
plain dicts (Envelope.model_dump()) so it never imports pydantic model classes directly.
"""
from __future__ import annotations

import json
from typing import Any

from .helpers import (
    AVAILABILITY_CLASS, AVAILABILITY_LABELS, FAMILY_LABELS, SECTION_LABELS, COVERAGE_FAMILIES,
    esc, fmt_date, fmt_money, fmt_value, display_value, label_from_field, org_registry_api_url,
    org_registry_search_url, coverage_score, employee_band, claims_by_family, evidence_by_id,
    field_value, first_available, plain_scalar, short_hash,
)

SITE_TITLE = "Signalpost"


def data_json_attr(data: dict) -> str:
    """A compact JSON blob safe to embed as an HTML attribute value (used inside data-json="...")."""
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return esc(raw)


# --------------------------------------------------------------------------------------- page shell


def page_shell(*, title: str, description: str, body: str, asset_prefix: str = "", nav_active: str = "") -> str:
    def nav_link(href: str, label: str, key: str) -> str:
        cls = ' aria-current="page"' if key == nav_active else ""
        return f'<a href="{esc(href)}"{cls}>{esc(label)}</a>'

    nav = (
        nav_link(f"{asset_prefix}index.html", "Directory", "directory")
        + nav_link(f"{asset_prefix}compare.html", "Compare", "compare")
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · {SITE_TITLE}</title>
<meta name="description" content="{esc(description)}">
<link rel="stylesheet" href="{asset_prefix}assets/style.css">
</head>
<body>
<header class="site-header">
  <div class="container">
    <a class="brand" href="{asset_prefix}index.html">Signal<span>post</span></a>
    <nav class="site-nav" aria-label="Primary">{nav}</nav>
  </div>
</header>
<main class="container">
{body}
</main>
<footer class="site-footer">
  <div class="container">Static, evidence-bound company research. Built entirely from checked public sources — no fact is shown without a source.</div>
</footer>
<script src="{asset_prefix}assets/app.js"></script>
</body>
</html>
"""


# --------------------------------------------------------------------------------------- directory row derivation


def build_row(envelope: dict, page_url: str) -> dict:
    """Compact per-company summary used for directory cards, compare table and CSV export."""
    org = envelope["organisation_number"]
    claims = envelope.get("claims", [])
    families = envelope.get("families", {})
    by_family = claims_by_family(claims)

    legal_name = envelope.get("legal_name") or org
    identity = by_family.get("identity", [])
    status = _val(field_value(identity, "identity", "status"))

    # Field shapes vary by connector version: real registry claims carry structured dicts
    # ({"code":.., "label":..} for legal_form/nace, {"count":..} for employees, no bare "municipality"
    # field at all — it lives inside business_address/postal_address). plain_scalar() pulls a filter- and
    # CSV-friendly plain value out of either shape; older/synthetic claims that are already plain strings
    # pass through unchanged.
    legal_form = plain_scalar(_val(first_available(identity, "identity", "legal_form")), prefer=("code", "label"))

    nace_value = _val(first_available(identity, "identity", "nace"))
    if isinstance(nace_value, dict):
        nace_code = nace_value.get("code")
        nace_label = nace_value.get("label")
    else:
        nace_code = _val(field_value(identity, "identity", "nace_code"))
        nace_label = _val(field_value(identity, "identity", "nace_description"))

    municipality = _val(field_value(identity, "identity", "municipality"))
    if not municipality:
        addr = _val(first_available(identity, "identity", "business_address", "postal_address"))
        if isinstance(addr, dict):
            municipality = addr.get("municipality") or addr.get("city")

    employees_value = _val(first_available(identity, "identity", "registered_employees", "employees_registered"))
    employees = employees_value.get("count") if isinstance(employees_value, dict) else employees_value

    fin = by_family.get("financials", [])
    revenue_claim = field_value(fin, "financials", "revenue")
    revenue = None
    revenue_currency = "NOK"
    if revenue_claim and isinstance(revenue_claim.get("value"), dict):
        revenue = revenue_claim["value"].get("amount")
        revenue_currency = revenue_claim["value"].get("currency", "NOK")
    result_claim = field_value(fin, "financials", "annual_result")
    result = result_claim["value"].get("amount") if result_claim and isinstance(result_claim.get("value"), dict) else None

    website_claims = [c for c in by_family.get("website", []) if c.get("status", "current") == "current"]
    website_state = "none"
    website_url = None
    if website_claims:
        exact = next((c for c in website_claims if c.get("relationship") == "exact" and c.get("availability") == "available"), None)
        chosen = exact or website_claims[0]
        website_url = chosen.get("value")
        website_state = "exact" if chosen.get("relationship") == "exact" and chosen.get("availability") == "available" else "ambiguous"

    profiles_count = len([c for c in by_family.get("profiles", []) if c.get("availability") == "available"])
    jobs = [c for c in by_family.get("jobs", []) if c.get("availability") == "available"]
    activity = [c for c in by_family.get("activity", []) if c.get("availability") == "available"]
    activity_sorted = sorted(activity, key=lambda c: c.get("effective_date") or "", reverse=True)
    latest_activity = activity_sorted[0] if activity_sorted else None

    return {
        "org": org,
        "legal_name": legal_name,
        "brand_name": _val(field_value(identity, "identity", "brand_name")),
        "municipality": municipality,
        "status": status,
        "legal_form": legal_form,
        "nace_code": nace_code,
        "nace_label": nace_label,
        "employees": employees,
        "employee_band": employee_band(employees),
        "revenue": revenue,
        "revenue_currency": revenue_currency,
        "result": result,
        "website": website_url,
        "website_state": website_state,
        "profiles_count": profiles_count,
        "jobs_count": len(jobs),
        "jobs_hiring": len(jobs) > 0,
        "activity_count": len(activity),
        "latest_activity_text": latest_activity.get("value") if latest_activity else None,
        "latest_activity_date": latest_activity.get("effective_date") if latest_activity else None,
        "coverage": coverage_score(families),
        "families": {f: families.get(f, {}).get("availability", "not_available") for f in COVERAGE_FAMILIES},
        "page_url": page_url,
    }


def _val(claim: dict | None) -> Any:
    return claim.get("value") if claim else None


# --------------------------------------------------------------------------------------- directory page


def render_directory_card(row: dict) -> str:
    badges = "".join(
        f'<span class="badge {AVAILABILITY_CLASS[state]}" title="{esc(FAMILY_LABELS[fam])}: {esc(AVAILABILITY_LABELS[state])}">{esc(FAMILY_LABELS[fam])}</span>'
        for fam, state in row["families"].items()
        if state == "available"
    )
    meta_bits = [b for b in [row["legal_form"], row["municipality"], row["nace_label"]] if b]
    return f"""<div class="card" data-json="{data_json_attr(row)}">
  <label class="compare-pick"><input type="checkbox" aria-label="Select {esc(row['legal_name'])} for comparison"> Compare</label>
  <h3><a href="{esc(row['page_url'])}">{esc(row['legal_name'])}</a></h3>
  <div class="meta">{esc(' · '.join(meta_bits))}</div>
  <div class="meta small">Org {esc(row['org'])} · {row['coverage']}/12 families available</div>
  <div class="badges">{badges}</div>
</div>
"""


def render_index_page(rows: list[dict]) -> str:
    cards = "\n".join(render_directory_card(r) for r in rows)
    legal_forms = sorted({r["legal_form"] for r in rows if r["legal_form"]})
    legal_form_opts = "".join(f'<option value="{esc(v)}">{esc(v)}</option>' for v in legal_forms)
    band_opts = "".join(
        f'<option value="{esc(v)}">{esc(v)}</option>' for v in ("0", "1-4", "5-19", "20-49", "50-249", "250+")
    )
    total = len(rows)
    all_five = len([r for r in rows if r["coverage"] >= 5])
    body = f"""
<h1>Company directory</h1>
<p class="muted">{total} companies checked against official Norwegian registers and public sources. {all_five} of {total} have data across at least five families.</p>

<div class="toolbar" role="search">
  <input type="search" id="q" placeholder="Search name, org number, municipality, industry…" aria-label="Search companies">
  <select id="f-legalform" aria-label="Filter by legal form"><option value="">All legal forms</option>{legal_form_opts}</select>
  <select id="f-employeeband" aria-label="Filter by employee band"><option value="">All employee sizes</option>{band_opts}</select>
  <label><input type="checkbox" id="f-website"> Has verified website</label>
  <label><input type="checkbox" id="f-hiring"> Hiring now</label>
  <label><input type="checkbox" id="f-activity"> Has recent activity</label>
  <label><input type="checkbox" id="f-all-areas"> Data in all 5 areas</label>
  <select id="sort" aria-label="Sort companies">
    <option value="coverage">Sort: most data found</option>
    <option value="name">Sort: name (A–Z)</option>
    <option value="revenue">Sort: revenue</option>
    <option value="employees">Sort: employees</option>
  </select>
</div>
<div class="export-row">
  <button type="button" class="btn" id="export-visible-csv">Download visible rows as CSV</button>
  <a class="btn" href="directory.csv" download>Download full directory CSV</a>
</div>
<p class="stat-line" id="stat-line" aria-live="polite">Showing {total} of {total} companies</p>

<div id="compare-bar" class="toolbar" style="display:none">
  <strong>Comparing:</strong> <span id="compare-list"></span>
  <button type="button" class="btn btn-primary" id="compare-go" disabled>Compare selected</button>
</div>

<div class="company-grid" id="company-grid">
{cards}
</div>
<div class="empty-state" id="empty-state" style="display:none">No companies match these filters.</div>
"""
    return page_shell(
        title="Directory",
        description="Search, filter and compare Norwegian companies with evidence-backed profiles.",
        body=body,
        asset_prefix="",
        nav_active="directory",
    )


# --------------------------------------------------------------------------------------- compare page


def render_compare_page(rows: list[dict]) -> str:
    items = "\n".join(
        f'<label class="fact-row" style="cursor:pointer" data-json="{data_json_attr(r)}">'
        f'<input type="checkbox" style="margin-right:8px"> <strong>{esc(r["legal_name"])}</strong>'
        f'<span class="muted small">&nbsp;({esc(r["org"])}, {r["coverage"]}/12)</span></label>'
        for r in rows
    )
    body = f"""
<h1>Compare companies</h1>
<p class="muted">Pick 2–4 companies to see key facts side by side, with links to the evidence behind each figure.</p>
<div class="compare-controls">
  <button type="button" class="btn btn-primary" id="compare-render">Show comparison</button>
</div>
<div id="compare-list-page">
{items}
</div>
<div id="compare-result" style="margin-top:18px"></div>
"""
    return page_shell(
        title="Compare",
        description="Side-by-side comparison of Norwegian companies with evidence links.",
        body=body,
        asset_prefix="",
        nav_active="compare",
    )


# --------------------------------------------------------------------------------------- evidence drawer


def evidence_drawer(claim: dict, ev_by_id: dict, drawer_id: str) -> str:
    ev_ids = claim.get("evidence_ids") or []
    if not ev_ids:
        return '<span class="muted small">no evidence recorded</span>'
    parts = []
    for i, eid in enumerate(ev_ids):
        e = ev_by_id.get(eid)
        if not e:
            continue
        quote = e.get("claim_span") or e.get("span")
        locator = e.get("span") if e.get("claim_span") and e.get("span") and e.get("span") != e.get("claim_span") else None
        rows = [
            ("Supports", f'<span class="span-text">&ldquo;{esc(quote)}&rdquo;</span>' if quote else ""),
            ("Source", f'<a href="{esc(e["source_url"])}" target="_blank" rel="noopener noreferrer">{esc(e["source_url"])}</a>'),
            ("Source class", esc(e.get("source_class"))),
            ("Retrieved", fmt_date(e.get("retrieved_at"))),
            ("Reporting period", _reporting_period_text(claim)),
            ("Content hash", esc(short_hash(e.get("content_sha256")))),
            ("Extraction method", esc(e.get("extraction_method")) + (f' v{esc(e.get("extractor_version"))}' if e.get("extractor_version") else "")),
            ("Identity basis", esc(claim.get("identity_basis") or "—")),
            ("Located at", f"<code>{esc(locator)}</code>" if locator else ""),
        ]
        rows_html = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows if v)
        parts.append(f'<div class="evidence-drawer"><dl>{rows_html}</dl></div>')
    label = "source" if len(ev_ids) == 1 else f"{len(ev_ids)} sources"
    return f'<details class="source-toggle" id="{esc(drawer_id)}"><summary>{esc(label)}</summary>{"".join(parts)}</details>'


def _reporting_period_text(claim: dict) -> str:
    rp = claim.get("reporting_period")
    if not rp:
        return ""
    start, end = rp.get("start"), rp.get("end")
    if start and end:
        return esc(f"{start} → {end}")
    return esc(start or end or "")


def _linked(title: Any, url: Any) -> str:
    """A readable title, linked to its source page when the URL is http(s)."""
    text = esc(title or url or "")
    link = str(url or "")
    if link.startswith(("http://", "https://")):
        return f'<a href="{esc(link)}" rel="noopener noreferrer" target="_blank">{text}</a>'
    return text


def fact_row(claim_id: str, label: str, value_html: str, source_html: str, note: str | None = None) -> str:
    note_html = f'<div class="fact-note">{esc(note)}</div>' if note else ""
    return (
        f'<div class="fact-row" id="claim-{esc(claim_id)}">'
        f'<span class="fact-label">{esc(label)}</span>'
        f'<span class="fact-value">{value_html}</span> {source_html}'
        f"{note_html}</div>"
    )


# --------------------------------------------------------------------------------------- company page sections


def render_company_page(envelope: dict) -> str:
    org = envelope["organisation_number"]
    claims = envelope.get("claims", [])
    evidence = envelope.get("evidence", [])
    families = envelope.get("families", {})
    changes = envelope.get("changes", [])
    summary = envelope.get("summary", {})
    run = envelope.get("run", {})

    ev_by_id = evidence_by_id(evidence)
    by_family = claims_by_family(claims)
    row = build_row(envelope, f"{org}.html")

    footnote_state: dict[str, int] = {}

    def footnote(claim_ids: list[str]) -> str:
        links = []
        for cid in claim_ids:
            n = footnote_state.get(cid)
            if n is None:
                n = footnote_state[cid] = len(footnote_state) + 1
            links.append(f'<a class="footnote-link" href="#claim-{esc(cid)}">[{n}]</a>')
        return "".join(links)

    header = _render_header(envelope, by_family, row)
    summary_html = _render_summary(summary, footnote)
    section_identity = _render_identity_section(by_family, ev_by_id)
    section_financials = _render_financials_section(by_family, ev_by_id)
    section_leadership = _render_leadership_locations(by_family, ev_by_id)
    section_web = _render_website_profiles(by_family, ev_by_id)
    section_hiring = _render_hiring_activity(by_family, ev_by_id)
    section_evidence = _render_family_table(families)
    section_refresh = _render_refresh(run, changes, ev_by_id)

    qa_profile = build_profile_json(envelope, by_family, ev_by_id, row, changes)

    body = f"""
{header}

<div class="export-row">
  <a class="btn" href="../data/companies/{esc(org)}.json" download>Download company JSON</a>
  <button type="button" class="btn" onclick="window.print()">Print / save as PDF</button>
</div>

<h2 id="summary">Summary</h2>
{summary_html}

<h2 id="legal-identity">{esc(SECTION_LABELS['legal_identity_and_brand'])}</h2>
{section_identity}

<h2 id="annual-accounts">{esc(SECTION_LABELS['annual_accounts'])}</h2>
{section_financials}

<h2 id="leadership-workplaces">{esc(SECTION_LABELS['leadership_and_workplaces'])}</h2>
{section_leadership}

<h2 id="website-profiles">{esc(SECTION_LABELS['website_and_profiles'])}</h2>
{section_web}

<h2 id="hiring-activity">{esc(SECTION_LABELS['hiring_and_activity'])}</h2>
{section_hiring}

<h2 id="evidence-availability">{esc(SECTION_LABELS['evidence_and_availability'])}</h2>
{section_evidence}

<h2 id="refresh-changes">{esc(SECTION_LABELS['refresh_and_changes'])}</h2>
{section_refresh}

<h2 id="ask">Ask this profile</h2>
<div class="qa-box" id="qa-box">
  <p class="muted small">Answers are generated only from the claims and sources shown on this page. Nothing is invented — if a source was not checked or found nothing, the answer says so.</p>
  <div class="qa-chips" id="qa-chips">
    <button type="button" data-q="What does it do?">What does it do?</button>
    <button type="button" data-q="Who runs it?">Who runs it?</button>
    <button type="button" data-q="What are the latest financials?">Latest financials</button>
    <button type="button" data-q="Is it hiring?">Is it hiring?</button>
    <button type="button" data-q="What is the recent activity?">Recent activity</button>
    <button type="button" data-q="What changed?">What changed?</button>
  </div>
  <form class="qa-form" id="qa-form">
    <input type="text" id="qa-input" placeholder="Ask a question about {esc(row['legal_name'])}…" aria-label="Ask a question">
    <button type="submit">Ask</button>
  </form>
  <ul class="qa-log" id="qa-log"></ul>
</div>
<script type="application/json" id="profile-data">{json.dumps(qa_profile, ensure_ascii=False)}</script>
"""
    return page_shell(
        title=row["legal_name"],
        description=f"Evidence-backed company profile for {row['legal_name']} (org. {org}).",
        body=body,
        asset_prefix="../",
        nav_active="",
    )


def _render_header(envelope: dict, by_family: dict, row: dict) -> str:
    org = envelope["organisation_number"]
    identity = by_family.get("identity", [])

    legal_form_value = _val(first_available(identity, "identity", "legal_form"))
    legal_form_html = display_value("legal_form", legal_form_value) if legal_form_value is not None else None

    nace_value = _val(first_available(identity, "identity", "nace"))
    if nace_value is not None:
        nace_html = display_value("nace", nace_value)
    elif row["nace_code"] or row["nace_label"]:
        nace_html = esc(f"{row['nace_code']} {row['nace_label']}".strip())
    else:
        nace_html = None

    address_value = _val(first_available(identity, "identity", "business_address", "postal_address"))
    address_html = display_value("business_address", address_value) if address_value is not None else None

    facts = [
        ("Status", esc(row["status"]) if row["status"] else None),
        ("Legal form", legal_form_html),
        ("Municipality", esc(row["municipality"]) if row["municipality"] else None),
        ("Industry (NACE)", nace_html),
        ("Employees (registered)", esc(row["employees"]) if row["employees"] is not None else None),
        ("Business address", address_html),
    ]
    _not_available = '<span class="muted">not available</span>'
    facts_html = "".join(
        f"<dt>{esc(k)}</dt><dd>{v if v not in (None, '') else _not_available}</dd>"
        for k, v in facts
    )
    brand = row.get("brand_name")
    brand_html = f'<div class="muted">Brand: {esc(brand)}</div>' if brand else ""
    api_url = org_registry_api_url(org)
    search_url = org_registry_search_url(org)
    return f"""<div class="header-card">
  <h1>{esc(row['legal_name'])}</h1>
  {brand_html}
  <div class="org-links">
    Org. number {esc(org)} ·
    <a href="{esc(api_url)}" target="_blank" rel="noopener noreferrer">Brønnøysund API record</a>
    <a href="{esc(search_url)}" target="_blank" rel="noopener noreferrer">Search brreg.no</a>
  </div>
  <dl class="identity-facts">{facts_html}</dl>
</div>"""


def _val_or_none(claim: dict | None):
    return claim.get("value") if claim else None


def _render_summary(summary: dict, footnote) -> str:
    sentences = summary.get("sentences", [])
    if not sentences:
        return '<p class="muted">No summary could be produced from available claims.</p>'
    items = "".join(
        f"<li>{esc(s['text'])}{footnote(s.get('claim_ids', []))}</li>" for s in sentences
    )
    unknowns = summary.get("unknowns", [])
    unknowns_html = ""
    if unknowns:
        unknowns_html = (
            '<div class="unknowns"><strong>Not established</strong><ul>'
            + "".join(f"<li>{esc(u)}</li>" for u in unknowns) + "</ul></div>"
        )
    return f'<ul class="summary-list">{items}</ul>{unknowns_html}'


# Fields rendered with a bespoke formatter rather than the generic display_value() fallback.
_DATE_IDENTITY_FIELDS = {"founded_date"}
_LONG_TEXT_IDENTITY_FIELDS = {"statutory_purpose"}
_CLIP_LEN = 220


def _clip_expandable(claim_id: str, text: str) -> str:
    """Long free-text fields (e.g. statutory purpose) get a short excerpt plus a native, keyboard- and
    print-friendly expand toggle — no JS required."""
    if len(text) <= _CLIP_LEN:
        return esc(text)
    excerpt = text[:_CLIP_LEN].rsplit(" ", 1)[0] + "…"
    return (
        f'{esc(excerpt)} <details class="source-toggle" style="margin-left:4px">'
        f'<summary>show full text</summary><div class="evidence-drawer">{esc(text)}</div></details>'
    )


def _identity_value_html(claim: dict) -> str:
    field, value = claim["field"], claim["value"]
    if claim.get("availability") != "available":
        return f'<span class="badge {AVAILABILITY_CLASS[claim["availability"]]}">{esc(AVAILABILITY_LABELS[claim["availability"]])}</span>'
    if field in _DATE_IDENTITY_FIELDS and isinstance(value, str):
        return fmt_date(value)
    if field == "registered_website" and isinstance(value, str) and value:
        return f'<a href="{esc(value)}" target="_blank" rel="noopener noreferrer">{esc(value)}</a>'
    if field in _LONG_TEXT_IDENTITY_FIELDS and isinstance(value, str):
        return _clip_expandable(claim["claim_id"], value)
    return display_value(field, value)


def _render_identity_section(by_family: dict, ev_by_id: dict) -> str:
    rows = []
    for c in by_family.get("identity", []):
        if c.get("status", "current") != "current":
            continue
        label = label_from_field(c["field"])
        value_html = _identity_value_html(c)
        src = evidence_drawer(c, ev_by_id, f"src-{c['claim_id']}")
        rows.append(fact_row(c["claim_id"], label, value_html, src, c.get("note")))
    group_rows = []
    for c in by_family.get("group", []):
        if c.get("availability") == "available":
            src = evidence_drawer(c, ev_by_id, f"src-{c['claim_id']}")
            v = c.get("value")
            if c["field"] == "group_role" and isinstance(v, dict) and v.get("role") == "parent_company":
                value_html = "Parent company (its filed annual accounts are marked <i>morselskap</i>)"
            else:
                value_html = fmt_value(c["field"], v)
            group_rows.append(fact_row(c["claim_id"], label_from_field(c["field"]), value_html, src, c.get("note")))
    body = "".join(rows) or '<p class="muted">No identity claims available.</p>'
    group_html = ("<h3>Group / ownership</h3>" + "".join(group_rows)) if group_rows else \
        '<h3>Group / ownership</h3><p class="muted">not available</p>'
    return f'<div class="section">{body}{group_html}</div>'


def _render_financials_section(by_family: dict, ev_by_id: dict) -> str:
    fin = [c for c in by_family.get("financials", []) if c.get("status", "current") == "current"]
    rows = []
    for c in fin:
        value_html = fmt_value(c["field"], c["value"]) if c.get("availability") == "available" else \
            f'<span class="badge {AVAILABILITY_CLASS[c["availability"]]}">{esc(AVAILABILITY_LABELS[c["availability"]])}</span>'
        src = evidence_drawer(c, ev_by_id, f"src-{c['claim_id']}")
        period = _reporting_period_text(c)
        label = label_from_field(c["field"]) + (f" ({period})" if period else "")
        rows.append(fact_row(c["claim_id"], label, value_html, src))
    latest_block = "".join(rows) or '<p class="muted">No filed accounts available.</p>'

    history_all = [c for c in by_family.get("financial_history", []) if c.get("status", "current") == "current"]
    # older/synthetic fixtures store one "revenue" claim per year (value_key=year); the real connector
    # may instead (or additionally) report which years have filings via a single "filed_years" list claim.
    revenue_history = [c for c in history_all if c.get("field") == "revenue" and c.get("value_key")]
    other_history = [c for c in history_all if c not in revenue_history]
    spark = _sparkline(revenue_history)
    hist_rows = []
    for c in sorted(revenue_history, key=lambda c: c.get("value_key") or "", reverse=True):
        value_html = fmt_value(c["field"], c["value"])
        src = evidence_drawer(c, ev_by_id, f"src-{c['claim_id']}")
        hist_rows.append(fact_row(c["claim_id"], f"Revenue {esc(c.get('value_key') or '')}", value_html, src))
    for c in other_history:
        value_html = fmt_value(c["field"], c["value"]) if c.get("availability") == "available" else \
            f'<span class="badge {AVAILABILITY_CLASS[c["availability"]]}">{esc(AVAILABILITY_LABELS[c["availability"]])}</span>'
        src = evidence_drawer(c, ev_by_id, f"src-{c['claim_id']}")
        hist_rows.append(fact_row(c["claim_id"], label_from_field(c["field"]), value_html, src))
    hist_html = "".join(hist_rows) or '<p class="muted">No earlier filed years available.</p>'
    return f'<div class="section">{latest_block}<h3>History &amp; trend</h3>{spark}{hist_html}</div>'


def _sparkline(history: list[dict]) -> str:
    points = []
    for c in history:
        year = c.get("value_key")
        val = c.get("value")
        amt = val.get("amount") if isinstance(val, dict) else None
        if year and amt is not None:
            points.append((year, amt))
    if len(points) < 2:
        return ""
    points.sort(key=lambda p: p[0])
    amounts = [p[1] for p in points]
    lo, hi = min(amounts), max(amounts)
    span = (hi - lo) or 1
    w, h, pad = 400, 70, 8
    n = len(points)
    step = (w - 2 * pad) / (n - 1) if n > 1 else 0
    coords = []
    for i, (_, amt) in enumerate(points):
        x = pad + i * step
        y = h - pad - ((amt - lo) / span) * (h - 2 * pad)
        coords.append((x, y))
    path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coords)
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" style="fill:var(--accent)"/>' for x, y in coords)
    labels = "".join(f"<span>{esc(p[0])}</span>" for p in points)
    return (
        f'<div class="sparkline-wrap"><svg class="sparkline" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="Revenue trend from {esc(points[0][0])} to {esc(points[-1][0])}">'
        f'<path d="{path}" fill="none" style="stroke:var(--accent)" stroke-width="2"/>{dots}</svg>'
        f'<div class="sparkline-labels">{labels}</div></div>'
    )


def _role_label_and_value(c: dict) -> tuple[str, str]:
    """Leadership `role` claims carry either a plain name string with a human role note (older/synthetic
    shape) or a structured {"role_code", "role_label", "name"} dict (real registry connector)."""
    value = c["value"]
    if c.get("availability") != "available":
        badge = f'<span class="badge {AVAILABILITY_CLASS[c["availability"]]}">{esc(AVAILABILITY_LABELS[c["availability"]])}</span>'
        return (c.get("note") or label_from_field(c["field"])), badge
    if c["field"] == "role" and isinstance(value, dict):
        role_label = value.get("role_label") or value.get("role_code") or "Role"
        return role_label, esc(value.get("name") or "")
    if c["field"] == "roles_last_changed" and isinstance(value, str):
        return label_from_field(c["field"]), fmt_date(value)
    return (c.get("note") or label_from_field(c["field"])), display_value(c["field"], value)


def _render_leadership_locations(by_family: dict, ev_by_id: dict) -> str:
    roles = [c for c in by_family.get("leadership", []) if c.get("status", "current") == "current"]
    role_rows = []
    for c in roles:
        label, value_html = _role_label_and_value(c)
        src = evidence_drawer(c, ev_by_id, f"src-{c['claim_id']}")
        role_rows.append(fact_row(c["claim_id"], label, value_html, src))
    role_html = "".join(role_rows) or '<p class="muted">No leadership roles available.</p>'

    locs = [c for c in by_family.get("locations", []) if c.get("status", "current") == "current"]
    loc_rows = []
    for c in locs:
        value_html = fmt_value(c["field"], c["value"]) if c.get("availability") == "available" else \
            f'<span class="badge {AVAILABILITY_CLASS[c["availability"]]}">{esc(AVAILABILITY_LABELS[c["availability"]])}</span>'
        src = evidence_drawer(c, ev_by_id, f"src-{c['claim_id']}")
        loc_rows.append(fact_row(c["claim_id"], label_from_field(c["field"]), value_html, src))
    loc_html = "".join(loc_rows) or '<p class="muted">No workplace/location data available.</p>'
    return f'<div class="section"><h3>Leadership</h3>{role_html}<h3>Locations &amp; workplaces</h3>{loc_html}</div>'


_PLATFORM_LABELS = {"linkedin": "LinkedIn", "youtube": "YouTube", "facebook": "Facebook", "instagram": "Instagram",
                    "x": "X", "twitter": "X (Twitter)", "tiktok": "TikTok", "vimeo": "Vimeo", "pinterest": "Pinterest"}


def _website_value_html(c: dict) -> str:
    value = c.get("value")
    if c.get("field") == "official_website":
        v = value if isinstance(value, dict) else {"url": value}
        url = str(v.get("url") or "")
        label = v.get("domain") or url
        return _linked(label, url)
    if isinstance(value, str) and "@" in value and " " not in value:
        return f'<a href="mailto:{esc(value)}">{esc(value)}</a>'
    return display_value(c["field"], value)


def _render_website_profiles(by_family: dict, ev_by_id: dict) -> str:
    site_rows = []
    for c in by_family.get("website", []):
        if c.get("status", "current") != "current":
            continue
        src = evidence_drawer(c, ev_by_id, f"src-{c['claim_id']}")
        if c.get("availability") == "available" and c.get("relationship") in (None, "exact"):
            tag = ' <span class="website-exact-tag">verified exact</span>' if c.get("field") == "official_website" else ""
            value_html = _website_value_html(c) + tag
        elif c.get("value"):
            tag = f' <span class="website-relationship">related / ambiguous ({esc(c.get("relationship") or "unclear")})</span>'
            value_html = _website_value_html(c) + tag
        else:
            value_html = f'<span class="badge {AVAILABILITY_CLASS[c["availability"]]}">{esc(AVAILABILITY_LABELS[c["availability"]])}</span>'
        site_rows.append(fact_row(c["claim_id"], label_from_field(c["field"]), value_html, src, c.get("note")))
    site_html = "".join(site_rows) or '<p class="muted">No website candidate found.</p>'

    desc_rows = []
    for c in by_family.get("description", []):
        if c.get("availability") != "available" or c.get("status", "current") != "current":
            continue
        src = evidence_drawer(c, ev_by_id, f"src-{c['claim_id']}")
        label = "From the register" if c.get("field") == "registry_activity" else "From the website"
        text = c["value"] if isinstance(c["value"], str) else display_value(c["field"], c["value"])
        desc_rows.append(fact_row(c["claim_id"], label, esc(text), src))
    desc_html = "".join(desc_rows) or '<p class="muted">No description available.</p>'

    prof_rows = []
    for c in by_family.get("profiles", []):
        if c.get("availability") != "available" or c.get("status", "current") != "current":
            continue
        src = evidence_drawer(c, ev_by_id, f"src-{c['claim_id']}")
        v = c["value"] if isinstance(c["value"], dict) else {"url": c["value"]}
        platform = _PLATFORM_LABELS.get(str(v.get("platform") or "").lower(), str(v.get("platform") or "Profile").title())
        prof_rows.append(fact_row(c["claim_id"], platform, _linked(v.get("url"), v.get("url")), src))
    prof_html = "".join(prof_rows) or '<p class="muted">No linked profiles found.</p>'

    return f'<div class="section"><h3>Website</h3>{site_html}<h3>Description</h3>{desc_html}<h3>Profiles</h3>{prof_html}</div>'


def _render_hiring_activity(by_family: dict, ev_by_id: dict) -> str:
    jobs = [c for c in by_family.get("jobs", []) if c.get("availability") == "available" and c.get("field") == "job_posting"]
    job_rows = []
    for c in jobs:
        src = evidence_drawer(c, ev_by_id, f"src-{c['claim_id']}")
        v = c["value"] if isinstance(c["value"], dict) else {"title": c["value"]}
        details = ", ".join(x for x in (v.get("location"), f"apply by {str(v['application_due'])[:10]}" if v.get("application_due") else "") if x)
        job_rows.append(fact_row(c["claim_id"], "Open role", _linked(v.get("title"), v.get("ad_url") or v.get("url"))
                                 + (f' <span class="muted small">({esc(details)})</span>' if details else ""), src))
    job_html = "".join(job_rows) or '<p class="muted">No open roles found.</p>'

    activity = sorted(
        [c for c in by_family.get("activity", []) if c.get("availability") == "available"],
        key=lambda c: c.get("effective_date") or "",
        reverse=True,
    )
    act_rows = []
    for c in activity:
        src = evidence_drawer(c, ev_by_id, f"src-{c['claim_id']}")
        v = c["value"] if isinstance(c["value"], dict) else {"title": c["value"]}
        when = c.get("effective_date") or v.get("published")
        date = f' <span class="muted small">({esc(str(when)[:10])})</span>' if when else ""
        act_rows.append(fact_row(c["claim_id"], "Activity", _linked(v.get("title"), v.get("url")) + date, src))
    act_html = "".join(act_rows) or '<p class="muted">No recent activity found.</p>'

    return f'<div class="section"><h3>Hiring</h3>{job_html}<h3>Activity</h3>{act_html}</div>'


def _render_family_table(families: dict) -> str:
    rows = []
    for fam in COVERAGE_FAMILIES:
        st = families.get(fam, {})
        avail = st.get("availability", "not_available")
        reason = st.get("reason") or ""
        sources = st.get("sources_checked") or []
        rows.append(
            f"<tr><td>{esc(FAMILY_LABELS[fam])}</td>"
            f'<td><span class="badge {AVAILABILITY_CLASS[avail]}">{esc(AVAILABILITY_LABELS[avail])}</span></td>'
            f"<td>{esc(reason)}</td>"
            f"<td>{st.get('claim_count', 0)}</td>"
            f"<td>{esc(', '.join(sources)) if sources else '<span class=\"muted\">none</span>'}</td></tr>"
        )
    return (
        '<div class="table-wrap"><table class="data-table"><thead><tr>'
        "<th>Family</th><th>State</th><th>Reason</th><th>Claims</th><th>Sources checked</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _render_refresh(run: dict, changes: list[dict], ev_by_id: dict) -> str:
    run_info = (
        f'<p class="small muted">Run <code>{esc(run.get("run_id"))}</code> completed {fmt_date(run.get("completed_at"))}'
        + (f' · previous run <code>{esc(run.get("previous_run_id"))}</code>' if run.get("previous_run_id") else " · first run, no prior profile")
        + f' · tier {esc(run.get("tier") or "—")}</p>'
    )
    if not changes:
        return run_info + '<p class="muted">No changes detected since the previous run.</p>'
    items = []
    for ch in changes:
        prev_src = _change_side_sources(ch.get("previous_evidence_ids", []), ev_by_id)
        curr_src = _change_side_sources(ch.get("current_evidence_ids", []), ev_by_id)
        prev_html = f'<span class="change-prev">{esc(_fmt_change_value(ch.get("previous_value")))}</span>{prev_src}' if ch.get("previous_value") is not None else '<span class="muted">none</span>'
        curr_html = f'{esc(_fmt_change_value(ch.get("current_value")))}{curr_src}' if ch.get("current_value") is not None else '<span class="muted">none</span>'
        items.append(
            f'<div class="change-item"><div class="change-type">{esc(ch["change_type"].replace("_", " "))}'
            f' · {esc(FAMILY_LABELS.get(ch["family"], ch["family"]))} / {esc(label_from_field(ch["field"]))}</div>'
            f'<div class="change-values">{prev_html} <span class="arrow">&rarr;</span> {curr_html}</div>'
            f'<div class="small muted">detected {fmt_date(ch.get("detected_at"))}{" · material" if ch.get("material") else " · minor"}</div></div>'
        )
    return run_info + "".join(items)


def _fmt_change_value(v: Any) -> str:
    if isinstance(v, dict) and "amount" in v:
        return fmt_money(v)
    return str(v)


def _change_side_sources(ev_ids: list[str], ev_by_id: dict) -> str:
    if not ev_ids:
        return ""
    links = []
    for eid in ev_ids:
        e = ev_by_id.get(eid)
        if e:
            links.append(f'<a href="{esc(e["source_url"])}" target="_blank" rel="noopener noreferrer" class="footnote-link">[src]</a>')
    return "".join(links)


# --------------------------------------------------------------------------------------- Q&A JSON payload


def build_profile_json(envelope: dict, by_family: dict, ev_by_id: dict, row: dict, changes: list[dict]) -> dict:
    def ev_list(ev_ids: list[str]) -> list[dict]:
        out = []
        for eid in ev_ids:
            e = ev_by_id.get(eid)
            if e:
                out.append({"source_url": e["source_url"], "source_class": e.get("source_class"), "retrieved_at": e.get("retrieved_at")})
        return out

    description = next((c for c in by_family.get("description", []) if c.get("availability") == "available"), None)
    leadership = []
    for c in by_family.get("leadership", []):
        if c.get("field") != "role" or c.get("availability") != "available":
            continue
        value = c["value"]
        if isinstance(value, dict):
            name, role = value.get("name"), value.get("role_label") or value.get("role_code")
        else:
            name, role = value, c.get("note")
        leadership.append({"name": name, "role": role, "evidence": ev_list(c.get("evidence_ids", []))})
    revenue_claim = next((c for c in by_family.get("financials", []) if c["field"] == "revenue" and c.get("availability") == "available"), None)
    result_claim = next((c for c in by_family.get("financials", []) if c["field"] == "annual_result" and c.get("availability") == "available"), None)
    financials = None
    if revenue_claim:
        financials = {
            "revenue": {"formatted": fmt_money(revenue_claim["value"])},
            "result": {"formatted": fmt_money(result_claim["value"])} if result_claim else None,
            "period": (f'{revenue_claim["reporting_period"].get("start")} - {revenue_claim["reporting_period"].get("end")}'
                       if revenue_claim.get("reporting_period") else None),
            "evidence": ev_list(revenue_claim.get("evidence_ids", [])),
        }
    website_claim = next((c for c in by_family.get("website", []) if c.get("status", "current") == "current"), None)
    website = None
    if website_claim and website_claim.get("value"):
        website = {
            "url": website_claim["value"],
            "state": "verified exact" if website_claim.get("relationship") == "exact" and website_claim.get("availability") == "available" else "related / ambiguous",
            "evidence": ev_list(website_claim.get("evidence_ids", [])),
        }
    jobs = [
        {"title": c["value"], "evidence": ev_list(c.get("evidence_ids", []))}
        for c in by_family.get("jobs", []) if c.get("availability") == "available"
    ]
    activity = sorted(
        [c for c in by_family.get("activity", []) if c.get("availability") == "available"],
        key=lambda c: c.get("effective_date") or "", reverse=True,
    )
    activity_out = [
        {"text": c["value"], "date": c.get("effective_date"), "evidence": ev_list(c.get("evidence_ids", []))}
        for c in activity
    ]
    changes_out = []
    for ch in changes:
        prev = _fmt_change_value(ch.get("previous_value")) if ch.get("previous_value") is not None else "none"
        curr = _fmt_change_value(ch.get("current_value")) if ch.get("current_value") is not None else "none"
        changes_out.append({
            "summary": f'{ch["change_type"].replace("_", " ")} in {FAMILY_LABELS.get(ch["family"], ch["family"])}: {prev} → {curr}',
        })

    return {
        "org": envelope["organisation_number"],
        "legal_name": row["legal_name"],
        "nace_code": row["nace_code"],
        "nace_label": row["nace_label"],
        "description": {"text": description["value"], "evidence": ev_list(description.get("evidence_ids", []))} if description else None,
        "leadership": leadership,
        "financials": financials,
        "website": website,
        "jobs": jobs,
        "activity": activity_out,
        "changes": changes_out,
    }


# --------------------------------------------------------------------------------------- CSV export


CSV_COLUMNS = [
    "org", "legal_name", "legal_form", "status", "municipality", "nace_code", "nace_label",
    "employees", "employee_band", "revenue", "revenue_currency", "result", "website", "website_state",
    "profiles_count", "jobs_count", "jobs_hiring", "activity_count", "coverage",
]


def render_directory_csv(rows: list[dict]) -> str:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow([r.get(c, "") if r.get(c) is not None else "" for c in CSV_COLUMNS])
    return buf.getvalue()
