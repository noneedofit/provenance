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
    assert website_claims[0].value == "https://example.no/"  # output contract: plain URL
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

    def is_shared(self, domain: str) -> bool:
        return self.org_count(domain) >= 3


class _FakeWikidata:
    def __init__(self, data):
        self.data = data

    def lookup(self, org):
        return self.data.get(org)


class _FakeCaches:
    def __init__(self, email_domains=None, wikidata=None):
        self.email_domains = email_domains
        self.wikidata = wikidata


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
    # The company's own registry entry names the group site: published, but labelled as the group's site,
    # never as an exact own-site, and nothing (profiles, description) is taken from it.
    claim = next((c for c in result.claims if c.family == "website" and c.field == "official_website"), None)
    assert claim is not None and claim.relationship != "exact"
    assert result.families["profiles"].availability != "available"
    assert result.families["description"].availability != "available"


def test_connector_shared_registry_domain_with_many_orgs_is_not_published():
    html = "<html><body>Vi forvalter over 500 borettslag. Kontakt oss.</body></html>"
    client = FakeHttpClient(pages={"https://www.boligforvalter.no/": FakePage(html)})
    caches = _FakeCaches(email_domains=_FakeEmailDomains({"boligforvalter.no": 549}))
    ctx = make_ctx(
        OUR_ORG, tier="T2", client=client, caches=caches,
        registry_facts={"name": "BORETTSLAGET STORGATA 1", "website": "www.boligforvalter.no"},
    )
    result = WebConnector().run(ctx)
    assert result.families["website"].availability != "available"


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


def test_website_org_count_uses_website_column_when_email_count_is_zero():
    from types import SimpleNamespace

    from signalpost.web.connector import _website_org_count

    class Domains:
        def org_count(self, domain):
            return 0

        def website_org_count(self, domain):
            return 28

    ctx = SimpleNamespace(caches=SimpleNamespace(email_domains=Domains()))
    assert _website_org_count(ctx, "rtbbl.no") == 28


def test_redirect_into_a_subpage_of_another_domain_needs_a_decisive_signal():
    from types import SimpleNamespace

    from signalpost.web.connector import _redirects_into_other_site

    cand = SimpleNamespace(url="https://albatross-as.no/")
    page = lambda u: SimpleNamespace(final_url=u)
    assert _redirects_into_other_site(cand, [page("https://www.toma.no/tjenester/camps/")])
    assert not _redirects_into_other_site(cand, [page("https://newname.no/")])
    assert not _redirects_into_other_site(cand, [page("https://www.albatross-as.no/hjem")])
    # The registry's hjemmeside forwarding to the domain of the registry's own e-mail for us stays ours.
    declared = SimpleNamespace(url="https://www.lundbeck.no/", source="registry_website")
    assert not _redirects_into_other_site(declared, [page("https://www.lundbeck.com/no")], "lundbeck.com")
    assert _redirects_into_other_site(declared, [page("https://www.lundbeck.com/no")], "other.no")
    guessed = SimpleNamespace(url="https://www.lundbeck.no/", source="name_guess")
    assert _redirects_into_other_site(guessed, [page("https://www.lundbeck.com/no")], "lundbeck.com")


def _previous_with_site(domain: str) -> dict:
    return {"last_envelope": {"claims": [{
        "field": "official_website", "status": "current", "relationship": "exact",
        "value": {"url": f"https://{domain}/", "domain": domain},
    }]}}


def test_previously_verified_site_unreachable_is_failed_not_gone():
    from web_fakes import FakePage

    client = FakeHttpClient(pages={"https://example.no/": FakePage("", status=503)})
    ctx = make_ctx(OUR_ORG, tier="T2", client=client,
                   registry_facts={"name": "EXAMPLE AS", "website": "example.no", "street": "", "postcode": "", "city": ""})
    ctx.previous = _previous_with_site("example.no")
    result = WebConnector().run(ctx)
    for fam in ("website", "profiles", "description"):
        assert result.families[fam].availability == "failed"
    assert result.shared.get("website_unchecked") is True


