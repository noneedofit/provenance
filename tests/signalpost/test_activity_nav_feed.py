from __future__ import annotations

from pathlib import Path

from nav_live_testkit import FAKE_TOKEN, FEED_URL, TOKEN_URL, FakeNavHttpClient

from signalpost.activity.nav_feed import NavFeedIndex, walk_feed


def _entry(uuid: str, status: str, *, business_name: str = "Example AS", title: str = "Job", municipal: str = "OSLO") -> dict:
    return {"_feed_entry": {"uuid": uuid, "status": status, "title": title, "businessName": business_name,
                             "municipal": municipal, "sistEndret": "2026-09-20T00:00:00+02:00"}}


def test_window_walk_seeds_if_modified_since_once_and_supersession_removes_closed_ads(tmp_path: Path):
    client = FakeNavHttpClient(pages={
        None: {"id": "p1", "items": [_entry("A", "ACTIVE"), _entry("B", "ACTIVE", business_name="Other AS")], "next_id": "p2"},
        "p2": {"id": "p2", "items": [_entry("A", "INACTIVE"), _entry("C", "ACTIVE", business_name="Third AS")], "next_id": None},
    })
    index = NavFeedIndex.open(tmp_path)

    stats = walk_feed(client, lambda force=False: FAKE_TOKEN, index, window_days=60, max_pages=200, max_seconds=60)

    assert stats["pages"] == 2
    assert stats["reached_end"] is True
    assert stats["resumed"] is False
    assert index.is_complete() is True
    assert index.get_cursor() == "p2"

    # A was ACTIVE on page 1 but a later INACTIVE for the same uuid on page 2 supersedes it - removed.
    uuids = {row["uuid"] for row in index.match({"example", "other", "third"})}
    assert "A" not in uuids
    assert uuids == {"B", "C"}

    # The very first request (root page, fresh index, no saved cursor) carries If-Modified-Since; later
    # page requests don't. Both carry the bearer token.
    feed_calls = [c for c in client.calls if c.url == FEED_URL or c.url.startswith(FEED_URL + "/")]
    assert len(feed_calls) == 2
    assert "If-Modified-Since" in (feed_calls[0].headers or {})
    assert feed_calls[0].headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert "If-Modified-Since" not in (feed_calls[1].headers or {})


def test_state_resume_continues_from_saved_cursor_without_if_modified_since(tmp_path: Path):
    first_client = FakeNavHttpClient(pages={
        None: {"id": "p1", "items": [_entry("A", "ACTIVE")], "next_id": "p2"},
        "p2": {"id": "p2", "items": [_entry("B", "ACTIVE")], "next_id": None},
    })
    index = NavFeedIndex.open(tmp_path)
    walk_feed(first_client, lambda force=False: FAKE_TOKEN, index, window_days=60, max_pages=200, max_seconds=60)
    index.close()

    # A later run opens the SAME state-dir; NAV has appended a new page after p2 in the meantime.
    resumed_index = NavFeedIndex.open(tmp_path)
    second_client = FakeNavHttpClient(pages={
        "p2": {"id": "p2", "items": [_entry("B", "ACTIVE")], "next_id": "p3"},  # re-fetched: now has a next page
        "p3": {"id": "p3", "items": [_entry("D", "ACTIVE", business_name="Fourth AS")], "next_id": None},
    })

    stats = walk_feed(second_client, lambda force=False: FAKE_TOKEN, resumed_index, window_days=60, max_pages=200, max_seconds=60)

    assert stats["resumed"] is True
    assert stats["reached_end"] is True
    assert resumed_index.get_cursor() == "p3"
    # Resuming never re-seeds If-Modified-Since - it starts directly at the saved page id.
    assert all("If-Modified-Since" not in (c.headers or {}) for c in second_client.calls)
    first_call = second_client.calls[0]
    assert first_call.url == f"{FEED_URL}/p2"

    # A/B (from the first walk, never closed) are still indexed under "example" - persistence works;
    # D (from this resumed walk) is newly indexed under "fourth".
    assert {row["uuid"] for row in resumed_index.match({"example"})} == {"A", "B"}
    assert {row["uuid"] for row in resumed_index.match({"fourth"})} == {"D"}


