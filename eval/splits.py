"""Frozen evaluation splits carved out of the universe file.

The universe file (`data/signalpost-universe.jsonl.gz`, 411,160 rows) is a de-normalised
snapshot of the Brreg bulk file with one JSON object per line:
    organisation_number, name, legal_form, employees, industry_code, industry_label,
    municipality, municipality_number, website, bankrupt, liquidating,
    latest_submitted_accounts

Everything here is deterministic: given the same universe file and the same SEED, calling
`build_splits` twice produces byte-identical output. That lets us freeze `eval/data/*.jsonl`
once and keep using it as a stable yardstick while the agent itself changes.

Split design
------------
- `dev` (300), `val` (200), `test` (200): uniform random samples over the whole universe,
  each the same shape as the organiser's daily batch (mostly shells, a handful staffed).
  Assigned by ranking every org with a seeded SHA-256 hash and slicing the ranking into
  disjoint windows, so no organisation number is ever in more than one of these three splits.
- `stress` (150): a *separate* pool, excluded from dev/val/test, deliberately enriched for the
  edge cases that break identity resolution: staffed companies, registry-website presence
  *and* absence, franchise/chain-like names, ASA/NUF legal forms, housing co-ops (BRL/SAM),
  and names built from generic dictionary words (higher risk of picking the wrong site).
- `daily_like(seed)`: not a frozen split. A convenience sampler that draws a uniform 100-row
  batch (with replacement across calls, i.e. independent of the frozen splits) to rehearse
  the organiser's daily-batch shape during development.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Iterator

SEED = "signalpost-eval-v1"

DEV_SIZE = 300
VAL_SIZE = 200
TEST_SIZE = 200
STRESS_SIZE = 150

FRANCHISE_HINTS = (
    "7-ELEVEN", "7 ELEVEN", "NARVESEN", "REMA", "REMA 1000", "COOP ", "KIWI", "SPAR",
    "JOKER", "MENY", "BUNNPRIS", "THON", "CLARION", "SCANDIC", "QUALITY HOTEL",
    "BURGER KING", "MCDONALD", "SUBWAY", "PEPPES", "DOMINO", "CIRCLE K", "ESSO",
    "SHELL", "UNO-X", "BAMA", "EUROPRIS", "XXL", "BIKE1", "FRISØR", "STUDIO 1",
    "RE/MAX", "EIENDOMSMEGLER 1", "DNB EIENDOM", "NORDVIK",
)
GENERIC_NAME_WORDS = (
    "NORGE", "NORDIC", "GRUPPEN", "GROUP", "SERVICE", "SERVICES", "CONSULT",
    "CONSULTING", "SOLUTIONS", "HOLDING", "INVEST", "EIENDOM", "BYGG", "DESIGN",
    "PARTNER", "PARTNERS", "TEAM", "STUDIO", "SENTER", "SENTERET",
)
HOUSING_FORMS = ("BRL", "SAM", "ESEK")


def _seeded_rank(org: str, *, salt: str = "") -> int:
    digest = hashlib.sha256(f"{SEED}:{salt}:{org}".encode("utf-8")).hexdigest()
    return int(digest, 16)


def iter_universe(path: str | Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _is_franchise_like(name: str) -> bool:
    upper = name.upper()
    return any(hint in upper for hint in FRANCHISE_HINTS)


def _is_generic_name(name: str) -> bool:
    upper = name.upper()
    return any(f" {word}" in f" {upper}" for word in GENERIC_NAME_WORDS)


def _stress_bucket(record: dict[str, Any]) -> str:
    """Which stress-set criterion a record satisfies (a record may match several; we tag the
    first one it hits so each bucket gets deterministic, roughly even representation)."""
    name = record.get("name") or ""
    legal_form = record.get("legal_form") or ""
    employees = record.get("employees")
    website = record.get("website") or ""
    if _is_franchise_like(name):
        return "franchise_like_name"
    if legal_form == "ASA":
        return "asa"
    if legal_form == "NUF":
        return "nuf"
    if legal_form in HOUSING_FORMS:
        return "housing_coop"
    if _is_generic_name(name):
        return "generic_name_words"
    if employees is not None and employees >= 5 and website:
        return "staffed_with_registry_website"
    if employees is not None and employees >= 5 and not website:
        return "staffed_without_registry_website"
    return ""


def build_splits(universe_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Build dev/val/test/stress splits in one pass over the universe file.

    Returns a dict of split name -> list of row dicts (same shape as universe rows, plus a
    `stress_bucket` field on stress rows). Deterministic given the same input file.
    """
    ranked: list[tuple[int, dict[str, Any]]] = []
    stress_candidates: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for record in iter_universe(universe_path):
        org = record["organisation_number"]
        ranked.append((_seeded_rank(org, salt="core"), record))
        bucket = _stress_bucket(record)
        if bucket:
            stress_candidates.setdefault(bucket, []).append((_seeded_rank(org, salt="stress"), record))

    ranked.sort(key=lambda item: item[0])
    dev = [r for _, r in ranked[:DEV_SIZE]]
    val = [r for _, r in ranked[DEV_SIZE:DEV_SIZE + VAL_SIZE]]
    test = [r for _, r in ranked[DEV_SIZE + VAL_SIZE:DEV_SIZE + VAL_SIZE + TEST_SIZE]]
    used_orgs = {r["organisation_number"] for r in dev + val + test}

    # Round-robin across buckets so the stress set is a mix, not dominated by the largest bucket.
    for bucket in stress_candidates:
        stress_candidates[bucket] = [
            item for item in sorted(stress_candidates[bucket], key=lambda item: item[0])
            if item[1]["organisation_number"] not in used_orgs
        ]
    stress: list[dict[str, Any]] = []
    stress_orgs: set[str] = set()
    bucket_names = sorted(stress_candidates)
    cursors = {b: 0 for b in bucket_names}
    while len(stress) < STRESS_SIZE and any(cursors[b] < len(stress_candidates[b]) for b in bucket_names):
        for bucket in bucket_names:
            if len(stress) >= STRESS_SIZE:
                break
            items = stress_candidates[bucket]
            idx = cursors[bucket]
            while idx < len(items) and items[idx][1]["organisation_number"] in stress_orgs:
                idx += 1
            if idx < len(items):
                record = dict(items[idx][1])
                record["stress_bucket"] = bucket
                stress.append(record)
                stress_orgs.add(record["organisation_number"])
                cursors[bucket] = idx + 1
            else:
                cursors[bucket] = idx

    return {"dev": dev, "val": val, "test": test, "stress": stress}


