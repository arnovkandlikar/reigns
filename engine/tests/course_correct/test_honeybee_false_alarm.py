"""Regression for DOI-backed papers misreported as absent (FR-D1-D5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.course_correct import api
from app.models import (
    ChatMessage,
    Claim,
    ClaimVerdict,
    CorrectionRecord,
    DetectorResult,
    Evidence,
    SessionContext,
)


@pytest.fixture
def honeybee_session() -> tuple[SessionContext, dict]:
    data = json.loads((Path(__file__).parent / "fixtures" / "honeybee_false_alarm.json").read_text())
    session = SessionContext(session_id="honeybee-false-alarm")
    session.messages = [
        ChatMessage(message_id="user", role="user", text=data["question"], position=0),
        ChatMessage(message_id="reply", role="assistant", text=data["reply"], position=1),
    ]
    for paper in data["papers"]:
        claim = Claim(
            claim_id=paper["id"], message_id="reply", quote=paper["quote"],
            normalized=paper["normalized"], type="paper", risk="high",
            context=data["question"],
        )
        session.claims[claim.claim_id] = claim
        session.verdicts[claim.claim_id] = ClaimVerdict(
            claim_id=claim.claim_id, quote=claim.quote, type="paper", risk="high", final="red",
            detector_results=[DetectorResult(
                detector="reference_auditor", status="contradicted", confidence=0.95,
                evidence=[Evidence(**item) for item in data["auditor_evidence"]],
                explanation="No matching paper found in Crossref or OpenAlex.",
            )],
        )
    return session, data


async def test_absent_lookup_with_dois_only_offers_verify_nudge(honeybee_session) -> None:
    session, data = honeybee_session
    profile = await api.diagnose(session)
    bubble = await api.build_bubble(4, profile, session)

    assert profile.blast_radius == []
    assert "anchored_wrong_assumption" not in profile.root_causes
    assert bubble.level == 1
    assert bubble.correction and bubble.correction.prompt_type == "verify_nudge"
    assert "fresh chat" not in bubble.correction.text.lower()
    assert bubble.confidence_label != "Very sure"
    for paper in data["papers"]:
        assert paper["doi"] in bubble.correction.text
    assert all("[verified]" not in problem.text for problem in bubble.problems)
    assert "Does this claim hold up?" not in bubble.correction.text
    assert "[verified] Crossref confirms" not in bubble.correction.text


async def test_genuine_counter_source_handoff_contains_original_question(honeybee_session) -> None:
    session, data = honeybee_session
    verdict = session.verdicts["anwar"]
    verdict.detector_results[0].evidence = [Evidence(
        source="Publisher", url="https://example.org/counter-record",
        snippet="The DOI identifies a different article by different authors.",
    )]
    session.verdicts.pop("ngo")
    session.claims.pop("ngo")
    profile = await api.diagnose(session)

    bubble = await api.build_bubble(4, profile, session)

    assert bubble.correction and bubble.correction.prompt_type == "fresh_start"
    assert data["question"] in bubble.correction.text
    assert "- Confirmed:" in bubble.correction.text
    assert "- Next step:" in bubble.correction.text
    assert "First question: what does the evidence establish?" not in bubble.correction.text
    assert "Later claims appear to rely" not in bubble.correction.text


@pytest.mark.parametrize("prompt_case", ["inserted_template", "missing_record", "inserted_short"])
async def test_fresh_start_after_inserted_fix_uses_original_user_question(
    honeybee_session, prompt_case: str
) -> None:
    session, data = honeybee_session
    verdict = session.verdicts["anwar"]
    verdict.detector_results[0].evidence = [Evidence(
        source="Publisher", url="https://example.org/counter-record",
        snippet="The DOI identifies a different article by different authors.",
    )]
    session.verdicts.pop("ngo")
    session.claims.pop("ngo")
    profile = await api.diagnose(session)
    fix_text = api._correction_text(2, profile, [verdict], session, "v3")
    if prompt_case == "inserted_short":
        fix_text = "Can you verify whether the Anwar DOI is correct?"
    if prompt_case != "missing_record":
        session.corrections.append(CorrectionRecord(
            correction_id="fix-1", prompt_type="targeted_correction", level=2,
            text=fix_text, inserted=True, target_claim_ids=["anwar"],
        ))
    session.messages.extend([
        ChatMessage(message_id="fix-paste", role="user", text=fix_text, position=2),
        ChatMessage(message_id="next-reply", role="assistant", text="The citation still needs checking.", position=3),
    ])

    bubble = await api.build_bubble(4, profile, session)

    assert bubble.correction and bubble.correction.prompt_type == "fresh_start"
    assert f"- Original question: {data['question']}\n" in bubble.correction.text
    assert "- Original question: Quick checklist" not in bubble.correction.text


async def test_missing_chat_document_never_claims_source_excerpts_were_checked() -> None:
    claim = Claim(
        claim_id="summary", message_id="reply", quote="The report lists 35 jobs.",
        normalized="The report lists 35 jobs.", type="source_summary", risk="high",
        source_ref="missing-document",
    )
    verdict = ClaimVerdict(
        claim_id=claim.claim_id, quote=claim.quote, type=claim.type, risk=claim.risk,
        final="red", detector_results=[DetectorResult(
            detector="source_faithfulness", status="not_in_source", confidence=0.9,
            explanation="No supporting passage was found.",
        )],
    )
    session = SessionContext(
        session_id="no-document", claims={claim.claim_id: claim},
        verdicts={claim.claim_id: verdict},
    )
    bubble = await api.build_bubble(4, await api.diagnose(session), session)

    assert bubble.correction and bubble.correction.prompt_type == "verify_nudge"
    assert "relevant source excerpts" not in bubble.correction.text
    assert "source does not support" not in bubble.problems[0].text


def test_firm_sourced_reply_resolves_absence_only_alarm(honeybee_session) -> None:
    session, data = honeybee_session
    session.corrections.append(CorrectionRecord(
        correction_id="fix-1", prompt_type="verify_nudge", level=1, text="Check these DOIs.",
        inserted=True, fixed=False, target_claim_ids=[p["id"] for p in data["papers"]],
    ))
    session.messages.append(ChatMessage(
        message_id="next", role="assistant", text=data["firm_reply"], position=3,
    ))

    api.on_fix_outcome(session, "fix-1", False)

    assert session.corrections[0].fixed is True
    assert session.resolved_claim_ids == {"anwar", "ngo"}
    assert session.cache["course_correct:fix_outcomes"]["fix-1"] is True
