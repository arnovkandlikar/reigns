"""Regression tests for bugs found by the QA run (tests/detectors/qa.py). Owner: Role C.

A3  BERT cited as 2015 passed: every record said 2019, so the "later re-registration" rule
    assumed 2015 was the original year.
A14 df.interpolate_missing() passed: calls on variables weren't typed (df = pd.read_csv(...)),
    and DataFrame's column __getattr__ made every missing name look possibly valid.
F7  the correct "Everest is 8,849 m" went red: a rounding-level "contradiction" (8,848.86 m)
    was stored as a correction card worded by the model, and memory then replayed it.
"""

from __future__ import annotations

import httpx
import pytest

from app.detectors.claim_verifier import ClaimVerifier, rounding_only
from app.detectors.code_api_checker import analyze
from app.detectors.reference_auditor import ReferenceAuditor
from app.learning.memory import correction_text
from app.models import Claim, DetectorResult, Evidence, SessionContext


def paper_claim(text: str) -> Claim:
    return Claim(
        claim_id="c1", message_id="m1", quote=text, normalized=text, type="paper", risk="high"
    )


BERT_TITLE = "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
BERT_AUTHORS = ["Jacob Devlin", "Ming-Wei Chang", "Kenton Lee", "Kristina Toutanova"]


def bert_handler(year: int):
    def handler(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        if host == "api.crossref.org":
            item = {
                "DOI": "10.18653/v1/N19-1423",
                "title": [BERT_TITLE],
                "author": [{"given": a.split()[0], "family": a.split()[-1]} for a in BERT_AUTHORS],
                "issued": {"date-parts": [[year]]},
            }
            return httpx.Response(200, json={"message": {"items": [item]}})
        if host == "api.openalex.org":
            work = {
                "id": "https://openalex.org/W1",
                "display_name": BERT_TITLE,
                "publication_year": year,
                "doi": None,
                "authorships": [{"author": {"display_name": a}} for a in BERT_AUTHORS],
            }
            return httpx.Response(200, json={"results": [work]})
        if host == "api.semanticscholar.org":
            paper = {
                "paperId": "b",
                "title": BERT_TITLE,
                "year": year,
                "authors": [{"name": a} for a in BERT_AUTHORS],
                "externalIds": {},
                "url": "https://www.semanticscholar.org/paper/b",
            }
            return httpx.Response(200, json={"total": 1, "offset": 0, "data": [paper]})
        return httpx.Response(404)

    return handler


# ------------------------------------------------------------------ A3
async def test_a3_citing_years_before_the_paper_existed_is_not_supported():
    ra = ReferenceAuditor(transport=httpx.MockTransport(bert_handler(2019)))
    r = await ra.check(
        paper_claim(f'Devlin, Chang, Lee and Toutanova (2015), "{BERT_TITLE}", ' "NAACL."),
        SessionContext(session_id="s"),
    )
    assert r.status != "supported"
    assert "2019" in r.explanation


async def test_a3_correct_year_still_supported():
    ra = ReferenceAuditor(transport=httpx.MockTransport(bert_handler(2019)))
    r = await ra.check(
        paper_claim(f'Devlin et al. (2019), "{BERT_TITLE}", NAACL.'), SessionContext(session_id="s")
    )
    assert r.status == "supported"


# ------------------------------------------------------------------ A14
def issues(code: str) -> list[str]:
    return [i["detail"] for i in analyze(code)["issues"]]


def test_a14_missing_dataframe_method_is_caught():
    code = (
        "import pandas as pd\n\ndf = pd.read_csv('weather.csv')\n"
        "df = df.interpolate_missing()\nprint(df.head())\n"
    )
    assert issues(code) == ["pandas.DataFrame has no 'interpolate_missing'"]


@pytest.mark.parametrize(
    "code",
    [
        # real methods, chained reassignments
        "import pandas as pd\ndf = pd.read_csv('x.csv')\ndf = df.dropna()\ndf = df.interpolate()\n"
        "print(df.head())\n",
        # column access is not a method call
        "import pandas as pd\ndf = pd.read_csv('x.csv')\nprint(df.price.mean())\n",
        # after a reassignment to another type, stop guessing
        "import pandas as pd\ndf = pd.read_csv('x.csv')\ndf = df.to_numpy()\nprint(df.reshape(2, -1))\n",
        # a parameter with the same name in another function is a different variable
        "import pandas as pd\ndf = pd.read_csv('x.csv')\ndef f(df):\n    return df.str.lower()\n",
        "import requests\nr = requests.get('https://x.org', timeout=5)\nprint(r.json())\n",
    ],
)
def test_a14_no_false_alarms_on_valid_code(code):
    assert issues(code) == []


def test_a14_response_and_ndarray_methods_are_checked():
    assert issues("import requests\nr = requests.get('https://x.org')\nr.jsonn()\n") == [
        "requests.Response has no 'jsonn'"
    ]
    assert issues("import numpy as np\na = np.array([1, 2])\na.reshape_to(2)\n") == [
        "numpy.ndarray has no 'reshape_to'"
    ]


# ------------------------------------------------------------------ F7
@pytest.mark.parametrize(
    "claim_text,quote,want",
    [
        ("Mount Everest is 8,849 metres tall.", "Its elevation is 8,848.86 m (29,031.7 ft).", True),
        ("The Eiffel Tower is about 330 metres tall.", "It is 330 metres (1,083 ft) tall", True),
        ("Mount Everest is 7,849 metres tall.", "Its elevation is 8,848.86 m.", False),
        ("The Eiffel Tower was completed in 1899.", "completed on 31 March 1889", False),
        (
            "Python was created by Guido van Rossum.",
            "Python was created by Guido van Rossum.",
            False,
        ),
    ],
)
def test_f7_rounding_detection(claim_text, quote, want):
    assert rounding_only(claim_text, quote) is want


async def test_f7_rounding_level_contradiction_is_not_red(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    quote = "Its elevation is 8,848.86 m (29,031.7 ft) above sea level."

    def web(req):
        if req.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": "https://en.wikipedia.org/wiki/Mount_Everest", "content": quote}
                    ]
                },
            )
        return httpx.Response(200, json={"query": {"search": []}})

    async def judge(system, user):
        return {
            "verdict": "contradicted",
            "confidence": 0.9,
            "evidence_index": 0,
            "quote": quote,
            "explanation": "Wikipedia says 8,848 m, not 8,849 m.",
        }

    c = Claim(
        claim_id="c",
        message_id="m",
        quote="Everest is 8,849 metres tall.",
        normalized="Mount Everest is 8,849 metres tall.",
        type="fact",
        risk="high",
    )
    r = await ClaimVerifier(transport=httpx.MockTransport(web), judge=judge).check(
        c, SessionContext(session_id="x")
    )
    assert r.status == "supported" and "rounding" in r.explanation


