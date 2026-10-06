# Data schema: envelopes, claims, evidence

Defined in `src/signalpost/models.py` (pydantic). Every module reads and writes these types; nothing
constructs an envelope directly except `pipeline.run_batch`. `SCHEMA_VERSION = "signalpost-envelope/1"`.

## Envelope

One `Envelope` per input organisation number, written as one line of `envelopes.jsonl`:

```python
class Envelope(BaseModel):
    schema_version: str = "signalpost-envelope/1"
    organisation_number: str
    legal_name: str | None
    run: RunInfo
    sections: dict[str, list[str]]       # section name -> claim_ids, see SECTIONS below
    families: dict[str, FamilyState]     # one entry per FAMILIES member, always
    claims: list[Claim]
    evidence: list[Evidence]
    changes: list[Change]                # from this run's refresh diff, [] on a first run
    summary: Summary
    errors: list[dict]
    operations: Operations               # requests, runtime_ms, third_party_cost_usd (always 0.0)
```

## Families

```
FAMILIES = (
    "identity", "financials", "financial_history", "leadership", "locations", "group",   # official
    "website", "profiles", "description", "jobs", "activity", "reviews",                  # external
)
```

`EXTERNAL_FAMILIES` (the coverage differentiators) is the last six. Every envelope has a `FamilyState`
for all twelve, even when nothing was found — see `AGENT.md`'s abstention-policy table for what each
`availability` value means.

```python
class FamilyState(BaseModel):
    family: str
    availability: Availability   # "available" | "not_available" | "blocked" | "not_applicable" | "ambiguous" | "failed"
    reason: str | None           # why not_available/blocked/ambiguous/failed
    sources_checked: list[str]
    claim_count: int
```

## Claims

```python
class Claim(BaseModel):
    claim_id: str                # "c_" + sha256(org|family|field|value_key)[:20] — stable across runs
    organisation_number: str
    family: str
    field: str                   # e.g. "legal_name", "revenue", "role", "official_website", "job_posting"
    value: Any                   # None only when availability != "available"
    value_key: str | None        # None for single-valued fields; a stable item key for multi-valued ones
    availability: Availability
    confidence: float = 1.0
    identity_basis: IdentityBasis | None
    relationship: Literal["exact","parent","subsidiary","franchise","brand","service_provider"] | None
    reporting_period: ReportingPeriod | None    # {start, end}, financial claims
    effective_date: str | None   # date the fact refers to (job published date, role change date, ...)
    evidence_ids: list[str]
    first_observed_at: str | None   # set by refresh
    last_observed_at: str | None    # set by refresh
    status: Literal["current","superseded","withdrawn"] = "current"
    note: str | None
```

`claim_key(org, family, field, value_key)` (`models.py`) is the single source of claim-id stability:
single-valued fields (`value_key=None`) keep the same `claim_id` when the value changes, so refresh can
report a `changed_*` event; multi-valued fields (roles, jobs, locations, profiles, activity items) pass a
stable per-item key — role `f"{role_code}|{normalized person name}"`, jobs `nav:{uuid}` / `ats:{url}`,
locations the subunit organisation number, profiles the normalized URL, activity items the item URL or
feed guid — so each item has its own new/ended lifecycle independent of the others.

`identity_basis` (why a claim is trusted, `IdentityBasis` in `models.py`): `registry_record` (from the
official register for this org number), `org_number_on_source` (the source itself shows the exact org
number), `registry_declared` (the register lists this website/email domain and it passed the live
verification gate), `job_feed_org_number` (a NAV ad carries the exact org number), `wikidata_org_number`
(Wikidata item carries the exact org number), `open_map_org_number` (an OpenStreetMap feature tagged
`ref:NO:orgnr` with the exact org number links the site), `linked_from_verified_site` (linked from a site already
verified `exact`), `corroborated` (≥2 independent corroborating signals, no conflict).

## Evidence

```python
class Evidence(BaseModel):
    evidence_id: str              # "e_" + sha256(source_url|span)[:20]; stable across runs
    source_url: str
    final_url: str | None
    redirect_chain: list[str]
    http_status: int | None
    source_class: SourceClass
    retrieved_at: str             # ISO 8601 UTC
    content_sha256: str | None    # hash of the raw response body
    snapshot_ref: str | None      # key into the gzip content-addressed snapshot store
    extraction_method: str        # e.g. "brreg_roles_v1", "jsonld_org_v1", "web_registry_declared_v1"
    extractor_version: str = "1"
    span: str | None              # exact supporting text / JSON path / selector, <=500 chars
    access_policy: str | None     # e.g. "NLOD-2.0", "CC0", "robots-allowed"
    id: str | None                # mirrors evidence_id (output-contract field)
    claim_span: str | None        # the exact supporting text as the source writes it, inline
```

