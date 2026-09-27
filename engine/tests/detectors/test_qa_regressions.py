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


# ------------------------------------------------------------------ Part C: judge wraps its JSON
def test_one_object_unwraps_a_list():
    from app.detectors.base import one_object

    assert one_object({"verdict": "supported"}) == {"verdict": "supported"}
    assert one_object([{"verdict": "supported"}]) == {"verdict": "supported"}
    assert one_object([]) is None and one_object("x") is None and one_object([1, 2]) is None


async def test_verifier_reads_a_verdict_wrapped_in_a_list(monkeypatch):
    """C1: "Earth orbits the Sun about once every 365.25 days" → claim_verifier error
    "judge returned list, expected an object", silently dropping the web check."""
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    text = "Earth orbits the Sun once every 365.25 days."

    def web(req):
        if req.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": [
                {"url": "https://en.wikipedia.org/wiki/Year", "content": text}]})
        return httpx.Response(200, json={"query": {"search": []}})

    async def judge(system, user, **kw):
        return [{"verdict": "supported", "confidence": 0.95, "evidence_index": 0,
                 "quote": text, "explanation": "Wikipedia says 365.25 days."}]

    v = ClaimVerifier(transport=httpx.MockTransport(web), judge=judge)
    r = await v.check(
        Claim(claim_id="c", message_id="m", quote=text, normalized=text, type="fact",
              risk="high"),
        SessionContext(session_id="s"),
    )
    assert r.status == "supported"


# ------------------------------------------------------------------ Part C, C7: probe on bundles
PY_BUNDLE = (
    "Python borrowed design elements from C, Unix, Modula-3, and ABC; some of these barely "
    "existed in 1980."
)


@pytest.mark.parametrize(
    "text,vague",
    [
        (PY_BUNDLE, True),
        ("Many of these languages barely survived the 1990s.", True),
        ("Flask was first released in 2010; Django was first released in 2005.", True),
        ("The Eiffel Tower was completed in 1889.", False),
        ("Typical home computers in 1980 had 8-bit CPUs and 16–64 KB of RAM.", True),
        ("Around 1984–87, machines grew to have 256 KB–1 MB of RAM.", True),
        ("ABC, a predecessor to Python, was built at CWI in the early 1980s.", True),
        ("The Eiffel Tower is about 330 metres tall.", False),
        ("The Ming dynasty ruled from 1368–1644.", False),
        ("Perl was released in 1987 and Tcl was released in 1988.", False),
    ],
)
def test_probe_skips_bundled_or_hedged_claims(text, vague):
    from app.detectors.consistency_probe import too_vague_to_probe

    assert too_vague_to_probe(text) is vague


def test_a_shorter_answer_contained_in_the_claim_agrees_with_it():
    from app.detectors.consistency_probe import _contained_match

    five = [[0, 1, 2, 3, 4]]
    assert _contained_match(PY_BUNDLE, ["C and Unix."] * 5, five, None) == 0
    # a genuinely different answer is still a mismatch
    assert _contained_match("The Eiffel Tower was completed in 1899.", ["1889"] * 5, five, None) is None
    # a long answer can't sneak in by sharing words
    long = "Python drew on C and Unix and also on many other languages from the 1980s and 1990s"
    assert _contained_match(PY_BUNDLE, [long] * 5, five, None) is None


async def test_probe_does_not_sample_a_bundled_claim():
    from app.detectors.consistency_probe import ConsistencyProbe

    async def never(*a, **k):
        raise AssertionError("must not sample")

    async def gate_judge(system, user, **kw):
        return {"standalone": PY_BUNDLE, "kind": "world_fact", "subject": "python influences",
                "question": "Which languages influenced Python?"}

    p = ConsistencyProbe(sampler=never, judge=never, gate_judge=gate_judge)
    c = Claim(claim_id="c", message_id="m", quote=PY_BUNDLE, normalized=PY_BUNDLE, type="fact",
              risk="high")
    assert await p.check(c, SessionContext(session_id="s")) is None


