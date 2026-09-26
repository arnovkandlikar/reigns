"""Role D Course Correct tests using the shared scenarios (PRD FR-D1–FR-D4)."""

from __future__ import annotations

import pytest

from app.course_correct import api
from app.models import Claim, ClaimVerdict, DetectorResult, DriftProfile, Evidence, SessionContext


@pytest.fixture
def summary_session(load_scenario) -> SessionContext:
    scenario = load_scenario("summary_drift")
    session = SessionContext(session_id="summary-test")
    results_by_claim = scenario["expected"]["detector_results"]

    for claim_data in scenario["expected"]["claims"]:
        claim = Claim(**claim_data)
        session.claims[claim.claim_id] = claim
        results = [DetectorResult(**result) for result in results_by_claim.get(claim.claim_id, [])]
        status = "red" if any(result.status == "not_in_source" for result in results) else "green"
        session.verdicts[claim.claim_id] = ClaimVerdict(
            claim_id=claim.claim_id,
            quote=claim.quote,
            type=claim.type,
            risk=claim.risk,
            final=status,
            detector_results=results,
        )
    return session


async def test_diagnosis_uses_active_summary_failure_and_names_source_drift(
    summary_session: SessionContext,
) -> None:
    profile = await api.diagnose(summary_session)

    assert len(profile.failures) == 1
    assert profile.blast_radius == []
    assert profile.trend == "isolated"
    assert profile.root_causes == ["source_drift"]


@pytest.mark.parametrize(
    ("level", "prompt_type"),
    [
        (1, "verify_nudge"),
        (2, "targeted_correction"),
        (3, "diagnostic_reset"),
        (4, "fresh_start"),
    ],
)
async def test_each_nonzero_level_builds_a_level_matched_prompt(
    summary_session: SessionContext, level: int, prompt_type: str
) -> None:
    profile = await api.diagnose(summary_session)
    bubble = await api.build_bubble(level, profile, summary_session)

    assert bubble.level == level
    assert len(bubble.headline) <= 60
    assert len(bubble.problems) <= 3
    assert all(len(problem.text) <= 120 for problem in bubble.problems)
    assert len(bubble.pattern_text) <= 200
    assert bubble.correction is not None
    assert bubble.correction.prompt_type == prompt_type
    assert "[verified]" in bubble.correction.text
    assert "[unverified]" in bubble.correction.text
    assert "I don't know" in bubble.correction.text


async def test_level_zero_has_no_correction(summary_session: SessionContext) -> None:
    profile = await api.diagnose(summary_session)

    bubble = await api.build_bubble(0, profile, summary_session)

    assert bubble.level == 0
    assert bubble.correction is None


async def test_unverified_red_bubble_uses_cautious_claim_copy() -> None:
    quote = "The Eiffel Tower was completed in 1899."
    claim = Claim(
        claim_id="unverified-1",
        message_id="answer-1",
        quote=quote,
        normalized=quote,
        type="fact",
        risk="high",
    )
    verdict = ClaimVerdict(
        claim_id=claim.claim_id,
        quote=quote,
        type=claim.type,
        risk=claim.risk,
        final="red",
        detector_results=[
            DetectorResult(
                detector="claim_verifier",
                status="unverified",
                confidence=0.9,
                explanation="None of the snippets mention a completion date, so it can't be confirmed or denied.",
            ),
            DetectorResult(
                detector="consistency_probe",
                status="likely_hallucination",
                confidence=0.9,
                explanation="The sampled answers disagreed.",
            ),
        ],
    )
    session = SessionContext(
        session_id="unverified-copy",
        claims={claim.claim_id: claim},
        verdicts={claim.claim_id: verdict},
    )
    profile = DriftProfile(failures=[claim.claim_id], root_causes=["knowledge_gap"])

    bubble = await api.build_bubble(4, profile, session)

    assert bubble.headline == "I couldn't confirm this claim."
    assert bubble.confidence_label == "Not sure"
    assert bubble.problems[0].text == f"{quote} I could not verify this."
    assert "snippets" not in bubble.problems[0].text
    assert "confident guess" not in bubble.pattern_text


