# Data for the JBOX-built Signalpost site

`companies.json` is a compact export of the 100-company smoke test (`../smoke-test/envelopes.jsonl`) for the
site built with JBOX: per company, a summary whose sentences point to facts, the facts themselves, and for
every fact its source URL, retrieval time and the exact source text. Contact details (register e-mail and
phone, site contact e-mail) are left out; a register quote that carries one keeps only the verbatim
fields that hold the fact (for example the address block). Regenerate it with:

```bash
uv run python jbox/make_data.py smoke-test/envelopes.jsonl jbox/companies.json
```

Displayed values are written for visitors ("Yes" for a true register flag, English role names, plain
sentences for what could not be found, newest news first); every source quote stays verbatim.
