"""Session Brief: a short running summary of the chat. Owner: Role C.

Why this exists (Assurant's long-chat feedback)
-----------------------------------------------
Long chats break two things:

1. REIGN's own context. The Claim Gate reads only the last few messages to resolve references,
   so "what's the max size it can store?" at turn 30 can't be resolved when the database was
   chosen at turn 3. The brief is handed to the gate as "EARLIER IN THIS CHAT", so references
   to old context still resolve.
2. Claude's context. In long chats Claude forgets what the user told it (budget, versions,
   rules). REIGN can't edit Claude's context window, but it can put the brief in Claude's
   message box (the same ComposerInserter the "Fix it" prompt uses). The user presses Enter
   and the key context is restated where Claude pays the most attention. This is the
   /compact-style refresh Assurant suggested.

What goes in the brief (and what never does)
--------------------------------------------
- goal:       what the user is trying to get done
- user_said:  facts, numbers, constraints and preferences the USER stated
- decisions:  what the user and Claude settled on or built
- open:       unresolved questions / next steps
Never included: general-knowledge claims Claude made (trivia belongs to the detectors), and
anything REIGN flagged red/amber. A brief that restated a hallucination would teach Claude
to repeat it.

How it runs
-----------
Incremental and in the background: the fast model merges the NEW messages into the previous
brief every REFRESH_EVERY assistant turns, or sooner when a lot of text arrived (see
refresh_chars), and again just before an offer is due so the offer carries a fresh brief. `memory.on_verdicts` (already spawned by the engine after each judged reply) calls
`schedule()`, so no engine changes are needed for the gate half. Best-effort everywhere: a
failed update keeps the previous brief and never raises into the engine.

Public API
----------
    schedule(session)            start a background update if one is due (never blocks)
    await refresh(session)       bring the brief up to date now (on-demand "refresh" click)
    current(session)             the latest Brief or None (no waiting)
    gate_context(session)        text block for the Claim Gate prompt ("" if no brief)
    for_claude(session)          first-person text for the user to send to Claude
    offer(session)               payload for the pet's "want a context refresh?" bubble, or None

When the pet offers a refresh: by LENGTH, not turn count
---------------------------------------------------------
What pushes early context out of Claude's attention is how much text the chat holds, not how
many turns it has: 20 one-line answers are tiny, 5 answers full of code are huge. So the pet
offers a refresh each time REIGNS_BRIEF_OFFER_CHARS characters (default 40,000, about 10k
tokens) have been added since the last offer, counting BOTH sides, because pasted documents
and Claude's replies fill the context window alike. Every offer carries a newly updated brief.

Env: REIGNS_SESSION_BRIEF=0 turns it off. REIGNS_BRIEF_OFFER_CHARS sets the offer threshold
(use ~3000 for a demo).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from app.detectors.base import one_object
from app.llm import LLMError, complete_json, fast_model_name, llm_available
from app.models import SessionContext

log = logging.getLogger("reigns.detectors.session_brief")

STATE_KEY = "session_brief:state"

START_AFTER_TURNS = 4  # the gate already sees the last 8 messages (= 4 turns) itself
REFRESH_EVERY = 4  # assistant turns between background updates
DEFAULT_OFFER_CHARS = 40_000  # ≈ 10k tokens of new conversation between offers
MIN_OFFER_CHARS = 500
PRE_OFFER_FRACTION = 0.75  # past this share of the threshold, update every turn (fresh offer)
CHUNK_MESSAGES = 16  # messages per model call when catching up on a long backlog
MESSAGE_CHARS = 1200  # per message, after removing pasted content
MAX_ITEMS = 8  # per list
MAX_ITEM_CHARS = 200
FORGOT_COOLDOWN_TURNS = 5  # don't nag twice in a row about forgotten context

BRIEF_SYSTEM = """You keep a short running brief of a chat between a user and an AI assistant,
so the chat's key context survives when the conversation gets long.

You get the PREVIOUS BRIEF (may be empty) and the NEW MESSAGES since it was written. Return the
updated brief as JSON:
{"goal": "...", "user_said": ["..."], "decisions": ["..."], "open": ["..."]}

