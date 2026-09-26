"""Tests for the Reference Auditor (FR-C1). Owner: Role C.

All HTTP is mocked with httpx.MockTransport, so these run offline in < 1 s. The fake API
responses copy the real Crossref / Semantic Scholar JSON shapes.

Live test against the real APIs (needs internet):
    REIGNS_LIVE=1 pytest tests/detectors/test_reference_auditor.py -k live -s
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.detectors.reference_auditor import (
    ReferenceAuditor,
    parse_packages,
    parse_paper,
    parse_url,
)
from app.models import Claim, SessionContext

# --------------------------------------------------------------------------- fake API data
VASWANI_CR = {
    "DOI": "10.5555/3295222.3295349",
    "title": ["Attention is all you need"],
    "author": [{"given": "Ashish", "family": "Vaswani"}, {"given": "Noam", "family": "Shazeer"}],
    "issued": {"date-parts": [[2017, 12, 4]]},
}
UNRELATED_CR = [
    {
        "DOI": "10.1000/bees1",
        "title": ["Honey bee colony losses in the United States"],
        "author": [{"given": "K.", "family": "Kulhanek"}],
        "issued": {"date-parts": [[2017]]},
    },
    {
        "DOI": "10.1000/ts2",
        "title": ["Temporal Fusion Transformers for interpretable multi-horizon forecasting"],
        "author": [{"given": "Bryan", "family": "Lim"}],
        "issued": {"date-parts": [[2021]]},
    },
]
VASWANI_S2 = {
    "paperId": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
    "title": "Attention is All you Need",
    "year": 2017,
    "authors": [
        {"authorId": "1", "name": "Ashish Vaswani"},
        {"authorId": "2", "name": "Noam M. Shazeer"},
    ],
    "externalIds": {"ArXiv": "1706.03762"},
    "url": "https://www.semanticscholar.org/paper/204e3073",
}
UNRELATED_S2 = {
    "paperId": "x",
    "title": "Deep learning for beehive monitoring: a review",
    "year": 2022,
    "authors": [{"name": "A. Researcher"}],
    "externalIds": {},
    "url": "https://www.semanticscholar.org/paper/x",
}


VASWANI_OA = {
    "id": "https://openalex.org/W2963403868",
    "display_name": "Attention Is All You Need",
    "publication_year": 2017,
    "doi": None,
    "authorships": [
        {"author": {"display_name": "Ashish Vaswani"}},
        {"author": {"display_name": "Noam Shazeer"}},
    ],
}
UNRELATED_OA = {
    "id": "https://openalex.org/W1",
    "display_name": "Machine learning approaches to honey bee health monitoring",
    "publication_year": 2021,
    "doi": "https://doi.org/10.1000/oa1",
    "authorships": [{"author": {"display_name": "B. Author"}}],
}


def api_handler(request: httpx.Request) -> httpx.Response:
    """Pretend to be Crossref, Semantic Scholar, PyPI, npm and a couple of websites."""
    host, path = request.url.host, request.url.path
    if host == "api.crossref.org":
        q = request.url.params.get("query.bibliographic", "").lower()
        items = [VASWANI_CR] if "attention is all you need" in q else UNRELATED_CR
        return httpx.Response(200, json={"status": "ok", "message": {"items": items}})
    if host == "api.openalex.org":
        q = (request.url.params.get("search", "") + request.url.params.get("filter", "")).lower()
        works = [VASWANI_OA] if "attention is all you need" in q else [UNRELATED_OA]
        return httpx.Response(200, json={"results": works})
    if host == "api.semanticscholar.org":
        q = request.url.params.get("query", "").lower()
        data = [VASWANI_S2] if "attention is all you need" in q else [UNRELATED_S2]
        return httpx.Response(200, json={"total": len(data), "offset": 0, "data": data})
    if host == "pypi.org":
        return httpx.Response(200 if path in ("/pypi/requests/json", "/pypi/fastapi/json") else 404)
    if host == "registry.npmjs.org":
        raw = request.url.raw_path.decode()
        return httpx.Response(200 if raw in ("/lodash", "/@types%2Fnode") else 404)
    if host == "docs.python.org":
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><p>The <b>asyncio</b> module is a library to write concurrent code "
            "using the async/await syntax.</p></html>",
        )
    if host == "example.com":
        return httpx.Response(404)
    if host == "no-such-site-xyz.dev":
        raise httpx.ConnectError("[Errno -2] Name or service not known", request=request)
    return httpx.Response(500)


def claim(type_: str, quote: str, normalized: str = "", context: str = "") -> Claim:
    return Claim(
        claim_id="c1",
        message_id="m1",
        quote=quote,
        normalized=normalized or quote,
        type=type_,
        risk="high",
        context=context,
    )


@pytest.fixture
def auditor() -> ReferenceAuditor:
    return ReferenceAuditor(transport=httpx.MockTransport(api_handler))


@pytest.fixture
def session() -> SessionContext:
    return SessionContext(session_id="t")


# --------------------------------------------------------------------------- parsing
def test_parse_paper_from_fixture_shape():
    ref = parse_paper(
        claim(
            "paper",
            'Moreau, Tanaka & Silva (2021), "HiveFormer: Attention-Based Acoustic Monitoring of Beehives"',
        )
    )
    assert ref.title == "HiveFormer: Attention-Based Acoustic Monitoring of Beehives"
    assert ref.surnames == ["Moreau", "Tanaka", "Silva"]
    assert ref.year == 2021


def test_parse_paper_et_al_and_normalized_fallback():
    ref = parse_paper(
        claim(
            "paper",
            "Okafor et al. (2023)",
            "The paper 'Predicting Colony Collapse Disorder' exists.",
        )
    )
    assert ref.title == "Predicting Colony Collapse Disorder"
    assert ref.surnames == ["Okafor"]
    assert ref.year == 2023


def test_parse_url_and_packages():
    assert parse_url(claim("url", "See https://docs.python.org/3/library/asyncio.html.")) == (
        "https://docs.python.org/3/library/asyncio.html"
    )
    assert parse_packages(claim("package", "pip install fastapi-ratelimiter==0.3 requests")) == (
        ["fastapi-ratelimiter", "requests"],
        "pypi",
    )
    assert parse_packages(claim("package", "pip install -U httpx to fetch pages")) == (
        ["httpx"],
        "pypi",
    )
    assert parse_packages(claim("package", "Run `npm i @types/node@20`")) == (
        ["@types/node"],
        "npm",
    )


# --------------------------------------------------------------------------- fixture scenario
async def test_fake_citation_scenario_matches_expected(load_scenario, auditor, session):
    sc = load_scenario("fake_citation")
    expected = sc["expected"]["detector_results"]
    for raw in sc["expected"]["claims"]:
        c = Claim(**raw)
        want = [r for r in expected.get(c.claim_id, []) if r["detector"] == "reference_auditor"]
        if not want:
            continue
        got = await auditor.check(c, session)
        assert got.status == want[0]["status"], (c.quote, got.explanation)
        assert got.evidence, "every verdict must carry evidence"


async def test_fake_paper_evidence_names_both_sources(auditor, session):
    c = claim(
        "paper", 'Lee & Park (2022), "Transformer Models for Honeybee Colony Collapse Forecasting"'
    )
    r = await auditor.check(c, session)
    assert r.status == "contradicted" and r.confidence >= 0.9
    assert {e.source for e in r.evidence} == {"Crossref", "Semantic Scholar", "OpenAlex"}


async def test_real_title_wrong_authors_is_only_unverified(auditor, session):
    c = claim("paper", 'Smith & Jones (2017), "Attention Is All You Need"')
    r = await auditor.check(c, session)
    assert r.status == "unverified"  # mis-cited, not invented → amber, never red (§8.4)
    assert "authors" in r.explanation


async def test_real_title_wrong_year(auditor, session):
    """Citing a year AFTER the paper appeared (2021 for a 2017 paper) → mis-citation, amber."""
    r = await auditor.check(
        claim("paper", 'Vaswani et al. (2021), "Attention Is All You Need"'), session
    )
    assert r.status == "unverified" and "year" in r.explanation


async def test_famous_paper_buried_by_relevance_is_found_by_citations(session):
    """Live run 3: OpenAlex relevance top-5 had only 'Attention is all you need in <X>'
    follow-ups; Crossref only a 2025 re-registration; S2 rate-limited → was amber."""
    follow_up = {
        "id": "W9",
        "display_name": "Attention Is All You Need in Speech Separation",
        "publication_year": 2021,
        "doi": None,
        "authorships": [{"author": {"display_name": "Cem Subakan"}}],
    }
    rereg = {**VASWANI_CR, "issued": {"date-parts": [[2025, 1, 1]]}}

    def handler(req):
        host = req.url.host
        if host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": [rereg]}})
        if host == "api.openalex.org":
            by_citations = req.url.params.get("sort") == "cited_by_count:desc"
            return httpx.Response(
                200, json={"results": [VASWANI_OA if by_citations else follow_up]}
            )
        return httpx.Response(429)

    import app.detectors.reference_auditor as ra

    async def no_sleep(s): ...

    real_sleep, ra.asyncio.sleep = ra.asyncio.sleep, no_sleep
    try:
        r = await ReferenceAuditor(transport=httpx.MockTransport(handler)).check(
            claim("paper", 'Vaswani et al. (2017), "Attention Is All You Need"'), session
        )
    finally:
        ra.asyncio.sleep = real_sleep
    assert r.status == "supported" and r.confidence >= 0.95
    assert r.evidence[0].source == "OpenAlex"


async def test_rereg_only_with_two_sources_answering_is_supported(session):
    """Even if OpenAlex answers with nothing useful, a title+author match whose records are all
    later than the citation is the real paper."""
    rereg = {**VASWANI_CR, "issued": {"date-parts": [[2025, 1, 1]]}}

    def handler(req):
        if req.url.host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": [rereg]}})
        if req.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": [UNRELATED_OA]})
        return httpx.Response(503)

    r = await ReferenceAuditor(transport=httpx.MockTransport(handler)).check(
        claim("paper", 'Vaswani et al. (2017), "Attention Is All You Need"'), session
    )
    assert r.status == "supported" and "2025" in r.explanation


async def test_both_apis_down_is_error_not_contradicted(session):
    down = ReferenceAuditor(transport=httpx.MockTransport(lambda req: httpx.Response(503)))
    r = await down.check(claim("paper", 'Lee (2022), "Some Paper That May Exist"'), session)
    assert r.status == "error"  # never flag a paper as fake when we couldn't look it up


async def test_one_api_down_still_decides(session):
    def handler(req):
        if req.url.host == "api.semanticscholar.org":
            return httpx.Response(429)
        return api_handler(req)

    r = await ReferenceAuditor(transport=httpx.MockTransport(handler)).check(
        claim(
            "paper",
            'Lee & Park (2022), "Transformer Models for Honeybee Colony Collapse Forecasting"',
        ),
        session,
    )
    assert r.status == "contradicted" and r.confidence < 0.95  # fewer sources → less sure


async def test_lookups_are_cached(session):
    calls = []

    def counting(req):
        calls.append(req.url.host)
        return api_handler(req)

    a = ReferenceAuditor(transport=httpx.MockTransport(counting))
    c = claim("paper", 'Vaswani et al. (2017), "Attention Is All You Need"')
    await a.check(c, session)
    await a.check(c, session)
    assert len(calls) == 4  # Crossref + S2 + 2× OpenAlex once; 2nd check served from cache


# --------------------------------------------------------------------------- URLs
async def test_url_404_is_contradicted(auditor, session):
    r = await auditor.check(claim("url", "Docs: https://example.com/api/v9/limits"), session)
    assert r.status == "contradicted" and "404" in r.explanation


async def test_url_dns_failure_is_contradicted(auditor, session):
    r = await auditor.check(claim("url", "https://no-such-site-xyz.dev/paper"), session)
    assert r.status == "contradicted" and "doesn't exist" in r.explanation


async def test_url_with_quote_found_on_page(auditor, session):
    c = claim(
        "url",
        'https://docs.python.org/3/library/asyncio.html says "a library to write '
        'concurrent code using the async/await syntax"',
    )
    r = await auditor.check(c, session)
    assert r.status == "supported"


async def test_url_with_quote_missing_from_page(auditor, session):
    c = claim(
        "url",
        'https://docs.python.org/3/library/asyncio.html says "asyncio was removed '
        'in Python 3.12"',
    )
    r = await auditor.check(c, session)
    assert r.status == "unverified"


async def test_url_server_error_is_not_contradicted(auditor, session):
    r = await auditor.check(claim("url", "https://flaky.org/page"), session)
    assert r.status == "unverified"


async def test_localhost_url_is_never_fetched(session):
    def explode(req):
        raise AssertionError("must not fetch private hosts")

    a = ReferenceAuditor(transport=httpx.MockTransport(explode))
    r = await a.check(claim("url", "http://127.0.0.1:8765/debug/status"), session)
    assert r.status == "unverified"


# --------------------------------------------------------------------------- packages
async def test_fake_pypi_package(auditor, session):
    r = await auditor.check(claim("package", "pip install fastapi-ratelimiter"), session)
    assert r.status == "contradicted" and "PyPI" in r.explanation


async def test_real_pypi_package(auditor, session):
    r = await auditor.check(claim("package", "Install it with `pip install requests`."), session)
    assert r.status == "supported"


async def test_npm_scoped_package(auditor, session):
    r = await auditor.check(claim("package", "npm install @types/node"), session)
    assert r.status == "supported"


async def test_fake_npm_package(auditor, session):
    r = await auditor.check(
        claim(
            "package",
            "Use the `react-super-hooks-pro` package",
            context="How do I do this in JavaScript?",
        ),
        session,
    )
    assert r.status == "contradicted" and "npm" in r.explanation


async def test_non_reference_claim_is_ignored(auditor, session):
    r = await auditor.check(claim("fact", "The Eiffel Tower is in Paris."), session)
    assert r.status == "unverified" and r.confidence == 0.0


# --------------------------------------------------------------------------- live (opt-in)
@pytest.mark.skipif(
    os.environ.get("REIGNS_LIVE") != "1", reason="set REIGNS_LIVE=1 to hit real APIs"
)
async def test_live_fixture_scenario(load_scenario):
    sc = load_scenario("fake_citation")
    live, session = ReferenceAuditor(), SessionContext(session_id="live")
    for raw in sc["expected"]["claims"]:
        r = await live.check(Claim(**raw), session)
        print(f"\n{r.status:13} {r.confidence:.2f} {r.latency_ms:5}ms  {raw['quote'][:70]}")
        print(f"              {r.explanation}")
        for e in r.evidence:
            print(f"              [{e.source}] {e.snippet}")


# --------------------------------------------------------------------------- Arnov's live run
# 1) near-miss threshold: a fake title about the same topic must NOT be a near-miss.
async def test_topic_neighbour_is_not_a_near_miss(session):
    mus = {
        "DOI": "10.1000/mus",
        "title": ["MUS-Tracker: An IoT Based System in Controlling and Monitoring of Beehives"],
        "author": [{"family": "Nguyen"}],
        "issued": {"date-parts": [[2020]]},
    }

    def handler(req):
        if req.url.host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": [mus]}})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"title": mus["title"][0], "year": 2020, "authors": [{"name": "T. Nguyen"}]}
                ]
            },
        )

    c = claim(
        "paper",
        'Moreau, Tanaka & Silva (2021), "HiveFormer: Attention-Based Acoustic '
        'Monitoring of Beehives"',
    )
    r = await ReferenceAuditor(transport=httpx.MockTransport(handler)).check(c, session)
    assert r.status == "contradicted", r.explanation  # red, not amber


async def test_real_near_miss_is_still_amber(session):
    """Same paper, words shuffled/garbled (spelling 0.82, words 0.83) → amber, not red.

    We might be wrong about a garbled citation of a real paper, so it only gets a question mark.
    """
    resnet = {
        "DOI": "10.1109/CVPR.2016.90",
        "title": ["Deep residual learning for image recognition"],
        "author": [{"family": "He"}],
        "issued": {"date-parts": [[2016]]},
    }

    def handler(req):
        if req.url.host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": [resnet]}})
        return httpx.Response(200, json={"data": []})

    c = claim("paper", 'He et al. (2016), "Residual Learning for Deep Image Recognition Models"')
    r = await ReferenceAuditor(transport=httpx.MockTransport(handler)).check(c, session)
    assert r.status == "unverified", r.explanation


# 2) rate limits
async def test_429_is_retried_once_after_retry_after(session, monkeypatch):
    import app.detectors.reference_auditor as ra

    waits: list[float] = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr(ra.asyncio, "sleep", fake_sleep)
    hits = {"crossref": 0}

    def handler(req):
        if req.url.host == "api.crossref.org":
            hits["crossref"] += 1
            if hits["crossref"] == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
        return api_handler(req)

    c = claim("paper", 'Vaswani et al. (2017), "Attention Is All You Need"')
    r = await ReferenceAuditor(transport=httpx.MockTransport(handler)).check(c, session)
    assert r.status == "supported"
    assert hits["crossref"] == 2
    assert waits == [2.0]  # Retry-After 7 s capped at 2 s


async def test_429_twice_falls_back_to_other_source(session, monkeypatch):
    import app.detectors.reference_auditor as ra

    async def no_sleep(s): ...

    monkeypatch.setattr(ra.asyncio, "sleep", no_sleep)

    def handler(req):
        if req.url.host == "api.semanticscholar.org":
            return httpx.Response(429)
        return api_handler(req)

    c = claim(
        "paper",
        'Okafor et al. (2023), "Predicting Colony Collapse Disorder with Temporal '
        'Fusion Transformers"',
    )
    r = await ReferenceAuditor(transport=httpx.MockTransport(handler)).check(c, session)
    assert r.status == "contradicted"  # Crossref alone still decides → red, not skipped


async def test_calls_to_each_api_are_serialized(load_scenario, session):
    """4 papers checked in parallel (like the engine does) → never 2 Crossref calls at once."""
    import asyncio

    in_flight = {"api.crossref.org": 0, "api.semanticscholar.org": 0, "api.openalex.org": 0}
    peak = dict(in_flight)

    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            host = request.url.host
            in_flight[host] += 1
            peak[host] = max(peak[host], in_flight[host])
            await asyncio.sleep(0.01)
            in_flight[host] -= 1
            return api_handler(request)

    a = ReferenceAuditor(transport=SlowTransport())
    claims = [Claim(**c) for c in load_scenario("fake_citation")["expected"]["claims"]][:4]
    results = await asyncio.gather(
        *(a.check(c, SessionContext(session_id=str(i))) for i, c in enumerate(claims))
    )
    assert peak == {"api.crossref.org": 1, "api.semanticscholar.org": 1, "api.openalex.org": 1}
    assert [r.status for r in results].count("contradicted") == 3
    assert [r.status for r in results].count("supported") == 1  # → 3 red + 1 green


# 3) mailto
async def test_crossref_mailto_is_sent(session, monkeypatch):
    monkeypatch.setenv("CROSSREF_MAILTO", "team@reigns.dev")
    seen = {}

    def handler(req):
        if req.url.host == "api.crossref.org":
            seen["param"] = req.url.params.get("mailto")
            seen["ua"] = req.headers.get("user-agent")
        return api_handler(req)

    await ReferenceAuditor(transport=httpx.MockTransport(handler)).check(
        claim("paper", 'Vaswani et al. (2017), "Attention Is All You Need"'), session
    )
    assert seen["param"] == "team@reigns.dev"
    assert "mailto:team@reigns.dev" in seen["ua"]


# --------------------------------------------------------------------------- live run 2
async def test_later_record_year_with_matching_authors_is_supported(session):
    """Live: S2 rate-limited, Crossref's only record was a 2025 re-registration of the 2017
    paper, OpenAlex also down → used to be amber. Same title + authors, later record → green."""
    rereg = {**VASWANI_CR, "issued": {"date-parts": [[2025, 1, 1]]}}

    def handler(req):
        if req.url.host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": [rereg]}})
        return httpx.Response(429)

    import app.detectors.reference_auditor as ra

    async def no_sleep(s): ...

    ra_sleep, ra.asyncio.sleep = ra.asyncio.sleep, no_sleep
    try:
        r = await ReferenceAuditor(transport=httpx.MockTransport(handler)).check(
            claim("paper", 'Vaswani et al. (2017), "Attention Is All You Need"'), session
        )
    finally:
        ra.asyncio.sleep = ra_sleep
    assert r.status == "supported" and "2025" in r.explanation


async def test_citing_a_later_year_than_the_paper_is_still_flagged(session):
    """The leniency is one-way: citing 2030 for a 2017 paper is still a mis-citation."""
    r = await ReferenceAuditor(transport=httpx.MockTransport(api_handler)).check(
        claim("paper", 'Vaswani et al. (2030), "Attention Is All You Need"'), session
    )
    assert r.status == "unverified"


async def test_openalex_alone_can_confirm_a_paper(session):
    def handler(req):
        if req.url.host == "api.openalex.org":
            return api_handler(req)
        return httpx.Response(503)

    r = await ReferenceAuditor(transport=httpx.MockTransport(handler)).check(
        claim("paper", 'Vaswani et al. (2017), "Attention Is All You Need"'), session
    )
    assert r.status == "supported" and r.evidence[0].source == "OpenAlex"


# --------------------------------------------------------------------------- long-chat fixes
async def test_unidentifiable_package_claim_is_silent(auditor, session):
    """Long-chat replay: "pandas works well here" → was amber "Couldn't tell which package"."""
    assert await auditor.check(claim("package", "pandas works well here"), session) is None


async def test_library_named_in_prose_is_looked_up(auditor, session):
    r = await auditor.check(
        claim("package", "The requests library's Session reuses connections."), session
    )
    assert r.status == "supported"
    r = await auditor.check(claim("package", "Use the fastapi-ratelimiter-pro package."), session)
    assert r.status == "contradicted"
