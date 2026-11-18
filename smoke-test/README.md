# 100-company smoke test

A clean run of the submitted agent, done the way the evaluator runs it: fresh clone of this repository at
commit `6f654925c4d94f5dd77f2ee3acc0e46f8f4e7091`, `uv sync`, then one command, with no pre-built data or caches.

```bash
uv sync
uv run python -m signalpost run --organisations organisations.txt --output-dir smoke/ --state-dir state/ --run-id smoke-A
```

`organisations.txt` is 100 companies drawn at random (seed 20260928) from the public 411,160-company list.

| | |
|---|---|
| Results | 100 of 100 envelopes, all with a terminal state; validation passed |
| Evidence | 5473 of 5473 evidence records carry a public source URL, retrieval time and the exact supporting text (`claim_span`) |
| Requests | 1680 in the batch + 1 one-time setup |
| Time | 5.5 min for the batch; first-run setup (register bulk download, lookup tables) adds a few minutes |
| Third-party cost | $0 |
| Available (companies of 100) | identity 100, financials 100, financial history 100, leadership 100, locations 100, description 98, website 18, group 19, profiles 10, activity 10, jobs 2, reviews 0 |

Checks on this commit:
- Determinism: a second fresh run of the same input gave identical facts for 100 of 100 companies
  and identical evidence (ids, sources, quotes) for 100 of 100.
- Quotes: 5,343 evidence texts were checked against the saved raw responses; 5,327 occur there word for
  word. The other 16 are e-mail-domain notes, address matches whose punctuation differs from the page, and
  texts the source encodes differently. Workforce quotes are OCR text of the scanned accounts copy (the
  page itself is an image). No birth date appears anywhere.
- Workforce from the annual-accounts copy (OCR): 92 of 100 companies. Activity: 10 companies.
- This run had no register bulk file: the download was abandoned as too slow, and every company was
  planned from its live registry entry instead.
- Run health (new in `run-report.json`): OK, not degraded.
- An immediate rerun with the same `--state-dir` reported **3 changes**, all for one company: its website
  (egonor.no) took over 20 seconds to answer in the first run and answered in the rerun, so the site, its
  contact e-mail and its description were newly found. No fact changed value.
- Audit: 0 problems (every published claim has evidence, no dangling or duplicate claims).

Files: `envelopes.jsonl` (one result per company), `run-report.json`, `requests.jsonl` (every outbound
request), and `site/` — the static viewer for these 100 profiles (open `site/index.html`).
