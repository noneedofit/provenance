from __future__ import annotations

from conftest import FakeHttpClient, load_fixture, make_ctx

from signalpost.activity import ats


def _shared_with_ats_link(url: str) -> dict:
    return {"verified_site": {"url": "https://eksempel.no", "domain": "eksempel.no"}, "ats_links": [url]}


def test_detect_teamtailor():
    hit = ats.detect(["https://schibsted.teamtailor.com/jobs"])
    assert hit == ("teamtailor", "https://schibsted.teamtailor.com/jobs")


def test_detect_none_when_no_known_provider():
    assert ats.detect(["https://eksempel.no/karriere"]) is None


def test_collect_teamtailor_rss():
    client = FakeHttpClient()
    client.route("https://schibsted.teamtailor.com/jobs.rss", load_fixture("teamtailor_jobs.rss.xml"))
    ctx = make_ctx(client=client, shared=_shared_with_ats_link("https://schibsted.teamtailor.com/jobs"))

    result = ats.collect(ctx)

    assert result["checked"] is True
    assert result["provider"] == "teamtailor"
    assert len(result["claims"]) == 3
    claim = result["claims"][0]
    assert claim.family == "jobs" and claim.field == "job_posting"
    assert claim.value_key.startswith("ats:")
    assert claim.identity_basis == "linked_from_verified_site"
    assert result["evidence"][0].extraction_method == "teamtailor_rss_v1"
    assert result["evidence"][0].source_class == "company_owned_platform"


def test_collect_greenhouse_json():
    client = FakeHttpClient()
    client.route("https://boards-api.greenhouse.io/v1/boards/eksempel/jobs", load_fixture("greenhouse_jobs.json"))
    ctx = make_ctx(client=client, shared=_shared_with_ats_link("https://boards.greenhouse.io/eksempel"))

    result = ats.collect(ctx)

    assert result["provider"] == "greenhouse"
    assert len(result["claims"]) == 3  # same-titled postings in different locations, distinct URLs
    assert result["evidence"][0].extraction_method == "greenhouse_json_v1"
    assert all(c.value["url"].startswith("https://stripe.com/jobs") for c in result["claims"])


def test_collect_lever_json():
    client = FakeHttpClient()
    client.route("https://api.lever.co/v0/postings/example?mode=json", load_fixture("lever_jobs.json"))
    ctx = make_ctx(client=client, shared=_shared_with_ats_link("https://jobs.lever.co/example"))

    result = ats.collect(ctx)

    assert result["provider"] == "lever"
    assert len(result["claims"]) == 2
    assert result["evidence"][0].extraction_method == "lever_json_v1"
    titles = {c.value["title"] for c in result["claims"]}
    assert titles == {"Backend Engineer", "Customer Success Manager"}


def test_generic_html_dedupes_and_filters_nav_links():
    client = FakeHttpClient()
    client.route("https://eksempel.jobbnorge.no/karriere", load_fixture("careers_generic.html"))
    ctx = make_ctx(client=client, shared=_shared_with_ats_link("https://eksempel.jobbnorge.no/karriere"))

    result = ats.collect(ctx)

    assert result["provider"] == "jobbnorge"
    assert len(result["claims"]) == 3  # 3 job links; Home/Kontakt/Personvern filtered out
    titles = {c.value["title"] for c in result["claims"]}
    assert titles == {"Regnskapsfører", "Prosjektleder bygg", "Kundekonsulent"}
    assert result["evidence"][0].extraction_method == "careers_html_v1"


def test_dedupes_against_nav_by_normalized_title():
    client = FakeHttpClient()
    client.route("https://schibsted.teamtailor.com/jobs.rss", load_fixture("teamtailor_jobs.rss.xml"))
    ctx = make_ctx(client=client, shared=_shared_with_ats_link("https://schibsted.teamtailor.com/jobs"))

    result = ats.collect(ctx, exclude_titles={"innholdsradgiver og produsent"})

    assert len(result["claims"]) == 2  # one of the three titles excluded


def test_no_provider_detected_is_noop():
    ctx = make_ctx(client=FakeHttpClient(), shared={"ats_links": ["https://eksempel.no/karriere"]})
    result = ats.collect(ctx)
    assert result["checked"] is False
    assert result["claims"] == []
    assert result["provider"] is None


def test_fetch_failure_records_error():
    client = FakeHttpClient()  # unregistered route -> error
    ctx = make_ctx(client=client, shared=_shared_with_ats_link("https://schibsted.teamtailor.com/jobs"))

    result = ats.collect(ctx)

    assert result["checked"] is True
    assert result["claims"] == []
    assert result["errors"]
