"""URL safety and social-profile URL normalisation.

Adapted from the Builderr Signalpost starter kit (`norway_company_agent.website`), which this agent was
built on: `assert_public_url` is the SSRF guard applied to every outbound request, and
`normalize_social_url` turns a link found on a verified company site into one canonical profile URL
(rejecting share/intent/post links).
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse

SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}

_CANONICAL_HOST = {
    "linkedin": "linkedin.com",
    "facebook": "facebook.com",
    "instagram": "instagram.com",
    "x": "x.com",
    "youtube": "youtube.com",
    "tiktok": "tiktok.com",
}

_REJECTED_FIRST_SEGMENT = {
    "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins"},
    "instagram": {"p", "reel", "reels", "stories", "explore"},
    "x": {"intent", "share", "home", "search", "i"},
}


def assert_public_url(url: str) -> None:
    """Raise ValueError unless `url` is http(s) and its host resolves only to public addresses."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hosts are blocked")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Hostname did not resolve") from exc
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")


def normalize_social_url(url: str) -> dict[str, str] | None:
    """{"platform", "url"} for a company profile link, or None for anything that is not a profile."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]
    if not parts or lowered[0] in _REJECTED_FIRST_SEGMENT.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] == "profile.php":
        return None
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
        return None
    if host == "youtu.be":
        return None
    if platform == "tiktok" and not parts[0].startswith("@"):
        return None
    if platform == "x" and len(parts) != 1:
        return None
    if platform == "linkedin":
        parts = parts[:2]
    elif platform == "youtube":
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    return {"platform": platform, "url": f"https://{_CANONICAL_HOST[platform]}/{'/'.join(parts)}"}
