"""Truth Ledger wiring: evidence-backed verdicts from a live reply become memory cards."""
import asyncio

from app import plugins
from app.learning import memory
from app.ledger import Ledger
from app.models import DetectorResult, Evidence, MessageNew
from app.session import Session

TEXT = "The Eiffel Tower was completed in 1889 for the World's Fair in Paris."


class SupportedVerifier:
    name = "claim_verifier"

    async def check(self, claim, session):
        return DetectorResult(
            detector="claim_verifier", status="supported", confidence=0.95,
            evidence=[Evidence(source="Wikipedia", url="https://en.wikipedia.org/wiki/Eiffel_Tower",
                               snippet="completed in 1889")],
            explanation="Wikipedia confirms it.",
        )


async def test_supported_verdict_becomes_verified_fact_card(monkeypatch, tmp_path):
    for key in ("VOYAGE_API_KEY", "REIGNS_MEMORY_SYNC", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    db = str(tmp_path / "mem.db")
    monkeypatch.setenv("REIGNS_MEMORY_DB", db)  # get_store() swaps stores whose path differs
    memory.set_store(memory.MemoryStore(db))
    dets = {"claim_verifier": SupportedVerifier()}
    monkeypatch.setattr(plugins, "get_detector", lambda n: dets.get(n))
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    try:
        s = Session("s", ledger)
        await s.on_message(MessageNew(message_id="u", role="user", position=0,
                                      text="When was the Eiffel Tower finished?"))
        out = await s.on_message(MessageNew(message_id="a", role="assistant", position=1, text=TEXT))
        assert any(c["final"] == "green" for c in out[0].payload["claims"])
        for _ in range(5):  # let the fire-and-forget hooks finish
            if s._background:
                await asyncio.gather(*list(s._background), return_exceptions=True)
            await asyncio.sleep(0)
        cards = await memory.list_cards(memory.user_id_for(s.ctx))
        assert [c.kind for c in cards] == ["verified_fact"]
        assert cards[0].source == "claim_verifier"
    finally:
        await ledger.close()
        memory.set_store(None)
