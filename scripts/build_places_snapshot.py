"""Rebuild the bundled open places snapshots (not needed to run the agent; the snapshots ship with the code).

1. Overture Maps Places -> src/signalpost/caches/snapshot/places_no.jsonl.gz
   Norwegian places that carry a website or social link, one JSON list per line:
   [id, name, postcode, phone keys, e-mail keys, websites, socials] - phones and e-mails are stored only as
   one-way keys (see caches/places.contact_key), never as raw contact data.
   Needs DuckDB (`uv run --with duckdb python scripts/build_places_snapshot.py`); reads the public,
   anonymous S3 release directly (about a minute).
2. OpenStreetMap features tagged ref:NO:orgnr -> src/signalpost/caches/snapshot/osm_orgnr_no.jsonl.gz
   One Overpass API query: [orgnr, "type/id", name, website, {platform: url}].

After rebuilding, update SNAPSHOT_META in src/signalpost/caches/places.py (release and OSM base time).
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

SNAP = Path(__file__).resolve().parent.parent / "src" / "signalpost" / "caches" / "snapshot"
OVERPASS = "https://overpass-api.de/api/interpreter"
OSM_QUERY = '[out:json][timeout:190];nwr["ref:NO:orgnr"](57.8,4.0,71.5,31.5);out tags;'


def _phone(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0047"):
        digits = digits[4:]
    if digits.startswith("47") and len(digits) == 10:
        digits = digits[2:]
    return digits if len(digits) == 8 else None


def _is_site_url(url: str) -> bool:
    """A website, not an e-mail address typed into the website field ("http://name@live.com")."""
    host = re.sub(r"^[a-z]+://", "", url or "", flags=re.I).split("/", 1)[0]
    return bool(host) and "@" not in (url or "")


def build_overture(release: str) -> int:
    import duckdb  # build-time only

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    rows = con.execute(f"""
        SELECT id, names.primary, addresses[1].postcode, phones, emails, websites, socials
        FROM read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*', hive_partitioning=1)
        WHERE bbox.xmin BETWEEN 4.0 AND 31.5 AND bbox.ymin BETWEEN 57.8 AND 71.5
          AND addresses[1].country = 'NO' AND (websites IS NOT NULL OR socials IS NOT NULL)
    """).fetchall()
    out = []
    from signalpost.caches.places import contact_key

    for pid, name, postcode, phones, emails, websites, socials in rows:
        # Phones and e-mails are stored only as one-way keys (no raw contact data in the repository); the
        # agent derives the same key from the register's phone/e-mail to match.
        phs = sorted({contact_key("tel", p) for p in (_phone(x) for x in phones or []) if p})
        ems = sorted({contact_key("mail", e.strip().lower()) for e in emails or [] if "@" in e})
        if not (phs or ems or (name and postcode)):
            continue
        webs = sorted({w for w in websites or [] if _is_site_url(w)})
        out.append([pid, name or "", postcode or "", phs, ems, webs, sorted(set(socials or []))])
    out.sort(key=lambda r: r[0])
    with gzip.open(SNAP / "places_no.jsonl.gz", "wt", encoding="utf-8", compresslevel=9) as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(out)


def build_osm() -> tuple[int, str]:
    data = urllib.parse.urlencode({"data": OSM_QUERY}).encode()
    with urllib.request.urlopen(urllib.request.Request(OVERPASS, data=data), timeout=200) as resp:
        payload = json.loads(resp.read())
    out = []
    for e in payload.get("elements", []):
        t = e.get("tags", {})
        org = re.sub(r"\D", "", t.get("ref:NO:orgnr", ""))
        if len(org) != 9:
            continue
        web = t.get("website") or t.get("contact:website") or t.get("url") or ""
        soc = {k.split(":")[-1]: t[k] for k in ("contact:facebook", "contact:instagram", "contact:linkedin",
                                                 "contact:youtube", "contact:twitter", "facebook", "instagram") if t.get(k)}
        out.append([org, f"{e['type']}/{e['id']}", t.get("name", ""), web, soc])
    out.sort()
    with gzip.open(SNAP / "osm_orgnr_no.jsonl.gz", "wt", encoding="utf-8", compresslevel=9) as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(out), payload.get("osm3s", {}).get("timestamp_osm_base", "")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", default="2026-09-23.1", help="Overture release, e.g. 2026-09-23.1")
    ap.add_argument("--skip-overture", action="store_true")
    ap.add_argument("--skip-osm", action="store_true")
    a = ap.parse_args()
    if not a.skip_overture:
        print("overture places:", build_overture(a.release))
    if not a.skip_osm:
        print("osm features, base time:", build_osm())
