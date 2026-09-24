"""Live NAV arbeidsplassen job-postings lookup (search + feedentry), keyless, no cache dependency.

Why this exists: `nav_jobs.py` reads from the cached NAV feed index (`ctx.caches.nav`, W2's contract),
which is a chronological event log built by walking the feed from mid-2023 - reaching today's ads needs a
multi-hour forward walk, so the cache is stale/incomplete for a live batch run. This module hits the two
live, keyless NAV endpoints instead (verified against the real API, see docs/activity.md):

1. `GET https://arbeidsplassen.nav.no/stillinger/api/search?q=<name>&size=100` - full-text search,
   robots.txt allows everything. Returns `hits.hits[]._source` with `uuid`, `title`, `businessName`,
   `employer.name` (legal name, usually upper case), `published`, `expires`, `status`,
   `locationList` - but NOT the employer's organisation number.
2. `GET https://pam-stilling-feed.nav.no/api/v1/feedentry/{uuid}` with header
   `Authorization: Bearer <token>` - confirms the organisation number. The token is public: the LAST
   non-empty line of `GET https://pam-stilling-feed.nav.no/api/publicToken`. Without the header this
   endpoint returns 401 (measured live); the token is fetched once per connector instance (i.e. once per
   run - `_default_connectors()` builds one `NavLiveConnector` for the whole batch) and cached in memory
   behind a lock, refreshed once on a 401 in case it expired mid-run.

`NavLiveConnector` (name="nav", families=()) publishes zero claims and zero FamilyStates itself - it only
fills `ctx.shared` so downstream connectors (activity's `nav_jobs.py`, later `web`) can consume it:
- `nav_ads`: list of verified ad dicts (organisation number confirmed against this org or a subunit).
- `nav_homepages`: normalized employer homepages from verified ads, each with the source uuid.
- `nav_checked`: {"checked": bool, "state": "ok"|"not_applicable"|"failed", "reason": str, "checked_at"}.
- `nav_total_matching_hits`: count of search hits whose employer name matched (verified or not).
- `nav_search_evidence_ids`: evidence ids for the search call(s), so a zero-ads run still has evidence to
  cite on the `jobs/active_postings_count` claim.

Runs before `web` (pipeline order: registry -> nav -> web -> activity) so `web` can use verified NAV
homepages as website candidates, and `activity` (which runs after `web`) turns `nav_ads` into
`jobs/job_posting` claims.

Rate limiting (measured live 2026-09-24): the search endpoint (`arbeidsplassen.nav.no/stillinger/api/`,
fronted by a CDN/WAF) returns 429 well before any documented quota - a burst of ~6-8 requests in quick
succession from one IP trips it, and it then took several minutes to clear in manual testing. Running this
connector across many companies with the pipeline's default thread pool (12 workers) fires that many
search requests almost simultaneously, so every single search 429'd in an early live batch run. To stay
under the trip threshold, all search requests (across every company, every thread - this connector is a
single shared instance for the whole batch, see `pipeline._default_connectors`) are serialized through
`_throttle_search` with a minimum spacing, plus a short bounded retry-with-backoff on a 429 response. The
feedentry endpoint (a different host, `pam-stilling-feed.nav.no`) was never observed to 429 in the same
testing, so it isn't throttled - only rate-limited by each company's own request budget.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from typing import Any

from ..context import CompanyContext
from ..models import ConnectorResult, utc_now
from ._common import make_evidence, norm_title

SEARCH_URL = "https://arbeidsplassen.nav.no/stillinger/api/search"
TOKEN_URL = "https://pam-stilling-feed.nav.no/api/publicToken"
FEEDENTRY_URL_TMPL = "https://pam-stilling-feed.nav.no/api/v1/feedentry/{uuid}"
AD_URL_TMPL = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"

DETAIL_CAP = {"T1": 3, "T2": 5, "T3": 8}
LIVE_TIERS = ("T1", "T2", "T3")
MIN_SEARCH_INTERVAL_S = 3.0
SEARCH_RETRY_DELAYS_S = (6.0, 15.0)
MAX_SUBUNIT_QUERIES = 2

_LEGAL_SUFFIX_TOKENS = {
    "as", "asa", "sa", "da", "ans", "ba", "ks", "iks", "nuf", "enk", "sf", "esek", "brl", "sti", "sam",
}


def _strip_legal_suffix(name: str) -> str:
    """Human-readable name with a trailing Norwegian legal-form token removed (for the search query)."""
    tokens = str(name or "").strip().split()
    while tokens and tokens[-1].strip(".,").casefold() in _LEGAL_SUFFIX_TOKENS:
        tokens.pop()
    stripped = " ".join(tokens)
    return stripped or str(name or "").strip()


def _normalize_name(name: Any) -> str:
    """Casefold, strip diacritics/punctuation/legal suffixes, collapse whitespace (reuses `norm_title`,
    which already does exactly this for cross-source job-title matching elsewhere in this package).
    """
    return norm_title(name)


def _normalize_homepage(value: Any) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    return value.rstrip("/")


# Circuit breaker: the search host rate-limits by IP. Once it answers 429 this many times in a row, stop
# searching for the rest of the run (jobs -> failed "rate-limited") instead of burning the request budget.
BREAKER_CONSECUTIVE_429 = 3
MAX_SEARCHES_PER_RUN = 60


class NavLiveConnector:
    name = "nav"
    families: tuple[str, ...] = ()

    def __init__(
        self, *, min_search_interval: float = MIN_SEARCH_INTERVAL_S,
        search_retry_delays: tuple[float, ...] = SEARCH_RETRY_DELAYS_S,
    ) -> None:
        self._token_lock = threading.Lock()
        self._token: str | None = None
        self._search_lock = threading.Lock()
        self._last_search_ts = 0.0
        # Overridable (defaults are the live-measured values) so unit tests can disable the deliberate
        # wall-clock throttling - see the module docstring's "Rate limiting" note for why it exists.
        self._min_search_interval = min_search_interval
        self._search_retry_delays = search_retry_delays
        self._breaker_lock = threading.Lock()
        self._consecutive_429 = 0
        self._searches = 0
        self.tripped_reason: str | None = None

    # -- search throttling ---------------------------------------------------------------------------

    def _throttle_search(self) -> None:
        """Serialize search requests (across every company/thread) with a minimum spacing - see the
        module docstring's "Rate limiting" note for why.
        """
        if self._min_search_interval <= 0:
            return
        with self._search_lock:
            now = time.monotonic()
            wait = self._min_search_interval - (now - self._last_search_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_search_ts = time.monotonic()

    def _breaker_open(self) -> str | None:
        with self._breaker_lock:
            return self.tripped_reason

    def _record_search(self, status: int) -> None:
        with self._breaker_lock:
            self._searches += 1
            self._consecutive_429 = self._consecutive_429 + 1 if status == 429 else 0
            if self._consecutive_429 >= BREAKER_CONSECUTIVE_429:
                self.tripped_reason = "nav_search_rate_limited"
            elif self._searches >= MAX_SEARCHES_PER_RUN and not self.tripped_reason:
                self.tripped_reason = "nav_search_run_cap"

    def _search(self, ctx: CompanyContext, url: str):
        client = ctx.client
        delays = (0.0, *self._search_retry_delays)
        resp = None
        for attempt, delay in enumerate(delays):
            if self._breaker_open():
                return resp
            if delay:
                time.sleep(delay)
            self._throttle_search()
            resp = client.get(url, org=ctx.org, purpose="nav_live_search", accept="application/json", respect_robots=True)
            self._record_search(resp.status)
            if resp.status != 429 or attempt == len(delays) - 1:
                return resp
        return resp

    # -- token -------------------------------------------------------------------------------------

    def _get_token(self, ctx: CompanyContext, errors: list[dict], *, force: bool = False) -> str | None:
        with self._token_lock:
            if self._token is not None and not force:
                return self._token
            client = ctx.client
            resp = client.get(
                TOKEN_URL, org=None, purpose="nav_live_token", accept="text/plain",
                respect_robots=True, snapshot=False,
            )
            if not resp.ok:
                errors.append({"stage": "nav_live_token", "error": resp.error or f"http_{resp.status}"})
                return None
            lines = [ln.strip() for ln in resp.text().splitlines() if ln.strip()]
            if not lines:
                errors.append({"stage": "nav_live_token", "error": "empty_token_response"})
                return None
            self._token = lines[-1]
            return self._token

    def _fetch_feedentry(self, ctx: CompanyContext, uuid: str, token: str):
        client = ctx.client
        url = FEEDENTRY_URL_TMPL.format(uuid=uuid)
        return client.get(
            url, org=ctx.org, purpose="nav_live_feedentry", accept="application/json",
            respect_robots=True, headers={"Authorization": f"Bearer {token}"},
        )

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
            return ConnectorResult(shared=shared, errors=errors)

        registry_facts = (ctx.shared or {}).get("registry_facts") or {}
        legal_name = registry_facts.get("name") or (ctx.bulk or {}).get("navn") or ""
        if not str(legal_name).strip():
            shared["nav_checked"] = {
                "checked": False, "state": "not_applicable", "reason": "no legal name available to search",
            }
            return ConnectorResult(shared=shared)

        subunits = registry_facts.get("subunits") or []

        stripped_name = _strip_legal_suffix(legal_name)
        query_names = [stripped_name]
        if tier in ("T2", "T3"):
            seen_norm = {_normalize_name(stripped_name)}
            for sub in subunits:
                nm = str(sub.get("name") or "").strip()
                if not nm:
                    continue
                key = _normalize_name(nm)
                if not key or key in seen_norm:
                    continue
                seen_norm.add(key)
                query_names.append(nm)
                if len(query_names) - 1 >= MAX_SUBUNIT_QUERIES:
                    break

        target_norms = {_normalize_name(stripped_name), _normalize_name(legal_name)}
        for sub in subunits:
            nm = sub.get("name")
            if nm:
                target_norms.add(_normalize_name(nm))
        target_norms.discard("")

        our_orgnrs = {str(ctx.org)}
        for sub in subunits:
            onr = str(sub.get("organisation_number") or "").strip()
            if onr:
                our_orgnrs.add(onr)

        matched_hits: dict[str, dict] = {}
        search_evidence_ids: list[str] = []

        for q in query_names:
            if client.remaining(ctx.org) < 1:
                break
            url = f"{SEARCH_URL}?q={urllib.parse.quote(q)}&size=100"
            resp = self._search(ctx, url)
            if resp is None:  # circuit breaker open: no request was made
                errors.append({"stage": "nav_live_search", "query": q, "error": self._breaker_open() or "nav_search_disabled"})
                break
            if resp.error:
                errors.append({"stage": "nav_live_search", "query": q, "error": resp.error})
                continue
            try:
                data = json.loads(resp.text())
            except Exception as exc:  # defensive: external API response
                errors.append({"stage": "nav_live_search_parse", "query": q, "error": f"{type(exc).__name__}: {exc}"})
                continue

            hits_block = data.get("hits") or {}
            hits = hits_block.get("hits") or []
            total = (hits_block.get("total") or {}).get("value")
            ev = make_evidence(
                source_url=resp.final_url or url, final_url=resp.final_url,
                redirect_chain=resp.redirect_chain, http_status=resp.status,
                source_class="public_job_feed", retrieved_at=resp.retrieved_at,
                content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
                extraction_method="nav_live_search_v1",
                span=f"$.hits.total.value={total}; q={q!r}",
                access_policy="NLOD-2.0 (NAV)",
            )
            evidence.append(ev)
            search_evidence_ids.append(ev.evidence_id)

            for hit in hits:
                src = hit.get("_source") or {}
                uuid = str(src.get("uuid") or hit.get("_id") or "").strip()
                if not uuid or uuid in matched_hits:
                    continue
                employer_name = ((src.get("employer") or {}).get("name")) or ""
                secondary_name = src.get("businessName") or ""
                name_for_match = employer_name if employer_name.strip() else secondary_name
                if _normalize_name(name_for_match) in target_norms:
                    matched_hits[uuid] = src

        if not search_evidence_ids and errors:
            # Every search query errored (e.g. rate-limited past the retry budget, or a network fault) -
            # this was never actually checked, so it must not be reported as "checked, zero found".
            shared["nav_checked"] = {
                "checked": False, "state": "failed",
                "reason": f"nav_live_search: {errors[-1].get('error', 'all search requests failed')}",
            }
            return ConnectorResult(claims=[], evidence=evidence, families={}, errors=errors, shared=shared)

        total_matching = len(matched_hits)
        shared["nav_total_matching_hits"] = total_matching
        shared["nav_search_evidence_ids"] = search_evidence_ids

        active_hits = [(u, s) for u, s in matched_hits.items() if str(s.get("status") or "").upper() == "ACTIVE"]
        active_hits.sort(key=lambda item: item[1].get("published") or "", reverse=True)
        cap = DETAIL_CAP.get(tier, 3)
        candidates = active_hits[:cap]

        verified_ads: list[dict] = []
        homepages: list[dict] = []
        token = self._get_token(ctx, errors) if candidates else None

        for uuid, _src in candidates:
            if client.remaining(ctx.org) < 1:
                break
            if token is None:
                break
            resp = self._fetch_feedentry(ctx, uuid, token)
            if resp.status == 401:
                token = self._get_token(ctx, errors, force=True)
                if token is None:
                    continue
                resp = self._fetch_feedentry(ctx, uuid, token)
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

        reason = f"checked NAV arbeidsplassen: {total_matching} name-matched hit(s), {len(verified_ads)} verified"
        shared["nav_checked"] = {"checked": True, "state": "ok", "reason": reason, "checked_at": utc_now()}

        return ConnectorResult(claims=[], evidence=evidence, families={}, errors=errors, shared=shared)
