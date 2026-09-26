"""Tests for the Consistency Probe (FR-C3). Owner: Role C.

Offline: the 5 samples and the grouping judge are scripted, so these test OUR logic (entropy,
thresholds, the "samples agree but not with the original" case, failure handling).
Live run (ANTHROPIC_API_KEY, internet):
    REIGNS_LIVE=1 pytest tests/detectors/test_consistency_probe.py -k live -s
"""

from __future__ import annotations

import math
import os

import pytest

from app.detectors.consistency_probe import (
    ConsistencyProbe,
    clean_groups,
    semantic_entropy,
)
from app.llm import LLMError
from app.models import Claim, SessionContext


def claim(normalized: str, question: str | None = "Who was the first mayor of Tórshavn?") -> Claim:
    return Claim(
        claim_id="c",
        message_id="m",
        quote=normalized,
        normalized=normalized,
        type="fact",
        risk="high",
        question=question,
    )


def scripted(answers: list[str]):
    """Sampler that returns the given answers in order (one per call)."""
    it = iter(answers)
    calls = []

    async def sampler(system, user, **kw):
        calls.append((user, kw))
        return next(it)

    sampler.calls = calls  # type: ignore[attr-defined]
    return sampler


def grouping(groups, original_group, question=None):
    calls = []

    async def judge(system, user, **kw):
        calls.append(user)
        if "Turn a factual claim into" in system:
            return {"question": question}
        return {"groups": groups, "original_group": original_group}

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr("app.detectors.consistency_probe.RETRY_BACKOFF_S", 0)


@pytest.fixture
def session() -> SessionContext:
    return SessionContext(session_id="t")


# --------------------------------------------------------------------------- math (§8.3)
def test_entropy_formula():
    assert semantic_entropy([5]) == 0.0
    assert semantic_entropy([1, 1, 1, 1, 1]) == 1.0
    # [3, 2] by hand: -(0.6 ln 0.6 + 0.4 ln 0.4) / ln 5
    expected = -(0.6 * math.log(0.6) + 0.4 * math.log(0.4)) / math.log(5)
    assert semantic_entropy([3, 2]) == pytest.approx(expected)


def test_clean_groups_repairs_judge_mistakes():
    # index 1 repeated, index 4 forgotten, junk values ignored
    assert clean_groups([[0, 1], [1, 2, "x", 9], [3]], 5) == [[0, 1], [2], [3], [4]]
    assert clean_groups("garbage", 3) == [[0], [1], [2]]


# --------------------------------------------------------------------------- fixture scenario
async def test_niche_scenario_scattered_answers_are_likely_hallucination(load_scenario, session):
    raw = load_scenario("niche_entropy")["expected"]["claims"][0]
    probe = ConsistencyProbe(
        sampler=scripted(
            [
                "Jógvan Poulsen",
                "Niels Winther",
                "Hans Christopher Müller",
                "Jákup Dahl",
                "Sverri Patursson",
            ]
        ),
        judge=grouping([[0], [1], [2], [3], [4]], 0),
    )
    r = await probe.check(Claim(**raw), session)
    want = load_scenario("niche_entropy")["expected"]["detector_results"][raw["claim_id"]]
    assert r.status == next(w["status"] for w in want if w["detector"] == "consistency_probe")
    assert r.status == "likely_hallucination" and r.confidence >= 0.95
    assert "5 different answers" in r.explanation
    assert "normalized entropy 1.00" in r.evidence[0].snippet


# --------------------------------------------------------------------------- thresholds
async def test_same_answer_every_time_is_consistent(session):
    probe = ConsistencyProbe(sampler=scripted(["Paris"] * 5), judge=grouping([[0, 1, 2, 3, 4]], 0))
    r = await probe.check(
        claim("The capital of France is Paris.", "What is the capital of France?"), session
    )
    assert r.status == "consistent" and r.confidence == 1.0