`SourceClass`: `official_registry` (live `data.brreg.no` API), `official_registry_bulk` (bulk CSV
download), `official_filing` (annual-account copy), `public_job_feed` (NAV), `open_knowledge_base`
(Wikidata), `company_owned` (the verified site itself), `company_owned_platform` (a company-owned profile
on a third-party platform — URL/feed only, never scraped content beyond the feed), `official_inspection`
(a public authority's inspection result page, e.g. Mattilsynet smilefjes), `open_places_dataset` (a record
in a bundled open places dataset, Overture Maps or OpenStreetMap), `derived` (computed by the agent, e.g.
the summary).

Every claim marked `available` must reference at least one evidence id that exists in the same envelope's
`evidence` list — enforced by `validate.py` (`DATA_SCHEMA.md`'s invariants are the same ones `EVAL.md`'s
scorer's `contract` check re-verifies against the output).

## Sections

`SECTIONS` (`models.py`) maps the seven brief-required sections to the families whose claims populate
them; `envelope.sections[name]` is the list of claim ids in that section:

```
legal_identity_and_brand      -> identity, group
annual_accounts               -> financials, financial_history
leadership_and_workplaces     -> leadership, locations
website_and_profiles          -> website, profiles, description
hiring_and_activity           -> jobs, activity, reviews
evidence_and_availability     -> ()  # satisfied by claims/evidence/families directly
refresh_and_changes           -> ()  # satisfied by run/changes directly
```

Claim values worth knowing (others are self-describing):

- `website/official_website`: the site URL (string). `relationship` is `exact` for the company's own
  verified site, or `parent`/`brand`/`subsidiary` for its group's site declared for this org number.
- `financial_history/filed_years`: list of years with filed accounts; for shell-tier companies
  `financial_history/latest_filed_year` (an int, from the register bulk file) instead.
- `reviews/inspection_rating`: `{rater, scheme, place, place_org_number, grade_code (0-3, or null when
  only the page's smiley is known), grade, inspected_on, url}` (date and smiley as stated on the page) — Mattilsynet's latest food-hygiene inspection of one food-service location;
  `place_org_number` is the company's or a subunit's org number shown on the inspection page.
- `jobs/job_posting`: one open NAV (or ATS) posting; `jobs/active_postings_count`: `{verified: n}`.

## Changes (refresh output)

```python
class Change(BaseModel):
    change_id: str          # "x_" + sha256(claim_id|type|previous|current)[:20]
    change_type: str        # new_role, ended_role, new_filing, changed_value, new_job, closed_job,
                             # new_location, closed_location, new_profile, removed_profile,
                             # changed_website, changed_description, new_activity, identity_changed
    family: str
    field: str
    claim_id: str
    previous_value: Any
    current_value: Any
    previous_evidence_ids: list[str]
    current_evidence_ids: list[str]
    detected_at: str
    material: bool = True
```

See `REFRESH.md` for how `changes` is computed and what `first_observed_at`/`last_observed_at` mean.

## Summary

```python
class Summary(BaseModel):
    language: str = "en"
    sentences: list[SummarySentence]   # {text, claim_ids}, claim_ids always non-empty
    unknowns: list[str]                # one entry per family not "available" this run, with its reason
    method: str = "template_v1"
```

See `docs/synthesis.md` for how sentences are built.

## Run info and operations

```python
class RunInfo(BaseModel):
    run_id: str
    previous_run_id: str | None
    started_at: str
    completed_at: str | None
    terminal_status: Literal["completed","partial","failed"] = "completed"
        # completed: every family was checked; partial: at least one family is failed/blocked, or the
        # deadline, request budget or registry-only pass applied; failed: the company itself crashed.
        # Non-fatal probe errors (a guessed feed path answering 404) are listed in `errors` only.
    agent_version: str = "0.1.0"
    tier: str | None            # the planner's budget tier for this company

class Operations(BaseModel):
    requests: int = 0
    bytes: int = 0
    runtime_ms: int = 0
    third_party_cost_usd: float = 0.0   # always 0.0 — no paid APIs anywhere in this agent
```

## ConnectorResult (internal, not part of the output contract)

```python
class ConnectorResult(BaseModel):
    claims: list[Claim] = []
    evidence: list[Evidence] = []
    families: dict[str, FamilyState] = {}
    errors: list[dict] = []
    shared: dict[str, Any] = {}   # facts other connectors may read, e.g. verified_site, site_pages
```

Every connector returns a `ConnectorResult`; only `pipeline.run_batch` assembles these into an
`Envelope`. `shared` is the mechanism by which `registry` → `nav` → `web` → `activity` hand facts
forward within one company's run (e.g. `registry_facts`, `verified_site`, `nav_ads`) — see `CRAWLERS.md`.


## Evidence record (output contract)

Every evidence record carries `id` (same as `evidence_id`), `source_url` (public http/https), `retrieved_at`,
`content_sha256` and `claim_span` — the exact supporting text from the source (e.g. `"Org nr. 926781995"`
on the company's site) or, for registry JSON, the exact value read at `span` (e.g. `"246367 NOK"` at
`$[id=…].sumDriftsinntekter`). A claim can therefore be verified from the saved result alone; the
validator rejects any available claim whose evidence lacks a public URL, retrieval time or `claim_span`.
The `official_website` claim value is the site URL as a plain string. Claims and evidence are emitted in
a fixed order.
