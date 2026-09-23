from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eval import splits  # noqa: E402


def _make_universe(path: Path, n: int = 1200) -> None:
    forms = ["AS", "ASA", "NUF", "BRL", "ENK", "SAM"]
    names = [
        "SANDNES ELEKTRISKE AS", "7-ELEVEN NORGE AS", "COOP MEGA BUTIKK AS", "NORDIC CONSULTING GRUPPEN AS",
        "ALSTRAY AS", "HANSEN BYGG SERVICE AS",
    ]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for i in range(n):
            row = {
                "organisation_number": f"{900000000 + i}",
                "name": f"{names[i % len(names)]} {i}",
                "legal_form": forms[i % len(forms)],
                "employees": None if i % 5 else (i % 60),
                "industry_code": "43.210",
                "industry_label": "Test",
                "municipality": "OSLO",
                "municipality_number": "0301",
                "website": "example.no" if i % 7 == 0 else "",
                "bankrupt": False,
                "liquidating": False,
                "latest_submitted_accounts": "2025",
            }
            handle.write(json.dumps(row) + "\n")


def test_build_splits_sizes_and_no_overlap(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.jsonl.gz"
    _make_universe(universe_path)

    result = splits.build_splits(universe_path)

    assert len(result["dev"]) == splits.DEV_SIZE
    assert len(result["val"]) == splits.VAL_SIZE
    assert len(result["test"]) == splits.TEST_SIZE
    assert len(result["stress"]) <= splits.STRESS_SIZE
    assert len(result["stress"]) > 0

    org_sets = {name: {r["organisation_number"] for r in rows} for name, rows in result.items()}
    names = list(org_sets)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not (org_sets[a] & org_sets[b]), f"overlap between {a} and {b}"

    for row in result["stress"]:
        assert "stress_bucket" in row and row["stress_bucket"]


def test_build_splits_is_deterministic(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.jsonl.gz"
    _make_universe(universe_path)

    first = splits.build_splits(universe_path)
    second = splits.build_splits(universe_path)

    for name in first:
        first_orgs = [r["organisation_number"] for r in first[name]]
        second_orgs = [r["organisation_number"] for r in second[name]]
        assert first_orgs == second_orgs


def test_daily_like_uniform_and_deterministic(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.jsonl.gz"
    _make_universe(universe_path, n=400)

    batch_a = splits.daily_like(universe_path, seed=1, size=100)
    batch_a_again = splits.daily_like(universe_path, seed=1, size=100)
    batch_b = splits.daily_like(universe_path, seed=2, size=100)

    assert len(batch_a) == 100
    assert [r["organisation_number"] for r in batch_a] == [r["organisation_number"] for r in batch_a_again]
    assert batch_a != batch_b


def test_read_write_jsonl_roundtrip(tmp_path: Path) -> None:
    rows = [{"organisation_number": "1", "name": "A"}, {"organisation_number": "2", "name": "B"}]
    path = tmp_path / "out.jsonl"
    splits.write_jsonl(rows, path)
    assert splits.read_jsonl(path) == rows
    assert splits.read_jsonl(tmp_path / "missing.jsonl") == []
