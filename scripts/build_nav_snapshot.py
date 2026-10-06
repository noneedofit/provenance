"""Rebuild the bundled NAV active-ads snapshot (src/signalpost/caches/snapshot/nav_active_ads.jsonl.gz).

Walks NAV's public job feed (pam-stilling-feed.nav.no, NLOD) over the last N days into a fresh index and
writes every ad that is ACTIVE at the end of the walk: [uuid, title, businessName, municipal, sistEndret],
preceded by one meta line {"as_of": <time of the last page>, "cursor": <feed page id at the live tip>}.
At run time the agent loads this snapshot and live-walks only the feed since `as_of`; every match is
still confirmed live (feedentry, employer org number) before anything is published.

Usage: uv run python scripts/build_nav_snapshot.py [--days 150] [--state-dir /tmp/nav-snap]
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


def write_snapshot(index_db: Path, out: Path = OUT) -> int:
    import sqlite3

    conn = sqlite3.connect(str(index_db))
    cursor = conn.execute("SELECT value FROM nav_feed_cursor").fetchone()
    as_of = conn.execute("SELECT max(updated_at) FROM active_ads").fetchone()[0]
    rows = conn.execute("SELECT uuid, title, business_name, municipal, sist_endret FROM active_ads ORDER BY uuid").fetchall()
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(json.dumps({"as_of": as_of, "cursor": cursor[0] if cursor else None}) + "\n")
        for row in rows:
            fh.write(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=150)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--from-index", default=None, help="write the snapshot from an existing nav_feed_index.sqlite")
    a = ap.parse_args()
    if a.from_index:
        print(write_snapshot(Path(a.from_index)), "active ads written to", OUT)
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
    print(write_snapshot(state / "nav_feed_index.sqlite"), "active ads written to", OUT)


if __name__ == "__main__":
    main()
