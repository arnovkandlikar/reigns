"""Tests for the Claim Verifier (FR-C2). Owner: Role C.

Offline: search APIs are faked with httpx.MockTransport and the judge LLM with a small async
function, so we test OUR logic (evidence gathering, quote verification, precision rules), not
the model. Live run (needs TAVILY_API_KEY + ANTHROPIC_API_KEY, internet):
    REIGNS_LIVE=1 pytest tests/detectors/test_claim_verifier.py -k live -s
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.detectors.claim_verifier import ClaimVerifier
from app.llm import LLMError
from app.models import Claim, SessionContext

EIFFEL_SUMMARY = (
    "The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. "
    "Constructed from 1887 to 1889 as the centerpiece of the 1889 World's Fair. "
    "It is 330 metres (1,083 ft) tall, about the same height as an 81-storey building."
)
BOILING = "At sea level, water boils at 100 °C (212 °F) at standard atmospheric pressure."


def web_handler(request: httpx.Request) -> httpx.Response:
    """Fake Tavily + Wikipedia. Anything about Tórshavn's mayor → nothing useful."""
    host = request.url.host
    if host == "api.tavily.com":
        q = request.read().decode().lower()
        if "eiffel" in q:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Eiffel Tower - history",
                            "url": "https://www.toureiffel.paris/en/history",
                            "content": "The tower was completed on 31 March 1889 for the Exposition "
                            "Universelle.",
                            "score": 0.9,
                        }
                    ]
                },
            )
        if "water boils" in q:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Boiling point",
                            "url": "https://www.britannica.com/boiling",
                            "content": BOILING,
                            "score": 0.9,
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"results": []})
    if host == "en.wikipedia.org" and request.url.path == "/w/api.php":
        params = request.url.params
        if params.get("list") == "search":
            q = params.get("srsearch", "").lower()
            if "eiffel" in q:
                hits = [{"title": "Eiffel Tower"}]
            elif "water" in q:
                hits = [{"title": "Boiling point"}]
            else:
                hits = []
            return httpx.Response(200, json={"query": {"search": hits}})
        if params.get("prop") == "extracts":
            text = EIFFEL_SUMMARY if "Eiffel" in params.get("titles", "") else BOILING
            return httpx.Response(200, json={"query": {"pages": {"1": {"extract": text}}}})
    return httpx.Response(404)


def fake_judge(answers: dict[str, dict]):
    """A stand-in for complete_json: picks a canned answer by a keyword in the claim."""
    calls: list[str] = []

    async def judge(system: str, user: str):
        calls.append(user)
        for keyword, answer in answers.items():
            if keyword in user.split("EVIDENCE")[0].lower():
                return answer
        return {"verdict": "unverified", "confidence": 0.4, "quote": "", "explanation": "?"}

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


GOOD_ANSWERS = {
    "1899": {
        "verdict": "contradicted",
        "confidence": 0.93,
        "evidence_index": 0,
        "quote": "Constructed from 1887 to 1889 as the centerpiece of the 1889 World's Fair",
        "explanation": "Wikipedia says it was completed in 1889, not 1899.",
    },
    "330": {
        "verdict": "supported",
        "confidence": 0.9,
        "evidence_index": 0,
        "quote": "It is 330 metres (1,083 ft) tall",
        "explanation": "Matches Wikipedia.",
    },
    "boils": {
        "verdict": "supported",
        "confidence": 0.96,
        "evidence_index": 0,
        "quote": "water boils at 100 °C (212 °F) at standard atmospheric pressure",
        "explanation": "Matches Wikipedia.",
    },
}


@pytest.fixture(autouse=True)
def tavily_key(monkeypatch, request):
    """Offline tests get a fake search key so they never touch the real Tavily.

    Live tests (test_live_*) must keep the real key from .env — overriding it there made Tavily
    answer 401 even though the key itself was fine.
    """
    if request.node.name.startswith("test_live"):
        return
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)


@pytest.fixture
def session() -> SessionContext:
    return SessionContext(session_id="t")


def verifier(judge=None, handler=web_handler) -> ClaimVerifier:
    return ClaimVerifier(
        transport=httpx.MockTransport(handler), judge=judge or fake_judge(GOOD_ANSWERS)
    )


def claim(text: str, quote: str | None = None) -> Claim:
    return Claim(
        claim_id="c",
        message_id="m",
        quote=quote or text,
        normalized=text,
        type="fact",
        risk="high",
        context="SECRET USER CONTEXT",
    )


