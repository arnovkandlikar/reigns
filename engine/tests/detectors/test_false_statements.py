"""Regression tests for the "give me 3 false statements" bug report. Owner: Role C.

What happened in the live test:
1. The user asked for 3 false statements; REIGN flagged all three as hallucinations.
2. "Water boils at 50°C" was marked contradicted by a sea-level PRESSURE sentence.
3. A correct claim ("water boils at 100°C") went amber because retrieval picked the wrong
   Wikipedia sentence.
4. REIGN's own "start fresh" hand-off, sent by the user, was learned as "what you told
   Claude" and came back as evidence, round after round.
"""

from __future__ import annotations

import httpx
import pytest

from app.detectors import claim_gate
from app.detectors.claim_gate import heuristic, requested_untrue
from app.detectors.claim_verifier import ClaimVerifier, _comparable_numbers, pick_sentences
from app.detectors.code_api_checker import CodeApiChecker
from app.detectors.consistency_probe import ConsistencyProbe
from app.detectors.memory_consistency import MemoryConsistency
from app.detectors.reference_auditor import ReferenceAuditor
from app.learning import memory
from app.learning.memory import MemoryStore, reign_authored
from app.models import ChatMessage, Claim, CorrectionRecord, SessionContext

FALSE_REPLY = (
    "Here are three false statements:\n1. The Great Wall of China is easy to see with the naked "
    "eye from the Moon.\n2. Water boils at 50°C at sea level.\n3. Python was created by Linus "
    "Torvalds in 2005."
)
HANDOFF = (
    "Start a fresh chat with this handoff:\n- Original question: give me 3 completely false "
    "statements\n- Confirmed: The Great Wall of China cannot be seen from the Moon with the "
    "naked eye\n- Problems and evidence:\n1. Recheck Water boils at 50°C at sea level"
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


def chat(user_text: str, reply: str = FALSE_REPLY) -> SessionContext:
    s = SessionContext(session_id="fs")
    s.messages += [
        ChatMessage(message_id="u1", role="user", position=0, text=user_text),
        ChatMessage(message_id="a1", role="assistant", position=1, text=reply),
    ]
    return s


def claim(quote: str, ctype: str = "fact", code: str | None = None) -> Claim:
    return Claim(
        claim_id="c1",
        message_id="a1",
        quote=quote,
        normalized=quote,
        type=ctype,
        risk="high",
        code=code,
    )


def gate_says(**answer):
    calls = []

    async def judge(system, user, **kw):
        calls.append((system, user))
        return answer

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


NOT_ASSERTED = {
    "standalone": "Water boils at 50°C at sea level.",
    "kind": "not_asserted",
    "subject": "water boiling point",
    "question": None,
}


# ------------------------------------------------------------------ 1. requested false content
@pytest.mark.parametrize(
    "text",
    [
        "give me 3 completely false statements",
        "Can you write a fake news headline about the moon?",
        "Please make up a fictional citation for my slide",
        "list five incorrect facts about Python",
        "Invent a made-up statistic about bees",
    ],
)
def test_requests_for_untrue_content_are_recognized(text):
    assert requested_untrue(chat(text), claim("Water boils at 50°C at sea level."))


@pytest.mark.parametrize(
    "text",
    [
        "Is this citation fake? Smith (2020), Nature.",
        "what are some false beliefs people have about sleep",  # asks for real facts ABOUT myths
        "When was Python created?",
        "My last answer was wrong, sorry. Now tell me about Flask.",
    ],
)
def test_ordinary_questions_are_not_requests_for_untrue_content(text):
    assert not requested_untrue(chat(text), claim("Water boils at 100°C at sea level."))


def test_gate_prompt_knows_not_asserted():
    assert "not_asserted" in claim_gate.GATE_SYSTEM and "not_asserted" in claim_gate.KINDS


def test_heuristic_fallback_marks_requested_false_statements():
    r = heuristic(claim("Water boils at 50°C at sea level."), chat("give me 3 false statements"))
    assert r.kind == "not_asserted" and not r.checkable


async def test_llm_gate_not_asserted_silences_the_web_check():
    hosts = []

    def spy(req):
        hosts.append(req.url.host)
        return httpx.Response(200, json={})

    v = ClaimVerifier(transport=httpx.MockTransport(spy), gate_judge=gate_says(**NOT_ASSERTED))
    assert await v.check(claim("Water boils at 50°C at sea level."), chat("hi")) is None
    assert hosts == []


async def test_llm_gate_not_asserted_silences_the_probe():
    async def never(*a, **k):
        raise AssertionError("probe must not sample")

    p = ConsistencyProbe(sampler=never, judge=never, gate_judge=gate_says(**NOT_ASSERTED))
    assert await p.check(claim("Water boils at 50°C at sea level."), chat("hi")) is None


async def test_llm_gate_not_asserted_silences_memory():
    async def never(*a, **k):
        raise AssertionError("memory judge must not run")

    async def no_cards(*a, **k):
        return {"cards": []}

    await memory.add_card(
        "local",
        "verified_fact",
        "Water boils at 100°C at sea level.",
        "water boiling point",
        "claim_verifier",
    )
    mc = MemoryConsistency(
        judge=never, extract_judge=no_cards, gate_judge=gate_says(**NOT_ASSERTED)
    )
    assert await mc.check(claim("Water boils at 50°C at sea level."), chat("hi")) is None


async def test_fake_citation_on_request_is_not_audited():
    def boom(req):
        raise AssertionError("no lookup expected")

    ra = ReferenceAuditor(transport=httpx.MockTransport(boom))
    c = claim(
        'Lee & Park (2022), "Transformer Models for Honeybee Forecasting", IEEE Access',
        ctype="paper",
    )
    assert await ra.check(c, chat("Give me a fake citation for my slides")) is None


async def test_broken_code_on_request_is_not_checked():
    code = "import requests\nrequests.get('https://x.org', retries=3)"
    c = claim(code, ctype="code_api", code=code)
    assert (
        await CodeApiChecker().check(c, chat("write code with a wrong API call on purpose")) is None
    )


# ------------------------------------------------------------------ 2. evidence about the wrong number
def test_pressure_sentence_cannot_contradict_a_boiling_point():
    assert not _comparable_numbers(
        "Water boils at 50°C at sea level.",
        "Average sea-level pressure is 1,013.25 hPa (29.921 inHg; 760.00 mmHg).",
    )


@pytest.mark.parametrize(
    "claim_text,quote",
    [
        ("Water boils at 50°C at sea level.", "At sea level, water boils at 100 °C (212 °F)."),
        (
            "The Eiffel Tower was completed in 1899.",
            "Constructed from 1887 to 1889 as the centerpiece",
        ),
        ("The Eiffel Tower is 300 metres tall.", "It is 330 metres (1,083 ft) tall"),
        (
            "Python was created by Linus Torvalds in 2005.",
            "Python was conceived by Guido van Rossum.",
        ),
        ("The Nozomi takes 2 hours 15 minutes.", "The fastest Nozomi takes 2 hours 21 minutes."),
    ],
)
def test_like_for_like_contradictions_still_count(claim_text, quote):
    assert _comparable_numbers(claim_text, quote)


async def test_verifier_downgrades_a_wrong_kind_contradiction(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    pressure = "Average sea-level pressure is 1,013.25 hPa (29.921 inHg; 760.00 mmHg)."

    def web(req):
        if req.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://en.wikipedia.org/wiki/Atmospheric_pressure",
                            "content": pressure,
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"query": {"search": []}})

    async def judge(system, user):
        return {
            "verdict": "contradicted",
            "confidence": 0.95,
            "evidence_index": 0,
            "quote": pressure,
            "explanation": "Sea-level conditions differ.",
        }

    v = ClaimVerifier(transport=httpx.MockTransport(web), judge=judge)
    r = await v.check(claim("Water boils at 50°C at sea level."), SessionContext(session_id="x"))
    assert r.status == "unverified"


