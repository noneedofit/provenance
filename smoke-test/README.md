# 100-company smoke test

A clean run of the submitted agent, done the way the evaluator runs it: fresh clone of this repository at
commit `87cadd38985948644fdefe8a62b4e44766459071`, `uv sync`, then one command, with no pre-built data or caches.

```bash
uv sync
uv run python -m signalpost run --organisations organisations.txt --output-dir smoke/ --state-dir state/ --run-id smoke-A
```

`organisations.txt` is 100 companies drawn at random (seed 20260928) from the public 411,160-company list.

| | |
|---|---|
| Results | 100 of 100 envelopes, all with a terminal state; validation passed |
| Evidence | 5508 of 5508 evidence records carry a public source URL, retrieval time and the exact supporting text (`claim_span`) |
| Requests | 1649 in the batch + 1 one-time setup |
| Time | 5.6 min for the batch; first-run setup (register bulk download, lookup tables) adds a few minutes |
| Third-party cost | $0 |
| Available (companies of 100) | identity 100, financials 100, financial history 100, leadership 100, locations 100, description 98, website 19, group 19, profiles 10, activity 10, jobs 1, reviews 0 |

Checks on this commit:
- Determinism: a second fresh run of the same input gave identical facts for 99 of 100 companies
  and identical evidence (ids, sources, quotes) for 99 of 100. The one difference is a company website
  (vossvekter.no) that did not answer the second run in time; that run reported it as not found.
- Quotes: 5,377 evidence texts were checked against the saved raw responses; 5,361 occur there word for
  word. The other 16 are e-mail-domain notes, address matches whose punctuation differs from the page, and
  texts the source encodes differently. Workforce quotes are OCR text of the scanned accounts copy (the
  page itself is an image). No birth date appears anywhere.
- Workforce from the annual-accounts copy (OCR): 92 of 100 companies. News articles naming the company
  (Bing News): part of activity 10.
- This run had no register bulk file: the download was abandoned as too slow, and every company was
  planned from its live registry entry instead.
- An immediate rerun with the same `--state-dir` reported **0 changes** (idempotent refresh).
- Audit: 0 problems (every published claim has evidence, no dangling or duplicate claims).

Files: `envelopes.jsonl` (one result per company), `run-report.json`, `requests.jsonl` (every outbound
request), and `site/` — the static viewer for these 100 profiles (open `site/index.html`).
