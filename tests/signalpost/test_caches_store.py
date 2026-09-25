from __future__ import annotations

import json
from pathlib import Path

import pytest

from signalpost.caches import store


def test_meta_roundtrip(tmp_path: Path):
    store.update_meta_part(tmp_path, "email_domains", {"row_count": 10})
    store.update_meta_part(tmp_path, "aliases", {"row_count": 3})
    meta = store.read_meta(tmp_path)
    assert meta is not None
    assert meta["parts"]["email_domains"]["row_count"] == 10
    assert meta["parts"]["aliases"]["row_count"] == 3
    assert "built_at" in meta


def test_read_meta_missing_returns_none(tmp_path: Path):
    assert store.read_meta(tmp_path / "nope") is None


def test_connect_ro_fast_missing_file_returns_none(tmp_path: Path):
    assert store.connect_ro_fast(tmp_path / "missing.sqlite") is None


def test_pack_and_fetch_and_unpack_roundtrip(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "meta.json").write_text(json.dumps({"built_at": "now", "parts": {}}), encoding="utf-8")
    (cache_dir / "email_domains.sqlite").write_bytes(b"fake-sqlite-bytes")

    tar_path = tmp_path / "cache.tar.gz"
    digest = store.pack(cache_dir, tar_path)
    assert tar_path.exists()
    assert len(digest) == 64

    class FakeResponse:
        def __init__(self, body: bytes):
            self.body = body
            self.status = 200
            self.error = None

        @property
        def ok(self):
            return True

    class FakeClient:
        def get(self, url, **kwargs):
            return FakeResponse(tar_path.read_bytes())

    dest = tmp_path / "unpacked"
    report = store.fetch_and_unpack("https://example.com/cache.tar.gz", dest, FakeClient())
    assert (dest / "meta.json").exists()
    assert (dest / "email_domains.sqlite").read_bytes() == b"fake-sqlite-bytes"
    assert report["sha256"] == digest
    assert set(report["files"]) == {"meta.json", "email_domains.sqlite"}


def test_sha256_file(tmp_path: Path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"hello world")
    import hashlib
    assert store.sha256_file(p) == hashlib.sha256(b"hello world").hexdigest()


def test_read_only_cache_handle_is_safe_across_threads(tmp_path):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor

    from signalpost.caches import store

    db = tmp_path / "t.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"v{i}") for i in range(500)])
    conn.commit()
    conn.close()
    handle = store.connect_ro_fast(db)

    def read(i):
        return handle.execute("SELECT v FROM t WHERE k = ?", (i % 500,)).fetchone()["v"]

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(read, range(4000)))
    assert results == [f"v{i % 500}" for i in range(4000)]
