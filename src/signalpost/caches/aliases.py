"""Subunit ("underenhet") cache, built from the Brønnøysund bulk subunits CSV.

Indexed by parent org number (`overordnetEnhet`). Gives consumers each subunit's address, contact info,
and — most usefully for identity/branding — its trade name, which is often a real public-facing brand
distinct from the legal entity name (e.g. "HAGELAND HOKKSUND" under "EIKER HAGESENTER AS").
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from . import store
from .email_domains import iter_bulk_rows  # gzip-aware CSV row iterator, reused as-is

# Legal-form / branch suffixes that show up appended to subunit names and carry no brand information.
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(AS|ASA|ANS|DA|BA|SA|NUF|ENK|AVD\.?|AVDELING)\b\.?",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


def _strip_legal_suffixes(name: str) -> str:
    cleaned = _LEGAL_SUFFIX_RE.sub(" ", name)
    cleaned = _WS_RE.sub(" ", cleaned).strip(" -,")
    return cleaned


def clean_subunit_name(subunit_name: str, parent_name: str | None) -> str | None:
    """Return a cleaned trade-name alias for a subunit, or None when it adds nothing beyond the parent
    legal name (e.g. "DIPS AS AVD BERGEN" under "DIPS AS" -> None; a distinct brand like
    "HAGELAND HOKKSUND" is kept).
    """
    if not subunit_name:
        return None
    cleaned = _strip_legal_suffixes(subunit_name)
    if not cleaned:
        return None
    parent_clean = _strip_legal_suffixes(parent_name) if parent_name else ""
    cleaned_cf = cleaned.casefold()
    parent_cf = parent_clean.casefold()
    if parent_cf and (cleaned_cf == parent_cf or cleaned_cf.startswith(parent_cf + " ") or
                       parent_cf.startswith(cleaned_cf + " ")):
        # The subunit name is just "<parent> <city/dept>" or a prefix/superset of the parent name:
        # no new brand information (covers "DIPS AS AVD BERGEN" under "DIPS AS").
        # A short remainder (a place/department qualifier) isn't a brand; a long distinct remainder
        # can still be a real alias, but conservatively drop anything that's a strict prefix match.
        if cleaned_cf.startswith(parent_cf):
            return None
    return cleaned


def build(bulk_underenheter_path: str | Path, cache_dir: str | Path,
          parent_names: dict[str, str] | None = None) -> dict:
    """Build `aliases.sqlite` from the (large, temp-downloaded) subunits bulk CSV.

    `parent_names` optionally maps parent org number -> legal name (from the enheter bulk), used only
    to clean subunit names at build time; when absent, cleaning happens without parent-name dedup at
    build time and `.names()` still strips legal suffixes.
    """
    cache_dir = Path(cache_dir)
    db_path = cache_dir / "aliases.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE subunits ("
        " parent_org TEXT NOT NULL,"
        " organisation_number TEXT NOT NULL,"
        " name TEXT,"
        " street TEXT,"
        " postcode TEXT,"
        " city TEXT,"
        " website TEXT,"
        " email TEXT,"
        " employees INTEGER,"
        " nace TEXT"
        ")"
    )

    parent_names = parent_names or {}
    rows_buffer = []
    row_count = 0
    for row in iter_bulk_rows(bulk_underenheter_path):
        row_count += 1
        parent = (row.get("overordnetEnhet") or "").strip()
        if not parent:
            continue
        employees = row.get("antallAnsatte") or None
        try:
            employees_val = int(employees) if employees not in (None, "") else None
        except ValueError:
            employees_val = None
        rows_buffer.append((
            parent,
            (row.get("organisasjonsnummer") or "").strip(),
            row.get("navn") or None,
            row.get("beliggenhetsadresse.adresse") or None,
            row.get("beliggenhetsadresse.postnummer") or None,
            row.get("beliggenhetsadresse.poststed") or None,
            row.get("hjemmeside") or None,
            row.get("epostadresse") or None,
            employees_val,
            row.get("naeringskode1.kode") or None,
        ))
        if len(rows_buffer) >= 50_000:
            conn.executemany(
                "INSERT INTO subunits VALUES (?,?,?,?,?,?,?,?,?,?)", rows_buffer)
            rows_buffer.clear()
    if rows_buffer:
        conn.executemany("INSERT INTO subunits VALUES (?,?,?,?,?,?,?,?,?,?)", rows_buffer)

    conn.execute("CREATE INDEX idx_subunits_parent ON subunits(parent_org)")
    conn.commit()
    conn.close()

    return {
        "source_urls": ["https://data.brreg.no/enhetsregisteret/api/underenheter/lastned/csv"],
        "input_path": str(bulk_underenheter_path),
        "input_sha256": store.sha256_file(bulk_underenheter_path),
        "row_count": row_count,
        "built_at": store.utc_now(),
        "license": "NLOD 2.0",
    }


class Aliases:
    """Loaded view over `aliases.sqlite`."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def subunits(self, org: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT organisation_number, name, street, postcode, city, website, email, employees, nace "
            "FROM subunits WHERE parent_org = ?",
            (org,),
        ).fetchall()
        return [dict(r) for r in rows]

    def names(self, org: str, parent_name: str | None = None) -> list[str]:
        """Distinct subunit trade names, cleaned of legal suffixes and parent-name duplication."""
        subs = self.subunits(org)
        out: list[str] = []
        seen: set[str] = set()
        for s in subs:
            cleaned = clean_subunit_name(s.get("name") or "", parent_name)
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(cleaned)
        return out

    @classmethod
    def load(cls, cache_dir: str | Path) -> "Aliases | None":
        conn = store.connect_ro_fast(Path(cache_dir) / "aliases.sqlite")
        if conn is None:
            return None
        return cls(conn)
