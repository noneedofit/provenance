from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from signalpost.context import CompanyContext, Response

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "activity"


def load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass
class FakeRoute:
    body: bytes = b""
    status: int = 200
    error: str | None = None
    content_type: str = "application/octet-stream"


@dataclass
class FakeHttpClient:
    """Minimal fake implementing the `HttpClient` protocol for unit tests (no network)."""
    routes: dict[str, FakeRoute] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)
    budget: dict[str, int] = field(default_factory=dict)
    default_remaining: int = 100

    def route(self, url: str, body: bytes, *, status: int = 200, error: str | None = None) -> None:
        self.routes[url] = FakeRoute(body=body, status=status, error=error)

    def get(
        self, url: str, *, org: str | None = None, purpose: str = "", accept: str = "*/*",
        max_bytes: int = 2_000_000, timeout: float = 10.0, respect_robots: bool = True,
        max_redirects: int = 4, snapshot: bool = True,
    ) -> Response:
        self.calls.append({"url": url, "org": org, "purpose": purpose})
        if org is not None:
            self.budget[org] = self.budget.get(org, self.default_remaining) - 1
        route = self.routes.get(url)
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
            requests_used=1, error=None, snapshot_ref=None,
        )

    def post_json(self, url: str, payload: Any, *, org: str | None = None, purpose: str = "", timeout: float = 20.0) -> Response:
        raise NotImplementedError

    def remaining(self, org: str | None = None) -> int:
        if org is None:
            return self.default_remaining
        return self.budget.get(org, self.default_remaining)

    def dns_resolves(self, hostname: str) -> bool:
        return True


@dataclass
class FakeNavCache:
    ads: list[dict]
    calls: list[list[str]] = field(default_factory=list)

    def ads_for(self, orgs: list[str]) -> list[dict]:
        self.calls.append(list(orgs))
        wanted = set(orgs)
        return [ad for ad in self.ads if str(ad.get("employer_orgnr")) in wanted]


@dataclass
class FakeAliasesCache:
    subunits_by_org: dict[str, list[dict]] = field(default_factory=dict)

    def subunits(self, org: str) -> list[dict]:
        return self.subunits_by_org.get(str(org), [])

    def names(self, org: str) -> list[str]:
        return [s.get("name") for s in self.subunits(org) if s.get("name")]


@dataclass
class FakeCaches:
    nav: Any = None
    aliases: Any = None
    wikidata: Any = None
    email_domains: Any = None
    meta: dict = field(default_factory=lambda: {"built_at": "2026-09-20T00:00:00Z"})


def make_ctx(
    org: str = "999888777",
    *,
    client: Any = None,
    caches: Any = None,
    shared: dict | None = None,
    tier: str = "T2",
) -> CompanyContext:
    return CompanyContext(
        org=org, run_id="test-run", now="2026-09-23T00:00:00Z", tier=tier, bulk={},
        registry={}, caches=caches, client=client, snapshots=None, shared=shared or {}, previous=None,
    )


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
