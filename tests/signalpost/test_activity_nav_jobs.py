from __future__ import annotations

from datetime import datetime, timedelta, timezone

from activity_testkit import FakeAliasesCache, FakeCaches, FakeNavCache, make_ctx

from signalpost.activity import nav_jobs

ORG = "999888777"


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()


def test_active_ad_becomes_job_posting_claim():
    ads = [{
        "uuid": "ad-1", "title": "Butikkmedarbeider", "status": "ACTIVE", "employer_name": "Eksempel AS",
        "employer_orgnr": ORG, "employer_homepage": "https://eksempel.no", "published": _iso(5),
        "expires": _iso(-25), "updated": _iso(5), "application_url": "https://eksempel.no/apply",
        "source_url": "https://arbeidsplassen.nav.no/stillinger/api/ad-1", "retrieved_at": "2026-09-20T00:00:00Z",
        "content_sha256": "a" * 64, "work_locations": [{"city": "Oslo"}],
    }]
    caches = FakeCaches(nav=FakeNavCache(ads=ads))
    ctx = make_ctx(caches=caches)

    result = nav_jobs.collect(ctx)

    assert result["checked"] is True
    assert result["error"] is None
    assert len(result["claims"]) == 1
    claim = result["claims"][0]
    assert claim.family == "jobs"
    assert claim.field == "job_posting"
    assert claim.value_key == "nav:ad-1"
    assert claim.availability == "available"
    assert claim.identity_basis == "job_feed_org_number"
    assert claim.value["title"] == "Butikkmedarbeider"
    assert claim.value["ad_url"] == "https://arbeidsplassen.nav.no/stillinger/stilling/ad-1"
    assert claim.value["location"] == "Oslo"
    assert claim.value["source"] == "NAV"
    assert claim.evidence_ids and result["evidence"][0].evidence_id == claim.evidence_ids[0]
    assert result["evidence"][0].source_class == "public_job_feed"
    assert result["evidence"][0].access_policy == "NLOD-2.0"
    assert result["recent_count"] == 1


def test_inactive_old_ad_not_counted_as_recent():
    ads = [{
        "uuid": "ad-2", "title": "Gammel stilling", "status": "INACTIVE", "employer_orgnr": ORG,
        "published": _iso(400), "retrieved_at": "2026-09-20T00:00:00Z",
    }]
    caches = FakeCaches(nav=FakeNavCache(ads=ads))
    ctx = make_ctx(caches=caches)

    result = nav_jobs.collect(ctx)

    assert result["claims"] == []  # inactive => no job_posting claim
    assert result["recent_count"] == 0


def test_no_ads_still_checked():
    caches = FakeCaches(nav=FakeNavCache(ads=[]))
    ctx = make_ctx(caches=caches)

    result = nav_jobs.collect(ctx)

    assert result["checked"] is True
    assert result["claims"] == []
    assert result["recent_count"] == 0
    assert result["error"] is None


def test_no_nav_cache_is_distinct_from_a_lookup_failure():
    ctx = make_ctx(caches=None)

    result = nav_jobs.collect(ctx)

    assert result["checked"] is False
    assert result["error"] is None
    assert result["cache_unavailable"] is True
    assert result["claims"] == []


def test_nav_lookup_exception_reported_as_failed_not_silent():
    class BrokenNavCache:
        def ads_for(self, orgs):
            raise RuntimeError("boom")

    caches = FakeCaches(nav=BrokenNavCache())
    ctx = make_ctx(caches=caches)

    result = nav_jobs.collect(ctx)

    assert result["checked"] is False
    assert result["error"] == "nav_lookup_failed:RuntimeError"
    assert result.get("cache_unavailable") is not True


def test_subunit_org_numbers_are_included_in_lookup():
    ads = [{"uuid": "sub-1", "status": "ACTIVE", "employer_orgnr": "111222333", "title": "Lagermedarbeider",
            "published": _iso(1), "retrieved_at": "2026-09-20T00:00:00Z"}]
    nav = FakeNavCache(ads=ads)
    aliases = FakeAliasesCache(subunits_by_org={ORG: [{"organisation_number": "111222333", "name": "Sub AS"}]})
    caches = FakeCaches(nav=nav, aliases=aliases)
    ctx = make_ctx(caches=caches)

    result = nav_jobs.collect(ctx)

    assert nav.calls[0] == [ORG, "111222333"]
    assert len(result["claims"]) == 1