async def test_mixed_answers_are_uncertain(session):
    probe = ConsistencyProbe(
        sampler=scripted(["A", "A", "A", "B", "B"]), judge=grouping([[0, 1, 2], [3, 4]], 0)
    )
    r = await probe.check(claim("X is A."), session)
    assert r.status == "uncertain"  # entropy 0.42


async def test_samples_agree_with_each_other_but_not_the_original(session):
    """Model reliably says B, the chat said A → the chat's answer was a one-off."""
    probe = ConsistencyProbe(sampler=scripted(["B"] * 5), judge=grouping([[0, 1, 2, 3, 4]], None))
    r = await probe.check(claim("The first mayor was A."), session)
    assert r.status == "likely_hallucination"
    assert "not what it said originally" in r.explanation


async def test_mid_entropy_but_original_never_repeated(session):
    """Live run 2: Tórshavn gave [3, 1, 1] → entropy 0.59 (below 0.6), 0/5 matched the
    original. Must be likely_hallucination, not uncertain."""
    probe = ConsistencyProbe(
        sampler=scripted(
            [
                "Andrias Samuelsen",
                "Andrias Samuelsen",
                "Andrias Samuelsen",
                "Jóannes Patursson",
                "J. Jacobsen",
            ]
        ),
        judge=grouping([[0, 1, 2], [3], [4]], None),
    )
    r = await probe.check(claim("The first mayor of Tórshavn was Jógvan Poulsen."), session)
    assert r.status == "likely_hallucination"
    assert "never gave this answer again" in r.explanation


async def test_mid_entropy_with_original_present_stays_uncertain(session):
    probe = ConsistencyProbe(
        sampler=scripted(["A", "A", "A", "B", "C"]), judge=grouping([[0, 1, 2], [3], [4]], 0)
    )
    r = await probe.check(claim("X is A."), session)
    assert r.status == "uncertain"


# --------------------------------------------------------------------------- robustness
async def test_samples_never_see_the_original_answer(session):
    sampler = scripted(["x"] * 5)
    probe = ConsistencyProbe(sampler=sampler, judge=grouping([[0, 1, 2, 3, 4]], 0))
    await probe.check(claim("The first mayor of Tórshavn was Jógvan Poulsen."), session)
    assert len(sampler.calls) == 5
    for user, kw in sampler.calls:
        assert "Poulsen" not in user  # only the question goes to the sampler
        assert kw["temperature"] == 1.0  # FR-C3


async def test_question_is_generated_when_extraction_gave_none(session):
    judge = grouping([[0, 1, 2, 3, 4]], 0, question="Who was the first mayor of Tórshavn?")
    sampler = scripted(["x"] * 5)
    await ConsistencyProbe(sampler=sampler, judge=judge).check(
        claim("The first mayor of Tórshavn was Jógvan Poulsen.", question=None), session
    )
    assert sampler.calls[0][0] == "Who was the first mayor of Tórshavn?"


async def test_yes_no_question_from_extraction_is_replaced(session):
    """Live engine run: extraction gave "Was Jógvan Poulsen the first mayor…?" — that leaks the
    answer. The probe must write its own open question instead."""
    judge = grouping([[0, 1, 2, 3, 4]], 0, question="Who was the first mayor of Tórshavn?")
    sampler = scripted(["x"] * 5)
    await ConsistencyProbe(sampler=sampler, judge=judge).check(
        claim(
            "The first mayor of Tórshavn was Jógvan Poulsen.",
            question="Was Jógvan Poulsen the first mayor of Tórshavn?",
        ),
        session,
    )
    assert sampler.calls[0][0] == "Who was the first mayor of Tórshavn?"


