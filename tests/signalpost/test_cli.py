

def test_run_builds_shared_domain_cache_when_none_supplied(tmp_path, monkeypatch):
    from signalpost import cli

    monkeypatch.setenv("SIGNALPOST_WIKIDATA_LIVE", "1")

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

    monkeypatch.setenv("SIGNALPOST_WIKIDATA_LIVE", "1")
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


def test_a_slow_bulk_download_stops_at_its_time_budget(tmp_path, monkeypatch):
    from signalpost import cli

    monkeypatch.chdir(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cli.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))

    class _Trickle:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            clock[0] += 50.0  # one megabyte every 50 seconds
            return b"x" * 1024

    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda *a, **k: _Trickle())
    path, used = cli._resolve_bulk_path(None, time_budget_s=600)
    assert path is None
    assert clock[0] <= 700  # gave up near the budget instead of retrying from zero three times
    assert not list((tmp_path / "data").glob("*.part"))


def test_missing_bulk_uses_bundled_shared_domain_table(tmp_path, monkeypatch):
    from signalpost import cli
    import signalpost.caches.wikidata as wd

    monkeypatch.setattr(wd, "build", lambda d: (d / "wikidata.sqlite").write_text("x") and {})
    cache_dir, _ = cli._ensure_caches(str(tmp_path / "c"), None)
    assert (tmp_path / "c" / "email_domains.sqlite").stat().st_size > 1_000_000


def test_small_plain_csv_bulk_file_is_accepted_not_deleted(tmp_path):
    from signalpost import cli

    small = tmp_path / "brreg-enheter.csv"
    small.write_text('"organisasjonsnummer","navn"\n"123456785","TEST AS"\n', encoding="utf-8")
    assert cli._bulk_file_ok(small) and small.exists()


def test_wikidata_uses_bundled_snapshot_by_default_without_requests(tmp_path, monkeypatch):
    from signalpost import cli
    import signalpost.caches.email_domains as ed
    import signalpost.caches.wikidata as wd

    monkeypatch.delenv("SIGNALPOST_WIKIDATA_LIVE", raising=False)
    monkeypatch.setattr(ed, "build", lambda bulk, d: (d / "email_domains.sqlite").write_text("x") and {})

    def live_query_must_not_run(cache_dir):
        raise AssertionError("live Wikidata query in default mode")

    monkeypatch.setattr(wd, "build", live_query_must_not_run)
    cache_dir, used = cli._ensure_caches(str(tmp_path / "c"), "bulk.csv")
    assert used == 0 and (tmp_path / "c" / "wikidata.sqlite").stat().st_size > 100_000


def test_run_output_folder_includes_the_viewer(tmp_path, monkeypatch, capsys):
    import json
    from pathlib import Path

    from signalpost import cli, pipeline

    envelope = Path("smoke-test/envelopes.jsonl").read_text(encoding="utf-8").splitlines()[0]
    out = tmp_path / "out"

    def fake_run_batch(orgs, output_dir, **kwargs):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        (Path(output_dir) / "envelopes.jsonl").write_text(envelope + "\n", encoding="utf-8")
        report = {"total_requests": 0}
        (Path(output_dir) / "run-report.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    monkeypatch.setattr(cli, "_resolve_bulk_path", lambda explicit, budget: (None, 0))
    monkeypatch.setattr(cli, "_ensure_caches", lambda explicit, bulk: (None, 0))
    monkeypatch.setattr(pipeline, "run_batch", fake_run_batch)
    orgs = tmp_path / "orgs.txt"
    orgs.write_text(json.loads(envelope)["organisation_number"] + "\n", encoding="utf-8")
    assert cli.main(["run", "--organisations", str(orgs), "--output-dir", str(out),
                     "--state-dir", str(tmp_path / "state"), "--run-id", "t"]) == 0
    assert (out / "site" / "index.html").exists() and (out / "site" / "compare.html").exists()
    report = json.loads(capsys.readouterr().out)  # stdout stays pure report JSON
    assert report["viewer"] == "site/index.html"
    assert json.loads((out / "run-report.json").read_text(encoding="utf-8"))["viewer"] == "site/index.html"
