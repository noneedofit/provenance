# 100-company smoke test

A clean run of the submitted agent, done the way the evaluator runs it: fresh clone of this repository at
commit `44945cf41ec8b0c68a47ebf4c8fac36832e01411`, `uv sync`, then one command, with no pre-built data or caches.

```bash
uv sync
uv run python -m signalpost run --organisations organisations.txt --output-dir smoke/ --state-dir state/ --run-id smoke-100
```

`organisations.txt` is 100 companies drawn at random (seed 20260928) from the public 411,160-company list.

| | |
|---|---|
| Results | 100 of 100 envelopes, all with a terminal state; validation passed |
| Evidence | 4901 of 4901 evidence records carry a public source URL, retrieval time and the exact supporting text (`claim_span`) |
| Requests | 1358 in the batch + 1 one-time setup |
| Time | 6.4 min for the batch; first-run setup (register bulk download, lookup tables) adds a few minutes |
| Third-party cost | $0 |
| Available (companies of 100) | identity 100, financials 100, financial history 100, leadership 100, locations 100, description 98, website 18, group 19, profiles 8, activity 1, jobs 1, reviews 0 |

Checks on this commit:
- Determinism: a second fresh run of the same input gave identical facts for 99 of 100 companies
  and identical evidence (ids, sources, quotes) for 99 of 100. The one difference is a network one: in
  this run the DNS lookup for rema.no returned a non-public address, which the SSRF guard refuses, so the
  related (brand) site of JA HANDEL AS was not checked; the other run and the rerun found it. No run
  published it as the company's website.
- An immediate rerun with the same `--state-dir` reported **1 change**: that same related site, now
  reachable (otherwise an idempotent refresh).
- Quotes: 4,862 evidence texts were checked against the saved raw responses. Every register, accounts,
  group, job-ad and feed quote occurs there word for word (4,846 in all). The other 16 are 6 e-mail-domain
  notes ("registry e-mail domain X is the site's domain"), 5 address matches whose punctuation differs from
  the page, and 5 texts the page encodes differently (escaped JSON-LD, a spaced org number, a malformed
  link). No birth date appears anywhere.
- This run had no register bulk file: the download was abandoned as too slow (0.38 MB/s), and every
  company was planned from its live registry entry instead.
- Audit: 0 problems (every published claim has evidence, no dangling or duplicate claims).

Files: `envelopes.jsonl` (one result per company), `run-report.json`, `requests.jsonl` (every outbound
request), and `site/` — the static viewer for these 100 profiles (open `site/index.html`).