# ------------------------------------------------------------------ Part C, C7: quoted phrases
def _paper(quote: str, normalized: str) -> Claim:
    return Claim(claim_id="c", message_id="m", quote=quote, normalized=normalized, type="paper",
                 risk="high")


def test_a_quoted_phrase_in_prose_is_not_a_citation():
    from app.detectors.reference_auditor import looks_like_citation, parse_paper

    glue = _paper('Scripting and Unix: Perl (1987) filled the "glue language" gap that shell '
                  "and awk left", "The paper 'glue language' exists.")
    assert not looks_like_citation(glue, parse_paper(glue))
    deep = _paper('LeCun, Bengio & Hinton (2015), "Deep learning", Nature',
                  "The paper 'Deep learning' exists.")
    assert looks_like_citation(deep, parse_paper(deep))
    bert = _paper(f'Devlin et al. (2019), "{BERT_TITLE}"', f"The paper '{BERT_TITLE}' exists.")
    assert looks_like_citation(bert, parse_paper(bert))


async def test_auditor_stays_silent_on_a_quoted_phrase():
    def boom(req):
        raise AssertionError("no lookup expected")

    ra = ReferenceAuditor(transport=httpx.MockTransport(boom))
    glue = _paper('Perl (1987) filled the "glue language" gap', "The paper 'glue language' exists.")
    assert await ra.check(glue, SessionContext(session_id="s")) is None


# ------------------------------------------------------------------ Part B, B1.1: failed databases
TINYML = "A Survey of TinyML Applications in Beekeeping for Hive Monitoring and Management"


def _only_crossref_answers(openalex_status: int = 500):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": []}})
        if req.url.host == "api.openalex.org":
            if openalex_status == 200:
                return httpx.Response(200, json={"results": []})
            return httpx.Response(openalex_status)
        if req.url.host == "api.semanticscholar.org":
            return httpx.Response(429)
        return httpx.Response(404)

    return handler


async def test_one_database_alone_cannot_call_a_paper_fake():
    """S2 rate-limited, OpenAlex errored, Crossref (no arXiv preprints) found nothing → the real
    arXiv survey went red. Now: amber, and it says which databases couldn't be searched."""
    ra = ReferenceAuditor(transport=httpx.MockTransport(_only_crossref_answers()))
    c = paper_claim(f'Sucipto, Zhou, Kwon and Chen (2025), "{TINYML}", arXiv.')
    r = await ra.check(c, SessionContext(session_id="s"))
    assert r.status == "unverified"
    assert "couldn't be searched" in r.explanation


async def test_two_databases_finding_nothing_still_means_fake():
    ra = ReferenceAuditor(transport=httpx.MockTransport(_only_crossref_answers(200)))
    c = paper_claim(f'Sucipto, Zhou, Kwon and Chen (2025), "{TINYML}", arXiv.')
    r = await ra.check(c, SessionContext(session_id="s"))
    assert r.status == "contradicted"


# ------------------------------------------------------------------ Part B: shorthand mentions
def test_shorthand_mentions_are_parsed_as_authors_year_and_a_nickname():
    from app.detectors.reference_auditor import parse_paper

    ref = parse_paper(_paper("Giovannesi et al. (2025) Vit4V", "There is a paper … Vit4V."))
    assert (ref.title, ref.surnames, ref.year, ref.quoted) == ("Vit4V", ["Giovannesi"], 2025, False)
    ref = parse_paper(_paper("Lee & Park (2022) honeybee transformers", "x"))
    assert ref.surnames == ["Lee", "Park"] and not ref.quoted
    ref = parse_paper(_paper(f'Devlin et al. (2019), "{BERT_TITLE}"', "x"))
    assert ref.quoted and ref.title == BERT_TITLE


