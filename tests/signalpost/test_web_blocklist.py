from __future__ import annotations

from signalpost.web.blocklist import (
    is_franchise_chain_domain,
    is_marketplace_or_directory,
)
from signalpost.web.candidates import Candidate, generate_candidates, registered_domain
from web_fakes import make_ctx


def test_all_spec_named_franchise_chains_are_gated():
    # BUILD_SPEC.md / orchestrator task: every one of these must be treated as a franchise/brand
    # corporate domain, never a bare candidate for an unrelated org's own site.
    for domain in (
        "joker.no", "coop.no", "rema.no", "kiwi.no", "meny.no", "spar.no", "extra.no", "obs.no",
        "europris.no", "7-eleven.no", "narvesen.no", "circlek.no", "mcdonalds.no", "burgerking.no",
        "peppes.no", "dominos.no", "sats.no", "elixia.no", "bestseller.com", "thon.no", "choice.no",
        "scandichotels.no",
    ):
        assert is_franchise_chain_domain(domain), domain


def test_marketplace_and_directory_hosts_are_never_candidates():
    for domain in (
        "fixit.no", "timma.no", "ledigtime.no", "bestille.no", "mittanbud.no", "finn.no",
        "gulesider.no", "1881.no", "proff.no", "purehelp.no", "facebook.com", "instagram.com",
        "linktr.ee", "odoo.com", "myshopify.com",
    ):
        assert is_marketplace_or_directory(domain), domain


def test_wix_weebly_squarespace_default_subdomains_resolve_to_blocked_apex():
    # A registrant's default site-builder subdomain (name.wixsite.com, name.weebly.com,
    # name.squarespace.com) must resolve, via registered_domain(), to a domain the blocklist covers --
    # not to the registrant's own subdomain, which would slip past the marketplace gate.
    for host, expected_apex in (
        ("myshop.wixsite.com", "wixsite.com"),
        ("myco.weebly.com", "weebly.com"),
        ("myco.squarespace.com", "squarespace.com"),
    ):
        domain = registered_domain(host)
        assert domain == expected_apex
        assert is_marketplace_or_directory(domain), host


def test_marketplace_domain_never_generated_as_a_candidate_even_as_registry_hjemmeside():
    ctx = make_ctx(
        "923456783",
        registry_facts={"name": "EXAMPLE AS", "website": "https://mittanbud.no/profil/example-as"},
    )
    cands = generate_candidates(ctx)
    assert all(c.domain != "mittanbud.no" for c in cands)


def test_marketplace_email_domain_never_derives_a_candidate():
    ctx = make_ctx(
        "923456783",
        registry_facts={"name": "EXAMPLE AS", "email": "post@myshop.wixsite.com", "email_domain": "myshop.wixsite.com"},
    )
    cands = generate_candidates(ctx)
    assert all(c.domain != "wixsite.com" for c in cands)


def test_franchise_chain_domain_is_still_a_candidate_for_verify_to_gate():
    # Unlike marketplace hosts, a franchise/chain domain IS a legitimate candidate (it might genuinely
    # be the chain's own headquarters org) -- verify.py's franchise gate, not candidate generation, is
    # responsible for keeping a franchisee's site from resolving to exact off corroboration alone.
    ctx = make_ctx("923456783", registry_facts={"name": "JOKER AS", "website": "joker.no"})
    cands = generate_candidates(ctx)
    assert any(c.domain == "joker.no" for c in cands)
