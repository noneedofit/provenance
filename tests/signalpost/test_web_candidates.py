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


def test_live_nav_homepages_from_shared_state_become_decisive_candidates():
    # A live NAV connector run this session publishes ctx.shared["nav_homepages"] (already filtered to
    # ads for our org number) ahead of the offline caches.nav snapshot. Must produce the same decisive
    # "nav_employer_homepage" source as the cache-based lookup, ordered before name guesses.
    ctx = make_ctx("923456783", registry_facts={"name": "EXAMPLE AS"})
    ctx.shared["nav_homepages"] = [{"homepage": "https://live-example.no", "uuid": "abc-123"}]
    cands = generate_candidates(ctx)
    live = [c for c in cands if c.domain == "live-example.no"]
    assert len(live) == 1
    assert live[0].source == "nav_employer_homepage"
    assert live[0].decisive is True
    name_guess_idx = next((i for i, c in enumerate(cands) if c.source == "name_guess"), len(cands))
    assert cands.index(live[0]) < name_guess_idx


def test_missing_nav_homepages_shared_state_is_handled_gracefully():
    ctx = make_ctx("923456783", registry_facts={"name": "EXAMPLE AS"})
    # ctx.shared has no "nav_homepages" key at all (older orchestrator / connector hasn't run) -- must
    # not raise.
    cands = generate_candidates(ctx)
    assert isinstance(cands, list)


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


def test_name_guesses_capped_small_for_t0():
    # T0 no longer skips name guesses entirely (many T0-classified companies are ordinary small
    # businesses whose bulk employee count is simply blank, not zero) -- but the cap is much smaller
    # than a non-T0 company's, and every guess is still DNS-prefiltered first.
    from signalpost.web.candidates import T0_MAX_NAME_GUESSES

    client = FakeHttpClient(dns={"eksempelfirma.no", "eksempelfirma.com"})
    ctx = make_ctx("923456783", tier="T0", registry_facts={"name": "Eksempelfirma AS"}, client=client)
    cands = generate_candidates(ctx)
    guesses = [c for c in cands if c.source == "name_guess"]
    assert 0 < len(guesses) <= T0_MAX_NAME_GUESSES


def test_generic_single_token_name_guess_is_dropped():
    client = FakeHttpClient(dns={"bygg.no"})
    ctx = make_ctx("923456783", tier="T2", registry_facts={"name": "Bygg Holding AS"}, client=client)
    cands = generate_candidates(ctx)
    # "bygg" alone (<=4 chars / generic) must not become a guess even though it resolves.
    assert not any(c.domain == "bygg.no" for c in cands)


def test_generic_industry_word_from_historic_subunit_name_is_dropped():
    # Bergen Bulktransport AS trap: a subunit's HISTORIC name ("AS Transport AS") strips down to the
    # single generic industry word "transport", which resolves to some unrelated, already-registered
    # transport-industry domain far more often than it resolves to us.
    client = FakeHttpClient(dns={"transport.no", "bergenbulktransport.no"})
    ctx = make_ctx(
        "923456783", tier="T2",
        registry_facts={
            "name": "BERGEN BULKTRANSPORT AS", "aliases": [],
            "subunits": [{"name": "AS Transport AS"}],
        },
        client=client,
    )
    cands = generate_candidates(ctx)
    assert not any(c.domain == "transport.no" for c in cands)


def test_same_domain_from_registry_and_wikidata_keeps_the_wikidata_source():
    # Kitron/Mowi/AF Gruppen/Norske Skog pattern: the registry hjemmeside and the Wikidata P856 website
    # are the SAME domain. Domain-dedup must keep the Wikidata-sourced candidate (independently tied to
    # our org number, decisive even when the domain is also shared by sibling entities) rather than
    # silently downgrading to the registry_website source, which a shared-domain/umbrella check could
    # block.
    caches = FakeCaches(wikidata=FakeWikidata({"923456783": {"websites": ["https://kitron.com/"]}}))
    ctx = make_ctx(
        "923456783", registry_facts={"name": "KITRON ASA", "website": "www.kitron.com"}, caches=caches,
    )
    cands = generate_candidates(ctx)
    kitron = [c for c in cands if c.domain == "kitron.com"]
    assert len(kitron) == 1
    assert kitron[0].source == "wikidata_website"


def test_first_two_tokens_variant_drops_branch_qualifier():
    # GULLSMED FJELL AVD. VOLLEN AS -> gullsmedfjell.no: the real domain drops the branch qualifier
    # ("AVD. VOLLEN") entirely, keeping only the first two distinctive words.
    client = FakeHttpClient(dns={"gullsmedfjell.no"})
    ctx = make_ctx("923456783", tier="T2", registry_facts={"name": "GULLSMED FJELL AVD. VOLLEN AS"}, client=client)
    cands = generate_candidates(ctx)
    assert any(c.domain == "gullsmedfjell.no" for c in cands)


