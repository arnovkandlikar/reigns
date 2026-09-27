"""Per-chat heat: flipping between Claude chats restores each chat's panic rating."""
import asyncio

import aiosqlite

from app import plugins
from app.heat import HeatState
from app.ledger import Ledger
from app.models import DetectorResult, Evidence, MessageNew, SessionStart
from app.session import Session, envelope

WRONG = "The Eiffel Tower was completed in 1899 for the World's Fair in Paris."


class Contradicts:
    name = "claim_verifier"

    async def check(self, claim, session):
        return DetectorResult(
            detector="claim_verifier", status="contradicted", confidence=0.95,
            evidence=[Evidence(source="Wikipedia", snippet="completed in 1889")],
            explanation="It was completed in 1889, not 1899.",
        )


async def settle(s: Session) -> None:
    for _ in range(5):
        if s._background:
            await asyncio.gather(*list(s._background), return_exceptions=True)
        await asyncio.sleep(0)


async def start(s: Session, chat_key=None):
    p = SessionStart(chat_key=chat_key)
    return await s.handle(envelope("session.start", s.sid, p), p)


def test_restore_shows_saved_level_immediately_and_pauses_decay():
    h = HeatState()
    h.restore(75, now=1000.0)
    assert (h.heat, h.displayed_level, h.tick(1000.0)) == (75, 3, 3)
    assert h.target_level(1029.0) == 3 and h.heat == 75  # no decay for time spent away


async def test_flipping_chats_restores_each_chats_heat(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(plugins, "get_detector", lambda n: Contradicts() if n == "claim_verifier" else None)
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    try:
        # Chat 1 (new chat, no key yet): a wrong answer heats it up.
        s1 = Session("s1", ledger)
        await start(s1)
        await s1.on_message(MessageNew(message_id="first-msg-of-chat-1", role="user", position=0,
                                       text="When was the Eiffel Tower finished?"))
        out = await s1.on_message(MessageNew(message_id="a1", role="assistant", position=1, text=WRONG))
        heat1 = next(e for e in out if e.type == "heat.update").payload["heat"]
        assert heat1 > 0 and s1.chat_key == "first-msg-of-chat-1"
        await settle(s1)

        # Flip to chat 2 (never seen): calm.
        s2 = Session("s2", ledger)
        [hu] = await start(s2, "chat-2")
        assert hu.payload["heat"] == 0

        # Flip back to chat 1: same heat, level and red count, bubble back, right away.
        s3 = Session("s3", ledger)
        out = await start(s3, "first-msg-of-chat-1")
        hu, bubble = out[0].payload, out[1]
        assert hu["heat"] == heat1 and hu["level"] >= 1 and hu["red_count"] == 1
        assert bubble.type == "bubble.content" and bubble.payload["level"] >= 1

        # Every message row logs the heat (panic rating) at that point.
        async with aiosqlite.connect(ledger.path) as db:
            rows = await (await db.execute(
                "SELECT message_id, heat FROM messages WHERE session_id='s1' ORDER BY position"
            )).fetchall()
        assert rows == [("first-msg-of-chat-1", 0), ("a1", heat1)]
    finally:
        await ledger.close()


async def test_old_database_gets_heat_column(tmp_path):
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE messages (session_id TEXT, message_id TEXT, role TEXT, "
                         "position INTEGER, text TEXT, ts TEXT, PRIMARY KEY (session_id, message_id))")
        await db.commit()
    ledger = Ledger(path)
    await ledger.open()
    await ledger.message("s", MessageNew(message_id="m", role="user", position=0, text="hi"), heat=40)
    await ledger.close()
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute("SELECT heat FROM messages")).fetchall() == [(40,)]


async def test_session_start_language(tmp_path):
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    try:
        s = Session("s", ledger)
        p = SessionStart(language="es")
        await s.handle(envelope("session.start", s.sid, p), p)
        assert s.ctx.language == "es" and s.ctx.character == "charlie"
        s3 = Session("s3", ledger)
        p3 = SessionStart(character="Marley")
        await s3.handle(envelope("session.start", s3.sid, p3), p3)
        assert s3.ctx.character == "marley"
        s2 = Session("s2", ledger)
        p2 = SessionStart()
        await s2.handle(envelope("session.start", s2.sid, p2), p2)
        assert s2.ctx.language == "en"
    finally:
        await ledger.close()
