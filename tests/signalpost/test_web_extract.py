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
    long_text = "Vi leverer gode tjenester. " * 40
    html = f'<html><head><meta name="description" content="{long_text}"></head><body>x</body></html>'
    desc, *_ = extract_description([page(html)])
    assert len(desc) <= 400


def test_description_uses_kontoret_page_when_homepage_has_no_body_text():
    # rakark.no-style: an image-portfolio homepage with only a nav-list of project names (no
    # substantive paragraph), but /kontoret/ ("the office" -- a common Norwegian about-us-equivalent
    # slug for architecture/consulting firms) carries the real company description.
    homepage_html = "<html><body><nav>Prosjekter Kontoret Planarbeid</nav></body></html>"
    kontoret_html = (
        "<html><body><p>"
        + ("Vi er et arkitektkontor med kompetanse som dekker alle faser av byggeprosjekter. " * 2)
        + "</p></body></html>"
    )
    pages = [
        page(homepage_html, kind="homepage", url="https://example.no/"),
        page(kontoret_html, kind="kontor", url="https://example.no/kontoret/"),
    ]
    desc, source_url, *_ = extract_description(pages)
    assert desc and "arkitektkontor" in desc
    assert source_url == "https://example.no/kontoret/"


def test_description_rejects_cookie_banner_and_bare_welcome_paragraphs():
    # A cookie-consent banner or a bare "Velkommen til X" greeting is often the first >=60-char text
    # node in the DOM (rendered above the real content) -- must not be published as the description.
    html = (
        "<html><body>"
        "<div>Vi bruker cookies for a gi deg en bedre brukeropplevelse og for a analysere trafikk pa nettsiden var.</div>"
        "<p>Velkommen til Eksempel Bedrift AS, din lokale leverandor av alt du trenger her.</p>"
        "<p>"
        + ("Vi leverer skreddersydde losninger til bedrifter over hele landet siden 1998. " * 2)
        + "</p>"
        "</body></html>"
    )
    desc, *_ = extract_description([page(html)])
    assert desc is not None
    assert "cookies" not in desc.casefold()
    assert not desc.casefold().startswith("velkommen til")
    assert "skreddersydde" in desc


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
    html = '<html><body>Kontakt oss: post@acme.no eller ring 12 34 56 78.</body></html>'
    email, email_url, phone, phone_url = extract_contact([page(html, kind="kontakt")])
    assert email == "post@acme.no"


def test_contact_email_skips_placeholders_platform_addresses_and_script_strings():
    html = (
        '<html><body><script>var dsn="https://605a7b@sentry-next.wixpress.com/1"; var x="logo_copyright@www.mir.jpg";</script>'
        '<form><input placeholder="bruker@domene.no"></form><p>Skriv til user@domain.com</p>'
        '<p>Nettside levert av <a href="mailto:einar@byraa.no">Byrå</a></p>'
        '<a href="mailto:Post@Holmen.no?subject=Hei">Post@Holmen.no</a></body></html>'
    )
    email, url, *_ = extract_contact([page(html, kind="kontakt", url="https://holmen.no/kontakt")])
    assert email == "Post@Holmen.no"


def test_contact_email_prefers_the_sites_own_domain():
    html = '<html><body><p>Ring Ola: ola.nordmann@gmail.com</p><p>Firma: post@acme.no</p></body></html>'
    home = page(html, url="https://www.acme.no/")
    assert extract_contact([home])[0] == "post@acme.no"
    other = page('<html><body><p>Kontakt: ola.nordmann@gmail.com</p></body></html>', url="https://www.acme.no/")
    assert extract_contact([other])[0] == "ola.nordmann@gmail.com"


def test_description_rejects_name_only_hours_and_dead_page_text():
    name_only = '<html><head><title>Helgeland BBL</title><meta name="description" content="Helgeland BBL"></head><body>x</body></html>'
    assert extract_description([page(name_only)])[0] is None
    one_word = '<html><head><meta name="description" content="sidekart"></head><body>x</body></html>'
    assert extract_description([page(one_word)])[0] is None
    hours = '<html><head><meta name="description" content="Man - Fre: kl. 08:00 - kl. 15:00"></head><body>x</body></html>'
    assert extract_description([page(hours)])[0] is None
    archive = "<html><body><p>Beklager, ingenting ble funnet i dette arkivet. Du kan pr\u00f8ve \u00e5 s\u00f8ke etter relaterte innlegg.</p></body></html>"
    assert extract_description([page(archive)])[0] is None
    piped = ('<html><head><title>Tonjum | From the Sognefjord to the Peruvian blueberries of Trujillo</title>'
             '<meta name="description" content="| From the Sognefjord to the Peruvian blueberries of Trujillo"></head><body>x</body></html>')
    assert extract_description([page(piped)])[0] == "From the Sognefjord to the Peruvian blueberries of Trujillo"
    encoded = '<html><head><meta name="description" content="Om oss LnRiLWJ1dHRvbntjb2xvcjojZjFmMWYxfS50Yi1idXR0b24tLWxlZnR7dGV4"></head><body>x</body></html>'
    assert extract_description([page(encoded)])[0] is None
    keywords = '<html><head><meta name="description" content="Tidsystemer - uranlegg - resultattavler"></head><body>x</body></html>'
    assert extract_description([page(keywords)])[0] == "Tidsystemer - uranlegg - resultattavler"
