from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from signalpost.caches import nav as nav_mod
from signalpost.caches.aliases import Aliases
from signalpost.caches.nav import Nav, _entry_to_ad_row, fuzzy_name_match

FIXTURES = Path(__file__).parent.parent / "fixtures" / "caches"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- _entry_to_ad_row -------------------------------------------------------------------------------
# Regression coverage for a real bug (fixed 2026-09-24): the live feedentry endpoint nests almost
# everything under `ad_content`, with only uuid/status/sistEndret at the top level. An earlier version
# read `employer`/`title`/etc. off the top level directly, which silently produced `employer_orgnr =
# NULL` for every detail-fetched ad in the real build (532/532 rows affected before the fix).

def test_entry_to_ad_row_reads_wrapped_ad_content():
    entry = _load("nav_feedentry_aaa.json")
    row = _entry_to_ad_row(entry, entry_id="aaa-uuid", retrieved_at="2026-09-24T00:00:00Z")
    (uuid, title, status, employer_name, employer_orgnr, employer_homepage, published, expires,
     updated, application_url, source_url, retrieved_at, content_sha256, work_locations) = row
    assert uuid == "aaa-uuid"
    assert title == "Butikkmedarbeider"
    assert status == "ACTIVE"
    assert employer_name == "Eiker Hagesenter AS"
    assert employer_orgnr == "100000001"
    assert employer_homepage == "https://www.eikerhagesenter.no"
    assert application_url == "https://eikerhagesenter.no/jobb/soknad"


def test_entry_to_ad_row_falls_back_to_flat_shape_when_no_ad_content():
    # Defensive fallback for a flatter shape (no `ad_content` wrapper) so a future API change or an
    # unusual response doesn't silently null out every field again.
    flat_entry = {
        "uuid": "flat-uuid",
        "title": "Flat Shape Job",
        "status": "ACTIVE",
        "employer": {"name": "Flat AS", "orgnr": "200000002", "homepage": "https://flat.no"},
        "published": "2026-01-01",
        "workLocations": [],
    }
    row = _entry_to_ad_row(flat_entry, entry_id="flat-uuid", retrieved_at="2026-09-24T00:00:00Z")
    assert row[0] == "flat-uuid"
    assert row[1] == "Flat Shape Job"
    assert row[4] == "200000002"  # employer_orgnr


# --- fuzzy_name_match -----------------------------------------------------------------------------

def test_fuzzy_name_match_exact_after_casefold_and_legal_strip():
    assert fuzzy_name_match("Eiker Hagesenter AS", ["EIKER HAGESENTER"]) is True


def test_fuzzy_name_match_token_overlap():
    assert fuzzy_name_match("Eiker Hagesenter Hokksund AS", ["Eiker Hagesenter"]) is True


def test_fuzzy_name_match_no_overlap():
    assert fuzzy_name_match("Completely Different Firma", ["Eiker Hagesenter"]) is False


def test_fuzzy_name_match_empty_inputs():
    assert fuzzy_name_match("", ["Eiker Hagesenter"]) is False
    assert fuzzy_name_match("Eiker Hagesenter", []) is False


# --- Nav.ads_for -----------------------------------------------------------------------------------

def _make_nav_db(tmp_path: Path) -> Nav:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    nav_mod._schema(conn)
    conn.execute(
        "INSERT INTO ads (uuid, title, status, employer_name, employer_orgnr, employer_homepage, "
        "published, expires, updated, application_url, source_url, retrieved_at, content_sha256, work_locations) "
        "VALUES ('u1','Job1','ACTIVE','Parent AS','100000001','https://parent.no',"
        "'2026-01-01','2026-12-31','2026-01-01','https://parent.no/apply',"
        "'https://pam-stilling-feed.nav.no/api/v1/feedentry/u1','2026-09-23T00:00:00Z','abc123','[]')"
    )
    conn.execute(
        "INSERT INTO ads (uuid, title, status, employer_name, employer_orgnr, employer_homepage, "
        "published, expires, updated, application_url, source_url, retrieved_at, content_sha256, work_locations) "
        "VALUES ('u2','Job2','ACTIVE','Subunit BEDR','200000002','https://parent.no',"
        "'2026-01-01','2026-12-31','2026-01-01','https://parent.no/apply',"
        "'https://pam-stilling-feed.nav.no/api/v1/feedentry/u2','2026-09-23T00:00:00Z','def456','[]')"
    )
    conn.execute(
        "INSERT INTO ads (uuid, title, status, employer_name, employer_orgnr, employer_homepage, "
        "published, expires, updated, application_url, source_url, retrieved_at, content_sha256, work_locations) "
        "VALUES ('u3','Job3','ACTIVE','Unrelated AS','999999999','https://unrelated.no',"
        "'2026-01-01','2026-12-31','2026-01-01','https://unrelated.no/apply',"
        "'https://pam-stilling-feed.nav.no/api/v1/feedentry/u3','2026-09-23T00:00:00Z','ghi789','[]')"
    )
    conn.commit()
    return Nav(conn, Path(":memory:"))