def test_previously_verified_site_now_404_is_reported_not_available():
    client = FakeHttpClient(pages={})  # every URL 404s: the site may really be gone
    ctx = make_ctx(OUR_ORG, tier="T2", client=client,
                   registry_facts={"name": "EXAMPLE AS", "website": "example.no", "street": "", "postcode": "", "city": ""})
    ctx.previous = _previous_with_site("example.no")
    result = WebConnector().run(ctx)
    assert result.families["website"].availability == "not_available"


def test_wikidata_profiles_are_published_even_without_a_verified_site():
    from types import SimpleNamespace

    class _Wikidata:
        def lookup(self, org):
            return {"qid": "Q1", "label": "Example AS", "websites": [], "retrieved_at": "2026-09-28T00:00:00Z",
                    "source_url": "https://query.wikidata.org/sparql",
                    "profiles": {"facebook": "https://www.facebook.com/exampleas/", "instagram": "not-a-url"}}

    ctx = make_ctx(OUR_ORG, tier="T2", client=FakeHttpClient(pages={}), caches=SimpleNamespace(wikidata=_Wikidata(), email_domains=None),
                   registry_facts={"name": "EXAMPLE AS", "website": None, "street": "", "postcode": "", "city": ""})
    result = WebConnector().run(ctx)
    profiles = [c for c in result.claims if c.family == "profiles"]
    assert [c.value["url"] for c in profiles] == ["https://facebook.com/exampleas"]
    assert profiles[0].identity_basis == "wikidata_org_number" and profiles[0].evidence_ids
    assert result.families["profiles"].availability == "available"


_SGCAPTCHA = ('<html><head><link rel="icon" href="data:;"><meta http-equiv="refresh" '
              'content="0;/.well-known/sgcaptcha/?r=%2F&y=ipc:1.2.3.4:1791305696.459"></meta></head></html>')


def test_a_declared_site_behind_a_captcha_is_blocked_not_absent():
    from web_fakes import FakePage

    client = FakeHttpClient(pages={"https://example.no/": FakePage(_SGCAPTCHA, status=202)})
    ctx = make_ctx(OUR_ORG, tier="T2", client=client,
                   registry_facts={"name": "EXAMPLE AS", "website": "example.no", "street": "", "postcode": "", "city": ""})
    result = WebConnector().run(ctx)
    assert result.families["website"].availability == "blocked"
    assert "bot challenge" in result.families["website"].reason


def test_a_previously_verified_site_behind_a_captcha_keeps_the_previous_profile():
    from web_fakes import FakePage

    challenge = "<html><body>Checking your browser... Javascript required</body></html>"
    client = FakeHttpClient(pages={"https://example.no/": FakePage(challenge, status=403)})
    ctx = make_ctx(OUR_ORG, tier="T2", client=client,
                   registry_facts={"name": "EXAMPLE AS", "website": "example.no", "street": "", "postcode": "", "city": ""})
    ctx.previous = _previous_with_site("example.no")
    result = WebConnector().run(ctx)
    for fam in ("website", "profiles", "description"):
        assert result.families[fam].availability == "failed"


GUESS_HOME_HTML = """
<html><head><title>Eksempelfirma</title></head><body>
<p>Velkommen til Eksempelfirma.</p>
<a href="/kontakt">Kontakt</a> <a href="/om-oss">Om oss</a> <a href="/personvern">Personvern</a>
</body></html>
"""


def _guess_ctx(personvern_html: str):
    client = FakeHttpClient(pages={
        "https://eksempelfirma.no/": FakePage(GUESS_HOME_HTML),
        "https://eksempelfirma.no/kontakt": FakePage("<html><body>Skriv til oss.</body></html>"),
        "https://eksempelfirma.no/om-oss": FakePage("<html><body>Vi lager ting.</body></html>"),
        "https://eksempelfirma.no/personvern": FakePage(personvern_html),
    }, dns={"eksempelfirma.no"})
    return make_ctx(OUR_ORG, tier="T1", client=client, registry_facts={"name": "EKSEMPELFIRMA AS"})