def test_first_two_tokens_variant_drops_trailing_conjunction_clause():
    # DRANGE KRAN OG TRANSPORT AS -> drangekran.no: drops "OG TRANSPORT" entirely.
    client = FakeHttpClient(dns={"drangekran.no"})
    ctx = make_ctx("923456783", tier="T2", registry_facts={"name": "DRANGE KRAN OG TRANSPORT AS"}, client=client)
    cands = generate_candidates(ctx)
    assert any(c.domain == "drangekran.no" for c in cands)


def test_first_two_tokens_variant_matches_ampersand_clause():
    # SVEIN SVENDSEN & SONN TRAFIKKSKOLE AS -> svein-svendsen.no.
    client = FakeHttpClient(dns={"svein-svendsen.no"})
    ctx = make_ctx("923456783", tier="T2", registry_facts={"name": "SVEIN SVENDSEN & SONN TRAFIKKSKOLE AS"}, client=client)
    cands = generate_candidates(ctx)
    assert any(c.domain == "svein-svendsen.no" for c in cands)


def test_first_two_tokens_variant_not_tried_for_short_names():
    # A 2-token name already gets this slug from the ordinary joined/hyphenated variants -- no need for
    # (and no extra guess spent on) a redundant first-two-tokens variant.
    from signalpost.web.candidates import _name_guess_slugs

    slugs = _name_guess_slugs("Eksempelfirma AS")
    assert slugs.count("eksempelfirma") == 1


def test_legal_form_only_stripped_variant_keeps_a_genuine_brand_descriptor():
    # FRESH WATER NORWAY AS -> freshwaternorway.com: "norway" is usually a generic, stripped descriptor
    # (SUFFIX_WORDS), but here it's part of the real brand. The legal-form-only-stripped variant keeps it
    # (unlike the fully-stripped variant) while still dropping the legal form "AS" (unlike the bare
    # `tokens` variant, which would wrongly produce "freshwaternorwayas").
    from signalpost.web.candidates import _name_guess_slugs

    slugs = _name_guess_slugs("FRESH WATER NORWAY AS")
    assert "freshwaternorway" in slugs


def test_max_name_guesses_allows_both_stripped_and_legal_form_only_variants():
    # Regression for the ordering bug this fixes: with a low guess cap, the fully-stripped variant's
    # 4 attempts (joined/hyphenated x2 TLDs) could exhaust the budget before the legal-form-only variant
    # (the one that's actually right here) ever got a turn.
    client = FakeHttpClient(dns={"freshwater.no", "freshwater.com", "fresh-water.no", "fresh-water.com", "freshwaternorway.com"})
    ctx = make_ctx("923456783", tier="T2", registry_facts={"name": "FRESH WATER NORWAY AS"}, client=client)
    cands = generate_candidates(ctx)
    assert any(c.domain == "freshwaternorway.com" for c in cands)


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


def test_initials_are_joined_into_one_token():
    # P.E. GAARUD AS trades as pe-gaarud.no; "p-e" on its own is a meaningless guess.
    from signalpost.web.candidates import _name_guess_slugs

    slugs = _name_guess_slugs("P.E. GAARUD AS")
    assert "pegaarud" in slugs and "pe-gaarud" in slugs
    assert "p-e" not in slugs and "p-e-gaarud" not in slugs


def test_first_distinctive_word_is_the_last_guess():
    # CIMPLE TECHNOLOGY AS -> cimple.no. Tried after the fuller guesses, on .no only; never for a short or
    # generic word, or for the company's own town (LYNGDAL HAGESENTER AS -> lyngdal.no is the municipality).
    from signalpost.web.candidates import _first_word_guess

    assert _first_word_guess("CIMPLE TECHNOLOGY AS", set()) == "cimple"
    assert _first_word_guess("NORSK EKSEMPELTEKNIKK AS", set()) is None
    assert _first_word_guess("ABC EKSEMPELTEKNIKK AS", set()) is None
    assert _first_word_guess("LYNGDAL HAGESENTER AS", {"lyngdal"}) is None
    assert _first_word_guess("EKSEMPELFIRMA AS", set()) is None  # one word: already the main guess

    client = FakeHttpClient(dns={"cimple.no", "cimple.com"})
    ctx = make_ctx("923456783", tier="T0", registry_facts={"name": "CIMPLE TECHNOLOGY AS", "city": "OSLO"}, client=client)
    domains = [c.domain for c in generate_candidates(ctx)]
    assert domains[-1] == "cimple.no" and "cimple.com" not in domains
