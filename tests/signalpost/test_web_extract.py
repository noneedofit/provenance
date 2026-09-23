from __future__ import annotations

from signalpost.web.crawl import PageFetch
from signalpost.web.extract import (
    extract_ats_links,
    extract_contact,
    extract_description,
    extract_feed_urls,
    extract_social_links,
)


def page(html: str, *, kind: str = "homepage", url: str = "https://example.no/", links: list[str] | None = None) -> PageFetch:
    from bs4 import BeautifulSoup

    try:
        import trafilatura

        text = trafilatura.extract(html, favor_precision=True) or ""
    except Exception:
        text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    title = BeautifulSoup(html, "lxml").title
    return PageFetch(
        url=url, final_url=url, status=200, ok=True, html=html, text=text,
        title=title.get_text(strip=True) if title else "", page_kind=kind, links=links or [],
    )


def test_description_prefers_jsonld_over_meta():
    html = (
        '<html><head><meta name="description" content="Meta fallback description here.">'
        '<script type="application/ld+json">{"@type":"Organization","name":"Example",'
        '"description":"JSON-LD description of the company and what it does."}</script>'
        "</head><body>text</body></html>"
    )
    desc, url, span, method = extract_description([page(html)])
    assert desc.startswith("JSON-LD description")
    assert method == "jsonld_description_v1"


def test_description_falls_back_to_meta_then_paragraph():
    html_meta = '<html><head><meta property="og:description" content="OG description of the firm."></head><body>x</body></html>'
    desc, url, span, method = extract_description([page(html_meta)])
    assert desc == "OG description of the firm."
    assert method == "meta_description_v1"

    html_para = "<html><body><p>" + ("This is a long enough first paragraph about the company. " * 3) + "</p></body></html>"
    desc2, *_ , method2 = extract_description([page(html_para, kind="om-oss")])
    assert desc2 and method2 == "trafilatura_first_paragraph_v1"


def test_description_truncated_to_400_chars():
    long_text = "A" * 1000
    html = f'<html><head><meta name="description" content="{long_text}"></head><body>x</body></html>'
    desc, *_ = extract_description([page(html)])
    assert len(desc) <= 400


def test_social_links_normalized_and_share_links_excluded():
    html = (
        "<html><body>"
        '<a href="https://www.facebook.com/examplecompany">FB</a>'
        '<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>'
        '<a href="https://www.linkedin.com/company/example-co">LI</a>'
        '<a href="https://twitter.com/intent/tweet?text=x">tweet intent</a>'
        '<a href="https://www.youtube.com/@examplecompany">YT</a>'
        "</body></html>"
    )
    links = extract_social_links([page(html)])
    platforms = {l["platform"] for l in links}
    assert "facebook" in platforms
    assert "linkedin" in platforms
    assert "youtube" in platforms
    urls = {l["url"] for l in links}
    assert not any("sharer" in u for u in urls)
    assert not any("intent" in u for u in urls)


def test_ats_links_detected_by_domain():
    html = '<html><body><a href="https://example.teamtailor.com/jobs">Ledige stillinger</a></body></html>'
    links = extract_ats_links([page(html, kind="karriere")])
    assert links and links[0]["platform"] == "teamtailor"


def test_feed_urls_detected_from_link_rel_alternate():
    html = '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head><body></body></html>'
    feeds = extract_feed_urls([page(html)])
    assert feeds == ["https://example.no/feed.xml"]


def test_contact_email_and_phone_extraction():
    html = '<html><body>Kontakt oss: post@example.no eller ring 12 34 56 78.</body></html>'
    email, email_url, phone, phone_url = extract_contact([page(html, kind="kontakt")])
    assert email == "post@example.no"
