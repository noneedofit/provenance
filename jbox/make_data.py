"""Compact companies.json for the JBOX site, from a Signalpost envelopes.jsonl.

Usage (from the repository root): uv run python jbox/make_data.py smoke-test/envelopes.jsonl jbox/companies.json
Every fact keeps its source URL, retrieval time and the verbatim source text. Contact details (register
e-mail/phone, site contact e-mail) are left out, and a register quote that carries one is cut down to
the verbatim fields that hold the fact."""
import json
import re
import sys

from signalpost.evidence_text import value_text
from signalpost.models import Claim

_CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\"(epostadresse|telefon|mobil)\"")
_CONTACT_ONLY = re.compile(r"\s*([\w.+-]+@[\w-]+\.[\w.]+|\+?[\d ]{8,15})\s*")


def drop_contact_sources(sources: list[dict], org: str) -> list[dict]:
    """Sources whose whole quote is an e-mail address or phone number (a register contact matched on the
    site) are left out, as long as another source still backs the fact. The org number written alone
    ('911 739 410') is identity evidence, not a phone number, and stays."""
    def contact_only(quote: str) -> bool:
        return bool(_CONTACT_ONLY.fullmatch(quote)) and re.sub(r"\D", "", quote) != org
    kept = [s for s in sources if not contact_only(s["quote"] or "")]
    return kept or sources


def top_level_pieces(text: str) -> list[str]:
    """The complete '"key":value' pieces at the top level of a JSON object text, verbatim; a piece cut
    off at the end of a truncated quote is dropped."""
    pieces, depth, in_str, esc, begin = [], 0, False, False, None
    for i, ch in enumerate(text):
        if in_str:
            esc, in_str = (not esc and ch == "\\"), (in_str if esc or ch != '"' else False)
            continue
        if ch == '"':
            in_str = True
            if depth == 1 and begin is None:
                begin = i
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0 and begin is not None:
                pieces.append(text[begin:i])
                begin = None
        elif ch == "," and depth == 1 and begin is not None:
            pieces.append(text[begin:i])
            begin = None
    return pieces


def lean_quote(quote: str, value: str) -> str:
    """The quote, or for a register JSON body that carries contact details, only its top-level
    '"key":value' pieces (verbatim substrings) that share a word with the fact's value."""
    if not _CONTACT.search(quote) or not quote.lstrip().startswith("{"):
        return quote
    words = set(re.findall(r"\w{3,}", value.casefold()))
    clean = [p for p in top_level_pieces(quote) if not _CONTACT.search(p)]
    pieces = [p for p in clean if words & set(re.findall(r"\w{3,}", p.casefold()))]
    # Nothing shares a word with the value: keep the record's own number and name, never an empty quote.
    pieces = pieces or [p for p in clean if p.startswith(('"organisasjonsnummer"', '"navn"'))]
    return " ... ".join(pieces)


# Plain-English labels for the register's Norwegian role names (the source quote keeps the original).
ROLE_EN = {"Daglig leder": "Chief executive officer", "Styrets leder": "Chair of the board", "Styremedlem": "Board member",
           "Nestleder": "Deputy chair", "Varamedlem": "Deputy board member", "Observatør": "Board observer",
           "Revisor": "Auditor", "Regnskapsfører": "Accountant", "Forretningsfører": "Business manager",
           "Innehaver": "Owner", "Kontaktperson": "Contact person", "Komplementar": "General partner",
           "Deltaker med delt ansvar": "Partner (shared liability)", "Deltaker med fullt ansvar": "Partner (full liability)",
           "Daglig leder/ adm.direktør": "Chief executive", "Oppl om foretaket i hjemlandet": "Foreign parent (home country)",
           "Norsk representant for utenlandsk enhet": "Norwegian representative", "Bestyrende reder": "Managing owner"}
STATUS_EN = {"active": "Active", "liquidating": "In liquidation", "bankrupt": "Bankrupt", "deleted": "Deleted"}


PLATFORM = {"facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn", "youtube": "YouTube",
            "x": "X (Twitter)", "twitter": "X (Twitter)", "tiktok": "TikTok", "pinterest": "Pinterest", "vimeo": "Vimeo"}
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def place_name(name: str | None) -> str | None:
    """'INDRE ØSTFOLD' -> 'Indre Østfold', 'NORD-FRON' -> 'Nord-Fron' (the register writes places in capitals)."""
    if not name:
        return name
    return " ".join("-".join(part.capitalize() for part in word.split("-")) for word in str(name).split())


