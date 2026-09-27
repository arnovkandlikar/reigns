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
