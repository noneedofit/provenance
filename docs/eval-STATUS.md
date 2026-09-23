# Gold-labelling batch status (checkpoint, 2026-09-23)

Working checkpoint while the independent gold-label collection (`eval/data/gold_web.jsonl`) is
being assembled from six parallel research batches. This file is a scratch tracker, not the
final documentation — once all batches land, merge `eval/data/gold_chunk_*.jsonl` into
`eval/data/gold_web.jsonl` (dedup by `organisation_number`; batch 1's rows, already merged in
directly, take priority for the orgs they cover), delete the chunk files, fold the final counts
into `docs/eval.md`'s `<!-- GOLD_SUMMARY_PLACEHOLDER -->` section, and delete this file.

## Batch status

| Batch | Source population | Companies | Output file | Status | Rows |
|---|---|---|---|---|---|
| 1 | `dev.jsonl`, staffed, top 26 by employees | 26 | `eval/data/gold_chunk_1.jsonl` (also merged directly into `gold_web.jsonl`) | **done** | 26 |
| 2 | `dev.jsonl`, staffed, remaining 25 | 25 | `eval/data/gold_chunk_2.jsonl` | **running** | 0 (not yet written) |
| 3 | `stress.jsonl`, bucket `staffed_with_registry_website` | 21 | `eval/data/gold_chunk_3.jsonl` | **done** | 21 |
| 4 | `stress.jsonl`, bucket `staffed_without_registry_website` | 21 | `eval/data/gold_chunk_4.jsonl` | **running** | 0 (not yet written) |
| 5 | `stress.jsonl`, bucket `franchise_like_name` | 22 | `eval/data/gold_chunk_5.jsonl` | **done** | 22 |
| 6 | `stress.jsonl`, bucket `asa` (first 16) | 16 | `eval/data/gold_chunk_6.jsonl` | **done** | 16 |

Batch 1's raw chunk file (`gold_chunk_1.jsonl`) is a duplicate of what's already in
`gold_web.jsonl` for those same 26 orgs (that batch was originally researched directly rather
than via a surviving agent run) — safe to drop during the final merge, no new information there.

Total labelled once batches 2 and 4 land: 26 + 25 + 21 + 21 + 22 + 16 = **131** companies
(target was >=120).

## Batches still running / how to resume

- **Batch 2** (dev-staffed, remaining 25 companies: 937718284, 921788207, 890251102, 912077640,
  994974432, 923479937, 839227302, 914569532, 933260151, 933936279, 932263750, 962969372,
  919593989, 930824208, 998588367, 960564146, 929589025, 999293379, 996599191, 919206039,
  926962221, 930302430, 933489175, 934263774, 919807911). Output: `eval/data/gold_chunk_2.jsonl`.
  If it stops before writing: relaunch only for orgs not already present in `gold_web.jsonl` or
  in a partial `gold_chunk_2.jsonl` (check with `jq -r .organisation_number
  eval/data/gold_chunk_2.jsonl` if a partial file exists), using the shared prompt template in
  the W7 agent's final report (Brreg API + live web verification, `exact`/`related`/
  `none_found`/`uncertain`, jobs.checked=true only for the first 14 orgs).
- **Batch 4** (stress `staffed_without_registry_website`, 21 companies: 976183606, 915503705,
  926043129, 822031412, 923779094, 990773394, 923081313, 918064648, 966242647, 929640683,
  824608202, 920677800, 864367992, 916128983, 988271632, 912386112, 935324939, 976024842,
  929633962, 967539082, 915513778). Output: `eval/data/gold_chunk_4.jsonl`. Same resume approach;
  jobs.checked=false for all of these; watch for namesake collisions on generic names (ELINE AS,
  LBU AS, SK STÅL AS, etc.).

## Independence rules (apply to any resume/relaunch)

- Research the live web and `https://data.brreg.no/enhetsregisteret/api/enheter/{org}` directly.
  Never read the agent's own output while labelling.
- `exact` requires a decisive signal (org number on the page) or >=2 independent corroborating
  signals (address, phone, exact legal name, etc.) with no conflict.
- A parent/franchise/group site is `related`, never `exact`, for the subsidiary/franchisee.
- Genuinely unresolved cases are `uncertain`, not a guess.
- Output schema: see `eval/gold.py` (`GoldRecord`) — one ND-JSON line per company, fields
  `organisation_number`, `name`, `website` (status/url/relationship/evidence_url/evidence_note),
  `profiles`, `jobs` (checked/active/note), `confidence`, `notes`, `labeller: "independent"`,
  `labelled_at`.
