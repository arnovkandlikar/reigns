"""Tests for the Session Brief (long-chat context for the Claim Gate and for Claude). Role C.

The summarizing model is scripted, so these check OUR plumbing: when updates run, that only
new messages are sent, that flagged claims and pasted content are kept out, that the gate
sees the brief, and when the pet offers a refresh.
"""

from __future__ import annotations

import pytest

from app.detectors import session_brief as sb
from app.detectors.claim_gate import gate
from app.models import (
    ChatMessage,
    Claim,
    ClaimVerdict,
    DetectorResult,
    SessionContext,
)


def make_chat(turns: int, language: str = "en") -> SessionContext:
    s = SessionContext(session_id="brief", language=language)
    for i in range(turns):
        s.messages.append(
            ChatMessage(message_id=f"u{i}", role="user", position=2 * i, text=f"user message {i}")
        )
        s.messages.append(
            ChatMessage(
                message_id=f"a{i}",
                role="assistant",
                position=2 * i + 1,
                text=f"assistant reply {i}",
            )
        )
    return s


def add_turn(s: SessionContext, user: str = "more", reply: str = "ok") -> None:
    n = len(s.messages)
    i = n // 2
    s.messages.append(ChatMessage(message_id=f"u{i}", role="user", position=n, text=user))
    s.messages.append(ChatMessage(message_id=f"a{i}", role="assistant", position=n + 1, text=reply))


class Judge:
    """Scripted summarizer: records prompts, returns a fixed brief (or raises)."""

    def __init__(self, reply=None, error: Exception | None = None):
        self.calls: list[str] = []
        self.models: list[str | None] = []
        self.reply = reply or {
            "goal": "Build a weather scraper",
            "user_said": ["On Python 3.8", "API allows 100 requests per minute"],
            "decisions": ["Store results in SQLite"],
            "open": [],
        }
        self.error = error

    async def __call__(self, system, user, max_tokens=700, model=None, **_):
        self.calls.append(user)
        self.models.append(model)
        if self.error:
            raise self.error
        return dict(self.reply)


def flag(
    s: SessionContext,
    message_id: str,
    quote: str,
    final="red",
    detector="claim_verifier",
    status="contradicted",
    cid="f1",
) -> None:
    c = Claim(
        claim_id=cid, message_id=message_id, quote=quote, normalized=quote, type="fact", risk="high"
    )
    s.claims[cid] = c
    s.verdicts[cid] = ClaimVerdict(
        claim_id=cid,
        quote=quote,
        type="fact",
        risk="high",
        final=final,
        detector_results=[
            DetectorResult(detector=detector, status=status, confidence=0.9, explanation="x")
        ],
    )


# ------------------------------------------------------------------------------ scheduling
async def test_no_brief_while_chat_fits_the_gate_window():
    s = make_chat(sb.START_AFTER_TURNS - 1)
    assert not sb.due(s)
    assert sb.schedule(s, judge=Judge()) is None
    assert sb.current(s) is None and sb.gate_context(s) == ""


async def test_first_brief_after_window_then_every_n_turns():
    s = make_chat(sb.START_AFTER_TURNS)
    j = Judge()
    await sb.schedule(s, judge=j)
    assert len(j.calls) == 1 and sb.current(s).turns == sb.START_AFTER_TURNS
    add_turn(s)
    assert sb.schedule(s, judge=j) is None  # not due yet
    for _ in range(sb.REFRESH_EVERY - 1):
        add_turn(s)
    await sb.schedule(s, judge=j)
    assert len(j.calls) == 2


async def test_update_sends_only_new_messages_and_the_previous_brief():
    s = make_chat(4)
    j = Judge()
    await sb.schedule(s, judge=j)
    assert "user message 0" in j.calls[0] and "(empty)" in j.calls[0]
    for k in range(4):
        add_turn(s, user=f"new user {k}", reply=f"new reply {k}")
    await sb.schedule(s, judge=j)
    second = j.calls[1]
    assert "new user 0" in second and "user message 0" not in second
    assert "On Python 3.8" in second  # previous brief is merged, not rebuilt from scratch


async def test_uses_the_fast_model():
    s = make_chat(4)
    j = Judge()
    await sb.schedule(s, judge=j)
    assert j.models == [sb.fast_model_name()]


async def test_long_backlog_is_chunked():
    s = make_chat(20)  # 40 messages, e.g. REIGN attached to an existing long chat
    j = Judge()
    await sb.refresh(s, judge=j)
    assert len(j.calls) == 3  # 16 + 16 + 8
    assert sb.current(s).upto == 39 and sb.current(s).turns == 20


