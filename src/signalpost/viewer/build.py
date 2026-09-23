"""Build the static Signalpost viewer site from an envelopes.jsonl file.

Usage:
    python -m signalpost.viewer.build --envelopes out/envelopes.jsonl --out out/site [--state-dir state]

Produces a fully static, self-contained site: no CDN, no external fonts/scripts. Works from a plain
file:// URL and from any static file host. Company pages are pre-rendered HTML (so the directory scales
to 1,000+ companies without per-page fetches); the directory page embeds one compact JSON row per
company inline for client-side search/filter/sort/compare, so nothing needs to be fetched at load time.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from signalpost.models import Envelope

from .render import build_row, render_company_page, render_directory_csv, render_index_page, render_compare_page

ASSETS_DIR = Path(__file__).parent / "assets"


def load_envelopes(path: Path) -> list[dict]:
    envelopes = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                env = Envelope.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - surface with line context, keep other lines building
                print(f"warning: skipping invalid envelope at line {lineno}: {exc}", file=sys.stderr)
                continue
            envelopes.append(env.model_dump(mode="json"))
    return envelopes


def build_site(envelopes_path: Path, out_dir: Path, state_dir: Path | None = None) -> None:
    envelopes = load_envelopes(envelopes_path)
    if not envelopes:
        raise SystemExit(f"no valid envelopes found in {envelopes_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "assets").mkdir(exist_ok=True)
    (out_dir / "companies").mkdir(exist_ok=True)
    (out_dir / "data" / "companies").mkdir(parents=True, exist_ok=True)

    for asset in ("style.css", "app.js"):
        shutil.copyfile(ASSETS_DIR / asset, out_dir / "assets" / asset)

    rows = []
    for env in sorted(envelopes, key=lambda e: e.get("legal_name") or e["organisation_number"]):
        org = env["organisation_number"]
        row = build_row(env, f"companies/{org}.html")
        rows.append(row)
        (out_dir / "companies" / f"{org}.html").write_text(render_company_page(env), encoding="utf-8")
        (out_dir / "data" / "companies" / f"{org}.json").write_text(
            json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    (out_dir / "index.html").write_text(render_index_page(rows), encoding="utf-8")
    (out_dir / "compare.html").write_text(render_compare_page(rows), encoding="utf-8")
    (out_dir / "directory.csv").write_text(render_directory_csv(rows), encoding="utf-8")
    (out_dir / "data" / "index.json").write_text(
        json.dumps({"companies": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"built {len(rows)} company pages -> {out_dir}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--envelopes", required=True, type=Path, help="Path to envelopes.jsonl")
    parser.add_argument("--out", required=True, type=Path, help="Output directory for the static site")
    parser.add_argument("--state-dir", type=Path, default=None, help="Unused by the viewer; accepted for CLI parity")
    args = parser.parse_args(argv)
    build_site(args.envelopes, args.out, args.state_dir)


if __name__ == "__main__":
    main()