def daily_like(universe_path: str | Path, seed: int, *, size: int = 100) -> list[dict[str, Any]]:
    """Uniform `size`-company batch, shaped like the organiser's daily run.

    Not a frozen split -- callers pass their own `seed` to draw different rehearsal batches.
    Deterministic for a given (universe_path, seed).
    """
    ranked = [
        (int(hashlib.sha256(f"{SEED}:daily:{seed}:{r['organisation_number']}".encode()).hexdigest(), 16), r)
        for r in iter_universe(universe_path)
    ]
    ranked.sort(key=lambda item: item[0])
    return [r for _, r in ranked[:size]]


def write_jsonl(rows: Iterable[dict[str, Any]], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build frozen eval splits from the universe file.")
    parser.add_argument("--universe", default="../data/signalpost-universe.jsonl.gz")
    parser.add_argument("--out-dir", default=str(Path(__file__).parent / "data"))
    args = parser.parse_args()

    splits = build_splits(args.universe)
    out_dir = Path(args.out_dir)
    overlap_check: dict[str, set[str]] = {}
    for name, rows in splits.items():
        orgs = {r["organisation_number"] for r in rows}
        overlap_check[name] = orgs
        write_jsonl(rows, out_dir / f"{name}.jsonl")
        print(f"{name}: {len(rows)} rows -> {out_dir / f'{name}.jsonl'}")

    names = list(overlap_check)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            overlap = overlap_check[a] & overlap_check[b]
            if overlap:
                raise SystemExit(f"org overlap between {a} and {b}: {sorted(overlap)[:5]}...")
    print("no org overlap across splits: OK")


if __name__ == "__main__":
    main()
