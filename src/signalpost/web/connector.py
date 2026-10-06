"""Orchestrates candidates -> crawl -> verify -> extract into a ConnectorResult.

Tries candidates in provenance order, stops at the first `exact` verdict. Publishes claims for the
`website`, `profiles` and `description` families, plus `shared` state consumed by W4 (activity/jobs).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.parse
from typing import Any

from .. import registry as registry_mod
from ..caches import places as places_mod
from ..context import CompanyContext
from ..models import (
    Claim,
    ConnectorResult,
    Evidence,
    FamilyState,
    canonical,
    claim_key,
    evidence_id,
    utc_now,
)
from ..urls import normalize_social_url
from . import candidates as candidates_mod
from . import extract as extract_mod
from . import verify as verify_mod
from .blocklist import is_marketplace_or_directory
from .candidates import Candidate, registered_domain
from .crawl import PageFetch, crawl_candidate, fetch_confirmation_pages

DECISIVE_SOURCES = {"registry_website", "wikidata_website", "nav_employer_homepage", "osm_orgnr_website"}


def _registry_facts(ctx: CompanyContext) -> dict[str, Any]:
    return candidates_mod.registry_facts(ctx)  # reuse: same fallback-to-bulk logic


def _website_org_count(ctx: CompanyContext, domain: str) -> int | None:
    """How many organisations this domain is registered to (BUILD_SPEC.md "Identity rules": a website
    domain used by >=3 orgs is a shared/parent site, never `exact` on registry_declared trust alone).

    `caches.email_domains.org_count(domain)` (BUILD_SPEC.md "Caches API") is the only documented counter
    for "how many orgs use this domain" and is built from the bulk file's website *and* email columns
    together, so it doubles as the website-domain shared-count here. `None`-safe: caches may be absent
    (no --caches passed) or, in an older/partial cache build, may not expose the method at all.
    """
    caches = getattr(ctx, "caches", None)
    if caches is None or not getattr(caches, "email_domains", None):
        return None
    email_domains = caches.email_domains
    counts: list[int] = []
    for method_name in ("org_count", "website_org_count"):
        method = getattr(email_domains, method_name, None)
        if method is None:
            continue
        try:
            counts.append(int(method(domain)))
        except Exception:
            continue
    # A domain is shared if either column says so (rtbbl.no: 0 e-mail users, 28 registry websites).
    return max(counts) if counts else None


def _previous_site_domain(ctx: Any) -> str | None:
    previous = getattr(ctx, "previous", None) or {}
    envelope = previous.get("last_envelope") or {}
    for claim in envelope.get("claims", []):
        if claim.get("field") == "official_website" and claim.get("status") == "current" and claim.get("relationship") in (None, "exact"):
            value = claim.get("value") or {}
            if isinstance(value, dict) and value.get("domain"):
                return str(value["domain"]).lower()
            if isinstance(value, str) and value:
                return (registered_domain(value) or "").lower() or None
    return None


_GROUP_SITE_MAX_ORGS = 9
_GENERIC_TOKENS = {
    "holding", "invest", "eiendom", "norge", "norway", "group", "gruppen", "bygg", "service", "drift", "as", "asa",
    "sa", "da", "ans", "stiftelsen", "og", "borettslag", "sameiet", "servicesenter", "senter",
}


def _declared_branch_page(ctx: Any, cand: Candidate, pages: list[PageFetch] | None) -> bool:
    """The register lists a page on a shared chain or group site for us, not just the site (HUSFLIDEN
    HOLMESTRAND SA -> www.norskflid.no/holmestrand): the listed path carries a distinctive word of our name,
    the page still loads under that path segment, and every distinctive word of our name is on it. It is the
    company's own page on that site."""
    if cand.source != "registry_website" or not pages:
        return False
    declared = urllib.parse.urlsplit(cand.url).path.strip("/").casefold()
    if not declared:
        return False
    name = str(_registry_facts(ctx).get("name") or "")
    tokens = {t for t in re.findall(r"[a-z0-9]+", verify_mod._normalize(name)) if len(t) >= 4 and t not in _GENERIC_TOKENS}
    path_text = verify_mod._normalize(declared).replace("-", "").replace("_", "")
    if not tokens or not any(t in path_text for t in tokens):
        return False
    page = next((p for p in pages if p.page_kind == "homepage"), pages[0])
    if not page.ok or registered_domain(page.final_url or "") != cand.domain:
        return False
    # The page may have moved within the site (/holmestrand -> /butikker/holmestrand/), not off our path.
    final_segments = urllib.parse.urlsplit(page.final_url or "").path.strip("/").casefold().split("/")
    if declared.split("/")[-1] not in final_segments:
        return False
    page_tokens = set(re.findall(r"[a-z0-9]+", verify_mod._normalize(page.title + " " + page.text)))
    return tokens <= page_tokens


