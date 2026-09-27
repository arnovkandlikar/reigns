"""Memory Consistency detector — flags replies that contradict what the USER told the AI.
Owner: Role C. Uses the Truth Ledger (app/learning/memory.py).

The other detectors check replies against the outside world. This one catches drift INSIDE the
conversation: the AI forgetting a constraint ("our API allows 100 requests/min" → code that
sends 500/min) or the user's setup ("I'm on Python 3.8" → code using `match`, a 3.10 feature).

For each claim (routed here by app/triage.py for fact/number/other/code/code_api claims):
  1. Ingest: earlier user messages in this session that the ledger hasn't seen yet are turned
     into memory cards (memory.on_user_message) — once per message, under a per-session lock.
  2. Recall: the 3 user cards (constraints / user facts) most related to the claim.
     Nothing relevant → return None → this detector stays silent and changes nothing.
  3. Check, cheapest first:
       a. numbers  — "100 requests per minute" (limit) vs "500 requests per minute" → violation.
                     No LLM, instant, and hard to argue with.
       b. Python version — a card says "Python 3.8" and the code uses syntax that needs newer
                     Python (match → 3.10, except* → 3.11, walrus → 3.8, …), checked with ast.
       c. judge LLM — for everything else ("must not use external libraries", "deploying on
                     AWS Lambda", …): contradicts / consistent / unrelated, quoting the card.
Result: contradicted (→ red, evidence = the user's own words), uncertain (→ amber),
consistent (→ green only if nothing else objects), or None.
"""

from __future__ import annotations

import ast
import asyncio
import re
from dataclasses import dataclass
from typing import Any

from app.detectors.base import BaseDetector, one_object, snippet
from app.detectors.claim_gate import gate
from app.learning import memory
from app.learning.memory import USER_KINDS, MemoryCard
from app.llm import complete_json
from app.models import Claim, DetectorResult, Evidence, SessionContext

RECALL_K = 3
MIN_CONTRADICT_CONFIDENCE = 0.8  # below → "uncertain" (amber), never red
EVIDENCE_SOURCE = "What you told Claude"

JUDGE_SYSTEM = """You check whether an AI assistant's latest statement or code CONTRADICTS or
VIOLATES the conversation's MEMORY: things the USER told it (their facts and rules) and facts
that were VERIFIED earlier against external sources (Wikipedia, paper databases, …).

You get the assistant's CLAIM (text or code) and numbered MEMORIES, each tagged with its kind:
  constraint / user_fact  → what the user said
  verified_fact           → confirmed true earlier by a source
  correction              → an earlier false claim and what the source said instead
Decide:
- "contradicts": the claim clearly conflicts with a memory — a different value than the user
  stated or than was verified, breaks a user rule, assumes a setup the user doesn't have, or
  repeats a mistake a correction already fixed.
- "consistent": the claim clearly agrees with a memory.
- "unrelated": the memories aren't about the same specific thing as the claim.
Rules:
- Use only the memories, not outside knowledge.
- Same SUBJECT required: a memory about the Eiffel Tower's completion year says nothing about
  its height.
- Rounding or measurement precision is NOT a contradiction (8,849 m vs "about 8,848 m";
  330 m vs 330.5 m). Different years, names or clearly different numbers ARE.
- Leaving out a detail is NOT a contradiction: a claim that states PART of what a memory says
  ("returns a new DataFrame; with inplace=True it modifies the original" vs a memory that also
  adds "and returns None") is consistent.
- Same SCOPE required: a figure for a different route, endpoint, period or version ("Tokyo to
  Shin-Osaka" vs "Tokyo to Kyoto") is unrelated, not a contradiction.
- Explaining that a user's request or rule CAN'T be met, correcting the user, or warning
  them ("requests.get has no retry option; you need a Session") is NOT a contradiction.
- Talking ABOUT something is not violating it ("pandas 2.0 added X" is not a contradiction of
  "user is on pandas 1.5" — but CODE that requires 2.0 for a 1.5 user is).
- "explanation": one short sentence for the user. Start with "You told Claude …" for user
  memories, or "Earlier this was verified: …" for verified facts/corrections.
Return {"verdict": "contradicts|consistent|unrelated", "memory_index": <int or null>,
        "confidence": 0.0-1.0, "explanation": "..."}"""


