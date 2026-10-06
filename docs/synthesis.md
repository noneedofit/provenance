# Summary synthesis

> Development notes, written while this part was built (September 2026). Some details have changed
> since; `README.md`, `AGENT.md`, `CRAWLERS.md`, `SOURCES.md`, `IDENTITY_RESOLUTION.md`, `DATA_SCHEMA.md`,
> `REFRESH.md` and `LIMITATIONS.md` describe current behaviour.

`signalpost.synthesis.build_summary(envelope: Envelope) -> Summary` produces deterministic English
sentences from an envelope's claims only — no network calls, no LLM, same input always gives the same
output. `summary_markdown(envelope) -> str` renders the summary (using `envelope.summary` if already
populated, else calling `build_summary`) as Markdown for the viewer.

Every sentence carries `claim_ids` for at least one claim it is based on (invariant enforced structurally:
every sentence builder either returns `None` or a `SummarySentence` with a non-empty `claim_ids` list). The
`unknowns` list is the only exception — each entry names a family that is not `available` this run, with
its plain-language reason from that family's `FamilyState.reason` (or availability state if no reason was
given).

## Sentence coverage

1. **What it does** — `description.company_description` (quoted, truncated to 200 chars, "according to
   its website"), else `identity.nace_label` and/or `identity.statutory_purpose`.
2. **Legal form / founded / location** — `identity.legal_form`, `identity.founded_date`,
   `identity.municipality`.
3. **Size** — `identity.employees`, `financials.revenue` + `financials.operating_result` (with reporting
   period). (A revenue-trend sentence exists for `financial_history.revenue` claims, but the registry
   connector publishes only filed years, so it does not fire on real data.)
4. **Leadership** — CEO and chair, found via `leadership.role` claims whose role code is in
   `CEO_ROLE_CODES` (`DAGL`, `CEO`) / `CHAIR_ROLE_CODES` (`LEDE`, `CHAIR`) — `DAGL`/`LEDE` are the actual
   Brønnøysund `/roller` role codes for daglig leder / styreleder.
5. **Footprint** — verified `website.official_website`, count of `profiles.profile` claims, count of
   `locations.workplace` claims.
6. **Hiring / activity** — count of `jobs.job_posting` claims (with the most recent title if present), and
   the most recent `activity.activity_item` by `effective_date`.
7. **What changed** — one sentence per `envelope.changes` entry: "Since the previous check (run
   `<run_id>`), the `<field>` changed from `<previous>` to `<current>`" (or a new/ended phrasing when one
   side is `None`), citing the change's `claim_id`.
8. **Unknowns** — one entry per family in `envelope.families` whose `availability != "available"`.

A sparse shell (no website/jobs/activity at all) falls back to a single sentence built from whatever
`identity` claims exist, e.g. *"X is a holding company registered in Y; no website, jobs, or public
activity were found in permitted sources."*

## Money formatting

`format_money(value, currency="NOK")` accepts either a raw number or the `{"amount", "currency"}` shape
used by financial claim values. Abbreviates to `M`/`B` above one million/one billion, otherwise renders
with thousands separators, e.g. `NOK 1.50B`, `NOK 2.30M`, `NOK 4,200`.

## Known gaps / assumptions

Field names are centralised in `FIELD_ALIASES` at the top of `src/signalpost/synthesis.py` — this module
tries every alias for a given logical field before giving up, so the orchestrator can add the registry/
web/activity connectors' real field spellings there without touching sentence logic. Assumed field names:
`legal_name`, `legal_form`, `founded_date`, `status`, `nace_label`, `statutory_purpose`, `municipality`,
`employees` (family `identity`); `revenue`, `operating_result`, `annual_result` (families `financials` /
`financial_history`); `role` with `value={"role_code", "name"}` or `value_key="{role_code}|{name}"`
(family `leadership`); `official_website` (family `website`); `profile` (family `profiles`);
`company_description` (family `description`); `location` (family `locations`); `job_posting` with
`value={"title": ...}` (family `jobs`); `activity_item` with `value={"title": ...}` and `effective_date`
set (family `activity`).