def test_ads_for_direct_org_match(tmp_path: Path):
    nav = _make_nav_db(tmp_path)
    results = nav.ads_for(["100000001"])
    uuids = {r["uuid"] for r in results}
    assert uuids == {"u1"}


def test_ads_for_resolves_subunits_via_aliases(tmp_path: Path):
    from signalpost.caches import aliases as aliases_mod
    fixture = FIXTURES / "underenheter_sample.csv.gz"
    aliases_mod.build(fixture, tmp_path)
    # Reuse the subunit 200000002 wiring but point it at parent 100000001 to match _make_nav_db's ad.
    a = Aliases.load(tmp_path)
    assert a is not None

    nav = _make_nav_db(tmp_path)
    nav.aliases = a
    # 100000001 (Eiker Hagesenter) has subunit 200000002 (Hageland Hokksund) in the fixture.
    results = nav.ads_for(["100000001"])
    uuids = {r["uuid"] for r in results}
    assert uuids == {"u1", "u2"}


def test_ads_for_empty_orgs_returns_empty(tmp_path: Path):
    nav = _make_nav_db(tmp_path)
    assert nav.ads_for([]) == []


def test_ads_for_no_match_returns_empty(tmp_path: Path):
    nav = _make_nav_db(tmp_path)
    assert nav.ads_for(["000000000"]) == []


# --- sync_incremental (via a fake context.HttpClient) -----------------------------------------------

class _FakeResponse:
    def __init__(self, body: dict, status: int = 200, error: str | None = None):
        self._body = json.dumps(body).encode("utf-8")
        self.status = status
        self.error = error
        self.requests_used = 1

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    def text(self) -> str:
        return self._body.decode("utf-8")


class _FakeClient:
    """Serves the two fixture feed pages plus one feedentry detail; records calls made."""

    def __init__(self):
        self.page1 = _load("nav_feed_page1.json")
        self.page2 = _load("nav_feed_page2.json")
        self.entry_aaa = _load("nav_feedentry_aaa.json")
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith("/api/v1/feed"):
            return _FakeResponse(self.page1)
        if url.endswith("/page-0002"):
            return _FakeResponse(self.page2)
        if url.endswith("/entry-aaa"):
            return _FakeResponse(self.entry_aaa)
        return _FakeResponse({}, status=404, error="http_4xx")


def _empty_nav(tmp_path: Path) -> Nav:
    conn = sqlite3.connect(str(tmp_path / "nav.sqlite"))
    conn.row_factory = sqlite3.Row
    nav_mod._schema(conn)
    return Nav(conn, tmp_path / "nav.sqlite")


def test_sync_incremental_matches_batch_name_and_fetches_detail(tmp_path: Path):
    nav = _empty_nav(tmp_path)
    client = _FakeClient()

    stats = nav.sync_incremental(client, max_requests=60, batch_names=["Eiker Hagesenter AS"])

    assert stats["pages_fetched"] == 2
    # Only the businessName matching a batch name should trigger a detail fetch.
    assert stats["new_active_matched"] == 1
    assert any(c.endswith("/entry-aaa") for c in client.calls)
    assert not any(c.endswith("/entry-ccc") for c in client.calls)

    ad = nav._conn.execute("SELECT * FROM ads WHERE uuid = 'aaa-uuid'").fetchone()
    assert ad is not None
    assert ad["employer_orgnr"] == "100000001"
    assert ad["employer_homepage"] == "https://www.eikerhagesenter.no"

    # Page 2 marks aaa-uuid INACTIVE -> the ad we just stored should be closed.
    assert stats["closed"] == 1
    ad2 = nav._conn.execute("SELECT status FROM ads WHERE uuid = 'aaa-uuid'").fetchone()
    assert ad2["status"] == "INACTIVE"


def test_sync_incremental_no_batch_names_fetches_nothing(tmp_path: Path):
    nav = _empty_nav(tmp_path)
    client = _FakeClient()
    stats = nav.sync_incremental(client, max_requests=60, batch_names=[])
    assert stats["new_active_matched"] == 0
    assert not any(c.endswith("/entry-aaa") for c in client.calls)


def test_sync_incremental_resumes_from_saved_cursor(tmp_path: Path):
    nav = _empty_nav(tmp_path)
    nav_mod._set_cursor(nav._conn, "last_feed_page_id", "page-0002")
    nav._conn.commit()
    client = _FakeClient()
    stats = nav.sync_incremental(client, max_requests=60, batch_names=[])
    # Should go straight to page 2, never re-fetching page 1.
    assert stats["pages_fetched"] == 1
    assert all(not c.endswith("/api/v1/feed") for c in client.calls)
