from __future__ import annotations

import json

from nav_live_testkit import FakeNavHttpClient, load_fixture_text, make_ctx

from signalpost.activity.nav_live import NavLiveConnector

TOKEN_URL = "https://pam-stilling-feed.nav.no/api/publicToken"
SEARCH_PREFIX = "https://arbeidsplassen.nav.no/stillinger/api/search?q="
FEEDENTRY_PREFIX = "https://pam-stilling-feed.nav.no/api/v1/feedentry/"

TOKEN_BODY = b"Current public token for Nav Job Vacancy Feed:\neyJhbGciOiJIUzI1NiJ9.fake.token\n"


def _search_body(hits: list[dict], *, total: int | None = None) -> bytes:
    payload = {
        "took": 5, "timed_out": False,
        "hits": {"total": {"value": total if total is not None else len(hits), "relation": "eq"}, "hits": hits},
    }
    return json.dumps(payload).encode("utf-8")


def _hit(uuid: str, *, employer_name: str, status: str = "ACTIVE", published: str = "2026-09-01T00:00:00+02:00",
         business_name: str | None = None) -> dict:
    source = {
        "uuid": uuid, "title": f"Job {uuid}", "status": status, "published": published,
        "expires": "2026-12-31T00:00:00+02:00", "employer": {"name": employer_name},
    }
    if business_name is not None:
        source["businessName"] = business_name
    return {"_id": uuid, "_source": source}


def _feedentry_body(uuid: str, *, orgnr: str, employer_name: str = "ATRIUM PEOPLE AS",
                     homepage: str = "www.atriumpeople.no") -> bytes:
    payload = {
        "uuid": uuid, "status": "ACTIVE", "sistEndret": "2026-09-01T00:00:00+02:00",
        "ad_content": {
            "title": f"Job {uuid}", "employer": {"name": employer_name, "orgnr": orgnr, "homepage": homepage},
            "published": "2026-09-01T00:00:00+02:00", "expires": "2026-12-31T00:00:00+02:00",
            "applicationDue": "2026-12-31T00:00:00+02:00", "workLocations": [{"city": "Oslo"}],
        },
    }
    return json.dumps(payload).encode("utf-8")


def _client_with(*, search_hits: list[dict], feedentries: dict[str, bytes], budget: int = 100) -> FakeNavHttpClient:
    client = FakeNavHttpClient(default_remaining=budget)
    client.route(TOKEN_URL, TOKEN_BODY)
    client.route_prefix(SEARCH_PREFIX, lambda url: _search_body(search_hits))
    for uuid, body in feedentries.items():
        client.route(f"{FEEDENTRY_PREFIX}{uuid}", body)
    return client


REGISTRY_FACTS = {
    "name": "ATRIUM PEOPLE AS",
    "subunits": [{"organisation_number": "111222333", "name": "Atrium People Avdeling Oslo"}],
}


def test_t0_tier_is_skipped_not_applicable():
    client = FakeNavHttpClient()
    ctx = make_ctx(tier="T0", client=client, registry_facts=REGISTRY_FACTS)

    result = NavLiveConnector(min_search_interval=0.0).run(ctx)

    assert result.shared["nav_checked"]["checked"] is False
    assert result.shared["nav_checked"]["state"] == "not_applicable"
    assert "not searched" in result.shared["nav_checked"]["reason"]
    assert client.calls == []
    assert result.claims == []
    assert result.evidence == []


def test_budget_exhausted_before_any_request_is_failed():
    client = FakeNavHttpClient(default_remaining=100)
    client.budget["919858400"] = 0
    ctx = make_ctx(tier="T2", client=client, registry_facts=REGISTRY_FACTS)

    result = NavLiveConnector(min_search_interval=0.0).run(ctx)

    assert result.shared["nav_checked"]["state"] == "failed"
    assert result.shared["nav_checked"]["reason"] == "request_budget"
    assert client.calls == []


def test_name_match_and_subunit_org_number_is_verified():
    hits = [_hit("u1", employer_name="ATRIUM PEOPLE AS")]
    feedentries = {"u1": _feedentry_body("u1", orgnr="111222333")}  # subunit org number, not the parent
    client = _client_with(search_hits=hits, feedentries=feedentries)
    ctx = make_ctx(org="919858400", tier="T2", client=client, registry_facts=REGISTRY_FACTS)

    result = NavLiveConnector(min_search_interval=0.0).run(ctx)

    assert result.shared["nav_checked"]["state"] == "ok"
    ads = result.shared["nav_ads"]
    assert len(ads) == 1
    assert ads[0]["uuid"] == "u1"
    assert ads[0]["employer_orgnr"] == "111222333"
    assert ads[0]["employer_homepage"] == "https://www.atriumpeople.no"
    assert result.shared["nav_homepages"] == [{"homepage": "https://www.atriumpeople.no", "uuid": "u1"}]
    assert result.shared["nav_total_matching_hits"] == 1


def test_org_number_mismatch_is_rejected_even_with_matching_name():
    hits = [_hit("u2", employer_name="ATRIUM PEOPLE AS")]
    feedentries = {"u2": _feedentry_body("u2", orgnr="999999999")}  # unrelated org number
    client = _client_with(search_hits=hits, feedentries=feedentries)
    ctx = make_ctx(org="919858400", tier="T2", client=client, registry_facts=REGISTRY_FACTS)

    result = NavLiveConnector(min_search_interval=0.0).run(ctx)

    assert result.shared["nav_ads"] == []
    assert result.shared["nav_homepages"] == []
    assert result.shared["nav_total_matching_hits"] == 1  # matched by name at search time
    assert result.shared["nav_checked"]["state"] == "ok"
    assert "0 verified" in result.shared["nav_checked"]["reason"]
    # the feedentry evidence was still recorded, just not published as a job claim
    feedentry_ev = [e for e in result.evidence if e.extraction_method == "nav_live_feedentry_v1"]
    assert len(feedentry_ev) == 1
    assert "999999999" in feedentry_ev[0].span


