"""Regressions from a handoff repeated after a false-statement conversation."""

from __future__ import annotations

from app.course_correct import api
from app.models import (
    ChatMessage,
    Claim,
    ClaimVerdict,
    DetectorResult,
    DriftProfile,
    Evidence,
    SessionContext,
)


def _claim(session: SessionContext, claim_id: str, normalized: str, position: int,
           final: str = "red") -> ClaimVerdict:
    message_id = f"answer-{position}"
    session.messages.append(ChatMessage(
        message_id=message_id, role="assistant", text=normalized, position=position,
    ))
    claim = Claim(
        claim_id=claim_id, message_id=message_id, quote=normalized,
        normalized=normalized, type="fact", risk="high",
    )
    verdict = ClaimVerdict(
        claim_id=claim_id, quote=normalized, type="fact", risk="high", final=final,
        detector_results=[DetectorResult(
            detector="claim_verifier", status="contradicted" if final == "red" else "supported",
            confidence=0.95, explanation="Checked against an official source.",
            evidence=[Evidence(source="Official source", snippet="The source states a different fact.")],
        )],
    )
    session.claims[claim_id] = claim
    session.verdicts[claim_id] = verdict
    return verdict


def test_handoff_evidence_comes_from_the_failed_result_only() -> None:
    session = SessionContext(session_id="mixed-results")
    amber = _claim(session, "amber", "The date is 1991", 1, "amber")
    amber.detector_results = [
        DetectorResult(
            detector="claim_verifier", status="unverified", confidence=0.5,
            explanation="No source settled the date.",
        ),
        DetectorResult(
            detector="consistency_probe", status="consistent", confidence=0.95,
            explanation="The sampled answers agreed.",
            evidence=[Evidence(source="Consistency Probe", snippet="5/5 agree with the original answer")],
        ),
    ]
    red = _claim(session, "red", "The date is 1992", 2)
    red.detector_results.insert(0, DetectorResult(
        detector="consistency_probe", status="supported", confidence=0.8,
        explanation="Samples agreed.",
        evidence=[Evidence(source="Consistency Probe", snippet="All samples agreed",
                           url="https://example.org/passing")],
    ))
    red.detector_results[1].evidence = [Evidence(
        source="Official source", snippet="The official record says 1993.",
        url="https://example.org/failing",
    )]

    prompt = api._correction_text(
        4, DriftProfile(failures=["amber", "red"]), [amber, red], session, "v1",
    )

    assert "The official record says 1993." in prompt
    assert "5/5 agree" not in prompt
    assert "All samples agreed" not in prompt
    assert "The date is 1991 — Evidence: No direct supporting quote was returned." in prompt
    assert api._evidence_url(red) == "https://example.org/failing"


def test_handoff_deduplicates_problems_and_confirmed_claims_using_newest_text() -> None:
    session = SessionContext(session_id="duplicate-rounds")
    older_red = _claim(session, "old-red", "water boils at 50°C at sea level.", 1)
    _claim(session, "old-green", "guido van rossum released Python in 1991.", 2, "green")
    newer_red = _claim(session, "new-red", "Water boils at 50°C at sea level!", 3)
    _claim(session, "new-green", "Guido van Rossum released Python in 1991!", 4, "green")
    profile = DriftProfile(failures=[older_red.claim_id, newer_red.claim_id])

    prompt = api._correction_text(4, profile, [older_red, newer_red], session, "v1")

    problems = prompt.split("- Problems and evidence:\n", 1)[1].split("\n- Pattern:", 1)[0]
    confirmed = prompt.split("- Confirmed: ", 1)[1].split("\n", 1)[0]
    assert problems.count("Evidence:") == 1
    assert "Water boils at 50°C at sea level" in problems
    assert "water boils at 50°C" not in problems
    assert "Water boils at 50°C" in prompt.split("- Next step:", 1)[1]
    assert confirmed == "Guido van Rossum released Python in 1991!"


def test_handoff_uses_neutral_pattern_without_judged_dependency() -> None:
    session = SessionContext(session_id="independent-facts")
    verdicts = [
        _claim(session, "one", "Water boils at 50°C at sea level", 1),
        _claim(session, "two", "Python was released in 1991", 2),
        _claim(session, "three", "The moon is made of cheese", 3),
    ]
    profile = DriftProfile(
        failures=[v.claim_id for v in verdicts],
        root_causes=["anchored_wrong_assumption", "knowledge_gap"],
        blast_radius=[],
    )

    prompt = api._correction_text(4, profile, verdicts, session, "v1")

    assert "- Pattern: Separate factual errors; recheck each one independently." in prompt
    assert "Later claims appear to rely" not in prompt


def test_handoff_uses_last_real_user_message_after_two_reign_handoffs() -> None:
    session = SessionContext(session_id="twice-pasted")
    session.messages.append(ChatMessage(
        message_id="user-original", role="user", text="Give me 3 false statements", position=0,
    ))
    verdict = _claim(session, "one", "Water boils at 50°C at sea level", 1)
    session.messages.extend([
        ChatMessage(
            message_id="handoff-one", role="user", position=2,
            text="Start a fresh chat with this handoff:\n- Problems and evidence: water boils at 50°C",
        ),
        ChatMessage(
            message_id="handoff-two", role="user", position=4,
            text="Start a fresh chat with this handoff:\n- Original question: "
                 "Start a fresh chat with this handoff:\n- Problems and evidence: repeated claim",
        ),
    ])

    prompt = api._correction_text(
        4, DriftProfile(failures=[verdict.claim_id]), [verdict], session, "v1",
    )

    assert "- Original question: Give me 3 false statements\n" in prompt
    assert "- Original question: Start a fresh chat" not in prompt


async def test_no_flagged_claims_means_no_fresh_start_handoff() -> None:
    session = SessionContext(session_id="false-statements")
    session.messages.append(ChatMessage(
        message_id="user-original", role="user", text="Give me 3 false statements", position=0,
    ))

    bubble = await api.build_bubble(4, DriftProfile(), session)

    assert bubble.level == 0
    assert bubble.correction is None
    assert bubble.action_text == ""