async def test_grouping_and_question_use_fast_model_samples_do_not(session, monkeypatch):
    monkeypatch.setenv("REIGNS_FAST_MODEL", "fast-test-model")
    seen = []

    async def judge(system, user, **kw):
        seen.append(kw.get("model"))
        if "Turn a factual claim into" in system:
            return {"question": "Who?"}
        return {"groups": [[0, 1, 2, 3, 4]], "original_group": 0}

    sampler = scripted(["x"] * 5)
    await ConsistencyProbe(sampler=sampler, judge=judge).check(claim("X.", question=None), session)
    assert seen == ["fast-test-model", "fast-test-model"]
    assert all("model" not in kw for _, kw in sampler.calls)


def test_is_open_question():
    from app.detectors.consistency_probe import is_open_question

    assert is_open_question("Who was the first mayor of Tórshavn?")
    assert is_open_question("In what year was the Eiffel Tower completed?")
    assert not is_open_question("Was Jógvan Poulsen the first mayor?")
    assert not is_open_question("Is Canberra the capital of Australia?")
    assert not is_open_question(None)


async def test_some_samples_failing_still_decides(session):
    answers = iter(["A", LLMError("rate limit"), "B", "C", "D"])

    async def flaky(system, user, **kw):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    probe = ConsistencyProbe(sampler=flaky, judge=grouping([[0], [1], [2], [3]], 0))
    r = await probe.check(claim("X is A."), session)
    assert r.status == "likely_hallucination"  # 4 answers, all different


async def test_failed_sample_is_retried_once(session):
    attempts = {"n": 0}

    async def flaky_first(system, user, **kw):
        attempts["n"] += 1
        # Calls run in order: sample 1 (fails) → its retry (ok) → sample 2 (fails) → retry (ok) …
        if attempts["n"] in (1, 3):
            raise LLMError("429")
        return "Paris"

    probe = ConsistencyProbe(sampler=flaky_first, judge=grouping([[0, 1, 2, 3, 4]], 0))
    r = await probe.check(claim("The capital of France is Paris.", "Capital of France?"), session)
    assert r.status == "consistent" and "5 samples" in r.evidence[0].snippet


async def test_too_few_samples_is_error(session):
    async def down(system, user, **kw):
        raise LLMError("no key")

    r = await ConsistencyProbe(sampler=down, judge=grouping([], None)).check(claim("X."), session)
    assert r.status == "error"
    assert "no key" in r.explanation  # the real reason is shown, not just "0 of 5"


async def test_garbage_grouping_is_treated_as_all_different(session):
    """A broken judge must not HIDE disagreement: unknown answers count as their own group."""
    probe = ConsistencyProbe(
        sampler=scripted(["A", "B", "C", "D", "E"]), judge=grouping("nonsense", None)
    )
    r = await probe.check(claim("X is A."), session)
    assert r.status == "likely_hallucination"


async def test_probe_is_cached_per_session(session):
    sampler = scripted(["Paris"] * 5)
    probe = ConsistencyProbe(sampler=sampler, judge=grouping([[0, 1, 2, 3, 4]], 0))
    c = claim("The capital of France is Paris.", "What is the capital of France?")
    await probe.check(c, session)
    await probe.check(c, session)
    assert len(sampler.calls) == 5  # second check served from session.cache


# --------------------------------------------------------------------------- live (opt-in)
@pytest.mark.skipif(os.environ.get("REIGNS_LIVE") != "1", reason="set REIGNS_LIVE=1")
async def test_live_probe():
    probe = ConsistencyProbe()
    cases = [
        claim("The first mayor of Tórshavn was Jógvan Poulsen, who took office in 1866."),
        claim("The capital of Australia is Canberra.", "What is the capital of Australia?"),
        claim(
            "The Eiffel Tower was completed in 1889.",
            "In what year was the Eiffel Tower completed?",
        ),
    ]
    for c in cases:
        r = await probe.check(c, SessionContext(session_id="live"))
        print(f"\n{r.status:21} {r.confidence:.2f} {r.latency_ms:5}ms  {c.normalized[:60]}")
        print(f"                      {r.explanation}")
        for e in r.evidence:
            print(f"                      [{e.source}] {e.snippet[:150]}")
