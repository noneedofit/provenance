"""RSS/Atom feed parsing and dated-item extraction from company site pages.

Primary sources are free: `ctx.shared["feed_urls"]` (`<link rel="alternate">` on already-crawled pages)
and `ctx.shared["news_urls"]` published by the web connector (W3), both only present once the company's
site is verified `exact`.

W3's own page-picker prioritizes identity-verification pages (kontakt/om-oss/personvern/vilkar/karriere)
over a news/blog page for its tier-limited secondary-page budget, so a real news section routinely never
gets fetched at all -- `feed_urls`/`news_urls` end up empty even when the site clearly has one (measured
on daily100: several "failed: no source" companies turned out to have an undiscovered `/blogg` or
`/aktuelt` link right on the homepage). `ARTICLE_DISCOVERY_BUDGET` extra requests per verified site pay for
targeted follow-up (see `collect` for the current order; the history below explains the earlier cap):
1. A same-domain link already visible on a page W3 fetched (`ctx.shared["site_pages"][*]["links"]`) but
   never itself fetched, whose path/text matches a Norwegian news/blog term W3's crawl priority ranks
   below the identity pages -- free to *find* (already-fetched HTML), only fetching it costs a request,
   and it's real (not a guess), so this goes first.
2. A WordPress `/feed/` guess (near-universal on WP sites, the single most common Norwegian
   small-business CMS) -- only tried when (1) found no candidate link to check.
A gold-131 measurement (before this reordering/cap) showed spending up to 3 requests/site (adding a
`/wp-json/wp/v2/posts` REST probe as a 3rd step) pushed one company over the request budget and gained
no further coverage the cheaper 2-step version didn't already reach -- cut back to 2 for that reason.
Every extra fetch still goes through `ctx.client.get(..., respect_robots=True)` like any other request,
and is counted against the same per-company budget.

Only dated items become claims. Undated items are dropped rather than fabricating a date.
"""
from __future__ import annotations

import html as html_lib
import itertools
import json
import re
import urllib.parse
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from ..models import evidence_id
from ._common import make_claim, make_evidence

MAX_ACTIVITY_ITEMS = 10

# Nov 2026: one guessed request found news on 1 of 17 verified sites in smoke100 although most of them
# publish news (dns.no/nyheter lists dozens of press releases). A listing page was read only for its own
# date and then dropped, and WordPress's post list was never asked. A verified site now gets up to
# ARTICLE_DISCOVERY_BUDGET requests: the WordPress post list (one request, up to 10 dated posts), then
# up to ARTICLE_FOLLOW_MAX article links from a news listing page. Only ~1 in 5 companies has a verified
# site, so this adds well under 100 requests per 100 companies.
ARTICLE_DISCOVERY_BUDGET = 4
ARTICLE_FOLLOW_MAX = 2
WP_POSTS_QUERY = "wp-json/wp/v2/posts?per_page=10&_fields=date,link,title"

# Terms W3's crawl.py PRIORITY_TERMS ranks below the identity-verification pages (kontakt/om-oss/
# personvern/vilkar/karriere), so a genuine news/blog link is routinely present on an already-fetched
# page but never itself fetched within the tier's secondary-page budget. Superset of W3's own
# nyheter/aktuelt/news (kept in sync deliberately -- see docs/activity.md) plus a few more common
# Norwegian/English news-and-blog terms that are just as unambiguous a signal.
_NEWS_LINK_TERMS = ("nyheter", "aktuelt", "presse", "blogg", "blog", "artikler", "news")

_NORWEGIAN_MONTHS = {
    "januar": 1, "februar": 2, "mars": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12,
}
_NO_MONTH_RE = re.compile(
    r"\b(\d{1,2})\.\s*(" + "|".join(_NORWEGIAN_MONTHS) + r")\s*(\d{4})\b", re.I
)
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(elem: ET.Element, *names: str) -> str | None:
    wanted = {n.lower() for n in names}
    for child in list(elem):
        if _local(child.tag) in wanted and child.text and child.text.strip():
            return child.text.strip()
    return None


def _atom_link(elem: ET.Element) -> str | None:
    links = [c for c in list(elem) if _local(c.tag) == "link"]
    if not links:
        return None
    for link in links:
        if link.get("rel") in (None, "alternate"):
            href = link.get("href")
            if href:
                return href
    href = links[0].get("href")
    return href