def _is_group_site(ctx: Any, cand: Candidate, verdict: Any, pages: list[PageFetch] | None = None) -> bool:
    """Registry-declared site that belongs to the company's own group: few organisations share the domain
    (not a property manager or franchise platform), and the domain carries a distinctive word of our name.
    Or the register lists our own page on a shared chain/group site (see _declared_branch_page)."""
    if verdict.relationship in ("parent", "brand", "subsidiary", "franchise") and _declared_branch_page(ctx, cand, pages):
        return True
    if verdict.relationship not in ("parent", "brand", "subsidiary"):
        return False
    if cand.source not in ("registry_website", "wikidata_website", "osm_orgnr_website"):
        return False
    if cand.source == "registry_website":
        count = _website_org_count(ctx, cand.domain)
        if count is not None and count > _GROUP_SITE_MAX_ORGS:
            return False
    # The domain must carry a distinctive word of our own name: AF GRUPPEN ASA -> afgruppen.no is our group's
    # site; a foundation whose Wikidata entry lists its property manager's site (vestbo.no) is not.
    name = str(_registry_facts(ctx).get("name") or "")
    tokens = [t for t in re.findall(r"[a-z0-9]+", verify_mod._normalize(name)) if t not in _GENERIC_TOKENS]
    label = (cand.domain or "").rsplit(".", 1)[0].replace("-", "")
    return any((len(t) >= 3 and t in label) or (len(t) == 2 and label.startswith(t)) for t in tokens)


def _worth_confirming(verdict: Any) -> bool:
    """An ambiguous verdict whose only support is our name (in the title or as the domain): one more look at
    the pages that name the business behind a site can settle it either way."""
    if verdict.status != "ambiguous" or verdict.conflicts:
        return False
    kinds = {s.kind for s in verdict.signals}
    return bool(kinds & {"legal_name_match", "domain_name_match"})


def _domain_is_our_name(ctx: Any, cand: Candidate) -> bool:
    """The domain label is our full legal name minus legal-form words (HAIKJEFTEN AS -> haikjeften.no), and
    that name is distinctive (8+ characters, not a generic word) -- the same test as verify's domain_name_match."""
    name = str(_registry_facts(ctx).get("name") or "")
    core = verify_mod._legal_name_core(name)
    slug = "".join(t for t in re.findall(r"[a-z0-9]+", verify_mod._normalize(name)) if t in core)
    label = (cand.domain or "").rsplit(".", 1)[0].replace("-", "")
    return bool(slug) and label == slug and len(slug) >= 8 and slug not in verify_mod.GENERIC_NAME_WORDS


def _publish_profiles(
    result: ConnectorResult, org: str, links: list[dict], pages: list[PageFetch], homepage: PageFetch,
    identity_basis: str, relationship: str, note: str | None = None,
) -> None:
    for link in links:
        source_page = next((p for p in pages if p.final_url == link.get("source_url")), homepage)
        ev = _make_evidence(source_page, link["url"], "web_social_link_v1")
        result.evidence.append(ev)
        result.claims.append(Claim(
            claim_id=claim_key(org, "profiles", "profile", link["url"]), organisation_number=org,
            family="profiles", field="profile", value={"platform": link["platform"], "url": link["url"]},
            value_key=link["url"], availability="available", identity_basis=identity_basis,
            relationship=relationship, evidence_ids=[ev.evidence_id], note=note,
        ))


def _debug_log(org: str, attempts: list[dict]) -> None:
    """Diagnostics only: with SIGNALPOST_WEB_DEBUG=<file>, append every candidate verdict as one JSON line.
    Never part of the output contract."""
    path = os.environ.get("SIGNALPOST_WEB_DEBUG")
    if not path:
        return
    try:
        with _DEBUG_LOCK, open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"org": org, "attempts": attempts}, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


_DEBUG_LOCK = threading.Lock()


def _previous_site_unreachable(ctx: Any, attempts: list[dict]) -> bool:
    domain = _previous_site_domain(ctx)
    if not domain:
        return False
    return any(
        a.get("domain", "").lower() == domain and a.get("status") in ("unreachable", "error") and _transient(a.get("reason"))
        for a in attempts
    )