def _result(explanation: str, snippet_text: str) -> DetectorResult:
    return DetectorResult(
        detector="claim_verifier",
        status="contradicted",
        confidence=0.9,
        evidence=[Evidence(source="Wikipedia", snippet=snippet_text)],
        explanation=explanation,
    )


def test_f7_correction_card_uses_only_numbers_from_the_evidence():
    # the model's explanation invents "8,849" (not in the evidence) → fall back to plain text
    bad = _result(
        "Wikipedia says Everest is about 8,848 m, not 8,849 m.", "Its elevation is 8,848.86 m."
    )
    assert correction_text("Mount Everest is 7,849 metres tall.", bad) == (
        "Not true: Mount Everest is 7,849 metres tall."
    )
    # grounded explanation is kept
    good = _result(
        "Wikipedia says it was completed in 1889, not 1899.",
        "Constructed from 1887 to 1889 as the centerpiece of the 1889 World's Fair",
    )
    assert correction_text("The Eiffel Tower was completed in 1899.", good).startswith(
        "Wikipedia says it was completed in 1889"
    )


# ------------------------------------------------------------------ many citations in one reply
async def test_five_papers_finish_within_the_engine_budget(monkeypatch):
    """Live log: a reply citing 5 papers timed out (12 s) and left every citation unchecked."""
    import asyncio
    import time

    from app.detectors import reference_auditor as ra

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(0.4)  # every API call takes 0.4 s
            return httpx.Response(
                200,
                json={"message": {"items": []}, "results": [], "total": 0, "offset": 0, "data": []},
            )

    auditor = ReferenceAuditor(transport=Slow())
    claims = [
        paper_claim(f'Smith (2020), "A Study of Made Up Topic Number {i} in Bees"')
        for i in range(5)
    ]
    t0 = time.perf_counter()
    results = await asyncio.gather(
        *(auditor.check(c, SessionContext(session_id="s")) for c in claims)
    )
    assert time.perf_counter() - t0 < ra.PAPER_BUDGET_S + 1
    assert all(r.status in ("contradicted", "unverified") for r in results)


