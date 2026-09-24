"""In-memory fakes for `nav_live.py` tests: no network, no filesystem.

Kept separate from `activity_testkit.py` / `fake_http.py` because `NavLiveConnector` needs a fake
`HttpClient` that (a) supports the `headers=` kwarg (`get()` on the real `BudgetedHttpClient` gained this
as an additive extension so `nav_live.py` can send `Authorization: Bearer <token>`) and (b) routes on
query-string-bearing URLs (`.../search?q=...`) rather than exact-match URLs only, since each test company
issues a different search query.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from signalpost.context import CompanyContext, Response

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nav_live"


def load_fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass
class FakeRoute:
    body: bytes = b""
    status: int = 200
    error: str | None = None
    content_type: str = "application/octet-stream"


@dataclass
class RecordedCall:
    url: str
    org: str | None
    purpose: str
    headers: dict[str, str] | None


@dataclass
class FakeNavHttpClient:
    """Fake `HttpClient` for nav_live tests.

    - Exact-URL routes (`route()`) match first.
    - Prefix routes (`route_prefix()`) match any URL starting with the given prefix - used for the
      search endpoint, where each query builds a different `?q=...` URL.
    - `remaining(org)` tracks a per-org budget so cap/exhaustion tests can drive it to zero.
    """

    routes: dict[str, FakeRoute] = field(default_factory=dict)
    prefix_routes: list[tuple[str, Callable[[str], FakeRoute]]] = field(default_factory=list)
    calls: list[RecordedCall] = field(default_factory=list)
    budget: dict[str, int] = field(default_factory=dict)
    default_remaining: int = 100
    required_bearer: str | None = None  # if set, feedentry requests without this exact header get 401

    def route(self, url: str, body: bytes, *, status: int = 200, error: str | None = None) -> None:
        self.routes[url] = FakeRoute(body=body, status=status, error=error)

    def route_prefix(self, prefix: str, body_fn: Callable[[str], bytes], *, status: int = 200) -> None:
        self.prefix_routes.append((prefix, lambda url: FakeRoute(body=body_fn(url), status=status)))

    def _resolve(self, url: str) -> FakeRoute | None:
        if url in self.routes:
            return self.routes[url]
        for prefix, fn in self.prefix_routes:
            if url.startswith(prefix):
                return fn(url)
        return None

    def get(
        self, url: str, *, org: str | None = None, purpose: str = "", accept: str = "*/*",
        max_bytes: int = 2_000_000, timeout: float = 10.0, respect_robots: bool = True,
        max_redirects: int = 4, snapshot: bool = True, headers: dict[str, str] | None = None,
    ) -> Response:
        self.calls.append(RecordedCall(url=url, org=org, purpose=purpose, headers=headers))
        if org is not None:
            remaining = self.budget.get(org, self.default_remaining)
            if remaining <= 0:
                return Response(
                    url=url, final_url=url, redirect_chain=[url], status=0, headers={}, body=b"",
                    retrieved_at="2026-09-23T00:00:00Z", content_sha256="", elapsed_ms=0, requests_used=0,
                    error="budget_exhausted",
                )
            self.budget[org] = remaining - 1

        if self.required_bearer is not None and "feedentry" in url:
            sent = (headers or {}).get("Authorization")
            if sent != f"Bearer {self.required_bearer}":
                raw = b'{"status":401}'
                return Response(
                    url=url, final_url=url, redirect_chain=[url], status=401,
                    headers={"content-type": "application/json"}, body=raw,
                    retrieved_at="2026-09-23T00:00:00Z", content_sha256=_sha(raw), elapsed_ms=1,
                    requests_used=1, error=None,
                )

        route = self._resolve(url)
        if route is None:
            return Response(
                url=url, final_url=url, redirect_chain=[url], status=0, headers={}, body=b"",
                retrieved_at="2026-09-23T00:00:00Z", content_sha256="", elapsed_ms=1, requests_used=1,
                error="not_found_in_fake",
            )
        if route.error:
            return Response(
                url=url, final_url=url, redirect_chain=[url], status=route.status, headers={}, body=b"",
                retrieved_at="2026-09-23T00:00:00Z", content_sha256="", elapsed_ms=1, requests_used=1,
                error=route.error,
            )
        return Response(
            url=url, final_url=url, redirect_chain=[url], status=route.status,
            headers={"content-type": route.content_type}, body=route.body,
            retrieved_at="2026-09-23T00:00:00Z", content_sha256=_sha(route.body), elapsed_ms=5,
            requests_used=1, error=None, snapshot_ref=f"snap:{_sha(route.body)[:12]}",
        )

    def post_json(self, url: str, payload: Any, *, org: str | None = None, purpose: str = "", timeout: float = 20.0) -> Response:
        raise NotImplementedError

    def remaining(self, org: str | None = None) -> int:
        if org is None:
            return self.default_remaining
        return self.budget.get(org, self.default_remaining)

    def dns_resolves(self, hostname: str) -> bool:
        return True


def make_ctx(
    org: str = "919858400",
    *,
    client: Any = None,
    tier: str = "T2",
    registry_facts: dict | None = None,
    bulk: dict | None = None,
) -> CompanyContext:
    shared = {}
    if registry_facts is not None:
        shared["registry_facts"] = registry_facts
    return CompanyContext(
        org=org, run_id="test-run", now="2026-09-23T00:00:00Z", tier=tier, bulk=bulk or {},
        registry={}, caches=None, client=client, snapshots=None, shared=shared, previous=None,
    )
