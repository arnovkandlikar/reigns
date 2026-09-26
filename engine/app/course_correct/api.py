"""Course Correct diagnosis and correction prompts (PRD FR-D1–FR-D4, FR-L2)."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid

from app.course_correct.bandit import choose_variant
from app.detectors.base import content_words, snippet
from app.llm import LLMError, complete_json, fast_model_name, llm_available
from app.models import (
    BubbleContent,
    BubbleProblem,
    Claim,
    ClaimVerdict,
    Correction,
    DriftProfile,
    RootCause,
    SessionContext,
)

log = logging.getLogger("reigns.course_correct")

_PROMPT_TYPES = {
    1: "verify_nudge",
    2: "targeted_correction",
    3: "diagnostic_reset",
    4: "fresh_start",
}
_GENERIC_WORDS = frozenset({"claim", "said", "says", "answer", "source", "document"})
_NUMBER_OR_DATE = re.compile(r"\b\d+(?:[.,]\d+)?\b|\b(?:19|20)\d{2}\b")
_MATCH_SCORE = re.compile(r"\s*\(best (?:title )?match \d+(?:\.\d+)?\)", re.IGNORECASE)
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s<>\]\[\"']+", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\]\[\"']+", re.IGNORECASE)
_TAG = re.compile(r"\[(?:verified|unverified)\]", re.IGNORECASE)
_QUESTION = re.compile(r"^Does this claim hold up\?\s*", re.IGNORECASE)


def _lookup_absence(result: object) -> bool:
    """FR-D4: a failed search is uncertainty, even if labeled contradicted upstream."""
    evidence = getattr(result, "evidence", [])
    pieces = [item.snippet for item in evidence] or [getattr(result, "explanation", "")]
    return bool(pieces) and all(
        re.search(r"no matching|not found|could not find|no supporting", part, re.IGNORECASE)
        for part in pieces
    )


def _checkable_source(text: str) -> str | None:
    match = _DOI.search(text) or _URL.search(text)
    return match.group(0).rstrip(".,;)") if match else None


def _clean_claim(verdict: ClaimVerdict, claim: Claim | None) -> str:
    raw = claim.normalized if claim and claim.normalized else verdict.quote
    raw = _QUESTION.sub("", _TAG.sub("", raw))
    raw = re.sub(r"[`*_#]", "", raw)
    raw = " ".join(raw.split()).strip().strip("-:;,.").strip()
    reference = _checkable_source(" ".join(filter(None, [verdict.quote, raw])))
    if reference and reference.lower() not in raw.lower():
        raw += f", DOI {reference}" if reference.startswith("10.") else f", {reference}"
    return raw


def _has_source_doc(session: SessionContext, claim: Claim | None) -> bool:
    return bool(claim and claim.source_ref and claim.source_ref in session.source_docs)

_CAUSE_COPY: dict[str, tuple[str, str, str]] = {
    "knowledge_gap": (
        "knowledge_gap",
        "A claim does not line up with the available evidence.",
        "Recheck the uncertain claim and mark anything the evidence cannot settle.",
    ),
    "anchored_wrong_assumption": (
        "anchored_wrong_assumption",
        "Later claims appear to rely on an earlier unsupported point.",
        "Recheck the failed point first, then redo only the later steps that depend on it.",
    ),
    "caving": (
        "caving",
        "The answer changed after pushback without new evidence.",
        "Compare both answers with the evidence; keep the original if it is supported.",
    ),
    "fabricated_sources": (
        "fabricated_sources",
        "It cited a source that could not be confirmed.",
        "Recheck the cited sources and keep only sources that can be verified.",
    ),
    "outdated_knowledge": (
        "outdated_knowledge",
        "This may depend on information that changes over time.",
        "Recheck the date-sensitive detail and state the date or version used.",
    ),
    "source_drift": (
        "source_drift",
        "The summary includes details the pasted document does not establish.",
        "Recheck the summary against the document and remove or qualify unsupported details.",
    ),
    "api_confusion": (
        "api_confusion",
        "The code appears to use an API that does not match the installed library.",
        "Recheck the library version and replace only the unsupported API calls.",
    ),
}

_HEADLINES = {
    0: "All good so far.",
    1: "Hmm, I couldn't confirm a claim.",
    2: "A few claims don't match the evidence.",
    3: "Some claims conflict with the evidence.",
    4: "This conversation has drifted off course.",
}


def _position(session: SessionContext, claim: Claim) -> int:
    message = session.message(claim.message_id)
    return message.position if message else -1


def _flagged(session: SessionContext) -> list[ClaimVerdict]:
    verdicts = [v for v in session.active_verdicts() if v.final in ("red", "amber")]
    verdicts.sort(key=lambda v: (v.final != "red", -_position_for_verdict(session, v)))
    return verdicts


def _position_for_verdict(session: SessionContext, verdict: ClaimVerdict) -> int:
    claim = session.claims.get(verdict.claim_id)
    return _position(session, claim) if claim else -1


def _result_for(verdict: ClaimVerdict):
    return next(
        (
            result
            for result in verdict.detector_results
            if result.status not in ("supported", "consistent", "error")
        ),
        None,
    )


def _root_cause(verdict: ClaimVerdict) -> str:
    for result in verdict.detector_results:
        if result.status in ("supported", "consistent", "error"):
            continue
        if result.detector == "pushback" or result.status == "caved_without_evidence":
            return "caving"
        if result.detector == "source_faithfulness" or result.status == "not_in_source":
            return "source_drift"
        if result.detector == "code_api_checker" or result.status == "nonexistent_api":
            return "api_confusion"
        if (result.detector == "reference_auditor" and result.status == "contradicted"
                and not _lookup_absence(result)):
            return "fabricated_sources"

    claim_text = verdict.quote.lower()
    if _NUMBER_OR_DATE.search(claim_text) and any(
        cue in claim_text for cue in ("current", "latest", "recent", "version", "price", "as of")
    ):
        return "outdated_knowledge"
    if any(
        result.status in ("likely_hallucination", "uncertain")
        for result in verdict.detector_results
    ):
        return "knowledge_gap"
    return "knowledge_gap"


def _dependency_links(session: SessionContext, failures: list[ClaimVerdict]) -> list[str]:
    """Conservative local fallback when the judge is unavailable."""
    ordered_claims = sorted(
        enumerate(session.claims.values()), key=lambda pair: (_position(session, pair[1]), pair[0])
    )
    links: set[str] = set()
    for failed in failures:
        failed_claim = session.claims.get(failed.claim_id)
        if failed_claim is None:
            continue
        failed_order = next(
            (order for order, candidate in ordered_claims if candidate.claim_id == failed.claim_id),
            -1,
        )
        failed_terms = content_words(failed_claim.normalized) - _GENERIC_WORDS
        if len(failed_terms) < 2:
            continue
        failure_position = _position(session, failed_claim)
        for candidate_order, candidate in ordered_claims:
            if candidate.claim_id == failed.claim_id:
                continue
            candidate_position = _position(session, candidate)
            if candidate_position < failure_position:
                continue
            if candidate_position == failure_position and candidate_order <= failed_order:
                continue
            candidate_terms = content_words(candidate.normalized) - _GENERIC_WORDS
            shared = failed_terms & candidate_terms
            if len(shared) >= 2 or (len(failed_terms) == 2 and failed_terms <= candidate_terms):
                links.add(candidate.claim_id)
    return sorted(links)


async def _judged_dependency_links(
    session: SessionContext, failures: list[ClaimVerdict]
) -> list[str]:
    """Ask the shared judge which later claims actually rely on failed claims."""
    if not failures:
        return []
    ordered = sorted(
        enumerate(session.claims.values()), key=lambda pair: (_position(session, pair[1]), pair[0])
    )
    order_by_id = {claim.claim_id: index for index, (_original, claim) in enumerate(ordered)}
    failed_ids = {verdict.claim_id for verdict in failures}
    first_failure = min(
        (order_by_id[i] for i in failed_ids if i in order_by_id), default=len(ordered)
    )
    candidates = [
        claim
        for index, (_original, claim) in enumerate(ordered)
        if index > first_failure and claim.claim_id not in failed_ids
    ]
    if not candidates:
        return []
    cache_key = (
        "course_correct:dependencies:"
        + ",".join(sorted(failed_ids))
        + ":"
        + ",".join(claim.claim_id for claim in candidates)
    )
    cached = session.cache.get(cache_key)
    if isinstance(cached, list):
        return cached
    if not llm_available():
        return []

    failed_lines = [
        {
            "claim_id": v.claim_id,
            "order": order_by_id[v.claim_id],
            "claim": _clip(session.claims[v.claim_id].normalized, 240),
        }
        for v in failures
        if v.claim_id in session.claims
    ]
    later_lines = [
        {
            "claim_id": claim.claim_id,
            "order": order_by_id[claim.claim_id],
            "claim": _clip(claim.normalized, 240),
        }
        for claim in candidates
    ]
    try:
        judged = await asyncio.wait_for(
            complete_json(
                "You judge claim dependencies in a conversation ledger. A later claim depends "
                "on a failed claim only if the later claim needs that failed claim to be true. "
                "Shared words or related topics alone are not dependence. Return JSON with "
                "dependent_claim_ids, an array of IDs from later_claims only. Be conservative.",
                f"Failed claims: {failed_lines}\nLater claims: {later_lines}",
                temperature=0,
                max_tokens=400,
                model=fast_model_name(),
            ),
            timeout=2.8,
        )
    except (LLMError, TimeoutError) as exc:
        log.warning("dependency judge unavailable; using local fallback: %s", exc)
        return []
    if not isinstance(judged, dict) or not isinstance(judged.get("dependent_claim_ids"), list):
        return []
    candidate_ids = {claim.claim_id for claim in candidates}
    links = sorted(
        {
            value
            for value in judged["dependent_claim_ids"]
            if isinstance(value, str) and value in candidate_ids
        }
    )
    session.cache[cache_key] = links
    return links


async def diagnose(session: SessionContext) -> DriftProfile:
    """Build a drift profile using an LLM judge for the blast radius (FR-D1)."""
    failures = _flagged(session)
    causes: list[RootCause] = []
    for verdict in failures:
        cause = _root_cause(verdict)
        if cause not in causes:
            causes.append(cause)  # type: ignore[arg-type]

    blast_radius = await _judged_dependency_links(session, failures)
    if blast_radius and "anchored_wrong_assumption" not in causes:
        causes.insert(0, "anchored_wrong_assumption")

    failed_messages = {_position_for_verdict(session, verdict) for verdict in failures}
    failed_messages.discard(-1)
    trend = "worsening" if len(failed_messages) > 1 or len(failures) >= 3 else "isolated"
    return DriftProfile(
        failures=[verdict.claim_id for verdict in failures],
        blast_radius=blast_radius,
        trend=trend,
        root_causes=causes,
    )


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _direct_issue(verdict: ClaimVerdict) -> bool:
    """A check can support confident copy without treating uncertainty as proof."""
    return any(
        result.confidence >= 0.8
        and (
            (result.status == "contradicted" and bool(result.evidence)
             and not _lookup_absence(result))
            or result.status in ("nonexistent_api", "caved_without_evidence")
        )
        for result in verdict.detector_results
    )


def _source_absence(verdict: ClaimVerdict, session: SessionContext | None = None) -> bool:
    if session is not None and not _has_source_doc(session, session.claims.get(verdict.claim_id)):
        return False
    return any(
        result.status == "not_in_source" and result.confidence >= 0.8
        for result in verdict.detector_results
    )


def _problem_text(verdict: ClaimVerdict, claim: Claim | None = None,
                  session: SessionContext | None = None) -> str:
    """Show Claude's claim and a plain reason, never a clipped detector note."""
    statuses = {result.status for result in verdict.detector_results}
    if any(result.status == "contradicted" and result.evidence
           and not _lookup_absence(result) for result in verdict.detector_results):
        reason = "Evidence conflicts with this."
    elif "nonexistent_api" in statuses:
        reason = "The library does not provide that API."
    elif "caved_without_evidence" in statuses:
        reason = "Claude changed its answer without new evidence."
    elif _source_absence(verdict, session):
        reason = "The source does not support this."
    elif "not_in_source" in statuses and session is not None and _has_source_doc(session, claim):
        reason = "I could not find this in the source."
    else:
        reason = "I could not verify this."

    candidates = [_clean_claim(verdict, claim)]
    sentences = []
    for text in candidates:
        flat = " ".join(text.split()).strip()
        if flat:
            sentences.append(flat if re.search(r"[.!?][\"']?$", flat) else f"{flat}.")
    for sentence in sentences:
        with_reason = f"{sentence} {reason}"
        if len(with_reason) <= 120:
            return with_reason
    for sentence in sentences:
        if len(sentence) <= 120:
            return sentence
        first = re.split(r"(?<=[.!?])\s+(?=[A-Z])", sentence, maxsplit=1)[0]
        if first != sentence and 30 <= len(first) <= 120:
            return first

    return {
        "paper": "Claude cited a paper that needs checking. Open Details for the full citation.",
        "source_summary": "A detail in Claude's summary needs checking against the document. See Details.",
        "code_api": "Claude used an API that needs checking. Open Details for the exact call.",
    }.get(
        verdict.type,
        "A longer claim from Claude needs checking. Open Details for its full wording.",
    )


