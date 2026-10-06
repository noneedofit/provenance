"""`from signalpost.caches import Caches` — the W2 caches API consumed by W1/W3/W4.

Caches are built outside the timed daily run (`python -m signalpost.caches.prepare --cache-dir cache/
--bulk <brreg-enheter.csv>`), published as a versioned tarball, and loaded here. Every part is
`None`-safe: a missing or corrupt sqlite file makes that attribute `None` instead of raising, so a run
started with `--caches` pointing at a partial or absent cache directory still completes (consumers must
handle `caches is None` and `caches.<part> is None`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import store
from .aliases import Aliases
from .email_domains import EmailDomains
from .nav import Nav
from .places import Places
from .wikidata import Wikidata

__all__ = ["Caches", "EmailDomains", "Aliases", "Wikidata", "Nav", "Places"]


@dataclass
class Caches:
    email_domains: EmailDomains | None
    aliases: Aliases | None
    wikidata: Wikidata | None
    nav: Nav | None
    meta: dict[str, Any]
    places: Places | None = None
    cache_dir: str | None = None

    @classmethod
    def load(cls, cache_dir: str | Path) -> "Caches":
        """Load every part found under `cache_dir`. Fast (<5s): each part is a single sqlite file
        opened read-only (or read-write only for `nav`, which the daily run may update via
        `sync_incremental`) with no bulk data read at load time.
        """
        cache_dir = Path(cache_dir)
        email_domains = EmailDomains.load(cache_dir)
        aliases = Aliases.load(cache_dir)
        wikidata = Wikidata.load(cache_dir)
        nav = Nav.load(cache_dir)
        if nav is not None:
            nav.aliases = aliases  # lets Nav.ads_for resolve parent -> subunit org numbers
        meta = store.read_meta(cache_dir) or {}
        places = Places.load(cache_dir)
        return cls(email_domains=email_domains, aliases=aliases, wikidata=wikidata, nav=nav,
                    meta=meta, cache_dir=str(cache_dir), places=places)
