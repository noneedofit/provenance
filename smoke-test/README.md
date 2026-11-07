# 100-company smoke test

A clean run of the submitted agent, done the way the evaluator runs it: fresh clone of this repository at
commit `0275bd83cda5bf0b2a024c622bbe7437ee130b1c`, `uv sync`, then one command, with no pre-built data or caches.

```bash
uv sync
uv run python -m signalpost run --organisations organisations.txt --output-dir smoke/ --state-dir state/ --run-id smoke-A
```

`organisations.txt` is 100 companies drawn at random (seed 20260928) from the public 411,160-company list.

| | |
|---|---|
| Results | 100 of 100 envelopes, all with a terminal state; validation passed |
| Evidence | 5500 of 5500 evidence records carry a public source URL, retrieval time and the exact supporting text (`claim_span`) |
| Requests | 1654 in the batch + 1 one-time setup |
| Time | 8.1 min for the batch; first-run setup (register bulk download, lookup tables) adds a few minutes |
| Third-party cost | $0 |
| Available (companies of 100) | identity 100, financials 100, financial history 100, leadership 100, locations 100, description 98, website 19, group 19, profiles 10, activity 10, jobs 1, reviews 0 |

Checks on this commit:
- Determinism: a second fresh run of the same input gave identical facts for 92 of 100 companies
  and identical evidence (ids, sources, quotes) for 92 of 100. All 8 differences are one workforce figure each: one run
  read it from the annual-accounts copy, the other skipped it (no figure differed). The two runs were made at
  the same time and share Brreg's request limit for those copies; when the queue is longer than 30 s the agent
  skips the copy rather than wait.
- Quotes: 5,377 evidence texts were checked against the saved raw responses; 5,361 occur there word for
  word. The other 16 are e-mail-domain notes, address matches whose punctuation differs from the page, and
  texts the source encodes differently. Workforce quotes are OCR text of the scanned accounts copy (the
  page itself is an image). No birth date appears anywhere.
- Workforce from the annual-accounts copy (OCR): 84 of 100 companies. Activity: 10 companies, 7 of them
  with news articles naming the company (Bing News).
- This run had no register bulk file: the download was abandoned as too slow, and every company was
  planned from its live registry entry instead.
- An immediate rerun with the same `--state-dir` reported **0 changes** (idempotent refresh).
- Audit: 0 problems (every published claim has evidence, no dangling or duplicate claims).

Files: `envelopes.jsonl` (one result per company), `run-report.json`, `requests.jsonl` (every outbound
request), and `site/` — the static viewer for these 100 profiles (open `site/index.html`).