async def test_flagged_claims_are_excluded():
    s = make_chat(4)
    flag(s, "a1", "The Eiffel Tower was completed in 1899")
    flag(s, "a2", "Water boils at 100 C", final="green", cid="g1")
    j = Judge()
    await sb.schedule(s, judge=j)
    flagged_block = j.calls[0].split("FLAGGED (do not include):")[1].split("NEW MESSAGES")[0]
    assert "Eiffel Tower was completed in 1899" in flagged_block
    assert "Water boils" not in flagged_block


async def test_pasted_content_is_not_summarized():
    s = make_chat(4)
    article = ("The council met on Tuesday and discussed the budget at length. " * 20).strip()
    s.messages[0].text = f"Summarize this for me:\n\n{article}"
    j = Judge()
    await sb.schedule(s, judge=j)
    assert "discussed the budget at length. The council" not in j.calls[0]
    assert "pasted content omitted" in j.calls[0]


async def test_failure_keeps_previous_brief_and_never_raises():
    s = make_chat(4)
    await sb.schedule(s, judge=Judge())
    before = sb.current(s)
    for _ in range(4):
        add_turn(s)
    await sb.schedule(s, judge=Judge(error=RuntimeError("api down")))
    assert sb.current(s) is before


async def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("REIGNS_SESSION_BRIEF", "0")
    s = make_chat(8)
    assert sb.schedule(s) is None
    assert await sb.refresh(s) is None


async def test_refresh_catches_up_to_latest_message():
    s = make_chat(4)
    j = Judge()
    await sb.schedule(s, judge=j)
    add_turn(s)
    await sb.refresh(s, judge=j)
    assert len(j.calls) == 2 and sb.current(s).upto == len(s.messages) - 1


async def test_bad_items_are_cleaned():
    s = make_chat(4)
    j = Judge(
        reply={
            "goal": "  g  ",
            "user_said": ["a", "a", "  b  ", ""] + ["x"] * 20,
            "decisions": "not a list",
            "open": None,
        }
    )
    await sb.schedule(s, judge=j)
    b = sb.current(s)
    assert b.goal == "g" and b.user_said == ["a", "b", "x"] and b.decisions == [] and b.open == []


# ------------------------------------------------------------------------------ outputs
async def test_for_claude_is_first_person_and_skips_empty_sections():
    s = make_chat(4)
    await sb.schedule(s, judge=Judge())
    text = sb.for_claude(s)
    assert text.startswith("Quick context refresh")
    assert "What I've told you:\n- On Python 3.8" in text
    assert "What we've decided:\n- Store results in SQLite" in text
    assert "Still open" not in text


async def test_for_claude_in_spanish():
    s = make_chat(4, language="es")
    await sb.schedule(s, judge=Judge())
    assert sb.for_claude(s).startswith("Un repaso rápido")


async def test_gate_sees_the_brief_in_long_chats():
    s = make_chat(4)
    await sb.schedule(s, judge=Judge())
    add_turn(
        s,
        user="What's the max size of a value it can store?",
        reply="By default a single value can be up to 1,000,000,000 bytes.",
    )
    seen = []

    async def gate_judge(system, user, **_):
        seen.append(user)
        return {
            "standalone": "SQLite's default maximum value size is 1,000,000,000 bytes.",
            "kind": "world_fact",
            "subject": "sqlite",
            "question": "q?",
        }

    c = Claim(
        claim_id="c1",
        message_id=s.messages[-1].message_id,
        quote="By default a single value can be up to 1,000,000,000 bytes.",
        normalized="By default a single value can be up to 1,000,000,000 bytes.",
        type="fact",
        risk="high",
    )
    await gate(c, s, judge=gate_judge)
    assert "EARLIER IN THIS CHAT" in seen[0] and "Store results in SQLite" in seen[0]


async def test_gate_prompt_unchanged_in_short_chats():
    s = make_chat(2)
    seen = []

    async def gate_judge(system, user, **_):
        seen.append(user)
        return {"standalone": "x", "kind": "advice", "subject": "x", "question": None}

    c = Claim(
        claim_id="c2",
        message_id="a1",
        quote="assistant reply 1",
        normalized="assistant reply 1",
        type="fact",
        risk="high",
    )
    await gate(c, s, judge=gate_judge)
    assert "EARLIER IN THIS CHAT" not in seen[0]


# ------------------------------------------------------------------------------ offers
async def test_no_offer_without_a_brief():
    assert sb.offer(make_chat(30)) is None


async def test_offer_counts_characters_not_turns(monkeypatch):
    monkeypatch.setenv("REIGNS_BRIEF_OFFER_CHARS", "2000")
    s = make_chat(4)  # short messages: well under 2,000 characters
    await sb.schedule(s, judge=Judge())
    for _ in range(30):
        add_turn(s)  # 30 more turns, but only ~150 characters in total
    assert sb.offer(s) is None  # many turns, little text → no offer
    add_turn(s, reply="x" * 2000)  # one long, code-heavy reply crosses the threshold
    o = sb.offer(s)
    assert o and o["reason"] == "long_chat" and o["text"].startswith("Quick context refresh")


