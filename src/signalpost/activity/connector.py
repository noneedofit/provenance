"""ActivityConnector: jobs, activity and reviews families.

Order per company:
1. NAV cached job ads (no HTTP; works at every tier, including T0).
2. ATS feed/page on the verified site (only if a site was verified `exact` and budget/tier allow HTTP).
3. Site RSS/Atom + dated news pages (same gate as ATS).
4. YouTube channel RSS for channels linked from the verified site (same gate).
5. reviews: always `not_available` - no permitted keyless review source exists.

Honesty rules (BUILD_SPEC "never turn absence into zero"):
- jobs is `available` when >=1 active posting was found (NAV and/or ATS).
- jobs is `not_available` (not zero-claims-silently) when NAV was checked and found nothing, with a
  reason that says what was checked and when the cache was built.
- jobs is `failed` when the NAV cache lookup itself errored, and `not_applicable` when no NAV cache was
  supplied to the run at all.
- activity is `not_applicable` when there's no verified site to derive it from (nothing to check) or the
  tier has no HTTP budget (T0); `not_available` reason "checked: none found" when sources were checked
  and nothing dated turned up; `failed` when every source that was attempted errored.
"""
from __future__ import annotations

from ..context import CompanyContext
from ..models import Claim, ConnectorResult, Evidence, FamilyState
from . import ats, feeds, nav_jobs, smilefjes, youtube
from ._common import make_claim, norm_title

REVIEWS_REASON = "checked Mattilsynet food-hygiene inspections: no inspected place with this organisation number; no other permitted keyless review source (Google/Trustpilot/Glassdoor require licensed API access)"
MIN_REQUESTS_FOR_SITE_SOURCES = 1


def _budget_available(ctx: CompanyContext) -> bool:
    if ctx.client is None:
        return False
    try:
        return ctx.client.remaining(ctx.org) >= MIN_REQUESTS_FOR_SITE_SOURCES
    except Exception:
        return True


def _cache_built_at(ctx: CompanyContext) -> str | None:
    caches = ctx.caches
    if caches is None:
        return None
    try:
        meta = getattr(caches, "meta", None) or {}
        return meta.get("built_at")
    except Exception:
        return None


