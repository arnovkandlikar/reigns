"""REIGN's own pasted prompts (fresh-start hand-off, Fix it, context refresh) are not treated as
a source document or as user pushback (live-test bug: bogus not_in_source flags)."""
from app import extraction
from app.learning import memory
from app.ledger import Ledger
from app.models import MessageNew
from app.session import Session

HANDOFF = ("Start a fresh chat with this handoff: Recheck the following before answering. "
           + "The user is writing a literature review on honeybee colony collapse. " * 40)
ARTICLE = "Honeybee colonies have declined in several regions over the past decade. " * 40


def fake_reign_authored(session, text):
    return text.startswith("Start a fresh chat with this handoff")


async def _session(tmp_path):
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    s = Session("s", ledger)
    await s.on_message(MessageNew(message_id="u0", role="user", position=0, text="Help me?"))
    await s.on_message(MessageNew(message_id="a1", role="assistant", position=1, text="Sure."))
    return s, ledger


async def test_handoff_is_not_a_source_doc_or_pushback(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(memory, "reign_authored", fake_reign_authored, raising=False)
    assert len(HANDOFF) > extraction.SOURCE_DOC_MIN_CHARS
    s, ledger = await _session(tmp_path)
    try:
        s.pending_pushback = True
        await s.on_message(MessageNew(message_id="u2", role="user", position=2, text=HANDOFF))
        assert s.ctx.source_docs == {} and s.pending_pushback is False
    finally:
        await ledger.close()


async def test_normal_long_article_is_still_a_source_doc(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(memory, "reign_authored", fake_reign_authored, raising=False)
    s, ledger = await _session(tmp_path)
    try:
        await s.on_message(MessageNew(message_id="u2", role="user", position=2, text=ARTICLE))
        assert len(s.ctx.source_docs) == 1
    finally:
        await ledger.close()


async def test_missing_or_crashing_check_changes_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def boom(session, text):
        raise RuntimeError("bad")

    monkeypatch.setattr(memory, "reign_authored", boom, raising=False)
    s, ledger = await _session(tmp_path)
    try:
        await s.on_message(MessageNew(message_id="u2", role="user", position=2, text=HANDOFF))
        assert len(s.ctx.source_docs) == 1  # old behaviour, no crash
    finally:
        await ledger.close()