# =============================================================================================
# a. Numbers with units
# =============================================================================================
@dataclass
class Quantity:
    value: float
    unit: str  # normalized, e.g. "requests/minute", "usd", "gb"
    text: str


_NUM = r"(\$)?\s?(\d[\d,]*(?:\.\d+)?)\s?(k|m)?"
_UNIT_WORDS = {
    "request": "requests",
    "requests": "requests",
    "req": "requests",
    "reqs": "requests",
    "call": "requests",
    "calls": "requests",
    "api call": "requests",
    "api calls": "requests",
    "query": "requests",
    "queries": "requests",
    "message": "messages",
    "messages": "messages",
    "email": "emails",
    "emails": "emails",
    "token": "tokens",
    "tokens": "tokens",
    "user": "users",
    "users": "users",
    "connection": "connections",
    "connections": "connections",
    "gb": "gb",
    "mb": "mb",
    "tb": "tb",
    "ms": "ms",
    "second": "seconds",
    "seconds": "seconds",
    "minute": "minutes",
    "minutes": "minutes",
    "hour": "hours",
    "hours": "hours",
    "day": "days",
    "days": "days",
    "dollar": "usd",
    "dollars": "usd",
    "usd": "usd",
}
_PER = {
    "second": "second",
    "sec": "second",
    "s": "second",
    "minute": "minute",
    "min": "minute",
    "m": "minute",
    "hour": "hour",
    "hr": "hour",
    "h": "hour",
    "day": "day",
    "d": "day",
    "month": "month",
    "mo": "month",
}
_QTY = re.compile(
    # the unit word must not itself be "per"/"a"/… (else "$1,200 per month" loses its "month")
    _NUM + r"\s*(api calls?|(?!(?:per|a|an|each|every)\b)[a-z]+)?\s*"
    r"(?:(?:per|a|an|each|every|/)\s*(second|sec|minute|min|"
    r"hour|hr|day|month|mo|s|m|h|d)\b)?",
    re.IGNORECASE,
)
_UPPER = re.compile(
    r"\b(limit|limited|max|maximum|at most|no more than|up to|only allows?|cap|capped|under|"
    r"below|budget|within|must not exceed|quota)\b",
    re.IGNORECASE,
)
_LOWER = re.compile(r"\b(at least|minimum|min\.?|no less than|or more)\b", re.IGNORECASE)


def quantities(text: str) -> list[Quantity]:
    out = []
    for m in _QTY.finditer(text):
        dollar, num, mult, word, per = m.groups()
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            continue
        value *= {"k": 1e3, "m": 1e6}.get((mult or "").lower(), 1)
        base = "usd" if dollar else _UNIT_WORDS.get((word or "").lower())
        if not base:
            continue  # a bare number ("3", "1.5") is too ambiguous to compare
        unit = f"{base}/{_PER[per.lower()]}" if per else base
        out.append(Quantity(value, unit, m.group(0).strip()))
    return out


def per_minute(q: Quantity) -> Quantity:
    """Put rates on one scale so "2 requests per second" compares with "100 per minute"."""
    factor = {"second": 60, "minute": 1, "hour": 1 / 60, "day": 1 / 1440}
    if "/" in q.unit:
        base, per = q.unit.split("/")
        if per in factor:
            return Quantity(q.value * factor[per], f"{base}/minute", q.text)
    return q


_DURATIONS = {"ms", "seconds", "minutes", "hours", "days"}


