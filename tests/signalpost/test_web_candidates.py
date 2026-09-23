from __future__ import annotations

from signalpost.web.candidates import generate_candidates, registered_domain
from web_fakes import FakeHttpClient, make_ctx


class FakeWikidata:
    def __init__(self, data):
        self.data = data

    def lookup(self, org):
        return self.data.get(org)


class FakeNav:
    def __init__(self, ads):
        self.ads = ads

    def ads_for(self, orgs):
        return [a for a in self.ads if a.get("employer_orgnr") in orgs]


class FakeEmailDomains:
    def __init__(self, shared_domains):
        self.shared_domains = shared_domains

    def is_shared(self, domain):
        return domain in self.shared_domains

    def website_org_count(self, domain):
        return 500 if domain in self.shared_domains else 1


class FakeCaches:
    def __init__(self, wikidata=None, nav=None, email_domains=None):
        self.wikidata = wikidata
        self.nav = nav
        self.email_domains = email_domains


def test_registry_website_is_first_candidate():
    ctx = make_ctx("923456783", registry_facts={"name": "EXAMPLE AS", "website": "example.no"})
    cands = generate_candidates(ctx)
    assert cands[0].domain == "example.no"
    assert cands[0].source == "registry_website"


def test_wikidata_and_nav_candidates_are_decisive_and_ordered_before_guesses():
    caches = FakeCaches(
        wikidata=FakeWikidata({"923456783": {"websites": ["https://wd-example.no"]}}),
        nav=FakeNav([{"employer_orgnr": "923456783", "employer_homepage": "https://nav-example.no"}]),
    )
    ctx = make_ctx("923456783", registry_facts={"name": "EXAMPLE AS"}, caches=caches)
    cands = generate_candidates(ctx)
    sources = [c.source for c in cands]
    assert "wikidata_website" in sources
    assert "nav_employer_homepage" in sources
    name_guess_idx = next((i for i, s in enumerate(sources) if s == "name_guess"), len(sources))
    assert sources.index("wikidata_website") < name_guess_idx
    assert sources.index("nav_employer_homepage") < name_guess_idx


def test_shared_email_domain_is_not_a_candidate():
    caches = FakeCaches(email_domains=FakeEmailDomains({"styrerommet.no"}))
    ctx = make_ctx(
        "923456783",
        registry_facts={"name": "SAMEIET X", "email": "post@styrerommet.no", "email_domain": "styrerommet.no"},
        caches=caches,
    )
    cands = generate_candidates(ctx)
    assert all(c.domain != "styrerommet.no" for c in cands)


def test_name_guesses_are_dns_prefiltered_and_capped():
    client = FakeHttpClient(dns={"eksempelfirma.no"})
    ctx = make_ctx("923456783", tier="T2", registry_facts={"name": "Eksempelfirma AS"}, client=client)
    cands = generate_candidates(ctx)
    guesses = [c for c in cands if c.source == "name_guess"]
    assert len(guesses) <= 4
    assert all(c.domain == "eksempelfirma.no" for c in guesses)  # .com/hyphenated variants didn't resolve


def test_name_guesses_skipped_for_t0():
    client = FakeHttpClient(dns={"eksempelfirma.no", "eksempelfirma.com"})
    ctx = make_ctx("923456783", tier="T0", registry_facts={"name": "Eksempelfirma AS"}, client=client)
    cands = generate_candidates(ctx)
    assert not any(c.source == "name_guess" for c in cands)


def test_generic_single_token_name_guess_is_dropped():
    client = FakeHttpClient(dns={"bygg.no"})
    ctx = make_ctx("923456783", tier="T2", registry_facts={"name": "Bygg Holding AS"}, client=client)
    cands = generate_candidates(ctx)
    # "bygg" alone (<=4 chars / generic) must not become a guess even though it resolves.
    assert not any(c.domain == "bygg.no" for c in cands)


def test_candidates_deduplicated_by_registered_domain():
    ctx = make_ctx(
        "923456783",
        registry_facts={
            "name": "EXAMPLE AS", "website": "example.no",
            "subunits": [{"name": "Example Avd Oslo", "website": "https://www.example.no/oslo"}],
        },
    )
    cands = generate_candidates(ctx)
    domains = [c.domain for c in cands]
    assert domains.count("example.no") == 1


def test_registered_domain_strips_www_and_path():
    assert registered_domain("https://www.example.no/some/path") == "example.no"