class ActivityConnector:
    name = "activity"
    families: tuple[str, ...] = ("jobs", "activity", "reviews")

    def run(self, ctx: CompanyContext) -> ConnectorResult:
        claims: list[Claim] = []
        evidence: list[Evidence] = []
        errors: list[dict] = []
        families: dict[str, FamilyState] = {}
        jobs_sources: list[str] = []
        activity_sources: list[str] = []

        # ---- reviews: Mattilsynet food-hygiene inspection results (official public rating) ----
        smile = smilefjes.collect(ctx) if ctx.client is not None and not (ctx.shared or {}).get("registry_only") else {"claims": [], "evidence": [], "checked": False, "error": None}
        claims.extend(smile["claims"])
        evidence.extend(smile["evidence"])
        if smile["claims"]:
            families["reviews"] = FamilyState(
                family="reviews", availability="available", sources_checked=[smilefjes.BASE_URL], claim_count=len(smile["claims"]),
            )
        elif smile["checked"]:
            families["reviews"] = FamilyState(family="reviews", availability="not_available", reason=REVIEWS_REASON, sources_checked=[smilefjes.BASE_URL])
        elif smile.get("error"):
            errors.append({"stage": "smilefjes", "error": smile["error"]})
            families["reviews"] = FamilyState(family="reviews", availability="failed", reason=f"inspection-rating index unavailable: {smile['error']}")
        else:
            families["reviews"] = FamilyState(family="reviews", availability="not_available", reason=REVIEWS_REASON)

        # ---- jobs: NAV (live search+feedentry when NavLiveConnector ran; else the cached feed index) ----
        nav_result = nav_jobs.collect(ctx)
        nav_is_live = nav_result.get("source") == "live"
        claims.extend(nav_result["claims"])
        evidence.extend(nav_result["evidence"])
        if nav_result.get("error"):
            errors.append({"stage": "nav_jobs", "error": nav_result["error"]})
        if nav_result["checked"]:
            jobs_sources.append("nav_arbeidsplassen_live" if nav_is_live else "nav_arbeidsplassen_cache")
        if nav_result.get("search_attempted"):
            jobs_sources.append("nav_search_fallback")

        active_nav_titles = {norm_title(c.value["title"]) for c in nav_result["claims"] if c.value and c.value.get("title")}

        # ---- jobs: ATS (needs HTTP + a verified site) ----
        has_verified_site = bool((ctx.shared or {}).get("verified_site"))
        # Every tier may use site-derived sources once a site is verified; the per-company request budget
        # (planner allowance) is the limit, not the tier label.
        tier_allows_http = True
        can_use_http = has_verified_site and tier_allows_http and _budget_available(ctx)

        ats_provider = None
        if can_use_http:
            ats_result = ats.collect(ctx, exclude_titles=active_nav_titles)
            claims.extend(ats_result["claims"])
            evidence.extend(ats_result["evidence"])
            errors.extend(ats_result.get("errors", []))
            ats_provider = ats_result.get("provider")
            if ats_result["checked"]:
                jobs_sources.append(f"ats_{ats_provider}")

        job_posting_claims = [c for c in claims if c.family == "jobs" and c.field == "job_posting"]

        # ---- jobs: recent_hiring summary (cache-path only; the live path publishes
        # jobs/active_postings_count instead, built in nav_jobs._collect_live) ----
        if nav_result["checked"] and not nav_is_live:
            if nav_result["recent_count"] > 0:
                claims.append(make_claim(
                    org=ctx.org, family="jobs", field="recent_hiring",
                    value={"count_last_12_months": nav_result["recent_count"]}, value_key=None,
                    availability="available", identity_basis="job_feed_org_number",
                    evidence_ids=nav_result["recent_evidence_ids"],
                ))
            else:
                claims.append(make_claim(
                    org=ctx.org, family="jobs", field="recent_hiring", value=None, value_key=None,
                    availability="not_available",
                    note="no NAV ads (active or closed) published for this org in the last 12 months",
                ))

        if job_posting_claims:
            families["jobs"] = FamilyState(
                family="jobs", availability="available", sources_checked=jobs_sources,
                claim_count=len([c for c in claims if c.family == "jobs" and c.availability == "available"]),
            )
        elif nav_result.get("family_availability"):
            # Live path: not_available / not_applicable / failed, as decided by NavLiveConnector's
            # tier/budget gate or a zero-verified-ads search (see nav_jobs._collect_live).
            reason = nav_result.get("family_reason")
            if reason and ats_provider:
                reason += f"; checked {ats_provider} careers feed: no postings"
            families["jobs"] = FamilyState(
                family="jobs", availability=nav_result["family_availability"], reason=reason,
                sources_checked=jobs_sources, claim_count=0,
            )
        elif nav_result["checked"]:
            built_at = _cache_built_at(ctx) or "unknown"
            reason = f"checked NAV feed (cache built {built_at}): no active postings"
            if ats_provider:
                reason += f"; checked {ats_provider} careers feed: no postings"
            families["jobs"] = FamilyState(family="jobs", availability="not_available", reason=reason, sources_checked=jobs_sources, claim_count=0)
        elif nav_result.get("error"):
            families["jobs"] = FamilyState(family="jobs", availability="failed", reason=nav_result["error"], sources_checked=jobs_sources, claim_count=0)
        else:
            families["jobs"] = FamilyState(family="jobs", availability="not_applicable", reason="no NAV cache supplied to this run", sources_checked=jobs_sources, claim_count=0)

        # ---- activity: site feeds/news + YouTube (needs HTTP + a verified site) ----
        if not has_verified_site and (ctx.shared or {}).get("website_unchecked"):
            families["activity"] = FamilyState(
                family="activity", availability="failed",
                reason="previously verified site unreachable this run; previous profile kept", sources_checked=[],
            )
        elif not has_verified_site:
            families["activity"] = FamilyState(
                family="activity", availability="not_applicable",
                reason="no verified company website to derive activity from", sources_checked=[],
            )
        elif not tier_allows_http:
            families["activity"] = FamilyState(
                family="activity", availability="not_applicable",
                reason="tier T0: no HTTP budget for site-derived activity sources", sources_checked=[],
            )
        elif not _budget_available(ctx):
            families["activity"] = FamilyState(
                family="activity", availability="failed", reason="request_budget", sources_checked=[],
            )
        else:
            before = len(claims)
            feeds_result = feeds.collect(ctx)
            claims.extend(feeds_result["claims"])
            evidence.extend(feeds_result["evidence"])
            errors.extend(feeds_result.get("errors", []))
            if feeds_result["checked"]:
                activity_sources.append("site_feeds_and_news")

            yt_result = youtube.collect(ctx)
            claims.extend(yt_result["claims"])
            evidence.extend(yt_result["evidence"])
            errors.extend(yt_result.get("errors", []))
            if yt_result["checked"]:
                activity_sources.append("youtube_rss")

            new_count = len(claims) - before
            any_errors = bool([e for e in (feeds_result.get("errors") or []) + (yt_result.get("errors") or [])])
            if new_count > 0:
                families["activity"] = FamilyState(family="activity", availability="available", sources_checked=activity_sources, claim_count=new_count)
            elif activity_sources:
                families["activity"] = FamilyState(family="activity", availability="not_available", reason="checked: none found", sources_checked=activity_sources, claim_count=0)
            elif any_errors:
                # Something was attempted (feed_urls/news_urls existed, or the WordPress feed/REST
                # discovery probe was tried) and it genuinely errored -- a real "failed", not an absence.
                families["activity"] = FamilyState(family="activity", availability="failed", reason="all activity sources failed or were unreachable", sources_checked=activity_sources, claim_count=0)
            else:
                # Nothing was even attempted: no feed_urls/news_urls from W3, and feeds.collect()'s own
                # discovery (WordPress /feed/, REST API, linked news/blog pages) found no candidate to
                # try either. An honest absence ("checked, nothing to check"), not a failure.
                families["activity"] = FamilyState(family="activity", availability="not_available", reason="no site-derived activity source discoverable (no feed, no news/blog page, no linked video channel)", sources_checked=activity_sources, claim_count=0)

        return ConnectorResult(claims=claims, evidence=evidence, families=families, errors=errors, shared={})
