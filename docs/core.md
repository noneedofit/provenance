# W1 core: http, snapshots, registry, planner, pipeline, report, validate, cli

## What this workstream owns
`src/signalpost/{http,snapshots,registry,planner,pipeline,report,validate,cli,__main__}.py`.

## Run it

```
uv run python -m signalpost run \
  --organisations data/baseline20.jsonl \
  --output-dir out --state-dir state --run-id my-run \
  --bulk data/brreg-enheter.csv.gz
```

Input formats: JSONL (`{"organisation_number": "..."}` per line), a JSON list, or a `.txt` file with one
organisation number per line. `--bulk` accepts a gzip CSV (the Brreg bulk download, despite the `.csv`
name) or a plain CSV; if omitted, the CLI looks for `./data/brreg-enheter.csv.gz` / `./data/brreg-enheter.csv`,
and falls back to downloading the bulk file once (counted as 1 request).

Outputs, under `--output-dir`: `envelopes.jsonl` (one `models.Envelope` per input, same order, even on
crash), `run-report.json` (request/family-state counts, runtime percentiles, budget stats), `requests.jsonl`
(the full request log: purpose, org, url, status, requests_used, elapsed_ms). Under `--state-dir`:
`snapshots/` (content-addressed gzip blobs + JSON sidecars) and `profiles/{org}.json` (written by W5's
`refresh.apply_refresh`, if that module is present — otherwise the pipeline just stamps
`first_observed_at`/`last_observed_at` with the current run time and skips persistence).

Validate an envelopes file:

```
uv run python -m signalpost validate --envelopes out/envelopes.jsonl --organisations data/baseline20.jsonl
```

`prepare` delegates to `signalpost.caches.prepare:main` (W2); if that module isn't present yet it exits 2
with a message rather than crashing.

## http.py — BudgetedHttpClient

Stdlib-only (`urllib`), implements `context.HttpClient`. Key behaviors:
- **Manual redirects**: a custom `HTTPRedirectHandler` that never auto-follows, so every hop is
  individually SSRF-checked, robots-checked and budget-charged. `Response.requests_used` sums every hop
  and retry for that one `.get()` call; `Response.redirect_chain` lists every URL visited.
- **SSRF guard**: reuses `norway_company_agent.website.assert_public_url` (blocks non-http(s), loopback,
  private, link-local, multicast) with a local fallback if that module is ever removed.
- **Robots.txt**: cached per `(scheme, host)`. The robots.txt fetch itself is charged to the calling org
  as one request (matches BUILD_SPEC). Pass `respect_robots=False` for official/keyless APIs
  (`data.brreg.no`, NAV feeds, Wikidata, YouTube feeds) — this is the caller's responsibility, `http.py`
  does not hardcode a host allowlist. An unreachable robots.txt defaults to allow (same policy as the
  starter kit).
- **Retries**: one retry per hop on `429` / `5xx` / timeout; the retry is charged like any other request.
- **Concurrency**: a `threading.Semaphore` per host, default 2, `data.brreg.no` gets 6
  (`HOST_CONCURRENCY_OVERRIDES`).
- **Budget**: `Budget` is a thread-safe global counter with a hard cap (`--max-requests`, default 1900) and
  soft per-org allowances (`allocate`/`charge`/`remaining`) that borrow from the shared pool — an org can
  spend past its own allocation as long as the global hard cap isn't hit. When the hard cap is hit,
  `.get()`/`.post_json()` return a `Response` with `error="budget_exhausted"` instead of raising.
  `client.remaining(org)` is what `pipeline.py` uses to decide whether a company's uncalled families should
  be marked `not_run` or `request_budget`.
- **Snapshots**: on `snapshot=True` and a 2xx status, the (already gzip-decoded) body is written to the
  `SnapshotStore` and `Response.snapshot_ref` is set to its sha256.
- **DNS**: `dns_resolves(host)` runs `socket.getaddrinfo` in a small thread pool with a 3s timeout, cached
  per host; DNS lookups are not charged against the request budget.
- **Request log**: `client.request_log` — a list of `RequestLogEntry(purpose, org, url, status,
  requests_used, elapsed_ms, error)`; `pipeline.py` dumps this to `requests.jsonl` and `report.py`
  aggregates it into `requests_by_purpose`.

## snapshots.py — SnapshotStore