def _transient(reason: Any) -> bool:
    """Connection-level or server-side failures; a 404 or DNS failure means the site may really be gone."""
    text = str(reason or "")
    return (text in ("ssl_error", "network_error", "timeout", "http_429", "bot_challenge")
            or text.startswith("http_5") or text.startswith("URLError"))


# Candidate sources that tie a domain to this organisation number before any page is read.
_DECLARED_SOURCES = {"registry_website", "wikidata_website", "nav_employer_homepage", "osm_orgnr_website"}


def _declared_site_challenged(attempts: list[dict]) -> str | None:
    """The domain of a registry/Wikidata/NAV/OSM-declared site that answered with a bot challenge."""
    for a in attempts:
        if a.get("source") in _DECLARED_SOURCES and a.get("status") == "unreachable" and a.get("reason") == "bot_challenge":
            return a.get("domain")
    return None


def _redirects_into_other_site(cand: Any, pages: list[PageFetch], email_domain: str | None = None) -> bool:
    """The candidate redirects to a subpage of a different domain (albatross-as.no ->
    toma.no/tjenester/camps): that is someone else's site, usually a parent's, so only a decisive
    signal such as our org number may make it our official site. A registry hjemmeside that forwards to
    the domain of the registry's own e-mail address for us (lundbeck.no -> lundbeck.com/no, e-mail
    norway@lundbeck.com; `email_domain` is passed only when no other organisation uses it) stays on a
    domain the register ties to us, so it does not count."""
    if not pages:
        return False
    final = pages[0].final_url or ""
    final_domain = registered_domain(final)
    if final_domain == registered_domain(cand.url):
        return False
    if getattr(cand, "source", None) == "registry_website" and email_domain and final_domain == registered_domain(email_domain):
        return False
    return urllib.parse.urlsplit(final).path.strip("/") != ""


def _public_url(cand: Any, homepage: PageFetch) -> str:
    """The company's own address: keep the candidate URL when it redirects onto a hosting platform
    (barokkanerne.no -> barokkanerne.squarespace.com), otherwise the final URL after redirects."""
    if is_marketplace_or_directory(registered_domain(homepage.final_url) or "") and registered_domain(cand.url) != registered_domain(homepage.final_url):
        return _without_default_port(cand.url)
    return _without_default_port(homepage.final_url)


def _without_default_port(url: str) -> str:
    """https://peab.no:443/bygg/ -> https://peab.no/bygg/ (a default port is noise in a published URL)."""
    parts = urllib.parse.urlsplit(url or "")
    if (parts.scheme, parts.port) in (("https", 443), ("http", 80)):
        parts = parts._replace(netloc=parts.hostname or parts.netloc)
    return urllib.parse.urlunsplit(parts)


def _source_class_for(page: PageFetch) -> str:
    return "company_owned"


_SIGNAL_RANK = {
    kind: i for i, kind in enumerate((
        "org_number_on_source", "jsonld_org_number", "wikidata", "osm_orgnr", "nav_employer_homepage",
        "registry_declared", "registry_email", "legal_name_match", "domain_name_match", "email_domain_match",
        "registry_email_domain", "open_places_contact_named", "registry_phone", "registered_address",
        "role_name", "open_places_contact",
    ))
}