def number_violation(card: MemoryCard, claim_text: str) -> str | None:
    """Plain-English violation if the claim exceeds an upper limit (or undercuts a minimum) the
    user set on the same unit. None when there's nothing comparable."""
    if card.kind != "constraint":
        return None
    upper, lower = bool(_UPPER.search(card.text)), bool(_LOWER.search(card.text))
    if not (upper or lower):
        return None
    for cq in map(per_minute, quantities(card.text)):
        if cq.unit in _DURATIONS:
            # A bare duration in a rule is ambiguous: a pacing gap ("0.6 s between calls"), a
            # timeout, a back-off. Comparing it with any other duration ("fall back to 60 s if
            # Retry-After is missing") made a correct reply red in the long-chat QA. Rates
            # (requests/minute), money and sizes stay checked; durations go to the judge.
            continue
        for q in map(per_minute, quantities(claim_text)):
            # A total cap ("budget $500") also bounds a rate ("$1,200 per month").
            same = q.unit == cq.unit or ("/" not in cq.unit and q.unit.split("/")[0] == cq.unit)
            if not same:
                continue
            if upper and q.value > cq.value * 1.0001:
                return f"You told Claude the limit is {cq.text}, but this uses {q.text}."
            if lower and q.value < cq.value * 0.9999:
                return f"You told Claude the minimum is {cq.text}, but this uses {q.text}."
    return None


# =============================================================================================
# b. Python version vs syntax the code uses
# =============================================================================================
_PY_VERSION = re.compile(r"\bpython\s*v?(3)\.(\d{1,2})\b", re.IGNORECASE)


def required_python(code: str) -> tuple[tuple[int, int], str] | None:
    """Newest-syntax feature in the code as ((3, minor), feature name). Parse-only."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    need: tuple[tuple[int, int], str] | None = None

    def bump(v: tuple[int, int], what: str) -> None:
        nonlocal need
        if need is None or v > need[0]:
            need = (v, what)

    for node in ast.walk(tree):
        if type(node).__name__ == "TryStar":
            bump((3, 11), "except* (exception groups)")
        elif isinstance(node, ast.Match):
            bump((3, 10), "match/case statements")
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.BitOr)
            and any(
                isinstance(n, ast.Name)
                and n.id in {"int", "str", "float", "bool", "None", "list", "dict", "bytes"}
                for n in (node.left, node.right)
            )
            and _in_annotation(tree, node)
        ):
            bump((3, 10), "X | Y type unions")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and (node.func.attr in {"removeprefix", "removesuffix"})
        ):
            bump((3, 9), f"str.{node.func.attr}()")
        elif isinstance(node, ast.NamedExpr):
            bump((3, 8), "the := walrus operator")
    return need


_PROPER = re.compile(r"\b[A-Z][\w'’-]*[A-Za-z0-9]")
_NOT_SUBJECT = {
    "the",
    "a",
    "an",
    "it",
    "this",
    "that",
    "wikipedia",
    "earlier",
    "yes",
    "no",
    "in",
    "on",
    "its",
    "their",
    "his",
    "her",
}


def _proper_nouns(text: str) -> set[str]:
    return {w.lower().rstrip("'’s") for w in _PROPER.findall(text)} - _NOT_SUBJECT


def _subjects_match(a: str, b: str) -> bool:
    """ "eiffel tower" ~ "the eiffel tower's height"; "flask" !~ "python"."""
    wa, wb = memory.tokens(a), memory.tokens(b)
    return bool(wa and wb and (wa <= wb or wb <= wa or len(wa & wb) >= 2))


def same_subject(card: MemoryCard, claim: Claim, claim_subject: str = "") -> bool:
    """A verified fact / correction only applies to a claim about the SAME thing.

    Preferred: both sides carry a subject from the Claim Gate (stored on the card when it was
    verified). Fallback for older cards / no LLM: proper nouns must overlap (Flask ≠ Python,
    Eiffel Tower = Eiffel Tower); cards without proper nouns need a shared meaningful word.
    """
    if card.subject and claim_subject:
        return _subjects_match(card.subject, claim_subject)
    claim_text = f"{claim.normalized} {claim.quote}"
    card_names, claim_names = _proper_nouns(card.text), _proper_nouns(claim_text)
    if card_names:
        return bool(card_names & claim_names)
    generic = {"first", "released", "release", "version", "created", "built", "completed",
               "made", "new", "old", "year", "years", "about", "approximately"}
    card_words = {w for w in memory.tokens(card.text) if not w[0].isdigit()} - generic
    return bool(card_words & memory.tokens(claim_text))


def _imported_roots(code: str) -> set[str]:
    """{"requests", "pandas", …} from the code's imports (parse only)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0].lower() for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0].lower())
    return roots


