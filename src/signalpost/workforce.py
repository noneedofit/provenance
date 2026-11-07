"""Workforce from the company's latest annual-accounts copy (Regnskapsregisteret, data.brreg.no).

Norwegian annual accounts state the workforce in the notes, usually on the payroll note: "Antall årsverk i
regnskapsåret: 8,40" (full-time equivalents) or "Gjennomsnittlig antall ansatte: 12", and small companies write
"Selskapet har ikke hatt ansatte" (no employees). The register already gives a headcount for some companies;
this reads the figure the company itself filed, for the year of its latest accounts.

The official copies are image scans of a clean typewriter-style print, so the text is read with Tesseract OCR
(page images rendered with Poppler's pdftoppm); a PDF that carries a text layer is read with pdftotext first.
These are system tools, not Python packages: when they are not installed the connector publishes nothing and
says so once on stderr (install: `brew install tesseract poppler` or `apt-get install tesseract-ocr poppler-utils`).
SIGNALPOST_WORKFORCE=0 turns the step off.

One request per company (the PDF). OCR runs a few pages, payroll-note pages first, at 100 dpi, and stops at the
first page with an unambiguous statement for the company itself (group / subsidiary figures are skipped).
The number of OCR processes running at once is capped (half the CPUs) so OCR never starves the rest of the run,
and the step is skipped when the run is behind pace for its deadline (see pipeline `on_pace`), close to its
request cap, or when no OCR slot frees up within a minute.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from .activity._common import make_claim, make_evidence
from .context import CompanyContext
from .models import ConnectorResult

PDF_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}"
# Pages to read, in order: in the register's copies the payroll note is on page 5 or 6 for nine companies in ten.
PAGE_ORDER = (5, 6, 4, 7, 8, 3)
DPI = 100
MAX_PDF_BYTES = 15_000_000
# Skip OCR when the run has less than this many seconds left before its deadline.
MIN_TIME_LEFT_S = 420
# Skip the PDF when the whole run has fewer requests than this left: the rest go to websites, news and jobs.
MIN_RUN_REQUESTS_LEFT = 100
# Longest wait for a free OCR slot (a safety net; the pace check in the pipeline is what protects the deadline).
SLOT_WAIT_S = 60.0
OCR_SLOTS = max(1, (os.cpu_count() or 2) // 2)
EXTRACTION_METHOD = "annual_accounts_ocr_v1"

_slots = threading.BoundedSemaphore(OCR_SLOTS)
_tools_lock = threading.Lock()
_tools: dict[str, Any] = {}

_NUM = r"([0-9oO][0-9oO .,]{0,7})"
_V = r"a{1,2}r\s?s\s?v\s?e\s?r\s?k"  # årsverk, folded, allowing OCR-inserted spaces
_YEAR = r"(?:\s*i\s*(?:regnskapsaret|rekneskapsaret|aret)(?:\s*20\d\d)?)?"
# (priority, measure, pattern): full-time equivalents are preferred over a headcount when both are stated.
_PATTERNS = [
    (0, "fte", re.compile(r"(?:antall|tal\s*pa|tall\s*pa)\s*" + _V + r"\s*(?:sysselsatt\s*)?" + _YEAR + r"\s*(?:er|var|:|=)?\s*" + _NUM)),
    (0, "fte", re.compile(r"gjennomsnittlig\s*antall\s*" + _V + r"(?:\s*sysselsatt)?" + _YEAR + r"\s*(?:er|var|:|=)?\s*" + _NUM)),
    (0, "fte", re.compile(r"(?:selskapet|foretaket|stiftelsen|virksomheten|laget|foreningen)\s*har(?:\s*i\s*20\d\d)?(?:\s*hatt)?(?:\s*sysselsatt)?\s*" + _NUM + r"\s*" + _V)),
    (0, "fte", re.compile(r"sysselsatt\s*" + _NUM + r"\s*" + _V)),
    (1, "employees", re.compile(r"gjennomsnittlig(?:e)?\s*antall\s*ansatte" + _YEAR + r"\s*(?:er|var|:|=)?\s*" + _NUM)),
    (1, "employees", re.compile(r"antall\s*ansatte(?:\s*pr\.?\s*31\.?\s*12\.?)?\s*(?:er|var|:|=)\s*" + _NUM)),
    (1, "employees", re.compile(r"(?:selskapet|foretaket|stiftelsen)\s*har(?:\s*hatt)?\s*" + _NUM + r"\s*ansatte")),
]
_ZERO = re.compile(r"(?:selskapet|stiftelsen|foretaket|sameiet|virksomheten|laget|foreningen|det)\s*(?:har|hadde)\s*(?:ikke\s*hatt\s*(?:noen\s*)?|ingen\s*)(ansatte|" + _V + r")")
_GROUP = re.compile(r"konsern|group|datterselskap")


def fold(text: str) -> str:
    """Lower-case ASCII with æ/ø/å folded to single letters (OCR often drops the ring), whitespace collapsed."""
    text = (text or "").lower().replace("ø", "o").replace("æ", "ae").replace("å", "a")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text)


def _number(raw: str) -> int | float | None:
    raw = raw.strip()
    split = re.fullmatch(r"([0-9oO]{1,4}) ([0-9oO]{2})", raw)  # "8 40": an OCR-split decimal
    if split:
        raw = f"{split.group(1)}.{split.group(2)}"
    cleaned = raw.replace("o", "0").replace("O", "0").replace(" ", "").strip(".,")
    if not cleaned:
        return None
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if not 0 <= value <= 50_000:
        return None
    return int(value) if value.is_integer() else round(value, 2)


def extract(text: str) -> dict[str, Any] | None:
    """The company's own workforce statement in `text`: {"status": "ok", "count", "measure", "match"} or
    {"status": "conflict", "values"} when the page states different figures at the same priority, else None."""
    folded = fold(text)
    found: list[tuple[int, Any, str, str]] = []
    for priority, measure, pattern in _PATTERNS:
        for m in pattern.finditer(folded):
            if _GROUP.search(folded[max(0, m.start() - 80): m.end() + 20]):
                continue
            value = _number(m.group(1))
            if value is not None:
                found.append((priority, value, measure, m.group(0)))
    for m in _ZERO.finditer(folded):
        if not _GROUP.search(folded[max(0, m.start() - 80): m.end() + 20]):
            found.append((0 if "verk" in m.group(1).replace(" ", "") else 1, 0, "fte" if "verk" in m.group(1).replace(" ", "") else "employees", m.group(0)))
    if not found:
        return None
    best = min(f[0] for f in found)
    values = {f[1] for f in found if f[0] == best}
    if len(values) != 1:
        return {"status": "conflict", "values": sorted(map(str, values))}
    hit = next(f for f in found if f[0] == best)
    return {"status": "ok", "count": hit[1], "measure": hit[2], "match": hit[3]}


def quote_for(text: str, match: str) -> str | None:
    """The source lines (as read, not folded) that contain the folded `match`: one line, or two when the
    statement wraps."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    target = fold(match).strip()
    for size in (1, 2, 3):
        for i in range(len(lines) - size + 1):
            window = " ".join(lines[i:i + size])
            if target in fold(window):
                return window[:300]
    return None


