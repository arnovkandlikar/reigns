"""Tests for the Memory Consistency detector (Truth Ledger). Owner: Role C.

Offline: card extraction and the judge are scripted; each test has its own memory database.
"""

from __future__ import annotations

import pytest

from app.detectors.memory_consistency import (
    MemoryConsistency,
    number_violation,
    python_violation,
    quantities,
    required_python,
)
from app.learning import memory
from app.learning.memory import MemoryCard, MemoryStore, add_card
from app.models import ChatMessage, Claim, SessionContext


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("REIGNS_MEMORY_SYNC", raising=False)
    monkeypatch.delenv("REIGNS_USER", raising=False)
    store = MemoryStore(str(tmp_path / "mem.db"))
    memory.set_store(store)
    monkeypatch.setenv("REIGNS_MEMORY_DB", store.path)
    yield
    memory.set_store(None)


def card(text, kind="constraint"):
    return MemoryCard(card_id="c", user_id="u", kind=kind, text=text)


def extractor(cards):
    calls = []

    async def judge(system, user, **kw):
        calls.append(user)
        return {"cards": cards}

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


def judge_says(answer):
    calls = []

    async def judge(system, user, **kw):
        calls.append(user)
        return answer

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


def convo(user_text: str) -> SessionContext:
    s = SessionContext(session_id="s")
    s.messages += [
        ChatMessage(message_id="u1", role="user", text=user_text, position=0),
        ChatMessage(message_id="a1", role="assistant", text="(reply)", position=1),
    ]
    return s


def claim(quote, code=None, ctype="fact"):
    return Claim(
        claim_id="cl",
        message_id="a1",
        quote=quote,
        normalized=quote,
        type=ctype,
        risk="high",
        code=code,
    )


RATE = [
    {
        "kind": "constraint",
        "text": "API rate limit is 100 requests per minute",
        "subject": "api rate limit",
    }
]
PY38 = [{"kind": "user_fact", "text": "Uses Python 3.8", "subject": "python version"}]


# --------------------------------------------------------------------------- number check
def test_quantities_and_rates():
    q = quantities("send 2 requests per second and $1,200 per month")
    assert [(x.value, x.unit) for x in q] == [(2, "requests/second"), (1200, "usd/month")]


@pytest.mark.parametrize(
    ("claim_text", "violates"),
    [
        ("Loop sending 500 requests per minute.", True),
        ("Throttle to 2 requests per second.", True),  # = 120/min
        ("Keep it at 50 requests per minute.", False),
        ("We'll do 1k requests per hour.", False),  # ≈ 17/min
        ("It retries 3 times.", False),  # no comparable unit
    ],
)
def test_number_violation(claim_text, violates):
    c = card("API rate limit is 100 requests per minute.")
    assert bool(number_violation(c, claim_text)) is violates


def test_budget_cap_bounds_monthly_cost():
    assert number_violation(card("Budget is under $500."), "This costs $1,200 per month.")
    assert not number_violation(card("Budget is under $500."), "This costs $300 a month.")
    assert not number_violation(
        card("Uses 100 requests per minute.", "user_fact"), "500 requests per minute"
    )  # facts aren't limits


# --------------------------------------------------------------------------- python version
def test_required_python():
    assert required_python("match x:\n    case 1:\n        pass")[0] == (3, 10)
    assert required_python("def f(a: int | None): pass")[0] == (3, 10)
    assert required_python("print('hi')") is None
    assert required_python("x = 1 | 2") is None  # bitwise or, not a type union


def test_python_violation():
    c = card("Uses Python 3.8.", "user_fact")
    assert "3.10" in python_violation(c, "match cmd:\n    case 'go':\n        pass")
    assert python_violation(c, "if (n := 3):\n    pass") is None  # walrus is 3.8: fine


# --------------------------------------------------------------------------- the detector
async def test_forgotten_rate_limit_is_red_without_llm_judge():
    s = convo("Our API only allows 100 requests per minute.")
    judge = judge_says({"verdict": "unrelated"})
    det = MemoryConsistency(judge=judge, extract_judge=extractor(RATE))
    r = await det.check(claim("I set it to send 500 requests per minute to finish faster."), s)
    assert r.status == "contradicted" and "500 requests per minute" in r.explanation
    assert r.evidence[0].source == "What you told Claude"
    assert "100 requests per minute" in r.evidence[0].snippet
    assert judge.calls == []  # the number check decided; no LLM cost


async def test_python_version_violation_in_code():
    s = convo("I'm stuck on Python 3.8 at work.")
    det = MemoryConsistency(
        judge=judge_says({"verdict": "unrelated"}), extract_judge=extractor(PY38)
    )
    code = "import sys\nmatch sys.argv[1]:\n    case 'run':\n        print('go')\n"
    r = await det.check(claim(code, code=code, ctype="code_api"), s)
    assert r.status == "contradicted" and "Python 3.8" in r.explanation


