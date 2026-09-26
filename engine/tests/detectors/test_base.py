"""Tests for app/detectors/base.py (FR-C5, §12.4). Owner: Role C."""

from __future__ import annotations

import asyncio

import pytest

from app.detectors.base import (
    BaseDetector,
    Detector,
    normalize_text,
    similarity,
    snippet,
)
from app.llm import LLMError
from app.models import Claim, DetectorResult, Evidence, SessionContext
from app.plugins import run_detector


# --- a tiny fake detector whose behaviour each test controls ---------------------------------
class FakeDetector(BaseDetector):
    name = "claim_verifier"

    def __init__(self, behaviour):
        self.behaviour = behaviour

    async def _check(self, claim, session):
        return await self.behaviour(self, claim, session)


@pytest.fixture
def claim(load_scenario) -> Claim:
    return Claim(**load_scenario("false_fact")["expected"]["claims"][0])


@pytest.fixture
def session() -> SessionContext:
    return SessionContext(session_id="test")


# --- interface ---------------------------------------------------------------------------------
def test_satisfies_detector_protocol():
    async def ok(self, c, s):
        return self.result("supported", 0.9, "fine")

    assert isinstance(FakeDetector(ok), Detector)


async def test_returns_result_with_name_and_latency(claim, session):
    async def ok(self, c, s):
        await asyncio.sleep(0.01)
        return self.result(
            "supported", 0.9, "Matches Wikipedia.", [Evidence(source="Wikipedia", snippet="x")]
        )

    r = await FakeDetector(ok).check(claim, session)
    assert isinstance(r, DetectorResult)
    assert r.detector == "claim_verifier"
    assert r.status == "supported"
    assert r.latency_ms >= 10


# --- FR-C5: never raise --------------------------------------------------------------------------
async def test_crash_becomes_error_status(claim, session):
    async def boom(self, c, s):
        raise ValueError("bad parse")

    r = await FakeDetector(boom).check(claim, session)
    assert r.status == "error"
    assert r.confidence == 0.0
    assert "ValueError" in r.explanation


async def test_llm_error_becomes_error_status(claim, session):
    async def no_key(self, c, s):
        raise LLMError("ANTHROPIC_API_KEY not set")

    r = await FakeDetector(no_key).check(claim, session)
    assert r.status == "error"
    assert "judge" in r.explanation


async def test_engine_timeout_still_works(claim, session):
    """Cancellation must propagate so the engine's wait_for returns on time."""

    async def slow(self, c, s):
        await asyncio.sleep(5)
        return self.result("supported", 1, "never")

    from app import plugins

    plugins._detectors["claim_verifier"] = FakeDetector(slow)
    try:
        r = await run_detector("claim_verifier", claim, session, timeout_s=0.05)
    finally:
        plugins._detectors.pop("claim_verifier", None)
    assert r is not None and r.status == "error"
    assert "timed out" in r.explanation


def test_confidence_is_clamped():
    async def unused(self, c, s): ...

    d = FakeDetector(unused)
    assert d.result("supported", 1.7, "x").confidence == 1.0
    assert d.result("supported", -2, "x").confidence == 0.0


# --- cache -----------------------------------------------------------------------------------------
async def test_cache_calls_factory_once_and_namespaces(session):
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"hits": 3}

    async def unused(self, c, s): ...

    d = FakeDetector(unused)
    assert await d.cached(session, "wiki:eiffel", fetch) == {"hits": 3}
    assert await d.cached(session, "wiki:eiffel", fetch) == {"hits": 3}
    assert calls == 1
    assert "claim_verifier:wiki:eiffel" in session.cache


async def test_cache_does_not_store_failures(session):
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("network blip")
        return "ok"

    async def unused(self, c, s): ...

    d = FakeDetector(unused)
    with pytest.raises(TimeoutError):
        await d.cached(session, "k", flaky)
    assert await d.cached(session, "k", flaky) == "ok"


# --- text helpers ------------------------------------------------------------------------------------
def test_normalize_text():
    assert normalize_text("Attention Is All You Need!") == "attention is all you need"
    assert normalize_text("  Jógvan   Poulsen ") == "jogvan poulsen"
    assert normalize_text("HiveFormer: Attention-Based") == "hiveformer attention based"


def test_similarity_threshold_behaviour():
    # same title, different punctuation/case → passes FR-C1's 0.85 bar
    assert similarity("Attention is all you need.", "ATTENTION IS ALL YOU NEED") >= 0.99
    # a made-up title vs a real one → far below the bar
    assert (
        similarity(
            "Transformer Models for Honeybee Colony Collapse Forecasting",
            "Attention Is All You Need",
        )
        < 0.5
    )
    assert similarity("", "anything") == 0.0


def test_snippet_truncates_and_flattens():
    assert snippet("a\n\nb   c") == "a b c"
    long = snippet("x" * 500, limit=50)
    assert len(long) == 50 and long.endswith("…")


def test_word_overlap_separates_topic_neighbours():
    from app.detectors.base import word_overlap

    fake = "HiveFormer: Attention-Based Acoustic Monitoring of Beehives"
    other = "MUS-Tracker: An IoT Based System in Controlling and Monitoring of Beehives"
    assert similarity(fake, other) > 0.6  # spelling alone looks close …
    assert word_overlap(fake, other) < 0.3  # … but they share almost no meaningful words
    assert word_overlap("Attention Is All You Need", "attention is all you need") == 1.0


@pytest.mark.parametrize(
    ("text", "instruction"),
    [
        (
            "Sleep 0.6 seconds between calls, which keeps you at exactly 100 requests per minute.",
            True,
        ),
        ("Read it from the response and fall back to 60 seconds if it's missing:", True),
        ("Here's the minimal app.", True),
        ("Then use df.to_csv to save it.", True),
        ("Linus Torvalds created Git in 2005.", False),
        ("Most APIs answer with HTTP status 429, which means Too Many Requests.", False),
        ("The retries argument makes requests retry failed downloads automatically.", False),
        ("Flask was first released in 2010.", False),
    ],
)
def test_looks_like_instruction(text, instruction):
    from app.detectors.base import looks_like_instruction

    assert looks_like_instruction(text) is instruction
