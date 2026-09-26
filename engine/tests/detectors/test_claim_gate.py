"""Tests for the Claim Gate (context-aware claim preparation). Owner: Role C.

The gate's model is scripted (`gate_judge`), so these check OUR plumbing: sharing one call,
abstaining on non-facts, searching/probing with the context-resolved text, and subject-scoped
memory — the three root causes of long-chat false alarms.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.detectors import claim_gate
from app.detectors.claim_gate import gate, heuristic
from app.detectors.claim_verifier import ClaimVerifier
from app.detectors.consistency_probe import ConsistencyProbe
from app.detectors.memory_consistency import MemoryConsistency
from app.learning import memory
from app.learning.memory import MemoryStore, add_card
from app.models import (
    ChatMessage,
    Claim,
    ClaimVerdict,
    DetectorResult,
    Evidence,
    SessionContext,
)


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    store = MemoryStore(str(tmp_path / "mem.db"))
    memory.set_store(store)
    monkeypatch.setenv("REIGNS_MEMORY_DB", store.path)
    monkeypatch.delenv("REIGNS_USER", raising=False)
    yield
    memory.set_store(None)


def chat() -> SessionContext:
    s = SessionContext(session_id="long")
    s.messages += [
        ChatMessage(
            message_id="u1",
            role="user",
            position=0,
            text="I want a tiny web page to show the data. Flask?",
        ),
        ChatMessage(
            message_id="a1",
            role="assistant",
            position=1,
            text="Flask is a good fit. It was created by Armin Ronacher and first "
            "released in 2010.",
        ),
    ]
    return s


def claim(quote, normalized=None, cid="c1", ctype="fact"):
    return Claim(
        claim_id=cid,
        message_id="a1",
        quote=quote,
        normalized=normalized or quote,
        type=ctype,
        risk="high",
    )


def gate_says(**answer):
    calls = []

    async def judge(system, user, **kw):
        calls.append(user)
        await asyncio.sleep(0.01)
        return answer

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


FLASK = {
    "standalone": "Flask was first released in 2010.",
    "kind": "world_fact",
    "subject": "flask",
    "question": "In what year was Flask first released?",
}


# --------------------------------------------------------------------------- the gate itself
async def test_one_call_shared_by_all_detectors():
    s, g = chat(), gate_says(**FLASK)
    c = claim("first released in 2010")
    results = await asyncio.gather(*(gate(c, s, g) for _ in range(3)))
    assert len(g.calls) == 1 and all(r.subject == "flask" for r in results)


async def test_gate_sees_the_conversation():
    s, g = chat(), gate_says(**FLASK)
    await gate(claim("first released in 2010"), s, g)
    assert "Flask?" in g.calls[0] and "LATEST REPLY" in g.calls[0]


async def test_gate_failure_falls_back_to_heuristic():
    async def broken(*a, **k):
        raise claim_gate.LLMError("down")

    r = await gate(claim("Linus Torvalds created Git in 2005."), chat(), broken)
    assert r.source == "heuristic" and r.checkable


def test_heuristic_does_not_check_unresolved_references():
    assert not heuristic(claim("It works best when the rows are sorted by date.")).checkable
    assert not heuristic(claim("That second option is df.interpolate().")).checkable
    assert heuristic(claim("The first mayor of Tórshavn was Jógvan Poulsen.")).checkable


# --------------------------------------------------------------------------- claim verifier
@pytest.mark.parametrize("kind", ["advice", "opinion", "user_context", "meta"])
async def test_verifier_only_checks_world_facts(kind):
    hosts = []

    def spy(req):
        hosts.append(req.url.host)
        return httpx.Response(200, json={})

    v = ClaimVerifier(
        transport=httpx.MockTransport(spy),
        gate_judge=gate_says(standalone="x", kind=kind, subject="x"),
    )
    assert await v.check(claim("It works best when the rows are sorted by date."), chat()) is None
    assert hosts == []  # no search spent


async def test_verifier_searches_the_resolved_claim(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    bodies = []

    def spy(req):
        bodies.append(req.url.params.get("srsearch", "") + req.read().decode())
        return httpx.Response(200, json={"results": [], "query": {"search": []}})

    v = ClaimVerifier(transport=httpx.MockTransport(spy), gate_judge=gate_says(**FLASK))
    await v.check(claim("first released in 2010"), chat())
    assert bodies and all("Flask" in b for b in bodies)  # not the subject-less fragment


# --------------------------------------------------------------------------- consistency probe
async def test_probe_uses_the_gates_standalone_question():
    asked = []

    async def sampler(system, user, **kw):
        asked.append(user)
        return "2010"

    async def group(system, user, **kw):
        return {"groups": [[0, 1, 2, 3, 4]], "original_group": 0}

    p = ConsistencyProbe(sampler=sampler, judge=group, gate_judge=gate_says(**FLASK))
    c = claim("first released in 2010").model_copy(update={"question": "When was it released?"})
    r = await p.check(c, chat())
    assert asked and all(q == "In what year was Flask first released?" for q in asked)
    assert r.status == "consistent"


async def test_probe_skips_non_facts():
    async def never(*a, **k):
        raise AssertionError("must not sample")

    p = ConsistencyProbe(
        sampler=never, judge=never, gate_judge=gate_says(standalone="x", kind="advice", subject="x")
    )
    assert await p.check(claim("Retry after the header's delay."), chat()) is None


# --------------------------------------------------------------------------- memory
async def test_memory_matches_by_subject_not_wording():
    """Long-chat replay: Flask 2010 was flagged against Python's 1991 card."""
    await add_card(
        "local",
        "verified_fact",
        "Python's first version was released in 1991.",
        subject="python",
        source="claim_verifier",
    )

    async def judge(system, user, **kw):
        raise AssertionError("the Python card must never reach the judge")

    async def no_cards(*a, **k):
        return {"cards": []}

    mc = MemoryConsistency(judge=judge, extract_judge=no_cards, gate_judge=gate_says(**FLASK))
    assert await mc.check(claim("first released in 2010"), chat()) is None


