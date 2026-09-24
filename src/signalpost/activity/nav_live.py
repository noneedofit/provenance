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
import re
import threading
from typing import Any

from ..context import CompanyContext
from ..models import ConnectorResult, utc_now
from ._common import make_evidence, norm_title
from .nav_feed import DEFAULT_MAX_PAGES, DEFAULT_MAX_SECONDS, DEFAULT_WINDOW_DAYS, FEED_URL, NavFeedIndex, walk_feed

TOKEN_URL = "https://pam-stilling-feed.nav.no/api/publicToken"
FEEDENTRY_URL_TMPL = "https://pam-stilling-feed.nav.no/api/v1/feedentry/{uuid}"
AD_URL_TMPL = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"

DETAIL_CAP = {"T1": 3, "T2": 5, "T3": 8}
LIVE_TIERS = ("T1", "T2", "T3")

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


class NavLiveConnector:
    name = "nav"
    families: tuple[str, ...] = ()

    def __init__(
        self, *, window_days: int = DEFAULT_WINDOW_DAYS, max_feed_pages: int = DEFAULT_MAX_PAGES,
        max_feed_seconds: float = DEFAULT_MAX_SECONDS, match_cap: dict[str, int] | None = None,
    ) -> None:
        self._window_days = window_days
        self._max_feed_pages = max_feed_pages
        self._max_feed_seconds = max_feed_seconds
        self._match_cap = match_cap or DETAIL_CAP

        self._token_lock = threading.Lock()
        self._token: str | None = None

        self._prepare_lock = threading.Lock()
        self._prepare_started = False
        self._prepared_event = threading.Event()
        self._feed_index: NavFeedIndex | None = None
        self._feed_stats: dict[str, Any] = {}
        self._feed_evidence: Any = None

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

    # -- shared feed index (built once per run) -----------------------------------------------------

    def start_prepare(self, client: Any, state_dir: str | None = None) -> threading.Thread | None:
        """Synchronously claim the "build the shared feed index" slot (idempotent: a second/concurrent
        call is a no-op, returning `None`), then run the actual walk in a background thread and return
        it. `pipeline.run_batch` calls this - claiming the slot synchronously, on the main thread, before
        any company is submitted to the worker pool, is what guarantees no company thread can race ahead
        and call the same-thread fallback (`prepare()`, via `_ensure_prepared`) with the wrong (`None`,
        in-memory, no persistence) `state_dir` before this real one has had a chance to start.
        """
        with self._prepare_lock:
            if self._prepare_started:
                return None
            self._prepare_started = True
        t = threading.Thread(target=self._do_prepare, args=(client, state_dir), daemon=True)
        t.start()
        return t

    def prepare(self, client: Any, state_dir: str | None = None) -> None:
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
        self._do_prepare(client, state_dir)

    def _do_prepare(self, client: Any, state_dir: str | None) -> None:
        """The actual feed walk. Callers must have already claimed `_prepare_started` (via
        `start_prepare` or `prepare`) before calling this.
        """
        try:
            index = NavFeedIndex.open(state_dir)
            token_errors: list[dict] = []
            stats = walk_feed(
                client, lambda force=False: self._get_token(client, token_errors, force=force), index,
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
                span=(
                    f"active_ads_indexed={stats['ad_count']}; window_days={self._window_days}; "
                    f"complete={stats['complete']}; pages_walked_this_run={stats.get('pages', 0)}"
                ),
            )
        except Exception as exc:  # a broken feed walk must never take the whole run down
            self._feed_stats = {"errors": [f"prepare_crashed:{type(exc).__name__}: {exc}"], "ad_count": 0, "complete": False}
            self._feed_index = None
        finally:
            self._prepared_event.set()

    def _ensure_prepared(self, ctx: CompanyContext) -> None:
        # Lazy fallback for a caller that never wired pipeline.py's explicit `start_prepare` step (a
        # standalone use or a unit test): `prepare()` claims-and-runs if nobody has yet, or just waits if
        # someone already has (e.g. pipeline.py's background walk) - either way this blocks until ready.
        self.prepare(ctx.client, None)

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

        matches = index.match(name_norms)
        by_uuid = {m["uuid"]: m for m in matches}
        ranked = sorted(by_uuid.values(), key=lambda m: m.get("sist_endret") or "", reverse=True)
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
        shared["nav_checked"] = {
            "checked": True, "state": "ok", "reason": reason, "checked_at": utc_now(),
            "index_complete": index_complete,
        }

        return ConnectorResult(claims=[], evidence=evidence, families={}, errors=errors, shared=shared)
