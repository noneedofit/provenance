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


class _FakeEmailDomains:
    """Matches the documented Caches API (BUILD_SPEC.md): org_count(domain) -> int."""

    def __init__(self, counts: dict[str, int]):
        self.counts = counts

    def org_count(self, domain: str) -> int:
        return self.counts.get(domain, 1)


class _FakeCaches:
    def __init__(self, email_domains=None):
        self.email_domains = email_domains


def test_connector_uses_caches_org_count_to_reject_shared_registry_domain():
    # A registry-declared domain used by >=3 organisations (per ctx.caches.email_domains.org_count, the
    # documented Caches API method) must not resolve to `exact` even with no conflicting org number.
    html = "<html><body>Avarn Security operates across the Nordics. Part of the Avarn Group.</body></html>"
    client = FakeHttpClient(pages={"https://www.avarn.no/": FakePage(html)})
    caches = _FakeCaches(email_domains=_FakeEmailDomains({"avarn.no": 6}))
    ctx = make_ctx(
        OUR_ORG, tier="T2", client=client, caches=caches,
        registry_facts={"name": "AVARN SECURITY AS", "website": "www.avarn.no"},
    )
    result = WebConnector().run(ctx)
    assert result.families["website"].availability != "available"
    claim = next((c for c in result.claims if c.family == "website" and c.field == "official_website"), None)
    if claim is not None:
        assert claim.relationship != "exact"


def test_connector_requires_decisive_signal_when_registry_site_unconfirmed():
    # DACON SERVICES AS trap: the registry-declared site (example.no) 404s / can't be confirmed, and a
    # name-guessed candidate (example.com, a different domain the .no/.com guess loop also tries)
    # genuinely carries 2 corroborating signals (address + legal name) on its own page. With the
    # registry's own claim unconfirmed, that must stay `ambiguous`, not jump to `exact` off corroboration
    # alone.
    guess_html = "<html><head><title>Example AS</title></head><body>Example AS. Storgata 1, 0155 Oslo.</body></html>"
    client = FakeHttpClient(pages={"https://example.com/": FakePage(guess_html)}, dns={"example.com"})
    ctx = make_ctx(
        OUR_ORG, tier="T2", client=client,
        registry_facts={
            "name": "EXAMPLE AS", "website": "example.no", "street": "Storgata 1", "postcode": "0155",
            "city": "", "phones": [], "email": None, "email_domain": None, "role_holders": [], "subunits": [],
        },
    )
    result = WebConnector().run(ctx)
    assert result.families["website"].availability != "available"
    claim = next((c for c in result.claims if c.family == "website" and c.field == "official_website"), None)
    if claim is not None:
        assert claim.relationship != "exact"
    attempts = result.shared["web_attempts"]
    guess_attempt = next(a for a in attempts if a["domain"] == "example.com")
    assert guess_attempt["status"] != "exact"


def test_connector_stops_at_budget_exhaustion():
    client = FakeHttpClient(pages={}, remaining=0)
    ctx = make_ctx(OUR_ORG, tier="T2", client=client, registry_facts={"name": "EXAMPLE AS", "website": "example.no"})
    result = WebConnector().run(ctx)
    attempts = result.shared["web_attempts"]
    assert attempts and attempts[0]["status"] == "skipped"
    assert attempts[0]["reason"] == "request_budget"
