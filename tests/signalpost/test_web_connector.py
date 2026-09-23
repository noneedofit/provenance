from __future__ import annotations

from signalpost.web.connector import WebConnector
from web_fakes import FakePage, FakeHttpClient, make_ctx

OUR_ORG = "923456783"

HOME_HTML = """
<html><head><title>Example AS</title>
<meta property="og:site_name" content="Example AS">
</head><body>
<p>Example AS leverer tjenester i Oslo.</p>
<a href="/kontakt">Kontakt oss</a>
<a href="https://www.linkedin.com/company/example-as">LinkedIn</a>
</body></html>
"""

KONTAKT_HTML = "<html><head><title>Kontakt</title></head><body>Org.nr: 923 456 783. post@example.no</body></html>"


def test_connector_publishes_exact_website_and_profiles_and_description():
    client = FakeHttpClient(pages={
        "https://example.no/": FakePage(HOME_HTML),
        "https://example.no/kontakt": FakePage(KONTAKT_HTML),
    })
    ctx = make_ctx(
        OUR_ORG, tier="T2", client=client,
        registry_facts={"name": "EXAMPLE AS", "website": "example.no", "street": "", "postcode": "", "city": ""},
    )
    result = WebConnector().run(ctx)

    assert result.families["website"].availability == "available"
    website_claims = [c for c in result.claims if c.family == "website" and c.field == "official_website"]
    assert len(website_claims) == 1
    assert website_claims[0].relationship == "exact"
    assert website_claims[0].value["domain"] == "example.no"
    assert website_claims[0].evidence_ids
    for eid in website_claims[0].evidence_ids:
        assert any(e.evidence_id == eid for e in result.evidence)

    profile_claims = [c for c in result.claims if c.family == "profiles"]
    assert profile_claims and profile_claims[0].value["platform"] == "linkedin"

    assert result.shared["verified_site"]["domain"] == "example.no"
    assert result.shared["web_attempts"]


def test_connector_not_available_when_no_candidates_resolve():
    client = FakeHttpClient(pages={})
    ctx = make_ctx(OUR_ORG, tier="T2", client=client, registry_facts={"name": "NOWHERE AS"})
    result = WebConnector().run(ctx)
    assert result.families["website"].availability == "not_available"
    assert not any(c.family == "website" and c.availability == "available" for c in result.claims)


def test_connector_marks_related_site_ambiguous_not_exact():
    conflict_org = "934567897"
    html = f"<html><body>7-Eleven Norge. Org.nr {conflict_org}. Franchisetaker: BUTIKKDRIFT EKSEMPEL AS.</body></html>"
    client = FakeHttpClient(pages={"https://7-eleven.no/": FakePage(html)})
    ctx = make_ctx(
        OUR_ORG, tier="T2", client=client,
        registry_facts={"name": "BUTIKKDRIFT EKSEMPEL AS", "website": "7-eleven.no"},
    )
    result = WebConnector().run(ctx)
    assert result.families["website"].availability == "ambiguous"
    claim = next(c for c in result.claims if c.family == "website" and c.field == "official_website")
    assert claim.availability == "ambiguous"
    assert claim.relationship in {"franchise", "parent", "brand"}
    # never published as exact for a conflicting org number
    assert claim.relationship != "exact"


def test_connector_stops_at_budget_exhaustion():
    client = FakeHttpClient(pages={}, remaining=0)
    ctx = make_ctx(OUR_ORG, tier="T2", client=client, registry_facts={"name": "EXAMPLE AS", "website": "example.no"})
    result = WebConnector().run(ctx)
    attempts = result.shared["web_attempts"]
    assert attempts and attempts[0]["status"] == "skipped"
    assert attempts[0]["reason"] == "request_budget"