def test_budget_and_page_cap_leaves_a_partial_incomplete_index(tmp_path: Path):
    client = FakeNavHttpClient(pages={
        None: {"id": "p1", "items": [_entry("A", "ACTIVE")], "next_id": "p2"},
        "p2": {"id": "p2", "items": [_entry("B", "ACTIVE")], "next_id": "p3"},
        "p3": {"id": "p3", "items": [_entry("C", "ACTIVE")], "next_id": "p4"},
        "p4": {"id": "p4", "items": [_entry("D", "ACTIVE")], "next_id": None},
    })
    index = NavFeedIndex.open(tmp_path)

    stats = walk_feed(client, lambda force=False: FAKE_TOKEN, index, window_days=60, max_pages=2, max_seconds=60)

    assert stats["pages"] == 2
    assert stats["reached_end"] is False
    assert index.is_complete() is False  # never claim full coverage when the cap stopped the walk early


def test_name_matching_uses_normalized_business_name(tmp_path: Path):
    index = NavFeedIndex.open(tmp_path)
    index.upsert_active(uuid="x1", title="Selger", business_name="ATRIUM PEOPLE AS", municipal="OSLO",
                         sist_endret="2026-09-01T00:00:00Z", updated_at="2026-09-24T00:00:00Z")
    index.commit()

    from signalpost.activity._common import norm_title

    matches = index.match({norm_title("Atrium People")})
    assert len(matches) == 1
    assert matches[0]["uuid"] == "x1"

    assert index.match({norm_title("Completely Different Name")}) == []


def test_fuzzy_match_catches_trade_name_and_department_suffix_but_not_unrelated_names(tmp_path: Path):
    from signalpost.activity._common import norm_title

    index = NavFeedIndex.open(tmp_path)
    # A shortened/partial businessName (e.g. NAV truncates or the poster dropped a word) - same
    # meaningful tokens, not an exact string match against the full legal name.
    index.upsert_active(uuid="y1", title="Selger", business_name="Olsen Nauen", municipal="BARKAKER",
                         sist_endret="2026-09-01T00:00:00Z", updated_at="2026-09-24T00:00:00Z")
    index.upsert_active(uuid="y2", title="Baker", business_name="Trade Name AS avd Bergen", municipal="BERGEN",
                         sist_endret="2026-09-01T00:00:00Z", updated_at="2026-09-24T00:00:00Z")
    index.upsert_active(uuid="y3", title="Unrelated job", business_name="Totally Unrelated Company AS",
                         municipal="OSLO", sist_endret="2026-09-01T00:00:00Z", updated_at="2026-09-24T00:00:00Z")
    index.commit()

    # "Olsen Nauen Klokkestøperi AS" (legal name) doesn't exact-match the ad's shorter businessName
    # "Olsen Nauen" - but a fuzzy token-overlap match (>= 60% of the smaller token set) still finds it.
    legal_name_norm = norm_title("Olsen Nauen Klokkestøperi AS")
    assert index.match({legal_name_norm}) == []  # exact match misses it
    fuzzy = index.match_fuzzy({legal_name_norm})
    assert {m["uuid"] for m in fuzzy} == {"y1"}

    # A subunit/trade name "Trade Name Bergen" should fuzzy-match "Trade Name AS avd Bergen".
    fuzzy2 = index.match_fuzzy({norm_title("Trade Name Bergen")})
    assert {m["uuid"] for m in fuzzy2} == {"y2"}

    # An unrelated name must never fuzzy-match just because it shares one common word.
    fuzzy3 = index.match_fuzzy({norm_title("Totally Different")})
    assert fuzzy3 == []


def test_missing_token_short_circuits_the_walk_without_a_request(tmp_path: Path):
    client = FakeNavHttpClient(pages={None: {"id": "p1", "items": [], "next_id": None}})
    index = NavFeedIndex.open(tmp_path)

    stats = walk_feed(client, lambda force=False: None, index, window_days=60, max_pages=200, max_seconds=60)

    assert stats["pages"] == 0
    assert stats["errors"] == ["no_token"]
    assert not any(c.url == FEED_URL for c in client.calls)


