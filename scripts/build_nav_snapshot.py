"""Rebuild the bundled NAV active-ads snapshot (src/signalpost/caches/snapshot/nav_active_ads.jsonl.gz).

Walks NAV's public job feed (pam-stilling-feed.nav.no, NLOD) over the last N days into a fresh index and
writes every ad that is ACTIVE at the end of the walk: [uuid, title, businessName, municipal, sistEndret],
preceded by one meta line {"as_of": <time of the last page>, "cursor": <feed page id at the live tip>}.
At run time the agent loads this snapshot and live-walks only the feed since `as_of`; every match is
still confirmed live (feedentry, employer org number) before anything is published.

Each ad's feedentry is read once (employer org number and homepage) unless --no-enrich.

Usage: uv run python scripts/build_nav_snapshot.py [--days 150] [--state-dir /tmp/nav-snap] [--no-enrich]
"""
from __future__ import annotations

import argparse
import gzip
import json
import tempfile
from pathlib import Path

from signalpost.activity import nav_feed
from signalpost.activity.nav_live import NavLiveConnector
from signalpost.http import Budget, BudgetedHttpClient

OUT = Path(__file__).resolve().parent.parent / "src" / "signalpost" / "caches" / "snapshot" / "nav_active_ads.jsonl.gz"


def fetch_employers(uuids: list[str], workers: int = 6) -> dict[str, dict]:
    """Read each ad's feedentry once (employer org number and homepage). Ads gone or no longer ACTIVE are
    returned with status != "ACTIVE" so they can be dropped."""
    import threading
    import time
    import urllib.error
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    from signalpost.http import USER_AGENT

    def token() -> str:
        req = urllib.request.Request("https://pam-stilling-feed.nav.no/api/publicToken", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            last = [line for line in resp.read().decode().splitlines() if line.strip()][-1]
        return last.split(":", 1)[-1].strip() if not last.startswith("ey") else last.strip()

    tok = [token()]
    lock = threading.Lock()
    out: dict[str, dict] = {}

    def one(uuid: str) -> None:
        for attempt in range(4):
            req = urllib.request.Request(
                f"https://pam-stilling-feed.nav.no/api/v1/feedentry/{uuid}",
                headers={"Authorization": f"Bearer {tok[0]}", "Accept": "application/json", "User-Agent": USER_AGENT},
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = json.loads(resp.read())
                ad = body.get("ad_content") or {}
                employer = ad.get("employer") or {}
                with lock:
                    out[uuid] = {"status": body.get("status"), "orgnr": employer.get("orgnr"), "homepage": employer.get("homepage")}
                return
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    with lock:
                        tok[0] = token()
                elif exc.code in (404, 410):
                    with lock:
                        out[uuid] = {"status": "GONE"}
                    return
            except Exception:
                pass
            time.sleep(2 * (attempt + 1))

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, uuids))
    return out


def fetch_parents(orgnrs: list[str], workers: int = 8) -> dict[str, str | None]:
    """Employer org number -> parent organisation (Enhetsregisteret `overordnetEnhet` when it is a subunit)."""
    import time
    import urllib.error
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    from signalpost.http import USER_AGENT

    out: dict[str, str | None] = {}

    def one(orgnr: str) -> None:
        for attempt in range(3):
            url = f"https://data.brreg.no/enhetsregisteret/api/underenheter/{orgnr}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    out[orgnr] = json.loads(resp.read()).get("overordnetEnhet")
                return
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):  # not a subunit (a main entity, or deleted)
                    out[orgnr] = None
                    return
            except Exception:
                pass
            time.sleep(2 * (attempt + 1))
        out[orgnr] = None

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, sorted(set(orgnrs))))
    return out


def write_snapshot(index_db: Path, out: Path = OUT, employers: dict[str, dict] | None = None,
                   parents: dict[str, str | None] | None = None) -> int:
    """Write the snapshot. With `employers` (uuid -> feedentry facts), each row also carries the employer
    org number and homepage, and ads whose feedentry is no longer ACTIVE are dropped."""
    import sqlite3

    conn = sqlite3.connect(str(index_db))
    cursor = conn.execute("SELECT value FROM nav_feed_cursor").fetchone()
    as_of = conn.execute("SELECT max(updated_at) FROM active_ads").fetchone()[0]
    rows = conn.execute("SELECT uuid, title, business_name, municipal, sist_endret FROM active_ads ORDER BY uuid").fetchall()
    written = 0
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(json.dumps({"as_of": as_of, "cursor": cursor[0] if cursor else None}) + "\n")
        for row in rows:
            record = list(row)
            if employers is not None:
                facts = employers.get(row[0])
                if facts is None or facts.get("status") != "ACTIVE":
                    continue
                record += [facts.get("orgnr"), facts.get("homepage"), (parents or {}).get(facts.get("orgnr") or "")]
            fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            written += 1
    return written


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=150)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--from-index", default=None, help="write the snapshot from an existing nav_feed_index.sqlite")
    ap.add_argument("--employers", default=None, help="JSONL of already-fetched feedentry facts (uuid, status, orgnr, homepage)")
    ap.add_argument("--no-enrich", action="store_true", help="skip reading each ad's employer org number/homepage")
    ap.add_argument("--parents", default=None, help="JSON {employer orgnr: {parent: ...}} already resolved")
    a = ap.parse_args()
    parents = None
    if a.parents:
        parents = {k: (v.get("parent") if isinstance(v, dict) else v) for k, v in json.load(open(a.parents)).items()}
    employers = None
    if a.employers:
        employers = {}
        for line in open(a.employers, encoding="utf-8"):
            d = json.loads(line)
            employers[d["uuid"]] = d
    if a.from_index:
        if employers is None and not a.no_enrich:
            import sqlite3

            uuids = [r[0] for r in sqlite3.connect(a.from_index).execute("SELECT uuid FROM active_ads")]
            employers = fetch_employers(uuids)
        if employers is not None and parents is None:
            parents = fetch_parents([e.get("orgnr") for e in employers.values() if e.get("orgnr")])
        print(write_snapshot(Path(a.from_index), employers=employers, parents=parents), "active ads written to", OUT)
        return
    state = Path(a.state_dir or tempfile.mkdtemp())
    client = BudgetedHttpClient(Budget(hard_cap=5000))
    conn = NavLiveConnector()
    index = nav_feed.NavFeedIndex.open(state)
    report = nav_feed.walk_feed(
        client, lambda force=False: conn._get_token(client, [], force=force), index,
        window_days=a.days, max_pages=5000, max_seconds=7200,
    )
    print(report)
    if not a.no_enrich:
        import sqlite3

        uuids = [r[0] for r in sqlite3.connect(str(state / "nav_feed_index.sqlite")).execute("SELECT uuid FROM active_ads")]
        employers = fetch_employers(uuids)
        parents = fetch_parents([e.get("orgnr") for e in employers.values() if e.get("orgnr")])
    print(write_snapshot(state / "nav_feed_index.sqlite", employers=employers, parents=parents), "active ads written to", OUT)


if __name__ == "__main__":
    main()
