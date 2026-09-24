# Submission artifacts

- `manifest-1000.jsonl` — the 1,000 companies profiled for the entry (rows copied from the public universe
  file, plus a `selection` field). 800 are a uniform random sample (seed 20260924), representative of the
  daily random batches; 200 are staffed companies (≥5 employees) with a registry website (seed 20260925),
  sampled to show full-depth profiles in the viewer. No company appears twice.
- `organisation-numbers-1000.txt` — the same organisation numbers, one per line.
- Profiles: produced by running the evaluator command over this manifest in 10 batches of 100
  (each batch respects the 2,000-request / 45-minute budget); see `profiles/` once generated.