def test_retrieval_prefers_the_boiling_point_sentence():
    article = (
        "The boiling point of a substance is the temperature at which its vapor pressure "
        "equals the surrounding pressure. Average sea-level pressure is 1,013.25 hPa "
        "(29.921 inHg; 760.00 mmHg). Kinetics of molecules are discussed elsewhere in depth. "
        "At sea level, the boiling point of water is 100 °C (212 °F)."
    )
    picked = pick_sentences(article, "Water boils at 100°C at sea level", k=1, intro=1)
    assert any("boiling point of water is 100" in s for s in picked)


# ------------------------------------------------------------------ 3. REIGN's own prompts
def test_handoff_text_is_recognized_as_reign_authored():
    assert reign_authored(None, HANDOFF)
    assert reign_authored(
        None, "Quick context refresh before we continue. Please keep this in mind:"
    )


def test_an_inserted_correction_is_recognized_even_without_markers():
    s = chat("hi")
    text = (
        "Please double-check the Eiffel Tower completion year against Wikipedia and "
        "correct your answer if needed, citing the source."
    )
    s.corrections.append(
        CorrectionRecord(correction_id="k", prompt_type="verify_nudge", level=2, text=text)
    )
    assert reign_authored(s, "  " + text + "\n")
    assert not reign_authored(s, "I'm on Python 3.8 and my API allows 100 requests a minute.")


async def test_handoff_is_never_learned_as_user_facts():
    async def extractor(*a, **k):
        raise AssertionError("must not even try to extract from REIGN's own prompt")

    s = SessionContext(session_id="h")
    msg = ChatMessage(message_id="u2", role="user", position=2, text=HANDOFF)
    assert await memory.on_user_message(s, msg, judge=extractor) == []
    assert await memory.list_cards("local") == []