def test_a_name_only_match_reads_the_privacy_page_for_the_org_number():
    ctx = _guess_ctx(f"<html><body>Behandlingsansvarlig er Eksempelfirma AS, org.nr. {OUR_ORG}.</body></html>")
    result = WebConnector().run(ctx)
    site = [c for c in result.claims if c.field == "official_website"]
    assert site and site[0].availability == "available" and site[0].identity_basis == "org_number_on_source"
    assert "https://eksempelfirma.no/personvern" in ctx.client.calls


def test_a_name_only_match_stays_unpublished_when_the_privacy_page_does_not_tie_it_to_us():
    ctx = _guess_ctx("<html><body>Vi tar personvern på alvor.</body></html>")
    result = WebConnector().run(ctx)
    assert result.families["website"].availability != "available"
    assert "https://eksempelfirma.no/personvern" in ctx.client.calls


def _group_site_ctx(name: str):
    sister = "934567897"
    html = (f"<html><head><title>Eksempelfirma</title></head><body>Eksempelfirma Butikk AS, org.nr {sister}."
            "<a href='https://www.facebook.com/eksempelfirma'>Facebook</a></body></html>")
    client = FakeHttpClient(pages={"https://eksempelfirma.no/": FakePage(html)}, dns={"eksempelfirma.no"})
    return make_ctx(OUR_ORG, tier="T0", client=client, registry_facts={"name": name, "website": "eksempelfirma.no"})


def test_a_group_site_named_exactly_like_us_gives_its_profiles_as_the_brand():
    result = WebConnector().run(_group_site_ctx("EKSEMPELFIRMA AS"))
    assert result.families["website"].availability == "available"
    profiles = [c for c in result.claims if c.family == "profiles"]
    assert result.families["profiles"].availability == "available"
    assert profiles and all(c.relationship == "brand" and c.identity_basis == "registry_declared" for c in profiles)


def test_a_group_site_with_a_different_name_keeps_its_profiles_to_itself():
    result = WebConnector().run(_group_site_ctx("EKSEMPELFIRMA BYGG AS"))
    assert result.families["website"].availability == "available"
    assert result.families["profiles"].availability == "not_available"
    assert not [c for c in result.claims if c.family == "profiles"]


def _branch_ctx(page_html: str, final_url: str | None = None):
    client = FakeHttpClient(pages={"https://www.kjedeflid.no/holmestrand": FakePage(page_html, final_url=final_url)})
    caches = _FakeCaches(email_domains=_FakeEmailDomains({"kjedeflid.no": 21}))
    return make_ctx(
        OUR_ORG, tier="T0", client=client, caches=caches,
        registry_facts={"name": "HUSFLIDEN HOLMESTRAND SA", "website": "www.kjedeflid.no/holmestrand"},
    )


def test_a_registry_listed_page_on_a_shared_chain_site_is_published_as_the_companys_page():
    # HUSFLIDEN HOLMESTRAND SA -> www.norskflid.no/holmestrand: the domain is shared by 21 organisations, but the
    # register lists this page for us and the page names us.
    result = WebConnector().run(_branch_ctx("<html><head><title>Husfliden Holmestrand</title></head><body>Velkommen.</body></html>"))
    claim = next(c for c in result.claims if c.field == "official_website")
    assert claim.availability == "available" and claim.relationship != "exact"
    assert claim.value == "https://www.kjedeflid.no/holmestrand"
    assert result.families["profiles"].availability != "available"
    moved = WebConnector().run(_branch_ctx(
        "<html><head><title>Holmestrand - Husfliden</title></head><body>x</body></html>",
        final_url="https://www.kjedeflid.no/butikker/holmestrand/"))
    assert next(c for c in moved.claims if c.field == "official_website").availability == "available"


