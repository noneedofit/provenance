"""In-memory fake HttpClient for connector tests: no network, no filesystem."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from signalpost.context import Response


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass
class FixtureRoute:
    status: int
    body: Any
    error: str | None = None


class FakeHttpClient:
    """Maps exact URLs to canned JSON bodies / statuses. Tracks requests_used like the real budget."""

    def __init__(self, routes: dict[str, FixtureRoute] | None = None, *, budget: int = 1000):
        self.routes = routes or {}
        self.calls: list[str] = []
        self._budget = budget
        self._used = 0

    def add_json(self, url: str, status: int, body: Any) -> None:
        self.routes[url] = FixtureRoute(status=status, body=body)

    def add_fixture_file(self, url: str, path: str) -> None:
        data = json.loads(open(path, "r", encoding="utf-8").read())
        self.routes[url] = FixtureRoute(status=data["status"], body=data["body"])

    def get(
        self, url: str, *, org: str | None = None, purpose: str = "", accept: str = "*/*",
        max_bytes: int = 2_000_000, timeout: float = 10.0, respect_robots: bool = True,
        max_redirects: int = 4, snapshot: bool = True,
    ) -> Response:
        self.calls.append(url)
        if self._used >= self._budget:
            return Response(
                url=url, final_url=url, redirect_chain=[url], status=0, headers={}, body=b"",
                retrieved_at="2026-09-23T00:00:00Z", content_sha256="", elapsed_ms=0, requests_used=0,
                error="budget_exhausted",
            )
        self._used += 1
        route = self.routes.get(url)
        if route is None:
            return Response(
                url=url, final_url=url, redirect_chain=[url], status=0, headers={}, body=b"",
                retrieved_at="2026-09-23T00:00:00Z", content_sha256="", elapsed_ms=1, requests_used=1,
                error="not_found_in_fixtures",
            )
        raw = json.dumps(route.body, ensure_ascii=False).encode("utf-8") if route.body is not None else b""
        return Response(
            url=url, final_url=url, redirect_chain=[url], status=route.status, headers={"content-type": "application/json"},
            body=raw, retrieved_at="2026-09-23T00:00:00Z", content_sha256=_sha256(raw), elapsed_ms=1,
            requests_used=1, error=route.error,
        )

    def post_json(self, url: str, payload: Any, *, org: str | None = None, purpose: str = "", timeout: float = 20.0) -> Response:
        return self.get(url, org=org, purpose=purpose)

    def remaining(self, org: str | None = None) -> int:
        return max(0, self._budget - self._used)

    def dns_resolves(self, hostname: str) -> bool:
        return True