def _evidence_url(verdict: ClaimVerdict) -> str | None:
    for result in verdict.detector_results:
        for item in result.evidence:
            if item.url:
                return item.url
    return None


def _evidence_summary(verdict: ClaimVerdict, session: SessionContext | None = None) -> str:
    for result in verdict.detector_results:
        if result.status == "not_in_source":
            if session is not None and not _has_source_doc(
                session, session.claims.get(verdict.claim_id)
            ):
                continue
            return "No supporting passage was found in the relevant source excerpts."
        if result.status in ("error", "unverified", "uncertain"):
            continue
        if result.evidence:
            if result.detector == "reference_auditor":
                missing = [
                    item.source
                    for item in result.evidence
                    if item.source in ("Crossref", "OpenAlex")
                    and item.snippet.lower().startswith("no matching work")
                ]
                if missing:
                    sources = " or ".join(dict.fromkeys(missing))
                    return f"No matching paper found in {sources}."
            plain = _MATCH_SCORE.sub("", result.evidence[0].snippet)
            return snippet(re.sub(r"\.{2,}", ".", plain).strip(), 240)
    return "No direct supporting quote was returned."


def _pattern(causes: list[RootCause]) -> tuple[str, str]:
    cause = causes[0] if causes else "knowledge_gap"
    _name, pattern, redo = _CAUSE_COPY[cause]
    return pattern, redo


