from __future__ import annotations

from conftest import FakeHttpClient, load_fixture, make_ctx

from signalpost.activity import feeds


def test_parse_feed_rss_generic():
    xml = load_fixture("generic_news.rss.xml").decode("utf-8")
    items = feeds.parse_feed(xml)
    assert len(items) == 3
    assert all(i["published"] for i in items)
    assert all(i["url"] and i["url"].startswith("http") for i in items)


def test_parse_feed_atom_youtube():
    xml = load_fixture("youtube_feed.atom.xml").decode("utf-8")
    items = feeds.parse_feed(xml)
    assert len(items) == 3
    assert items[0]["url"].startswith("https://www.youtube.com/watch?v=")
    assert items[0]["published"].startswith("2026-")


def test_parse_norwegian_date():
    assert feeds.parse_norwegian_date("Publisert 12. mars 2026 av redaksjonen") == "2026-03-12"
    assert feeds.parse_norwegian_date("Dato: 12.03.2026") == "2026-03-12"
    assert feeds.parse_norwegian_date("no date here") is None


def test_extract_date_from_html_prefers_time_tag():
    html = '<html><body><time datetime="2026-01-05T10:00:00Z">5. januar</time></body></html>'
    iso, span = feeds.extract_date_from_html(html)
    assert iso == "2026-01-05"
    assert "time" in span


def test_extract_date_from_html_jsonld_and_norwegian_fallback():
    html = load_fixture("news_article.html").decode("utf-8")
    iso, span = feeds.extract_date_from_html(html)
    assert iso == "2026-03-12"


def test_extract_date_from_html_undated_returns_none():
    html = "<html><body><p>No date anywhere on this page.</p></body></html>"
    iso, span = feeds.extract_date_from_html(html)
    assert iso is None
    assert span is None


def test_collect_builds_claims_from_feed_and_news_page():
    client = FakeHttpClient()
    client.route("https://eksempel.no/rss", load_fixture("generic_news.rss.xml"))
    client.route("https://eksempel.no/aktuelt/lansering", load_fixture("news_article.html"))
    ctx = make_ctx(client=client, shared={
        "verified_site": {"url": "https://eksempel.no", "domain": "eksempel.no"},
        "feed_urls": ["https://eksempel.no/rss"],
        "news_urls": ["https://eksempel.no/aktuelt/lansering"],
    })

    result = feeds.collect(ctx)

    assert result["checked"] is True
    assert result["errors"] == []
    assert len(result["claims"]) == 4  # 3 from rss + 1 dated news page
    for claim in result["claims"]:
        assert claim.family == "activity"
        assert claim.field == "activity_item"
        assert claim.availability == "available"
        assert claim.identity_basis == "linked_from_verified_site"
        assert claim.evidence_ids
    # sorted newest first
    dates = [c.value["published"] for c in result["claims"]]
    assert dates == sorted(dates, reverse=True)


def test_collect_drops_undated_news_pages():
    client = FakeHttpClient()
    client.route("https://eksempel.no/undated", b"<html><body><p>no date here</p></body></html>")
    ctx = make_ctx(client=client, shared={"news_urls": ["https://eksempel.no/undated"]})

    result = feeds.collect(ctx)

    assert result["checked"] is True
    assert result["claims"] == []


def test_collect_no_sources_is_a_noop():
    ctx = make_ctx(client=FakeHttpClient(), shared={})
    result = feeds.collect(ctx)
    assert result["checked"] is False
    assert result["claims"] == []


def test_collect_records_fetch_errors():
    client = FakeHttpClient()  # route not registered -> 'not_found_in_fake' error
    ctx = make_ctx(client=client, shared={"feed_urls": ["https://eksempel.no/missing-rss"]})

    result = feeds.collect(ctx)

    assert result["checked"] is True
    assert result["claims"] == []
    assert result["errors"][0]["url"] == "https://eksempel.no/missing-rss"
