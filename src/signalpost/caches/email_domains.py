"""Email/website domain sharing cache, built from the Brønnøysund `enheter` bulk CSV.

Counts, per registered-domain, how many organisations use it in `epostadresse` (email) and how many use
it in `hjemmeside` (website), so identity resolution can tell a company-owned domain (used by ~1 org) from
a shared service-provider or franchise-platform domain (accountant, property manager, shopping-centre
site, freemail) used by many.
"""
from __future__ import annotations

import csv
import gzip
import sqlite3
from pathlib import Path
from typing import Iterable, Iterator

import tldextract

from . import store

# Common Norwegian/international freemail and free-webmail domains. A domain here is always "shared"
# regardless of the observed count (some may appear on few orgs in a sample but are still not
# organisation-owned).
FREEMAIL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "online.no", "hotmail.com", "outlook.com", "live.no", "yahoo.no", "c2i.net",
    "lyse.net", "icloud.com", "me.com", "broadpark.no", "frisurf.no", "hotmail.no", "yahoo.com",
    "altibox.no", "getmail.no", "msn.com", "live.com", "mac.com", "yahoo.co.uk",
    "hotmail.co.uk", "start.no", "nextmail.no", "chello.no", "runbox.no", "runbox.com", "protonmail.com",
    "gmx.com", "gmx.net", "tele2.no",
})

_extract = tldextract.TLDExtract(suffix_list_urls=())  # offline: use tldextract's bundled suffix list


def registered_domain(value: str | None) -> str | None:
    """Extract the registered domain (e.g. "example.co.uk") from an email address or a URL/hostname.
    Returns None when there isn't a usable domain.
    """
    if not value:
        return None
    v = value.strip().strip(".").lower()
    if not v:
        return None
    if "@" in v:
        v = v.rsplit("@", 1)[-1]
    else:
        # Strip a scheme and any path/query if this looks like a URL.
        if "//" in v:
            v = v.split("//", 1)[-1]
        v = v.split("/", 1)[0]
    v = v.split(":")[0]  # drop a port
    if not v or " " in v:
        return None
    ext = _extract(v)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}"


def iter_bulk_rows(bulk_path: str | Path) -> Iterator[dict[str, str]]:
    """Yield dict rows from the Brønnøysund `enheter` bulk CSV. The file is gzip-compressed even
    though it is conventionally named `*.csv`; we detect the gzip magic bytes so a plain CSV also works
    (useful for test fixtures).
    """
    bulk_path = Path(bulk_path)
    with open(bulk_path, "rb") as raw:
        magic = raw.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(bulk_path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        yield from reader


def build(bulk_path: str | Path, cache_dir: str | Path) -> dict:
    """Build `email_domains.sqlite` from the bulk CSV. Returns a small report dict for meta.json."""
    cache_dir = Path(cache_dir)
    db_path = cache_dir / "email_domains.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE domains (domain TEXT PRIMARY KEY, email_org_count INTEGER NOT NULL DEFAULT 0, "
                 "website_org_count INTEGER NOT NULL DEFAULT 0)")

    email_counts: dict[str, int] = {}
    website_counts: dict[str, int] = {}
    row_count = 0
    for row in iter_bulk_rows(bulk_path):
        row_count += 1
        ed = registered_domain(row.get("epostadresse"))
        if ed:
            email_counts[ed] = email_counts.get(ed, 0) + 1
        wd = registered_domain(row.get("hjemmeside"))
        if wd:
            website_counts[wd] = website_counts.get(wd, 0) + 1

    all_domains = set(email_counts) | set(website_counts)
    conn.executemany(
        "INSERT INTO domains (domain, email_org_count, website_org_count) VALUES (?, ?, ?)",
        ((d, email_counts.get(d, 0), website_counts.get(d, 0)) for d in all_domains),
    )
    conn.execute("CREATE INDEX idx_domains_email_count ON domains(email_org_count)")
    conn.commit()
    conn.close()

    return {
        "source_urls": ["https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"],
        "input_path": str(bulk_path),
        "input_sha256": store.sha256_file(bulk_path),
        "row_count": row_count,
        "domain_count": len(all_domains),
        "built_at": store.utc_now(),
        "license": "NLOD 2.0",
    }


class EmailDomains:
    """Loaded view over `email_domains.sqlite`."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def org_count(self, domain: str) -> int:
        d = registered_domain(domain) or domain
        row = self._conn.execute("SELECT email_org_count FROM domains WHERE domain = ?", (d,)).fetchone()
        return int(row["email_org_count"]) if row else 0

    def website_org_count(self, domain: str) -> int:
        d = registered_domain(domain) or domain
        row = self._conn.execute("SELECT website_org_count FROM domains WHERE domain = ?", (d,)).fetchone()
        return int(row["website_org_count"]) if row else 0

    def is_freemail(self, domain: str) -> bool:
        d = registered_domain(domain) or domain
        return d in FREEMAIL_DOMAINS

    def is_shared(self, domain: str) -> bool:
        d = registered_domain(domain) or domain
        return self.is_freemail(d) or self.org_count(d) >= 3

    @classmethod
    def load(cls, cache_dir: str | Path) -> "EmailDomains | None":
        conn = store.connect_ro_fast(Path(cache_dir) / "email_domains.sqlite")
        if conn is None:
            return None
        return cls(conn)