async def test_judge_catches_rule_break():
    s = convo("We must not use any external libraries in this project.")
    rule = [
        {
            "kind": "constraint",
            "text": "Must not use external libraries",
            "subject": "external libraries",
        }
    ]
    judge = judge_says(
        {
            "verdict": "contradicts",
            "memory_index": 0,
            "confidence": 0.9,
            "explanation": "You told Claude not to use external libraries, "
            "but this code imports requests.",
        }
    )
    det = MemoryConsistency(judge=judge, extract_judge=extractor(rule))
    code = "import requests\nrequests.get('https://x.org', timeout=5)"
    r = await det.check(claim(code, code=code, ctype="code_api"), s)
    assert r.status == "contradicted" and "external libraries" in r.explanation


async def test_unsure_judge_is_amber_not_red():
    s = convo("Our API only allows 100 requests per minute.")
    judge = judge_says(
        {
            "verdict": "contradicts",
            "memory_index": 0,
            "confidence": 0.6,
            "explanation": "Maybe too fast.",
        }
    )
    det = MemoryConsistency(judge=judge, extract_judge=extractor(RATE))
    r = await det.check(claim("Call the API rate limit endpoint in a tight loop."), s)
    assert r.status == "uncertain"


async def test_silent_when_memory_has_nothing_relevant():
    s = convo("Our API only allows 100 requests per minute.")
    judge = judge_says({"verdict": "contradicts", "memory_index": 0, "confidence": 0.99})
    det = MemoryConsistency(judge=judge, extract_judge=extractor(RATE))
    assert await det.check(claim("Photosynthesis happens in chloroplasts."), s) is None
    assert judge.calls == []


async def test_silent_when_judge_says_unrelated():
    s = convo("Our API only allows 100 requests per minute.")
    det = MemoryConsistency(
        judge=judge_says({"verdict": "unrelated"}), extract_judge=extractor(RATE)
    )
    assert await det.check(claim("The API rate limit docs are on the website."), s) is None


async def test_each_user_message_is_ingested_once():
    s = convo("Our API only allows 100 requests per minute.")
    ex = extractor(RATE)
    det = MemoryConsistency(judge=judge_says({"verdict": "unrelated"}), extract_judge=ex)
    import asyncio

    await asyncio.gather(*(det.check(claim(f"rate limit note {i}"), s) for i in range(4)))
    assert len(ex.calls) == 1  # parallel claims share one extraction (per-session lock)


async def test_ai_messages_are_never_ingested():
    s = convo("hi")
    s.messages.append(
        ChatMessage(
            message_id="a0",
            role="assistant",
            position=0,
            text="Your API limit is 5000 requests per minute.",
        )
    )
    ex = extractor(RATE)
    await MemoryConsistency(judge=judge_says({}), extract_judge=ex).check(claim("x"), s)
    assert all("5000" not in c for c in ex.calls)


async def test_memory_from_an_earlier_session_is_used():
    await add_card(
        "local", "constraint", "API rate limit is 100 requests per minute", "api rate limit"
    )
    s = SessionContext(session_id="new-chat")  # brand-new chat, nothing said yet
    s.messages.append(ChatMessage(message_id="a1", role="assistant", text="x", position=0))
    det = MemoryConsistency(judge=judge_says({"verdict": "unrelated"}), extract_judge=extractor([]))
    r = await det.check(claim("Fire 900 requests per minute."), s)
    assert r.status == "contradicted"  # personalised across chats


# --------------------------------------------------------------------------- engine end-to-end
async def test_engine_turns_forgotten_constraint_red(monkeypatch, tmp_path):
    """Full pipeline: user states a limit, assistant breaks it → red verdict."""
    from app import plugins
    from app.ledger import Ledger
    from app.models import MessageNew
    from app.session import Session

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # heuristic extraction path
    mc = MemoryConsistency(
        judge=judge_says({"verdict": "unrelated"}), extract_judge=extractor(RATE)
    )
    monkeypatch.setattr(
        plugins, "get_detector", lambda n: mc if n == "memory_consistency" else None
    )
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    s = Session("e2e", ledger)
    await s.on_message(
        MessageNew(
            message_id="u1",
            role="user",
            position=0,
            text="Our API only allows 100 requests per minute.",
        )
    )
    out = await s.on_message(
        MessageNew(
            message_id="a1",
            role="assistant",
            position=1,
            text="To go faster, the script now sends 500 requests per minute to the API.",
        )
    )
    await ledger.close()
    claims = out[0].payload["claims"]
    assert any(
        c["final"] == "red"
        and any(r["detector"] == "memory_consistency" for r in c["detector_results"])
        for c in claims
    )