# --------------------------------------------------------------------------- fixture scenarios
@pytest.mark.parametrize("scenario", ["false_fact", "clean", "niche_entropy"])
async def test_scenarios_match_expected(load_scenario, session, scenario):
    sc = load_scenario(scenario)
    for raw in sc["expected"]["claims"]:
        c = Claim(**raw)
        want = [
            r
            for r in sc["expected"]["detector_results"].get(c.claim_id, [])
            if r["detector"] == "claim_verifier"
        ]
        if not want:
            continue
        got = await verifier().check(c, session)
        assert got.status == want[0]["status"], (c.normalized, got.explanation)


async def test_contradiction_carries_verbatim_quote_and_url(session):
    r = await verifier().check(claim("The Eiffel Tower was completed in 1899."), session)
    assert r.status == "contradicted"
    assert r.evidence[0].source == "Wikipedia"
    assert r.evidence[0].url == "https://en.wikipedia.org/wiki/Eiffel_Tower"
    assert "1889" in r.evidence[0].snippet
    assert "1889, not 1899" in r.explanation


# --------------------------------------------------------------------------- precision rules
async def test_no_evidence_is_unverified_and_judge_not_called(session):
    judge = fake_judge(GOOD_ANSWERS)
    r = await verifier(judge).check(
        claim("The first mayor of Tórshavn was Jógvan Poulsen."), session
    )
    assert r.status == "unverified"
    assert judge.calls == []  # nothing to judge → no LLM cost


async def test_invented_quote_downgrades_contradiction(session):
    """Judge says 'contradicted' but quotes text that isn't in any snippet → never red."""
    liar = fake_judge(
        {
            "1899": {
                "verdict": "contradicted",
                "confidence": 0.99,
                "evidence_index": 0,
                "quote": "It was finished in 1887 exactly",
                "explanation": "Wrong year.",
            }
        }
    )
    r = await verifier(liar).check(claim("The Eiffel Tower was completed in 1899."), session)
    assert r.status == "unverified"


async def test_wrong_evidence_index_is_tolerated(session):
    """Judge quotes a real passage but names the wrong snippet number → still found."""
    off_by_one = fake_judge({"1899": {**GOOD_ANSWERS["1899"], "evidence_index": 5}})
    r = await verifier(off_by_one).check(claim("The Eiffel Tower was completed in 1899."), session)
    assert r.status == "contradicted"


async def test_low_confidence_verdict_becomes_unverified(session):
    unsure = fake_judge({"1899": {**GOOD_ANSWERS["1899"], "confidence": 0.4}})
    r = await verifier(unsure).check(claim("The Eiffel Tower was completed in 1899."), session)
    assert r.status == "unverified"


async def test_weak_contradiction_is_not_red(session):
    """Live run: judge gave 0.70 'contradicted' from an inference (a birth year). Must be amber."""
    inferred = fake_judge({"1899": {**GOOD_ANSWERS["1899"], "confidence": 0.7}})
    r = await verifier(inferred).check(claim("The Eiffel Tower was completed in 1899."), session)
    assert r.status == "unverified"


def test_judge_is_told_not_to_infer_contradictions():
    from app.detectors.claim_verifier import JUDGE_SYSTEM

    assert "DIRECTLY" in JUDGE_SYSTEM and "indirect" in JUDGE_SYSTEM
    # live scenario_code_api: "requests retries argument" was marked supported from snippets
    # about urllib3's Retry — support must be about the same subject.
    assert "SAME fact about the SAME subject" in JUDGE_SYSTEM


async def test_unverified_shows_no_unrelated_evidence(session):
    unsure = fake_judge(
        {
            "1899": {
                "verdict": "unverified",
                "confidence": 0.5,
                "quote": "",
                "explanation": "The snippets don't settle this.",
            }
        }
    )
    r = await verifier(unsure).check(claim("The Eiffel Tower was completed in 1899."), session)
    assert r.status == "unverified" and r.evidence == []


async def test_garbage_judge_output_is_safe(session):
    weird = fake_judge({"1899": {"verdict": "DEFINITELY FAKE", "confidence": "high"}})
    r = await verifier(weird).check(claim("The Eiffel Tower was completed in 1899."), session)
    assert r.status == "unverified"


