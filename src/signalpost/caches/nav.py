"""NAV public job-vacancy feed cache (arbeidsplassen.no), built from `pam-stilling-feed.nav.no`.

## Feed shape (measured 2026-09-23, see docs/caches.md for the full write-up)
- `GET /api/publicToken` returns free-text; the LAST LINE is a short-lived JWT, used as
  `Authorization: Bearer <token>` on every other call.
- `GET /api/v1/feed` returns the FIRST page (oldest first) unless `?last=true` is given, which returns
  the single newest item and no `next_id` — useful as a liveness probe but not for full traversal
  (the feed has no `previous_id`, so it can only be walked forward from page 1 or resumed from a saved
  `next_id` checkpoint).
- `GET /api/v1/feed/{feedPageId}` returns up to 1000 items plus `next_id`/`next_url`; `next_id` is null
  on the last page.
- Each item carries a full-history `_feed_entry` (uuid, status ACTIVE/INACTIVE, businessName, sistEndret).
  The feed is an append-only *event log*: the same job-ad uuid can appear multiple times as its status
  changes, so the correct "current" state for a uuid is whichever entry for it appears LAST when walking
  forward.
- Measured: the first ~121 pages (121,000 events) span only ~5 minutes of `sistEndret` time
  (2023-06-14T12:21 -> 2023-06-14T12:26), i.e. a one-time backfill burst when the feed was seeded — not
  representative of the steady-state posting rate. Per-request latency measured at ~3.6-3.9s regardless
  of client-side throttling (server/pagination cost, not our rate limit), so a full sequential walk from
  page 1 to "now" is a multi-hour job (thousands of pages at ~3.8s/page), far past the 45-minute daily
  run budget. `sync_incremental` is the tool for the timed daily run; a full historical build is a
  `prepare`-time job you resume across multiple invocations via the saved cursor.
- `GET /api/v1/feedentry/{entryId}` returns `{uuid, status, sistEndret, ad_content: {employer {name,
  orgnr, homepage, description}, title, published, expires, updated, sourceurl, link, applicationUrl,
  workLocations, ...}}` — almost everything is nested under `ad_content`; only `uuid`/`status`/
  `sistEndret` are top-level. (An earlier version of this cache read `employer`/`title`/etc. straight off
  the top-level object, which silently produced `employer_orgnr = NULL` for every detail-fetched ad —
  fixed 2026-09-24; `_entry_to_ad_row` now reads from `ad_content` and falls back to the top level if
  `ad_content` is absent.) `employer.orgnr` is frequently a SUBUNIT ("BEDR") organisation number, not the
  parent's — callers resolve subunit -> parent via `caches.aliases`; `Nav.ads_for` does this
  automatically when `.aliases` is set (Caches.load wires it up).

## Storage
sqlite `ads` table keyed by uuid, indexed by `employer_orgnr`; a `cursor` table holding the last feed
page id walked (`last_feed_page_id`) and its `sistEndret` timestamp, so both `prepare`'s full build and a
daily `sync_incremental` can resume/continue from where the previous run stopped.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import store

TOKEN_URL = "https://pam-stilling-feed.nav.no/api/publicToken"
BASE_URL = "https://pam-stilling-feed.nav.no"
FEED_URL = BASE_URL + "/api/v1/feed"
USER_AGENT = "SignalpostResearchAgent/0.1 (+https://github.com/noneedofit/provenance)"  # same as http.USER_AGENT

MIN_REQUEST_INTERVAL = 1.0 / 5.0  # politeness cap: <=5 req/s


def fetch_public_token(*, timeout: float = 15.0) -> str:
    req = urllib.request.Request(TOKEN_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode("utf-8", "replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("fetch_public_token: empty response")
    return lines[-1]


def _schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ads ("
        " uuid TEXT PRIMARY KEY,"
        " title TEXT,"
        " status TEXT,"
        " employer_name TEXT,"
        " employer_orgnr TEXT,"
        " employer_homepage TEXT,"
        " published TEXT,"
        " expires TEXT,"
        " updated TEXT,"
        " application_url TEXT,"
        " source_url TEXT,"
        " retrieved_at TEXT,"
        " content_sha256 TEXT,"
        " work_locations TEXT"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ads_employer_orgnr ON ads(employer_orgnr)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cursor (key TEXT PRIMARY KEY, value TEXT)"
    )
    # Lightweight status ledger for every feed entry seen but not detail-fetched, so a rerun can tell an
    # already-INACTIVE-at-first-sight ad from one never observed, without re-fetching its detail.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_status ("
        " uuid TEXT PRIMARY KEY, status TEXT, business_name TEXT, sist_endret TEXT"
        ")"
    )
    conn.commit()


def _get_cursor(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM cursor WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_cursor(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO cursor(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


@dataclass
class _RawFetcher:
    """Direct urllib fetcher used by `prepare` (no context.HttpClient available at cache-build time,
    which runs outside the timed daily run / outside the pipeline's budget).
    """
    token: str
    _last_request_ts: float = 0.0

    def get_json(self, url: str) -> dict[str, Any]:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request_ts)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "User-Agent": USER_AGENT,
        })
        backoff = 1.0
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    self._last_request_ts = time.monotonic()
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt < 4:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise
        raise RuntimeError(f"get_json: exhausted retries for {url}")


def _entry_to_ad_row(entry_json: dict[str, Any], *, entry_id: str, retrieved_at: str) -> tuple:
    # The real `GET /api/v1/feedentry/{id}` response nests almost everything under `ad_content`, with
    # only `uuid`/`status`/`sistEndret` at the top level (measured 2026-09-23 against the live feed;
    # confirmed by inspecting an actual response body, since the endpoint isn't documented). Fall back
    # to treating `entry_json` itself as the ad content when `ad_content` is absent, so a flatter shape
    # (e.g. a future API change, or a hand-built test fixture) still parses.
    ad = entry_json.get("ad_content") if isinstance(entry_json.get("ad_content"), dict) else entry_json
    employer = ad.get("employer") or {}
    status = entry_json.get("status") or ad.get("status")
    content = json.dumps(entry_json, ensure_ascii=False, sort_keys=True)
    content_sha256 = store.sha256_bytes(content.encode("utf-8"))
    source_url = f"{BASE_URL}/api/v1/feedentry/{entry_id}"
    return (
        entry_json.get("uuid") or ad.get("uuid") or entry_id,
        ad.get("title"),
        status,
        employer.get("name"),
        (employer.get("orgnr") or "").strip() or None,
        employer.get("homepage"),
        ad.get("published"),
        ad.get("expires"),
        ad.get("updated"),
        ad.get("applicationUrl"),
        source_url,
        retrieved_at,
        content_sha256,
        json.dumps(ad.get("workLocations") or [], ensure_ascii=False),
    )


def _upsert_ad(conn: sqlite3.Connection, row: tuple) -> None:
    conn.execute(
        "INSERT INTO ads (uuid, title, status, employer_name, employer_orgnr, employer_homepage, "
        "published, expires, updated, application_url, source_url, retrieved_at, content_sha256, work_locations) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(uuid) DO UPDATE SET title=excluded.title, status=excluded.status, "
        "employer_name=excluded.employer_name, employer_orgnr=excluded.employer_orgnr, "
        "employer_homepage=excluded.employer_homepage, published=excluded.published, "
        "expires=excluded.expires, updated=excluded.updated, application_url=excluded.application_url, "
        "source_url=excluded.source_url, retrieved_at=excluded.retrieved_at, "
        "content_sha256=excluded.content_sha256, work_locations=excluded.work_locations",
        row,
    )


def _mark_seen(conn: sqlite3.Connection, uuid: str, status: str, business_name: str | None, sist_endret: str | None) -> None:
    conn.execute(
        "INSERT INTO seen_status(uuid, status, business_name, sist_endret) VALUES (?,?,?,?) "
        "ON CONFLICT(uuid) DO UPDATE SET status=excluded.status, business_name=excluded.business_name, "
        "sist_endret=excluded.sist_endret",
        (uuid, status, business_name, sist_endret),
    )


def build_full(cache_dir: str | Path, *, max_pages: int | None = None, max_seconds: float | None = None,
                fetch_details_for_active: bool = True, resume: bool = True,
                progress_every: int = 25) -> dict:
    """Walk the feed forward from page 1 (or a saved checkpoint, when `resume=True` and one exists),
    updating `seen_status` for every item and upserting a full `ads` row (with entry detail fetched)
    for items currently ACTIVE. Bounded by `max_pages` and/or `max_seconds` so a `prepare` run can be
    stopped well inside a practical build window; the cursor is saved after every page, so a second call
    continues exactly where the first left off.

    Returns a report dict: pages_fetched, items_seen, active_seen, detail_fetches, requests_used,
    elapsed_s, last_feed_page_id, reached_end (bool).
    """
    cache_dir = Path(cache_dir)
    db_path = cache_dir / "nav.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _schema(conn)

    token = fetch_public_token()
    fetcher = _RawFetcher(token=token)

    start_page_id = _get_cursor(conn, "last_feed_page_id") if resume else None
    pages_fetched = 0
    items_seen = 0
    active_seen = 0
    detail_fetches = 0
    requests_used = 1  # the token fetch counts as a request
    t0 = time.monotonic()
    reached_end = False

    if start_page_id:
        page_id: str | None = start_page_id
        page_url = f"{BASE_URL}/api/v1/feed/{page_id}"
    else:
        page_url = FEED_URL
        page_id = None

    while True:
        if max_pages is not None and pages_fetched >= max_pages:
            break
        if max_seconds is not None and (time.monotonic() - t0) >= max_seconds:
            break
        data = fetcher.get_json(page_url)
        requests_used += 1
        pages_fetched += 1
        this_page_id = data.get("id")

        for item in data.get("items", []):
            items_seen += 1
            fe = item.get("_feed_entry") or {}
            uuid = fe.get("uuid") or item.get("id")
            status = fe.get("status")
            _mark_seen(conn, uuid, status, fe.get("businessName"), fe.get("sistEndret"))
            if status == "ACTIVE":
                active_seen += 1
                if fetch_details_for_active:
                    entry_id = item.get("id") or uuid
                    try:
                        detail = fetcher.get_json(f"{BASE_URL}/api/v1/feedentry/{entry_id}")
                        requests_used += 1
                        detail_fetches += 1
                        row = _entry_to_ad_row(detail, entry_id=entry_id, retrieved_at=store.utc_now())
                        _upsert_ad(conn, row)
                    except Exception:
                        pass  # keep the seen_status record; a later run can retry the detail fetch
            elif status == "INACTIVE":
                # Ad closed: if we're already tracking it in `ads`, flip its status so consumers don't
                # publish a stale ACTIVE job.
                conn.execute("UPDATE ads SET status = 'INACTIVE' WHERE uuid = ?", (uuid,))

        next_id = data.get("next_id")
        if this_page_id:
            _set_cursor(conn, "last_feed_page_id", this_page_id)
        if pages_fetched % progress_every == 0:
            conn.commit()
        if not next_id:
            reached_end = True
            break
        page_id = next_id
        page_url = f"{BASE_URL}/api/v1/feed/{page_id}"

    conn.commit()
    conn.close()
    elapsed = time.monotonic() - t0
    return {
        "pages_fetched": pages_fetched,
        "items_seen": items_seen,
        "active_seen": active_seen,
        "detail_fetches": detail_fetches,
        "requests_used": requests_used,
        "elapsed_s": elapsed,
        "reached_end": reached_end,
    }


# --- name matching for sync_incremental -------------------------------------------------------------

_LEGAL_TOKENS = {"as", "asa", "ans", "da", "ba", "sa", "nuf", "enk"}


def _name_tokens(name: str) -> set[str]:
    words = "".join(ch if ch.isalnum() else " " for ch in (name or "").casefold()).split()
    return {w for w in words if w not in _LEGAL_TOKENS}


def fuzzy_name_match(business_name: str, candidate_names: Iterable[str]) -> bool:
    """Casefold, strip legal-form tokens, and require meaningful token overlap between the feed's
    `businessName` and a candidate company/subunit name from the current batch.
    """
    b_tokens = _name_tokens(business_name)
    if not b_tokens:
        return False
    for cand in candidate_names:
        c_tokens = _name_tokens(cand)
        if not c_tokens:
            continue
        if b_tokens == c_tokens:
            return True
        overlap = b_tokens & c_tokens
        if not overlap:
            continue
        # Overlap must cover most of the shorter name to avoid matching on one common word.
        smaller = min(len(b_tokens), len(c_tokens))
        if smaller and len(overlap) / smaller >= 0.6:
            return True
    return False


class Nav:
    """Loaded view over `nav.sqlite`. `.aliases` (an `aliases.Aliases`, wired by `Caches.load`) lets
    `ads_for` resolve a parent org number to its subunits' org numbers before matching `employer_orgnr`.
    """

    def __init__(self, conn: sqlite3.Connection, db_path: Path):
        self._conn = conn
        self._db_path = db_path
        self.aliases: Any = None  # set post-load by Caches.load

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        return {
            "uuid": row["uuid"],
            "title": row["title"],
            "status": row["status"],
            "employer_name": row["employer_name"],
            "employer_orgnr": row["employer_orgnr"],
            "employer_homepage": row["employer_homepage"],
            "published": row["published"],
            "expires": row["expires"],
            "updated": row["updated"],
            "application_url": row["application_url"],
            "source_url": row["source_url"],
            "retrieved_at": row["retrieved_at"],
            "content_sha256": row["content_sha256"],
            "work_locations": json.loads(row["work_locations"] or "[]"),
        }

    def ads_for(self, orgs: list[str]) -> list[dict]:
        """Ads whose employer_orgnr is one of `orgs` or any of their subunits (via `.aliases`)."""
        org_set: set[str] = set()
        for org in orgs:
            org_set.add(org)
            if self.aliases is not None:
                for sub in self.aliases.subunits(org):
                    num = sub.get("organisation_number")
                    if num:
                        org_set.add(num)
        if not org_set:
            return []
        placeholders = ",".join("?" for _ in org_set)
        rows = self._conn.execute(
            f"SELECT * FROM ads WHERE employer_orgnr IN ({placeholders})", tuple(org_set)
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def sync_incremental(self, client: Any, max_requests: int = 60, batch_names: list[str] | None = None) -> dict:
        """Daily-run incremental sync through `client` (a context.HttpClient; every request charged with
        org=None, purpose="nav_feed"/"nav_entry"). Walks feed pages forward from the saved cursor,
        updates `seen_status`/`ads` status for items it passes, and fetches full entry detail only for
        NEW ACTIVE items whose businessName fuzzy-matches a name in `batch_names` (so we spend the small
        daily request budget on companies actually in today's batch).
        """
        conn = self._conn
        requests_used = 0
        pages_fetched = 0
        new_active_matched = 0
        closed = 0
        errors: list[str] = []
        batch_names = batch_names or []

        page_id = _get_cursor(conn, "last_feed_page_id")
        if page_id:
            page_url = f"{BASE_URL}/api/v1/feed/{page_id}"
        else:
            page_url = FEED_URL

        while requests_used < max_requests:
            resp = client.get(page_url, org=None, purpose="nav_feed", accept="application/json")
            requests_used += getattr(resp, "requests_used", 1) or 1
            if not resp.ok:
                errors.append(f"feed page fetch failed: {getattr(resp, 'error', resp.status)}")
                break
            data = json.loads(resp.text())
            pages_fetched += 1
            this_page_id = data.get("id")

            for item in data.get("items", []):
                fe = item.get("_feed_entry") or {}
                uuid = fe.get("uuid") or item.get("id")
                status = fe.get("status")
                business_name = fe.get("businessName") or ""
                already_known = conn.execute("SELECT status FROM ads WHERE uuid = ?", (uuid,)).fetchone()
                _mark_seen(conn, uuid, status, business_name, fe.get("sistEndret"))

                if status == "INACTIVE":
                    if already_known and already_known["status"] != "INACTIVE":
                        conn.execute("UPDATE ads SET status = 'INACTIVE' WHERE uuid = ?", (uuid,))
                        closed += 1
                elif status == "ACTIVE" and not already_known and requests_used < max_requests:
                    if fuzzy_name_match(business_name, batch_names):
                        entry_id = item.get("id") or uuid
                        detail_resp = client.get(f"{BASE_URL}/api/v1/feedentry/{entry_id}", org=None,
                                                  purpose="nav_entry", accept="application/json")
                        requests_used += getattr(detail_resp, "requests_used", 1) or 1
                        if detail_resp.ok:
                            detail = json.loads(detail_resp.text())
                            row = _entry_to_ad_row(detail, entry_id=entry_id, retrieved_at=store.utc_now())
                            _upsert_ad(conn, row)
                            new_active_matched += 1
                        else:
                            errors.append(f"entry fetch failed for {entry_id}: {getattr(detail_resp, 'error', detail_resp.status)}")

            if this_page_id:
                _set_cursor(conn, "last_feed_page_id", this_page_id)
            conn.commit()

            next_id = data.get("next_id")
            if not next_id:
                break
            page_url = f"{BASE_URL}/api/v1/feed/{next_id}"

        conn.commit()
        return {
            "pages_fetched": pages_fetched,
            "requests_used": requests_used,
            "new_active_matched": new_active_matched,
            "closed": closed,
            "errors": errors,
        }

    @classmethod
    def load(cls, cache_dir: str | Path) -> "Nav | None":
        # Opened read-write (unlike the other caches): `sync_incremental` updates ad statuses and adds
        # newly matched ads during the daily run, and advances the saved feed cursor.
        db_path = Path(cache_dir) / "nav.sqlite"
        if not db_path.exists():
            return None
        try:
            conn = store.connect(db_path, readonly=False)
            conn.execute("SELECT 1").fetchone()
        except Exception:
            return None
        return cls(conn, db_path)
