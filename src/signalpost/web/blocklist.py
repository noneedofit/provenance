"""Domains that must never be treated as a company's own website.

Two distinct lists, from the gold-label traps in docs/PLAN.md and coordinator review:

- `FRANCHISE_CHAIN_DOMAINS`: national chain / franchisor corporate domains. A local franchisee's page
  living under one of these (e.g. a Joker or 7-Eleven store finder entry) must be published as a
  `related` (`franchise`/`brand`) relationship at best, never `exact` -- even if the store's own address,
  phone or name happen to appear on the page (a franchise "find your store" page routinely lists exactly
  that corroborating information for every franchisee, so the normal >=2-corroborating-signal exact rule
  would otherwise false-positive here).
- `MARKETPLACE_BLOCKLIST`: third-party directories, booking/scheduling platforms, marketplaces and
  generic site-builder hosts. These are never candidates at all (BUILD_SPEC.md: directories aren't
  evidence) and a registry/subunit email domain on one of these hosts must not be used to derive a
  website candidate either.
"""
from __future__ import annotations

# National chains / franchisors: a page under these domains describes the chain, not the specific
# legal entity, unless that entity IS the chain's own registered organisation number.
FRANCHISE_CHAIN_DOMAINS: frozenset[str] = frozenset({
    "joker.no", "coop.no", "rema.no", "rema1000.no", "kiwi.no", "meny.no", "spar.no", "eurospar.no",
    "extra.no", "obs.no", "obsbygg.no", "europris.no", "7-eleven.no", "narvesen.no", "circlek.no",
    "mcdonalds.no", "burgerking.no", "peppes.no", "dominos.no", "sats.no", "elixia.no",
    "bestseller.com", "thon.no", "choice.no", "nordicchoicehotels.no", "scandichotels.no",
    "obos.no", "usbl.no", "styrerommet.no",
})

# Directories, marketplaces, booking/scheduling platforms and generic site-builder hosts. Never a
# candidate; never a source for a derived email-domain candidate.
MARKETPLACE_BLOCKLIST: frozenset[str] = frozenset({
    "fixit.no", "timma.no", "ledigtime.no", "bestille.no", "mittanbud.no",
    "finn.no", "gulesider.no", "1881.no", "proff.no", "purehelp.no",
    "facebook.com", "instagram.com", "linkedin.com", "linktr.ee", "youtube.com", "tiktok.com", "x.com",
    "wix.com", "wixsite.com", "weebly.com", "squarespace.com", "wordpress.com", "google.com", "goo.gl", "bit.ly",
    "odoo.com", "myshopify.com", "webnode.no", "webnode.com", "jimdofree.com", "jimdosite.com",
})

# Domains that must never be used as a website candidate at all (union of both lists — a franchise
# corporate domain is a legitimate candidate for the chain itself, but never derivable/guessable as a
# *subsidiary's* own site; treat it the same as a marketplace for candidate-generation purposes and let
# verify.py's franchise gate handle it if it is deliberately supplied, e.g. via a registry hjemmeside).
NEVER_A_CANDIDATE_DOMAINS: frozenset[str] = MARKETPLACE_BLOCKLIST


def is_marketplace_or_directory(domain: str) -> bool:
    return (domain or "").lower() in MARKETPLACE_BLOCKLIST


def is_franchise_chain_domain(domain: str) -> bool:
    return (domain or "").lower() in FRANCHISE_CHAIN_DOMAINS
