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
    # the off-domain item and the 2018 item (older than three years) are never fetched
    assert "https://annet.no/gjest/" not in checked and len(checked) == 3


def test_item_page_check_tolerates_a_day_or_two():
    page = ARTICLE.format(d="2026-10-06T00:30:00+02:00", body="Tekst")
    assert feeds._item_page_confirms(page, "2026-10-05")
    assert not feeds._item_page_confirms(page, "2026-09-20")
    assert not feeds._item_page_confirms("<html><body><p>Ingen dato</p></body></html>", "2026-10-05")


def _feed_ev():
    from types import SimpleNamespace
    return SimpleNamespace(extraction_method="rss_v1")


def test_a_site_with_theme_demo_posts_publishes_none_of_its_feed_items():
    ev = _feed_ev()
    items = [({"title": "Ny avtale", "published": "2026-10-05"}, ev), ({"title": "Post with Gallery", "published": "2026-04-13"}, ev)]
    assert feeds._without_demo_and_import_dates(items) == []


def test_posts_sharing_one_import_day_are_dropped():
    ev = _feed_ev()
    imported = [({"title": f"Gammel sak {i}", "published": "2021-01-27"}, ev) for i in range(3)]
    fresh = [({"title": "Ny sak", "published": "2026-10-05"}, ev)]
    assert feeds._without_demo_and_import_dates(imported + fresh) == fresh


def test_old_items_and_the_sample_post_slug_are_dropped():
    ev = _feed_ev()
    items = [({"title": "Produkter og tjenester", "published": "2017-12-14", "url": "https://x.no/2017/12/14/produkter/"}, ev),
             ({"title": "Nyheter på lager", "published": "2026-02-01", "url": "https://x.no/hei-verden/"}, ev),
             ({"title": "Ny avtale", "published": "2026-10-05", "url": "https://x.no/ny-avtale/"}, ev)]
    assert [i["title"] for i, _ in feeds._without_demo_and_import_dates(items)] == ["Ny avtale"]


def test_a_wordpress_page_is_not_a_post():
    page = ('<html><head><meta property="article:published_time" content="2026-10-05"></head>'
            '<body class="page-template-default page page-id-12"><h1>Interiør</h1></body></html>')
    assert not feeds._item_page_confirms(page, "2026-10-05")
    post = page.replace('class="page-template-default page page-id-12"', 'class="post-template-default single single-post postid-40"')
    assert feeds._item_page_confirms(post, "2026-10-05")
