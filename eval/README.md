# eval — local proxy scorer and independent gold labels (W7)

This package is the local yardstick we use to decide whether a change to the agent is worth
keeping, before it ever touches the organiser's hidden pooled collection. It has three parts:

1. **`splits.py`** — frozen, stable-hash `dev` / `val` / `test` (300/200/200) and `stress` (150)
   samples carved out of `data/signalpost-universe.jsonl.gz` (411,160 rows), plus `daily_like()`
   for rehearsing the organiser's uniform 100-company daily batch. Deterministic: rerunning
   `python -m eval.splits` reproduces byte-identical files. No organisation number appears in
   more than one split.
2. **`gold.py`** + **`data/gold_web.jsonl`** — an *independently* researched ground-truth
   collection for the external families (website, profiles, jobs) on the `dev` split's staffed
   companies plus the `stress` set. "Independent" means labelled by researching the live web and
   the Brreg API directly, never by reading our own agent's output — that's what lets the scorer
   catch a wrong-company publication instead of grading the agent against its own mistakes.
3. **`score.py`** / **`report.py`** — `score()` computes envelope-contract validity, per-family
   coverage, precision/recall vs gold, a pooled-recall proxy, refresh checks (given a previous
   run's envelopes) and budget checks (given `run-report.json`), and rolls them into a proxy
   total out of 100 using the organiser's category weights. `report.py` compares two `score()`
   outputs and applies a PROMOTE / HOLD / REJECT rule (promote only with no precision regression).

## Quickstart

```bash
# (re)build the frozen splits from the universe file (only needed if the universe file changes)
uv run python -m eval.splits --universe ../data/signalpost-universe.jsonl.gz

# score a run's envelopes against the gold collection
uv run python -m eval.score \
  --envelopes out/envelopes.jsonl \
  --gold eval/data/gold_web.jsonl \
  --previous out/prev-envelopes.jsonl \
  --report out/run-report.json \
  --out eval-report.json

# compare a challenger run against a baseline and get a promotion decision
uv run python -m eval.report --baseline eval-report-baseline.json --challenger eval-report-new.json
```

## Files

- `splits.py` — split construction + `daily_like(seed)`.
- `gold.py` — `GoldRecord` pydantic schema, `load_gold`/`write_gold`, `summarize`.
- `score.py` — `score(envelopes_path, gold_paths, *, previous_envelopes=None, report=None)`.
- `report.py` — `compare(baseline, challenger) -> {"decision": "PROMOTE"|"HOLD"|"REJECT", ...}`.
- `data/dev.jsonl`, `data/val.jsonl`, `data/test.jsonl`, `data/stress.jsonl` — frozen splits.
- `data/gold_web.jsonl` — the independent gold-label collection (see `docs/eval.md` for
  methodology, label counts, and traps found while labelling).

## What the scorer does and does not prove

`score()` is a **local optimization proxy**, not a prediction of the organiser's leaderboard
score: it uses a small independently-labelled sample, not the organiser's pooled hidden
collection, and several checks (UX, some of synthesis) are only partial or placeholder. Use it
to rank our own changes — see `docs/eval.md` for the exact caveats per component.