def normalize_iso_date(raw: str | None) -> str | None:
    """Best-effort normalization of a date string to an ISO 8601 date (YYYY-MM-DD or full timestamp)."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        return dt.date().isoformat()
    except Exception:
        pass
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    return None


def parse_norwegian_date(text: str) -> str | None:
    m = _NO_MONTH_RE.search(text)
    if m:
        day = int(m.group(1))
        month = _NORWEGIAN_MONTHS[m.group(2).lower()]
        year = int(m.group(3))
        if 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    m = _NUMERIC_DATE_RE.search(text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


_SECTION_TITLE = re.compile(
    r"^(siste\s+)?(nyheter|news|presse(meldinger)?|press|aktuelt|blogg?|artikler|articles|arkiv|archive)"
    r"(\s*(/|og|&|and)\s*\w+)?(\s+(fra|from|i|hos)\s+.{1,40})?$",
    re.IGNORECASE,
)
_DATE_TAGS = ("pubDate", "published", "dc:date")


def raw_feed_item_quote(text: str, item: dict) -> str | None:
    """The item's own <title> and date elements copied verbatim from the feed XML (found via its link)."""
    url = str(item.get("url") or "")
    if not text or not url:
        return None
    at = -1
    for needle in (url, url.replace("&", "&amp;")):
        at = text.find(needle)
        if at >= 0:
            break
    if at < 0:
        return None
    start = max(text.rfind("<item", 0, at), text.rfind("<entry", 0, at))
    ends = [i for i in (text.find("</item>", at), text.find("</entry>", at)) if i >= 0]
    if start < 0 or not ends:
        return None
    block = text[start:min(ends)]
    parts = []
    title = re.search(r"<title\b[^>]*>(.*?)</title>", block, re.S)
    if title:
        shown = html_lib.unescape(re.sub(r"^<!\[CDATA\[|\]\]>$", "", title.group(1).strip())).strip()
        if " ".join(shown.split()) != " ".join(str(item.get("title") or "").split()):
            return None  # the link was found inside another item: never quote that item's title
        parts.append(title.group(0))
    for tag in _DATE_TAGS:
        found = re.search(rf"<{tag}\b[^>]*>.*?</{tag}>", block, re.S)
        if found:
            parts.append(found.group(0))
            break
    return " | ".join(parts)[:500] or None


def raw_news_page_quote(page_html: str, date_span: str | None) -> str | None:
    """The page's heading and the tag carrying its publication date, copied verbatim from the HTML."""
    parts = []
    heading = re.search(r"<h1\b[^>]*>.*?</h1>", page_html, re.S) or re.search(r"<title\b[^>]*>.*?</title>", page_html, re.S)
    if heading and len(heading.group(0)) <= 300:
        parts.append(heading.group(0))
    date_tag = None
    if date_span and date_span.startswith("<time"):
        value = re.search(r'datetime="([^"]*)"', date_span)
        if value:
            date_tag = re.search(r"<time\b[^>]*" + re.escape(value.group(1)) + r"[^>]*>", page_html)
    elif date_span and "article:published_time" in date_span:
        date_tag = re.search(r"<meta\b[^>]*article:published_time[^>]*>", page_html)
    elif date_span == "JSON-LD datePublished":
        date_tag = re.search(r'"datePublished"\s*:\s*"[^"]*"', page_html)
    elif date_span and date_span.startswith("meta[") and date_span.endswith("]"):
        date_tag = re.search(r"<meta\b[^>]*[\"']" + re.escape(date_span[5:-1]) + r"[\"'][^>]*>", page_html, re.I)
    elif date_span == "labelled publication date":
        date_tag = labelled_date_match(page_html)
    if date_tag:
        parts.append(date_tag.group(0))
    return " | ".join(parts)[:500] or None


def feed_item_quote(item: dict) -> str:
    return " — ".join(str(x).strip() for x in (item.get("title"), item.get("published_raw")) if x)


