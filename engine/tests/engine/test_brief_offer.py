"""brief.offer plumbing: Role C's Session Brief can offer a context refresh after a calm reply."""
from fastapi.testclient import TestClient

from app import plugins
from app.detectors import session_brief
from app.ledger import Ledger
from app.main import app
from app.models import DetectorResult, Evidence, MessageNew
from app.session import Session

TEXT = "The Eiffel Tower was completed in 1889 for the World's Fair in Paris."
OFFER = {"reason": "long_chat", "headline": "Long chat! Want me to refresh Claude's memory?",
         "action": "Paste a context refresh", "text": "Quick context refresh...", "turns": 2}


class Supported:
    name = "claim_verifier"

    async def check(self, claim, session):
        return DetectorResult(detector="claim_verifier", status="supported", confidence=0.95,
                              evidence=[Evidence(source="Wikipedia", snippet="1889")],
                              explanation="Confirmed.")


async def _reply(monkeypatch, tmp_path, offer, start_heat=0):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("REIGNS_BRIEF_OFFER_TURNS", "2")
    monkeypatch.setattr(plugins, "get_detector",
                        lambda n: Supported() if n == "claim_verifier" else None)
    calls = []

    def fake_offer(ctx):
        calls.append(ctx.session_id)
        return offer

    monkeypatch.setattr(session_brief, "offer", fake_offer)
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    try:
        s = Session("s", ledger)
        s.heat.heat = start_heat
        await s.on_message(MessageNew(message_id="u", role="user", position=0, text="When?"))
        out = await s.on_message(MessageNew(message_id="a", role="assistant", position=1, text=TEXT))
        return [e.type for e in out], out, calls
    finally:
        await ledger.close()


async def test_calm_reply_sends_offer_after_the_bubble(monkeypatch, tmp_path):
    types, out, calls = await _reply(monkeypatch, tmp_path, OFFER)
    assert types == ["verdicts.update", "heat.update", "bubble.content", "brief.offer"]
    assert out[-1].payload == OFFER
    assert calls == ["s"]  # offer() asked exactly once per judged reply


async def test_no_offer_over_a_warning(monkeypatch, tmp_path):
    types, _, calls = await _reply(monkeypatch, tmp_path, OFFER, start_heat=60)  # → level 2
    assert "brief.offer" not in types and calls == []


async def test_offer_none_means_no_envelope(monkeypatch, tmp_path):
    types, _, calls = await _reply(monkeypatch, tmp_path, None)
    assert "brief.offer" not in types and calls == ["s"]


async def test_malformed_offer_is_dropped_not_fatal(monkeypatch, tmp_path):
    types, _, _ = await _reply(monkeypatch, tmp_path, {"reason": "bogus"})
    assert types[-1] == "bubble.content"


def test_debug_brief_for_a_new_session(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(app) as client:
        res = client.get("/debug/brief?session_id=brief-test").json()
    assert res == {"text": "", "turns": 0}
