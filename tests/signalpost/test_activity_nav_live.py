from __future__ import annotations

import json

from nav_live_testkit import FEED_URL, FEEDENTRY_PREFIX, TOKEN_URL, FakeNavHttpClient, load_fixture_text, make_ctx

from signalpost.activity.nav_live import NavLiveConnector

ORG = "919858400"


def _entry(uuid: str, *, business_name: str = "ATRIUM PEOPLE AS", status: str = "ACTIVE") -> dict:
    return {"_feed_entry": {"uuid": uuid, "status": status, "title": f"Job {uuid}", "businessName": business_name,
                             "municipal": "OSLO", "sistEndret": "2026-09-20T00:00:00+02:00"}}


def _feedentry_body(uuid: str, *, orgnr: str, employer_name: str = "ATRIUM PEOPLE AS",
                     homepage: str = "www.atriumpeople.no") -> bytes:
    payload = {
        "uuid": uuid, "status": "ACTIVE", "sistEndret": "2026-09-20T00:00:00+02:00",
        "ad_content": {
            "title": f"Job {uuid}", "employer": {"name": employer_name, "orgnr": orgnr, "homepage": homepage},
            "published": "2026-09-01T00:00:00+02:00", "expires": "2026-12-31T00:00:00+02:00",
            "applicationDue": "2026-12-31T00:00:00+02:00", "workLocations": [{"city": "Oslo"}],
        },
    }
    return json.dumps(payload).encode("utf-8")


def _client_with_feed(*, items: list[dict], feedentries: dict[str, bytes], budget: int = 100) -> FakeNavHttpClient:
    return FakeNavHttpClient(
        pages={None: {"id": "p1", "items": items, "next_id": None}},
        feedentries=feedentries, default_remaining=budget,
    )


REGISTRY_FACTS = {
    "name": "ATRIUM PEOPLE AS",
    "aliases": [],
    "subunits": [{"organisation_number": "111222333", "name": "Atrium People Avdeling Oslo"}],
}


def test_t0_tier_is_skipped_not_applicable():
    client = FakeNavHttpClient()
    ctx = make_ctx(tier="T0", client=client, registry_facts=REGISTRY_FACTS)

    result = NavLiveConnector().run(ctx)

    assert result.shared["nav_checked"]["checked"] is False
    assert result.shared["nav_checked"]["state"] == "not_applicable"
    assert "not searched" in result.shared["nav_checked"]["reason"]
    assert client.calls == []
    assert result.claims == []
    assert result.evidence == []


def test_budget_exhausted_before_any_request_is_failed():
    client = FakeNavHttpClient(default_remaining=100)
    client.budget[ORG] = 0
    ctx = make_ctx(tier="T2", client=client, registry_facts=REGISTRY_FACTS)

    result = NavLiveConnector().run(ctx)

    assert result.shared["nav_checked"]["state"] == "failed"
    assert result.shared["nav_checked"]["reason"] == "request_budget"
    assert client.calls == []


def test_name_match_and_subunit_org_number_is_verified():
    items = [_entry("u1", business_name="ATRIUM PEOPLE AS")]
    feedentries = {"u1": _feedentry_body("u1", orgnr="111222333")}  # subunit org number, not the parent
    client = _client_with_feed(items=items, feedentries=feedentries)
    ctx = make_ctx(org=ORG, tier="T2", client=client, registry_facts=REGISTRY_FACTS)

    connector = NavLiveConnector()
    connector.prepare(client, None)
    result = connector.run(ctx)

    assert result.shared["nav_checked"]["state"] == "ok"
    ads = result.shared["nav_ads"]
    assert len(ads) == 1
    assert ads[0]["uuid"] == "u1"
    assert ads[0]["employer_orgnr"] == "111222333"
    assert ads[0]["employer_homepage"] == "https://www.atriumpeople.no"
    assert result.shared["nav_homepages"] == [{"homepage": "https://www.atriumpeople.no", "uuid": "u1"}]
    assert result.shared["nav_total_matching_hits"] == 1


def test_org_number_mismatch_is_rejected_even_with_matching_name():
    items = [_entry("u2", business_name="ATRIUM PEOPLE AS")]
    feedentries = {"u2": _feedentry_body("u2", orgnr="999999999")}  # unrelated org number
    client = _client_with_feed(items=items, feedentries=feedentries)
    ctx = make_ctx(org=ORG, tier="T2", client=client, registry_facts=REGISTRY_FACTS)

    connector = NavLiveConnector()
    connector.prepare(client, None)
    result = connector.run(ctx)

    assert result.shared["nav_ads"] == []
    assert result.shared["nav_homepages"] == []
    assert result.shared["nav_total_matching_hits"] == 1  # matched by name in the feed index
    assert result.shared["nav_checked"]["state"] == "ok"
    assert "0 verified" in result.shared["nav_checked"]["reason"]
    feedentry_ev = [e for e in result.evidence if e.extraction_method == "nav_live_feedentry_v1"]
    assert len(feedentry_ev) == 1
    assert "999999999" in feedentry_ev[0].span


