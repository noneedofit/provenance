#!/usr/bin/env bash
# Produce the 1,000 submission profiles in 10 batches of 100 (each batch within the daily budget),
# then merge envelopes and build the viewer site.
set -euo pipefail
cd "$(dirname "$0")/.."
RUN_ID="${1:-submission-$(date -u +%Y%m%d)}"
BULK="${BULK:-../data/brreg-enheter.csv}"
CACHES="${CACHES:-../cache}"
OUT="submission/profiles/$RUN_ID"
STATE="submission/state"
mkdir -p "$OUT/batches"
split -l 100 -d -a 2 submission/organisation-numbers-1000.txt "$OUT/batches/orgs-"
for f in "$OUT"/batches/orgs-*; do
  n="${f##*-}"
  uv run python -m signalpost run --organisations "$f" --output-dir "$OUT/batches/b$n" \
    --state-dir "$STATE" --run-id "$RUN_ID-b$n" --bulk "$BULK" --caches "$CACHES"
done
cat "$OUT"/batches/b*/envelopes.jsonl > "$OUT/envelopes.jsonl"
uv run python -m signalpost validate --envelopes "$OUT/envelopes.jsonl" --organisations submission/organisation-numbers-1000.txt
uv run python -m signalpost.viewer.build --envelopes "$OUT/envelopes.jsonl" --out "$OUT/site"
echo "done: $OUT"