def parse_feed(text: str) -> list[dict]:
    """Parse an RSS 2.0 or Atom feed into [{title, url, published, published_raw}]: `published` as ISO date
    or None, `published_raw` the date exactly as the feed writes it (quoted as evidence)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    items: list[dict] = []
    for elem in root.iter():
        local = _local(elem.tag)
        if local == "item":  # RSS
            title = _child_text(elem, "title")
            link = _child_text(elem, "link")
            pub = _child_text(elem, "pubdate", "pubDate", "date")
            items.append({"title": title, "url": link, "published": normalize_iso_date(pub), "published_raw": pub})
        elif local == "entry":  # Atom
            title = _child_text(elem, "title")
            link = _atom_link(elem)
            # Atom <updated> is when an entry was last edited, not when it was published: never used as a date.
            pub = _child_text(elem, "published")
            items.append({"title": title, "url": link, "published": normalize_iso_date(pub), "published_raw": pub})
    return items


_ARTICLE_TYPES = {"article", "newsarticle", "blogposting", "report", "pressrelease", "analysisnewsarticle"}
_NOT_ARTICLE_OG = {"product", "restaurant.menu", "restaurant.restaurant", "place", "business.business", "profile"}
_DATE_HINT = re.compile(r"publi|posted|post-date|entry-date|article-date|news-date|dato", re.I)
# "Last modified" markers: a date marked only like this is when a page was edited, not when it was published.
# WordPress marks the one date of an unedited post "entry-date published updated": a publish marker wins.
_MODIFIED_HINT = re.compile(r"modif|updated|oppdatert|endret|revid", re.I)
_PUBLISH_HINT = re.compile(r"publi|posted|post-date|entry-date", re.I)
# <meta name=...> spellings CMSs use for an article's publication date (haugesund-sparebank.no: name="publisheddate").
_PUBLISH_META_NAMES = {"publisheddate", "publishdate", "publish_date", "publish-date", "pubdate", "article.published",
                       "dc.date.issued", "dcterms.issued", "parsely-pub-date", "sailthru.date", "og:published_time"}
_MONTHS_EN = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                           "september", "october", "november", "december"), 1)}
# A date the page labels as its publication date in visible text: "Publisert 11.04.2025" (luftambulanse.no),
# "Opprettet 16. januar 2018", "Published: March 3, 2026". Tags and &nbsp; may sit between label and date.
_GAP = r"(?:\s|&nbsp;|&#160;|:|<[^>]{0,200}>){0,12}"
_DATE_TEXT = (r"(\d{1,2}\.\s?\d{1,2}\.\s?\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}\.?\s(?:" + "|".join(_NORWEGIAN_MONTHS)
              + r"|" + "|".join(_MONTHS_EN) + r")\s\d{4}|(?:" + "|".join(_MONTHS_EN) + r")\s\d{1,2},?\s\d{4})")
_LABELLED_DATE_RE = re.compile(r"\b(publisert|publisering|published|posted|opprettet|skrevet|postet)\b(?:\s+den)?" + _GAP + _DATE_TEXT, re.I)


def labelled_date_match(page_html: str):
    """The first "Publisert <date>"-style label in the raw HTML from the article heading on (menus and the
    site header come before it), searched in the raw text so the quote is the page's own bytes."""
    start = next((i for i in (page_html.find("<h1"), page_html.find("<main"), page_html.find("<article")) if i >= 0), 0)
    return _LABELLED_DATE_RE.search(page_html, start, start + 60_000)


def _parse_text_date(raw: str) -> str | None:
    raw = " ".join(html_lib.unescape(raw).split()).rstrip(".,")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return normalize_iso_date(raw)
    iso = parse_norwegian_date(raw)
    if iso:
        return iso
    m = re.match(r"(\d{1,2})\.?\s([a-z]+)\s(\d{4})$", raw, re.I) or None
    if m and m.group(2).lower() in _MONTHS_EN:
        return f"{int(m.group(3)):04d}-{_MONTHS_EN[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.match(r"([a-z]+)\s(\d{1,2}),?\s(\d{4})$", raw, re.I)
    if m and m.group(1).lower() in _MONTHS_EN:
        return f"{int(m.group(3)):04d}-{_MONTHS_EN[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    return None


def _jsonld_article_date(soup: BeautifulSoup) -> str | None:
    """datePublished of a JSON-LD node typed as an article (Article, NewsArticle, BlogPosting...)."""
    def walk(node):
        if isinstance(node, list):
            for item in node:
                yield from walk(item)
        elif isinstance(node, dict):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if node.get("datePublished") and any(str(t).lower() in _ARTICLE_TYPES for t in types if t):
                yield str(node["datePublished"])
            for key in ("@graph", "mainEntity", "mainEntityOfPage"):
                if key in node:
                    yield from walk(node[key])

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except Exception:
            continue
        for raw in walk(data):
            if normalize_iso_date(raw):
                return raw
    return None


