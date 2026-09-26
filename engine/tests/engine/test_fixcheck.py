"""FR-D5 fix verification, resolved claims, async Course Correct hook, bandit counts."""
import asyncio
import types

from app import plugins
from app.fixcheck import same_claim, verify_fix
from app.ledger import Ledger
from app.learning import store
from app.models import (
    BubbleContent, Claim, ClaimVerdict, CorrectionRecord, DetectorResult, DriftProfile, Evidence,
    FeedbackDisagree, MessageNew, SessionContext,
)
from app.session import Session

EV = [Evidence(source="Crossref", snippet="none")]


def claim(cid, quote, ctype="paper", norm=None):
    return Claim(claim_id=cid, message_id="m", quote=quote, normalized=norm or quote,
                 type=ctype, risk="high")


def verdict(c, final):
    status = {"red": "contradicted", "amber": "unverified", "green": "supported"}[final]
    return ClaimVerdict(claim_id=c.claim_id, quote=c.quote, type=c.type, risk="high", final=final,
                        detector_results=[DetectorResult(detector="reference_auditor",
                                                         status=status, confidence=0.9,
                                                         evidence=EV, explanation="e")])


FAKE = claim("t1", 'Lee & Park (2022), "Transformer Models for Honeybee Colony Collapse"',
             norm="The paper 'Transformer Models for Honeybee Colony Collapse' exists.")


def test_repeating_a_target_is_not_fixed():
    again = claim("n1", '"Transformer Models for Honeybee Colony Collapse" (Lee, 2022)')
    assert same_claim(again, FAKE)
    fixed, reason = verify_fix([FAKE], [again], [verdict(again, "amber")])
    assert not fixed and "Repeated" in reason


def test_new_red_is_not_fixed_and_clean_reply_is_fixed():
    other = claim("n2", "The Eiffel Tower was completed in 1899.", "fact")
    assert not verify_fix([FAKE], [other], [verdict(other, "red")])[0]
    real = claim("n3", 'Vaswani et al. (2017), "Attention Is All You Need"')
    assert verify_fix([FAKE], [real], [verdict(real, "green")])[0]
    assert verify_fix([FAKE], [], [])[0]  # model just withdrew the claims


def test_same_fact_restated():
    t = claim("t", "x", "fact", norm="The Eiffel Tower was completed in 1899 in Paris.")
    n = claim("n", "y", "fact", norm="Eiffel Tower completed 1899, Paris.")
    assert same_claim(n, t)
    assert not same_claim(claim("n", "z", "fact", norm="Bees make honey from nectar."), t)


async def test_async_course_correct_and_timeout(monkeypatch):
    s = SessionContext(session_id="s")

    async def diagnose(session):
        await asyncio.sleep(0.01)
        return DriftProfile()

    def build(level, profile, session):
        return BubbleContent(level=level, headline="async ok")

    mod = types.SimpleNamespace(diagnose=diagnose, build_bubble=build)
    monkeypatch.setattr(plugins, "_optional_import", lambda name: mod)
    assert (await plugins.build_bubble(1, s)).headline == "async ok"

    async def slow(session):
        await asyncio.sleep(5)

    monkeypatch.setattr(plugins, "COURSE_CORRECT_TIMEOUT_S", 0.05)
    mod.diagnose = slow
    assert (await plugins.build_bubble(1, s)).headline != "async ok"  # fell back, no hang


async def test_scenario_resolves_targets_and_records_variant(load_scenario, tmp_path, monkeypatch):
    """End to end: targets are stored, variant from session.cache is kept, fix resolves them."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class Auditor:
        name = "reference_auditor"

        async def check(self, c, session):
            ok = "Vaswani" in c.quote
            return DetectorResult(detector="reference_auditor",
                                  status="supported" if ok else "contradicted", confidence=0.9,
                                  evidence=EV, explanation="e")

    monkeypatch.setattr(plugins, "get_detector",
                        lambda n: Auditor() if n == "reference_auditor" else None)
    real_build = plugins.build_bubble

    async def tagging_build(level, session):  # simulate Role D's bandit tagging its variant
        b = await real_build(level, session)
        if b.correction:
            session.cache[f"course_correct:variant:{b.correction.correction_id}"] = \
                "fabricated_sources:diagnostic_reset:v2"
        return b

    monkeypatch.setattr(plugins, "build_bubble", tagging_build)
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    s = Session("s", ledger)
    msgs = [m for m in load_scenario("fake_citation")["companion_to_engine"]
            if m["type"] == "message.new"]
    await s.on_message(MessageNew(**msgs[0]["payload"]))
    await s.on_message(MessageNew(**msgs[1]["payload"]))
    corr = s.ctx.corrections[-1]
    assert len(corr.target_claim_ids) == 3
    assert corr.variant_id == "fabricated_sources:diagnostic_reset:v2"
    corr.inserted = True
    out = await s.on_message(MessageNew(**msgs[2]["payload"]))
    await ledger.close()
    assert corr.fixed is True
    assert set(corr.target_claim_ids) <= s.ctx.resolved_claim_ids
    heat = out[1].payload
    assert heat["heat"] == 0 and heat["red_count"] == 0 and heat["recovered"] is True
    assert out[2].payload["correction"] is None  # nothing left to fix


async def test_disagree_still_works_with_resolved(tmp_path):
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    s = Session("s", ledger)
    res = await s.on_disagree(FeedbackDisagree(claim_id="unknown"))
    await ledger.close()
    assert res[0].payload["heat"] == 0


class FakeColl:
    def __init__(self):
        self.docs, self.updates = [], []

    async def insert_one(self, d):
        self.docs.append(d)

    async def update_one(self, flt, upd, upsert=False):
        self.updates.append((flt, upd))


class FakeDB(dict):
    def __missing__(self, k):
        self[k] = FakeColl()
        return self[k]

    def __getattr__(self, k):
        return self[k]


async def test_trial_updates_bandit_counts(monkeypatch):
    db = FakeDB()
    monkeypatch.setenv("MONGODB_URI", "mongodb://fake")
    monkeypatch.setattr(store, "_db", db)
    rec = CorrectionRecord(correction_id="c1", prompt_type="diagnostic_reset", level=3, text="t",
                           fixed=True, variant_id="fabricated_sources:diagnostic_reset:v2")
    await store.record_trial(rec, "claude")
    flt, upd = db["prompt_variants"].updates[0]
    assert flt == {"_id": "fabricated_sources:diagnostic_reset:v2"}
    assert upd["$inc"] == {"alpha": 1}
    assert upd["$setOnInsert"]["failure_type"] == "fabricated_sources"
    rec.fixed = False
    rec.correction_id = "c2"
    await store.record_trial(rec, "claude")
    assert db["prompt_variants"].updates[1][1]["$inc"] == {"beta": 1}
