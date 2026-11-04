from __future__ import annotations

from activity_testkit import FakeHttpClient, load_fixture, make_ctx

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
    html = '<html><body><article><h1>Ny avtale</h1><time datetime="2026-01-05T10:00:00Z">5. januar</time></article></body></html>'
    iso, span = feeds.extract_date_from_html(html)
    assert iso == "2026-01-05"
    assert "time" in span


def test_extract_date_from_html_ignores_dates_outside_an_article():
    # A product or menu page with a stray <time> (opening hours, a footer) or a date in the text is not news.
    loose = '<html><body><h1>Lunsjmeny</h1><time datetime="2026-01-05">5. januar</time><p>Oppdatert 12. mars 2026</p></body></html>'
    assert feeds.extract_date_from_html(loose) == (None, None)
    product = ('<html><head><meta property="og:type" content="product">'
               '<meta property="article:published_time" content="2026-01-05"></head><body></body></html>')
    assert feeds.extract_date_from_html(product) == (None, None)
    jsonld_product = '<html><head><script type="application/ld+json">{"@type": "Product", "datePublished": "2026-01-05"}</script></head></html>'
    assert feeds.extract_date_from_html(jsonld_product) == (None, None)
    # Some CMSs type every page "website": an explicit article publish time still counts.
    cms = ('<html><head><meta property="og:type" content="website">'
           '<meta property="article:published_time" content="2026-02-01T08:00:00Z"></head></html>')
    assert feeds.extract_date_from_html(cms)[0] == "2026-02-01"


def test_parse_feed_atom_never_uses_updated_as_published():
    xml = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Om oss</title>'
           '<link href="https://example.no/om-oss"/><updated>2026-10-01T00:00:00Z</updated></entry></feed>')
    items = feeds.parse_feed(xml)
    assert items and items[0]["published"] is None


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


def test_parse_wp_rest_posts():
    body = (
        '[{"title": {"rendered": "Ny kontrakt signert"}, "link": "https://eksempel.no/2026/ny-kontrakt",'
        ' "date": "2026-02-10T09:00:00"},'
        ' {"title": {"rendered": "Uten dato"}, "link": "https://eksempel.no/uten-dato", "date": null}]'
    )
    items = feeds.parse_wp_rest_posts(body)
    assert len(items) == 1
    assert items[0]["title"] == "Ny kontrakt signert"
    assert items[0]["published"] == "2026-02-10"


def test_parse_wp_rest_posts_ignores_malformed_json():
    assert feeds.parse_wp_rest_posts("not json") == []
    assert feeds.parse_wp_rest_posts('{"not": "a list"}') == []


def test_collect_falls_back_to_wordpress_feed_when_w3_found_nothing():
    # No feed_urls/news_urls from W3 (the priority-page picker never fetched a news page), but the
    # site is a WordPress site with a working /feed/.
    client = FakeHttpClient()
    client.route("https://eksempel.no/feed/", load_fixture("generic_news.rss.xml"))
    ctx = make_ctx(client=client, shared={"verified_site": {"url": "https://eksempel.no/", "domain": "eksempel.no"}})

    result = feeds.collect(ctx)

    assert result["checked"] is True
    assert len(result["claims"]) == 3
    assert {"url": "https://eksempel.no/feed/", "org": "999888777", "purpose": "site_feed"} in [
        {"url": c["url"], "org": c["org"], "purpose": c["purpose"]} for c in client.calls
    ]


def test_collect_discovers_news_link_from_already_fetched_page():
    # W3 fetched the homepage (for identity verification) and found a "/blogg" link on it, but never
    # fetched /blogg itself (crowded out by kontakt/om-oss in the tier's secondary-page budget). The
    # link-scan step runs BEFORE the WordPress guess, so /feed/ should never even be called here.
    client = FakeHttpClient()
    client.route("https://eksempel.no/blogg", load_fixture("news_article.html"))
    ctx = make_ctx(client=client, shared={
        "verified_site": {"url": "https://eksempel.no/", "domain": "eksempel.no"},
        "site_pages": [{"url": "https://eksempel.no/", "links": ["https://eksempel.no/blogg", "https://eksempel.no/kontakt"]}],
    })

    result = feeds.collect(ctx)

    assert result["checked"] is True
    assert len(result["claims"]) == 1
    assert result["claims"][0].value["url"] == "https://eksempel.no/blogg"
    called_urls = {c["url"] for c in client.calls}
    assert "https://eksempel.no/feed/" not in called_urls


def test_collect_falls_back_to_wordpress_feed_when_link_scan_finds_nothing():
    # No feed_urls/news_urls and no matching link on any already-fetched page -> falls through to the
    # WordPress /feed/ guess as the last resort.
    client = FakeHttpClient()
    client.route("https://eksempel.no/feed/", load_fixture("generic_news.rss.xml"))
    ctx = make_ctx(client=client, shared={
        "verified_site": {"url": "https://eksempel.no/", "domain": "eksempel.no"},
        "site_pages": [{"url": "https://eksempel.no/", "links": ["https://eksempel.no/kontakt"]}],
    })

    result = feeds.collect(ctx)

    assert result["checked"] is True
    assert len(result["claims"]) == 3