def extract_date_from_html(html: str) -> tuple[str | None, str | None]:
    """(iso_date, span) of the page's own publication date, or (None, None).

    Only a date the page states as an article's publication date counts: meta article:published_time,
    JSON-LD datePublished on an article-typed node, or a <time datetime> inside <article> or marked as the
    publish/post date. A page typed as a product, menu, place or profile is never a dated news item,
    and a date merely written somewhere in the text (opening hours, "updated", a price list) is not used."""
    soup = BeautifulSoup(html, "lxml")
    og_type = soup.find("meta", attrs={"property": "og:type"})
    og = str(og_type.get("content") or "").strip().lower() if og_type else ""
    if og in _NOT_ARTICLE_OG or og.startswith("product"):
        return None, None

    meta = soup.find("meta", attrs={"property": "article:published_time"})
    if meta and meta.get("content"):
        iso = normalize_iso_date(meta["content"])
        if iso:
            return iso, "meta[property=article:published_time]"

    for meta_tag in soup.find_all("meta"):
        key = str(meta_tag.get("name") or meta_tag.get("property") or meta_tag.get("itemprop") or "").strip().lower()
        if key in _PUBLISH_META_NAMES and meta_tag.get("content"):
            iso = normalize_iso_date(str(meta_tag["content"]).strip())
            if iso:
                return iso, f"meta[{key}]"

    raw = _jsonld_article_date(soup)
    if raw:
        return normalize_iso_date(raw), "JSON-LD datePublished"

    for time_el in soup.find_all("time", attrs={"datetime": True}):
        near = [time_el, *itertools.islice(time_el.parents, 2)]
        hints = " ".join(" ".join(el.get("class") or []) + " " + str(el.get("itemprop") or "") for el in near if hasattr(el, "get"))
        if _MODIFIED_HINT.search(hints) and not _PUBLISH_HINT.search(hints):
            continue  # an edit date, even inside <article>
        marked = time_el.find_parent("article") is not None or str(time_el.get("itemprop") or "") in ("datePublished", "dateCreated") \
            or _DATE_HINT.search(hints)
        if not marked:
            continue
        iso = normalize_iso_date(time_el["datetime"])
        if iso:
            return iso, f'<time datetime="{time_el["datetime"]}">'

    labelled = labelled_date_match(html)
    if labelled:
        iso = _parse_text_date(labelled.group(2))
        if iso:
            return iso, "labelled publication date"

    if og == "article":
        text = soup.get_text(" ", strip=True)[:4000]
        iso = parse_norwegian_date(text)
        if iso:
            return iso, "regex Norwegian date pattern"
    return None, None


def _extract_title(html: str, fallback: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return fallback


def parse_wp_rest_posts(text: str) -> list[dict]:
    """Parse a WordPress REST API `/wp-json/wp/v2/posts` JSON response into [{title, url, published}].

    Public, keyless, no auth -- every stock WordPress site exposes this unless explicitly disabled.
    Structured JSON (unlike HTML scraping): `title.rendered`, `link`, `date` (site-local, no timezone
    suffix -- treated as a plain date, matching this module's ISO-date-only granularity elsewhere).
    """
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    items: list[dict] = []
    for post in data:
        if not isinstance(post, dict):
            continue
        title_obj = post.get("title")
        title = title_obj.get("rendered") if isinstance(title_obj, dict) else None
        url = post.get("link")
        date_raw = post.get("date") or post.get("date_gmt")
        published = normalize_iso_date(date_raw) if date_raw else None
        if title and url and published:
            items.append({"title": BeautifulSoup(str(title), "lxml").get_text(strip=True), "url": url, "published": published})
    return items


def _homepage_url(ctx) -> str | None:
    verified = (ctx.shared or {}).get("verified_site") or {}
    url = verified.get("url")
    if url:
        return url
    domain = verified.get("domain")
    return f"https://{domain}/" if domain else None


_SITEMAP_NEWS = re.compile(r"post|news|nyhet|artik|article|blog|aktuelt|presse", re.I)
_SITEMAP_NOT_NEWS = re.compile(r"page|side|product|produkt|categor|kategori|tag|author|forfatter|attachment|media|image", re.I)
_LOC_RE = re.compile(r"<url>(.*?)</url>", re.S)


def _sitemap_article_links(ctx, homepage: str, max_requests: int) -> tuple[list[str], int]:
    """(newest news/blog URLs from the site's sitemap, requests used). The sitemap comes from robots.txt's
    Sitemap: lines, else /sitemap.xml; a sitemap index is followed into its post/news child. A URL counts
    when it sits in a post/news sitemap or its path has a news word; newest by <lastmod> first. The
    lastmod is only used to order: each article's date is read from the article itself."""
    used = 0
    host = urllib.parse.urlsplit(homepage).netloc.lower().removeprefix("www.")
    lister = getattr(ctx.client, "robots_sitemaps", None)
    maps = [u for u in (lister(homepage, org=ctx.org) if lister else []) if urllib.parse.urlsplit(u).netloc.lower().removeprefix("www.") == host]
    url = maps[0] if maps else urllib.parse.urljoin(homepage, "/sitemap.xml")
    in_news_map = False
    for _ in range(2):
        if used >= max_requests:
            return [], used
        resp = ctx.client.get(url, org=ctx.org, purpose="site_sitemap", accept="application/xml, text/xml", respect_robots=True)
        used += 1
        if not resp.ok:
            return [], used
        text = resp.text()
        if "<sitemapindex" in text[:2000]:
            children = [c for c in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text) if _SITEMAP_NEWS.search(c.rsplit("/", 1)[-1])
                        and not _SITEMAP_NOT_NEWS.search(c.rsplit("/", 1)[-1])]
            if not children:
                return [], used
            url, in_news_map = html_lib.unescape(children[0]), True
            continue
        rows = []
        for block in _LOC_RE.findall(text):
            loc = re.search(r"<loc>\s*([^<\s]+)\s*</loc>", block)
            if not loc:
                continue
            link = html_lib.unescape(loc.group(1))
            path = urllib.parse.urlsplit(link).path
            if urllib.parse.urlsplit(link).netloc.lower().removeprefix("www.") != host or _NOT_ARTICLE_PATH.search(path) \
                    or _PRODUCT_PATH.search(path) or path.strip("/").count("/") < (0 if in_news_map else 1):
                continue
            if not in_news_map and not any(term in path.casefold() for term in _NEWS_LINK_TERMS):
                continue
            lastmod = re.search(r"<lastmod>\s*([^<\s]+)", block)
            rows.append((lastmod.group(1) if lastmod else "", link))
        rows.sort(reverse=True)
        return [link for _, link in rows[:ARTICLE_FOLLOW_MAX]], used
    return [], used


