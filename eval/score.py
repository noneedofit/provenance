"""Local proxy scorer for Signalpost envelopes.

Mirrors the organiser's rubric (coverage 35 / accuracy 30 / refresh 20 / synthesis 10 / UX 5)
closely enough to rank our own changes, using independent gold labels (`eval/gold.py`) instead
of the organiser's hidden pooled collection. It is a proxy: see `CLAIM_BOUNDARY` below.

Usage:
    python -m eval.score --envelopes out/envelopes.jsonl --gold eval/data/gold_web.jsonl \
        [--previous out/prev-envelopes.jsonl] [--report out/run-report.json] \
        --out eval-report.json [--markdown eval-report.md]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from signalpost.models import FAMILIES, EXTERNAL_FAMILIES  # noqa: E402

from eval.gold import GoldRecord, load_gold, normalize_url  # noqa: E402

CLAIM_BOUNDARY = (
    "Local optimization proxy against independently researched gold labels for a sample of "
    "companies. It is not the organiser's hidden pooled collection or frozen batch; use it to "
    "rank our own changes (promote/hold/reject), not as a prediction of the leaderboard score."
)

# Default equal weighting across external families for the pooled-recall proxy.
DEFAULT_FAMILY_WEIGHTS: dict[str, float] = {
    "website": 1.0,
    "profiles": 1.0,
    "description": 1.0,
    "jobs": 1.0,
    "activity": 1.0,
}

PROFILE_PLATFORM_HINTS: dict[str, str] = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "twitter.com": "twitter",
    "x.com": "twitter",
}

RUBRIC_WEIGHTS = {"coverage": 35.0, "accuracy": 30.0, "refresh": 20.0, "synthesis": 10.0, "ux": 5.0}

BUDGET_MAX_REQUESTS = 2000
BUDGET_MAX_RUNTIME_MS = 45 * 60 * 1000


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _platform_for_url(url: str) -> str:
    normalized = normalize_url(url)
    for host, platform in PROFILE_PLATFORM_HINTS.items():
        if normalized.startswith(host) or f"//{host}" in url:
            return platform
    return normalized.split("/")[0]


# --------------------------------------------------------------------------------------
# 1. Envelope contract validity
# --------------------------------------------------------------------------------------

def check_contract(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    issues: list[str] = []
    orgs_seen: dict[str, int] = defaultdict(int)
    missing_families = 0
    claims_missing_evidence = 0
    claims_zero_for_missing = 0
    dangling_evidence_refs = 0
    total_available_claims = 0
    n = len(envelopes)

    for env in envelopes:
        org = env.get("organisation_number", "")
        orgs_seen[org] += 1
        families = env.get("families", {}) or {}
        for fam in FAMILIES:
            if fam not in families:
                missing_families += 1
        evidence_ids = {e.get("evidence_id") for e in env.get("evidence", []) or []}
        for claim in env.get("claims", []) or []:
            available = claim.get("availability") == "available"
            if available:
                total_available_claims += 1
                if not claim.get("evidence_ids"):
                    claims_missing_evidence += 1
                if claim.get("value") is None:
                    claims_zero_for_missing += 1
            elif claim.get("value") is not None and not (claim.get("availability") == "ambiguous" and claim.get("relationship")):
                # a non-available claim should not carry a value (labelled related sites are the exception)
                claims_zero_for_missing += 1
            for eid in claim.get("evidence_ids", []) or []:
                if eid not in evidence_ids:
                    dangling_evidence_refs += 1

    duplicate_orgs = [org for org, count in orgs_seen.items() if count > 1]
    if duplicate_orgs:
        issues.append(f"{len(duplicate_orgs)} organisation_number(s) appear more than once")
    if missing_families:
        issues.append(f"{missing_families} (envelope, family) pairs missing a FamilyState")
    if claims_missing_evidence:
        issues.append(f"{claims_missing_evidence} available claims have no evidence_ids")
    if dangling_evidence_refs:
        issues.append(f"{dangling_evidence_refs} evidence_ids referenced but not present in envelope.evidence")
    if claims_zero_for_missing:
        issues.append(f"{claims_zero_for_missing} claims violate the value/availability invariant")

    passed = n > 0 and not duplicate_orgs and missing_families == 0 and claims_missing_evidence == 0 and dangling_evidence_refs == 0
    return {
        "envelopes": n,
        "unique_organisations": len(orgs_seen),
        "duplicate_organisations": duplicate_orgs,
        "missing_family_states": missing_families,
        "claims_missing_evidence": claims_missing_evidence,
        "claims_violating_value_invariant": claims_zero_for_missing,
        "dangling_evidence_refs": dangling_evidence_refs,
        "total_available_claims": total_available_claims,
        "issues": issues,
        "passed": passed,
    }


# --------------------------------------------------------------------------------------
# 2. Per-family coverage (company + fact counts), independent of gold
# --------------------------------------------------------------------------------------

def family_coverage(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(envelopes)
    result: dict[str, Any] = {}
    for fam in FAMILIES:
        companies_available = 0
        companies_not_available = 0
        companies_blocked_or_failed = 0
        fact_count = 0
        for env in envelopes:
            state = (env.get("families", {}) or {}).get(fam, {})
            avail = state.get("availability")
            if avail == "available":
                companies_available += 1
                fact_count += state.get("claim_count", 0) or 0
            elif avail in ("not_available", "not_applicable", "ambiguous"):
                companies_not_available += 1
            elif avail in ("blocked", "failed"):
                companies_blocked_or_failed += 1
        result[fam] = {
            "companies_available": companies_available,
            "companies_available_rate": round(companies_available / n, 4) if n else 0.0,
            "companies_not_available": companies_not_available,
            "companies_blocked_or_failed": companies_blocked_or_failed,
            "fact_count": fact_count,
        }
    return result


def abstention_rate(envelopes: list[dict[str, Any]]) -> float:
    """Fraction of (envelope, external family) pairs that abstained (not_available/ambiguous)
    rather than guessed -- a healthy agent abstains a lot on the ~85% shell companies."""
    total = 0
    abstained = 0
    for env in envelopes:
        families = env.get("families", {}) or {}
        for fam in EXTERNAL_FAMILIES:
            state = families.get(fam)
            if state is None:
                continue
            total += 1
            if state.get("availability") in ("not_available", "ambiguous"):
                abstained += 1
    return round(abstained / total, 4) if total else 0.0


# --------------------------------------------------------------------------------------
# 3. Precision / recall vs independent gold, for website + profiles + jobs
# --------------------------------------------------------------------------------------

WEBSITE_FIELDS = ("official_website",)


def _claim_url(claim: dict[str, Any]) -> str | None:
    value = claim.get("value")
    if isinstance(value, dict):
        value = value.get("url") or value.get("domain")
    return normalize_url(str(value)) if value else None


def _host_path(url: str) -> tuple[str, str]:
    host, _, path = url.partition("/")
    return host, path.strip("/")


LANGUAGE_PATHS = {"", "en", "no", "nb", "nn", "sv", "da", "de", "en-gb", "en-us", "nb-no", "no-nb", "home", "index.html", "hjem"}


def same_site(ours: str | None, gold: str | None) -> bool:
    """Same site if URLs match after normalisation, or same host where the gold URL has no specific path
    and ours is the root or a language/home path (scatec.com vs scatec.com/en)."""
    if not ours or not gold:
        return False
    if ours == gold:
        return True
    oh, op = _host_path(ours)
    gh, gp = _host_path(gold)
    if oh != gh:
        return False
    if not gp:
        return op in LANGUAGE_PATHS
    return op == gp or op.startswith(gp + "/")


def _website_claims(env: dict[str, Any]) -> list[dict[str, Any]]:
    """Published website identities: exact (available) and labelled related sites (ambiguous + relationship)."""
    return [
        c for c in env.get("claims", []) or []
        if c.get("family") == "website" and c.get("field") in WEBSITE_FIELDS
        and c.get("availability") in ("available", "ambiguous") and c.get("value")
    ]


def _profile_claims(env: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in env.get("claims", []) or [] if c.get("family") == "profiles" and c.get("availability") == "available"]


def _jobs_family_available(env: dict[str, Any]) -> bool:
    state = (env.get("families", {}) or {}).get("jobs", {})
    return state.get("availability") == "available" and (state.get("claim_count") or 0) > 0


def score_website(envelopes: list[dict[str, Any]], gold: dict[str, GoldRecord]) -> dict[str, Any]:
    by_org = {e.get("organisation_number"): e for e in envelopes}
    exact_published = 0
    related_published = 0
    wrong_company = 0
    wrong_company_orgs: list[str] = []
    correct_exact = 0
    correct_related = 0
    gold_has_site = [r for r in gold.values() if r.website.status in ("exact", "related")]
    gold_none = [r for r in gold.values() if r.website.status == "none_found"]
    recalled = 0
    false_positive_on_none = 0

    for org, record in gold.items():
        env = by_org.get(org)
        claims = _website_claims(env) if env else []
        our_exact_urls = {_claim_url(c) for c in claims if c.get("availability") == "available" and c.get("relationship") in (None, "exact")} - {None}
        our_related_urls = {_claim_url(c) for c in claims if c.get("relationship") not in (None, "exact") or c.get("availability") == "ambiguous"} - {None} - our_exact_urls
        exact_published += len(our_exact_urls)
        related_published += len(our_related_urls)

        gold_key = record.website_key()
        if record.website.status == "exact":
            if our_exact_urls:
                if any(same_site(u, gold_key) for u in our_exact_urls):
                    correct_exact += 1
                    recalled += 1
                else:
                    wrong_company += len(our_exact_urls)
                    wrong_company_orgs.append(org)
            elif our_related_urls:
                # publishing an exact-status company's site as a mere "related" link isn't a
                # wrong-company error, just an undercount -- not counted as wrong, not recalled.
                pass
        elif record.website.status == "related":
            if our_exact_urls:
                # we published someone else's related/franchise/parent site as exact -> wrong company
                wrong_company += len(our_exact_urls)
                wrong_company_orgs.append(org)
            elif gold_key and any(same_site(u, gold_key) for u in our_related_urls):
                correct_related += 1
                recalled += 1
        elif record.website.status == "none_found":
            if our_exact_urls:
                false_positive_on_none += len(our_exact_urls)
        # "uncertain" gold rows are excluded from precision/recall (we don't know the truth).

    total_published = exact_published + related_published
    precision = round((total_published - wrong_company - false_positive_on_none) / total_published, 4) if total_published else None
    company_recall = round(recalled / len(gold_has_site), 4) if gold_has_site else None
    return {
        "gold_companies": len(gold),
        "gold_with_site": len(gold_has_site),
        "gold_none_found": len(gold_none),
        "gold_uncertain": sum(1 for r in gold.values() if r.website.status == "uncertain"),
        "published_exact": exact_published,
        "published_related": related_published,
        "correct_exact": correct_exact,
        "correct_related": correct_related,
        "wrong_company_publications": wrong_company,
        "wrong_company_organisations": wrong_company_orgs,
        "false_positive_publications_where_gold_says_none": false_positive_on_none,
        "precision": precision,
        "company_recall": company_recall,
    }


def score_profiles(envelopes: list[dict[str, Any]], gold: dict[str, GoldRecord]) -> dict[str, Any]:
    by_org = {e.get("organisation_number"): e for e in envelopes}
    gold_profile_urls: dict[str, set[str]] = {
        org: {normalize_url(p.url) for p in record.profiles} for org, record in gold.items() if record.profiles
    }
    total_published = 0
    correct = 0
    wrong = 0
    fact_recall_hits = 0
    unverified = 0
    fact_recall_total = sum(len(urls) for urls in gold_profile_urls.values())
    companies_with_gold_profiles = len(gold_profile_urls)
    companies_recalled = 0

    for org, gold_urls in gold_profile_urls.items():
        env = by_org.get(org)
        claims = _profile_claims(env) if env else []
        our_urls = {_claim_url(c) for c in claims} - {None}
        total_published += len(our_urls)
        matched = {u for u in our_urls if any(same_site(u, g) or same_site(g, u) for g in gold_urls)}
        correct += len(matched)
        # a profile missing from gold is unverified, not proof of a wrong company
        unverified += len(our_urls - matched)
        fact_recall_hits += len(matched)
        if matched:
            companies_recalled += 1

    # companies with gold_url==none but envelope published a profile: still counted in `wrong`
    # via the loop above only for orgs that HAVE gold profiles; check the rest separately.
    for org, record in gold.items():
        if org in gold_profile_urls:
            continue
        env = by_org.get(org)
        claims = _profile_claims(env) if env else []
        unverified += len({_claim_url(c) for c in claims} - {None})

    # Profiles are only published when linked from a verified site, so a profile is wrong-company exactly
    # when the site it came from was; website scoring already counts that. Here we report agreement only.
    precision = round(correct / (correct + unverified), 4) if (correct + unverified) else None
    company_recall = round(companies_recalled / companies_with_gold_profiles, 4) if companies_with_gold_profiles else None
    fact_recall = round(fact_recall_hits / fact_recall_total, 4) if fact_recall_total else None
    return {
        "gold_companies_with_profiles": companies_with_gold_profiles,
        "gold_profile_urls": fact_recall_total,
        "published_profile_urls": total_published,
        "correct": correct,
        "wrong": wrong, "unverified_not_in_gold": unverified,
        "precision": precision,
        "company_recall": company_recall,
        "fact_recall": fact_recall,
    }


def score_jobs(envelopes: list[dict[str, Any]], gold: dict[str, GoldRecord]) -> dict[str, Any]:
    by_org = {e.get("organisation_number"): e for e in envelopes}
    checked = [r for r in gold.values() if r.jobs.checked]
    if not checked:
        return {"gold_companies_checked": 0, "accuracy": None, "company_recall": None}
    correct = 0
    active_gold = [r for r in checked if r.jobs.active]
    recalled = 0
    for record in checked:
        env = by_org.get(record.organisation_number)
        we_say_active = _jobs_family_available(env) if env else False
        if we_say_active == bool(record.jobs.active):
            correct += 1
        if record.jobs.active and we_say_active:
            recalled += 1
    return {
        "gold_companies_checked": len(checked),
        "gold_companies_with_active_jobs": len(active_gold),
        "accuracy": round(correct / len(checked), 4),
        "company_recall": round(recalled / len(active_gold), 4) if active_gold else None,
    }


# --------------------------------------------------------------------------------------
# 4. Pooled-recall proxy (organiser's 0.7 company-recall + 0.3 fact-recall, per family)
# --------------------------------------------------------------------------------------

def pooled_recall_proxy(
    website: dict[str, Any], profiles: dict[str, Any], jobs: dict[str, Any],
    coverage: dict[str, Any], *, family_weights: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    weights = dict(family_weights or DEFAULT_FAMILY_WEIGHTS)
    per_family: dict[str, Optional[float]] = {}

    website_company_recall = website.get("company_recall")
    # fact recall for website: correct urls / gold urls with a site
    gold_with_site = website.get("gold_with_site") or 0
    website_correct = (website.get("correct_exact") or 0) + (website.get("correct_related") or 0)
    website_fact_recall = round(website_correct / gold_with_site, 4) if gold_with_site else None
    per_family["website"] = (
        round(0.7 * website_company_recall + 0.3 * website_fact_recall, 4)
        if website_company_recall is not None and website_fact_recall is not None else None
    )

    per_family["profiles"] = (
        round(0.7 * profiles["company_recall"] + 0.3 * profiles["fact_recall"], 4)
        if profiles.get("company_recall") is not None and profiles.get("fact_recall") is not None else None
    )

    per_family["jobs"] = (
        round(0.7 * jobs["company_recall"] + 0.3 * jobs["accuracy"], 4)
        if jobs.get("company_recall") is not None and jobs.get("accuracy") is not None else None
    )

    # description / activity have no independent gold in this harness yet: fall back to a
    # coverage-only proxy (company recall against "we published something", no fact precision),
    # flagged as unverified so it never masquerades as a precision-checked number.
    for fam in ("description", "activity"):
        rate = coverage.get(fam, {}).get("companies_available_rate")
        per_family[fam] = rate  # unverified: no gold to check precision against

    verified_families = [f for f in ("website", "profiles", "jobs") if per_family.get(f) is not None]
    unverified_families = [f for f in ("description", "activity") if per_family.get(f) is not None]

    weighted_sum = 0.0
    weight_total = 0.0
    for fam, value in per_family.items():
        if value is None:
            continue
        w = weights.get(fam, 0.0)
        weighted_sum += w * value
        weight_total += w
    overall = round(weighted_sum / weight_total, 4) if weight_total else None

    return {
        "per_family": per_family,
        "verified_families": verified_families,
        "unverified_families_coverage_only": unverified_families,
        "family_weights": weights,
        "overall": overall,
    }


# --------------------------------------------------------------------------------------
# 5. Refresh checks (rerun vs previous envelopes)
# --------------------------------------------------------------------------------------

def score_refresh(envelopes: list[dict[str, Any]], previous: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
    if previous is None:
        return {"checked": False, "reason": "no --previous envelopes supplied"}

    prev_by_org = {e.get("organisation_number"): e for e in previous}
    duplicate_claim_ids = 0
    false_changes_on_unchanged = 0
    total_changes = 0
    idempotency_checked_orgs = 0

    for env in envelopes:
        org = env.get("organisation_number")
        claim_ids = [c.get("claim_id") for c in env.get("claims", []) or []]
        duplicate_claim_ids += len(claim_ids) - len(set(claim_ids))
        changes = env.get("changes", []) or []
        total_changes += len(changes)

        prev_env = prev_by_org.get(org)
        if prev_env is None:
            continue
        idempotency_checked_orgs += 1
        prev_claim_values = {
            c.get("claim_id"): c.get("value") for c in prev_env.get("claims", []) or [] if c.get("status") == "current"
        }
        cur_claim_values = {
            c.get("claim_id"): c.get("value") for c in env.get("claims", []) or [] if c.get("status") == "current"
        }
        if prev_claim_values == cur_claim_values and changes:
            false_changes_on_unchanged += len(changes)

    return {
        "checked": True,
        "envelopes_compared": len(envelopes),
        "matched_previous_organisations": idempotency_checked_orgs,
        "duplicate_claim_ids": duplicate_claim_ids,
        "total_changes_reported": total_changes,
        "false_changes_on_unchanged_sources": false_changes_on_unchanged,
        "passed": duplicate_claim_ids == 0 and false_changes_on_unchanged == 0,
    }


# --------------------------------------------------------------------------------------
# 6. Budget checks
# --------------------------------------------------------------------------------------

def score_budget(report: Optional[dict[str, Any]]) -> dict[str, Any]:
    if report is None:
        return {"checked": False, "reason": "no --report run-report.json supplied"}
    total_requests = report.get("total_requests") or report.get("requests_total")
    runtime_ms = report.get("runtime_ms") or (report.get("runtime") or {}).get("total_ms")
    if runtime_ms is None and report.get("runtime_s") is not None:
        runtime_ms = int(float(report["runtime_s"]) * 1000)
    within_requests = total_requests is not None and total_requests <= BUDGET_MAX_REQUESTS
    within_runtime = runtime_ms is not None and runtime_ms <= BUDGET_MAX_RUNTIME_MS
    return {
        "checked": True,
        "total_requests": total_requests,
        "max_requests": BUDGET_MAX_REQUESTS,
        "within_request_budget": within_requests,
        "runtime_ms": runtime_ms,
        "max_runtime_ms": BUDGET_MAX_RUNTIME_MS,
        "within_runtime_budget": within_runtime,
        "passed": within_requests and within_runtime,
    }


# --------------------------------------------------------------------------------------
# 7. Synthesis checks
# --------------------------------------------------------------------------------------

def score_synthesis(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(envelopes)
    sentences_total = 0
    sentences_with_citations = 0
    unknowns_present = 0
    for env in envelopes:
        summary = env.get("summary", {}) or {}
        sentences = summary.get("sentences", []) or []
        sentences_total += len(sentences)
        sentences_with_citations += sum(1 for s in sentences if s.get("claim_ids"))
        if summary.get("unknowns"):
            unknowns_present += 1
    citation_rate = round(sentences_with_citations / sentences_total, 4) if sentences_total else None
    unknowns_rate = round(unknowns_present / n, 4) if n else 0.0
    return {
        "envelopes": n,
        "sentences_total": sentences_total,
        "sentences_with_citations": sentences_with_citations,
        "citation_rate": citation_rate,
        "envelopes_listing_unknowns": unknowns_present,
        "unknowns_listed_rate": unknowns_rate,
        "passed": citation_rate in (None, 1.0) and unknowns_rate >= 0.5,
    }


# --------------------------------------------------------------------------------------
# Proxy total /100
# --------------------------------------------------------------------------------------

def proxy_total(
    contract: dict[str, Any], pooled: dict[str, Any], website: dict[str, Any],
    profiles: dict[str, Any], refresh: dict[str, Any], budget: dict[str, Any],
    synthesis: dict[str, Any],
) -> dict[str, Any]:
    coverage_points = round(RUBRIC_WEIGHTS["coverage"] * (pooled.get("overall") or 0.0), 3)

    wrong_company = website.get("wrong_company_publications", 0) + profiles.get("wrong", 0)
    precision = website.get("precision")
    accuracy_base = precision if precision is not None else 0.0
    # heavy penalty per wrong-company publication, beyond what precision already reflects
    wrong_company_penalty = min(RUBRIC_WEIGHTS["accuracy"], wrong_company * 5.0)
    accuracy_points = round(max(0.0, RUBRIC_WEIGHTS["accuracy"] * accuracy_base - wrong_company_penalty), 3)

    if refresh.get("checked"):
        refresh_ok = refresh.get("passed", False)
        refresh_points = RUBRIC_WEIGHTS["refresh"] if refresh_ok else round(
            RUBRIC_WEIGHTS["refresh"] * 0.3, 3
        )
    else:
        refresh_points = 0.0  # not measured this run -> no credit claimed

    synthesis_points = RUBRIC_WEIGHTS["synthesis"] if synthesis.get("passed") else round(
        RUBRIC_WEIGHTS["synthesis"] * (synthesis.get("citation_rate") or 0.0), 3
    )

    ux_points = 0.0  # placeholder: UX has no automated proxy here (see docs/eval.md)

    qualification_gates = {
        "contract_valid": contract.get("passed", False),
        "zero_wrong_company_publications": wrong_company == 0,
        "within_budget": budget.get("passed") is not False,  # unmeasured budget doesn't block
    }
    qualifies = all(qualification_gates.values())

    total = round(coverage_points + accuracy_points + refresh_points + synthesis_points + ux_points, 3)
    return {
        "coverage_points": coverage_points,
        "accuracy_points": accuracy_points,
        "refresh_points": refresh_points,
        "synthesis_points": synthesis_points,
        "ux_points": ux_points,
        "wrong_company_total": wrong_company,
        "qualification_gates": qualification_gates,
        "qualifies": qualifies,
        "total": total if qualifies else min(total, 40.0),  # a disqualifying run is capped hard
    }


# --------------------------------------------------------------------------------------
# Top-level entrypoint
# --------------------------------------------------------------------------------------

def score(
    envelopes_path: str | Path,
    gold_paths: list[str | Path],
    *,
    previous_envelopes: Optional[str | Path] = None,
    report: Optional[str | Path] = None,
    family_weights: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    envelopes = read_jsonl(envelopes_path)
    gold = load_gold(gold_paths)
    previous = read_jsonl(previous_envelopes) if previous_envelopes else None
    run_report = json.loads(Path(report).read_text(encoding="utf-8")) if report else None

    contract = check_contract(envelopes)
    coverage = family_coverage(envelopes)
    abstention = abstention_rate(envelopes)
    website = score_website(envelopes, gold)
    profiles = score_profiles(envelopes, gold)
    jobs = score_jobs(envelopes, gold)
    pooled = pooled_recall_proxy(website, profiles, jobs, coverage, family_weights=family_weights)
    refresh = score_refresh(envelopes, previous)
    budget = score_budget(run_report)
    synthesis = score_synthesis(envelopes)
    total = proxy_total(contract, pooled, website, profiles, refresh, budget, synthesis)

    return {
        "scorer": "signalpost_eval_proxy_v1",
        "claim_boundary": CLAIM_BOUNDARY,
        "envelopes_path": str(envelopes_path),
        "gold_paths": [str(p) for p in gold_paths],
        "gold_label_count": len(gold),
        "contract": contract,
        "family_coverage": coverage,
        "abstention_rate": abstention,
        "website": website,
        "profiles": profiles,
        "jobs": jobs,
        "pooled_recall_proxy": pooled,
        "refresh": refresh,
        "budget": budget,
        "synthesis": synthesis,
        "proxy_score": total,
    }


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Eval proxy report -- {report['envelopes_path']}",
        "",
        f"> {report['claim_boundary']}",
        "",
        f"Gold labels used: {report['gold_label_count']}",
        "",
        "## Proxy score",
        "",
        "| Component | Points |",
        "|---|---|",
    ]
    total = report["proxy_score"]
    for key in ("coverage_points", "accuracy_points", "refresh_points", "synthesis_points", "ux_points"):
        lines.append(f"| {key.replace('_points', '')} | {total[key]} |")
    lines.append(f"| **total** | **{total['total']} / 100** |")
    lines.append(f"| qualifies | {total['qualifies']} |")
    lines.append(f"| wrong-company publications | {total['wrong_company_total']} |")
    lines += ["", "## Contract", "", f"- passed: {report['contract']['passed']}"]
    for issue in report["contract"]["issues"]:
        lines.append(f"  - {issue}")
    lines += ["", "## Family coverage", "", "| Family | Companies available | Fact count |", "|---|---|---|"]
    for fam, stats in report["family_coverage"].items():
        lines.append(f"| {fam} | {stats['companies_available']} ({stats['companies_available_rate']:.0%}) | {stats['fact_count']} |")
    lines += [
        "",
        "## Website precision/recall vs gold",
        "",
        f"- gold companies: {report['website']['gold_companies']} "
        f"(with site: {report['website']['gold_with_site']}, none found: {report['website']['gold_none_found']}, "
        f"uncertain: {report['website']['gold_uncertain']})",
        f"- precision: {report['website']['precision']}",
        f"- company recall: {report['website']['company_recall']}",
        f"- wrong-company publications: {report['website']['wrong_company_publications']}",
        "",
        "## Refresh",
        "",
        f"- checked: {report['refresh']['checked']}",
    ]
    if report["refresh"]["checked"]:
        lines.append(f"- passed: {report['refresh']['passed']}")
    lines += ["", "## Budget", "", f"- checked: {report['budget']['checked']}"]
    if report["budget"]["checked"]:
        lines.append(f"- passed: {report['budget']['passed']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Score Signalpost envelopes against independent gold labels.")
    parser.add_argument("--envelopes", required=True)
    parser.add_argument("--gold", nargs="+", required=True)
    parser.add_argument("--previous")
    parser.add_argument("--report")
    parser.add_argument("--out", required=True)
    parser.add_argument("--markdown")
    args = parser.parse_args()

    result = score(
        args.envelopes, args.gold,
        previous_envelopes=args.previous, report=args.report,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["proxy_score"], ensure_ascii=False, indent=2))

    md_path = Path(args.markdown) if args.markdown else out_path.with_suffix(".md")
    md_path.write_text(to_markdown(result), encoding="utf-8")
    print(f"markdown report: {md_path}")


if __name__ == "__main__":
    main()
