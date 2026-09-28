

def test_run_builds_shared_domain_cache_when_none_supplied(tmp_path, monkeypatch):
    from signalpost import cli

    built = {}

    def fake_email_build(bulk, cache_dir):
        (cache_dir / "email_domains.sqlite").write_text("x")
        built["email"] = bulk
        return {"row_count": 1}

    def fake_wikidata_build(cache_dir):
        (cache_dir / "wikidata.sqlite").write_text("x")
        return {"raw_binding_count": 11000}

    import signalpost.caches.email_domains as ed
    import signalpost.caches.wikidata as wd
    monkeypatch.setattr(ed, "build", fake_email_build)
    monkeypatch.setattr(wd, "build", fake_wikidata_build)
    cache_dir, used = cli._ensure_caches(str(tmp_path / "cache"), "bulk.csv")
    assert built["email"] == "bulk.csv" and used == 1
    assert (tmp_path / "cache" / "email_domains.sqlite").exists()
    # Second call reuses what exists: no rebuild, no requests.
    built.clear()
    assert cli._ensure_caches(cache_dir, "bulk.csv") == (cache_dir, 0) and not built


def test_run_falls_back_to_bundled_wikidata_snapshot(tmp_path, monkeypatch):
    from signalpost import cli
    import signalpost.caches.email_domains as ed
    import signalpost.caches.wikidata as wd

    monkeypatch.setattr(ed, "build", lambda bulk, d: (d / "email_domains.sqlite").write_text("x") and {})
    def boom(cache_dir):
        raise RuntimeError("HTTP Error 429")
    monkeypatch.setattr(wd, "build", boom)
    cache_dir, used = cli._ensure_caches(str(tmp_path / "c"), "bulk.csv")
    assert used == 1
    assert (tmp_path / "c" / "wikidata.sqlite").stat().st_size > 100_000


def _gzip_file(path, n_bytes):
    import gzip, os
    with gzip.open(path, "wb") as f:
        f.write(os.urandom(n_bytes))


def test_bulk_file_check_rejects_a_truncated_download(tmp_path):
    from signalpost import cli

    good = tmp_path / "good.csv"
    _gzip_file(good, 1_500_000)
    assert cli._bulk_file_ok(good)
    bad = tmp_path / "bad.csv"
    bad.write_bytes(good.read_bytes()[:-30_000])  # IncompleteRead-style truncation
    assert not cli._bulk_file_ok(bad)


def test_failed_bulk_download_never_crashes_the_run(tmp_path, monkeypatch):
    from signalpost import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "brreg-enheter.csv").write_bytes(b"\x1f\x8b" + b"x" * 10)  # corrupt leftover

    def boom(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    path, used = cli._resolve_bulk_path(None)
    assert path is None and used == cli.BULK_DOWNLOAD_ATTEMPTS
    assert not (tmp_path / "data" / "brreg-enheter.csv").exists()


def test_missing_bulk_uses_bundled_shared_domain_table(tmp_path, monkeypatch):
    from signalpost import cli
    import signalpost.caches.wikidata as wd

    monkeypatch.setattr(wd, "build", lambda d: (d / "wikidata.sqlite").write_text("x") and {})
    cache_dir, _ = cli._ensure_caches(str(tmp_path / "c"), None)
    assert (tmp_path / "c" / "email_domains.sqlite").stat().st_size > 1_000_000