# Signals that are facts about the page (quoted from the page that showed them).
_PAGE_SIGNALS = {
    "org_number_on_source", "jsonld_org_number", "legal_name_match", "domain_name_match", "registry_email",
    "registry_phone", "registered_address", "role_name",
}


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _signal_evidence(ctx: Any, sig: Any, cand: Candidate, pages: list[PageFetch], homepage: PageFetch) -> Evidence | None:
    """Evidence for one identity signal, from the source that actually states it: the page that showed it,
    the register bulk row, the Wikidata item, the OpenStreetMap feature or the NAV ad. Open-places matches
    only nominate a candidate and are not evidence of their own."""
    method = f"web_{sig.kind}_v1"
    if sig.kind in _PAGE_SIGNALS:
        page = next((p for p in pages if p.final_url == sig.page_url), homepage)
        return _make_evidence(page, sig.span, method)
    org = str(ctx.org)
    bulk = getattr(ctx, "bulk", None) or {}
    bulk_at = (getattr(ctx, "shared", None) or {}).get("bulk_retrieved_at")
    if sig.kind in ("registry_declared", "registry_email_domain", "email_domain_match"):
        key = "hjemmeside" if sig.kind == "registry_declared" else "epostadresse"
        value = str(bulk.get(key) or "").strip()
        if value and bulk_at:
            return _source_evidence(
                source_url=registry_mod.BULK_DOWNLOAD_URL, source_class="official_registry_bulk", retrieved_at=bulk_at,
                content_sha256=registry_mod.bulk_row_sha256(bulk), method=method,
                span=f"row organisasjonsnummer={org}: {key}", quote=f"{key}: {value}",
            )
    elif sig.kind == "wikidata":
        wd = ctx.caches.wikidata.lookup(org) if getattr(getattr(ctx, "caches", None), "wikidata", None) else None
        if wd and wd.get("qid"):
            sites = ", ".join(wd.get("websites") or [])
            return _source_evidence(
                source_url=f"https://www.wikidata.org/wiki/{wd['qid']}", source_class="open_knowledge_base",
                retrieved_at=wd.get("retrieved_at") or homepage.retrieved_at or utc_now(), content_sha256=_sha(wd),
                method=method, span=f"{wd['qid']} P2333", quote=f"{wd['qid']}: Norwegian organisation number (P2333) {org}; official website (P856) {sites}",
            )
    elif sig.kind == "osm_orgnr":
        m = re.search(r"OpenStreetMap (\w+/\d+) ref:NO:orgnr=(\d{9})", cand.label or "")
        if m:
            return _source_evidence(
                source_url=f"https://www.openstreetmap.org/{m.group(1)}", source_class="open_places_dataset",
                retrieved_at=places_mod.SNAPSHOT_META["osm_base"], content_sha256=_sha([m.group(1), m.group(2), cand.url]),
                method=method, span=f"{m.group(1)} ref:NO:orgnr", quote=f"ref:NO:orgnr={m.group(2)}; website={cand.url}",
            )
    elif sig.kind == "nav_employer_homepage":
        for ad in (getattr(ctx, "shared", None) or {}).get("nav_ads") or []:
            if registered_domain(ad.get("employer_homepage") or "") == cand.domain and ad.get("feedentry_url"):
                return _source_evidence(
                    source_url=ad["feedentry_url"], source_class="public_job_feed", retrieved_at=ad.get("retrieved_at") or utc_now(),
                    content_sha256=ad.get("content_sha256"), method=method, span="$.ad_content.employer",
                    quote=ad.get("employer_quote") or f'"orgnr": "{ad.get("employer_orgnr")}", "homepage": "{ad.get("employer_homepage")}"',
                )
    if sig.kind.startswith("open_places"):
        return None
    page = next((p for p in pages if p.final_url == sig.page_url), homepage)
    return _make_evidence(page, sig.span, method)


def _source_evidence(*, source_url: str, source_class: str, retrieved_at: str, content_sha256: str | None,
                     method: str, span: str, quote: str) -> Evidence:
    return Evidence(
        evidence_id=evidence_id(source_url, span), source_url=source_url, final_url=source_url,
        redirect_chain=[source_url], http_status=None, source_class=source_class, retrieved_at=retrieved_at,
        content_sha256=content_sha256, extraction_method=method, span=span, claim_span=quote[:500],
    )


def _make_evidence(page: PageFetch, span: str | None, method: str) -> Evidence:
    return Evidence(
        evidence_id=evidence_id(page.final_url, span),
        source_url=page.url,
        final_url=page.final_url,
        redirect_chain=page.redirect_chain,
        http_status=page.status,
        source_class="company_owned",
        retrieved_at=page.retrieved_at or utc_now(),
        content_sha256=page.content_sha256,
        snapshot_ref=page.snapshot_ref,
        extraction_method=method,
        span=(span or "")[:500] or None,
    )


def _profile_key(url: str) -> str | None:
    """Same normalisation as site-linked profiles (starter-kit rules: rejects share/personal/post URLs),
    so a profile has one claim id whichever source found it."""
    normalized = normalize_social_url(url)
    return normalized["url"] if normalized else None