Content-addressed gzip store: `<state-dir>/snapshots/<sha256[:2]>/<sha256>.gz` + a `.json` sidecar with
`{sha256, url, retrieved_at, content_type, bytes}`. `put()` dedupes by sha256 (identical bytes are written
once); `get(snapshot_ref)` returns the decompressed body or `None`.

## registry.py — bulk loader, live Brreg fetchers, `RegistryConnector`

- `load_bulk(path, orgs) -> dict[org, row]`: streams the bulk CSV (gzip by content-sniffing the magic
  bytes, not by extension — a plain CSV also works, used in tests), keeping only the requested orgs. ~1M
  rows in the real file; this only holds the rows you ask for.
- `RegistryConnector` (`name = "registry"`, families `identity, financials, financial_history, leadership,
  locations, group`). Per company: always fetches `enheter/{org}` and `enheter/{org}/roller`
  (`respect_robots=False` — these are official APIs); fetches `underenheter?overordnetEnhet={org}` for
  tier T1+ (T0 uses the business address only, per BUILD_SPEC); fetches
  `regnskapsregisteret/regnskap/{org}` (latest financials) at every tier; fetches
  `regnskap/aarsregnskap/kopi/{org}/aar` (the filed-years list) only for T2/T3. If the live entity fetch
  fails (network error, non-2xx other than 404/410, or budget exhaustion), identity falls back to the bulk
  row with `source_class="official_registry_bulk"`. 404/410 on any endpoint → `not_available`; a caught
  exception or non-retryable HTTP failure → `failed`; `budget_exhausted` → `failed` reason
  `request_budget`.
  - Financial claims: one claim per metric (`revenue`, `operating_result`, `annual_result`,
    `total_assets`, `equity`, `total_debt`) with `reporting_period` and `{amount, currency}`, from the
    latest `SELSKAP`-type record (falls back to whatever record exists if none is tagged `SELSKAP`); plus
    `accounting_type` (`{type, small_enterprise}`) and `audit_status` (raw `revisjon` block) claims.
  - `financial_history`: one `filed_years` claim (list of year strings); `not_applicable` when the legal
    form isn't in the categorically-filing-obliged set (`AS, ASA, BRL, BBL, STI, SF, VPFO`) and the years
    endpoint didn't return a usable list.
  - Leadership: one `role` claim per role instance, `value_key = "{role_code}|{normalized name}"`,
    `status="withdrawn"` when Brreg marks the role `avregistrert`; one `roles_last_changed` claim per role
    group keyed by group code, from `sistEndret`. `fodselsdato` is never read.
  - Locations: one `workplace` claim per subunit (`value_key` = subunit org number: name, street,
    postcode, city, employees, nace), plus a single `business_address` claim from the parent entity's
    `forretningsadresse`.
  - Group: a `parent` claim only when the entity body carries `overordnetEnhet`/`hovedenhet` (i.e. this
    org is itself registered as a subunit of another). `konsernstruktur` is not queried by this workstream
    (not in the BUILD_SPEC pipeline-order endpoint list) — group is `not_available` for standalone
    companies. **Known gap**: no group data for companies that hold subsidiaries rather than being one.
- `registry_facts(ctx) -> dict`: normalized name, aliases (historic names + subunit trade names), street,
  postcode, city, phones (normalized digits), email, email_domain, website, `role_holders` (current
  leadership names), and the subunit list — published into `ConnectorResult.shared["registry_facts"]` for
  W3/W4 to use as identity-corroboration and candidate-generation input.

## planner.py — budget tiers

