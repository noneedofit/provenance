from __future__ import annotations

import json
from pathlib import Path

from signalpost.caches import wikidata as wd_mod
from signalpost.caches.wikidata import Wikidata

FIXTURE = Path(__file__).parent.parent / "fixtures" / "caches" / "wikidata_sample.json"


def _load_bindings():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["results"]["bindings"]


def test_build_from_rows_and_lookup(tmp_path: Path):
    rows = _load_bindings()
    info = wd_mod.build_from_rows(rows, tmp_path)
    assert info["row_count"] == 2
    assert info["license"] == "CC0"
    assert (tmp_path / "wikidata.sqlite").exists()

    wd = Wikidata.load(tmp_path)
    assert wd is not None

    sarpsborg = wd.lookup("974560569")
    assert sarpsborg is not None
    assert sarpsborg["qid"] == "Q108025"
    assert sarpsborg["label"] == "Sarpsborg"
    assert "https://www.sarpsborg.com/" in sarpsborg["websites"]
    assert sarpsborg["profiles"]["facebook"] == "https://www.facebook.com/Sarpsborgkommune"
    assert sarpsborg["profiles"]["instagram"] == "https://www.instagram.com/sarpsborgkommune/"
    assert sarpsborg["profiles"]["x"] == "https://x.com/sarpsborgkomm"
    assert sarpsborg["profiles"]["linkedin"] == "https://www.linkedin.com/company/sarpsborg-kommune"
    assert sarpsborg["profiles"]["youtube"] == "https://www.youtube.com/channel/UCbJ92O5dwbkZVw3YdzPe2Pw"
    assert sarpsborg["source_url"] == "https://www.wikidata.org/wiki/Q108025"

    fjord1 = wd.lookup("983472583")
    assert fjord1 is not None
    assert "facebook" not in fjord1["profiles"]
    assert fjord1["profiles"]["instagram"] == "https://www.instagram.com/fjord1as/"


def test_lookup_normalizes_spaces(tmp_path: Path):
    wd_mod.build_from_rows(_load_bindings(), tmp_path)
    wd = Wikidata.load(tmp_path)
    assert wd is not None
    assert wd.lookup("974 560 569") is not None


def test_lookup_unknown_org_returns_none(tmp_path: Path):
    wd_mod.build_from_rows(_load_bindings(), tmp_path)
    wd = Wikidata.load(tmp_path)
    assert wd is not None
    assert wd.lookup("000000000") is None


def test_load_missing_cache_dir_returns_none(tmp_path: Path):
    assert Wikidata.load(tmp_path / "does-not-exist") is None
