"""YouTube channel activity for channels linked from the verified company site.

Only company-owned YouTube channels count (URL found in `ctx.shared["social_links"]`, which W3 only
populates for links found on an exact-verified site). We resolve a channel id (handles/`/c/`/`/user/`
URLs need one extra page fetch) then read the keyless `feeds/videos.xml` Atom feed - no API key, no
scraping of video pages. LinkedIn/Facebook/Instagram/X/TikTok are intentionally out of scope here (ToS);
those stay URL-only profiles owned by W3.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ._common import make_claim, make_evidence
from .feeds import feed_item_quote, parse_feed

MAX_CHANNELS = 2
MAX_VIDEOS_PER_CHANNEL = 5

CHANNEL_ID_URL_RE = re.compile(r"youtube\.com/channel/(UC[0-9A-Za-z_-]{10,})", re.I)


def _find_youtube_links(ctx) -> list[str]:
    shared = ctx.shared or {}
    out = []
    for item in shared.get("social_links") or []:
        if not isinstance(item, dict):
            continue
        platform = str(item.get("platform") or "").lower()
        url = item.get("url")
        if platform == "youtube" and url:
            out.append(url)
    return out


def resolve_channel_id(ctx, url: str):
    """Returns (channel_id | None, response | None). response is None when no fetch was needed."""
    m = CHANNEL_ID_URL_RE.search(url)
    if m:
        return m.group(1), None
    resp = ctx.client.get(url, org=ctx.org, purpose="youtube_channel_resolve", accept="text/html", respect_robots=True)
    if not resp.ok:
        return None, resp
    html = resp.text()
    m2 = re.search(r'"channelId":"(UC[0-9A-Za-z_-]{10,})"', html)
    if m2:
        return m2.group(1), resp
    soup = BeautifulSoup(html, "lxml")
    meta = soup.find("meta", attrs={"itemprop": "identifier"})
    if meta and str(meta.get("content", "")).startswith("UC"):
        return meta["content"], resp
    canonical = soup.find("link", attrs={"rel": "canonical"})
    if canonical and canonical.get("href"):
        m3 = CHANNEL_ID_URL_RE.search(canonical["href"])
        if m3:
            return m3.group(1), resp
    return None, resp


def collect(ctx) -> dict:
    result: dict = {"claims": [], "evidence": [], "checked": False, "errors": []}
    if ctx.client is None:
        return result
    links = _find_youtube_links(ctx)
    if not links:
        return result

    claims = []
    evidence = []
    for url in links[:MAX_CHANNELS]:
        channel_id, resolve_resp = resolve_channel_id(ctx, url)
        if resolve_resp is not None:
            result["checked"] = True
            if not resolve_resp.ok:
                result["errors"].append({"stage": "youtube_resolve", "url": url, "error": resolve_resp.error or f"http_{resolve_resp.status}"})
        if not channel_id:
            continue

        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        resp = ctx.client.get(feed_url, org=ctx.org, purpose="youtube_feed", accept="application/atom+xml, application/xml", respect_robots=True)
        result["checked"] = True
        if not resp.ok:
            # YouTube's robots.txt disallows the channel feed; respecting it is the expected outcome, not
            # a failure of this company's research.
            if resp.error != "robots_disallowed":
                result["errors"].append({"stage": "youtube_feed", "url": feed_url, "error": resp.error or f"http_{resp.status}"})
            continue

        items = [it for it in parse_feed(resp.text()) if it.get("url") and it.get("published")]
        items.sort(key=lambda it: it["published"], reverse=True)
        ev = make_evidence(
            source_url=feed_url, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
            http_status=resp.status, source_class="company_owned_platform", retrieved_at=resp.retrieved_at,
            content_sha256=resp.content_sha256, snapshot_ref=resp.snapshot_ref,
            extraction_method="youtube_rss_v1", span=f"channel_id={channel_id}; videos={len(items)}",
            access_policy="robots-allowed",
            quote=" | ".join(feed_item_quote(it) for it in items[:MAX_VIDEOS_PER_CHANNEL]) or None,
        )
        for it in items[:MAX_VIDEOS_PER_CHANNEL]:
            claims.append(make_claim(
                org=ctx.org, family="activity", field="activity_item",
                value={"title": it["title"], "url": it["url"], "published": it["published"], "platform": "YouTube"},
                value_key=it["url"], availability="available", identity_basis="linked_from_verified_site",
                effective_date=it["published"], evidence_ids=[ev.evidence_id],
            ))
        if items:
            evidence.append(ev)

    result["claims"] = claims
    result["evidence"] = evidence
    return result
