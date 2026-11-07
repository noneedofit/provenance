from __future__ import annotations

from activity_testkit import FakeHttpClient

from signalpost.activity import news_search
from signalpost.context import CompanyContext

ORG = "936199208"
ARTICLE = "https://www.bt.no/nyheter/i/abc/fruktkongen"
RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Fruktkongen AS: Vi hadde rekordaar</title><link>http://www.bing.com/news/apiclick.aspx?ref=x&amp;url=https%3a%2f%2fwww.bt.no%2fnyheter%2fi%2fabc%2ffruktkongen&amp;c=1</link><description>Fruktkongen omsatte for mer</description></item>
<item><title>Fruktkongen bond price</title><link>https://markets.example.com/bonds/fruktkongen</link><description>Fruktkongen</description></item>
<item><title>Kongen og frukt</title><link>https://www.vg.no/x</link><description>noe annet</description></item>
</channel></rss>"""


def page(body: str, date: str = '<meta property="article:published_time" content="2024-02-29T17:03:00Z">') -> bytes:
    return f"<html><head>{date}<title>t</title></head><body><article>{body}</article></body></html>".encode()


def _ctx(client, name="FRUKTKONGEN AS"):
    return CompanyContext(org=ORG, run_id="t", now="2026-11-08T00:00:00Z", tier="T1", bulk={}, client=client,
                          shared={"registry_facts": {"name": name}})


def _client(article: bytes) -> FakeHttpClient:
    client = FakeHttpClient()
    client.routes = {}
    client.route(news_search.BING_NEWS_RSS.format(query=news_search.urllib.parse.urlencode(
        {"q": '"FRUKTKONGEN"', "format": "rss", "setmkt": "nb-NO", "setlang": "nb"})), RSS)
    client.route(ARTICLE, article)
    return client


def test_core_name():
    assert news_search.core_name("FRUKTKONGEN AS") == "FRUKTKONGEN"
    assert news_search.core_name("AS SLETTEBAKKVEIEN 104") == "SLETTEBAKKVEIEN 104"
    assert news_search.core_name("ODDA RUTEBUSS AS I LIKVIDASJON") == "ODDA RUTEBUSS"


def test_publishes_article_naming_full_legal_name(monkeypatch):
    monkeypatch.setattr(news_search, "BING_INTERVAL_S", 0)
    r = news_search.collect(_ctx(_client(page("Fruktkongen AS hadde rekordår i fjor."))))
    assert len(r["claims"]) == 1
    c = r["claims"][0]
    assert c.value == {"title": "Fruktkongen AS: Vi hadde rekordaar", "url": ARTICLE, "published": "2024-02-29", "source": "bt.no"}
    assert c.identity_basis == "legal_name_on_source"
    assert r["evidence"][0].source_class == "news_media"


def test_org_number_on_page_counts(monkeypatch):
    monkeypatch.setattr(news_search, "BING_INTERVAL_S", 0)
    r = news_search.collect(_ctx(_client(page("Fruktkongen (org.nr. 936 199 208) vokser."))))
    assert r["claims"][0].identity_basis == "org_number_on_source"


def test_namesake_without_legal_form_is_rejected(monkeypatch):
    monkeypatch.setattr(news_search, "BING_INTERVAL_S", 0)
    assert news_search.collect(_ctx(_client(page("Fruktkongen er en bok om frukt."))))["claims"] == []


def test_page_without_publication_date_is_rejected(monkeypatch):
    monkeypatch.setattr(news_search, "BING_INTERVAL_S", 0)
    assert news_search.collect(_ctx(_client(page("Fruktkongen AS vokser.", date=""))))["claims"] == []


def test_off_switch_and_registry_only(monkeypatch):
    client = FakeHttpClient()
    monkeypatch.setenv("SIGNALPOST_NEWS_SEARCH", "0")
    assert news_search.collect(_ctx(client))["claims"] == []
    monkeypatch.delenv("SIGNALPOST_NEWS_SEARCH")
    ctx = _ctx(client)
    ctx.shared["registry_only"] = True
    assert news_search.collect(ctx)["claims"] == [] and client.calls == []
