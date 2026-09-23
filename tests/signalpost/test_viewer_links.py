from __future__ import annotations

import os
import re
from pathlib import Path

from signalpost.viewer import build

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "viewer"


def test_every_internal_link_resolves(tmp_path):
    for name in ("envelopes.jsonl", "real_registry.jsonl"):
        out = tmp_path / name
        build.build_site(FIXTURES / name, out)
        broken = []
        for page in out.rglob("*.html"):
            for href in re.findall(r'(?:href|src)="([^"#?]+)', page.read_text(encoding="utf-8")):
                if href.startswith(("http", "mailto:", "data:", "javascript:")):
                    continue
                if not os.path.exists(os.path.normpath(page.parent / href)):
                    broken.append((page.name, href))
        assert broken == [], broken