def _homepage_links(ctx) -> list[str]:
    """Links on the homepage W3 fetched: the site's menus, shown on every page."""
    pages = (ctx.shared or {}).get("site_pages") or []
    home = _homepage_url(ctx)
    for page in pages:
        if page.get("final_url") == home or page.get("url") == home:
            return list(page.get("links") or [])
    return list(pages[0].get("links") or []) if pages else []


def _discover_link_candidates(ctx, already_seen: set[str]) -> list[str]:
    """Same-domain links already visible on pages W3 fetched (`ctx.shared["site_pages"][*]["links"]`)
    whose path or anchor text matches a Norwegian news/blog term ranked below the identity-verification
    pages in W3's own crawl priority (see module docstring) -- zero extra requests to find, since these
    pages were already fetched for identity verification; only fetching the matched page itself costs
    a request. Free of any HTTP call, so always safe to run before spending the extra-discovery budget.
    """
    site_pages = (ctx.shared or {}).get("site_pages") or []
    found: list[str] = []
    for page in site_pages:
        for link in page.get("links") or []:
            if link in already_seen or link in found:
                continue
            path = urllib.parse.urlparse(link).path.casefold()
            if any(term in path for term in _NEWS_LINK_TERMS):
                found.append(link)
    return found


_COMMENT_TITLE = re.compile(r"^\s*(comment on|comments on|kommentar til|kommentarer til|kommentar på)\b", re.I)
_PLACEHOLDER_KEYS = {"hello world", "hei verden", "hallo verden", "sample page", "eksempelside"}
# Page-builder drafts and test posts ("Elementor #2569", "Test", "Untitled"): never published news.
_DRAFT_TITLE = re.compile(
    r"^\s*(elementor\s*#?\s*\d+|test(ing)?( ?\d+)?|untitled|uten tittel|draft|kladd|ny side|new page|#\d+)\s*$", re.I)


# A web shop's product feed (WooCommerce /product/feed, ?post_type=product) or a product URL is a catalogue
# entry, not news (gullsmedcamilla.no/produkt/...).
_PRODUCT_PATH = re.compile(r"/(produkt|produkter|product|products|shop|butikk|nettbutikk|vare|varer)/", re.I)
# Titles that only appear on a hacked or spam-filled site (replica-bag and AliExpress posts on a clinic's
# blog). One strong title condemns the feed; words a real company can use ("Casino" as a venue, a replica
# boat) only count when two different items carry them.
_SPAM_STRONG = re.compile(
    r"\b(birkin|aliexpress|louis vuitton|viagra|cialis|payday loans?|essay writing|"
    r"replica (bags?|handbags?|watch(es)?|rolex)|cheap (jerseys|handbags))\b", re.I)
