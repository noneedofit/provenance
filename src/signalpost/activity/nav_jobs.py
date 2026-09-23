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


def collect(ctx) -> dict:
    """Returns dict: claims, evidence, checked, error, recent_count, recent_evidence_ids."""
    result: dict = {
        "claims": [], "evidence": [], "checked": False, "error": None,
        "recent_count": 0, "recent_evidence_ids": [],
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
