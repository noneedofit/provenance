# 100-company smoke test

A clean run of the submitted agent, done the way the evaluator runs it: fresh clone of this repository at
commit `67ddc7b983e42049431251bfd85cb8e460db9991`, `uv sync`, then one command, with no pre-built data or
caches.

```bash
uv sync
uv run python -m signalpost run --organisations organisations.txt --output-dir smoke/ --state-dir state/ --run-id smoke-100
```

`organisations.txt` is 100 companies drawn at random (seed 20260928) from the public 411,160-company list.

| | |
|---|---|
| Results | 100 of 100 envelopes, all `completed`; validation passed |
| Requests | 954 in the batch + 2 one-time setup (bulk file download, Wikidata) |
| Time | 6.7 min for the batch; 10.9 min wall including first-run setup |
| Third-party cost | $0 |
| Available | identity, leadership, locations, financials, financial history: 100 each; description 98 (registry activity or website text), website 15, profiles 8, activity 4 |

Files: `envelopes.jsonl` (one result per company), `run-report.json`, `requests.jsonl` (every outbound
request), and `site/` — the static viewer for these 100 profiles (open `site/index.html`).
