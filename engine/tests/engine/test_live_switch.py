"""Switching language or pet mid-chat (session.update) keeps the same session: the score,
history and already-spoken problems carry over, and the bubble is rebuilt."""
from app import plugins
from app.ledger import Ledger
from app.main import parse_inbound
from app.models import DetectorResult, Evidence, MessageNew, SessionStart, SessionUpdate
from app.session import Session, envelope

WRONG = "The Eiffel Tower was completed in 1899 for the World's Fair in Paris."


class Contradicts:
    name = "claim_verifier"

    async def check(self, claim, session):
        return DetectorResult(detector="claim_verifier", status="contradicted", confidence=0.95,
                              evidence=[Evidence(source="Wikipedia", snippet="1889")],
                              explanation="It was 1889.")


def test_session_update_is_a_valid_inbound_message():
    env, payload = parse_inbound({"type": "session.update", "session_id": "s",
                                  "payload": {"language": "es"}})
    assert isinstance(payload, SessionUpdate) and payload.language == "es"
    assert payload.character is None


async def test_switching_language_and_pet_keeps_the_score(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(plugins, "get_detector",
                        lambda n: Contradicts() if n == "claim_verifier" else None)
    built = []
    real_build = plugins.build_bubble

    async def spy_build(level, ctx):
        built.append(ctx.language)
        return await real_build(level, ctx)

    monkeypatch.setattr(plugins, "build_bubble", spy_build)
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    try:
        s = Session("s", ledger)
        start = SessionStart(chat_key="chat-1")
        await s.handle(envelope("session.start", s.sid, start), start)
        await s.on_message(MessageNew(message_id="u", role="user", position=0, text="When?"))
        out = await s.on_message(MessageNew(message_id="a", role="assistant", position=1, text=WRONG))
        heat = next(e for e in out if e.type == "heat.update").payload["heat"]
        assert heat > 0
        spoken_before = set(s.voice_state.spoken_ids)

        for upd in (SessionUpdate(language="es"), SessionUpdate(character="marley"),
                    SessionUpdate(language="en"), SessionUpdate(language="es", character="charlie")):
            out = await s.handle(envelope("session.update", s.sid, upd), upd)
            hu = out[0].payload
            assert hu["heat"] == heat and hu["red_count"] == 1  # nothing reset
            assert out[1].type == "bubble.content"  # bubble re-sent in the new language

        assert (s.ctx.language, s.ctx.character) == ("es", "charlie")
        assert [m.message_id for m in s.ctx.messages] == ["u", "a"]  # history kept
        assert s.voice_state.spoken_ids == spoken_before  # old problems aren't re-read
        assert built[-4:] == ["es", "es", "en", "es"]
    finally:
        await ledger.close()
