"""Independent gold-label schema and I/O for the external-family ground truth.

These labels are produced by *researching the live web ourselves* (registry API, search,
fetching the candidate site) -- never by reading the agent's own output. That independence is
what lets `eval.score` catch a wrong-company publication instead of grading the agent against
its own mistakes.

One gold record per organisation. Fields:
- organisation_number, name: identity, copied from the split row at labelling time.
- website: dict describing the correct official website, if any --
    status: "exact" | "related" | "none_found" | "uncertain"
    url: the site's normalized root URL, or null
    relationship: "parent" | "subsidiary" | "franchise" | "brand" | "service_provider" | null
        (set when status == "related" -- the site is real but not this legal entity's own site)
    evidence_url: the exact page we found the org number / matching address / phone on
    evidence_note: short free-text description of what was checked and what it showed
- profiles: list of {platform, url} company-owned social/video profiles linked from the site
- jobs: dict {checked: bool, active: bool | null, note: str} -- only populated for the ~40
  companies we also checked on arbeidsplassen.nav.no
- confidence: "high" | "medium" | "low"
- notes: free text -- traps found (franchise, shared domain, namesake collision, etc.)
- labeller: always "independent" for this collection
- labelled_at: ISO date the label was produced

`load_gold` merges any number of gold files (later files override earlier ones by org number,
so a correction file can be layered on top without editing history) and returns a dict keyed by
organisation_number.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

WebsiteStatus = Literal["exact", "related", "none_found", "uncertain"]
Relationship = Literal["parent", "subsidiary", "franchise", "brand", "service_provider", None]
Confidence = Literal["high", "medium", "low"]


class GoldWebsite(BaseModel):
    status: WebsiteStatus
    url: Optional[str] = None
    relationship: Relationship = None
    evidence_url: Optional[str] = None
    evidence_note: str = ""


class GoldProfile(BaseModel):
    platform: str
    url: str


class GoldJobs(BaseModel):
    checked: bool = False
    active: Optional[bool] = None
    note: str = ""


class GoldRecord(BaseModel):
    organisation_number: str
    name: str
    website: GoldWebsite
    profiles: list[GoldProfile] = Field(default_factory=list)
    jobs: GoldJobs = Field(default_factory=GoldJobs)
    confidence: Confidence = "medium"
    notes: str = ""
    labeller: str = "independent"
    labelled_at: str

    def website_key(self) -> str | None:
        """Normalized site key used for company-recall matching (scheme/www/trailing slash
        stripped, lowercased host + path)."""
        if not self.website.url:
            return None
        return normalize_url(self.website.url)


def normalize_url(url: str) -> str:
    url = url.strip().lower()
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    if url.startswith("www."):
        url = url[4:]
    url = url.rstrip("/")
    return url


def load_gold(paths: list[str | Path]) -> dict[str, GoldRecord]:
    records: dict[str, GoldRecord] = {}
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                record = GoldRecord.model_validate(raw)
                records[record.organisation_number] = record
    return records


def write_gold(records: list[GoldRecord], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n")


def summarize(records: dict[str, GoldRecord]) -> dict[str, Any]:
    """Label-count summary used by the final report and by `python -m eval.gold --summarize`."""
    status_counts: dict[str, int] = {}
    confidence_counts: dict[str, int] = {}
    jobs_checked = 0
    jobs_active = 0
    for record in records.values():
        status_counts[record.website.status] = status_counts.get(record.website.status, 0) + 1
        confidence_counts[record.confidence] = confidence_counts.get(record.confidence, 0) + 1
        if record.jobs.checked:
            jobs_checked += 1
            if record.jobs.active:
                jobs_active += 1
    return {
        "total_labelled": len(records),
        "website_status_counts": status_counts,
        "confidence_counts": confidence_counts,
        "jobs_checked": jobs_checked,
        "jobs_active": jobs_active,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Summarize a gold-label collection.")
    parser.add_argument("--gold", nargs="+", required=True)
    args = parser.parse_args()
    records = load_gold(args.gold)
    print(json.dumps(summarize(records), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