# --------------------------------------------------------------------------- far down the chat
async def test_violation_many_turns_later_is_still_caught():
    """Rule in turn 1, 12 unrelated exchanges, violation at turn 14."""
    s = SessionContext(session_id="long")
    s.messages.append(
        ChatMessage(
            message_id="u0",
            role="user",
            position=0,
            text="Our API only allows 100 requests per minute.",
        )
    )
    pos = 1
    for i in range(12):  # small talk about other things
        s.messages.append(
            ChatMessage(
                message_id=f"a{i}",
                role="assistant",
                position=pos,
                text=f"Here is step {i} of the report layout.",
            )
        )
        s.messages.append(
            ChatMessage(
                message_id=f"u{i + 1}",
                role="user",
                position=pos + 1,
                text=f"Thanks, now format section {i} as a table.",
            )
        )
        pos += 2
    s.messages.append(
        ChatMessage(
            message_id="late",
            role="assistant",
            position=pos,
            text="To speed it up, fire 600 requests per minute.",
        )
    )
    by_msg = {"Our API only allows 100 requests per minute.": RATE}

    async def extract(system, user, **kw):
        return {"cards": next((v for k, v in by_msg.items() if k in user), [])}

    det = MemoryConsistency(judge=judge_says({"verdict": "unrelated"}), extract_judge=extract)
    c = Claim(
        claim_id="x",
        message_id="late",
        quote="fire 600 requests per minute",
        normalized="The script sends 600 requests per minute.",
        type="number",
        risk="high",
    )
    r = await det.check(c, s)
    assert r is not None and r.status == "contradicted" and "600" in r.explanation


# --------------------------------------------------------------------------- paraphrase (Voyage)
async def test_paraphrase_found_only_with_embeddings(monkeypatch):
    """ "Stop throttling your calls" shares no words with "API rate limit is 100 requests per
    minute" — word matching can't link them; meaning-based embeddings can."""
    await add_card(
        "local", "constraint", "API rate limit is 100 requests per minute", "api rate limit"
    )
    claim_text = "Stop throttling your calls entirely so everything finishes sooner."
    judge = judge_says(
        {
            "verdict": "contradicts",
            "memory_index": 0,
            "confidence": 0.9,
            "explanation": "You told Claude the API allows only 100 requests "
            "per minute; removing throttling breaks that.",
        }
    )
    s = SessionContext(session_id="p")
    s.messages.append(ChatMessage(message_id="a1", role="assistant", text="x", position=0))
    det = MemoryConsistency(judge=judge, extract_judge=extractor([]))

    # 1) no Voyage key → words only → nothing found → silent
    assert await det.check(claim(claim_text), s) is None

    # 2) with embeddings: rate-limit card and throttling claim are close in meaning
    async def fake_many(texts, input_type="document"):
        return [
            [1.0, 0.1] if ("rate limit" in t or "throttling" in t) else [0.0, 1.0] for t in texts
        ]

    monkeypatch.setattr(memory, "embed_many", fake_many)
    s2 = SessionContext(session_id="p2")
    s2.messages.append(ChatMessage(message_id="a1", role="assistant", text="x", position=0))
    r = await det.check(claim(claim_text), s2)
    assert r is not None and r.status == "contradicted"


async def test_voyage_request_shape(monkeypatch):
    """query vs document input_type, model, dimension, and graceful failure."""
    import httpx

    seen = []

    def handler(req):
        import json

        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "data": [{"index": i, "embedding": [0.1, 0.2]} for i in range(len(body["input"]))]
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "test")
    assert await memory.embed("fire 600 requests", "query") == [0.1, 0.2]
    assert await memory.embed_many(["a card", "another"], "document") == [[0.1, 0.2]] * 2
    assert seen[0]["input_type"] == "query" and seen[1]["input_type"] == "document"
    assert seen[0]["model"] == memory.EMBED_MODEL and seen[0]["output_dimension"] == 512

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(500)), **kw),
    )
    assert await memory.embed("x", "query") is None  # never raises; falls back to words


async def test_old_cards_get_embedded_when_key_appears(monkeypatch):
    await add_card("local", "constraint", "API rate limit is 100 requests per minute")
    assert (await memory.list_cards("local"))[0].embedding is None  # made without a key

    async def fake_many(texts, input_type="document"):
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(memory, "embed_many", fake_many)
    await memory.relevant("local", "requests per minute")
    assert (await memory.list_cards("local"))[0].embedding == [1.0, 0.0]