def test_detail_fetch_is_capped_per_tier_newest_first():
    items = [
        _entry("old", business_name="ATRIUM PEOPLE AS"),
        _entry("mid", business_name="ATRIUM PEOPLE AS"),
        _entry("new", business_name="ATRIUM PEOPLE AS"),
        _entry("newer", business_name="ATRIUM PEOPLE AS"),
    ]
    feedentries = {u: _feedentry_body(u, orgnr=ORG) for u in ["old", "mid", "new", "newer"]}
    client = _client_with_feed(items=items, feedentries=feedentries)
    ctx = make_ctx(org=ORG, tier="T1", client=client, registry_facts={"name": "ATRIUM PEOPLE AS", "aliases": [], "subunits": []})

    connector = NavLiveConnector()
    connector.prepare(client, None)
    result = connector.run(ctx)

    # T1 cap is 3; only 3 of the 4 matching ACTIVE ads should get a feedentry fetch.
    fetched_uuids = {c.url[len(FEEDENTRY_PREFIX):] for c in client.calls if c.url.startswith(FEEDENTRY_PREFIX)}
    assert len(fetched_uuids) == 3
    assert len(result.shared["nav_ads"]) == 3


def test_caches_aliases_names_are_matched_too():
    items = [_entry("u9", business_name="TRADE NAME AS")]
    feedentries = {"u9": _feedentry_body("u9", orgnr=ORG, employer_name="TRADE NAME AS")}
    client = _client_with_feed(items=items, feedentries=feedentries)

    class FakeAliases:
        def names(self, org):
            return ["Trade Name AS"]

    class FakeCaches:
        aliases = FakeAliases()

    ctx = make_ctx(org=ORG, tier="T2", client=client, caches=FakeCaches(),
                    registry_facts={"name": "SOMETHING ELSE AS", "aliases": [], "subunits": []})

    connector = NavLiveConnector()
    connector.prepare(client, None)
    result = connector.run(ctx)

    assert len(result.shared["nav_ads"]) == 1


def test_evidence_fields_source_class_and_access_policy():
    items = [_entry("u1", business_name="ATRIUM PEOPLE AS")]
    feedentries = {"u1": _feedentry_body("u1", orgnr=ORG)}
    client = _client_with_feed(items=items, feedentries=feedentries)
    ctx = make_ctx(org=ORG, tier="T1", client=client, registry_facts={"name": "ATRIUM PEOPLE AS", "aliases": [], "subunits": []})

    connector = NavLiveConnector()
    connector.prepare(client, None)
    result = connector.run(ctx)

    for ev in result.evidence:
        assert ev.source_class == "public_job_feed"
        assert ev.access_policy == "NLOD-2.0 (NAV)"
        assert ev.retrieved_at
    feedentry_ev = [e for e in result.evidence if e.extraction_method == "nav_live_feedentry_v1"][0]
    assert feedentry_ev.span == f"$.ad_content.employer.orgnr={ORG}"
    index_ev = [e for e in result.evidence if e.extraction_method == "nav_feed_index_v1"]
    assert len(index_ev) == 1
    assert "active_ads_indexed" in index_ev[0].span


def test_active_postings_count_claim_has_evidence_even_with_zero_matches():
    # A company with zero matches still needs the count claim to carry evidence (BUILD_SPEC: an
    # "available" claim always has evidence) - the shared feed-index-build evidence covers this.
    client = _client_with_feed(items=[_entry("other", business_name="UNRELATED AS")], feedentries={})
    ctx = make_ctx(org=ORG, tier="T2", client=client, registry_facts={"name": "ATRIUM PEOPLE AS", "aliases": [], "subunits": []})

    connector = NavLiveConnector()
    connector.prepare(client, None)
    result = connector.run(ctx)

    assert result.shared["nav_total_matching_hits"] == 0
    assert result.shared["nav_ads"] == []
    assert result.shared["nav_search_evidence_ids"]  # non-empty: the shared feed-index evidence id


def test_token_refetched_once_on_401_then_succeeds():
    items = [_entry("u1", business_name="ATRIUM PEOPLE AS")]
    client = _client_with_feed(items=items, feedentries={"u1": _feedentry_body("u1", orgnr=ORG)})
    client.required_bearer = "eyJhbGciOiJIUzI1NiJ9.fake.token"
    ctx = make_ctx(org=ORG, tier="T1", client=client, registry_facts={"name": "ATRIUM PEOPLE AS", "aliases": [], "subunits": []})

    connector = NavLiveConnector()
    connector.prepare(client, None)
    result = connector.run(ctx)

    assert len(result.shared["nav_ads"]) == 1
    token_calls = [c for c in client.calls if c.url == TOKEN_URL]
    assert len(token_calls) == 1  # cached after the first fetch, not refetched (correct token first try)


