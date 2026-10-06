"""Budgeted, SSRF-safe HTTP client implementing `signalpost.context.HttpClient`.

Stdlib only (urllib). Every redirect hop and every retry counts against the request budget, exactly like
an outbound HTTP request would be counted by the competition's request cap. Robots.txt is honoured per
host (cached) unless the caller passes `respect_robots=False` (official/keyless APIs), and the robots.txt
fetch itself is charged to the calling org like any other request.
"""
from __future__ import annotations

import concurrent.futures
import gzip
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from typing import Any

from .context import Response
from .snapshots import SnapshotStore

try:  # reuse the starter kit's SSRF guard rather than re-implement it
    from norway_company_agent.website import assert_public_url
except Exception:  # pragma: no cover - fallback if the starter kit package is ever removed
    import ipaddress

    def assert_public_url(url: str) -> None:  # type: ignore[no-redef]
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host:
            raise ValueError("Only public HTTP(S) URLs are allowed")
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            raise ValueError("Local hosts are blocked")
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            }
        except socket.gaierror as exc:
            raise ValueError("Hostname did not resolve") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")


def _retry_after_seconds(headers: dict | None, default: float = 5.0, cap: float = 20.0) -> float:
    value = ""
    for key, val in (headers or {}).items():
        if str(key).lower() == "retry-after":
            value = str(val).strip()
    try:
        return max(1.0, min(cap, float(value)))
    except ValueError:
        return default


