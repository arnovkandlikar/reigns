"""Risk triage + detector routing (FR-B4).

Stage 1 (run concurrently): routes below.
Stage 2: Consistency Probe for high-risk factual claims where Claim Verifier found no evidence.
"""
from __future__ import annotations

import re

from app.models import Claim, ClaimVerdict, DetectorResult

_RANK = {"low": 0, "medium": 1, "high": 2}
_HIGH_TYPES = {"paper", "url", "package", "code_api", "source_summary"}
_SPECIFIC = re.compile(
    r"\d|\b(?:January|February|March|April|May|June|July|August|September|October|November|"
    r"December)\b|\b[A-Z][a-z]+\s+[A-Z][a-z]+\b|\"[^\"]+\"|'[^']+'"
)


def heuristic_risk(claim: Claim) -> str:
    if claim.type in _HIGH_TYPES:
        return "high"
    if _SPECIFIC.search(claim.quote) or claim.type == "number":
        return "high"
    if claim.type == "fact":
        return "medium"
    return "low"


def assign_risk(claim: Claim) -> Claim:
    """Final risk = the higher of the extractor's guess and the heuristic (never downgrade)."""
    h = heuristic_risk(claim)
    if _RANK[h] > _RANK[claim.risk]:
        claim.risk = h  # type: ignore[assignment]
    return claim


def route(claim: Claim) -> list[str]:
    """Stage-1 detector names for a claim. Low risk → nothing (skipped)."""
    if claim.risk == "low":
        return []
    if claim.type in ("paper", "url", "package"):
        return ["reference_auditor"]
    if claim.type == "code_api":
        return ["code_api_checker", "memory_consistency"]
    if claim.type == "source_summary":
        return ["source_faithfulness"]
    if claim.type in ("fact", "number", "other", "code"):
        # memory_consistency (Role C) returns None when nothing relevant was said earlier,
        # so it never changes a verdict unless the claim clashes with the user's own facts.
        return ["claim_verifier", "memory_consistency"]
    return []


def needs_consistency_probe(claim: Claim, stage1: list[DetectorResult]) -> bool:
    """High factual claim and Claim Verifier found no evidence (unverified / error / missing)."""
    if claim.risk != "high" or claim.type not in ("fact", "number", "other"):
        return False
    cv = next((r for r in stage1 if r.detector == "claim_verifier"), None)
    return cv is None or cv.status in ("unverified", "error")


# ---------------------------------------------------------------------------
# Pushback trigger (FR-C4 / FR-B4: "user message that matches pushback patterns")
# ---------------------------------------------------------------------------
PUSHBACK = re.compile(
    r"\b(are you sure|you sure\??|i think (you'?re|you are|that'?s) (wrong|mistaken|incorrect)|"
    r"that'?s (not right|wrong|incorrect)|no,? (it'?s|that'?s)|double[- ]check|really\?|"
    r"i don'?t think so|that doesn'?t sound right|are you certain|you'?re wrong)\b",
    re.I,
)
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_EVIDENCE = re.compile(r"https?://|\"[^\"]{12,}\"|“[^”]{12,}”|according to|source:", re.I)


def is_pushback_without_evidence(user_text: str, prior_chat_text: str) -> bool:
    if not PUSHBACK.search(user_text):
        return False
    if _EVIDENCE.search(user_text):
        return False
    new_numbers = set(_NUM.findall(user_text)) - set(_NUM.findall(prior_chat_text))
    return not new_numbers


def counts(verdicts: list[ClaimVerdict]) -> tuple[int, int]:
    red = sum(1 for v in verdicts if v.final == "red")
    amber = sum(1 for v in verdicts if v.final == "amber")
    return red, amber
