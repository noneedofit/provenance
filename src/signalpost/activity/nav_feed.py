"""Shared, once-per-run "active ads" index built by walking the NAV job-vacancy feed forward from a
recent starting point, via `If-Modified-Since`, instead of NAV's search API.

Why: `arbeidsplassen.nav.no/stillinger/api/search` rate-limits aggressively per IP (measured: a full
131-company gold-set run tripped it for 93 companies, each ending `jobs=failed`). NAV's feed API
(`pam-stilling-feed.nav.no`) doesn't have that problem, but a full forward walk from the feed's 2023
start is a multi-hour job (see `caches/nav.py`'s docstring). The fix, measured live 2026-09-24: `GET
/api/v1/feed` with header `If-Modified-Since: <HTTP-date>` jumps straight to the page containing that
time, instead of page 1. Walking forward from `now - window_days` (default 60) for ~180 pages covers
the whole window in about ten minutes and ~180 requests - a one-time cost per run (or per state-dir),
not per company.

`NavFeedIndex` is the persisted result: a small sqlite table of currently-ACTIVE ads (uuid, title,
business name + its normalized form for matching, municipality, last-changed time), plus a cursor
(`last_feed_page_id`) so a later run with the same `--state-dir` only walks the handful of new pages
since the last run, rather than repeating the whole window (mirrors `caches/nav.py`'s `build_full` /
`sync_incremental` cursor pattern exactly, just seeded by `If-Modified-Since` instead of page 1).

`walk_feed(...)` performs the walk against a `context.HttpClient`, charging every request to `org=None`
(the shared pool, not any one company's allowance - same convention as `caches/nav.py`'s
`sync_incremental` and this package's own NAV public-token fetch) so it never eats into a company's own
budget or the registry reserve (`http.Budget`'s reserve is keyed by purpose prefix `registry_`, which
`nav_feed_page`/`nav_live_token` don't match). Bounded by `max_pages` and `max_seconds` so a single run
can never blow the deadline; if the walk doesn't reach the live tip of the feed within those bounds, the
index is left `partial` (not `complete`) and callers must say so rather than reporting a confident zero.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Callable

from ._common import norm_title

FEED_BASE_URL = "https://pam-stilling-feed.nav.no"
FEED_URL = FEED_BASE_URL + "/api/v1/feed"

DEFAULT_WINDOW_DAYS = 60
DEFAULT_MAX_PAGES = 200
DEFAULT_MAX_SECONDS = 720.0  # 12 minutes


class NavFeedIndex:
    """Sqlite-backed view of currently-ACTIVE NAV ads, keyed by uuid, matchable by normalized business
    name. `:memory:`/no path -> in-memory (tests, or a run with no `--state-dir`); a real path persists
    across runs so `walk_feed` can resume.

    One `NavFeedIndex` is shared by every company's worker thread for the whole run (built once by
    `NavLiveConnector.prepare`/`start_prepare`, then read by every `run(ctx)` via `.match(...)`) - a
    single `sqlite3.Connection` is not safe for concurrent use from multiple threads even with
    `check_same_thread=False` (that flag only lifts the same-thread *check*; simultaneous statements on
    one connection from different threads can still raise `sqlite3.InterfaceError: bad parameter or
    other API misuse`, measured live on a 131-company run under the pipeline's normal 12-way
    concurrency). Every method that touches `self._conn` therefore holds `self._lock` for its duration.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()
        self._schema()

    @classmethod
    def open(cls, state_dir: str | Path | None) -> "NavFeedIndex":
        if state_dir is None:
            conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            path = Path(state_dir) / "nav_feed_index.sqlite"
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return cls(conn)

    def _schema(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS active_ads ("
                " uuid TEXT PRIMARY KEY,"
                " title TEXT,"
                " business_name TEXT,"
                " business_name_norm TEXT,"
                " municipal TEXT,"
                " sist_endret TEXT,"
                " updated_at TEXT"
                ")"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_active_ads_norm ON active_ads(business_name_norm)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS nav_feed_cursor (key TEXT PRIMARY KEY, value TEXT)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS nav_search_history (organisation_number TEXT PRIMARY KEY, searched_at TEXT)"
            )
            self._conn.commit()

    # -- search-fallback history (nav_live.py's employee-ranked search fallback) -------------------

    def record_searched(self, org: str, when: str) -> None:
        """Record that `org` was searched (the keyless arbeidsplassen search fallback) at `when` (ISO
        UTC), so a later run's selection can rotate through different companies instead of re-searching
        the same ones every day - see `recently_searched`.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO nav_search_history(organisation_number, searched_at) VALUES (?, ?) "
                "ON CONFLICT(organisation_number) DO UPDATE SET searched_at = excluded.searched_at",
                (org, when),
            )
            self._conn.commit()

    def recently_searched(self, org: str, *, within_days: int, now: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT searched_at FROM nav_search_history WHERE organisation_number = ?", (org,)
            ).fetchone()
        if row is None:
            return False
        try:
            searched_at = datetime.fromisoformat(row["searched_at"].replace("Z", "+00:00"))
            now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError:
            return False
        return (now_dt - searched_at).total_seconds() < within_days * 86400

    # -- cursor -----------------------------------------------------------------------------------

    def get_cursor(self) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM nav_feed_cursor WHERE key = 'last_feed_page_id'").fetchone()
            return row["value"] if row else None

    def set_cursor(self, page_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO nav_feed_cursor(key, value) VALUES ('last_feed_page_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (page_id,),
            )

    def is_complete(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT value FROM nav_feed_cursor WHERE key = 'complete'").fetchone()
            return bool(row and row["value"] == "1")

    def set_complete(self, complete: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO nav_feed_cursor(key, value) VALUES ('complete', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("1" if complete else "0",),
            )

    # -- ads --------------------------------------------------------------------------------------

    def upsert_active(self, *, uuid: str, title: str | None, business_name: str | None,
                       municipal: str | None, sist_endret: str | None, updated_at: str) -> None:
        norm = norm_title(business_name)
        with self._lock:
            self._conn.execute(
                "INSERT INTO active_ads (uuid, title, business_name, business_name_norm, municipal, sist_endret, updated_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(uuid) DO UPDATE SET title=excluded.title, business_name=excluded.business_name, "
                "business_name_norm=excluded.business_name_norm, municipal=excluded.municipal, "
                "sist_endret=excluded.sist_endret, updated_at=excluded.updated_at",
                (uuid, title, business_name, norm, municipal, sist_endret, updated_at),
            )

    def remove(self, uuid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM active_ads WHERE uuid = ?", (uuid,))

    def match(self, name_norms: set[str]) -> list[dict]:
        """Ads whose normalized business name is exactly one of `name_norms` (already normalized by
        the caller with the same `norm_title` function used to build the index).
        """
        name_norms = {n for n in name_norms if n}
        if not name_norms:
            return []
        placeholders = ",".join("?" for _ in name_norms)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM active_ads WHERE business_name_norm IN ({placeholders})", tuple(name_norms)
            ).fetchall()
        return [dict(r) for r in rows]

    def match_fuzzy(self, name_norms: set[str], *, min_overlap: float = 0.6) -> list[dict]:
        """Ads whose normalized business name shares most of its tokens with one of `name_norms` -
        catches a trade/brand name, an "avd." (department) suffix, or a subunit name that isn't an exact
        string match but clearly refers to the same company (e.g. ad businessName "Fresh Water Norway"
        vs legal name "Fresh Water Norway AS", or "X AS avd Bergen" vs a subunit trade name "X Bergen").
        Token overlap must cover at least `min_overlap` of the SMALLER of the two token sets - the same
        threshold and shape as `caches/nav.py`'s `fuzzy_name_match`, reused here as a precedented,
        already-reviewed heuristic rather than inventing a new one. This is deliberately permissive: the
        caller (`nav_live.py`) must still confirm every candidate by organisation number via feedentry
        before publishing anything - a name overlap alone is never sufficient, only a way to find more
        candidates worth that confirmation check.
        """
        name_token_sets = [set(n.split()) for n in name_norms if n]
        name_token_sets = [t for t in name_token_sets if t]
        if not name_token_sets:
            return []
        with self._lock:
            rows = self._conn.execute("SELECT * FROM active_ads").fetchall()
        matched = []
        for row in rows:
            ad_tokens = set((row["business_name_norm"] or "").split())
            if not ad_tokens:
                continue
            for target_tokens in name_token_sets:
                overlap = ad_tokens & target_tokens
                if not overlap:
                    continue
                smaller = min(len(ad_tokens), len(target_tokens))
                if smaller and len(overlap) / smaller >= min_overlap:
                    matched.append(dict(row))
                    break
        return matched

    def content_sha256(self) -> str:
        """Hash of the indexed ACTIVE ads (uuid + last change), so evidence citing the index is pinned to
        exactly the ad set this run matched against."""
        import hashlib

        digest = hashlib.sha256()
        with self._lock:
            for row in self._conn.execute("SELECT uuid FROM active_ads ORDER BY uuid"):
                digest.update(str(row["uuid"]).encode("utf-8") + b"\n")
        return digest.hexdigest()

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM active_ads").fetchone()
            return int(row["n"]) if row else 0

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _http_date(dt: datetime) -> str:
    return format_datetime(dt, usegmt=True)


def walk_feed(
    client: Any,
    get_token: Callable[..., str | None],
    index: NavFeedIndex,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    purpose: str = "nav_feed_page",
) -> dict[str, Any]:
    """Walk the feed forward, updating `index` (ACTIVE ads upserted, closed/INACTIVE ones removed).

    Resumes from `index.get_cursor()` (the last page id this state-dir reached) when present; otherwise
    seeds the walk with `If-Modified-Since: now - window_days` so it starts near the requested window
    instead of the feed's 2023 origin. Every request is charged to `org=None` (see module docstring).
    Returns a report dict: pages, items_seen, active_seen, closed, reached_end, resumed, elapsed_s,
    errors (list[str]).
    """
    t0 = time.monotonic()
    pages = 0
    items_seen = 0
    active_seen = 0
    closed = 0
    errors: list[str] = []

    saved_page_id = index.get_cursor()
    resumed = saved_page_id is not None
    url = f"{FEED_URL}/{saved_page_id}" if saved_page_id else FEED_URL

    token = get_token()
    if token is None:
        return {
            "pages": 0, "items_seen": 0, "active_seen": 0, "closed": 0, "reached_end": False,
            "resumed": resumed, "elapsed_s": 0.0, "errors": ["no_token"],
        }

    reached_end = False
    first_request = True
    token_retries = 0
    while pages < max_pages and (time.monotonic() - t0) < max_seconds:
        headers = {"Authorization": f"Bearer {token}"}
        if first_request and not resumed:
            start_dt = datetime.now(timezone.utc) - timedelta(days=window_days)
            headers["If-Modified-Since"] = _http_date(start_dt)
        resp = client.get(url, org=None, purpose=purpose, accept="application/json", respect_robots=True, headers=headers)
        if resp.status == 401 and token_retries < 1:
            token = get_token(force=True)
            token_retries += 1
            if token is None:
                errors.append("token_refresh_failed")
                break
            continue
        first_request = False
        if resp.error == "budget_exhausted":
            errors.append("budget_exhausted")
            break
        if not resp.ok:
            errors.append(f"feed_page_failed:{resp.error or resp.status}")
            break
        try:
            data = json.loads(resp.text())
        except Exception as exc:  # defensive: external API response
            errors.append(f"parse_error:{type(exc).__name__}: {exc}")
            break

        pages += 1
        this_page_id = data.get("id")
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        for item in data.get("items", []):
            fe = item.get("_feed_entry") or {}
            uuid = str(fe.get("uuid") or item.get("id") or "").strip()
            if not uuid:
                continue
            items_seen += 1
            status = str(fe.get("status") or "").upper()
            if status == "ACTIVE":
                active_seen += 1
                index.upsert_active(
                    uuid=uuid, title=fe.get("title"), business_name=fe.get("businessName"),
                    municipal=fe.get("municipal"), sist_endret=fe.get("sistEndret"), updated_at=now_iso,
                )
            else:
                # A later INACTIVE (or any other non-ACTIVE status) supersedes an earlier ACTIVE for the
                # same uuid - the feed is an append-only event log, so this closes it if we had it.
                closed += 1
                index.remove(uuid)

        if this_page_id:
            index.set_cursor(this_page_id)
        index.commit()

        next_id = data.get("next_id")
        if not next_id:
            reached_end = True
            break
        url = f"{FEED_URL}/{next_id}"

    index.set_complete(reached_end or index.is_complete())
    index.commit()  # the per-page commits above cover the cursor/ads written during the loop, but not
    # this final "complete" flag write - without this it's silently lost (sqlite rolls back an
    # uncommitted transaction when the connection is dropped, e.g. at normal process exit).
    elapsed_s = time.monotonic() - t0
    return {
        "pages": pages, "items_seen": items_seen, "active_seen": active_seen, "closed": closed,
        "reached_end": reached_end, "resumed": resumed, "elapsed_s": elapsed_s, "errors": errors,
    }
