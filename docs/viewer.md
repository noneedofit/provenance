# W6 — static viewer

`src/signalpost/viewer/` turns `out/envelopes.jsonl` (one `models.Envelope` per line) into a fully static,
self-contained website: no CDN, no external fonts/scripts, no server-side code at view time. It works from
a plain `file://` URL and from any static file host (S3, GitHub Pages, `python -m http.server`, …).

## Build

```
python -m signalpost.viewer.build --envelopes out/envelopes.jsonl --out out/site [--state-dir state]
```

`--state-dir` is accepted for CLI parity with the rest of the pipeline but unused — the viewer only reads
the envelopes file; it never touches the refresh state store.

## Output layout

```
out/site/
  index.html            directory: search, filter, sort, CSV export, compare picker
  compare.html           side-by-side comparison of 2-4 companies
  companies/{org}.html   one fully pre-rendered profile page per company
  data/companies/{org}.json   the company's Envelope, pretty-printed (download target + audit trail)
  data/index.json        the directory rows as JSON (for anyone who wants to consume it programmatically)
  directory.csv           the full directory as CSV
  assets/style.css, assets/app.js   shared, local-only assets
```

### Why per-company HTML pages, not per-company fetched JSON

Company pages are pre-rendered at build time and carry all their content (including the evidence for
every fact) inline. This keeps the site working from `file://` — browsers block `fetch()` of local files
under `file://` due to CORS, so per-company data cannot be lazily fetched there. The **directory** page
instead embeds one compact JSON object per company as a `data-json="…"` attribute on its card (not a
network fetch), which is what JS reads client-side for search/filter/sort/compare — this scales the
directory to 1,000+ companies without any request at all, and still works offline. `compare.html` embeds
the same compact rows and lets the visitor tick 2-4 companies (or arrives pre-selected via
`?orgs=<org1>,<org2>,…` from the directory's "Compare selected" button, itself just a query string, not a
fetch).

## What each page shows

- **Directory** (`index.html`): search by name / org number / municipality / industry; filters for legal
  form, employee band, "has verified website", "hiring now", "has recent activity", "data in all 5 areas";
  sort by most data found (default), name, revenue, employees. Coverage badges per family use five
  distinct, accessible styles (`available` / `not_available` / `ambiguous` / `blocked` / `not_applicable` /
  `failed`) — a family that was never checked or found nothing is never rendered as if it were zero.
  Export: "download visible rows as CSV" (client-side, respects current filters) and a static
  `directory.csv` with every company.
- **Company page** (`companies/{org}.html`): header with legal name, brand (if any), org number linking to
  the live Brønnøysund API record and a brreg.no search, status/legal form/municipality/NACE; a deterministic
  summary with footnote links into the sections below; the seven required sections (legal identity & brand,
  annual accounts with an inline-SVG revenue trend sparkline, leadership & workplaces, website & profiles,
  hiring & activity, evidence & availability as a full family-state table, refresh & changes as a
  previous→current timeline with evidence on both sides); an "Ask this profile" box; JSON/print export.
  Every fact has a `<details class="source-toggle">` "source" control (keyboard accessible, works without
  JS, prints openly) that expands to the evidence: source URL, source class, retrieved-at, reporting period,
  a short content hash, extraction method, identity basis, and the exact supporting span text. Verified
  exact websites are tagged distinctly from related/ambiguous candidates (e.g. a franchise site showing the
  franchisor's org number) — the ambiguous case is never presented as the company's own site.
- **Compare** (`compare.html`): pick 2-4 companies, see revenue, result, employees, website, profiles,
  hiring, latest activity and data coverage side by side, each company name linking back to its full profile
  (and therefore its evidence).
- **Ask this profile**: a client-side, keyword-matched Q&A box (no LLM, no network call) that answers from
  the same JSON payload embedded in the page (`#profile-data`) — what the company does, who runs it, latest
  financials, whether it's hiring, recent activity, what changed. Every answer that has evidence cites it as
  numbered links to the source URLs; anything outside what the profile's evidence covers gets
  "Not found in checked sources", never an invented answer.

## Accessibility, responsiveness, printing

Mobile-first CSS (tested down to 360-375px), keyboard-navigable controls (native `<details>`/`<summary>`
for evidence drawers, visible focus rings), light/dark via `prefers-color-scheme` with distinct badge
palettes tuned for contrast in both, and a `@media print` stylesheet that hides navigation/toolbars and
keeps facts + evidence legible on paper. Dates render as a human string with the ISO value in the `title`
attribute (hover) and the `<time datetime>` attribute. Norwegian characters (æøå) are UTF-8 throughout.

## Robustness to real connector output

Claim values are not always plain strings. The real registry connector emits structured values for many
identity/leadership/locations/financials fields (dicts, lists, bools) — e.g. `identity/legal_form =
{"code": "AS", "label": "Aksjeselskap"}`, `identity/business_address = {street, postcode, city,
municipality, country}` (there is no separate top-level `municipality` claim — it lives inside the
address), `leadership/role = {"role_code", "role_label", "name"}`, `locations/workplace = {...}`,
`financial_history/filed_years = [...]`. Every render path goes through
`helpers.display_value(field, value)`, one generic, robust renderer that handles str/int/float/bool/dict/
list/None: money-shaped and address-shaped dicts get bespoke formatting, then `label`, then `name`, then
`count`, then `url`/`title` are preferred, with a readable "Key: value; …" fallback for anything else — so
an unmapped connector field can never crash the page. `helpers.plain_scalar()` is the equivalent for
directory rows / CSV / JS filter values, which must stay plain (unescaped, non-HTML) scalars rather than
rendered HTML. `helpers.first_available()` tries several candidate field names in order (e.g. `"nace"` vs.
the older synthetic `"nace_code"`/`"nace_description"` pair) so both connector shapes render identically.
`tests/fixtures/viewer/real_registry.jsonl` holds 5 companies taken verbatim from a real registry-connector
run, exercising these shapes end to end.

## Known gaps

- The client-side "most data found" sort ranks by count of `available` families only; it does not weight
  by how many claims each family contributed.
- The keyword-matched Q&A box is intentionally simple (substring matching over a fixed topic list) — it
  will occasionally miss an oddly-phrased question and fall back to "not found in checked sources" even
  when the profile does have a relevant claim under a different family than expected.
- CSV export only carries the directory-row fields (not the full claim/evidence set) — use the per-company
  JSON download for the full envelope.
- No visible light/dark toggle is offered; the site follows the OS/browser `prefers-color-scheme` only.

## Tests

`tests/signalpost/test_viewer.py` builds the site from `tests/fixtures/viewer/envelopes.jsonl` (six
companies constructed directly through the pydantic models: a rich profile with financials/roles/6
subunits/website/profiles/activity/changes, a small staffed firm with a verified site and a NAV job, a
franchise with an ambiguous related website, a pure holding shell, a company with failed/request_budget
families, and one with a full refresh change history) and asserts on the generated files: every family
renders, evidence links are present, no external script/link tags, valid index/company JSON and CSV, and a
synthetic envelope exercises the `blocked` availability state end-to-end. Run with:

```
uv run --with pytest pytest -q tests/signalpost
```

The build was also verified manually in a real browser (`python -m http.server` over the built `out/site`)
at desktop width and at a 375px mobile viewport: directory search/filter/sort, the evidence drawer toggle,
the revenue sparkline, the family-state table, the refresh/changes timeline, the Ask-this-profile box, and
the directory→compare selection flow all render and behave correctly in both.