_SPAM_WEAK = re.compile(r"\b(replicas?|counterfeit|knockoffs?|casino|handbags?)\b", re.I)


def feed_is_compromised(items: list[dict]) -> bool:
    """A feed with spam posts is a hacked site: none of its items can be trusted as the company's news."""
    titles = [str(i.get("title") or "") for i in items]
    return any(_SPAM_STRONG.search(t) for t in titles) or sum(1 for t in titles if _SPAM_WEAK.search(t)) >= 2


def is_company_item(item: dict, feed_url: str = "") -> bool:
    """False for items that are not the company's own publication: blog comments (a comments feed,
    '#comment-' links, 'Comment on ...' titles), CMS placeholder posts ('Hello world!'), and shop products."""
    url = str(item.get("url") or "")
    title = str(item.get("title") or "").strip()
    if "/comments/feed" in feed_url or "/comments/" in url or "#comment" in url or "replytocom=" in url:
        return False
    if "post_type=product" in feed_url or _PRODUCT_PATH.search(urllib.parse.urlsplit(feed_url).path + "/") \
            or _PRODUCT_PATH.search(urllib.parse.urlsplit(url).path):
        return False
    if _COMMENT_TITLE.search(title) or _DRAFT_TITLE.match(title) \
            or re.sub(r"[^\w ]", "", title.lower()).strip() in _PLACEHOLDER_KEYS:
        return False
    return True


def _process_feed_url(ctx, feed_url: str, seen_urls: set[str], result: dict, *, guessed: bool = False) -> list[tuple[dict, object]]:
    resp = ctx.client.get(
        feed_url, org=ctx.org, purpose="site_feed",
        accept="application/rss+xml, application/atom+xml, application/xml, text/xml",
        respect_robots=True,
    )
    result["checked"] = True
    if not resp.ok:
        # A guessed URL (WordPress /feed/) that does not exist is an answer, not an error; recording it
        # would mark the whole company "partial".
        if not (guessed and 400 <= (resp.status or 0) < 500):
            result["errors"].append({"stage": "feed", "url": feed_url, "error": resp.error or f"http_{resp.status}"})
        return []
    parsed = parse_feed(resp.text())
    ev = make_evidence(
        source_url=feed_url, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
        http_status=resp.status, source_class="company_owned", retrieved_at=resp.retrieved_at,
        content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
        extraction_method="rss_v1", span=f"feed item count={len(parsed)}", access_policy="robots-allowed",
    )
    out = []
    raw_text = resp.text()
    if feed_is_compromised(parsed):
        return []  # read fine, but a hacked site: nothing in it is the company's news (not a fetch error)
    for item in parsed:
        url = item.get("url")
        if not url or not item.get("published") or url in seen_urls or not is_company_item(item, feed_url):
            continue
        seen_urls.add(url)
        quote = raw_feed_item_quote(raw_text, item)
        # Each item gets its own evidence record, quoting its own XML, so every dated item is checkable alone.
        item_ev = ev.model_copy(update={
            "evidence_id": evidence_id(feed_url, f"feed item {url}"), "span": f"feed item {url}"[:500], "claim_span": quote,
        }) if quote else ev
        out.append((item, item_ev))
    return out


def _process_wp_rest_url(ctx, rest_url: str, seen_urls: set[str], result: dict) -> list[tuple[dict, object]]:
    resp = ctx.client.get(rest_url, org=ctx.org, purpose="site_feed", accept="application/json", respect_robots=True)
    result["checked"] = True
    if not resp.ok:
        # REST switched off or behind a login (401/403/404) is an answer, not an error.
        if not (400 <= (resp.status or 0) < 500):
            result["errors"].append({"stage": "wp_rest", "url": rest_url, "error": resp.error or f"http_{resp.status}"})
        return []
    raw_text = resp.text()
    parsed = parse_wp_rest_posts(raw_text)
    if feed_is_compromised(parsed):
        return []  # the same hacked WordPress as its RSS feed: nothing in it is the company's news
    ev = make_evidence(
        source_url=rest_url, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
        http_status=resp.status, source_class="company_owned", retrieved_at=resp.retrieved_at,
        content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
        extraction_method="wp_rest_v1", span=f"wp-json post count={len(parsed)}", access_policy="robots-allowed",
    )
    out = []
    for item in parsed:
        url = item.get("url")
        if not url or url in seen_urls or not is_company_item(item, rest_url):
            continue
        seen_urls.add(url)
        quote = raw_wp_post_quote(raw_text, url)
        item_ev = ev.model_copy(update={
            "evidence_id": evidence_id(rest_url, f"wp post {url}"), "span": f"wp post {url}"[:500], "claim_span": quote,
        }) if quote else ev
        out.append((item, item_ev))
    return out


