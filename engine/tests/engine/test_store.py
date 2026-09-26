"""FR-L1 learning store: confirmed-only, anonymized, never raises (uses a fake DB, no network)."""
import pytest

from app.learning import store
from app.models import Claim, ClaimVerdict, CorrectionRecord, DetectorResult, Evidence, SessionContext

EV = [Evidence(source="Crossref", url=None, snippet="No matching work found")]


class FakeCollection:
    def __init__(self, fail=False):
        self.docs, self.fail = [], fail

    async def insert_one(self, doc):
        if self.fail:
            raise ConnectionError("atlas down")
        self.docs.append(doc)


class FakeDB(dict):
    def __missing__(self, key):
        self[key] = FakeCollection()
        return self[key]


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setenv("MONGODB_URI", "mongodb://fake")
    monkeypatch.setattr(store, "_db", fake)
    store._queue.clear()
    return fake


def claim(cid, ctype="paper", text="The paper 'X' exists."):
    return Claim(claim_id=cid, message_id="m", quote="q", normalized=text, type=ctype, risk="high")


def verdict(cid, final, status, evidence=EV, ctype="paper", detector="reference_auditor"):
    return ClaimVerdict(claim_id=cid, quote="q", type=ctype, risk="high", final=final,
                        detector_results=[DetectorResult(detector=detector, status=status,
                                                         confidence=0.9, evidence=evidence,
                                                         explanation="e")])


async def test_only_confirmed_verdicts_become_cases(db):
    claims = [claim("a"), claim("b"), claim("c"), claim("d", "source_summary")]
    verdicts = [
        verdict("a", "red", "contradicted"),                     # evidence → stored
        verdict("b", "amber", "unverified", evidence=[]),        # not confirmed
        verdict("c", "red", "likely_hallucination", evidence=[],
                detector="consistency_probe"),                   # FR-L5: never alone
        verdict("d", "red", "not_in_source", ctype="source_summary",
                detector="source_faithfulness"),                 # privacy: skip
    ]
    await store.record_verdicts(SessionContext(session_id="s"), claims, verdicts)
    docs = db["cases"].docs
    assert len(docs) == 1
    d = docs[0]
    assert d["claim_type"] == "paper" and d["final"] == "red" and d["confirmed_by"] == "evidence"
    assert set(d) >= {"_id", "normalized", "detector", "origin", "evidence_snippet", "created_at"}
    assert "quote" not in d and "text" not in d  # no conversation text


async def test_feedback_and_trial(db):
    await store.record_feedback(verdict("a", "red", "contradicted"))
    assert db["feedback"].docs[0]["detector"] == "reference_auditor"
    rec = CorrectionRecord(correction_id="c1", prompt_type="diagnostic_reset", level=3, text="t")
    await store.record_trial(rec, "claude")  # outcome unknown → nothing
    assert db["prompt_trials"].docs == []
    rec.fixed = True
    await store.record_trial(rec, "claude")
    assert db["prompt_trials"].docs[0]["_id"] == "c1" and db["prompt_trials"].docs[0]["fixed"]


async def test_failures_are_queued_then_flushed(db):
    db["cases"] = FakeCollection(fail=True)
    await store.record_verdicts(SessionContext(session_id="s"), [claim("a")],
                                [verdict("a", "red", "contradicted")])
    assert store.queued() == 1
    db["cases"].fail = False
    await store.record_feedback(verdict("a", "red", "contradicted"))  # next success flushes
    assert store.queued() == 0 and len(db["cases"].docs) == 1


async def test_disabled_is_a_noop(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    await store.record_verdicts(SessionContext(session_id="s"), [claim("a")],
                                [verdict("a", "red", "contradicted")])
    assert await store.ping() is False