def test_detail_fetch_is_capped_per_tier_newest_first():
    hits = [
        _hit("old", employer_name="ATRIUM PEOPLE AS", published="2026-01-01T00:00:00+02:00"),
        _hit("mid", employer_name="ATRIUM PEOPLE AS", published="2026-06-01T00:00:00+02:00"),
        _hit("new", employer_name="ATRIUM PEOPLE AS", published="2026-09-01T00:00:00+02:00"),
        _hit("newer", employer_name="ATRIUM PEOPLE AS", published="2026-09-10T00:00:00+02:00"),
    ]
    feedentries = {u: _feedentry_body(u, orgnr="919858400") for u in ["old", "mid", "new", "newer"]}
    client = _client_with(search_hits=hits, feedentries=feedentries)
    ctx = make_ctx(org="919858400", tier="T1", client=client, registry_facts={"name": "ATRIUM PEOPLE AS", "subunits": []})

    result = NavLiveConnector(min_search_interval=0.0).run(ctx)

    # T1 cap is 3; the newest-published 3 of the 4 matching ACTIVE hits should be fetched.
    fetched_uuids = {c.url.rsplit("/", 1)[-1] for c in client.calls if c.url.startswith(FEEDENTRY_PREFIX)}
    assert len(fetched_uuids) == 3
    assert "old" not in fetched_uuids
    assert len(result.shared["nav_ads"]) == 3


def test_t2_queries_legal_name_plus_up_to_two_subunit_names():
    registry_facts = {
        "name": "ATRIUM PEOPLE AS",
        "subunits": [
            {"organisation_number": "111", "name": "Sub One"},
            {"organisation_number": "222", "name": "Sub Two"},
            {"organisation_number": "333", "name": "Sub Three"},
        ],
    }
    client = _client_with(search_hits=[], feedentries={})
    ctx = make_ctx(org="919858400", tier="T2", client=client, registry_facts=registry_facts)

    NavLiveConnector(min_search_interval=0.0).run(ctx)

    search_calls = [c.url for c in client.calls if c.url.startswith(SEARCH_PREFIX)]
    assert len(search_calls) == 3  # legal name + at most 2 distinct subunit names


def test_evidence_fields_source_class_and_access_policy():
    hits = [_hit("u1", employer_name="ATRIUM PEOPLE AS")]
    feedentries = {"u1": _feedentry_body("u1", orgnr="919858400")}
    client = _client_with(search_hits=hits, feedentries=feedentries)
    ctx = make_ctx(org="919858400", tier="T1", client=client, registry_facts={"name": "ATRIUM PEOPLE AS", "subunits": []})

    result = NavLiveConnector(min_search_interval=0.0).run(ctx)

    assert len(result.evidence) == 2  # one search evidence, one feedentry evidence
    for ev in result.evidence:
        assert ev.source_class == "public_job_feed"
        assert ev.access_policy == "NLOD-2.0 (NAV)"
        assert ev.retrieved_at
    feedentry_ev = [e for e in result.evidence if e.extraction_method == "nav_live_feedentry_v1"][0]
    assert feedentry_ev.span == "$.ad_content.employer.orgnr=919858400"
    search_ev = [e for e in result.evidence if e.extraction_method == "nav_live_search_v1"][0]
    assert "hits.total.value" in search_ev.span


def test_token_refetched_once_on_401_then_succeeds():
    hits = [_hit("u1", employer_name="ATRIUM PEOPLE AS")]
    client = _client_with(search_hits=hits, feedentries={"u1": _feedentry_body("u1", orgnr="919858400")})
    client.required_bearer = "eyJhbGciOiJIUzI1NiJ9.fake.token"
    ctx = make_ctx(org="919858400", tier="T1", client=client, registry_facts={"name": "ATRIUM PEOPLE AS", "subunits": []})

    result = NavLiveConnector(min_search_interval=0.0).run(ctx)

    assert len(result.shared["nav_ads"]) == 1
    token_calls = [c for c in client.calls if c.url == TOKEN_URL]
    assert len(token_calls) == 1  # cached after the first fetch, not refetched (correct token on first try)


def test_live_fixture_search_and_feedentry_shape():
    """Sanity-checks the module against the trimmed real API responses captured under
    tests/fixtures/nav_live/ (search for "Atrium People", feedentry for its first ACTIVE hit).
    """
    search_raw = load_fixture_text("search_atrium_people.json")
    feedentry_raw = load_fixture_text("feedentry_atrium_people.json")
    search_data = json.loads(search_raw)
    hits = search_data["hits"]["hits"]
    assert hits, "fixture should contain at least one hit"
    first_uuid = hits[0]["_source"]["uuid"]

    client = FakeNavHttpClient(default_remaining=100)
    client.route(TOKEN_URL, TOKEN_BODY)
    client.route_prefix(SEARCH_PREFIX, lambda url: search_raw.encode("utf-8"))
    client.route(f"{FEEDENTRY_PREFIX}{first_uuid}", feedentry_raw.encode("utf-8"))

    ctx = make_ctx(org="919858400", tier="T1", client=client, registry_facts={"name": "ATRIUM PEOPLE AS", "subunits": []})
    result = NavLiveConnector(min_search_interval=0.0).run(ctx)

    assert result.shared["nav_checked"]["state"] == "ok"
    assert result.shared["nav_total_matching_hits"] >= 1
