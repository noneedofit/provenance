"""Shared helpers for cache storage: sqlite connections, meta.json, packing/publishing.

All caches are stored as sqlite3 files under a cache directory, plus one `meta.json` describing how and
when they were built. Nothing here talks to the network except `fetch_and_unpack`, which downloads a
published tarball through the caller's HttpClient.
"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

META_FILENAME = "meta.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    """Streaming sha256 of a file on disk (works for large bulk downloads)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a sqlite database. Read-only connections use the `file:` URI form so a missing file
    raises immediately instead of silently creating an empty database.
    """
    path = Path(path)
    if readonly:
        if not path.exists():
            raise FileNotFoundError(path)
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class ReadOnlyPerThread:
    """A read-only sqlite handle that gives each worker thread its own connection.

    One sqlite3.Connection shared by the pipeline's worker threads raises InterfaceError / returns
    garbled rows under concurrent queries; read-only files are safe to open once per thread.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self._path, readonly=True)
            self._local.conn = conn
        return conn

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self._conn().execute(sql, params)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def connect_ro_fast(path: str | Path) -> "ReadOnlyPerThread | None":
    """Best-effort read-only open for `Caches.load`; returns None instead of raising so a missing or
    corrupt cache file degrades to `None` on the containing attribute (never a hard failure).
    """
    try:
        handle = ReadOnlyPerThread(path)
        # Cheap sanity probe: fail fast on a corrupt/incompatible file rather than at first query.
        handle.execute("SELECT 1").fetchone()
        return handle
    except Exception:
        return None


@dataclass
class Meta:
    built_at: str = ""
    parts: dict[str, dict[str, Any]] = field(default_factory=dict)  # part name -> {source_urls, row_count, input_sha256, ...}
    signalpost_agent_version: str = "0.1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "built_at": self.built_at,
            "parts": self.parts,
            "signalpost_agent_version": self.signalpost_agent_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Meta":
        return cls(
            built_at=d.get("built_at", ""),
            parts=d.get("parts", {}),
            signalpost_agent_version=d.get("signalpost_agent_version", "0.1.0"),
        )


def read_meta(cache_dir: str | Path) -> dict[str, Any] | None:
    p = Path(cache_dir) / META_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_meta(cache_dir: str | Path, meta: Meta | dict[str, Any]) -> None:
    p = Path(cache_dir) / META_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    data = meta.to_dict() if isinstance(meta, Meta) else meta
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def update_meta_part(cache_dir: str | Path, part: str, info: dict[str, Any]) -> None:
    """Merge one part's build info into meta.json, updating built_at."""
    existing = read_meta(cache_dir) or {"built_at": utc_now(), "parts": {}}
    existing.setdefault("parts", {})[part] = info
    existing["built_at"] = utc_now()
    write_meta(cache_dir, existing)


# --- Publishing: pack a built cache directory into a tarball, and fetch+unpack one ------------------

PACKED_FILES = ("meta.json", "email_domains.sqlite", "aliases.sqlite", "wikidata.sqlite", "nav.sqlite")


def pack(cache_dir: str | Path, tar_path: str | Path) -> str:
    """Tar+gzip the cache directory's known files into `tar_path` for distribution as a release
    asset. Returns the sha256 of the resulting tarball.
    """
    cache_dir = Path(cache_dir)
    tar_path = Path(tar_path)
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "w:gz") as tf:
        for name in PACKED_FILES:
            fp = cache_dir / name
            if fp.exists():
                tf.add(fp, arcname=name)
    return sha256_file(tar_path)


def fetch_and_unpack(url: str, dest: str | Path, client: Any) -> dict[str, Any]:
    """Download a published cache tarball through `client` (a context.HttpClient) and unpack it into
    `dest`. Counts as a request against the caller's budget (purpose "cache_fetch", org=None).
    Returns a small report dict: {url, bytes, sha256, files}.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    resp = client.get(url, org=None, purpose="cache_fetch", accept="application/gzip", max_bytes=500_000_000, snapshot=False)
    if not getattr(resp, "ok", False):
        raise RuntimeError(f"fetch_and_unpack: GET {url} failed: status={getattr(resp, 'status', None)} error={getattr(resp, 'error', None)}")
    body = resp.body
    digest = sha256_bytes(body)
    files: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
        safe_members = [m for m in tf.getmembers() if not (m.name.startswith("/") or ".." in Path(m.name).parts)]
        try:
            tf.extractall(dest, members=safe_members, filter="data")
        except TypeError:
            # Python < 3.12 tarfile.extractall doesn't accept `filter`.
            tf.extractall(dest, members=safe_members)
        files = [m.name for m in safe_members]
    return {"url": url, "bytes": len(body), "sha256": digest, "files": files}
