"""Role D tests for the Source Faithfulness Checker (PRD FR-C6)."""

from __future__ import annotations

import json

import pytest

from app.detectors import source_faithfulness
from app.models import Claim, SessionContext, SourceDoc


@pytest.fixture
def summary_scenario(load_scenario) -> dict:
    return load_scenario("summary_drift")


@pytest.fixture
def claim(summary_scenario: dict) -> Claim:
    return Claim(**summary_scenario["expected"]["claims"][1])


@pytest.fixture
def session(summary_scenario: dict, claim: Claim) -> SessionContext:
    ctx = SessionContext(session_id="test")
    source_message = next(
        message["payload"]
        for message in summary_scenario["companion_to_engine"]
        if message["type"] == "message.new" and message["payload"]["role"] == "user"
    )
    ctx.source_docs[claim.source_ref] = SourceDoc(
        doc_id=claim.source_ref,
        message_id=source_message["message_id"],
        text=source_message["text"],
    )
    return ctx


def judge_response(status: str, confidence: float, quote: str, explanation: str) -> dict:
    return {
        "status": status,
        "confidence": confidence,
        "quote": quote,
        "explanation": explanation,
    }


async def test_supported_result_requires_and_returns_exact_source_quote(
    monkeypatch: pytest.MonkeyPatch, claim: Claim, session: SessionContext
) -> None:
    exact_quote = (
        "The new plan sets aside $4.2 million for construction, down from the $5 million "
        "originally proposed last spring"
    )
    calls = []

    async def fake_complete_json(**kwargs):
        calls.append(kwargs)
        return judge_response("supported", 0.95, exact_quote, "The source states this directly.")

    monkeypatch.setattr(source_faithfulness, "complete_json", fake_complete_json)
    result = await source_faithfulness.detector.check(
        claim.model_copy(
            update={
                "quote": exact_quote,
                "normalized": "The new plan sets aside $4.2 million for construction, down from "
                "the $5 million originally proposed last spring.",
            }
        ),
        session,
    )

    assert result.status == "supported"
    assert result.detector == "source_faithfulness"
    assert result.evidence[0].source == "Pasted document"
    assert result.evidence[0].snippet == exact_quote
    assert calls[0]["temperature"] == 0.0
    request = json.loads(calls[0]["user"])
    assert request["source_excerpts"]
    assert all(
        len(source_faithfulness._TOKEN.findall(chunk)) <= 800
        for chunk in request["source_excerpts"]
    )


async def test_high_confidence_missing_fact_is_not_in_source(
    monkeypatch: pytest.MonkeyPatch, claim: Claim, session: SessionContext
) -> None:
    async def fake_complete_json(**_kwargs):
        return judge_response(
            "not_in_source", 0.91, "", "The pasted article does not mention new jobs."
        )

    monkeypatch.setattr(source_faithfulness, "complete_json", fake_complete_json)
    result = await source_faithfulness.detector.check(claim, session)

    assert result.status == "not_in_source"
    assert result.confidence == 0.91
    assert result.evidence == []


async def test_low_confidence_absence_is_unverified(
    monkeypatch: pytest.MonkeyPatch, claim: Claim, session: SessionContext
) -> None:
    async def fake_complete_json(**_kwargs):
        return judge_response("not_in_source", 0.7, "", "The excerpt is inconclusive.")

    monkeypatch.setattr(source_faithfulness, "complete_json", fake_complete_json)
    result = await source_faithfulness.detector.check(claim, session)

    assert result.status == "unverified"
    assert result.confidence == 0.7


async def test_paraphrased_judge_quote_does_not_count_as_evidence(
    monkeypatch: pytest.MonkeyPatch, claim: Claim, session: SessionContext
) -> None:
    async def fake_complete_json(**_kwargs):
        return judge_response(
            "contradicted",
            0.94,
            "The article says the council had a budget of 4.2 million.",
            "The source conflicts with the claim.",
        )

    monkeypatch.setattr(source_faithfulness, "complete_json", fake_complete_json)
    result = await source_faithfulness.detector.check(claim, session)

    assert result.status == "unverified"
    assert result.evidence == []
    assert result.confidence < 0.8


async def test_missing_document_returns_error_without_calling_judge(
    monkeypatch: pytest.MonkeyPatch, claim: Claim
) -> None:
    async def unexpected_call(**_kwargs):
        pytest.fail("judge must not run without the referenced source document")

    monkeypatch.setattr(source_faithfulness, "complete_json", unexpected_call)
    result = await source_faithfulness.detector.check(claim, SessionContext(session_id="test"))

    assert result.status == "error"
    assert "unavailable" in result.explanation


async def test_identical_lookup_is_cached_for_the_session(
    monkeypatch: pytest.MonkeyPatch, claim: Claim, session: SessionContext
) -> None:
    calls = 0

    async def fake_complete_json(**_kwargs):
        nonlocal calls
        calls += 1
        return judge_response("not_in_source", 0.9, "", "No supporting passage was found.")

    monkeypatch.setattr(source_faithfulness, "complete_json", fake_complete_json)
    await source_faithfulness.detector.check(claim, session)
    await source_faithfulness.detector.check(claim, session)

    assert calls == 1


def test_source_is_chunked_and_only_three_relevant_chunks_are_selected() -> None:
    source = (
        " ".join(f"Unrelated filler paragraph {i}." for i in range(900))
        + " The Eastside library budget was approved at $4.2 million."
    )

    chunks = source_faithfulness._source_chunks(source)
    selected = source_faithfulness._relevant_chunks(source, "Eastside library budget $4.2 million")

    assert len(chunks) > 3
    assert len(selected) == 3
    assert any("Eastside library budget" in chunk for chunk in selected)
    assert all(len(source_faithfulness._TOKEN.findall(chunk)) <= 800 for chunk in chunks)
