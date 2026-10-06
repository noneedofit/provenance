"""Live NAV arbeidsplassen job-postings connector, keyless, backed by a shared feed index.

## History / why this shape

The first version of this connector queried `arbeidsplassen.nav.no/stillinger/api/search` once per
company. That endpoint rate-limits aggressively per IP: a live run against the 131-company gold set
measured 93 companies ending `jobs=failed` ("nav_search_rate_limited") and zero companies with any
verified postings - per-company search cannot work at batch scale, no matter how much the request is
throttled or retried (both were tried).

The fix (measured live 2026-09-24, see `nav_feed.py`'s module docstring for the full write-up): NAV's
feed API (`pam-stilling-feed.nav.no`) supports `GET /api/v1/feed` with header
`If-Modified-Since: <HTTP-date>`, which jumps straight to the page containing that time instead of the
feed's 2023 start. Walking forward from `now - 60 days` reaches the live tip in ~180 requests and about
ten minutes - a one-time cost for the whole run (or, with a persistent `--state-dir`, a one-time cost
ever - later runs resume from the saved cursor and only walk the handful of new pages since). This
module now:

1. Builds (or resumes) that shared index **once per run**, via `prepare()` (called by `pipeline.py`
   before any company is processed; `run()` also lazily calls it as a fallback so the connector still
   works standalone/in tests). See `nav_feed.NavFeedIndex` / `nav_feed.walk_feed`.
2. Per company: matches the index's currently-ACTIVE ads by normalized business name against the legal
   name, registry aliases/subunit names, and `caches.aliases.names(org)` trade names - all local, no
   HTTP. For each match (capped per tier, newest-changed first), fetches
   `GET pam-stilling-feed.nav.no/api/v1/feedentry/{uuid}` with `Authorization: Bearer <token>` (the
   public token: last non-empty line of `GET .../api/publicToken`, cached and refreshed once on 401) to
   read `ad_content.employer.orgnr` and confirm it against this org number or a subunit's - never
   publishes a job claim on a name match alone.

The search endpoint is no longer used in the default path at all (not even behind a flag - the old
per-company search code, its throttle, and its 3-consecutive-429 circuit breaker have been deleted; see
git history on this file for that implementation if it's ever needed again).

`NavLiveConnector` (name="nav", families=()) still publishes zero claims/FamilyStates itself - it fills
`ctx.shared` for `activity/nav_jobs.py` to turn into claims:
- `nav_ads`: list of verified ad dicts (organisation number confirmed against this org or a subunit).
- `nav_homepages`: normalized employer homepages from verified ads, each with the source uuid.
- `nav_checked`: {"checked": bool, "state": "ok"|"not_applicable"|"failed", "reason": str, "checked_at"}.
  When the feed index hasn't yet reached the live tip of the feed (a fresh state-dir mid-walk, or the
  per-run page/time cap was hit before catching up), `reason` says so explicitly on a zero-match result
  - a company with zero matches from an incomplete index is still reported honestly, never as a
  confident "no postings" (BUILD_SPEC: never turn absence into zero, and never a false negative either).
- `nav_total_matching_hits`: count of index ads whose business name matched (verified or not).

Runs before `web` (pipeline order: registry -> nav -> web -> activity) so `web` can use verified NAV
homepages as website candidates, and `activity` (which runs after `web`) turns `nav_ads` into
`jobs/job_posting` claims.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from ..context import CompanyContext
from ..models import ConnectorResult, utc_now
from ._common import make_evidence, norm_title
from .nav_feed import (
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_SECONDS,
    DEFAULT_WINDOW_DAYS,
    FEED_URL,
    NavFeedIndex,
    load_snapshot,
    snapshot_as_of,
    walk_feed,
    walk_feed_segmented,
)

TOKEN_URL = "https://pam-stilling-feed.nav.no/api/publicToken"
FEEDENTRY_URL_TMPL = "https://pam-stilling-feed.nav.no/api/v1/feedentry/{uuid}"
AD_URL_TMPL = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
SEARCH_URL = "https://arbeidsplassen.nav.no/stillinger/api/search"

DETAIL_CAP = {"T1": 3, "T2": 5, "T3": 8}
LIVE_TIERS = ("T1", "T2", "T3")

# Limited search fallback for companies the feed index can't see at all (import-sourced ads that never
# generate a feed event - see docs/activity.md's "Recall gap" section for the live-measured diagnosis).
# Deliberately small and cautious: this endpoint rate-limits hard at batch scale (measured: 93/131 gold
# companies failed when every company searched once), so only a handful of the highest-value companies
# (by registered employee count) get one serialized, jittered, breaker-protected search per run.
EMPLOYEE_THRESHOLD = 5          # only companies with at least this many registered employees are candidates
# Off by default: whether a rate-limited search succeeds depends on timing, which would make identical
# runs differ. SIGNALPOST_NAV_SEARCH_MAX=<n> enables up to n searches per run.
SEARCH_FALLBACK_MAX = int(__import__("os").environ.get("SIGNALPOST_NAV_SEARCH_MAX", "0") or 0)
SEARCH_MIN_INTERVAL_S = 4.0     # minimum spacing between searches (serialized across every company)
SEARCH_JITTER_MAX_S = 2.0       # extra random jitter added on top of the minimum spacing
SEARCH_BREAKER_CONSECUTIVE_429 = 2   # trip after this many consecutive 429s; stop searching for the run
SEARCH_HISTORY_COOLDOWN_DAYS = 7     # prefer companies not searched within this many days (rotation)

_LEGAL_SUFFIX_TOKENS = {
    "as", "asa", "sa", "da", "ans", "ba", "ks", "iks", "nuf", "enk", "sf", "esek", "brl", "sti", "sam",
}


def _strip_legal_suffix(name: str) -> str:
    """Human-readable name with a trailing Norwegian legal-form token removed."""
    tokens = str(name or "").strip().split()
    while tokens and tokens[-1].strip(".,").casefold() in _LEGAL_SUFFIX_TOKENS:
        tokens.pop()
    stripped = " ".join(tokens)
    return stripped or str(name or "").strip()


def _normalize_name(name: Any) -> str:
    """Casefold, strip diacritics/punctuation/legal suffixes, collapse whitespace (reuses `norm_title`,
    which already does exactly this for cross-source job-title matching elsewhere in this package, and
    is also what `nav_feed.NavFeedIndex` uses to normalize each ad's business name).
    """
    return norm_title(name)


def _normalize_homepage(value: Any) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    return value.rstrip("/")


def _name_overlaps(candidate_norm: str, target_norms: set[str], *, min_overlap: float = 0.6) -> bool:
    """Exact or fuzzy (token-overlap) match, same threshold/shape as `nav_feed.NavFeedIndex.match_fuzzy`
    - used to filter search-fallback hits, which aren't in the sqlite index so can't use that method.
    """
    if candidate_norm in target_norms:
        return True
    cand_tokens = set(candidate_norm.split())
    if not cand_tokens:
        return False
    for target in target_norms:
        target_tokens = set(target.split())
        if not target_tokens:
            continue
        overlap = cand_tokens & target_tokens
        if not overlap:
            continue
        if len(overlap) / min(len(cand_tokens), len(target_tokens)) >= min_overlap:
            return True
    return False


class NavLiveConnector:
    name = "nav"
    families: tuple[str, ...] = ()

    def __init__(
        self, *, window_days: int = DEFAULT_WINDOW_DAYS, max_feed_pages: int = DEFAULT_MAX_PAGES,
        max_feed_seconds: float = DEFAULT_MAX_SECONDS, match_cap: dict[str, int] | None = None,
        search_min_interval: float = SEARCH_MIN_INTERVAL_S, search_jitter_max: float = SEARCH_JITTER_MAX_S,
    ) -> None:
        self._window_days = window_days
        # Bundled active-ads snapshot (SIGNALPOST_NAV_SNAPSHOT=0 walks the whole window live instead).
        self._use_snapshot = os.environ.get("SIGNALPOST_NAV_SNAPSHOT", "1").strip() != "0"
        self._max_feed_pages = max_feed_pages
        self._max_feed_seconds = max_feed_seconds
        self._match_cap = match_cap or DETAIL_CAP
        # Overridable (defaults are the live-measured-safe values) so unit tests can disable the
        # deliberate wall-clock throttling/jitter between search-fallback requests.
        self._search_min_interval = search_min_interval
        self._search_jitter_max = search_jitter_max

        self._token_lock = threading.Lock()
        self._token: str | None = None

        self._prepare_lock = threading.Lock()
        self._prepare_started = False
        self._prepared_event = threading.Event()
        self._feed_index: NavFeedIndex | None = None
        self._feed_stats: dict[str, Any] = {}
        self._feed_evidence: Any = None

        # Search fallback (see the constants above): selection is decided once, at prepare-time, from
        # the bulk rows (registered employees + legal name) and the freshly-built feed index.
        self._search_fallback_orgs: dict[str, str] = {}  # org -> query name (the legal name, suffix-stripped)
        self._search_lock = threading.Lock()
        self._last_search_ts = 0.0
        self._search_consecutive_429 = 0
        self._search_breaker_tripped = False
        self._searches_used = 0

    # -- token -------------------------------------------------------------------------------------

    def _get_token(self, client: Any, errors: list[dict] | None, *, force: bool = False) -> str | None:
        with self._token_lock:
            if self._token is not None and not force:
                return self._token
            resp = client.get(
                TOKEN_URL, org=None, purpose="nav_live_token", accept="text/plain",
                respect_robots=True, snapshot=False,
            )
            if not resp.ok:
                if errors is not None:
                    errors.append({"stage": "nav_live_token", "error": resp.error or f"http_{resp.status}"})
                return None
            lines = [ln.strip() for ln in resp.text().splitlines() if ln.strip()]
            if not lines:
                if errors is not None:
                    errors.append({"stage": "nav_live_token", "error": "empty_token_response"})
                return None
            self._token = lines[-1]
            return self._token

    def _fetch_feedentry(self, client: Any, org: str, uuid: str, token: str):
        url = FEEDENTRY_URL_TMPL.format(uuid=uuid)
        return client.get(
            url, org=org, purpose="nav_live_feedentry", accept="application/json",
            respect_robots=True, headers={"Authorization": f"Bearer {token}"},
        )

    def _confirm_subunit(self, client: Any, org: str, sub_orgnr: str, errors: list[dict]) -> Any:
        """Evidence that `sub_orgnr` is a registered subunit of `org` (Enhetsregisteret, live), or None."""
        url = f"https://data.brreg.no/enhetsregisteret/api/underenheter/{sub_orgnr}"
        resp = client.get(url, org=org, purpose="registry_subunit_confirm", accept="application/json", respect_robots=False)
        if not resp.ok:
            if resp.status not in (404, 410):
                errors.append({"stage": "registry_subunit_confirm", "orgnr": sub_orgnr, "error": resp.error or f"http_{resp.status}"})
            return None
        try:
            body = json.loads(resp.text())
        except Exception:
            return None
        if str(body.get("overordnetEnhet") or "") != str(org):
            return None
        return make_evidence(
            source_url=resp.final_url or url, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
            http_status=resp.status, source_class="official_registry", retrieved_at=resp.retrieved_at,
            content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref, extraction_method="brreg_subunit_parent_v1",
            span=f"$.overordnetEnhet={org}", access_policy="NLOD-2.0",
        )

    # -- shared feed index (built once per run) -----------------------------------------------------

    def start_prepare(
        self, client: Any, state_dir: str | None = None, bulk_rows: dict[str, dict] | None = None,
    ) -> threading.Thread | None:
        """Synchronously claim the "build the shared feed index" slot (idempotent: a second/concurrent
        call is a no-op, returning `None`), then run the actual walk in a background thread and return
        it. `pipeline.run_batch` calls this - claiming the slot synchronously, on the main thread, before
        any company is submitted to the worker pool, is what guarantees no company thread can race ahead
        and call the same-thread fallback (`prepare()`, via `_ensure_prepared`) with the wrong (`None`,
        in-memory, no persistence) `state_dir` before this real one has had a chance to start.

        `bulk_rows` (org -> raw Brreg bulk CSV row, the same dict `pipeline.run_batch` already loaded)
        is used, once the feed index is ready, to select this run's search-fallback candidates - see
        `_select_search_fallback`.
        """
        with self._prepare_lock:
            if self._prepare_started:
                return None
            self._prepare_started = True
        t = threading.Thread(target=self._do_prepare, args=(client, state_dir, bulk_rows), daemon=True)
        t.start()
        return t

    def prepare(self, client: Any, state_dir: str | None = None, bulk_rows: dict[str, dict] | None = None) -> None:
        """Build (or resume, from `state_dir`) the shared active-ads feed index once for the whole run,
        blocking the caller until it's ready. If another caller already claimed the slot (e.g.
        `pipeline.run_batch` via `start_prepare`), this just waits for that work instead of repeating it
        - safe to call from every company's thread (`run()`'s lazy fallback does exactly that).
        """
        with self._prepare_lock:
            already_claimed = self._prepare_started
            if not already_claimed:
                self._prepare_started = True
        if already_claimed:
            self._prepared_event.wait()
            return
        self._do_prepare(client, state_dir, bulk_rows)

    def _do_prepare(self, client: Any, state_dir: str | None, bulk_rows: dict[str, dict] | None = None) -> None:
        """The actual feed walk (plus search-fallback candidate selection). Callers must have already
        claimed `_prepare_started` (via `start_prepare` or `prepare`) before calling this.
        """
        try:
            index = NavFeedIndex.open(state_dir)
            token_errors: list[dict] = []
            get_token = lambda force=False: self._get_token(client, token_errors, force=force)  # noqa: E731
            if index.get_cursor() is None:
                # Fresh state: seed from the bundled active-ads snapshot and walk only the feed since it was
                # taken (the whole window when there is no usable snapshot), in parallel time slices. Ads
                # closed since the snapshot are removed by that walk, and every match is still confirmed
                # live (feedentry) before anything is published.
                now = datetime.now(timezone.utc)
                as_of = snapshot_as_of() if self._use_snapshot and index.count() == 0 else None
                if as_of is not None and now - as_of > timedelta(days=self._window_days):
                    as_of = None  # older than the window: its closed ads could never be removed; walk live
                if as_of is not None:
                    as_of = load_snapshot(index)
                if as_of is not None:
                    days = max(0.0, (now - as_of).total_seconds() / 86400)
                    stats = walk_feed_segmented(
                        client, get_token, index, since=as_of, segments=max(1, min(6, math.ceil(days / 2))),
                        max_pages=self._max_feed_pages, max_seconds=self._max_feed_seconds,
                    )
                    stats["snapshot_as_of"] = as_of.isoformat()
                else:
                    stats = walk_feed_segmented(
                        client, get_token, index, window_days=self._window_days,
                        max_pages=self._max_feed_pages, max_seconds=self._max_feed_seconds,
                    )
            else:
                # Persistent state: resume from the saved cursor (only the pages since the last run).
                stats = walk_feed(
                    client, get_token, index,
                    window_days=self._window_days, max_pages=self._max_feed_pages, max_seconds=self._max_feed_seconds,
                )
            if token_errors:
                stats["errors"] = list(stats.get("errors") or []) + [e.get("error", "token_error") for e in token_errors]
            stats["complete"] = index.is_complete()
            stats["ad_count"] = index.count()
            self._feed_index = index
            self._feed_stats = stats
            self._feed_evidence = make_evidence(
                source_url=FEED_URL, source_class="public_job_feed", retrieved_at=utc_now(),
                extraction_method="nav_feed_index_v1", access_policy="NLOD-2.0 (NAV)",
                content_sha256=index.content_sha256(),
                # Stable text: run statistics (ads indexed, pages walked) change with feed timing and belong
                # in the family reason, not in the evidence quote.
                span=f"active_ads_indexed: NAV job feed, ads active within the last {self._window_days} days",
            )
            if bulk_rows:
                try:
                    self._search_fallback_orgs = self._select_search_fallback(bulk_rows, index)
                except Exception:  # selection must never block the run
                    self._search_fallback_orgs = {}
        except Exception as exc:  # a broken feed walk must never take the whole run down
            self._feed_stats = {"errors": [f"prepare_crashed:{type(exc).__name__}: {exc}"], "ad_count": 0, "complete": False}
            self._feed_index = None
        finally:
            self._prepared_event.set()

    def _select_search_fallback(self, bulk_rows: dict[str, dict], index: NavFeedIndex) -> dict[str, str]:
        """Pick at most `SEARCH_FALLBACK_MAX` companies for the search fallback: registered employees
        (bulk row `antallAnsatte`) >= `EMPLOYEE_THRESHOLD`, the feed index has no match (exact or fuzzy)
        for their bulk-row legal name already, ranked by employees descending. Companies not searched in
        the last `SEARCH_HISTORY_COOLDOWN_DAYS` are preferred over recently-searched ones (rotation) -
        recently-searched companies only fill remaining capacity if there aren't enough fresh candidates.
        Returns {org: query_name}.
        """
        now = utc_now()
        candidates: list[tuple[int, str, str]] = []  # (employees, org, query_name)
        for org, row in bulk_rows.items():
            name = str((row or {}).get("navn") or "").strip()
            if not name:
                continue
            raw_employees = str((row or {}).get("antallAnsatte") or "").strip()
            employees = int(raw_employees) if raw_employees.isdigit() else 0
            if employees < EMPLOYEE_THRESHOLD:
                continue
            query_name = _strip_legal_suffix(name)
            name_norm = _normalize_name(query_name)
            if not name_norm:
                continue
            if index.match({name_norm}) or index.match_fuzzy({name_norm}):
                continue  # the feed already has this company covered - no need to search it
            candidates.append((employees, str(org), query_name))

        candidates.sort(key=lambda t: t[0], reverse=True)
        fresh = [c for c in candidates if not index.recently_searched(c[1], within_days=SEARCH_HISTORY_COOLDOWN_DAYS, now=now)]
        stale = [c for c in candidates if index.recently_searched(c[1], within_days=SEARCH_HISTORY_COOLDOWN_DAYS, now=now)]
        selected = (fresh + stale)[:SEARCH_FALLBACK_MAX]
        return {org: query_name for _employees, org, query_name in selected}

    def _ensure_prepared(self, ctx: CompanyContext) -> None:
        # Lazy fallback for a caller that never wired pipeline.py's explicit `start_prepare` step (a
        # standalone use or a unit test): `prepare()` claims-and-runs if nobody has yet, or just waits if
        # someone already has (e.g. pipeline.py's background walk) - either way this blocks until ready.
        self.prepare(ctx.client, None)

    # -- search fallback (employee-ranked, capped, breaker-protected) -------------------------------

    def _search_breaker_open(self) -> bool:
        with self._search_lock:
            return self._search_breaker_tripped

    def _do_one_search(self, ctx: CompanyContext, query_name: str) -> tuple[Any, str] | None:
        """Throttle, issue the single search request, and update the breaker/cap counters - all under
        one lock held for the *entire* critical section (throttle sleep through breaker update), not
        just the sleep. Holding the lock across the actual `client.get()` call fully serializes searches
        globally (one in flight at a time): earlier drafts released the lock after the throttle sleep,
        which let several already-queued worker threads each pass their own post-sleep breaker recheck
        before an in-flight sibling's response had come back and updated the shared counters - measured
        live, this let a handful of extra requests slip through after the circuit breaker should have
        already tripped. Returns `None` if the cap/breaker closed while waiting for the lock (checked
        again here, since another thread may have tripped it or used up the cap first).
        """
        org = str(ctx.org)
        with self._search_lock:
            if self._search_breaker_tripped:
                return None
            if self._searches_used >= SEARCH_FALLBACK_MAX:
                return None
            now = time.monotonic()
            wait = self._search_min_interval - (now - self._last_search_ts)
            if wait > 0:
                time.sleep(wait)
            if self._search_jitter_max > 0:
                time.sleep(random.uniform(0.0, self._search_jitter_max))
            self._last_search_ts = time.monotonic()

            url = f"{SEARCH_URL}?q={urllib.parse.quote(query_name)}&size=20"
            resp = ctx.client.get(url, org=org, purpose="nav_search_fallback", accept="application/json", respect_robots=True)

            self._searches_used += 1
            if resp.status == 429:
                self._search_consecutive_429 += 1
                if self._search_consecutive_429 >= SEARCH_BREAKER_CONSECUTIVE_429:
                    self._search_breaker_tripped = True
            else:
                self._search_consecutive_429 = 0
            return resp, url

    def _try_search_fallback(
        self, ctx: CompanyContext, name_norms: set[str], errors: list[dict], evidence: list[Any],
    ) -> tuple[list[dict], str]:
        """Runs the single, capped, serialized search-fallback query for `ctx.org` if it was selected
        (`_select_search_fallback`) and neither the per-run cap nor the 429 circuit breaker has stopped
        further searching. Returns (matched candidate rows in the same shape `index.match` produces,
        a short human-readable note for `nav_checked["reason"]` - e.g. what was found, or why nothing
        was attempted). A company that isn't selected, or that was selected but skipped because of the
        cap/breaker, always gets a note (never silently skipped) but never `failed` on that account alone
        - the feed-index result already computed by the caller stands on its own.
        """
        org = str(ctx.org)
        query_name = self._search_fallback_orgs.get(org)
        if query_name is None:
            return [], ""  # not selected: no note needed, this is the common case
        if self._search_breaker_open():
            return [], "search fallback skipped (circuit breaker open after repeated rate-limiting)"
        client = ctx.client
        if client.remaining(org) < 1:
            return [], "search fallback skipped (request_budget)"

        outcome = self._do_one_search(ctx, query_name)
        if outcome is None:
            reason = "circuit breaker open after repeated rate-limiting" if self._search_breaker_open() else "per-run search cap reached"
            return [], f"search fallback skipped ({reason})"
        resp, url = outcome
        if self._feed_index is not None:
            try:
                self._feed_index.record_searched(org, utc_now())
            except Exception:
                pass

        if resp.status == 429:
            # The capped, optional search being rate-limited is the breaker working, not a failure of this
            # company's research: the feed-based jobs result stands and the note records the attempt.
            return [], "search fallback attempted: rate-limited (429)"
        if not resp.ok:
            errors.append({"stage": "nav_search_fallback", "error": resp.error or f"http_{resp.status}"})
            return [], f"search fallback attempted: {resp.error or resp.status}"

        try:
            data = json.loads(resp.text())
        except Exception as exc:  # defensive: external API response
            errors.append({"stage": "nav_search_fallback_parse", "error": f"{type(exc).__name__}: {exc}"})
            return [], "search fallback attempted: parse_error"

        hits_block = data.get("hits") or {}
        hits = hits_block.get("hits") or []
        total = (hits_block.get("total") or {}).get("value")
        ev = make_evidence(
            source_url=resp.final_url or url, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
            http_status=resp.status, source_class="public_job_feed", retrieved_at=resp.retrieved_at,
            content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
            extraction_method="nav_search_fallback_v1", access_policy="NLOD-2.0 (NAV)",
            span=f"$.hits.total.value={total}; q={query_name!r}",
        )
        evidence.append(ev)

        matched: list[dict] = []
        for hit in hits:
            src = hit.get("_source") or {}
            uuid = str(src.get("uuid") or hit.get("_id") or "").strip()
            if not uuid or str(src.get("status") or "").upper() != "ACTIVE":
                continue
            employer_name = ((src.get("employer") or {}).get("name")) or ""
            business_name = src.get("businessName") or ""
            name_norm = _normalize_name(employer_name if employer_name.strip() else business_name)
            if not _name_overlaps(name_norm, name_norms):
                continue
            matched.append({
                "uuid": uuid, "business_name": employer_name or business_name,
                "sist_endret": src.get("published") or "",
            })
        note = f"search fallback attempted: {len(hits)} hit(s), {len(matched)} name-matched"
        return matched, note

    # -- main ----------------------------------------------------------------------------------------

    def run(self, ctx: CompanyContext) -> ConnectorResult:
        errors: list[dict] = []
        evidence: list[Any] = []
        shared: dict[str, Any] = {}

        tier = str(getattr(ctx, "tier", "") or "")
        if tier not in LIVE_TIERS:
            shared["nav_checked"] = {
                "checked": False, "state": "not_applicable",
                "reason": "not searched: no registered staff or web footprint",
            }
            return ConnectorResult(shared=shared)

        client = ctx.client
        if client is None:
            shared["nav_checked"] = {
                "checked": False, "state": "not_applicable", "reason": "no HTTP client available",
            }
            return ConnectorResult(shared=shared)
        if client.remaining(ctx.org) < 1:
            shared["nav_checked"] = {"checked": False, "state": "failed", "reason": "request_budget"}
            return ConnectorResult(shared=shared)

        self._ensure_prepared(ctx)
        index = self._feed_index
        stats = self._feed_stats or {}
        if index is None or (stats.get("ad_count", 0) == 0 and stats.get("errors")):
            # The feed walk never produced a usable index (e.g. the public token fetch failed) - this
            # was genuinely never checked, so it must not be reported as "checked, none found".
            reason = "; ".join(str(e) for e in (stats.get("errors") or ["nav_feed_index_unavailable"]))
            shared["nav_checked"] = {"checked": False, "state": "failed", "reason": f"nav_feed: {reason}"}
            return ConnectorResult(shared=shared, errors=errors)

        registry_facts = (ctx.shared or {}).get("registry_facts") or {}
        legal_name = registry_facts.get("name") or (ctx.bulk or {}).get("navn") or ""
        if not str(legal_name).strip():
            shared["nav_checked"] = {
                "checked": False, "state": "not_applicable", "reason": "no legal name available to match",
            }
            return ConnectorResult(shared=shared)

        subunits = registry_facts.get("subunits") or []

        name_norms = {_normalize_name(legal_name), _normalize_name(_strip_legal_suffix(legal_name))}
        for alias in registry_facts.get("aliases") or []:
            n = _normalize_name(alias)
            if n:
                name_norms.add(n)
        for sub in subunits:
            n = _normalize_name(sub.get("name"))
            if n:
                name_norms.add(n)
        aliases_cache = getattr(ctx.caches, "aliases", None) if ctx.caches is not None else None
        if aliases_cache is not None:
            try:
                for nm in aliases_cache.names(ctx.org) or []:
                    n = _normalize_name(nm)
                    if n:
                        name_norms.add(n)
            except Exception:
                pass
        name_norms.discard("")

        our_orgnrs = {str(ctx.org)}
        for sub in subunits:
            onr = str(sub.get("organisation_number") or "").strip()
            if onr:
                our_orgnrs.add(onr)

        # Exact match first (cheap, indexed); fuzzy token-overlap match second catches a trade/brand
        # name, an "avd." department suffix, or wording NAV's businessName doesn't share verbatim with
        # the legal/alias/subunit name - never a publishing risk on its own, since every candidate
        # (exact or fuzzy) still needs its organisation number confirmed via feedentry below.
        # Ads whose employer org number is already known (snapshot) come first: they catch ads posted under
        # a trade name or a subunit's number that no name match would find.
        by_orgnr = {m["uuid"]: m for m in index.match_orgnr(our_orgnrs)}
        by_uuid: dict[str, dict] = dict(by_orgnr)
        for m in index.match(name_norms):
            by_uuid.setdefault(m["uuid"], m)
        for m in index.match_fuzzy(name_norms):
            by_uuid.setdefault(m["uuid"], m)
        feed_matching = len(by_uuid)

        # Search fallback: only when the feed index found nothing at all for this company, and only if
        # it was pre-selected at prepare-time (employees >= threshold, capped, rotated - see
        # `_select_search_fallback`). Every candidate this finds still needs feedentry+orgnr
        # confirmation below, exactly like a feed-matched candidate - a search hit is never published
        # on its own.
        search_note = ""
        if feed_matching == 0:
            search_hits, search_note = self._try_search_fallback(ctx, name_norms, errors, evidence)
            for m in search_hits:
                by_uuid.setdefault(m["uuid"], m)

        ranked = sorted(by_uuid.values(), key=lambda m: (m["uuid"] in by_orgnr, m.get("sist_endret") or "", m["uuid"]), reverse=True)
        cap = self._match_cap.get(tier, 5)
        candidates = ranked[:cap]
        total_matching = len(by_uuid)
        shared["nav_total_matching_hits"] = total_matching

        verified_ads: list[dict] = []
        homepages: list[dict] = []
        token = self._get_token(client, errors) if candidates else None

        for m in candidates:
            if client.remaining(ctx.org) < 1:
                break
            if token is None:
                break
            uuid = m["uuid"]
            resp = self._fetch_feedentry(client, ctx.org, uuid, token)
            if resp.status == 401:
                token = self._get_token(client, errors, force=True)
                if token is None:
                    continue
                resp = self._fetch_feedentry(client, ctx.org, uuid, token)
            if not resp.ok:
                # 404/410: the ad closed between the feed walk and this lookup -- nothing to confirm.
                if resp.status not in (404, 410):
                    errors.append({"stage": "nav_live_feedentry", "uuid": uuid, "error": resp.error or f"http_{resp.status}"})
                continue
            try:
                entry = json.loads(resp.text())
            except Exception as exc:  # defensive: external API response
                errors.append({"stage": "nav_live_feedentry_parse", "uuid": uuid, "error": f"{type(exc).__name__}: {exc}"})
                continue

            ad = entry.get("ad_content") if isinstance(entry.get("ad_content"), dict) else entry
            employer = ad.get("employer") or {}
            orgnr = str(employer.get("orgnr") or "").strip()
            ev = make_evidence(
                source_url=resp.final_url or resp.url, final_url=resp.final_url,
                redirect_chain=resp.redirect_chain, http_status=resp.status,
                source_class="public_job_feed", retrieved_at=resp.retrieved_at,
                content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
                extraction_method="nav_live_feedentry_v1",
                span=f"$.ad_content.employer.orgnr={orgnr or 'null'}",
                access_policy="NLOD-2.0 (NAV)",
            )
            evidence.append(ev)

            extra_evidence_ids: list[str] = []
            if orgnr and orgnr not in our_orgnrs and m.get("employer_parent") == str(ctx.org):
                # The ad names one of our workplaces (a subunit we did not list this run). Confirm in the
                # register, live, that this subunit belongs to us before counting the ad as ours.
                sub_ev = self._confirm_subunit(client, ctx.org, orgnr, errors)
                if sub_ev is not None:
                    evidence.append(sub_ev)
                    extra_evidence_ids.append(sub_ev.evidence_id)
                    our_orgnrs.add(orgnr)
            if not orgnr or orgnr not in our_orgnrs:
                continue  # org number not confirmed for this company: never publish as a job claim

            homepage = _normalize_homepage(employer.get("homepage"))
            ad_url = AD_URL_TMPL.format(uuid=uuid)
            ad_row = {
                "uuid": uuid,
                "title": ad.get("title") or ad.get("jobtitle"),
                "employer_name": employer.get("name"),
                "employer_orgnr": orgnr,
                "employer_homepage": homepage,
                "published": ad.get("published"),
                "expires": ad.get("expires"),
                "application_due": ad.get("applicationDue") or ad.get("applicationdue"),
                "work_locations": ad.get("workLocations") or [],
                "ad_url": ad_url,
                "feedentry_url": resp.final_url or resp.url,
                "retrieved_at": resp.retrieved_at,
                "content_sha256": resp.content_sha256,
                "snapshot_ref": resp.snapshot_ref,
                "evidence_id": ev.evidence_id,
                "extra_evidence_ids": extra_evidence_ids,
            }
            verified_ads.append(ad_row)
            if homepage:
                homepages.append({"homepage": homepage, "uuid": uuid})

        shared["nav_ads"] = verified_ads

        seen_hp: set[str] = set()
        dedup_hp: list[dict] = []
        for hp in homepages:
            if hp["homepage"] in seen_hp:
                continue
            seen_hp.add(hp["homepage"])
            dedup_hp.append(hp)
        shared["nav_homepages"] = dedup_hp

        if self._feed_evidence is not None:
            evidence.append(self._feed_evidence)
            shared["nav_search_evidence_ids"] = [self._feed_evidence.evidence_id]
        else:
            shared["nav_search_evidence_ids"] = []

        index_complete = bool(stats.get("complete"))
        partial_note = "" if index_complete else " - feed index has not yet reached the live tip (partial window: a false zero is possible until it does)"
        reason = f"checked NAV arbeidsplassen feed index: {total_matching} name-matched active ad(s), {len(verified_ads)} verified{partial_note}"
        if search_note:
            reason += f"; {search_note}"
        shared["nav_checked"] = {
            "checked": True, "state": "ok", "reason": reason, "checked_at": utc_now(),
            "index_complete": index_complete,
            "search_attempted": search_note.startswith("search fallback attempted"),
        }

        return ConnectorResult(claims=[], evidence=evidence, families={}, errors=errors, shared=shared)