async def test_mixed_evidence_bubble_limits_confidence_to_fairly_sure() -> None:
    quotes = ["The tower opened in 1899.", "The tower is 330 metres tall."]
    claims = {
        str(index): Claim(
            claim_id=str(index),
            message_id="answer-1",
            quote=quote,
            normalized=quote,
            type="fact",
            risk="high",
        )
        for index, quote in enumerate(quotes)
    }
    verdicts = {
        "0": ClaimVerdict(
            claim_id="0",
            quote=quotes[0],
            type="fact",
            risk="high",
            final="red",
            detector_results=[
                DetectorResult(
                    detector="claim_verifier",
                    status="contradicted",
                    confidence=0.95,
                    evidence=[Evidence(source="Wikipedia", snippet="Opened in 1889")],
                    explanation="The date is contradicted.",
                )
            ],
        ),
        "1": ClaimVerdict(
            claim_id="1",
            quote=quotes[1],
            type="fact",
            risk="high",
            final="amber",
            detector_results=[
                DetectorResult(
                    detector="claim_verifier",
                    status="unverified",
                    confidence=0.5,
                    explanation="Could not confirm the height.",
                )
            ],
        ),
    }
    session = SessionContext(session_id="mixed-copy", claims=claims, verdicts=verdicts)
    profile = DriftProfile(failures=["0", "1"], root_causes=["knowledge_gap"])

    bubble = await api.build_bubble(3, profile, session)

    assert bubble.headline == "Some claims have clear issues; others need checking."
    assert bubble.confidence_label == "Fairly sure"
    assert bubble.problems[0].text == f"{quotes[0]} Evidence conflicts with this."
    assert bubble.problems[1].text == f"{quotes[1]} I could not verify this."


def test_long_problem_copy_ends_at_a_complete_sentence() -> None:
    quote = (
        "The report says the new transit network will open next year and carry millions of "
        "passengers every day across several cities, even though its planning document is "
        "still unfinished and no funding has been approved."
    )
    verdict = ClaimVerdict(
        claim_id="long-1",
        quote=quote,
        type="fact",
        risk="high",
        final="amber",
        detector_results=[
            DetectorResult(
                detector="claim_verifier",
                status="unverified",
                confidence=0.5,
                explanation="No result verifies this, so it can't be confirmed or denied.",
            )
        ],
    )

    text = api._problem_text(verdict)

    assert len(text) <= 120
    assert text.endswith(".")
    assert "…" not in text
    assert "can't be confirmed" not in text


async def test_disagreed_verdict_is_excluded_from_diagnosis(
    summary_session: SessionContext,
) -> None:
    failed_id = next(iter((await api.diagnose(summary_session)).failures))
    summary_session.disagreed_claim_ids.add(failed_id)

    profile = await api.diagnose(summary_session)

    assert profile.failures == []
    assert profile.root_causes == []


def test_fix_outcome_is_kept_for_following_bandit_stage(summary_session: SessionContext) -> None:
    api.on_fix_outcome(summary_session, "correction-1", True)

    assert summary_session.cache["course_correct:fix_outcomes"] == {"correction-1": True}


@pytest.mark.parametrize(("level", "variant"), [(1, "v1"), (2, "v2"), (3, "v3"), (4, "v1")])
def test_fix_it_citation_copy_hides_scores_and_avoids_nested_quotes(
    level: int, variant: str
) -> None:
    quote = 'Lee & Park (2022), "Transformer Models for Honeybee Colony Collapse Forecasting"'
    claim = Claim(
        claim_id="citation-1",
        message_id="answer-1",
        quote=quote,
        normalized="The paper 'Transformer Models for Honeybee Colony Collapse Forecasting' exists.",
        type="paper",
        risk="high",
    )
    verdict = ClaimVerdict(
        claim_id=claim.claim_id,
        quote=quote,
        type=claim.type,
        risk=claim.risk,
        final="red",
        detector_results=[
            DetectorResult(
                detector="reference_auditor",
                status="contradicted",
                confidence=0.95,
                explanation="No matching paper was found.",
                evidence=[
                    Evidence(
                        source="Crossref", snippet="No matching work found (best title match 0.58)"
                    ),
                    Evidence(source="OpenAlex", snippet="No matching work (best title match 0.61)"),
                ],
            )
        ],
    )
    session = SessionContext(session_id="citation-copy", claims={claim.claim_id: claim})
    profile = DriftProfile(failures=[claim.claim_id], root_causes=["fabricated_sources"])

    prompt = api._correction_text(level, profile, [verdict], session, variant)

    assert "No matching paper found in Crossref or OpenAlex" in prompt
    assert "best title match" not in prompt
    assert "0.58" not in prompt
    assert "exists.." not in prompt
    assert f"“{quote}”" not in prompt
    assert quote in prompt