async def test_a_shorthand_mention_is_never_called_fake():
    """My QA summary said "Sucipto et al. (2025) TinyML survey (arXiv)" → red "no paper titled
    'Sucipto et al. (2025) TinyML survey (arXiv)'". Nothing found by a nickname proves nothing."""
    ra = ReferenceAuditor(transport=httpx.MockTransport(_only_crossref_answers(200)))
    c = _paper("Sucipto et al. (2025) TinyML survey (arXiv)",
               "There is a paper by Sucipto et al. from 2025 titled TinyML survey on arXiv.")
    assert await ra.check(c, SessionContext(session_id="s")) is None


async def test_a_shorthand_mention_can_still_be_confirmed():
    def handler(req):
        if req.url.host == "api.crossref.org":
            item = {"DOI": "10.1/x", "title": [TINYML], "issued": {"date-parts": [[2025]]},
                    "author": [{"given": "W.", "family": "Sucipto"}]}
            return httpx.Response(200, json={"message": {"items": [item]}})
        return httpx.Response(429)

    ra = ReferenceAuditor(transport=httpx.MockTransport(handler))
    c = _paper("Sucipto et al. (2025) TinyML survey (arXiv)", "x")
    r = await ra.check(c, SessionContext(session_id="s"))
    assert r is not None and r.status == "supported"


