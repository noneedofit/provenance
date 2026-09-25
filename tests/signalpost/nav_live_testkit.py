"""In-memory fakes for `nav_feed.py` / `nav_live.py` tests: no network, no filesystem.

`FakeNavHttpClient` implements the `HttpClient` protocol plus the additive `headers=` kwarg
(`BudgetedHttpClient.get()` gained it so `nav_live.py`/`nav_feed.py` can send
`Authorization: Bearer <token>` and `If-Modified-Since`). It answers three endpoint families:
- the public token endpoint (exact URL),
- feed pages (`FEED_URL` for the root page, `FEED_URL/{page_id}` for any other - registered by page id),
- feedentry lookups (`FEEDENTRY_PREFIX{uuid}` - exact URL per uuid).

Every call is recorded (url, org, purpose, headers) so tests can assert on request sequencing/headers
(e.g. "If-Modified-Since only on the very first, non-resumed request").
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

TOKEN_URL = "https://pam-stilling-feed.nav.no/api/publicToken"
FEED_URL = "https://pam-stilling-feed.nav.no/api/v1/feed"
FEEDENTRY_PREFIX = "https://pam-stilling-feed.nav.no/api/v1/feedentry/"
SEARCH_URL = "https://arbeidsplassen.nav.no/stillinger/api/search"
TOKEN_BODY = b"Current public token for Nav Job Vacancy Feed:\neyJhbGciOiJIUzI1NiJ9.fake.token\n"
FAKE_TOKEN = "eyJhbGciOiJIUzI1NiJ9.fake.token"


def load_fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass
class RecordedCall:
    url: str
    org: str | None
    purpose: str
    headers: dict[str, str] | None


@dataclass
class FakeNavHttpClient:
    """Fake `HttpClient` for nav_feed/nav_live tests.

    `pages`: {page_id_or_None: page_json_dict}; `None` is the root `FEED_URL` (no id in the URL).
    `feedentries`: {uuid: body_bytes}. `token_body` defaults to a valid token response.
    `required_bearer`: if set, feed/feedentry requests without exactly this bearer get 401.
    """

    pages: dict[str | None, dict] = field(default_factory=dict)
    feedentries: dict[str, bytes] = field(default_factory=dict)
    token_body: bytes = TOKEN_BODY
    token_status: int = 200
    calls: list[RecordedCall] = field(default_factory=list)
    budget: dict[str, int] = field(default_factory=dict)
    default_remaining: int = 1000
    required_bearer: str | None = None
    # search fallback: search_responder(url, call_number) -> (status, body_bytes); call_number starts at 1.
    search_responder: Callable[[str, int], tuple[int, bytes]] | None = None
    search_calls: int = 0

    def _json_response(self, url: str, obj: dict) -> Response:
        import json

        raw = json.dumps(obj).encode("utf-8")
        return Response(
            url=url, final_url=url, redirect_chain=[url], status=200,
            headers={"content-type": "application/json"}, body=raw,
            retrieved_at="2026-09-24T00:00:00Z", content_sha256=_sha(raw), elapsed_ms=1,
            requests_used=1, error=None, snapshot_ref=f"snap:{_sha(raw)[:12]}",
        )

    def _unauthorized(self, url: str) -> Response:
        raw = b'{"status":401}'
        return Response(
            url=url, final_url=url, redirect_chain=[url], status=401,
            headers={"content-type": "application/json"}, body=raw,
            retrieved_at="2026-09-24T00:00:00Z", content_sha256=_sha(raw), elapsed_ms=1,
            requests_used=1, error=None,
        )

    def _not_found(self, url: str) -> Response:
        return Response(
            url=url, final_url=url, redirect_chain=[url], status=0, headers={}, body=b"",
            retrieved_at="2026-09-24T00:00:00Z", content_sha256="", elapsed_ms=1, requests_used=1,
            error="not_found_in_fake",
        )

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
                    retrieved_at="2026-09-24T00:00:00Z", content_sha256="", elapsed_ms=0, requests_used=0,
                    error="budget_exhausted",
                )
            self.budget[org] = remaining - 1

        if url == TOKEN_URL:
            return Response(
                url=url, final_url=url, redirect_chain=[url], status=self.token_status,
                headers={"content-type": "text/plain"}, body=self.token_body,
                retrieved_at="2026-09-24T00:00:00Z", content_sha256=_sha(self.token_body), elapsed_ms=1,
                requests_used=1, error=None,
            )

        bearer = (headers or {}).get("Authorization")
        if self.required_bearer is not None and bearer != f"Bearer {self.required_bearer}":
            return self._unauthorized(url)

        if url == FEED_URL or url.startswith(FEED_URL + "/"):
            page_id = None if url == FEED_URL else url[len(FEED_URL) + 1:]
            page = self.pages.get(page_id)
            if page is None:
                return self._not_found(url)
            return self._json_response(url, page)

        if url.startswith(FEEDENTRY_PREFIX):
            uuid = url[len(FEEDENTRY_PREFIX):]
            body = self.feedentries.get(uuid)
            if body is None:
                raw = b'{"status":404}'
                return Response(
                    url=url, final_url=url, redirect_chain=[url], status=404,
                    headers={"content-type": "application/json"}, body=raw,
                    retrieved_at="2026-09-24T00:00:00Z", content_sha256=_sha(raw), elapsed_ms=1,
                    requests_used=1, error="http_4xx",
                )
            return Response(
                url=url, final_url=url, redirect_chain=[url], status=200,
                headers={"content-type": "application/json"}, body=body,
                retrieved_at="2026-09-24T00:00:00Z", content_sha256=_sha(body), elapsed_ms=1,
                requests_used=1, error=None, snapshot_ref=f"snap:{_sha(body)[:12]}",
            )

        if url.startswith(SEARCH_URL + "?"):
            self.search_calls += 1
            if self.search_responder is not None:
                status, body = self.search_responder(url, self.search_calls)
            else:
                status, body = 200, b'{"hits":{"total":{"value":0},"hits":[]}}'
            error = "http_4xx" if 400 <= status < 500 else None
            return Response(
                url=url, final_url=url, redirect_chain=[url], status=status,
                headers={"content-type": "application/json"}, body=body,
                retrieved_at="2026-09-24T00:00:00Z", content_sha256=_sha(body), elapsed_ms=1,
                requests_used=1, error=error,
            )

        return self._not_found(url)

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
    caches: Any = None,
) -> CompanyContext:
    shared = {}
    if registry_facts is not None:
        shared["registry_facts"] = registry_facts
    return CompanyContext(
        org=org, run_id="test-run", now="2026-09-23T00:00:00Z", tier=tier, bulk=bulk or {},
        registry={}, caches=caches, client=client, snapshots=None, shared=shared, previous=None,
    )
