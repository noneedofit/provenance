from __future__ import annotations

import json
from pathlib import Path

from nav_live_testkit import FEEDENTRY_PREFIX, SEARCH_URL, FakeNavHttpClient, make_ctx

from signalpost.activity.nav_live import (
    EMPLOYEE_THRESHOLD,
    SEARCH_BREAKER_CONSECUTIVE_429,
    SEARCH_FALLBACK_MAX,
    SEARCH_HISTORY_COOLDOWN_DAYS,
    NavLiveConnector,
)


def _empty_feed_client(**kw) -> FakeNavHttpClient:
    return FakeNavHttpClient(pages={None: {"id": "p1", "items": [], "next_id": None}}, **kw)


def _bulk_row(name: str, employees: int) -> dict:
    return {"navn": name, "antallAnsatte": str(employees)}


def _search_body(hits: list[dict]) -> bytes:
    return json.dumps({"hits": {"total": {"value": len(hits)}, "hits": hits}}).encode("utf-8")


def _hit(uuid: str, *, employer_name: str, status: str = "ACTIVE", published: str = "2026-09-01T00:00:00+02:00") -> dict:
    return {"_id": uuid, "_source": {"uuid": uuid, "status": status, "published": published,
                                      "employer": {"name": employer_name}}}


def _feedentry_body(uuid: str, *, orgnr: str) -> bytes:
    payload = {
        "uuid": uuid, "status": "ACTIVE",
        "ad_content": {"title": "Job", "employer": {"name": "X", "orgnr": orgnr, "homepage": "x.no"},
                       "published": "2026-09-01T00:00:00+02:00", "expires": "2026-12-31T00:00:00+02:00"},
    }
    return json.dumps(payload).encode("utf-8")


def test_only_employees_above_threshold_are_selected(tmp_path: Path):
    bulk = {
        "919800001": _bulk_row("BELOW THRESHOLD AS", EMPLOYEE_THRESHOLD - 1),
        "919800002": _bulk_row("AT THRESHOLD AS", EMPLOYEE_THRESHOLD),
        "919800003": _bulk_row("NO EMPLOYEES AS", 0),
    }
    client = _empty_feed_client()
    connector = NavLiveConnector(search_min_interval=0.0, search_jitter_max=0.0)
    connector.prepare(client, str(tmp_path), bulk)

    selected = connector._search_fallback_orgs
    assert "919800001" not in selected
    assert "919800003" not in selected
    assert "919800002" in selected


def test_selection_ranks_by_employees_descending_and_caps_at_max(tmp_path: Path):
    bulk = {}
    # SEARCH_FALLBACK_MAX + 5 eligible companies, distinct employee counts so ranking is unambiguous.
    n = SEARCH_FALLBACK_MAX + 5
    for i in range(n):
        org = f"9198{i:05d}"
        employees = 1000 - i  # company 0 has the most employees, company n-1 the fewest
        bulk[org] = _bulk_row(f"COMPANY {i} AS", employees)

    client = _empty_feed_client()
    connector = NavLiveConnector(search_min_interval=0.0, search_jitter_max=0.0)
    connector.prepare(client, str(tmp_path), bulk)

    selected = connector._search_fallback_orgs
    assert len(selected) == SEARCH_FALLBACK_MAX
    # the top SEARCH_FALLBACK_MAX by employees (companies 0..SEARCH_FALLBACK_MAX-1) must be exactly selected
    expected_orgs = {f"9198{i:05d}" for i in range(SEARCH_FALLBACK_MAX)}
    assert set(selected.keys()) == expected_orgs


def test_company_already_matched_by_feed_index_is_not_selected(tmp_path: Path):
    items = [{"_feed_entry": {"uuid": "u1", "status": "ACTIVE", "title": "Job", "businessName": "Already Found AS",
                               "municipal": "OSLO", "sistEndret": "2026-09-01T00:00:00Z"}}]
    client = FakeNavHttpClient(pages={None: {"id": "p1", "items": items, "next_id": None}})
    bulk = {"919800009": _bulk_row("Already Found AS", 50)}
    connector = NavLiveConnector(search_min_interval=0.0, search_jitter_max=0.0)
    connector.prepare(client, str(tmp_path), bulk)

    assert "919800009" not in connector._search_fallback_orgs


def test_rotation_prefers_companies_not_recently_searched(tmp_path: Path):
    # Only 1 slot's worth of "fresh" candidates needed to prove ordering: with SEARCH_FALLBACK_MAX slots
    # and one already-recently-searched high-employee company, a fresh lower-employee company should
    # still be preferred over it (fresh candidates come first, recently-searched only fill leftover cap).
    bulk = {
        "919800010": _bulk_row("Recently Searched AS", 500),
        "919800011": _bulk_row("Fresh Candidate AS", 10),
    }
    client = _empty_feed_client()
    connector = NavLiveConnector(search_min_interval=0.0, search_jitter_max=0.0)
    # Mark 919800010 as searched "now" (well within the cooldown window) before prepare() runs.
    from signalpost.activity.nav_feed import NavFeedIndex

    seed_index = NavFeedIndex.open(tmp_path)
    from signalpost.models import utc_now

    seed_index.record_searched("919800010", utc_now())
    seed_index.close()

    connector.prepare(client, str(tmp_path), bulk)
    selected_orgs = list(connector._search_fallback_orgs.keys())
    assert selected_orgs.index("919800011") < selected_orgs.index("919800010")