USER_AGENT = "SignalpostResearchAgent/0.1 (+https://github.com/noneedofit/provenance)"
DEFAULT_HOST_CONCURRENCY = 2
HOST_CONCURRENCY_OVERRIDES = {
    "data.brreg.no": 10,
    "pam-stilling-feed.nav.no": 8,   # NAV's bulk feed for job-board consumers; one request per time slice
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disables automatic redirect following so every hop can be budgeted and SSRF-checked."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _build_https_handler() -> urllib.request.HTTPSHandler:
    """Verify against certifi's CA bundle rather than the interpreter's OS-default trust store.

    `ssl.create_default_context()` with no `cafile` falls back to whatever `SSL_CERT_FILE` /
    `ssl.get_default_verify_paths()` resolves to on the host -- on a dev machine with several Python
    installs (e.g. an Anaconda env exporting `SSL_CERT_FILE`) that can point at a stale or unrelated CA
    bundle missing a current intermediate cert, so a perfectly valid, live company site fails with
    `CERTIFICATE_VERIFY_FAILED` and gets misclassified as `network_error` -- a real recall bug found by
    comparing this client's failures against `curl`/a plain `certifi`-backed context on the same host
    (freshwater.no and others: genuinely reachable, wrongly marked unreachable). `certifi` is already a
    pinned transitive dependency (via `requests`/`trafilatura`), so this needs no pyproject.toml change.
    """
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # pragma: no cover - certifi always present in this project's lockfile
        context = ssl.create_default_context()
    return urllib.request.HTTPSHandler(context=context)


_OPENER = urllib.request.build_opener(_NoRedirect(), _build_https_handler())


@dataclass
class RequestLogEntry:
    purpose: str
    org: str | None
    url: str
    status: int
    requests_used: int
    elapsed_ms: int
    error: str | None = None


class Budget:
    """Thread-safe global request budget with per-org allowances and shared-pool borrowing.

    `allocate(org, n)` grants a soft per-company allowance (the planner's tier estimate). `charge(org, n)`
    debits both the org's own usage and the global counter; it is allowed to exceed the org's allowance
    (borrowing from the shared pool) as long as the hard global cap is not exceeded. `remaining(org)`
    reports how much the org can still spend without the caller needing to know the borrowing rule.
    """

    RESERVED_PURPOSE_PREFIX = "registry_"

    def __init__(self, hard_cap: int = 1900):
        self.hard_cap = hard_cap
        self._lock = threading.Lock()
        self._used_total = 0
        self._org_allocated: dict[str | None, int] = {}
        self._org_used: dict[str | None, int] = {}
        # Requests held back for official registry calls, so optional sources can never starve them.
        self._reserved: dict[str | None, int] = {}
        self._reserved_total = 0

    def reserve(self, org: str | None, n: int) -> None:
        """Hold back n requests for `org`'s official registry calls (purpose prefix `registry_`)."""
        with self._lock:
            self._reserved[org] = self._reserved.get(org, 0) + n
            self._reserved_total += n

    def release(self, org: str | None) -> None:
        """Return whatever is left of an org's registry reserve to the shared pool."""
        with self._lock:
            self._reserved_total -= self._reserved.pop(org, 0)

    def allocate(self, org: str | None, n: int) -> None:
        with self._lock:
            self._org_allocated[org] = self._org_allocated.get(org, 0) + n

    def charge(self, org: str | None, n: int = 1, purpose: str = "") -> bool:
        with self._lock:
            own_reserve = self._reserved.get(org, 0) if purpose.startswith(self.RESERVED_PURPOSE_PREFIX) else 0
            ceiling = self.hard_cap - (self._reserved_total - own_reserve)
            if self._used_total + n > ceiling:
                return False
            if own_reserve:
                used_from_reserve = min(own_reserve, n)
                self._reserved[org] = own_reserve - used_from_reserve
                self._reserved_total -= used_from_reserve
            self._used_total += n
            self._org_used[org] = self._org_used.get(org, 0) + n
            return True

    def remaining(self, org: str | None = None) -> int:
        with self._lock:
            global_remaining = max(0, self.hard_cap - self._used_total - self._reserved_total)
            allocated = self._org_allocated.get(org)
            if allocated is None:
                return global_remaining
            org_remaining = allocated - self._org_used.get(org, 0)
            return max(0, min(org_remaining, global_remaining)) if org_remaining > 0 else global_remaining

    def used_total(self) -> int:
        with self._lock:
            return self._used_total

    def org_used(self, org: str | None) -> int:
        with self._lock:
            return self._org_used.get(org, 0)


def _utc_now() -> str:
    from .models import utc_now

    return utc_now()


def _sha256(body: bytes) -> str:
    from .models import sha256_text

    return sha256_text(body)


class BudgetedHttpClient:
    """Stdlib HttpClient implementation: manual redirects, retries, robots, SSRF guard, snapshots."""

    def __init__(
        self,
        budget: Budget,
        *,
        snapshots: SnapshotStore | None = None,
        dns_timeout: float = 3.0,
    ):
        self.budget = budget
        self.snapshots = snapshots
        self.dns_timeout = dns_timeout

        self._request_log: list[RequestLogEntry] = []
        self._log_lock = threading.Lock()

        self._host_semaphores: dict[str, threading.Semaphore] = {}
        self._host_sem_lock = threading.Lock()

        self._robots_cache: dict[tuple[str, str], urllib.robotparser.RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        self._robots_host_locks: dict[tuple[str, str], threading.Lock] = {}

        self._dns_cache: dict[str, bool] = {}
        self._dns_lock = threading.Lock()
        self._dns_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="signalpost-dns")

    # -- public API (context.HttpClient protocol) -------------------------------------------------

    def remaining(self, org: str | None = None) -> int:
        return self.budget.remaining(org)

    def dns_resolves(self, hostname: str) -> bool:
        with self._dns_lock:
            cached = self._dns_cache.get(hostname)
            if cached is not None:
                return cached
        ok = False
        try:
            future = self._dns_executor.submit(socket.getaddrinfo, hostname, None)
            future.result(timeout=self.dns_timeout)
            ok = True
        except Exception:
            ok = False
        with self._dns_lock:
            self._dns_cache[hostname] = ok
        return ok

    def get(
        self,
        url: str,
        *,
        org: str | None = None,
        purpose: str = "",
        accept: str = "*/*",
        max_bytes: int = 2_000_000,
        timeout: float = 10.0,
        respect_robots: bool = True,
        max_redirects: int = 4,
        snapshot: bool = True,
        headers: dict[str, str] | None = None,
    ) -> Response:
        # `headers` is an additive, backward-compatible extension beyond `context.HttpClient`'s declared
        # protocol (default None, every existing call site unaffected) - needed so a connector can send
        # `Authorization: Bearer <token>` to a keyless-but-token-gated public API (e.g. NAV's
        # pam-stilling-feed feedentry endpoint) without a broader interface change to context.py.
        return self._request(
            "GET", url, org=org, purpose=purpose, accept=accept, max_bytes=max_bytes, timeout=timeout,
            respect_robots=respect_robots, max_redirects=max_redirects, snapshot=snapshot, body=None,
            extra_headers=headers,
        )

    def post_json(
        self, url: str, payload: Any, *, org: str | None = None, purpose: str = "", timeout: float = 20.0
    ) -> Response:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._request(
            "POST", url, org=org, purpose=purpose, accept="application/json", max_bytes=2_000_000,
            timeout=timeout, respect_robots=False, max_redirects=0, snapshot=True, body=body,
        )

    @property
    def request_log(self) -> list[RequestLogEntry]:
        with self._log_lock:
            return list(self._request_log)

    # -- internals ----------------------------------------------------------------------------------

    def _log(self, entry: RequestLogEntry) -> None:
        with self._log_lock:
            self._request_log.append(entry)

    def _semaphore_for(self, host: str) -> threading.Semaphore:
        with self._host_sem_lock:
            sem = self._host_semaphores.get(host)
            if sem is None:
                sem = threading.Semaphore(HOST_CONCURRENCY_OVERRIDES.get(host, DEFAULT_HOST_CONCURRENCY))
                self._host_semaphores[host] = sem
            return sem

    def _fail(
        self, url: str, redirect_chain: list[str], error: str, *, org: str | None, purpose: str,
        status: int = 0, requests_used: int = 0, started: float | None = None, already_logged: int = 0,
    ) -> Response:
        elapsed_ms = int((time.monotonic() - started) * 1000) if started else 0
        self._log(RequestLogEntry(purpose=purpose, org=org, url=url, status=status, requests_used=requests_used - already_logged, elapsed_ms=elapsed_ms, error=error))
        return Response(
            url=url, final_url=redirect_chain[-1] if redirect_chain else url, redirect_chain=redirect_chain,
            status=status, headers={}, body=b"", retrieved_at=_utc_now(), content_sha256="",
            elapsed_ms=elapsed_ms, requests_used=requests_used, error=error,
        )

    def _robots_allowed(self, url: str, *, org: str | None, timeout: float) -> tuple[bool, int]:
        """Returns (allowed, requests_charged_for_robots_fetch)."""
        parsed = urllib.parse.urlparse(url)
        key = (parsed.scheme, parsed.hostname or "")
        with self._robots_lock:
            cached = self._robots_cache.get(key)
            if key in self._robots_cache:
                return (True if cached is None else cached.can_fetch(USER_AGENT, url)), 0
            host_lock = self._robots_host_locks.setdefault(key, threading.Lock())

        with host_lock:
            with self._robots_lock:
                if key in self._robots_cache:
                    cached = self._robots_cache[key]
                    return (True if cached is None else cached.can_fetch(USER_AGENT, url)), 0
            robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
            charged = 0
            parser: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            if not self.budget.charge(org, 1, purpose="robots"):
                # No budget to even check robots: fail closed is unhelpful, so cache "allow" but do not
                # charge (the caller's own GET charge, done next, is what will report budget_exhausted).
                with self._robots_lock:
                    self._robots_cache[key] = None
                return True, 0
            charged = 1
            started = time.monotonic()
            try:
                sem = self._semaphore_for(parsed.hostname or "")
                with sem:
                    request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT, "Accept": "text/plain"})
                    with _OPENER.open(request, timeout=timeout) as resp:
                        raw = resp.read(200_000)
                        status = resp.status
                parser.parse(raw.decode("utf-8", "replace").splitlines())
                elapsed_ms = int((time.monotonic() - started) * 1000)
                self._log(RequestLogEntry(purpose="robots", org=org, url=robots_url, status=status, requests_used=1, elapsed_ms=elapsed_ms))
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                self._log(RequestLogEntry(purpose="robots", org=org, url=robots_url, status=0, requests_used=1, elapsed_ms=elapsed_ms, error=type(exc).__name__))
                parser = None  # robots.txt unavailable: default to allow (same policy as the starter kit)
            with self._robots_lock:
                self._robots_cache[key] = parser
            return (True if parser is None else parser.can_fetch(USER_AGENT, url)), charged

    def _do_http(self, method: str, url: str, *, accept: str, timeout: float, max_bytes: int, body: bytes | None, extra_headers: dict[str, str] | None = None):
        """One raw HTTP attempt. Returns (status, headers, raw_body, final_url, error)."""
        headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "gzip"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with _OPENER.open(request, timeout=timeout) as resp:
                raw = resp.read(max_bytes + 1)
                return resp.status, dict(resp.headers.items()), raw, resp.geturl(), None
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read(max_bytes + 1)
            except Exception:
                pass
            return exc.code, dict(exc.headers.items()) if exc.headers else {}, raw, url, None
        except (TimeoutError, socket.timeout):
            return 0, {}, b"", url, "timeout"
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "not known" in reason or "nodename" in reason:
                error = "dns"
            elif isinstance(getattr(exc, "reason", None), ssl.SSLError) or "SSL" in reason or "certificate" in reason.lower():
                # A small, live Norwegian business site not uncommonly has a broken/expired cert, a
                # hostname mismatch (shared/default hosting cert), or a server that rejects modern TLS on
                # the bare domain while still serving plain HTTP fine -- genuinely reachable, real content,
                # just misconfigured TLS. Tagging this distinctly (not the generic "network_error" bucket)
                # lets `_request` retry once over plain http:// instead of silently dropping the candidate.
                error = "ssl_error"
            else:
                error = "network_error"
            return 0, {}, b"", url, error
        except Exception as exc:  # pragma: no cover - defensive
            return 0, {}, b"", url, f"{type(exc).__name__}: {str(exc)[:120]}"

    def _request(
        self, method: str, url: str, *, org: str | None, purpose: str, accept: str, max_bytes: int,
        timeout: float, respect_robots: bool, max_redirects: int, snapshot: bool, body: bytes | None,
        extra_headers: dict[str, str] | None = None,
    ) -> Response:
        started = time.monotonic()
        redirect_chain: list[str] = []
        current_url = url
        total_requests_used = 0
        hops = 0
        robots_logged = 0
        tried_http_fallback = False

        while True:
            redirect_chain.append(current_url)
            try:
                assert_public_url(current_url)
            except ValueError:
                return self._fail(url, redirect_chain, "blocked_ssrf", org=org, purpose=purpose, requests_used=total_requests_used, started=started, already_logged=robots_logged)

            if respect_robots:
                allowed, robots_charged = self._robots_allowed(current_url, org=org, timeout=timeout)
                total_requests_used += robots_charged
                robots_logged += robots_charged  # already logged by _robots_allowed under purpose "robots"
                if robots_charged and self.budget.remaining(org) < 0:
                    pass  # informational only; charge() already enforced the hard cap
                if not allowed:
                    return self._fail(url, redirect_chain, "robots_disallowed", org=org, purpose=purpose, requests_used=total_requests_used, started=started, already_logged=robots_logged)

            if not self.budget.charge(org, 1, purpose=purpose):
                return self._fail(url, redirect_chain, "budget_exhausted", org=org, purpose=purpose, requests_used=total_requests_used, started=started, already_logged=robots_logged)
            total_requests_used += 1

            parsed_host = urllib.parse.urlparse(current_url).hostname or ""
            sem = self._semaphore_for(parsed_host)
            hop_started = time.monotonic()
            with sem:
                status, headers, raw, final_hop_url, error = self._do_http(method, current_url, accept=accept, timeout=timeout, max_bytes=max_bytes, body=body, extra_headers=extra_headers)

            # Connection-level failures (TLS handshake reset, connection refused/reset) are usually
            # transient: a live run saw ~30% of registry calls fail this way for a few minutes.
            # A TLS error on a company website is usually a permanently broken certificate (the http://
            # fallback below handles it), so only official APIs (fetched without robots) retry it.
            connection_error = error == "network_error" or (error == "ssl_error" and not respect_robots)
            retryable = error == "timeout" or status == 429 or status >= 500 or connection_error
            if retryable:
                if not self.budget.charge(org, 1, purpose=purpose):
                    return self._fail(url, redirect_chain, "budget_exhausted", org=org, purpose=purpose, requests_used=total_requests_used, started=started, already_logged=robots_logged)
                total_requests_used += 1
                if connection_error:
                    time.sleep(1.0)
                elif status == 429 and not respect_robots:
                    # Official APIs: back off before retrying a rate limit (Retry-After when given,
                    # capped). Rate-limited optional sources (NAV search) have their own breaker.
                    time.sleep(_retry_after_seconds(headers))
                with sem:
                    status, headers, raw, final_hop_url, error = self._do_http(method, current_url, accept=accept, timeout=timeout, max_bytes=max_bytes, body=body, extra_headers=extra_headers)

            hop_elapsed_ms = int((time.monotonic() - hop_started) * 1000)

            # A live small-business (or occasionally larger) site can have a broken/expired cert, a
            # hostname-mismatched shared-hosting cert, or reject modern TLS on the bare domain entirely
            # while still serving plain HTTP fine -- genuinely reachable, real content. Downgrade to
            # http:// once, on the FIRST hop only (never mid-redirect-chain, and never more than once per
            # request), charged as one more budgeted request like the existing timeout/5xx retry above.
            # Scoped to `web_homepage` only (not secondary/sitemap/robots/official-registry purposes):
            # the homepage is the one page whose loss kills the whole candidate outright, so it is the
            # only place the extra request is worth spending -- a secondary page's own SSL failure just
            # means one fewer page of corroboration text, not a dead candidate, and official registry
            # APIs essentially never hit this. Keeps the fix's request-budget footprint small (a gold-set
            # measurement without this scoping pushed the batch over the request cap).
            if (
                error == "ssl_error" and not tried_http_fallback and len(redirect_chain) == 1
                and current_url.startswith("https://") and purpose == "web_homepage"
            ):
                tried_http_fallback = True
                if self.budget.charge(org, 1, purpose=purpose):
                    total_requests_used += 1
                    current_url = "http://" + current_url[len("https://"):]
                    redirect_chain.append(current_url)
                    with sem:
                        status, headers, raw, final_hop_url, error = self._do_http(method, current_url, accept=accept, timeout=timeout, max_bytes=max_bytes, body=body, extra_headers=extra_headers)
                    hop_elapsed_ms = int((time.monotonic() - hop_started) * 1000)

            if error:
                self._log(RequestLogEntry(purpose=purpose, org=org, url=current_url, status=status, requests_used=total_requests_used - robots_logged, elapsed_ms=hop_elapsed_ms, error=error))
                return Response(
                    url=url, final_url=current_url, redirect_chain=redirect_chain, status=status, headers=headers,
                    body=b"", retrieved_at=_utc_now(), content_sha256="", elapsed_ms=int((time.monotonic() - started) * 1000),
                    requests_used=total_requests_used, error=error,
                )

            lowered_headers = {k.lower(): v for k, v in headers.items()}
            if status in (301, 302, 303, 307, 308):
                location = lowered_headers.get("location")
                if location and hops < max_redirects:
                    current_url = urllib.parse.urljoin(current_url, location)
                    hops += 1
                    continue
                # No location, or redirect budget exhausted: treat as terminal (report the redirect itself).
                error_final = None if not location else "too_many_redirects"
                self._log(RequestLogEntry(purpose=purpose, org=org, url=current_url, status=status, requests_used=total_requests_used - robots_logged, elapsed_ms=hop_elapsed_ms, error=error_final))
                return Response(
                    url=url, final_url=current_url, redirect_chain=redirect_chain, status=status, headers=lowered_headers,
                    body=b"", retrieved_at=_utc_now(), content_sha256="", elapsed_ms=int((time.monotonic() - started) * 1000),
                    requests_used=total_requests_used, error=error_final,
                )
            break

        truncated_body = raw[:max_bytes]
        if lowered_headers.get("content-encoding", "").lower() == "gzip":
            try:
                truncated_body = gzip.decompress(truncated_body)
            except Exception:
                pass  # leave as-is if decompression fails on a truncated stream

        error_final = "http_4xx" if 400 <= status < 500 else ("http_5xx" if status >= 500 else None)
        content_sha256 = _sha256(truncated_body)
        retrieved_at = _utc_now()
        snapshot_ref = None
        if snapshot and self.snapshots is not None and 200 <= status < 300:
            snapshot_ref = self.snapshots.put(
                truncated_body, url=final_hop_url or current_url, retrieved_at=retrieved_at,
                content_type=lowered_headers.get("content-type"),
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._log(RequestLogEntry(purpose=purpose, org=org, url=current_url, status=status, requests_used=total_requests_used - robots_logged, elapsed_ms=hop_elapsed_ms, error=error_final))
        return Response(
            url=url, final_url=final_hop_url or current_url, redirect_chain=redirect_chain, status=status,
            headers=lowered_headers, body=truncated_body, retrieved_at=retrieved_at, content_sha256=content_sha256,
            elapsed_ms=elapsed_ms, requests_used=total_requests_used, error=error_final, snapshot_ref=snapshot_ref,
        )
