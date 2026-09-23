from __future__ import annotations

from pathlib import Path

from signalpost.caches import email_domains as ed_mod
from signalpost.caches.email_domains import EmailDomains

FIXTURE = Path(__file__).parent.parent / "fixtures" / "caches" / "enheter_sample.csv.gz"


def test_registered_domain_from_email_and_url():
    assert ed_mod.registered_domain("post@example.no") == "example.no"
    assert ed_mod.registered_domain("Post@EXAMPLE.NO") == "example.no"
    assert ed_mod.registered_domain("https://www.example.co.uk/kontakt") == "example.co.uk"
    assert ed_mod.registered_domain("www.example.no") == "example.no"
    assert ed_mod.registered_domain("") is None
    assert ed_mod.registered_domain(None) is None
    assert ed_mod.registered_domain("not a domain") is None


def test_build_and_load(tmp_path: Path):
    info = ed_mod.build(FIXTURE, tmp_path)
    assert info["row_count"] == 7
    assert info["domain_count"] > 0
    assert (tmp_path / "email_domains.sqlite").exists()

    domains = EmailDomains.load(tmp_path)
    assert domains is not None

    # accountant domain used by 3 orgs -> shared
    assert domains.org_count("sharedaccounting.no") == 3
    assert domains.is_shared("sharedaccounting.no") is True
    assert domains.is_freemail("sharedaccounting.no") is False

    # gmail is always shared/freemail regardless of observed count
    assert domains.is_freemail("gmail.com") is True
    assert domains.is_shared("gmail.com") is True

    # a domain used by exactly one org is not shared
    assert domains.org_count("uniktdomene.no") == 1
    assert domains.is_shared("uniktdomene.no") is False

    # website_org_count tracks hjemmeside separately from epostadresse
    assert domains.website_org_count("uniktdomene.no") == 1
    assert domains.website_org_count("dips.com") == 1

    # unknown domain -> zero, never shared
    assert domains.org_count("neverseen.example") == 0
    assert domains.is_shared("neverseen.example") is False


def test_load_missing_cache_dir_returns_none(tmp_path: Path):
    assert EmailDomains.load(tmp_path / "does-not-exist") is None