def _is_reigns_prompt(text: str, session: SessionContext) -> bool:
    """Exclude pasted Fix it text from the user's original task question."""
    normalized = " ".join(text.split()).casefold()
    for correction in session.corrections:
        if not correction.inserted:
            continue
        prompt = " ".join(correction.text.split()).casefold()
        if prompt and (normalized == prompt or (len(prompt) >= 80 and prompt in normalized)):
            return True
    return (
        normalized.startswith("start a fresh chat with this handoff:")
        or (
            "what's wrong and the evidence:" in normalized
            and "pattern:" in normalized
            and "[verified] or [unverified]" in normalized
        )
        or (
            "the pattern appears to be" in normalized
            and "mark factual claims [verified] or [unverified]" in normalized
        )
    )


def _correction_text(
    level: int,
    profile: DriftProfile,
    flagged: list[ClaimVerdict],
    session: SessionContext,
    variant: str,
) -> str:
    pattern, redo = _pattern(profile.root_causes)
    issue_lines = []
    for index, verdict in enumerate(flagged, start=1):
        evidence = _evidence_summary(verdict, session)
        clean = _clean_claim(verdict, session.claims.get(verdict.claim_id))
        if variant in ("v2", "v3"):
            issue_lines.append(f"{index}. Recheck {clean} — Evidence: {evidence}")
        else:
            issue_lines.append(f"{index}. {clean} — Evidence: {evidence}")
    issues = "\n".join(issue_lines) or "No active flagged claim is available to recheck."
    target_ids = list(dict.fromkeys(profile.failures + profile.blast_radius))
    source_claims = [session.claims[cid] for cid in target_ids if cid in session.claims]
    target = "; ".join(
        _clean_claim(session.verdicts[claim.claim_id], claim)
        for claim in source_claims
        if claim.claim_id in session.verdicts
    )
    if not target:
        target = "the specific flagged points above"

    # FR-D2: each level keeps all five required parts, with the requested level-specific length.
    if level == 1:
        first_issue = (
            f"this claim: {_clean_claim(flagged[0], session.claims.get(flagged[0].claim_id))} — Evidence: "
            f"{_evidence_summary(flagged[0], session).rstrip(' .')}"
            if flagged
            else target
        )
        opening = {
            "v1": "Please check",
            "v2": "Could you verify",
            "v3": "Quick checklist: verify",
        }[variant]
        return (
            f"{opening} {first_issue}; the pattern appears to be {pattern.lower()} "
            f"Recheck only {target}; use evidence for the rest of this chat, mark factual claims "
            "[verified] or [unverified], and say ‘I don't know’ if unsure—keep your original "
            "answer if the evidence supports it."
        )

    if level == 2:
        opening = "Quick checklist of what to recheck:\n" if variant == "v3" else ""
        return (
            f"{opening}What's wrong and the evidence:\n{issues}\n\n"
            f"Pattern: {pattern}\n\nRedo only this: {redo} Target: {target}.\n\n"
            "For the rest of this chat, use evidence for factual claims. Mark each claim "
            "[verified] or [unverified]; it is fine to say ‘I don't know’. If the evidence supports "
            "your original answer, keep it and explain why."
        )

    if level == 3:
        opening = (
            "Work through this checklist:\n"
            if variant == "v3"
            else ("First ask what the evidence actually establishes.\n" if variant == "v2" else "")
        )
        return (
            f"{opening}I want to pause and correct the specific issues below.\n\n"
            f"What's wrong and the evidence:\n{issues}\n\n"
            f"Pattern: {pattern} Redo only this: {redo} Target: {target}.\n\n"
            "Going forward, use evidence for factual claims and avoid building later steps on an "
            "unverified point. Mark claims [verified] or [unverified]. It is fine to say ‘I don't "
            "know’; if the evidence supports your original answer, keep it and explain why."
        )

    user_questions = [m.text.strip() for m in sorted(session.messages, key=lambda m: m.position)
                      if m.role == "user" and m.text.strip()
                      and not _is_reigns_prompt(m.text, session)]
    original_question = next((text for text in reversed(user_questions) if "?" in text),
                             user_questions[-1] if user_questions else "") or next(
        (claim.context for claim in source_claims
         if claim.context and not _is_reigns_prompt(claim.context, session)),
        "Original question unavailable",
    )
    confirmed = [
        _clean_claim(verdict, session.claims.get(verdict.claim_id))
        for verdict in session.active_verdicts() if verdict.final == "green"
    ]
    confirmed_text = "; ".join(confirmed[:3]) if confirmed else "No claims independently confirmed yet."
    return (
        "Start a fresh chat with this handoff:\n"
        + f"- Original question: {original_question}\n"
        + f"- Confirmed: {confirmed_text}\n"
        f"- Problems and evidence:\n{issues}\n"
        f"- Pattern: {pattern}\n"
        f"- Next step: {redo} Recheck {target}.\n"
        "- Guardrails: use only evidence for factual claims; do not rely on unsupported points.\n"
        "- Format: mark claims [verified] or [unverified]; say ‘I don't know’ when evidence is "
        "missing. Keep any original answer that the evidence supports."
    )