- goal: one sentence, what the user is trying to get done overall. Update it if it changed.
- user_said: facts, numbers, constraints and preferences the USER stated about themselves,
  their project, data, setup, budget or rules ("On Python 3.8", "API allows 100 requests per
  minute", "Budget is $3,000, not counting flights"). Keep exact numbers, versions and names.
  If the user changed a value, keep only the latest one.
- decisions: what the user and assistant settled on or built so far (tools, approach,
  structure, plan). Only things the user accepted or went along with, not every suggestion.
- open: unresolved questions or next steps. Empty list if none.

Rules:
- Merge with the previous brief: keep what is still true, update what changed, drop what was
  replaced or abandoned.
- NEVER include general-knowledge facts the assistant stated (dates, heights, history,
  trivia). Only the user's own context and what was decided.
- NEVER include anything listed under FLAGGED. It may be wrong.
- Each item at most 20 words, plain and specific. At most 8 items per list; merge or drop the
  least important.
- Write in the language the user writes in."""


@dataclass
class Brief:
    goal: str = ""
    user_said: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    open: list[str] = field(default_factory=list)
    upto: int = -1  # position of the last message this brief covers
    turns: int = 0  # assistant turns covered

    def empty(self) -> bool:
        return not (self.goal or self.user_said or self.decisions or self.open)

    def as_json(self) -> str:
        return json.dumps(
            {
                "goal": self.goal,
                "user_said": self.user_said,
                "decisions": self.decisions,
                "open": self.open,
            },
            ensure_ascii=False,
        )


@dataclass
class _State:
    brief: Brief | None = None
    task: asyncio.Future | None = None
    offered_upto: int = -1  # position of the last message when the long-chat offer was made
    forgot_turn: int | None = None  # "Claude forgot what you told it" offer


# ------------------------------------------------------------------------------ helpers
def enabled() -> bool:
    return os.environ.get("REIGNS_SESSION_BRIEF", "1") != "0" and llm_available()


def offer_after_chars() -> int:
    """Characters of new conversation (both sides) between context-refresh offers."""
    try:
        value = int(os.environ.get("REIGNS_BRIEF_OFFER_CHARS", str(DEFAULT_OFFER_CHARS)))
    except ValueError:
        return DEFAULT_OFFER_CHARS
    return max(MIN_OFFER_CHARS, value)


def refresh_chars() -> int:
    """Update the brief early when this much text arrived since the last update."""
    return max(MIN_OFFER_CHARS, offer_after_chars() // 4)


def chars_since(session: SessionContext, position: int) -> int:
    """Characters in all messages after `position` (both user and assistant)."""
    return sum(len(m.text) for m in session.messages if m.position > position)


def _latest_position(session: SessionContext) -> int:
    return max((m.position for m in session.messages), default=-1)


def _state(session: SessionContext) -> _State:
    st = session.cache.get(STATE_KEY)
    if not isinstance(st, _State):
        st = _State()
        session.cache[STATE_KEY] = st
    return st


def assistant_turns(session: SessionContext) -> int:
    return sum(1 for m in session.messages if m.role == "assistant")


def current(session: SessionContext) -> Brief | None:
    b = _state(session).brief
    return b if b and not b.empty() else None


def due(session: SessionContext) -> bool:
    turns = assistant_turns(session)
    if turns == 0:
        return False
    st = _state(session)
    b = st.brief
    upto = b.upto if b else -1
    if upto >= _latest_position(session):
        return False  # nothing new
    new_chars = chars_since(session, upto)
    if b is None:
        # Short chats fit in the gate's own window; start once the chat outgrows it, or as
        # soon as a lot of text arrived (a pasted document on turn 1).
        return turns >= START_AFTER_TURNS or new_chars >= refresh_chars()
    if turns - b.turns >= REFRESH_EVERY or new_chars >= refresh_chars():
        return True
    # Close to an offer: keep the brief current every turn so the offer isn't stale.
    return chars_since(session, st.offered_upto) >= PRE_OFFER_FRACTION * offer_after_chars()


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = " ".join(str(item).split())[:MAX_ITEM_CHARS]
        if text and text not in out:
            out.append(text)
    return out[:MAX_ITEMS]


def _authored(text: str) -> str:
    """The part the user actually wrote: pasted articles/code/quotes are summarized away."""
    try:
        from app.learning.memory import split_authored  # local: keep import graph light

        authored, attachments = split_authored(text)
    except Exception:  # noqa: BLE001
        return text[:MESSAGE_CHARS]
    authored = authored.strip()
    notes = []
    if len(text.strip()) - len(authored) > 200:
        notes.append("pasted content omitted")
    if attachments:
        notes.append("attached: " + ", ".join(attachments[:5]))
    note = f" [{'; '.join(notes)}]" if notes else ""
    return (authored[:MESSAGE_CHARS] + note).strip()


def _reign_prompt(session: SessionContext, text: str) -> bool:
    try:
        from app.learning.memory import reign_authored

        return reign_authored(session, text)
    except Exception:  # noqa: BLE001
        return False


def _flagged(session: SessionContext, positions: set[int]) -> list[str]:
    """Quotes REIGN marked red/amber in these messages: never let them into the brief."""
    pos_of = {m.message_id: m.position for m in session.messages}
    out = []
    for cid, v in session.verdicts.items():
        c = session.claims.get(cid)
        if c is None or pos_of.get(c.message_id) not in positions:
            continue
        if v.final in ("red", "amber"):
            out.append(v.quote[:200])
    return out


# ------------------------------------------------------------------------------ updating
async def _update(session: SessionContext, judge) -> Brief | None:
    st = _state(session)
    prev = st.brief or Brief()
    new = sorted((m for m in session.messages if m.position > prev.upto), key=lambda m: m.position)
    if not new:
        return st.brief
    brief = prev
    for i in range(0, len(new), CHUNK_MESSAGES):
        chunk = new[i : i + CHUNK_MESSAGES]
        flagged = _flagged(session, {m.position for m in chunk})
        lines = []
        for m in chunk:
            if m.role == "user" and _reign_prompt(session, m.text):
                text = "[a REIGN correction prompt: not the user's own words, ignore it]"
            else:
                text = _authored(m.text) if m.role == "user" else m.text[:MESSAGE_CHARS]
            lines.append(f"{m.role.upper()}: {text}")
        user = (
            f"PREVIOUS BRIEF:\n{brief.as_json() if not brief.empty() else '(empty)'}\n\n"
            f"FLAGGED (do not include):\n"
            + ("\n".join(f"- {q}" for q in flagged) if flagged else "(none)")
            + "\n\nNEW MESSAGES:\n"
            + "\n".join(lines)
        )
        data = one_object(
            await judge(BRIEF_SYSTEM, user, max_tokens=700, model=fast_model_name())
        )
        if data is None:
            raise LLMError("brief: model returned no object")
        brief = Brief(
            goal=" ".join(str(data.get("goal") or brief.goal).split())[:300],
            user_said=_clean_list(data.get("user_said")),
            decisions=_clean_list(data.get("decisions")),
            open=_clean_list(data.get("open")),
            upto=chunk[-1].position,
            turns=sum(
                1
                for m in session.messages
                if m.role == "assistant" and m.position <= chunk[-1].position
            ),
        )
        st.brief = brief  # keep partial progress if a later chunk fails
    return brief


async def _run(session: SessionContext, judge) -> Brief | None:
    try:
        return await _update(session, judge)
    except Exception as exc:  # noqa: BLE001 (best-effort: keep the previous brief)
        log.warning("session brief update failed for %s: %r", session.session_id, exc)
        return _state(session).brief


def schedule(session: SessionContext, judge: Any = None) -> asyncio.Future | None:
    """Start a background update if one is due. Never blocks, never raises."""
    try:
        if judge is None and not enabled():
            return None
        st = _state(session)
        if st.task is not None and not st.task.done():
            return st.task  # one update at a time
        if not due(session):
            return None
        st.task = asyncio.ensure_future(_run(session, judge or complete_json))
        return st.task
    except Exception as exc:  # noqa: BLE001
        log.warning("session brief schedule failed: %r", exc)
        return None


async def refresh(session: SessionContext, judge: Any = None) -> Brief | None:
    """Bring the brief up to the latest message now (for an on-demand "refresh" click)."""
    st = _state(session)
    if st.task is not None and not st.task.done():
        await asyncio.shield(st.task)
    if judge is None and not enabled():
        return current(session)
    if assistant_turns(session) == 0:
        return current(session)
    latest = max((m.position for m in session.messages), default=-1)
    if st.brief is None or st.brief.upto < latest:
        st.task = asyncio.ensure_future(_run(session, judge or complete_json))
        await asyncio.shield(st.task)
    return current(session)


# ------------------------------------------------------------------------------ outputs
_TEXT = {
    "en": {
        "intro": "Quick context refresh before we continue. Please keep this in mind:",
        "goal": "Goal",
        "user_said": "What I've told you",
        "decisions": "What we've decided",
        "open": "Still open",
        "outro": "If anything earlier in this chat conflicts with this summary, go with the summary.",
        "long_chat": "Long chat! Want me to refresh Claude's memory of what matters?",
        "forgot": "Claude seems to have forgotten something you told it. Refresh its memory?",
        "action": "Paste a context refresh",
    },
    "es": {
        "intro": "Un repaso rápido del contexto antes de seguir. Tenlo en cuenta, por favor:",
        "goal": "Objetivo",
        "user_said": "Lo que te he dicho",
        "decisions": "Lo que hemos decidido",
        "open": "Pendiente",
        "outro": "Si algo anterior en este chat contradice este resumen, sigue el resumen.",
        "long_chat": "¡Chat largo! ¿Quieres que le refresque a Claude lo importante?",
        "forgot": "Parece que Claude olvidó algo que le dijiste. ¿Le refresco la memoria?",
        "action": "Pegar un repaso del contexto",
    },
}


def _t(session: SessionContext) -> dict[str, str]:
    return _TEXT.get(getattr(session, "language", "en"), _TEXT["en"])


def gate_context(session: SessionContext) -> str:
    """Compact block for the Claim Gate prompt. Empty until the chat outgrows the gate window."""
    b = current(session)
    if b is None:
        return ""
    parts = []
    if b.goal:
        parts.append(f"Goal: {b.goal}")
    if b.user_said:
        parts.append("User said: " + "; ".join(b.user_said))
    if b.decisions:
        parts.append("Decided: " + "; ".join(b.decisions))
    return "\n".join(parts)


def for_claude(session: SessionContext, brief: Brief | None = None) -> str:
    """First-person text the USER sends to Claude (they press Enter; REIGN never does)."""
    b = brief or current(session)
    if b is None:
        return ""
    t = _t(session)
    lines = [t["intro"], ""]
    if b.goal:
        lines += [f"{t['goal']}: {b.goal}", ""]
    for key in ("user_said", "decisions", "open"):
        items = getattr(b, key)
        if items:
            lines.append(f"{t[key]}:")
            lines += [f"- {x}" for x in items]
            lines.append("")
    lines.append(t["outro"])
    return "\n".join(lines).strip()


def _forgot_in_latest_reply(session: SessionContext) -> bool:
    """Memory check caught Claude contradicting what the user told it, in the latest reply."""
    latest = max(
        (m for m in session.messages if m.role == "assistant"),
        key=lambda m: m.position,
        default=None,
    )
    if latest is None:
        return False
    for cid, v in session.verdicts.items():
        c = session.claims.get(cid)
        if c is None or c.message_id != latest.message_id:
            continue
        for r in v.detector_results:
            if r.detector == "memory_consistency" and r.status == "contradicted":
                return True
    return False


def offer(session: SessionContext) -> dict[str, Any] | None:
    """Should the pet offer a context refresh right now? Returns the bubble payload or None.

    Offers each time REIGNS_BRIEF_OFFER_CHARS characters of new conversation (both sides) have
    built up since the last offer, or right away when the memory check caught Claude
    forgetting something the user said. Marks the offer as made, so call it once per judged
    reply. If the threshold is crossed before a brief exists, the offer waits for the next
    reply (the brief updates in the background in the meantime).
    """
    try:
        b = current(session)
        if b is None:
            return None
        st = _state(session)
        turns = assistant_turns(session)
        t = _t(session)
        reason = None
        if _forgot_in_latest_reply(session) and (
            st.forgot_turn is None or turns - st.forgot_turn >= FORGOT_COOLDOWN_TURNS
        ):
            reason, st.forgot_turn = "forgot", turns
            st.offered_upto = _latest_position(session)  # the user just got a fresh brief
        elif chars_since(session, st.offered_upto) >= offer_after_chars():
            reason, st.offered_upto = "long_chat", _latest_position(session)
        if reason is None:
            return None
        return {
            "reason": reason,
            "headline": t[reason],
            "action": t["action"],
            "text": for_claude(session, b),
            "turns": turns,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("session brief offer failed: %r", exc)
        return None