def test_rotation_uses_recently_searched_to_fill_spare_capacity(tmp_path: Path):
    bulk = {"919800012": _bulk_row("Only Candidate AS", 50)}
    client = _empty_feed_client()
    connector = NavLiveConnector(search_min_interval=0.0, search_jitter_max=0.0)
    from signalpost.activity.nav_feed import NavFeedIndex
    from signalpost.models import utc_now

    seed_index = NavFeedIndex.open(tmp_path)
    seed_index.record_searched("919800012", utc_now())
    seed_index.close()

    connector.prepare(client, str(tmp_path), bulk)
    # Even though it was just searched, it's the only candidate - spare capacity means it still gets in.
    assert "919800012" in connector._search_fallback_orgs


def test_search_hit_still_requires_feedentry_orgnr_confirmation(tmp_path: Path):
    org = "937718284"
    bulk = {org: _bulk_row("OSLO AKUTTEN AS", 20)}
    hits = [_hit("s1", employer_name="Oslo Akutten")]

    def responder(url, n):
        return 200, _search_body(hits)

    client = _empty_feed_client(search_responder=responder)
    client.feedentries["s1"] = _feedentry_body("s1", orgnr="999999999")  # wrong org: must not verify
    connector = NavLiveConnector(search_min_interval=0.0, search_jitter_max=0.0)
    connector.prepare(client, str(tmp_path), bulk)

    ctx = make_ctx(org=org, tier="T2", client=client, registry_facts={"name": "OSLO AKUTTEN AS", "aliases": [], "subunits": []})
    result = connector.run(ctx)

    assert result.shared["nav_ads"] == []
    assert client.search_calls == 1
    feedentry_calls = [c for c in client.calls if c.url.startswith(FEEDENTRY_PREFIX)]
    assert len(feedentry_calls) == 1  # the search hit WAS fetched, just correctly rejected


def test_search_hit_with_matching_orgnr_is_published(tmp_path: Path):
    org = "937718284"
    bulk = {org: _bulk_row("OSLO AKUTTEN AS", 20)}
    hits = [_hit("s2", employer_name="Oslo Akutten")]

    def responder(url, n):
        return 200, _search_body(hits)

    client = _empty_feed_client(search_responder=responder)
    client.feedentries["s2"] = _feedentry_body("s2", orgnr=org)
    connector = NavLiveConnector(search_min_interval=0.0, search_jitter_max=0.0)
    connector.prepare(client, str(tmp_path), bulk)

    ctx = make_ctx(org=org, tier="T2", client=client, registry_facts={"name": "OSLO AKUTTEN AS", "aliases": [], "subunits": []})
    result = connector.run(ctx)

    assert len(result.shared["nav_ads"]) == 1
    assert result.shared["nav_checked"]["search_attempted"] is True
    assert "search fallback attempted" in result.shared["nav_checked"]["reason"]


def test_company_not_selected_for_search_stays_not_available_never_failed(tmp_path: Path):
    # No bulk data at all -> nobody is selected for search fallback; the feed-based zero-match result
    # must still be a normal "checked, none found" - never `failed` just because search wasn't tried.
    client = _empty_feed_client()
    connector = NavLiveConnector(search_min_interval=0.0, search_jitter_max=0.0)
    connector.prepare(client, str(tmp_path), None)

    ctx = make_ctx(org="937718284", tier="T2", client=client,
                    registry_facts={"name": "OSLO AKUTTEN AS", "aliases": [], "subunits": []})
    result = connector.run(ctx)

    assert result.shared["nav_checked"]["state"] == "ok"
    assert result.shared["nav_checked"]["search_attempted"] is False
    assert "checked NAV arbeidsplassen feed index" in result.shared["nav_checked"]["reason"]
    assert client.search_calls == 0


def test_breaker_trips_after_consecutive_429s_and_stops_all_further_searches(tmp_path: Path):
    bulk = {
        "919800020": _bulk_row("Company A AS", 100),
        "919800021": _bulk_row("Company B AS", 90),
        "919800022": _bulk_row("Company C AS", 80),
    }
    assert SEARCH_BREAKER_CONSECUTIVE_429 == 2  # this test assumes the documented default

    def responder(url, n):
        return 429, b'{"status":429}'

    client = _empty_feed_client(search_responder=responder)
    connector = NavLiveConnector(search_min_interval=0.0, search_jitter_max=0.0)
    connector.prepare(client, str(tmp_path), bulk)
    results = []
    for org in bulk:
        ctx = make_ctx(org=org, tier="T2", client=client, registry_facts={
            "name": bulk[org]["navn"], "aliases": [], "subunits": [],
        })
        results.append(connector.run(ctx))

    # Exactly 2 companies actually hit the search endpoint (tripping the breaker); the 3rd is skipped.
    assert client.search_calls == SEARCH_BREAKER_CONSECUTIVE_429
    reasons = [r.shared["nav_checked"]["reason"] for r in results]
    assert sum("circuit breaker open" in r for r in reasons) == 1
    assert sum("rate-limited (429)" in r for r in reasons) == 2
    # None of them ever end up `failed` just because of the rate limiting - still a normal feed result.
    assert all(r.shared["nav_checked"]["state"] == "ok" for r in results)


def test_search_history_is_recorded_after_an_attempt(tmp_path: Path):
    org = "919800030"
    bulk = {org: _bulk_row("Recorded Co AS", 30)}

    def responder(url, n):
        return 200, _search_body([])

    client = _empty_feed_client(search_responder=responder)
    connector = NavLiveConnector(search_min_interval=0.0, search_jitter_max=0.0)
    connector.prepare(client, str(tmp_path), bulk)
    ctx = make_ctx(org=org, tier="T2", client=client, registry_facts={"name": "Recorded Co AS", "aliases": [], "subunits": []})
    connector.run(ctx)

    from signalpost.models import utc_now

    assert connector._feed_index.recently_searched(org, within_days=SEARCH_HISTORY_COOLDOWN_DAYS, now=utc_now())