def raw_wp_post_quote(text: str, url: str) -> str | None:
    """The post's own JSON object ({"date":..,"link":..,"title":..}) copied verbatim from the REST response."""
    if not text or not url:
        return None
    at = -1
    for needle in (json.dumps(url)[1:-1].replace("/", "\\/"), json.dumps(url)[1:-1]):
        at = text.find('"' + needle + '"')
        if at >= 0:
            break
    if at < 0:
        return None
    start = text.rfind("{", 0, text.rfind('"date"', 0, at) + 1)
    end = text.find("}}", at)
    if start < 0 or end < 0 or end - start > 600:
        return None
    return text[start:end + 2][:500]


# Paths that are sections, filters or assets, never one article.
_NOT_ARTICLE_PATH = re.compile(
    r"/(page|side|kategori|category|categories|tag|tags|stikkord|author|forfatter|feed|wp-content|wp-json|"
    r"arkiv|archive|search|sok|søk|nyhetsbrev|newsletter|abonner|subscribe|login|logg-inn)(/|$)|\.(pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?)$", re.I)


def article_links_from_listing(listing_url: str, listing_html: str, nav_links: set[str]) -> list[str]:
    """Links on a news listing page that look like single articles: same host, a path below or beside the
    listing, a real headline as anchor text, and not a menu link the homepage also shows. In page order."""
    base = urllib.parse.urlsplit(listing_url)
    host = base.netloc.lower().removeprefix("www.")
    soup = BeautifulSoup(listing_html, "lxml")
    root = soup.find("main") or soup.body or soup
    for tag in root.find_all(["nav", "header", "footer", "aside"]):
        tag.decompose()
    found: list[str] = []
    for a in root.find_all("a", href=True):
        href = urllib.parse.urljoin(listing_url, str(a["href"]).strip()).split("#", 1)[0]
        parts = urllib.parse.urlsplit(href)
        if parts.scheme not in ("http", "https") or parts.netloc.lower().removeprefix("www.") != host or parts.query:
            continue
        path = parts.path or "/"
        if path.rstrip("/") in ("", base.path.rstrip("/")) or _NOT_ARTICLE_PATH.search(path) or _PRODUCT_PATH.search(path):
            continue
        if href in nav_links or href.rstrip("/") in {n.rstrip("/") for n in nav_links}:
            continue
        text = " ".join(a.get_text(" ", strip=True).split())
        slug = path.rstrip("/").rsplit("/", 1)[-1]
        if len(text) < 12 and slug.count("-") < 2:
            continue  # "Les mer" on its own still counts when the URL is a headline slug
        if href not in found:
            found.append(href)
    return found


def _process_news_url(ctx, news_url: str, seen_urls: set[str], result: dict, *, purpose: str = "site_news_page") -> tuple[dict, object] | None:
    resp = ctx.client.get(news_url, org=ctx.org, purpose=purpose, accept="text/html", respect_robots=True)
    result["checked"] = True
    if not resp.ok:
        result["errors"].append({"stage": "news_page", "url": news_url, "error": resp.error or f"http_{resp.status}"})
        return None
    html = resp.text()
    result.setdefault("listings", {})[resp.final_url or news_url] = html
    iso, span = extract_date_from_html(html)
    if not iso or news_url in seen_urls:
        return None
    seen_urls.add(news_url)
    ev = make_evidence(
        source_url=news_url, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
        http_status=resp.status, source_class="company_owned", retrieved_at=resp.retrieved_at,
        content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
        extraction_method="html_news_v1", span=span or "", access_policy="robots-allowed",
        quote=raw_news_page_quote(html, span),
    )
    title = _extract_title(html, fallback=news_url)
    if _SECTION_TITLE.match(title.strip()):
        return None  # a news/blog listing page ("Nyheter", "Nyheter fra X"), not one dated article
    return ({"title": title, "url": news_url, "published": iso}, ev)


