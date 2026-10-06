"""Brønnøysund registry: bulk CSV loader, live Brreg fetchers, and the `registry` connector.

Public API used by other workstreams:
- `load_bulk(path, orgs) -> dict[org, row]`
- `RegistryConnector` (name="registry") — implements `context.Connector`
- `registry_facts(ctx) -> dict` — normalized facts other connectors can read from
  `ConnectorResult.shared["registry_facts"]` (name, aliases, address, phone, email, website, role holders,
  subunits).
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
import threading
import time
from typing import Any

from .context import CompanyContext
from .evidence_text import json_quote
from .models import (
    Claim,
    ConnectorResult,
    Evidence,
    FamilyState,
    canonical,
    claim_key,
    evidence_id,
)

BRREG_ENTITY = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
BRREG_ROLES = BRREG_ENTITY + "/roller"
BRREG_SUBUNITS = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=100"
BRREG_ACCOUNTS = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
# The filed-years endpoint is documented at roughly 30 requests/minute; space calls across all threads.
_HISTORY_MIN_INTERVAL_S = 1.0
_history_lock = threading.Lock()
_history_last = [0.0]


def _history_slot() -> None:
    with _history_lock:
        wait = _HISTORY_MIN_INTERVAL_S - (time.monotonic() - _history_last[0])
        if wait > 0:
            time.sleep(wait)
        _history_last[0] = time.monotonic()


BRREG_ACCOUNT_YEARS = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar"
BULK_DOWNLOAD_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"

# Legal forms with a categorical accounting/filing obligation (from Brreg's public guidance; see the
# starter kit's official.py for the fuller rationale).
ALWAYS_FILING_OBLIGED_FORMS = {"AS", "ASA", "BRL", "BBL", "STI", "SF", "VPFO"}


# --------------------------------------------------------------------------------------------------
# Bulk CSV loader
# --------------------------------------------------------------------------------------------------


def _looks_gzip(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def load_bulk(path: str, orgs: list[str] | set[str]) -> dict[str, dict[str, Any]]:
    """Stream the (gzip-compressed, despite the .csv name) bulk enheter file, keeping only `orgs`.

    Accepts a plain CSV too, for tests and small fixtures. Returns raw string-valued rows keyed by
    organisation number, matching the Brreg bulk column names (see BUILD_SPEC.md).
    """
    wanted = set(orgs)
    found: dict[str, dict[str, Any]] = {}
    if not wanted:
        return found
    opener = (lambda p: gzip.open(p, "rt", encoding="utf-8", newline="")) if _looks_gzip(path) else (
        lambda p: open(p, "rt", encoding="utf-8", newline="")
    )
    with opener(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            org = (row.get("organisasjonsnummer") or "").strip()
            if org in wanted:
                found[org] = row
                if len(found) == len(wanted):
                    break
    return found


def _bulk_bool(row: dict[str, Any], key: str) -> bool:
    return str(row.get(key) or "").strip().lower() == "true"


def _bulk_int(row: dict[str, Any], key: str) -> int | None:
    raw = str(row.get(key) or "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _bulk_float(row: dict[str, Any], key: str) -> float | None:
    raw = str(row.get(key) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------------------


def _get(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def normalize_digits(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None


def normalize_website(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    return value


def _person_name(person: dict[str, Any]) -> str | None:
    navn = person.get("navn") or {}
    return " ".join(filter(None, [navn.get("fornavn"), navn.get("mellomnavn"), navn.get("etternavn")])) or None


def _address_dict(addr: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(addr, dict):
        return None
    return {
        "street": ", ".join(addr.get("adresse") or []) or None,
        "postcode": addr.get("postnummer"),
        "city": addr.get("poststed"),
        "municipality": addr.get("kommune"),
        "country": addr.get("land"),
    }


class _ClaimBuilder:
    """Accumulates claims + evidence for one company, deduping evidence by id."""

    def __init__(self, org: str, now: str, bulk_retrieved_at: str | None = None):
        self.org = org
        self.now = now
        self.bulk_retrieved_at = bulk_retrieved_at  # download time of the bulk file, for facts read from it
        self.claims: list[Claim] = []
        self.evidence: dict[str, Evidence] = {}
        self.sources_checked: dict[str, list[str]] = {}

    def add_evidence(
        self, *, source_url: str, source_class: str, retrieved_at: str, extraction_method: str,
        span: str | None, content_sha256: str | None = None, final_url: str | None = None,
        redirect_chain: list[str] | None = None, http_status: int | None = None,
        snapshot_ref: str | None = None, access_policy: str | None = "NLOD-2.0", quote: str | None = None,
    ) -> str:
        """`quote`: the exact source text the value was read from (becomes the evidence's claim_span)."""
        eid = evidence_id(source_url, span)
        if eid not in self.evidence:
            self.evidence[eid] = Evidence(
                evidence_id=eid, source_url=source_url, final_url=final_url, redirect_chain=redirect_chain or [],
                http_status=http_status, source_class=source_class, retrieved_at=retrieved_at,
                content_sha256=content_sha256, snapshot_ref=snapshot_ref, extraction_method=extraction_method,
                span=span, access_policy=access_policy, claim_span=quote,
            )
        return eid

    def add_claim(
        self, *, family: str, field: str, value: Any, value_key: str | None = None,
        availability: str = "available", evidence_ids: list[str] | None = None,
        identity_basis: str | None = "registry_record", reporting_period: Any = None,
        effective_date: str | None = None, confidence: float = 1.0, note: str | None = None,
        status: str = "current",
    ) -> Claim:
        claim = Claim(
            claim_id=claim_key(self.org, family, field, value_key),
            organisation_number=self.org,
            family=family,
            field=field,
            value=value,
            value_key=value_key,
            availability=availability,
            confidence=confidence,
            identity_basis=identity_basis,
            reporting_period=reporting_period,
            effective_date=effective_date,
            evidence_ids=evidence_ids or [],
            status=status,
        )
        self.claims.append(claim)
        return claim

    def note_checked(self, family: str, source: str) -> None:
        self.sources_checked.setdefault(family, [])
        if source not in self.sources_checked[family]:
            self.sources_checked[family].append(source)


# --------------------------------------------------------------------------------------------------
# Identity (bulk fallback + live)
# --------------------------------------------------------------------------------------------------


def bulk_row_sha256(row: dict[str, Any]) -> str:
    """Content hash of the exact bulk-file row a value was read from (canonical JSON of the row)."""
    return hashlib.sha256(canonical(row).encode("utf-8")).hexdigest()


def identity_claims_from_bulk(builder: _ClaimBuilder, row: dict[str, Any], *, snapshot_sha256: str | None) -> None:
    """Fallback identity claims from the bulk row (used when the live entity fetch fails)."""
    org = builder.org
    eid = builder.add_evidence(
        source_url=BULK_DOWNLOAD_URL, source_class="official_registry_bulk", retrieved_at=builder.bulk_retrieved_at or builder.now,
        extraction_method="brreg_bulk_row_v1", span=f"row organisasjonsnummer={org}", content_sha256=snapshot_sha256 or bulk_row_sha256(row),
    )
    legal_form = (row.get("organisasjonsform.kode") or "").strip() or None
    if row.get("navn"):
        builder.add_claim(family="identity", field="legal_name", value=row["navn"], evidence_ids=[eid])
    if legal_form:
        builder.add_claim(family="identity", field="legal_form", value={
            "code": legal_form, "label": row.get("organisasjonsform.beskrivelse") or None,
        }, evidence_ids=[eid])
    status = "bankrupt" if _bulk_bool(row, "konkurs") else "liquidating" if _bulk_bool(row, "underAvvikling") else "active"
    builder.add_claim(family="identity", field="status", value=status, evidence_ids=[eid])
    if row.get("naeringskode1.kode"):
        builder.add_claim(family="identity", field="nace", value={
            "code": row.get("naeringskode1.kode"), "label": row.get("naeringskode1.beskrivelse"),
        }, evidence_ids=[eid])
    employees = _bulk_int(row, "antallAnsatte")
    if _bulk_bool(row, "harRegistrertAntallAnsatte") and employees is not None:
        builder.add_claim(family="identity", field="registered_employees", value={
            "count": employees, "registered_at": row.get("registreringsdatoAntallAnsatteEnhetsregisteret") or None,
        }, evidence_ids=[eid])
    business_address = {
        "street": row.get("forretningsadresse.adresse"), "postcode": row.get("forretningsadresse.postnummer"),
        "city": row.get("forretningsadresse.poststed"), "municipality": row.get("forretningsadresse.kommune"),
        "country": row.get("forretningsadresse.land"),
    }
    if any(business_address.values()):
        builder.add_claim(family="identity", field="business_address", value=business_address, evidence_ids=[eid])
    postal_address = {
        "street": row.get("postadresse.adresse"), "postcode": row.get("postadresse.postnummer"),
        "city": row.get("postadresse.poststed"), "municipality": row.get("postadresse.kommune"),
        "country": row.get("postadresse.land"),
    }
    if any(postal_address.values()):
        builder.add_claim(family="identity", field="postal_address", value=postal_address, evidence_ids=[eid])
    if row.get("stiftelsesdato"):
        builder.add_claim(family="identity", field="founded_date", value=row["stiftelsesdato"], evidence_ids=[eid])
    website = normalize_website(row.get("hjemmeside"))
    if website:
        builder.add_claim(family="identity", field="registered_website", value=website, evidence_ids=[eid])
    if row.get("epostadresse"):
        builder.add_claim(family="identity", field="registry_email", value=row["epostadresse"], evidence_ids=[eid])
    phone = row.get("telefon") or row.get("mobil")
    if phone:
        builder.add_claim(family="identity", field="registry_phone", value=phone, evidence_ids=[eid])
    if row.get("registrertIMvaRegisteret") is not None:
        builder.add_claim(family="identity", field="vat_registered", value=_bulk_bool(row, "registrertIMvaRegisteret"), evidence_ids=[eid])
    if row.get("vedtektsfestetFormaal"):
        builder.add_claim(family="identity", field="statutory_purpose", value=row["vedtektsfestetFormaal"], evidence_ids=[eid])
    activity = _registry_activity_text(row.get("aktivitet"), row.get("vedtektsfestetFormaal"))
    if activity:
        builder.add_claim(family="description", field="registry_activity", value=activity, evidence_ids=[eid])
        builder.note_checked("description", BULK_DOWNLOAD_URL)
    builder.note_checked("identity", BULK_DOWNLOAD_URL)


def _carry_previous_claim(builder: "_ClaimBuilder", previous: dict[str, Any] | None, family: str, field: str) -> None:
    """Re-publish a stored claim (with its evidence) that this run could not re-check.

    Used when the live entity call fails for a known company: the registry description shares its family
    with the website description, so family-level carry-forward in refresh cannot keep it.
    """
    envelope = (previous or {}).get("last_envelope") or {}
    evidence = {e.get("evidence_id"): e for e in envelope.get("evidence", [])}
    for raw in envelope.get("claims", []):
        if raw.get("family") == family and raw.get("field") == field and raw.get("status") == "current":
            claim = Claim.model_validate(raw)
            builder.claims.append(claim)
            for eid in claim.evidence_ids:
                if eid in evidence and eid not in builder.evidence:
                    builder.evidence[eid] = Evidence.model_validate(evidence[eid])
            return


def _registry_activity_text(activity: Any, purpose: Any) -> str | None:
    """What the company says it does, as registered: `aktivitet`, else the statutory purpose.

    Published as a registry-sourced description (field `registry_activity`) so every registered company
    has one, separate from a description taken from its own website (`company_description`).
    """
    for raw in (activity, purpose):
        if not raw:
            continue
        text = " ".join(str(x) for x in raw) if isinstance(raw, list) else str(raw)
        text = " ".join(text.split())
        if len(text) >= 12:
            return text
    return None


def identity_claims_from_live(builder: _ClaimBuilder, body: dict[str, Any], response: Any) -> None:
    span_prefix = "$"

    def ev(span: str, quote: str | None = None) -> str:
        return builder.add_evidence(
            source_url=response.url, source_class="official_registry", retrieved_at=response.retrieved_at,
            extraction_method="brreg_entity_v1", span=span, content_sha256=response.content_sha256,
            final_url=response.final_url, redirect_chain=response.redirect_chain, http_status=response.status,
            snapshot_ref=response.snapshot_ref, quote=quote,
        )

    if body.get("navn"):
        builder.add_claim(family="identity", field="legal_name", value=body["navn"], evidence_ids=[ev(f"{span_prefix}.navn")])
    form = _get(body, "organisasjonsform", "kode")
    if form:
        builder.add_claim(family="identity", field="legal_form", value={
            "code": form, "label": _get(body, "organisasjonsform", "beskrivelse"),
        }, evidence_ids=[ev(f"{span_prefix}.organisasjonsform")])
    status = "bankrupt" if body.get("konkurs") else "liquidating" if body.get("underAvvikling") else "active"
    builder.add_claim(family="identity", field="status", value=status, evidence_ids=[ev(
        f"{span_prefix}.konkurs/underAvvikling",
        json_quote(body, ("konkurs", "underAvvikling", "underTvangsavviklingEllerTvangsopplosning")),
    )])
    nace = body.get("naeringskode1")
    if nace:
        builder.add_claim(family="identity", field="nace", value={"code": nace.get("kode"), "label": nace.get("beskrivelse")}, evidence_ids=[ev(f"{span_prefix}.naeringskode1")])
    if body.get("harRegistrertAntallAnsatte") and body.get("antallAnsatte") is not None:
        builder.add_claim(family="identity", field="registered_employees", value={
            "count": body.get("antallAnsatte"), "registered_at": body.get("registreringsdatoAntallAnsatteEnhetsregisteret"),
        }, evidence_ids=[ev(
            f"{span_prefix}.antallAnsatte",
            json_quote(body, ("antallAnsatte", "registreringsdatoAntallAnsatteEnhetsregisteret")),
        )])
    business_address = _address_dict(body.get("forretningsadresse"))
    if business_address:
        builder.add_claim(family="identity", field="business_address", value=business_address, evidence_ids=[ev(f"{span_prefix}.forretningsadresse")])
    postal_address = _address_dict(body.get("postadresse"))
    if postal_address:
        builder.add_claim(family="identity", field="postal_address", value=postal_address, evidence_ids=[ev(f"{span_prefix}.postadresse")])
    if body.get("stiftelsesdato"):
        builder.add_claim(family="identity", field="founded_date", value=body["stiftelsesdato"], evidence_ids=[ev(f"{span_prefix}.stiftelsesdato")])
    website = normalize_website(body.get("hjemmeside"))
    if website:
        builder.add_claim(family="identity", field="registered_website", value=website, evidence_ids=[ev(f"{span_prefix}.hjemmeside")])
    if body.get("epostadresse"):
        builder.add_claim(family="identity", field="registry_email", value=body["epostadresse"], evidence_ids=[ev(f"{span_prefix}.epostadresse")])
    phone = body.get("telefon") or body.get("mobil")
    if phone:
        builder.add_claim(family="identity", field="registry_phone", value=phone, evidence_ids=[ev(f"{span_prefix}.telefon")])
    if body.get("registrertIMvaregisteret") is not None:
        builder.add_claim(family="identity", field="vat_registered", value=bool(body.get("registrertIMvaregisteret")), evidence_ids=[ev(f"{span_prefix}.registrertIMvaregisteret")])
    purpose = body.get("vedtektsfestetFormaal")
    if purpose:
        text = " ".join(purpose) if isinstance(purpose, list) else str(purpose)
        builder.add_claim(family="identity", field="statutory_purpose", value=text, evidence_ids=[ev(f"{span_prefix}.vedtektsfestetFormaal")])
    activity = _registry_activity_text(body.get("aktivitet"), purpose)
    if activity:
        span = "aktivitet" if body.get("aktivitet") else "vedtektsfestetFormaal"
        builder.add_claim(family="description", field="registry_activity", value=activity, evidence_ids=[ev(f"{span_prefix}.{span}")])
        builder.note_checked("description", getattr(response, "url", "") or "")
    historic = body.get("historiskeNavn") or []
    if historic:
        names = [item.get("navn") for item in historic if item.get("navn")]
        if names:
            builder.add_claim(family="identity", field="historic_names", value=names, evidence_ids=[ev(f"{span_prefix}.historiskeNavn")])
    # group (parent), via overordnetEnhet if this org is itself a subunit of another entity
    parent = body.get("overordnetEnhet") or body.get("hovedenhet")
    if parent:
        builder.add_claim(family="group", field="parent", value={"organisation_number": parent}, evidence_ids=[ev(f"{span_prefix}.overordnetEnhet")])
    builder.note_checked("identity", response.url)


# --------------------------------------------------------------------------------------------------
# Leadership
# --------------------------------------------------------------------------------------------------


def leadership_claims(builder: _ClaimBuilder, body: dict[str, Any], response: Any) -> None:
    groups = body.get("rollegrupper") or []
    if not groups:
        builder.note_checked("leadership", response.url)
        return
    for group in groups:
        group_code = _get(group, "type", "kode")
        last_changed = group.get("sistEndret")
        if last_changed:
            eid = builder.add_evidence(
                source_url=response.url, source_class="official_registry", retrieved_at=response.retrieved_at,
                extraction_method="brreg_roles_v1", span=f"$.rollegrupper[type.kode={group_code}].sistEndret",
                content_sha256=response.content_sha256, final_url=response.final_url,
                redirect_chain=response.redirect_chain, http_status=response.status, snapshot_ref=response.snapshot_ref,
            )
            builder.add_claim(family="leadership", field="roles_last_changed", value=last_changed, value_key=group_code, evidence_ids=[eid])
        for role in group.get("roller", []):
            role_code = _get(role, "type", "kode")
            role_label = _get(role, "type", "beskrivelse")
            person = role.get("person") or {}
            entity = role.get("enhet") or {}
            name = _person_name(person) if person else None
            entity_name = entity.get("navn")
            if isinstance(entity_name, list):
                entity_name = entity_name[0] if entity_name else None
            display_name = name or entity_name
            if not display_name or not role_code:
                continue
            resigned = bool(role.get("avregistrert"))
            span = f"$.rollegrupper[type.kode={group_code}].roller[type.kode={role_code}]"
            eid = builder.add_evidence(
                source_url=response.url, source_class="official_registry", retrieved_at=response.retrieved_at,
                extraction_method="brreg_roles_v1", span=span, content_sha256=response.content_sha256,
                final_url=response.final_url, redirect_chain=response.redirect_chain, http_status=response.status,
                snapshot_ref=response.snapshot_ref,
            )
            value = {"role_code": role_code, "role_label": role_label, "name": display_name}
            if entity_name and entity.get("organisasjonsnummer"):
                value["entity_name"] = entity_name
                value["entity_organisation_number"] = entity.get("organisasjonsnummer")
            builder.add_claim(
                family="leadership", field="role", value=value,
                value_key=f"{role_code}|{display_name.strip().casefold()}", evidence_ids=[eid],
                status="withdrawn" if resigned else "current",
                note="resigned/deregistered" if resigned else None,
            )
    builder.note_checked("leadership", response.url)


# --------------------------------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------------------------------


def locations_claims_from_subunits(builder: _ClaimBuilder, body: dict[str, Any], response: Any) -> list[dict[str, Any]]:
    rows = _get(body, "_embedded", "underenheter") or []
    facts: list[dict[str, Any]] = []
    for item in rows:
        sub_org = item.get("organisasjonsnummer")
        addr = item.get("beliggenhetsadresse") or item.get("postadresse")
        addr_norm = _address_dict(addr) or {}
        value = {
            "organisation_number": sub_org,
            "name": item.get("navn"),
            "street": addr_norm.get("street"),
            "postcode": addr_norm.get("postcode"),
            "city": addr_norm.get("city"),
            "employees": item.get("antallAnsatte"),
            "nace": _get(item, "naeringskode1", "kode"),
        }
        span = f"$._embedded.underenheter[organisasjonsnummer={sub_org}]"
        eid = builder.add_evidence(
            source_url=response.url, source_class="official_registry", retrieved_at=response.retrieved_at,
            extraction_method="brreg_subunits_v1", span=span, content_sha256=response.content_sha256,
            final_url=response.final_url, redirect_chain=response.redirect_chain, http_status=response.status,
            snapshot_ref=response.snapshot_ref,
        )
        builder.add_claim(family="locations", field="workplace", value=value, value_key=sub_org, evidence_ids=[eid])
        facts.append({
            "organisation_number": sub_org, "name": item.get("navn"), "street": addr_norm.get("street"),
            "postcode": addr_norm.get("postcode"), "city": addr_norm.get("city"), "employees": item.get("antallAnsatte"),
            "website": normalize_website(item.get("hjemmeside")), "email": item.get("epostadresse"),
            "phones": [p for p in (item.get("telefon"), item.get("mobil")) if p],
        })
    builder.note_checked("locations", response.url)
    return facts


def business_address_location_claim(builder: _ClaimBuilder, business_address: dict[str, Any] | None, eid: str) -> None:
    if business_address and any(business_address.values()):
        builder.add_claim(family="locations", field="business_address", value=business_address, evidence_ids=[eid])


# --------------------------------------------------------------------------------------------------
# Financials
# --------------------------------------------------------------------------------------------------


_METRIC_PATHS = {
    "revenue": ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
    "operating_result": ("resultatregnskapResultat", "driftsresultat", "driftsresultat"),
    "annual_result": ("resultatregnskapResultat", "aarsresultat"),
    "total_assets": ("eiendeler", "sumEiendeler"),
    "equity": ("egenkapitalGjeld", "egenkapital", "sumEgenkapital"),
    "total_debt": ("egenkapitalGjeld", "gjeldOversikt", "sumGjeld"),
}


def financials_claims(builder: _ClaimBuilder, body: list[Any], response: Any) -> None:
    if not isinstance(body, list) or not body:
        builder.note_checked("financials", response.url)
        return
    # Prefer SELSKAP (company-level) records; pick the most recent period.
    records = [r for r in body if isinstance(r, dict)]
    records.sort(key=lambda r: (_get(r, "regnskapsperiode", "tilDato") or ""), reverse=True)
    company_records = [r for r in records if r.get("regnskapstype") == "SELSKAP"] or records
    latest = company_records[0]
    period_start = _get(latest, "regnskapsperiode", "fraDato")
    period_end = _get(latest, "regnskapsperiode", "tilDato")
    currency = latest.get("valuta") or "NOK"
    record_id = latest.get("id")
    period = {"start": period_start, "end": period_end}
    for field, path in _METRIC_PATHS.items():
        amount = _get(latest, *path)
        if amount is None:
            continue
        span = f"$[id={record_id}]." + ".".join(path)
        eid = builder.add_evidence(
            source_url=response.url, source_class="official_filing", retrieved_at=response.retrieved_at,
            extraction_method="brreg_financials_v1", span=span, content_sha256=response.content_sha256,
            final_url=response.final_url, redirect_chain=response.redirect_chain, http_status=response.status,
            snapshot_ref=response.snapshot_ref,
        )
        builder.add_claim(
            family="financials", field=field, value={"amount": amount, "currency": currency}, value_key=field,
            reporting_period=period, evidence_ids=[eid],
        )
    principles = latest.get("regnkapsprinsipper") if isinstance(latest.get("regnkapsprinsipper"), dict) else {}
    eid_meta = builder.add_evidence(
        source_url=response.url, source_class="official_filing", retrieved_at=response.retrieved_at,
        extraction_method="brreg_financials_v1", span=f"$[id={record_id}].regnskapstype",
        content_sha256=response.content_sha256, final_url=response.final_url,
        redirect_chain=response.redirect_chain, http_status=response.status, snapshot_ref=response.snapshot_ref,
        quote=", ".join(x for x in (json_quote(latest, ("regnskapstype",)), json_quote(principles, ("smaaForetak",))) if x) or None,
    )
    builder.add_claim(
        family="financials", field="accounting_type", value={
            "type": latest.get("regnskapstype"),
            "small_enterprise": _get(latest, "regnkapsprinsipper", "smaaForetak"),
        }, value_key=None, reporting_period=period, evidence_ids=[eid_meta],
    )
    if _get(latest, "virksomhet", "morselskap") is True:
        # The filed accounts mark this company as a parent company (morselskap): an official group fact.
        eid_parent = builder.add_evidence(
            source_url=response.url, source_class="official_filing", retrieved_at=response.retrieved_at,
            extraction_method="brreg_financials_v1", span=f"$[id={record_id}].virksomhet.morselskap",
            content_sha256=response.content_sha256, final_url=response.final_url,
            redirect_chain=response.redirect_chain, http_status=response.status, snapshot_ref=response.snapshot_ref,
            quote=json_quote(latest.get("virksomhet"), ("morselskap",)),
        )
        builder.add_claim(
            family="group", field="group_role", value={"role": "parent_company", "basis": "annual accounts: morselskap"},
            reporting_period=period, evidence_ids=[eid_parent],
        )
        builder.note_checked("group", response.url)
    audit = latest.get("revisjon")
    if audit is not None:
        eid_audit = builder.add_evidence(
            source_url=response.url, source_class="official_filing", retrieved_at=response.retrieved_at,
            extraction_method="brreg_financials_v1", span=f"$[id={record_id}].revisjon",
            content_sha256=response.content_sha256, final_url=response.final_url,
            redirect_chain=response.redirect_chain, http_status=response.status, snapshot_ref=response.snapshot_ref,
            quote=json_quote(audit, tuple(audit)) if isinstance(audit, dict) else None,
        )
        builder.add_claim(
            family="financials", field="audit_status", value=audit, value_key=None,
            reporting_period=period, evidence_ids=[eid_audit],
        )
    builder.note_checked("financials", response.url)


def financial_history_claims(builder: _ClaimBuilder, body: Any, response: Any, *, legal_form: str | None) -> None:
    if not isinstance(body, list):
        if legal_form and legal_form.upper() not in ALWAYS_FILING_OBLIGED_FORMS:
            eid = builder.add_evidence(
                source_url=response.url, source_class="official_filing", retrieved_at=response.retrieved_at,
                extraction_method="brreg_financial_history_v1", span=None, content_sha256=response.content_sha256,
            )
            builder.add_claim(family="financial_history", field="filed_years", value=None, availability="not_applicable", evidence_ids=[eid], note="Legal form is not categorically filing-obliged")
        builder.note_checked("financial_history", response.url)
        return
    years = sorted({str(y) for y in body if str(y).isdigit()})
    eid = builder.add_evidence(
        source_url=response.url, source_class="official_filing", retrieved_at=response.retrieved_at,
        extraction_method="brreg_financial_history_v1", span="$", content_sha256=response.content_sha256,
        final_url=response.final_url, redirect_chain=response.redirect_chain, http_status=response.status,
        snapshot_ref=response.snapshot_ref,
    )
    builder.add_claim(family="financial_history", field="filed_years", value=years, evidence_ids=[eid])
    builder.note_checked("financial_history", response.url)


# --------------------------------------------------------------------------------------------------
# Connector
# --------------------------------------------------------------------------------------------------


class RegistryConnector:
    name = "registry"
    families: tuple[str, ...] = ("identity", "financials", "financial_history", "leadership", "locations", "group", "description")

    def run(self, ctx: CompanyContext) -> ConnectorResult:
        org = ctx.org
        now = ctx.now
        builder = _ClaimBuilder(org, now, (ctx.shared or {}).get("bulk_retrieved_at"))
        errors: list[dict[str, Any]] = []
        families: dict[str, FamilyState] = {}
        client = ctx.client
        tier = (ctx.tier or "T0").upper()

        bulk_row = ctx.bulk or {}
        legal_form = (bulk_row.get("organisasjonsform.kode") or "").strip() or None

        entity_body: dict[str, Any] | None = None
        entity_ok = False
        if client is not None:
            resp = client.get(BRREG_ENTITY.format(org=org), org=org, purpose="registry_entity", accept="application/json", respect_robots=False)
            if resp.error == "budget_exhausted":
                families["identity"] = FamilyState(family="identity", availability="failed", reason="request_budget")
            elif resp.ok:
                try:
                    entity_body = _json(resp)
                    entity_ok = True
                except Exception as exc:
                    errors.append({"stage": "registry_entity", "error": str(exc)})
            elif resp.status in (404, 410):
                families["identity"] = FamilyState(family="identity", availability="not_available", reason=f"registry entity {resp.status}")
            else:
                errors.append({"stage": "registry_entity", "status": resp.status, "error": resp.error})
                if ctx.previous:
                    # Live registry unreachable for a company we have profiled before: report the family
                    # as failed so refresh carries the stored facts forward. The bulk file formats
                    # addresses differently and lacks historic names, so using it here would publish
                    # false changes and removals.
                    families["identity"] = FamilyState(family="identity", availability="failed", reason=f"live registry unavailable ({resp.error or resp.status}); previous profile kept")
                    families["locations"] = FamilyState(family="locations", availability="failed", reason="live registry unavailable; previous profile kept")
                    _carry_previous_claim(builder, ctx.previous, "description", "registry_activity")

        if entity_ok and entity_body is not None:
            identity_claims_from_live(builder, entity_body, resp)
            ctx.registry["entity"] = entity_body
        elif "identity" not in families:
            if bulk_row:
                identity_claims_from_bulk(builder, bulk_row, snapshot_sha256=None)
            else:
                families["identity"] = FamilyState(family="identity", availability="failed", reason="no registry data available")

        if "identity" not in families:
            families["identity"] = FamilyState(
                family="identity", availability="available",
                sources_checked=builder.sources_checked.get("identity", []),
                claim_count=sum(1 for c in builder.claims if c.family == "identity"),
            )

        # -------- leadership (roles) --------
        if client is not None and "leadership" not in families:
            resp = client.get(BRREG_ROLES.format(org=org), org=org, purpose="registry_roles", accept="application/json", respect_robots=False)
            if resp.error == "budget_exhausted":
                families["leadership"] = FamilyState(family="leadership", availability="failed", reason="request_budget")
            elif resp.ok:
                try:
                    body = _json(resp)
                    leadership_claims(builder, body, resp)
                    role_claims = [c for c in builder.claims if c.family == "leadership"]
                    families["leadership"] = FamilyState(
                        family="leadership",
                        availability="available" if role_claims else "not_available",
                        reason=None if role_claims else "checked: none found",
                        sources_checked=builder.sources_checked.get("leadership", []), claim_count=len(role_claims),
                    )
                except Exception as exc:
                    errors.append({"stage": "registry_roles", "error": str(exc)})
                    families["leadership"] = FamilyState(family="leadership", availability="failed", reason="parse_error")
            elif resp.status in (404, 410):
                families["leadership"] = FamilyState(family="leadership", availability="not_available", reason=f"registry roles {resp.status}")
            else:
                families["leadership"] = FamilyState(family="leadership", availability="failed", reason=f"http {resp.status or resp.error}")
        elif "leadership" not in families:
            families["leadership"] = FamilyState(family="leadership", availability="failed", reason="not_run")

        # -------- locations (subunits) --------
        subunit_facts: list[dict[str, Any]] = []
        run_subunits = client is not None and (tier != "T0")
        if run_subunits and "locations" not in families:
            resp = client.get(BRREG_SUBUNITS.format(org=org), org=org, purpose="registry_subunits", accept="application/json", respect_robots=False)
            if resp.error == "budget_exhausted":
                families["locations"] = FamilyState(family="locations", availability="failed", reason="request_budget")
            elif resp.ok:
                try:
                    body = _json(resp)
                    subunit_facts = locations_claims_from_subunits(builder, body, resp)
                except Exception as exc:
                    errors.append({"stage": "registry_subunits", "error": str(exc)})
            elif resp.status in (404, 410):
                pass
            else:
                errors.append({"stage": "registry_subunits", "status": resp.status, "error": resp.error})
                if ctx.previous:
                    families["locations"] = FamilyState(family="locations", availability="failed", reason=f"registry subunits unavailable ({resp.error or resp.status}); previous profile kept")

        business_address = None
        if entity_ok and entity_body is not None:
            business_address = _address_dict(entity_body.get("forretningsadresse"))
        elif bulk_row and families.get("locations") is None:
            business_address = {
                "street": bulk_row.get("forretningsadresse.adresse"), "postcode": bulk_row.get("forretningsadresse.postnummer"),
                "city": bulk_row.get("forretningsadresse.poststed"), "municipality": bulk_row.get("forretningsadresse.kommune"),
                "country": bulk_row.get("forretningsadresse.land"),
            }
        if business_address and any(business_address.values()):
            identity_evidence = [c for c in builder.claims if c.family == "identity" and c.field == "business_address"]
            eid = identity_evidence[0].evidence_ids[0] if identity_evidence and identity_evidence[0].evidence_ids else builder.add_evidence(
                source_url=BRREG_ENTITY.format(org=org), source_class="official_registry_bulk", retrieved_at=now,
                extraction_method="brreg_bulk_row_v1", span="forretningsadresse",
            )
            business_address_location_claim(builder, business_address, eid)

        if "locations" not in families:
            location_claims = [c for c in builder.claims if c.family == "locations"]
            if not run_subunits:
                families["locations"] = FamilyState(
                    family="locations", availability="available" if location_claims else "not_available",
                    reason="tier T0: business address only" if tier == "T0" else ("checked: none found" if not location_claims else None),
                    sources_checked=builder.sources_checked.get("locations", []), claim_count=len(location_claims),
                )
            else:
                families["locations"] = FamilyState(
                    family="locations", availability="available" if location_claims else "not_available",
                    reason=None if location_claims else "checked: none found",
                    sources_checked=builder.sources_checked.get("locations", []), claim_count=len(location_claims),
                )

        # -------- financials (latest) --------
        if client is not None and "financials" not in families:
            resp = client.get(BRREG_ACCOUNTS.format(org=org), org=org, purpose="registry_financials", accept="application/json", respect_robots=False)
            if resp.error == "budget_exhausted":
                families["financials"] = FamilyState(family="financials", availability="failed", reason="request_budget")
            elif resp.ok:
                try:
                    body = _json(resp)
                    financials_claims(builder, body, resp)
                    fin_claims = [c for c in builder.claims if c.family == "financials"]
                    families["financials"] = FamilyState(
                        family="financials", availability="available" if fin_claims else "not_available",
                        reason=None if fin_claims else "checked: none found",
                        sources_checked=builder.sources_checked.get("financials", []), claim_count=len(fin_claims),
                    )
                except Exception as exc:
                    errors.append({"stage": "registry_financials", "error": str(exc)})
                    families["financials"] = FamilyState(family="financials", availability="failed", reason="parse_error")
            elif resp.status in (404, 410):
                families["financials"] = FamilyState(family="financials", availability="not_available", reason=f"registry financials {resp.status}")
            else:
                families["financials"] = FamilyState(family="financials", availability="failed", reason=f"http {resp.status or resp.error}")
        elif "financials" not in families:
            families["financials"] = FamilyState(family="financials", availability="failed", reason="not_run")

        # -------- financial_history (every filing company; one cheap call) --------
        if client is not None and (ctx.shared or {}).get("registry_only"):
            # Time budget is short for the rest of the batch; this endpoint is rate-limited to 1/s.
            families["financial_history"] = FamilyState(family="financial_history", availability="failed", reason="time_budget: registry-only pass")
        elif client is not None and tier == "T0" and str(bulk_row.get("sisteInnsendteAarsregnskap") or "").strip().isdigit():
            # Shell-tier companies: the filed-years endpoint is throttled (about one call a second across the
            # run), so the latest filed year comes from the same-day bulk row instead of a live call.
            year = int(str(bulk_row["sisteInnsendteAarsregnskap"]).strip())
            eid = builder.add_evidence(
                source_url=BULK_DOWNLOAD_URL, source_class="official_registry_bulk", retrieved_at=builder.bulk_retrieved_at or builder.now,
                extraction_method="brreg_bulk_row_v1", span="$.sisteInnsendteAarsregnskap",
                content_sha256=bulk_row_sha256(bulk_row),
            )
            builder.add_claim(family="financial_history", field="latest_filed_year", value=year, evidence_ids=[eid])
            builder.note_checked("financial_history", BULK_DOWNLOAD_URL)
            families["financial_history"] = FamilyState(
                family="financial_history", availability="available", reason=None,
                sources_checked=[BULK_DOWNLOAD_URL], claim_count=1,
            )
        elif client is not None:
            _history_slot()
            resp = client.get(BRREG_ACCOUNT_YEARS.format(org=org), org=org, purpose="registry_financial_history", accept="application/json", respect_robots=False)
            if resp.error == "budget_exhausted":
                families["financial_history"] = FamilyState(family="financial_history", availability="failed", reason="request_budget")
            elif resp.ok:
                try:
                    body = _json(resp)
                    financial_history_claims(builder, body, resp, legal_form=legal_form)
                    hist_claims = [c for c in builder.claims if c.family == "financial_history"]
                    not_applicable = any(c.availability == "not_applicable" for c in hist_claims)
                    families["financial_history"] = FamilyState(
                        family="financial_history",
                        availability="not_applicable" if not_applicable else ("available" if hist_claims else "not_available"),
                        reason=(hist_claims[0].note if not_applicable and hist_claims else (None if hist_claims else "checked: none found")),
                        sources_checked=builder.sources_checked.get("financial_history", []), claim_count=len(hist_claims),
                    )
                except Exception as exc:
                    errors.append({"stage": "registry_financial_history", "error": str(exc)})
                    families["financial_history"] = FamilyState(family="financial_history", availability="failed", reason="parse_error")
            elif resp.status in (404, 410):
                families["financial_history"] = FamilyState(family="financial_history", availability="not_available", reason=f"registry history {resp.status}")
            else:
                families["financial_history"] = FamilyState(family="financial_history", availability="failed", reason=f"http {resp.status or resp.error}")
        else:
            families["financial_history"] = FamilyState(
                family="financial_history", availability="not_available" if tier in ("T0", "T1") else "failed",
                reason="registry client unavailable",
            )

        # -------- group --------
        group_claims = [c for c in builder.claims if c.family == "group"]
        group_sources = list(dict.fromkeys([BRREG_ENTITY.format(org=org)] + builder.sources_checked.get("group", [])))
        if group_claims:
            families["group"] = FamilyState(family="group", availability="available", sources_checked=group_sources, claim_count=len(group_claims))
        elif families.get("financials") is not None and families["financials"].availability == "failed":
            # The parent-company flag comes from the accounts; unread accounts mean "not checked".
            families["group"] = FamilyState(family="group", availability="failed", reason="annual accounts unavailable this run", sources_checked=group_sources)
        else:
            families["group"] = FamilyState(family="group", availability="not_available", reason="no parent relationship or parent-company flag in registry data", sources_checked=group_sources)

        ctx.registry["subunits"] = subunit_facts
        shared = {"registry_facts": registry_facts(ctx, builder=builder, entity_body=entity_body, bulk_row=bulk_row, subunit_facts=subunit_facts)}

        return ConnectorResult(claims=builder.claims, evidence=list(builder.evidence.values()), families=families, errors=errors, shared=shared)


def _json(response: Any) -> Any:
    import json

    return json.loads(response.body.decode("utf-8", "replace"))


def registry_facts(
    ctx: CompanyContext, *, builder: _ClaimBuilder | None = None, entity_body: dict[str, Any] | None = None,
    bulk_row: dict[str, Any] | None = None, subunit_facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalized registry facts for other connectors (candidate generation, corroboration)."""
    bulk_row = bulk_row if bulk_row is not None else (ctx.bulk or {})
    entity_body = entity_body if entity_body is not None else ctx.registry.get("entity")
    subunit_facts = subunit_facts if subunit_facts is not None else ctx.registry.get("subunits") or []

    if entity_body:
        name = entity_body.get("navn")
        street = _get(entity_body, "forretningsadresse", "adresse")
        street = ", ".join(street) if isinstance(street, list) else street
        postcode = _get(entity_body, "forretningsadresse", "postnummer")
        city = _get(entity_body, "forretningsadresse", "poststed")
        email = entity_body.get("epostadresse")
        website = normalize_website(entity_body.get("hjemmeside"))
        historic = [item.get("navn") for item in entity_body.get("historiskeNavn") or [] if item.get("navn")]
    else:
        name = bulk_row.get("navn")
        street = bulk_row.get("forretningsadresse.adresse")
        postcode = bulk_row.get("forretningsadresse.postnummer")
        city = bulk_row.get("forretningsadresse.poststed")
        email = bulk_row.get("epostadresse")
        website = normalize_website(bulk_row.get("hjemmeside"))
        historic = []

    # The live entity API omits contact fields (e-mail, phone, often homepage); the bulk snapshot has them.
    email = email or bulk_row.get("epostadresse") or None
    website = website or normalize_website(bulk_row.get("hjemmeside"))
    phone_values = [bulk_row.get("telefon"), bulk_row.get("mobil")]
    if entity_body:
        phone_values += [entity_body.get("telefon"), entity_body.get("mobil")]
    phones = sorted({normalize_digits(p) for p in phone_values if p and normalize_digits(p)})
    role_holders: list[str] = []
    if builder is not None:
        for claim in builder.claims:
            if claim.family == "leadership" and claim.field == "role" and claim.status == "current":
                holder_name = (claim.value or {}).get("name")
                if holder_name:
                    role_holders.append(holder_name)

    email_domain = None
    if email and "@" in email:
        email_domain = email.rsplit("@", 1)[-1].strip().lower() or None

    subunit_names = sorted({s.get("name") for s in subunit_facts if s.get("name")})

    return {
        "organisation_number": ctx.org,
        "name": name,
        "aliases": historic + subunit_names,
        "street": street,
        "postcode": postcode,
        "city": city,
        "phones": phones,
        "email": email,
        "email_domain": email_domain,
        "website": website,
        "role_holders": role_holders,
        "subunits": subunit_facts,
    }
