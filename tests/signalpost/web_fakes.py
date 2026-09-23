"""Test-only fakes: a minimal in-memory HttpClient implementing context.HttpClient, plus a CompanyContext
builder. No network. Import directly (not a test module itself)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from signalpost.context import CompanyContext, Response
from signalpost.models import sha256_text, utc_now


@dataclass
class FakePage:
    body: str
    status: int = 200
    final_url: str | None = None
    content_type: str = "text/html; charset=utf-8"


class FakeHttpClient:
    """Maps exact URLs to canned HTML/status. `dns` restricts which guessed hostnames resolve."""

    def __init__(self, pages: dict[str, FakePage] | None = None, dns: set[str] | None = None, remaining: int = 1000):
        self.pages = pages or {}
        self.dns = dns  # None = everything resolves
        self._remaining = remaining
        self.calls: list[str] = []

    def get(
        self, url: str, *, org: str | None = None, purpose: str = "", accept: str = "*/*",
        max_bytes: int = 2_000_000, timeout: float = 10.0, respect_robots: bool = True,
        max_redirects: int = 4, snapshot: bool = True,
    ) -> Response:
        self.calls.append(url)
        self._remaining -= 1
        entry = self.pages.get(url)
        if entry is None:
            return Response(
                url=url, final_url=url, redirect_chain=[url], status=404, headers={}, body=b"",
                retrieved_at=utc_now(), content_sha256="", elapsed_ms=1, requests_used=1, error="http_404",
            )
        body = entry.body.encode("utf-8")
        final_url = entry.final_url or url
        ok = 200 <= entry.status < 300
        return Response(
            url=url, final_url=final_url, redirect_chain=[url] if final_url == url else [url, final_url],
            status=entry.status, headers={"content-type": entry.content_type}, body=body,
            retrieved_at=utc_now(), content_sha256=sha256_text(body), elapsed_ms=1, requests_used=1,
            error=None if ok else f"http_{entry.status}", snapshot_ref=f"snap:{sha256_text(body)[:12]}",
        )

    def post_json(self, url: str, payload: Any, *, org: str | None = None, purpose: str = "", timeout: float = 20.0) -> Response:
        raise NotImplementedError

    def remaining(self, org: str | None = None) -> int:
        return self._remaining

    def dns_resolves(self, hostname: str) -> bool:
        if self.dns is None:
            return True
        return hostname in self.dns


def make_ctx(
    org: str, *, tier: str = "T2", bulk: dict | None = None, registry_facts: dict | None = None,
    client: FakeHttpClient | None = None, caches: Any = None,
) -> CompanyContext:
    shared: dict[str, Any] = {}
    if registry_facts is not None:
        shared["registry_facts"] = registry_facts
    return CompanyContext(
        org=org, run_id="test-run", now=utc_now(), tier=tier, bulk=bulk or {}, registry={},
        caches=caches, client=client or FakeHttpClient(), snapshots=None, shared=shared, previous=None,
    )
