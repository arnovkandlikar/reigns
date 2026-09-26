"""Aggregate detector results into one final status per claim (FR-B5, PRD §8.4).

Precision rule: a claim is red only if an independent signal backs it. When in doubt → amber.
"""
from __future__ import annotations

from app.models import Claim, ClaimVerdict, DetectorResult, FinalStatus

DEFAULT_NOT_IN_SOURCE_RED = 0.8  # FR-L4 (Role D) may raise this per detector
# §8.4 "likely_hallucination AND Claim Verifier found no supporting evidence → red": with no
# evidence either way, only very scattered answers count as red; otherwise amber (precision
# rule "when in doubt → amber"; docs/requests.md 04:55 from Role A).
LIKELY_HALLUCINATION_RED = 0.85


def final_status(
    claim: Claim,
    results: list[DetectorResult],
    not_in_source_red: float = DEFAULT_NOT_IN_SOURCE_RED,
) -> FinalStatus:
    if claim.risk == "low":
        return "skipped"
    usable = [r for r in results if r.status != "error"]
    if not usable:
        return "skipped"

    statuses = {r.status for r in usable}
    cv = next((r for r in usable if r.detector == "claim_verifier"), None)
    cv_found_no_support = cv is not None and cv.status != "supported"

    # 🔴 red
    for r in usable:
        if r.status == "contradicted" and r.evidence:
            return "red"
        if r.status in ("caved_without_evidence", "nonexistent_api"):
            return "red"
        if r.status == "not_in_source" and r.confidence >= not_in_source_red:
            return "red"
    lh = [r for r in usable if r.status == "likely_hallucination"]
    if lh and cv_found_no_support and max(r.confidence for r in lh) >= LIKELY_HALLUCINATION_RED:
        return "red"

    # 🟠 amber
    amber = {"unverified", "uncertain", "likely_hallucination", "not_in_source", "contradicted"}
    if statuses & amber:  # contradicted w/o evidence falls here (precision rule)
        return "amber"

    # 🟢 green
    if statuses & {"supported", "consistent"}:
        return "green"
    return "skipped"


def is_caved(results: list[DetectorResult]) -> bool:
    return any(r.status == "caved_without_evidence" for r in results)


def to_verdict(claim: Claim, results: list[DetectorResult], final: FinalStatus) -> ClaimVerdict:
    return ClaimVerdict(
        claim_id=claim.claim_id,
        quote=claim.quote,
        type=claim.type,
        risk=claim.risk,
        final=final,
        detector_results=results,
    )