def test_a_registry_listed_chain_page_that_no_longer_names_us_is_not_our_page():
    result = WebConnector().run(_branch_ctx("<html><head><title>Kjedeflid</title></head><body>Finn din butikk.</body></html>"))
    assert result.families["website"].availability != "available"
    redirected = WebConnector().run(_branch_ctx(
        "<html><head><title>Husfliden Holmestrand</title></head><body>x</body></html>", final_url="https://www.kjedeflid.no/"))
    assert redirected.families["website"].availability != "available"


def test_a_generic_registry_listed_path_on_a_shared_site_is_not_our_page():
    # A property manager's /index.php that happens to mention our name is not our page: the listed path must
    # itself carry a distinctive word of our name.
    client = FakeHttpClient(pages={"https://www.forvalter.no/index.php": FakePage("<html><body>Husfliden Holmestrand</body></html>")})
    caches = _FakeCaches(email_domains=_FakeEmailDomains({"forvalter.no": 300}))
    ctx = make_ctx(OUR_ORG, tier="T0", client=client, caches=caches,
                   registry_facts={"name": "HUSFLIDEN HOLMESTRAND SA", "website": "www.forvalter.no/index.php"})
    assert WebConnector().run(ctx).families["website"].availability != "available"


def _forwarding_ctx(email_org_count: int):
    page = FakePage("<html><head><title>Eksempel Pharma Norge</title></head><body>Eksempel Pharma AS, Storgata 1, 0155 Oslo."
                    " norge@eksempelpharma.com</body></html>", final_url="https://www.eksempelpharma.com/no")
    client = FakeHttpClient(pages={"https://www.eksempelpharma.no/": page}, dns={"www.eksempelpharma.no", "eksempelpharma.no"})
    caches = _FakeCaches(email_domains=_FakeEmailDomains({"eksempelpharma.com": email_org_count}))
    return make_ctx(OUR_ORG, tier="T2", client=client, caches=caches, registry_facts={
        "name": "EKSEMPEL PHARMA AS", "website": "www.eksempelpharma.no", "email": "norge@eksempelpharma.com",
        "email_domain": "eksempelpharma.com", "street": "Storgata 1", "postcode": "0155", "city": "OSLO",
    })


def test_a_registry_site_forwarding_to_our_own_email_domain_is_our_site():
    # H. LUNDBECK AS: lundbeck.no -> lundbeck.com/no, and the register's e-mail is norway@lundbeck.com.
    result = WebConnector().run(_forwarding_ctx(1))
    site = next(c for c in result.claims if c.field == "official_website")
    assert site.availability == "available" and site.relationship == "exact"


def test_a_registry_site_forwarding_to_a_shared_email_domain_is_not_trusted_on_the_register_alone():
    # An e-mail domain used by many organisations (an accountant's) does not tie the forwarding target to us.
    result = WebConnector().run(_forwarding_ctx(40))
    site = [c for c in result.claims if c.field == "official_website" and c.availability == "available" and c.relationship == "exact"]
    assert not site


def test_a_name_domain_forwarding_to_the_parents_site_does_not_give_the_parents_profiles():
    # FRONT SYSTEMS AS: frontsystems.no forwards to egsoftware.com, whose links are EG's own profiles.
    sister = "934567897"
    html = (f"<html><head><title>EG</title></head><body>EG A/S, org.nr {sister}."
            "<a href='https://www.facebook.com/egparent'>Facebook</a></body></html>")
    page = FakePage(html, final_url="https://egparent.com/front")
    client = FakeHttpClient(pages={"https://eksempelfirma.no/": page}, dns={"eksempelfirma.no"})
    ctx = make_ctx(OUR_ORG, tier="T0", client=client, registry_facts={"name": "EKSEMPELFIRMA AS", "website": "eksempelfirma.no"})
    result = WebConnector().run(ctx)
    assert not [c for c in result.claims if c.family == "profiles"]