# ------------------------------------------------------------------ Part B: OpenAlex 503 / key
async def test_openalex_retries_a_503_and_sends_the_api_key(monkeypatch):
    from app.detectors import reference_auditor as rmod

    monkeypatch.setattr(rmod, "RETRY_5XX_S", 0)
    monkeypatch.setenv("OPENALEX_API_KEY", "oa-key")
    seen = {"n": 0, "keys": set()}

    def handler(req):
        if req.url.host == "api.openalex.org":
            seen["n"] += 1
            seen["keys"].add(req.url.params.get("api_key"))
            if seen["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404)

    ra = ReferenceAuditor(transport=httpx.MockTransport(handler))
    ref = rmod.PaperRef(title=TINYML, surnames=["Sucipto"], year=2025)
    assert await ra._search_openalex(ref) == []
    assert seen["keys"] == {"oa-key"}


async def test_one_failed_openalex_query_keeps_the_other_results(monkeypatch):
    from app.detectors import reference_auditor as rmod

    monkeypatch.setattr(rmod, "RETRY_5XX_S", 0)
    work = {"id": "W1", "display_name": TINYML, "publication_year": 2025, "doi": None,
            "authorships": [{"author": {"display_name": "W. Sucipto"}}]}

    def handler(req):
        if "filter" in req.url.params:
            return httpx.Response(503)
        return httpx.Response(200, json={"results": [work]})

    ra = ReferenceAuditor(transport=httpx.MockTransport(handler))
    ref = rmod.PaperRef(title=TINYML, surnames=["Sucipto"], year=2025)
    got = await ra._search_openalex(ref)
    assert [c.title for c in got] == [TINYML]


# ------------------------------------------------------------------ Part B, B1.1: new arXiv preprints
BEEVE = ("BeeVe: Unsupervised Discovery of Non-Semantic Acoustic States, Towards a Non-Invasive "
         "Assessment of Honey Bee Colony Health")
ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2601.01234v1</id>
    <published>2026-01-05T00:00:00Z</published>
    <title>BeeVe: Unsupervised Discovery of Non-Semantic Acoustic States, Towards a
      Non-Invasive Assessment of Honey Bee Colony Health</title>
    <author><name>Hesham Hammami</name></author>
    <author><name>Nour Abdulaziz</name></author>
  </entry>
</feed>"""


def _indexes_without_the_preprint(arxiv_feed: str | None, calls: list | None = None):
    def handler(req):
        host = req.url.host
        if calls is not None:
            calls.append(host)
        if host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": []}})
        if host == "api.openalex.org":
            return httpx.Response(200, json={"results": []})
        if host == "export.arxiv.org" and arxiv_feed is not None:
            return httpx.Response(200, text=arxiv_feed)
        return httpx.Response(429)

    return handler


async def test_a_new_arxiv_preprint_is_found_on_arxiv():
    ra = ReferenceAuditor(transport=httpx.MockTransport(_indexes_without_the_preprint(ARXIV_FEED)))
    c = paper_claim(f'Hammami, H. & Abdulaziz, N. (2026). "{BEEVE}." arXiv preprint.')
    r = await ra.check(c, SessionContext(session_id="s"))
    assert r.status == "supported" and "arXiv" in r.explanation


async def test_a_made_up_preprint_is_still_caught():
    empty = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    ra = ReferenceAuditor(transport=httpx.MockTransport(_indexes_without_the_preprint(empty)))
    c = paper_claim('Lee & Park (2026), "Transformer Models for Honeybee Colony Collapse '
                    'Forecasting", arXiv.')
    r = await ra.check(c, SessionContext(session_id="s"))
    assert r.status == "contradicted" and "arXiv" in r.explanation


async def test_older_journal_papers_do_not_query_arxiv():
    calls: list = []
    ra = ReferenceAuditor(
        transport=httpx.MockTransport(_indexes_without_the_preprint(ARXIV_FEED, calls))
    )
    c = paper_claim('Smith & Jones (2015), "Hive Weight Forecasting with Kalman Filters", '
                    "Journal of Apicultural Research.")
    await ra.check(c, SessionContext(session_id="s"))
    assert "export.arxiv.org" not in calls


# ------------------------------------------------------------------ Part B, B1.2: arXiv IDs, APA
BERT_FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry>
  <id>http://arxiv.org/abs/1810.04805v2</id><published>2018-10-11T00:00:00Z</published>
  <title>{BERT_TITLE}</title>
  <author><name>Jacob Devlin</name></author><author><name>Ming-Wei Chang</name></author>
</entry></feed>"""
NO_ENTRY = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'


def _arxiv_by_id(feed: str):
    def handler(req):
        if req.url.host == "export.arxiv.org" and "id_list" in req.url.params:
            return httpx.Response(200, text=feed)
        if req.url.host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": []}})
        if req.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(429)

    return handler


BERT_SENTENCE = "The BERT preprint appeared on arXiv in 2018 (arXiv:1810.04805)"


async def test_an_arxiv_id_is_looked_up_directly():
    ra = ReferenceAuditor(transport=httpx.MockTransport(_arxiv_by_id(BERT_FEED)))
    r = await ra.check(_paper(BERT_SENTENCE, BERT_SENTENCE), SessionContext(session_id="s"))
    assert r.status == "supported" and "1810.04805" in r.explanation


async def test_a_made_up_arxiv_id_is_caught():
    ra = ReferenceAuditor(transport=httpx.MockTransport(_arxiv_by_id(NO_ENTRY)))
    fake = "The BERT preprint appeared on arXiv in 2018 (arXiv:1810.99999)"
    r = await ra.check(_paper(fake, fake), SessionContext(session_id="s"))
    assert r.status == "contradicted"


async def test_an_arxiv_id_for_a_different_paper_is_amber():
    other = BERT_FEED.replace(BERT_TITLE, "Deep Residual Learning for Image Recognition").replace(
        "Jacob Devlin", "Kaiming He").replace("Ming-Wei Chang", "Xiangyu Zhang")
    ra = ReferenceAuditor(transport=httpx.MockTransport(_arxiv_by_id(other)))
    r = await ra.check(_paper(BERT_SENTENCE, BERT_SENTENCE), SessionContext(session_id="s"))
    assert r.status == "unverified"


async def test_a_sentence_about_a_paper_is_never_called_fake():
    """Without quote marks the whole sentence became the 'title' → red."""
    ra = ReferenceAuditor(transport=httpx.MockTransport(_only_crossref_answers(200)))
    t = "The BERT preprint appeared on arXiv in 2018"
    assert await ra.check(_paper(t, t), SessionContext(session_id="s")) is None


def test_apa_without_quote_marks_keeps_the_real_title():
    from app.detectors.reference_auditor import parse_paper

    q = ("Devlin, J., Chang, M.-W., Lee, K., & Toutanova, K. (2019). BERT: Pre-training of deep "
         "bidirectional transformers for language understanding. In Proceedings of NAACL.")
    ref = parse_paper(_paper(q, q))
    assert ref.quoted and ref.year == 2019 and "Devlin" in ref.surnames
    assert ref.title == ("BERT: Pre-training of deep bidirectional transformers for language "
                         "understanding")


# ------------------------------------------------------------------ demo run: DOI links
JEB_DOI = "10.1242/jeb.068718"
JEB_TITLE = ("A nicotinic acetylcholine receptor agonist affects honey bee sucrose "
             "responsiveness and decreases waggle dancing")
JEB_CITATION = (
    "Eiri, D. M., & Nieh, J. C. (2012). A nicotinic acetylcholine receptor agonist affects "
    "honey bee sucrose responsiveness and decreases waggle dancing. Journal of Experimental "
    f"Biology, 215(12), 2022–2029. https://doi.org/{JEB_DOI}"
)


def _doi_world(registered: dict, handle_known: set = frozenset(), calls: list | None = None):
    def handler(req):
        path = req.url.path
        if calls is not None:
            calls.append(str(req.url))
        if req.url.host == "api.crossref.org" and path.startswith("/works/"):
            doi = path[len("/works/"):]
            if doi in registered:
                title, family = registered[doi]
                return httpx.Response(200, json={"message": {
                    "DOI": doi, "title": [title], "issued": {"date-parts": [[2012]]},
                    "author": [{"family": family}]}})
            return httpx.Response(404)
        if req.url.host == "doi.org" and path.startswith("/api/handles/"):
            doi = path[len("/api/handles/"):]
            code = 1 if doi in handle_known else 100
            return httpx.Response(200 if code == 1 else 404, json={"responseCode": code})
        return httpx.Response(403)  # publishers block bots

    return handler


def _url_claim_in(reply: str, url: str):
    s = SessionContext(session_id="s")
    from app.models import ChatMessage
    s.messages.append(ChatMessage(message_id="m", role="assistant", position=1, text=reply))
    c = Claim(claim_id="c", message_id="m", quote=url, normalized=f"The URL {url} exists.",
              type="url", risk="high")
    return c, s


async def test_a_real_doi_is_green_even_when_the_publisher_blocks_bots():
    ra = ReferenceAuditor(transport=httpx.MockTransport(
        _doi_world({JEB_DOI: (JEB_TITLE, "Eiri")})))
    c, s = _url_claim_in(JEB_CITATION, f"https://doi.org/{JEB_DOI}")
    r = await ra.check(c, s)
    assert r.status == "supported" and "waggle" in r.explanation


async def test_a_made_up_doi_is_red():
    ra = ReferenceAuditor(transport=httpx.MockTransport(_doi_world({})))
    fake = JEB_CITATION.replace(JEB_DOI, "10.1242/jeb.999999")
    c, s = _url_claim_in(fake, "https://doi.org/10.1242/jeb.999999")
    r = await ra.check(c, s)
    assert r.status == "contradicted" and "doesn't exist" in r.explanation


async def test_a_real_doi_attached_to_a_different_paper_is_red():
    other = ("Deep residual learning for image recognition", "He")
    ra = ReferenceAuditor(transport=httpx.MockTransport(_doi_world({JEB_DOI: other})))
    c, s = _url_claim_in(JEB_CITATION, f"https://doi.org/{JEB_DOI}")
    r = await ra.check(c, s)
    assert r.status == "contradicted" and "different paper" in r.explanation


async def test_a_non_crossref_doi_known_to_doi_org_is_green():
    arxiv_doi = "10.48550/arXiv.1810.04805"
    ra = ReferenceAuditor(transport=httpx.MockTransport(_doi_world({}, {arxiv_doi})))
    c, s = _url_claim_in(f"BERT preprint: https://doi.org/{arxiv_doi}",
                         f"https://doi.org/{arxiv_doi}")
    r = await ra.check(c, s)
    assert r.status == "supported"


async def test_a_bare_doi_link_with_no_citation_text_is_just_checked_for_existence():
    ra = ReferenceAuditor(transport=httpx.MockTransport(
        _doi_world({JEB_DOI: ("Something unrelated entirely", "Zed")})))
    c, s = _url_claim_in(f"See https://doi.org/{JEB_DOI}", f"https://doi.org/{JEB_DOI}")
    r = await ra.check(c, s)
    assert r.status == "supported"  # too little context to call it a mismatch


async def test_a_normal_link_blocked_by_the_site_is_not_flagged():
    ra = ReferenceAuditor(transport=httpx.MockTransport(lambda req: httpx.Response(403)))
    c, s = _url_claim_in("Docs: https://example-journal.org/article/42",
                         "https://example-journal.org/article/42")
    assert await ra.check(c, s) is None


# ------------------------------------------------------------------ demo run: rules vs facts
async def test_a_true_fact_explaining_why_a_rule_cant_be_met_is_not_red(ledger):
    """User: 'retries … using only requests.get'. Claude: 'Automatic retries need a Session with
    an HTTPAdapter' (true). It went red as 'you told Claude to use only requests.get'."""
    from app.detectors.memory_consistency import MemoryConsistency

    await ledger.add_card("local", "constraint", "Must use only requests.get, no other methods.",
                          "requests method", "user_message")
    text = "Automatic retries need a Session with an HTTPAdapter"

    async def judge(system, user, **kw):
        return {"verdict": "contradicts", "memory_index": 0, "confidence": 0.9,
                "explanation": "You told Claude to use only requests.get."}

    async def no_cards(*a, **k):
        return {"cards": []}

    mc = MemoryConsistency(judge=judge, extract_judge=no_cards,
                           gate_judge=_world_gate(text, "requests retries"))
    c = Claim(claim_id="c1", message_id="a1", quote=text, normalized=text, type="fact",
              risk="high")
    assert await mc.check(c, SessionContext(session_id="s")) is None


async def test_code_that_breaks_a_user_rule_is_still_red(ledger):
    from app.detectors.memory_consistency import MemoryConsistency

    await ledger.add_card("local", "constraint", "API rate limit is 100 requests per minute.",
                          "api rate limit", "user_message")
    code = "import time\nfor c in cities:\n    fetch(c)  # 1000 requests per minute\n"

    async def no_cards(*a, **k):
        return {"cards": []}

    async def gate(system, user, **kw):
        return {"standalone": code, "kind": "advice", "subject": "request loop", "question": None}

    mc = MemoryConsistency(extract_judge=no_cards, gate_judge=gate)
    c = Claim(claim_id="c1", message_id="a1", quote=code, normalized=code, type="code",
              risk="high", code=code)
    r = await mc.check(c, SessionContext(session_id="s"))
    assert r is not None and r.status == "contradicted"


def test_extraction_prompt_skips_one_off_answer_format_rules():
    from app.learning.memory import EXTRACT_SYSTEM

    assert "using only requests.get" in EXTRACT_SYSTEM and "no tools" in EXTRACT_SYSTEM


async def test_a_true_price_fact_does_not_break_the_users_budget(ledger):
    from app.detectors.memory_consistency import MemoryConsistency

    await ledger.add_card("local", "constraint", "Total trip budget is $500.", "trip budget",
                          "user_message")
    text = "Round-trip flights from New York to Tokyo usually cost about $1,000."

    async def judge(system, user, **kw):
        return {"verdict": "unrelated"}

    async def no_cards(*a, **k):
        return {"cards": []}

    mc = MemoryConsistency(judge=judge, extract_judge=no_cards,
                           gate_judge=_world_gate(text, "flight prices"))
    c = Claim(claim_id="c1", message_id="a1", quote=text, normalized=text, type="number",
              risk="high")
    assert await mc.check(c, SessionContext(session_id="s")) is None


# ------------------------------------------------------------------ demo run: glued link text
async def test_a_doi_glued_to_the_next_sentence_is_still_checked_as_the_real_doi():
    """The companion read '…605–612.https://doi.org/10.2307/1414040This was the first…' and six
    real DOIs went red as 404s."""
    ra = ReferenceAuditor(transport=httpx.MockTransport(
        _doi_world({JEB_DOI: (JEB_TITLE, "Eiri")})))
    glued = f"https://doi.org/{JEB_DOI}This"
    c, s = _url_claim_in(JEB_CITATION.replace(JEB_DOI, JEB_DOI + "This was the study"), glued)
    r = await ra.check(c, s)
    assert r.status == "supported"


async def test_a_normal_link_glued_to_the_next_word_is_retried_without_it():
    def handler(req):
        return httpx.Response(200 if req.url.path == "/docs/intro" else 404, text="ok")

    ra = ReferenceAuditor(transport=httpx.MockTransport(handler))
    c, s = _url_claim_in("See https://example.org/docs/introThen continue.",
                         "https://example.org/docs/introThen")
    r = await ra.check(c, s)
    assert r.status == "supported"


async def test_a_real_camelcase_link_is_left_alone():
    def handler(req):
        return httpx.Response(200 if req.url.path == "/MayankKotla" else 404, text="ok")

    ra = ReferenceAuditor(transport=httpx.MockTransport(handler))
    c, s = _url_claim_in("https://github.com/MayankKotla", "https://github.com/MayankKotla")
    r = await ra.check(c, s)
    assert r.status == "supported"


# ------------------------------------------------------------------ demo switch
def _asked(text_user: str, text_reply: str):
    from app.models import ChatMessage

    s = SessionContext(session_id="s")
    s.messages += [ChatMessage(message_id="u", role="user", position=0, text=text_user),
                   ChatMessage(message_id="a", role="assistant", position=1, text=text_reply)]
    c = Claim(claim_id="c", message_id="a", quote="Water boils at 50°C at sea level.",
              normalized="Water boils at 50°C at sea level.", type="fact", risk="high")
    return s, c


async def test_requested_lies_are_skipped_by_default(monkeypatch):
    from app.detectors.claim_gate import gate

    monkeypatch.delenv("REIGNS_DEMO_CATCH_REQUESTED", raising=False)
    s, c = _asked("give me 3 false statements", "1. Water boils at 50°C at sea level.")

    async def judge(system, user, **kw):
        return {"standalone": c.normalized, "kind": "not_asserted", "subject": "water",
                "question": None}

    assert (await gate(c, s, judge)).kind == "not_asserted"


async def test_demo_switch_checks_requested_lies(monkeypatch):
    from app.detectors.claim_gate import gate, requested_untrue

    monkeypatch.setenv("REIGNS_DEMO_CATCH_REQUESTED", "1")
    s, c = _asked("give me 3 false statements", "1. Water boils at 50°C at sea level.")

    async def judge(system, user, **kw):
        return {"standalone": c.normalized, "kind": "not_asserted", "subject": "water",
                "question": None}

    assert not requested_untrue(s, c)
    assert (await gate(c, s, judge)).kind == "world_fact"


async def test_demo_switch_keeps_rejected_myths_unflagged(monkeypatch):
    from app.detectors.claim_gate import gate

    monkeypatch.setenv("REIGNS_DEMO_CATCH_REQUESTED", "1")
    s, c = _asked("What are common myths about water?",
                  "Some people say water boils at 50°C at sea level, but that's false.")

    async def judge(system, user, **kw):
        return {"standalone": c.normalized, "kind": "not_asserted", "subject": "water",
                "question": None}

    assert (await gate(c, s, judge)).kind == "not_asserted"