async def build_bubble(level: int, profile: DriftProfile, session: SessionContext) -> BubbleContent:
    """Create the level-matched plain-English bubble and correction (PRD FR-D2–FR-D4)."""
    level = max(0, min(4, int(level)))
    flagged = _flagged(session)
    visible = flagged
    direct_count = sum(_direct_issue(v) for v in visible)
    source_absence = any(_source_absence(v, session) for v in visible)
    substantiated = bool(direct_count or source_absence)
    # FR-D4: an upstream red verdict backed only by failed lookup is still a verify nudge.
    if visible and not substantiated:
        level = 1
    causes = profile.root_causes
    pattern, _redo = _pattern(causes)
    if level == 1 and flagged:
        headline = (
            "I found a claim worth rechecking."
            if substantiated
            else "I couldn't confirm this claim."
        )
    elif level >= 2 and visible and not substantiated:
        headline = (
            "I couldn't confirm this claim."
            if len(visible) == 1
            else "I couldn't confirm these claims."
        )
    elif level >= 2 and direct_count and direct_count < len(visible):
        headline = "Some claims have clear issues; others need checking."
    elif level >= 2 and source_absence and not all(_source_absence(v) for v in visible):
        headline = "Some details lack support; others need checking."
    elif level >= 2 and direct_count and causes and causes[0] == "knowledge_gap":
        headline = "Some claims conflict with the evidence."
    elif level >= 2 and causes:
        headline = {
            "fabricated_sources": "Heads up: a cited source could not be confirmed.",
            "source_drift": "Heads up: the summary drifts from the document.",
            "api_confusion": "Heads up: this code uses an unsupported API.",
            "caving": "The answer changed without new evidence.",
            "knowledge_gap": "Some details are uncertain and need checking.",
            "outdated_knowledge": "This detail may be out of date.",
            "anchored_wrong_assumption": "Later steps may rely on a wrong assumption.",
        }.get(causes[0], _HEADLINES[level])
    else:
        headline = _HEADLINES[level]
    headline = _clip(headline, 60)

    problems = [
        BubbleProblem(
            claim_id=verdict.claim_id,
            text=_problem_text(verdict, session.claims.get(verdict.claim_id), session),
            evidence_url=_evidence_url(verdict),
        )
        for verdict in visible
    ]

    if visible and direct_count == len(visible):
        confidence_label = "Very sure"
        confidence_reason = "Each problem has a direct check behind it."
    elif direct_count:
        confidence_label = "Fairly sure"
        confidence_reason = "Some problems have direct support; others still need checking."
    elif source_absence:
        confidence_label = "Fairly sure"
        confidence_reason = "The supplied document does not support at least one detail."
    elif visible:
        confidence_label = "Not sure"
        confidence_reason = "The checks could not confirm or rule out these claims."
    else:
        confidence_label = ""
        confidence_reason = ""

    correction = None
    if level >= 1 and flagged:
        failure_type = causes[0] if causes else "knowledge_gap"
        prompt_type = _PROMPT_TYPES[level]
        variant, variant_id = await choose_variant(failure_type, prompt_type)
        correction = Correction(
            correction_id=str(uuid.uuid4()),
            prompt_type=prompt_type,
            text=_correction_text(level, profile, flagged, session, variant),
        )
        session.cache[f"course_correct:variant:{correction.correction_id}"] = variant_id

    action = {
        0: "",
        1: "Want Claude to double-check this?",
        2: "I wrote a targeted correction prompt.",
        3: "I wrote a prompt to recheck the linked issues.",
        4: "Consider starting a fresh chat with this handoff.",
    }[level]
    if not visible:
        pattern_text = ""
    elif substantiated:
        pattern_text = pattern
    else:
        pattern_text = "The checks did not establish whether these claims are correct."
    return BubbleContent(
        level=level,
        headline=headline,
        problems=problems,
        pattern_text=_clip(pattern_text, 200),
        confidence_label=confidence_label,
        confidence_reason=confidence_reason,
        action_text=action,
        correction=correction,
    )


