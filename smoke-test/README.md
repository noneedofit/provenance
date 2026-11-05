# 100-company smoke test

A clean run of the submitted agent, done the way the evaluator runs it: fresh clone of this repository at
commit `0a12e5a15ff82852c14dd4a921cae06549d616d0`, `uv sync`, then one command, with no pre-built data or caches.

```bash
uv sync
uv run python -m signalpost run --organisations organisations.txt --output-dir smoke/ --state-dir state/ --run-id smoke-A
```

`organisations.txt` is 100 companies drawn at random (seed 20260928) from the public 411,160-company list.

| | |
|---|---|
| Results | 100 of 100 envelopes, all with a terminal state; validation passed |
| Evidence | 5402 of 5402 evidence records carry a public source URL, retrieval time and the exact supporting text (`claim_span`) |
| Requests | 1326 in the batch + 1 one-time setup |
| Time | 4.8 min for the batch; first-run setup (register bulk download, lookup tables) adds a few minutes |
| Third-party cost | $0 |
| Available (companies of 100) | identity 100, financials 100, financial history 100, leadership 100, locations 100, description 98, website 19, group 19, profiles 10, activity 4, jobs 1, reviews 0 |

Checks on this commit:
- Determinism: a second fresh run of the same input gave identical facts for 100 of 100 companies
  and identical evidence (ids, sources, quotes) for 100 of 100.
- Quotes: 5,363 evidence texts were checked against the saved raw responses. Every register, accounts,
  group, job-ad and feed quote occurs there word for word (5,347 in all). The other 16 are 6 e-mail-domain
  notes ("registry e-mail domain X is the site's domain"), 5 address matches whose punctuation differs from
  the page, and 5 texts the source encodes differently (escaped JSON-LD, NAV feed JSON, a spaced org number,
  a register website value, a malformed link). No birth date appears anywhere.
- This run had no register bulk file: the download was abandoned as too slow, and every company was
  planned from its live registry entry instead.
- An immediate rerun with the same `--state-dir` reported **0 changes** (idempotent refresh).
- Audit: 0 problems (every published claim has evidence, no dangling or duplicate claims).

Files: `envelopes.jsonl` (one result per company), `run-report.json`, `requests.jsonl` (every outbound
request), and `site/` — the static viewer for these 100 profiles (open `site/index.html`).