def test_collect_extra_discovery_never_runs_when_free_sources_already_found_something():
    # Free feed_urls already produced claims -> no WordPress/link-scan probes should fire at all.
    client = FakeHttpClient()
    client.route("https://eksempel.no/rss", load_fixture("generic_news.rss.xml"))
    ctx = make_ctx(client=client, shared={
        "verified_site": {"url": "https://eksempel.no/", "domain": "eksempel.no"},
        "feed_urls": ["https://eksempel.no/rss"],
    })

    result = feeds.collect(ctx)

    assert len(result["claims"]) == 3
    called_urls = {c["url"] for c in client.calls}
    assert "https://eksempel.no/feed/" not in called_urls


def test_collect_extra_discovery_spends_at_most_the_budget():
    client = FakeHttpClient()  # every probe 404s / not routed
    ctx = make_ctx(client=client, shared={
        "verified_site": {"url": "https://eksempel.no/", "domain": "eksempel.no"},
        "site_pages": [{"url": "https://eksempel.no/", "links": [
            "https://eksempel.no/nyheter", "https://eksempel.no/aktuelt", "https://eksempel.no/presse",
        ]}],
    })

    result = feeds.collect(ctx)

    assert result["claims"] == []
    # 3 link candidates on offer, but the budget caps spend at EXTRA_DISCOVERY_BUDGET requests.
    assert len(client.calls) <= feeds.EXTRA_DISCOVERY_BUDGET


def test_blog_comments_and_placeholder_posts_are_not_company_activity():
    from signalpost.activity.feeds import is_company_item

    assert not is_company_item({"title": "Comment on Oversize Sweatshirt by Willie Clark", "url": "https://x.no/p/#comment-77"})
    assert not is_company_item({"title": "Kommentar til Solplassen av Ola", "url": "https://x.no/solplassen/"})
    assert not is_company_item({"title": "Hello world!", "url": "https://x.no/hello-world/"})
    assert not is_company_item({"title": "Nytt prosjekt", "url": "https://x.no/a/"}, "https://x.no/comments/feed/")
    assert is_company_item({"title": "Vi åpner ny avdeling i Bergen", "url": "https://x.no/nyheter/ny-avdeling"})


def test_formal_replies_are_still_company_activity():
    from signalpost.activity.feeds import is_company_item

    assert is_company_item({"title": "Svar på høring om ny avfallsforskrift", "url": "https://x.no/nyheter/horing"})


def test_shop_products_are_not_news():
    item = {"url": "https://gullsmed.example/produkt/ring-gull/", "title": "Ring i gull", "published": "2026-10-01"}
    assert not feeds.is_company_item(item, "https://gullsmed.example/feed/")
    post = {"url": "https://gullsmed.example/2026/10/nye-ringer/", "title": "Nye ringer", "published": "2026-10-01"}
    assert feeds.is_company_item(post, "https://gullsmed.example/feed/")
    assert not feeds.is_company_item(post, "https://gullsmed.example/product/feed/")
    assert not feeds.is_company_item(post, "https://gullsmed.example/feed/?post_type=product")
    assert feeds.is_company_item(post, "https://gullsmed.example/kategori/nyheter/feed/")


def test_a_feed_with_spam_posts_is_a_hacked_site():
    assert feeds.feed_is_compromised([{"title": "Velkommen"}, {"title": "Cheap Birkin replica bags"}])
    assert feeds.feed_is_compromised([{"title": "Best AliExpress deals 2026"}])
    assert not feeds.feed_is_compromised([{"title": "Vi har åpnet ny klinikk i Skien"}])


def test_spam_words_a_company_can_use_need_two_items():
    assert not feeds.feed_is_compromised([{"title": "Julebord på Casino Steinkjer"}, {"title": "Nye åpningstider"}])
    assert feeds.feed_is_compromised([{"title": "Best online casino bonus"}, {"title": "Replica watches for sale"}])


def test_a_hacked_wordpress_rest_api_is_not_news():
    import json as _json
    body = _json.dumps([
        {"link": "https://klinikk.example/2026/10/aapnet/", "date": "2026-10-01T10:00:00", "title": {"rendered": "Vi har åpnet"}},
        {"link": "https://klinikk.example/2026/10/bags/", "date": "2026-10-02T10:00:00", "title": {"rendered": "Cheap Birkin bags"}},
    ])
    rest = "https://klinikk.example/wp-json/wp/v2/posts"
    client = FakeHttpClient()
    client.route(rest, body.encode())
    ctx = make_ctx(client=client)
    result = {"checked": False, "errors": []}
    assert feeds._process_wp_rest_url(ctx, rest, set(), result) == [] and result["errors"] == []


def test_an_edit_date_is_not_a_publication_date():
    html = ('<html><head><meta property="og:type" content="article"></head><body><article>'
            '<span class="endret-dato"><time datetime="2026-10-30">30. okt</time></span></article></body></html>')
    assert feeds.extract_date_from_html(html) == (None, None)
    html2 = html.replace('class="endret-dato"', 'class="artikkel-dato"')
    assert feeds.extract_date_from_html(html2)[0] == "2026-10-30"


def test_a_wordpress_entry_date_marked_published_and_updated_is_the_publication_date():
    html = ('<html><body><article><time class="entry-date published updated" datetime="2026-09-12T08:00:00+02:00">'
            '12. september</time></article></body></html>')
    assert feeds.extract_date_from_html(html)[0] == "2026-09-12"


def test_page_builder_drafts_and_test_posts_are_not_news():
    for title in ("Elementor #2569", "Test", "Untitled", "Kladd"):
        assert not feeds.is_company_item({"url": "https://x.example/a/", "title": title}), title
    assert feeds.is_company_item({"url": "https://x.example/a/", "title": "Testing av nye maskiner"})
