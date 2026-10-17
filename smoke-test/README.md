# 100-company smoke test

A clean run of the submitted agent, done the way the evaluator runs it: fresh clone of this repository at
commit `d553b85d2498987562e23f65e9498ea32c6f7b0a`, `uv sync`, then one command, with no pre-built data or caches.

```bash
uv sync
uv run python -m signalpost run --organisations organisations.txt --output-dir smoke/ --state-dir state/ --run-id smoke-100
```

`organisations.txt` is 100 companies drawn at random (seed 20260928) from the public 411,160-company list.

| | |
|---|---|
| Results | 100 of 100 envelopes, all with a terminal state; validation passed |
| Evidence | 2892 of 2892 evidence records carry a public source URL, retrieval time and the exact supporting text (`claim_span`) |
| Requests | 1189 in the batch + 1 one-time setup |
| Time | 3.5 min for the batch; first-run setup (register bulk download, lookup tables) adds a few minutes |
| Third-party cost | $0 |
| Available (companies of 100) | identity 100, financials 100, financial history 100, leadership 100, locations 100, description 98, website 18, group 12, profiles 8, activity 2, jobs 0, reviews 0 |

Checks on this commit:
- Determinism: a second fresh run of the same input gave identical facts for 100 of 100 companies
  and identical evidence (ids, sources, quotes) for 100 of 100.
- An immediate rerun with the same `--state-dir` reported **1 change**: fossberg.no's contact page (the page that showed the brand site's other org number) failed with a network error on the rerun, so the related-site verdict could not be repeated and the related website was reported as no longer found. A live-site failure, not a code difference: both full runs gave identical facts.
- Audit: 0 problems (every published claim has evidence, no dangling or duplicate claims).

Files: `envelopes.jsonl` (one result per company), `run-report.json`, `requests.jsonl` (every outbound
request), and `site/` — the static viewer for these 100 profiles (open `site/index.html`).
