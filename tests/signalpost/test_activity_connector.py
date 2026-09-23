from __future__ import annotations

from datetime import datetime, timedelta, timezone

from activity_testkit import FakeCaches, FakeHttpClient, FakeNavCache, load_fixture, make_ctx

from signalpost.activity import ActivityConnector

ORG = "999888777"


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()


def test_reviews_always_not_available():
    ctx = make_ctx(caches=FakeCaches(nav=FakeNavCache(ads=[])))
    result = ActivityConnector().run(ctx)
    reviews = result.families["reviews"]
    assert reviews.availability == "not_available"
    assert "licensed API access" in reviews.reason


def test_jobs_available_when_nav_has_active_ad():
    ads = [{"uuid": "a1", "status": "ACTIVE", "employer_orgnr": ORG, "title": "Selger", "published": _iso(2)}]
    ctx = make_ctx(caches=FakeCaches(nav=FakeNavCache(ads=ads)))

    result = ActivityConnector().run(ctx)

    jobs = result.families["jobs"]
    assert jobs.availability == "available"
    assert jobs.claim_count >= 1
    posting = [c for c in result.claims if c.field == "job_posting"][0]
    assert posting.availability == "available"
    assert posting in result.claims
    assert all(e.evidence_id in [c for cl in result.claims for c in cl.evidence_ids] for e in result.evidence if e.source_class == "public_job_feed")


def test_jobs_not_available_distinguishable_from_unchecked():
    # checked, zero postings -> not_available with a reason naming what was checked
    ctx_checked = make_ctx(caches=FakeCaches(nav=FakeNavCache(ads=[])))
    checked_state = ActivityConnector().run(ctx_checked).families["jobs"]
    assert checked_state.availability == "not_available"
    assert "checked NAV feed" in checked_state.reason
    assert "cache built" in checked_state.reason

    # no cache supplied at all -> not_applicable, never a silent zero
    ctx_unchecked = make_ctx(caches=None)
    unchecked_state = ActivityConnector().run(ctx_unchecked).families["jobs"]
    assert unchecked_state.availability == "not_applicable"
    assert checked_state.reason != unchecked_state.reason


def test_reviews_and_jobs_present_even_with_no_client():
    ctx = make_ctx(client=None, caches=FakeCaches(nav=FakeNavCache(ads=[])))
    result = ActivityConnector().run(ctx)
    assert set(result.families) == {"jobs", "activity", "reviews"}


def test_activity_not_applicable_without_verified_site():
    ctx = make_ctx(client=FakeHttpClient(), caches=FakeCaches(nav=FakeNavCache(ads=[])), shared={})
    result = ActivityConnector().run(ctx)
    activity = result.families["activity"]
    assert activity.availability == "not_applicable"
    assert "no verified company website" in activity.reason


def test_activity_not_applicable_at_tier_t0_even_with_site():
    client = FakeHttpClient()
    shared = {"verified_site": {"url": "https://eksempel.no", "domain": "eksempel.no"}}
    ctx = make_ctx(client=client, tier="T0", caches=FakeCaches(nav=FakeNavCache(ads=[])), shared=shared)

    result = ActivityConnector().run(ctx)

    assert result.families["activity"].availability == "not_applicable"
    assert "T0" in result.families["activity"].reason
    assert client.calls == []  # T0 makes no HTTP requests at all


def test_activity_available_from_feed_and_dedup_evidence_present():
    client = FakeHttpClient()
    client.route("https://eksempel.no/rss", load_fixture("generic_news.rss.xml"))
    shared = {
        "verified_site": {"url": "https://eksempel.no", "domain": "eksempel.no"},
        "feed_urls": ["https://eksempel.no/rss"],
    }
    ctx = make_ctx(client=client, caches=FakeCaches(nav=FakeNavCache(ads=[])), shared=shared)

    result = ActivityConnector().run(ctx)

    activity = result.families["activity"]
    assert activity.availability == "available"
    assert activity.claim_count == 3
    for claim in result.claims:
        if claim.family != "activity":
            continue
        assert claim.evidence_ids
        assert any(e.evidence_id in claim.evidence_ids for e in result.evidence)


def test_activity_not_available_when_checked_but_nothing_found():
    client = FakeHttpClient()
    client.route("https://eksempel.no/undated", b"<html><body>no date</body></html>")
    shared = {
        "verified_site": {"url": "https://eksempel.no", "domain": "eksempel.no"},
        "news_urls": ["https://eksempel.no/undated"],
    }
    ctx = make_ctx(client=client, caches=FakeCaches(nav=FakeNavCache(ads=[])), shared=shared)

    result = ActivityConnector().run(ctx)

    assert result.families["activity"].availability == "not_available"
    assert result.families["activity"].reason == "checked: none found"


def test_ats_dedupes_against_nav_by_normalized_title():
    ads = [{"uuid": "a1", "status": "ACTIVE", "employer_orgnr": ORG,
            "title": "Innholdsrådgiver og -produsent", "published": _iso(1)}]
    client = FakeHttpClient()
    client.route("https://schibsted.teamtailor.com/jobs.rss", load_fixture("teamtailor_jobs.rss.xml"))
    shared = {
        "verified_site": {"url": "https://schibsted.no", "domain": "schibsted.no"},
        "ats_links": ["https://schibsted.teamtailor.com/jobs"],
    }
    ctx = make_ctx(caches=FakeCaches(nav=FakeNavCache(ads=ads)), client=client, shared=shared)

    result = ActivityConnector().run(ctx)

    job_postings = [c for c in result.claims if c.field == "job_posting"]
    # 1 from NAV + (3 teamtailor - 1 duplicate title) = 3
    assert len(job_postings) == 3
    sources = {c.value["source"] for c in job_postings}
    assert "NAV" in sources and "teamtailor" in sources


def test_every_family_present_and_available_claims_have_evidence():
    ads = [{"uuid": "a1", "status": "ACTIVE", "employer_orgnr": ORG, "title": "Selger", "published": _iso(1)}]
    ctx = make_ctx(caches=FakeCaches(nav=FakeNavCache(ads=ads)))
    result = ActivityConnector().run(ctx)

    assert set(result.families) == set(ActivityConnector.families)
    for claim in result.claims:
        if claim.availability == "available":
            assert claim.value is not None
            assert claim.evidence_ids
    evidence_ids = {e.evidence_id for e in result.evidence}
    for claim in result.claims:
        for eid in claim.evidence_ids:
            assert eid in evidence_ids
