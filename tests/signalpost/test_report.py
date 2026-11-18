from __future__ import annotations

from types import SimpleNamespace

from signalpost.report import run_health


def _env(identity: str = "available", website: str = "not_available", reason: str | None = None):
    fam = lambda availability, why=None: SimpleNamespace(availability=availability, reason=why)  # noqa: E731
    return SimpleNamespace(
        families={"identity": fam(identity), "website": fam(website), "profiles": fam("not_available"),
                  "activity": fam("not_applicable", reason), "jobs": fam("not_applicable")},
        errors=[],
    )


def _log(error: str | None, n: int):
    return [SimpleNamespace(error=error) for _ in range(n)]


def test_a_healthy_run_is_not_degraded():
    envs = [_env(website="available")] + [_env() for _ in range(99)]
    health = run_health(envs, _log(None, 900) + _log("http_4xx", 50), deadline_hits=0, budget_exhausted_count=0)
    assert health["degraded"] is False and health["reasons"] == []
    assert health["external_family_companies"]["website"] == 1


def test_failing_registry_lookups_and_connections_flag_the_run():
    envs = [_env(identity="failed") for _ in range(10)] + [_env() for _ in range(90)]
    health = run_health(envs, _log(None, 500) + _log("dns", 200), deadline_hits=0, budget_exhausted_count=3)
    assert health["degraded"] is True
    assert health["identity_failed_companies"] == 10
    assert any("registry lookup failed for 10 of 100" in r for r in health["reasons"])
    assert any("request budget" in r for r in health["reasons"])
    assert any("connection level" in r for r in health["reasons"])


def test_registry_only_pass_is_counted():
    envs = [_env(reason="time_budget: registry-only pass")] + [_env() for _ in range(9)]
    health = run_health(envs, _log(None, 10), deadline_hits=0, budget_exhausted_count=0)
    assert health["registry_only_companies"] == 1 and health["degraded"] is True