def tools() -> dict[str, Any]:
    """Which external tools are installed (checked once per process)."""
    with _tools_lock:
        if not _tools:
            _tools["pdftotext"] = shutil.which("pdftotext")
            _tools["pdftoppm"] = shutil.which("pdftoppm")
            _tools["pdfinfo"] = shutil.which("pdfinfo")
            _tools["tesseract"] = shutil.which("tesseract")
            _tools["ocr"] = bool(_tools["pdftoppm"] and _tools["tesseract"])
            if not _tools["ocr"]:
                print("[workforce] tesseract and/or pdftoppm not installed: workforce from annual accounts is skipped "
                      "(brew install tesseract poppler / apt-get install tesseract-ocr poppler-utils)", file=sys.stderr)
        return _tools


def _run(args: list[str], timeout: float) -> str:
    env = dict(os.environ, OMP_THREAD_LIMIT="1")  # one core per Tesseract process; parallelism is OCR_SLOTS
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env).stdout or ""
    except (subprocess.SubprocessError, OSError):
        return ""


def _page_count(pdf: Path) -> int:
    if not tools()["pdfinfo"]:
        return max(PAGE_ORDER)
    m = re.search(r"Pages:\s+(\d+)", _run([tools()["pdfinfo"], str(pdf)], 30))
    return int(m.group(1)) if m else max(PAGE_ORDER)


