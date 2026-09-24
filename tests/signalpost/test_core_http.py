from __future__ import annotations

import http.server
import threading
import time

import pytest

from signalpost import http as sp_http
from signalpost.http import Budget, BudgetedHttpClient


def test_budget_allocate_charge_remaining():
    budget = Budget(hard_cap=10)
    budget.allocate("org1", 5)
    assert budget.remaining("org1") == 5
    assert budget.charge("org1", 3) is True
    assert budget.remaining("org1") == 2
    assert budget.used_total() == 3


def test_budget_borrows_from_shared_pool_up_to_hard_cap():
    budget = Budget(hard_cap=5)
    budget.allocate("org1", 2)
    assert budget.charge("org1", 2) is True
    # org1's own allowance is used up, but the hard cap still has room: borrowing succeeds
    assert budget.charge("org1", 2) is True
    assert budget.used_total() == 4
    # now the hard cap itself is nearly exhausted
    assert budget.charge("org2", 2) is False  # would exceed hard_cap=5
    assert budget.charge("org2", 1) is True
    assert budget.used_total() == 5


def test_budget_unknown_org_reports_global_remaining():
    budget = Budget(hard_cap=10)
    budget.charge(None, 4)
    assert budget.remaining("never-allocated") == 6


class _Handler(http.server.BaseHTTPRequestHandler):
    hit_counts: dict[str, int] = {}

    def log_message(self, format, *args):  # noqa: A002 - silence test server logging
        pass

    def do_GET(self):
        _Handler.hit_counts[self.path] = _Handler.hit_counts.get(self.path, 0) + 1
        if self.path == "/redirect-a":
            self.send_response(302)
            self.send_header("Location", "/redirect-b")
            self.end_headers()
        elif self.path == "/redirect-b":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        elif self.path == "/flaky":
            if _Handler.hit_counts[self.path] == 1:
                self.send_response(503)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
        elif self.path == "/robots.txt":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"User-agent: *\nDisallow: /blocked\n")
        elif self.path == "/blocked":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"should not be fetched")
        elif self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"x" * 1000)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _allow_localhost(monkeypatch):
    # The real SSRF guard blocks loopback addresses; the test server is loopback-only.
    monkeypatch.setattr(sp_http, "assert_public_url", lambda url: None)


def test_redirect_hops_all_count_against_budget(server):
    port = server.server_address[1]
    budget = Budget(hard_cap=100)
    client = BudgetedHttpClient(budget)
    resp = client.get(f"http://127.0.0.1:{port}/redirect-a", org="orgA", purpose="test", respect_robots=False)
    assert resp.ok
    assert resp.status == 200
    assert resp.requests_used == 2  # one hop for /redirect-a, one for /redirect-b
    assert len(resp.redirect_chain) == 2
    assert budget.used_total() == 2


def test_retry_on_5xx_is_counted(server):
    port = server.server_address[1]
    budget = Budget(hard_cap=100)
    client = BudgetedHttpClient(budget)
    resp = client.get(f"http://127.0.0.1:{port}/flaky", org="orgB", purpose="test", respect_robots=False)
    assert resp.ok
    assert resp.requests_used == 2  # first attempt (503) + retry (200)


def test_budget_exhausted_returns_error_response(server):
    port = server.server_address[1]
    budget = Budget(hard_cap=0)
    client = BudgetedHttpClient(budget)
    resp = client.get(f"http://127.0.0.1:{port}/redirect-b", org="orgC", purpose="test", respect_robots=False)
    assert resp.error == "budget_exhausted"
    assert not resp.ok


def test_robots_txt_blocks_disallowed_path(server):
    port = server.server_address[1]
    budget = Budget(hard_cap=100)
    client = BudgetedHttpClient(budget)
    resp = client.get(f"http://127.0.0.1:{port}/blocked", org="orgD", purpose="test", respect_robots=True)
    assert resp.error == "robots_disallowed"
    # robots.txt fetch itself is charged
    assert budget.used_total() >= 1


def test_max_bytes_truncates_body(server):
    port = server.server_address[1]
    budget = Budget(hard_cap=100)
    client = BudgetedHttpClient(budget)
    resp = client.get(f"http://127.0.0.1:{port}/big", org="orgE", purpose="test", respect_robots=False, max_bytes=100)
    assert resp.ok
    assert len(resp.body) <= 100


def test_dns_resolves_caches_result():
    budget = Budget(hard_cap=10)
    client = BudgetedHttpClient(budget)
    assert client.dns_resolves("localhost") is True
    assert "localhost" in client._dns_cache


def test_registry_reserve_cannot_be_spent_by_optional_sources():
    from signalpost.http import Budget

    budget = Budget(hard_cap=10)
    budget.reserve("A", 3)
    budget.reserve("B", 3)
    spent = sum(budget.charge("C", 1, purpose="nav_live_search") for _ in range(10))
    assert spent == 4  # 10 - 6 reserved
    assert budget.charge("A", 1, purpose="registry_entity")
    assert budget.charge("B", 1, purpose="registry_roles")
    assert not budget.charge("C", 1, purpose="web_homepage")
    budget.release("A")  # A's 2 unused reserve go back to the pool
    assert budget.charge("C", 1, purpose="web_homepage")


def test_request_log_total_matches_budget_with_robots_and_redirects(server):
    # The run report sums the request log; robots.txt fetches must not be counted twice.
    port = server.server_address[1]
    budget = Budget(hard_cap=100)
    client = BudgetedHttpClient(budget)
    client.get(f"http://127.0.0.1:{port}/redirect-a", org="orgE", purpose="test", respect_robots=True)
    client.get(f"http://127.0.0.1:{port}/blocked", org="orgE", purpose="test", respect_robots=True)
    client.get(f"http://127.0.0.1:{port}/flaky", org="orgE", purpose="test", respect_robots=True)
    log = client.request_log() if callable(client.request_log) else client.request_log
    assert sum(e.requests_used for e in log) == budget.used_total()
