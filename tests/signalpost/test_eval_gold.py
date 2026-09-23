from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eval.gold import GoldJobs, GoldProfile, GoldRecord, GoldWebsite, load_gold, normalize_url, summarize, write_gold  # noqa: E402


def _record(org: str, status: str = "exact", url: str | None = "https://example.no/") -> GoldRecord:
    return GoldRecord(
        organisation_number=org,
        name=f"COMPANY {org}",
        website=GoldWebsite(status=status, url=url, evidence_url=url, evidence_note="found org number in footer"),
        profiles=[GoldProfile(platform="linkedin", url="https://www.linkedin.com/company/example")],
        jobs=GoldJobs(checked=True, active=True, note="2 ads on arbeidsplassen.nav.no"),
        confidence="high",
        labelled_at="2026-09-23",
    )


def test_normalize_url_strips_scheme_www_and_trailing_slash() -> None:
    assert normalize_url("https://www.Example.no/") == "example.no"
    assert normalize_url("http://example.no") == "example.no"
    assert normalize_url("example.no/") == "example.no"


def test_write_and_load_gold_roundtrip(tmp_path: Path) -> None:
    records = [_record("111"), _record("222", status="none_found", url=None)]
    path = tmp_path / "gold.jsonl"
    write_gold(records, path)

    loaded = load_gold([path])
    assert set(loaded) == {"111", "222"}
    assert loaded["111"].website.status == "exact"
    assert loaded["222"].website.url is None
    assert loaded["111"].website_key() == "example.no"


def test_load_gold_merges_multiple_files_last_wins(tmp_path: Path) -> None:
    base = tmp_path / "base.jsonl"
    correction = tmp_path / "correction.jsonl"
    write_gold([_record("111", status="exact")], base)
    write_gold([_record("111", status="none_found", url=None)], correction)

    merged = load_gold([base, correction])
    assert merged["111"].website.status == "none_found"


def test_summarize_counts_status_and_jobs() -> None:
    records = {
        "1": _record("1", status="exact"),
        "2": _record("2", status="none_found", url=None),
        "3": _record("3", status="related"),
    }
    summary = summarize(records)
    assert summary["total_labelled"] == 3
    assert summary["website_status_counts"]["exact"] == 1
    assert summary["website_status_counts"]["none_found"] == 1
    assert summary["website_status_counts"]["related"] == 1
    assert summary["jobs_checked"] == 3
    assert summary["jobs_active"] == 3