async def test_offer_repeats_every_threshold_of_new_text(monkeypatch):
    monkeypatch.setenv("REIGNS_BRIEF_OFFER_CHARS", "1000")
    s = make_chat(4)
    await sb.schedule(s, judge=Judge())
    add_turn(s, reply="x" * 1000)
    assert sb.offer(s)["reason"] == "long_chat"
    assert sb.offer(s) is None  # not twice for the same text
    add_turn(s, reply="y" * 600)
    assert sb.offer(s) is None  # 600 new characters < 1,000
    add_turn(s, user="z" * 500)  # user text (e.g. a pasted document) counts too
    assert sb.offer(s)["reason"] == "long_chat"


async def test_threshold_crossed_before_a_brief_waits_for_it(monkeypatch):
    monkeypatch.setenv("REIGNS_BRIEF_OFFER_CHARS", "1000")
    s = make_chat(1)
    add_turn(s, user="Summary of my project, long: " + "detail " * 200)
    assert sb.offer(s) is None  # no brief yet: nothing to offer
    await sb.schedule(s, judge=Judge())  # lots of text → the brief starts early (turn 2)
    assert sb.current(s) is not None
    assert sb.offer(s)["reason"] == "long_chat"  # offer arrives on the next reply


async def test_brief_updates_early_when_lots_of_text_arrives(monkeypatch):
    monkeypatch.setenv("REIGNS_BRIEF_OFFER_CHARS", "40000")
    s = make_chat(4)
    j = Judge()
    await sb.schedule(s, judge=j)
    add_turn(s, reply="code " * 2500)  # 12,500 chars in ONE turn ≥ refresh_chars (10,000)
    await sb.schedule(s, judge=j)
    assert len(j.calls) == 2  # updated after 1 turn instead of waiting for 4


async def test_brief_kept_fresh_just_before_an_offer(monkeypatch):
    monkeypatch.setenv("REIGNS_BRIEF_OFFER_CHARS", "4000")
    s = make_chat(4)
    j = Judge()
    await sb.schedule(s, judge=j)
    add_turn(s, reply="a" * 900)  # under refresh_chars (1,000) → normally not due…
    assert sb.schedule(s, judge=j) is None
    add_turn(s, reply="b" * 900)
    add_turn(s, reply="c" * 900)  # …but now ≥ 75% of the offer threshold since the last offer
    await sb.schedule(s, judge=j)
    assert len(j.calls) == 2


def test_offer_threshold_env_parsing(monkeypatch):
    monkeypatch.delenv("REIGNS_BRIEF_OFFER_CHARS", raising=False)
    assert sb.offer_after_chars() == sb.DEFAULT_OFFER_CHARS
    monkeypatch.setenv("REIGNS_BRIEF_OFFER_CHARS", "nonsense")
    assert sb.offer_after_chars() == sb.DEFAULT_OFFER_CHARS
    monkeypatch.setenv("REIGNS_BRIEF_OFFER_CHARS", "10")
    assert sb.offer_after_chars() == sb.MIN_OFFER_CHARS


async def test_offer_when_claude_forgets_what_user_said():
    s = make_chat(4)
    await sb.schedule(s, judge=Judge())
    flag(s, "a3", "Your limit is 1,000 requests per minute", detector="memory_consistency")
    o = sb.offer(s)
    assert o["reason"] == "forgot" and "forgot" in o["headline"]
    add_turn(s)
    flag(s, "a4", "1,000 per minute again", detector="memory_consistency", cid="f2")
    assert sb.offer(s) is None  # cooldown: don't nag every turn


async def test_forgot_offer_resets_the_length_counter(monkeypatch):
    monkeypatch.setenv("REIGNS_BRIEF_OFFER_CHARS", "1000")
    s = make_chat(4)
    await sb.schedule(s, judge=Judge())
    add_turn(s, reply="x" * 1200)
    flag(s, "a4", "Your limit is 1,000 requests per minute", detector="memory_consistency")
    assert sb.offer(s)["reason"] == "forgot"
    add_turn(s)
    assert sb.offer(s) is None  # they just got a brief; no second offer for the same text


async def test_forgot_only_counts_the_latest_reply():
    s = make_chat(4)
    await sb.schedule(s, judge=Judge())
    flag(s, "a0", "old contradiction", detector="memory_consistency")
    assert sb.offer(s) is None


@pytest.mark.parametrize("lang,word", [("en", "Long chat"), ("es", "Chat largo")])
async def test_offer_headline_language(monkeypatch, lang, word):
    monkeypatch.setenv("REIGNS_BRIEF_OFFER_CHARS", "500")
    s = make_chat(4, language=lang)
    await sb.schedule(s, judge=Judge())
    add_turn(s, reply="x" * 500)
    assert word in sb.offer(s)["headline"]