async def test_slow_databases_never_produce_a_false_red(monkeypatch):
    """Only Crossref answered before the budget; OpenAlex (which has arXiv papers) was still
    running → not enough to call the paper fake."""
    import asyncio

    from app.detectors import reference_auditor as ra

    monkeypatch.setattr(ra, "PAPER_BUDGET_S", 0.3)

    class Mixed(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.host != "api.crossref.org":
                await asyncio.sleep(2)
            return httpx.Response(
                200,
                json={"message": {"items": []}, "results": [], "total": 0, "offset": 0, "data": []},
            )

    r = await ReferenceAuditor(transport=Mixed()).check(
        paper_claim('Smith (2020), "Some Real arXiv Paper About Bees"'),
        SessionContext(session_id="s"),
    )
    assert r.status == "unverified" and "in time" in r.explanation


async def test_a2_rereg_in_every_database_is_still_the_real_paper(monkeypatch):
    """After the A3 fix, QA run 2: every database (not just Crossref) listed the 2017
    transformer paper under a recent 2025 record → it must stay supported."""
    from app.detectors import reference_auditor as ra

    monkeypatch.setattr(ra, "_this_year", lambda: 2026)
    title = "Attention Is All You Need"
    authors = ["Ashish Vaswani", "Noam Shazeer"]

    def handler(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        if host == "api.crossref.org":
            item = {
                "DOI": "10.1/x",
                "title": [title],
                "issued": {"date-parts": [[2025]]},
                "author": [{"given": a.split()[0], "family": a.split()[-1]} for a in authors],
            }
            return httpx.Response(200, json={"message": {"items": [item]}})
        if host == "api.openalex.org":
            work = {
                "id": "https://openalex.org/W2",
                "display_name": title,
                "publication_year": 2025,
                "doi": None,
                "authorships": [{"author": {"display_name": a}} for a in authors],
            }
            return httpx.Response(200, json={"results": [work]})
        return httpx.Response(429)

    r = await ReferenceAuditor(transport=httpx.MockTransport(handler)).check(
        paper_claim(f'Vaswani et al. (2017), "{title}", NeurIPS.'), SessionContext(session_id="s")
    )
    assert r.status == "supported"


# ------------------------------------------------------------------ F7 (run 3): reported claims
def _reply_session(reply: str) -> SessionContext:
    from app.models import ChatMessage

    s = SessionContext(session_id="r")
    s.messages += [
        ChatMessage(message_id="u", role="user", position=0, text="How tall is Everest?"),
        ChatMessage(message_id="m", role="assistant", position=1, text=reply),
    ]
    return s


@pytest.mark.parametrize(
    "reply,quote,want",
    [
        (
            "Some old sites say Everest is 7,849 metres, but the official height is 8,849 metres.",
            "Some old sites say Everest is 7,849 metres",
            True,
        ),
        (
            "A common myth is that the Great Wall is visible from the Moon; in reality it isn't.",
            "A common myth is that the Great Wall is visible from the Moon",
            True,
        ),
        (
            "It is often said that goldfish have 3-second memories, but studies show months.",
            "It is often said that goldfish have 3-second memories",
            True,
        ),
        # attribution WITHOUT rejection stays checkable
        (
            "Most sources say Everest is 8,849 metres tall.",
            "Most sources say Everest is 8,849 metres tall",
            False,
        ),
        # plain assertion
        ("Everest is 7,849 metres tall.", "Everest is 7,849 metres tall", False),
    ],
)
def test_reported_then_rejected(reply, quote, want):
    from app.detectors.claim_gate import reported_then_rejected

    c = Claim(claim_id="c", message_id="m", quote=quote, normalized=quote, type="fact", risk="high")
    assert reported_then_rejected(_reply_session(reply), c) is want


async def test_reported_claim_is_not_checked_even_if_the_model_would_say_world_fact():
    from app.detectors.claim_gate import gate

    reply = "Some old sites say Everest is 7,849 metres, but the official height is 8,849 metres."
    quote = "Some old sites say Everest is 7,849 metres"
    c = Claim(claim_id="c", message_id="m", quote=quote, normalized=quote, type="fact", risk="high")

    async def model(system, user, **kw):
        return {"standalone": quote, "kind": "world_fact", "subject": "everest", "question": "?"}

    g = await gate(c, _reply_session(reply), judge=model)
    assert g.kind == "not_asserted"


# ------------------------------------------------------------------ F7 (run 4): probe rounding
def test_probe_counts_rounded_numbers_as_the_same_answer():
    from app.detectors.consistency_probe import _rounding_match

    answers = ["Mount Everest's official height is 8,848.86 metres."] * 4 + ["8,850 m"]
    groups = [[0, 1, 2, 3], [4]]
    # the grouping model said the original (8,849) matched nothing
    assert _rounding_match("The official height is 8,849 metres.", answers, groups, None) == 0
    # a genuinely different number stays unmatched
    assert _rounding_match("Everest is 7,849 metres tall.", answers, groups, None) is None


# ------------------------------------------------------------------ search outage (long-chat QA)
def _claim(text: str) -> Claim:
    return Claim(
        claim_id="c", message_id="m", quote=text, normalized=text, type="fact", risk="high"
    )


def _outage_handler(brave_ok: bool = False, wiki_text: str | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        if host == "api.tavily.com":
            return httpx.Response(
                432, json={"detail": {"error": "exceeds your plan's usage limit"}}
            )
        if host == "api.search.brave.com":
            if not brave_ok:
                return httpx.Response(401)
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "url": "https://example.org/eiffel",
                                "description": "The Eiffel Tower was completed in 1889.",
                            }
                        ]
                    }
                },
            )
        if host == "en.wikipedia.org":
            params = req.url.params
            if params.get("list") == "search":
                hits = [{"title": "Eiffel Tower"}] if wiki_text else []
                return httpx.Response(200, json={"query": {"search": hits}})
            return httpx.Response(
                200, json={"query": {"pages": {"1": {"extract": wiki_text or ""}}}}
            )
        return httpx.Response(404)

    return handler


