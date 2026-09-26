"""Registering Role C's memory_consistency detector (routing + None-safety)."""
from app import plugins, triage
from app.aggregate import final_status
from app.ledger import Ledger
from app.models import ChatMessage, Claim, DetectorResult, Evidence, MessageNew
from app.session import Session


def claim(ctype, risk="high"):
    return Claim(claim_id="c", message_id="m", quote="q", normalized="q", type=ctype, risk=risk)


def test_routes():
    assert triage.route(claim("fact")) == ["claim_verifier", "memory_consistency"]
    assert triage.route(claim("number")) == ["claim_verifier", "memory_consistency"]
    assert triage.route(claim("code_api")) == ["code_api_checker", "memory_consistency"]
    assert triage.route(claim("paper")) == ["reference_auditor"]
    assert triage.route(claim("source_summary")) == ["source_faithfulness"]
    assert triage.route(claim("fact", risk="low")) == []
    assert "memory_consistency" in plugins.DETECTOR_NAMES


def test_schema_accepts_new_detector_name():
    DetectorResult(detector="memory_consistency", status="contradicted", confidence=0.9,
                   evidence=[Evidence(source="Earlier in chat", snippet="budget is $500")],
                   explanation="You said your budget is $500.")


async def _run(monkeypatch, tmp_path, memory):
    class CV:
        name = "claim_verifier"

        async def check(self, c, s):
            return DetectorResult(detector="claim_verifier", status="supported", confidence=0.9,
                                  evidence=[Evidence(source="Wiki", snippet="x")], explanation="ok")

    dets = {"claim_verifier": CV()}
    if memory is not None:
        dets["memory_consistency"] = memory
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(plugins, "get_detector", lambda n: dets.get(n))
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    s = Session("s", ledger)
    s.ctx.messages.append(ChatMessage(message_id="u", role="user", text="hi", position=0))
    out = await s.on_message(MessageNew(message_id="a", role="assistant", position=1,
                                        text="The Eiffel Tower was completed in 1889 in Paris."))
    await ledger.close()
    return out[0].payload["claims"]


async def test_missing_or_silent_memory_detector_changes_nothing(monkeypatch, tmp_path):
    class Silent:
        name = "memory_consistency"

        async def check(self, c, s):
            return None  # nothing relevant in memory

    for memory in (None, Silent()):  # file not written yet / nothing to say
        claims = await _run(monkeypatch, tmp_path, memory)
        assert claims and all(c["final"] == "green" for c in claims)
        assert all(r["detector"] == "claim_verifier" for c in claims
                   for r in c["detector_results"])


async def test_memory_contradiction_turns_red(monkeypatch, tmp_path):
    class Clash:
        name = "memory_consistency"

        async def check(self, c, s):
            return DetectorResult(detector="memory_consistency", status="contradicted",
                                  confidence=0.9,
                                  evidence=[Evidence(source="Earlier in chat", snippet="...")],
                                  explanation="Contradicts what you said earlier.")

    claims = await _run(monkeypatch, tmp_path, Clash())
    assert any(c["final"] == "red" for c in claims)
    assert final_status(claim("fact"), [DetectorResult(
        detector="memory_consistency", status="contradicted", confidence=0.9,
        evidence=[Evidence(source="chat", snippet="s")], explanation="e")]) == "red"
