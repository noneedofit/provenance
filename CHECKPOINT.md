# Checkpoint — 23 Sep 2026

## Merged on `main` (133 tests passing)
| Workstream | What | Notes |
|---|---|---|
| W1 core | http budget client, snapshots, registry connector, planner, pipeline, validator, CLI | live: 20 companies, 67 requests, 36 s, validation passes |
| W4 activity | NAV jobs (from cache), ATS feeds, site RSS/news, reviews=not_available | YouTube RSS disallowed by robots → profile URL only (decision: respect robots) |
| W5 refresh + summary | claim diffing, carry-forward, typed changes, cited summary | replay 10/10, rerun on live data = 0 false changes |
| W6 viewer | static site: directory, company pages, compare, evidence drawers, Q&A | orchestrator fixed directory→company link bug + link-integrity test |
| Orchestrator fixes | summary rendering of structured values, employees null guard, both phones, validation in run report, installable package | |

Run: `uv run python -m signalpost run --organisations <file> --output-dir out/x --state-dir state --run-id x --bulk ../data/brreg-enheter.csv`
Site: `uv run python -m signalpost.viewer.build --envelopes out/x/envelopes.jsonl --out out/site`

## In flight (branches in `.claude/worktrees/`)
| Workstream | Branch | State |
|---|---|---|
| W2 caches | `worktree-agent-ae128225d59c085cb` | code written; prepare build (NAV/Wikidata/email/aliases) into `../cache` — see docs/caches-STATUS.md on branch |
| W3 web identity | `worktree-agent-adad349271db94046` | 2 commits; 150-company probe running; trap regressions requested (below) — see docs/web-STATUS.md on branch |
| W7 eval + gold | `worktree-agent-a52fa5a05ea0afa83` | splits, scorer, report committed; gold batches 1,3,5,6 done (85 companies), 2 & 4 running — see docs/eval-STATUS.md on branch |

## Trap regressions W3 must implement (from gold labels)
- Hijacked registry domain (GAASA AS → casino spam) → rejected.
- Namesake: JOKER AS is a fishing company, joker.no is the chain → never exact.
- Chain/franchise domains (joker, coop, rema, kiwi, europris, 7-eleven, thon, …) → related/ambiguous.
- Booking/marketplace/directory platforms (fixit, timma, finn, proff, 1881, facebook…) → never candidates.
- Parent/umbrella sites (samfundet.no, hav.no, assemblin.com, bilfinger) → related unless our org number appears.
- Valid: subpage on shared domain that shows our org number (DNT Nord-Trøndelag on dnt.no) → exact with page URL.

## Next steps
1. Merge W3, W2, W7 after QA (conftest conflicts: keep minimal conftest, move fakes to *_testkit.py).
2. Run 100-company daily-like batch + eval.score against gold; check wrong-company = 0, requests ≤ 1,700.
3. Refresh check on second run; build site; fix gaps; publish caches tarball.
4. Rewrite author of first 3 commits (ad9b4f7, 6c5b312, 99e2114) to `noneedofit <78908173+noneedofit@users.noreply.github.com>` before any push.
5. Submission prep (repo, manifest of 1,000, run command, cost $0) — target v1 ~4 Oct.
Pending external: Builderr reply on JBOX eligibility.

## Paused 23 Sep 2026 (user session limit). Resume order: batch 4 → batch 2 → W3 → W2 → W7 merge.