# --------------------------------------------------------------------------- failures
async def test_llm_unavailable_is_error(session):
    async def no_key(system, user):
        raise LLMError("ANTHROPIC_API_KEY not set")

    r = await verifier(no_key).check(claim("The Eiffel Tower was completed in 1899."), session)
    assert r.status == "error"


async def test_all_sources_down_is_error_not_unverified(session):
    def down(req):
        raise httpx.ConnectError("offline", request=req)

    r = await verifier(handler=down).check(
        claim("The Eiffel Tower was completed in 1899."), session
    )
    assert r.status == "error"  # "no sources found" would wrongly trigger the Consistency Probe


async def test_wikipedia_only_when_no_search_key(session, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY")
    hosts = []

    def spy(req):
        hosts.append(req.url.host)
        return web_handler(req)

    r = await verifier(handler=spy).check(claim("The Eiffel Tower was completed in 1899."), session)
    assert r.status == "contradicted"
    assert "api.tavily.com" not in hosts


# --------------------------------------------------------------------------- privacy + cache
async def test_only_claim_text_is_sent_to_search(session):
    """§14 rule 13: search APIs get the normalized claim, never the conversation."""
    bodies = []

    def spy(req):
        bodies.append(req.url.params.get("srsearch", "") + req.read().decode())
        return web_handler(req)

    await verifier(handler=spy).check(claim("The Eiffel Tower was completed in 1899."), session)
    assert bodies and all("SECRET USER CONTEXT" not in b for b in bodies)


async def test_repeat_claim_uses_cache(session):
    judge = fake_judge(GOOD_ANSWERS)
    hits = []

    def spy(req):
        hits.append(req.url.host)
        return web_handler(req)

    v = verifier(judge, spy)
    c = claim("The Eiffel Tower was completed in 1899.")
    await v.check(c, session)
    n = len(hits)
    await v.check(c, session)
    assert len(hits) == n and len(judge.calls) == 1


# --------------------------------------------------------------------------- live (opt-in)
@pytest.mark.skipif(os.environ.get("REIGNS_LIVE") != "1", reason="set REIGNS_LIVE=1")
async def test_live_scenarios(load_scenario):
    live = ClaimVerifier()
    for name in ("false_fact", "clean", "niche_entropy"):
        session = SessionContext(session_id=name)
        for raw in load_scenario(name)["expected"]["claims"]:
            r = await live.check(Claim(**raw), session)
            print(
                f"\n{r.status:13} {r.confidence:.2f} {r.latency_ms:5}ms  {raw['normalized'][:70]}"
            )
            print(f"              {r.explanation}")
            for e in r.evidence[:1]:
                print(f"              [{e.source}] {e.snippet[:120]}")


@pytest.mark.skipif(os.environ.get("REIGNS_LIVE") != "1", reason="set REIGNS_LIVE=1")
async def test_live_sources_diagnostic():
    """Shows what each evidence source returns, or its exact error. Use when results look off."""
    import asyncio

    v = ClaimVerifier()
    print("\nTAVILY_API_KEY:", "set" if os.environ.get("TAVILY_API_KEY") else "MISSING")
    for q in ("The Eiffel Tower is about 330 metres tall.", "Water boils at 100 °C at sea level."):
        web, wiki = await asyncio.gather(v._web_search(q), v._wikipedia(q), return_exceptions=True)
        print(f"\nQUERY: {q}")
        for name, part in (("web search", web), ("Wikipedia", wiki)):
            if isinstance(part, BaseException):
                print(f"  {name}: FAILED → {part!r}")
            else:
                print(f"  {name}: {len(part)} snippets")
                for sn in part[:3]:
                    print(f"      [{sn.source}] {sn.text[:110]}")


# --------------------------------------------------------------------------- long-chat fixes
async def test_instructions_are_not_fact_checked(session):
    hosts = []

    def spy(req):
        hosts.append(req.url.host)
        return web_handler(req)

    r = await verifier(handler=spy).check(
        claim("Sleep 0.6 seconds between calls, which keeps you at 100 requests per minute."),
        session,
    )
    assert r is None and hosts == []  # abstains without spending a search


async def test_not_checkable_verdict_abstains(session):
    judge = fake_judge({"1899": {"verdict": "not_checkable", "confidence": 0.9}})
    assert (
        await verifier(judge).check(claim("The Eiffel Tower was completed in 1899."), session)
        is None
    )
