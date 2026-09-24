from __future__ import annotations

from signalpost.web.candidates import Candidate
from signalpost.web.crawl import crawl_candidate
from web_fakes import FakePage, FakeHttpClient, make_ctx

HOME_HTML = """
<html><head><title>Example AS</title></head><body>
<p>Welcome to Example AS, Storgata 1, 0155 Oslo.</p>
<a href="/kontakt">Kontakt oss</a>
<a href="/om-oss">Om oss</a>
<a href="/karriere">Ledige stillinger</a>
<a href="https://facebook.com/example">Facebook</a>
</body></html>
"""

KONTAKT_HTML = "<html><head><title>Kontakt</title></head><body>Org.nr: 923 456 783. Telefon 12345678.</body></html>"
OM_OSS_HTML = "<html><head><title>Om oss</title></head><body>Example AS er et norsk selskap grunnlagt i 2001.</body></html>"


def test_crawl_fetches_homepage_and_priority_secondary_pages():
    client = FakeHttpClient(pages={
        "https://example.no/": FakePage(HOME_HTML),
        "https://example.no/kontakt": FakePage(KONTAKT_HTML),
        "https://example.no/om-oss": FakePage(OM_OSS_HTML),
    })
    ctx = make_ctx("923456783", tier="T2", client=client)
    cand = Candidate(domain="example.no", url="https://example.no/", source="registry_website", label="x", decisive=True)
    result = crawl_candidate(ctx, cand)
    kinds = {p.page_kind for p in result.pages}
    assert "homepage" in kinds
    assert "kontakt" in kinds
    assert "om-oss" in kinds
    assert not result.parked
    assert not result.js_shell


def test_crawl_respects_tier_secondary_page_budget():
    client = FakeHttpClient(pages={
        "https://example.no/": FakePage(HOME_HTML),
        "https://example.no/kontakt": FakePage(KONTAKT_HTML),
        "https://example.no/om-oss": FakePage(OM_OSS_HTML),
        "https://example.no/karriere": FakePage("<html><body>Jobb hos oss</body></html>"),
    })
    ctx = make_ctx("923456783", tier="T1", client=client)  # T1 budget = 2 secondary pages
    cand = Candidate(domain="example.no", url="https://example.no/", source="registry_website", label="x", decisive=True)
    result = crawl_candidate(ctx, cand)
    secondary = [p for p in result.pages if p.page_kind != "homepage"]
    assert len(secondary) <= 2


def test_crawl_detects_parked_domain():
    client = FakeHttpClient(pages={
        "https://parked.no/": FakePage("<html><body>This domain is for sale. Buy this domain via HugeDomains.</body></html>"),
    })
    ctx = make_ctx("923456783", tier="T2", client=client)
    cand = Candidate(domain="parked.no", url="https://parked.no/", source="name_guess", label="x")
    result = crawl_candidate(ctx, cand)
    assert result.parked
    assert len(result.pages) == 1  # no secondary crawl once parked


def test_crawl_detects_js_only_shell():
    js_shell_html = (
        '<html><head></head><body><div id="root"></div>'
        '<script src="/static/app.js"></script><script src="/static/vendor.js"></script>'
        "</body></html>"
    )
    client = FakeHttpClient(pages={"https://spa.no/": FakePage(js_shell_html)})
    ctx = make_ctx("923456783", tier="T2", client=client)
    cand = Candidate(domain="spa.no", url="https://spa.no/", source="name_guess", label="x")
    result = crawl_candidate(ctx, cand)
    assert result.js_shell


def test_crawl_records_fatal_error_when_homepage_unreachable():
    client = FakeHttpClient(pages={})
    ctx = make_ctx("923456783", tier="T2", client=client)
    cand = Candidate(domain="missing.no", url="https://missing.no/", source="name_guess", label="x")
    result = crawl_candidate(ctx, cand)
    assert result.fatal_error is not None


def test_crawl_falls_back_to_sitemap_when_no_priority_links_on_homepage():
    sitemap_xml = (
        '<?xml version="1.0"?><urlset>'
        "<url><loc>https://example.no/kontakt-oss</loc></url>"
        "<url><loc>https://example.no/produkter</loc></url>"
        "</urlset>"
    )
    client = FakeHttpClient(pages={
        "https://example.no/": FakePage("<html><body>No useful links here.</body></html>"),
        "https://example.no/sitemap.xml": FakePage(sitemap_xml, content_type="application/xml"),
        "https://example.no/kontakt-oss": FakePage(KONTAKT_HTML),
    })
    ctx = make_ctx("923456783", tier="T2", client=client)
    cand = Candidate(domain="example.no", url="https://example.no/", source="registry_website", label="x", decisive=True)
    result = crawl_candidate(ctx, cand)
    urls = [p.url for p in result.pages]
    assert "https://example.no/kontakt-oss" in urls