def collect(ctx) -> dict:
    """Fetch shared feed_urls + news_urls (free), then, only if that found nothing, spend up to
    ARTICLE_DISCOVERY_BUDGET extra requests on WordPress post list/feed discovery and same-domain news/blog
    links already visible on pages W3 fetched. Extract dated items, return the <=10 most recent as
    claims."""
    result: dict = {"claims": [], "evidence": [], "checked": False, "errors": []}
    shared = ctx.shared or {}
    feed_urls = list(shared.get("feed_urls") or [])
    news_urls = list(shared.get("news_urls") or [])
    if ctx.client is None:
        return result
    if not feed_urls and not news_urls and not shared.get("verified_site") and not shared.get("site_pages"):
        return result

    seen_urls: set[str] = set()
    candidates: list[tuple[dict, object]] = []  # (item, evidence)

    for feed_url in feed_urls:
        candidates.extend(_process_feed_url(ctx, feed_url, seen_urls, result))

    for news_url in news_urls:
        item = _process_news_url(ctx, news_url, seen_urls, result)
        if item:
            candidates.append(item)

    # Extra discovery, only when the free sources above produced nothing, capped at ARTICLE_DISCOVERY_BUDGET
    # requests. Most dated items per request first.
    extra = 0
    if not candidates:
        homepage = _homepage_url(ctx)
        # 1. WordPress post list: one request, up to 10 dated posts with their own links.
        if shared.get("wordpress") and homepage:
            candidates.extend(_process_wp_rest_url(ctx, f"{homepage.rstrip('/')}/{WP_POSTS_QUERY}", seen_urls, result))
            extra += 1
        listings = {**(shared.get("news_listing_html") or {}), **(result.get("listings") or {})}
        # 2. A news/blog link seen on a fetched page but never fetched (crowded out by kontakt/om-oss).
        if not candidates and not listings:
            for link in _discover_link_candidates(ctx, seen_urls | set(news_urls))[:1]:
                item = _process_news_url(ctx, link, seen_urls, result, purpose="site_news_page_discovered")
                extra += 1
                if item:
                    candidates.append(item)
            listings.update(result.get("listings") or {})
        # 3. Single articles linked from a news listing page.
        if not candidates and listings:
            nav = set(_homepage_links(ctx))
            followed = 0
            for listing_url, listing_html in listings.items():
                for link in article_links_from_listing(listing_url, listing_html or "", nav):
                    if followed >= ARTICLE_FOLLOW_MAX or extra >= ARTICLE_DISCOVERY_BUDGET:
                        break
                    if link in seen_urls:
                        continue
                    item = _process_news_url(ctx, link, seen_urls, result, purpose="site_news_article")
                    extra += 1
                    followed += 1
                    if item:
                        candidates.append(item)
        # 4. WordPress /feed/ (also works where the REST list is switched off). Only on WordPress: on other
        # sites the guess 404s (7 of 7 non-WordPress sites checked on ref100).
        if not candidates and extra < ARTICLE_DISCOVERY_BUDGET and homepage and shared.get("wordpress"):
            wp_feed_url = f"{homepage.rstrip('/')}/feed/"
            if wp_feed_url not in feed_urls:
                candidates.extend(_process_feed_url(ctx, wp_feed_url, seen_urls, result, guessed=True))
                extra += 1
        # 5. The sitemap's newest news/blog URLs, each read for its own publication date.
        if not candidates and extra < ARTICLE_DISCOVERY_BUDGET - 1 and homepage:
            links, used = _sitemap_article_links(ctx, homepage, ARTICLE_DISCOVERY_BUDGET - extra - 1)
            extra += used
            for link in links:
                if extra >= ARTICLE_DISCOVERY_BUDGET:
                    break
                if link in seen_urls:
                    continue
                item = _process_news_url(ctx, link, seen_urls, result, purpose="site_news_article")
                extra += 1
                if item:
                    candidates.append(item)

    candidates.sort(key=lambda pair: pair[0]["published"], reverse=True)
    claims = []
    evidence = []
    quotes: dict[str, list[str]] = {}
    for item, ev in candidates[:MAX_ACTIVITY_ITEMS]:
        if item.get("published_raw") and str(ev.span or "").startswith("feed item count"):
            # Quote the feed as written ("Wed, 04 Feb 2026 10:00:00 +0000"), not the normalised ISO date.
            quotes.setdefault(ev.evidence_id, []).append(feed_item_quote(item))
            ev.claim_span = " | ".join(quotes[ev.evidence_id])[:500]
        claim = make_claim(
            org=ctx.org, family="activity", field="activity_item",
            value={"title": item["title"], "url": item["url"], "published": item["published"], "source": "company website"},
            value_key=item["url"], availability="available", identity_basis="linked_from_verified_site",
            effective_date=item["published"], evidence_ids=[ev.evidence_id],
        )
        claims.append(claim)
        evidence.append(ev)
    result.pop("listings", None)
    result["claims"] = claims
    result["evidence"] = evidence
    return result
