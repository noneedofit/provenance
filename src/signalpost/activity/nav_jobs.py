"""Job claims from the cached NAV (arbeidsplassen.no) ad feed.

Reads only from `ctx.caches.nav.ads_for(...)` - no live HTTP. Charges nothing against the per-company
request budget, so this works even at tier T0. `ads_for` already resolves parent -> subunit org numbers,
but we also look up subunit org numbers from `ctx.caches.aliases` (when present) and pass all of them,
since a NAV ad's `employer_orgnr` may name a subunit directly.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from ..models import utc_now
from ._common import make_claim, make_evidence

NAV_AD_URL_TMPL = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
RECENT_WINDOW_DAYS = 365


def _subunit_orgs(ctx) -> list[str]:
    orgs = [str(ctx.org)]
    caches = ctx.caches
    aliases = getattr(caches, "aliases", None) if caches is not None else None
    if aliases is not None:
        try:
            for row in aliases.subunits(ctx.org) or []:
                onr = str(row.get("organisation_number") or "").strip()
                if onr and onr not in orgs:
                    orgs.append(onr)
        except Exception:
            pass
    return orgs


def _parse_date(raw) -> date | None:
    if not raw:
        return None
    text = str(raw)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _location(ad: dict) -> str | None:
    locs = ad.get("work_locations") or []
    parts = []
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        city = loc.get("city") or loc.get("municipality") or loc.get("postalCode")
        if city:
            parts.append(str(city))
    if parts:
        return ", ".join(dict.fromkeys(parts))
    return None


def _work_locations_text(locations: list) -> str | None:
    parts = []
    for loc in locations or []:
        if not isinstance(loc, dict):
            continue
        city = loc.get("city") or loc.get("municipal") or loc.get("municipality") or loc.get("postalCode")
        if city:
            parts.append(str(city))
    if parts:
        return ", ".join(dict.fromkeys(parts))
    return None


def _collect_live(ctx, shared: dict) -> dict:
    """Build job claims from `NavLiveConnector`'s `ctx.shared["nav_ads"]` / `nav_checked` (the shared
    feed-index match + feedentry-confirm lookup, see `nav_live.py` / `nav_feed.py`). Preferred over the
    cached-feed path whenever `nav_checked` is present, i.e. `NavLiveConnector` ran for this company's
    tier and budget.
    """
    result: dict = {
        "claims": [], "evidence": [], "checked": False, "error": None,
        "recent_count": 0, "recent_evidence_ids": [], "source": "live",
        "family_availability": None, "family_reason": None, "search_attempted": False,
    }
    nav_checked = shared.get("nav_checked") or {}
    state = nav_checked.get("state")
    reason = nav_checked.get("reason")
    result["search_attempted"] = bool(nav_checked.get("search_attempted"))

    if state != "ok":
        if state == "failed":
            result["error"] = reason or "request_budget"
            result["family_availability"] = "failed"
            result["family_reason"] = reason or "request_budget"
        else:  # "not_applicable" or unrecognized -> fail safe to not_applicable, never a silent zero
            result["family_availability"] = "not_applicable"
            result["family_reason"] = reason or "not searched: no registered staff or web footprint"
        return result

    result["checked"] = True
    nav_ads = shared.get("nav_ads") or []
    search_evidence_ids = list(shared.get("nav_search_evidence_ids") or [])

    claims = []
    all_evidence_ids: list[str] = []
    for ad in nav_ads:
        uuid = str(ad.get("uuid") or "").strip()
        if not uuid:
            continue
        eid = ad.get("evidence_id")
        evidence_ids = [eid] if eid else []
        all_evidence_ids.extend(evidence_ids)
        value = {
            "title": ad.get("title"),
            "employer_name": ad.get("employer_name"),
            "location": _work_locations_text(ad.get("work_locations")),
            "published": ad.get("published"),
            "expires": ad.get("expires"),
            "application_due": ad.get("application_due"),
            "ad_url": ad.get("ad_url"),
            "source": "NAV arbeidsplassen",
        }
        claims.append(make_claim(
            org=ctx.org, family="jobs", field="job_posting", value=value,
            value_key=f"nav:{uuid}", availability="available",
            identity_basis="job_feed_org_number", effective_date=ad.get("published"),
            evidence_ids=evidence_ids,
        ))

    count_evidence_ids = list(dict.fromkeys(all_evidence_ids + search_evidence_ids))
    count_claim = make_claim(
        org=ctx.org, family="jobs", field="active_postings_count",
        # The fact is the verified count only: name-match totals depend on feed timing and the check time
        # belongs to the evidence (retrieved_at), so neither may vary the value between identical runs.
        value={"verified": len(nav_ads)},
        value_key=None, availability="available" if count_evidence_ids else "not_available",
        identity_basis="job_feed_org_number" if count_evidence_ids else None,
        evidence_ids=count_evidence_ids,
    )
    claims.append(count_claim)

    result["claims"] = claims
    if nav_ads:
        result["family_availability"] = "available"
    else:
        result["family_availability"] = "not_available"
        # Use NavLiveConnector's own reason when it has one (e.g. it names a partial feed-index window,
        # which BUILD_SPEC requires we say honestly rather than reporting a confident zero) - only fall
        # back to a generic reason if it didn't supply one.
        result["family_reason"] = reason or "checked NAV arbeidsplassen: no active postings for this org number"
    return result


def collect(ctx) -> dict:
    """Returns dict: claims, evidence, checked, error, recent_count, recent_evidence_ids.

    Prefers the live NAV search+feedentry path (`ctx.shared["nav_checked"]`, populated by
    `NavLiveConnector` when it ran for this company) over the cached feed-index path; falls back to the
    cache when `NavLiveConnector` didn't run at all (e.g. `nav` connector not wired into this pipeline).
    """
    shared = ctx.shared or {}
    if shared.get("nav_checked") is not None:
        return _collect_live(ctx, shared)
    return _collect_cache(ctx)


def _collect_cache(ctx) -> dict:
    """Returns dict: claims, evidence, checked, error, recent_count, recent_evidence_ids."""
    result: dict = {
        "claims": [], "evidence": [], "checked": False, "error": None,
        "recent_count": 0, "recent_evidence_ids": [], "source": "cache",
    }
    caches = ctx.caches
    nav_cache = getattr(caches, "nav", None) if caches is not None else None
    if nav_cache is None:
        # No cache was supplied to this run at all - nothing to check, distinct from a lookup failure.
        result["cache_unavailable"] = True
        return result

    orgs = _subunit_orgs(ctx)
    try:
        ads = nav_cache.ads_for(orgs) or []
    except Exception as exc:  # defensive: caches are external input
        result["error"] = f"nav_lookup_failed:{type(exc).__name__}"
        return result

    result["checked"] = True
    now = datetime.now(timezone.utc).date()
    claims = []
    evidence = []
    recent_count = 0
    recent_evidence_ids = []

    for ad in ads:
        uuid = str(ad.get("uuid") or "").strip()
        if not uuid:
            continue
        status = str(ad.get("status") or "").upper()
        ad_url = NAV_AD_URL_TMPL.format(uuid=uuid)
        source_url = str(ad.get("source_url") or ad_url)
        ev = make_evidence(
            source_url=source_url, final_url=source_url, redirect_chain=[source_url], http_status=200,
            source_class="public_job_feed", retrieved_at=str(ad.get("retrieved_at") or utc_now()),
            content_sha256=ad.get("content_sha256"), extraction_method="nav_feed_v1",
            span=f"$.uuid={uuid}; $.status={status}; $.employer_orgnr={ad.get('employer_orgnr')}",
            access_policy="NLOD-2.0",
        )
        evidence.append(ev)

        if status == "ACTIVE":
            value = {
                "title": ad.get("title"),
                "employer_name": ad.get("employer_name"),
                "location": _location(ad),
                "published": ad.get("published"),
                "expires": ad.get("expires"),
                "application_url": ad.get("application_url"),
                "ad_url": ad_url,
                "source": "NAV",
            }
            claims.append(make_claim(
                org=ctx.org, family="jobs", field="job_posting", value=value,
                value_key=f"nav:{uuid}", availability="available",
                identity_basis="job_feed_org_number", effective_date=ad.get("published"),
                evidence_ids=[ev.evidence_id],
            ))

        pub_date = _parse_date(ad.get("published"))
        if pub_date is not None and (now - pub_date).days <= RECENT_WINDOW_DAYS:
            recent_count += 1
            recent_evidence_ids.append(ev.evidence_id)

    result["claims"] = claims
    result["evidence"] = evidence
    result["recent_count"] = recent_count
    result["recent_evidence_ids"] = recent_evidence_ids
    return result
