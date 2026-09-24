# Refresh: scheduling, snapshots, diffs

Implemented in `src/signalpost/refresh.py` (`apply_refresh`, `diff_envelopes`). Refresh is not a
separate command — it is the automatic last step of `pipeline.run_batch` before `validate`, triggered
whenever a run points `--state-dir` at a directory that already has a stored profile for a company.

## Scheduling

There is no built-in scheduler. A refresh is simply: run `signalpost run` again with the **same
`--state-dir`** as before (a new `--run-id`, and optionally a different `--organisations` list — a
company not in this run's batch is untouched, its stored profile unchanged). How often to do this (daily,
per the competition's daily-batch scoring) is an operational decision outside the pipeline itself.

## State layout (`<state-dir>`)

```
<state-dir>/
  profiles/{organisation_number}.json   # last envelope + full claim history, per company
  snapshots/                            # gzip, content-addressed raw response bodies
```

### `profiles/{org}.json`

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

Writes are atomic: a temp file in the same directory, then `os.replace` (`refresh._save_profile`,
`pipeline._atomic_write`). A crash mid-write never leaves a corrupt or partially-written profile.

### `snapshots/`

`snapshots.SnapshotStore` (`src/signalpost/snapshots.py`) stores every successful (`2xx`) raw response
body, gzip-compressed, content-addressed by its sha256, referenced from `Evidence.snapshot_ref`. Prior
snapshots are never deleted by a later run — each run only adds new content-addressed blobs, so the full
evidentiary history behind every claim, past and present, stays on disk and auditable.

## Matching and classification

Claims are matched by `claim_id` (`models.claim_key`, stable across runs for the same
organisation/family/field/value_key — see `DATA_SCHEMA.md`).

- **Same `claim_id`, same canonical value** (value + reporting period) → unchanged.
  `first_observed_at` carries over from the previous claim; `last_observed_at` bumps to `now`. No
  `Change`.
- **Same `claim_id`, different value** → the previous claim moves to `history` with
  `status="superseded"` (its evidence retained in `history_evidence`), the current claim gets fresh
  `first_observed_at = last_observed_at = now`, and a `Change` is emitted. `change_type`:
  - `new_filing` — family `financials`/`financial_history` and the reporting period advanced (or there
    was no previous period).
  - `changed_website` / `changed_description` / `identity_changed` — family `website` / `description` /
    `identity`.
  - `changed_value` — everything else, including a same-period value restatement in a financial family.
- **New `claim_id`, multi-valued field** (`value_key` set — roles, jobs, locations, profiles, activity) →
  `new_role` / `new_job` / `new_location` / `new_profile` / `new_activity`.
- **New `claim_id`, single-valued field** that previously had none at all → the same `changed_*` mapping,
  `previous_value=None`.
- **Previous current claim absent this run** — decided by *this run's* `FamilyState` for that claim's
  family:
  - Family was actually checked (`available` / `not_available` / `not_applicable`) → the item **ended**:
    `ended_role` / `closed_job` / `closed_location` / `removed_profile` (single-valued fields reuse the
    `changed_*` mapping with `current_value=None`). Moves to `history` as `superseded`, evidence
    retained.
  - Family was **not** actually rechecked (`failed` / `blocked` / `ambiguous`, or its reason mentions
    `deadline` / `request_budget`) → the claim is **carried forward unchanged** into the output envelope
    (`status="current"`, `last_observed_at` untouched, a `note` of `"carried forward: source not
    re-checked"` appended once), an error entry is added to `envelope.errors`, and **no** `Change` is
    emitted. This is the mechanism that keeps a company's known facts from silently vanishing just
    because one run hit a rate limit or the deadline.
  - Family is `activity` and was checked successfully → assumed to have simply aged out of a feed/top-N
    window, not "ended": moved to `history` with `status="withdrawn"`, **no** `Change` emitted. Only
    genuinely new activity items produce `new_activity`.

## Materiality

`Change.material` is always `True` for a change that is actually emitted — immaterial edits never
become a `Change` in the first place. A `description` text change is filtered out entirely (no claim
update, no `Change`) when the only difference from the previous value is whitespace, case, or punctuation
(`refresh._normalize_text` / `_is_immaterial_text_change`).

## Idempotency

Running the pipeline twice against the same sources with the same `--state-dir` produces **zero**
`Change`s the second time, and no duplicate claims, evidence, or history entries — `Change.change_id` is
deterministic (`sha256(claim_id|type|previous|current)`), so re-detecting the same diff never creates a
second event. Verified by `tests/signalpost/test_refresh.py` and by the replay harness below.

## Replay harness

`scripts/run_refresh_replay_v2.py` loads `tests/fixtures/refresh/scenarios.json` (10 scenarios: unchanged
rerun, revenue change with a new reporting period, CEO replaced, board member added, job opened/closed,
subunit closed, website changed, whitespace-only description edit, a failed source that must carry
forward, and an activity item aging out of a feed window), runs each scenario's prev → curr through
`apply_refresh` twice, and reports precision/recall of `change_type` detection plus an idempotency flag.

## Pure diff function

`diff_envelopes(prev: Envelope, curr: Envelope) -> list[Change]` is the side-effect-free comparison
`apply_refresh` uses internally (called once `curr` already has carry-forward claims merged in). It can
be used directly to compare any two envelopes for the same organisation without touching state — useful
for ad hoc "what changed between these two files" checks outside a full pipeline run.

## Known gaps / assumptions

- A single-valued field disappearing while its family remains checked-and-available (e.g. the website
  goes down) is reported as a `changed_*` event to `None`, not a dedicated "removed" type — the spec only
  names `ended_role`/`closed_job`/`closed_location`/`removed_profile` for multi-valued items.
- `not_applicable` is treated as a successful check (a claim disappearing under it is "ended," not
  carried forward), since it represents a deliberate determination (e.g. tier T0 has no HTTP budget for
  `activity`), not a failure to check.
