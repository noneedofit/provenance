from __future__ import annotations

from activity_testkit import FakeHttpClient, make_ctx

from signalpost.activity import feeds

FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>X</title>
<item><title>Ny avtale med kommunen</title><link>https://firma.no/ny-avtale/</link><pubDate>Mon, 05 Oct 2026 10:00:00 +0000</pubDate></item>
<item><title>Ålesund</title><link>https://firma.no/lokasjoner/alesund/</link><pubDate>Fri, 12 Dec 2025 10:00:00 +0000</pubDate></item>
<item><title>Overskrifter</title><link>https://firma.no/2018/04/13/overskrifter/</link><pubDate>Fri, 13 Apr 2018 10:00:00 +0000</pubDate></item>
<item><title>Gjesteinnlegg</title><link>https://annet.no/gjest/</link><pubDate>Thu, 01 Oct 2026 10:00:00 +0000</pubDate></item>
<item><title>Feil dato</title><link>https://firma.no/feil-dato/</link><pubDate>Thu, 01 Oct 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""

ARTICLE = '<html><head><meta property="article:published_time" content="{d}"></head><body><article><h1>T</h1><p>{body}</p></article></body></html>'


def test_feed_items_are_published_only_when_their_own_page_confirms_them():
    client = FakeHttpClient(item_pages_exist=False)
    client.route("https://firma.no/feed/", FEED.encode())
    client.route("https://firma.no/ny-avtale/", ARTICLE.format(d="2026-10-05T10:00:00Z", body="Vi har signert.").encode())
    client.route("https://firma.no/lokasjoner/alesund/", b"<html><body><h1>Alesund</h1><p>Adresse</p></body></html>")
    client.route("https://firma.no/2018/04/13/overskrifter/", ARTICLE.format(d="2018-04-13", body="Lorem ipsum dolor sit amet").encode())
    client.route("https://firma.no/feil-dato/", ARTICLE.format(d="2024-01-01", body="Gammel sak").encode())
    ctx = make_ctx(client=client, shared={
        "verified_site": {"url": "https://firma.no", "domain": "firma.no"},
        "feed_urls": ["https://firma.no/feed/"],
    })
    result = feeds.collect(ctx)
    urls = [c.value["url"] for c in result["claims"]]
    assert urls == ["https://firma.no/ny-avtale/"]
    # the off-domain item is never fetched; each own-domain item page is fetched once
    checked = [c["url"] for c in client.calls if c["purpose"] == "site_news_item_check"]
    assert "https://annet.no/gjest/" not in checked and len(checked) == 4


def test_item_page_check_tolerates_a_day_or_two():
    page = ARTICLE.format(d="2026-10-06T00:30:00+02:00", body="Tekst")
    assert feeds._item_page_confirms(page, "2026-10-05")
    assert not feeds._item_page_confirms(page, "2026-09-20")
    assert not feeds._item_page_confirms("<html><body><p>Ingen dato</p></body></html>", "2026-10-05")