async def test_search_outage_is_not_amber(monkeypatch):
    """Tavily usage limit + Wikipedia doesn't settle it → 'couldn't check' (error), not amber."""
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    async def judge(system, user):
        return {"verdict": "unverified", "confidence": 0.4, "quote": "", "explanation": "?"}

    v = ClaimVerifier(
        transport=httpx.MockTransport(
            _outage_handler(
                wiki_text="The Eiffel Tower is a lattice tower in Paris. It is named after Gustave Eiffel."
            )
        ),
        judge=judge,
    )
    r = await v.check(_claim("Flask was first released in 2010."), SessionContext(session_id="s"))
    assert r.status == "error" and "unavailable" in r.explanation


async def test_search_outage_falls_back_to_brave(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setenv("BRAVE_API_KEY", "b")
    seen = []

    async def judge(system, user):
        seen.append(user)
        return {
            "verdict": "supported",
            "confidence": 0.9,
            "evidence_index": 0,
            "quote": "The Eiffel Tower was completed in 1889.",
            "explanation": "ok",
        }

    v = ClaimVerifier(transport=httpx.MockTransport(_outage_handler(brave_ok=True)), judge=judge)
    r = await v.check(
        _claim("The Eiffel Tower was completed in 1889."), SessionContext(session_id="s")
    )
    assert r.status == "supported" and "example.org" in seen[0]


async def test_wikipedia_still_decides_during_an_outage(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    text = "The Eiffel Tower was completed in 1889 for the World's Fair."

    async def judge(system, user):
        return {
            "verdict": "supported",
            "confidence": 0.95,
            "evidence_index": 0,
            "quote": "The Eiffel Tower was completed in 1889",
            "explanation": "ok",
        }

    v = ClaimVerifier(transport=httpx.MockTransport(_outage_handler(wiki_text=text)), judge=judge)
    r = await v.check(
        _claim("The Eiffel Tower was completed in 1889."), SessionContext(session_id="s")
    )
    assert r.status == "supported"


# ------------------------------------------------------------------ long chat: memory numbers
def test_memory_card_numbers_must_come_from_the_user():
    from app.learning.memory import numbers_grounded

    user = "We're stuck on Python 3.8 and the API only allows 100 requests per minute."
    assert numbers_grounded("API rate limit is 100 requests per minute.", user)
    assert numbers_grounded("Uses Python 3.8.", user)
    assert not numbers_grounded(
        "API rate limit is 100 requests per minute; sleep 0.6 seconds between calls.", user
    )
    assert numbers_grounded("Budget is $1,200.", "my budget is $1200")


async def test_extraction_drops_cards_built_from_the_assistants_advice():
    from app.learning.memory import extract_user_cards

    async def model(system, user, **kw):
        return {
            "cards": [
                {
                    "kind": "constraint",
                    "subject": "api rate limit",
                    "text": "API rate limit is 100 requests per minute; sleep 0.6 seconds "
                    "between calls.",
                }
            ]
        }

    cards = await extract_user_cards(
        "What happens if I go over the limit?", "Sleep 0.6 seconds between calls.", judge=model
    )
    assert cards == []


def test_retry_backoff_is_not_checked_against_a_pacing_interval():
    from app.detectors.memory_consistency import number_violation
    from app.learning.memory import MemoryCard

    card = MemoryCard(
        card_id="c",
        user_id="u",
        kind="constraint",
        text="API rate limit is 100 requests per minute; wait 0.6 seconds between calls.",
        subject="api rate limit",
        source="user_message",
        confidence=1.0,
        created_at="2026-09-27T00:00:00Z",
        last_used_at="2026-09-27T00:00:00Z",
    )
    assert number_violation(card, "fall back to 60 seconds if Retry-After is missing") is None
    # the rate itself is still enforced
    assert number_violation(card, "that's 1,000 requests per minute") is not None


# ------------------------------------------------------------------ long chat: verified echoes
DROPNA_FACT = (
    "The pandas DataFrame method dropna() returns a new DataFrame by default, but when "
    "inplace=True is passed, it modifies the original DataFrame in place and returns None instead."
)
NOZOMI_FACT = (
    "Nozomi trains on the Tokaido Shinkansen take about 2 hours 15 minutes to travel from "
    "Tokyo to Kyoto."
)
NOZOMI_CLAIM = (
    "Nozomi trains on the Tokaido Shinkansen take approximately 2 hours 15 minutes to travel "
    "from Tokyo to Kyoto."
)
NOZOMI_BAD_CORRECTION = (
    "Wikipedia states the fastest Nozomi service takes 2 hours 21 minutes from Tokyo to Osaka "
    "(which is beyond Kyoto), not 2 hours 15 minutes to Kyoto."
)


@pytest.mark.parametrize(
    "fact,claim",
    [
        (
            DROPNA_FACT,
            "df.dropna() returns a new DataFrame by default, but with inplace=True it modifies "
            "the original",
        ),
        (NOZOMI_FACT, NOZOMI_CLAIM),
        ("The Eiffel Tower is about 330 metres tall.", "The Eiffel Tower is 330 metres tall."),
    ],
)
def test_a_claim_saying_what_was_verified_echoes_it(fact, claim):
    from app.learning.memory import echoes

    assert echoes(fact, claim)


@pytest.mark.parametrize(
    "fact,claim",
    [
        (NOZOMI_FACT, "Nozomi trains take about 2 hours 45 minutes from Tokyo to Kyoto."),
        ("The Eiffel Tower was completed in 1889.", "The Eiffel Tower was completed in 1899."),
        ("The Eiffel Tower is 330 metres tall.", "The Eiffel Tower in London is 330 metres tall."),
        ("Tipping is customary in the US.", "Tipping is not customary in the US."),
        ("Canberra is the capital of Australia.", "Sydney is the capital of Australia."),
    ],
)
def test_a_different_claim_does_not_echo(fact, claim):
    from app.learning.memory import echoes

    assert not echoes(fact, claim)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from app.learning import memory

    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("REIGNS_USER", raising=False)
    store = memory.MemoryStore(str(tmp_path / "mem.db"))
    memory.set_store(store)
    monkeypatch.setenv("REIGNS_MEMORY_DB", store.path)  # else get_store() swaps it back
    yield memory
    memory.set_store(None)


def _world_gate(standalone: str, subject: str):
    async def judge(system, user, **kw):
        return {"standalone": standalone, "kind": "world_fact", "subject": subject,
                "question": None}

    return judge


async def test_memory_never_contradicts_a_claim_it_verified_earlier(ledger):
    """Japan long chat: a bad correction card (Tokyo→Osaka figure) made the verified
    Tokyo→Kyoto 2 h 15 min red. The verified card wins; memory stays silent."""
    from app.detectors.memory_consistency import MemoryConsistency

    await ledger.add_card("local", "verified_fact", NOZOMI_FACT, "nozomi travel time",
                          "claim_verifier", 0.9)
    await ledger.add_card("local", "correction", NOZOMI_BAD_CORRECTION, "nozomi travel time",
                          "claim_verifier", 0.9)

    async def judge(system, user, **kw):
        return {"verdict": "contradicts", "memory_index": 0, "confidence": 0.9,
                "explanation": "Earlier this was verified: 2 hours 21 minutes."}

    async def no_cards(*a, **k):
        return {"cards": []}

    mc = MemoryConsistency(judge=judge, extract_judge=no_cards,
                           gate_judge=_world_gate(NOZOMI_CLAIM, "nozomi travel time"))
    c = Claim(claim_id="c1", message_id="a1", quote=NOZOMI_CLAIM, normalized=NOZOMI_CLAIM,
              type="fact", risk="high")
    assert await mc.check(c, SessionContext(session_id="s")) is None


async def test_a_real_contradiction_of_a_verified_fact_still_goes_red(ledger):
    from app.detectors.memory_consistency import MemoryConsistency

    await ledger.add_card("local", "verified_fact", "The Eiffel Tower was completed in 1889.",
                          "eiffel tower completion", "claim_verifier", 0.9)
    claim_text = "The Eiffel Tower was completed in 1899."

    async def judge(system, user, **kw):
        return {"verdict": "contradicts", "memory_index": 0, "confidence": 0.9,
                "explanation": "Earlier this was verified: completed in 1889."}

    async def no_cards(*a, **k):
        return {"cards": []}

    mc = MemoryConsistency(judge=judge, extract_judge=no_cards,
                           gate_judge=_world_gate(claim_text, "eiffel tower completion"))
    c = Claim(claim_id="c1", message_id="a1", quote=claim_text, normalized=claim_text,
              type="fact", risk="high")
    r = await mc.check(c, SessionContext(session_id="s"))
    assert r is not None and r.status == "contradicted"


async def test_no_correction_is_stored_over_a_verified_fact(ledger):
    from app.models import ClaimVerdict

    await ledger.add_card("local", "verified_fact", NOZOMI_FACT, "nozomi travel time",
                          "claim_verifier", 0.9)
    c = Claim(claim_id="c1", message_id="a1", quote=NOZOMI_CLAIM, normalized=NOZOMI_CLAIM,
              type="fact", risk="high")
    ev = Evidence(source="Wikipedia", url="https://en.wikipedia.org/wiki/Nozomi",
                  snippet="The fastest Nozomi takes 2 hours 21 minutes to Shin-Osaka.")
    v = ClaimVerdict(
        claim_id="c1", quote=NOZOMI_CLAIM, type="fact", risk="high", final="red",
        detector_results=[DetectorResult(detector="claim_verifier", status="contradicted",
                                         confidence=0.9, evidence=[ev],
                                         explanation="Wikipedia says 2 hours 21 minutes.")],
    )
    stored = await ledger.on_verdicts(SessionContext(session_id="s"), [c], [v])
    assert stored == []
    assert [x.kind for x in await ledger.list_cards("local")] == ["verified_fact"]


async def test_a_correction_with_no_conflicting_verified_fact_is_still_stored(ledger):
    from app.models import ClaimVerdict

    t = "The Eiffel Tower was completed in 1899."
    c = Claim(claim_id="c1", message_id="a1", quote=t, normalized=t, type="fact", risk="high")
    ev = Evidence(source="Wikipedia", url="https://en.wikipedia.org/wiki/Eiffel_Tower",
                  snippet="The tower was completed in 1889.")
    v = ClaimVerdict(
        claim_id="c1", quote=t, type="fact", risk="high", final="red",
        detector_results=[DetectorResult(detector="claim_verifier", status="contradicted",
                                         confidence=0.9, evidence=[ev],
                                         explanation="Wikipedia says 1889, not 1899.")],
    )
    stored = await ledger.on_verdicts(SessionContext(session_id="s"), [c], [v])
    assert [x.kind for x in stored] == ["correction"]