def nice_date(iso: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{int(m.group(3))} {_MONTHS[int(m.group(2)) - 1]} {m.group(1)}" if m else (iso or "")


# The site shows no e-mail addresses (some contact addresses on company pages are personal).
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def display_value(c: dict, text: str) -> str:
    """How the site shows a fact: readable words instead of the raw record. The verbatim source quote
    is untouched; only the line a visitor reads changes."""
    v, f = c.get("value"), c["field"]
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if f == "status" and isinstance(v, str):
        return STATUS_EN.get(v, v.capitalize())
    if isinstance(v, dict):
        if f == "accounting_type":
            kind = "Company accounts" if v.get("type") == "SELSKAP" else "Group accounts" if v.get("type") == "KONSERN" else str(v.get("type") or "Accounts")
            return kind + ("; small enterprise rules" if v.get("small_enterprise") else "")
        if f == "audit_status":
            if v.get("fravalgRevisjon"):
                return "Not audited (the company has opted out of audit)"
            return "Not audited" if v.get("ikkeRevidertAarsregnskap") else "Audited"
        if f == "group_role":
            return "Parent company of a group (stated in its annual accounts)"
        if f in ("parent_company", "subsidiary") and v.get("name"):
            share = re.sub(r"[,.]0+%$", "%", str(v.get("ownership") or ""))
            own = (f", owns {share} of this company" if f == "parent_company" else f", {share} owned") if share else ""
            return f"{v['name']} (org. no. {v.get('organisation_number')}){own}"
        if f == "active_postings_count" and "verified" in v:
            n = int(v["verified"] or 0)
            return f"{n} open job ad{'s' if n != 1 else ''} on NAV"
    return text


_UNKNOWN_RULES = [
    (r"^Public reviews or ratings:", "Reviews and ratings: none found. We checked Mattilsynet's food-safety inspections; Google, Trustpilot and Glassdoor reviews are only available under a paid licence."),
    (r"^Public activity[^:]*: no (exact-)?verified company website.*news search", "News and posts: no news article naming the company was found (Bing News), and no company website could be confirmed."),
    (r"^Public activity[^:]*: .*news search", "News and posts: none found on the company website or in news search (Bing News)."),
    (r"^Public activity[^:]*: no (exact-)?verified company website", "News and posts: none, because no company website could be confirmed."),
    (r"^Public activity[^:]*: no site-derived activity source", "News and posts: the company website has no news page, news feed or linked video channel."),
    (r"^Public activity[^:]*: checked: none found", "News and posts: none found on the company website."),
    (r"^Public social or video profiles: no (exact-)?verified site", "Social profiles: none, because no company website could be confirmed."),
    (r"^Public social or video profiles: checked: none found", "Social profiles: the company website links none."),
    (r"^A description of what it does: no (exact-)?verified site", "Description: none, because no company website could be confirmed."),
    (r"^Its group / ownership structure: not part of a registered group", "Group and ownership: not part of a registered group."),
    (r"^Open job postings: not searched", "Job ads: not searched, as the company has no registered employees and no website."),
    (r"^Open job postings: checked NAV", "Job ads: no open ads under this organisation number on NAV (arbeidsplassen.no)."),
    (r"^An official website: no candidates", "Official website: none found."),
    (r"^An official website: related site found \(brand\)", "Official website: the site found belongs to the brand owner, so it is not listed as this company's own."),
    (r"^An official website: related site found \(parent\): registry hjemmeside is shared by (\d+)",
     lambda m: f"Official website: the registered website is shared by {m.group(1)} organisations, so it is not listed as this company's own."),
    (r"^An official website: related site found", "Official website: the site found belongs to a parent or related organisation, so it is not listed as this company's own."),
]


def plain_unknown(text: str) -> str:
    """A visitor-facing sentence for one 'could not find' line; the pipeline's wording otherwise."""
    m = re.match(r"^An official website: checked (\d+) candidates?: none verified", text)
    if m:
        n = int(m.group(1))
        found = "none showed" if n != 1 else "it did not show"
        return (f"Official website: none confirmed. We checked {n} possible site{'s' if n != 1 else ''}; "
                f"{found} this company's name together with its address or org. number.")
    m = re.match(r"^An official website: checked: (\S+) belongs to another organisation", text)
    if m:
        return f"Official website: {m.group(1)} belongs to a different organisation, so it is not listed."
    for pattern, out in _UNKNOWN_RULES:
        m = re.match(pattern, text)
        if m:
            return out(m) if callable(out) else out
    return text

SKIP = {("identity", "registry_email"), ("identity", "registry_phone"), ("website", "site_contact_email")}
SECTION = {
    "identity": "Identity", "financials": "Accounts", "financial_history": "Accounts history", "group": "Group",
    "leadership": "Leadership", "locations": "Locations", "website": "Website", "profiles": "Social profiles",
    "description": "About", "jobs": "Jobs", "activity": "News & activity", "reviews": "Reviews",
}
KIND = {"official_registry": "Official register (Brønnøysund)", "official_registry_bulk": "Official register bulk file",
        "company_owned": "Company website", "official_filing": "Official accounts copy (Brønnøysund)", "news_media": "News article", "official_public": "Public agency", "public_open_data": "Open data"}
LABEL = {"legal_name": "Legal name", "nace": "Industry", "secondary_nace": "Other industry",
         "institutional_sector": "Sector", "registration_date": "Registered", "registered_in": "Registered in", "legal_form": "Legal form", "founded_date": "Founded",
         "status": "Status", "business_address": "Business address", "registered_employees": "Number of employees",
         "official_website": "Website", "revenue": "Revenue", "annual_result": "Annual result",
         "operating_result": "Operating result", "equity": "Equity", "total_assets": "Total assets",
         "total_debt": "Total debt", "role": "Role", "workplace": "Workplace", "profile": "Profile",
         "company_description": "Description (company website)", "registry_activity": "Registered activity",
         "statutory_purpose": "Statutory purpose", "vat_registered": "VAT registered", "parent_company": "Parent company",
         "subsidiary": "Subsidiary", "annual_accounts_copy": "Annual accounts (PDF)", "activity_item": "News item",
         "job_posting": "Job ad", "active_postings_count": "Open job ads (NAV)", "historic_names": "Former names"}


def year_of(c):
    v = c.get("value")
    if isinstance(v, dict) and v.get("year"):
        return v["year"]
    period = c.get("reporting_period") or {}
    return (period.get("end") or c.get("effective_date") or "")[:4] or None


def main(src, out):
    companies = []
    for line in open(src, encoding="utf-8"):
        e = json.loads(line)
        ev = {x["evidence_id"]: x for x in e.get("evidence", [])}
        facts, quick = [], {}
        for c in e.get("claims", []):
            if (c["family"], c["field"]) in SKIP or c.get("status", "current") != "current" or c.get("availability") != "available":
                continue
            text = value_text(Claim(**c))
            if not text:
                continue
            sources = []
            for eid in c.get("evidence_ids", []):
                x = ev.get(eid)
                if x:
                    sources.append({"url": x.get("final_url") or x.get("source_url"), "retrieved_at": x.get("retrieved_at"),
                                    "kind": KIND.get(x.get("source_class"), x.get("source_class")),
                                    "quote": _EMAIL.sub("[e-mail]", lean_quote(x.get("claim_span") or "", text)[:600])})
            sources = drop_contact_sources(sources, e["organisation_number"])
            fact = {"id": c["claim_id"], "section": SECTION.get(c["family"], c["family"]),
                    "label": LABEL.get(c["field"], c["field"].replace("_", " ").capitalize()), "value": display_value(c, text),
                    "year": year_of(c) if c["family"] in ("financials", "financial_history") else None, "sources": sources}
            if c["field"] == "role" and isinstance(c.get("value"), dict):
                role_label = str(c["value"].get("role_label") or "Role")
                fact["label"] = ROLE_EN.get(role_label, role_label)
                fact["value"] = str(c["value"].get("name") or text)
                if c["value"].get("role_code") == "DAGL":
                    quick["ceo"] = fact["value"]
                elif c["value"].get("role_code") == "LEDE":
                    quick["chair"] = fact["value"]
            v = c.get("value")
            if c["field"] == "profile" and isinstance(v, dict):
                fact["label"] = PLATFORM.get(str(v.get("platform") or "").lower(), str(v.get("platform") or "Profile").capitalize())
            elif c["field"] == "activity_item" and isinstance(v, dict) and v.get("title"):
                fact["label"] = "News · " + nice_date(str(v.get("published") or ""))
                fact["value"] = str(v["title"])
                if str(v.get("url") or "").startswith(("https://", "http://")):
                    fact["link"] = v["url"]  # the article itself (the source may be the site's feed)
            elif c["field"] == "registered_employees" and isinstance(v, dict) and v.get("count") is not None:
                # The bare count, so it matches the comparison column; the register date goes beside it.
                fact["value"] = str(v["count"])
                if v.get("registered_at"):
                    fact["year"] = "registered " + nice_date(str(v["registered_at"]))
            elif c["field"] == "workforce" and isinstance(v, dict) and v.get("count") is not None:
                n = v["count"]
                shown = f"{n:g}" if isinstance(n, (int, float)) else str(n)
                fact["label"] = "Workforce (annual accounts)"
                fact["value"] = f"{shown} full-time equivalent{'' if shown == '1' else 's'}" if v.get("measure") == "fte" else f"{shown} employee{'' if shown == '1' else 's'}"
                if v.get("url"):
                    fact["link"] = v["url"]
            elif c["field"] == "annual_accounts_copy" and year_of(c):
                fact["label"] = f"Annual accounts {year_of(c)} (PDF)"
            facts.append(fact)
            if c["family"] == "identity" and c["field"] == "business_address" and isinstance(v, dict) and v.get("municipality"):
                facts.append({"id": c["claim_id"] + "_municipality", "section": fact["section"], "label": "Municipality",
                              "value": place_name(v["municipality"]), "year": None, "sources": sources})
            f = c["field"]
            if c["family"] == "financials" and f in ("revenue", "annual_result"):
                quick[f] = c.get("value", {}).get("amount") if isinstance(c.get("value"), dict) else c.get("value")
                quick["accounts_year"] = year_of(c)
            elif f == "registered_employees":
                quick["employees"] = c["value"].get("count") if isinstance(c["value"], dict) else c["value"]
            elif f == "workforce" and isinstance(c["value"], dict):
                quick["workforce_fte"] = c["value"].get("count")
                quick["workforce_year"] = c["value"].get("year")
            elif f == "official_website":
                quick["website"] = text
            elif f == "nace":
                quick["industry"] = text
            elif f == "business_address" and c["family"] == "identity" and isinstance(c["value"], dict):
                quick["municipality"] = place_name(c["value"].get("municipality") or c["value"].get("city"))
        # Newest news first (the site lists facts in file order).
        published = {c["claim_id"]: str((c.get("value") or {}).get("published") or "") for c in e.get("claims", [])
                     if c["family"] == "activity" and isinstance(c.get("value"), dict)}
        news = sorted((f for f in facts if f["id"] in published), key=lambda f: published[f["id"]], reverse=True)
        news_iter = iter(news)
        facts = [next(news_iter) if f["id"] in published else f for f in facts]
        kept = {f["id"] for f in facts}
        # The site heads the summary "In short", so the first sentence does not repeat it.
        # Dates in the prose read as "1 Jul 2012", like the rest of the site.
        summary = [{"text": re.sub(r"\b\d{4}-\d{2}-\d{2}\b", lambda m: nice_date(m.group(0)),
                                   re.sub(r"^In short:\s*", "", s["text"]) if n == 0 else s["text"]),
                    "fact_ids": [i for i in s.get("claim_ids", []) if i in kept]}
                   for n, s in enumerate((e.get("summary") or {}).get("sentences", []))]
        quick["news_count"] = len(published)
        quick["fact_count"] = len(facts)
        companies.append({"org": e["organisation_number"], "name": e.get("legal_name"), "quick": quick,
                          "summary": summary, "unknowns": [plain_unknown(u) for u in (e.get("summary") or {}).get("unknowns", [])],
                          "retrieved": e["run"].get("completed_at"), "facts": facts})
    companies.sort(key=lambda c: c["name"] or "")
    json.dump({"source": "Provenance (Signalpost agent) run output", "companies": companies}, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