def _add_wikidata_profiles(ctx: Any, result: ConnectorResult) -> None:
    """Public social profiles listed on the Wikidata item that carries this organisation number (P2333).

    The org number on the item makes the attribution exact, so these are published even when no website
    was verified. Profiles already linked from the verified site are not duplicated.
    """
    caches = getattr(ctx, "caches", None)
    wikidata = getattr(caches, "wikidata", None) if caches is not None else None
    if wikidata is None:
        return
    try:
        row = wikidata.lookup(ctx.org)
    except Exception:
        return
    profiles = (row or {}).get("profiles") or {}
    if not profiles:
        return
    org = ctx.org
    existing = {c.value["url"] for c in result.claims if c.family == "profiles" and isinstance(c.value, dict) and c.value.get("url")}
    added = 0
    for platform, url in sorted(profiles.items()):
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        key = _profile_key(url)
        if key is None or key in existing:
            continue
        existing.add(key)
        source = row.get("source_url") or "https://query.wikidata.org/sparql"
        ev = Evidence(
            evidence_id=evidence_id(source, f"{row.get('qid')}:{platform}"),
            source_url=f"https://www.wikidata.org/wiki/{row['qid']}" if row.get("qid") else source,
            source_class="open_knowledge_base", retrieved_at=row.get("retrieved_at") or utc_now(),
            content_sha256=hashlib.sha256(f"{row.get('qid')}|{platform}|{url}".encode("utf-8")).hexdigest(),
            extraction_method="wikidata_profiles_v1", span=f"{row.get('qid')} {platform} (item has P2333={org})",
            access_policy="CC0 (Wikidata)",
        )
        result.evidence.append(ev)
        result.claims.append(Claim(
            claim_id=claim_key(org, "profiles", "profile", key), organisation_number=org,
            family="profiles", field="profile", value={"platform": platform, "url": key},
            value_key=key, availability="available", identity_basis="wikidata_org_number",
            relationship="exact", evidence_ids=[ev.evidence_id],
        ))
        added += 1
    if not added:
        return
    state = result.families.get("profiles")
    if state is None or state.availability in ("not_available", "not_applicable"):
        count = sum(1 for c in result.claims if c.family == "profiles")
        result.families["profiles"] = FamilyState(family="profiles", availability="available", claim_count=count,
                                                  sources_checked=list((state.sources_checked if state else []) or []) + ["wikidata"])
    elif state.availability == "available":
        state.claim_count = (state.claim_count or 0) + added


