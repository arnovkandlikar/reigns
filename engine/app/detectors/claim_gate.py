"""Claim Gate — one shared, context-aware look at each claim BEFORE any detector checks it.
Owner: Role C. Used by claim_verifier, consistency_probe and memory_consistency (and memory.py
when it stores verified facts).

Why (root cause of long-chat false alarms, Assurant feedback)
-------------------------------------------------------------
1. Context loss. Extraction only sees the last user message + the reply, so deep into a chat
   claims arrive as fragments: "first released in 2010" (of what?), "It works best when the
   rows are sorted" (what is "it"?). Fragments can't be verified → amber; re-asked out of
   context → the Consistency Probe gets scattered answers → red.
2. "Can't verify" treated as "suspicious". Advice, tips, opinions, and math about the user's
   own setup can never be confirmed by a public source, so checking them only yields
   "unverified". The longer the chat, the more such sentences.
3. Memory matched by wording, not subject. As the ledger grows, "Flask was first released in
   2010" lands next to "Python's first version was released in 1991".

What the gate does (one fast-model call per claim, shared by all detectors, cached)
-----------------------------------------------------------------------------------
Given the claim and a window of the conversation, it returns:
  standalone  the claim rewritten to stand alone — every reference resolved from the
              conversation ("that route" → "the Flask '/' route"), nothing added
  kind        world_fact | user_context | advice | opinion | meta
  subject     the specific thing the claim is about ("flask", "eiffel tower")
  question    for world facts: an open question that a stranger could answer
Detectors then: web-check / probe ONLY world facts, search with the standalone text, and match
memory BY SUBJECT. Cheap heuristics stay as a fast path that skips the call for obvious cases.

Privacy (§14 rule 13): the conversation window goes only to the LLM (as extraction already
does). Search APIs still receive only the claim's standalone text.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from app.detectors import session_brief
from app.detectors.base import looks_like_instruction, normalize_text
from app.llm import LLMError, complete_json, fast_model_name, llm_available
from app.models import Claim, SessionContext

log = logging.getLogger("reigns.detectors.claim_gate")

Kind = Literal["world_fact", "user_context", "advice", "opinion", "meta", "not_asserted", "unknown"]
KINDS = ("world_fact", "user_context", "advice", "opinion", "meta", "not_asserted")

WINDOW_MESSAGES = 8  # earlier messages given as context (enough to resolve references)
WINDOW_CHARS_PER_MESSAGE = 700
REPLY_CHARS = 2500
# Claim types the gate never needs to look at: other detectors handle them structurally.
SKIP_TYPES = {"paper", "url", "package", "code_api", "code", "source_summary"}

GATE_SYSTEM = """You prepare ONE claim from an AI assistant's latest reply for fact-checking.
You get the recent CONVERSATION (for context only), the assistant's LATEST REPLY, and the CLAIM
taken from it. In long chats you also get EARLIER IN THIS CHAT: a summary of older messages
(the user's goal, what they said, what was decided). Use it to resolve references to things
set up long ago; the recent conversation wins if the two disagree.

Return:
- "standalone": the claim rewritten so a stranger understands it WITHOUT the conversation.
  Resolve every reference using the conversation ("it", "that route", "the second option",
  a missing subject like "first released in 2010" → "Flask was first released in 2010").
  Keep the exact meaning; do not add facts, numbers or hedges that aren't implied.
- "kind", exactly one of:
    world_fact    a statement about the world that a public source (encyclopedia, official
                  docs, registry, news) could confirm or deny: dates, numbers, names,
                  definitions, history, science, what a library function/parameter does.
    user_context  about the user's own project, data, code, plan, setup or numbers — including
                  arithmetic on their numbers ("that's about 1,500 cities per run").
    advice        a recommendation, instruction, tip or best practice ("sleep 0.6 s between
                  calls", "it works best when rows are sorted", "write it to S3 instead").
    opinion       a subjective judgement ("Flask is a good fit", "pandas works well here").
    meta          about the conversation itself: summaries, recaps, "as I said", "got it".
    not_asserted  the assistant does NOT claim this is true: content the user ASKED to be
                  false, made up or fictional ("give me 3 false statements", "invent a fake
                  headline", "write a story"), a myth or quote the assistant mentions in order
                  to reject or discuss it ("'Water boils at 50°C' is false", "the myth that the
                  Wall is visible from the Moon"), hypotheticals, role-play and examples.
                  Judge by what the assistant is asserting, given what the user asked for.
                  When a reply lists a false statement and then its correction, the false
                  statement is not_asserted and the correction ("water boils at 100°C at sea
                  level") is a world_fact.
  If a sentence mixes kinds, pick the kind of its MAIN point.
- "subject": the specific thing the claim is about, 1–4 lowercase words ("flask",
  "eiffel tower", "http 429", "pandas interpolate"). Never a pronoun.
- "question": for world_fact only — an open question (who/what/when/how many…) that someone
  who has NOT seen the conversation could answer, without revealing the answer. Else null.

Return {"standalone": "...", "kind": "...", "subject": "...", "question": "..." | null}"""


@dataclass(frozen=True)
class GateResult:
    standalone: str
    kind: Kind
    subject: str
    question: str | None
    source: str  # "llm" | "heuristic" | "skip"

    @property
    def checkable(self) -> bool:
        """Only world facts can be confirmed or denied by public sources."""
        return self.kind == "world_fact"

    def resolved(self, claim: Claim) -> Claim:
        """The claim with its standalone text — what detectors should reason about."""
        if not self.standalone or self.standalone == claim.normalized:
            return claim
        return claim.model_copy(update={"normalized": self.standalone})


# Claims that open with a reference to something earlier ("It returns…", "That second option
# is…", "The latter…"). "The first mayor of Tórshavn…" is NOT a reference — ordinals only count
# when they point at an earlier item ("the second option / one / approach").
_PRONOUN_START = re.compile(
    r"^\s*(?:(?:it|its|it's|this|that|these|those|they|them|their|he|she|his|her|both|"
    r"either|neither)\b|the (?:former|latter|same)\b|(?:the|that) (?:first|second|third|other|"
    r"last|previous) (?:one|option|approach|method|way|version|choice|solution|step)\b)",
    re.IGNORECASE,
)


# "Give me 3 false statements", "make up a fake citation", "write a fictional news story":
# the user asked for untrue content, so the reply's statements aren't claims to fact-check.
# Deliberately narrow: it must be a request (imperative), not "is this citation fake?".
_REQUEST_UNTRUE = re.compile(
    r"^\W*(?:(?:can|could|would) you\s+|please\s+)?"
    r"(?:give|write|make|generate|list|create|invent|tell|come up with|share|produce|draft)\b"
    r"[^.?!\n]{0,80}?\b(?:false|fake|made[- ]up|fictional|fictitious|untrue|incorrect|wrong|"
    r"bogus|imaginary|invented|nonexistent|non-existent|hallucinated)\b",
    re.IGNORECASE,
)


def requested_untrue(session: SessionContext, claim: Claim) -> bool:
    """Did the user's message right before this reply ask for false/made-up content?"""
    user = session.previous(claim.message_id, "user")
    return bool(user and _REQUEST_UNTRUE.search(user.text.strip()[:400]))


def heuristic(claim: Claim, session: SessionContext | None = None) -> GateResult:
    """No-LLM fallback. Conservative: only obvious world facts count as checkable."""
    text = claim.normalized or claim.quote
    if session is not None and requested_untrue(session, claim):
        kind: Kind = "not_asserted"
    elif looks_like_instruction(claim.quote):
        kind = "advice"
    elif _PRONOUN_START.match(claim.quote) and _PRONOUN_START.match(text):
        kind = "unknown"  # can't resolve the reference without the model → don't check it
    else:
        kind = "world_fact"
    return GateResult(text, kind, "", claim.question, "heuristic")


def _enabled() -> bool:
    return os.environ.get("REIGNS_CLAIM_GATE", "1") != "0" and llm_available()


def _window(claim: Claim, session: SessionContext) -> tuple[str, str]:
    reply = session.message(claim.message_id)
    cutoff = reply.position if reply else 10**9
    earlier = sorted(
        (m for m in session.messages if m.position < cutoff), key=lambda m: m.position
    )[-WINDOW_MESSAGES:]
    convo = "\n".join(f"{m.role.upper()}: {m.text[:WINDOW_CHARS_PER_MESSAGE]}" for m in earlier)
    return convo, (reply.text[:REPLY_CHARS] if reply else claim.quote)


async def _classify(claim: Claim, session: SessionContext, judge) -> GateResult:
    convo, reply = _window(claim, session)
    # Long chats: references can point further back than the window ("the max size it can
    # store" when the database was chosen 30 turns ago). The session brief covers that.
    brief = session_brief.gate_context(session)
    user = (
        (f"EARLIER IN THIS CHAT (summary of older messages):\n{brief}\n\n" if brief else "")
        + f"CONVERSATION (earlier messages):\n{convo or '(none)'}\n\n"
        f"LATEST REPLY:\n{reply}\n\nCLAIM: {claim.quote[:600]}"
    )
    if claim.normalized and claim.normalized != claim.quote:
        user += f"\n(extractor's restatement: {claim.normalized[:400]})"
    data = await judge(GATE_SYSTEM, user, max_tokens=300, model=fast_model_name())
    if not isinstance(data, dict):
        raise LLMError("gate returned no object")
    kind = str(data.get("kind", "")).strip().lower()
    standalone = str(data.get("standalone") or "").strip() or claim.normalized or claim.quote
    question = data.get("question")
    return GateResult(
        standalone=standalone[:600],
        kind=kind if kind in KINDS else "unknown",  # type: ignore[arg-type]
        subject=normalize_text(str(data.get("subject") or ""))[:60],
        question=str(question).strip() if kind == "world_fact" and question else None,
        source="llm",
    )


def _remember(session: SessionContext, key: str, result: GateResult) -> GateResult:
    """Record fast-path / heuristic decisions too, so peek() (memory) sees every decision."""
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    fut.set_result(result)
    session.cache.setdefault(key, fut)
    return result


async def gate(claim: Claim, session: SessionContext, judge: Any = None) -> GateResult:
    """Classify `claim` once per session. Concurrent detectors share the same call."""
    key = f"claim_gate:{claim.claim_id}"
    if claim.type in SKIP_TYPES:
        return GateResult(claim.normalized, "world_fact", "", claim.question, "skip")
    if looks_like_instruction(claim.quote):  # fast path: obvious advice, no call needed
        return _remember(
            session, key, GateResult(claim.normalized, "advice", "", None, "heuristic")
        )
    if judge is None and not _enabled():
        return _remember(session, key, heuristic(claim, session))

    task = session.cache.get(key)
    if task is None:
        task = asyncio.ensure_future(_classify(claim, session, judge or complete_json))
        session.cache[key] = task
    try:
        # shield: one detector timing out must not cancel the call the others are awaiting
        return await asyncio.shield(task)
    except (LLMError, TypeError, ValueError) as exc:
        log.warning("claim gate failed for %s, using heuristic: %s", claim.claim_id, exc)
        return heuristic(claim, session)


def peek(session: SessionContext, claim_id: str) -> GateResult | None:
    """The gate's result for a claim if it already finished (used when storing memory)."""
    task = session.cache.get(f"claim_gate:{claim_id}")
    if task is None or not task.done() or task.cancelled() or task.exception():
        return None
    return task.result()