def _in_annotation(tree: ast.AST, target: ast.AST) -> bool:
    for node in ast.walk(tree):
        ann = getattr(node, "annotation", None) or getattr(node, "returns", None)
        if ann is not None and any(n is target for n in ast.walk(ann)):
            return True
    return False


def python_violation(card: MemoryCard, code: str | None) -> str | None:
    if not code or card.kind not in USER_KINDS:
        return None
    m = _PY_VERSION.search(card.text)
    if not m:
        return None
    user_version = (int(m.group(1)), int(m.group(2)))
    need = required_python(code)
    if need and need[0] > user_version:
        (maj, minor), feature = need
        return (
            f"You told Claude you're on Python {user_version[0]}.{user_version[1]}, "
            f"but this code uses {feature}, which needs Python {maj}.{minor}+."
        )
    return None


# =============================================================================================
# The detector
# =============================================================================================
class MemoryConsistency(BaseDetector):
    name = "memory_consistency"

    def __init__(self, judge=None, extract_judge=None, gate_judge=None) -> None:
        self.gate_judge = gate_judge
        self.judge = judge  # injectable for tests; None → app.llm.complete_json
        self.extract_judge = extract_judge

    async def _check(self, claim: Claim, session: SessionContext) -> DetectorResult | None:
        await self._ingest(claim, session)
        uid = memory.user_id_for(session)
        g = await gate(claim, session, self.gate_judge)
        if g.kind == "not_asserted":
            return None  # requested false statements, quoted myths: Claude isn't claiming them
        claim = g.resolved(claim)  # reason about the context-resolved claim, not a fragment
        text = claim.code or f"{claim.quote}\n{claim.normalized}"
        # All kinds: the user's facts/rules AND earlier evidence-backed verdicts. Verified facts
        # and corrections are about the WORLD, so they only apply to world-fact claims about the
        # SAME subject; the user's own rules apply to advice and code too.
        hits = await memory.relevant(uid, text, k=RECALL_K)
        # A statement about the world can't "break" a user's RULE; only code and advice can.
        # QA (demo run): the user asked for retries "using only requests.get", Claude correctly
        # said that's impossible and explained HTTPAdapter, and the true explanation went red as
        # "you told Claude to use only requests.get". Rules apply to code/advice; world facts are
        # checked against the user's FACTS and against verified facts only.
        # Hard numbers ("500 requests per minute" vs a 100/min limit) are still always checked.
        # Only the model gate can tell "flights cost $1,000" (world fact) from "I set it to 500
        # per minute" (Claude's own plan); the keyword fallback keeps every rule in play.
        rules_apply = bool(claim.code) or g.kind != "world_fact" or g.source != "llm"
        cards = [
            c for c, _ in hits
            if c.kind in USER_KINDS
            or (
                c.kind not in USER_KINDS
                and g.kind in ("world_fact", "unknown")
                and same_subject(c, claim, g.subject)
            )
        ]
        if claim.code:
            # Code rarely shares words with "Uses Python 3.8" or "on pandas 1.5", so similarity
            # misses them. Setup cards about Python or an imported library always apply.
            cards += [c for c in await self._setup_cards(uid, claim.code) if c not in cards]
        if not cards:
            return None

        # a/b: deterministic checks first — no LLM, instant.
        for card in cards:
            # A world fact ("flights to Tokyo cost about $1,000") can't break a user's budget;
            # only a plan, advice or code can. Heuristic-gate claims ("unknown") stay checked.
            why = (number_violation(card, text) if rules_apply else None) or python_violation(
                card, claim.code
            )
            if why:
                return self.result("contradicted", 0.93, why, [self._evidence(card)])

        # An earlier VERIFIED fact already says what this claim says → memory has nothing to
        # object to (even if some other card disagrees: then memory disagrees with itself, and
        # the web detectors decide). Checked against the whole ledger, not just the top-3 recall.
        if not claim.code and g.kind in ("world_fact", "unknown"):
            for c in await memory.list_cards(uid):
                if (
                    c.kind == "verified_fact"
                    and not getattr(c, "superseded_by", None)
                    and memory.echoes(c.text, claim.normalized)
                ):
                    return None

        # c: judge. A world fact is judged against facts, never against the user's rules.
        if not rules_apply:
            cards = [c for c in cards if c.kind != "constraint"]
            if not cards:
                return None
        data = await self.cached(
            session, f"judge:{claim.claim_id}", lambda: self._judge(claim, cards)
        )
        return self._to_result(data, cards)

    @staticmethod
    async def _setup_cards(uid: str, code: str) -> list[MemoryCard]:
        roots = _imported_roots(code)
        out = []
        for c in await memory.list_cards(uid):
            low = c.text.lower()
            if c.kind in USER_KINDS and (
                _PY_VERSION.search(c.text)
                or any(re.search(rf"\b{re.escape(r)}\b", low) for r in roots)
                or re.search(
                    r"\b(external|third[- ]party) (libraries|packages|dependencies)\b", low
                )
            ):
                out.append(c)
        return out[:RECALL_K]

    # ------------------------------------------------------------------ ingest
    async def _ingest(self, claim: Claim, session: SessionContext) -> None:
        """Feed earlier user messages to the ledger once each. The per-session lock stops the
        engine's parallel claims from extracting the same message several times."""
        lock = session.cache.setdefault(f"{self.name}:lock", asyncio.Lock())
        target = session.message(claim.message_id)
        cutoff = target.position if target else 10**9
        async with lock:
            for m in sorted(session.messages, key=lambda m: m.position):
                key = f"{self.name}:ingested:{m.message_id}"
                if m.role != "user" or m.position >= cutoff or key in session.cache:
                    continue
                session.cache[key] = True
                await memory.on_user_message(session, m, judge=self.extract_judge)

    # ------------------------------------------------------------------ judge
    async def _judge(self, claim: Claim, cards: list[MemoryCard]) -> dict[str, Any]:
        numbered = "\n".join(f"[{i}] ({c.kind}) {c.text}" for i, c in enumerate(cards))
        if claim.code:
            body = f"CODE:\n{claim.code[:3000]}"
        else:
            # The normalized form carries the SUBJECT ("Flask was first released in 2010");
            # the quote alone may be a fragment ("first released in 2010") that the judge then
            # pinned on the wrong memory (long-chat replay: matched to Python's 1991 card).
            body = f"CLAIM: {claim.normalized[:500]}"
            if claim.quote and claim.quote.strip() != claim.normalized.strip():
                body += f"\n(original wording: {claim.quote[:300]})"
        raw = await (self.judge or complete_json)(
            JUDGE_SYSTEM, f"{body}\n\nUSER MEMORIES:\n{numbered}"
        )
        data = one_object(raw)
        if data is None:
            raise TypeError(f"judge returned {type(raw).__name__}, expected an object")
        return data

    def _to_result(self, data: dict[str, Any], cards: list[MemoryCard]) -> DetectorResult | None:
        verdict = str(data.get("verdict", "unrelated")).lower()
        idx = data.get("memory_index")
        card = cards[idx] if isinstance(idx, int) and 0 <= idx < len(cards) else None
        try:
            conf = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        explanation = str(data.get("explanation") or "").strip()

        if verdict == "contradicts" and card is not None:
            status = "contradicted" if conf >= MIN_CONTRADICT_CONFIDENCE else "uncertain"
            return self.result(
                status,
                conf,
                explanation or f"You told Claude: {card.text}",
                [self._evidence(card)],
            )
        if verdict == "consistent" and card is not None and conf >= 0.7:
            return self.result(
                "consistent",
                conf,
                explanation or "Matches what you told Claude earlier.",
                [self._evidence(card)],
            )
        return None  # unrelated / unsure → stay silent

    @staticmethod
    def _evidence(card: MemoryCard) -> Evidence:
        if card.kind in USER_KINDS:
            label = "Your rule" if card.kind == "constraint" else "Your setup"
            return Evidence(
                source=EVIDENCE_SOURCE, url=None, snippet=snippet(f"{label}: {card.text}")
            )
        label = "Verified earlier" if card.kind == "verified_fact" else "Corrected earlier"
        source = {"claim_verifier": "web/Wikipedia", "reference_auditor": "paper databases"}.get(
            card.source, "a source"
        )
        return Evidence(
            source=f"Memory ({source})", url=None, snippet=snippet(f"{label}: {card.text}")
        )


detector = MemoryConsistency()