async def test_same_subject_card_still_catches_a_repeat():
    await add_card(
        "local",
        "correction",
        "Flask was first released in 2010, not 2014.",
        subject="flask",
        source="claim_verifier",
    )

    async def judge(system, user, **kw):
        return {
            "verdict": "contradicts",
            "memory_index": 0,
            "confidence": 0.95,
            "explanation": "Earlier this was verified: 2010.",
        }

    async def no_cards(*a, **k):
        return {"cards": []}

    gj = gate_says(
        standalone="Flask was first released in 2014.",
        kind="world_fact",
        subject="flask",
        question="When was Flask first released?",
    )
    mc = MemoryConsistency(judge=judge, extract_judge=no_cards, gate_judge=gj)
    r = await mc.check(claim("first released in 2014"), chat())
    assert r is not None and r.status == "contradicted"


async def test_verified_facts_are_stored_standalone_with_subject():
    s, c = chat(), claim("first released in 2010")
    await gate(c, s, gate_says(**FLASK))
    v = ClaimVerdict(
        claim_id="c1",
        quote="q",
        type="fact",
        risk="high",
        final="green",
        detector_results=[
            DetectorResult(
                detector="claim_verifier",
                status="supported",
                confidence=0.95,
                explanation="ok",
                evidence=[Evidence(source="Wikipedia", snippet="Flask … initial release 2010")],
            )
        ],
    )
    stored = await memory.on_verdicts(s, [c], [v])
    assert [(x.text, x.subject) for x in stored] == [("Flask was first released in 2010.", "flask")]


async def test_advice_is_never_stored_as_a_verified_fact():
    s, c = chat(), claim("Write the file to S3 instead.", cid="c2")
    await gate(
        c, s, gate_says(standalone="Write the CSV to Amazon S3.", kind="advice", subject="s3")
    )
    v = ClaimVerdict(
        claim_id="c2",
        quote="q",
        type="fact",
        risk="high",
        final="green",
        detector_results=[
            DetectorResult(
                detector="claim_verifier",
                status="supported",
                confidence=0.95,
                explanation="ok",
                evidence=[Evidence(source="x", snippet="S3")],
            )
        ],
    )
    assert await memory.on_verdicts(s, [c], [v]) == []
