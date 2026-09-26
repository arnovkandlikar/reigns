"""Tests for experience memory (FR-L3). Owner: Role C.

Atlas is faked with a tiny in-memory stand-in for Motor (list/create search index, aggregate),
so these check OUR logic: the vector query we send, the PRD filters (same type, cosine >= 0.75,
confirmed only, top 3), silent fallback, and how the Claim Verifier uses the examples.
Live check against a real cluster: python tests/detectors/experience_live.py
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.detectors import experience as ex
from app.detectors.claim_verifier import ClaimVerifier
from app.models import Claim, SessionContext
from tests.detectors.test_claim_verifier import GOOD_ANSWERS, fake_judge, web_handler


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return list(self.rows)[: length or None]


class FakeCollection:
    def __init__(self, rows=None, indexes=None, fail_aggregate=False, fail_index=False):
        self.rows = rows or []
        self.indexes = list(indexes or [])
        self.created = []
        self.pipelines = []
        self.fail_aggregate = fail_aggregate
        self.fail_index = fail_index

    def list_search_indexes(self, name=None):
        if self.fail_index:
            raise RuntimeError("Atlas Search not available")
        return Cursor([i for i in self.indexes if name is None or i["name"] == name])

    async def create_search_index(self, model):
        self.created.append(model.document)
        self.indexes.append({"name": model.document["name"]})
        return model.document["name"]

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        if self.fail_aggregate:
            raise RuntimeError("index not ready")
        return Cursor(self.rows)


class FakeDB:
    def __init__(self, coll: FakeCollection):
        self.coll = coll

    def __getitem__(self, name):
        assert name == "cases"
        return self.coll


def row(text, final="red", cosine=0.9, confirmed_by="evidence", origin="live"):
    return {
        "normalized": text,
        "final": final,
        "detector": "claim_verifier",
        "evidence_snippet": f"evidence for {text}",
        "confirmed_by": confirmed_by,
        "origin": origin,
        "score": (1 + cosine) / 2,  # how Atlas reports cosine
    }


async def embed_ok(text):
    return [0.1] * 512


@pytest.fixture(autouse=True)
def fresh_index_state(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")  # fake search (MockTransport), never real
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    ex._reset_for_tests()
    yield
    ex._reset_for_tests()


# ------------------------------------------------------------------------------ scoring
def test_atlas_score_is_converted_back_to_cosine():
    assert ex.cosine_from_score(0.875) == pytest.approx(0.75)
    assert ex.cosine_from_score(1.0) == pytest.approx(1.0)
    assert ex.cosine_from_score(0.5) == pytest.approx(0.0)


def test_prd_filters_threshold_confirmed_dedupe_top3():
    rows = [
        row("A too far", cosine=0.74),  # below 0.75
        row("B", cosine=0.80),
        row("C", cosine=0.95, final="green"),
        row("D amber", final="amber"),  # not confirmed red/green
        row("E unconfirmed", confirmed_by="model_guess"),
        row("c", cosine=0.90),  # duplicate of C (case-insensitive)
        row("F", cosine=0.76),
        row("G", cosine=0.77),
    ]
    cases = ex._to_cases(rows, k=3)
    assert [c.text for c in cases] == ["C", "B", "G"]  # sorted by similarity, top 3
    assert cases[0].final == "green" and cases[0].cosine == 0.95


# ------------------------------------------------------------------------------ query
async def test_vector_query_filters_by_claim_type():
    coll = FakeCollection(
        rows=[row("The Eiffel Tower was completed in 1899")], indexes=[{"name": "cases_vector"}]
    )
    cases = await ex.similar_cases(
        "Eiffel Tower completed 1899", "fact", db=FakeDB(coll), embed_fn=embed_ok
    )
    assert len(cases) == 1
    stage = coll.pipelines[0][0]["$vectorSearch"]
    assert stage["index"] == "cases_vector" and stage["path"] == "embedding"
    assert stage["filter"] == {"claim_type": "fact"}
    assert len(stage["queryVector"]) == 512 and stage["numCandidates"] >= stage["limit"]


async def test_index_created_once_when_missing():
    coll = FakeCollection()
    db = FakeDB(coll)
    await ex.similar_cases("x", "fact", db=db, embed_fn=embed_ok)
    await ex.similar_cases("y", "fact", db=db, embed_fn=embed_ok)
    assert len(coll.created) == 1
    created = coll.created[0]
    assert created["name"] == "cases_vector" and created["type"] == "vectorSearch"
    fields = created["definition"]["fields"]
    assert {
        "type": "vector",
        "path": "embedding",
        "numDimensions": 512,
        "similarity": "cosine",
    } in fields
    assert {"type": "filter", "path": "claim_type"} in fields


async def test_existing_index_is_not_recreated():
    coll = FakeCollection(indexes=[{"name": "cases_vector"}])
    await ex.similar_cases("x", "fact", db=FakeDB(coll), embed_fn=embed_ok)
    assert coll.created == []


# ------------------------------------------------------------------------------ fallbacks
async def test_disabled_without_atlas_or_voyage(monkeypatch):
    monkeypatch.delenv("REIGNS_EXPERIENCE", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    assert await ex.similar_cases("x", "fact") == []
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://example")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    assert not ex.enabled()


async def test_env_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://example")
    monkeypatch.setenv("VOYAGE_API_KEY", "k")
    monkeypatch.setenv("REIGNS_EXPERIENCE", "0")
    assert not ex.enabled()


async def test_no_embedding_means_no_query():
    coll = FakeCollection(rows=[row("x")])

    async def no_embed(text):
        return None

    assert await ex.similar_cases("x", "fact", db=FakeDB(coll), embed_fn=no_embed) == []
    assert coll.pipelines == []


async def test_atlas_errors_are_silent():
    coll = FakeCollection(fail_aggregate=True, fail_index=True)
    assert await ex.similar_cases("x", "fact", db=FakeDB(coll), embed_fn=embed_ok) == []


async def test_slow_lookup_times_out(monkeypatch):
    monkeypatch.setattr(ex, "LOOKUP_TIMEOUT_S", 0.05)

    async def slow_embed(text):
        await asyncio.sleep(1)
        return [0.1] * 512

    coll = FakeCollection(rows=[row("x")])
    assert await ex.similar_cases("x", "fact", db=FakeDB(coll), embed_fn=slow_embed) == []


async def test_empty_text_returns_nothing():
    assert (
        await ex.similar_cases("  ", "fact", db=FakeDB(FakeCollection()), embed_fn=embed_ok) == []
    )


# ------------------------------------------------------------------------------ prompt block
def test_examples_block_labels_outcomes_and_says_not_evidence():
    cases = ex._to_cases([row("Eiffel completed 1899"), row("Eiffel is 330 m", "green")], 3)
    block = ex.examples_block(cases)
    assert block.startswith("PAST CONFIRMED CASES")
    assert "NOT evidence" in block
    assert 'turned out WRONG: "Eiffel completed 1899"' in block
    assert 'turned out CORRECT: "Eiffel is 330 m"' in block
    assert ex.examples_block([]) == ""


# ------------------------------------------------------------------------------ in the verifier
def make_verifier(judge, experience_fn):
    return ClaimVerifier(
        transport=httpx.MockTransport(web_handler), judge=judge, experience_fn=experience_fn
    )


def eiffel_claim() -> Claim:
    t = "The Eiffel Tower was completed in 1899."
    return Claim(claim_id="c", message_id="m", quote=t, normalized=t, type="fact", risk="high")


async def test_judge_sees_past_cases_and_verdict_still_needs_todays_evidence():
    judge = fake_judge(GOOD_ANSWERS)
    seen_types = []

    async def past(text, claim_type):
        seen_types.append(claim_type)
        return ex._to_cases([row("The Eiffel Tower was finished in 1899")], 3)

    r = await make_verifier(judge, past).check(eiffel_claim(), SessionContext(session_id="t"))
    assert r.status == "contradicted"
    assert seen_types == ["fact"]
    prompt = judge.calls[0]
    assert "PAST CONFIRMED CASES" in prompt
    assert prompt.index("EVIDENCE SNIPPETS") < prompt.index("PAST CONFIRMED CASES")
    assert all("PAST CONFIRMED" not in e.snippet for e in r.evidence)  # never cited as evidence


async def test_no_past_cases_leaves_prompt_unchanged():
    judge = fake_judge(GOOD_ANSWERS)

    async def none(text, claim_type):
        return []

    await make_verifier(judge, none).check(eiffel_claim(), SessionContext(session_id="t"))
    assert "PAST CONFIRMED CASES" not in judge.calls[0]


async def test_experience_failure_never_breaks_the_verifier():
    judge = fake_judge(GOOD_ANSWERS)

    async def broken(text, claim_type):
        raise RuntimeError("atlas down")

    r = await make_verifier(judge, broken).check(eiffel_claim(), SessionContext(session_id="t"))
    assert r.status == "contradicted"
