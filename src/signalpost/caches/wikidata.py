"""Wikidata cache: items carrying a Norwegian organisation number (P2333), with official website and
social-profile identifiers, built from one SPARQL query against https://query.wikidata.org/sparql.

CC0 licensed. ~10-11k items as of 2026-09; a single unpaginated query returns them all in ~10s, but we
still page defensively (by `?item` via OFFSET) in case the endpoint times out on a given day.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import store

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "SignalpostAgent/0.1 (Builderr Signalpost keyless research agent; contact: https://github.com/noneedofit/provenance)"

# property -> (result var, profile-URL template or None for a raw URL value)
_PROPS: dict[str, tuple[str, str | None]] = {
    "P856": ("website", None),                                             # official website (raw URL)
    "P2013": ("facebook", "https://www.facebook.com/{}"),                  # Facebook ID
    "P2003": ("instagram", "https://www.instagram.com/{}/"),               # Instagram username
    "P2002": ("x", "https://x.com/{}"),                                    # X (Twitter) username
    "P4264": ("linkedin", "https://www.linkedin.com/company/{}"),          # LinkedIn company ID
    "P2397": ("youtube", "https://www.youtube.com/channel/{}"),            # YouTube channel ID
    "P1581": ("blog", None),                                               # official blog (raw URL)
}

_QUERY_TEMPLATE = """
SELECT ?item ?orgnr ?website ?facebook ?instagram ?x ?linkedin ?youtube ?blog ?labelNb ?labelEn WHERE {{
  ?item wdt:P2333 ?orgnr .
  OPTIONAL {{ ?item wdt:P856 ?website . }}
  OPTIONAL {{ ?item wdt:P2013 ?facebook . }}
  OPTIONAL {{ ?item wdt:P2003 ?instagram . }}
  OPTIONAL {{ ?item wdt:P2002 ?x . }}
  OPTIONAL {{ ?item wdt:P4264 ?linkedin . }}
  OPTIONAL {{ ?item wdt:P2397 ?youtube . }}
  OPTIONAL {{ ?item wdt:P1581 ?blog . }}
  OPTIONAL {{ ?item rdfs:label ?labelNb . FILTER(LANG(?labelNb) = "nb") }}
  OPTIONAL {{ ?item rdfs:label ?labelEn . FILTER(LANG(?labelEn) = "en") }}
}}
ORDER BY ?item
LIMIT {limit}
OFFSET {offset}
"""


def _normalize_orgnr(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def run_query(limit: int, offset: int, *, timeout: float = 60.0) -> dict[str, Any]:
    query = _QUERY_TEMPLATE.format(limit=limit, offset=offset)
    url = SPARQL_ENDPOINT + "?" + urllib.parse.urlencode({"query": query})
    req = urllib.request.Request(url, headers={
        "Accept": "application/sparql-results+json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_all_rows(*, page_size: int = 20_000, max_pages: int = 20) -> list[dict[str, Any]]:
    """Fetch every P2333-bearing item, paginated defensively. In practice one page (page_size well above
    the ~11k item count) covers everything; pagination guards against a future item-count increase or a
    query timeout forcing smaller pages.
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    for _ in range(max_pages):
        data = run_query(page_size, offset)
        bindings = data["results"]["bindings"]
        if not bindings:
            break
        rows.extend(bindings)
        if len(bindings) < page_size:
            break
        offset += page_size
    return rows


def _binding_value(binding: dict[str, Any], key: str) -> str | None:
    v = binding.get(key)
    return v.get("value") if v else None


def build_from_rows(rows: list[dict[str, Any]], cache_dir: str | Path) -> dict:
    """Build `wikidata.sqlite` from SPARQL result bindings (as returned by `fetch_all_rows`)."""
    cache_dir = Path(cache_dir)
    db_path = cache_dir / "wikidata.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE entities (orgnr TEXT PRIMARY KEY, qid TEXT, label TEXT, websites TEXT, "
        "profiles TEXT, source_url TEXT, retrieved_at TEXT)"
    )

    retrieved_at = store.utc_now()
    by_org: dict[str, dict[str, Any]] = {}
    for b in rows:
        orgnr = _normalize_orgnr(_binding_value(b, "orgnr") or "")
        item_uri = _binding_value(b, "item") or ""
        if not orgnr or not item_uri:
            continue
        qid = item_uri.rsplit("/", 1)[-1]
        entry = by_org.setdefault(orgnr, {
            "qid": qid,
            "label": _binding_value(b, "labelNb") or _binding_value(b, "labelEn"),
            "websites": set(),
            "profiles": {},
            "source_url": f"https://www.wikidata.org/wiki/{qid}",
        })
        website = _binding_value(b, "website")
        if website:
            entry["websites"].add(website)
        for prop_var, (key, template) in (
            ("facebook", ("facebook", _PROPS["P2013"][1])),
            ("instagram", ("instagram", _PROPS["P2003"][1])),
            ("x", ("x", _PROPS["P2002"][1])),
            ("linkedin", ("linkedin", _PROPS["P4264"][1])),
            ("youtube", ("youtube", _PROPS["P2397"][1])),
            ("blog", ("blog", None)),
        ):
            raw = _binding_value(b, prop_var)
            if raw:
                url = template.format(raw) if template else raw
                entry["profiles"][key] = url

    rows_out = []
    for orgnr, entry in by_org.items():
        rows_out.append((
            orgnr,
            entry["qid"],
            entry["label"],
            json.dumps(sorted(entry["websites"]), ensure_ascii=False),
            json.dumps(entry["profiles"], ensure_ascii=False, sort_keys=True),
            entry["source_url"],
            retrieved_at,
        ))
    conn.executemany(
        "INSERT INTO entities (orgnr, qid, label, websites, profiles, source_url, retrieved_at) "
        "VALUES (?,?,?,?,?,?,?)",
        rows_out,
    )
    conn.commit()
    conn.close()

    return {
        "source_urls": [SPARQL_ENDPOINT + "?query=<P2333 items with websites/social ids>"],
        "row_count": len(rows_out),
        "raw_binding_count": len(rows),
        "built_at": retrieved_at,
        "license": "CC0",
    }


def build(cache_dir: str | Path) -> dict:
    rows = fetch_all_rows()
    return build_from_rows(rows, cache_dir)


class Wikidata:
    """Loaded view over `wikidata.sqlite`."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def lookup(self, org: str) -> dict | None:
        orgnr = _normalize_orgnr(org)
        row = self._conn.execute(
            "SELECT orgnr, qid, label, websites, profiles, source_url, retrieved_at FROM entities WHERE orgnr = ?",
            (orgnr,),
        ).fetchone()
        if not row:
            return None
        return {
            "qid": row["qid"],
            "label": row["label"],
            "websites": json.loads(row["websites"] or "[]"),
            "profiles": json.loads(row["profiles"] or "{}"),
            "source_url": row["source_url"],
            "retrieved_at": row["retrieved_at"],
        }

    @classmethod
    def load(cls, cache_dir: str | Path) -> "Wikidata | None":
        conn = store.connect_ro_fast(Path(cache_dir) / "wikidata.sqlite")
        if conn is None:
            return None
        return cls(conn)
