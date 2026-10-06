"""Extract description, brand name, contact info, JSON-LD facts, feeds, ATS links and social profiles
from an exact-verified site's fetched pages.

No network access — operates purely on already-fetched `crawl.PageFetch` objects.
"""
from __future__ import annotations

import re
import urllib.parse
import warnings
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from ..text import fold
from ..urls import normalize_social_url

from .candidates import registered_domain
from .crawl import PageFetch

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

ATS_DOMAINS = {
    "teamtailor.com": "teamtailor",
    "webcruiter.no": "webcruiter",
    "webcruiter.com": "webcruiter",
    "jobylon.com": "jobylon",
    "reachmee.com": "reachmee",
    "hr-manager.net": "hrmanager",
    "hrmanager.no": "hrmanager",
    "recman.no": "recman",
    "easycruit.com": "easycruit",
    "myworkdayjobs.com": "workday",
    "workday.com": "workday",
    "smartrecruiters.com": "smartrecruiters",
    "lever.co": "lever",
    "greenhouse.io": "greenhouse",
    "jobbnorge.no": "jobbnorge",
    "finn.no": "finn_job",
}

MAX_DESCRIPTION_CHARS = 400


@dataclass
class ExtractionResult:
    description: str | None = None
    description_source_url: str | None = None
    description_span: str | None = None
    description_method: str | None = None
    brand_name: str | None = None
    brand_name_source_url: str | None = None
    social_links: list[dict[str, str]] = field(default_factory=list)  # {platform, url, source_url}
    contact_email: str | None = None
    contact_email_source_url: str | None = None
    contact_phone: str | None = None
    contact_phone_source_url: str | None = None
    addresses: list[dict[str, str]] = field(default_factory=list)
    jsonld_organizations: list[dict[str, Any]] = field(default_factory=list)
    feed_urls: list[str] = field(default_factory=list)
    ats_links: list[dict[str, str]] = field(default_factory=list)  # {platform, url}
    news_url: str | None = None


def _jsonld_objects(html: str, base_url: str) -> list[dict[str, Any]]:
    try:
        import extruct

        data = extruct.extract(html, base_url=base_url, syntaxes=["json-ld"])
        return data.get("json-ld", []) or []
    except Exception:
        return []


