"""G1 latency: the Consistency Probe starts alongside the Claim Verifier, not after it."""
import asyncio
import time

from app import plugins
from app.ledger import Ledger
from app.models import ChatMessage, DetectorResult, Evidence, MessageNew
from app.session import Session

TEXT = "The first mayor of Torshavn was Jogvan Poulsen, who took office in 1866."


def make(cv_status, calls, probe_s=0.3):
    class CV:
        name = "claim_verifier"

        async def check(self, c, s):
            await asyncio.sleep(0.3)
            ev = [Evidence(source="Wiki", snippet="x")] if cv_status != "unverified" else []
            return DetectorResult(detector="claim_verifier", status=cv_status, confidence=0.9,
                                  evidence=ev, explanation="e")

    class Probe:
        name = "consistency_probe"

        async def check(self, c, s):
            calls.append("start")
            await asyncio.sleep(probe_s)
            calls.append("done")
            return DetectorResult(detector="consistency_probe", status="likely_hallucination",
                                  confidence=0.9, evidence=[], explanation="scattered")

    return {"claim_verifier": CV(), "consistency_probe": Probe()}


async def run(monkeypatch, tmp_path, cv_status, probe_s=0.3):
    calls = []
    dets = make(cv_status, calls, probe_s)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(plugins, "get_detector", lambda n: dets.get(n))
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    s = Session("s", ledger)
    s.ctx.messages.append(ChatMessage(message_id="u", role="user", text="mayor?", position=0))
    t0 = time.perf_counter()
    out = await s.on_message(MessageNew(message_id="a", role="assistant", position=1, text=TEXT))
    elapsed = time.perf_counter() - t0
    await asyncio.sleep(0.4)  # let any cancelled task settle
    await ledger.close()
    return out[0].payload["claims"], elapsed, calls


async def test_probe_runs_in_parallel_when_needed(monkeypatch, tmp_path):
    claims, elapsed, calls = await run(monkeypatch, tmp_path, "unverified")
    assert elapsed < 0.55, f"probe waited for the verifier ({elapsed:.2f}s)"  # ~0.3, not 0.6
    assert any(r["detector"] == "consistency_probe" for c in claims for r in c["detector_results"])
    assert any(c["final"] == "red" for c in claims)


async def test_probe_cancelled_when_verifier_has_evidence(monkeypatch, tmp_path):
    claims, elapsed, calls = await run(monkeypatch, tmp_path, "supported", probe_s=0.8)
    assert calls == ["start"]  # started speculatively, then cancelled (never finished)
    assert elapsed < 0.6  # didn't wait for the slow probe
    assert all(r["detector"] != "consistency_probe" for c in claims for r in c["detector_results"])
    assert all(c["final"] == "green" for c in claims)
