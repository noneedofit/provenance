# W2 caches — status checkpoint

Written mid-build, while a NAV `prepare` invocation is still running in the background. See
`docs/caches.md` for the full write-up once it's finished — this file is just the "what's done, what's
left, how to resume" snapshot for this checkpoint commit.

## Done

- `src/signalpost/caches/{__init__,store,email_domains,aliases,wikidata,nav,prepare}.py` — full API from
  BUILD_SPEC.md, all `None`-safe, `Caches.load` sub-millisecond.
- 32 unit tests (`uv run --with pytest pytest -q tests/signalpost`), all passing, no network, small
  fixtures under `tests/fixtures/caches/` (added a `.gitignore` exemption so those small `.csv.gz`/
  fixtures aren't swept up by the bulk-data ignore rules).
- `docs/caches.md` written (sources, licences, API, NAV feed findings, refresh cadence, known gaps).
- Real build run into `../cache` (outside the repo):
  - `email_domains.sqlite`: 1,175,168 bulk rows -> 127,744 domains, 13.1s build, 7.3MB.
  - `aliases.sqlite`: 864,903 subunit rows, 8.0s build, 100.8MB.
  - `wikidata.sqlite`: 10,279 orgs with a Wikidata item (10,939 raw SPARQL bindings), 25.9s build, 2.0MB.
  - All three verified end-to-end via `Caches.load` (real lookups: `is_shared("gmail.com")`, Posten's 133
    subunits, a Wikidata hit for org 974560569) and load-time measured at <1ms.
- Committed: `b56b247 Add W2 caches: email_domains, aliases, wikidata, nav`.

## In progress at checkpoint time

- `nav.sqlite`: a `prepare --skip-email-domains --skip-aliases --skip-wikidata --nav-max-seconds 600`
  invocation has been running in the background (PID 14687, started ~19:20, this checkpoint at ~19:27)
  walking the NAV feed forward from page 1 into `../cache/nav.sqlite`.
  Measured before this run: ~3.6-3.9s per feed page (server-latency-bound, not our rate limit), and the
  feed's first 100-121 pages are a one-time 2023-06-14 backfill burst (100,000+ events in ~4 minutes of
  `sistEndret` time) that is NOT representative of steady-state density — so total page count / total
  build time cannot be estimated by extrapolating early pages. Full findings and the reasoning are
  written up in `src/signalpost/caches/nav.py`'s module docstring and will be finalized with the run's
  actual numbers in `docs/caches.md`.

## Left to do

1. Let the current 10-minute-capped `prepare` invocation finish (or check its log at
   `/tmp/signalpost_build/nav_prepare.log`); record its reported `pages_fetched`, `active_seen`,
   `detail_fetches`, `elapsed_s` into `docs/caches.md`'s NAV section (replacing the placeholder language
   there) and into this checkpoint's follow-up commit.
2. Sanity-check `nav.sqlite` (`Caches.load(...).nav`, confirm `ads_for` and the `cursor`/`seen_status`
   tables are populated) the same way `email_domains`/`aliases`/`wikidata` were checked above.
3. Delete this file (`docs/caches-STATUS.md`) once `docs/caches.md` has the final NAV numbers — it's a
   checkpoint artifact, not a permanent doc.
4. Final commit + final report to the coordinator (files touched, API surface, cache sizes/row counts,
   NAV findings, known gaps, branch name).

## How to resume the NAV build yourself

```
cd signalpost-agent/.claude/worktrees/agent-ae128225d59c085cb
PYTHONPATH=src uv run python -m signalpost.caches.prepare \
  --cache-dir ../cache \
  --skip-email-domains --skip-aliases --skip-wikidata \
  --nav-max-seconds <N>
```

It resumes automatically from the saved `last_feed_page_id` cursor in `nav.sqlite` (that's the whole
point of the checkpointing) — running it again just continues the walk instead of restarting from page 1.
`(PYTHONPATH=src is needed because pyproject.toml has `package = false` and is orchestrator-owned, so
`uv run python -m signalpost...` needs `src/` on the path; a `tests/signalpost/conftest.py` does the same
for pytest.)`
