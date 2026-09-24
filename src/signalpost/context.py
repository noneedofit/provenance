"""Runtime interfaces shared by the pipeline and connectors.

`Response`, `HttpClient`, `SnapshotStore` and `CompanyContext` are the contract. The core workstream implements
them in http.py / snapshots.py / pipeline.py; connectors only depend on this file and models.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import ConnectorResult


@dataclass
class Response:
    url: str                      # requested URL
    final_url: str                # after redirects
    redirect_chain: list[str]     # every hop, including the first URL
    status: int                   # 0 when the request failed before a status
    headers: dict[str, str]
    body: bytes                   # truncated at max_bytes
    retrieved_at: str             # ISO UTC
    content_sha256: str           # sha256 of body ("" on failure)
    elapsed_ms: int
    requests_used: int            # hops + retries counted against the budget
    error: str | None = None      # "budget_exhausted", "robots_disallowed", "timeout", "dns", "http_4xx", ...
    snapshot_ref: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class BudgetExhausted(Exception):
    pass


class HttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        org: str | None = None,         # charge the request to this company's allowance
        purpose: str = "",              # short label for the request log, e.g. "brreg_roles", "site_kontakt"
        accept: str = "*/*",
        max_bytes: int = 2_000_000,
        timeout: float = 10.0,
        respect_robots: bool = True,    # official APIs may pass False; company sites must pass True
        max_redirects: int = 4,
        snapshot: bool = True,          # store raw body in the snapshot store
        headers: dict[str, str] | None = None,  # extra request headers (e.g. NAV feed bearer token)
    ) -> Response: ...

    def post_json(self, url: str, payload: Any, *, org: str | None = None, purpose: str = "", timeout: float = 20.0) -> Response: ...

    def remaining(self, org: str | None = None) -> int: ...

    def dns_resolves(self, hostname: str) -> bool: ...   # DNS only, not an HTTP request; cached


class SnapshotStore(Protocol):
    def put(self, body: bytes, *, url: str, retrieved_at: str, content_type: str | None) -> str: ...  # returns snapshot_ref
    def get(self, snapshot_ref: str) -> bytes | None: ...


@dataclass
class CompanyContext:
    org: str
    run_id: str
    now: str
    tier: str                                  # "T0" shell .. "T3" large (planner)
    bulk: dict[str, Any]                       # raw Brønnøysund bulk CSV row (see registry.py for keys)
    registry: dict[str, Any] = field(default_factory=dict)  # normalized live registry data (entity, roles, subunits, accounts)
    caches: Any = None                         # caches.Caches instance (nav index, wikidata, email domains, aliases)
    client: HttpClient | None = None
    snapshots: SnapshotStore | None = None
    shared: dict[str, Any] = field(default_factory=dict)     # outputs other connectors published via ConnectorResult.shared
    previous: dict[str, Any] | None = None     # previous stored profile for refresh, if any


class Connector(Protocol):
    name: str
    families: tuple[str, ...]

    def run(self, ctx: CompanyContext) -> ConnectorResult: ...
