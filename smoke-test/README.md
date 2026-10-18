# 100-company smoke test

A clean run of the submitted agent, done the way the evaluator runs it: fresh clone of this repository at
commit `19a02dd5577284a528f32510c098d739750fd57d`, `uv sync`, then one command, with no pre-built data or caches.

```bash
uv sync
uv run python -m signalpost run --organisations organisations.txt --output-dir smoke/ --state-dir state/ --run-id smoke-100
```

`organisations.txt` is 100 companies drawn at random (seed 20260928) from the public 411,160-company list.

| | |
|---|---|
| Results | 100 of 100 envelopes, all with a terminal state; validation passed |
| Evidence | 4309 of 4309 evidence records carry a public source URL, retrieval time and the exact supporting text (`claim_span`) |
| Requests | 1279 in the batch + 1 one-time setup |
| Time | 5.2 min for the batch; first-run setup (register bulk download, lookup tables) adds a few minutes |
| Third-party cost | $0 |
| Available (companies of 100) | identity 100, financials 100, financial history 100, leadership 100, locations 100, description 98, website 18, group 19, profiles 8, activity 1, jobs 0, reviews 0 |

Checks on this commit:
- Determinism: a second fresh run of the same input gave identical facts for 99 of 100 companies
  and identical evidence (ids, sources, quotes) for 99 of 100. The one difference is fossberg.no, which
  dropped connections in both runs (on its contact page in one, its homepage in the other) while two runs
  hit it at once, so its related-site verdict could be reached in only one of them.
- Quotes: 4,178 evidence quotes were checked against the saved raw responses; every register, accounts,
  group, job-ad and feed quote occurs there word for word. The other 18 are 11 related/group-site notes
  ("site shows a different organisation number ..."), 5 address matches whose punctuation differs from the
  page, and 2 texts the page encodes differently (an escaped JSON-LD description, a malformed link).
  No birth date appears anywhere.
- An immediate rerun with the same `--state-dir` reported **0 changes** (idempotent refresh).
- Audit: 0 problems (every published claim has evidence, no dangling or duplicate claims).

Files: `envelopes.jsonl` (one result per company), `run-report.json`, `requests.jsonl` (every outbound
request), and `site/` — the static viewer for these 100 profiles (open `site/index.html`).
