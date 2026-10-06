from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "signalpost"


@pytest.fixture(autouse=True)
def _no_bundled_nav_snapshot(monkeypatch):
    """Unit tests drive NAV through fake feeds; the bundled real-world snapshot would add real ads."""
    monkeypatch.setenv("SIGNALPOST_NAV_SNAPSHOT", "0")
