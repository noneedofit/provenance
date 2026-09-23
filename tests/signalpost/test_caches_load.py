from __future__ import annotations

import json
from pathlib import Path

from signalpost.caches import Caches
from signalpost.caches import aliases as aliases_mod
from signalpost.caches import email_domains as ed_mod
from signalpost.caches import wikidata as wd_mod

FIXTURES = Path(__file__).parent.parent / "fixtures" / "caches"


def test_load_empty_dir_is_none_safe(tmp_path: Path):
    caches = Caches.load(tmp_path)
    assert caches.email_domains is None
    assert caches.aliases is None
    assert caches.wikidata is None
    assert caches.nav is None
    assert caches.meta == {}


def test_load_partial_cache_dir_only_fills_built_parts(tmp_path: Path):
    ed_mod.build(FIXTURES / "enheter_sample.csv.gz", tmp_path)
    caches = Caches.load(tmp_path)
    assert caches.email_domains is not None
    assert caches.aliases is None
    assert caches.wikidata is None
    assert caches.nav is None


def test_load_full_cache_dir_and_nav_wiring(tmp_path: Path):
    ed_mod.build(FIXTURES / "enheter_sample.csv.gz", tmp_path)
    aliases_mod.build(FIXTURES / "underenheter_sample.csv.gz", tmp_path)
    rows = json.loads((FIXTURES / "wikidata_sample.json").read_text())["results"]["bindings"]
    wd_mod.build_from_rows(rows, tmp_path)

    # Build an empty but schema-valid nav.sqlite the way prepare's build_full would (skip the network
    # walk itself; just prove Caches.load wires .aliases onto .nav).
    from signalpost.caches import nav as nav_mod
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "nav.sqlite"))
    nav_mod._schema(conn)
    conn.commit()
    conn.close()

    caches = Caches.load(tmp_path)
    assert caches.email_domains is not None
    assert caches.aliases is not None
    assert caches.wikidata is not None
    assert caches.nav is not None
    assert caches.nav.aliases is caches.aliases

    # end-to-end sanity: identity resolution helpers actually usable together
    assert caches.email_domains.is_shared("sharedaccounting.no") is True
    assert caches.aliases.subunits("100000001")[0]["name"] == "HAGELAND HOKKSUND"
    assert caches.wikidata.lookup("974560569")["qid"] == "Q108025"
