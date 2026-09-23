from __future__ import annotations

from pathlib import Path

from signalpost.caches import aliases as aliases_mod
from signalpost.caches.aliases import Aliases, clean_subunit_name

FIXTURE = Path(__file__).parent.parent / "fixtures" / "caches" / "underenheter_sample.csv.gz"


def test_clean_subunit_name_drops_pure_parent_duplicate():
    assert clean_subunit_name("DIPS AS AVD BERGEN", "DIPS AS") is None
    assert clean_subunit_name("DIPS AS", "DIPS AS") is None


def test_clean_subunit_name_keeps_real_brand_alias():
    cleaned = clean_subunit_name("HAGELAND HOKKSUND", "EIKER HAGESENTER AS")
    assert cleaned == "HAGELAND HOKKSUND"


def test_clean_subunit_name_empty():
    assert clean_subunit_name("", "DIPS AS") is None


def test_build_and_subunits(tmp_path: Path):
    info = aliases_mod.build(FIXTURE, tmp_path)
    assert info["row_count"] == 3
    assert (tmp_path / "aliases.sqlite").exists()

    a = Aliases.load(tmp_path)
    assert a is not None

    dips_subs = a.subunits("100000002")
    assert len(dips_subs) == 2
    names = {s["name"] for s in dips_subs}
    assert names == {"DIPS AS AVD BERGEN", "DIPS AS"}

    hagesenter_subs = a.subunits("100000001")
    assert len(hagesenter_subs) == 1
    assert hagesenter_subs[0]["website"] == "www.hageland.no"
    assert hagesenter_subs[0]["employees"] == 10

    # no subunits for an unknown org
    assert a.subunits("999999999") == []


def test_names_cleans_and_dedupes(tmp_path: Path):
    aliases_mod.build(FIXTURE, tmp_path)
    a = Aliases.load(tmp_path)
    assert a is not None

    # DIPS's subunits add no brand information beyond the parent name
    assert a.names("100000002", parent_name="DIPS AS") == []

    # Eiker Hagesenter's subunit is a real, distinct brand
    assert a.names("100000001", parent_name="EIKER HAGESENTER AS") == ["HAGELAND HOKKSUND"]


def test_load_missing_cache_dir_returns_none(tmp_path: Path):
    assert Aliases.load(tmp_path / "does-not-exist") is None