def test_complete_flag_is_committed_and_survives_a_fresh_connection(tmp_path: Path):
    # Regression: `set_complete()` was written but never committed, so a brand-new connection to the
    # same sqlite file (e.g. the next process that opens this state-dir) wouldn't see it - only the
    # per-page cursor/ads writes (committed inside the loop) survived a process exit.
    client = FakeNavHttpClient(pages={None: {"id": "p1", "items": [_entry("A", "ACTIVE")], "next_id": None}})
    index = NavFeedIndex.open(tmp_path)
    stats = walk_feed(client, lambda force=False: FAKE_TOKEN, index, window_days=60, max_pages=200, max_seconds=60)
    assert stats["reached_end"] is True
    index.close()  # drops the connection without an explicit extra commit - relies on walk_feed's own

    fresh = NavFeedIndex.open(tmp_path)
    assert fresh.is_complete() is True


def test_match_is_safe_under_concurrent_reads_and_writes(tmp_path: Path):
    # Regression: a single sqlite3.Connection used from many worker threads at once (one shared
    # NavFeedIndex, read by every company's thread via .match()) raised
    # "sqlite3.InterfaceError: bad parameter or other API misuse" under real pipeline concurrency (12
    # workers) on a 131-company live run - check_same_thread=False only lifts the same-thread *check*,
    # it doesn't make concurrent statements on one connection safe. NavFeedIndex now serializes every
    # method behind its own lock.
    import threading

    index = NavFeedIndex.open(tmp_path)
    for i in range(20):
        index.upsert_active(uuid=f"u{i}", title="Job", business_name=f"Company {i} AS", municipal="OSLO",
                             sist_endret="2026-09-01T00:00:00Z", updated_at="2026-09-24T00:00:00Z")
    index.commit()

    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                index.match({f"company {i}"})
                index.upsert_active(uuid=f"w{n}-{i}", title="Job", business_name=f"Worker {n} Co {i} AS",
                                     municipal="OSLO", sist_endret="2026-09-01T00:00:00Z",
                                     updated_at="2026-09-24T00:00:00Z")
                index.count()
        except Exception as exc:  # pragma: no cover - only hit when the regression reappears
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []


def test_segmented_walk_applies_slices_in_time_order(tmp_path):
    """An ad opened in an early time slice and closed in a later one ends closed, whatever order the
    parallel slices finish in; the walk covers the live tip and marks the index complete."""
    import json
    from datetime import datetime, timedelta, timezone

    from signalpost.activity.nav_feed import FEED_URL, NavFeedIndex, walk_feed_segmented
    from signalpost.context import Response

    now = datetime.now(timezone.utc)

    def item(uuid, status, days_ago):
        when = (now - timedelta(days=days_ago)).isoformat()
        return {"id": uuid, "date_modified": when,
                "_feed_entry": {"uuid": uuid, "status": status, "title": "t", "businessName": "ACME AS", "sistEndret": when}}

    pages = {
        "early": {"id": "early", "items": [item("a", "ACTIVE", 50), item("b", "ACTIVE", 45)], "next_id": "late"},
        "late": {"id": "late", "items": [item("a", "INACTIVE", 5), item("c", "ACTIVE", 2)], "next_id": None},
    }

    class Client:
        def get(self, url, *, headers=None, **_):
            page = url.rsplit("/", 1)[-1] if url != FEED_URL else None
            if page is None:
                since = datetime.strptime(headers["If-Modified-Since"], "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
                page = "early" if since < now - timedelta(days=30) else "late"
            body = json.dumps(pages[page]).encode()
            return Response(url=url, final_url=url, redirect_chain=[url], status=200, headers={}, body=body,
                            retrieved_at="2026-10-06T00:00:00Z", content_sha256="x", elapsed_ms=1, requests_used=1)

    index = NavFeedIndex.open(tmp_path)
    stats = walk_feed_segmented(Client(), lambda force=False: "token", index, window_days=60, segments=4)
    active = {r["uuid"] for r in index._conn.execute("SELECT uuid FROM active_ads")}
    assert active == {"b", "c"}
    assert stats["reached_end"] and index.is_complete()