def read_pdf(pdf: Path) -> dict[str, Any] | None:
    """{"result": extract(...), "page": n or None, "text": source text, "method": "text"|"ocr"} or None."""
    t = tools()
    if t["pdftotext"]:
        text = _run([t["pdftotext"], "-layout", str(pdf), "-"], 60)
        result = extract(text)
        if result:
            return {"result": result, "page": None, "text": text, "method": "text"}
    if not t["ocr"]:
        return None
    pages = _page_count(pdf)
    with tempfile.TemporaryDirectory(prefix="sp-wf-") as tmp:
        for page in (p for p in PAGE_ORDER if p <= pages):
            image = Path(tmp) / f"p{page}"
            if not _slots.acquire(timeout=SLOT_WAIT_S):
                return None
            try:
                _run([t["pdftoppm"], "-f", str(page), "-l", str(page), "-r", str(DPI), "-gray", "-png", "-singlefile", str(pdf), str(image)], 60)
                if not image.with_suffix(".png").exists():
                    continue
                text = _run([t["tesseract"], str(image.with_suffix(".png")), "stdout", "-l", "eng", "--psm", "6"], 90)
            finally:
                _slots.release()
            result = extract(text)
            if result:
                return {"result": result, "page": page, "text": text, "method": "ocr"}
    return None


class WorkforceConnector:
    """Adds `financials.workforce` (latest filed year) from the official annual-accounts copy."""

    name = "workforce"
    families: tuple[str, ...] = ()  # adds a claim to `financials`; the register connector owns that family's state

    def run(self, ctx: CompanyContext) -> ConnectorResult:
        shared = ctx.shared or {}
        year = shared.get("latest_filed_year")
        client = ctx.client
        if os.environ.get("SIGNALPOST_WORKFORCE", "1") == "0":
            return ConnectorResult()
        if client is None or shared.get("registry_only") or not year or not tools()["ocr"] and not tools()["pdftotext"]:
            return ConnectorResult()
        deadline_at = shared.get("deadline_at")
        if deadline_at is not None and deadline_at - time.monotonic() < MIN_TIME_LEFT_S:
            return ConnectorResult()
        on_pace = shared.get("on_pace")
        if callable(on_pace) and not on_pace():
            return ConnectorResult()  # the run is behind pace: websites, news and jobs come first
        if client.remaining(ctx.org) < 1 or client.remaining(None) < MIN_RUN_REQUESTS_LEFT:
            return ConnectorResult()
        url = PDF_URL.format(org=ctx.org, year=year)
        resp = client.get(url, org=ctx.org, purpose="annual_accounts_pdf", accept="*/*", max_bytes=MAX_PDF_BYTES,
                          timeout=30.0, respect_robots=False, snapshot=False)
        if not resp.ok or not resp.body.startswith(b"%PDF") or len(resp.body) > MAX_PDF_BYTES:
            error = resp.error or ("not_pdf" if resp.ok else f"http {resp.status}")
            return ConnectorResult(errors=[{"stage": "workforce", "url": url, "error": error, "status": resp.status}])
        with tempfile.TemporaryDirectory(prefix="sp-wf-") as tmp:
            pdf = Path(tmp) / "accounts.pdf"
            pdf.write_bytes(resp.body)
            read = read_pdf(pdf)
        if not read or read["result"]["status"] != "ok":
            return ConnectorResult()
        result = read["result"]
        quote = quote_for(read["text"], result["match"])
        where = f"page {read['page']}" if read["page"] else "text layer"
        evidence = make_evidence(
            source_url=url, source_class="official_filing", retrieved_at=resp.retrieved_at,
            extraction_method=EXTRACTION_METHOD, final_url=resp.final_url, redirect_chain=resp.redirect_chain,
            http_status=resp.status, content_sha256=resp.content_sha256,
            span=f"{where} ({read['method']}): {quote or result['match']}", access_policy="NLOD-2.0", quote=quote,
        )
        label = "full-time equivalents (årsverk)" if result["measure"] == "fte" else "employees (ansatte)"
        claim = make_claim(
            org=ctx.org, family="financials", field="workforce",
            value={"count": result["count"], "measure": result["measure"], "label": label, "year": int(year), "url": url},
            value_key=str(year), identity_basis="registry_record",
            reporting_period={"start": f"{year}-01-01", "end": f"{year}-12-31"}, evidence_ids=[evidence.evidence_id],
            confidence=0.9 if read["method"] == "ocr" else 1.0,
            note="read from the annual-accounts copy filed for this organisation number" + (" (OCR)" if read["method"] == "ocr" else ""),
        )
        return ConnectorResult(claims=[claim], evidence=[evidence])
