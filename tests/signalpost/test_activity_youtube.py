from __future__ import annotations

from activity_testkit import FakeHttpClient, load_fixture, make_ctx

from signalpost.activity import youtube


def _shared(url: str) -> dict:
    return {
        "verified_site": {"url": "https://eksempel.no", "domain": "eksempel.no"},
        "social_links": [{"platform": "youtube", "url": url}],
    }


def test_resolve_channel_id_from_channel_url_no_fetch():
    ctx = make_ctx(client=FakeHttpClient())
    channel_id, resp = youtube.resolve_channel_id(ctx, "https://www.youtube.com/channel/UCabcdefghij1234567890")
    assert channel_id == "UCabcdefghij1234567890"
    assert resp is None  # no HTTP needed


def test_resolve_channel_id_from_handle_needs_one_fetch():
    client = FakeHttpClient()
    client.route("https://www.youtube.com/@eksempel", load_fixture("youtube_channel_page.html"))
    ctx = make_ctx(client=client)

    channel_id, resp = youtube.resolve_channel_id(ctx, "https://www.youtube.com/@eksempel")

    assert channel_id == "UCabcdefghij1234567890"
    assert resp is not None and resp.ok


def test_collect_builds_activity_claims_from_channel_feed():
    client = FakeHttpClient()
    client.route(
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghij1234567890",
        load_fixture("youtube_feed.atom.xml"),
    )
    ctx = make_ctx(client=client, shared=_shared("https://www.youtube.com/channel/UCabcdefghij1234567890"))

    result = youtube.collect(ctx)

    assert result["checked"] is True
    assert len(result["claims"]) == 3
    claim = result["claims"][0]
    assert claim.family == "activity" and claim.field == "activity_item"
    assert claim.value["platform"] == "YouTube"
    assert claim.identity_basis == "linked_from_verified_site"
    assert result["evidence"][0].extraction_method == "youtube_rss_v1"
    assert result["evidence"][0].source_class == "company_owned_platform"


def test_collect_no_youtube_links_is_noop():
    ctx = make_ctx(client=FakeHttpClient(), shared={"social_links": []})
    result = youtube.collect(ctx)
    assert result["checked"] is False
    assert result["claims"] == []


def test_collect_records_feed_fetch_error():
    client = FakeHttpClient()  # channel feed route unregistered
    ctx = make_ctx(client=client, shared=_shared("https://www.youtube.com/channel/UCabcdefghij1234567890"))

    result = youtube.collect(ctx)

    assert result["checked"] is True
    assert result["claims"] == []
    assert result["errors"]