def _walk_organizations(nodes: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(nodes, dict):
        kind = nodes.get("@type")
        kinds = set(kind if isinstance(kind, list) else [kind])
        if kinds & {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant"}:
            out.append(nodes)
        for child in nodes.values():
            _walk_organizations(child, out)
    elif isinstance(nodes, list):
        for child in nodes:
            _walk_organizations(child, out)


def _first_org(pages: list[PageFetch]) -> tuple[dict[str, Any] | None, str | None]:
    for page in pages:
        if not page.ok:
            continue
        objs: list[dict[str, Any]] = []
        _walk_organizations(_jsonld_objects(page.html, page.final_url), objs)
        if objs:
            return objs[0], page.final_url
    return None, None


def extract_description(pages: list[PageFetch]) -> tuple[str | None, str | None, str | None, str | None]:
    """Returns (description, source_url, span, method), preferring JSON-LD, then meta description, then
    the first substantive paragraph of an about-ish page. Each candidate must read as a description (see
    `_usable_description`); a rejected one falls through to the next source."""
    names = _site_names(pages)
    org, org_url = _first_org(pages)
    if org and isinstance(org.get("description"), str):
        text = _trim(org["description"])[:MAX_DESCRIPTION_CHARS]
        if _usable_description(text, names):
            return text, org_url, text, "jsonld_description_v1"

    for page in pages:
        if not page.ok:
            continue
        soup = BeautifulSoup(page.html, "lxml")
        tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
        content = _trim(str(tag.get("content") or "")) if tag else ""
        if _usable_description(content[:MAX_DESCRIPTION_CHARS], names):
            return content[:MAX_DESCRIPTION_CHARS], page.final_url, content[:MAX_DESCRIPTION_CHARS], "meta_description_v1"

    # "kontor"/"kontoret" ("the office") is a common Norwegian about-us-equivalent page slug (see
    # crawl.py's PRIORITY_TERMS) -- an image-portfolio-style homepage with no body text at all
    # (architecture/design firms especially) routinely carries its real description there instead.
    about_pages = [p for p in pages if p.ok and p.page_kind in {"om-oss", "om_oss", "about-us", "about", "kontor"}]
    for page in about_pages or [p for p in pages if p.ok and p.page_kind == "homepage"]:
        text = (page.text or "").strip()
        # first substantive paragraph: first run of text >= 60 chars
        for para in re.split(r"\n{1,}", text):
            para = _trim(para)
            if len(para) >= 60 and _usable_description(para, names):
                return para[:MAX_DESCRIPTION_CHARS], page.final_url, para[:MAX_DESCRIPTION_CHARS], "trafilatura_first_paragraph_v1"
    return None, None, None, None


def _trim(text: str) -> str:
    """Drop separator debris a title template leaves at the ends ("| From the Sognefjord ...")."""
    return text.strip().strip("|-–—:·• ").strip()


def _site_names(pages: list[PageFetch]) -> set[str]:
    """The site's own name as its titles, og:site_name and JSON-LD name state it (each title part too)."""
    raw: list[str] = []
    for page in pages:
        if not page.ok:
            continue
        raw.append(page.title or "")
        raw.extend(re.split(r"\s[|\-–—:·]\s", page.title or ""))
        soup = BeautifulSoup(page.html, "lxml")
        tag = soup.select_one('meta[property="og:site_name"]')
        if tag:
            raw.append(str(tag.get("content") or ""))
    org, _ = _first_org(pages)
    if org and isinstance(org.get("name"), str):
        raw.append(org["name"])
    return {n for n in (_name_key(r) for r in raw) if n}


def _name_key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", fold(text)))


_DAY_RE = re.compile(r"\b(man|tir|ons|tor|fre|lor|son|mon|tue|wed|thu|fri|sat|sun)[a-z]*\b")
_TIME_RE = re.compile(r"\d{1,2}[:.]\d{2}")


def _usable_description(text: str, names: set[str]) -> bool:
    """Reads as a description of the company: at least three words, not just the site's name, not opening
    hours, not boilerplate, and not the text of an error/empty/default page."""
    if not text:
        return False
    key = _name_key(text)
    words = len(key.split())
    # A short text equal to the site's name (or a title part) is a name, not a description; a longer title
    # part is usually the company's tagline and is kept.
    if words < 3 or (words <= 4 and key in names):
        return False
    if max(len(token) for token in text.split()) > 40:  # encoded data or a run-together URL, not prose
        return False
    folded = fold(text)
    if len(text) < 80 and _TIME_RE.search(folded) and _DAY_RE.search(folded):
        return False
    if any(marker in folded for marker in _DEAD_PAGE_MARKERS):
        return False
    return not _is_boilerplate_paragraph(text)


# Text of a 404 / empty-archive / server or hosting default page, which a live site can still serve.
_DEAD_PAGE_MARKERS = (
    "proudly served by", "index of /", "this one is taken", "ingenting ble funnet", "nothing was found",
    "nothing found", "page not found", "siden finnes ikke", "fant ikke siden", "finner ikke siden",
    "siden du leter etter", "the page you are looking for",
)


# Cookie-consent banners, generic CMS placeholder text and bare "Welcome to X" greetings routinely
# beat the real about-paragraph to the "first run of text >= 60 chars" check (a cookie banner is
# often the first substantial text node in the DOM, rendered above the actual page content) --
# publishing one of these as the company's "description" is technically non-empty but useless and
# occasionally actively misleading (a cookie-policy sentence has nothing to do with the company).
# A short, narrow keyword/pattern list, checked against the FULL paragraph (not the whole page) so
# it only skips a paragraph that is itself boilerplate, not one that merely appears near a banner.
_BOILERPLATE_MARKERS = (
    "informasjonskapsler", "cookies", "personvernerklæring", "personvernerklaering",
    "vi bruker cookies", "we use cookies", "accept all cookies", "aksepter alle",
    "denne nettsiden bruker", "this website uses",
)
_WELCOME_ONLY_RE = re.compile(
    r"^(velkommen til|welcome to)\s+[^.!?]{0,80}[.!?]?$", re.IGNORECASE
)


def _is_boilerplate_paragraph(text: str) -> bool:
    normalized = text.strip().casefold()
    if _WELCOME_ONLY_RE.match(normalized):
        return True
    return any(marker in normalized for marker in _BOILERPLATE_MARKERS)


def extract_brand_name(pages: list[PageFetch]) -> tuple[str | None, str | None]:
    org, org_url = _first_org(pages)
    if org and isinstance(org.get("name"), str) and org["name"].strip():
        return org["name"].strip(), org_url
    for page in pages:
        if not page.ok:
            continue
        soup = BeautifulSoup(page.html, "lxml")
        tag = soup.select_one('meta[property="og:site_name"]')
        if tag and str(tag.get("content") or "").strip():
            return str(tag["content"]).strip(), page.final_url
    for page in pages:
        if page.ok and page.title:
            return page.title, page.final_url
    return None, None


_CREDIT_WORDS = (
    "levert av", "laget av", "utviklet av", "design av", "designet av", "nettside av", "nettsider av",
    "webdesign", "web design", "designed by", "developed by", "powered by", "made by", "site by", "website by",
    "drevet av", "produsert av",
)


def _is_credit_link(node: Any) -> bool:
    """A link inside a "made by <agency>" credit belongs to the web agency, not to the company."""
    own = node.get_text(" ", strip=True).casefold()
    if any(word in own for word in _CREDIT_WORDS):
        return True
    parent = node.parent
    if parent is None or not hasattr(parent, "select"):
        return False
    # Only a short credit line that holds this one link (not a footer listing the company's own links).
    if len(parent.select("a[href]")) != 1:
        return False
    text = parent.get_text(" ", strip=True).casefold()
    return len(text) <= 150 and any(word in text for word in _CREDIT_WORDS)


def extract_social_links(pages: list[PageFetch]) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}
    for page in pages:
        if not page.ok:
            continue
        soup = BeautifulSoup(page.html, "lxml")
        candidates = [str(node.get("href") or "") for node in soup.select("a[href]") if not _is_credit_link(node)]
        candidates.extend(str(node.get("src") or "") for node in soup.select("iframe[src]"))
        for raw in candidates:
            url = urllib.parse.urljoin(page.final_url, raw)
            normalized = normalize_social_url(url)
            if normalized:
                key = (normalized["platform"], normalized["url"])
                if key not in found:
                    found[key] = {**normalized, "source_url": page.final_url}
        org, _ = _first_org([page])
        if org:
            same_as = org.get("sameAs")
            urls = same_as if isinstance(same_as, list) else [same_as] if same_as else []
            for raw in urls:
                if not isinstance(raw, str):
                    continue
                normalized = normalize_social_url(raw)
                if normalized:
                    key = (normalized["platform"], normalized["url"])
                    if key not in found:
                        found[key] = {**normalized, "source_url": page.final_url}
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(\+?47[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}|\d{2}[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2})(?!\d)")


# Template/demo addresses left in themes and forms ("bruker@domene.no", "user@domain.com"), and addresses
# that belong to the site builder or its error tracker rather than to the company.
_PLACEHOLDER_EMAIL_DOMAIN_LABELS = {
    "domene", "domain", "example", "eksempel", "email", "website", "navnesen", "yourdomain", "dittdomene",
    "mydomain", "yoursite", "dittfirma", "firmanavn", "companyname",
}
_PLACEHOLDER_EMAIL_LOCALS = {"navn", "name", "bruker", "user", "fornavn", "fornavn.etternavn", "ditt.navn", "your.name", "yourname"}
_PLATFORM_EMAIL_DOMAINS = (
    "wixpress.com", "wix.com", "sentry.io", "webador.com", "squarespace.com", "jimdo.com", "godaddy.com",
    "one.com", "domeneshop.no", "wordpress.com", "weebly.com", "shopify.com",
)


def _clean_email(raw: str) -> str | None:
    email = urllib.parse.unquote(raw).strip().strip(".")
    if not _EMAIL_RE.fullmatch(email):
        return None
    local, domain = email.lower().rsplit("@", 1)
    if domain.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")) or domain.startswith("www."):
        return None
    if local in _PLACEHOLDER_EMAIL_LOCALS or domain.split(".")[0] in _PLACEHOLDER_EMAIL_DOMAIN_LABELS:
        return None
    if "sentry" in domain or any(_on_domain(domain, d) for d in _PLATFORM_EMAIL_DOMAINS):
        return None
    return email


def _page_emails(page: PageFetch) -> list[str]:
    """E-mails the page shows a visitor: mailto links (not inside a web-agency credit) and the page's visible
    text, footer included. Scripts, styles and attributes are not scanned: they hold e-mail-shaped strings
    (error-tracker keys, image file names, embedded dealer lists) that are no contact of this company."""
    soup = BeautifulSoup(page.html, "lxml")
    raw = [str(a.get("href") or "")[7:].split("?")[0] for a in soup.select('a[href^="mailto:" i]') if not _is_credit_link(a)]
    for node in soup.select("script, style, noscript, template"):
        node.decompose()
    for credit in [a for a in soup.select("a[href]") if _is_credit_link(a)]:
        credit.decompose()
    raw.extend(_EMAIL_RE.findall(soup.get_text(" ", strip=True)))
    return [e for e in dict.fromkeys(_clean_email(r) for r in raw) if e]


def _on_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def extract_contact(pages: list[PageFetch]) -> tuple[str | None, str | None, str | None, str | None]:
    contact_pages = [p for p in pages if p.ok and p.page_kind in {"kontakt", "contact", "homepage"}]
    site_domain = next((registered_domain(p.final_url) for p in pages if p.ok and p.page_kind == "homepage"), None)
    found = [(email, page) for page in (contact_pages or pages) if page.ok for email in _page_emails(page)]
    # An address on the site's own domain first; otherwise the first one shown (often a free-mail address).
    own = [(e, p) for e, p in found if site_domain and _on_domain(e.rsplit("@", 1)[1].lower(), site_domain)]
    if own or found:
        email, page = (own or found)[0]
        return email, page.final_url, None, None
    for page in contact_pages or pages:
        if not page.ok:
            continue
        phones = _PHONE_RE.findall(page.text)
        if phones:
            return None, None, re.sub(r"[\s.]", "", phones[0]), page.final_url
    return None, None, None, None


def extract_addresses(pages: list[PageFetch]) -> list[dict[str, str]]:
    addresses: list[dict[str, str]] = []
    seen = set()
    for page in pages:
        if not page.ok:
            continue
        org, _ = _first_org([page])
        if not org:
            continue
        addr = org.get("address")
        if isinstance(addr, dict):
            entry = {
                "street": str(addr.get("streetAddress") or ""),
                "postcode": str(addr.get("postalCode") or ""),
                "city": str(addr.get("addressLocality") or ""),
                "source_url": page.final_url,
            }
            key = (entry["street"], entry["postcode"], entry["city"])
            if any(entry.values()) and key not in seen:
                seen.add(key)
                addresses.append(entry)
    return addresses


def extract_jsonld_fields(pages: list[PageFetch]) -> list[dict[str, Any]]:
    org, url = _first_org(pages)
    if not org:
        return []
    fields = {k: org[k] for k in ("foundingDate", "numberOfEmployees", "address", "sameAs") if k in org}
    if not fields:
        return []
    return [{"source_url": url, **fields}]


def extract_feed_urls(pages: list[PageFetch]) -> list[str]:
    found: list[str] = []
    for page in pages:
        if not page.ok:
            continue
        soup = BeautifulSoup(page.html, "lxml")
        for link in soup.select('link[rel="alternate"]'):
            link_type = str(link.get("type") or "").lower()
            if link_type in {"application/rss+xml", "application/atom+xml"}:
                href = str(link.get("href") or "")
                if href and "/comments/feed" not in href and "comments-feed" not in href:
                    # A comments feed holds visitors' comments, never company news.
                    url = urllib.parse.urljoin(page.final_url, href)
                    if url not in found:
                        found.append(url)
    return found


def extract_ats_links(pages: list[PageFetch]) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for page in pages:
        if not page.ok:
            continue
        for link_url in list(page.links or []):
            _match_ats(link_url, found)
        soup = BeautifulSoup(page.html, "lxml")
        for anchor in soup.select("a[href]"):
            href = str(anchor.get("href") or "")
            if href:
                _match_ats(urllib.parse.urljoin(page.final_url, href), found)
    return sorted(found.values(), key=lambda item: item["url"])


def _match_ats(url: str, found: dict[str, dict[str, str]]) -> None:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for domain, platform in ATS_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            found.setdefault(url, {"platform": platform, "url": url})
            return


def extract_news_url(pages: list[PageFetch]) -> str | None:
    for page in pages:
        if page.ok and page.page_kind in {"nyheter", "aktuelt", "news"}:
            return page.final_url
    return None


def extract_all(pages: list[PageFetch]) -> ExtractionResult:
    description, desc_url, desc_span, desc_method = extract_description(pages)
    brand_name, brand_url = extract_brand_name(pages)
    email, email_url, phone, phone_url = extract_contact(pages)
    return ExtractionResult(
        description=description,
        description_source_url=desc_url,
        description_span=desc_span,
        description_method=desc_method,
        brand_name=brand_name,
        brand_name_source_url=brand_url,
        social_links=extract_social_links(pages),
        contact_email=email,
        contact_email_source_url=email_url,
        contact_phone=phone,
        contact_phone_source_url=phone_url,
        addresses=extract_addresses(pages),
        jsonld_organizations=extract_jsonld_fields(pages),
        feed_urls=extract_feed_urls(pages),
        ats_links=extract_ats_links(pages),
        news_url=extract_news_url(pages),
    )
