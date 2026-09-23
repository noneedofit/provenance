"""Content-addressed gzip snapshot store.

Stores raw response bodies under `<state-dir>/snapshots/<sha256[:2]>/<sha256>.gz` with a JSON sidecar
holding metadata (url, retrieved_at, content_type). Bodies are deduplicated by sha256: writing the same
bytes twice is a no-op for the compressed blob (the sidecar is refreshed with the latest url).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import threading
from pathlib import Path


class SnapshotStore:
    """Implements `signalpost.context.SnapshotStore`."""

    def __init__(self, root: str | Path):
        self.root = Path(root) / "snapshots"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _paths(self, sha256: str) -> tuple[Path, Path]:
        shard = self.root / sha256[:2]
        return shard / f"{sha256}.gz", shard / f"{sha256}.json"

    def put(self, body: bytes, *, url: str, retrieved_at: str, content_type: str | None) -> str:
        sha256 = hashlib.sha256(body).hexdigest()
        blob_path, meta_path = self._paths(sha256)
        with self._lock:
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            if not blob_path.exists():
                tmp = blob_path.with_suffix(".gz.tmp")
                with gzip.open(tmp, "wb") as f:
                    f.write(body)
                tmp.replace(blob_path)
            meta = {
                "sha256": sha256,
                "url": url,
                "retrieved_at": retrieved_at,
                "content_type": content_type,
                "bytes": len(body),
            }
            tmp_meta = meta_path.with_suffix(".json.tmp")
            tmp_meta.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            tmp_meta.replace(meta_path)
        return sha256

    def get(self, snapshot_ref: str) -> bytes | None:
        blob_path, _ = self._paths(snapshot_ref)
        if not blob_path.exists():
            return None
        with gzip.open(blob_path, "rb") as f:
            return f.read()

    def get_meta(self, snapshot_ref: str) -> dict | None:
        _, meta_path = self._paths(snapshot_ref)
        if not meta_path.exists():
            return None
        return json.loads(meta_path.read_text(encoding="utf-8"))