def on_fix_outcome(session: SessionContext, correction_id: str, fixed: bool) -> None:
    """Keep the verified outcome available for the bandit stage (FR-D5 / FR-L2)."""
    correction = next((c for c in session.corrections if c.correction_id == correction_id), None)
    if correction and not fixed:
        next_reply = next((m for m in reversed(session.messages) if m.role == "assistant"), None)
        target_sources = {
            _checkable_source(session.claims[cid].quote + " " + session.claims[cid].normalized)
            for cid in correction.target_claim_ids if cid in session.claims
        }
        target_sources.discard(None)
        if next_reply and any(source.lower() in next_reply.text.lower()
                              for source in target_sources):
            # FR-D3: standing firm with a checkable citation resolves an absence-only alarm.
            absence_only = all(
                _lookup_absence(result)
                for cid in correction.target_claim_ids
                if cid in session.verdicts
                for result in session.verdicts[cid].detector_results
                if result.status not in ("supported", "consistent", "error")
            )
            if absence_only:
                fixed = True
                correction.fixed = True
                correction.fix_reason = "The model stood firm and supplied a checkable source."
                session.resolved_claim_ids.update(correction.target_claim_ids)
    outcomes = session.cache.setdefault("course_correct:fix_outcomes", {})
    outcomes[correction_id] = bool(fixed)