## Agent registry (resume with SendMessage to the ID; each keeps its full context)
| ID | Role | Worktree branch | Last known state |
|---|---|---|---|
| ae128225d59c085cb | W2 caches | worktree-agent-ae128225d59c085cb | PAUSED — committed f82728e (+ docs/caches-STATUS.md) |
| adad349271db94046 | W3 web identity | worktree-agent-adad349271db94046 | PAUSED — WIP e9c173b; was wiring franchise-domain gate into verify.py |
| a52fa5a05ea0afa83 | W7 eval + gold (owns batch agents) | worktree-agent-a52fa5a05ea0afa83 | PAUSED — committed cb8963e (docs/eval-STATUS.md); merge batches 2 & 4 when they finish |
| a37f7f246cbaf25f4 | gold-label batch 2 (dev staffed, remaining 25) | writes into W7 worktree eval/data/ | PAUSED mid-research, nothing written yet |
| ae865681d581710b5 | gold-label batch 4 (staffed, no registry website) | writes into W7 worktree eval/data/ | PAUSED right before writing its file — resume first |
| ae9b7198a1ad94738 | W1 core | merged | done |
| a4b12181a63b2a55b | W4 activity | merged | done |
| a8fabd4eec4370c17 | W5 refresh + summary | merged | done |
| a71c067fcd38e751c | W6 viewer | merged | done |
| a6127b58bd083a3eb, ad921e98b0eeb73b7, afeae3ef010fbf096, a91cbef7527f8caa9 | gold batches 1, 5, 6, 3 | W7 worktree | done |

If an agent can't be resumed (new session), relaunch it from its original brief in BUILD_SPEC.md and point it at
its branch: "check out branch <branch>, read its *-STATUS.md, continue".

## Session 2 (24 Sep) — relaunched (old agent transcripts not resumable across sessions)
| ID | Role | Where |
|---|---|---|
| a95540b1e80007e26 | gold batch 2 relaunch | writes eval/data/gold_chunk_2.jsonl in W7 worktree |
| a93d72bf610f0252f | gold batch 4 relaunch | writes eval/data/gold_chunk_4.jsonl in W7 worktree |
| aa73440218cea562f | W3 web continue | worktree agent-adad349271db94046 |
| a59847804506c3fa6 | W2 caches continue | worktree agent-ae128225d59c085cb |
W7 merged to main (eval harness + gold batches 1,3,5,6). Baseline (registry only, 84 gold orgs): 377 requests,
0 wrong-company, website recall 0 → W3 must beat this.

## 24 Sep night (autonomous)
- Gold set complete: 131 companies (74 exact / 16 related / 31 none / 10 uncertain) in eval/data/gold_web.jsonl.
- W2 caches merged (email_domains, aliases, wikidata built in ../cache). NAV feed cache is stale by design
  (feed walks from 2023) → replaced by live NAV lookup: arbeidsplassen search by name + feedentry org-number
  confirmation (agent a1f9fe0b6965c3a99, worktree branch, in progress).
- Scorer fixed (dict URLs, same-site matching, related-as-exact = wrong, profiles unverified ≠ wrong).
- Integrated main+W3 on 131 gold: 1,545 requests, website precision 97.8%, recall 35.6%, 1 wrong (Xledger),
  proxy 40/100 (refresh/UX unmeasured). W3 agent aa73440218cea562f fixing Xledger + big-company recall.

## 24 Sep daytime (autonomous)
- Fixed: NAV 429 storm starved registry → per-company registry budget reserve + NAV circuit breaker.
- Fixed: registry_facts now uses bulk e-mail/phones/homepage (live API omits them).
- Merged W3 + recall branch: gold (131) website precision ~98–100%, recall 51%, 0 wrong-company;
  daily-like 100: ~470 requests, ~3–4 min, refresh rerun 0 changes (refresh 20/20 on proxy).
- Gold label corrected: HONG KONG PALACE AS → sushime.no (org number on site).
- Docs written (README, AGENT, CRAWLERS, IDENTITY_RESOLUTION, DATA_SCHEMA, REFRESH, EVAL, LIMITATIONS, SOURCES).
- Submission manifest: submission/manifest-1000.jsonl (800 random + 200 staffed-with-website), runner
  scripts/run_submission_batches.sh; profiles generating into submission/profiles/submission-20260924/.
- User (24 Sep): no contact details anywhere; no GitHub push yet (no repo); Builderr no reply on JBOX. Author rewrite still needed before any future push. Hand-checks use Haiku agents.
