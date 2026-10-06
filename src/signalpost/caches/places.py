"""Open places datasets as website/profile *candidate* sources.

Two bundled, pinned snapshots (see `caches/snapshot/`, rebuilt with `scripts/build_places_snapshot.py`):

- `places_no.jsonl.gz` - Overture Maps Places (release recorded in SNAPSHOT_META), Norwegian places that
  carry a website or social link: `[id, name, postcode, phone keys, e-mail keys, websites, socials]`
  (phones/e-mails only as one-way `contact_key`s).
  CDLA-Permissive-2.0 / Apache-2.0 / CC0 (per Overture's per-source licensing; no attribution required).
- `osm_orgnr_no.jsonl.gz` - OpenStreetMap features tagged `ref:NO:orgnr`:
  `[orgnr, "type/id", name, website, {platform: url}]`. ODbL 1.0, (c) OpenStreetMap contributors.

A match here only nominates a candidate. The website is still fetched and verified live (verify.py);
an Overture match contributes at most one corroborating signal, an OSM org-number tag counts as the same
kind of org-number-keyed source as Wikidata. Pinned snapshots keep two runs of the same input identical.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Any

SNAPSHOT_DIR = Path(__file__).parent / "snapshot"
PLACES_SNAPSHOT = SNAPSHOT_DIR / "places_no.jsonl.gz"
OSM_SNAPSHOT = SNAPSHOT_DIR / "osm_orgnr_no.jsonl.gz"
SNAPSHOT_META = {
    "format": "2",  # 2: phones/e-mails stored as one-way contact keys
    "overture_release": "2026-09-23.1",
    "overture_source": "s3://overturemaps-us-west-2/release/2026-09-23.1/theme=places/type=place/",
    "osm_base": "2026-06-01T08:52:28Z",
}
OVERTURE_SOURCE_URL = "https://docs.overturemaps.org/guides/places/"
DB_NAME = "places.sqlite"

_FOLD = str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"})
_LEGAL = {"as", "asa", "ans", "da", "enk", "sa", "nuf", "iks", "sti", "stiftelsen", "ba", "bl", "sam"}


def fold(text: str) -> str:
    return unicodedata.normalize("NFKD", (text or "").translate(_FOLD)).encode("ascii", "ignore").decode().casefold()


def name_core(name: str) -> str:
    return " ".join(t for t in re.findall(r"[a-z0-9]+", fold(name)) if t not in _LEGAL)


def norm_phone(raw: Any) -> str | None:
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("0047"):
        digits = digits[4:]
    if digits.startswith("47") and len(digits) == 10:
        digits = digits[2:]
    return digits if len(digits) == 8 else None


def contact_key(kind: str, value: str) -> str:
    """One-way key for a phone ("tel", 8 digits) or e-mail ("mail", lower case). The bundled snapshot holds
    only these keys, so it ships no raw contact data; a registry value is matched by computing its key."""
    return hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:20]


def build(cache_dir: str | Path) -> dict:
    """Index both bundled snapshots into `cache_dir/places.sqlite`. A few seconds; no network."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = cache_dir / DB_NAME
    tmp = db_path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(str(tmp))
    conn.executescript(
        """
        CREATE TABLE place (id TEXT PRIMARY KEY, name TEXT, core TEXT, postcode TEXT, websites TEXT, socials TEXT);
        CREATE TABLE place_phone (phone TEXT, id TEXT);
        CREATE TABLE place_email (email TEXT, id TEXT);
        CREATE TABLE osm (orgnr TEXT, osm_id TEXT, name TEXT, website TEXT, socials TEXT);
        """
    )
    places = phones = emails = osm = 0
    with gzip.open(PLACES_SNAPSHOT, "rt", encoding="utf-8") as fh:
        for line in fh:
            pid, name, postcode, phs, ems, webs, socs = json.loads(line)
            conn.execute("INSERT OR IGNORE INTO place VALUES (?,?,?,?,?,?)",
                         (pid, name, name_core(name), postcode, json.dumps(webs), json.dumps(socs)))
            places += 1
            for p in phs:
                conn.execute("INSERT INTO place_phone VALUES (?,?)", (p, pid))
                phones += 1
            for e in ems:
                conn.execute("INSERT INTO place_email VALUES (?,?)", (e, pid))
                emails += 1
    with gzip.open(OSM_SNAPSHOT, "rt", encoding="utf-8") as fh:
        for line in fh:
            orgnr, osm_id, name, website, socials = json.loads(line)
            conn.execute("INSERT INTO osm VALUES (?,?,?,?,?)", (orgnr, osm_id, name, website, json.dumps(socials)))
            osm += 1
    conn.executescript(
        """
        CREATE INDEX ix_phone ON place_phone(phone);
        CREATE INDEX ix_email ON place_email(email);
        CREATE INDEX ix_core ON place(core, postcode);
        CREATE INDEX ix_osm ON osm(orgnr);
        """
    )
    conn.commit()
    conn.close()
    tmp.replace(db_path)
    return {"places": places, "phones": phones, "emails": emails, "osm": osm, **SNAPSHOT_META}


class Places:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()

    @classmethod
    def load(cls, cache_dir: str | Path) -> "Places | None":
        db_path = Path(cache_dir) / DB_NAME
        if not db_path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
            conn.execute("SELECT 1 FROM place LIMIT 1")
            return cls(conn)
        except sqlite3.Error:
            return None

    def _rows(self, sql: str, args: tuple) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def osm_for(self, orgnrs: list[str]) -> list[dict[str, Any]]:
        out = []
        for orgnr in dict.fromkeys(o for o in orgnrs if o):
            for o, osm_id, name, website, socials in self._rows("SELECT * FROM osm WHERE orgnr = ? ORDER BY osm_id", (orgnr,)):
                out.append({"orgnr": o, "osm_id": osm_id, "name": name, "website": website, "socials": json.loads(socials or "{}"),
                            "url": f"https://www.openstreetmap.org/{osm_id}"})
        return out

    def match(self, *, name: str, postcode: str | None, phones: list[str], emails: list[str]) -> list[dict[str, Any]]:
        """Places tied to this company by a registry phone, a registry e-mail, or name core + postcode.
        Each result lists every key that matched (`how`), so callers can weigh a place matched on two keys
        above one matched on a shared switchboard number alone. Deterministic order (place id)."""
        found: dict[str, set[str]] = {}
        for p in dict.fromkeys(x for x in (norm_phone(p) for p in phones) if x):
            for (pid,) in self._rows("SELECT id FROM place_phone WHERE phone = ?", (contact_key("tel", p),)):
                found.setdefault(pid, set()).add("phone")
        for e in dict.fromkeys(x.strip().lower() for x in emails if x and "@" in x):
            for (pid,) in self._rows("SELECT id FROM place_email WHERE email = ?", (contact_key("mail", e),)):
                found.setdefault(pid, set()).add("email")
        core = name_core(name)
        if core and postcode:
            for (pid,) in self._rows("SELECT id FROM place WHERE core = ? AND postcode = ?", (core, postcode)):
                found.setdefault(pid, set()).add("name_postcode")
        out = []
        for pid in sorted(found):
            rows = self._rows("SELECT name, core, postcode, websites, socials FROM place WHERE id = ?", (pid,))
            if not rows:
                continue
            pname, pcore, ppost, webs, socs = rows[0]
            how = set(found[pid])
            if core and pcore == core:
                how.add("name")
            out.append({"id": pid, "name": pname, "postcode": ppost, "websites": json.loads(webs or "[]"),
                        "socials": json.loads(socs or "[]"), "how": sorted(how)})
        return out