`classify(bulk_row, caches=None, allowances=None) -> TierResult(tier, allowance, signals)`. Pure function
of the bulk row (+ optional `caches.email_domains.is_shared()` to avoid tiering a company up just because
it shares an accountant's email domain). T0 shell ~5 req, T1 small ~15, T2 staffed ~30, T3 large ~45
(`planner.DEFAULT_ALLOWANCES`, overridable). Bankrupt/liquidating companies are capped down one notch
(T2→T1, T1/T0→T0) since a defunct company rarely has an external footprint worth chasing.

## pipeline.py — `run_batch(...)`

```python
run_batch(
    orgs: list[str], *, output_dir: str, state_dir: str, run_id: str,
    bulk_path: str | None = None, caches_dir: str | None = None,
    max_requests: int = 1900, deadline_s: int = 2400, workers: int = 12,
    connectors: list[Connector] | None = None,
    bulk_rows: dict[str, dict] | None = None,  # test hook: inject rows instead of a bulk CSV
) -> dict  # the run-report.json contents
```

Builds one `CompanyContext` per org (tier from `planner.classify`, `budget.allocate(org, tier.allowance)`),
then runs connectors in order `[registry, web, activity]`. `web`/`activity` are imported lazily
(`signalpost.web.connector:WebConnector`, `signalpost.activity.connector:ActivityConnector`) inside a
`try/except ImportError`, so the pipeline runs standalone today and picks up W3/W4 automatically once they
land — **no code change needed in this workstream when they merge**. A connector that raises is caught
per-connector (logged into `envelope.errors`, other connectors still run); a company whose *context setup*
itself throws (e.g. a malformed bulk row) is caught by an outer handler that still emits exactly one
envelope with `terminal_status="failed"` and all families `reason="crash"` — inputs are never dropped.

Merge semantics: claims dedup by `claim_id` (last writer wins — connectors should not collide on the same
claim key across families), evidence dedup by `evidence_id`, `families` dict is a plain union (each
connector only reports states for its own families, so nothing is overwritten), `ctx.shared` is updated in
connector order so a later connector sees what an earlier one published (e.g. W3 reading
`registry_facts`).

After connectors run: `signalpost.synthesis.build_summary(envelope)` and
`signalpost.refresh.apply_refresh(envelope, state_dir)` are called if importable (both optional —
`ImportError` is swallowed; without `refresh`, claims just get `first_observed_at = last_observed_at =
now`). Any family not reported by a connector is filled with `FamilyState(availability="failed", reason=...)`
where reason is `"deadline"` (global deadline passed), `"request_budget"` (`client.remaining(org) <= 0`), or
`"not_run"`. `sections` is built from `models.SECTIONS`. Concurrency is a `ThreadPoolExecutor` sized by
`--workers` (default 12); the global deadline (`--deadline-seconds`, default 2400s = 40 min) is checked
before each company starts and before each connector call — once past it, remaining companies still get an
envelope, just with `failed`/`deadline` families, never a dropped input.

Outputs are written atomically (`tempfile.mkstemp` + `os.replace`) so a crash mid-write never corrupts
`envelopes.jsonl`.

**Not implemented**: checkpoint/resume (marked "nice to have" in BUILD_SPEC) — a killed run must be
restarted from scratch, though `refresh`'s stored profiles mean re-running is still idempotent for
unchanged sources.

## report.py / validate.py

`report.build_report(...)` aggregates `family_state_counts` (per family, per availability),
`requests_by_purpose`, `total_requests`, `runtime_ms_p50/p95` (over `operations.runtime_ms`, the elapsed
wall time from run start when that company's envelope was finalized), `deadline_hits`,
`budget_exhausted_count`, `cache_versions` (passed through from `caches.meta` if `--caches` was given), and
`third_party_cost_usd: 0.0`.

`validate.validate_envelope(env) -> list[str]` checks: every family in `models.FAMILIES` has a
`FamilyState`; every `available` claim has a non-`None` value and ≥1 evidence id; every referenced evidence
id exists in `env.evidence`; every claim id referenced from `env.sections` exists in `env.claims`.
`validate.validate_envelopes(envelopes, expected_organisations=None)` additionally checks uniqueness, exact
count, input order, and zero dropped inputs when given the original organisation-number list. `python -m
signalpost validate --envelopes ... --organisations ...` runs this and exits 1 on failure.

## Known gaps / notes for other workstreams
- `group` family only fires when the queried org is itself a registered subunit (`overordnetEnhet`); a
  parent company's subsidiaries are not surfaced (would need `konsernstruktur`, not in the BUILD_SPEC
  endpoint list for W1).
- `robots_disallowed`/`budget_exhausted` are distinguishable via `Response.error`, but `registry.py`
  currently reports `financials`/`financial_history`/`leadership` as `"failed"` (not `"blocked"`) when
  robots disallows an official endpoint — this should never happen in practice since registry endpoints
  are always called with `respect_robots=False`, but W3/W4 should map `robots_disallowed` to
  `availability="blocked"` for their own connectors.
- `BudgetedHttpClient._request` retries only once per hop, not once per whole `.get()` call, matching "1
  retry on 429/5xx/timeout" read as a per-attempt policy.
- Live smoke-test numbers are in the final report / commit history, not duplicated here to avoid drift.