class WebConnector:
    name = "web"
    families: tuple[str, ...] = ("website", "profiles", "description")

    def run(self, ctx: CompanyContext) -> ConnectorResult:
        result = ConnectorResult()
        org = ctx.org
        facts = _registry_facts(ctx)

        try:
            cand_list = candidates_mod.generate_candidates(ctx)
        except Exception as exc:  # candidate generation must never crash the pipeline
            result.errors.append({"stage": "candidates", "error": f"{type(exc).__name__}: {exc}"[:300]})
            cand_list = []

        # Decisive-source candidates first, preserving relative order within each group.
        ordered = sorted(cand_list, key=lambda c: (c.source not in DECISIVE_SOURCES, c.rank))

        attempts: list[dict[str, Any]] = []
        exact_pages: list[PageFetch] | None = None
        exact_candidate: Candidate | None = None
        exact_verdict: verify_mod.Verdict | None = None
        best_related: tuple[Candidate, verify_mod.Verdict, list[PageFetch]] | None = None

        client = getattr(ctx, "client", None)

        # A DECISIVE-sourced candidate (registry hjemmeside, Wikidata, NAV) is a strong, independent
        # claim -- if one EXISTS but can't be confirmed (unreachable, parked, rejected, or merely
        # ambiguous, including a transient fetch failure on an otherwise-correct candidate), a different,
        # weaker candidate reached later needs a decisive signal to reach `exact`, not bare 2-signal
        # corroboration alone. Two traps found on the gold set:
        # - DACON SERVICES AS: a name-guessed sibling/coincidence domain can genuinely show a matching
        #   address+phone on its own contact page while the registry-declared site sits behind a broken
        #   SSL cert -- corroboration can't tell "same company, second domain" apart from "different
        #   company, same office/phone history" here.
        # - AF GRUPPEN ASA: the correct wikidata_website candidate (afgruppen.no) hit a transient network
        #   failure in one run of a live batch; without this guard, a LATER name-guessed candidate
        #   (afgruppen.com, a plausible-looking but wrong international/investor domain) had enough
        #   corroborating signals of its own to reach exact and get published instead -- a live-network
        #   flake turning into a wrong-company publication. Checking ALL of DECISIVE_SOURCES (not just
        #   registry_website) closes this: any of them being unconfirmed raises the bar for what's left.
        has_decisive_source = any(c.source in DECISIVE_SOURCES for c in cand_list)
        decisive_source_unconfirmed = False

        def _mark_unconfirmed(cand: Candidate) -> None:
            nonlocal decisive_source_unconfirmed
            if cand.source in DECISIVE_SOURCES:
                decisive_source_unconfirmed = True

        own_email = candidates_mod.own_email_domain(ctx, facts)
        for cand in ordered:
            require_decisive = (
                has_decisive_source and decisive_source_unconfirmed
                and cand.source not in DECISIVE_SOURCES
            )
            if client is not None and client.remaining(org) <= 0:
                attempts.append({"domain": cand.domain, "source": cand.source, "status": "skipped", "reason": "request_budget"})
                _mark_unconfirmed(cand)
                continue
            try:
                crawl_result = crawl_candidate(ctx, cand)
            except Exception as exc:
                attempts.append({"domain": cand.domain, "source": cand.source, "status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]})
                _mark_unconfirmed(cand)
                continue

            if crawl_result.fatal_error:
                attempts.append({"domain": cand.domain, "source": cand.source, "status": "unreachable", "reason": crawl_result.fatal_error})
                _mark_unconfirmed(cand)
                continue
            if crawl_result.parked:
                attempts.append({"domain": cand.domain, "source": cand.source, "status": "rejected", "reason": "parked_or_placeholder"})
                _mark_unconfirmed(cand)
                continue

            website_org_count = _website_org_count(ctx, cand.domain) if cand.source == "registry_website" else None
            strict = require_decisive or _redirects_into_other_site(cand, crawl_result.pages, own_email)
            verdict = verify_mod.assess(org, crawl_result.pages, facts, cand, website_org_count=website_org_count,
                                        require_decisive=strict, own_email_domain=own_email)
            if _worth_confirming(verdict) and fetch_confirmation_pages(ctx, crawl_result):
                # The site carries our name but nothing else read so far ties it to our record: read its
                # privacy/terms/contact pages (where the org number usually is) and assess again, same rules.
                verdict = verify_mod.assess(org, crawl_result.pages, facts, cand, website_org_count=website_org_count,
                                            require_decisive=strict, own_email_domain=own_email)
            attempts.append({
                "domain": cand.domain, "source": cand.source, "status": verdict.status,
                "identity_basis": verdict.identity_basis, "relationship": verdict.relationship,
                "note": verdict.note, "signals": [s.kind for s in verdict.signals],
                "conflicts": [c.org_number for c in verdict.conflicts],
            })
            if verdict.status != "exact":
                _mark_unconfirmed(cand)

            if verdict.status == "exact":
                exact_pages = crawl_result.pages
                exact_candidate = cand
                exact_verdict = verdict
                break
            if verdict.status == "related" and best_related is None:
                best_related = (cand, verdict, crawl_result.pages)

        result.shared["web_attempts"] = attempts
        _debug_log(org, attempts)

        if exact_pages is not None and exact_candidate is not None and exact_verdict is not None:
            self._publish_exact(ctx, result, exact_candidate, exact_verdict, exact_pages)
        elif best_related is not None and _is_group_site(ctx, *best_related):
            self._publish_group_site(ctx, result, *best_related)
        elif best_related is not None:
            self._publish_related(ctx, result, *best_related)
        elif _previous_site_unreachable(ctx, attempts):
            # The site we verified last run could not be fetched this run (TLS/connection error). That is
            # "not checked", not "gone": mark these families failed so refresh keeps the stored facts.
            result.shared["website_unchecked"] = True
            reason = "previously verified site unreachable this run; previous profile kept"
            for fam in ("website", "profiles", "description"):
                result.families[fam] = FamilyState(family=fam, availability="failed", reason=reason, sources_checked=[a["domain"] for a in attempts])
        elif (challenged := _declared_site_challenged(attempts)) is not None:
            # The site the registry (or Wikidata/NAV/OSM) declares for this company answered with a CAPTCHA or
            # browser check. It was not read, so the family is blocked, not "nothing found".
            result.families["website"] = FamilyState(
                family="website", availability="blocked", sources_checked=[a["domain"] for a in attempts],
                reason=f"declared site {challenged} answered with a bot challenge (CAPTCHA); not bypassed",
            )
            result.families["profiles"] = FamilyState(family="profiles", availability="not_available", reason="no verified site to link profiles from")
            result.families["description"] = FamilyState(family="description", availability="not_available", reason="no verified site to extract description from")
        else:
            reason = f"checked {len(attempts)} candidates: none verified" if attempts else "no candidates"
            result.families["website"] = FamilyState(family="website", availability="not_available", reason=reason, sources_checked=[a["domain"] for a in attempts])
            result.families["profiles"] = FamilyState(family="profiles", availability="not_available", reason="no verified site to link profiles from")
            result.families["description"] = FamilyState(family="description", availability="not_available", reason="no verified site to extract description from")

        _add_wikidata_profiles(ctx, result)
        return result

    # -- publication helpers -------------------------------------------------------------------------

    def _publish_exact(
        self, ctx: CompanyContext, result: ConnectorResult, cand: Candidate, verdict: verify_mod.Verdict, pages: list[PageFetch],
    ) -> None:
        org = ctx.org
        homepage = next((p for p in pages if p.page_kind == "homepage"), pages[0])
        extraction = extract_mod.extract_all(pages)

        # Evidence for the decisive/corroborating signals that proved identity.
        site_evidence: list[Evidence] = []
        # Strongest proof first, one record per kind, in a fixed order: the quoted text must not depend on
        # which page or signal happened to be found first in this run.
        seen_kinds: set[str] = set()
        ranked = []
        for sig in sorted(verdict.signals, key=lambda x: (_SIGNAL_RANK.get(x.kind, 99), x.page_url or "", x.span or "")):
            if sig.kind not in seen_kinds:
                seen_kinds.add(sig.kind)
                ranked.append(sig)
        for sig in ranked[:5]:
            ev = _signal_evidence(ctx, sig, cand, pages, homepage)
            if ev is not None:
                site_evidence.append(ev)
        if not site_evidence:
            site_evidence = [_make_evidence(homepage, None, "web_homepage_fetch_v1")]
        for ev in site_evidence:
            result.evidence.append(ev)
        evidence_ids = [ev.evidence_id for ev in site_evidence]

        # Output contract: the official_website value is the site URL itself.
        website_value = _public_url(cand, homepage)
        ckey = claim_key(org, "website", "official_website", None)
        result.claims.append(Claim(
            claim_id=ckey, organisation_number=org, family="website", field="official_website",
            value=website_value, availability="available", confidence=1.0,
            identity_basis=verdict.identity_basis, relationship="exact", evidence_ids=evidence_ids,
        ))
        result.families["website"] = FamilyState(family="website", availability="available", sources_checked=[cand.domain], claim_count=1)

        contact_claims = 0
        if extraction.contact_email:
            ev = _make_evidence(homepage, extraction.contact_email, "web_contact_email_v1")
            result.evidence.append(ev)
            result.claims.append(Claim(
                claim_id=claim_key(org, "website", "site_contact_email", None), organisation_number=org,
                family="website", field="site_contact_email", value=extraction.contact_email,
                availability="available", identity_basis="linked_from_verified_site", relationship="exact",
                evidence_ids=[ev.evidence_id],
            ))
            contact_claims += 1
        if extraction.contact_phone:
            ev = _make_evidence(homepage, extraction.contact_phone, "web_contact_phone_v1")
            result.evidence.append(ev)
            result.claims.append(Claim(
                claim_id=claim_key(org, "website", "site_phone", None), organisation_number=org,
                family="website", field="site_phone", value=extraction.contact_phone,
                availability="available", identity_basis="linked_from_verified_site", relationship="exact",
                evidence_ids=[ev.evidence_id],
            ))
            contact_claims += 1
        result.families["website"].claim_count += contact_claims

        # profiles
        if extraction.social_links:
            _publish_profiles(result, org, extraction.social_links, pages, homepage, "linked_from_verified_site", "exact")
            result.families["profiles"] = FamilyState(family="profiles", availability="available", claim_count=len(extraction.social_links), sources_checked=[cand.domain])
        else:
            result.families["profiles"] = FamilyState(family="profiles", availability="not_available", reason="checked: none found", sources_checked=[cand.domain])

        # description
        if extraction.description:
            desc_page = next((p for p in pages if p.final_url == extraction.description_source_url), homepage)
            ev = _make_evidence(desc_page, extraction.description_span, extraction.description_method or "web_description_v1")
            result.evidence.append(ev)
            result.claims.append(Claim(
                claim_id=claim_key(org, "description", "company_description", None), organisation_number=org,
                family="description", field="company_description", value=extraction.description,
                availability="available", identity_basis="linked_from_verified_site", relationship="exact",
                evidence_ids=[ev.evidence_id],
            ))
            result.families["description"] = FamilyState(family="description", availability="available", claim_count=1, sources_checked=[cand.domain])
        else:
            result.families["description"] = FamilyState(family="description", availability="not_available", reason="checked: none found", sources_checked=[cand.domain])

        result.shared["verified_site"] = {"url": homepage.final_url, "domain": cand.domain, "identity_basis": verdict.identity_basis}
        result.shared["site_pages"] = [
            {
                "url": p.url, "final_url": p.final_url, "snapshot_ref": p.snapshot_ref,
                "text_excerpt": (p.text or "")[:3000], "links": p.links,
            }
            for p in pages if p.ok
        ]
        result.shared["social_links"] = extraction.social_links
        result.shared["feed_urls"] = extraction.feed_urls
        result.shared["ats_links"] = extraction.ats_links
        result.shared["brand_name"] = extraction.brand_name
        result.shared["news_urls"] = [extraction.news_url] if extraction.news_url else []

    def _publish_group_site(
        self, ctx: CompanyContext, result: ConnectorResult, cand: Candidate, verdict: verify_mod.Verdict, pages: list[PageFetch],
    ) -> None:
        """The registry lists this site for the company, and it is the company's group/parent site (shared by a
        few group companies, or showing another group company's org number). Published as the company's
        website, labelled with the relationship -- never as an exact own-site, so no profiles or description
        are taken from it."""
        org = ctx.org
        homepage = next((p for p in pages if p.page_kind == "homepage"), pages[0])
        declared_by = "registry hjemmeside" if cand.source == "registry_website" else cand.label
        branch = _declared_branch_page(ctx, cand, pages)
        where = "its own page on a shared chain/group site" if branch else "the company's group site"
        note = f"website declared for this organisation number ({declared_by}) is {where} ({verdict.relationship}): {verdict.note}"
        ev = _make_evidence(homepage, f"{declared_by}: {cand.url}", "web_group_site_v1")
        result.evidence.append(ev)
        result.claims.append(Claim(
            claim_id=claim_key(org, "website", "official_website", None), organisation_number=org,
            family="website", field="official_website", value=_public_url(cand, homepage), availability="available",
            confidence=0.8,
            identity_basis={"wikidata_website": "wikidata_org_number", "osm_orgnr_website": "open_map_org_number"}.get(cand.source, "registry_declared"),
            relationship=verdict.relationship,
            evidence_ids=[ev.evidence_id], note=note,
        ))
        result.families["website"] = FamilyState(
            family="website", availability="available", reason=note, sources_checked=[cand.domain], claim_count=1,
        )
        result.families["profiles"] = FamilyState(family="profiles", availability="not_available", reason="website is a group site; its profiles belong to the group")
        stays_on_domain = registered_domain(homepage.final_url or "") == cand.domain
        if not branch and stays_on_domain and _domain_is_our_name(ctx, cand):
            # The group site's domain is exactly our own name (HAIKJEFTEN AS -> haikjeften.no, which shows a
            # sister company's org number) and the page is on that domain, not forwarded to the parent's
            # (frontsystems.no -> egsoftware.com): the profiles it links are the brand we trade under.
            # Published with the group relationship, never as exact.
            links = extract_mod.extract_all(pages).social_links
            if links:
                basis = {"wikidata_website": "wikidata_org_number", "osm_orgnr_website": "open_map_org_number"}.get(cand.source, "registry_declared")
                _publish_profiles(result, org, links, pages, homepage, basis, verdict.relationship,
                                  note=f"linked from the group site {cand.domain}, whose domain is this organisation's name")
                result.families["profiles"] = FamilyState(
                    family="profiles", availability="available", claim_count=len(links), sources_checked=[cand.domain],
                    reason="linked from the group site that carries this organisation's name",
                )
        result.families["description"] = FamilyState(family="description", availability="not_available", reason="website is a group site; its text describes the group")
        result.shared["web_related_site"] = {"url": homepage.final_url, "domain": cand.domain, "relationship": verdict.relationship}

    def _publish_related(
        self, ctx: CompanyContext, result: ConnectorResult, cand: Candidate, verdict: verify_mod.Verdict, pages: list[PageFetch],
    ) -> None:
        org = ctx.org
        homepage = next((p for p in pages if p.page_kind == "homepage"), pages[0])
        ev = _make_evidence(homepage, verdict.note, "web_related_site_v1")
        result.evidence.append(ev)
        value = homepage.final_url
        result.claims.append(Claim(
            claim_id=claim_key(org, "website", "official_website", None), organisation_number=org,
            family="website", field="official_website", value=value, availability="ambiguous",
            confidence=0.4, relationship=verdict.relationship, evidence_ids=[ev.evidence_id],
            note=verdict.note,
        ))
        result.families["website"] = FamilyState(
            family="website", availability="ambiguous",
            reason=f"related site found ({verdict.relationship}): {verdict.note}", sources_checked=[cand.domain], claim_count=1,
        )
        result.families["profiles"] = FamilyState(family="profiles", availability="not_available", reason="no exact-verified site to link profiles from")
        result.families["description"] = FamilyState(family="description", availability="not_available", reason="no exact-verified site to extract description from")
        result.shared["web_related_site"] = {"url": homepage.final_url, "domain": cand.domain, "relationship": verdict.relationship}
