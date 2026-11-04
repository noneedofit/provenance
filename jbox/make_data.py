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


def drop_contact_sources(sources: list[dict]) -> list[dict]:
    """Sources whose whole quote is an e-mail address or phone number (a register contact matched on the
    site) are left out, as long as another source still backs the fact."""
    kept = [s for s in sources if not _CONTACT_ONLY.fullmatch(s["quote"] or "")]
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
    pieces = [p for p in top_level_pieces(quote)
              if not _CONTACT.search(p) and words & set(re.findall(r"\w{3,}", p.casefold()))]
    return " ... ".join(pieces)

SKIP = {("identity", "registry_email"), ("identity", "registry_phone"), ("website", "site_contact_email")}
SECTION = {
    "identity": "Identity", "financials": "Accounts", "financial_history": "Accounts history", "group": "Group",
    "leadership": "Leadership", "locations": "Locations", "website": "Website", "profiles": "Social profiles",
    "description": "About", "jobs": "Jobs", "activity": "News & activity", "reviews": "Reviews",
}
KIND = {"official_registry": "Official register (Brønnøysund)", "official_registry_bulk": "Official register bulk file",
        "company_owned": "Company website", "official_public": "Public agency", "public_open_data": "Open data"}
LABEL = {"legal_name": "Legal name", "nace": "Industry", "legal_form": "Legal form", "founded_date": "Founded",
         "status": "Status", "business_address": "Business address", "registered_employees": "Registered employees",
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
                                    "quote": lean_quote(x.get("claim_span") or "", text)[:600]})
            sources = drop_contact_sources(sources)
            fact = {"id": c["claim_id"], "section": SECTION.get(c["family"], c["family"]),
                    "label": LABEL.get(c["field"], c["field"].replace("_", " ").capitalize()), "value": text,
                    "year": year_of(c) if c["family"] in ("financials", "financial_history") else None, "sources": sources}
            if c["field"] == "role" and isinstance(c.get("value"), dict):
                fact["label"] = str(c["value"].get("role_label") or "Role")
                fact["value"] = str(c["value"].get("name") or text)
                if c["value"].get("role_code") == "DAGL":
                    quick["ceo"] = fact["value"]
                elif c["value"].get("role_code") == "LEDE":
                    quick["chair"] = fact["value"]
            facts.append(fact)
            f = c["field"]
            if c["family"] == "financials" and f in ("revenue", "annual_result"):
                quick[f] = c.get("value", {}).get("amount") if isinstance(c.get("value"), dict) else c.get("value")
                quick["accounts_year"] = year_of(c)
            elif f == "registered_employees":
                quick["employees"] = c["value"]
            elif f == "official_website":
                quick["website"] = text
            elif f == "nace":
                quick["industry"] = text
            elif f == "business_address" and c["family"] == "identity" and isinstance(c["value"], dict):
                quick["municipality"] = c["value"].get("municipality") or c["value"].get("city")
        kept = {f["id"] for f in facts}
        summary = [{"text": s["text"], "fact_ids": [i for i in s.get("claim_ids", []) if i in kept]}
                   for s in (e.get("summary") or {}).get("sentences", [])]
        companies.append({"org": e["organisation_number"], "name": e.get("legal_name"), "quick": quick,
                          "summary": summary, "unknowns": (e.get("summary") or {}).get("unknowns", []),
                          "retrieved": e["run"].get("completed_at"), "facts": facts})
    companies.sort(key=lambda c: c["name"] or "")
    json.dump({"source": "Provenance (Signalpost agent) run output", "companies": companies}, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
