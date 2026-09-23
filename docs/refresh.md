# Refresh / change detection (W5)

`signalpost.refresh.apply_refresh(envelope, state_dir, *, now=None) -> Envelope` merges a freshly built
`Envelope` for one organisation with the previously stored profile for that organisation and returns an
updated `Envelope` ready to emit. It is the last step before `validate` in the pipeline (see BUILD_SPEC
step 5).

## State on disk

`<state_dir>/profiles/{org}.json`:

```json
{
  "organisation_number": "923609016",
  "last_envelope": { "...": "full Envelope dict, all claims status=current" },
  "history": [ "...claim dicts, status=superseded (replaced/ended) or withdrawn (aged out quietly)..." ],
  "history_evidence": [ "...evidence dicts referenced by history claims, kept so changes stay verifiable..." ],
  "change_log": [ "...change dicts, bounded to the most recent 200..." ],
  "run_ids": [ "run-1", "run-2" ]
}
```

Writes are atomic (tempfile + `os.replace`).

## Matching and classification

Claims are matched by `claim_id` (from `models.claim_key`), which is stable across runs for the same
organisation/family/field/value_key.

- **Same claim_id, same canonical value** (value + reporting_period) → unchanged. `first_observed_at` is
  carried over from the previous claim; `last_observed_at` is bumped to `now`. No `Change`.
- **Same claim_id, different value** → the previous claim is copied into `history` with
  `status="superseded"` (its evidence is retained in `history_evidence`), the current claim gets fresh
  `first_observed_at = last_observed_at = now`, and a `Change` is emitted. The change_type is:
  - `new_filing` — family is `financials`/`financial_history` and the reporting period advanced (or there
    was no previous period).
  - `changed_website` / `changed_description` / `identity_changed` — family is `website` / `description` /
    `identity`.
  - `changed_value` — everything else, including a same-period value restatement in a financial family.
- **New claim_id, multi-valued field** (`value_key` set: roles, jobs, locations, profiles, activity) →
  `new_role` / `new_job` / `new_location` / `new_profile` / `new_activity`.
- **New claim_id, single-valued field** (`value_key` is `None`) that previously had no claim at all (the
  family was `not_available`) → treated as a "changed from nothing" event, using the same type mapping as
  above (`previous_value=None`).
- **Previous current claim not present this run** — decided by the *current run's* `FamilyState` for that
  claim's family:
  - Family availability is `available` / `not_available` / `not_applicable` (i.e. the source actually got
    checked) → the item **ended**: `ended_role` / `closed_job` / `closed_location` / `removed_profile`
    (single-valued fields disappearing reuse the `changed_*` mapping with `current_value=None`). The claim
    moves to `history` as `superseded`, evidence retained.
  - Family availability is `failed` / `blocked` / `ambiguous`, or its `reason` mentions `deadline` /
    `request_budget` (source not actually rechecked) → the claim is **carried forward** unchanged into the
    output envelope (`status="current"`, `last_observed_at` untouched, a `note` of
    `"carried forward: source not re-checked"` appended once — not duplicated on repeat carries), an error
    entry is added to `envelope.errors`, and **no** `Change` is emitted.
  - Family is `activity` and was checked successfully → the item is assumed to have simply aged out of a
    feed/top-N window, not "ended": it is moved to `history` with `status="withdrawn"` and **no** `Change`
    is emitted. Only genuinely new activity items produce `new_activity`.

## Materiality

`Change.material` is always `True` for an emitted change (immaterial edits are filtered before a `Change`
is ever created): financial, website and role changes are always material; a `description` text change is
filtered out entirely (no claim update to `first_observed_at`, no `Change`) when the only difference is
whitespace, case or punctuation (`_normalize_text` in refresh.py).

## Idempotency

Running `apply_refresh` twice with identical input envelopes (same values, same claim_ids) produces zero
`Change`s the second time, and no duplicate claims, evidence, or history entries — verified by
`tests/signalpost/test_refresh.py` and by `scripts/run_refresh_replay_v2.py`. `Change.change_id` is
deterministic (`sha256(claim_id|type|previous|current)`), so a change detected in run N belongs to run N's
envelope only; the stored `change_log` is the durable record across runs (bounded to the last 200 entries).

## Pure diff function

`diff_envelopes(prev: Envelope, curr: Envelope) -> list[Change]` is the side-effect-free comparison used
internally by `apply_refresh` (called once `curr` already has carry-forward claims merged in). It can also
be used directly to compare any two envelopes for the same organisation without touching state.

## Replay harness

`scripts/run_refresh_replay_v2.py` loads `tests/fixtures/refresh/scenarios.json` (10 scenarios: unchanged
rerun, revenue change with a new reporting period, CEO replaced, board member added, job opened/closed,
subunit closed, website changed, whitespace-only description edit, a failed source that must carry
forward, and an activity item aging out of a feed window), runs each scenario's prev → curr through
`apply_refresh` twice, and reports precision/recall of change_type detection plus an idempotency flag —
current result: precision 1.0, recall 1.0, idempotent across all scenarios.

## Known gaps / assumptions

- Field/family names for claims (`legal_form`, `role`, `official_website`, `company_description`,
  `job_posting`, `activity_item`, `location`, role codes `DAGL`/`LEDE`/`MEDL`/...) are assumed pending the
  registry/web/activity connectors landing; see `FIELD_ALIASES`/role-code constants centralised in
  `src/signalpost/synthesis.py` (refresh.py itself is field-name agnostic — it only relies on `family`,
  `value_key`, `value`, `reporting_period`).
- A single-valued field disappearing while its family remains checked-and-available (e.g. website goes
  down) is treated as a `changed_*` event to `None` rather than a dedicated "removed" type, since the spec
  only names `ended_role`/`closed_job`/`closed_location`/`removed_profile` for multi-valued items.
- `not_applicable` is treated as a successful check (so a claim disappearing under it is "ended", not
  carried forward) since it represents a deliberate determination, not a failure to check.
