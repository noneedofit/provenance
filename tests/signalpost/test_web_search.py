import json

from signalpost.web import search
from signalpost.web.candidates import registered_domain


class FakeResponse:
    def __init__(self, payload, ok=True):
        self.ok = ok
        self.body = json.dumps(payload).encode()


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payload)


PAYLOAD = {"web": {"results": [
    {"url": "https://www.proff.no/selskap/gemma-as/bergen/123"},
    {"url": "https://www.dingullsmed.no/om-oss"},
    {"url": "https://dingullsmed.no/kontakt"},
    {"url": "https://www.facebook.com/dinGullsmed.no"},
    {"url": "https://aseco.no/butikker"},
    {"url": "ftp://files.example.no/x"},
]}}


def test_no_key_means_no_request(monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    client = FakeClient(PAYLOAD)
    assert search.search_candidates(client, "983594891", "GEMMA AS", "BERGEN", registered_domain) == []
    assert client.calls == []


def test_results_become_deduplicated_candidates_without_directories(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALPOST_SEARCH_RPS", "1000")
    search.reset_for_tests()
    client = FakeClient(PAYLOAD)
    found = search.search_candidates(client, "983594891", "GEMMA AS", "BERGEN", registered_domain)
    assert found == [("dingullsmed.no", "https://www.dingullsmed.no/"), ("aseco.no", "https://aseco.no/")]
    url, kwargs = client.calls[0]
    assert "test-key" not in url  # the key travels only in a header
    assert kwargs["headers"]["X-Subscription-Token"] == "test-key"
    assert "%22GEMMA%22" in url and "Bergen" in url  # legal-form suffix dropped, name quoted


def test_search_cap_stops_further_requests(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    monkeypatch.setenv("SIGNALPOST_SEARCH_RPS", "1000")
    monkeypatch.setenv("SIGNALPOST_SEARCH_MAX", "1")
    search.reset_for_tests()
    client = FakeClient(PAYLOAD)
    search.search_candidates(client, "1", "A AS", None, registered_domain)
    assert search.search_candidates(client, "2", "B AS", None, registered_domain) == []
    assert len(client.calls) == 1
