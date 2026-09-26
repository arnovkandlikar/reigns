"""The horse reacts as soon as a reply is judged: no 2 s hysteresis wait, and verdicts/heat are
pushed before the (slower) bubble is built."""
import asyncio

from app import plugins
from app.heat import HeatState
from app.ledger import Ledger
from app.models import DetectorResult, Evidence, MessageNew
from app.session import Session


class Contradicts:
    name = "claim_verifier"

    async def check(self, claim, session):
        return DetectorResult(detector="claim_verifier", status="contradicted", confidence=0.95,
                              evidence=[Evidence(source="Wikipedia", snippet="1889")],
                              explanation="It was 1889.")


def test_show_now_skips_hysteresis_but_decay_still_smooths():
    h = HeatState()
    h.add_claims([("a", "red", False), ("b", "red", False), ("c", "red", False)], now=0)
    assert h.show_now(0) == 3 and h.tick(0) == 3
    assert h.tick(0.5) == 3


async def test_verdicts_and_heat_are_sent_before_the_bubble_is_built(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(plugins, "get_detector", lambda n: Contradicts() if n == "claim_verifier" else None)
    sent, order = [], []

    async def slow_bubble(level, ctx):
        order.append(("bubble_start", [e.type for e in sent]))
        await asyncio.sleep(0.05)
        from app.models import BubbleContent
        return BubbleContent(level=level, headline="A date is wrong")

    monkeypatch.setattr(plugins, "build_bubble", slow_bubble)
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    try:
        s = Session("s", ledger)

        async def sink(env):
            sent.append(env)

        s.voice_sink = sink
        await s.on_message(MessageNew(message_id="u", role="user", position=0, text="When?"))
        out = await s.on_message(MessageNew(
            message_id="a", role="assistant", position=1,
            text="The Eiffel Tower was completed in 1899 for the World's Fair in Paris."))
        assert order == [("bubble_start", ["verdicts.update", "heat.update"])]
        assert [e.type for e in out] == ["bubble.content"]
        assert sent[1].payload["level"] >= 1  # already showing the new level, no 2 s wait
    finally:
        await ledger.close()
