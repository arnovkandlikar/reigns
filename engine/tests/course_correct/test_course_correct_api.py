"""Role D Course Correct tests using the shared scenarios (PRD FR-D1–FR-D4)."""

from __future__ import annotations

import pytest

from app.course_correct import api
from app.models import Claim, ClaimVerdict, DetectorResult, SessionContext


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