def test_prepare_is_idempotent_and_shared_across_companies():
    items = [_entry("u1", business_name="ATRIUM PEOPLE AS"), _entry("u2", business_name="NORSK SCANIA AS")]
    client = _client_with_feed(items=items, feedentries={
        "u1": _feedentry_body("u1", orgnr=ORG),
        "u2": _feedentry_body("u2", orgnr="879263662", employer_name="NORSK SCANIA AS"),
    })
    connector = NavLiveConnector()
    connector.prepare(client, None)
    connector.prepare(client, None)  # second call must be a no-op, not a second feed walk

    feed_calls_after_first_prepare = [c for c in client.calls if c.url == FEED_URL]
    assert len(feed_calls_after_first_prepare) == 1

    ctx1 = make_ctx(org=ORG, tier="T2", client=client, registry_facts={"name": "ATRIUM PEOPLE AS", "aliases": [], "subunits": []})
    ctx2 = make_ctx(org="879263662", tier="T2", client=client, registry_facts={"name": "NORSK SCANIA AS", "aliases": [], "subunits": []})
    r1 = connector.run(ctx1)
    r2 = connector.run(ctx2)

    assert len(r1.shared["nav_ads"]) == 1
    assert len(r2.shared["nav_ads"]) == 1
    # still exactly one feed walk (root page) across both companies
    assert len([c for c in client.calls if c.url == FEED_URL]) == 1


def test_live_fixture_feedentry_shape():
    """Sanity-checks the module against a trimmed real feedentry response captured live for Atrium
    People AS (orgnr 919858400), under tests/fixtures/nav_live/.
    """
    feedentry_raw = load_fixture_text("feedentry_atrium_people.json")
    uuid = json.loads(feedentry_raw)["uuid"]

    client = _client_with_feed(
        items=[_entry(uuid, business_name="ATRIUM PEOPLE AS")],
        feedentries={uuid: feedentry_raw.encode("utf-8")},
    )
    ctx = make_ctx(org=ORG, tier="T1", client=client, registry_facts={"name": "ATRIUM PEOPLE AS", "aliases": [], "subunits": []})

    connector = NavLiveConnector()
    connector.prepare(client, None)
    result = connector.run(ctx)

    assert result.shared["nav_checked"]["state"] == "ok"
    assert len(result.shared["nav_ads"]) == 1
    assert result.shared["nav_ads"][0]["employer_orgnr"] == ORG
    assert result.shared["nav_ads"][0]["employer_homepage"] == "https://www.atriumpeople.no"


def test_partial_index_is_reported_honestly_not_as_a_confident_zero():
    # max_feed_pages=1 with a feed that has more pages left: the walk stops early, index stays partial.
    client = FakeNavHttpClient(pages={
        None: {"id": "p1", "items": [_entry("u1", business_name="SOMEONE ELSE AS")], "next_id": "p2"},
        "p2": {"id": "p2", "items": [_entry("u2", business_name="ATRIUM PEOPLE AS")], "next_id": None},
    })
    connector = NavLiveConnector(max_feed_pages=1)
    connector.prepare(client, None)
    ctx = make_ctx(org=ORG, tier="T2", client=client, registry_facts={"name": "ATRIUM PEOPLE AS", "aliases": [], "subunits": []})

    result = connector.run(ctx)

    assert result.shared["nav_ads"] == []  # u2 (the real match) is on the unwalked page 2
    assert result.shared["nav_checked"]["index_complete"] is False
    assert "partial" in result.shared["nav_checked"]["reason"].lower()


def test_start_prepare_claims_synchronously_so_a_racing_company_thread_waits_for_the_real_state_dir(tmp_path):
    # start_prepare() must set the "claimed" flag on the calling (main) thread before returning, so a
    # company thread calling run() immediately after can never win a race and build its own throwaway
    # (state_dir=None) index instead of waiting for the real, persisted one pipeline.py asked for.
    items = [_entry("u1", business_name="ATRIUM PEOPLE AS")]
    client = _client_with_feed(items=items, feedentries={"u1": _feedentry_body("u1", orgnr=ORG)})
    connector = NavLiveConnector()

    thread = connector.start_prepare(client, str(tmp_path))
    assert thread is not None
    assert connector._prepare_started is True  # already claimed by the time start_prepare() returns

    # A second start_prepare (as if another connector reference, or a re-entrant call) is a no-op.
    assert connector.start_prepare(client, str(tmp_path)) is None

    ctx = make_ctx(org=ORG, tier="T2", client=client, registry_facts={"name": "ATRIUM PEOPLE AS", "aliases": [], "subunits": []})
    result = connector.run(ctx)  # blocks on the event until the background thread finishes
    thread.join(timeout=5)

    assert len(result.shared["nav_ads"]) == 1
    assert (tmp_path / "nav_feed_index.sqlite").exists()  # used the real state_dir, not an in-memory one
